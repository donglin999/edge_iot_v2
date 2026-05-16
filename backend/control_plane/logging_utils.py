"""Asynchronous logging support (M6).

The acquisition pipeline logs on its hot path. A plain ``RotatingFileHandler``
does the disk write — and the rollover ``rename()`` — synchronously on the
calling thread, so a slow disk can stall an acquisition cycle.

:class:`AsyncRotatingFileHandler` decouples the two: log records are pushed
onto an in-process queue and a single background :class:`QueueListener` thread
drains the queue into a real ``RotatingFileHandler``. Callers only pay the cost
of an ``enqueue()``.

Python 3.12 added native ``QueueHandler``/``QueueListener`` wiring to
``logging.config.dictConfig``; this project runs on 3.9, so we provide a
self-contained handler that owns and starts its own listener.
"""
from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue


class AsyncRotatingFileHandler(logging.handlers.QueueHandler):
    """A ``QueueHandler`` that owns a ``QueueListener`` → ``RotatingFileHandler``.

    Configure it from ``dictConfig`` like a normal file handler — pass
    ``filename``/``maxBytes``/``backupCount`` plus an optional ``fmt``/``style``
    so the *target* handler (not this queue handler) carries the formatter.
    """

    def __init__(
        self,
        filename: str,
        maxBytes: int = 0,
        backupCount: int = 0,
        encoding: str | None = "utf-8",
        delay: bool = False,
        fmt: str | None = None,
        style: str = "{",
        queue_size: int = -1,
    ) -> None:
        log_queue: queue.Queue = queue.Queue(queue_size)
        super().__init__(log_queue)

        target = logging.handlers.RotatingFileHandler(
            filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
        )
        if fmt is not None:
            target.setFormatter(logging.Formatter(fmt, style=style))

        self._listener = logging.handlers.QueueListener(
            log_queue, target, respect_handler_level=True
        )
        self._target = target
        self._listener.start()
        # Flush the queue and close the file cleanly on interpreter shutdown.
        atexit.register(self._stop)

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Enqueue the record untouched.

        The base ``QueueHandler.prepare`` pre-formats the record (so it can be
        pickled across processes). Our queue is in-process, so we hand the raw
        record to the listener and let the *target* handler's formatter render
        it once — keeping the verbose file format intact.
        """
        return record

    def _stop(self) -> None:
        try:
            self._listener.stop()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass

    def close(self) -> None:
        self._stop()
        try:
            self._target.close()
        finally:
            super().close()
