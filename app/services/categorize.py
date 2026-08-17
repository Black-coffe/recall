"""Авто-підказка напрямку (Phase 15C) — семантичний k-NN по розмічених транскриптах.

Для транскрипту: усереднюємо його chunk-embeddings у doc-вектор, шукаємо
найсхожіші ВЖЕ розмічені (category_id IS NOT NULL) транскрипти і зважено
голосуємо їхніми напрямками (вага голосу = cosine similarity). Без правил/
ключових слів — самонавчається з ростом розмітки, повторно використовує ті самі
embeddings, що й RAG-пошук.

Холодний старт: поки розмічено <2 напрямків (з embeddings), будь-який класифікатор
звівся б до одного класу і завжди пропонував би його. Тому повертаємо
reason='insufficient_labels' — UI не нав'язує підказку, доки користувач не
засіє кілька різних напрямків вручну.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

import numpy as np

from app.db.connection import get_db_connection
from app.services import embeddings


logger = logging.getLogger(__name__)

_KNN = 8                      # скільки найближчих сусідів голосують
_MIN_LABELED_CATEGORIES = 2  # менше = cold-start, підказка не має сенсу


def _doc_vector(blobs: list[bytes]) -> Optional[np.ndarray]:
    """Усереднити chunk-embeddings у єдиний нормалізований doc-вектор."""
    vecs = [embeddings.blob_to_vec(b) for b in blobs]
    vecs = [v for v in vecs if v.shape[0] == embeddings.EMBED_DIM]
    if not vecs:
        return None
    m = np.mean(np.vstack(vecs), axis=0)
    n = np.linalg.norm(m)
    return (m / n).astype(np.float32) if n else None


def suggest_category(db_path: str, transcription_id: int, k: int = _KNN) -> dict:
    """Підказати напрямок транскрипту через k-NN по розмічених.

    Returns:
      {"suggestion": {"category_id", "name", "confidence"},
       "candidates": [{"category_id","name","score"}...], "neighbors": int}
      або {"suggestion": None, "reason": <code>, ...} якщо підказати не можна.
    """
    if not embeddings.is_available():
        return {"suggestion": None, "reason": "embeddings_unavailable"}

    with get_db_connection(db_path) as conn:
        my_chunks = conn.execute(
            "SELECT embedding FROM chunks WHERE transcription_id = ? "
            "AND embedding IS NOT NULL", (transcription_id,),
        ).fetchall()
        if not my_chunks:
            return {"suggestion": None, "reason": "not_embedded"}

        # T4.6: не голосуємо embedding'ами soft-deleted транскриптів (undo-вікно
        # ≠ дійсна розмітка).
        labeled = conn.execute(
            "SELECT ch.transcription_id AS tid, t.category_id AS cat, ch.embedding AS emb "
            "FROM chunks ch JOIN transcriptions t ON t.id = ch.transcription_id "
            "WHERE t.category_id IS NOT NULL AND t.deleted_at IS NULL "
            "AND ch.transcription_id != ? AND ch.embedding IS NOT NULL", (transcription_id,),
        ).fetchall()
        cat_names = {r["id"]: r["name"]
                     for r in conn.execute("SELECT id, name FROM categories").fetchall()}

    q = _doc_vector([r["embedding"] for r in my_chunks])
    if q is None:
        return {"suggestion": None, "reason": "dim_mismatch"}

    # Групуємо чанки розмічених транскриптів → по одному doc-вектору на транскрипт.
    blobs_by_tid: dict[int, list] = defaultdict(list)
    cat_by_tid: dict[int, int] = {}
    for r in labeled:
        blobs_by_tid[r["tid"]].append(r["emb"])
        cat_by_tid[r["tid"]] = r["cat"]

    docs: list[tuple[np.ndarray, int]] = []
    for tid, blobs in blobs_by_tid.items():
        dv = _doc_vector(blobs)
        if dv is not None:
            docs.append((dv, cat_by_tid[tid]))

    if not docs:
        return {"suggestion": None, "reason": "no_labeled_data"}

    distinct = {cat for _, cat in docs}
    if len(distinct) < _MIN_LABELED_CATEGORIES:
        return {"suggestion": None, "reason": "insufficient_labels",
                "labeled_categories": len(distinct)}

    # k найближчих сусідів за cosine, зважене голосування.
    sims = sorted(((float(dv @ q), cat) for dv, cat in docs),
                  key=lambda x: x[0], reverse=True)[:k]
    votes: dict[int, float] = defaultdict(float)
    for sim, cat in sims:
        votes[cat] += max(sim, 0.0)
    total = sum(votes.values()) or 1.0

    ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
    best_cat, best_score = ranked[0]
    return {
        "suggestion": {
            "category_id": best_cat,
            "name": cat_names.get(best_cat, ""),
            "confidence": round(best_score / total, 3),
        },
        "candidates": [
            {"category_id": c, "name": cat_names.get(c, ""), "score": round(v / total, 3)}
            for c, v in ranked
        ],
        "neighbors": len(sims),
    }
