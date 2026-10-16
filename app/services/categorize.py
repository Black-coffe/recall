"""Direction auto-suggestion (Phase 15C) - semantic k-NN over labelled transcripts.

For a transcript: average its chunk embeddings into a doc vector, look for the
most similar ALREADY labelled (category_id IS NOT NULL) transcripts and cast a
weighted vote with their directions (vote weight = cosine similarity). No rules/
keywords - it learns by itself as the labelling grows, and reuses the same
embeddings as the RAG search.

Cold start: while fewer than 2 directions are labelled (with embeddings), any classifier
would collapse to a single class and always suggest it. So we return
reason='insufficient_labels' - the UI does not push a suggestion until the user
has seeded a few different directions by hand.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

import numpy as np

from app.db.connection import get_db_connection
from app.services import embeddings


logger = logging.getLogger(__name__)

_KNN = 8                      # how many nearest neighbours vote
_MIN_LABELED_CATEGORIES = 2  # fewer = cold start, a suggestion makes no sense


def _doc_vector(blobs: list[bytes]) -> Optional[np.ndarray]:
    """Average chunk embeddings into a single normalised doc vector."""
    vecs = [embeddings.blob_to_vec(b) for b in blobs]
    vecs = [v for v in vecs if v.shape[0] == embeddings.EMBED_DIM]
    if not vecs:
        return None
    m = np.mean(np.vstack(vecs), axis=0)
    n = np.linalg.norm(m)
    return (m / n).astype(np.float32) if n else None


def suggest_category(db_path: str, transcription_id: int, k: int = _KNN) -> dict:
    """Suggest a direction for a transcript via k-NN over the labelled ones.

    Returns:
      {"suggestion": {"category_id", "name", "confidence"},
       "candidates": [{"category_id","name","score"}...], "neighbors": int}
      or {"suggestion": None, "reason": <code>, ...} if nothing can be suggested.

    **Epoch guard (ADR-017, story 05).** `chunks` has no `embedding_model`/
    `embedding_version` of its own - those fields live on the parent
    `transcriptions` (one pair per whole record, atomic with DELETE+INSERT of the chunks
    in `chunk_and_embed_transcription`). Both selections - the record's own chunks and
    the labelled corpus - are therefore filtered by the current pair
    (`embeddings.EMBED_MODEL`, `embeddings.EMBED_VERSION`) via `JOIN
    transcriptions`: otherwise during a move to a new epoch (`EMBED_VERSION`
    2->3) the function would silently compute a cosine between vectors of different epochs -
    a geometrically meaningless number with no error at all. If a record has its own
    chunks but not in the current epoch, `reason: "epoch_mixed"` is returned instead of quiet
    garbage.
    """
    if not embeddings.is_available():
        return {"suggestion": None, "reason": "embeddings_unavailable"}

    epoch = (embeddings.EMBED_MODEL, embeddings.EMBED_VERSION)

    with get_db_connection(db_path) as conn:
        my_chunks = conn.execute(
            "SELECT ch.embedding FROM chunks ch JOIN transcriptions t "
            "ON t.id = ch.transcription_id WHERE ch.transcription_id = ? "
            "AND ch.embedding IS NOT NULL "
            "AND t.embedding_model = ? AND t.embedding_version = ?",
            (transcription_id, *epoch),
        ).fetchall()
        if not my_chunks:
            any_chunks = conn.execute(
                "SELECT 1 FROM chunks WHERE transcription_id = ? "
                "AND embedding IS NOT NULL LIMIT 1", (transcription_id,),
            ).fetchone()
            if any_chunks:
                return {"suggestion": None, "reason": "epoch_mixed"}
            return {"suggestion": None, "reason": "not_embedded"}

        # T4.6: do not vote with embeddings of soft-deleted transcripts (the undo window
        # != a valid label).
        labeled = conn.execute(
            "SELECT ch.transcription_id AS tid, t.category_id AS cat, ch.embedding AS emb "
            "FROM chunks ch JOIN transcriptions t ON t.id = ch.transcription_id "
            "WHERE t.category_id IS NOT NULL AND t.deleted_at IS NULL "
            "AND ch.transcription_id != ? AND ch.embedding IS NOT NULL "
            "AND t.embedding_model = ? AND t.embedding_version = ?",
            (transcription_id, *epoch),
        ).fetchall()
        cat_names = {r["id"]: r["name"]
                     for r in conn.execute("SELECT id, name FROM categories").fetchall()}

    q = _doc_vector([r["embedding"] for r in my_chunks])
    if q is None:
        return {"suggestion": None, "reason": "dim_mismatch"}

    # Group the chunks of labelled transcripts -> one doc vector per transcript.
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

    # k nearest neighbours by cosine, weighted vote.
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
