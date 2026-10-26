"""Background stop, wall-clock duration timer and the stopping registry (spec mcp-recording-live).

A stop from an agent answers at once: ``request_stop`` registers the session, runs the real stop on
a daemon thread and returns ``'started'``. A repeat while that thread runs answers ``'already'``.
``arm_timer`` stops a session after its duration limit, wall-clock, only while it is still live.

No blueprint import and no ``app.state``: the caller injects ``stop_fn`` and ``is_live``. The
timer is in memory only: a restart does not re-arm it (the boot path does not call this module).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from app.services.recording.service import SessionNotFoundError

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_stopping: dict[str, float] = {}   # sid -> monotonic time the stop started
_timers: dict[str, Any] = {}       # sid -> armed duration timer


def is_stopping(session_id: str) -> bool:
    with _lock:
        return session_id in _stopping


def request_stop(session_id: str, name: Optional[str], *, stop_fn: Callable[..., Any]) -> str:
    """``'started'`` when a stop thread was launched, ``'already'`` while one is still running."""
    with _lock:
        if session_id in _stopping:
            return 'already'
        _stopping[session_id] = time.monotonic()
    t = threading.Thread(target=_run_stop, args=(session_id, name, stop_fn),
                         name=f'rec-stop-{session_id}', daemon=True)
    t.start()
    return 'started'


def _run_stop(session_id: str, name: Optional[str], stop_fn: Callable[..., Any]) -> None:
    try:
        stop_fn(session_id, name=name)
    except SessionNotFoundError:
        logger.debug("background stop: session %s is no longer active", session_id)
    finally:
        with _lock:
            _stopping.pop(session_id, None)
        cancel_timer(session_id)


def arm_timer(session_id: str, minutes: float, *, stop_fn: Callable[..., Any],
              is_live: Callable[[str], bool], timer_factory: Callable[..., Any] = threading.Timer) -> None:
    """Stop the session after ``minutes`` of wall-clock time, if it is still live then.

    Re-arming replaces the previous timer. ``timer_factory(seconds, fn)`` is the test seam."""

    def _fire() -> None:
        with _lock:
            if _timers.get(session_id) is t:
                _timers.pop(session_id, None)
        if is_live(session_id):
            request_stop(session_id, None, stop_fn=stop_fn)

    t = timer_factory(float(minutes) * 60.0, _fire)
    t.daemon = True
    with _lock:
        old = _timers.pop(session_id, None)
        _timers[session_id] = t
    if old is not None:
        old.cancel()
    t.start()


def cancel_timer(session_id: str) -> None:
    """Disarm the session's duration timer. Idempotent."""
    with _lock:
        t = _timers.pop(session_id, None)
    if t is not None:
        t.cancel()


def reset() -> None:
    """Cancel every timer and clear both registries (tests, and the conftest fixture)."""
    with _lock:
        timers = list(_timers.values())
        _timers.clear()
        _stopping.clear()
    for t in timers:
        t.cancel()
