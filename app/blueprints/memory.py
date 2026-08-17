"""Meeting Memory API (Phase 13) — наскрізний граф сутностей + RAG-архів.

Ендпоінти:
- POST /api/memory/backfill                     — масове збагачення історії (job + SSE)
- GET  /api/memory/backfill/status              — статус останнього backfill
- POST /api/memory/transcriptions/<id>/enrich   — збагатити один транскрипт
- GET  /api/memory/entities                     — список сутностей (фільтр type/q/sort)
- GET  /api/memory/entities/<id>                — деталі сутності: мітинги + задачі + aliases
- GET  /api/memory/action-items                 — задачі (фільтр status/owner)
- GET  /api/memory/stats                        — зведена статистика архіву

SSE-канал прогресу backfill: "backfill" (через /api/events/<channel> events_bp).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date as _date, timedelta as _timedelta

from flask import Blueprint, Response, current_app, jsonify, request

from app import state
from app.services import enrichment


logger = logging.getLogger(__name__)
memory_bp = Blueprint('memory', __name__)

# Останній статус backfill (для GET status; SSE дає live-прогрес).
_backfill_state: dict = {"running": False, "processed": 0, "total": 0,
                         "done": 0, "skipped": 0, "failed": 0}
_backfill_lock = threading.Lock()


def _get_db():
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


def _norm_cat(name: str) -> str:
    """Нормалізований ключ унікальності напрямку. casefold() згортає регістр для
    БУДЬ-ЯКОГО алфавіту (кирилиця теж), на відміну від SQLite COLLATE NOCASE
    (тільки ASCII). Сюди ж — стиск повторних пробілів."""
    return " ".join((name or "").strip().split()).casefold()


def _null_category_refs(conn, cid: int) -> None:
    """Обнулити category_id у всіх таблицях, що посилаються на напрямок (FK у
    SQLite не інлайнились, цілісність — у app-коді). Викликається при видаленні."""
    conn.execute("UPDATE transcriptions SET category_id = NULL WHERE category_id = ?", (cid,))
    for tbl in ("copilot_sessions", "tg_monitored_chats"):
        try:
            conn.execute(f"UPDATE {tbl} SET category_id = NULL WHERE category_id = ?", (cid,))
        except Exception:
            pass  # таблиці може не бути на старій БД


def _reassign_category_refs(conn, src: int, dst: "int | None") -> int:
    """Перенести всі записи з напрямку src → dst (для merge/move). Повертає
    к-сть перенесених транскриптів (copilot/tg переносимо тихо)."""
    n = conn.execute("UPDATE transcriptions SET category_id = ? WHERE category_id = ?",
                     (dst, src)).rowcount
    for tbl in ("copilot_sessions", "tg_monitored_chats"):
        try:
            conn.execute(f"UPDATE {tbl} SET category_id = ? WHERE category_id = ?", (dst, src))
        except Exception:
            pass
    return n


def _parse_category(raw) -> "int | str | None":
    """'5' → 5 (конкретний напрямок); 'none' → 'none' (без напрямку, IS NULL);
    '', 'all', None → None (усі напрямки)."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s or s == 'all':
        return None
    if s == 'none':
        return 'none'
    return int(s) if s.isdigit() else None


# ============================================================
# Напрямки / категорії (Phase 14)
# ============================================================

@memory_bp.route('/api/memory/categories', methods=['GET'])
def list_categories():
    """Список напрямків + лічильник транскриптів у кожному."""
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT c.id, c.name, c.slug, c.color, c.icon, c.sort_order, "
            "(SELECT COUNT(*) FROM transcriptions t "
            " WHERE t.category_id = c.id AND t.deleted_at IS NULL) AS count "
            "FROM categories c ORDER BY c.sort_order, c.name"
        ).fetchall()
        uncat = conn.execute(
            "SELECT COUNT(*) AS n FROM transcriptions "
            "WHERE category_id IS NULL AND deleted_at IS NULL"
        ).fetchone()["n"]
    return jsonify({"success": True, "categories": [dict(r) for r in rows],
                    "uncategorized": uncat})


@memory_bp.route('/api/memory/categories', methods=['POST'])
def create_category():
    """Створити напрямок. Body: {name, color?, icon?}. Унікальність — по
    casefold-ключу (регістронезалежно для кирилиці теж)."""
    data = request.get_json(silent=True) or {}
    name = " ".join((data.get("name") or "").strip().split())
    if not name:
        return jsonify({"success": False, "error": "Порожня назва"}), 400
    norm = _norm_cat(name)
    with _get_db() as conn:
        exists = conn.execute("SELECT name FROM categories WHERE name_norm = ?",
                              (norm,)).fetchone()
        if exists:
            return jsonify({"success": False,
                            "error": f"Напрямок «{exists['name']}» уже існує"}), 409
        mx = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM categories").fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO categories (name, name_norm, color, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
            (name, norm, data.get("color", "#6c757d"), data.get("icon", "fa-folder"), mx))
        conn.commit()
        cid = cur.lastrowid
    return jsonify({"success": True, "id": cid, "name": name,
                    "color": data.get("color", "#6c757d"), "icon": data.get("icon", "fa-folder")})


@memory_bp.route('/api/memory/categories/<int:cid>', methods=['PATCH'])
def update_category(cid: int):
    """Перейменувати/змінити колір/іконку/порядок напрямку.
    Body: {name?, color?, icon?, sort_order?}. Перейменування оновлює name_norm
    і перевіряє casefold-унікальність (виключаючи сам напрямок)."""
    data = request.get_json(silent=True) or {}
    fields, params = [], []
    with _get_db() as conn:
        if "name" in data and data["name"] is not None:
            name = " ".join(str(data["name"]).strip().split())
            if not name:
                return jsonify({"success": False, "error": "Порожня назва"}), 400
            norm = _norm_cat(name)
            clash = conn.execute(
                "SELECT name FROM categories WHERE name_norm = ? AND id != ?",
                (norm, cid)).fetchone()
            if clash:
                return jsonify({"success": False,
                                "error": f"Напрямок «{clash['name']}» уже існує"}), 409
            fields += ["name = ?", "name_norm = ?"]
            params += [name, norm]
        for key in ("color", "icon"):
            if key in data and data[key] is not None:
                fields.append(f"{key} = ?")
                params.append(str(data[key]).strip())
        if "sort_order" in data and str(data["sort_order"]).lstrip("-").isdigit():
            fields.append("sort_order = ?")
            params.append(int(data["sort_order"]))
        if not fields:
            return jsonify({"success": False, "error": "Нічого оновлювати"}), 400
        params.append(cid)
        cur = conn.execute(f"UPDATE categories SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"success": False, "error": "Напрямок не знайдено"}), 404
    return jsonify({"success": True})


@memory_bp.route('/api/memory/categories/<int:cid>', methods=['DELETE'])
def delete_category(cid: int):
    """Видалити напрямок. Усі записи (транскрипти/копілот/tg) стають 'без
    напрямку' (category_id=NULL)."""
    with _get_db() as conn:
        _null_category_refs(conn, cid)
        cur = conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"success": False, "error": "Напрямок не знайдено"}), 404
    return jsonify({"success": True})


@memory_bp.route('/api/memory/categories/<int:cid>/merge', methods=['POST'])
def merge_category(cid: int):
    """Перенести/обʼєднати напрямок. Body: {target_id: int|null, delete_source: bool}.
    Усі записи напрямку cid → target_id (null = «без напрямку»). Якщо
    delete_source=true — після перенесення напрямок cid видаляється.
    Сценарії: «перенести A→B і лишити A порожнім» (delete_source=false) або
    «обʼєднати A в B» (delete_source=true)."""
    data = request.get_json(silent=True) or {}
    raw = data.get("target_id")
    dst = int(raw) if (raw is not None and str(raw).isdigit()) else None
    delete_source = bool(data.get("delete_source"))
    if dst == cid:
        return jsonify({"success": False, "error": "Не можна обʼєднати напрямок із самим собою"}), 400
    with _get_db() as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone():
            return jsonify({"success": False, "error": "Напрямок не знайдено"}), 404
        if dst is not None and not conn.execute(
                "SELECT 1 FROM categories WHERE id = ?", (dst,)).fetchone():
            return jsonify({"success": False, "error": "Цільовий напрямок не знайдено"}), 404
        moved = _reassign_category_refs(conn, cid, dst)
        if delete_source:
            conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
        conn.commit()
    return jsonify({"success": True, "moved": moved, "deleted": delete_source})


@memory_bp.route('/api/memory/transcriptions/<int:tid>/category', methods=['PATCH'])
def set_transcription_category(tid: int):
    """Призначити напрямок транскрипту. Body: {category_id: int|null}."""
    data = request.get_json(silent=True) or {}
    cid = data.get("category_id")
    cid = int(cid) if (cid is not None and str(cid).isdigit()) else None
    with _get_db() as conn:
        if cid is not None and not conn.execute(
                "SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone():
            return jsonify({"success": False, "error": "Напрямок не знайдено"}), 404
        cur = conn.execute("UPDATE transcriptions SET category_id = ? WHERE id = ?", (cid, tid))
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"success": False, "error": "Транскрипт не знайдено"}), 404
    return jsonify({"success": True, "category_id": cid})


@memory_bp.route('/api/memory/transcriptions/<int:tid>/suggest-category', methods=['GET'])
def suggest_transcription_category(tid: int):
    """Авто-підказка напрямку через k-NN по розмічених транскриптах (Phase 15C).
    Повертає suggestion=None з reason, якщо підказати не можна (cold-start тощо)."""
    from app.services import categorize
    try:
        res = categorize.suggest_category(current_app.config['DATABASE'], tid)
    except Exception as e:
        logger.error("[memory] suggest-category tx=%s failed: %s", tid, e, exc_info=True)
        return jsonify({"success": False, "error": "Помилка підказки. Перевірте логи."}), 500
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/transcriptions/bulk-category', methods=['POST'])
def bulk_set_category():
    """Масово призначити напрямок. Body: {ids: [..], category_id} АБО
    {source_type: 'youtube'|'file'|..., category_id} (усі без напрямку цього типу)."""
    data = request.get_json(silent=True) or {}
    cid = data.get("category_id")
    cid = int(cid) if (cid is not None and str(cid).isdigit()) else None
    ids = data.get("ids")
    src = data.get("source_type")
    with _get_db() as conn:
        if ids:
            ph = ",".join("?" * len(ids))
            n = conn.execute(f"UPDATE transcriptions SET category_id = ? WHERE id IN ({ph})",
                             (cid, *ids)).rowcount
        elif src:
            n = conn.execute("UPDATE transcriptions SET category_id = ? "
                             "WHERE source_type = ? AND category_id IS NULL", (cid, src)).rowcount
        else:
            return jsonify({"success": False, "error": "Вкажіть ids або source_type"}), 400
        conn.commit()
    return jsonify({"success": True, "updated": n})


# ============================================================
# Збагачення
# ============================================================

@memory_bp.route('/api/memory/transcriptions/<int:tid>/enrich', methods=['POST'])
def enrich_one(tid: int):
    """Збагатити один транскрипт (синхронно). Body: {"force": bool, "model": str}."""
    # Як і в backfill нижче: embed-фаза не потребує Claude — див. коментар там.
    if not enrichment.any_available():
        return jsonify({"success": False,
                        "error": "Немає ні ANTHROPIC_API_KEY, ні локальних embeddings"}), 400
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    model = data.get("model")
    try:
        res = enrichment.enrich_transcription(
            current_app.config['DATABASE'], tid, model=model, force=force,
        )
    except Exception as e:
        logger.error("[memory] enrich tx=%s failed: %s", tid, e, exc_info=True)
        return jsonify({"success": False, "error": "Помилка збагачення. Перевірте логи."}), 500
    if res.get("status") == "not_found":
        return jsonify({"success": False, "error": "Транскрипт не знайдено"}), 404
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/backfill', methods=['POST'])
def start_backfill():
    """Запустити масове збагачення історії у фоні. Body: {force, limit, model}."""
    # T6.5: гейт по any_available(), а не is_available(). Embed-фаза локальна і
    # безкоштовна, а після бампу EMBED_VERSION саме вона стає основною роботою
    # (весь архів на re-embed). Гейт по одному лише Claude-ключу означав би:
    # ключа нема → індексер вічно показує повний архів як роботу, і запустити
    # її з UI неможливо. Інжест уже давно ходить через any_available()
    # (transcription.py/documents.py/copilot.py) — тут був розсинхрон.
    if not enrichment.any_available():
        return jsonify({"success": False,
                        "error": "Немає ні ANTHROPIC_API_KEY, ні локальних embeddings"}), 400
    with _backfill_lock:
        if _backfill_state.get("running"):
            return jsonify({"success": False, "error": "Backfill вже виконується"}), 409

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    limit = data.get("limit")
    model = data.get("model")
    effort = data.get("effort", "medium")
    if effort not in ("low", "medium", "high"):
        effort = "medium"
    db_path = current_app.config['DATABASE']

    pending = enrichment.list_unenriched_ids(db_path) if not force else None

    def _job(job):
        with _backfill_lock:
            _backfill_state.update({"running": True, "processed": 0, "total": 0,
                                    "done": 0, "skipped": 0, "failed": 0})

        def _progress(p):
            with _backfill_lock:
                _backfill_state.update(p)
                _backfill_state["running"] = True
            try:
                state.sse_broker.publish("backfill", "progress", p)
            except Exception:
                pass

        try:
            result = enrichment.backfill(
                db_path, model=model, limit=limit, force=force, effort=effort,
                progress_cb=_progress, cancel_cb=job.is_cancelled,
            )
        finally:
            with _backfill_lock:
                _backfill_state["running"] = False
        try:
            state.sse_broker.publish("backfill", "complete", {"status": "completed", **result})
        except Exception:
            pass
        return result

    job = state.job_queue.submit("enrichment_backfill", _job, meta={"kind": "backfill"})
    return jsonify({
        "success": True, "job_id": job.id, "channel": "backfill",
        "pending": len(pending) if pending is not None else None,
    })


@memory_bp.route('/api/memory/backfill/status', methods=['GET'])
def backfill_status():
    with _backfill_lock:
        return jsonify({"success": True, **_backfill_state})


@memory_bp.route('/api/memory/import', methods=['POST'])
def import_archive():
    """Імпорт корпусу meeting_archive. Body: {"path": "<dir>"}.

    Заливає .md-мітинги як транскрипти (idempotent). Збагачення (enrich+embed)
    запускати окремо через POST /api/memory/backfill — це контрольований
    (платний для Claude) крок.
    """
    data = request.get_json(silent=True) or {}
    root = (data.get("path") or "").strip()
    if not root:
        return jsonify({"success": False, "error": "Вкажіть path до директорії meeting_archive"}), 400
    if not os.path.isdir(root):
        return jsonify({"success": False, "error": f"Директорію не знайдено: {root}"}), 400
    from app.services import archive_import
    res = archive_import.import_directory(current_app.config['DATABASE'], root)
    return jsonify({"success": True, **res})


# ============================================================
# Пошук (гібрид: вектори + FTS5)
# ============================================================

@memory_bp.route('/api/memory/search', methods=['GET'])
def search():
    """Гібридний семантичний пошук по архіву. Query: q, k (top-k)."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"success": False, "error": "Порожній запит"}), 400
    try:
        k = min(max(int(request.args.get('k', 8)), 1), 30)
    except ValueError:
        k = 8
    from app.services import retrieval
    res = retrieval.search(current_app.config['DATABASE'], q, top_k=k,
                           category_id=_parse_category(request.args.get('category_id')))
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/ask', methods=['POST'])
def ask():
    """RAG «Запитай архів»: питання → відповідь з цитатами.

    Body: {question, k, model, category_id, project}. `project` (Трек 2) звужує
    пошук до записів, де згадано цей проєкт/людину — категорії недостатньо,
    бо «Робота» покриває більша частина корпусу."""
    if not enrichment.is_available():
        return jsonify({"success": False, "error": "ANTHROPIC_API_KEY не налаштовано"}), 400
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"success": False, "error": "Порожнє питання"}), 400
    from app.services import rag
    # Дефолт k живе в rag._DEFAULT_TOP_K (одна точка правди, env RAG_TOP_K).
    try:
        k = min(max(int(data.get("k", rag._DEFAULT_TOP_K)), 1), 20)
    except (ValueError, TypeError):
        k = rag._DEFAULT_TOP_K
    try:
        res = rag.answer_question(current_app.config['DATABASE'], q, top_k=k,
                                  model=data.get("model"),
                                  category_id=_parse_category(data.get("category_id")),
                                  project=(data.get("project") or None))
    except Exception as e:
        logger.error("[memory] ask failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": "Помилка RAG. Перевірте логи."}), 500
    return jsonify({"success": True, **res})


@memory_bp.route('/api/memory/ask/stream', methods=['POST'])
def ask_stream():
    """RAG зі стрімом відповіді (SSE). Body: {question, k, model, category_id, project}.

    Події: sources (одразу) → delta* (токени) → done | error.
    Усі значення з request читаємо ДО генератора (поза request-контекстом).
    """
    if not enrichment.is_available():
        return jsonify({"success": False, "error": "ANTHROPIC_API_KEY не налаштовано"}), 400
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"success": False, "error": "Порожнє питання"}), 400
    from app.services import rag
    try:
        k = min(max(int(data.get("k", rag._DEFAULT_TOP_K)), 1), 20)
    except (ValueError, TypeError):
        k = rag._DEFAULT_TOP_K
    model = data.get("model")
    category_id = _parse_category(data.get("category_id"))
    # Трек 2: зріз за проєктом/людиною. Читаємо ТУТ, а не в генераторі — той
    # виконується поза request-контекстом (та сама причина, що й для решти полів).
    project = (data.get("project") or None)
    db_path = current_app.config['DATABASE']

    def generate():
        try:
            yield from rag.answer_question_stream(db_path, q, top_k=k, model=model,
                                                  category_id=category_id, project=project)
        except Exception as e:
            logger.error("[memory] ask_stream generator failed: %s", e, exc_info=True)
            yield rag._sse("error", {"error": "Помилка RAG."})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ============================================================
# Сутності
# ============================================================

@memory_bp.route('/api/memory/entities', methods=['GET'])
def list_entities():
    """Список сутностей. Query: type, q (пошук по імені/alias), sort, limit, offset."""
    etype = request.args.get('type')
    q = (request.args.get('q') or '').strip()
    sort = request.args.get('sort', 'mentions')  # mentions | meetings | name | recent
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        limit, offset = 100, 0

    # Поріг значущості: per-meeting LLM-екстракція плодить разові сутності
    # (≈65-83% згадані лише в 1 мітингу — шум). За замовчуванням ховаємо їх
    # (min_meetings=2). Явний пошук по імені (q) показує і рідкісні (=1).
    default_min = 1 if q else 2
    try:
        min_meetings = max(int(request.args.get('min_meetings', default_min)), 1)
    except (ValueError, TypeError):
        min_meetings = default_min

    where, params = [], []
    if etype in enrichment._VALID_ENTITY_TYPES:
        where.append("e.type = ?")
        params.append(etype)
    if min_meetings > 1:
        where.append("e.meeting_count >= ?")
        params.append(min_meetings)
    if q:
        where.append(
            "(e.normalized_name LIKE ? OR e.id IN "
            "(SELECT entity_id FROM entity_aliases WHERE normalized_alias LIKE ?))"
        )
        like = f"%{enrichment._normalize(q)}%"
        params.extend([like, like])
    cat = _parse_category(request.args.get('category_id'))
    if cat == 'none':
        where.append(
            "e.id IN (SELECT me.entity_id FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.category_id IS NULL AND t.deleted_at IS NULL)")
    elif cat is not None:
        where.append(
            "e.id IN (SELECT me.entity_id FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.category_id = ? AND t.deleted_at IS NULL)")
        params.append(cat)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order = {
        "mentions": "e.mention_count DESC",
        "meetings": "e.meeting_count DESC",
        "name": "e.canonical_name COLLATE NOCASE ASC",
        "recent": "e.updated_at DESC",
    }.get(sort, "e.mention_count DESC")

    with _get_db() as conn:
        rows = conn.execute(
            f"SELECT e.id, e.type, e.canonical_name, e.role, e.mention_count, "
            f"e.meeting_count, e.speaker_id FROM entities e {where_sql} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM entities e {where_sql}", params
        ).fetchone()["n"]

    return jsonify({
        "success": True, "total": total, "limit": limit, "offset": offset,
        "min_meetings": min_meetings,
        "entities": [dict(r) for r in rows],
    })


@memory_bp.route('/api/memory/entities/<int:eid>', methods=['GET'])
def entity_detail(eid: int):
    """Деталі сутності: мітинги (таймлайн), задачі-власник, aliases."""
    with _get_db() as conn:
        ent = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        if not ent:
            return jsonify({"success": False, "error": "Сутність не знайдено"}), 404

        meetings = conn.execute(
            "SELECT t.id, t.source_name, t.source_type, t.created_at, "
            "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date, "
            "me.mention_count, me.role_in_meeting "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ? AND t.deleted_at IS NULL "
            "ORDER BY meeting_date DESC, t.id DESC",
            (eid,),
        ).fetchall()

        actions = conn.execute(
            "SELECT ai.id, ai.task, ai.due, ai.status, ai.transcription_id, "
            "t.source_name FROM action_items ai "
            "LEFT JOIN transcriptions t ON t.id = ai.transcription_id AND t.deleted_at IS NULL "
            "WHERE ai.owner_entity_id = ? ORDER BY ai.id DESC",
            (eid,),
        ).fetchall()

        aliases = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (eid,),
        ).fetchall()

    return jsonify({
        "success": True,
        "entity": dict(ent),
        "meetings": [dict(r) for r in meetings],
        "action_items": [dict(r) for r in actions],
        "aliases": [r["alias"] for r in aliases],
    })


# ============================================================
# Action items
# ============================================================

# Дашборд-вікна дедлайну. Взаємно виключні й вичерпні — кожна задача потрапляє
# РІВНО в одне відро, інакше бакети на /tasks брехали б сумою.
#
# NB: свідомо відрізняються від вікон MCP-зводів (commitments._WINDOWS):
#  1. Там `this_week` = понеділок…неділя і тому ПЕРЕТИНАЄТЬСЯ з `overdue`. Для
#     понеділкового зводу це нормально (два окремі списки), для дашборда — ні.
#  2. Календарний тиждень вироджується під кінець: у суботу «цей тиждень» — це
#     два дні, і відро стабільно порожнє. Тому тут `soon` — це СЛИЗЬКЕ вікно
#     «наступні 7 днів від сьогодні», яке однаково корисне будь-якого дня.
_TASK_WINDOWS = ('overdue', 'soon', 'later', 'no_date', 'all')
_SOON_DAYS = 7


def _soon_end_iso(today=None) -> str:
    """ISO-дата кінця вікна «найближчі 7 днів»."""
    return ((today or _date.today()) + _timedelta(days=_SOON_DAYS)).isoformat()


def _task_window_sql(window: str, today=None):
    """(sql_clauses, params) для дашборд-вікна. Порожньо для 'all'."""
    t = (today or _date.today()).isoformat()
    if window == 'no_date':
        return ["ai.due_date IS NULL"], []
    if window == 'overdue':
        return ["ai.due_date IS NOT NULL", "ai.due_date < ?"], [t]
    if window == 'soon':
        return ["ai.due_date >= ?", "ai.due_date <= ?"], [t, _soon_end_iso(today)]
    if window == 'later':
        return ["ai.due_date > ?"], [_soon_end_iso(today)]
    return [], []


@memory_bp.route('/api/memory/action-items', methods=['GET'])
def list_action_items():
    """Задачі. Query: status (open|done|cancelled|stale|all), owner_entity_id,
    owner (canonical або сира назва), category_id, period (24h|7d|month|year|all),
    window (overdue|soon|later|no_date|all), limit.

    Додатково повертає ``windows`` (лічильники по відрах у межах решти фільтрів)
    і ``owners`` (фасет власників) — щоб дашборд показував правдиві суми навіть
    коли сам список обрізаний лімітом."""
    status = request.args.get('status', 'open')
    owner = request.args.get('owner_entity_id')
    owner_name = (request.args.get('owner') or '').strip()
    try:
        limit = min(int(request.args.get('limit', 200)), 1000)
    except ValueError:
        limit = 200

    window = (request.args.get('window') or 'all').strip().lower()
    if window not in _TASK_WINDOWS:
        window = 'all'

    # Три НЕЗАЛЕЖНІ групи умов, кожна тримає свої clauses разом зі своїми
    # params. Це не педантизм: нижче потрібні три різні комбінації тих самих
    # фільтрів (список / лічильники відер / фасет власників), і зшивати їх з
    # одного плоского списку без прив'язки параметрів до умов неможливо.
    common, common_p = [], []
    if status and status != 'all':
        common.append("ai.status = ?")
        common_p.append(status)
    # T4.6: задачі мітингу, який видалено (soft-delete), не мають спливати на
    # дашборді — незалежно від category-фільтра нижче.
    common.append(
        "ai.transcription_id IN (SELECT id FROM transcriptions WHERE deleted_at IS NULL)")
    # Трек 1: повтори однієї домовленості з різних зустрічей позначені dup_of →
    # за замовчуванням показуємо лише канонічну (include_dups=1 щоб побачити всі).
    if request.args.get('include_dups') not in ('1', 'true', 'yes'):
        common.append("ai.dup_of IS NULL")
    cat = _parse_category(request.args.get('category_id'))
    if cat == 'none':
        common.append("ai.transcription_id IN (SELECT id FROM transcriptions WHERE category_id IS NULL)")
    elif cat is not None:
        common.append("ai.transcription_id IN (SELECT id FROM transcriptions WHERE category_id = ?)")
        common_p.append(cat)

    # Фільтр періоду — за датою запису-джерела (meeting_date, інакше created_at),
    # бо користувач мислить «задачі за останні N» у термінах коли був дзвінок,
    # а не коли відпрацював enrichment. Whitelisted модифікатори SQLite.
    period = (request.args.get('period') or 'all').strip().lower()
    _period_mod = {'24h': '-1 day', '7d': '-7 days', 'month': '-1 month', 'year': '-1 year'}
    if period in _period_mod:
        common.append("date(COALESCE(t.meeting_date, t.created_at)) >= date('now', ?)")
        common_p.append(_period_mod[period])

    own, own_p = [], []
    if owner:
        own.append("ai.owner_entity_id = ?")
        own_p.append(owner)
    if owner_name:
        # Точний збіг, а не підрядок: значення приходить з чипа фасета (він і
        # зібраний тим самим COALESCE), і підрядок зробив би чип «Андрій»
        # фільтром, що тягне ще й «Андрій Ткаченко».
        #
        # Дві гілки, бо одне й те саме імʼя живе у двох місцях:
        #   1) канонічне імʼя сутності (або сире, коли звʼязку немає) — це чип;
        #   2) аліаси графа — після злиття сутностей (`entity_dedup merge`) усі
        #      інші написання людини переїжджають САМЕ туди. У власника архіву
        #      це 40 варіантів, і без цієї гілки посилання виду
        #      `/tasks?owner=Мельник` давало порожньо при живих даних.
        #
        # Третьої гілки — по сирому `ai.owner_name` — тут свідомо НЕМАЄ, хоча в
        # MCP-фільтрі вона є. На дашборді число на чипі має дорівнювати довжині
        # списку, а сире імʼя цю рівність ламає: задача 2566 звучить як «Юлія»,
        # але звʼязана з сутністю «Юля», тобто живе під ІНШИМ чипом — і список
        # ставав на рядок довшим за чип, який його відкрив. Для вільного тексту
        # є MCP (`list_action_items`), там підрядок і доречний.
        # Порівняння аліасів — по `normalized_alias`, і ключ рахуємо ТІЄЮ Ж
        # функцією, що його й записала (`enrichment._normalize`): робити це в
        # SQL через LOWER() не можна — він не згортає кирилицю, тож
        # регістронезалежність вийшла б фікцією.
        own.append("(COALESCE(e.canonical_name, ai.owner_name) = ? "
                   "OR EXISTS(SELECT 1 FROM entity_aliases ea "
                   "WHERE ea.entity_id = e.id AND ea.normalized_alias = ?))")
        own_p += [owner_name, enrichment._normalize(owner_name)]

    win, win_p = _task_window_sql(window)

    def _sql(*groups):
        cl, pr = [], []
        for c, p in groups:
            cl += c
            pr += p
        return (("WHERE " + " AND ".join(cl)) if cl else ""), pr

    where_sql, params = _sql((common, common_p), (own, own_p), (win, win_p))
    # Лічильники відер — у межах решти фільтрів, але БЕЗ вікна: інакше активне
    # відро завжди показувало б себе як 100%, а решту як нулі.
    count_sql, count_p = _sql((common, common_p), (own, own_p))
    # Фасет власників — фільтр РІВНЯ СТОРІНКИ, тож рахуємо його без вікна і без
    # самого себе: чипи мають показувати «скільки всього задач у цієї людини»,
    # а не «скільки її задач потрапило у відро, яке зараз відкрите».
    facet_sql, facet_p = _sql((common, common_p))

    _JOINS = ("FROM action_items ai "
              "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
              "LEFT JOIN entities e ON e.id = ai.owner_entity_id ")

    # Протерміноване — СВІЖИМ УГОРУ (задача, прострочена вчора, ще жива; торішня
    # — археологія). Решта вікон — найближчий дедлайн першим, недатоване в хвіст.
    # Тай-брейк — дата розмови, потім id: після Волни 5.1 id більше не збігається
    # з хронологією (задачі з переписки записані останніми, а говорять про
    # травень), і на самому id вони витісняли дзвінки з ведра «без дати».
    order_sql = ("ORDER BY ai.due_date DESC, meeting_date DESC, ai.id DESC"
                 if window == 'overdue'
                 else "ORDER BY ai.due_date IS NULL, ai.due_date, meeting_date DESC, ai.id DESC")

    today = _date.today().isoformat()
    soon_end = _soon_end_iso()

    with _get_db() as conn:
        rows = conn.execute(
            f"SELECT ai.id, ai.task, ai.owner_name, ai.owner_entity_id, ai.due, "
            f"ai.due_date, ai.due_precision, ai.stale_at, ai.dup_of, "
            f"ai.status, ai.transcription_id, ai.source, t.source_name, t.category_id, "
            # Волна 5.1: у списку зʼявились задачі з переписки, а source_name у
            # TG — це превʼю самого повідомлення («[TG] Так, звичайно, плануємо…»),
            # тобто підпис, який повторює текст задачі. Картці потрібна адреса:
            # чат і автор репліки.
            f"t.source_type, t.tg_chat_title, t.tg_sender, "
            f"COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date, "
            f"e.canonical_name AS owner_canonical "
            f"{_JOINS}{where_sql} {order_sql} LIMIT ?",
            (*params, limit),
        ).fetchall()

        wc = conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN ai.due_date IS NULL THEN 1 ELSE 0 END) AS no_date, "
            f"SUM(CASE WHEN ai.due_date IS NOT NULL AND ai.due_date < ? THEN 1 ELSE 0 END) AS overdue, "
            f"SUM(CASE WHEN ai.due_date >= ? AND ai.due_date <= ? THEN 1 ELSE 0 END) AS soon, "
            f"SUM(CASE WHEN ai.due_date > ? THEN 1 ELSE 0 END) AS later, "
            f"COUNT(*) AS total "
            f"{_JOINS}{count_sql}",
            (today, today, soon_end, soon_end, *count_p),
        ).fetchone()

        _facet_and = " AND " if facet_sql else "WHERE "
        owners = [dict(r) for r in conn.execute(
            f"SELECT COALESCE(e.canonical_name, ai.owner_name) AS owner, "
            f"MIN(ai.owner_entity_id) AS owner_entity_id, COUNT(*) AS n "
            f"{_JOINS}{facet_sql}{_facet_and}"
            f"COALESCE(e.canonical_name, ai.owner_name) IS NOT NULL "
            f"GROUP BY owner ORDER BY n DESC, owner LIMIT 40",
            facet_p,
        ).fetchall()]

    windows = {k: (wc[k] or 0) for k in ('no_date', 'overdue', 'soon', 'later')}
    windows['all'] = wc['total'] or 0
    return jsonify({
        "success": True,
        "action_items": [dict(r) for r in rows],
        "windows": windows,
        "owners": owners,
        "today": today,
        "soon_end": soon_end,
        "soon_days": _SOON_DAYS,
    })


@memory_bp.route('/api/memory/action-items/<int:aid>', methods=['PATCH'])
def update_action_item(aid: int):
    """Оновити статус задачі. Body: {"status": "open|done|cancelled"}."""
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('open', 'done', 'cancelled'):
        return jsonify({"success": False, "error": "Невірний статус"}), 400
    with _get_db() as conn:
        cur = conn.execute(
            "UPDATE action_items SET status = ? WHERE id = ?", (status, aid),
        )
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"success": False, "error": "Задачу не знайдено"}), 404
    return jsonify({"success": True})


# ============================================================
# Статистика
# ============================================================

@memory_bp.route('/api/memory/stats', methods=['GET'])
def stats():
    from app.services import embeddings
    with _get_db() as conn:
        by_type = {
            r["type"]: r["n"] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM entities GROUP BY type"
            ).fetchall()
        }
        # Значущі = згадані у ≥2 мітингах (хребет графа; решта — разовий шум).
        by_type_sig = {
            r["type"]: r["n"] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM entities WHERE meeting_count >= 2 GROUP BY type"
            ).fetchall()
        }
        actions = {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM action_items GROUP BY status"
            ).fetchall()
        }
        # T6.5: «embedded» рахуємо тим самим предикатом, що й список роботи
        # індексера (enrichment.list_unenriched_ids) — модель І версія. Інакше
        # після бампу EMBED_VERSION на одному екрані стояли б два числа, які
        # суперечать одне одному: «векторизовано 4502/4503» і «до обробки 4503».
        tx = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END) AS enriched, "
            "SUM(CASE WHEN embedded_at IS NOT NULL AND embedding_model = ? "
            "          AND embedding_version = ? THEN 1 ELSE 0 END) AS embedded "
            "FROM transcriptions WHERE deleted_at IS NULL",
            (embeddings.EMBED_MODEL, embeddings.EMBED_VERSION),
        ).fetchone()
        chunks_total = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    return jsonify({
        "success": True,
        "entities": by_type,
        "entities_significant": by_type_sig,
        "action_items": actions,
        "transcriptions": {
            "total": tx["total"] or 0,
            "enriched": tx["enriched"] or 0,
            "embedded": tx["embedded"] or 0,
        },
        "chunks": chunks_total,
        "enrichment_available": enrichment.is_available(),
        "embeddings_available": embeddings.is_available(),
        "embedding_model": embeddings.EMBED_MODEL,
    })
