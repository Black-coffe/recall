#!/usr/bin/env python
"""Draft golden-set for action-items (eval-gate, C7) - for manual truth labelling.

Takes a STRATIFIED sample of `action_items` from a DB snapshot - by (`source`
IS NULL / `'tg_thread'`) x (whether a raw deadline phrase `due` exists) - and puts each
row as `status="unlabeled"` with an empty `truth`. `commitments.tg_tasks`
(SOURCE="tg_thread") are tasks extracted from chat history; NULL are inherited
from Claude call cards. `action_items.source='comment'` (the provenance of
owner comments) is DELIBERATELY and PERMANENTLY excluded from the sample (D14,
finding 24 of review-round-1.md): a literal reading of C7 (`"source":
null|"tg_thread"`), not a temporary oversight; extending it to 'comment' is
a separate owner decision, not a silent side effect of this file.

Each picked row carries `db_snapshot`, a rough identity marker of the snapshot
(file name + size) it was taken from (D7): `evals/tasks_eval.py`
checks it against the `--db` it measures with and excludes the item from the count if they do not
match; re-ingest recreates `action_items`, and without this
`owner_accuracy`/`due_accuracy` silently become meaningless (finding 11).

Neither `due_date` nor `owner_entity_id` is computed or checked here;
`evals/tasks_eval.py` does that separately, on any snapshot.

CLI:
    python -m evals.build_tasks_golden --db evals/snapshots/x.db
    python -m evals.build_tasks_golden --db evals/snapshots/x.db --n 60 --merge
    python -m evals.build_tasks_golden --db evals/snapshots/x.db --stats
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.graph_links import is_live_db, open_readonly  # noqa: E402
from evals.golden_io import read_jsonl  # noqa: E402 - shared reader (D13), written by story 05

#: A2 analogue for action-items (C7): a labelling target, not a code acceptance criterion.
TASKS_TARGET = 30

#: (source, whether a due phrase exists) - four strata, even distribution.
_STRATA = [(None, True), (None, False), ("tg_thread", True), ("tg_thread", False)]


# ============================================================
# Stratified sample (read-only - ADR-003, we do not mutate the snapshot)
# ============================================================

def sample_action_items(conn, n: int, *, seed: int = 0) -> list[dict]:
    """Stratified sample of `action_items` from the snapshot. Deterministic
    (`seed=0`): the same snapshot gives the same set on a repeated run."""
    rows = conn.execute(
        # C7 carries only "source": null|"tg_thread"; 'comment' (the provenance
        # of owner comments) is excluded DELIBERATELY and PERMANENTLY (D14), do not
        # revisit this WHERE without a separate owner decision.
        "SELECT ai.id, ai.transcription_id, ai.task, ai.owner_name, ai.due, ai.source, "
        "COALESCE(t.meeting_date, substr(t.created_at, 1, 10)) AS meeting_date "
        "FROM action_items ai LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
        "WHERE ai.source IS NULL OR ai.source = 'tg_thread'"
    ).fetchall()

    buckets: dict[tuple, list[dict]] = {k: [] for k in _STRATA}
    for r in rows:
        has_due = bool(r["due"] and str(r["due"]).strip())
        buckets.setdefault((r["source"], has_due), []).append(dict(r))

    rng = random.Random(seed)
    per_stratum = max(1, n // len(_STRATA))
    picked: list[dict] = []
    picked_ids: set[int] = set()
    for key in _STRATA:
        pool = list(buckets.get(key, []))
        rng.shuffle(pool)
        for row in pool[:per_stratum]:
            picked.append(row)
            picked_ids.add(row["id"])

    if len(picked) < n:
        remainder = [row for key in _STRATA for row in buckets.get(key, [])
                     if row["id"] not in picked_ids]
        rng.shuffle(remainder)
        for row in remainder[: n - len(picked)]:
            picked.append(row)
            picked_ids.add(row["id"])

    return picked[:n]


def _snapshot_identity(db_path: str) -> dict:
    """Rough snapshot identity marker (D7): file base name + size.
    Not a hash (315 MB on every run is too expensive); the same copy as in
    `evals/tasks_eval.py` (we do not pull a neighbouring module's private name across the
    file boundary)."""
    st = os.stat(db_path)
    return {"path": os.path.basename(db_path), "size": st.st_size}


# ============================================================
# JSONL (read by evals.golden_io.read_jsonl - D13; writes its own record, because
# golden_io writes only the C1 set of the retrieval gate, not C7)
# ============================================================

def _existing_items(path: str) -> list[dict]:
    """The existing `--out`, if there is one (first run: no file yet, not an
    error). An unreadable/invalid file: `read_jsonl` raises, `main` maps it
    to exit 2 (D3)."""
    if not os.path.exists(path):
        return []
    return read_jsonl(path)


def _write_jsonl(path: str, items: list[dict]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


#: `task-0001`, ... - the same format that `_make_item` emits.
_ID_RE = re.compile(r"^task-(\d+)$")


def _next_index(existing: list[dict]) -> int:
    """The next free id number - NOT `len(existing) + 1` (finding 16):
    manually deleting one row from the file shifts the length, and `len+1` re-issues a
    number that still belongs to ANOTHER item further down the list. The maximum
    taken number + 1 cannot produce such a collision: the set of taken
    numbers only grows."""
    used = set()
    for it in existing:
        m = _ID_RE.match(str(it.get("id") or ""))
        if m:
            used.add(int(m.group(1)))
    return (max(used) + 1) if used else 1


def _make_item(idx: int, row: dict, db_snapshot: dict) -> dict:
    return {
        "id": f"task-{idx:04d}",
        "action_item_id": row["id"],
        "transcription_id": row["transcription_id"],
        "task": row["task"],
        "owner_name_raw": row["owner_name"],
        "due_raw": row["due"],
        "meeting_date": row["meeting_date"],
        "source": row["source"],
        "truth": {"owner": None, "due_date": None, "due_precision": None},
        "status": "unlabeled",
        "db_snapshot": db_snapshot,
    }


def print_stats(items: list[dict]) -> None:
    labeled = sum(1 for it in items if it.get("status") == "labeled")
    deficit = max(0, TASKS_TARGET - labeled)
    print(f"[build_tasks_golden] labeled={labeled} target={TASKS_TARGET} "
          f"deficit={deficit} (total={len(items)})")


# ============================================================
# CLI
# ============================================================

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="whisper_history.db", help="Path to the DB SNAPSHOT")
    parser.add_argument("--n", type=int, default=60, help="How many tasks to pick in total (default: 60)")
    parser.add_argument("--out", default="evals/tasks_golden.local.jsonl",
                         help="Where to write the JSONL (default: evals/tasks_golden.local.jsonl)")
    parser.add_argument("--merge", action="store_true",
                         help="Add only new action_item_id to the existing --out "
                              "(without this flag --out is OVERWRITTEN with a fresh sample)")
    parser.add_argument("--stats", action="store_true", help="Print labeled against the target")
    parser.add_argument("--seed", type=int, default=0, help="Seed of the deterministic sample (default: 0)")
    parser.add_argument("--yes-live", action="store_true",
                         help="Allow opening the file that matches Config.DATABASE")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[build_tasks_golden] DB not found: {args.db}", file=sys.stderr)
        return 2
    if is_live_db(args.db) and not args.yes_live:
        print(f"[build_tasks_golden] Refused: {args.db!r} is the live DB (Config.DATABASE). "
              "This tooling works on a SNAPSHOT. If this is a deliberate choice, add --yes-live.",
              file=sys.stderr)
        return 2

    try:
        existing = _existing_items(args.out) if args.merge else []
        if not args.merge and os.path.exists(args.out):
            prior = _existing_items(args.out)
            lost = sum(1 for it in prior if it.get("status") == "labeled")
            if lost:
                print(f"[build_tasks_golden] WARNING: {args.out} is overwritten without --merge - "
                      f"{lost} labeled items will be lost. Add --merge to keep them.",
                      file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"[build_tasks_golden] {args.out} unreadable: {exc}", file=sys.stderr)
        return 2

    existing_ids = {it.get("action_item_id") for it in existing}

    conn = open_readonly(args.db)
    try:
        sampled = sample_action_items(conn, args.n, seed=args.seed)
    finally:
        conn.close()

    db_snapshot = _snapshot_identity(args.db)
    next_idx = _next_index(existing)
    new_items = []
    for row in sampled:
        if row["id"] in existing_ids:
            continue
        new_items.append(_make_item(next_idx, row, db_snapshot))
        existing_ids.add(row["id"])
        next_idx += 1

    items = existing + new_items

    # Duplicate ids are not silently collapsed (finding 16): by construction of `_next_index`
    # there should be none here; this is the last line of defence against a corrupted
    # `--out` that someone edited by hand.
    dupe_ids = sorted({i for i, n in Counter(it.get("id") for it in items).items() if n > 1})
    if dupe_ids:
        print(f"[build_tasks_golden] Duplicate ids in {args.out} (not silently collapsed): {dupe_ids}", file=sys.stderr)
        return 2

    _write_jsonl(args.out, items)
    print(f"[build_tasks_golden] {args.out}: kept={len(existing)} new={len(new_items)} total={len(items)}")

    if args.stats:
        print_stats(items)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
