#!/usr/bin/env python
"""Deterministic eval for task deadlines/owners (eval-gate, C7/A3/C3).

Does not call Claude and does not run `enrich_transcription`/`tg_tasks`; it computes
only what is deterministic offline: `commitments.parse_due(due_raw, meeting_date)`
against `truth.due_date` and the current `action_items.owner_entity_id` from the snapshot
(-> canonical name/alias) against `truth.owner`. The accuracy of Claude's own extraction is
outside this eval (A3).

Metrics are computed only over `status="labeled"` items of the golden set
(`evals/build_tasks_golden.py`) whose `truth` is ASSERTED, i.e. differs from
the default empty block with which the builder marks a just-picked, not yet
labeled row (D7/finding 9 of review-round-1.md). A file where only
`status` was switched to `labeled` asserts nothing and will not be counted.
An item whose `action_item_id` no longer names the same row (re-ingest
recreates `action_items`, finding 11), or whose snapshot provenance does not
match the current `--db` (D7), is excluded from the count as "stale" rather than
being silently measured against someone else's data.

CLI:
    python -m evals.tasks_eval --golden evals/tasks_golden.local.jsonl --db evals/snapshots/x.db
    python -m evals.tasks_eval --golden ... --db ... --gate --min-due-acc 0.90 --min-owner-acc 0.80
"""
from __future__ import annotations

import argparse
import os
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
from app.services import commitments  # noqa: E402 - stdlib + app.db.connection only, no torch/e5


def _normalize_owner_name(name: str) -> str:
    """The same key as `commitments._normalize_name`/`link_owners` (a deliberate
    6-line copy: we do not pull a neighbouring module's private name across the
    file boundary; case is computed in Python because SQLite `LOWER` does not fold Cyrillic)."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t\n\r.,;:!?\"'`«»()[]{}-–—")


def _snapshot_identity(db_path: str) -> dict:
    """Rough snapshot identity marker (D7): file base name + size.
    Not a hash (315 MB on every run is too expensive); it catches the main threat
    (a re-ingest rewrote the snapshot data IN PLACE) and does not raise an alarm
    for a file moved under another name for no reason."""
    st = os.stat(db_path)
    return {"path": os.path.basename(db_path), "size": st.st_size}


# ============================================================
# Truth: an item counts only if ASSERTED (D7, finding 9)
# ============================================================

#: The same default block with which `build_tasks_golden._make_item` marks
#: a just-picked, NOT yet labeled row. `truth == _DEFAULT_TRUTH` after
#: `status="labeled"` means the field was merely switched without filling anything in.
_DEFAULT_TRUTH = {"owner": None, "due_date": None, "due_precision": None}


def _truth_asserted(it: dict) -> bool:
    truth = it.get("truth") or {}
    normalized = {
        "owner": truth.get("owner"),
        "due_date": truth.get("due_date"),
        "due_precision": truth.get("due_precision"),
    }
    return normalized != _DEFAULT_TRUTH


# ============================================================
# Row provenance: whether `action_item_id` still names the same item
# (finding 11: re-ingest recreates action_items)
# ============================================================

def _row_status(conn, action_item_id: int, expected_task) -> tuple[str, Optional[dict]]:
    """`("fresh"|"row-missing"|"task-text-changed", row|None)`."""
    row = conn.execute(
        "SELECT id, task, owner_entity_id FROM action_items WHERE id = ?",
        (action_item_id,),
    ).fetchone()
    if row is None:
        return "row-missing", None
    if expected_task is not None and row["task"] != expected_task:
        return "task-text-changed", row
    return "fresh", row


# ============================================================
# Owner: owner_entity_id from the snapshot -> canonical name/alias against truth.owner
# ============================================================

def _entity_canonical_and_names(conn, entity_id: Optional[int]) -> tuple[Optional[str], set[str]]:
    """The normalized canonical name separately (for the first-name fallback below) +
    the canonical name together with all aliases (`commitments._normalize_name`,
    the same key as `link_owners`). Empty if `entity_id` is not set
    or the entity disappeared from the snapshot (deletion/merge)."""
    if entity_id is None:
        return None, set()
    names: set[str] = set()
    canonical_norm: Optional[str] = None
    row = conn.execute("SELECT canonical_name FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row and row["canonical_name"]:
        canonical_norm = _normalize_owner_name(row["canonical_name"])
        names.add(canonical_norm)
    for r in conn.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?", (entity_id,)):
        if r["alias"]:
            names.add(_normalize_owner_name(r["alias"]))
    return canonical_norm, names


def _owner_match(conn, entity_id: Optional[int], truth_owner: Optional[str]) -> bool:
    canonical_norm, names = _entity_canonical_and_names(conn, entity_id)
    want = _normalize_owner_name(truth_owner) if truth_owner else ""
    if not want:
        return not names
    if want in names:
        return True
    # First-name fallback of the linker (commitments.link_owners: «Юля Гончар» -> «юля»)
    # - a multi-word truth.owner against a single-word CANONICAL name (not
    # aliases: the production fallback also looks only at the canonical one; finding 25).
    if " " in want and canonical_norm and " " not in canonical_norm:
        if want.split(" ", 1)[0] == canonical_norm:
            return True
    return False


# ============================================================
# Scoring
# ============================================================

def evaluate(golden_items: list[dict], conn, *, db_path: Optional[str] = None) -> dict:
    labeled = [it for it in golden_items if it.get("status") == "labeled"]
    current_snapshot = _snapshot_identity(db_path) if db_path else None

    due_results: list[bool] = []
    owner_results: list[bool] = []
    misses: list[dict] = []
    stale: list[dict] = []
    n_unasserted = 0
    n_due_no_anchor = 0
    n_scored = 0

    for it in labeled:
        if not _truth_asserted(it):
            n_unasserted += 1
            continue

        aiid = it.get("action_item_id")
        row_state, row = _row_status(conn, aiid, it.get("task"))

        item_snapshot = it.get("db_snapshot")
        snapshot_mismatch = bool(
            current_snapshot and item_snapshot
            and (item_snapshot.get("path"), item_snapshot.get("size"))
            != (current_snapshot["path"], current_snapshot["size"])
        )

        if row_state != "fresh" or snapshot_mismatch:
            stale.append({
                "id": it.get("id"), "action_item_id": aiid,
                "reason": "snapshot-mismatch" if (row_state == "fresh" and snapshot_mismatch) else row_state,
            })
            continue

        n_scored += 1
        truth = it.get("truth") or {}

        due_raw = it.get("due_raw")
        meeting_date = it.get("meeting_date")
        due_ok: Optional[bool] = None
        if due_raw and meeting_date is None:
            # commitments.parse_due(raw, anchor=None) silently substitutes
            # date.today() for relative phrases (finding 17); we do not call it,
            # the item does not count toward due_accuracy.
            n_due_no_anchor += 1
        else:
            got_due, _prec = commitments.parse_due(due_raw, meeting_date)
            want_due = truth.get("due_date")
            due_ok = got_due == want_due
            due_results.append(due_ok)

        owner_ok = _owner_match(conn, row["owner_entity_id"], truth.get("owner"))
        owner_results.append(owner_ok)

        if due_ok is False or not owner_ok:
            misses.append({
                "id": it.get("id"), "action_item_id": aiid,
                "due_ok": due_ok, "want_due": truth.get("due_date"),
                "owner_ok": owner_ok, "owner_entity_id": row["owner_entity_id"], "want_owner": truth.get("owner"),
            })

    def _acc(results: list[bool]) -> Optional[float]:
        return (sum(1 for r in results if r) / len(results)) if results else None

    return {
        "n_total": len(golden_items),
        "n_labeled": len(labeled),
        "n_scored": n_scored,
        "n_unasserted": n_unasserted,
        "n_stale": len(stale),
        "stale": stale,
        "n_due_no_anchor": n_due_no_anchor,
        "due_accuracy": _acc(due_results),
        "n_due_scored": len(due_results),
        "owner_accuracy": _acc(owner_results),
        "n_owner_scored": len(owner_results),
        "misses": misses,
    }


def _print_report(result: dict) -> None:
    print()
    print(f"[tasks_eval] golden: {result['n_total']} records, labeled={result['n_labeled']}, "
          f"asserted={result['n_scored']} "
          f"(unasserted={result['n_unasserted']}, stale={result['n_stale']}, "
          f"no-due-anchor={result['n_due_no_anchor']})")
    due_acc = result["due_accuracy"]
    owner_acc = result["owner_accuracy"]
    print(f"  due_accuracy:   {due_acc * 100:.1f}% (n={result['n_due_scored']})"
          if due_acc is not None else "  due_accuracy:   — (n=0)")
    print(f"  owner_accuracy: {owner_acc * 100:.1f}% (n={result['n_owner_scored']})"
          if owner_acc is not None else "  owner_accuracy: — (n=0)")
    if result["stale"]:
        print(f"\nStale items ({len(result['stale'])}), excluded from the count:")
        for s in result["stale"]:
            print(f"  {s['id']} (#{s['action_item_id']}): {s['reason']}")
    if result["misses"]:
        print(f"\nMisses ({len(result['misses'])}):")
        for m in result["misses"]:
            parts = [f"  {m['id']} (#{m['action_item_id']}):"]
            if m["due_ok"] is False:
                parts.append(f"due want={m['want_due']!r}")
            if not m["owner_ok"]:
                parts.append(f"owner entity=#{m['owner_entity_id']} want={m['want_owner']!r}")
            print(" ".join(parts))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", required=True, help="Path to the evals/build_tasks_golden.py JSONL")
    parser.add_argument("--db", default="whisper_history.db", help="Path to the DB SNAPSHOT (owner_entity_id/entities)")
    parser.add_argument("--gate", action="store_true", help="Return 0/1 by thresholds instead of always 0")
    parser.add_argument("--min-due-acc", type=float, default=0.90, help="due_accuracy threshold for --gate (default: 0.90)")
    parser.add_argument("--min-owner-acc", type=float, default=0.80, help="owner_accuracy threshold for --gate (default: 0.80)")
    parser.add_argument("--yes-live", action="store_true",
                         help="Allow opening the file that matches Config.DATABASE")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[tasks_eval] DB not found: {args.db}", file=sys.stderr)
        return 2
    if is_live_db(args.db) and not args.yes_live:
        print(f"[tasks_eval] Refused: {args.db!r} is the live DB (Config.DATABASE). "
              "This tooling works on a SNAPSHOT. If this is a deliberate choice, add --yes-live.",
              file=sys.stderr)
        return 2

    try:
        golden_items = read_jsonl(args.golden)
    except (OSError, ValueError) as exc:
        print(f"[tasks_eval] Golden set unreadable: {exc}", file=sys.stderr)
        return 2

    dupes = sorted({i for i, n in Counter(it.get("id") for it in golden_items).items() if n > 1})
    if dupes:
        print(f"[tasks_eval] Duplicate ids in the golden set (not silently collapsed): {dupes}", file=sys.stderr)
        return 2

    conn = open_readonly(args.db)
    try:
        result = evaluate(golden_items, conn, db_path=args.db)
    finally:
        conn.close()

    _print_report(result)

    if result["n_scored"] == 0:
        print("\n[tasks_eval] No items with asserted truth on this snapshot - nothing to count.",
              file=sys.stderr)
        return 2

    if not args.gate:
        return 0

    due_acc = result["due_accuracy"] or 0.0
    owner_acc = result["owner_accuracy"] or 0.0
    passed = due_acc >= args.min_due_acc and owner_acc >= args.min_owner_acc
    print(f"\n[tasks_eval] gate: {'PASS' if passed else 'FAIL'} "
          f"(due>={args.min_due_acc:.2f} owner>={args.min_owner_acc:.2f})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
