"""Co-pilot REST endpoints (Phase 19, Step 1).

A thin HTTP layer over :class:`CopilotService`. At Step 1 — only session CRUD:
start (with a settings snapshot), state, stop. Live analysis (SSE insights) comes later.

- ``POST /api/copilot/start``          — new copilot session (body: settings + recording_session_id?).
- ``GET  /api/copilot/<id>/state``     — session snapshot.
- ``POST /api/copilot/<id>/stop``      — end the session.
- ``POST /api/copilot/move``           — move the copilot from recording A to recording B.
- ``GET  /api/copilot/availability``   — whether it is enabled + whether the local LLM is available (for the UI panel).

If ``state.copilot_service is None`` (COPILOT_ENABLED=False) — 503.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Optional

from flask import Blueprint, Response, current_app, jsonify, request

from app import state
from app.core.i18n import tr as _t
from app.db.connection import get_db_connection
from app.services import live_ask, local_llm
from app.services.copilot import export as cp_export
from app.services.copilot.service import CopilotBusyError


logger = logging.getLogger(__name__)
copilot_bp = Blueprint('copilot', __name__)


def _service_unavailable():
    return jsonify({
        'success': False,
        'error': _t('capture.cp.err.disabled'),
        'error_code': 'COPILOT_DISABLED',
    }), 503


def _require_service():
    if state.copilot_service is None:
        return _service_unavailable()
    return None


def _busy_response(e: CopilotBusyError):
    return jsonify({
        'success': False,
        'error': _t('capture.cp.err.busy'),
        'error_code': 'COPILOT_BUSY',
        'owner_session_id': e.owner_session_id,
    }), 409


def end_for_recording(recording_session_id: str) -> Optional[str]:
    """End the copilot session bound to the recording (if any): stop the background
    worker, then close the row. Soft — errors are only logged.

    Returns the id of the ended copilot session (as a string) or None if there was no active one.
    """
    # First stop the background topic tracker (Step 2), then close the session.
    if state.copilot_worker is not None:
        try:
            state.copilot_worker.stop(recording_session_id, wait=False)
        except Exception:
            logger.debug(
                "copilot_worker.stop failed for %s (best-effort, does not block stop/discard)",
                recording_session_id, exc_info=True,
            )
    if state.copilot_service is None:
        return None
    try:
        cs = state.copilot_service.get_by_recording(recording_session_id)
        if cs and cs.get('status') != 'ended':
            state.copilot_service.end(cs['id'])
            return str(cs['id'])
    except Exception as e:
        logger.warning("copilot end failed for %s: %s", recording_session_id, e)
    return None


@copilot_bp.route('/api/copilot/availability', methods=['GET'])
def availability():
    """Feature state for the UI: whether it is enabled + the local LLM status + (for the
    settings section) the matrix defaults, the Claude API status and the feedback aggregates.
    Always 200 (even if disabled) — so the panel can show the reason."""
    from app.services import text_polishing
    from app.services.copilot import config as cp_config
    enabled = state.copilot_service is not None
    ok, reason = local_llm.availability()
    payload = {
        'success': True,
        'enabled': enabled,
        'local_llm_available': ok,
        'local_llm_reason': reason,
        'local_llm_model': local_llm.LOCAL_LLM_MODEL,
        'api_available': text_polishing.is_available(),
        'defaults': cp_config.resolve_settings({}),
    }
    if enabled:
        try:
            payload['feedback'] = state.copilot_service.get_feedback_stats()
        except Exception:
            payload['feedback'] = None
    return jsonify(payload)


_LIVE_ASK_SCOPES = {'call', 'archive', 'both'}

# top_k from the request body — an upper bound against uncontrolled search over the archive
# (mirrors _MAX_TOP_K in live_ask.py, the second line of defence is there too).
_MAX_TOP_K = 20

# Speaker labels for the prompt — the same dictionary as `speaker_label` in
# `live_transcribe.py` ('mic' → 'self', 'system' → 'other').
_SPEAKER_LABELS = {'self': 'Оператор', 'other': 'Співрозмовник'}


def _live_transcript_text(session_id: Optional[str]) -> Optional[str]:
    """The session's live transcript as text (for `scope='call'/'both'`).

    Each line is marked with the speaker (`[Оператор]`/`[Співрозмовник]`) — without
    this the model cannot answer "what did the client say" (C3/acceptance).

    None — there is no session, the live worker is off in this instance, or the session has no
    active live preview (finalized/not started yet). `live_ask.ask_local`
    turns this into an understandable `reason` instead of failing.
    """
    if not session_id or state.live_transcribe_worker is None:
        return None
    if not state.live_transcribe_worker.is_active(session_id):
        return None
    segments = state.live_transcribe_worker.get_preview(session_id)
    lines = []
    for s in segments:
        text = (s.get('text') or '').strip()
        if not text:
            continue
        label = _SPEAKER_LABELS.get(s.get('speaker'), '?')
        lines.append(f"[{label}] {text}")
    return "\n".join(lines).strip() or None


def _parse_top_k(raw: Any) -> tuple[Optional[int], Optional[str]]:
    """``top_k`` from the request body → (1.._MAX_TOP_K, default 6) or (None, error).

    `int()` on `float('inf')` (Flask/JSON accepts ``Infinity``) raises
    `OverflowError` → without this check the request fell into a 500 instead of a 400.
    """
    if raw is None:
        return 6, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, _t('capture.cp.err.top_k_number')
    if not math.isfinite(raw):
        return None, _t('capture.cp.err.top_k_finite')
    if raw <= 0:
        return None, _t('capture.cp.err.top_k_positive')
    return min(int(raw), _MAX_TOP_K), None


def _active_recording_ids() -> list:
    """Live recording sessions (C4 `active_session_ids()`); fallback to
    `active_session_id` while RecordingService is old (story 04 is not merged)."""
    rs = state.recording_service
    if rs is None:
        return []
    ids_fn = getattr(rs, 'active_session_ids', None)
    if callable(ids_fn):
        return list(ids_fn() or [])
    sid = getattr(rs, 'active_session_id', None)
    return [sid] if sid else []


@copilot_bp.route('/api/copilot/live-ask', methods=['POST'])
def live_ask_endpoint():
    """C2 (`docs/specs/mcp-live-call/plan.md`): an agent question → the local
    model (Ollama, $0) → an answer from the live transcript and/or the archive.

    Body: ``{question: str, session_id: str|null, scope: 'call'|'archive'|'both',
    top_k: int=6}``. ``session_id=null`` → the active recording session is taken.

    WHY NO `_require_service()`: this is not a copilot-session step (COPILOT_ENABLED),
    but a separate read-only path of agent clarifications — it works even if the copilot
    is off, as long as Ollama and/or the archive are available (A2/non-goals of this story).
    Writes nothing to the DB, the recording session or the operator widget.

    Always 200 (except an empty question) — Ollama being unavailable or no
    context is `available: false` + `reason`, not a server error.
    """
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'success': False, 'error': _t('capture.cp.err.empty_question'),
                        'error_code': 'EMPTY_QUESTION'}), 400

    scope = data.get('scope') if data.get('scope') in _LIVE_ASK_SCOPES else 'both'
    top_k, top_k_err = _parse_top_k(data.get('top_k'))
    if top_k_err:
        return jsonify({'success': False, 'error': top_k_err,
                        'error_code': 'INVALID_TOP_K'}), 400

    # Story 08: the mode (raw during recording / generated outside it) is determined
    # ONLY by whether an active recording session exists on the server, not by the request body —
    # the agent gets no parameter with which it could force generation (non-goals).
    live_sids = _active_recording_ids()
    recording_active = bool(live_sids)

    session_id = data.get('session_id') or None
    if not session_id:
        if len(live_sids) > 1:
            return jsonify({
                'success': False,
                'error': _t('capture.cp.err.ambiguous'),
                'error_code': 'AMBIGUOUS_SESSION',
                'sessions': [{'session_id': s} for s in live_sids],
            }), 409
        if live_sids:
            session_id = live_sids[0]

    transcript_text = None
    if scope in ('call', 'both'):
        transcript_text = _live_transcript_text(session_id)

    db_path = current_app.config['DATABASE']
    result = live_ask.ask_local(db_path, question, transcript_text=transcript_text,
                                scope=scope, top_k=top_k, recording_active=recording_active)
    return jsonify({'success': True, 'session_id': session_id, **result})


@copilot_bp.route('/api/copilot/start', methods=['POST'])
def start():
    """Create a copilot session. Body (JSON, everything optional):
    ``recording_session_id``, ``mode``, ``importance``, ``api_enabled``,
    ``budget_usd``, ``category_id``.

    Response 200: ``{success, copilot_session_id, config}``.
    """
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    rec_sid = data.get('recording_session_id')
    try:
        res = state.copilot_service.start(settings=data, recording_session_id=rec_sid)
    except CopilotBusyError as e:
        return _busy_response(e)
    except Exception as e:
        logger.exception("copilot start failed: %s", e)
        return jsonify({'success': False, 'error': str(e), 'error_code': 'INTERNAL'}), 500
    return jsonify({'success': True, **res})


@copilot_bp.route('/api/copilot/move', methods=['POST'])
def move():
    """Move the copilot from recording A to the live recording B (C6).

    Body: ``{from_session_id, to_session_id, settings?}``; without ``settings``
    the config of the session on A is taken. Not atomic: A is ended first, a failed
    start on B → 200 ``COPILOT_MOVE_FAILED`` with ``ended_from: true``.
    """
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    from_sid = data.get('from_session_id')
    to_sid = data.get('to_session_id')
    if not from_sid or not to_sid:
        return jsonify({'success': False, 'error': _t('capture.cp.err.move_ids'),
                        'error_code': 'BAD_REQUEST'}), 400
    rs = state.recording_service
    is_active = getattr(rs, 'is_active', None) if rs is not None else None
    if callable(is_active) and not is_active(to_sid):
        return jsonify({'success': False, 'error': _t('capture.cp.err.move_inactive'),
                        'error_code': 'NOT_FOUND'}), 404
    prev = state.copilot_service.get_by_recording(from_sid)
    if not prev or prev.get('status') == 'ended':
        return jsonify({'success': False, 'error': _t('capture.cp.err.move_not_running'),
                        'error_code': 'NO_COPILOT'}), 404
    settings = data.get('settings')
    if settings is None:
        try:
            settings = json.loads(prev.get('config_json') or '{}')
        except (ValueError, TypeError):
            settings = {}
    end_for_recording(from_sid)
    try:
        res = state.copilot_service.start(settings=settings, recording_session_id=to_sid)
        cs_id = res.get('copilot_session_id')
        if cs_id and state.copilot_worker is not None:
            state.copilot_worker.start(
                copilot_session_id=cs_id,
                recording_session_id=to_sid,
                config=res.get('config'),
            )
    except Exception as e:
        logger.warning("copilot move %s -> %s: start failed: %s", from_sid, to_sid, e)
        reason = ('COPILOT_BUSY' if isinstance(e, CopilotBusyError)
                  else _t('capture.cp.err.move_failed'))
        return jsonify({'success': False, 'error_code': 'COPILOT_MOVE_FAILED',
                        'ended_from': True, 'reason': reason})
    return jsonify({'success': True, 'ended_from': True,
                    'copilot_session_id': cs_id, 'recording_session_id': to_sid})


@copilot_bp.route('/api/copilot/sessions', methods=['GET'])
def list_sessions():
    """List of copilot sessions (the "what the co-pilot suggested" overview, Step 7)."""
    err = _require_service()
    if err:
        return err
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        limit, offset = 50, 0
    return jsonify({'success': True,
                    'sessions': state.copilot_service.list_sessions(limit=limit, offset=offset)})


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/timeline', methods=['GET'])
def timeline(copilot_session_id: int):
    """Full historical track of a session (topics + events + aggregates) for the timeline (Step 7)."""
    err = _require_service()
    if err:
        return err
    tl = state.copilot_service.get_timeline(copilot_session_id)
    if tl is None:
        return jsonify({'success': False, 'error': _t('capture.cp.err.session_not_found'),
                        'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'timeline': tl})


def _session_comments(session: dict) -> list:
    """Operator comments of this session — from the transcript if it exists, otherwise
    from the recording session itself.

    Two addresses, because a comment moves: during the call it hangs on
    `recording_session` (no transcript yet), after transcription —
    on `transcription` (Wave 4). The export must return them the same way in both
    states, otherwise exporting a session that has not been transcribed yet would silently lose
    everything the operator dictated along the way.
    """
    try:
        from app.services import comments as comments_svc
        db = current_app.config['DATABASE']
        tid = session.get('transcription_id')
        if tid:
            rows = comments_svc.list_for(db, 'transcription', tid)
            if rows:
                return rows
        sid = session.get('recording_session_id')
        return comments_svc.list_for(db, 'recording_session', sid) if sid else []
    except Exception:
        logger.debug("[copilot] operator comments not attached to the export", exc_info=True)
        return []


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/export', methods=['GET'])
def export_session(copilot_session_id: int):
    """Session export — dialogue with timecodes + inline co-pilot notes (Step 8).
    ``?format=md`` (default) or ``json``. Serves a file for download."""
    err = _require_service()
    if err:
        return err
    tl = state.copilot_service.get_timeline(copilot_session_id)
    if tl is None:
        return jsonify({'success': False, 'error': _t('capture.cp.err.session_not_found'),
                        'error_code': 'NOT_FOUND'}), 404
    session = tl.get('session') or {}
    tr = state.copilot_service.get_transcript_brief(session.get('transcription_id'))
    cms = _session_comments(session)
    fmt = (request.args.get('format') or 'md').lower()
    if fmt == 'json':
        body = json.dumps(cp_export.build_json(tl, tr, cms), ensure_ascii=False, indent=2)
        mime, ext = 'application/json; charset=utf-8', 'json'
    else:
        body = cp_export.build_markdown(tl, tr, cms)
        mime, ext = 'text/markdown; charset=utf-8', 'md'
    fname = f"copilot-session-{copilot_session_id}.{ext}"
    return Response(body, mimetype=mime,
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/reingest', methods=['POST'])
def reingest(copilot_session_id: int):
    """Save the co-pilot hints into the archive (Step 8): a new transcriptions record
    ``source_type='copilot'`` → enrich/embed through the same pipeline → searchable in RAG.
    Response: ``{success, transcription_id, source_name}``."""
    err = _require_service()
    if err:
        return err
    tl = state.copilot_service.get_timeline(copilot_session_id)
    if tl is None:
        return jsonify({'success': False, 'error': _t('capture.cp.err.session_not_found'),
                        'error_code': 'NOT_FOUND'}), 404
    session = tl.get('session') or {}
    tr = state.copilot_service.get_transcript_brief(session.get('transcription_id'))
    digest = cp_export.notes_digest(tl, tr, _session_comments(session))
    if not digest or len(digest) < 20:
        return jsonify({'success': False, 'error': _t('capture.cp.err.no_hints'),
                        'error_code': 'EMPTY'}), 400
    name = (tr or {}).get('source_name')
    src_name = f"Ко-пілот: {name}" if name else f"Ко-пілот сесії #{copilot_session_id}"
    db_path = current_app.config['DATABASE']
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "language, category_id) VALUES ('copilot', ?, ?, 'uk', ?)",
            (src_name, digest, session.get('category_id')))
        conn.commit()
        new_tid = cur.lastrowid

    # enrich/embed in the background — the same pipeline as for a regular transcription.
    try:
        from app.services import enrichment
        if state.job_queue is not None and enrichment.any_available():
            state.job_queue.submit(
                "enrichment",
                lambda job, _t=new_tid, _d=db_path: enrichment.enrich_transcription(_d, _t),
                meta={"transcription_id": new_tid, "source": "copilot_reingest"})
    except Exception as e:
        logger.warning("copilot reingest enrich job failed: %s", e)
    return jsonify({'success': True, 'transcription_id': new_tid, 'source_name': src_name})


@copilot_bp.route('/api/copilot/by-transcription/<int:transcription_id>', methods=['GET'])
def by_transcription(transcription_id: int):
    """Find the copilot session of a transcript and return its timeline (for the tab on the
    transcript page). Always 200: ``{success, found, timeline?}``."""
    err = _require_service()
    if err:
        return err
    cs = state.copilot_service.get_by_transcription(transcription_id)
    if not cs:
        return jsonify({'success': True, 'found': False})
    tl = state.copilot_service.get_timeline(cs['id'])
    return jsonify({'success': True, 'found': tl is not None, 'timeline': tl})


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/state', methods=['GET'])
def get_state(copilot_session_id: int):
    err = _require_service()
    if err:
        return err
    snap = state.copilot_service.get_state(copilot_session_id)
    if snap is None:
        return jsonify({'success': False, 'error': _t('capture.cp.err.session_not_found'),
                        'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'state': snap})


_MODES = {'light', 'medium', 'hard'}
_IMPORTANCE = {'low', 'medium', 'high'}


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/settings', methods=['POST'])
def update_settings(copilot_session_id: int):
    """Change the session mode/importance ON THE FLY during a call (Step 6). Body:
    ``mode`` (light|medium|hard) and/or ``importance`` (low|medium|high).
    Re-resolves the matrix, updates the DB and the live worker. Response: ``{success, config}``."""
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    mode = data.get('mode') if data.get('mode') in _MODES else None
    importance = data.get('importance') if data.get('importance') in _IMPORTANCE else None
    if mode is None and importance is None:
        return jsonify({'success': False, 'error': _t('capture.cp.err.no_fields'),
                        'error_code': 'BAD_SETTINGS'}), 400
    cfg = state.copilot_service.update_settings(copilot_session_id, mode=mode,
                                                importance=importance)
    if cfg is None:
        return jsonify({'success': False, 'error': _t('capture.cp.err.session_not_found'),
                        'error_code': 'NOT_FOUND'}), 404
    if state.copilot_worker is not None:
        snap = state.copilot_service.get_state(copilot_session_id)
        rec_sid = snap.get('recording_session_id') if snap else None
        if rec_sid:
            try:
                state.copilot_worker.update_config(rec_sid, cfg)
            except Exception as e:
                logger.warning("live config update failed: %s", e)
    return jsonify({'success': True, 'config': cfg})


_OPERATOR_ACTIONS = {'dismiss', 'pin', 'unpin', 'thumbs_up', 'thumbs_down', 'escalate'}


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/action', methods=['POST'])
def action(copilot_session_id: int):
    """Operator action on an insight card (Step 4). Body:
    ``action`` (one of _OPERATOR_ACTIONS), ``event_id`` (the insight acted on),
    ``payload`` (opt.). escalate is only logged — the effect is in Step 5.

    Response 200: ``{success, event_id}`` (id of the recorded action event).
    """
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    act = data.get('action')
    if act not in _OPERATOR_ACTIONS:
        return jsonify({'success': False, 'error': _t('capture.cp.err.unknown_action'),
                        'error_code': 'BAD_ACTION'}), 400
    ev = data.get('event_id')
    ev = int(ev) if (ev is not None and str(ev).isdigit()) else None
    ts = data.get('ts_offset_sec')
    ts = float(ts) if isinstance(ts, (int, float)) else None
    try:
        eid = state.copilot_service.log_action(
            copilot_session_id, act, ref_event_id=ev, ts_offset_sec=ts,
            payload=data.get('payload') if isinstance(data.get('payload'), dict) else None)
    except Exception as e:
        logger.exception("copilot action failed: %s", e)
        return jsonify({'success': False, 'error': str(e), 'error_code': 'INTERNAL'}), 500

    # "Dig deeper" → forced escalation of the card to Claude (Step 5).
    escalated = False
    if act == 'escalate' and ev is not None and state.copilot_worker is not None:
        snap = state.copilot_service.get_state(copilot_session_id)
        rec_sid = snap.get('recording_session_id') if snap else None
        if rec_sid:
            try:
                escalated = state.copilot_worker.escalate_now(rec_sid, ev)
            except Exception as e:
                logger.warning("manual escalate failed: %s", e)
    return jsonify({'success': True, 'event_id': eid, 'escalated': escalated})


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/stop', methods=['POST'])
def stop(copilot_session_id: int):
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    tid = data.get('transcription_id')
    tid = int(tid) if (tid is not None and str(tid).isdigit()) else None
    ended = state.copilot_service.end(copilot_session_id, transcription_id=tid)
    return jsonify({'success': True, 'copilot_session_id': copilot_session_id,
                    'ended': ended})
