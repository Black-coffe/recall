"""Hybrid search over the archive (Phase 13B + 15A).

Combines two signals at the CHUNK level:
  1. Vector (semantics) -- cosine between the query embedding and chunk embeddings.
  2. Lexical (keywords) -- chunks_fts BM25.
Fusion is Reciprocal Rank Fusion (RRF): stable, needs no scale calibration.

After fusion (Phase 15A) two post-processing steps are applied so that the result
is USEFUL, not merely relevant:
  - Recency: a soft boost for recent meetings so that "what did we decide
    last time" does not drown among equally relevant old fragments. The weight
    is moderate -- it is a tie-breaker, not a dominant (it does not break a "timeless" question).
  - Diversification (per-meeting cap): we limit the number of chunks from ONE meeting
    in the top so that one wordy call does not eat all the context slots and
    the answer rests on several different sources.

Returns ranked chunks with provenance (transcript, title, date, speaker, timecode)
-- the basis for both /api/memory/search and the "Ask the archive" RAG chat.

Vector search is brute-force numpy (loads all embeddings into memory). For the
current scale (thousands of chunks) that is <100ms. If needed later -- sqlite-vec.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

import numpy as np

from app.core import settings as _settings
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.services import categories, embeddings, record_meta, reranker


logger = logging.getLogger(__name__)

_RRF_K = 60

# --- Recency ---
# final = rrf_score * (1 + RECENCY_WEIGHT * recency01), where recency01 ∈ [0,1]
# decays with a half-life of RECENCY_HALF_LIFE_DAYS. The weight is deliberately
# moderate: semantics (RRF) stays the main signal, recency only separates equals.
_RECENCY_WEIGHT = float(os.environ.get("RAG_RECENCY_WEIGHT", "0.25"))
_RECENCY_HALF_LIFE_DAYS = float(os.environ.get("RAG_RECENCY_HALF_LIFE_DAYS", "180"))

# --- Diversification ---
# The maximum number of chunks from one meeting that reach the final top.
_MAX_PER_MEETING = int(os.environ.get("RAG_MAX_PER_MEETING", "3"))

# --- T6.8: soft FTS relevance cutoff ---
# _fts_or_query ORs EVERY query word -- needed for recall (natural
# questions), but for long queries a chunk with ONE accidental shared word
# (e.g. "how") still gets into RRF as a lexical hit. For queries of at least
# _FTS_CUTOFF_MIN_WORDS words we require at least _FTS_MIN_MATCHES real
# term matches -- counted CHEAPLY (only inside the already narrowed candidate_k
# set, not over the whole table). Short queries (1-2 words) are NOT filtered
# -- there the single match is the signal.
_FTS_CUTOFF_MIN_WORDS = int(os.environ.get("RAG_FTS_CUTOFF_MIN_WORDS", "3"))
_FTS_MIN_MATCHES = int(os.environ.get("RAG_FTS_MIN_MATCHES", "2"))

# --- T6.4: local cross-encoder rerank (optional, OFF by default) ---
# How many top candidates (by RRF+recency) to run through reranker.rerank().
# 20-30 is a balance: enough to fix RRF mistakes in the relevant tail,
# but not the whole candidate_k (~40+) -- that would not make top_k anyway, and
# the cross-encoder forward pass is more expensive than cosine. Candidates OUTSIDE the pool
# stay in the original RRF+recency order (rerank only improves, never harms).
_RERANK_POOL_SIZE = int(os.environ.get("RAG_RERANK_POOL_SIZE", "24"))

# --- Comment layer (Wave 2) ---
# A comment is not another fragment of conversation but a sentence the owner wrote ABOUT
# a record: a clarification, a correction, an emphasis. So at equal relevance it should
# beat the raw transcript, not share a place with it.
#
# Multiplier: final *= (1 + _COMMENT_WEIGHT * kind_weight), where kind_weight ∈ [0,1]
# comes from `comments.KIND_WEIGHTS` (correction 1.0 ... question 0.4). So a
# correction gets up to +60% to the score, and an open question up to +24%. The weight
# is deliberately moderate, for the same reason as with recency: it must be a STRONG
# tie-breaker, not a silencer after which any question returns
# nothing but comments.
#
# Comments are searched separately (`comment_chunks`/`comment_chunks_fts`) because
# they live in a separate index -- why exactly so is written up in migration v37.
_COMMENT_WEIGHT = float(os.environ.get("RAG_COMMENT_WEIGHT", "0.6"))

#: How many candidates to take from the comment index. There are orders of magnitude fewer than
#: transcript chunks, so the full candidate_k is wasted work here; but taking too
#: few is not allowed either, otherwise the boost has nothing to apply to.
_COMMENT_CANDIDATE_CAP = int(os.environ.get("RAG_COMMENT_CANDIDATE_CAP", "20"))

#: What SHARE of the top comments may take at most.
#:
#: The ceiling appeared not out of caution but from measurements (Wave 2.5,
#: `evals/comments_eval.py`). Each comment is its own diversification group, so that the
#: cap "no more than N from one meeting" would not choke it. The consequence turned out to be this:
#: on 30 planted comments the output gave them 115 slots out of 120 at k=8, and
#: on eight questions out of fifteen the ENTIRE top-12 consisted of comments only.
#:
#: That is the real breakage, not a "strong boost": the model received clarifications
#: without the material they clarify. A correction "actually the sum is 12k" without
#: the fragment that names 20k has nothing to correct -- and the answer comes out
#: worse than it would be without the layer at all.
#:
#: A third is a compromise: priority stays (comments win their slots and
#: come first), but two thirds of the top are guaranteed to be held by the archive. At least
#: one slot always: a single correction must get through even at k=2.
_COMMENT_MAX_SHARE = float(os.environ.get("RAG_COMMENT_MAX_SHARE", "0.34"))


def _cap_comments(ranked_ids: list[int], top_k: int,
                  share: float) -> list[int]:
    """Limit the share of comments in the top, preserving their order.

    Excess comments are not thrown out but shifted TO THE END: if there is simply no archive
    material for the query, it is better to return comments than emptiness.
    """
    if share >= 1.0:
        return ranked_ids
    allowed = max(1, int(top_k * share))
    kept, overflow, seen = [], [], 0
    for cid in ranked_ids:
        if _is_comment_key(cid):
            seen += 1
            (kept if seen <= allowed else overflow).append(cid)
        else:
            kept.append(cid)
    return kept + overflow


def _recency_factor(meeting_date: Optional[str], now: Optional[datetime] = None) -> float:
    """recency01 ∈ [0,1]: 1.0 = today, ~0.5 after one half-life, →0 for old ones.
    Unknown/invalid date → 0.0 (neutral, no boost)."""
    if not meeting_date:
        return 0.0
    s = str(meeting_date)[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            d = datetime.strptime(s, fmt)
            break
        except ValueError:
            d = None
    if d is None:
        return 0.0
    now = now or datetime.now()
    age_days = (now - d).days
    if age_days <= 0:
        return 1.0
    if _RECENCY_HALF_LIFE_DAYS <= 0:
        return 0.0
    return float(0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS))


def _fts_words(query: str) -> list[str]:
    """Query tokens that go into the FTS OR query (>1 character)."""
    words = re.findall(r"\w+", query, re.UNICODE)
    return [w for w in words if len(w) > 1]


def _fts_or_query(query: str) -> str:
    """OR query for FTS5: every word quoted, joined with OR. The hybrid needs
    RECALL semantics (vector gives precision), so OR is better than the AND-of-all-words
    of the standard sanitize_fts_query (too strict for natural questions)."""
    words = _fts_words(query)
    if not words:
        return ""
    return " OR ".join(f'"{w}"' for w in words)


def _term_match_counts(conn, words: list[str], rowids: list[int]) -> dict[int, int]:
    """For every rowid in the candidate set -- how many of the OR terms ACTUALLY
    matched (and not merely "made the bm25 top-N through one rare
    term"). Cheap: the candidate set is already small (<= candidate_k), the query is
    one term at a time, bounded by the same rowid set (FTS5 index)."""
    counts: dict[int, int] = {rid: 0 for rid in rowids}
    if not rowids:
        return counts
    placeholders = ",".join("?" * len(rowids))
    for w in words:
        rows = conn.execute(
            f"SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            f"AND rowid IN ({placeholders})",
            [f'"{w}"'] + rowids,
        ).fetchall()
        for r in rows:
            counts[r["rowid"]] = counts.get(r["rowid"], 0) + 1
    return counts


def _record_axes_sql(category_id=None, tags: Optional[list[str]] = None,
                     since: Optional[str] = None,
                     until: Optional[str] = None) -> tuple[str, list]:
    """Filter axes that live at the RECORD level, as a single WHERE tail.

    The fragment is designed for a subquery over `transcriptions` WITHOUT an alias
    (`SELECT id FROM transcriptions WHERE ...`), so columns have no prefix.
    The semantics of each axis is held by `app.services.categories` (contract C2) --
    retrieval no longer writes `category_id = ?` itself: a direction must
    expand into its projects the same way everywhere.

    All three axes are applied at the SQL level BEFORE RRF/rerank (both branches and
    the comment branch): a post-filter after ranking would return fewer than k
    results and compete for rerank slots.
    """
    sql, params = "", []
    for frag, frag_params in (
        categories.filter_sql("category_id", category_id),
        categories.tags_sql("id", categories.parse_tags(tags)),
        categories.date_sql("COALESCE(meeting_date, created_at)", since, until),
    ):
        sql += frag
        params += frag_params
    return sql, params


def _scope_sql(scope_tids: Optional[list[int]]) -> tuple[str, list]:
    """WHERE fragment that narrows the search to a given list of records.

    Second scope layer (Track 2): category 1:1 does not help, because «Фонд Робота» is
    ~more than half of the corpus, and different projects live inside it. The list itself is computed by
    `app.services.scope.scope_filter_ids` (entity graph ∪ text mention) --
    retrieval deliberately knows nothing about the graph, it only filters by id.
    """
    if not scope_tids:
        return "", []
    ph = ",".join("?" * len(scope_tids))
    return f" AND ch.transcription_id IN ({ph})", list(scope_tids)


def _vector_search(db_path: str, query: str, candidate_k: int,
                   category_id: Optional[int] = None,
                   scope_tids: Optional[list[int]] = None,
                   tags: Optional[list[str]] = None,
                   since: Optional[str] = None,
                   until: Optional[str] = None) -> list[tuple[int, float]]:
    """Top-N chunks by cosine. Returns [(chunk_id, score)] descending."""
    if not embeddings.is_available():
        return []
    qvec = embeddings.embed_query(query)
    if not np.any(qvec):
        return []
    # T4.6: soft-deleted transcripts must NOT surface in RAG/search -- the filter is
    # always active, regardless of category_id (not only in category branches).
    # production-rag-wave-b-04: alongside -- the embedding epoch (embedding_model +
    # embedding_version). Vector hits are computed ONLY over chunks of the current pair --
    # otherwise a change of EMBED_MODEL/EMBED_VERSION would silently mix cosines of different
    # models/cuts (a BLOB of the same size does not mean the same model).
    axes_sql, axes_params = _record_axes_sql(category_id, tags, since, until)
    sql = ("SELECT ch.id, ch.embedding FROM chunks ch WHERE ch.embedding IS NOT NULL "
           "AND ch.transcription_id IN (SELECT id FROM transcriptions "
           "WHERE deleted_at IS NULL AND duplicate_of IS NULL "
           "AND embedding_model = ? AND embedding_version = ?" + axes_sql + ")")
    params: list = [embeddings.EMBED_MODEL, embeddings.EMBED_VERSION] + axes_params
    scope_sql, scope_params = _scope_sql(scope_tids)
    sql += scope_sql
    params += scope_params
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    ids = np.empty(len(rows), dtype=np.int64)
    mat = np.empty((len(rows), embeddings.EMBED_DIM), dtype=np.float32)
    n = 0
    for r in rows:
        vec = embeddings.blob_to_vec(r["embedding"])
        if vec.shape[0] != embeddings.EMBED_DIM:
            # The filter on (embedding_model, embedding_version) above should already have
            # narrowed the rows to the current pair -- a dimension mismatch here means a
            # corrupted BLOB or a version/dim desync, not the ordinary path of
            # ageing. It used to be skipped silently -- no trace in the log,
            # if the archive suddenly accumulated incompatible vectors.
            logger.error(
                "[retrieval] chunk id=%s: vector size %d != EMBED_DIM=%d "
                "(model/version %s) -- skipping", r["id"], vec.shape[0],
                embeddings.EMBED_DIM, embeddings.EMBED_MODEL)
            continue
        ids[n] = r["id"]
        mat[n] = vec
        n += 1
    if n == 0:
        return []
    sims = mat[:n] @ qvec  # both normalized → dot = cosine
    top = np.argsort(-sims)[:candidate_k]
    return [(int(ids[i]), float(sims[i])) for i in top]


def _fts_search(db_path: str, query: str, candidate_k: int,
                category_id: Optional[int] = None,
                scope_tids: Optional[list[int]] = None,
                tags: Optional[list[str]] = None,
                since: Optional[str] = None,
                until: Optional[str] = None) -> list[tuple[int, float]]:
    """Top-N chunks by BM25. Returns [(chunk_id, -rank)] best first."""
    words = _fts_words(query)
    fts_q = " OR ".join(f'"{w}"' for w in words) if words else ""
    if not fts_q:
        return []
    # T4.6: as in _vector_search -- soft-deleted are always excluded, not only
    # when category_id is given.
    axes_sql, axes_params = _record_axes_sql(category_id, tags, since, until)
    # `+rowid`, not `rowid`: a bare rowid SQLite hands to FTS5 as a constraint of the
    # virtual table (plan `INDEX 32:=M2`), and FTS5 runs MATCH separately for
    # each id in the list -- on a snapshot of the live DB 14 s per query vs 0.03 s
    # (fts-filter-pushdown). Also keeps the filter outside FTS5, the rows are the same.
    sql = ("SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? "
           "AND +rowid IN (SELECT id FROM chunks WHERE transcription_id IN "
           "(SELECT id FROM transcriptions WHERE deleted_at IS NULL "
           "AND duplicate_of IS NULL" + axes_sql + "))")
    params: list = [fts_q] + axes_params
    if scope_tids:
        ph_s = ",".join("?" * len(scope_tids))
        sql += (" AND +rowid IN (SELECT id FROM chunks WHERE transcription_id IN "
                f"({ph_s}))")
        params += list(scope_tids)
    sql += " ORDER BY rank LIMIT ?"
    params.append(candidate_k)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        # rank: lower = better (bm25). We return -rank as score (higher = better).
        hits = [(int(r["rowid"]), -float(r["rank"])) for r in rows]

        # T6.8: soft relevance cutoff for long queries (>= _FTS_CUTOFF_MIN_WORDS
        # words) -- we require >= _FTS_MIN_MATCHES real term matches, otherwise a chunk
        # with ONE accidental shared word clutters RRF. Short queries (1-2
        # words) are NOT filtered -- recall there is deliberately wide (the OR itself is the signal).
        # If the filter eats ALL hits (e.g. rare terms are spread across different
        # chunks) -- we fall back to the unfiltered list: do NOT zero out recall.
        if len(words) >= _FTS_CUTOFF_MIN_WORDS and hits:
            rowids = [cid for cid, _ in hits]
            counts = _term_match_counts(conn, words, rowids)
            filtered = [(cid, score) for cid, score in hits
                       if counts.get(cid, 0) >= _FTS_MIN_MATCHES]
            if filtered:
                hits = filtered
    return hits


# ============================================================
# Comments (Wave 2)
# ============================================================

# The identifier of a comment chunk in the shared output is the NEGATIVE id of a
# `comment_chunks` row. The id spaces of the two tables overlap (both have a row #5),
# and consumers (copilot: `valid_ids`, `chunk_meta`, `shown_chunks`) treat
# `chunk_id` as an opaque identity within one list -- so a pair of
# equal ids would make the insight proof ambiguous. The sign separates the spaces without
# any change downstream: no consumer goes to the DB by `chunk_id`.
def _cc_key(comment_chunk_id: int) -> int:
    return -int(comment_chunk_id)


def _is_comment_key(key: int) -> bool:
    return key < 0


def _comment_scope_sql(category_id, scope_tids: Optional[list[int]],
                       tags: Optional[list[str]] = None,
                       since: Optional[str] = None,
                       until: Optional[str] = None) -> tuple[str, list]:
    """Narrow the comment index to the same scope as the transcripts.

    A comment is attached to a CARD, while the scope is given as a list of transcripts. So
    under an active scope (direction or project) we keep only comments whose target
    is a transcript from this scope. Comments on other card types (a Media Library
    file, a task, an entity) are NOT shown under a scope: there is nothing to prove they belong
    to the slice, and quietly slipping them past the filter is worse than not showing them.
    Without a scope all are taken. Tags and period act here the same way as on
    transcripts: they describe the comment's TARGET record (the comment itself has no tags
    or meeting date), so a comment on a foreign record is cut off by the same
    axes as the record itself.
    """
    axes_sql, axes_params = _record_axes_sql(category_id, tags, since, until)
    if not axes_sql and not scope_tids:
        return "", []
    sub = "SELECT id FROM transcriptions WHERE deleted_at IS NULL" + axes_sql
    params: list = list(axes_params)
    if scope_tids:
        sub += f" AND id IN ({','.join('?' * len(scope_tids))})"
        params += list(scope_tids)
    return (f" AND c.target_type = 'transcription' AND c.target_id IN ({sub})", params)


def _comment_base_where(category_id, scope_tids, tags=None,
                        since=None, until=None) -> tuple[str, list]:
    """WHERE tail shared by the vector and lexical search of comments."""
    sql = " AND c.deleted_at IS NULL"
    scope_sql, params = _comment_scope_sql(category_id, scope_tids, tags, since, until)
    return sql + scope_sql, params


def _comment_vector_search(db_path: str, query: str, candidate_k: int,
                           category_id=None,
                           scope_tids: Optional[list[int]] = None,
                           tags: Optional[list[str]] = None,
                           since: Optional[str] = None,
                           until: Optional[str] = None
                           ) -> list[tuple[int, float]]:
    if not embeddings.is_available():
        return []
    qvec = embeddings.embed_query(query)
    if not np.any(qvec):
        return []
    where, params = _comment_base_where(category_id, scope_tids, tags, since, until)
    sql = ("SELECT cc.id, cc.embedding FROM comment_chunks cc "
           "JOIN comments c ON c.id = cc.comment_id "
           "WHERE cc.embedding IS NOT NULL" + where)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    ids, vecs = [], []
    for r in rows:
        vec = embeddings.blob_to_vec(r["embedding"])
        if vec.shape[0] != embeddings.EMBED_DIM:
            continue  # the model changed -- incompatible ones are skipped, as with chunks
        ids.append(int(r["id"]))
        vecs.append(vec)
    if not ids:
        return []
    sims = np.stack(vecs) @ qvec
    top = np.argsort(-sims)[:candidate_k]
    return [(_cc_key(ids[i]), float(sims[i])) for i in top]


def _comment_fts_search(db_path: str, query: str, candidate_k: int,
                        category_id=None,
                        scope_tids: Optional[list[int]] = None,
                        tags: Optional[list[str]] = None,
                        since: Optional[str] = None,
                        until: Optional[str] = None
                        ) -> list[tuple[int, float]]:
    words = _fts_words(query)
    if not words:
        return []
    fts_q = " OR ".join(f'"{w}"' for w in words)
    where, params = _comment_base_where(category_id, scope_tids, tags, since, until)
    sql = ("SELECT f.rowid AS rid, f.rank AS rank FROM comment_chunks_fts f "
           "JOIN comment_chunks cc ON cc.id = f.rowid "
           "JOIN comments c ON c.id = cc.comment_id "
           "WHERE comment_chunks_fts MATCH ?" + where + " ORDER BY f.rank LIMIT ?")
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, [fts_q] + params + [candidate_k]).fetchall()
    # The soft cutoff that acts on transcripts (_FTS_MIN_MATCHES) is NOT
    # applied here: a comment is one or two sentences, and the requirement "two terms from the
    # query in one chunk" would cut off exactly the short corrections the layer
    # was made for ("the sum is 12k, not 20k" against a ten-word question).
    return [(_cc_key(int(r["rid"])), -float(r["rank"])) for r in rows]


def _hydrate_comments(db_path: str, keys: list[int]) -> dict[int, dict]:
    """Provenance of comment chunks: the comment itself, its kind/weight and the title of the card
    it is attached to."""
    if not keys:
        return {}
    ids = [-k for k in keys]
    ph = ",".join("?" * len(ids))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT cc.id AS cc_id, cc.text, cc.chunk_index, "
            f"c.id AS comment_id, c.target_type, c.target_id, c.kind, c.weight, "
            f"c.pinned, c.anchor_time, c.author, c.source AS comment_source, c.confirmed_at, "
            f"substr(c.created_at, 1, 10) AS comment_date, "
            f"t.source_name AS tx_name, t.source_type AS tx_source_type, "
            f"COALESCE(t.meeting_date, substr(t.created_at, 1, 10)) AS tx_date, "
            f"a.title AS audio_title "
            f"FROM comment_chunks cc "
            f"JOIN comments c ON c.id = cc.comment_id "
            f"LEFT JOIN transcriptions t ON c.target_type = 'transcription' AND t.id = c.target_id "
            f"LEFT JOIN audio_downloads a ON c.target_type = 'audio_download' AND a.id = c.target_id "
            f"WHERE cc.id IN ({ph}) AND c.deleted_at IS NULL",
            ids).fetchall()
    return {_cc_key(r["cc_id"]): r for r in rows}


def _target_label(row) -> str:
    """Human title of the card a comment is written on -- needed both for the
    RAG answer ("what is this clarification about") and for the click in the output."""
    if row["target_type"] == "transcription":
        return row["tx_name"] or tr("svc_retrieval.comment.target_record", id=row['target_id'])
    if row["target_type"] == "audio_download":
        return row["audio_title"] or tr("svc_retrieval.comment.target_audio", id=row['target_id'])
    return f"{row['target_type']} #{row['target_id']}"


def _comment_entry(key: int, row, meta: dict) -> dict:
    """Output item for a comment chunk -- deliberately the same shape as for a
    transcript chunk (`chunk_id`, `text`, `score`, `meeting_date`), so that all
    existing consumers (copilot, MCP, frontend) work without changes; they are told apart by the
    field `source_type='comment'`."""
    from app.services import comments as comments_svc
    return {
        "chunk_id": key,                      # negative -- see _cc_key
        "comment_id": row["comment_id"],
        "comment_chunk_id": -key,
        "source_type": "comment",
        "comment_kind": row["kind"],
        "comment_weight": comments_svc.kind_weight(row["kind"], row["weight"],
                                                      row["comment_source"], row["confirmed_at"]),
        "pinned": bool(row["pinned"]),
        "author": row["author"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "target_label": _target_label(row),
        # The same key as for transcript chunks: the consumer does not have to
        # branch by type to name the source.
        "display_name": _target_label(row),
        "transcription_id": row["target_id"] if row["target_type"] == "transcription" else None,
        "source_name": _target_label(row),
        "doc_type": None,
        "meeting_date": row["comment_date"],
        "target_date": row["tx_date"],
        "speaker": row["author"],
        "start_time": row["anchor_time"],
        "end_time": None,
        "page": None,
        "section": None,
        "text": row["text"],
        "score": round(meta["final"], 5),
        "matched_by": sorted(set(meta["modes"])),
    }


def _why_top(cid: int, meta: dict, rerank_moved: set[int],
            rec_w: float, com_w: float) -> str:
    """Which stage ACTUALLY moved the result's position (Story 09, plan D1).

    It used to be: a fixed chain of priorities that named the stage that COULD have
    fired (recency was returned as soon as `_recency_factor` gave something positive,
    even at `rec_w=0`, when the multiplier = 1.0 and changed nothing). Now we
    compare the ACTUAL contributions: rerank weighs the most when it really
    moved this candidate (a positional change, not a multiplier -- comparing it by
    magnitude with recency/comment makes no sense); otherwise the multiplier with the larger
    actual contribution (`rec_w * recency` vs `comment_delta`); if neither
    moved anything -- `rrf`."""
    if cid in rerank_moved:
        return "rerank"
    rec_delta = rec_w * meta.get("recency", 0.0)
    com_delta = meta.get("comment_delta", 0.0)
    if rec_delta <= 0.0 and com_delta <= 0.0:
        return "rrf"
    return "comment_boost" if com_delta > rec_delta else "recency"


# Required keys of `why` (contract C2) -- ONE source of truth. Both
# producers (`_build_why` below and the attached-comment marker in
# `rag.order_citables`, via `build_placeholder_why`) take the list
# from here instead of rewriting it as a literal -- exactly the defect that was
# repaired twice (NEW-2, NEW-7) and fixed at the root by story 13
# (plan D3). The order of values when using `dict(zip(...))` must
# stay src/rrf/rec/by/top.
WHY_REQUIRED_KEYS: tuple[str, ...] = ("src", "rrf", "rec", "by", "top")


def build_placeholder_why(source_type: str, top: str, note: Optional[str] = None) -> dict:
    """`why` for items that did not go through `search()` (e.g. attached
    comments in `rag.order_citables`) -- zero contributions, the same
    required set of keys `WHY_REQUIRED_KEYS`. The additional `note`
    (allowed by contract C2) explains in words why the contributions are zero."""
    why: dict = dict(zip(WHY_REQUIRED_KEYS, (source_type, 0.0, 0.0, [], top)))
    if note:
        why["note"] = note
    return why


def _build_why(cid: int, meta: dict, source_type: str, matched_by: list[str],
               rerank_scores: dict[int, float], rerank_moved: set[int],
               stage_maps: dict[str, dict[int, tuple[int, float]]],
               explain: bool, rec_w: float, com_w: float,
               capped: dict[str, bool],
               rewrite_variants: Optional[list[str]] = None) -> dict:
    """Contract C2 (`docs/specs/grep-explainability/plan.md`): a compact `why`
    on every result, `stages` -- only when `explain=True`. Does not change
    ranking -- only reads the already computed `meta`/`rerank_scores`."""
    why: dict = dict(zip(WHY_REQUIRED_KEYS, (
        source_type,
        round(meta["score"], 5),
        round(meta.get("recency", 0.0), 5),
        matched_by,
        _why_top(cid, meta, rerank_moved, rec_w, com_w),
    )))
    if cid in rerank_scores:
        why["rr"] = round(rerank_scores[cid], 5)
    if explain:
        stages: dict = {}
        for label, smap in stage_maps.items():
            hit = smap.get(cid)
            if hit is None:
                continue
            pos, raw = hit
            field = "sim" if label in ("vector", "comment_vector") else "bm25"
            stages[label] = {"pos": pos, field: raw}
        why["stages"] = stages
        why["weights"] = {"recency": rec_w, "comment": com_w}
        why["final_raw"] = meta["final"]
        # A flag for the WHOLE search (the same on every result), not for
        # this candidate -- finding 12/09: the name must reflect this, so that
        # "capped" is not read as "this result was affected".
        why["search_capped"] = dict(capped)
        # production-rag-wave-b-07: variants of the rewritten query that produced
        # additional sub-queries of this `search()` call (empty when
        # rewrite is off). The same flag for the WHOLE search, not per candidate.
        why["rewrites"] = list(rewrite_variants or [])
    return why


def _rrf(result_lists: list[list[tuple[int, float]]],
         labels: Optional[list[str]] = None) -> dict[int, dict]:
    """Reciprocal Rank Fusion. Returns {chunk_id: {"score", "modes"}}.

    `labels` names the lists in `modes` (the provenance "found by what"). Without it
    the behaviour is as it was: the first two are vector/fts.
    """
    fused: dict[int, dict] = {}
    for mode_idx, results in enumerate(result_lists):
        if labels and mode_idx < len(labels):
            mode = labels[mode_idx]
        else:
            mode = ("vector", "fts")[mode_idx] if mode_idx < 2 else f"m{mode_idx}"
        for position, (cid, _score) in enumerate(results):
            entry = fused.setdefault(cid, {"score": 0.0, "modes": []})
            entry["score"] += 1.0 / (_RRF_K + position + 1)
            entry["modes"].append(mode)
    return fused


def _group_key(row) -> tuple:
    """What to count as one source when diversifying the output.

    For Telegram a record = one message, so the max_per_meeting cap per
    transcript restricts nothing. The source for TG is:

    - the THREAD, if there is one (Wave 4.5) -- this is the most precise level: in a fund chat
      Acmecorp, Nova Dance and legal questions go on at once, and a per-chat cap
      would choke them against each other, though these are different conversations;
    - otherwise the CHAT -- as it was in Wave 4, while threads are not laid out.

    For the remaining sources -- the transcript, as it was."""
    if row["source_type"] == "telegram":
        thread_id = row["tg_thread_id"] if "tg_thread_id" in row.keys() else None
        if thread_id is not None:
            return ("tgt", thread_id)
        if row["tg_chat_id"] is not None:
            return ("tg", row["tg_chat_id"])
    return ("tx", row["transcription_id"])


def _diversify(ranked_ids: list[int], tid_of: dict[int, object],
               top_k: int, max_per_meeting: int) -> list[int]:
    """Select top_k chunks with the max_per_meeting limit per SOURCE.

    The source is not always a transcript: for Telegram a record = one message, so
    the cap "3 per transcript" NEVER worked there, and eight messages from
    one chat calmly took all eight slots, pushing out calls and
    documents. For TG the CHAT counts as the source (see _group_key).

    The shortfall (if the cap left empty slots) is filled with the rest in rank order --
    better a few duplicates from one meeting than to return fewer than top_k."""
    if max_per_meeting <= 0:
        return ranked_ids[:top_k]
    selected: list[int] = []
    overflow: list[int] = []
    counts: dict[int, int] = {}
    for cid in ranked_ids:
        tid = tid_of.get(cid)
        if len(selected) < top_k and counts.get(tid, 0) < max_per_meeting:
            selected.append(cid)
            counts[tid] = counts.get(tid, 0) + 1
        else:
            overflow.append(cid)
    i = 0
    while len(selected) < top_k and i < len(overflow):
        selected.append(overflow[i])
        i += 1
    return selected


def _apply_rerank(query: str, ranked_ids: list[int], by_id: dict,
                  pool_size: int) -> tuple[list[int], dict[int, float]]:
    """T6.4: rerank the TOP pool (pool_size) of candidates with the local
    cross-encoder (reranker.rerank) by real semantic relevance
    to the query. The tail OUTSIDE the pool stays in the original RRF+recency
    order -- appended after the reranked pool unchanged.

    Order relative to the heuristics: rerank is applied AFTER RRF+recency (they
    have already narrowed and ordered the candidates), BUT BEFORE the diversity cap -- i.e.
    rerank decides WHICH chunks are semantically most relevant (including the order
    inside one meeting), and the cap then only keeps one meeting from
    taking the whole top. This keeps the diversity behaviour unchanged (the same
    _diversify call), while making the input order more precise.

    Graceful degradation is inherited transparently from reranker.rerank(): if the
    model is unavailable/failed -- the pool is returned in the original order, and the
    result is identical to a call without rerank.

    Returns (new ranked_ids, {chunk_id: rerank_score} for the pool only).
    """
    if len(ranked_ids) <= 1 or pool_size <= 0:
        return ranked_ids, {}
    pool_ids = ranked_ids[:pool_size]
    tail_ids = ranked_ids[pool_size:]
    items = [{"id": cid, "text": (by_id[cid]["text"] or "")} for cid in pool_ids]
    t0 = time.time()
    reranked = reranker.rerank(query, items, text_key="text")
    logger.debug("[retrieval] rerank pool=%d in %.3fs", len(items), time.time() - t0)
    new_pool_ids = [it["id"] for it in reranked]
    scores = {it["id"]: it["rerank_score"] for it in reranked if "rerank_score" in it}
    return new_pool_ids + tail_ids, scores


def search(db_path: str, query: str, top_k: int = 8,
           candidate_k: Optional[int] = None,
           category_id: Optional[int] = None,
           recency_weight: Optional[float] = None,
           max_per_meeting: Optional[int] = None,
           rerank: bool = False,
           rerank_pool_size: Optional[int] = None,
           scope_tids: Optional[list[int]] = None,
           include_comments: bool = True,
           comment_weight: Optional[float] = None,
           comment_share: Optional[float] = None,
           explain: bool = False,
           rewrite: Optional[bool] = None,
           *,
           tags: Optional[list[str]] = None,
           since: Optional[str] = None,
           until: Optional[str] = None) -> dict:
    """Hybrid search. category_id -- restrict to a node of the directions tree
    (None = all): a direction id expands into itself AND its projects,
    a project id gives only itself, 'none' -- records without a direction
    (`app.services.categories.filter_sql`).
    scope_tids -- narrow to specific records (the second scope layer on top of
    the category); the list is computed by `app.services.scope.scope_filter_ids`.
    recency_weight/max_per_meeting -- overrides of the defaults (for tests/tuning).
    rerank -- an optional second stage of reranking by the local cross-encoder
    (T6.4), OFF by default; enabled selectively only for the RAG chat
    (see app/services/rag.py) -- does not touch copilot/categorize/MCP calls.
    include_comments -- mix in the owner's comments (Wave 2) with a boost by
    kind; comment_weight -- override of the default (for tests/tuning);
    comment_share -- a ceiling on the share of comments in the top (Wave 2.5).
    explain -- Story 02 (`docs/specs/grep-explainability`): False (default) --
    a compact `why` on every result, True -- adds `why["stages"]` with the
    position and raw score at every stage where the candidate was met. Does not
    affect ranking or output order.
    tags/since/until -- directions-projects-tags-03 (contract C5), keyword-only:
    `tags` -- tag names (a list or a comma-separated string), OR between them, with
    `category_id` -- AND; an unknown name narrows the output to empty (the filter
    is never "ignored"). `since`/`until` -- absolute ISO dates
    `YYYY-MM-DD`, BOTH bounds inclusive, over `COALESCE(meeting_date, created_at)`
    of the record (a malformed date -> ValueError from `categories.parse_date`). All three
    are applied at the SQL level BEFORE RRF/rerank in both branches and in the comment
    branch -- not as a post-filter after ranking.
    rewrite -- production-rag-wave-b-07: `None` (default) reads the hot env
    `RAG_QUERY_REWRITE`; `False`/disabled env -- `query_rewrite.rewrite_query`
    is NOT called at all, the output is byte-for-byte as without the flag. `True` -- 1-3
    alternative formulations of the query (Claude) give ADDITIONAL vector+FTS
    sub-queries into the same list before RRF (`_rrf` does not tell them from
    the original -- it does not touch RRF/recency/cap/rerank/comment layer).
    Returns {"query", "chunks": [...], "vector_available": bool}."""
    query = (query or "").strip()
    if not query:
        return {"query": query, "chunks": [], "vector_available": embeddings.is_available()}

    rec_w = _RECENCY_WEIGHT if recency_weight is None else recency_weight
    cap = _MAX_PER_MEETING if max_per_meeting is None else max_per_meeting
    com_w = _COMMENT_WEIGHT if comment_weight is None else comment_weight
    comment_share = (_COMMENT_MAX_SHARE if comment_share is None else comment_share)
    if rewrite is None:
        rewrite = _settings.env_bool("RAG_QUERY_REWRITE")

    candidate_k = candidate_k or max(top_k * 5, 40)
    axes = {"tags": tags, "since": since, "until": until}
    vec_hits = _vector_search(db_path, query, candidate_k, category_id, scope_tids, **axes)
    fts_hits = _fts_search(db_path, query, candidate_k, category_id, scope_tids, **axes)

    lists = [vec_hits, fts_hits]
    labels = ["vector", "fts"]

    # production-rag-wave-b-07: variants of the rewritten query -- ADDITIONAL
    # vector+FTS sub-queries into the same list before RRF, with the same
    # labels "vector"/"fts" (RRF and matched_by do not tell WHOSE
    # formulation it is -- the original or a variant). rewrite_query() is best-effort:
    # an empty list when the flag is off, on an API failure or invalid JSON.
    rewrite_variants: list[str] = []
    if rewrite:
        from app.services import query_rewrite
        rewrite_variants = query_rewrite.rewrite_query(query)
        for variant in rewrite_variants:
            lists.append(_vector_search(db_path, variant, candidate_k, category_id,
                                        scope_tids, **axes))
            labels.append("vector")
            lists.append(_fts_search(db_path, variant, candidate_k, category_id,
                                     scope_tids, **axes))
            labels.append("fts")

    if include_comments:
        # The comment index may be absent on an old DB (migration v37 not yet
        # applied) -- then we simply work as before. Breaking the search over a
        # missing table is not allowed: this is the main path of the whole product.
        ck = min(candidate_k, _COMMENT_CANDIDATE_CAP)
        try:
            comment_lists = [
                _comment_vector_search(db_path, query, ck, category_id, scope_tids, **axes),
                _comment_fts_search(db_path, query, ck, category_id, scope_tids, **axes),
            ]
        except sqlite3.OperationalError as exc:
            # An old DB without migration v37 (comment index) -- we drop ONLY the
            # comment lists, not a positional slice [:2]: with `rewrite` enabled
            # the extra vector/fts sub-queries of the variants already stand before them
            # (production-rag-wave-b-07), and a [:2] slice would silently throw them all out.
            logger.debug("[retrieval] comment index unavailable: %s", exc)
        else:
            lists += comment_lists
            labels += ["comment_vector", "comment_fts"]

    # explain=True: the position + raw score of each candidate in each input
    # list (before RRF) -- exactly what the rank alone lacks (C2).
    # Labels are NOT renamed for rewritten-query variants (History 07,
    # contract S2 C2: `by` stays vector/fts) -- so a label may repeat
    # (the original + variant(s), each with its own vector/fts list). So this is a
    # MERGE by label, not an overwrite by the last list: a candidate found by both the
    # original and a variant keeps the record with the best (smallest) position
    # of that very list.
    stage_maps: dict[str, dict[int, tuple[int, float]]] = {}
    for lbl, lst in zip(labels, lists):
        smap = stage_maps.setdefault(lbl, {})
        for pos, (cid, raw) in enumerate(lst):
            cur = smap.get(cid)
            if cur is None or pos < cur[0]:
                smap[cid] = (pos, raw)

    fused = _rrf(lists, labels)
    if not fused:
        return {"query": query, "chunks": [], "vector_available": embeddings.is_available()}

    # Hydrate the provenance of ALL candidates (<= ~2*candidate_k rows) -- we need
    # transcription_id (diversification) and meeting_date (recency) BEFORE the final cut.
    comment_keys = [k for k in fused if _is_comment_key(k)]
    comment_rows = _hydrate_comments(db_path, comment_keys)
    candidate_ids = [k for k in fused if not _is_comment_key(k)]
    rows = []
    if candidate_ids:
        placeholders = ",".join("?" * len(candidate_ids))
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT ch.id, ch.transcription_id, ch.chunk_index, ch.start_time, "
                f"ch.end_time, ch.speaker, ch.text, ch.page, ch.section, "
                f"t.source_name, t.title, t.source_type, t.doc_type, "
                f"t.tg_chat_id, t.tg_chat_title, t.tg_message_id, t.tg_link, t.tg_reply_to, "
                f"t.tg_thread_id, "
                f"COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date "
                f"FROM chunks ch JOIN transcriptions t ON t.id = ch.transcription_id "
                f"WHERE ch.id IN ({placeholders}) AND t.deleted_at IS NULL "
                f"AND t.duplicate_of IS NULL",
                candidate_ids,
            ).fetchall()
    by_id = {r["id"]: r for r in rows}

    # Recency: multiply the RRF score by a soft recency boost.
    # Comments get an ADDITIONAL multiplier by kind (correction > note >
    # question) -- it is what makes the owner's clarification weigh more than the raw
    # transcript at equal relevance.
    now = datetime.now()
    for cid, meta in fused.items():
        if _is_comment_key(cid):
            cr = comment_rows.get(cid)
            if cr is None:
                meta["recency"] = 0.0
                meta["final"] = 0.0
                continue
            from app.services import comments as comments_svc
            kw = comments_svc.kind_weight(cr["kind"], cr["weight"],
                                          cr["comment_source"], cr["confirmed_at"])
            rec = _recency_factor(cr["comment_date"], now)
            meta["recency"] = rec
            # The actual contribution of the comment multiplier -- _why_top needs it in order to
            # compare REAL contributions, not the mere presence of a comment key.
            meta["comment_delta"] = com_w * kw
            meta["final"] = meta["score"] * (1.0 + rec_w * rec) * (1.0 + com_w * kw)
            continue
        r = by_id.get(cid)
        rec = _recency_factor(r["meeting_date"], now) if r else 0.0
        meta["recency"] = rec
        meta["final"] = meta["score"] * (1.0 + rec_w * rec)

    # Rank by the final score (drop chunks without provenance), then the cap.
    def _hydrated(k: int) -> bool:
        return (k in comment_rows) if _is_comment_key(k) else (k in by_id)

    ranked_ids = [cid for cid, _ in sorted(
        ((cid, m) for cid, m in fused.items() if _hydrated(cid)),
        key=lambda kv: kv[1]["final"], reverse=True)]

    rerank_scores: dict[int, float] = {}
    rerank_moved: set[int] = set()
    if rerank:
        pool = _RERANK_POOL_SIZE if rerank_pool_size is None else rerank_pool_size
        text_of = {k: (comment_rows[k] if _is_comment_key(k) else by_id[k])
                   for k in ranked_ids}
        pre_pool_ids = ranked_ids[:pool]
        ranked_ids, rerank_scores = _apply_rerank(query, ranked_ids, text_of, pool)
        post_pool_ids = ranked_ids[:pool]
        # Who from the rerank stage really changed the position -- not "who is in the pool"
        # (_why_top must say "rerank" only when the position was actually moved).
        rerank_moved = {cid for i, cid in enumerate(post_pool_ids)
                        if i >= len(pre_pool_ids) or pre_pool_ids[i] != cid}

    # The ceiling on the share of comments in the top -- BEFORE diversification, because it
    # decides who competes for the slots at all. The measurement showed that without it
    # comments take almost everything (see _COMMENT_MAX_SHARE).
    _pre_cap_ids = ranked_ids
    ranked_ids = _cap_comments(ranked_ids, top_k, comment_share)
    # explain: whether the comment ceiling changed anything at all in this call
    # (a global flag for the whole search, not per candidate -- _cap_comments itself
    # does not mark WHICH candidates were shifted).
    _comment_share_capped = ranked_ids != _pre_cap_ids

    # Each comment is its own diversification group. The cap "no more than N from one
    # source" exists against a wordy call eating all slots; comments
    # have no such problem by construction (it is one or two sentences), and
    # under a shared key with the transcript they would compete with it for the same
    # limit -- i.e. a clarification would push out the context that explains it.
    tid_of = {cid: (("cm", comment_rows[cid]["comment_id"]) if _is_comment_key(cid)
                    else _group_key(by_id[cid]))
              for cid in ranked_ids}
    _naive_top = ranked_ids[:top_k]
    chosen = _diversify(ranked_ids, tid_of, top_k, cap)
    # explain: whether diversification changed the naive top-k cut (the same approach
    # as above for comments -- a flag for the whole search).
    _diversity_capped = chosen != _naive_top
    _capped = {"comment_share": _comment_share_capped, "diversity": _diversity_capped}

    out = []
    for cid in chosen:
        meta = fused[cid]
        if _is_comment_key(cid):
            entry = _comment_entry(cid, comment_rows[cid], meta)
            if cid in rerank_scores:
                entry["rerank_score"] = round(rerank_scores[cid], 5)
            entry["why"] = _build_why(cid, meta, entry["source_type"], entry["matched_by"],
                                      rerank_scores, rerank_moved, stage_maps, explain, rec_w, com_w,
                                      _capped, rewrite_variants)
            out.append(entry)
            continue
        r = by_id[cid]
        entry = {
            "chunk_id": cid,
            "transcription_id": r["transcription_id"],
            "source_name": r["source_name"],
            # The record's own title (contract C2): `source_name` stays the
            # provenance (file name / TG preview) -- existing consumers and `why`
            # read it -- while the human name travels ALONGSIDE in separate
            # keys, so that the context for Claude and the UI name the record the way the
            # owner named it.
            "title": r["title"],
            "display_name": record_meta.display_name(
                {"title": r["title"], "source_name": r["source_name"],
                 "id": r["transcription_id"]}),
            "source_type": r["source_type"],
            "doc_type": r["doc_type"],
            "meeting_date": r["meeting_date"],
            "speaker": r["speaker"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "page": r["page"],
            "section": r["section"],
            "text": r["text"],
            "score": round(meta["final"], 5),
            "matched_by": sorted(set(meta["modes"])),
        }
        # Telegram provenance: without (tg_chat_id, tg_message_id) a finding cannot be
        # opened in the thread, and tg_chat_title is the only thing that says IN WHICH chat it was
        # said (source_name is "[TG] chat: snippet", from which the chat had to be
        # dug out with a regex). tg_link is not always there: in legacy groups
        # a message link physically does not exist, so the bridge is built on the id pair.
        if r["source_type"] == "telegram":
            entry.update({
                "tg_chat_id": r["tg_chat_id"],
                "tg_chat_title": r["tg_chat_title"],
                "tg_message_id": r["tg_message_id"],
                "tg_link": r["tg_link"],
                "tg_reply_to": r["tg_reply_to"],
                "tg_thread_id": r["tg_thread_id"],
            })
        if cid in rerank_scores:
            entry["rerank_score"] = round(rerank_scores[cid], 5)
        entry["why"] = _build_why(cid, meta, entry["source_type"], entry["matched_by"],
                                  rerank_scores, rerank_moved, stage_maps, explain, rec_w, com_w,
                                  _capped, rewrite_variants)
        out.append(entry)
    return {"query": query, "chunks": out, "vector_available": embeddings.is_available()}


#: How many attached comments to add to one answer at most. The ceiling
#: is needed because a card with thirty clarifications would otherwise eat the whole context
#: window, pushing out the very material these clarifications explain.
_ATTACH_COMMENT_CAP = int(os.environ.get("RAG_ATTACH_COMMENT_CAP", "8"))


def attach_comments(db_path: str, chunks: list[dict],
                    cap: int = _ATTACH_COMMENT_CAP) -> list[dict]:
    """Attach to the output the pinned comments and corrections of those cards that made it
    into it (Wave 2, layer 2 of priority).

    Why. The boost in ranking works only when the comment ITSELF matched the
    query. But the most valuable comment often does not match: "Ivan is no longer on the
    project" shares no words with "who is responsible for the facade", and "actually the
    sum is 12k" will be found only if the question already names the sum. That is, exactly those
    clarifications the layer was made for would be missed by the boost.

    So: if the output has a fragment of record X, then `pinned` comments and
    correction comments of this X arrive in the context regardless of
    a match -- the mechanics mirror `attach_thread_context` for TG threads.
    ONLY the kinds in `comments.ATTACH_KINDS` are attached, plus those pinned
    manually: if everything were attached indiscriminately, every answer would pull all the notes
    of the card and the layer would turn into noise. An agent comment (`source='mcp'`) the owner
    has not confirmed is never attached: rag labels attached comments as the owner's and ranks
    them above the transcript.

    Returns a LIST of comments (does not mutate chunks) -- rag renders them as a separate
    priority block, not mixed in with the citations.
    """
    from app.services import comments as comments_svc

    tids = {ch.get("transcription_id") for ch in chunks
            if ch.get("source_type") != "comment" and ch.get("transcription_id")}
    already = {ch.get("comment_id") for ch in chunks
               if ch.get("source_type") == "comment"}
    if not tids:
        return []

    kinds = sorted(comments_svc.ATTACH_KINDS)
    ph_t = ",".join("?" * len(tids))
    ph_k = ",".join("?" * len(kinds))
    try:
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT c.id, c.target_id, c.body, c.kind, c.weight, c.pinned, "
                f"c.author, c.anchor_time, c.source, c.confirmed_at, "
                f"substr(c.created_at, 1, 10) AS comment_date, "
                f"t.source_name AS tx_name "
                f"FROM comments c "
                f"LEFT JOIN transcriptions t ON t.id = c.target_id "
                f"WHERE c.deleted_at IS NULL AND c.target_type = 'transcription' "
                f"AND c.target_id IN ({ph_t}) "
                f"AND (c.pinned = 1 OR c.kind IN ({ph_k})) "
                f"AND (c.source IS NOT 'mcp' OR c.confirmed_at IS NOT NULL) "
                f"ORDER BY c.pinned DESC, c.created_at DESC",
                list(tids) + kinds).fetchall()
    except sqlite3.OperationalError as exc:
        logger.debug("[retrieval] comments not attached (no table?): %s", exc)
        return []

    out = []
    for r in rows:
        if r["id"] in already:
            continue                      # already arrived as a standalone finding
        out.append({
            "comment_id": r["id"],
            "transcription_id": r["target_id"],
            "target_label": r["tx_name"] or tr("svc_retrieval.comment.target_record", id=r['target_id']),
            "kind": r["kind"],
            "weight": comments_svc.kind_weight(r["kind"], r["weight"],
                                              r["source"], r["confirmed_at"]),
            "pinned": bool(r["pinned"]),
            "author": r["author"],
            "anchor_time": r["anchor_time"],
            "date": r["comment_date"],
            "text": r["body"],
            "attached": True,
        })
        if len(out) >= cap:
            break
    return out


# How many thread neighbours to attach to a finding. 6 is a compromise: a typical thread is
# short (the median burst is 4 messages), and pulling a long correspondence whole into the
# prompt makes no sense -- it would push other sources out of the window.
_THREAD_STITCH = int(os.environ.get("RAG_THREAD_STITCH", "6"))


def _truncate_chars(text: str, limit: int) -> tuple[str, bool]:
    """Truncates ``text`` to ``limit`` characters including the truncation suffix.

    The suffix is sized by the upper bound (the number of digits in the length of the whole text --
    the exact number of truncated characters is not greater than it), so the result is guaranteed
    to fit in ``limit`` without iterative fitting."""
    n = len(text)
    if n <= limit:
        return text, False
    reserve = len(f"…[обрізано, ще {n} симв.]")
    kept = max(limit - reserve, 0)
    omitted = n - kept
    suffix = f"…[обрізано, ще {omitted} симв.]"
    return text[:kept] + suffix, True


def attach_thread_context(db_path: str, chunks: list[dict],
                          max_msgs: int = _THREAD_STITCH) -> list[dict]:
    """Attach to TG findings the neighbours of their thread (Wave 4.5).

    Why. In correspondence a finding is often a QUESTION, and the needed answer is
    in the next message, with no words in common with the query, so neither vector nor FTS
    will fetch it. A live failure: on "when is the meeting with the governor"
    the output gave 7 slots out of 8, and all seven were questions without a single answer.
    A thread (v32) gives the same conversation whole, so the answer arrives together with
    the question and does NOT take a separate output slot.

    Mutates the list elements (adds ``thread``), returns the same list.
    Findings without a thread stay as they were -- that is not an error but the state "threads are
    not laid out yet for this chat"."""
    thread_ids = {ch.get("tg_thread_id") for ch in chunks
                  if ch.get("source_type") == "telegram" and ch.get("tg_thread_id")}
    if not thread_ids:
        return chunks

    ids = list(thread_ids)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT tg_thread_id, tg_message_id, tg_date, tg_sender, transcript_text "
            f"FROM transcriptions WHERE tg_thread_id IN ({','.join('?' * len(ids))}) "
            f"AND deleted_at IS NULL ORDER BY tg_thread_id, tg_date",
            ids).fetchall()
        labels = dict(conn.execute(
            f"SELECT id, label FROM tg_threads WHERE id IN ({','.join('?' * len(ids))})",
            ids).fetchall())

    by_thread: dict[int, list] = {}
    for r in rows:
        by_thread.setdefault(r["tg_thread_id"], []).append(r)

    # Ceilings are read by a function (not a module constant) -- they switch without
    # a process restart, like the rest of the env settings in settings.py.
    msg_limit = _settings.env_int("RAG_THREAD_MSG_CHARS")
    thread_limit = _settings.env_int("RAG_THREAD_CHARS")

    for ch in chunks:
        tid = ch.get("tg_thread_id")
        msgs = by_thread.get(tid) if tid else None
        if not msgs or len(msgs) < 2:
            continue
        # A window around the finding itself, not the start of the thread: in a long conversation
        # the first messages may have nothing to do with what was found.
        hit = next((i for i, m in enumerate(msgs)
                    if m["tg_message_id"] == ch.get("tg_message_id")), 0)
        half = max_msgs // 2
        start = max(0, hit - half)
        window = msgs[start:start + max_msgs]

        chars_total = sum(len(m["transcript_text"] or "") for m in window)

        hit_pos = hit - start  # position of the finding inside the window
        hit_row = window[hit_pos]

        # The hit gets a budget that is never smaller than a neighbour's budget: not
        # less than msg_limit, but not more than half of the thread ceiling (to leave
        # room for at least one neighbour), and in no case more than the ceiling.
        hit_budget = min(thread_limit, max(msg_limit, thread_limit // 2))
        hit_text, hit_truncated = _truncate_chars(hit_row["transcript_text"] or "", hit_budget)
        remaining = thread_limit - len(hit_text)

        included: dict[int, dict] = {
            hit_row["tg_message_id"]: {
                "tg_message_id": hit_row["tg_message_id"], "date": hit_row["tg_date"],
                "sender": hit_row["tg_sender"], "text": hit_text, "is_hit": True,
            }
        }
        if hit_truncated:
            included[hit_row["tg_message_id"]]["truncated"] = True

        # Neighbours in order of closeness to the hit (the closest by tg_date first,
        # from both sides in turn) -- not in the window's chronological order.
        neighbour_order = []
        dist = 1
        while hit_pos - dist >= 0 or hit_pos + dist < len(window):
            if hit_pos - dist >= 0:
                neighbour_order.append(window[hit_pos - dist])
            if hit_pos + dist < len(window):
                neighbour_order.append(window[hit_pos + dist])
            dist += 1

        dropped = 0
        for idx, m in enumerate(neighbour_order):
            budget = min(msg_limit, remaining)
            raw = m["transcript_text"] or ""
            n = len(raw)
            if n > budget:
                reserve = len(f"…[обрізано, ще {n} симв.]")
                kept = max(budget - reserve, 0)
                if kept <= 0:
                    # The remainder fits nothing but the suffix -- the neighbour and all
                    # further ones (in order of closeness) are dropped.
                    dropped = len(neighbour_order) - idx
                    break
            text, truncated = _truncate_chars(raw, max(budget, 0))
            remaining -= len(text)
            msg = {"tg_message_id": m["tg_message_id"], "date": m["tg_date"],
                   "sender": m["tg_sender"], "text": text, "is_hit": False}
            if truncated:
                msg["truncated"] = True
            included[m["tg_message_id"]] = msg

        # The order of messages stays chronological (the window order).
        messages = [included[m["tg_message_id"]] for m in window
                    if m["tg_message_id"] in included]

        chars_kept = sum(len(m["text"]) for m in messages)
        ch["thread"] = {
            "thread_id": tid,
            "label": labels.get(tid),
            "total_messages": len(msgs),
            "chars_total": chars_total,
            "chars_kept": chars_kept,
            "dropped": dropped,
            "messages": messages,
        }
    return chunks
