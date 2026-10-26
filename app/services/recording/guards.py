"""Who may drive a recording session (mcp-recording-live, ADR-051).

Pure rules: no Flask, no ``app.state``. The blueprint, the approval summaries and
the copilot routes feed them what they read and act on the answer.

``started_by`` is written once into the manifest at creation from a server-side
proof (the per-boot UI token or the K2 toast token). The ``X-Recall-Actor`` header
is never a proof: it only picks the label inside the agent class.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# The owner's own sessions: UI, the K2 call-prompt toast, a future auto-start (Q3).
OWNER_CLASS = frozenset({'ui', 'call_prompt', 'auto_call'})
# Labels an agent may claim; anything else becomes 'unknown' (still agent class).
AGENT_LABELS = frozenset({'mcp', 'hook'})
# An agent may cancel its own live session only this young.
CANCEL_OWN_WINDOW_SEC = 300


def started_by_for(actor_class: str, claimed_label: Optional[str]) -> str:
    """``'ui'`` for the UI-token caller; otherwise the claimed label if it is a known
    agent label, else ``'unknown'``."""
    if actor_class == 'ui':
        return 'ui'
    return claimed_label if claimed_label in AGENT_LABELS else 'unknown'


def is_owner_session(manifest: Optional[dict]) -> bool:
    """True for an owner-class session. A missing manifest or field is owner class:
    sessions created before this field existed were all started by the owner."""
    if not manifest:
        return True
    return (manifest.get('started_by') or 'ui') in OWNER_CLASS


def cancel_own_check(manifest: Optional[dict], is_active: bool, elapsed_sec: float) -> Optional[str]:
    """Why an agent may not cancel this session, or ``None`` when it may.

    Precedence: ``'owner'`` (owner-class session) → ``'not_live'`` → ``'too_old'``
    (``elapsed_sec >= CANCEL_OWN_WINDOW_SEC``)."""
    if is_owner_session(manifest):
        return 'owner'
    if not is_active:
        return 'not_live'
    if float(elapsed_sec or 0.0) >= CANCEL_OWN_WINDOW_SEC:
        return 'too_old'
    return None


def recording_kept(session_id: str, store, autosave, db_path: Optional[str]) -> bool:
    """True when discarding would destroy something already saved: the autosave is
    running or done, or a live transcript points at the recording's library file.

    ``store`` is accepted for the callers' symmetry and not read today."""
    if autosave is not None and autosave.info(session_id).get('status') in ('running', 'done'):
        return True
    if not db_path:
        return False   # no archive configured: nothing a discard could orphan
    from app.db.connection import get_db_connection
    try:
        with get_db_connection(db_path) as conn:
            row = conn.execute(
                'SELECT 1 FROM audio_downloads a JOIN transcriptions t ON t.file_path = a.file_path '
                'WHERE (a.recording_session_id = ? OR a.youtube_id = ?) AND t.deleted_at IS NULL LIMIT 1',
                (session_id, f'recording_{session_id}'),
            ).fetchone()
    except Exception:
        logger.warning("discard guard: transcript lookup for %s failed; refusing", session_id, exc_info=True)
        return True
    return row is not None
