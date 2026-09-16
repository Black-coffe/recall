"""Дублі аудіо/YouTube/записів: хеш тексту на інжесті + офлайн-прохід (Хвиля A).

Один і той самий дзвінок потрапляє в архів двічі (перезалив файлу, повторне
завантаження відео, друга транскрипція того самого запису) — і обидві копії
далі живуть повноцінними записами: чанкуються, ембедяться і конкурують за
слоти у видачі RAG однаковим текстом.

**Що робить модуль:**

* `hash_for(source_type, text)` — sha256 нормалізованого тексту для
  `file`/`youtube`/`recording` (Telegram і документи НЕ чіпаємо: у документів
  свій дедуп на `content_hash`, у Telegram дубль — нормальне явище).
* `find_original(...)` — існуючий НЕ-дубль з тим самим хешем; інжест ставить
  новому запису `duplicate_of` і не будує для нього чанки.
* `mark_duplicates(...)` — офлайн-прохід по вже накопиченому архіву: рахує
  хеші там, де їх немає, групує, найменший id у групі лишає оригіналом, решті
  ставить `duplicate_of`. Ідемпотентний, з `--dry-run`.

Запис-дубль НЕ видаляється і не отримує `deleted_at` — він лишається в
Бібліотеці з поміткою «дубль #N», разом зі своїми коментарями, файлом і
задачами. З пошуку його прибирає `retrieval.search` (фільтр `duplicate_of IS
NULL` поруч із `deleted_at IS NULL`).

CLI:
    python -m app.services.dedup_audio mark --dry-run
    python -m app.services.dedup_audio mark
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Optional

from app.db.connection import get_db_connection
# Хеш і нормалізацію не дублюємо: у документів (16E) вони вже є і саме в цій
# парі — sha256 над текстом, який парсер уже прогнав через `_normalize_text`.
# Для аудіо нормалізувати треба явно (whisper віддає сирий текст), тому
# приватна функція імпортується як є, а не переписується поруч.
from app.services.document_parser import _normalize_text, content_hash

logger = logging.getLogger(__name__)

#: Типи джерел, які дедуплікуються. Telegram і document свідомо поза списком.
AUDIO_SOURCE_TYPES = ("file", "youtube", "recording")


def hash_for(source_type: Optional[str], text: Optional[str]) -> Optional[str]:
    """sha256 нормалізованого тексту для аудіо-джерел. None — якщо не наш тип
    або текст порожній (порожнеча не робить записи дублями одне одного)."""
    if source_type not in AUDIO_SOURCE_TYPES:
        return None
    norm = _normalize_text(text or "")
    if not norm:
        return None
    return content_hash(norm)


def find_original(conn, content_hash_value: Optional[str],
                  exclude_id: Optional[int] = None) -> Optional[dict]:
    """Найстаріший живий НЕ-дубль з тим самим хешем. None — якщо дубля немає.

    `duplicate_of IS NULL` у критерії тримає ланцюжок пласким: третя копія
    вказує на той самий оригінал, що й друга, а не на другу копію.
    """
    if not content_hash_value:
        return None
    sql = ("SELECT id, source_name FROM transcriptions "
           "WHERE content_hash = ? AND deleted_at IS NULL AND duplicate_of IS NULL "
           f"AND source_type IN ({','.join('?' * len(AUDIO_SOURCE_TYPES))})")
    params: list = [content_hash_value, *AUDIO_SOURCE_TYPES]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    sql += " ORDER BY id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ============================================================
# Офлайн-прохід
# ============================================================

def mark_duplicates(db_path: str, *, dry_run: bool = False) -> dict:
    """Позначити дублі серед уже накопичених аудіо-записів.

    Хеші рахуються на льоту (в архіві до Хвилі A колонка `content_hash` в
    аудіо порожня) і, якщо прохід не сухий, дописуються в БД — інакше інжест
    наступної копії знову нічого б не побачив.
    """
    ph = ",".join("?" * len(AUDIO_SOURCE_TYPES))
    stats = {"dry_run": dry_run, "scanned": 0, "groups": 0, "duplicates": 0,
             "hashes_written": 0, "marked": 0, "groups_detail": []}
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, source_name, source_type, content_hash, duplicate_of, "
            "transcript_text FROM transcriptions "
            f"WHERE source_type IN ({ph}) AND deleted_at IS NULL ORDER BY id",
            list(AUDIO_SOURCE_TYPES),
        ).fetchall()

        by_hash: dict[str, list[dict]] = {}
        hash_fixes: list[tuple] = []
        for r in rows:
            stats["scanned"] += 1
            h = hash_for(r["source_type"], r["transcript_text"])
            if not h:
                continue
            if r["content_hash"] != h:
                hash_fixes.append((h, r["id"]))
            by_hash.setdefault(h, []).append(
                {"id": r["id"], "source_name": r["source_name"],
                 "source_type": r["source_type"], "duplicate_of": r["duplicate_of"]})

        marks: list[tuple] = []
        for h, group in by_hash.items():
            if len(group) < 2:
                continue
            original = group[0]              # найменший id — рядки вже ORDER BY id
            dups = group[1:]
            stats["groups"] += 1
            stats["duplicates"] += len(dups)
            todo = [d for d in dups if d["duplicate_of"] != original["id"]]
            marks += [(original["id"], d["id"]) for d in todo]
            stats["groups_detail"].append({
                "hash": h[:12],
                "original": {"id": original["id"], "source_name": original["source_name"]},
                "duplicates": [{"id": d["id"], "source_name": d["source_name"]} for d in dups],
                "count": len(group),
                "to_mark": len(todo),
            })

        stats["hashes_written"] = len(hash_fixes) if not dry_run else 0
        stats["marked"] = len(marks) if not dry_run else 0
        stats["pending_hashes"] = len(hash_fixes)
        stats["pending_marks"] = len(marks)
        if not dry_run and (hash_fixes or marks):
            if hash_fixes:
                conn.executemany(
                    "UPDATE transcriptions SET content_hash = ? WHERE id = ?", hash_fixes)
            if marks:
                conn.executemany(
                    "UPDATE transcriptions SET duplicate_of = ? WHERE id = ?", marks)
            conn.commit()

    logger.info("dedup_audio.mark: scanned=%(scanned)s groups=%(groups)s "
                "duplicates=%(duplicates)s marked=%(marked)s dry_run=%(dry_run)s", stats)
    return stats


# ============================================================
# CLI
# ============================================================

def _print_report(stats: dict) -> None:
    for g in stats["groups_detail"]:
        o = g["original"]
        print(f"[{g['hash']}] оригінал #{o['id']} «{o['source_name']}» "
              f"— копій: {len(g['duplicates'])} (нових позначок: {g['to_mark']})")
        for d in g["duplicates"]:
            print(f"    дубль #{d['id']} «{d['source_name']}»")
    summary = {k: v for k, v in stats.items() if k != "groups_detail"}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _cmd(args) -> int:
    if args.command == "mark":
        stats = mark_duplicates(args.db, dry_run=args.dry_run)
    else:  # pragma: no cover
        return 2
    _print_report(stats)
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(
        prog="dedup_audio",
        description="Позначити дублі аудіо/YouTube/записів (duplicate_of).")
    p.add_argument("--db", default=default_db)
    # --dry-run має рятувати з будь-якого місця рядка: підпарсер із тим самим
    # dest переписав би батьківський namespace своїм дефолтом False, тому
    # верхній флаг іде в окремий dest, а фінальне значення — OR обох.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Нічого не писати в БД")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Нічого не писати в БД")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("mark", parents=[common],
                   help="Групи однакових текстів → duplicate_of на копіях")

    args = p.parse_args(argv)
    args.dry_run = args.dry_run or args.dry_run_pre
    return _cmd(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
