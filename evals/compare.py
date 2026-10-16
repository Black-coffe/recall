#!/usr/bin/env python
"""Comparison of two `evals.gate --json-out` runs (Wave B, story 06).

The decision "whether to switch the live embedding model or not" is made from a table
over TWO `k` values (lesson `eval-verdict-flips-with-k`: the verdict flips from
one k to another) + a per-item diff, not from a single number. This script computes
and measures NOTHING itself; it only reads two ready-made gate `--json-out` files
(`evals/gate.py`) and puts them side by side. It does not touch or call the gate itself;
there is no access to the DB or retrieval here.

Usage:
    python -m evals.compare A.json B.json [--label-a e5] [--label-b qwen3] \\
        [--json-out evals/compare_run.local.json]

Gate provenance in `--json-out` is only `db` (the path of the snapshot it was measured on),
`golden` (the set path) and `k`/`aggregate` (item count, mean
recall_at_k). The embedding model, version and the `RAG_QUERY_REWRITE` flag
(story 07) do NOT end up in this provenance (the gate knows nothing about them), so
the difference between A and B is named by a human via `--label-a/--label-b`, not by the file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# The same trick as in evals/gate.py and evals/run_eval.py: works both as
# `python evals/compare.py` and as `python -m evals.compare` without installing the package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals import golden_io  # noqa: E402

_QUESTION_MAX = 80


def _load_result(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_questions(golden_path: Optional[str]) -> dict:
    """Best effort: load question text from the golden-set the run
    refers to (the ``golden`` field in `--json-out`) in order to show it in the
    diff. The gate's `--json-out` itself does not store question text (only ``id``,
    `evals/metrics.py::evaluate_item`); if the file is unreachable, renamed
    or changed, we return an empty map and the diff shows only ids."""
    if not golden_path:
        return {}
    try:
        items = golden_io.read_golden(golden_path)
    except Exception as exc:  # noqa: BLE001 - best-effort decoration of the diff,
        # a missing or corrupted golden file must not break the comparison.
        print(f"[compare] questions from {golden_path!r} unavailable: {exc}", file=sys.stderr)
        return {}
    return {str(it["id"]): it.get("question", "") for it in items}


def _truncate(text: str, limit: int = _QUESTION_MAX) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _mean(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:5.1f}%" if v is not None else "   — "


def _fmt_delta(v: Optional[float]) -> str:
    return f"{v * 100:+5.1f}%" if v is not None else "   — "


def _per_item_map(result: dict, k: int) -> dict:
    rows = (result.get("per_item") or {}).get(str(k)) or []
    return {str(r["id"]): r for r in rows if r.get("id") is not None}


def _provenance_lines(label: str, path: str, result: dict) -> list:
    lines = [f"[{label}] {path}"]
    lines.append(f"  db (snapshot):  {result.get('db')}")
    lines.append(f"  golden:         {result.get('golden')}")
    lines.append(f"  k:              {result.get('k')}")
    lines.append(f"  gate verdict:   {result.get('verdict')}")
    try:
        mtime = os.path.getmtime(path)
        lines.append(
            "  time (mtime of the result file): "
            + datetime.fromtimestamp(mtime).isoformat(timespec="seconds"))
    except OSError:
        pass
    agg = result.get("aggregate") or {}
    for k_str in sorted(agg, key=lambda s: int(s)):
        a = agg[k_str] or {}
        lines.append(
            f"  k={k_str}: n={a.get('n')} recall_at_k={_fmt_pct(a.get('recall_at_k'))}")
    return lines


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("result_a", help="gate --json-out, run A (e.g. a snapshot on the old model)")
    parser.add_argument("result_b", help="gate --json-out, run B (e.g. a snapshot after re-embed)")
    parser.add_argument("--label-a", default="A", help="Label of run A in the table/provenance (default: A)")
    parser.add_argument("--label-b", default="B", help="Label of run B in the table/provenance (default: B)")
    parser.add_argument("--json-out", default=None, help="Write the structured comparison to JSON")
    args = parser.parse_args(argv)

    try:
        result_a = _load_result(args.result_a)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[compare] could not read {args.result_a!r}: {exc}", file=sys.stderr)
        return 2
    try:
        result_b = _load_result(args.result_b)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[compare] could not read {args.result_b!r}: {exc}", file=sys.stderr)
        return 2

    ks_a = {int(k) for k in (result_a.get("k") or [])}
    ks_b = {int(k) for k in (result_b.get("k") or [])}
    common_k = sorted(ks_a & ks_b)
    if not common_k:
        print(f"[compare] no common k between {args.result_a!r} (k={sorted(ks_a)}) and "
              f"{args.result_b!r} (k={sorted(ks_b)}) - nothing to compare.", file=sys.stderr)
        return 2

    questions = _load_questions(result_a.get("golden"))
    questions.update({k: v for k, v in _load_questions(result_b.get("golden")).items()
                       if k not in questions})

    table_rows = {}
    diff_by_k = {}
    only_in_a: set = set()
    only_in_b: set = set()

    for k in common_k:
        items_a = _per_item_map(result_a, k)
        items_b = _per_item_map(result_b, k)
        ids_a, ids_b = set(items_a), set(items_b)
        common_ids = ids_a & ids_b
        only_in_a |= (ids_a - ids_b)
        only_in_b |= (ids_b - ids_a)

        recall_a = _mean([items_a[i].get("recall_at_k") for i in common_ids])
        recall_b = _mean([items_b[i].get("recall_at_k") for i in common_ids])
        delta = (recall_b - recall_a) if (recall_a is not None and recall_b is not None) else None
        table_rows[k] = {"a": recall_a, "b": recall_b, "delta": delta, "n": len(common_ids)}

        hit_to_miss, miss_to_hit = [], []
        for item_id in sorted(common_ids):
            hit_a = items_a[item_id].get("hit")
            hit_b = items_b[item_id].get("hit")
            entry = {"id": item_id, "question": _truncate(questions.get(item_id, ""))}
            if hit_a is True and hit_b is False:
                hit_to_miss.append(entry)
            elif hit_a is False and hit_b is True:
                miss_to_hit.append(entry)
        diff_by_k[k] = {"hit_to_miss": hit_to_miss, "miss_to_hit": miss_to_hit}

    common_count = len(set(_per_item_map(result_a, common_k[0])) & set(_per_item_map(result_b, common_k[0])))

    print("=" * 70)
    for label, path, result in ((args.label_a, args.result_a, result_a),
                                 (args.label_b, args.result_b, result_b)):
        for line in _provenance_lines(label, path, result):
            print(line)
        print("-" * 70)
    print("Model/version/rerank are not recorded in the gate --json-out (not part of its "
          "provenance) - tell the runs apart via --label-a/--label-b.")
    print("-" * 70)

    print(f"[compare] common items: {common_count} "
          f"(only in {args.label_a}: {len(only_in_a)}, only in {args.label_b}: {len(only_in_b)})")
    if only_in_a or only_in_b:
        print(f"[compare] the sets differ - comparing only on the intersection.")
        if only_in_a:
            print(f"  only in {args.label_a} (not compared): {', '.join(sorted(only_in_a))}")
        if only_in_b:
            print(f"  only in {args.label_b} (not compared): {', '.join(sorted(only_in_b))}")
    print("-" * 70)

    print(f"{'k':<4}{args.label_a:>10}{args.label_b:>10}{'Δ':>10}{'n':>6}")
    for k in common_k:
        row = table_rows[k]
        print(f"{k:<4}{_fmt_pct(row['a']):>10}{_fmt_pct(row['b']):>10}"
              f"{_fmt_delta(row['delta']):>10}{row['n']:>6}")

    for k in common_k:
        d = diff_by_k[k]
        print("-" * 70)
        print(f"k={k}  hit→miss ({len(d['hit_to_miss'])}):")
        for entry in d["hit_to_miss"]:
            suffix = f" — {entry['question']}" if entry["question"] else ""
            print(f"    {entry['id']}{suffix}")
        print(f"k={k}  miss→hit ({len(d['miss_to_hit'])}):")
        for entry in d["miss_to_hit"]:
            suffix = f" — {entry['question']}" if entry["question"] else ""
            print(f"    {entry['id']}{suffix}")

    if args.json_out:
        out = {
            "label_a": args.label_a,
            "label_b": args.label_b,
            "result_a_path": os.path.abspath(args.result_a),
            "result_b_path": os.path.abspath(args.result_b),
            "common_k": common_k,
            "common_count": common_count,
            "only_in_a": sorted(only_in_a),
            "only_in_b": sorted(only_in_b),
            "table": {str(k): table_rows[k] for k in common_k},
            "diff": {str(k): diff_by_k[k] for k in common_k},
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
