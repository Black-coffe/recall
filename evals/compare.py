#!/usr/bin/env python
"""Порівняння двох прогонів `evals.gate --json-out` (Хвиля B, історія 06).

Рішення «перемикати боевую модель ембедингів чи ні» приймається за таблицею
на ДВОХ `k` (урок `eval-verdict-flips-with-k` — вердикт перевертається від
одного k) + per-item diff, а не за одним числом. Цей скрипт НІЧОГО не рахує
й не міряє сам — лише читає два вже готові файли `--json-out` гейта
(`evals/gate.py`) і зводить їх поруч. Сам гейт цей модуль не чіпає й не
викликає; жодного звернення до БД чи retrieval тут немає.

Використання:
    python -m evals.compare A.json B.json [--label-a e5] [--label-b qwen3] \\
        [--json-out evals/compare_run.local.json]

Провенанс гейта у `--json-out` — лише `db` (шлях знімка, на якому виміряно),
`golden` (шлях набору) і `k`/`aggregate` (кількість пунктів, середній
recall_at_k). Модель ембедингів, версія і прапорець `RAG_QUERY_REWRITE`
(історія 07) у цей провенанс НЕ потрапляють (гейт про них не знає) — тому
різницю між A і B називає людина через `--label-a/--label-b`, а не файл.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Той самий трюк, що в evals/gate.py і evals/run_eval.py — працює і як
# `python evals/compare.py`, і як `python -m evals.compare` без встановлення пакету.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals import golden_io  # noqa: E402

_QUESTION_MAX = 80


def _load_result(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_questions(golden_path: Optional[str]) -> dict:
    """Найкраще зусилля: підвантажити текст питань з golden-set, на який
    посилається прогін (поле ``golden`` у `--json-out`), щоб показати їх у
    diff. Сам `--json-out` гейда текст питання не зберігає (лише ``id`` —
    `evals/metrics.py::evaluate_item`) — якщо файл недосяжний, перейменований
    чи змінився, повертаємо порожню мапу і diff показує лише id."""
    if not golden_path:
        return {}
    try:
        items = golden_io.read_golden(golden_path)
    except Exception as exc:  # noqa: BLE001 — best-effort прикраса diff'а,
        # відсутність чи зіпсованість golden-файлу не має валити порівняння.
        print(f"[compare] питання з {golden_path!r} недоступні: {exc}", file=sys.stderr)
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
    lines.append(f"  db (знімок):    {result.get('db')}")
    lines.append(f"  golden:         {result.get('golden')}")
    lines.append(f"  k:              {result.get('k')}")
    lines.append(f"  verdict гейта:  {result.get('verdict')}")
    try:
        mtime = os.path.getmtime(path)
        lines.append(
            "  час (mtime файлу результату): "
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
    parser.add_argument("result_a", help="--json-out гейта, прогін A (напр. знімок на старій моделі)")
    parser.add_argument("result_b", help="--json-out гейта, прогін B (напр. знімок після re-embed)")
    parser.add_argument("--label-a", default="A", help="Підпис прогону A у таблиці/провенансі (default: A)")
    parser.add_argument("--label-b", default="B", help="Підпис прогону B у таблиці/провенансі (default: B)")
    parser.add_argument("--json-out", default=None, help="Записати структуроване порівняння у JSON")
    args = parser.parse_args(argv)

    try:
        result_a = _load_result(args.result_a)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[compare] не вдалось прочитати {args.result_a!r}: {exc}", file=sys.stderr)
        return 2
    try:
        result_b = _load_result(args.result_b)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[compare] не вдалось прочитати {args.result_b!r}: {exc}", file=sys.stderr)
        return 2

    ks_a = {int(k) for k in (result_a.get("k") or [])}
    ks_b = {int(k) for k in (result_b.get("k") or [])}
    common_k = sorted(ks_a & ks_b)
    if not common_k:
        print(f"[compare] немає спільного k між {args.result_a!r} (k={sorted(ks_a)}) і "
              f"{args.result_b!r} (k={sorted(ks_b)}) — порівнювати нічого.", file=sys.stderr)
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
    print("Модель/версія/rerank у --json-out гейта не записані (не входять у його "
          "провенанс) — розрізняйте прогони через --label-a/--label-b.")
    print("-" * 70)

    print(f"[compare] спільних пунктів: {common_count} "
          f"(лише в {args.label_a}: {len(only_in_a)}, лише в {args.label_b}: {len(only_in_b)})")
    if only_in_a or only_in_b:
        print(f"[compare] набори різняться — порівняння лише по перетину.")
        if only_in_a:
            print(f"  лише в {args.label_a} (не порівняно): {', '.join(sorted(only_in_a))}")
        if only_in_b:
            print(f"  лише в {args.label_b} (не порівняно): {', '.join(sorted(only_in_b))}")
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
