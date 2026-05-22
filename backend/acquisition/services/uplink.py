"""Optional uplink hook for distributed (edge) deployments.

In a monolith deployment nothing registers a hook here, so every ``emit_*``
call is a cheap no-op and the acquisition pipeline behaves exactly as
before. On an edge gateway the ``edge-agent`` process registers a callback
at startup; the pipeline's :class:`~acquisition.services.sinks.WebSocketSink`
then forwards each 1 Hz aggregated sample window to it, and the agent
relays it to the center over the WS control plane (protocol v0.3
``sample_batch``).

Keeping the hook here — rather than wiring the edge-agent directly into the
sink — means the acquisition code has zero edge/center awareness: it just
offers an extension point. The hook is process-global because the
edge-agent runs the pipeline in-process; a single registration covers
every task thread.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SampleWindow:
    """One aggregation window of point values, ready for ``sample_batch``.

    ``samples`` items are ``{point_code, value, quality, timestamp}`` dicts
    — exactly the shape :class:`WebSocketSink` already builds for its local
    Channels broadcast, so forwarding costs nothing extra to assemble.
    """

    session_id: int
    task_id: Optional[int]
    window_start: str
    window_end: str
    samples: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AlarmEvent:
    """One locally-triggered alarm, ready for an ``alarm_event`` uplink frame.

    Built by :class:`~acquisition.services.sinks.AlarmSink` from the
    :class:`~acquisition.models.Alarm` row that :func:`evaluate_readings`
    just created, so the edge-agent can relay it to the center without a
    second ORM read. ``rule_id`` is the center-side ``AlarmRule.pk`` (rules
    are mirrored to the edge under the same pk via ``apply_config``).
    """

    rule_id: int
    point_code: str
    device_code: str
    value: Any
    severity: str = "warning"
    status: str = "firing"
    message: str = ""
    fired_at: Optional[str] = None


SampleHook = Callable[[SampleWindow], None]
AlarmHook = Callable[[AlarmEvent], None]

_lock = threading.Lock()
_sample_hook: Optional[SampleHook] = None
_alarm_hook: Optional[AlarmHook] = None


def register_sample_hook(hook: SampleHook) -> None:
    """Install the process-global sample-window callback.

    Replaces any previous hook. Safe to call before or after pipelines
    start — the WebSocketSink reads the current hook on every broadcast.
    """
    global _sample_hook
    with _lock:
        _sample_hook = hook
    logger.info("acquisition uplink: sample hook registered (%r)", hook)


def clear_sample_hook() -> None:
    """Remove the sample-window callback — emits become no-ops again."""
    global _sample_hook
    with _lock:
        _sample_hook = None


def has_sample_hook() -> bool:
    with _lock:
        return _sample_hook is not None


def emit_sample_window(window: SampleWindow) -> None:
    """Forward one aggregated window to the registered hook, if any.

    A missing hook (monolith deployment) is the common case and returns
    immediately. Any error raised by the hook is swallowed and logged —
    the uplink is best-effort and MUST NOT break local acquisition.
    """
    with _lock:
        hook = _sample_hook
    if hook is None:
        return
    try:
        hook(window)
    except Exception:  # noqa: BLE001
        logger.exception("acquisition uplink: sample hook raised — dropping window")


# --- alarm uplink (M4) ------------------------------------------------------


def register_alarm_hook(hook: AlarmHook) -> None:
    """Install the process-global alarm-event callback (edge deployments).

    Replaces any previous hook. The :class:`AlarmSink` calls
    :func:`emit_alarm` for every alarm :func:`evaluate_readings` fires; on
    an edge gateway the edge-agent registers a hook here that relays the
    event to the center as a v0.4 ``alarm_event`` frame. In a monolith
    nothing registers, so :func:`emit_alarm` stays a cheap no-op.
    """
    global _alarm_hook
    with _lock:
        _alarm_hook = hook
    logger.info("acquisition uplink: alarm hook registered (%r)", hook)


def clear_alarm_hook() -> None:
    """Remove the alarm-event callback — emits become no-ops again."""
    global _alarm_hook
    with _lock:
        _alarm_hook = None


def has_alarm_hook() -> bool:
    with _lock:
        return _alarm_hook is not None


def emit_alarm(event: AlarmEvent) -> None:
    """Forward one triggered alarm to the registered hook, if any.

    A missing hook (monolith deployment) is the common case and returns
    immediately. Any error raised by the hook is swallowed and logged —
    the uplink is best-effort and MUST NOT break local alarm evaluation.
    """
    with _lock:
        hook = _alarm_hook
    if hook is None:
        return
    try:
        hook(event)
    except Exception:  # noqa: BLE001
        logger.exception("acquisition uplink: alarm hook raised — dropping event")
