#!/usr/bin/env python
"""Golden-set miner from REAL MCP queries (eval-gate, C1/C8; production-rag-wave-b-01).

Reads `logs/mcp_calls.log` (`RECALL_MCP_DEBUG_LOG=1`, `mcp_server.py::_CallLogMiddleware` -
format `HH:MM:SS ->   START <tool> args={json}`), extracts `query` from `search_archive` and
`question` from `ask_archive`, deduplicates with normalization and orders by frequency
(questions that repeat more often come first; frequency > 1 goes into `notes`), and for
each unique query computes the top-5 `retrieval.search` (on a DB SNAPSHOT) -> source candidates
and the majority slice (`calls`/`tg`/`docs`, C8). Candidates are a HINT, not the truth:
every new row comes out as `status="unlabeled"` with empty `expected_*`; only a human
moves it to `labeled` (A2).

`args` in the log is truncated to 600 characters (mcp_server.py), so long `question`/`query`
get cut in the middle of a string value. The parser first tries a plain `json.loads`,
then "closing" the truncated string (`+'"}'`/`+'}'`), and only as a last resort
a regex from the field to the end of the line (without requiring a closing quote).

CLI:
    python -m evals.build_golden --db evals/snapshots/x.db
    python -m evals.build_golden --db evals/snapshots/x.db --merge
    python -m evals.build_golden --db evals/snapshots/x.db --stats
    python -m evals.build_golden --db evals/snapshots/x.db --from-ask-log --stats
    python -m evals.build_golden --db evals/snapshots/x.db --out evals/golden_set.local.json \
        --merge --from-ask-log --limit 30 --stats

`--limit` cuts the freshly deduplicated list of NEW questions FROM THE LOG from the top (already
ordered by frequency); without it `retrieval.search` on hundreds of unique
questions takes hours (brute-force over the whole snapshot); the skipped ones remain
candidates for the next run (`--merge` does not see them as "known" until they are added).
Questions from `--from-ask-log` are not cut by the limit; otherwise hundreds of log rows eat
it entirely and a run "from both sources" silently yields just one.

`--from-ask-log` adds a second source of questions, the `ask_log` table (migration v41,
UI + MCP): it has the owner's rating thumbs-up/thumbs-down, so thumbs-down questions become candidates
first, and the candidate sources are the records the model actually cited.

`--out` detects the format by extension: `.jsonl` is the modern one (one object per
line), anything else (e.g. `golden_set.local.json`) is legacy
`{"description": ..., "items": [...]}`. In legacy mode existing items
are read and written RAW (without `golden_io` normalization), so that with `--merge`
they do not acquire new keys and stay the same as on disk.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Optional

# Allows running both as `python evals/build_golden.py` and as
# `python -m evals.build_golden` without installing the package (the same trick as
# in evals/run_eval.py and evals/graph_links.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.golden_io import GoldenSetError, read_jsonl  # noqa: E402
from evals.graph_links import is_live_db  # noqa: E402

#: A2: labelling target per slice (NOT a code acceptance criterion - build_golden only
#: counts progress toward it in `--stats`).
SLICE_TARGET = 50
#: A nearer fill target for the whole set (production-rag-wave-b-01), separate
#: from SLICE_TARGET, which remains the long-term per-slice target.
TOTAL_TARGET = 30
SLICES = ("calls", "tg", "docs")

_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2} →   START (\S+) args=(.*)$")
_TOOL_FIELD = {"search_archive": "query", "ask_archive": "question"}
_SOURCE_TO_SLICE = {"telegram": "tg", "document": "docs"}


# ============================================================
# Log mining (C8)
# ============================================================

def _parse_args_json(argstr: str) -> Optional[dict]:
    """Best-effort parse of the JSON tail of a line, accounting for the 600-character truncation."""
    for candidate in (argstr, argstr + '"}', argstr + "}"):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_field(argstr: str, field: str) -> Optional[str]:
    parsed = _parse_args_json(argstr)
    if parsed is not None:
        val = parsed.get(field)
        return val.strip() if isinstance(val, str) and val.strip() else None
    m = re.search(rf'"{field}"\s*:\s*"((?:\\.|[^"\\])*)', argstr)
    if not m:
        return None
    raw = (m.group(1).replace('\\"', '"').replace("\\n", " ")
           .replace("\\t", " ").replace("\\\\", "\\")).strip()
    return raw or None


def _category_id_from_args(argstr: str) -> Optional[int]:
    parsed = _parse_args_json(argstr)
    cid = parsed.get("category_id") if parsed else None
    return int(cid) if isinstance(cid, int) else None


def mine_ask_log(db_path: str) -> list[dict]:
    """Questions from the snapshot's `ask_log` table (migration v41), thumbs-down FIRST.

    The MCP log knows only the question text (and that truncated to 600 characters); here
    there is the channel, the scope, the cited sources and, most importantly, the owner's verdict. The order
    is not cosmetic: the owner labels about ten questions a week, and the first in the queue
    should be those where the system has already measurably erred (thumbs-down), not random ones.

    Dedup is normalized, as in `mine_queries`; within the same question
    the first occurrence in this order wins.
    """
    out: list[dict] = []
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT question, scope_json, source_ids_json, rating, note FROM ask_log "
                # -1 -> 0, the rest -> 1: thumbs-down first, then thumbs-up and unrated by time
                "ORDER BY CASE WHEN rating = -1 THEN 0 ELSE 1 END, id"
            ).fetchall()
        except sqlite3.Error as exc:
            # the snapshot is older than migration v41: this is not a call error, there is simply
            # nothing to change there (without --from-ask-log we would not have come in here).
            print(f"[build_golden] ask_log unavailable in {db_path}: {exc}", file=sys.stderr)
            return out
    finally:
        conn.close()

    seen: set[str] = set()
    for row in rows:
        question = (row["question"] or "").strip()
        key = _norm_query(question)
        if not key or key in seen:
            continue
        seen.add(key)
        scope = {}
        with contextlib.suppress(ValueError, TypeError):
            scope = json.loads(row["scope_json"] or "{}") or {}
        source_ids = []
        with contextlib.suppress(ValueError, TypeError):
            source_ids = json.loads(row["source_ids_json"] or "[]") or []
        cid = scope.get("category_id")
        notes = []
        if row["rating"] in (1, -1):
            notes.append("owner 👎" if row["rating"] == -1 else "owner 👍")
        if (row["note"] or "").strip():
            notes.append((row["note"] or "").strip())
        out.append({"question": question, "tool": "ask_archive",
                    "category_id": cid if isinstance(cid, int) else None,
                    "source": "ask_log",
                    "source_ids": [i for i in source_ids if isinstance(i, int)],
                    "notes": " · ".join(notes)})
    return out


def candidates_from_ids(db_path: str, source_ids: list[int]) -> tuple[list[dict], str]:
    """Candidates from the cited sources of an `ask_log` row (without `retrieval.search`).

    This is the same HINT as the top-5 of search: what the model cited does not
    prove correctness; only a human proves it, by moving the item to `labeled`.
    But the hint is stronger: these sources have already passed through an answer."""
    if not source_ids:
        return [], "calls"
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(source_ids))
        rows = conn.execute(
            f"SELECT id, source_name, source_type FROM transcriptions WHERE id IN ({placeholders})",
            source_ids).fetchall()
    finally:
        conn.close()
    by_id = {r["id"]: r for r in rows}
    chunks = []
    for tid in source_ids:
        r = by_id.get(tid)
        chunks.append({"transcription_id": tid,
                       "source_name": r["source_name"] if r else None,
                       "source_type": r["source_type"] if r else None})
    return chunks, _slice_for_chunks(chunks)


def _norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def mine_queries(log_path: str) -> list[dict]:
    """Unique queries from the log, ordered by frequency (the most repeated
    first; a tie is broken by order of first appearance, dedup is normalized).

    Returns ``[{"question", "tool", "category_id", "notes"}, ...]``; these are still
    NOT golden items, only raw queries (candidates/slice are computed separately,
    only for those that will really go into the output file, so as not to pay for retrieval
    on duplicates). Frequency, how many times the same query (normalized)
    occurred in the log, is written to ``notes`` when it is > 1 (the gate
    ignores this field, it is a hint to the human who labels)."""
    counts: Counter[str] = Counter()
    first_seen: dict[str, dict] = {}
    order: list[str] = []
    if not os.path.exists(log_path):
        return []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            tool, argstr = m.group(1), m.group(2)
            field = _TOOL_FIELD.get(tool)
            if not field:
                continue
            text = _extract_field(argstr, field)
            if not text:
                continue
            key = _norm_query(text)
            if not key:
                continue
            counts[key] += 1
            if key not in first_seen:
                first_seen[key] = {"question": text, "tool": tool,
                                    "category_id": _category_id_from_args(argstr)}
                order.append(key)

    # sorted() is stable: at equal frequency the order of first appearance stays.
    ordered_keys = sorted(order, key=lambda k: -counts[k])
    out: list[dict] = []
    for key in ordered_keys:
        item = dict(first_seen[key])
        freq = counts[key]
        if freq > 1:
            item["notes"] = f"MCP-log frequency: {freq}×"
        out.append(item)
    return out


# ============================================================
# Candidates + slice (retrieval.search on a SNAPSHOT, ADR-003)
# ============================================================

@contextlib.contextmanager
def _scratch_copy(db_path: str):
    """A temporary copy of the snapshot: `retrieval.search` goes through `get_db_connection`
    (`PRAGMA journal_mode=WAL`), which mutates the file merely by opening it (ADR-003:
    the measuring tooling does not mutate the snapshot it measures). The copy takes the side
    effect upon itself, the snapshot stays byte-for-byte unchanged. A deliberate copy of the
    pattern of `evals/graph_links.py::_scratch_copy` (we do not import a private name
    from a neighbouring module: the same reason `commitments._normalize_name`
    deliberately duplicates 6 lines instead of importing from `enrichment`)."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # copyfile stays INSIDE the try; otherwise a copy failure (disk full,
        # permissions) leaves an empty mkstemp stub on disk forever (finding 20).
        shutil.copyfile(db_path, tmp_path)
        yield tmp_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path + suffix
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError as exc:
                    # do not swallow silently (CLAUDE.md: no bare except without a log);
                    # this is only temp-file cleanup, not a critical error.
                    print(f"[build_golden] failed to remove the temporary file "
                          f"{candidate}: {exc}", file=sys.stderr)


def _slice_for_chunks(chunks: list[dict]) -> str:
    """Majority slice by the candidates' `source_type`. `comment` (comment
    provenance, `app/services/retrieval.py`) does not vote; otherwise a question
    whose answer was taken mostly from comments wrongly settles in `calls`
    (finding 22)."""
    votes = [ch for ch in chunks if ch.get("source_type") != "comment"]
    if not votes:
        return "calls"
    counts = Counter(_SOURCE_TO_SLICE.get(ch.get("source_type"), "calls") for ch in votes)
    return counts.most_common(1)[0][0]


def candidates_for(db_path: str, question: str, top_k: int = 5) -> tuple[list[dict], str]:
    """Top-``top_k`` `retrieval.search` -> candidates (id/name/source type) + slice."""
    from app.services import retrieval

    res = retrieval.search(db_path, question, top_k=top_k)
    chunks = res.get("chunks") or []
    candidates = [{"transcription_id": ch.get("transcription_id"),
                   "source_name": ch.get("source_name"),
                   "source_type": ch.get("source_type")} for ch in chunks]
    return candidates, _slice_for_chunks(chunks)


# ============================================================
# Reading/writing --out: JSONL (modern format, D13) OR legacy
# `{"description": ..., "items": [...]}` (the old `golden_set.local.json`,
# `evals/golden_io.py` recognizes the same split by file extension).
# The legacy format is read RAW (without `golden_io` normalization), otherwise
# the 15 existing items acquire new keys (`slice`/`source`/`candidates`)
# and stop being the same as on disk (Non-goals: do not touch them).
# ============================================================


def _read_existing(path: str) -> tuple[list[dict], Optional[str]]:
    """Read the existing `--out` for merge/duplicate checks.

    Returns ``(items, description)``; `description` only for legacy `.json`
    (``None`` for `.jsonl` and for a legacy file without this field)."""
    if path.endswith(".jsonl"):
        return read_jsonl(path), None
    p = Path(path)
    if not p.exists():
        return [], None
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldenSetError(f"golden-set {path}: could not read: {exc}") from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise GoldenSetError(f"golden-set {path}: invalid JSON: {exc}") from exc
    if isinstance(data, dict):
        items, description = data.get("items"), data.get("description")
    elif isinstance(data, list):
        items, description = data, None
    else:
        raise GoldenSetError(f"golden-set {path}: expected a list or {{'items': [...]}}")
    if not isinstance(items, list):
        raise GoldenSetError(f"golden-set {path}: 'items' must be a list")
    return items, description


def _write_golden(path: str, items: list[dict], description: Optional[str]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if path.endswith(".jsonl"):
        with open(path, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return
    payload: dict = {}
    if description is not None:
        payload["description"] = description
    payload["items"] = items
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


_ID_RE = re.compile(r"^mined-(\d+)$")


def _next_start_idx(existing: list[dict]) -> int:
    """The next free numeric tail of `mined-NNNN` among `existing`.

    `len(existing) + 1` duplicates an id if a row in the middle was deleted by hand;
    the new item with the same number would silently merge with the old one (finding 16)."""
    max_idx = 0
    for it in existing:
        m = _ID_RE.match(str(it.get("id") or ""))
        if m:
            max_idx = max(max_idx, int(m.group(1)))
    return max_idx + 1


def _make_item(idx: int, q: dict, candidates: list[dict], slice_: str) -> dict:
    return {
        "id": f"mined-{idx:04d}",
        "question": q["question"],
        "slice": slice_,
        "category_id": q.get("category_id"),
        "expected_transcription_ids": [],
        "expected_source_name_contains": [],
        "expected_facts": [],
        "notes": q.get("notes") or "",
        "source": q.get("source") or "mcp_log",
        "status": "unlabeled",
        "candidates": candidates,
    }


def print_stats(items: list[dict]) -> None:
    table = {s: Counter() for s in SLICES}
    total_labeled = 0
    for it in items:
        s = it.get("slice") if it.get("slice") in SLICES else "calls"
        # An item without an explicit "status" is a legacy record (the old golden_set.local.json,
        # `evals/golden_io.py::_read_legacy` treats such as "labeled" by default),
        # NOT a freshly mined unlabeled one; otherwise the 15 already labeled items are counted
        # as unfinished work in this statistics.
        status = it.get("status") or "labeled"
        table[s][status] += 1
        if status == "labeled":
            total_labeled += 1
    print(f"{'slice':<8}{'labeled':>10}{'unlabeled':>12}{'negative':>10}{'target':>8}{'deficit':>10}")
    for s in SLICES:
        c = table[s]
        labeled = c.get("labeled", 0)
        deficit = max(0, SLICE_TARGET - labeled)
        print(f"{s:<8}{labeled:>10}{c.get('unlabeled', 0):>12}{c.get('negative', 0):>10}"
              f"{SLICE_TARGET:>8}{deficit:>10}")
    print(f"labeled {total_labeled} / target {TOTAL_TARGET}")


# ============================================================
# CLI
# ============================================================

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mcp-log", default=str(_PROJECT_ROOT / "logs" / "mcp_calls.log"),
                         help="Path to the MCP call log (default: logs/mcp_calls.log)")
    parser.add_argument("--db", default="whisper_history.db", help="Path to the DB SNAPSHOT for retrieval.search")
    parser.add_argument("--out", default="evals/golden_candidates.local.jsonl",
                         help="Where to write the JSONL (default: evals/golden_candidates.local.jsonl)")
    parser.add_argument("--merge", action="store_true",
                         help="Keep labeled/negative items from the existing --out, add only new unlabeled ones "
                              "(without this flag --out is OVERWRITTEN with fresh output from the log)")
    parser.add_argument("--yes-overwrite-labeled", action="store_true",
                         help="Deliberately allow overwriting --out without --merge when it already holds "
                              "labeled (labeled/negative) items. Without --merge and without this flag "
                              "such an --out is NOT overwritten: manual labelling is irreplaceable.")
    parser.add_argument("--from-ask-log", action="store_true",
                         help="Add questions from the snapshot's ask_log table (UI+MCP, with the "
                              "owner's rating): thumbs-down first, candidates are the cited sources")
    parser.add_argument("--stats", action="store_true", help="Print the slice x status table and the deficit to the target")
    parser.add_argument("--top-k", type=int, default=5, help="How many candidates per query (default: 5)")
    parser.add_argument("--limit", type=int, default=None,
                         help="How many NEW (after dedup) questions FROM THE MCP LOG to really run "
                              "through retrieval.search (default: unlimited). `retrieval.search` "
                              "is brute-force over the whole snapshot, so on hundreds of unique questions from "
                              "the log one run takes hours; the wave plan asks to mine "
                              "'>=30 candidates' (not the whole log), so --limit cuts the frequency-ordered "
                              "log list from the top: the most frequent questions stay. "
                              "Questions from --from-ask-log are NOT cut by the limit: there are about ten of them and they come with "
                              "the owner's rating.")
    parser.add_argument("--yes-live", action="store_true",
                         help="Allow opening the file that matches Config.DATABASE")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[build_golden] DB not found: {args.db}", file=sys.stderr)
        return 2
    if is_live_db(args.db) and not args.yes_live:
        print(f"[build_golden] Refused: {args.db!r} is the live DB (Config.DATABASE). "
              "This tooling works on a SNAPSHOT. If this is a deliberate choice, add --yes-live.",
              file=sys.stderr)
        return 2

    # We read the existing --out ONCE, regardless of --merge: it is needed both for
    # merging and for counting the labeled ones before refusing an overwrite.
    # The format (JSONL or legacy `{"description":..., "items":[...]}`) is determined by
    # the file extension (`_read_existing`); invalid content gives exit 2 with
    # a message, not a traceback (D3).
    existing: list[dict] = []
    description: Optional[str] = None
    if os.path.exists(args.out):
        try:
            prior, description = _read_existing(args.out)
        except GoldenSetError as exc:
            print(f"[build_golden] {args.out}: {exc}", file=sys.stderr)
            return 2
        if args.merge:
            existing = prior
        else:
            # Legacy items often have no explicit "status" (golden_io by
            # default treats them as "labeled": this is already HAND-labeled data,
            # not freshly mined), so the default here is the same, otherwise the
            # overwrite protection silently fails on exactly the most important rows.
            lost = sum(1 for it in prior if (it.get("status") or "labeled") in ("labeled", "negative"))
            if lost and not args.yes_overwrite_labeled:
                print(f"[build_golden] Refused: {args.out} holds {lost} labeled "
                      "(labeled/negative) items and would be overwritten without --merge. "
                      "Add --merge to keep them, or --yes-overwrite-labeled "
                      "to deliberately overwrite and lose them.", file=sys.stderr)
                return 2
            if lost:
                print(f"[build_golden] WARNING: {args.out} is overwritten with "
                      f"--yes-overwrite-labeled - {lost} labeled items will be lost.",
                      file=sys.stderr)

    mined = mine_queries(args.mcp_log)
    if args.from_ask_log:
        # We read from the snapshot COPY too: `sqlite3.connect` on a WAL file leaves
        # -wal/-shm behind, and the snapshot must stay byte-for-byte (ADR-003).
        with _scratch_copy(args.db) as scratch:
            mined += mine_ask_log(scratch)

    known = {_norm_query(it.get("question", "")) for it in existing}
    seen_new: set[str] = set()
    new_queries = []
    for q in mined:
        key = _norm_query(q["question"])
        if not key or key in known or key in seen_new:
            continue
        seen_new.add(key)
        new_queries.append(q)

    # --limit cuts ONLY questions from the MCP log. There are hundreds of them (786 unique as of
    # 17.09), while `ask_log` is about ten rows with the owner's rating; a shared limit
    # ate them to the last one, and a run "from both sources" silently yielded just one.
    skipped_by_limit = 0
    if args.limit is not None:
        kept: list[dict] = []
        from_log = 0
        for q in new_queries:
            if q.get("source") == "ask_log":
                kept.append(q)
            elif from_log < args.limit:
                kept.append(q)
                from_log += 1
            else:
                skipped_by_limit += 1
        new_queries = kept

    new_items: list[dict] = []
    if new_queries:
        with _scratch_copy(args.db) as scratch:
            next_idx = _next_start_idx(existing)
            for q in new_queries:
                if q.get("source") == "ask_log":
                    # the sources are already cited in the answer: a second search for the same
                    # question adds nothing, it only costs time
                    candidates, slice_ = candidates_from_ids(scratch, q.get("source_ids") or [])
                else:
                    candidates, slice_ = candidates_for(scratch, q["question"], top_k=args.top_k)
                new_items.append(_make_item(next_idx, q, candidates, slice_))
                next_idx += 1

    items = existing + new_items
    dup_ids = sorted({it_id for it_id, count in Counter(it.get("id") for it in items).items()
                       if count > 1})
    if dup_ids:
        print(f"[build_golden] Refused: duplicate ids in {args.out} ({len(dup_ids)}): "
              f"{dup_ids[:5]}{' …' if len(dup_ids) > 5 else ''} - file NOT written.",
              file=sys.stderr)
        return 2

    _write_golden(args.out, items, description)
    suffix = f" skipped_by_limit={skipped_by_limit}" if skipped_by_limit else ""
    print(f"[build_golden] {args.out}: kept={len(existing)} new={len(new_items)} "
          f"total={len(items)}{suffix}")

    if args.stats:
        print_stats(items)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
