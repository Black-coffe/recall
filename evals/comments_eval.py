"""Measuring the effect of the comments layer on the results (Wave 2.5).

WHAT IS MEASURED HERE AND WHY IT IS NOT recall@k.

Plain recall@k answers the question "did we find the right record". The comments
layer asks a different question: "did the boost spoil what used to work". These are
different dimensions, and for the second one a golden-set with expected ids is not needed; what is needed is
a DIFF between two runs of the same query:

  A. `include_comments=False` - the results as they were before the layer;
  B. `include_comments=True`  - the results with comments and the boost.

Three numbers come out of this diff:

  * **drift** - how many records from A dropped out of B. This is "pushed out something useful".
  * **comment_slots** - how many slots in B were taken by comments themselves.
  * **rank_shift** - by how many positions the survivors moved.

TWO k ARE MANDATORY. At k=8 and k=12 the verdict has already flipped once
(memory/eval-verdict-flips-with-k): a narrow window punishes any addition because
there are few slots, a wide one forgives. A single k gives a confident answer that does not survive
a check with the second.

EMPTY-ARCHIVE INVARIANT. While there are no comments, A and B must match
BYTE FOR BYTE. If not, the layer changes something in every query even before a single
comment appears in it, and that is not a "small effect" but a bug. The `--invariant`
mode checks exactly this and returns a non-zero code on a mismatch.

--seed MODE. Without comments in the DB there is nothing to measure the shift on. `--seed N` plants
synthetic comments on a COPY of the database, made from the text of REAL chunks that the
results already return (i.e. maximally "plausible": they are guaranteed to be
relevant to the query and therefore give an UPPER estimate of the shift, worse than reality).
The script never touches the live DB: `--db` must point to a copy.

Usage:

    # 1. Invariant on the live DB (safe, read-only):
    python -m evals.comments_eval --golden evals/golden_set.local.json --invariant

    # 2. Shift on a copy with planted comments:
    python -m evals.comments_eval --golden evals/golden_set.local.json \\
        --db copy.db --seed 2 --json-out evals/comments_drift.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: Both k are always measured - see the module docstring.
K_VALUES = (8, 12)


def _load_questions(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", data) if isinstance(data, dict) else data
    return [{"id": it.get("id") or f"q{i}", "question": it["question"],
             "category_id": it.get("category_id")}
            for i, it in enumerate(items) if it.get("question")]


def _key(ch: dict):
    """Identity of a result item for the diff.

    For an archive chunk, its id; for a comment, a separate space so that
    negative ids do not mix with positive ones (same reason as in retrieval).
    """
    if ch.get("source_type") == "comment":
        return ("comment", ch.get("comment_id"))
    return ("chunk", ch.get("chunk_id"))


def _run_pair(db_path: str, item: dict, k: int) -> dict:
    """One query, two runs, a diff between them."""
    from app.services import retrieval

    base = retrieval.search(db_path, item["question"], top_k=k,
                            category_id=item.get("category_id"),
                            include_comments=False)["chunks"]
    with_cm = retrieval.search(db_path, item["question"], top_k=k,
                               category_id=item.get("category_id"),
                               include_comments=True)["chunks"]

    base_keys = [_key(c) for c in base]
    new_keys = [_key(c) for c in with_cm]
    base_pos = {kk: i for i, kk in enumerate(base_keys)}
    new_pos = {kk: i for i, kk in enumerate(new_keys)}

    dropped = [kk for kk in base_keys if kk not in new_pos]
    comment_slots = [kk for kk in new_keys if kk[0] == "comment"]
    # Rank shift is counted only for those that remain in both - otherwise "disappeared"
    # would be counted both as drift and as a giant shift.
    shifts = [abs(new_pos[kk] - base_pos[kk]) for kk in base_keys if kk in new_pos]

    return {
        "id": item["id"], "k": k,
        "identical": base_keys == new_keys,
        "drift": len(dropped),
        "comment_slots": len(comment_slots),
        "max_rank_shift": max(shifts) if shifts else 0,
        "mean_rank_shift": round(sum(shifts) / len(shifts), 2) if shifts else 0.0,
        "dropped": [list(x) for x in dropped],
        "top1_changed": bool(base_keys[:1] != new_keys[:1]),
    }


def _aggregate(rows: list[dict], k: int) -> dict:
    at_k = [r for r in rows if r["k"] == k]
    n = len(at_k) or 1
    return {
        "k": k, "items": len(at_k),
        "identical": sum(1 for r in at_k if r["identical"]),
        "drift_total": sum(r["drift"] for r in at_k),
        "drift_mean": round(sum(r["drift"] for r in at_k) / n, 2),
        "comment_slots_total": sum(r["comment_slots"] for r in at_k),
        "top1_changed": sum(1 for r in at_k if r["top1_changed"]),
        "max_rank_shift": max((r["max_rank_shift"] for r in at_k), default=0),
    }


# ------------------------------------------------------------------ seeding

def seed_comments(db_path: str, questions: list[dict], per_question: int) -> dict:
    """Plant synthetic comments on a COPY of the database.

    The text source is real chunks that the results already return for these same questions.
    This is deliberately the worst case: such comments are guaranteed to be relevant, so
    the shift they produce is an upper bound, not a typical value.
    """
    from app.services import comments, retrieval

    made = 0
    for item in questions:
        chunks = retrieval.search(db_path, item["question"], top_k=per_question,
                                  category_id=item.get("category_id"),
                                  include_comments=False)["chunks"]
        for ch in chunks[:per_question]:
            tid = ch.get("transcription_id")
            if not tid:
                continue
            body = (ch.get("text") or "")[:400].strip()
            if not body:
                continue
            c = comments.create(db_path, "transcription", tid,
                                f"Уточнення власника: {body}", kind="correction",
                                source="import", check_target=False)
            comments.index_comment(db_path, c["id"])
            made += 1
    return {"seeded": made}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--golden", required=True)
    p.add_argument("--db", default="whisper_history.db")
    p.add_argument("--invariant", action="store_true",
                   help="require A and B to match (an archive without comments)")
    p.add_argument("--seed", type=int, default=0,
                   help="plant N comments per question BEFORE measuring (DB copy only!)")
    p.add_argument("--json-out", default=None)
    a = p.parse_args(argv)

    questions = _load_questions(a.golden)
    if not questions:
        print("golden-set is empty", file=sys.stderr)
        return 2

    if a.seed:
        if os.path.abspath(a.db) == os.path.abspath(
                os.path.join(_PROJECT_ROOT, "whisper_history.db")):
            print("REFUSED: --seed writes to the DB. Pass a COPY via --db.",
                  file=sys.stderr)
            return 2
        print(f"seed: {seed_comments(a.db, questions, a.seed)}")

    rows = [_run_pair(a.db, item, k) for k in K_VALUES for item in questions]
    aggs = [_aggregate(rows, k) for k in K_VALUES]

    print(f"\n{'k':>3} {'questions':>9} {'identical':>10} {'drift':>7} "
          f"{'drift/avg':>10} {'CM slots':>10} {'top1!=':>6} {'max shift':>9}")
    for g in aggs:
        print(f"{g['k']:>3} {g['items']:>7} {g['identical']:>10} {g['drift_total']:>7} "
              f"{g['drift_mean']:>10} {g['comment_slots_total']:>10} "
              f"{g['top1_changed']:>6} {g['max_rank_shift']:>9}")

    changed = [r for r in rows if not r["identical"]]
    if changed:
        print(f"\nChanged queries ({len(changed)}):")
        for r in sorted(changed, key=lambda x: -x["drift"])[:12]:
            print(f"  k={r['k']:>2} {r['id']:<28} drift={r['drift']} "
                  f"CM slots={r['comment_slots']} max shift={r['max_rank_shift']}")

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump({"aggregates": aggs, "per_item": rows}, f,
                      ensure_ascii=False, indent=2)
        print(f"\n→ {a.json_out}")

    if a.invariant:
        if changed:
            print(f"\n❌ INVARIANT VIOLATED: {len(changed)} queries changed, "
                  f"although there should be no comments in the archive.", file=sys.stderr)
            return 1
        print("\n✅ Invariant holds: without comments the results are unchanged.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
