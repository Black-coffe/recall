"""Перемір звʼязків графа TG на ЗНІМКУ БД (entity-graph-tg, історія 04).

**Навіщо.** Історія 03 додала морфо-матчер (`app/services/tg_entities.py`:
`SOURCE_MORPH`, `TG_ENTITIES_MORPH_ENABLED`, дефолт вимкнено) — рішення
«вмикати чи ні» має ухвалюватись на числах, а не на враженні. `evals/run_eval.py`
міряє RAG (recall@k) і графа не бачить — це окремий модуль, не розширення
RAG-eval (`evals/metrics.py`/`run_eval.py` тут не чіпаються).

**Read-only, тільки на знімку.** Підключення в режимі `file:...?mode=ro`.
Спроба відкрити файл, що збігається з `Config.DATABASE` (бойова БД), без
`--yes-live` — відмова: знімок робить власник через `scripts/backup_db.ps1`
(`VACUUM INTO`), не цей модуль (Non-goals — тут навмисно НЕМА VACUUM).

**Два зрізи, без автоматичного гейту.** Рахуємо exact-only (як живе зараз
за замовчуванням) і exact+morph (морфо-гілка «як якби» — увімкнена лише на
час підрахунку, у БД нічого не пишеться) над ОДНИМИ й тими самими TG-
повідомленнями знімка, віддаємо дельту на сутність. Як і `evals/README.md`
(154–159) — жодного «X% просідання = fail», лише два прогони + diff руками.

**Другий шар доказу** (урок `derived-claims-need-second-source`): поруч із
кількістю звʼязків — кількість РІЗНИХ повідомлень і РІЗНИХ чатів, у яких
сутність зустрілась, щоб приріст від одного чату/однієї сутності
(як «Україні» — 25% приросту на замірі 14.08) було видно, а не сховано за
сумарним числом.

Приклади:
    python -m evals.graph_links --db path\\to\\snapshot.db
    python -m evals.graph_links --db path\\to\\snapshot.db --json-out evals/graph_links_run1.json
    python -m evals.graph_links --db path\\to\\snapshot.db --top-n 30
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Дозволяє запускати і як `python evals/graph_links.py`, і як
# `python -m evals.graph_links` без встановлення пакету (той самий трюк, що
# у evals/run_eval.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.services import entity_dedup  # noqa: E402
from app.services import tg_entities  # noqa: E402

#: Змінна оточення, якою `tg_entities._morph_enabled()`/`find_mentions`
#: вирішують, чи запускати морфо-гілку. Контракт історії 03 — не власна назва.
_MORPH_ENV = "TG_ENTITIES_MORPH_ENABLED"


# ============================================================
# Read-only доступ до знімка
# ============================================================

def _ro_uri(db_path: str) -> str:
    """URI для `sqlite3.connect(..., uri=True)` у режимі `mode=ro` — файл
    відкривається лише на читання на рівні SQLite, не лише "ми не пишемо"."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def _live_db_path() -> Optional[str]:
    """Абсолютний шлях бойової БД (`Config.DATABASE`). Окрема функція —
    тести підміняють її, не чіпаючи справжній `config.py`/файлову систему."""
    try:
        from config import Config
    except Exception:  # noqa: BLE001 — конфіг може бути недоступний у CI/venv без .env
        return None
    return str((Config.BASE_DIR / Config.DATABASE).resolve())


def is_live_db(db_path: str) -> bool:
    """Чи збігається `db_path` з бойовою БД за абсолютним шляхом."""
    live = _live_db_path()
    if live is None:
        return False
    return str(Path(db_path).resolve()) == live


def open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _scratch_copy(db_path: str) -> Iterator[str]:
    """Тимчасова копія знімка для функцій, що НЕ вміють `mode=ro` —
    `tg_entities.load_names` і `entity_dedup.find_alias_cross_type_collisions`
    відкривають власне зʼєднання через `get_db_connection`, яке ставить
    `PRAGMA journal_mode=WAL`; на знімку в режимі `delete` це перекидає
    journal_mode файлу і лишає поруч `-wal`/`-shm`. Копія приймає цей
    побічний ефект на себе — знімок, що міряємо, лишається байт-у-байт
    незмінним."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copyfile(db_path, tmp_path)
    try:
        yield tmp_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path + suffix
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass


# ============================================================
# Існуючий стан графа (як записано в знімку)
# ============================================================

def existing_graph_state(conn: sqlite3.Connection) -> dict:
    """Звʼязки графа, що вже стоять у знімку, розкладені за `source`
    (NULL = успадковано від Claude-збагачення, `thread_match`/`thread_morph`
    — цей модуль/`tg_entities.py`)."""
    rows = conn.execute(
        "SELECT COALESCE(source, 'NULL') AS src, COUNT(*) AS n, "
        "COUNT(DISTINCT entity_id) AS entities FROM meeting_entities "
        "GROUP BY 1").fetchall()
    total_entities = conn.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM meeting_entities").fetchone()[0]
    return {
        "links_by_source": {r["src"]: r["n"] for r in rows},
        "entities_by_source": {r["src"]: r["entities"] for r in rows},
        "entities_with_any_link": total_entities,
    }


def _tg_rows(conn: sqlite3.Connection) -> list[tuple[int, Optional[int], str]]:
    rows = conn.execute(
        "SELECT id, tg_chat_id, transcript_text FROM transcriptions "
        "WHERE source_type = 'telegram' AND deleted_at IS NULL").fetchall()
    return [(r["id"], r["tg_chat_id"], r["transcript_text"] or "") for r in rows]


def _entity_meta(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute("SELECT id, type, canonical_name FROM entities").fetchall()
    return {r["id"]: {"type": r["type"], "name": r["canonical_name"]} for r in rows}


def _collision_entity_ids(db_path: str) -> set[int]:
    """Сутності, залучені у кросс-типове зіткнення аліасів
    (`entity_dedup.find_alias_cross_type_collisions`) — прапорець-застереження
    в per-entity diff: приріст морфо-гілки на такій сутності вартий подвійної
    перевірки очима перш ніж вважати доказом."""
    ids: set[int] = set()
    for group in entity_dedup.find_alias_cross_type_collisions(db_path):
        if "keep" in group:
            ids.add(group["keep"]["id"])
            ids.update(m["id"] for m in group.get("merge", []))
        for m in group.get("members", []):
            ids.add(m["id"])
    return ids


# ============================================================
# Симуляція «як якби» (exact-only / exact+morph) — нічого не пишеться
# ============================================================

def _simulate(rows: list[tuple[int, Optional[int], str]], names: dict[str, int],
              *, morph: bool) -> dict[int, dict]:
    """entity_id → {"links": кількість повідомлень зі згадкою, "records":
    set(transcription_id), "chats": set(tg_chat_id)}.

    `find_mentions` вирішує, чи запускати морфо-гілку, за змінною оточення
    `TG_ENTITIES_MORPH_ENABLED` (контракт історії 03) — тимчасово виставляємо
    її на час підрахунку одного зрізу і повертаємо як було. У БД нічого не
    пишеться: рахуємо над текстом знімка, `meeting_entities` не чіпаємо.
    """
    prev = os.environ.get(_MORPH_ENV)
    os.environ[_MORPH_ENV] = "1" if morph else "0"
    try:
        out: dict[int, dict] = {}
        for tid, chat_id, text in rows:
            for eid in tg_entities.find_mentions(text, names):
                slot = out.setdefault(eid, {"links": 0, "records": set(), "chats": set()})
                slot["links"] += 1
                slot["records"].add(tid)
                if chat_id is not None:
                    slot["chats"].add(chat_id)
        return out
    finally:
        if prev is None:
            os.environ.pop(_MORPH_ENV, None)
        else:
            os.environ[_MORPH_ENV] = prev


def per_entity_diff(exact: dict[int, dict], morph: dict[int, dict],
                     entity_meta: dict[int, dict], collisions: set[int]) -> list[dict]:
    """Рядок на сутність: `+N` звʼязків морфо-гілки понад exact-only, плюс
    другий шар доказу (різні записи/чати). Сортування — стабільне
    (`-delta, -morph_links, entity_id`), щоб `--json-out` двох прогонів
    можна було diff'ити напряму."""
    empty = {"links": 0, "records": set(), "chats": set()}
    out = []
    for eid in sorted(set(exact) | set(morph)):
        ex = exact.get(eid, empty)
        mo = morph.get(eid, empty)
        meta = entity_meta.get(eid, {"type": "?", "name": f"#{eid}"})
        out.append({
            "entity_id": eid,
            "type": meta["type"],
            "name": meta["name"],
            "exact_links": ex["links"],
            "morph_links": mo["links"],
            "delta": mo["links"] - ex["links"],
            "distinct_records": len(mo["records"] | ex["records"]),
            "distinct_chats": len(mo["chats"] | ex["chats"]),
            "cross_type_collision": eid in collisions,
        })
    out.sort(key=lambda r: (-r["delta"], -r["morph_links"], r["entity_id"]))
    return out


def build_report(conn: sqlite3.Connection, db_path: str) -> dict:
    """`top_n` тут навмисно немає: JSON-артефакт несе повний per-entity diff
    (`report["diff"]`) незалежно від того, скільки рядків друкується в
    термінал — `--top-n` впливає лише на `_print_report`."""
    existing = existing_graph_state(conn)
    entity_meta = _entity_meta(conn)
    tg_rows = _tg_rows(conn)

    with _scratch_copy(db_path) as scratch:
        names = tg_entities.load_names(scratch)
        exact = _simulate(tg_rows, names, morph=False)
        morph = _simulate(tg_rows, names, morph=True)
        collisions = _collision_entity_ids(scratch)
    diff = per_entity_diff(exact, morph, entity_meta, collisions)

    exact_links = sum(v["links"] for v in exact.values())
    morph_links = sum(v["links"] for v in morph.values())

    return {
        "db": os.path.abspath(db_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "existing_graph": existing,
        "simulation": {
            "messages_total": len(tg_rows),
            "exact_only": {"total_links": exact_links, "entities_with_links": len(exact)},
            "exact_plus_morph": {"total_links": morph_links, "entities_with_links": len(morph)},
            "delta": {"links": morph_links - exact_links, "entities": len(morph) - len(exact)},
        },
        "diff": diff,
    }


def _print_report(report: dict, *, top_n: int) -> None:
    print()
    print(f"[graph_links] БД: {report['db']}")
    print("-" * 60)
    eg = report["existing_graph"]
    print("Існуючі звʼязки за source (у знімку):")
    for src, n in sorted(eg["links_by_source"].items()):
        print(f"  {src:<14} links={n:<6} entities={eg['entities_by_source'].get(src, 0)}")
    print(f"  entities_with_any_link (усі source разом): {eg['entities_with_any_link']}")
    print("-" * 60)
    sim = report["simulation"]
    print(f"Симуляція «як якби» ({sim['messages_total']} TG-повідомлень):")
    print(f"  exact-only:      links={sim['exact_only']['total_links']:<6} "
          f"entities={sim['exact_only']['entities_with_links']}")
    print(f"  exact+morph:     links={sim['exact_plus_morph']['total_links']:<6} "
          f"entities={sim['exact_plus_morph']['entities_with_links']}")
    print(f"  дельта:          links={sim['delta']['links']:+d}  entities={sim['delta']['entities']:+d}")
    print("-" * 60)
    shown = [r for r in report["diff"][:top_n] if r["delta"] != 0]
    print(f"TOP-{len(shown)} за приростом (морфо-гілка):")
    for r in shown:
        flag = " ⚠ cross-type collision" if r["cross_type_collision"] else ""
        print(f"  [{r['type']}] {r['name']}: +{r['delta']} "
              f"(exact={r['exact_links']} morph={r['morph_links']}, "
              f"records={r['distinct_records']}, chats={r['distinct_chats']}){flag}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True,
                         help="Шлях до ЗНІМКА БД (VACUUM INTO — scripts/backup_db.ps1), read-only")
    parser.add_argument("--json-out", default=None, help="Записати повний звіт у JSON-файл")
    parser.add_argument("--top-n", type=int, default=20,
                         help="Скільки сутностей показувати в TOP-за-приростом (default: 20)")
    parser.add_argument("--yes-live", action="store_true",
                         help="Дозволити відкрити файл, що збігається з Config.DATABASE "
                              "(за замовчуванням заборонено — ця оснастка для знімка)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[graph_links] БД не знайдено: {args.db}", file=sys.stderr)
        return 2

    if is_live_db(args.db) and not args.yes_live:
        print(f"[graph_links] Відмова: {args.db!r} — це бойова БД (Config.DATABASE). "
              "Ця оснастка працює на ЗНІМКУ (VACUUM INTO — scripts/backup_db.ps1), "
              "не на живому файлі. Якщо це свідомий вибір — додайте --yes-live.",
              file=sys.stderr)
        return 2

    conn = open_readonly(args.db)
    try:
        report = build_report(conn, args.db)
    finally:
        conn.close()

    _print_report(report, top_n=args.top_n)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"\n[graph_links] Повний результат записано у {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
