"""Approval gate: one ``before_request`` in front of the destructive routes, for every caller.

A request whose endpoint is in ``_D_ROUTES`` runs its view only after the owner pressed "Yes" in
the native dialog (``app/services/approvals.py``). Order of checks (plan C4):

1. the D ceiling for the actor class (``ui`` / ``unknown``) -> 429 ``RATE_CEILING``;
2. a non-``ui`` request during a recording (probe failure counts as recording) -> 409
   ``RECORDING_ACTIVE``, no dialog; ``RECORDING_ALLOWED_ACTIONS`` (``recording_stop``,
   ``recording_cancel_own``) are the only exemption: they go on to the dialog mid-call;
3. the summary from the DB: ``None`` (the path id names no object) -> no dialog, the view
   answers its own 404; a precondition the view would refuse (``Precondition``: a direction
   with projects, the self speaker) -> no dialog and the view never runs: the gate answers with
   the view's own status and body, and gives the ceiling slot back; a request the
   summary cannot describe (malformed body, missing body object) -> 400 ``BAD_REQUEST`` without
   running the view, and its ceiling slot is given back;
4. another dialog is open -> 409 ``APPROVAL_PENDING``;
5. ``approvals.ask``: approved -> the view runs; denied -> 403; unavailable -> 503.

A *conditional* D route (``_CONDITIONAL_D_ROUTES``) is gated only for a non-``ui`` caller and
only when its predicate holds (re-transcribe that would drop a version, ask 9; a Claude
description of video frames, ask 8; every stop and discard of a recording, mcp-recording-live);
a predicate error gates the request (fail closed). Otherwise it is an ordinary call: no dialog,
no slot. A recording's dialog is built from its manifest and the live snapshot, never the
request: the agent's ``name`` reaches the dialog only as the quoted ``X-Recall-Note``.
An approval leaves the action on ``g.approved_action``.

There is no key, header, env var or setting that skips the dialog or changes the numbers.
Every outcome is left on ``g.recall_audit`` for the audit hook. The dialog text is built in the
owner's UI language (``server_lang()``), never the request's ``X-Recall-Lang``.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from urllib.parse import unquote

from flask import Flask, current_app, g, jsonify, request

from app.core import auth
from app.core.i18n import server_lang, tr
from app.db.connection import get_db_connection
from app.services import approval_summaries, approvals
from app.services.recording.state import recording_active

logger = logging.getLogger(__name__)

D_ACTIONS = ("transcript_delete", "transcript_bulk_delete", "taxonomy_delete", "taxonomy_merge",
             "speaker_delete", "speaker_merge", "enrich_backfill", "telegram_backfill",
             "settings_remove_anthropic_key", "mailbox_disconnect", "mailbox_delete", "mailbox_backfill",
             "mailbox_label_enrich", "audio_delete", "transcript_version_evict",
             "document_import_folder", "document_reparse", "video_describe_claude",
             "telegram_enable_chat",
             # mcp-recording-live
             "recording_stop", "recording_cancel_own")
# mcp-recording-live: the only D actions a non-``ui`` caller may run during a recording (still
# through the dialog, the ceilings, the one-at-a-time rule and the audit).
RECORDING_ALLOWED_ACTIONS = frozenset({"recording_stop", "recording_cancel_own"})

# Flask endpoint name -> action. A new destructive route must join this table by hand;
# tests/test_endpoints_smoke.py fails on a DELETE rule or a delete/merge/purge/backfill
# endpoint that is in neither table.
_D_ROUTES: dict[str, str] = {
    "transcription.delete_transcription": "transcript_delete",
    "transcription.bulk_delete_transcriptions": "transcript_bulk_delete",
    "memory.delete_category": "taxonomy_delete",
    "memory.merge_category": "taxonomy_merge",
    "memory.delete_tag_route": "taxonomy_delete",
    "memory.merge_tags_route": "taxonomy_merge",
    "memory.start_backfill": "enrich_backfill",
    "speakers.delete_speaker": "speaker_delete",
    "speakers.merge_speakers": "speaker_merge",
    "telegram.tg_backfill": "telegram_backfill",
    "settings_api.clear_anthropic_key": "settings_remove_anthropic_key",
    "mail.mail_accounts_delete": "mailbox_delete",
    "mail.mail_accounts_disconnect": "mailbox_disconnect",
    "mail.mail_backfill": "mailbox_backfill",
    "mail.mail_label_enrich": "mailbox_label_enrich",
    "audio_library.delete_audio_download": "audio_delete",
    "documents.import_folder": "document_import_folder",
    "documents.reparse_document": "document_reparse",
    "documents.reparse_job": "document_reparse",
    "telegram.tg_enable_chat": "telegram_enable_chat",
}



def _retranscribe_would_evict(view_args: dict) -> bool:
    from app.services import retranscribe
    return retranscribe.would_evict(view_args["transcription_id"],
                                    db_path=current_app.config["DATABASE"])


def _video_describes_with_claude(view_args: dict) -> bool:
    """Ask 8: frames would leave the machine for Anthropic. Mirrors the route: ``describe``
    defaults to "an explicit ``vision_backend`` was sent", and the backend that describes is
    ``video_vision._backend`` (the body value, else the ``VIDEO_VISION_BACKEND`` setting), so
    ``{"describe": true}`` under a Claude setting gates too. A body that is not an object
    raises (the predicate error gates the request)."""
    from app.services import video_vision
    body = request.get_json(silent=True)
    if body is None:
        return False
    if not isinstance(body, dict):
        raise ValueError("analyze-video body is not an object")
    backend = body.get("vision_backend")
    describe = bool(body["describe"]) if "describe" in body else backend is not None
    return describe and video_vision._backend(backend) == "claude"


def _telegram_real_repair(view_args: dict) -> bool:
    """A repair that would fetch messages (not a dry run). Mirrors the view's truthiness of
    ``dry_run``; a body that is not an object raises (the predicate error gates the request)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("repair body is not an object")
    return not body.get("dry_run")


def _always(view_args: dict) -> bool:
    """mcp-recording-live: every non-``ui`` stop or discard of a recording is gated."""
    return True


# Flask endpoint name -> (action, predicate over the view args). Gated for non-``ui`` callers
# when the predicate is true; the view reads ``g.evict_allowed`` / ``g.approved_action``.
_CONDITIONAL_D_ROUTES: dict[str, tuple] = {
    "transcription.retranscribe_transcription": ("transcript_version_evict", _retranscribe_would_evict),
    "transcription.analyze_video_endpoint": ("video_describe_claude", _video_describes_with_claude),
    "telegram.tg_repair": ("telegram_backfill", _telegram_real_repair),
    # mcp-recording-live: the owner's own Stop/Discard (``ui``) stay undialogued
    "recording.stop_recording": ("recording_stop", _always),
    "recording.discard_recording": ("recording_cancel_own", _always),
}

_D_EXEMPT: dict[str, str] = {
    "comments.delete": "soft delete with restore; the owner's ask does not name comments (plan A2)",
    "bookmarks.delete_bookmark": "user bookkeeping, not archive data (plan A2)",
    "bookmarks.delete_saved_search": "user bookkeeping, not archive data (plan A2)",
    "speakers.unmerge_speakers": "the undo of a gated merge; it restores, it does not destroy",
    "memory.backfill_status": "GET, read-only progress of the gated backfill",
    "telegram.tg_deleted": "the listener mirrors a deletion made in Telegram (X-Telegram-Token stream)",
    "transcription.merge_segments": "segment edit, excluded from MCP by F35 until undo exists (plan A19)",
}

D_PER_MINUTE, D_PER_DAY = 3, 20
_MINUTE, _DAY = 60.0, 86400.0
NOTE_MAX = 60

_counts: dict[str, deque] = {}
_counts_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def reset_counters() -> None:
    """Forget the D counters (tests; a restart does the same)."""
    with _counts_lock:
        _counts.clear()


def _take(actor: str) -> int | None:
    """Count one request for ``actor``; return seconds to wait when over the ceiling (not counted)."""
    now = _now()
    with _counts_lock:
        window = _counts.setdefault(actor, deque())
        while window and now - window[0] >= _DAY:
            window.popleft()
        if len(window) >= D_PER_DAY:
            return max(1, math.ceil(window[0] + _DAY - now))
        recent = [t for t in window if now - t < _MINUTE]
        if len(recent) >= D_PER_MINUTE:
            return max(1, math.ceil(recent[0] + _MINUTE - now))
        window.append(now)
        return None


def _untake(actor: str) -> None:
    """Give back the slot ``_take`` just counted (a request refused as malformed)."""
    with _counts_lock:
        window = _counts.get(actor)
        if window:
            window.pop()


def _clean_note(raw: str | None) -> str | None:
    """The quoted note: percent-decoded first (WSGI hands header values latin-1-decoded, so the
    MCP side percent-encodes UTF-8), then cleaned and cut."""
    if not raw:
        return None
    raw = unquote(raw, encoding="utf-8", errors="replace")
    text = "".join(ch for ch in raw if ch.isprintable() and ch not in "«»").strip()
    return text[:NOTE_MAX] or None


def _label(raw: str | None) -> str | None:
    if not raw:
        return None
    return "".join(ch for ch in raw if ch.isprintable() and ch not in "«»").strip()[:64] or None


def _fallback_ref() -> str:
    args = request.view_args or {}
    return f"{request.endpoint}:{next(iter(args.values()))}" if args else f"{request.endpoint}:*"


def _refuse(status: int, audit: dict, decision: str, *, error_code: str, error: str, **extra):
    audit["decision"] = decision
    g.recall_audit = audit
    return jsonify({"success": False, "error": error, "error_code": error_code, **extra}), status


def _conditional_action(actor: str) -> str | None:
    entry = _CONDITIONAL_D_ROUTES.get(request.endpoint or "")
    if entry is None or actor == "ui":
        return None
    action, predicate = entry
    g.evict_allowed = False  # a non-ui caller drops a version only after an approval
    try:
        needed = predicate(request.view_args or {})
    except Exception:
        logger.warning("approval gate: predicate failed for %s, gating", request.endpoint, exc_info=True)
        needed = True
    return action if needed else None


def _gate():
    action = _D_ROUTES.get(request.endpoint or "")
    actor = auth.classify_request()[0]
    if action is None:
        action = _conditional_action(actor)
    if action is None:
        return None
    audit = {"action": action, "object_ref": _fallback_ref(), "summary": None, "decision": None}

    wait = _take(actor)
    if wait is not None:
        return _refuse(429, audit, "rate_ceiling", error_code="RATE_CEILING",
                       error=tr("approvals.err.ceiling", seconds=wait), retry_after_sec=wait)

    if actor != "ui" and action not in RECORDING_ALLOWED_ACTIONS and recording_active(default=True):
        return _refuse(409, audit, "recording_active", error_code="RECORDING_ACTIVE",
                       error=tr("approvals.err.recording"))

    lang = server_lang()
    try:
        with get_db_connection(current_app.config["DATABASE"]) as conn:
            summary = approval_summaries.build(action, request.endpoint, conn, request.view_args or {},
                                               request.get_json(silent=True), actor=actor)
    except approval_summaries.Precondition as exc:
        _untake(actor)
        audit["decision"] = f"precondition:{exc.reason}"
        g.recall_audit = audit
        return jsonify(exc.body), exc.status
    except approval_summaries.Unsummarisable as exc:
        _untake(actor)
        return _refuse(400, audit, f"refused:{exc.reason}", error_code="BAD_REQUEST",
                       error=tr("approvals.err.malformed"), reason=exc.reason)
    except Exception:
        logger.exception("approval gate: summary failed for %s", request.endpoint)
        return _refuse(503, audit, "unavailable", error_code="APPROVAL_UNAVAILABLE",
                       error=tr("approvals.err.unavailable"), reason="summary_failed")
    if summary is None:
        audit["decision"] = "not_found"
        g.recall_audit = audit
        return None

    audit["object_ref"] = approval_summaries.object_ref(summary)
    audit["summary"] = approval_summaries.one_line(summary, lang)
    if approvals.busy():
        return _refuse(409, audit, "pending", error_code="APPROVAL_PENDING",
                       error=tr("approvals.err.pending"))

    text = approval_summaries.render(
        summary, actor=actor,
        claimed_actor=_label(request.headers.get("X-Recall-Actor")),
        claimed_tool=_label(request.headers.get("X-Recall-Tool")),
        note=_clean_note(request.headers.get("X-Recall-Note")),
        lang=lang, timeout_s=approvals.APPROVAL_TIMEOUT_S)
    decision = approvals.ask(text, timeout_s=approvals.APPROVAL_TIMEOUT_S)
    if decision.status == "approved":
        audit["decision"] = "approved"
        g.recall_audit = audit
        g.approved_action = action
        return None
    if decision.status == "denied":
        reason = decision.reason or "no"
        key = "approvals.err.timeout" if reason == "timeout" else "approvals.err.denied"
        return _refuse(403, audit, f"denied:{reason}", error_code="APPROVAL_DENIED", error=tr(key),
                       reason=reason)
    if decision.status == "pending":
        return _refuse(409, audit, "pending", error_code="APPROVAL_PENDING",
                       error=tr("approvals.err.pending"))
    return _refuse(503, audit, "unavailable", error_code="APPROVAL_UNAVAILABLE",
                   error=tr("approvals.err.unavailable"), reason=decision.reason or "messagebox_failed")


def register(app: Flask) -> None:
    """Install the gate; call after ``auth.register`` and before ``audit_hooks.register``."""

    @app.before_request
    def _recall_approval_gate():  # noqa: ANN202 - Flask hook
        return _gate()
