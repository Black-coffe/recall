"""Дослідження бренду / великий експорт (Phase 18).

- GET  /api/research/preview    — швидка статистика згадок (0 токенів, без AI)
- POST /api/research/originals   — markdown усіх оригіналів-фрагментів (без AI)
- POST /api/research/summary     — SSE: структурований звіт (дешева модель)

Логіка у app/services/research.py (тестовно, без app-контексту).
"""
from __future__ import annotations

import logging
import re

from flask import Blueprint, Response, current_app, jsonify, request

from app.services import research


logger = logging.getLogger(__name__)
research_bp = Blueprint('research', __name__)


def _db() -> str:
    return current_app.config['DATABASE']


def _filename(terms: list[str], suffix: str, display=None) -> str:
    base = display or "-".join(terms[:3]) or "research"
    base = re.sub(r"[^\w\-]+", "-", base, flags=re.UNICODE).strip("-").lower() or "research"
    return f"recall-{base}-{suffix}.md"


def _resolve_query(data) -> "tuple[str, str | None] | tuple[None, None]":
    """З тіла/параметрів дістати (q, display). Якщо є entity_id — резолвимо
    сутність у q=ім'я+псевдоніми та display=канонічне ім'я. Інакше — вільний q.
    Returns (None, None) якщо нічого валідного нема."""
    eid = data.get('entity_id')
    if eid is not None and str(eid).strip().isdigit():
        res = research.entity_terms(_db(), int(eid))
        if not res:
            return None, None
        name, terms = res
        return ",".join(terms), name
    q = (data.get('q') or '').strip()
    return (q or None), None


@research_bp.route('/api/research/preview', methods=['GET'])
def preview():
    """Скільки згадок і де (без витягу повного markdown, без AI).
    Приймає q АБО entity_id (тоді шукає по імені+псевдонімах сутності)."""
    q, display = _resolve_query(request.args)
    if not q:
        return jsonify({"success": False, "error": "Порожній запит"}), 400
    category_id = research._parse_category(request.args.get('category_id'))
    collected = research.collect(_db(), q, category_id, display=display)
    # Легкий прев'ю-список (без важких фрагментів) — для UI.
    sample = [{
        "id": r["id"], "source_type": r["source_type"], "title": r["title"],
        "date": research._fmt_date_human(r["date_display"] or r["date_sort"]),
        "who": r["who"], "mentions": r["mentions"], "title_only": r["title_only"],
    } for r in collected["records"][:50]]
    return jsonify({"success": True, "terms": collected["terms"], "display": display,
                    "stats": collected["stats"], "sample": sample,
                    "truncated": len(collected["records"]) > 50})


@research_bp.route('/api/research/originals', methods=['POST'])
def originals():
    """Повний markdown оригіналів-фрагментів. 0 токенів, без AI."""
    data = request.get_json(silent=True) or {}
    q, display = _resolve_query(data)
    if not q:
        return jsonify({"success": False, "error": "Порожній запит"}), 400
    category_id = research._parse_category(data.get('category_id'))
    base_url = request.host_url  # напр. http://localhost:5050/
    collected = research.collect(_db(), q, category_id, display=display)
    markdown = research.render_originals_md(collected, base_url)
    return jsonify({
        "success": True,
        "markdown": markdown,
        "filename": _filename(collected["terms"], "originals", display),
        "stats": collected["stats"],
    })


@research_bp.route('/api/research/summary', methods=['POST'])
def summary():
    """SSE: структурований звіт по бренду (map-reduce, дешева модель)."""
    data = request.get_json(silent=True) or {}
    q, display = _resolve_query(data)
    if not q:
        return jsonify({"success": False, "error": "Порожній запит"}), 400
    category_id = research._parse_category(data.get('category_id'))
    model = data.get('model')
    db_path = _db()

    def generate():
        try:
            yield from research.summarize_stream(db_path, q, category_id, model, display=display)
        except Exception as e:
            logger.error("[research] summary generator failed: %s", e, exc_info=True)
            yield research._sse("error", {"error": "Помилка саммарі."})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
