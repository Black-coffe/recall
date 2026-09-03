#!/usr/bin/env python
"""Детермінований eval для дедлайнів/власників задач (eval-gate, C7/A3/C3).

Не викликає Claude і не запускає `enrich_transcription`/`tg_tasks` — рахує
лише те, що детерміноване офлайн: `commitments.parse_due(due_raw, meeting_date)`
проти `truth.due_date` і поточний `action_items.owner_entity_id` зі знімка
(→ канонічне імʼя/аліас) проти `truth.owner`. Точність самого витягу Claude —
поза межами цього eval (A3).

Метрики рахуються лише по `status="labeled"` пунктах golden-набору
(`evals/build_tasks_golden.py`), чия `truth` СТВЕРДЖЕНА — відрізняється від
дефолтного порожнього блоку, яким будівник відмічає щойно вибраний, ще не
розмічений рядок (D7/знахідка 9 review-round-1.md). Файл, якому лише
перемкнули `status` на `labeled`, нічого не стверджує — рахуватись не буде.
Пункт, чий `action_item_id` більше не називає той самий рядок (реінджест
перестворює `action_items` — знахідка 11), чи чий знімок-провенанс не
збігається з поточним `--db` (D7), виключається з рахунку як "стейл", а не
тихо змірюється проти чужих даних.

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
from evals.golden_io import read_jsonl  # noqa: E402 — спільний читач (D13), пишеться історією 05
from app.services import commitments  # noqa: E402 — лише stdlib + app.db.connection, без torch/e5


def _normalize_owner_name(name: str) -> str:
    """Той самий ключ, що `commitments._normalize_name`/`link_owners` (свідома
    копія 6 рядків — не тягнемо приватне імʼя сусіднього модуля через межу
    файлів; регістр рахуємо в Python, бо SQLite `LOWER` не згортає кирилицю)."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t\n\r.,;:!?\"'`«»()[]{}-–—")


def _snapshot_identity(db_path: str) -> dict:
    """Груба ознака ідентичності знімка (D7): базове імʼя файлу + розмір.
    Не хеш (315 МБ на кожен прогін — задорого) — ловить головну загрозу
    (реінджест переписав дані знімка НА МІСЦІ), не переносить файл під іншим
    імʼям без причини бити тривогу."""
    st = os.stat(db_path)
    return {"path": os.path.basename(db_path), "size": st.st_size}


# ============================================================
# Правда: пункт зараховується лише якщо СТВЕРДЖЕНА (D7, знахідка 9)
# ============================================================

#: Той самий дефолтний блок, яким `build_tasks_golden._make_item` відмічає
#: щойно вибраний, ще НЕ розмічений рядок. `truth == _DEFAULT_TRUTH` після
#: `status="labeled"` означає, що поле лише перемкнули, нічого не заповнивши.
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
# Провенанс рядка: чи ще називає `action_item_id` той самий пункт
# (знахідка 11 — реінджест перестворює action_items)
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
# Власник: owner_entity_id зі знімка → канонічне імʼя/аліас проти truth.owner
# ============================================================

def _entity_canonical_and_names(conn, entity_id: Optional[int]) -> tuple[Optional[str], set[str]]:
    """Нормалізоване канонічне імʼя окремо (для first-name fallback нижче) +
    канонічне імʼя разом з усіма аліасами (`commitments._normalize_name`,
    той самий ключ, що й `link_owners`). Порожні, якщо `entity_id` не задано
    або сутність зникла зі знімка (видалення/merge)."""
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
    # First-name fallback лінкера (commitments.link_owners: «Юля Гончар» → «юля»)
    # — багатослівне truth.owner проти однослівного КАНОНІЧНОГО імені (не
    # аліасів — фолбек продакшена теж дивиться лише на канонічне; знахідка 25).
    if " " in want and canonical_norm and " " not in canonical_norm:
        if want.split(" ", 1)[0] == canonical_norm:
            return True
    return False


# ============================================================
# Оцінка
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
            # commitments.parse_due(raw, anchor=None) мовчки підставляє
            # date.today() для відносних фраз (знахідка 17) — не кличемо,
            # пункт не зараховується в due_accuracy.
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
    print(f"[tasks_eval] golden: {result['n_total']} записів, labeled={result['n_labeled']}, "
          f"ствердж.={result['n_scored']} "
          f"(без-правди={result['n_unasserted']}, стейл={result['n_stale']}, "
          f"без-якоря-дедлайну={result['n_due_no_anchor']})")
    due_acc = result["due_accuracy"]
    owner_acc = result["owner_accuracy"]
    print(f"  due_accuracy:   {due_acc * 100:.1f}% (n={result['n_due_scored']})"
          if due_acc is not None else "  due_accuracy:   — (n=0)")
    print(f"  owner_accuracy: {owner_acc * 100:.1f}% (n={result['n_owner_scored']})"
          if owner_acc is not None else "  owner_accuracy: — (n=0)")
    if result["stale"]:
        print(f"\nСтейл-пункти ({len(result['stale'])}), виключені з рахунку:")
        for s in result["stale"]:
            print(f"  {s['id']} (#{s['action_item_id']}): {s['reason']}")
    if result["misses"]:
        print(f"\nПромахи ({len(result['misses'])}):")
        for m in result["misses"]:
            parts = [f"  {m['id']} (#{m['action_item_id']}):"]
            if m["due_ok"] is False:
                parts.append(f"due want={m['want_due']!r}")
            if not m["owner_ok"]:
                parts.append(f"owner entity=#{m['owner_entity_id']} want={m['want_owner']!r}")
            print(" ".join(parts))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", required=True, help="Шлях до JSONL evals/build_tasks_golden.py")
    parser.add_argument("--db", default="whisper_history.db", help="Шлях до ЗНІМКА БД (owner_entity_id/entities)")
    parser.add_argument("--gate", action="store_true", help="Повернути 0/1 за порогами замість завжди 0")
    parser.add_argument("--min-due-acc", type=float, default=0.90, help="Поріг due_accuracy для --gate (default: 0.90)")
    parser.add_argument("--min-owner-acc", type=float, default=0.80, help="Поріг owner_accuracy для --gate (default: 0.80)")
    parser.add_argument("--yes-live", action="store_true",
                         help="Дозволити відкрити файл, що збігається з Config.DATABASE")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[tasks_eval] БД не знайдено: {args.db}", file=sys.stderr)
        return 2
    if is_live_db(args.db) and not args.yes_live:
        print(f"[tasks_eval] Відмова: {args.db!r} — це бойова БД (Config.DATABASE). "
              "Ця оснастка працює на ЗНІМКУ. Якщо це свідомий вибір — додайте --yes-live.",
              file=sys.stderr)
        return 2

    try:
        golden_items = read_jsonl(args.golden)
    except (OSError, ValueError) as exc:
        print(f"[tasks_eval] Golden-набір нечитний: {exc}", file=sys.stderr)
        return 2

    dupes = sorted({i for i, n in Counter(it.get("id") for it in golden_items).items() if n > 1})
    if dupes:
        print(f"[tasks_eval] Дублі id у golden-наборі (не згортаються мовчки): {dupes}", file=sys.stderr)
        return 2

    conn = open_readonly(args.db)
    try:
        result = evaluate(golden_items, conn, db_path=args.db)
    finally:
        conn.close()

    _print_report(result)

    if result["n_scored"] == 0:
        print("\n[tasks_eval] Нема пунктів зі ствердженою правдою на цьому знімку — нема на чому рахувати.",
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
