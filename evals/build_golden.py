#!/usr/bin/env python
"""Мінер golden-set із РЕАЛЬНИХ MCP-запитів (eval-gate, C1/C8).

Читає `logs/mcp_calls.log` (`RECALL_MCP_DEBUG_LOG=1`, `mcp_server.py::_CallLogMiddleware` —
формат `HH:MM:SS →   START <tool> args={json}`), витягує `query` з `search_archive` і
`question` з `ask_archive`, дедуплікує нормалізовано і для кожного унікального запиту
рахує top-5 `retrieval.search` (на ЗНІМКУ БД) → кандидатів джерел і мажоритарний зріз
(`calls`/`tg`/`docs`, C8). Кандидати — ПІДКАЗКА, не правда: кожен новий рядок виходить
`status="unlabeled"` з порожніми `expected_*`; лише людина переводить у `labeled` (A2).

`args` у логу обрізано до 600 символів (mcp_server.py) — довгі `question`/`query`
труться посеред рядкового значення. Парсер спершу пробує звичайний `json.loads`,
далі — "закрити" обрізаний рядок (`+'"}'`/`+'}'`), і лише як останній засіб —
регулярку з поля до кінця рядка (без вимоги закритої лапки).

CLI:
    python -m evals.build_golden --db evals/snapshots/x.db
    python -m evals.build_golden --db evals/snapshots/x.db --merge
    python -m evals.build_golden --db evals/snapshots/x.db --stats
    python -m evals.build_golden --db evals/snapshots/x.db --from-ask-log --stats

`--from-ask-log` додає друге джерело питань — таблицю `ask_log` (міграція v41,
UI + MCP): там є оцінка власника 👍/👎, тож 👎-питання стають кандидатами
першими, а кандидатами джерел ідуть ті записи, які модель реально процитувала.
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

# Дозволяє запускати і як `python evals/build_golden.py`, і як
# `python -m evals.build_golden` без встановлення пакету (той самий трюк, що
# у evals/run_eval.py і evals/graph_links.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.golden_io import GoldenSetError, read_jsonl  # noqa: E402
from evals.graph_links import is_live_db  # noqa: E402

#: A2: ціль розмітки на зріз (НЕ критерій прийняття коду — build_golden лише
#: рахує прогрес до неї у `--stats`).
SLICE_TARGET = 50
SLICES = ("calls", "tg", "docs")

_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2} →   START (\S+) args=(.*)$")
_TOOL_FIELD = {"search_archive": "query", "ask_archive": "question"}
_SOURCE_TO_SLICE = {"telegram": "tg", "document": "docs"}


# ============================================================
# Мінінг логу (C8)
# ============================================================

def _parse_args_json(argstr: str) -> Optional[dict]:
    """Best-effort парс JSON-хвоста рядка з поправкою на 600-символьне обрізання."""
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
    """Питання з таблиці `ask_log` знімка (міграція v41) — 👎 ПЕРШИМИ.

    Лог MCP знає лише текст питання (та й той обрізаний до 600 символів); тут
    є канал, скоуп, процитовані джерела і — головне — вердикт власника. Порядок
    не косметичний: власник розмічає десяток питань на тиждень, і перші в черзі
    мають бути ті, де система вже виміряно помилилась (👎), а не випадкові.

    Дедуп нормалізовано, як у `mine_queries`; у межах однакового питання
    виграє перша поява в цьому порядку.
    """
    out: list[dict] = []
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT question, scope_json, source_ids_json, rating, note FROM ask_log "
                # -1 → 0, решта → 1: 👎 попереду, далі 👍 і неоцінені за часом
                "ORDER BY CASE WHEN rating = -1 THEN 0 ELSE 1 END, id"
            ).fetchall()
        except sqlite3.Error as exc:
            # знімок старший за міграцію v41 — це не помилка виклику, просто
            # звідти нічого міняти (без --from-ask-log ми б сюди й не зайшли).
            print(f"[build_golden] ask_log недоступний у {db_path}: {exc}", file=sys.stderr)
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
            notes.append("👎 власника" if row["rating"] == -1 else "👍 власника")
        if (row["note"] or "").strip():
            notes.append((row["note"] or "").strip())
        out.append({"question": question, "tool": "ask_archive",
                    "category_id": cid if isinstance(cid, int) else None,
                    "source": "ask_log",
                    "source_ids": [i for i in source_ids if isinstance(i, int)],
                    "notes": " · ".join(notes)})
    return out


def candidates_from_ids(db_path: str, source_ids: list[int]) -> tuple[list[dict], str]:
    """Кандидати з процитованих джерел рядка `ask_log` (без `retrieval.search`).

    Це та сама ПІДКАЗКА, що й топ-5 пошуку: те, що модель процитувала, не
    доводить правильності — доводить лише людина, переводячи пункт у `labeled`.
    Але підказка сильніша: ці джерела вже пройшли крізь відповідь."""
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
    """Унікальні запити з логу (у порядку першої появи, дедуп нормалізовано).

    Повертає ``[{"question", "tool", "category_id"}, ...]`` — це ще НЕ golden-
    пункти, лише сирі запити (candidates/slice рахуються окремо, лише для тих,
    що дійсно підуть у вихідний файл, щоб не платити retrieval за дублі)."""
    seen: set[str] = set()
    out: list[dict] = []
    if not os.path.exists(log_path):
        return out
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
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"question": text, "tool": tool,
                        "category_id": _category_id_from_args(argstr)})
    return out


# ============================================================
# Кандидати + зріз (retrieval.search на ЗНІМКУ, ADR-003)
# ============================================================

@contextlib.contextmanager
def _scratch_copy(db_path: str):
    """Тимчасова копія знімка — `retrieval.search` іде через `get_db_connection`
    (`PRAGMA journal_mode=WAL`), що мутує файл лише відкриттям (ADR-003:
    оснастка заміру не мутує знімок, який міряє). Копія приймає побічний
    ефект на себе, знімок лишається байт-у-байт незмінним. Свідома копія
    патерну `evals/graph_links.py::_scratch_copy` (не імпортуємо приватне
    зі сусіднього модуля — та сама причина, що `commitments._normalize_name`
    свідомо дублює 6 рядків замість імпорту з `enrichment`)."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # copyfile лишається ВСЕРЕДИНІ try — інакше збій копіювання (диск повний,
        # права) лишає порожню заглушку від mkstemp на диску назавжди (finding 20).
        shutil.copyfile(db_path, tmp_path)
        yield tmp_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path + suffix
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError as exc:
                    # не ковтаємо мовчки (CLAUDE.md: no bare except without a log) —
                    # це лише прибирання тимчасового файлу, не критична помилка.
                    print(f"[build_golden] не вдалося прибрати тимчасовий файл "
                          f"{candidate}: {exc}", file=sys.stderr)


def _slice_for_chunks(chunks: list[dict]) -> str:
    """Мажоритарний зріз за `source_type` кандидатів. `comment` (провенанс
    коментаря, `app/services/retrieval.py`) не голосує — інакше питання,
    відповідь на яке взято здебільшого з коментарів, хибно осідає в `calls`
    (finding 22)."""
    votes = [ch for ch in chunks if ch.get("source_type") != "comment"]
    if not votes:
        return "calls"
    counts = Counter(_SOURCE_TO_SLICE.get(ch.get("source_type"), "calls") for ch in votes)
    return counts.most_common(1)[0][0]


def candidates_for(db_path: str, question: str, top_k: int = 5) -> tuple[list[dict], str]:
    """Топ-``top_k`` `retrieval.search` → кандидати (id/назва/тип джерела) + зріз."""
    from app.services import retrieval

    res = retrieval.search(db_path, question, top_k=top_k)
    chunks = res.get("chunks") or []
    candidates = [{"transcription_id": ch.get("transcription_id"),
                   "source_name": ch.get("source_name"),
                   "source_type": ch.get("source_type")} for ch in chunks]
    return candidates, _slice_for_chunks(chunks)


# ============================================================
# JSONL (читання — спільний `evals.golden_io.read_jsonl`, D13; запис лишається
# тут, бо власного контракту помилок не потребує)
# ============================================================


def _write_jsonl(path: str, items: list[dict]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


_ID_RE = re.compile(r"^mined-(\d+)$")


def _next_start_idx(existing: list[dict]) -> int:
    """Наступний вільний числовий хвіст `mined-NNNN` серед `existing`.

    `len(existing) + 1` дублює id, якщо рядок посередині видалили вручну —
    новий пункт з тим самим номером тихо зіллється зі старим (finding 16)."""
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
    for it in items:
        s = it.get("slice") if it.get("slice") in SLICES else "calls"
        table[s][it.get("status") or "unlabeled"] += 1
    print(f"{'slice':<8}{'labeled':>10}{'unlabeled':>12}{'negative':>10}{'ціль':>8}{'дефіцит':>10}")
    for s in SLICES:
        c = table[s]
        labeled = c.get("labeled", 0)
        deficit = max(0, SLICE_TARGET - labeled)
        print(f"{s:<8}{labeled:>10}{c.get('unlabeled', 0):>12}{c.get('negative', 0):>10}"
              f"{SLICE_TARGET:>8}{deficit:>10}")


# ============================================================
# CLI
# ============================================================

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mcp-log", default=str(_PROJECT_ROOT / "logs" / "mcp_calls.log"),
                         help="Шлях до логу викликів MCP (default: logs/mcp_calls.log)")
    parser.add_argument("--db", default="whisper_history.db", help="Шлях до ЗНІМКА БД для retrieval.search")
    parser.add_argument("--out", default="evals/golden_candidates.local.jsonl",
                         help="Куди писати JSONL (default: evals/golden_candidates.local.jsonl)")
    parser.add_argument("--merge", action="store_true",
                         help="Зберегти labeled/negative пункти з наявного --out, додати лише нові unlabeled "
                              "(без цього прапорця --out ПЕРЕЗАПИСУЄТЬСЯ свіжим виводом з логу)")
    parser.add_argument("--yes-overwrite-labeled", action="store_true",
                         help="Свідомо дозволити перезапис --out без --merge, коли в ньому вже є "
                              "розмічені (labeled/negative) пункти. Без --merge і без цього прапорця "
                              "такий --out НЕ перезаписується — ручна розмітка незамінна.")
    parser.add_argument("--from-ask-log", action="store_true",
                         help="Додати питання з таблиці ask_log знімка (UI+MCP, з оцінкою "
                              "власника): 👎 першими, кандидати — процитовані джерела")
    parser.add_argument("--stats", action="store_true", help="Надрукувати таблицю slice×status і дефіцит до цілі")
    parser.add_argument("--top-k", type=int, default=5, help="Скільки кандидатів на запит (default: 5)")
    parser.add_argument("--yes-live", action="store_true",
                         help="Дозволити відкрити файл, що збігається з Config.DATABASE")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[build_golden] БД не знайдено: {args.db}", file=sys.stderr)
        return 2
    if is_live_db(args.db) and not args.yes_live:
        print(f"[build_golden] Відмова: {args.db!r} — це бойова БД (Config.DATABASE). "
              "Ця оснастка працює на ЗНІМКУ. Якщо це свідомий вибір — додайте --yes-live.",
              file=sys.stderr)
        return 2

    # Наявний --out читаємо ОДИН раз, незалежно від --merge: потрібен і для
    # злиття, і для підрахунку розміченого перед відмовою в перезаписі.
    # Невалідний JSONL — exit 2 з повідомленням, а не трейсбек (D3).
    existing: list[dict] = []
    if os.path.exists(args.out):
        try:
            prior = read_jsonl(args.out)
        except GoldenSetError as exc:
            print(f"[build_golden] {args.out}: {exc}", file=sys.stderr)
            return 2
        if args.merge:
            existing = prior
        else:
            lost = sum(1 for it in prior if it.get("status") in ("labeled", "negative"))
            if lost and not args.yes_overwrite_labeled:
                print(f"[build_golden] Відмова: {args.out} містить {lost} розмічених "
                      "(labeled/negative) пунктів і без --merge буде перезаписаний. "
                      "Додайте --merge, щоб зберегти їх, або --yes-overwrite-labeled, "
                      "щоб свідомо перезаписати й втратити їх.", file=sys.stderr)
                return 2
            if lost:
                print(f"[build_golden] УВАГА: {args.out} перезаписується з "
                      f"--yes-overwrite-labeled — {lost} розмічених пунктів буде втрачено.",
                      file=sys.stderr)

    mined = mine_queries(args.mcp_log)
    if args.from_ask_log:
        # Читаємо теж із КОПІЇ знімка: `sqlite3.connect` на WAL-файл лишає по
        # собі -wal/-shm, а знімок має лишитись байт-у-байт (ADR-003).
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

    new_items: list[dict] = []
    if new_queries:
        with _scratch_copy(args.db) as scratch:
            next_idx = _next_start_idx(existing)
            for q in new_queries:
                if q.get("source") == "ask_log":
                    # джерела вже процитовані у відповіді — другий пошук по тому
                    # самому питанню нічого не додає, лише платить часом
                    candidates, slice_ = candidates_from_ids(scratch, q.get("source_ids") or [])
                else:
                    candidates, slice_ = candidates_for(scratch, q["question"], top_k=args.top_k)
                new_items.append(_make_item(next_idx, q, candidates, slice_))
                next_idx += 1

    items = existing + new_items
    dup_ids = sorted({it_id for it_id, count in Counter(it.get("id") for it in items).items()
                       if count > 1})
    if dup_ids:
        print(f"[build_golden] Відмова: дублі id у {args.out} ({len(dup_ids)}): "
              f"{dup_ids[:5]}{' …' if len(dup_ids) > 5 else ''} — файл НЕ записано.",
              file=sys.stderr)
        return 2

    _write_jsonl(args.out, items)
    print(f"[build_golden] {args.out}: збережено={len(existing)} нових={len(new_items)} усього={len(items)}")

    if args.stats:
        print_stats(items)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
