"""Claude actions over one transcript: polish, summarize, sentiment, translate, topics, enrich.

One place knows the cache column of each kind: ``cached`` reads it and spends nothing, ``run``
answers from it unless ``force`` and otherwise calls Claude and writes the result back. The five
synchronous routes in ``app/blueprints/transcription.py`` are thin views over ``run``;
``submit`` queues a ``transcript_ai`` job for the background route (no ``force``, no ``model``).
``enrich`` goes through ``enrichment.enrich_transcription`` and is refused during a recording.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Optional

from app import state
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.repositories import transcriptions as tx_repo
from app.services import text_polishing
from app.services.recording.state import recording_active

logger = logging.getLogger(__name__)

KINDS = ("polish", "summarize", "sentiment", "translate", "topics", "enrich")
#: Target languages the background route accepts; the sync translate route keeps its own check.
TARGET_LANGS = ("en", "uk", "ru")
JOB_KIND = "transcript_ai"

_submit_lock = threading.Lock()


class TranscriptAIError(Exception):
    """A refusal or a failure; ``code`` is an ``error_codes`` key, ``status`` the HTTP status.

    ``sync_body`` is what the five sync routes answered before (no ``error_code``);
    ``body`` adds the code for the background route."""

    def __init__(self, code: str, status: int, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.error_code = code
        self.status = status
        self.message = message
        self.extra = extra

    def sync_body(self) -> dict:
        return {"success": False, "error": self.message}

    def body(self) -> dict:
        return {"success": False, "error": self.message, "error_code": self.code, **self.extra}


def _metric(name: str, **kw) -> None:
    metrics = getattr(state, "metrics", None)
    if metrics is not None:
        metrics.inc(name, **kw)


def _not_found() -> TranscriptAIError:
    return TranscriptAIError("NOT_FOUND", 404, tr("transcript.err.tx_not_found"))


def _empty() -> TranscriptAIError:
    return TranscriptAIError("EMPTY", 400, tr("transcript.err.tx_empty"))


def _claude_empty() -> TranscriptAIError:
    return TranscriptAIError("INTERNAL_ERROR", 500, tr("transcript.err.claude_empty"))


def _recording_refusal() -> TranscriptAIError:
    return TranscriptAIError("RECORDING_ACTIVE", 409, tr("transcript.err.ai_enrich_recording"))


def _key_refusal(kind: str) -> Optional[TranscriptAIError]:
    """The missing-key refusal each kind answered before, or None when the key is there."""
    if kind == "enrich":
        from app.services import enrichment
        if enrichment.any_available():
            return None
        return TranscriptAIError("BAD_REQUEST", 400, tr("knowledge.memory.err.no_key_no_embeddings"))
    if text_polishing.is_available():
        return None
    key = "transcript.err.api_key_env" if kind in ("polish", "summarize") else "transcript.err.api_key_unset"
    return TranscriptAIError("BAD_REQUEST", 400, tr(key))


def _call(label: str, fn, *args, error_msg: Optional[str] = None, **kwargs):
    """A text_polishing.* call: RuntimeError -> 400 with its text, anything else logged + 500."""
    try:
        return fn(*args, **kwargs)
    except RuntimeError as e:
        raise TranscriptAIError("BAD_REQUEST", 400, str(e)) from e
    except Exception as e:
        logger.error(f"[{label}] error: {e}", exc_info=True)
        raise TranscriptAIError("INTERNAL_ERROR", 500,
                                error_msg or tr("transcript.err.claude_api_logs")) from e


def _load_speaker_context(conn, tid: int, segments_json) -> tuple[list[dict], bool, dict[str, str]]:
    """Parse `segments` JSON and, if diarized, load the raw_label->name map."""
    segments = json.loads(segments_json) if segments_json else []
    has_speakers = any(s.get("speaker") for s in segments)
    speaker_map: dict[str, str] = {}
    if has_speakers:
        rows = conn.execute('''
            SELECT m.raw_label, s.name
            FROM transcription_speaker_map m
            LEFT JOIN speakers s ON s.id = m.speaker_id
            WHERE m.transcription_id = ?
        ''', (tid,)).fetchall()
        for r in rows:
            if r["name"]:
                speaker_map[r["raw_label"]] = r["name"]
    return segments, has_speakers, speaker_map


def _json(raw):
    """Parsed JSON of a cache column, or None when empty or broken (broken cache -> regenerate)."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _check_kind(kind) -> None:
    if kind not in KINDS:
        raise TranscriptAIError("INVALID_ARGUMENT", 400, tr("transcript.err.ai_kind_invalid"))


# ============================================================ cache

def _cached_from_row(kind: str, row, target_lang: Optional[str]) -> Optional[dict]:
    if kind == "polish":
        return {"polished_text": row["polished_text"]} if row["polished_text"] else None
    if kind == "summarize":
        return _json(row["summary_json"])
    if kind == "sentiment":
        return _json(row["sentiment_json"])
    if kind == "topics":
        parsed = _json(row["topics_json"])
        return None if parsed is None else {"topics": parsed.get("topics", [])}
    if kind == "translate":
        translations = _json(row["translations_json"]) or {}
        entry = translations.get(target_lang) if target_lang else None
        if entry is None:
            return None
        return {"translated_text": entry.get("text", ""), "target_lang": target_lang,
                "model": entry.get("model", "")}
    return None


_CACHE_COLUMNS = {
    "polish": ["id", "polished_text"],
    "summarize": ["id", "summary_json"],
    "sentiment": ["id", "sentiment_json"],
    "topics": ["id", "topics_json"],
    "translate": ["id", "translations_json"],
    "enrich": ["id", "enriched_at", "enriched_model", "enrichment_version", "summary_json",
               "topics_json", "embedded_at", "embedding_model", "embedding_version"],
}


def _enrich_done(row) -> bool:
    from app.services import embeddings, enrichment
    return (bool(row["enriched_at"]) and row["enrichment_version"] == enrichment.ENRICHMENT_VERSION
            and bool(row["embedded_at"]) and row["embedding_model"] == embeddings.EMBED_MODEL
            and row["embedding_version"] == embeddings.EMBED_VERSION)


def cached(kind: str, tid: int, *, target_lang: Optional[str] = None, db_path: str) -> Optional[dict]:
    """The stored result of ``kind`` for record ``tid``, or None; never calls Claude.

    Raises ``TranscriptAIError`` NOT_FOUND for an unknown record, INVALID_ARGUMENT for a bad kind."""
    _check_kind(kind)
    with get_db_connection(db_path) as conn:
        row = tx_repo.get_by_id(conn, tid, columns=_CACHE_COLUMNS[kind])
    if row is None:
        raise _not_found()
    if kind != "enrich":
        return _cached_from_row(kind, row, target_lang)
    if not _enrich_done(row):
        return None
    summary = _json(row["summary_json"])
    topics = _json(row["topics_json"]) or {}
    return {"transcription_id": tid, "status": "done", "enriched_at": row["enriched_at"],
            "enriched_model": row["enriched_model"], "summary": summary,
            "topics": topics.get("topics", [])}


# ============================================================ run

def run(kind: str, tid: int, *, target_lang: Optional[str] = None, model: Optional[str] = None,
        force: bool = False, db_path: str) -> dict:
    """Answer ``kind`` from the cache unless ``force``, else call Claude and store the result.

    Returns ``{"cached": bool, "result": dict}``; ``result`` is the sync route body without
    ``success``/``cached``. A refusal raises ``TranscriptAIError``."""
    _check_kind(kind)
    tid = int(tid)
    if kind == "enrich":
        return _run_enrich(tid, model=model, force=force, db_path=db_path)
    refusal = _key_refusal(kind)
    if refusal is not None:
        raise refusal
    return _RUNNERS[kind](tid, model=model, force=force, db_path=db_path, target_lang=target_lang)


def _run_polish(tid, *, model, force, db_path, target_lang=None) -> dict:
    with get_db_connection(db_path) as conn:
        row = tx_repo.get_by_id(conn, tid, columns=["id", "transcript_text", "polished_text", "segments"])
        if not row:
            raise _not_found()
        if row["polished_text"] and not force:
            return {"cached": True, "result": {"polished_text": row["polished_text"]}}
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, row["segments"])

    raw_text = row["transcript_text"] or ""
    if not raw_text.strip() and not has_speakers:
        raise _empty()

    if has_speakers:
        logger.info(
            "[polish] using diarized polish for transcription %d (%d segments, %d named speakers)",
            tid, len(segments), len(speaker_map),
        )
        result = _call("polish", text_polishing.polish_diarized_transcript, segments, speaker_map,
                       model=model)
    else:
        result = _call("polish", text_polishing.polish_transcript, raw_text, model=model)

    polished = result["polished_text"]
    if not polished:
        raise _claude_empty()

    with get_db_connection(db_path) as conn:
        conn.execute(
            'UPDATE transcriptions SET polished_text = ?, polished_at = CURRENT_TIMESTAMP, polished_model = ? WHERE id = ?',
            (polished, result["model"], tid),
        )
        conn.commit()

    m = result["model"]
    _metric("whisper_polish_total", model=m)
    _metric("whisper_polish_tokens_total", value=result["input_tokens"], model=m, kind="input")
    _metric("whisper_polish_tokens_total", value=result["output_tokens"], model=m, kind="output")
    _metric("whisper_polish_tokens_total", value=result["cache_read_tokens"], model=m, kind="cache_read")

    return {"cached": False, "result": {
        "polished_text": polished,
        "model": m,
        "usage": {
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "cache_read_tokens": result["cache_read_tokens"],
            "cache_creation_tokens": result["cache_creation_tokens"],
        },
    }}


def _run_summarize(tid, *, model, force, db_path, target_lang=None) -> dict:
    with get_db_connection(db_path) as conn:
        row = tx_repo.get_by_id(conn, tid, columns=[
            "id", "transcript_text", "polished_text", "segments", "summary_json",
        ])
        if not row:
            raise _not_found()
        if not force:
            parsed = _json(row["summary_json"])
            if parsed is not None:
                return {"cached": True, "result": parsed}
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, row["segments"])

    raw_text = row["transcript_text"] or ""
    polished = row["polished_text"] or ""

    # Priority: polished > diarized formatted > raw
    diarized_text = None
    if has_speakers and speaker_map:
        # Reuse the polish formatter - it forms 'Name: text\n\nName: text'.
        diarized_text = text_polishing._format_diarized_input(segments, speaker_map)

    body = polished if polished.strip() else (diarized_text or raw_text)
    if not body or not body.strip():
        raise _empty()

    result = _call(
        "summarize", text_polishing.summarize_transcript,
        text=body if not diarized_text else raw_text,
        diarized_text=diarized_text,
        model=model,
    )

    payload = {
        "summary": result["summary"],
        "key_points": result["key_points"],
        "action_items": result["action_items"],
    }
    with get_db_connection(db_path) as conn:
        conn.execute(
            'UPDATE transcriptions SET summary_json = ?, summary_at = CURRENT_TIMESTAMP, '
            'summary_model = ? WHERE id = ?',
            (json.dumps(payload, ensure_ascii=False), result["model"], tid),
        )
        conn.commit()

    m = result["model"]
    _metric("whisper_summarize_total", model=m)
    _metric("whisper_summarize_tokens_total", value=result["input_tokens"], model=m, kind="input")
    _metric("whisper_summarize_tokens_total", value=result["output_tokens"], model=m, kind="output")

    return {"cached": False, "result": {
        "model": m,
        "usage": {
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "cache_read_tokens": result["cache_read_tokens"],
        },
        **payload,
    }}


def _run_sentiment(tid, *, model, force, db_path, target_lang=None) -> dict:
    with get_db_connection(db_path) as conn:
        row = tx_repo.get_by_id(conn, tid, columns=["id", "segments", "sentiment_json"])
        if not row:
            raise _not_found()
        if not force:
            parsed = _json(row["sentiment_json"])
            if parsed is not None:
                return {"cached": True, "result": parsed}
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, row["segments"])
        if not has_speakers:
            raise TranscriptAIError("BAD_REQUEST", 400, tr("transcript.err.sentiment_needs_diarization"))

    diarized_text = text_polishing._format_diarized_input(segments, speaker_map)
    if not diarized_text.strip():
        raise _empty()

    result = _call("sentiment", text_polishing.analyze_sentiment, diarized_text, model=model,
                   error_msg=tr("transcript.err.claude_api"))

    payload = {"speakers": result["speakers"], "model": result["model"]}
    with get_db_connection(db_path) as conn:
        conn.execute(
            'UPDATE transcriptions SET sentiment_json = ? WHERE id = ?',
            (json.dumps(payload, ensure_ascii=False), tid),
        )
        conn.commit()

    _metric("whisper_sentiment_total", model=result["model"])
    return {"cached": False, "result": payload}


def _run_translate(tid, *, model, force, db_path, target_lang=None) -> dict:
    target_lang = (target_lang or "").strip().lower()
    if not target_lang or len(target_lang) > 5:
        raise TranscriptAIError("INVALID_ARGUMENT", 400, tr("transcript.err.target_lang_required"))

    with get_db_connection(db_path) as conn:
        row = tx_repo.get_by_id(conn, tid, columns=[
            "id", "transcript_text", "polished_text", "segments", "language", "translations_json",
        ])
        if not row:
            raise _not_found()
        if row["language"] and row["language"].lower() == target_lang:
            raise TranscriptAIError("INVALID_ARGUMENT", 400,
                                    tr("transcript.err.already_in_lang", lang=target_lang))

        translations = _json(row["translations_json"])
        if not isinstance(translations, dict):
            translations = {}
        if target_lang in translations and not force:
            return {"cached": True, "result": {
                "translated_text": translations[target_lang].get("text", ""),
                "target_lang": target_lang,
                "model": translations[target_lang].get("model", ""),
            }}
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, row["segments"])

    raw_text = row["transcript_text"] or ""
    polished = row["polished_text"] or ""
    diarized_text = None
    if has_speakers and speaker_map:
        diarized_text = text_polishing._format_diarized_input(segments, speaker_map)
    body_text = polished if polished.strip() else raw_text

    if not body_text.strip() and not diarized_text:
        raise _empty()

    result = _call("translate", text_polishing.translate_transcript,
                   text=body_text, target_lang=target_lang, diarized_text=diarized_text, model=model)

    translated = result["translated_text"]
    if not translated:
        raise _claude_empty()

    translations[target_lang] = {
        "text": translated,
        "model": result["model"],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with get_db_connection(db_path) as conn:
        conn.execute(
            'UPDATE transcriptions SET translations_json = ? WHERE id = ?',
            (json.dumps(translations, ensure_ascii=False), tid),
        )
        conn.commit()

    m = result["model"]
    _metric("whisper_translate_total", model=m, target_lang=target_lang)
    _metric("whisper_translate_tokens_total", value=result["input_tokens"], model=m, kind="input")
    _metric("whisper_translate_tokens_total", value=result["output_tokens"], model=m, kind="output")

    return {"cached": False, "result": {
        "translated_text": translated,
        "target_lang": target_lang,
        "model": m,
        "usage": {
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "cache_read_tokens": result["cache_read_tokens"],
        },
    }}


def _run_topics(tid, *, model, force, db_path, target_lang=None) -> dict:
    with get_db_connection(db_path) as conn:
        row = tx_repo.get_by_id(conn, tid, columns=[
            "id", "transcript_text", "polished_text", "segments", "topics_json",
        ])
        if not row:
            raise _not_found()
        if not force:
            parsed = _json(row["topics_json"])
            if parsed is not None:
                return {"cached": True, "result": {"topics": parsed.get("topics", [])}}
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, row["segments"])

    body_text = row["polished_text"] or row["transcript_text"] or ""
    diarized_text = None
    if has_speakers and speaker_map:
        diarized_text = text_polishing._format_diarized_input(segments, speaker_map)

    if not body_text.strip() and not diarized_text:
        raise _empty()

    result = _call("topics", text_polishing.extract_topics,
                   text=body_text, diarized_text=diarized_text, model=model,
                   error_msg=tr("transcript.err.claude_api"))

    payload = {"topics": result["topics"], "model": result["model"]}
    with get_db_connection(db_path) as conn:
        conn.execute(
            'UPDATE transcriptions SET topics_json = ? WHERE id = ?',
            (json.dumps(payload, ensure_ascii=False), tid),
        )
        conn.commit()

    _metric("whisper_topics_total", model=result["model"])
    return {"cached": False, "result": {"topics": result["topics"], "model": result["model"]}}


def _run_enrich(tid, *, model, force, db_path) -> dict:
    if recording_active():
        raise _recording_refusal()
    if not force:
        hit = cached("enrich", tid, db_path=db_path)
        if hit is not None:
            return {"cached": True, "result": hit}
    refusal = _key_refusal("enrich")
    if refusal is not None:
        raise refusal
    from app.services import enrichment
    try:
        res = enrichment.enrich_transcription(db_path, tid, model=model, force=force)
    except Exception as e:
        logger.error("[transcript_ai] enrich tx=%s failed: %s", tid, e, exc_info=True)
        raise TranscriptAIError("INTERNAL_ERROR", 500, tr("knowledge.memory.err.enrich_failed")) from e
    if res.get("status") == "not_found":
        raise _not_found()
    return {"cached": False, "result": res}


_RUNNERS = {
    "polish": _run_polish,
    "summarize": _run_summarize,
    "sentiment": _run_sentiment,
    "translate": _run_translate,
    "topics": _run_topics,
}


# ============================================================ background job

def active_job_id(tid: int, kind: str, target_lang: Optional[str] = None) -> Optional[str]:
    """The queued/running ``transcript_ai`` job for ``(tid, kind, target_lang)``, if any."""
    queue = getattr(state, "job_queue", None)
    if queue is None:
        return None
    for job in queue.list(kind=JOB_KIND, active_only=True):
        meta = job.meta or {}
        if (meta.get("transcription_id") == int(tid) and meta.get("kind") == kind
                and meta.get("target_lang") == target_lang):
            return job.id
    return None


def submit(kind: str, tid: int, *, target_lang: Optional[str] = None, actor: Optional[str] = None,
           db_path: str) -> dict:
    """Answer from the cache, join a live job, or queue a new one.

    Returns ``{"cached": True, "result"}`` on a hit, else ``{"cached": False, "job_id", "created"}``.
    Arguments are validated by the caller (``kind`` in KINDS, ``target_lang`` in TARGET_LANGS)."""
    _check_kind(kind)
    tid = int(tid)
    if kind != "translate":
        target_lang = None
    elif target_lang not in TARGET_LANGS:
        raise TranscriptAIError("INVALID_ARGUMENT", 400, tr("transcript.err.ai_target_lang_invalid"))
    if kind == "enrich" and recording_active():
        raise _recording_refusal()
    hit = cached(kind, tid, target_lang=target_lang, db_path=db_path)
    if hit is not None:
        return {"cached": True, "result": hit}
    refusal = _key_refusal(kind)
    if refusal is not None:
        raise refusal
    if getattr(state, "job_queue", None) is None:
        raise TranscriptAIError("UNAVAILABLE", 503, tr("transcript.err.queue_unavailable"))
    with _submit_lock:
        live = active_job_id(tid, kind, target_lang)
        if live:
            return {"cached": False, "job_id": live, "created": False}
        job = state.job_queue.submit(
            JOB_KIND, _job, kind, tid, target_lang, db_path,
            meta={"transcription_id": tid, "kind": kind, "target_lang": target_lang,
                  "actor": actor, "stage": "queued"})
    return {"cached": False, "job_id": job.id, "created": True}


def _job(job, kind: str, tid: int, target_lang: Optional[str], db_path: str) -> Optional[dict]:
    if kind == "enrich" and recording_active():
        raise _recording_refusal()
    if job.is_cancelled():
        return None
    job.meta["stage"] = "running"
    out = run(kind, tid, target_lang=target_lang, db_path=db_path)
    job.meta["stage"] = "done"
    return {"kind": kind, "transcription_id": tid, "result": out["result"]}
