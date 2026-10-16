"""Comments Layer: data, index, graph, live call.

A comment is a sentence the owner wrote ABOUT a recording, not something said IN the recording.
It clarifies, corrects or emphasizes what the raw transcript says badly,
incompletely or simply wrongly. That is why in search it weighs MORE than the transcript,
not the same as it.

Five things this module holds:

1. **CRUD with a polymorphic target.** Not only a transcript can be commented: a Media
   Library file without text, a task, an entity, a recording session. The registry of allowed
   targets (`TARGETS`) lives here, not in the DB, the same way `action_items.source`
   and `meeting_entities.source` are validated in code. A target is addressed by a number
   or a string (a recording session is `rec_<hex>`), and the registry decides which.

2. **Own index.** `comment_chunks` + `comment_chunks_fts` (migration v37)
   mirror `chunks`/`chunks_fts` in shape but are a separate table. Why not a
   shared table: the long reason is written in the migration itself; the short one: in
   `chunks.transcription_id` is NOT NULL, while a comment on an untranscribed file
   has no transcript.

3. **Weight.** `kind` → a ranking multiplier (Wave 2) and a "pin
   even without a match" flag. `weight` is a manual override; NULL means "take from kind".

4. **Derivatives.** Local links to the graph (`link_entities`, free, at
   ingest) and an on-demand Claude analysis (`analyze` — tasks and entities,
   never automatic).

5. **Live call.** A comment is written to a recording session while the transcript does not
   exist yet, and moves onto it after transcription (`retarget`), keeping
   `anchor_time`.

Degradation. Without an embedding model (`embeddings.is_available()` = False)
a comment is still created, lands in FTS (triggers) and shows on the card,
just without a vector, until the next `reindex`. There is no silent loss:
`embedded_at` stays NULL, and the backfill sees the work.

CLI (offline, idempotent):

    python -m app.services.comments reindex [--force] [--dry-run] [--limit N]
    python -m app.services.comments relink [--dry-run]
    python -m app.services.comments analyze <id> [--force]     # paid
    python -m app.services.comments stats
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.services import embeddings, record_meta


logger = logging.getLogger(__name__)


# ============================================================
# Registry of targets and kinds
# ============================================================

#: What can be commented: target_type → (table, id column, key kind).
#: Validation is here, not an FK in the DB: an FK to one table is impossible for a
#: polymorphic target, and keeping eight nullable columns instead of a (type, id)
#: pair is worse in everything except formal integrity.
#:
#: The key kind ("int" | "str") is not a detail but what a card is addressed by.
#: A recording session has a string id (`rec_<hex16>`), and it is needed exactly
#: when a comment is most valuable: during a call the transcript does not exist yet. String
#: targets live in `target_key`, numeric ones in `target_id` (migration v39).
TARGETS: dict[str, tuple[Optional[str], Optional[str], str]] = {
    "transcription": ("transcriptions", "id", "int"),
    "audio_download": ("audio_downloads", "id", "int"),
    "action_item": ("action_items", "id", "int"),
    "entity": ("entities", "id", "int"),
    "speaker": ("speakers", "id", "int"),
    "category": ("categories", "id", "int"),
    "tg_thread": ("tg_threads", "id", "int"),
    # Recording session: string key, no table of its own (it lives in the manifest on
    # disk), so we do not check existence, otherwise creating a comment
    # during a call would be impossible. After transcription the session's comments
    # are re-attached to the transcript (`retarget`, Wave 4).
    "recording_session": (None, None, "str"),
}


def key_kind(target_type: str) -> str:
    """Which key this target type is addressed by: 'int' or 'str'."""
    entry = TARGETS.get(target_type)
    return entry[2] if entry else "int"


#: What goes in `target_id` when a card is addressed by a string. The column was created
#: in v37 as `NOT NULL`, and dropping the constraint would require
#: rebuilding the table that `comment_chunks` references by FK — too much
#: risk for cosmetics. Zero is safe: AUTOINCREMENT in SQLite
#: starts at 1, so id=0 belongs to no row of any table.
STR_TARGET_ID = 0


def _norm_target(target_type: str, target_id) -> tuple[int, Optional[str]]:
    """(target_id, target_key) for the key kind of this target type."""
    if key_kind(target_type) == "str":
        key = str(target_id).strip()
        if not key:
            raise CommentError(tr("svc_pipes.comments.err.empty_key"))
        return STR_TARGET_ID, key
    try:
        return int(target_id), None
    except (TypeError, ValueError):
        raise CommentError(tr("svc_pipes.comments.err.target_numeric_got", target_type=target_type, target_id=target_id))


def _target_clause(target_type: str, target_id) -> tuple[str, list]:
    """WHERE fragment for "this same card" — one switch for the whole module."""
    tid, tkey = _norm_target(target_type, target_id)
    if tkey is not None:
        return " AND target_type = ? AND target_key = ?", [target_type, tkey]
    return " AND target_type = ? AND target_id = ?", [target_type, tid]

#: kind → ranking weight. Not a decorative list: it decides whether a
#: comment outweighs the raw transcript at equal relevance.
KIND_WEIGHTS: dict[str, float] = {
    "correction": 1.0,   # corrects what was said in the recording
    "decision": 0.8,     # summary / decision made
    "note": 0.6,         # ordinary note (default)
    "context": 0.5,      # background, backstory
    "question": 0.4,     # open question to self
}
DEFAULT_KIND = "note"

#: Kinds that are attached to results even if they did not match the query themselves
#: (Wave 2). A correction is critical exactly where there are no words shared with the question:
#: "Ivan is no longer on the project" does not match "who is responsible for X".
ATTACH_KINDS = frozenset({"correction"})

#: Creation sources, for provenance in results and export.
SOURCES = frozenset({"ui", "live", "mcp", "import"})

MAX_BODY_CHARS = 20000


class CommentError(ValueError):
    """Invalid input (unknown target/kind, empty body, no such card)."""


def kind_weight(kind: Optional[str], weight: Optional[float] = None) -> float:
    """Effective comment weight: the manual override, otherwise by kind."""
    if weight is not None:
        return max(0.0, min(1.0, float(weight)))
    return KIND_WEIGHTS.get((kind or DEFAULT_KIND), KIND_WEIGHTS[DEFAULT_KIND])


# ============================================================
# Validation
# ============================================================

def _validate(target_type: str, kind: Optional[str], source: Optional[str],
              body: str) -> tuple[str, str]:
    if target_type not in TARGETS:
        raise CommentError(
            tr("svc_pipes.comments.err.unknown_target",
            target_type=target_type, known=', '.join(sorted(TARGETS))))
    k = (kind or DEFAULT_KIND).strip().lower()
    if k not in KIND_WEIGHTS:
        raise CommentError(
            tr("svc_pipes.comments.err.unknown_kind",
            kind=kind, known=', '.join(sorted(KIND_WEIGHTS))))
    s = (source or "ui").strip().lower()
    if s not in SOURCES:
        raise CommentError(tr("svc_pipes.comments.err.unknown_source", source=source))
    if not (body or "").strip():
        raise CommentError(tr("svc_pipes.comments.err.empty_body"))
    if len(body) > MAX_BODY_CHARS:
        raise CommentError(tr("svc_pipes.comments.err.too_long", max=MAX_BODY_CHARS))
    return k, s


def target_exists(db_path: str, target_type: str, target_id: int) -> bool:
    """Whether the card the comment is attached to exists.

    The check is needed because the target is polymorphic and no FK holds it: without it
    a comment on a deleted/nonexistent card would quietly settle in the DB, be found by
    search and have nowhere to be shown. For types without a table of their own
    (`recording_session`) there is nothing to check, so we assume it exists.
    """
    entry = TARGETS.get(target_type)
    table, col = (entry[0], entry[1]) if entry else (None, None)
    if not table:
        return True
    with get_db_connection(db_path) as conn:
        exists = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone())
        if not exists:
            return False
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {col} = ?", (target_id,)).fetchone()
    return bool(row)


# ============================================================
# CRUD
# ============================================================

_ROW_FIELDS = (
    "id, target_type, target_id, target_key, body, kind, weight, pinned, anchor_time, "
    "anchor_chunk_id, author, source, lang, parent_id, created_at, updated_at, "
    "embedded_at, embedding_model, embedding_version, analyzed_at, analyzed_model"
)


def _to_dict(row) -> dict:
    d = dict(row)
    d["pinned"] = bool(d.get("pinned"))
    # One output contract for both key kinds: the consumer (frontend, MCP,
    # export) must not know which column holds the card's address.
    key = d.get("target_key")
    d["target_ref"] = key if key is not None else d.get("target_id")
    d["effective_weight"] = kind_weight(d.get("kind"), d.get("weight"))
    d["indexed"] = bool(d.get("embedded_at"))
    # The analysis is paid and started by a button, so the interface must tell apart
    # "not analyzed yet" from "analyzed, no tasks found", otherwise the owner
    # pays for the same comment twice.
    d["analyzed"] = bool(d.get("analyzed_at"))
    return d


def create(db_path: str, target_type: str, target_id, body: str,
           kind: str = DEFAULT_KIND, *, pinned: bool = False,
           weight: Optional[float] = None, anchor_time: Optional[float] = None,
           anchor_chunk_id: Optional[int] = None, author: Optional[str] = None,
           source: str = "ui", parent_id: Optional[int] = None,
           check_target: bool = True) -> dict:
    """Create a comment. Indexing is NOT done here, see `index_comment`
    (the blueprint queues it so the POST does not wait for the GPU).

    `target_id` is a number or a string depending on the target type (see `TARGETS`)."""
    k, s = _validate(target_type, kind, source, body)
    tid, tkey = _norm_target(target_type, target_id)
    if check_target and tkey is None and not target_exists(db_path, target_type, tid):
        raise CommentError(tr("svc_pipes.comments.err.no_card", target_type=target_type, target_id=target_id))
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO comments (target_type, target_id, target_key, body, kind, weight, "
            "pinned, anchor_time, anchor_chunk_id, author, source, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_type, tid, tkey, body.strip(), k, weight,
             1 if pinned else 0, anchor_time, anchor_chunk_id, author, s, parent_id),
        )
        cid = cur.lastrowid
        conn.commit()
        row = conn.execute(
            f"SELECT {_ROW_FIELDS} FROM comments WHERE id = ?", (cid,)).fetchone()
    logger.info("[comments] +%s#%s %s (%s)", target_type, target_id, cid, k)
    return _to_dict(row)


def get(db_path: str, comment_id: int) -> Optional[dict]:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_ROW_FIELDS} FROM comments "
            "WHERE id = ? AND deleted_at IS NULL", (comment_id,)).fetchone()
    return _to_dict(row) if row else None


def update(db_path: str, comment_id: int, *, body: Optional[str] = None,
           kind: Optional[str] = None, pinned: Optional[bool] = None,
           weight: Optional[float] = None,
           anchor_time: Optional[float] = None) -> Optional[dict]:
    """Edit a comment. Changing the BODY resets `embedded_at`, otherwise the index
    would keep the vector of the old revision forever while the card showed the new one
    (a silent mismatch between what is visible and what is searched)."""
    cur = get(db_path, comment_id)
    if not cur:
        return None
    sets, params = [], []
    body_changed = False
    if body is not None:
        _validate(cur["target_type"], kind or cur["kind"], "ui", body)
        body_changed = body.strip() != (cur["body"] or "").strip()
        sets.append("body = ?"); params.append(body.strip())
    if kind is not None:
        k = kind.strip().lower()
        if k not in KIND_WEIGHTS:
            raise CommentError(tr("svc_pipes.comments.err.unknown_kind_short", kind=kind))
        sets.append("kind = ?"); params.append(k)
    if pinned is not None:
        sets.append("pinned = ?"); params.append(1 if pinned else 0)
    if weight is not None:
        sets.append("weight = ?"); params.append(max(0.0, min(1.0, float(weight))))
    if anchor_time is not None:
        sets.append("anchor_time = ?"); params.append(anchor_time)
    if not sets:
        return cur
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if body_changed:
        sets.append("embedded_at = NULL")
    with get_db_connection(db_path) as conn:
        conn.execute(f"UPDATE comments SET {', '.join(sets)} WHERE id = ?",
                     params + [comment_id])
        if body_changed:
            # Old chunks are removed immediately, not at reindex: otherwise
            # in the window between the edit and reindex search would return text that
            # is no longer on the card.
            conn.execute("DELETE FROM comment_chunks WHERE comment_id = ?", (comment_id,))
        conn.commit()
    return get(db_path, comment_id)


def delete(db_path: str, comment_id: int) -> bool:
    """Soft-delete (like `transcriptions`/`audio_downloads`) + removal from the index.

    Chunks are erased PHYSICALLY although the row stays: soft-delete exists for the sake of
    "Undo" in the UI, not so that a deleted comment keeps
    influencing RAG answers for another week. Restoring puts it back in the indexing queue.
    """
    # The anchor is read BEFORE deletion: afterwards `get` no longer sees the row, and
    # there would be nothing to recompute the graph from.
    existing = get(db_path, comment_id)
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE comments SET deleted_at = CURRENT_TIMESTAMP, embedded_at = NULL, "
            "analyzed_at = NULL WHERE id = ? AND deleted_at IS NULL", (comment_id,))
        conn.execute("DELETE FROM comment_chunks WHERE comment_id = ?", (comment_id,))
        # Tasks extracted by Claude from THIS comment go away with it.
        # Otherwise they would stay "open" in the dashboard and in the Monday digest
        # forever, and the source would be invisible: the owner deleted the comment as
        # erroneous, yet three tasks from it keep demanding attention, and
        # there is nowhere left to find out where they came from. We also clear `analyzed_at`, so
        # that after restore the comment can be analyzed anew.
        conn.execute("DELETE FROM action_items WHERE comment_id = ?", (comment_id,))
        conn.commit()
        ok = cur.rowcount > 0
    if ok and existing:
        _refresh_graph(db_path, existing)
    return ok


def restore(db_path: str, comment_id: int) -> bool:
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE comments SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
            (comment_id,))
        conn.commit()
        ok = cur.rowcount > 0
    if ok:
        _refresh_graph(db_path, get(db_path, comment_id))
    return ok


def _refresh_graph(db_path: str, comment: Optional[dict]) -> None:
    """Recompute the graph links for this comment's card.

    Here, not only in the blueprint: a deleted comment whose mentions stayed in the
    graph is a fact nobody asserts anymore, and finding such a thing later is
    impossible. The guarantee must hold at the service level so it need not be
    repeated at every call site (CLI, future MCP, tests).
    """
    if not comment:
        return
    try:
        tid = resolve_transcription(db_path, comment["target_type"],
                                    comment.get("target_ref", comment.get("target_id")))
        if tid is not None:
            link_entities(db_path, tid)
    except Exception:
        # The graph is a layer on top of the comment: the comment itself is already deleted/restored
        # correctly, and `relink` heals the mismatch. Swallow with a log.
        logger.warning("[comments] graph not refreshed after change of #%s",
                       comment.get("id"), exc_info=True)


def list_for(db_path: str, target_type: str, target_id,
             include_deleted: bool = False) -> list[dict]:
    """Comments of one card. Order: pinned first, then by time:
    on a card one must first see what the owner deliberately raised.

    For a recording session (string key) we sort within the pinned ones by
    `anchor_time` when present: during a call comments read naturally
    in the order of the conversation, not by the moment Enter was pressed."""
    where, params = _target_clause(target_type, target_id)
    sql = f"SELECT {_ROW_FIELDS} FROM comments WHERE 1=1{where}"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    sql += " ORDER BY pinned DESC, COALESCE(anchor_time, 1e18), created_at ASC"
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_to_dict(r) for r in rows]


def counts_for(db_path: str, target_type: str,
               target_ids: list) -> dict:
    """Counters for a LIST of cards in one query.

    A separate function, not a correlated subquery in every list SELECT:
    there are already four lists (Library, Media Library, tasks, entities), and the
    subquery would have to be duplicated in each, every time risking divergence
    in the definition of a "live" comment.

    Returns {target: {"n": how many in total, "corrections": how many corrections}}.
    """
    raw = list(target_ids or [])
    if not raw:
        return {}
    is_str = key_kind(target_type) == "str"
    col = "target_key" if is_str else "target_id"
    try:
        ids = [str(i) for i in raw] if is_str else [int(i) for i in raw]
    except (TypeError, ValueError):
        raise CommentError(tr("svc_pipes.comments.err.target_numeric", target_type=target_type))
    out: dict = {}
    # Cut into batches: SQLite has a limit on the number of parameters (999 in the
    # standard build), and "select all N by filter" in the Library
    # goes up to 3000 ids.
    CHUNK = 800
    with get_db_connection(db_path) as conn:
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            ph = ",".join("?" * len(part))
            # CORRECTIONS are counted separately: the badge on a card has a third state
            # ("something in the recording is refuted"), and without this number the frontend cannot
            # show it, and it is the most important thing a card says about itself.
            rows = conn.execute(
                f"SELECT {col} AS ref, COUNT(*) AS n, "
                f"SUM(CASE WHEN kind = 'correction' THEN 1 ELSE 0 END) AS fixes "
                f"FROM comments WHERE target_type = ? AND deleted_at IS NULL "
                f"AND {col} IN ({ph}) GROUP BY {col}",
                [target_type] + part).fetchall()
            for r in rows:
                key = r["ref"] if is_str else int(r["ref"])
                out[key] = {"n": int(r["n"]), "corrections": int(r["fixes"] or 0)}
    return out


def _feed_where(kind, target_type, since, search) -> tuple[str, list]:
    sql = " WHERE c.deleted_at IS NULL"
    params: list = []
    if kind:
        sql += " AND c.kind = ?"; params.append(kind)
    if target_type:
        sql += " AND c.target_type = ?"; params.append(target_type)
    if since:
        sql += " AND c.created_at >= ?"; params.append(since)
    if search:
        # LIKE, not FTS: the page browses the owner's OWN records, of which there are hundreds, and
        # a substring is more honest here than tokenization: the owner looks for a phrase they
        # wrote themselves, often from the middle of a word. A comparison cannot be wrapped in Python, so
        # we rely on LIKE with COLLATE NOCASE: it works for Latin, for
        # Cyrillic it stays case-sensitive (SQLite LOWER does not fold
        # Cyrillic, the same limitation as in the rest of the project).
        sql += " AND c.body LIKE ?"; params.append(f"%{search}%")
    return sql, params


def list_recent(db_path: str, limit: int = 50, kind: Optional[str] = None,
                target_type: Optional[str] = None,
                since: Optional[str] = None, search: Optional[str] = None,
                offset: int = 0) -> dict:
    """Comments feed for the overview page: rows + total + facets.

    Returns a `dict`, not a list: the page needs both the denominator (how many in total
    under the filter) and the breakdown by kind, otherwise "5 corrections" reads as
    the full picture although it is 5 out of 200 (the same trap that made the digest
    `coverage`).

    `target_name` is assembled with one LEFT JOIN onto transcripts and Media Library cards;
    other target types are shown as "type #id": pulling five more tables in here
    for rare cases costs more than it is worth.
    """
    where, params = _feed_where(kind, target_type, since, search)
    sel = ", ".join("c." + f for f in _ROW_FIELDS.split(", "))
    sql = (f"SELECT {sel}, t.source_name AS tx_name, t.title AS tx_title, "
           f"a.title AS audio_title "
           f"FROM comments c "
           f"LEFT JOIN transcriptions t ON c.target_type = 'transcription' "
           f"AND t.id = c.target_id "
           f"LEFT JOIN audio_downloads a ON c.target_type = 'audio_download' "
           f"AND a.id = c.target_id"
           # id as a tie-breaker is mandatory: `created_at` has one-second
           # resolution, and live comments are written in batches of several at a time:
           # without it, ties in time would come back in rowid order, i.e. REVERSED
           # relative to the promised "newest on top".
           f"{where} ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?")
    lim = max(1, min(int(limit), 500))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params + [lim, max(0, int(offset))]).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM comments c{where}", params).fetchone()[0]
        # Facets are counted WITHOUT the kind filter, otherwise after picking "corrections"
        # the user would see counters "corrections 5, rest 0" and could not
        # judge where to switch.
        fw, fp = _feed_where(None, target_type, since, search)
        by_kind = dict(conn.execute(
            f"SELECT c.kind, COUNT(*) FROM comments c{fw} GROUP BY c.kind", fp).fetchall())
    out = []
    for r in rows:
        d = _to_dict(r)
        tx_name = d.pop("tx_name", None)
        tx_title = d.pop("tx_title", None)
        audio_title = d.pop("audio_title", None)
        if d["target_type"] == "transcription" and (tx_name is not None or tx_title is not None):
            # editable-title-description-02: own title (title) overrides
            # source_name — display_name applies the same priority as GET.
            name = record_meta.display_name(
                {"id": d["target_id"], "title": tx_title, "source_name": tx_name}
            )
        else:
            name = tx_name or audio_title
        d["target_name"] = name or f"{d['target_type']} #{d['target_ref']}"
        out.append(d)
    return {"comments": out, "total": int(total), "by_kind": by_kind,
            "limit": lim, "offset": max(0, int(offset))}


def retarget(db_path: str, from_type: str, from_id,
             to_type: str, to_id) -> int:
    """Re-attach comments from one card to another.

    Needed for exactly one scenario (Wave 4), but that scenario is mandatory:
    during a call a comment is written to the RECORDING SESSION (no transcript yet),
    and after finalize it must live on the transcript, otherwise everything the owner
    dictated during the call would hang on an id not visible
    from any screen.
    """
    if to_type not in TARGETS or from_type not in TARGETS:
        raise CommentError(tr("svc_pipes.comments.err.unknown_target_to", to_type=to_type))
    new_id, new_key = _norm_target(to_type, to_id)
    where, params = _target_clause(from_type, from_id)
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE comments SET target_type = ?, target_id = ?, target_key = ?, "
            f"updated_at = CURRENT_TIMESTAMP WHERE 1=1{where}",
            [to_type, new_id, new_key] + params)
        conn.commit()
        n = cur.rowcount
    if n:
        logger.info("[comments] %s#%s → %s#%s: re-attached %d",
                    from_type, from_id, to_type, to_id, n)
        # The new target may have a graph (transcript): the old anchor no longer applies.
        _refresh_graph(db_path, {"id": None, "target_type": to_type,
                                 "target_id": to_id})
    return n


# ============================================================
# Indexer
# ============================================================

def _build_comment_chunks(body: str) -> list[dict]:
    """Cut the comment body into chunks.

    The vast majority of comments are one or two paragraphs, i.e. one chunk, and
    there is nothing to split. Long ones (a pasted quote of a letter, a detailed analysis)
    are cut by the same text chunker as transcripts
    (`embeddings._window_text`), so a long comment does not lose its tail and
    so the chunk shape matches the shape of the transcript chunks it
    competes to displace.
    """
    text = (body or "").strip()
    if not text:
        return []
    parts = embeddings._window_text(text)
    return [{"chunk_index": i, "text": p} for i, p in enumerate(parts) if p.strip()]


def index_comment(db_path: str, comment_id: int, force: bool = False) -> dict:
    """Cut + encode + save the comment. Idempotent via
    `embedded_at` + model + version (the same condition as for transcripts)."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, body, embedded_at, embedding_model, embedding_version, "
            "deleted_at FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if not row:
        return {"status": "not_found", "comment_id": comment_id}
    if row["deleted_at"]:
        return {"status": "skipped", "comment_id": comment_id, "reason": "deleted"}

    already = (bool(row["embedded_at"])
               and row["embedding_model"] == embeddings.EMBED_MODEL
               and row["embedding_version"] == embeddings.EMBED_VERSION)
    if already and not force:
        return {"status": "skipped", "comment_id": comment_id,
                "reason": "already_embedded"}

    chunks = _build_comment_chunks(row["body"])
    if not chunks:
        return {"status": "empty", "comment_id": comment_id}

    # Without a model the comment stays in FTS (triggers fill
    # comment_chunks_fts on INSERT), but WITHOUT a vector and WITHOUT the
    # embedded_at mark, i.e. the backfill later finds it as work. This is the
    # degradation without silent loss: lexical search works right away.
    available = embeddings.is_available()
    vecs = embeddings.embed_texts([c["text"] for c in chunks]) if available else None

    with get_db_connection(db_path) as conn:
        conn.execute("DELETE FROM comment_chunks WHERE comment_id = ?", (comment_id,))
        for i, ch in enumerate(chunks):
            blob = (vecs[i].astype(np.float32).tobytes()) if available else None
            conn.execute(
                "INSERT INTO comment_chunks (comment_id, chunk_index, text, "
                "embedding, token_estimate) VALUES (?, ?, ?, ?, ?)",
                (comment_id, ch["chunk_index"], ch["text"], blob, len(ch["text"]) // 4))
        if available:
            conn.execute(
                "UPDATE comments SET embedded_at = CURRENT_TIMESTAMP, "
                "embedding_model = ?, embedding_version = ? WHERE id = ?",
                (embeddings.EMBED_MODEL, embeddings.EMBED_VERSION, comment_id))
        conn.commit()

    status = "embedded" if available else "fts_only"
    logger.info("[comments] #%s indexed: %d chunk(s), %s",
                comment_id, len(chunks), status)
    return {"status": status, "comment_id": comment_id, "chunks": len(chunks)}


def pending_ids(db_path: str, limit: Optional[int] = None,
                force: bool = False) -> list[int]:
    """Comments that need (re)indexing.

    The condition MUST mirror the skip logic of `index_comment`, otherwise the backfill
    would forever return rows the indexer silently skips, and the work would
    never finish (the same trap described in `embeddings` for
    `list_unenriched_ids`).
    """
    sql = "SELECT id FROM comments WHERE deleted_at IS NULL"
    params: list = []
    if not force:
        sql += (" AND (embedded_at IS NULL OR embedding_model IS NOT ? "
                "OR embedding_version IS NOT ?)")
        params += [embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"; params.append(int(limit))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [int(r["id"]) for r in rows]


def reindex(db_path: str, force: bool = False, limit: Optional[int] = None,
            dry_run: bool = False) -> dict:
    """Backfill: index everything that is waiting. Idempotent."""
    ids = pending_ids(db_path, limit=limit, force=force)
    if dry_run:
        return {"pending": len(ids), "ids": ids[:50], "dry_run": True}
    stats = {"pending": len(ids), "embedded": 0, "fts_only": 0,
             "skipped": 0, "empty": 0}
    for cid in ids:
        res = index_comment(db_path, cid, force=force)
        stats[res["status"]] = stats.get(res["status"], 0) + 1
    return stats


def stats(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE deleted_at IS NULL").fetchone()[0]
        indexed = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE deleted_at IS NULL "
            "AND embedded_at IS NOT NULL").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM comment_chunks").fetchone()[0]
        by_kind = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM comments WHERE deleted_at IS NULL "
            "GROUP BY kind").fetchall())
        by_target = dict(conn.execute(
            "SELECT target_type, COUNT(*) FROM comments WHERE deleted_at IS NULL "
            "GROUP BY target_type").fetchall())
    return {"total": total, "indexed": indexed, "pending": total - indexed,
            "chunks": chunks, "by_kind": by_kind, "by_target": by_target}


# ============================================================
# Graph links (Wave 3, layer A — local, free)
# ============================================================

#: Provenance of a link in `meeting_entities.source`. The label is not a formality: a mention in
#: a comment means "named in a remark ABOUT the meeting", not "named AT the
#: meeting". These are different claims, and a slice by person that mixes them will lie
#: exactly as the digest once lied (memory/derived-claims-need-second-source).
ENTITY_SOURCE = "comment"


def resolve_transcription(db_path: str, target_type: str,
                          target_id: int) -> Optional[int]:
    """Which transcript to attach the comment's derivatives (graph, tasks) to.

    The graph and tasks in Recall are transcript-centric: `meeting_entities` and
    `action_items` both hold `transcription_id NOT NULL`. So a comment
    yields derivatives only where a transcript can be named:

    - `transcription` — itself;
    - `audio_download` — its transcript IF it already exists (a file without text is
      the main slice of the Media Library, and it is a normal state, not an error);
    - `action_item` — the transcript the task hangs on;
    - `recording_session` — via the Media Library card created by finalize
      (`audio_downloads.recording_session_id`), and then its transcript. While
      the call is going on the chain breaks at the first step, and that is right:
      the transcript physically does not exist yet.

    The remaining types (entity, speaker, direction, thread) have no anchor —
    we return None, and layer A simply does not work for them. Forcibly attaching them
    to "some" transcript would be worse than no link: the graph
    would get a fact nobody asserted.
    """
    if target_type == "transcription":
        return int(target_id)
    with get_db_connection(db_path) as conn:
        if target_type == "audio_download":
            row = conn.execute(
                "SELECT t.id FROM transcriptions t "
                "JOIN audio_downloads a ON a.id = ? "
                "WHERE t.file_path = a.file_path AND t.deleted_at IS NULL "
                "ORDER BY t.id DESC LIMIT 1", (int(target_id),)).fetchone()
            return int(row["id"]) if row else None
        if target_type == "action_item":
            row = conn.execute(
                "SELECT transcription_id FROM action_items WHERE id = ?",
                (int(target_id),)).fetchone()
            return int(row["transcription_id"]) if row else None
        if target_type == "recording_session":
            row = conn.execute(
                "SELECT t.id FROM transcriptions t "
                "JOIN audio_downloads a ON a.recording_session_id = ? "
                "WHERE t.file_path = a.file_path AND t.deleted_at IS NULL "
                "ORDER BY t.id DESC LIMIT 1", (str(target_id),)).fetchone()
            return int(row["id"]) if row else None
    return None


def link_entities(db_path: str, transcription_id: int) -> dict:
    """Recompute the graph links produced by this transcript's comments.

    We count ALL live comments of the recording at once, not just the one just added. The reason
    is the key: `meeting_entities` has PRIMARY KEY (transcription_id, entity_id) and
    no field for a comment, so "removing this comment's links"
    is impossible without touching neighbours. A full recompute per recording makes the
    operation idempotent by construction and equally correct after creating,
    editing and deleting a comment.

    Foreign links (a Claude card `source IS NULL`, TG `source='thread_match'`)
    are not touched; only our own `source='comment'` is erased.

    Local and free: it uses the same name dictionary and the same
    rule "written as a proper name" as Telegram ingest
    (`tg_entities.find_exact_mentions`). A shared rule is mandatory here:
    if they diverged, the two layers would start giving different links from the same text.

    Deliberately the exact matcher, not `find_mentions`: the measurement of story 04 saw only
    exact matches, and `TG_ENTITIES_MORPH_ENABLED` is a TG ingest flag; it must
    not quietly change what the comment layer writes (turning the flag off and removing
    already written morph links of comments is not possible here).
    """
    from app.services import tg_entities

    names = tg_entities.names_for(db_path)
    if not names:
        return {"transcription_id": transcription_id, "status": "no_entities"}

    with get_db_connection(db_path) as conn:
        bodies = [r["body"] for r in conn.execute(
            "SELECT body FROM comments WHERE deleted_at IS NULL "
            "AND target_type = 'transcription' AND target_id = ?",
            (transcription_id,)).fetchall()]

        hits: set[int] = set()
        for body in bodies:
            hits |= tg_entities.find_exact_mentions(body, names)

        conn.execute(
            "DELETE FROM meeting_entities WHERE source = ? AND transcription_id = ?",
            (ENTITY_SOURCE, transcription_id))
        written = 0
        for eid in hits:
            # OR IGNORE, not REPLACE: if the entity is already attached to this
            # recording by a Claude card, its link is a stronger claim ("it was said
            # at the meeting"), and it must not be overwritten with comment provenance.
            cur = conn.execute(
                "INSERT OR IGNORE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, source) "
                "VALUES (?, ?, 1, ?)", (transcription_id, eid, ENTITY_SOURCE))
            written += cur.rowcount
        conn.commit()

    return {"transcription_id": transcription_id, "status": "ok",
            "comments": len(bodies), "mentions": len(hits), "written": written}


def link_entities_for_comment(db_path: str, comment_id: int) -> dict:
    """Convenient wrapper for the ingest path: find the comment's anchor and recompute.

    Deliberately silent for comments without an anchor: that is not a failure but a normal state
    (a note on an entity or on a file without a transcript)."""
    c = get(db_path, comment_id)
    if not c:
        return {"comment_id": comment_id, "status": "not_found"}
    tid = resolve_transcription(db_path, c["target_type"], c["target_ref"])
    if tid is None:
        return {"comment_id": comment_id, "status": "no_anchor"}
    res = link_entities(db_path, tid)
    res["comment_id"] = comment_id
    return res


def relink_all(db_path: str, dry_run: bool = False) -> dict:
    """Pass over all transcripts that have comments. Idempotent."""
    with get_db_connection(db_path) as conn:
        tids = [int(r["target_id"]) for r in conn.execute(
            "SELECT DISTINCT target_id FROM comments WHERE deleted_at IS NULL "
            "AND target_type = 'transcription' ORDER BY target_id").fetchall()]
    if dry_run:
        return {"transcriptions": len(tids), "dry_run": True}
    written = 0
    for tid in tids:
        written += link_entities(db_path, tid).get("written", 0)
    return {"transcriptions": len(tids), "written": written}


# ============================================================
# Analysis of a comment via Claude (Wave 3, layer B — paid, on demand)
# ============================================================

ACTION_SOURCE = "comment"


def _persist_analysis(db_path: str, comment_id: int, transcription_id: int,
                      parsed: dict) -> dict:
    """Write the analysis result: entities, graph links, tasks."""
    from app.services import enrichment, tg_entities

    entity_ids: set[int] = set()
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        for etype, key in (("person", "people"), ("project", "projects"),
                           ("org", "orgs")):
            for item in (parsed.get(key) or []):
                if not isinstance(item, dict):
                    continue
                eid = enrichment._upsert_entity(
                    c, etype, item.get("name") or "",
                    role=item.get("role"), aliases=item.get("aliases") or [])
                if eid:
                    entity_ids.add(eid)
        conn.commit()

    # Local recompute BEFORE inserting what Claude found: it erases all
    # `source='comment'` links of the recording, so the reverse order would eat what
    # we just wrote.
    link_entities(db_path, transcription_id)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        for eid in entity_ids:
            c.execute(
                "INSERT OR IGNORE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, source) "
                "VALUES (?, ?, 1, ?)", (transcription_id, eid, ENTITY_SOURCE))

        # Own tasks are rewritten entirely, which is exactly why v38 introduced
        # comment_id. Without it a repeated analysis would either erase tasks of neighbouring
        # comments of the same recording or breed duplicates.
        c.execute("DELETE FROM action_items WHERE comment_id = ?", (comment_id,))
        written = 0
        for it in (parsed.get("action_items") or []):
            if not isinstance(it, dict):
                continue
            task = (it.get("task") or "").strip()
            if not task:
                continue
            c.execute(
                "INSERT INTO action_items (transcription_id, task, owner_name, "
                "due, due_date, status, source, comment_id) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                (transcription_id, task, it.get("owner"), it.get("due"),
                 it.get("due_date"), ACTION_SOURCE, comment_id))
            written += 1

        c.execute(
            "UPDATE comments SET analyzed_at = CURRENT_TIMESTAMP, analyzed_model = ? "
            "WHERE id = ?", (parsed.get("model") or "", comment_id))
        conn.commit()

    if entity_ids:
        with get_db_connection(db_path) as conn:
            enrichment._recompute_entity_aggregates(conn.cursor(), entity_ids)
            conn.commit()
        # New entities must start being caught by the local layer right away, not
        # in 5 minutes when the name cache expires.
        tg_entities.reset_names_cache()

    return {"entities": len(entity_ids), "action_items": written}


def analyze(db_path: str, comment_id: int, model: Optional[str] = None,
            force: bool = False, effort: str = "low") -> dict:
    """Extract tasks and entities from the comment via Claude.

    STARTED BY A BUTTON, NOT AUTOMATICALLY. Automatic analysis of every
    comment is a paid call per note, including "uh-huh", and the owner's decision
    (24.07.2026) keeps Claude on what is really worth it.
    The local layer (`link_entities`) works on its own and for free.

    Without an anchor (`resolve_transcription` → None) we exit BEFORE calling the model:
    there is nowhere to put the task (`action_items.transcription_id` NOT NULL), so
    paying for an extraction that will be written nowhere makes no sense.
    """
    from app.services import enrichment, text_polishing

    c = get(db_path, comment_id)
    if not c:
        return {"status": "not_found", "comment_id": comment_id}
    if c.get("analyzed_at") and not force:
        return {"status": "skipped", "comment_id": comment_id,
                "reason": "already_analyzed"}
    if not enrichment.is_available():
        return {"status": "unavailable", "comment_id": comment_id,
                "reason": "no_api_key"}

    tid = resolve_transcription(db_path, c["target_type"], c["target_ref"])
    if tid is None:
        return {"status": "no_anchor", "comment_id": comment_id,
                "reason": tr("svc_pipes.comments.reason.no_transcript", target_type=c['target_type'], target_id=c['target_id'])}

    try:
        parsed = text_polishing.extract_comment_items(
            c["body"], comment_date=str(c["created_at"])[:10],
            model=model, effort=effort)
    except Exception as exc:
        # Soft degradation, as in the card phase of enrichment: the comment stays on
        # the card and in search, the analysis can be repeated. We log: a silent failure
        # here would read as "Claude found nothing".
        logger.warning("[comments] analysis of #%s failed: %s", comment_id, exc)
        return {"status": "retry_needed", "comment_id": comment_id,
                "error": str(exc)}

    counts = _persist_analysis(db_path, comment_id, tid, parsed)
    logger.info("[comments] #%s analyzed: entities=%d, tasks=%d (in=%d out=%d)",
                comment_id, counts["entities"], counts["action_items"],
                parsed.get("input_tokens", 0), parsed.get("output_tokens", 0))
    return {
        "status": "analyzed", "comment_id": comment_id,
        "transcription_id": tid, "model": parsed.get("model", ""),
        "counts": counts,
        "input_tokens": parsed.get("input_tokens", 0),
        "output_tokens": parsed.get("output_tokens", 0),
        "cache_read_tokens": parsed.get("cache_read_tokens", 0),
    }


# ============================================================
# CLI
# ============================================================

def _default_db() -> str:
    return os.environ.get("DATABASE", os.path.join(os.getcwd(), "whisper_history.db"))


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(prog="python -m app.services.comments")
    p.add_argument("command", choices=["reindex", "relink", "analyze", "stats"])
    p.add_argument("id", nargs="?", type=int, default=None,
                   help="comment id (for analyze)")
    p.add_argument("--db", default=_default_db())
    p.add_argument("--force", action="store_true",
                   help="reindex/reanalyze even those already processed")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="only show how much work there is, write nothing")
    a = p.parse_args(argv)

    if a.command == "stats":
        res = stats(a.db)
    elif a.command == "relink":
        res = relink_all(a.db, dry_run=a.dry_run)
    elif a.command == "analyze":
        if a.id is None:
            p.error("analyze needs a comment id (analysis is paid — "
                    "a bulk run is deliberately absent here)")
        res = analyze(a.db, a.id, force=a.force)
    else:
        res = reindex(a.db, force=a.force, limit=a.limit, dry_run=a.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
