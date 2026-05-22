"""Run ``backend.acquisition`` tasks on the edge.

The runner owns a worker thread per assigned task. ``AcquisitionService``
itself comes straight from the center codebase (``backend.acquisition``)
— the edge does NOT keep a separate copy. The thread runs
``service.run_continuous()`` until the session row is flipped to
``stopped`` from outside (e.g. when apply_config removes the task).

Tasks emit lifecycle updates via the ``on_state`` callback the agent
supplies. The agent then wraps each event in a ``task_state`` frame and
sends it on the WS.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional


from .protocol import (
    TASK_STATE_ERROR,
    TASK_STATE_RUNNING,
    TASK_STATE_STARTING,
    TASK_STATE_STOPPED,
    TASK_STATE_STOPPING,
)

logger = logging.getLogger(__name__)


StateCallback = Callable[[int, str, str, Optional[str]], None]
# (task_id, task_code, state, error)


@dataclass
class _Worker:
    task_id: int
    task_code: str
    thread: threading.Thread
    session_id: int
    stop_flag: threading.Event


class TaskRunner:
    """Owns one OS thread per running task.

    The runner is intentionally NOT a singleton — tests construct fresh
    instances against a fake ``on_state`` and a stub ``service_factory``
    so we never have to spin a real ``AcquisitionService`` inside CI.
    """

    def __init__(
        self,
        *,
        on_state: StateCallback,
        service_factory: Optional[Callable] = None,
    ) -> None:
        self._on_state = on_state
        # Hookable for tests; the production path resolves
        # ``AcquisitionService`` lazily inside ``_run_one`` so we can avoid
        # importing the full Django backend until ``ensure_setup`` ran.
        self._service_factory = service_factory
        self._workers: Dict[int, _Worker] = {}
        self._lock = threading.RLock()

    # ---- public API -------------------------------------------------------

    def reconcile(self, desired_task_ids: list[int]) -> None:
        """Bring the running set of task threads in line with ``desired_task_ids``.

        - Tasks not currently running are started.
        - Tasks running but no longer in ``desired_task_ids`` are asked to stop.
        - Tasks already running and still desired are left untouched.
        """
        desired = set(int(tid) for tid in desired_task_ids)
        with self._lock:
            running = set(self._workers.keys())
            to_start = desired - running
            to_stop = running - desired

        for task_id in sorted(to_stop):
            self.stop(task_id)
        for task_id in sorted(to_start):
            self.start(task_id)

    def start(self, task_id: int) -> None:
        with self._lock:
            if task_id in self._workers:
                return
            try:
                task, session = self._create_session(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("TaskRunner: failed to create session for task %s", task_id)
                self._on_state(task_id, str(task_id), TASK_STATE_ERROR, str(exc))
                return

            stop_flag = threading.Event()
            thread = threading.Thread(
                target=self._run_one,
                args=(task, session, stop_flag),
                name=f"edge-task-{task.code}",
                daemon=True,
            )
            self._workers[task_id] = _Worker(
                task_id=task_id,
                task_code=task.code,
                thread=thread,
                session_id=session.id,
                stop_flag=stop_flag,
            )
            self._on_state(task_id, task.code, TASK_STATE_STARTING, None)
            thread.start()

    def stop(self, task_id: int) -> None:
        with self._lock:
            worker = self._workers.get(task_id)
            if worker is None:
                return
        # Flip the session row's status; AcquisitionService re-reads it
        # every loop iteration and exits cleanly. This is the same path
        # the center API uses for "stop task".
        worker.stop_flag.set()
        self._on_state(worker.task_id, worker.task_code, TASK_STATE_STOPPING, None)
        try:
            self._mark_session_stopped(worker.session_id)
        except Exception:  # noqa: BLE001
            logger.exception("TaskRunner: failed to flip session %s to STOPPED", worker.session_id)
        worker.thread.join(timeout=15.0)
        if worker.thread.is_alive():
            logger.warning(
                "TaskRunner: task %s thread did not exit within 15s — leaking",
                worker.task_code,
            )
        with self._lock:
            self._workers.pop(task_id, None)
        self._on_state(worker.task_id, worker.task_code, TASK_STATE_STOPPED, None)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._workers.keys())
        for tid in ids:
            self.stop(tid)

    def running_task_ids(self) -> list[int]:
        with self._lock:
            return list(self._workers.keys())

    def task_code_for(self, task_id: int) -> Optional[str]:
        """Return the ``task_code`` of a running task, or ``None``.

        Used by the sample uplink to label ``sample_batch`` frames without
        an ORM round-trip — the runner already holds the code per worker.
        """
        with self._lock:
            worker = self._workers.get(int(task_id))
            return worker.task_code if worker is not None else None

    # ---- internals --------------------------------------------------------

    def _create_session(self, task_id: int):
        """Create a fresh AcquisitionSession for the assigned task."""
        from acquisition import models as acq_models
        from configuration.models import AcqTask
        from django.utils import timezone

        task = AcqTask.objects.prefetch_related("points__device", "points__template").get(pk=task_id)
        session = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            started_at=timezone.now(),
            metadata={"runner": "edge-agent"},
        )
        return task, session

    def _run_one(self, task, session, stop_flag: threading.Event) -> None:
        """Worker-thread body. Runs the continuous acquisition loop."""
        service = None
        try:
            if self._service_factory is not None:
                service = self._service_factory(task, session)
            else:
                from acquisition.services.acquisition_service import AcquisitionService

                service = AcquisitionService(task, session)

            # The service flips status to RUNNING on entry; we emit
            # ``running`` from this side as soon as the service is ready
            # so the center sees a transition pair (starting → running)
            # before any read-loop work.
            self._on_state(task.id, task.code, TASK_STATE_RUNNING, None)

            # Watcher thread: if stop_flag fires, set the session status
            # so the service's ``_should_continue`` returns False.
            def _stop_watch() -> None:
                stop_flag.wait()
                try:
                    self._mark_session_stopped(session.id)
                except Exception:  # noqa: BLE001
                    logger.exception("TaskRunner: stop watcher could not mark session stopped")

            watcher = threading.Thread(target=_stop_watch, name=f"edge-task-{task.code}-watch", daemon=True)
            watcher.start()

            service.run_continuous()
        except Exception as exc:  # noqa: BLE001
            logger.exception("TaskRunner: task %s crashed", task.code)
            self._on_state(task.id, task.code, TASK_STATE_ERROR, str(exc))
        else:
            self._on_state(task.id, task.code, TASK_STATE_STOPPED, None)

    def _mark_session_stopped(self, session_id: int) -> None:
        from acquisition.models import AcquisitionSession
        from django.utils import timezone

        AcquisitionSession.objects.filter(pk=session_id).update(
            status=AcquisitionSession.STATUS_STOPPED,
            stopped_at=timezone.now(),
        )
