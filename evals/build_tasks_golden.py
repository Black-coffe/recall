#!/usr/bin/env python
"""Чернетка golden-set для action-items (eval-gate, C7) — під ручну правду.

Бере СТРАТИФІКОВАНУ вибірку `action_items` зі знімка БД — за (`source`
IS NULL / `'tg_thread'`) × (чи є сира фраза дедлайну `due`) — і кладе кожен
рядок як `status="unlabeled"` з порожнім `truth`. `commitments.tg_tasks`
(SOURCE="tg_thread") — це задачі, витягнуті з переписки; NULL — успадковані
з Claude-карток дзвінків. `action_items.source='comment'` (провенанс
коментарів власника) СВІДОМО і ПОСТІЙНО не потрапляє у вибірку (D14,
знахідка 24 review-round-1.md) — буквальне читання C7 (`"source":
null|"tg_thread"`), не тимчасовий недогляд; розширювати на 'comment' —
окреме рішення власника, не мовчазний side-effect цього файлу.

Кожен вибраний рядок несе `db_snapshot` — груба ознака ідентичності знімка
(імʼя файлу + розмір), з якого його взято (D7): `evals/tasks_eval.py`
звіряє її з `--db`, яким міряє, і виключає пункт зі рахунку, якщо вони не
збігаються — реінджест перестворює `action_items`, і без цього
`owner_accuracy`/`due_accuracy` тихо стають беззмістовними (знахідка 11).

Ані `due_date`, ані `owner_entity_id` тут не рахуються і не звіряються —
це робить `evals/tasks_eval.py` окремо, на будь-якому знімку.

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
from evals.golden_io import read_jsonl  # noqa: E402 — спільний читач (D13), пишеться історією 05

#: A2-аналог для action-items (C7): ціль розмітки, не критерій прийняття коду.
TASKS_TARGET = 30

#: (source, чи є due-фраза) — чотири страти, рівний розподіл.
_STRATA = [(None, True), (None, False), ("tg_thread", True), ("tg_thread", False)]


# ============================================================
# Стратифікована вибірка (read-only — ADR-003, не мутуємо знімок)
# ============================================================

def sample_action_items(conn, n: int, *, seed: int = 0) -> list[dict]:
    """Стратифікована вибірка `action_items` зі знімка. Детермінована
    (`seed=0`) — той самий знімок дає той самий набір при повторному прогоні."""
    rows = conn.execute(
        # C7 несе лише "source": null|"tg_thread" — 'comment' (провенанс
        # коментарів власника) виключений СВІДОМО і ПОСТІЙНО (D14), не
        # переглядати цей WHERE без окремого рішення власника.
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
    """Груба ознака ідентичності знімка (D7): базове імʼя файлу + розмір.
    Не хеш (315 МБ на кожен прогін — задорого) — та сама копія, що
    `evals/tasks_eval.py` (не тягнемо приватну назву сусіднього модуля через
    межу файлів)."""
    st = os.stat(db_path)
    return {"path": os.path.basename(db_path), "size": st.st_size}


# ============================================================
# JSONL (читає evals.golden_io.read_jsonl — D13; пише свій запис, бо
# golden_io пише лише C1-набір retrieval-гейта, не C7)
# ============================================================

def _existing_items(path: str) -> list[dict]:
    """Наявний `--out`, якщо він є (перший прогін — файлу ще нема, це не
    помилка). Нечитний/невалідний файл — `read_jsonl` кидає, `main` мапить
    у exit 2 (D3)."""
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


#: `task-0001`, ... — той самий формат, що видає `_make_item`.
_ID_RE = re.compile(r"^task-(\d+)$")


def _next_index(existing: list[dict]) -> int:
    """Наступний вільний номер id — НЕ `len(existing) + 1` (знахідка 16):
    ручне видалення одного рядка з файлу зсуває довжину, і `len+1` реіснує
    номер, який усе ще належить ІНШОМУ пункту нижче в списку. Максимум
    зайнятого номера + 1 такого зіткнення дати не може — множина зайнятих
    номерів лише росте."""
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
    print(f"[build_tasks_golden] labeled={labeled} ціль={TASKS_TARGET} "
          f"дефіцит={deficit} (усього={len(items)})")


# ============================================================
# CLI
# ============================================================

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="whisper_history.db", help="Шлях до ЗНІМКА БД")
    parser.add_argument("--n", type=int, default=60, help="Скільки задач вибрати сумарно (default: 60)")
    parser.add_argument("--out", default="evals/tasks_golden.local.jsonl",
                         help="Куди писати JSONL (default: evals/tasks_golden.local.jsonl)")
    parser.add_argument("--merge", action="store_true",
                         help="Додати лише нові action_item_id до наявного --out "
                              "(без цього прапорця --out ПЕРЕЗАПИСУЄТЬСЯ свіжою вибіркою)")
    parser.add_argument("--stats", action="store_true", help="Надрукувати labeled проти цілі")
    parser.add_argument("--seed", type=int, default=0, help="Насіння детермінованої вибірки (default: 0)")
    parser.add_argument("--yes-live", action="store_true",
                         help="Дозволити відкрити файл, що збігається з Config.DATABASE")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[build_tasks_golden] БД не знайдено: {args.db}", file=sys.stderr)
        return 2
    if is_live_db(args.db) and not args.yes_live:
        print(f"[build_tasks_golden] Відмова: {args.db!r} — це бойова БД (Config.DATABASE). "
              "Ця оснастка працює на ЗНІМКУ. Якщо це свідомий вибір — додайте --yes-live.",
              file=sys.stderr)
        return 2

    try:
        existing = _existing_items(args.out) if args.merge else []
        if not args.merge and os.path.exists(args.out):
            prior = _existing_items(args.out)
            lost = sum(1 for it in prior if it.get("status") == "labeled")
            if lost:
                print(f"[build_tasks_golden] УВАГА: {args.out} перезаписується без --merge — "
                      f"{lost} розмічених пунктів буде втрачено. Додайте --merge, щоб зберегти їх.",
                      file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"[build_tasks_golden] {args.out} нечитний: {exc}", file=sys.stderr)
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

    # Дублі id не згортаються мовчки (знахідка 16) — за побудовою `_next_index`
    # їх тут бути не повинно; це остання лінія оборони проти пошкодженого
    # `--out`, який хтось редагував руками.
    dupe_ids = sorted({i for i, n in Counter(it.get("id") for it in items).items() if n > 1})
    if dupe_ids:
        print(f"[build_tasks_golden] Дублі id у {args.out} (не згортаються мовчки): {dupe_ids}", file=sys.stderr)
        return 2

    _write_jsonl(args.out, items)
    print(f"[build_tasks_golden] {args.out}: збережено={len(existing)} нових={len(new_items)} усього={len(items)}")

    if args.stats:
        print_stats(items)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
