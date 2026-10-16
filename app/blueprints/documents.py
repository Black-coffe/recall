"""Loading documents into the RAG archive (Phase 16A–16E).

- POST /api/documents/upload            - file → text → transcriptions
- POST /api/documents/<id>/reparse      - re-parse the stored original (16E)
- POST /api/documents/import-folder     - bulk import from a folder (background job, 16E)

The document is stored as the ORIGINAL (file_path) + the parsed text in transcript_text
with source_type='document'. Then the SAME background enrichment job is reused
as for audio transcriptions (chunk+embed locally + Claude card) - so the document
immediately becomes a full participant of RAG search, the entity graph and directions.

Dedup (16E): re-uploading the same content (content_hash) does not create a
duplicate - the existing record is returned (override via the form field 'force').

Listing/viewing documents goes through the existing /api/history and /api/history/<id>
(filter source_type='document').
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

from app import state
from app.core.i18n import tr
from app.repositories import transcriptions as tx_repo
from app.services import document_ingest, document_parser, record_meta
from app.utils.files import allowed_document_file
from app.utils.paths import safe_path_within, safe_path_within_any


logger = logging.getLogger(__name__)
documents_bp = Blueprint('documents', __name__)


def _db_path() -> str:
    return current_app.config['DATABASE']


def _conn(db_path: str):
    from app.db.connection import get_db_connection
    return get_db_connection(db_path)


from app.services.document_ingest import (  # noqa: E402  (shared chain; names kept for callers)
    describe_tabular as _describe_tabular,
    find_duplicate as _find_duplicate,
    persist_document as _persist_document,
    structure_json as _structure_json,
    submit_enrichment as _submit_enrichment,
)


def _save_original(file, original_name: str) -> str:
    """Save the uploaded file to DOCUMENTS_FOLDER with a timestamp prefix.
    Returns the absolute path. Path traversal protection (as in /api/transcribe)."""
    docs_dir = os.path.abspath(current_app.config.get('DOCUMENTS_FOLDER', 'documents'))
    os.makedirs(docs_dir, exist_ok=True)

    safe = secure_filename(original_name) or "document"
    ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
    if ext and not safe.lower().endswith(f'.{ext}'):
        safe = f"{safe}.{ext}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{safe}"
    filepath = safe_path_within(docs_dir, os.path.join(docs_dir, filename))
    if filepath is None:
        raise ValueError("path traversal")
    file.save(filepath)
    return filepath


# ============================================================
# Upload
# ============================================================

@documents_bp.route('/api/documents/upload', methods=['POST'])
def upload_document():
    """Upload a document → parse → (dedup) → save → enrich."""
    if 'document' not in request.files:
        return jsonify({"success": False, "error": tr('library.doc.err.no_file')}), 400
    file = request.files['document']
    if not file or file.filename == '':
        return jsonify({"success": False, "error": tr('library.doc.err.no_file_selected')}), 400
    if not allowed_document_file(file.filename):
        supported = ", ".join(sorted(document_parser.ALLOWED_DOCUMENT_EXTENSIONS))
        return jsonify({"success": False,
                        "error": tr('library.doc.err.unsupported', supported=supported)}), 400

    # editable-title-description-02: validate BEFORE parsing/saving the file.
    try:
        title = record_meta.normalize_title(request.form.get('title'))
        description = record_meta.normalize_description(request.form.get('description'))
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    db_path = _db_path()
    force = (request.form.get('force') or '').lower() in ('1', 'true', 'yes', 'on')
    original_name = file.filename
    try:
        filepath = _save_original(file, original_name)
    except ValueError:
        return jsonify({"success": False, "error": tr('library.doc.err.bad_name')}), 400
    except Exception as e:
        logger.error("Could not save the document: %s", e)
        return jsonify({"success": False, "error": tr('library.doc.err.save_failed')}), 500

    # --- Parsing (synchronous) ---
    t0 = time.time()
    try:
        parsed = document_parser.parse_document(filepath, filename=original_name)
    except document_parser.DocumentParseError as e:
        _safe_remove(filepath)
        logger.info("Document '%s' was not parsed: %s", original_name, e)
        return jsonify({"success": False, "error": str(e)}), 422
    except Exception as e:
        _safe_remove(filepath)
        logger.error("Parsing error for '%s': %s", original_name, e, exc_info=True)
        return jsonify({"success": False, "error": tr('library.doc.err.parse_failed')}), 500
    parse_time = round(time.time() - t0, 3)

    # --- Dedup (16E): before the Claude description, so no call is burned on a duplicate ---
    if not force:
        dup = _find_duplicate(db_path, parsed.get("content_hash"))
        if dup:
            _safe_remove(filepath)
            return jsonify({
                "success": True, "duplicate": True,
                "transcription_id": dup["id"], "source_name": dup["source_name"],
                "message": tr('library.doc.err.duplicate'),
            })

    _cat_raw = request.form.get('category_id')
    category_id = int(_cat_raw) if (_cat_raw and _cat_raw.isdigit()) else None

    described_sheets = _describe_tabular(parsed)   # Phase 16C (optional)
    transcription_id = _persist_document(
        db_path, source_name=original_name, filepath=filepath, parsed=parsed,
        category_id=category_id, parse_time=parse_time,
        title=title, description=description,
    )
    state.metrics.inc("whisper_documents_total", doc_type=parsed["doc_type"])
    enrich_status = _submit_enrichment(transcription_id, db_path)

    logger.info("Document '%s' (%s, %d chars) → tx=%d in %.2fs",
                original_name, parsed["doc_type"], parsed["char_count"],
                transcription_id, parse_time)

    return jsonify({
        "success": True,
        "duplicate": False,
        "transcription_id": transcription_id,
        "source_name": original_name,
        "doc_type": parsed["doc_type"],
        "page_count": parsed.get("page_count"),
        "char_count": parsed["char_count"],
        "parse_time": parse_time,
        "needs_ocr": bool(parsed.get("meta", {}).get("needs_ocr")),
        "described_sheets": described_sheets,
        "enrichment": {"status": enrich_status},
    })


# ============================================================
# Re-parse (16E)
# ============================================================

@documents_bp.route('/api/documents/<int:tid>/reparse', methods=['POST'])
def reparse_document(tid: int):
    """Re-parse the stored document original. Useful after installing
    OCR/openpyxl/a Claude key or after a parser update. Forces re-embed."""
    db_path = _db_path()
    with _conn(db_path) as conn:
        row = tx_repo.get_by_id(
            conn, tid,
            columns=("id", "source_type", "file_path", "original_filename", "source_name"),
        )
    if not row or row["source_type"] != "document":
        return jsonify({"success": False, "error": tr('library.doc.err.not_found')}), 404
    filepath = row["file_path"]
    if not filepath or not os.path.exists(filepath):
        return jsonify({"success": False,
                        "error": tr('library.doc.err.no_original')}), 400

    fname = row["original_filename"] or row["source_name"] or os.path.basename(filepath)
    t0 = time.time()
    try:
        parsed = document_parser.parse_document(filepath, filename=fname)
    except document_parser.DocumentParseError as e:
        return jsonify({"success": False, "error": str(e)}), 422
    except Exception as e:
        logger.error("Reparse '%s' error: %s", fname, e, exc_info=True)
        return jsonify({"success": False, "error": tr('library.doc.err.parse_failed')}), 500
    parse_time = round(time.time() - t0, 3)

    _describe_tabular(parsed)
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE transcriptions SET transcript_text = ?, structure_json = ?, "
            "doc_type = ?, page_count = ?, byte_size = ?, content_hash = ?, "
            "parsed_at = ?, parser_version = ?, processing_time = ? WHERE id = ?",
            (
                parsed["text"], _structure_json(parsed), parsed["doc_type"],
                parsed.get("page_count"), parsed.get("byte_size"),
                parsed.get("content_hash"), datetime.now().isoformat(timespec='seconds'),
                parsed.get("parser_version"), parse_time, tid,
            ),
        )
        conn.commit()

    # the text changed → force full enrichment (re-embed + re-card)
    enrich_status = _submit_enrichment(tid, db_path, force=True)
    logger.info("Reparse tx=%d (%s, %d chars)", tid, parsed["doc_type"], parsed["char_count"])
    return jsonify({
        "success": True,
        "transcription_id": tid,
        "doc_type": parsed["doc_type"],
        "page_count": parsed.get("page_count"),
        "char_count": parsed["char_count"],
        "needs_ocr": bool(parsed.get("meta", {}).get("needs_ocr")),
        "enrichment": {"status": enrich_status},
    })


# ============================================================
# Folder import (16E) - background bulk import
# ============================================================

def _collect_documents(folder: str, recursive: bool) -> list[str]:
    out: list[str] = []
    if recursive:
        for root, _dirs, names in os.walk(folder):
            for n in sorted(names):
                if allowed_document_file(n):
                    out.append(os.path.join(root, n))
    else:
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if os.path.isfile(p) and allowed_document_file(n):
                out.append(p)
    return out


def _copy_into_docs(src: str, docs_dir: str) -> str:
    os.makedirs(docs_dir, exist_ok=True)
    base = secure_filename(os.path.basename(src)) or "document"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = safe_path_within(docs_dir, os.path.join(docs_dir, f"{ts}_{base}"))
    if dest is None:
        raise ValueError("path traversal")
    shutil.copy2(src, dest)
    return dest


def _run_folder_import(files: list[str], db_path: str, docs_dir: str, category_id) -> dict:
    """Worker (outside the request context): parses, dedups, saves, queues enrich
    for each file. Progress goes to the 'doc_import' SSE channel."""
    imported = skipped = failed = 0
    total = len(files)
    for i, src in enumerate(files):
        fname = os.path.basename(src)
        try:
            res = document_ingest.ingest_file(
                db_path, src, original_name=fname, category_id=category_id,
                store=lambda p: _copy_into_docs(p, docs_dir),
            )
            if res["status"] == "created":
                imported += 1
            elif res["status"] == "duplicate":
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("[import] error '%s': %s", src, e)
            failed += 1
        try:
            state.sse_broker.publish("doc_import", "progress", {
                "processed": i + 1, "total": total,
                "imported": imported, "skipped": skipped, "failed": failed,
            })
        except Exception:
            pass

    summary = {"total": total, "imported": imported, "skipped": skipped, "failed": failed}
    logger.info("[import] finished: %s", summary)
    try:
        state.sse_broker.publish("doc_import", "complete", {"status": "completed", **summary})
    except Exception:
        pass
    return summary


def _import_roots() -> list[str]:
    """Allowed root directories for import-folder (T1.3, Wave 1).

    `RECALL_IMPORT_ROOTS` in `.env` - a list of absolute paths separated by `;`.
    `;` was chosen rather than POSIX `:` because `:` conflicts with the drive letter (`C:\\...`)
    on Windows. Empty/unset → the list is empty → import-folder
    is forbidden everywhere (the default is "forbid until allowed" - safer
    than "allow everything except a list")."""
    raw = os.environ.get("RECALL_IMPORT_ROOTS", "")
    roots: list[str] = []
    for part in raw.split(";"):
        part = part.strip().strip('"')
        if part:
            roots.append(os.path.abspath(part))
    return roots


@documents_bp.route('/api/documents/import-folder', methods=['POST'])
def import_folder():
    """Bulk import of all supported documents from a folder (background job).
    Body: {path, recursive?, category_id?}. Results appear in History.

    T1.3 (Wave 1): `path` is allowed only inside the allowlist roots from
    `RECALL_IMPORT_ROOTS` (see `_import_roots`) - without it an arbitrary path
    combined with network access to the server gave the primitive "read
    any file the process can reach" (.env, SSH keys, etc.)."""
    data = request.get_json(silent=True) or {}
    folder = (data.get("path") or "").strip()
    if not folder:
        return jsonify({"success": False, "error": tr('library.doc.err.no_path')}), 400

    roots = _import_roots()
    if not roots:
        return jsonify({
            "success": False,
            "error": tr('library.doc.err.import_disabled'),
        }), 403

    resolved = safe_path_within_any(roots, folder)
    if resolved is None:
        return jsonify({
            "success": False,
            "error": tr('library.doc.err.import_forbidden'),
        }), 403
    folder = resolved

    if not os.path.isdir(folder):
        return jsonify({"success": False, "error": tr('library.doc.err.folder_missing')}), 400

    recursive = bool(data.get("recursive"))
    _cat = data.get("category_id")
    category_id = int(_cat) if (_cat is not None and str(_cat).isdigit()) else None

    files = _collect_documents(folder, recursive)
    if not files:
        return jsonify({"success": False,
                        "error": tr('library.doc.err.folder_empty')}), 400
    if state.job_queue is None:
        return jsonify({"success": False, "error": tr('library.doc.err.queue_unavailable')}), 503

    db_path = _db_path()
    docs_dir = os.path.abspath(current_app.config.get('DOCUMENTS_FOLDER', 'documents'))

    def _job(job, _files=files, _db=db_path, _docs=docs_dir, _cat=category_id):
        return _run_folder_import(_files, _db, _docs, _cat)

    state.job_queue.submit("doc_import", _job, meta={"count": len(files)})
    logger.info("[import] started: %d files from %s (recursive=%s)", len(files), folder, recursive)
    return jsonify({"success": True, "started": True, "total": len(files),
                    "channel": "doc_import"})


# ============================================================

def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
