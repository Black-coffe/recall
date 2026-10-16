"""Document ingest service: one persistence path for uploads, folder import and mail attachments.

Flask-free: every function takes ``db_path`` explicitly, so it works from request handlers,
background workers and the mail sync alike.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Callable, Optional

from app import state
from app.db.connection import get_db_connection
from app.services import document_parser, text_polishing

logger = logging.getLogger(__name__)

# ============================================================
# Table description (Phase 16C)
# ============================================================

def table_preview(md: str, max_lines: int = 30) -> str:
    """A markdown table sample (header + first rows) for a Claude description."""
    lines = (md or "").splitlines()
    out = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        out += f"\n… (ще {len(lines) - max_lines} рядків)"
    return out


def describe_tabular(parsed: dict) -> int:
    """Phase 16C: for xlsx/csv mix an NL description of each sheet (Claude) into the text
    of the block - so the table becomes semantically findable. Mutates parsed (blocks + text).
    Optional: without ANTHROPIC_API_KEY or on error - only the tables remain.
    Returns the number of sheets for which a description was generated."""
    blocks = parsed.get("blocks")
    if not blocks or parsed.get("doc_type") not in ("xlsx", "csv"):
        return 0
    if not text_polishing.is_available():
        return 0
    try:
        sheets = [{
            "name": b.get("section") or f"Аркуш {b.get('page')}",
            "rows": b.get("rows"), "cols": b.get("cols"),
            "preview": table_preview(b.get("text", "")),
        } for b in blocks]
        descs = text_polishing.describe_sheets(sheets).get("descriptions") or []
    except Exception as e:
        logger.warning("describe_sheets failed (continuing with the tables): %s", e)
        return 0

    described = 0
    for b, d in zip(blocks, descs):
        d = (d or "").strip()
        if not d:
            continue
        name = b.get("section")
        head = f"[Опис таблиці «{name}»]" if name else "[Опис таблиці]"
        b["description"] = d
        b["text"] = f"{head}\n{d}\n\n{b['text']}"
        described += 1
    if described:
        parsed["text"] = "\n\n".join(b["text"] for b in blocks)
    return described


# ============================================================
# Shared helpers (upload / reparse / import)
# ============================================================

def structure_json(parsed: dict):
    blocks = parsed.get("blocks")
    return json.dumps(blocks, ensure_ascii=False) if blocks else None


def find_duplicate(db_path: str, content_hash: str):
    """An existing document with the same content_hash (dedup, 16E). None if there is none."""
    if not content_hash:
        return None
    with get_db_connection(db_path) as conn:
        # T4.6: a soft-deleted one does not count as a duplicate - otherwise re-uploading
        # the same file after delete would be "blocked" forever by a duplicate
        # the user cannot see at all (the undo window has not expired yet).
        row = conn.execute(
            "SELECT id, source_name FROM transcriptions "
            "WHERE content_hash = ? AND source_type = 'document' AND deleted_at IS NULL "
            "ORDER BY id LIMIT 1",
            (content_hash,),
        ).fetchone()
    return dict(row) if row else None


def persist_document(db_path: str, *, source_name: str, filepath: str, parsed: dict,
                      category_id, parse_time: float,
                      title: str | None = None, description: str | None = None) -> int:
    """INSERT a document into transcriptions. Returns transcription_id.
    Shared by upload and folder-import (does not depend on the request context).
    `title`/`description` are already normalized by the caller (editable-title-description-02)."""
    parsed_at = datetime.now().isoformat(timespec='seconds')
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        c.execute(
            '''INSERT INTO transcriptions
               (source_type, source_name, file_path, transcript_text, language,
                model_used, processing_time, segments, category_id,
                doc_type, original_filename, page_count, byte_size, content_hash,
                parsed_at, parser_version, structure_json, title, description)
               VALUES ('document', ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                source_name, filepath, parsed["text"], parse_time, category_id,
                parsed["doc_type"], source_name, parsed.get("page_count"),
                parsed.get("byte_size"), parsed.get("content_hash"),
                parsed_at, parsed.get("parser_version"), structure_json(parsed),
                title, description,
            ),
        )
        tid = c.lastrowid
        conn.commit()
    return tid


def submit_enrichment(transcription_id: int, db_path: str, force: bool = False) -> str:
    """Queue the SAME background enrichment job as for audio (chunk+embed +
    Claude card). db_path is passed explicitly - so it works outside the request context
    too (the folder-import worker). force=True - for reparse (the text changed → re-embed)."""
    try:
        from app.services import enrichment
        if not (enrichment.any_available() and state.job_queue is not None):
            return "unavailable"
        ch = f"enrich_{transcription_id}"

        def _job(job, _tid=transcription_id, _db=db_path, _c=ch, _force=force):
            try:
                state.sse_broker.publish(_c, "enrich", {"status": "running", "transcription_id": _tid})
            except Exception:
                pass
            res = enrichment.enrich_transcription(_db, _tid, force=_force)
            try:
                state.sse_broker.publish(_c, "complete", {"status": "completed", **res})
            except Exception:
                pass
            return res

        state.job_queue.submit("enrichment", _job, meta={"transcription_id": transcription_id})
        return "queued"
    except Exception as e:
        logger.warning("Enrichment job not queued (continuing): %s", e)
        return "error"


def _apply_filter(parsed: dict, text_filter: Callable[[str], str]) -> None:
    """Run text_filter over the text and every block, then recompute the content hash."""
    parsed["text"] = text_filter(parsed["text"])
    for b in parsed.get("blocks") or []:
        b["text"] = text_filter(b.get("text", ""))
    parsed["content_hash"] = document_parser.content_hash(parsed["text"])
    parsed["char_count"] = len(parsed["text"])


def ingest_file(db_path: str, path: str, *, original_name: str, category_id=None,
                enrich: bool = True, describe_tables: bool = True,
                text_filter: Optional[Callable[[str], str]] = None,
                store: Optional[Callable[[str], str]] = None) -> dict:
    """Parse a file on disk and persist it as a document. Never raises for unreadable
    files or empty parses.

    ``text_filter`` (masking) runs before hashing, so the stored text and the dedup hash
    both come from the filtered text. ``store(path) -> stored_path`` is called only once
    the file is known to be new (e.g. to copy it into the documents folder); otherwise
    the document points at ``path``.
    Returns {"status": "created"|"duplicate"|"no_text"|"error", "transcription_id": int|None}.
    """
    try:
        parsed = document_parser.parse_document(path, filename=original_name)
    except document_parser.NoTextFound as e:
        logger.info("[ingest] no text in '%s': %s", original_name, e)
        return {"status": "no_text", "transcription_id": None}
    except document_parser.DocumentParseError as e:
        logger.info("[ingest] skipped '%s': %s", original_name, e)
        return {"status": "error", "transcription_id": None}
    except Exception as e:
        logger.warning("[ingest] parse failed for '%s': %s", original_name, e)
        return {"status": "error", "transcription_id": None}

    try:
        if text_filter is not None:
            _apply_filter(parsed, text_filter)
            if not (parsed["text"] or "").strip():
                return {"status": "no_text", "transcription_id": None}
        dup = find_duplicate(db_path, parsed.get("content_hash"))
        if dup:
            return {"status": "duplicate", "transcription_id": dup["id"]}
        stored = store(path) if store is not None else path
        if describe_tables:
            describe_tabular(parsed)
        tid = persist_document(
            db_path, source_name=original_name, filepath=stored, parsed=parsed,
            category_id=category_id, parse_time=0.0,
        )
    except Exception as e:
        logger.warning("[ingest] failed for '%s': %s", original_name, e)
        return {"status": "error", "transcription_id": None}

    try:
        state.metrics.inc("whisper_documents_total", doc_type=parsed["doc_type"])
    except Exception:
        logger.debug("documents metric not recorded", exc_info=True)
    if enrich:
        submit_enrichment(tid, db_path)
    return {"status": "created", "transcription_id": tid}
