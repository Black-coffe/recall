"""Co-pilot REST endpoints (Phase 19, Крок 1).

Тонкий HTTP-шар над :class:`CopilotService`. На Кроці 1 — лише CRUD сесії:
старт (зі знімком налаштувань), стан, стоп. Live-аналіз (SSE-інсайти) — далі.

- ``POST /api/copilot/start``          — нова копілот-сесія (body: settings + recording_session_id?).
- ``GET  /api/copilot/<id>/state``     — snapshot сесії.
- ``POST /api/copilot/<id>/stop``      — завершити сесію.
- ``GET  /api/copilot/availability``   — чи увімкнено + чи доступний локальний LLM (для UI-панелі).

Якщо ``state.copilot_service is None`` (COPILOT_ENABLED=False) — 503.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Optional

from flask import Blueprint, Response, current_app, jsonify, request

from app import state
from app.db.connection import get_db_connection
from app.services import live_ask, local_llm
from app.services.copilot import export as cp_export


logger = logging.getLogger(__name__)
copilot_bp = Blueprint('copilot', __name__)


def _service_unavailable():
    return jsonify({
        'success': False,
        'error': 'Ко-пілот вимкнено (COPILOT_ENABLED=False)',
        'error_code': 'COPILOT_DISABLED',
    }), 503


def _require_service():
    if state.copilot_service is None:
        return _service_unavailable()
    return None


@copilot_bp.route('/api/copilot/availability', methods=['GET'])
def availability():
    """Стан фічі для UI: чи увімкнено + статус локального LLM + (для розділу
    налаштувань) дефолти матриці, статус Claude-API та агрегати фідбеку.
    Завжди 200 (навіть якщо вимкнено) — щоб панель могла показати причину."""
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

# top_k із тіла запиту — верхня межа проти неконтрольованого пошуку по архіву
# (дзеркалить _MAX_TOP_K у live_ask.py, другий рубіж там же).
_MAX_TOP_K = 20

# Мітки спікера для промпта — той самий словник, що й `speaker_label` у
# `live_transcribe.py` ('mic' → 'self', 'system' → 'other').
_SPEAKER_LABELS = {'self': 'Оператор', 'other': 'Співрозмовник'}


def _live_transcript_text(session_id: Optional[str]) -> Optional[str]:
    """Живий транскрипт сесії текстом (для `scope='call'/'both'`).

    Кожен рядок позначений спікером (`[Оператор]`/`[Співрозмовник]`) — без
    цього модель не може відповісти на «що сказав клієнт» (C3/приймання).

    None — немає сесії, живий воркер вимкнений в інстансі, або сесія без
    активного live-прев'ю (finalized/ще не стартувала). `live_ask.ask_local`
    перетворює це у зрозумілий `reason`, а не падає.
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
    """``top_k`` з тіла запиту → (1.._MAX_TOP_K, дефолт 6) або (None, помилка).

    `int()` на `float('inf')` (Flask/JSON приймає ``Infinity``) кидає
    `OverflowError` → без цієї перевірки запит падав у 500 замість 400.
    """
    if raw is None:
        return 6, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "top_k має бути числом"
    if not math.isfinite(raw):
        return None, "top_k має бути скінченним числом"
    if raw <= 0:
        return None, "top_k має бути додатним"
    return min(int(raw), _MAX_TOP_K), None


@copilot_bp.route('/api/copilot/live-ask', methods=['POST'])
def live_ask_endpoint():
    """C2 (`docs/specs/mcp-live-call/plan.md`): питання агента → локальна
    модель (Ollama, $0) → відповідь з живого транскрипту і/або архіву.

    Body: ``{question: str, session_id: str|null, scope: 'call'|'archive'|'both',
    top_k: int=6}``. ``session_id=null`` → береться активна сесія запису.

    ЧОМУ БЕЗ `_require_service()`: це не крок ко-пілот-сесії (COPILOT_ENABLED),
    а окремий read-only шлях уточнень агента — працює навіть якщо ко-пілот
    вимкнено, доки доступні Ollama і/або архів (A2/non-goals цієї історії).
    Нічого не пише в БД, в сесію запису чи у віджет оператора.

    Завжди 200 (крім порожнього питання) — недоступність Ollama чи відсутність
    контексту це `available: false` + `reason`, не помилка сервера.
    """
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'success': False, 'error': 'Порожнє питання',
                        'error_code': 'EMPTY_QUESTION'}), 400

    scope = data.get('scope') if data.get('scope') in _LIVE_ASK_SCOPES else 'both'
    top_k, top_k_err = _parse_top_k(data.get('top_k'))
    if top_k_err:
        return jsonify({'success': False, 'error': top_k_err,
                        'error_code': 'INVALID_TOP_K'}), 400

    # Історія 08: режим (raw під час запису / generated поза ним) визначається
    # ЛИШЕ наявністю активної сесії запису на сервері, а не тілом запиту —
    # агент не отримує параметра, яким міг би форсувати генерацію (non-goals).
    recording_active = bool(
        state.recording_service is not None
        and state.recording_service.active_session_id
    )

    session_id = data.get('session_id') or None
    if not session_id and state.recording_service is not None:
        session_id = state.recording_service.active_session_id

    transcript_text = None
    if scope in ('call', 'both'):
        transcript_text = _live_transcript_text(session_id)

    db_path = current_app.config['DATABASE']
    result = live_ask.ask_local(db_path, question, transcript_text=transcript_text,
                                scope=scope, top_k=top_k, recording_active=recording_active)
    return jsonify({'success': True, 'session_id': session_id, **result})


@copilot_bp.route('/api/copilot/start', methods=['POST'])
def start():
    """Створити копілот-сесію. Body (JSON, усе опціональне):
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
    except Exception as e:
        logger.exception("copilot start failed: %s", e)
        return jsonify({'success': False, 'error': str(e), 'error_code': 'INTERNAL'}), 500
    return jsonify({'success': True, **res})


@copilot_bp.route('/api/copilot/sessions', methods=['GET'])
def list_sessions():
    """Список копілот-сесій (огляд «що підказував ко-пілот», Крок 7)."""
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
    """Повна історична доріжка сесії (теми + події + агрегати) для таймлайну (Крок 7)."""
    err = _require_service()
    if err:
        return err
    tl = state.copilot_service.get_timeline(copilot_session_id)
    if tl is None:
        return jsonify({'success': False, 'error': 'Сесію не знайдено',
                        'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'timeline': tl})


def _session_comments(session: dict) -> list:
    """Коментарі оператора цієї сесії — з транскрипту, якщо він уже є, інакше
    з самої сесії запису.

    Дві адреси, бо коментар переїжджає: під час дзвінка він висить на
    `recording_session` (транскрипту ще немає), після транскрибування —
    на `transcription` (Волна 4). Експорт має віддавати їх однаково в обох
    станах, інакше вигрузка сесії, яку ще не транскрибували, мовчки губила б
    усе, що оператор надиктував по ходу.
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
        logger.debug("[copilot] коментарі до експорту не долучено", exc_info=True)
        return []


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/export', methods=['GET'])
def export_session(copilot_session_id: int):
    """Експорт сесії — діалог з таймкодами + інлайн-нотатки ко-пілота (Крок 8).
    ``?format=md`` (дефолт) або ``json``. Віддає файл на завантаження."""
    err = _require_service()
    if err:
        return err
    tl = state.copilot_service.get_timeline(copilot_session_id)
    if tl is None:
        return jsonify({'success': False, 'error': 'Сесію не знайдено',
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
    """Зберегти підказки ко-пілота в архів (Крок 8): новий transcriptions-запис
    ``source_type='copilot'`` → enrich/embed тим самим пайплайном → шукабельно у RAG.
    Response: ``{success, transcription_id, source_name}``."""
    err = _require_service()
    if err:
        return err
    tl = state.copilot_service.get_timeline(copilot_session_id)
    if tl is None:
        return jsonify({'success': False, 'error': 'Сесію не знайдено',
                        'error_code': 'NOT_FOUND'}), 404
    session = tl.get('session') or {}
    tr = state.copilot_service.get_transcript_brief(session.get('transcription_id'))
    digest = cp_export.notes_digest(tl, tr, _session_comments(session))
    if not digest or len(digest) < 20:
        return jsonify({'success': False, 'error': 'Нема підказок для збереження',
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

    # enrich/embed у фоні — той самий пайплайн, що й для звичайної транскрипції.
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
    """Знайти копілот-сесію транскрипту і повернути її таймлайн (для вкладки на
    сторінці транскрипту). Завжди 200: ``{success, found, timeline?}``."""
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
        return jsonify({'success': False, 'error': 'Сесію не знайдено',
                        'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'state': snap})


_MODES = {'light', 'medium', 'hard'}
_IMPORTANCE = {'low', 'medium', 'high'}


@copilot_bp.route('/api/copilot/<int:copilot_session_id>/settings', methods=['POST'])
def update_settings(copilot_session_id: int):
    """Змінити режим/важливість сесії НА ЛЬОТУ під час дзвінка (Крок 6). Body:
    ``mode`` (light|medium|hard) та/або ``importance`` (low|medium|high).
    Перерезолвлює матрицю, оновлює БД і живий воркер. Response: ``{success, config}``."""
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    mode = data.get('mode') if data.get('mode') in _MODES else None
    importance = data.get('importance') if data.get('importance') in _IMPORTANCE else None
    if mode is None and importance is None:
        return jsonify({'success': False, 'error': 'Нема валідних полів',
                        'error_code': 'BAD_SETTINGS'}), 400
    cfg = state.copilot_service.update_settings(copilot_session_id, mode=mode,
                                                importance=importance)
    if cfg is None:
        return jsonify({'success': False, 'error': 'Сесію не знайдено',
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
    """Дія оператора над карткою-інсайтом (Крок 4). Body:
    ``action`` (один з _OPERATOR_ACTIONS), ``event_id`` (інсайт, над яким діяли),
    ``payload`` (опц.). escalate лише логуються — ефект у Кроці 5.

    Response 200: ``{success, event_id}`` (id записаної події дії).
    """
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    act = data.get('action')
    if act not in _OPERATOR_ACTIONS:
        return jsonify({'success': False, 'error': 'Невідома дія',
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

    # «Копнути глибше» → форсована ескалація картки в Claude (Крок 5).
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
