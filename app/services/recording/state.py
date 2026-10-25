"""Recording state shared by the services that must yield to a live call.

One definition of "is a recording in progress" (it used to be duplicated in the
transcription blueprint and in reembed) and the declared set of actions that
must not run while a call is recorded. The set is data; its first enforcer is the async
transcription route (`POST /api/transcribe/async`, `transcription_run.submit` and its job),
which refuses `transcribe` for a non-UI caller while a recording runs.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Actions that must not run during a recording (declared here, enforced by their routes).
RECORDING_BLOCKED = frozenset({
    "transcribe",
    "video_analyze:describe_local",
    "transcript_suggest_meta",
    "transcript_ai:enrich",
    "settings:copilot_disable",
})


def recording_active(*, default: bool = False) -> bool:
    """Whether at least one live recording is in progress, by the server's own sessions.

    Uses ``active_session_ids()`` (the source of ``GET /api/recordings/active``):
    ``active_session_id`` is None with two recordings and would let heavy work through.
    No service configured means no recording (a host without a recorder cannot record).
    ``default`` applies only when the probe itself fails: the existing callers keep the
    fail-open ``False``, the approval gate passes ``True`` (fail closed)."""
    try:
        from app import state
        service = getattr(state, "recording_service", None)
        if service is None:
            return False
        ids_fn = getattr(service, "active_session_ids", None)
        if callable(ids_fn):
            return bool(ids_fn())
        return bool(getattr(service, "active_session_id", None))
    except Exception:  # noqa: BLE001 - the caller chooses how a broken probe reads
        logger.debug("recording state unavailable; assuming %s", default, exc_info=True)
        return default
