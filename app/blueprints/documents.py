"""Підвантаження документів у RAG-архів (Phase 16A–16E).

- POST /api/documents/upload            — файл → текст → transcriptions
- POST /api/documents/<id>/reparse      — переразібрати збережений оригінал (16E)
- POST /api/documents/import-folder     — масовий імпорт з папки (фоновий job, 16E)

Документ зберігається як ОРИГІНАЛ (file_path) + розібраний текст у transcript_text
з source_type='document'. Далі переіспользується ТОЙ САМИЙ фоновий enrichment-job,
що й для аудіо-транскрипцій (chunk+embed локально + Claude-картка) — тож документ
одразу стає повноцінним учасником RAG-пошуку, графа сутностей і напрямків.

Дедуп (16E): повторне завантаження того самого вмісту (content_hash) не створює
дубль — повертається наявний запис (override через form 'force').

Список/перегляд документів — через існуючі /api/history та /api/history/<id>
(фільтр source_type='document').
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
from app.repositories import transcriptions as tx_repo
from app.services import document_parser, text_polishing
from app.utils.files import allowed_document_file
from app.utils.paths import safe_path_within, safe_path_within_any


logger = logging.getLogger(__name__)
documents_bp = Blueprint('documents', __name__)


def _db_path() -> str:
    return current_app.config['DATABASE']


def _conn(db_path: str):
    from app.db.connection import get_db_connection
    return get_db_connection(db_path)


# ============================================================
# Опис таблиць (Phase 16C)
# ============================================================

def _table_preview(md: str, max_lines: int = 30) -> str:
    """Зразок markdown-таблиці (заголовок + перші рядки) для опису через Claude."""
    lines = (md or "").splitlines()
    out = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        out += f"\n… (ще {len(lines) - max_lines} рядків)"
    return out


def _describe_tabular(parsed: dict) -> int:
    """Phase 16C: для xlsx/csv домішати NL-опис кожного аркуша (Claude) у текст
    блоку — щоб таблиця стала знаходимою семантично. Мутує parsed (blocks + text).
    Опційно: без ANTHROPIC_API_KEY або при помилці — лишаються тільки таблиці.
    Returns к-сть аркушів, для яких згенеровано опис."""
    blocks = parsed.get("blocks")
    if not blocks or parsed.get("doc_type") not in ("xlsx", "csv"):
        return 0
    if not text_polishing.is_available():
        return 0
    try:
        sheets = [{
            "name": b.get("section") or f"Аркуш {b.get('page')}",
            "rows": b.get("rows"), "cols": b.get("cols"),
            "preview": _table_preview(b.get("text", "")),
        } for b in blocks]
        descs = text_polishing.describe_sheets(sheets).get("descriptions") or []
    except Exception as e:
        logger.warning("describe_sheets не вдалось (продовжую з таблицями): %s", e)
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
# Спільні хелпери (upload / reparse / import)
# ============================================================

def _structure_json(parsed: dict):
    blocks = parsed.get("blocks")
    return json.dumps(blocks, ensure_ascii=False) if blocks else None


def _find_duplicate(db_path: str, content_hash: str):
    """Існуючий документ з тим самим content_hash (дедуп, 16E). None якщо немає."""
    if not content_hash:
        return None
    with _conn(db_path) as conn:
        # T4.6: soft-deleted не рахується дублем — інакше повторне завантаження
        # того самого файлу після delete назавжди «блокувалось» би дублем,
        # якого користувач узагалі не бачить (undo-вікно ще не сплило).
        row = conn.execute(
            "SELECT id, source_name FROM transcriptions "
            "WHERE content_hash = ? AND source_type = 'document' AND deleted_at IS NULL "
            "ORDER BY id LIMIT 1",
            (content_hash,),
        ).fetchone()
    return dict(row) if row else None


def _persist_document(db_path: str, *, source_name: str, filepath: str, parsed: dict,
                      category_id, parse_time: float) -> int:
    """INSERT документа у transcriptions. Returns transcription_id.
    Спільний для upload і folder-import (не залежить від request context)."""
    parsed_at = datetime.now().isoformat(timespec='seconds')
    with _conn(db_path) as conn:
        c = conn.cursor()
        c.execute(
            '''INSERT INTO transcriptions
               (source_type, source_name, file_path, transcript_text, language,
                model_used, processing_time, segments, category_id,
                doc_type, original_filename, page_count, byte_size, content_hash,
                parsed_at, parser_version, structure_json)
               VALUES ('document', ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                source_name, filepath, parsed["text"], parse_time, category_id,
                parsed["doc_type"], source_name, parsed.get("page_count"),
                parsed.get("byte_size"), parsed.get("content_hash"),
                parsed_at, parsed.get("parser_version"), _structure_json(parsed),
            ),
        )
        tid = c.lastrowid
        conn.commit()
    return tid


def _submit_enrichment(transcription_id: int, db_path: str, force: bool = False) -> str:
    """Поставити ТОЙ САМИЙ фоновий enrichment-job, що й для аудіо (chunk+embed +
    Claude-картка). db_path передається явно — щоб працювало і поза request context
    (folder-import worker). force=True — для reparse (текст змінився → re-embed)."""
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
        logger.warning("Enrichment job не поставлено (продовжую): %s", e)
        return "error"


def _save_original(file, original_name: str) -> str:
    """Зберегти завантажений файл у DOCUMENTS_FOLDER з timestamp-префіксом.
    Повертає абсолютний шлях. Захист від path traversal (як у /api/transcribe)."""
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
    """Підвантажити документ → розібрати → (дедуп) → зберегти → enrich."""
    if 'document' not in request.files:
        return jsonify({"success": False, "error": "Файл не знайдено"}), 400
    file = request.files['document']
    if not file or file.filename == '':
        return jsonify({"success": False, "error": "Файл не вибрано"}), 400
    if not allowed_document_file(file.filename):
        supported = ", ".join(sorted(document_parser.ALLOWED_DOCUMENT_EXTENSIONS))
        return jsonify({"success": False,
                        "error": f"Непідтримуваний формат. Дозволені: {supported}"}), 400

    db_path = _db_path()
    force = (request.form.get('force') or '').lower() in ('1', 'true', 'yes', 'on')
    original_name = file.filename
    try:
        filepath = _save_original(file, original_name)
    except ValueError:
        return jsonify({"success": False, "error": "Некоректне ім'я файлу"}), 400
    except Exception as e:
        logger.error("Не вдалося зберегти документ: %s", e)
        return jsonify({"success": False, "error": "Не вдалося зберегти файл"}), 500

    # --- Парсинг (синхронно) ---
    t0 = time.time()
    try:
        parsed = document_parser.parse_document(filepath, filename=original_name)
    except document_parser.DocumentParseError as e:
        _safe_remove(filepath)
        logger.info("Документ '%s' не розібрано: %s", original_name, e)
        return jsonify({"success": False, "error": str(e)}), 422
    except Exception as e:
        _safe_remove(filepath)
        logger.error("Помилка парсингу '%s': %s", original_name, e, exc_info=True)
        return jsonify({"success": False, "error": "Помилка розбору документа"}), 500
    parse_time = round(time.time() - t0, 3)

    # --- Дедуп (16E): до Claude-опису, щоб не палити виклик на дублі ---
    if not force:
        dup = _find_duplicate(db_path, parsed.get("content_hash"))
        if dup:
            _safe_remove(filepath)
            return jsonify({
                "success": True, "duplicate": True,
                "transcription_id": dup["id"], "source_name": dup["source_name"],
                "message": "Такий документ уже є в архіві",
            })

    _cat_raw = request.form.get('category_id')
    category_id = int(_cat_raw) if (_cat_raw and _cat_raw.isdigit()) else None

    described_sheets = _describe_tabular(parsed)   # Phase 16C (опційно)
    transcription_id = _persist_document(
        db_path, source_name=original_name, filepath=filepath, parsed=parsed,
        category_id=category_id, parse_time=parse_time,
    )
    state.metrics.inc("whisper_documents_total", doc_type=parsed["doc_type"])
    enrich_status = _submit_enrichment(transcription_id, db_path)

    logger.info("Документ '%s' (%s, %d симв.) → tx=%d за %.2fs",
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
    """Переразібрати збережений оригінал документа. Корисно після встановлення
    OCR/openpyxl/Claude-ключа або оновлення парсера. Форсує re-embed."""
    db_path = _db_path()
    with _conn(db_path) as conn:
        row = tx_repo.get_by_id(
            conn, tid,
            columns=("id", "source_type", "file_path", "original_filename", "source_name"),
        )
    if not row or row["source_type"] != "document":
        return jsonify({"success": False, "error": "Документ не знайдено"}), 404
    filepath = row["file_path"]
    if not filepath or not os.path.exists(filepath):
        return jsonify({"success": False,
                        "error": "Оригінал файлу відсутній — переразбір неможливий"}), 400

    fname = row["original_filename"] or row["source_name"] or os.path.basename(filepath)
    t0 = time.time()
    try:
        parsed = document_parser.parse_document(filepath, filename=fname)
    except document_parser.DocumentParseError as e:
        return jsonify({"success": False, "error": str(e)}), 422
    except Exception as e:
        logger.error("Reparse '%s' помилка: %s", fname, e, exc_info=True)
        return jsonify({"success": False, "error": "Помилка розбору документа"}), 500
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

    # текст змінився → форсуємо повне збагачення (re-embed + re-card)
    enrich_status = _submit_enrichment(tid, db_path, force=True)
    logger.info("Reparse tx=%d (%s, %d симв.)", tid, parsed["doc_type"], parsed["char_count"])
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
# Folder import (16E) — фоновий масовий імпорт
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
    """Worker (поза request context): парсить, дедупить, зберігає, ставить enrich
    кожен файл. Прогрес — у SSE-канал 'doc_import'."""
    imported = skipped = failed = 0
    total = len(files)
    for i, src in enumerate(files):
        fname = os.path.basename(src)
        try:
            parsed = document_parser.parse_document(src, filename=fname)
            if _find_duplicate(db_path, parsed.get("content_hash")):
                skipped += 1
            else:
                dest = _copy_into_docs(src, docs_dir)
                _describe_tabular(parsed)
                tid = _persist_document(
                    db_path, source_name=fname, filepath=dest, parsed=parsed,
                    category_id=category_id, parse_time=0.0,
                )
                try:
                    state.metrics.inc("whisper_documents_total", doc_type=parsed["doc_type"])
                except Exception:
                    pass
                _submit_enrichment(tid, db_path)
                imported += 1
        except document_parser.DocumentParseError as e:
            logger.info("[import] пропущено '%s': %s", src, e)
            failed += 1
        except Exception as e:
            logger.error("[import] помилка '%s': %s", src, e)
            failed += 1
        try:
            state.sse_broker.publish("doc_import", "progress", {
                "processed": i + 1, "total": total,
                "imported": imported, "skipped": skipped, "failed": failed,
            })
        except Exception:
            pass

    summary = {"total": total, "imported": imported, "skipped": skipped, "failed": failed}
    logger.info("[import] завершено: %s", summary)
    try:
        state.sse_broker.publish("doc_import", "complete", {"status": "completed", **summary})
    except Exception:
        pass
    return summary


def _import_roots() -> list[str]:
    """Дозволені кореневі директорії для import-folder (T1.3, Волна 1).

    `RECALL_IMPORT_ROOTS` у `.env` — список абсолютних шляхів, розділених `;`.
    Обрано `;`, а не POSIX `:`, бо `:` конфліктує з літерою диска (`C:\\...`)
    на Windows. Порожньо/не задано → список порожній → import-folder
    заборонений звідусіль (дефолт "заборонити, поки не дозволено" — безпечніше
    за "дозволити все, крім списку").
    """
    raw = os.environ.get("RECALL_IMPORT_ROOTS", "")
    roots: list[str] = []
    for part in raw.split(";"):
        part = part.strip().strip('"')
        if part:
            roots.append(os.path.abspath(part))
    return roots


@documents_bp.route('/api/documents/import-folder', methods=['POST'])
def import_folder():
    """Масовий імпорт усіх підтримуваних документів з папки (фоновий job).
    Body: {path, recursive?, category_id?}. Результати зʼявляються в Історії.

    T1.3 (Волна 1): `path` дозволений лише всередині allowlist-коренів з
    `RECALL_IMPORT_ROOTS` (див. `_import_roots`) — без нього довільний path
    у поєднанні з мережевим доступом до сервера давав примітив «прочитати
    будь-який файл, доступний процесу» (.env, SSH-ключі тощо)."""
    data = request.get_json(silent=True) or {}
    folder = (data.get("path") or "").strip()
    if not folder:
        return jsonify({"success": False, "error": "Не вказано шлях до папки"}), 400

    roots = _import_roots()
    if not roots:
        return jsonify({
            "success": False,
            "error": "Import-folder вимкнено: задайте RECALL_IMPORT_ROOTS у .env",
        }), 403

    resolved = safe_path_within_any(roots, folder)
    if resolved is None:
        return jsonify({
            "success": False,
            "error": "Ця папка недоступна для імпорту (поза дозволеними директоріями)",
        }), 403
    folder = resolved

    if not os.path.isdir(folder):
        return jsonify({"success": False, "error": "Папку не знайдено"}), 400

    recursive = bool(data.get("recursive"))
    _cat = data.get("category_id")
    category_id = int(_cat) if (_cat is not None and str(_cat).isdigit()) else None

    files = _collect_documents(folder, recursive)
    if not files:
        return jsonify({"success": False,
                        "error": "У папці немає підтримуваних документів"}), 400
    if state.job_queue is None:
        return jsonify({"success": False, "error": "Черга завдань недоступна"}), 503

    db_path = _db_path()
    docs_dir = os.path.abspath(current_app.config.get('DOCUMENTS_FOLDER', 'documents'))

    def _job(job, _files=files, _db=db_path, _docs=docs_dir, _cat=category_id):
        return _run_folder_import(_files, _db, _docs, _cat)

    state.job_queue.submit("doc_import", _job, meta={"count": len(files)})
    logger.info("[import] запущено: %d файлів з %s (recursive=%s)", len(files), folder, recursive)
    return jsonify({"success": True, "started": True, "total": len(files),
                    "channel": "doc_import"})


# ============================================================

def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
