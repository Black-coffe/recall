"""Ingest повідомлень з реального Telegram-акаунта (Phase 17B).

Архітектура: слухач (telegram_listener.py) — ОКРЕМИЙ процес (Telethon, asyncio),
бо сесію Telethon (SQLite) не відкрити двома клієнтами, а Flask у debug ще й
перезавантажується. Слухач ТОНКИЙ: ловить подію, СКАЧУЄ медіа на диск і POST'ить
сюди по localhost. Уся важка робота (parse/OCR/транскрипція/embeddings/Claude)
лишається у Flask-процесі, де моделі вже завантажені (один інстанс на GPU —
без другої копії whisper/e5-large у пам'яті відеокарти).

POST /api/telegram/ingest  (тільки з localhost)
  Body JSON: {kind, text?, caption?, file_path?, chat_id, chat_title, sender,
              message_id, date, link}
  Маршрутизація за kind:
    text                 → transcript_text = text                  (sync)
    photo | document     → document_parser.parse_document (OCR/parse) (sync)
    voice | audio | video→ фоновий job: whisper → persist → enrich   (queued)
  Усі записи: source_type='telegram', tg_* провенанс, дедуп (chat_id,message_id),
  category_id успадковується з tg_monitored_chats. Далі — той самий enrichment
  (chunk+embed + Claude-картка), що й для аудіо/документів → запис одразу
  повноцінний учасник RAG-пошуку, графа сутностей і напрямків.

POST /api/telegram/chats/toggle  (T2.6, тільки з localhost + shared-secret)
  CLI-only twin до /api/telegram/chats: `telegram_listener.py enable/disable`
  проксіює сюди замість прямого SQL-запису в окремому процесі — app.py
  лишається єдиним писарем у SQLite (див. tg_chats_toggle нижче).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.utils.paths import safe_path_within
from telegram_common import CONTROL_TOKEN_HEADER, control_token


logger = logging.getLogger(__name__)
telegram_bp = Blueprint('telegram', __name__)

# kind → людський підпис для source_name коли немає тексту.
_KIND_LABEL = {
    "text": "повідомлення", "photo": "фото", "voice": "голосове",
    "audio": "аудіо", "video": "відео", "document": "документ",
}
_MEDIA_KINDS = {"voice", "audio", "video"}      # потребують whisper (фоновий job)
_PARSE_KINDS = {"photo", "document"}            # document_parser (sync)


def _db_path() -> str:
    return current_app.config['DATABASE']


def _conn(db_path: str):
    from app.db.connection import get_db_connection
    return get_db_connection(db_path)


def _is_localhost() -> bool:
    return (request.remote_addr or "") in ("127.0.0.1", "::1", "localhost")


def _authorized() -> bool:
    """Ingest захищений localhost + shared-secret (IP-перевірка спуфабельна,
    інший локальний процес не має токена). Слухач шле той самий токен."""
    if not _is_localhost():
        return False
    return request.headers.get(CONTROL_TOKEN_HEADER) == control_token()


def _listener_call(path: str, method: str = "GET", body=None, timeout: int = 60):
    """Виклик control-API слухача (Flask→listener). Returns (ok, data).
    ok=False якщо слухач вимкнений/недоступний — UI покаже «оффлайн»."""
    cfg = current_app.config
    host = cfg.get('TELEGRAM_CONTROL_HOST', '127.0.0.1')
    port = cfg.get('TELEGRAM_CONTROL_PORT', 5051)
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", CONTROL_TOKEN_HEADER: control_token()},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads((r.read().decode("utf-8") or "{}"))
    except urllib.error.URLError as e:
        return False, {"error": "listener offline", "detail": str(getattr(e, "reason", e))}
    except Exception as e:
        return False, {"error": str(e)}


def _safe_media_path(file_path: str | None) -> str | None:
    """Confine file_path під TELEGRAM_MEDIA_DIR. Ingest приймає шлях від клієнта
    (слухач на тій самій машині), але інший локальний процес міг би підсунути
    БУДЬ-ЯКИЙ файл диску (C:\\Windows\\…, чужі документи) → Recall прочитав би й
    проіндексував його вміст у RAG = arbitrary file read. Тому пускаємо лише
    файли з telegram_media/ — єдиного каталогу, куди пише слухач.
    Повертає realpath якщо валідний, інакше None (як path-traversal guard у
    documents._save_original)."""
    if not file_path:
        return None
    media_root = current_app.config.get('TELEGRAM_MEDIA_DIR', 'telegram_media')
    return safe_path_within(media_root, file_path)


def _resolve_category(db_path: str, chat_id: int):
    """category_id з tg_monitored_chats для цього чату (у який напрямок класти)."""
    if chat_id is None:
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT category_id FROM tg_monitored_chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["category_id"] if row and row["category_id"] is not None else None


def _is_duplicate(db_path: str, chat_id, message_id) -> int | None:
    """Існуючий запис цього повідомлення (real-time ↔ backfill перетин). tx_id або None."""
    if chat_id is None or message_id is None:
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM transcriptions WHERE tg_chat_id = ? AND tg_message_id = ?",
            (chat_id, message_id),
        ).fetchone()
    return row["id"] if row else None


def _meeting_date(iso_date: str | None) -> str | None:
    """День розмови (локальний) з ISO-дати повідомлення — для meeting_date.

    Без цього поля весь стек датує запис через
    COALESCE(meeting_date, substr(created_at,1,10)), тобто моментом інжесту:
    recency-буст (retrieval.py) вважає лютневу переписку свіжою, а parse_due
    (commitments.py) розгортає «до пʼятниці» відносно дня завантаження. На
    живих даних розбіжність була у 53% TG-записів, у 1067 — більш ніж на місяць.

    localtime, бо Telegram віддає UTC, а нас цікавить день, коли йшла розмова
    (повідомлення після 21:00 UTC інакше поїхало б на добу назад)."""
    if not iso_date:
        return None
    try:
        dt = datetime.fromisoformat(iso_date)
    except (TypeError, ValueError):
        logger.debug("[tg] не розібрав дату %r — meeting_date лишається порожнім", iso_date)
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date().isoformat()


def _source_name(prov: dict, text: str, kind: str) -> str:
    """Лейбл для Історії: [TG] чат: сніпет (або тип медіа)."""
    chat = prov.get("chat_title") or f"chat {prov.get('chat_id')}"
    snippet = " ".join((text or "").split())[:60]
    if not snippet:
        snippet = _KIND_LABEL.get(kind, kind)
    return f"[TG] {chat}: {snippet}"


def _persist(db_path: str, *, kind: str, text: str, prov: dict, category_id,
             file_path: str | None, doc_type: str | None,
             segments_json: str | None, model_used: str | None,
             processing_time: float, structure_json: str | None = None) -> int:
    """INSERT TG-повідомлення у transcriptions. Повертає transcription_id.
    Не залежить від request context (викликається і з фонового job).

    Гонку з паралельним інжестом того самого повідомлення (real-time проти
    ремонту/догонки) ловимо на UNIQUE-індексі idx_tx_tg_msg, а не перевіркою
    заздалегідь: між `_is_duplicate` і INSERT є вікно, і саме в нього раніше
    прилітав IntegrityError, який валив фоновий job разом із повідомленням."""
    with _conn(db_path) as conn:
        c = conn.cursor()
        try:
            c.execute(
                '''INSERT INTO transcriptions
                   (source_type, source_name, file_path, transcript_text, language,
                    model_used, processing_time, segments, category_id,
                    doc_type, structure_json, meeting_date,
                    tg_chat_id, tg_chat_title, tg_sender, tg_message_id, tg_date, tg_link,
                    tg_sender_id, tg_reply_to, tg_grouped_id, tg_edit_date)
                   VALUES ('telegram', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?)''',
                (
                    _source_name(prov, text, kind), file_path, text,
                    model_used, processing_time, segments_json, category_id,
                    doc_type, structure_json, _meeting_date(prov.get("date")),
                    prov.get("chat_id"), prov.get("chat_title"), prov.get("sender"),
                    prov.get("message_id"), prov.get("date"), prov.get("link"),
                    prov.get("sender_id"), prov.get("reply_to"),
                    prov.get("grouped_id"), prov.get("edit_date"),
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT id FROM transcriptions WHERE tg_chat_id = ? AND tg_message_id = ?",
                (prov.get("chat_id"), prov.get("message_id"))).fetchone()
            if existing:
                logger.info("[tg] гонка на (chat=%s msg=%s) — беру наявний tx=%d",
                            prov.get("chat_id"), prov.get("message_id"), existing["id"])
                return existing["id"]
            raise
        tid = c.lastrowid
        conn.commit()
    return tid


def _finalize_media(db_path: str, tid: int, *, kind: str, text: str, prov: dict,
                    segments_json: str | None, model_used: str | None,
                    processing_time: float) -> None:
    """Дописати результат розпізнавання у ЗАЗДАЛЕГІДЬ створений рядок.

    Рядок для медіа створюється синхронно на інжесті (заглушкою), а не в кінці
    фонового job'а. Інакше повідомлення «в польоті» невидиме: `_is_duplicate`
    його не бачить (два whisper-прогони на одному голосовому + IntegrityError),
    watermark догонки його не враховує, а якщо app.py впаде посеред черги —
    job_queue.recover_crashed лише позначить job 'crashed', НЕ перезапустить,
    і повідомлення зникне без сліду."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE transcriptions SET transcript_text = ?, source_name = ?, "
            "segments = ?, model_used = ?, processing_time = ? WHERE id = ?",
            (text, _source_name(prov, text, kind), segments_json,
             model_used, processing_time, tid),
        )
        conn.commit()


def _parse_media(file_path: str, caption: str, kind: str):
    """photo/document → document_parser (OCR для фото). М'яка деградація: якщо
    парсер недоступний/впав — лишаємо caption (повідомлення не губиться).
    Returns (text, doc_type, structure_json)."""
    from app.services import document_parser
    caption = (caption or "").strip()
    try:
        parsed = document_parser.parse_document(file_path, filename=os.path.basename(file_path))
        body = (parsed.get("text") or "").strip()
        text = f"{caption}\n\n{body}".strip() if caption else body
        blocks = parsed.get("blocks")
        structure = json.dumps(blocks, ensure_ascii=False) if blocks else None
        return text, parsed.get("doc_type"), structure
    except Exception as e:
        logger.info("[tg] парсинг %s не вдався (%s) — лишаю caption", kind, e)
        fallback = caption or f"[{_KIND_LABEL.get(kind, kind)} без тексту]"
        return fallback, kind, None


def _link_entities(db_path: str, transcription_id: int) -> None:
    """Звʼязати повідомлення з графом сутностей (Волна 4.5.3).

    Без цього кроку граф поповнювався лише ручним прогоном CLI, тобто кожне
    нове повідомлення лишалось поза зрізом по проєкту й поза `list_stale_topics`
    до наступного запуску вручну. Робота локальна й дешева (пошук написань у
    тексті), моделі не потребує.
    """
    try:
        from app.services import tg_entities
        tg_entities.link_message(db_path, transcription_id)
    except Exception as exc:
        # Граф — надбудова над записом, як і нитка: повідомлення вже в архіві
        # й знаходиться пошуком. Ковтаємо, але з логом — мовчазний збій тут
        # читався б як «сутностей у цьому чаті просто нема».
        logger.warning("[tg] сутності для tx=%s не звʼязано: %s", transcription_id, exc)


def _submit_embed_only(transcription_id: int, db_path: str, force: bool = False) -> str:
    """Phase 17: для Telegram ставимо ТІЛЬКИ локальні embeddings — БЕЗ Claude-картки.
    Чат-повідомлення короткі: summary/сутності на однорядковику малокорисні, а на
    тисячах backfill-повідомлень палять платний Claude API. Пошук/RAG працюють на
    embeddings (локально, безкоштовно). Idempotent (embedded_at). db_path явно —
    job_queue-воркер біжить поза app context."""
    try:
        from app.services import embeddings
        if not (embeddings.is_available() and state.job_queue is not None):
            # Звʼязок із графом не потребує ані векторів, ані черги — інакше
            # вимкнені embeddings мовчки лишали б повідомлення поза зрізами.
            _link_entities(db_path, transcription_id)
            return "unavailable"

        def _job(job, _tid=transcription_id, _db=db_path, _force=force):
            try:
                res = embeddings.chunk_and_embed_transcription(_db, _tid, force=_force)
                # Нитка (Волна 4.5) — ПІСЛЯ ембедингу: центроїд нитки будується з
                # векторів її повідомлень, а до чанкінгу вектора ще немає. Рішення
                # тут попереднє ('pending') і без моделі; остаточно сплеск розкладає
                # tg_threads.resettle_pending, коли розмова стихне.
                try:
                    from app.services import tg_threads
                    tg_threads.assign_incoming(_db, _tid)
                except Exception as exc:
                    # Нитка — надбудова: без неї пошук працює як у Волні 4, тож
                    # збій тут не має валити ембединг самого повідомлення.
                    logger.warning("[tg] нитку для tx=%s не визначено: %s", _tid, exc)
                return res
            finally:
                # У `finally`, а не після ембедингу: якщо чанкінг КИНЕ (OOM на
                # відеокарті, збій завантаження моделі), job піде у 'failed' і
                # більше не повториться (`job_queue` не має ретраю) — а
                # повідомлення лишиться поза графом назавжди, тобто рівно та
                # тиха діра, заради якої цей виклик тут і зʼявився. Звʼязок із
                # графом не потребує ані векторів, ані успіху ембедингу.
                _link_entities(_db, _tid)

        state.job_queue.submit("tg_embed", _job, meta={"transcription_id": transcription_id})
        return "queued"
    except Exception as e:
        logger.warning("TG embed job не поставлено (продовжую): %s", e)
        _link_entities(db_path, transcription_id)   # див. гілку 'unavailable'
        return "error"


def _finish(db_path: str, tid: int, prov: dict):
    """Спільний хвіст: лог + embed-only job (без Claude — див. _submit_embed_only)."""
    enrich = _submit_embed_only(tid, db_path)
    logger.info("[tg] %s → tx=%d (chat=%s msg=%s) embed=%s",
                prov.get("kind"), tid, prov.get("chat_id"), prov.get("message_id"), enrich)
    return enrich


@telegram_bp.route('/api/telegram/ingest', methods=['POST'])
def ingest():
    if not _authorized():
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "text").lower()
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    caption = data.get("caption") or ""
    text = (data.get("text") or "").strip()
    file_path = data.get("file_path")

    prov = {
        "kind": kind,
        "chat_id": chat_id,
        "chat_title": data.get("chat_title"),
        "sender": data.get("sender"),
        "message_id": message_id,
        "date": data.get("date"),
        "link": data.get("link"),
        # Волна 4 — сигнали від Telegram (нитка, автор, альбом, правка).
        "sender_id": data.get("sender_id"),
        "reply_to": data.get("reply_to"),
        "grouped_id": data.get("grouped_id"),
        "edit_date": data.get("edit_date"),
    }

    db_path = _db_path()

    # --- Дедуп: real-time і backfill можуть перетнутись по одному message_id ---
    dup = _is_duplicate(db_path, chat_id, message_id)
    if dup:
        return jsonify({"success": True, "duplicate": True, "transcription_id": dup})

    category_id = _resolve_category(db_path, chat_id)

    # --- Медіа з whisper → фоновий job (не блокуємо слухач) ---
    if kind in _MEDIA_KINDS:
        file_path = _safe_media_path(file_path)
        if not file_path or not os.path.isfile(file_path):
            return jsonify({"success": False,
                            "error": "file_path відсутній або поза telegram_media/"}), 400
        if state.job_queue is None:
            return jsonify({"success": False, "error": "Черга завдань недоступна"}), 503

        # current_app валідний ТУТ (request context) — резолвимо моделі тепер і
        # передаємо у job явно: воркер job_queue біжить у фоновому потоці БЕЗ
        # app context (як і enrichment-job у documents.py).
        cfg = current_app.config
        model = cfg.get('TELEGRAM_WHISPER_MODEL', 'large-v3-turbo')
        language = cfg.get('TELEGRAM_WHISPER_LANG', 'uk')

        # Рядок створюємо ЗАРАЗ, заглушкою: доки його немає, повідомлення
        # невидиме для дедупу і для watermark догонки (див. _finalize_media).
        placeholder = (caption or "").strip() or f"[{_KIND_LABEL.get(kind, kind)} — очікує розпізнавання]"
        tid = _persist(db_path, kind=kind, text=placeholder, prov=prov,
                       category_id=category_id, file_path=file_path, doc_type=None,
                       segments_json=None, model_used=None, processing_time=0.0)

        def _job(job, _tid=tid, _fp=file_path, _kind=kind, _cap=caption, _prov=prov,
                 _db=db_path, _model=model, _lang=language):
            return _transcribe_and_finalize(_tid, _fp, _kind, _cap, _prov, _db, _model, _lang)

        state.job_queue.submit("tg_transcribe", _job,
                               meta={"chat_id": chat_id, "message_id": message_id,
                                     "kind": kind, "transcription_id": tid})
        return jsonify({"success": True, "queued": True, "kind": kind,
                        "transcription_id": tid})

    # --- photo / document → парсимо синхронно (швидко, без GPU) ---
    if kind in _PARSE_KINDS:
        file_path = _safe_media_path(file_path)
        if not file_path or not os.path.isfile(file_path):
            return jsonify({"success": False,
                            "error": "file_path відсутній або поза telegram_media/"}), 400
        text, doc_type, structure = _parse_media(file_path, caption, kind)
        tid = _persist(db_path, kind=kind, text=text, prov=prov, category_id=category_id,
                       file_path=file_path, doc_type=doc_type, segments_json=None,
                       model_used=None, processing_time=0.0, structure_json=structure)
        try:
            state.metrics.inc("whisper_telegram_total", kind=kind)
        except Exception:
            pass
        enrich = _finish(db_path, tid, prov)
        return jsonify({"success": True, "transcription_id": tid, "kind": kind,
                        "enrichment": {"status": enrich}})

    # --- text (default) ---
    if caption:
        text = f"{caption}\n\n{text}".strip() if text else caption
    if not text:
        return jsonify({"success": True, "skipped": "empty"})
    tid = _persist(db_path, kind="text", text=text, prov=prov, category_id=category_id,
                   file_path=None, doc_type=None, segments_json=None,
                   model_used=None, processing_time=0.0)
    try:
        state.metrics.inc("whisper_telegram_total", kind="text")
    except Exception:
        pass
    enrich = _finish(db_path, tid, prov)
    return jsonify({"success": True, "transcription_id": tid, "kind": "text",
                    "enrichment": {"status": enrich}})


def _transcribe_and_finalize(tid: int, file_path: str, kind: str, caption: str, prov: dict,
                             db_path: str, model: str, language: str) -> dict:
    """Фоновий job: (video→витяг аудіо) → whisper → дописати рядок → enrich.
    Виконується у воркері job_queue (фоновий потік, БЕЗ Flask app context) —
    тому ВСІ залежності (db_path, model, language) передаються явно, без
    current_app. whisper_manager — module-singleton (state), GPU під semaphore."""
    caption = (caption or "").strip()

    audio_fp = file_path
    extracted = None
    if kind == "video":
        from app.utils.audio import extract_audio_from_video
        extracted = os.path.splitext(file_path)[0] + "_audio.mp3"
        if not extract_audio_from_video(file_path, extracted, add_log=None):
            logger.warning("[tg] не вдалось витягти аудіо з відео %s", file_path)
            extracted = None
        else:
            audio_fp = extracted

    text = ""
    segments_json = None
    t0 = time.time()
    if audio_fp and os.path.isfile(audio_fp):
        try:
            result = state.whisper_manager.transcribe_with_progress(
                audio_path=audio_fp, model_name=model, language=language)
            if "error" in result:
                logger.warning("[tg] whisper error на %s: %s", audio_fp, result["error"])
            else:
                text = (result.get("text") or "").strip()
                segs = result.get("segments")
                if segs:
                    segments_json = json.dumps(segs, ensure_ascii=False)
        except Exception as e:
            logger.error("[tg] транскрипція впала на %s: %s", audio_fp, e, exc_info=True)
    processing_time = round(time.time() - t0, 2)

    # caption завжди зберігаємо (часто несе суть голосового/відео)
    if caption:
        text = f"{caption}\n\n{text}".strip() if text else caption
    if not text:
        text = f"[{_KIND_LABEL.get(kind, kind)} без розпізнаного тексту]"

    _finalize_media(db_path, tid, kind=kind, text=text, prov=prov,
                    segments_json=segments_json, model_used=model,
                    processing_time=processing_time)
    try:
        state.metrics.inc("whisper_telegram_total", kind=kind)
    except Exception:
        pass
    enrich = _submit_embed_only(tid, db_path)
    logger.info("[tg] %s → tx=%d транскрибовано за %.1fs embed=%s",
                kind, tid, processing_time, enrich)
    return {"transcription_id": tid, "kind": kind, "chars": len(text)}


# ============================================================
# Управління (Phase 17C): статус / діалоги / моніторені чати / backfill
# ============================================================

@telegram_bp.route('/api/telegram/status', methods=['GET'])
def tg_status():
    """Живий слухач? Хто залогінений? (проксі до control-API слухача)."""
    ok, data = _listener_call("/status", timeout=15)
    if not ok:
        return jsonify({"alive": False, **data})
    return jsonify({"alive": True, **data})


@telegram_bp.route('/api/telegram/dialogs', methods=['GET'])
def tg_dialogs():
    """Список діалогів акаунта для вибору галочками (потребує живого слухача —
    лише він тримає Telethon-сесію). Оффлайн → 503, UI підкаже запустити слухач."""
    ok, data = _listener_call("/dialogs", timeout=60)
    if not ok:
        return jsonify({"success": False, **data}), 503
    return jsonify({"success": True, **data})


@telegram_bp.route('/api/telegram/chats', methods=['GET'])
def tg_chats():
    """Моніторені чати з БД (працює навіть коли слухач вимкнений)."""
    with _conn(_db_path()) as conn:
        rows = conn.execute(
            "SELECT m.chat_id, m.title, m.username, m.chat_type, m.enabled, "
            "m.category_id, m.last_message_id, c.name AS category_name "
            "FROM tg_monitored_chats m LEFT JOIN categories c ON c.id = m.category_id "
            "ORDER BY m.enabled DESC, m.title"
        ).fetchall()
    return jsonify({"success": True, "chats": [dict(r) for r in rows]})


@telegram_bp.route('/api/telegram/chats', methods=['POST'])
def tg_chats_upsert():
    """Увімкнути/вимкнути чат + привязати напрямок. Часткове оновлення: лише
    присутні в body поля змінюються. category_id=null → відвʼязати напрямок.
    Запис прямо в БД → слухач підхопить на наступному refresh (≤30с)."""
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": "chat_id обов'язковий"}), 400
    try:
        chat_id = int(data["chat_id"])
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "chat_id має бути числом"}), 400

    category_id = None
    if "category_id" in data and data["category_id"] not in (None, "", "null"):
        try:
            category_id = int(data["category_id"])
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "category_id має бути числом"}), 400

    db_path = _db_path()
    with _conn(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM tg_monitored_chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        cur = dict(cur) if cur else {}
        enabled = int(bool(data["enabled"])) if "enabled" in data else cur.get("enabled", 1)
        if "category_id" in data:
            cat = category_id        # явно передано (можливо null = відвʼязати)
        else:
            cat = cur.get("category_id")
        title = data.get("title", cur.get("title"))
        username = data.get("username", cur.get("username"))
        chat_type = data.get("chat_type", cur.get("chat_type"))
        conn.execute(
            """INSERT INTO tg_monitored_chats
                 (chat_id, title, username, chat_type, enabled, category_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 title=excluded.title, username=excluded.username,
                 chat_type=excluded.chat_type, enabled=excluded.enabled,
                 category_id=excluded.category_id""",
            (chat_id, title, username, chat_type, enabled, cat),
        )
        conn.commit()
    return jsonify({"success": True, "chat_id": chat_id, "enabled": enabled, "category_id": cat})


@telegram_bp.route('/api/telegram/chats/toggle', methods=['POST'])
def tg_chats_toggle():
    """T2.6 (REMEDIATION_PLAN Волна 2): CLI-only upsert для
    `telegram_listener.py enable/disable <chat_id>`. Раніше ці команди писали
    в tg_monitored_chats НАПРЯМУ з окремого процесу (той самий SQLite-файл,
    що й app.py, по WAL) — порушувало задокументований інваріант «лише app.py
    пише в БД». Тепер слухач лише POST'ить сюди (як /api/telegram/ingest),
    запис виконує Flask.

    НАВМИСНО окремий ендпоінт від /api/telegram/chats (яким користується UI):
    той захищений лише загальним auth-гейтом сесії (localhost/API-key), а тут
    викликач — інший локальний ПРОЦЕС без браузерної сесії, тож потрібен той
    самий shared-secret, що і в ingest (_authorized: localhost + X-Telegram-
    Token). Семантика ідентична старому прямому SQL з телеграм_listener.py:
    оновлює лише title/username/chat_type/enabled, category_id НЕ чіпає
    (щоб не затерти прив'язку напрямку, зроблену в UI)."""
    if not _authorized():
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": "chat_id обов'язковий"}), 400
    try:
        chat_id = int(data["chat_id"])
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "chat_id має бути числом"}), 400

    enabled = 1 if data.get("enabled") else 0
    title = data.get("title")
    username = data.get("username")
    chat_type = data.get("chat_type")

    db_path = _db_path()
    with _conn(db_path) as conn:
        conn.execute(
            """INSERT INTO tg_monitored_chats (chat_id, title, username, chat_type, enabled)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 enabled=excluded.enabled,
                 title=COALESCE(excluded.title, tg_monitored_chats.title),
                 username=COALESCE(excluded.username, tg_monitored_chats.username),
                 chat_type=COALESCE(excluded.chat_type, tg_monitored_chats.chat_type)""",
            (chat_id, title, username, chat_type, enabled),
        )
        conn.commit()
    return jsonify({"success": True, "chat_id": chat_id, "enabled": enabled, "title": title})


def _iso_day(value: str | None) -> str | None:
    return value[:10] if value else None


def _days_between(later: str | None, earlier: str | None) -> int | None:
    """Різниця в днях між двома ISO-датами (обидві можуть бути None)."""
    if not later or not earlier:
        return None
    try:
        a = datetime.fromisoformat(later[:19].replace(" ", "T"))
        b = datetime.fromisoformat(earlier[:19].replace(" ", "T"))
    except ValueError:
        return None
    return (a.date() - b.date()).days


def _coverage_verdict(row: dict, live: dict | None, listed: bool) -> str:
    """Чому чат тихий: він справді спить чи слухач його не чує.

    Саме на це не було відповіді ні у власника, ні в агента — «12 чатів мовчать
    понад тиждень» однаково виглядало і як мертвий проєкт, і як зламаний інжест.
    """
    if live and live.get("migrated_to"):
        return "migrated"          # chat_id змінився → у tg_monitored_chats мертвий id
    if not row.get("enabled"):
        # Вимкнений чат МАЄ відставати — ми його свідомо не слухаємо. Без цієї
        # гілки звіт кричав «відстав на 170 днів» про те, що працює як задумано,
        # і справжні дві-три проблеми тонули серед хибних тривог.
        return "not_monitored"
    if not listed:
        return "not_listed"        # немає серед діалогів: вийшли/видалено/поза лімітом
    if not row.get("archived_msgs"):
        return "never_ingested"
    live_day = _iso_day((live or {}).get("last_message_date"))
    arch_day = _iso_day(row.get("archived_last_date"))
    if live_day and arch_day and live_day > arch_day:
        return "behind"            # у Telegram новіше, ніж в архіві → слухач відстав
    return "ok"


@telegram_bp.route('/api/telegram/coverage', methods=['GET'])
def tg_coverage():
    """Волна 1 «Бачити»: архів проти живого Telegram, по кожному моніторенему чату.

    Живий курсор береться з проходу iter_dialogs у слухача (Dialog.message) —
    без другої Telethon-сесії і без жодного зайвого запиту до Telegram.

    Слухач офлайн — НЕ помилка: повертаємо архівну половину (коли чат востаннє
    потрапляв в архів), бо на питання «що ми знаємо про цей чат» вона відповідає
    і без мережі. live=false у відповіді.

    Відставання в ПОВІДОМЛЕННЯХ рахуємо лише для супергруп (-100…): в legacy-групах
    і особистих чатах message_id береться з глобальної послідовності акаунта, тож
    арифметика по id там безглузда (на живих даних це 15 чатів з 19).
    """
    db_path = _db_path()
    with _conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT m.chat_id, m.title, m.chat_type, m.enabled, m.category_id, "
            "  (SELECT COUNT(*) FROM transcriptions t "
            "     WHERE t.tg_chat_id = m.chat_id AND t.deleted_at IS NULL) AS archived_msgs, "
            "  (SELECT MAX(t.tg_date) FROM transcriptions t "
            "     WHERE t.tg_chat_id = m.chat_id AND t.deleted_at IS NULL) AS archived_last_date, "
            "  (SELECT MAX(t.tg_message_id) FROM transcriptions t "
            "     WHERE t.tg_chat_id = m.chat_id AND t.deleted_at IS NULL) AS archived_last_id "
            "FROM tg_monitored_chats m ORDER BY m.enabled DESC, m.title"
        ).fetchall()]

    ok, data = _listener_call("/dialogs", timeout=60)
    live_by_id = {}
    if ok:
        live_by_id = {d["id"]: d for d in (data.get("dialogs") or []) if d.get("id") is not None}

    out = []
    for row in rows:
        live = live_by_id.get(row["chat_id"])
        listed = (not ok) or (live is not None)   # без слухача про «не в списку» не судимо
        entry = {
            "chat_id": row["chat_id"], "title": row["title"], "chat_type": row["chat_type"],
            "enabled": bool(row["enabled"]), "category_id": row["category_id"],
            "archived_msgs": row["archived_msgs"],
            "archived_last_date": row["archived_last_date"],
            "archived_last_id": row["archived_last_id"],
            "live_last_date": (live or {}).get("last_message_date"),
            "live_last_id": (live or {}).get("last_message_id"),
            "unread_count": (live or {}).get("unread_count"),
            "migrated_to": (live or {}).get("migrated_to"),
            "lag_days": _days_between((live or {}).get("last_message_date"),
                                      row["archived_last_date"]) if ok else None,
            "quiet_days": _days_between(datetime.now().isoformat(), row["archived_last_date"]),
        }
        # Дельта по id — тільки там, де id послідовні всередині чату.
        if ok and str(row["chat_id"]).startswith("-100") and live and live.get("last_message_id"):
            entry["behind_messages"] = max(
                0, int(live["last_message_id"]) - int(row["archived_last_id"] or 0))
        else:
            entry["behind_messages"] = None
        entry["status"] = _coverage_verdict(row, live, listed) if ok else "unknown"
        out.append(entry)

    return jsonify({
        "success": True,
        "live": ok,
        "listener": None if ok else data.get("error"),
        "dialogs_seen": len(live_by_id) if ok else None,
        "dialog_limit": data.get("limit") if ok else None,
        "chats": out,
        "note": ("behind_messages рахується лише для супергруп (-100…); пропуски в id "
                 "можуть бути стікерами/службовими повідомленнями, які інжест свідомо "
                 "не бере. lag_days показує відставання ХВОСТА — дірки в середині "
                 "історії ним не видно. not_listed при dialogs_seen == dialog_limit "
                 "може означати просто «не вліз у вікно діалогів»"),
    })


@telegram_bp.route('/api/telegram/edited', methods=['POST'])
def tg_edited():
    """Повідомлення виправили — оновити текст (Волна 4).

    Раніше архів писався один раз і не лагодився: `_is_duplicate` при повторному
    заході повертав наявний id і виходив БЕЗ оновлення тексту, а підписки на
    MessageEdited не було взагалі. У робочих чатах правка-виправлення — норма
    (сума, дата, адреса), і архів назавжди зберігав першу, хибну версію."""
    if not _authorized():
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    chat_id, message_id = data.get("chat_id"), data.get("message_id")
    text = (data.get("text") or "").strip()
    if chat_id is None or message_id is None:
        return jsonify({"success": False, "error": "потрібні chat_id і message_id"}), 400

    db_path = _db_path()
    tid = _is_duplicate(db_path, chat_id, message_id)
    if not tid:
        return jsonify({"success": True, "skipped": "not_in_archive"})
    if not text:
        return jsonify({"success": True, "skipped": "empty_text"})

    with _conn(db_path) as conn:
        row = conn.execute("SELECT transcript_text FROM transcriptions WHERE id = ?",
                           (tid,)).fetchone()
        if row and (row["transcript_text"] or "") == text:
            return jsonify({"success": True, "unchanged": True, "transcription_id": tid})
        conn.execute(
            # embedded_at скидаємо: старий вектор описує стару редакцію, і без
            # скидання пошук знаходив би текст, якого вже немає.
            "UPDATE transcriptions SET transcript_text = ?, tg_edit_date = ?, "
            "embedded_at = NULL WHERE id = ?",
            (text, data.get("edit_date"), tid))
        conn.commit()
    enrich = _submit_embed_only(tid, db_path, force=True)
    logger.info("[tg] правка tx=%d (chat=%s msg=%s) embed=%s", tid, chat_id, message_id, enrich)
    return jsonify({"success": True, "transcription_id": tid, "updated": True,
                    "enrichment": {"status": enrich}})


@telegram_bp.route('/api/telegram/deleted', methods=['POST'])
def tg_deleted():
    """Повідомлення видалили — мʼяко прибрати з архіву (Волна 4).

    Без цього Recall цитує те, чого в чаті вже немає. deleted_at поважають і
    пошук, і скоуп, тож запис одразу зникає з видачі."""
    if not _authorized():
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id")
    ids = data.get("message_ids") or []
    if chat_id is None or not ids:
        return jsonify({"success": False, "error": "потрібні chat_id і message_ids"}), 400

    with _conn(_db_path()) as conn:
        ph = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE transcriptions SET deleted_at = CURRENT_TIMESTAMP "
            f"WHERE tg_chat_id = ? AND tg_message_id IN ({ph}) AND deleted_at IS NULL",
            [chat_id] + [int(i) for i in ids])
        conn.commit()
        marked = cur.rowcount
    if marked:
        logger.info("[tg] видалено автором: %d запис(ів) (chat=%s)", marked, chat_id)
    return jsonify({"success": True, "marked": marked})


def _is_supergroup(chat_id) -> bool:
    """id послідовні всередині чату лише в супергрупах (-100…). У legacy-групах
    і особистих чатах message_id береться з глобальної послідовності акаунта,
    тож «дірка в id» там нічого не означає (15 чатів з 19 на живих даних)."""
    return str(chat_id).startswith("-100")


def _missing_ids(db_path: str, chat_id: int, live_last_id: int | None = None) -> list[int]:
    """id, яких бракує в архіві: дірки всередині [min…max] + хвіст до живого останнього.

    Нижче min не лізем — це вже не ремонт, а догрузка історії (окремий backfill):
    відрізняти «ми це втратили» від «ми туди ще не ходили» важливо, бо перше
    полагодити треба, а друге — рішення власника.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT tg_message_id AS mid FROM transcriptions "
            "WHERE tg_chat_id = ? AND tg_message_id IS NOT NULL", (chat_id,)).fetchall()
    have = {int(r["mid"]) for r in rows}
    if not have:
        return []                      # порожній чат — це backfill, не ремонт
    lo, hi = min(have), max(have)
    missing = [i for i in range(lo, hi + 1) if i not in have]
    if live_last_id and live_last_id > hi:
        missing.extend(range(hi + 1, int(live_last_id) + 1))
    return missing


@telegram_bp.route('/api/telegram/repair', methods=['POST'])
def tg_repair():
    """Волна 3 «Полагодити минуле»: точковий ремонт дірок у супергрупі.

    Body: {chat_id, dry_run=false, max_messages=200}.

    Тягне КОНКРЕТНІ відсутні id через get_messages(ids=…) — на місці видалених
    Telegram віддає None, тож у відповіді `deleted` (видалив автор) відокремлено
    від `ingested` (втратив слухач). Саме це уточнює оцінку «359 пропущено»,
    яка досі була верхньою межею: дірка в id могла бути й стікером, і службовим
    повідомленням, і видаленим.

    Пачка обмежена max_messages — виклик лишається обмеженим у часі, у відповіді
    `remaining` показує, скільки ще лишилось (повторювати до нуля).
    """
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": "chat_id обов'язковий"}), 400
    try:
        chat_id = int(data["chat_id"])
        max_messages = max(1, min(int(data.get("max_messages", 200)), 500))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "chat_id/max_messages мають бути числами"}), 400

    if not _is_supergroup(chat_id):
        return jsonify({
            "success": False,
            "error": "ремонт по id доступний лише для супергруп (-100…): у решті чатів "
                     "message_id з глобальної послідовності акаунта. Для них — /backfill за датами",
        }), 400

    live_last_id = None
    ok, dialogs = _listener_call("/dialogs", timeout=90)
    if ok:
        for d in (dialogs.get("dialogs") or []):
            if d.get("id") == chat_id:
                live_last_id = d.get("last_message_id")
                break

    missing = _missing_ids(_db_path(), chat_id, live_last_id)
    if not missing:
        return jsonify({"success": True, "missing_total": 0, "remaining": 0,
                        "note": "дірок немає (або чат порожній — тоді це backfill)"})

    batch = missing[:max_messages]
    if data.get("dry_run"):
        return jsonify({"success": True, "dry_run": True, "missing_total": len(missing),
                        "would_attempt": len(batch), "sample": batch[:20],
                        "live_last_id": live_last_id})

    ok, res = _listener_call("/repair", method="POST",
                             body={"chat_id": chat_id, "ids": batch}, timeout=900)
    if not ok:
        return jsonify({"success": False, **res}), 503
    return jsonify({"success": True, "missing_total": len(missing),
                    "remaining": len(missing) - len(batch), **res})


@telegram_bp.route('/api/telegram/backfill', methods=['POST'])
def tg_backfill():
    """Догрузити історію чату (проксі до слухача; сам backfill — Phase 17D)."""
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": "chat_id обов'язковий"}), 400
    ok, res = _listener_call("/backfill", method="POST", body={
        "chat_id": data.get("chat_id"),
        "limit": data.get("limit", 200),
    }, timeout=30)
    if not ok:
        return jsonify({"success": False, **res}), 503
    return jsonify({"success": True, **res})
