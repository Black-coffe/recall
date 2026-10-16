"""Literal and regex search over archive chunks (grep-explainability, story 01,
repair -- story 07 / Plan deltas D1).

**Problem:** `chunks_fts` (FTS5, `tokenize='unicode61 remove_diacritics 1'`)
tokenizes the text -- it splits `ID-4471`, `1 200 000`, `@nick` into separate
tokens and does not find them as a string. The reranker on top of vector/FTS
search additionally hides exact identifiers, amounts and names behind
semantically close but inexact matches. `grep()` is a separate pass over
`chunks`, without embeddings and without ranking, that answers exactly "where
in the archive does this string (or this regex) occur".

**Contract** (from `docs/specs/grep-explainability/plan.md` §C1 -- the source
of truth, do not change silently): the `grep()` signature and the shape of each
`matches` item are fixed, because story 04 (the MCP tool) is built on top of
them unchanged. Story 07 added compatible top-level response fields
(`match_total`, `truncated_reason`, `chunk_boundary_caveat`,
`arbitrary_scan_caveat`) -- the old keys are neither renamed nor removed.

**Cyrillic (Plan deltas D1, finding 1 -- critical).** Case is folded
EXCLUSIVELY in Python (`str.casefold`), never in SQL: SQLite `lower()` folds
only ASCII, so `instr(lower(text), lower(?))` silently returns 0 on Cyrillic.
Literal mode casefolds the string and the needle in Python; regex mode uses
`re.IGNORECASE` (for str patterns in Python it also works on Unicode case
tables) -- both must mean the same thing under `ignore_case=True`.

**Scan (Plan deltas D1, findings 2-3).** No `ORDER BY` in the SQL query:
sorting by an unindexed expression would force SQLite to run the filter over
the whole join before `LIMIT`. The cursor yields rows in natural order, each
is checked in Python; `max_scan` counts exactly the rows LOOKED AT and stops
the scan itself (not only the output). Found matches are sorted in Python
(`meeting_date DESC, chunk_index`). In addition -- a wall-clock deadline
checked BETWEEN rows: it protects the MCP stdio process from a regex pattern
with catastrophic backtracking. It does not save from a single row whose own
check already exceeds the deadline -- only from accumulation over many rows.

**Chunk boundary (Plan deltas D1, finding 8) -- not cured.** The search goes
over `chunks`, not over the full transcript text: a string cut in half by a
chunking seam honestly will not be found. End-to-end search over
`transcriptions.transcript_text` is a separate search surface, left on the
roadmap.

**Does not import** `torch`, `numpy` embeddings, `app.services.retrieval` -- the
module must load in the MCP stdio process without heavy models (memo
`mcp-stdio-no-heavy-models`).
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from app.db.connection import get_db_connection
from app.services import categories

# COALESCE -- Telegram messages often have no explicit meeting_date; then the
# meeting date is absent and the date of the archive entry serves as the date
# (as in retrieval.py).
_MEETING_DATE_EXPR = "COALESCE(t.meeting_date, substr(t.created_at, 1, 10))"

_EXCERPT_RADIUS = 80  # characters on each side of the first match

# Wall-clock deadline for the whole scan, checked BETWEEN rows (not inside the
# execution of a single regex call). Tests can patch the constant via
# monkeypatch so as not to wait real seconds on catastrophic backtracking.
_DEFAULT_DEADLINE_SECONDS = 5.0

_CHUNK_BOUNDARY_CAVEAT = (
    "The search goes over chunks, not over the full transcript text: a string "
    "cut in half by a chunking seam will not be found here (end-to-end search "
    "over transcriptions.transcript_text is a separate surface, not built yet)."
)

# Shown ONLY when the scan stopped early (max_scan/deadline), not when
# truncated_reason == "limit". Without ORDER BY the SQLite cursor yields rows in
# natural order, so an early stop looks at an ARBITRARY prefix, not at the
# newest max_scan rows: after sorting, the result is the newest AMONG THOSE
# LOOKED AT, not the newest in the archive (Plan deltas D2, finding 3).
_ARBITRARY_SCAN_CAVEAT = (
    "The scan stopped early (max_scan or deadline) without reaching the end of "
    "the table: without ORDER BY the rows looked at are an arbitrary prefix of "
    "the cursor's natural order, NOT guaranteed to be the newest entries. matches "
    "are sorted by meeting_date only among those that were looked at."
)


def _compile_pattern(pattern: str, ignore_case: bool) -> re.Pattern:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex '{pattern}': {exc}") from exc


def _make_excerpt(text: str, start: int, end: int) -> str:
    lo = max(0, start - _EXCERPT_RADIUS)
    hi = min(len(text), end + _EXCERPT_RADIUS)
    excerpt = text[lo:hi]
    if lo > 0:
        excerpt = "…" + excerpt
    if hi < len(text):
        excerpt = excerpt + "…"
    return excerpt


def _find_all(haystack: str, needle: str) -> list[int]:
    """Indices of all NON-overlapping occurrences of `needle` in `haystack` (like `str.count`)."""
    positions: list[int] = []
    start = 0
    step = len(needle)
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + step
    return positions


def _match_row(
    text: str,
    *,
    regex: bool,
    compiled: re.Pattern | None,
    needle: str,
    ignore_case: bool,
) -> tuple[int, str] | None:
    """Checks ONE already-read row for a match. Called exactly once per
    row looked at -- this very call is what the `max_scan` test counts,
    proving that the ceiling limits the work, not only the output.

    Returns `(match_count, excerpt)` around the ACTUAL match, or `None`.
    """
    if regex:
        found = list(compiled.finditer(text))
        if not found:
            return None
        return len(found), _make_excerpt(text, found[0].start(), found[0].end())

    haystack = text.casefold() if ignore_case else text
    positions = _find_all(haystack, needle)
    if not positions:
        return None
    start = positions[0]
    # the excerpt window is by the actual match length (of the casefolded
    # needle), not by the raw pattern length (Plan deltas / finding 6)
    return len(positions), _make_excerpt(text, start, start + len(needle))


def _get_context(conn: sqlite3.Connection, transcription_id: int, chunk_index: int, context: int) -> tuple[list[dict], list[dict]]:
    if context <= 0:
        return [], []
    before_rows = conn.execute(
        "SELECT chunk_index, text FROM chunks "
        "WHERE transcription_id = ? AND chunk_index BETWEEN ? AND ? "
        "ORDER BY chunk_index",
        (transcription_id, chunk_index - context, chunk_index - 1),
    ).fetchall()
    after_rows = conn.execute(
        "SELECT chunk_index, text FROM chunks "
        "WHERE transcription_id = ? AND chunk_index BETWEEN ? AND ? "
        "ORDER BY chunk_index",
        (transcription_id, chunk_index + 1, chunk_index + context),
    ).fetchall()
    before = [{"chunk_index": r["chunk_index"], "text": r["text"]} for r in before_rows]
    after = [{"chunk_index": r["chunk_index"], "text": r["text"]} for r in after_rows]
    return before, after


def grep(
    db_path: str,
    pattern: str,
    *,
    regex: bool = False,
    ignore_case: bool = True,
    context: int = 1,
    limit: int = 20,
    source_type: str | None = None,
    transcription_id: int | None = None,
    days: int | None = None,
    category_id: int | None = None,
    tags: list[str] | None = None,
    max_scan: int = 200_000,
) -> dict[str, Any]:
    """Literal or regex search over `chunks`, with metadata and ±context neighbours.

    Result order is `meeting_date DESC, chunk_index` (not relevance:
    there is no vector, FTS ranking or reranker here), computed in
    Python over the already collected matches (the SQL query deliberately has
    no `ORDER BY` -- see the module docstring). `truncated=True` if `max_scan`
    was hit (as many rows were looked at as allowed, and unviewed ones may
    remain), the wall-clock deadline, or `limit` (more were found among those
    looked at than are shown) -- `truncated_reason` says which one it is,
    and `match_total` says how many were found among those LOOKED AT (not in
    the whole archive, if the scan stopped early).

    Rows without a date (`meeting_date is None`) are sorted LAST, after all
    dated ones -- contract C1 declares `meeting_date: str | None`, and comparing
    None with a str raises `TypeError`, so they are pulled out of the date
    sort and appended at the end without changing their relative order.

    An early stop (`truncated_reason` is `"max_scan"` or `"deadline"`)
    looks at an ARBITRARY prefix of the cursor's natural order, not "the newest
    max_scan rows" -- the SQL deliberately has no `ORDER BY` (see the module docstring).
    In that case the response carries `arbitrary_scan_caveat` with an explanation; when
    the stop happened only because of `limit` (the whole scan finished), this field is `None`.

    Case (`ignore_case=True`) is folded in Python (`str.casefold`) for
    both modes -- NEVER in SQL (`SQLite lower()` does not fold Cyrillic).

    `category_id` -- id of a direction or project (`list_categories`): a direction
    expands to itself and all its projects, a project only to itself.
    `tags` -- tag names (OR); together with `category_id` -- intersection (AND). Both
    are filtered in SQL BEFORE scanning, via `app.services.categories.filter_sql/
    tags_sql` (C6, ADR-023 §C) -- the single source of this subquery.

    An empty or whitespace-only `pattern` is an error (`ValueError`), not the whole
    archive.

    Chunk boundary: a string cut by a chunking seam will not be found here (see
    the module docstring and `chunk_boundary_caveat` in the response).
    """
    if not pattern or not pattern.strip():
        raise ValueError(
            "An empty or whitespace-only pattern is not accepted -- otherwise grep() "
            "would silently return the entire archive."
        )

    compiled = _compile_pattern(pattern, ignore_case) if regex else None
    # for literal mode the needle is casefolded once here, not in SQL
    needle = pattern.casefold() if (not regex and ignore_case) else pattern

    where = ["t.deleted_at IS NULL"]
    params: list[Any] = []

    if source_type is not None:
        where.append("t.source_type = ?")
        params.append(source_type)
    if transcription_id is not None:
        where.append("ch.transcription_id = ?")
        params.append(transcription_id)
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        where.append(f"{_MEETING_DATE_EXPR} >= ?")
        params.append(cutoff)

    where_tail = ""
    frag, p = categories.filter_sql("t.category_id", category_id)
    where_tail += frag; params += p
    frag, p = categories.tags_sql("ch.transcription_id", tags or [])
    where_tail += frag; params += p

    # Deliberately WITHOUT ORDER BY and without a text predicate in SQL (Plan deltas D1):
    # both the text filter (regex/casefold) and the final sorting are in Python
    # below, so that max_scan limits the scan work itself.
    sql = (
        "SELECT ch.id AS chunk_id, ch.transcription_id, ch.chunk_index, ch.text, "
        "ch.speaker, ch.start_time, t.source_type, t.source_name, "
        f"t.tg_chat_title, t.tg_link, {_MEETING_DATE_EXPR} AS meeting_date "
        "FROM chunks ch JOIN transcriptions t ON t.id = ch.transcription_id "
        f"WHERE {' AND '.join(where)}{where_tail}"
    )

    deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
    scanned = 0
    hit_scan_ceiling = False
    hit_deadline = False
    found: list[tuple[sqlite3.Row, int, str]] = []

    with get_db_connection(db_path) as conn:
        cur = conn.execute(sql, params)
        for row in cur:
            if scanned >= max_scan:
                hit_scan_ceiling = True
                break
            if time.monotonic() >= deadline:
                hit_deadline = True
                break
            scanned += 1
            text = row["text"] or ""
            result = _match_row(text, regex=regex, compiled=compiled, needle=needle, ignore_case=ignore_case)
            if result is not None:
                match_count, excerpt = result
                found.append((row, match_count, excerpt))

        match_total = len(found)

        # meeting_date DESC, chunk_index ASC -- a stable double sort
        # reproduces the former SQL ORDER BY removed from the scan above. C1 declares
        # meeting_date as str | None (Telegram rows with no meeting date and no
        # created_at are not excluded either) -- Python cannot compare None with
        # str, so NULL rows are sorted separately and go to the end, without changing
        # the relative order of the dated ones (Plan deltas D2, finding 1).
        found.sort(key=lambda item: item[0]["chunk_index"])
        dated = [item for item in found if item[0]["meeting_date"] is not None]
        undated = [item for item in found if item[0]["meeting_date"] is None]
        dated.sort(key=lambda item: item[0]["meeting_date"], reverse=True)
        found = dated + undated

        hit_limit_ceiling = match_total > limit
        selected = found[:limit]

        matches = []
        for row, match_count, excerpt in selected:
            context_before, context_after = _get_context(
                conn, row["transcription_id"], row["chunk_index"], context
            )
            matches.append({
                "chunk_id": row["chunk_id"],
                "transcription_id": row["transcription_id"],
                "chunk_index": row["chunk_index"],
                "source_type": row["source_type"],
                "source_name": row["source_name"],
                "meeting_date": row["meeting_date"],
                "speaker": row["speaker"],
                "start_time": row["start_time"],
                "tg_chat_title": row["tg_chat_title"],
                "tg_link": row["tg_link"],
                "match_count": match_count,
                "excerpt": excerpt,
                "context_before": context_before,
                "context_after": context_after,
            })

    if hit_deadline:
        truncated_reason = "deadline"
    elif hit_scan_ceiling:
        truncated_reason = "max_scan"
    elif hit_limit_ceiling:
        truncated_reason = "limit"
    else:
        truncated_reason = None

    return {
        "pattern": pattern,
        "regex": regex,
        "count": len(matches),
        "match_total": match_total,
        "scanned": scanned,
        "truncated": truncated_reason is not None,
        "truncated_reason": truncated_reason,
        "matches": matches,
        "chunk_boundary_caveat": _CHUNK_BOUNDARY_CAVEAT,
        "arbitrary_scan_caveat": (
            _ARBITRARY_SCAN_CAVEAT if truncated_reason in ("max_scan", "deadline") else None
        ),
    }
