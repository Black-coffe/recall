"""Ingest of messages from a real Telegram account (Phase 17B).

Architecture: the listener (telegram_listener.py) is a SEPARATE process (Telethon, asyncio),
because a Telethon session (SQLite) cannot be opened by two clients, and Flask in debug also
reloads. The listener is THIN: it catches the event, DOWNLOADS media to disk and POSTs
here over localhost. All the heavy work (parse/OCR/transcription/embeddings/Claude)
stays in the Flask process, where the models are already loaded (one instance on the GPU —
no second copy of whisper/e5-large in video memory).

POST /api/telegram/ingest  (localhost only)
  Body JSON: {kind, text?, caption?, file_path?, chat_id, chat_title, sender,
              message_id, date, link}
  Routing by kind:
    text                 → transcript_text = text                  (sync)
    photo | document     → document_parser.parse_document (OCR/parse) (sync)
    voice | audio        → background job: whisper → persist → enrich   (queued)
    video                → NOT downloaded (owner's decision, tg-media-policy-01):
                            a stub row with a link to the message, no
                            file_path, no whisper (sync)
  All records: source_type='telegram', tg_* provenance, dedup (chat_id,message_id),
  category_id inherited from tg_monitored_chats. Then the same enrichment
  (chunk+embed + Claude card) as for audio/documents → the record is immediately
  a full participant of RAG search, the entity graph and directions.

POST /api/telegram/chats/toggle  (T2.6, localhost + shared-secret only)
  CLI-only twin of /api/telegram/chats: `telegram_listener.py enable/disable`
  proxies here instead of a direct SQL write in a separate process — app.py
  stays the only SQLite writer (see tg_chats_toggle below).
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
from app.core import settings as _settings
from app.core.i18n import tr
from app.services import categories as cat_svc
from app.utils.paths import safe_path_within
from telegram_common import CONTROL_TOKEN_HEADER, control_token


logger = logging.getLogger(__name__)
telegram_bp = Blueprint('telegram', __name__)

# kind → human-readable label for source_name when there is no text.
_KIND_LABEL = {
    "text": "повідомлення", "photo": "фото", "voice": "голосове",
    "audio": "аудіо", "video": "відео", "document": "документ",
}
_MEDIA_KINDS = {"voice", "audio"}               # need whisper (background job)
_PARSE_KINDS = {"photo", "document"}            # document_parser (sync)
# "video" is deliberately NOT here (tg-media-policy-01): video is never
# downloaded (no threshold by size/age) — see the separate branch in ingest().


def _db_path() -> str:
    return current_app.config['DATABASE']


def _conn(db_path: str):
    from app.db.connection import get_db_connection
    return get_db_connection(db_path)


def _schedule_reembed(ids, reason: str) -> dict:
    """Queue the changed records for a targeted re-embed (direction and tags go into the
    chunk's context prefix). A scheduler error does not fail the request —
    the edit is already saved, the re-embed is secondary. `reembed` is inside the function:
    it pulls in `embeddings` (torch), note `mcp-stdio-no-heavy-models`."""
    if not ids:
        return {"mode": "none", "count": 0, "stale": 0}
    try:
        from app.services import reembed
        return reembed.schedule_records_reembed(ids, db_path=_db_path(), reason=reason)
    except Exception:  # noqa: BLE001 — the change is already in the DB, the re-embed is secondary
        logger.exception("[tg] %s: could not schedule a re-embed for %s records",
                         reason, len(ids))
        return {"mode": "none", "count": 0, "stale": 0}


def _is_localhost() -> bool:
    return (request.remote_addr or "") in ("127.0.0.1", "::1", "localhost")


def _authorized() -> bool:
    """Ingest is protected by localhost + shared-secret (an IP check can be spoofed,
    another local process has no token). The listener sends the same token."""
    if not _is_localhost():
        return False
    return request.headers.get(CONTROL_TOKEN_HEADER) == control_token()


def _listener_call(path: str, method: str = "GET", body=None, timeout: int = 60):
    """Call the listener's control-API (Flask→listener). Returns (ok, data).
    ok=False if the listener is off/unavailable — the UI will show «offline»."""
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
    """Confine file_path under TELEGRAM_MEDIA_DIR. Ingest accepts a path from the client
    (the listener on the same machine), but another local process could slip in
    ANY file on disk (C:\\Windows\\…, other people's documents) → Recall would read and
    index its content into RAG = arbitrary file read. So we allow only
    files from telegram_media/ — the only directory the listener writes to.
    Returns the realpath if valid, otherwise None (like the path-traversal guard in
    documents._save_original)."""
    if not file_path:
        return None
    media_root = current_app.config.get('TELEGRAM_MEDIA_DIR', 'telegram_media')
    return safe_path_within(media_root, file_path)


def _resolve_category(db_path: str, chat_id: int):
    """category_id from tg_monitored_chats for this chat (which direction to put it in)."""
    if chat_id is None:
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT category_id FROM tg_monitored_chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["category_id"] if row and row["category_id"] is not None else None


def _is_duplicate(db_path: str, chat_id, message_id) -> int | None:
    """The existing record of this message (real-time ↔ backfill overlap). tx_id or None."""
    if chat_id is None or message_id is None:
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM transcriptions WHERE tg_chat_id = ? AND tg_message_id = ?",
            (chat_id, message_id),
        ).fetchone()
    return row["id"] if row else None


def _meeting_date(iso_date: str | None) -> str | None:
    """The conversation day (local) from the message's ISO date — for meeting_date.

    Without this field the whole stack dates a record via
    COALESCE(meeting_date, substr(created_at,1,10)), i.e. by the moment of ingest:
    the recency boost (retrieval.py) considers February correspondence fresh, and parse_due
    (commitments.py) expands «до пʼятниці» relative to the upload day. In
    live data the discrepancy was in 53% of TG records, in 1067 of them by more than a month.

    localtime, because Telegram returns UTC, and we care about the day the conversation happened
    (a message after 21:00 UTC would otherwise shift back by a day)."""
    if not iso_date:
        return None
    try:
        dt = datetime.fromisoformat(iso_date)
    except (TypeError, ValueError):
        logger.debug("[tg] could not parse date %r — meeting_date stays empty", iso_date)
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date().isoformat()


def _source_name(prov: dict, text: str, kind: str) -> str:
    """Label for History: [TG] chat: snippet (or the media type)."""
    chat = prov.get("chat_title") or f"chat {prov.get('chat_id')}"
    snippet = " ".join((text or "").split())[:60]
    if not snippet:
        snippet = _KIND_LABEL.get(kind, kind)
    return f"[TG] {chat}: {snippet}"


def _persist(db_path: str, *, kind: str, text: str, prov: dict, category_id,
             file_path: str | None, doc_type: str | None,
             segments_json: str | None, model_used: str | None,
             processing_time: float, structure_json: str | None = None) -> int:
    """INSERT a TG message into transcriptions. Returns transcription_id.
    Does not depend on the request context (also called from a background job).

    A race with a parallel ingest of the same message (real-time against
    repair/catch-up) is caught on the UNIQUE index idx_tx_tg_msg, not by a check
    beforehand: there is a window between `_is_duplicate` and INSERT, and an IntegrityError
    used to fly into it and fail the background job together with the message."""
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
                logger.info("[tg] race on (chat=%s msg=%s) — taking the existing tx=%d",
                            prov.get("chat_id"), prov.get("message_id"), existing["id"])
                return existing["id"]
            raise
        tid = c.lastrowid
        chat_id = prov.get("chat_id")
        if chat_id is not None:
            # Ingest inherits NOT ONLY category_id (see _resolve_category
            # above) — tags bound to the chat (`PUT .../<chat_id>/tags`),
            # retroactively in exactly the same way as the direction.
            cat_svc.inherit_chat_tags(conn, tid, chat_id)
        conn.commit()
    return tid


def _finalize_media(db_path: str, tid: int, *, kind: str, text: str, prov: dict,
                    segments_json: str | None, model_used: str | None,
                    processing_time: float) -> None:
    """Append the recognition result to a row created IN ADVANCE.

    The row for media is created synchronously at ingest (as a stub), not at the end of the
    background job. Otherwise an «in flight» message is invisible: `_is_duplicate`
    does not see it (two whisper runs on one voice message + IntegrityError),
    the catch-up watermark does not count it, and if app.py crashes mid-queue —
    job_queue.recover_crashed only marks the job 'crashed', does NOT restart it,
    and the message vanishes without a trace."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE transcriptions SET transcript_text = ?, source_name = ?, "
            "segments = ?, model_used = ?, processing_time = ? WHERE id = ?",
            (text, _source_name(prov, text, kind), segments_json,
             model_used, processing_time, tid),
        )
        conn.commit()


PARSE_FAILED = "parse_failed"


def _parse_media(file_path: str, caption: str, kind: str):
    """photo/document → document_parser (OCR for photos). Soft degradation: if the
    parser is unavailable/crashed — we keep the caption (the message is not lost), BUT
    the record is marked ``model_used = PARSE_FAILED`` and is visible in coverage: that is how
    862 photos lay for years without text and nobody saw it.
    Text from the picture is masked against passwords/tokens (ADR-030).
    Returns (text, doc_type, structure_json, failed)."""
    from app.services import document_parser
    caption = (caption or "").strip()
    try:
        parsed = document_parser.parse_document(file_path, filename=os.path.basename(file_path))
        body = (parsed.get("text") or "").strip()      # OCR text is already masked (document_parser)
        text = f"{caption}\n\n{body}".strip() if caption else body
        blocks = parsed.get("blocks")
        structure = json.dumps(blocks, ensure_ascii=False) if blocks else None
        return text, parsed.get("doc_type"), structure, False
    except document_parser.NoTextFound:
        # a plain photo without text — parsing succeeded, nothing to mark
        return caption or f"[{_KIND_LABEL.get(kind, kind)} без тексту]", kind, None, False
    except Exception as e:
        logger.warning("[tg] parsing %s failed (%s) — keeping the caption, record marked %s",
                       kind, e, PARSE_FAILED)
        fallback = caption or f"[{_KIND_LABEL.get(kind, kind)} без тексту]"
        return fallback, kind, None, True


def _link_entities(db_path: str, transcription_id: int) -> None:
    """Link a message to the entity graph (Wave 4.5.3).

    Without this step the graph was fed only by a manual CLI run, i.e. every
    new message stayed outside the project slice and outside `list_stale_topics`
    until the next manual run. The work is local and cheap (looking up spellings in
    the text), it needs no model.
    """
    try:
        from app.services import tg_entities
        tg_entities.link_message(db_path, transcription_id)
    except Exception as exc:
        # The graph is a superstructure over the record, like the thread: the message is already in the archive
        # and is found by search. We swallow, but with a log — a silent failure here
        # would read as «there are simply no entities in this chat».
        logger.warning("[tg] entities for tx=%s not linked: %s", transcription_id, exc)


def _submit_embed_only(transcription_id: int, db_path: str, force: bool = False) -> str:
    """Phase 17: for Telegram we queue ONLY local embeddings — NO Claude card.
    Chat messages are short: summary/entities on a one-liner are of little use, and on
    thousands of backfill messages they burn the paid Claude API. Search/RAG work on
    embeddings (local, free). Idempotent (embedded_at). db_path is explicit —
    the job_queue worker runs outside the app context."""
    try:
        from app.services import embeddings
        if not (embeddings.is_available() and state.job_queue is not None):
            # Linking to the graph needs neither vectors nor the queue — otherwise
            # disabled embeddings would silently leave messages outside the slices.
            _link_entities(db_path, transcription_id)
            return "unavailable"

        def _job(job, _tid=transcription_id, _db=db_path, _force=force):
            try:
                res = embeddings.chunk_and_embed_transcription(_db, _tid, force=_force)
                # The thread (Wave 4.5) comes AFTER the embedding: the thread centroid is built from
                # its messages' vectors, and before chunking there is no vector yet. The decision
                # here is provisional ('pending') and without a model; finally the burst is laid out by
                # tg_threads.resettle_pending once the conversation calms down.
                try:
                    from app.services import tg_threads
                    tg_threads.assign_incoming(_db, _tid)
                except Exception as exc:
                    # The thread is a superstructure: without it search works as in Wave 4, so
                    # a failure here must not fail the embedding of the message itself.
                    logger.warning("[tg] thread for tx=%s not determined: %s", _tid, exc)
                return res
            finally:
                # In `finally`, not after the embedding: if chunking RAISES (OOM
                # on the GPU, model load failure), the job goes to 'failed' and
                # never repeats (`job_queue` has no retry) — and the
                # message would stay outside the graph forever, i.e. exactly the
                # silent hole this call was introduced for. Linking to the
                # graph needs neither vectors nor the success of the embedding.
                _link_entities(_db, _tid)

        state.job_queue.submit("tg_embed", _job, meta={"transcription_id": transcription_id})
        return "queued"
    except Exception as e:
        logger.warning("TG embed job not queued (continuing): %s", e)
        _link_entities(db_path, transcription_id)   # see the 'unavailable' branch
        return "error"


def _finish(db_path: str, tid: int, prov: dict):
    """Shared tail: log + embed-only job (no Claude — see _submit_embed_only)."""
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
        # Wave 4 — signals from Telegram (thread, author, album, edit).
        "sender_id": data.get("sender_id"),
        "reply_to": data.get("reply_to"),
        "grouped_id": data.get("grouped_id"),
        "edit_date": data.get("edit_date"),
    }

    db_path = _db_path()

    # --- Dedup: real-time and backfill may overlap on one message_id ---
    dup = _is_duplicate(db_path, chat_id, message_id)
    if dup:
        return jsonify({"success": True, "duplicate": True, "transcription_id": dup})

    category_id = _resolve_category(db_path, chat_id)

    # --- Video is NEVER downloaded (owner's decision, tg-media-policy-01:
    # no threshold by size or age) — we leave a stub row with a
    # link so that «there was a video in this chat» stays findable. file_path
    # does not come from the listener here at all (telegram_listener.py does not download it),
    # but even if it did — we deliberately ignore it, whisper and
    # extract_audio_from_video are not called anywhere in this path.
    if kind == "video":
        text = (caption or "").strip() or "[відео — не завантажено, лише посилання]"
        # tg-media-policy-03: doc_type=kind (the existing convention of the degraded
        # path, telegram.py _parse_media) — otherwise with a caption the row looks like
        # an ordinary text message, the library draws the label exactly from
        # doc_type (static/js/recall/views/library.js).
        tid = _persist(db_path, kind=kind, text=text, prov=prov, category_id=category_id,
                       file_path=None, doc_type=kind, segments_json=None,
                       model_used=None, processing_time=0.0)
        try:
            state.metrics.inc("whisper_telegram_total", kind=kind)
        except Exception as e:
            logger.debug("[tg] metric whisper_telegram_total not incremented: %s", e)
        enrich = _finish(db_path, tid, prov)
        return jsonify({"success": True, "transcription_id": tid, "kind": kind,
                        "skipped": "video_not_downloaded", "enrichment": {"status": enrich}})

    # --- Media with whisper → background job (do not block the listener) ---
    if kind in _MEDIA_KINDS:
        file_path = _safe_media_path(file_path)
        if not file_path or not os.path.isfile(file_path):
            return jsonify({"success": False,
                            "error": tr("telegram.err.filePath")}), 400
        if state.job_queue is None:
            return jsonify({"success": False, "error": tr("telegram.err.queueDown")}), 503

        # current_app is valid HERE (request context) — we resolve the models now and
        # pass them into the job explicitly: the job_queue worker runs in a background thread WITHOUT
        # an app context (like the enrichment job in documents.py).
        cfg = current_app.config
        model = cfg.get('TELEGRAM_WHISPER_MODEL', 'large-v3-turbo')
        language = cfg.get('TELEGRAM_WHISPER_LANG', 'uk')

        # We create the row NOW, as a stub: while it does not exist, the message is
        # invisible to dedup and to the catch-up watermark (see _finalize_media).
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

    # --- photo / document → parse synchronously (fast, no GPU) ---
    if kind in _PARSE_KINDS:
        file_path = _safe_media_path(file_path)
        if not file_path or not os.path.isfile(file_path):
            return jsonify({"success": False,
                            "error": tr("telegram.err.filePath")}), 400
        text, doc_type, structure, failed = _parse_media(file_path, caption, kind)
        tid = _persist(db_path, kind=kind, text=text, prov=prov, category_id=category_id,
                       file_path=file_path, doc_type=doc_type, segments_json=None,
                       model_used=PARSE_FAILED if failed else None, processing_time=0.0,
                       structure_json=structure)
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


def _delete_intermediate(path: str | None) -> None:
    """Remove an intermediate artifact (e.g. `_audio.mp3` extracted from a video) —
    it is never needed by itself, unlike the original. Soft
    degradation: the file is already gone / no permission — log, do not fail the job."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        logger.warning("[tg] could not remove the intermediate file %s: %s", path, e)


def _delete_if_heavy(path: str | None, threshold_bytes: int, *, label: str) -> None:
    """tg-media-policy-02: remove `path` if it is heavier than `threshold_bytes`.
    Small originals stay on disk (owner's decision). Safe thanks to
    `get_original_file` (mcp_server.py) — degrades to original_available=False,
    a missing file is not an error there."""
    if not path:
        return
    try:
        if not os.path.isfile(path):
            return
        size = os.path.getsize(path)
        if size > threshold_bytes:
            os.remove(path)
            logger.info("[tg] %s removed (%.1f MB > threshold): %s",
                        label, size / 1_048_576, path)
    except OSError as e:
        logger.warning("[tg] could not remove %s %s: %s", label, path, e)


def _original_max_mb() -> int | None:
    """Safe reading of TELEGRAM_ORIGINAL_MAX_MB (tg-media-policy-03): a bad
    (empty/non-numeric) or negative value does NOT fail the job and does NOT delete the
    original — we just skip deletion in this run. `env_int`
    (app/core/settings.py) is a bare `int(env(name))`, and one bad value
    in .env used to raise ValueError in the middle of a background job with no retry."""
    try:
        threshold = _settings.env_int("TELEGRAM_ORIGINAL_MAX_MB")
    except (ValueError, TypeError) as e:
        logger.warning("[tg] TELEGRAM_ORIGINAL_MAX_MB is not a number — not deleting the original: %s", e)
        return None
    if threshold < 0:
        logger.warning("[tg] TELEGRAM_ORIGINAL_MAX_MB=%d < 0 — not deleting the original", threshold)
        return None
    return threshold


def _transcribe_and_finalize(tid: int, file_path: str, kind: str, caption: str, prov: dict,
                             db_path: str, model: str, language: str) -> dict:
    """Background job: (video→audio extraction) → whisper → append to the row → enrich.
    Runs in a job_queue worker (a background thread, WITHOUT a Flask app context) —
    so ALL dependencies (db_path, model, language) are passed explicitly, without
    current_app. whisper_manager is a module singleton (state), the GPU is under a semaphore."""
    caption = (caption or "").strip()

    audio_fp = file_path
    # tg-media-policy-03: `extracted` holds the intended _audio.mp3 path REGARDLESS
    # of whether the extraction succeeded — ffmpeg may manage to partly write the file even
    # on failure, and it is by this path that _delete_intermediate removes the orphan
    # below. Earlier a failure reset `extracted = None`, and that partial file
    # stayed on disk forever.
    extracted = None
    if kind == "video":
        from app.utils.audio import extract_audio_from_video
        extracted = os.path.splitext(file_path)[0] + "_audio.mp3"
        if not extract_audio_from_video(file_path, extracted, add_log=None):
            logger.warning("[tg] could not extract audio from video %s", file_path)
            audio_fp = None
        else:
            audio_fp = extracted

    text = ""
    segments_json = None
    extraction_ok = False
    t0 = time.time()
    if audio_fp and os.path.isfile(audio_fp):
        try:
            from app.services import vocabulary
            result = state.whisper_manager.transcribe_with_progress(
                audio_path=audio_fp, model_name=model, language=language,
                decode_options=vocabulary.decode_options_for('telegram', db_path))
            if "error" in result:
                logger.warning("[tg] whisper error on %s: %s", audio_fp, result["error"])
            else:
                text = (result.get("text") or "").strip()
                # tg-media-policy-03: success is non-empty text, not «no
                # error key». Whisper on deaf/broken audio returns
                # {"text": ""} without an error; extraction_ok=True in this branch
                # used to give the right to delete the original on the strength of the stub alone.
                if text:
                    extraction_ok = True
                segs = result.get("segments")
                if segs:
                    segments_json = json.dumps(segs, ensure_ascii=False)
        except Exception as e:
            logger.error("[tg] transcription failed on %s: %s", audio_fp, e, exc_info=True)
    processing_time = round(time.time() - t0, 2)

    # always keep the caption (it often carries the gist of the voice message/video)
    if caption:
        text = f"{caption}\n\n{text}".strip() if text else caption
    if not text:
        text = f"[{_KIND_LABEL.get(kind, kind)} без розпізнаного тексту]"

    _finalize_media(db_path, tid, kind=kind, text=text, prov=prov,
                    segments_json=segments_json, model_used=model,
                    processing_time=processing_time)

    # tg-media-policy-02: the intermediate _audio.mp3 (video→audio) is always an orphan
    # after extraction, regardless of whether whisper succeeded.
    _delete_intermediate(extracted)
    # The original — only if text was extracted (no text, no right to throw away
    # the source) and only if heavier than the threshold (small ones stay, owner's decision).
    if extraction_ok:
        threshold_mb = _original_max_mb()
        if threshold_mb is not None:
            _delete_if_heavy(file_path, threshold_mb * 1_048_576, label="TG media original")

    try:
        state.metrics.inc("whisper_telegram_total", kind=kind)
    except Exception:
        pass
    enrich = _submit_embed_only(tid, db_path)
    logger.info("[tg] %s → tx=%d transcribed in %.1fs embed=%s",
                kind, tid, processing_time, enrich)
    return {"transcription_id": tid, "kind": kind, "chars": len(text)}


# ============================================================
# Management (Phase 17C): status / dialogs / monitored chats / backfill
# ============================================================

@telegram_bp.route('/api/telegram/status', methods=['GET'])
def tg_status():
    """Is the listener alive? Who is logged in? (proxy to the listener's control-API)."""
    ok, data = _listener_call("/status", timeout=15)
    if not ok:
        return jsonify({"alive": False, **data})
    return jsonify({"alive": True, **data})


@telegram_bp.route('/api/telegram/dialogs', methods=['GET'])
def tg_dialogs():
    """List of the account's dialogs for picking with checkboxes (needs a live listener —
    only it holds the Telethon session). Offline → 503, the UI will suggest starting the listener."""
    ok, data = _listener_call("/dialogs", timeout=60)
    if not ok:
        return jsonify({"success": False, **data}), 503
    return jsonify({"success": True, **data})


@telegram_bp.route('/api/telegram/chats', methods=['GET'])
def tg_chats():
    """Monitored chats from the DB (works even when the listener is off).

    `?category_id=` — the same filter as everywhere (contract C2, via
    `categories.filter_sql`): a direction id expands into itself and all
    its projects, a project id (a leaf) gives only itself. Each row carries `tags`
    (`categories.chat_tags`) — the set that all the chat's future history will inherit.
    """
    try:
        frag, params = cat_svc.filter_sql("m.category_id", request.args.get("category_id"))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    with _conn(_db_path()) as conn:
        rows = conn.execute(
            "SELECT m.chat_id, m.title, m.username, m.chat_type, m.enabled, "
            "m.category_id, m.last_message_id, c.name AS category_name "
            "FROM tg_monitored_chats m LEFT JOIN categories c ON c.id = m.category_id "
            f"WHERE 1=1{frag} ORDER BY m.enabled DESC, m.title", params
        ).fetchall()
        chats = []
        for r in rows:
            d = dict(r)
            d["tags"] = cat_svc.chat_tags(conn, d["chat_id"])
            chats.append(d)
    return jsonify({"success": True, "chats": chats})


@telegram_bp.route('/api/telegram/chats', methods=['POST'])
def tg_chats_upsert():
    """Enable/disable a chat + bind a direction. Partial update: only the
    fields present in the body change. category_id=null → unbind the direction.
    Written straight to the DB → the listener picks it up on the next refresh (≤30s)."""
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": tr("telegram.err.chatIdRequired")}), 400
    try:
        chat_id = int(data["chat_id"])
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": tr("telegram.err.chatIdNumber")}), 400

    category_id = None
    if "category_id" in data and data["category_id"] not in (None, "", "null"):
        try:
            category_id = int(data["category_id"])
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": tr("telegram.err.categoryIdNumber")}), 400

    db_path = _db_path()
    with _conn(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM tg_monitored_chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        cur = dict(cur) if cur else {}
        enabled = int(bool(data["enabled"])) if "enabled" in data else cur.get("enabled", 1)
        if "category_id" in data:
            cat = category_id        # passed explicitly (possibly null = unbind)
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


@telegram_bp.route('/api/telegram/chats/<int(signed=True):chat_id>/tags', methods=['PUT'])
def tg_chat_tags_put(chat_id: int):
    """Replace the chat's SET of tags (`{"tags": ["AI", "інвестори"]}`).

    `int(signed=True)`, not a bare `int`: group and channel ids in Telegram
    are negative (`-100…`), and Flask's default converter does not accept a minus — with it the
    endpoint would return 404 exactly on the chats it exists for.

    PUT, not a POST-append: a chat tag is a property of the chat as a whole, and removing
    it from the map must be removed from history just as retroactively as adding
    lays it on (`categories.apply_chat_tags` touches only rows with
    `source='chat'`, a manual tag on a specific message stays).

    404 on a non-monitored chat: tags without `tg_monitored_chats` would hang on a
    `chat_id` the UI does not show, and there would be nothing to remove them with.

    `records_changed` — how many records actually changed their tag set; exactly these
    ids go to the targeted re-embed (tags are in the chunk's context prefix), so
    a repeated identical PUT gives 0 and does not touch the GPU.
    """
    data = request.get_json(silent=True) or {}
    if "tags" not in data:
        return jsonify({"success": False, "error": tr("telegram.err.tagsRequired")}), 400
    raw = data["tags"]
    if not isinstance(raw, (list, tuple, str)):
        return jsonify({"success": False, "error": tr("telegram.err.tagsList")}), 400
    names = cat_svc.parse_tags(raw)
    too_long = [n for n in names if len(n) > cat_svc.TAG_NAME_MAX]
    if too_long:
        return jsonify({"success": False,
                        "error": tr("telegram.err.tagTooLong", max=cat_svc.TAG_NAME_MAX,
                                  name=too_long[0])}), 400

    with _conn(_db_path()) as conn:
        if not conn.execute("SELECT 1 FROM tg_monitored_chats WHERE chat_id = ?",
                            (chat_id,)).fetchone():
            return jsonify({"success": False, "error": tr("telegram.err.chatNotMonitored")}), 404
        changed = cat_svc.apply_chat_tags(conn, chat_id, names)
        tags = cat_svc.chat_tags(conn, chat_id)
    reembed_stats = _schedule_reembed(changed, "chat-tags")
    return jsonify({"success": True, "chat_id": chat_id, "tags": tags,
                    "records_changed": len(changed), "reembed": reembed_stats})


@telegram_bp.route('/api/telegram/chats/toggle', methods=['POST'])
def tg_chats_toggle():
    """T2.6 (REMEDIATION_PLAN Wave 2): CLI-only upsert for
    `telegram_listener.py enable/disable <chat_id>`. Earlier these commands wrote
    to tg_monitored_chats DIRECTLY from a separate process (the same SQLite file
    as app.py, via WAL) — violating the documented invariant «only app.py
    writes to the DB». Now the listener only POSTs here (like /api/telegram/ingest),
    and Flask performs the write.

    DELIBERATELY a separate endpoint from /api/telegram/chats (used by the UI):
    that one is protected only by the general session auth gate (localhost/API-key), and here the
    caller is another local PROCESS without a browser session, so the same
    shared-secret as in ingest is needed (_authorized: localhost + X-Telegram-
    Token). The semantics are identical to the old direct SQL from telegram_listener.py:
    it updates only title/username/chat_type/enabled, does NOT touch category_id
    (so as not to erase a direction binding made in the UI)."""
    if not _authorized():
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": tr("telegram.err.chatIdRequired")}), 400
    try:
        chat_id = int(data["chat_id"])
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": tr("telegram.err.chatIdNumber")}), 400

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
    """Difference in days between two ISO dates (both may be None)."""
    if not later or not earlier:
        return None
    try:
        a = datetime.fromisoformat(later[:19].replace(" ", "T"))
        b = datetime.fromisoformat(earlier[:19].replace(" ", "T"))
    except ValueError:
        return None
    return (a.date() - b.date()).days


def _coverage_verdict(row: dict, live: dict | None, listed: bool) -> str:
    """Why a chat is quiet: is it really asleep or does the listener not hear it.

    Neither the owner nor the agent had an answer to exactly this — «12 chats are silent
    for over a week» looked the same as a dead project and as a broken ingest.
    """
    if live and live.get("migrated_to"):
        return "migrated"          # chat_id changed → a dead id in tg_monitored_chats
    if not row.get("enabled"):
        # A disabled chat SHOULD lag — we deliberately do not listen to it. Without this
        # branch the report screamed «lagging by 170 days» about something that works as designed,
        # and the real two or three problems drowned among false alarms.
        return "not_monitored"
    if not listed:
        return "not_listed"        # not among the dialogs: left/deleted/beyond the limit
    if not row.get("archived_msgs"):
        return "never_ingested"
    live_day = _iso_day((live or {}).get("last_message_date"))
    arch_day = _iso_day(row.get("archived_last_date"))
    if live_day and arch_day and live_day > arch_day:
        return "behind"            # Telegram has newer than the archive → the listener lagged
    return "ok"


@telegram_bp.route('/api/telegram/coverage', methods=['GET'])
def tg_coverage():
    """Wave 1 «See»: the archive against live Telegram, for each monitored chat.

    The live cursor comes from the listener's iter_dialogs pass (Dialog.message) —
    without a second Telethon session and without a single extra request to Telegram.

    Listener offline is NOT an error: we return the archive half (when the chat last
    reached the archive), because it answers the question «what do we know about this chat»
    even without the network. live=false in the response.

    Lag in MESSAGES is counted only for supergroups (-100…): in legacy groups
    and personal chats message_id comes from the account's global sequence, so
    id arithmetic there is meaningless (in live data that is 15 chats of 19).
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
        listed = (not ok) or (live is not None)   # without the listener we do not judge «not listed»
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
        # Delta by id — only where ids are sequential inside the chat.
        if ok and str(row["chat_id"]).startswith("-100") and live and live.get("last_message_id"):
            entry["behind_messages"] = max(
                0, int(live["last_message_id"]) - int(row["archived_last_id"] or 0))
        else:
            entry["behind_messages"] = None
        entry["status"] = _coverage_verdict(row, live, listed) if ok else "unknown"
        out.append(entry)

    try:
        with _conn(_db_path()) as conn:
            media_unparsed = conn.execute(
                "SELECT COUNT(*) FROM transcriptions WHERE source_type = 'telegram' "
                "AND model_used = ? AND deleted_at IS NULL", (PARSE_FAILED,)).fetchone()[0]
    except Exception:
        logger.warning("[tg] coverage: counter of unrecognized media not read", exc_info=True)
        media_unparsed = None

    return jsonify({
        "success": True,
        "live": ok,
        "media_unparsed": media_unparsed,
        "listener": None if ok else data.get("error"),
        "dialogs_seen": len(live_by_id) if ok else None,
        "dialog_limit": data.get("limit") if ok else None,
        "chats": out,
        "note": tr("telegram.coverage.note"),
    })


@telegram_bp.route('/api/telegram/edited', methods=['POST'])
def tg_edited():
    """A message was edited — update the text (Wave 4).

    Earlier the archive was written once and never fixed: `_is_duplicate` on a repeated
    visit returned the existing id and exited WITHOUT updating the text, and there was no subscription to
    MessageEdited at all. In work chats a correcting edit is the norm
    (amount, date, address), and the archive forever kept the first, wrong version."""
    if not _authorized():
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    chat_id, message_id = data.get("chat_id"), data.get("message_id")
    text = (data.get("text") or "").strip()
    if chat_id is None or message_id is None:
        return jsonify({"success": False, "error": tr("telegram.err.needMessageId")}), 400

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
            # we reset embedded_at: the old vector describes the old revision, and without
            # the reset search would find text that no longer exists.
            "UPDATE transcriptions SET transcript_text = ?, tg_edit_date = ?, "
            "embedded_at = NULL WHERE id = ?",
            (text, data.get("edit_date"), tid))
        conn.commit()
    enrich = _submit_embed_only(tid, db_path, force=True)
    logger.info("[tg] edit tx=%d (chat=%s msg=%s) embed=%s", tid, chat_id, message_id, enrich)
    return jsonify({"success": True, "transcription_id": tid, "updated": True,
                    "enrichment": {"status": enrich}})


@telegram_bp.route('/api/telegram/deleted', methods=['POST'])
def tg_deleted():
    """A message was deleted — softly remove it from the archive (Wave 4).

    Without this Recall quotes what is no longer in the chat. deleted_at is respected by both
    search and scope, so the record disappears from results immediately."""
    if not _authorized():
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id")
    ids = data.get("message_ids") or []
    if chat_id is None or not ids:
        return jsonify({"success": False, "error": tr("telegram.err.needMessageIds")}), 400

    with _conn(_db_path()) as conn:
        ph = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE transcriptions SET deleted_at = CURRENT_TIMESTAMP "
            f"WHERE tg_chat_id = ? AND tg_message_id IN ({ph}) AND deleted_at IS NULL",
            [chat_id] + [int(i) for i in ids])
        conn.commit()
        marked = cur.rowcount
    if marked:
        logger.info("[tg] deleted by the author: %d record(s) (chat=%s)", marked, chat_id)
    return jsonify({"success": True, "marked": marked})


def _is_supergroup(chat_id) -> bool:
    """ids are sequential inside a chat only in supergroups (-100…). In legacy groups
    and personal chats message_id comes from the account's global sequence,
    so a «gap in ids» there means nothing (15 chats of 19 in live data)."""
    return str(chat_id).startswith("-100")


def _missing_ids(db_path: str, chat_id: int, live_last_id: int | None = None) -> list[int]:
    """ids missing from the archive: gaps inside [min…max] + the tail up to the live last one.

    We do not go below min — that is no longer repair but history loading (a separate backfill):
    telling «we lost this» from «we have not been there yet» matters, because the first
    must be fixed, and the second is the owner's decision.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT tg_message_id AS mid FROM transcriptions "
            "WHERE tg_chat_id = ? AND tg_message_id IS NOT NULL", (chat_id,)).fetchall()
    have = {int(r["mid"]) for r in rows}
    if not have:
        return []                      # an empty chat is a backfill, not a repair
    lo, hi = min(have), max(have)
    missing = [i for i in range(lo, hi + 1) if i not in have]
    if live_last_id and live_last_id > hi:
        missing.extend(range(hi + 1, int(live_last_id) + 1))
    return missing


@telegram_bp.route('/api/telegram/repair', methods=['POST'])
def tg_repair():
    """Wave 3 «Repair the past»: targeted repair of gaps in a supergroup.

    Body: {chat_id, dry_run=false, max_messages=200}.

    Fetches SPECIFIC missing ids via get_messages(ids=…) — in place of deleted ones
    Telegram returns None, so in the response `deleted` (deleted by the author) is separated
    from `ingested` (lost by the listener). This refines the estimate «359 skipped»,
    which until now was an upper bound: a gap in ids could also be a sticker,
    and a service message, and a deleted one.

    The batch is bounded by max_messages — the call stays time-bounded, and the response
    `remaining` shows how many are left (repeat until zero).
    """
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": tr("telegram.err.chatIdRequired")}), 400
    try:
        chat_id = int(data["chat_id"])
        max_messages = max(1, min(int(data.get("max_messages", 200)), 500))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": tr("telegram.err.repairNumbers")}), 400

    if not _is_supergroup(chat_id):
        return jsonify({
            "success": False,
            "error": tr("telegram.err.repairSupergroup"),
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
                        "note": tr("telegram.gaps.none")})

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
    """Load a chat's history (proxy to the listener; the backfill itself — Phase 17D)."""
    data = request.get_json(silent=True) or {}
    if data.get("chat_id") is None:
        return jsonify({"success": False, "error": tr("telegram.err.chatIdRequired")}), 400
    ok, res = _listener_call("/backfill", method="POST", body={
        "chat_id": data.get("chat_id"),
        "limit": data.get("limit", 200),
    }, timeout=30)
    if not ok:
        return jsonify({"success": False, **res}), 503
    return jsonify({"success": True, **res})
