#!/usr/bin/env python
"""Двостороння звірка медіа-файлів проти БД (dry-run за замовчуванням).

Пряма звірка (диск → БД):
    Файли в медіа-теках, на які НЕ посилається жоден рядок
    ``audio_downloads`` чи ``transcriptions`` (за абсолютним шляхом АБО
    імʼям файлу) → осиротілі (кандидати на видалення).

Зворотна звірка (БД → диск):
    Рядки БД, чий ``file_path`` вказує на відсутній на диску файл.
    - ``audio_downloads``: мертві каталожні рядки Аудіотеки — можна прибрати
      (файлу нема, запис не грається/не транскрибується).
    - ``transcriptions``: НЕ видаляємо НІКОЛИ — текст, сутності та RAG-чанки
      цінні; відсутнє оригінальне аудіо старих завантажень — це норма.
      Лише попередження.

За замовчуванням скрипт ЛИШЕ ЗВІТУЄ (dry-run). Прапор ``--apply`` виконує
чистку: видаляє осиротілі файли + мертві ``audio_downloads``-рядки. Транскрипти
не чіпає за жодних умов.

Приклади:
    .venv/Scripts/python.exe scripts/reconcile_media.py            # dry-run
    .venv/Scripts/python.exe scripts/reconcile_media.py --apply    # чистка
    .venv/Scripts/python.exe scripts/reconcile_media.py --dirs youtube_downloads uploads
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Теки з НАДІЙНИМ трекінгом file_path — лише їх скануємо за замовчуванням:
#   youtube_downloads → audio_downloads.file_path (повний);
#   uploads           → transcriptions.file_path (source_type='file', повний).
#
# СВІДОМО ВИКЛЮЧЕНО з дефолту (file_path трекається ненадійно → пряма звірка
# дала б хибні «сироти», --apply видалив би потрібне):
#   documents/      — Phase 16: текст у архіві, але file_path НЕ зберігається
#                     (0 рядків) → усі PDF/DOCX виглядали б як сироти;
#   telegram_media/ — лише частина медіа має file_path (текстові TG-повідомлення
#                     без файлів) → багато хибних спрацювань;
#   recordings/     — власний життєвий цикл (не-фіналізовані сесії, finalize сам
#                     чистить проміжні WAV).
# Їх можна передати явно через --dirs, але ЛИШЕ для звіту й з ручною перевіркою.
DEFAULT_DIRS = ['youtube_downloads', 'uploads']
# Теки, для яких --apply НЕБЕЗПЕЧНИЙ (немає надійного трекінгу file_path).
UNSAFE_FOR_APPLY = {'documents', 'telegram_media', 'recordings'}


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def gather_referenced(db_path: str) -> tuple[set[str], set[str]]:
    """Усі file_path з audio_downloads + transcriptions → (abs-шляхи, basenames)."""
    abs_set: set[str] = set()
    base_set: set[str] = set()
    con = sqlite3.connect(db_path)
    try:
        for tbl in ('audio_downloads', 'transcriptions'):
            for (fp,) in con.execute(
                f'SELECT file_path FROM {tbl} WHERE file_path IS NOT NULL AND file_path != ""'
            ):
                ap = _norm(fp)
                abs_set.add(ap)
                base_set.add(os.path.basename(ap))
    finally:
        con.close()
    return abs_set, base_set


def scan_dirs(dirs: list[Path]) -> list[tuple[str, int]]:
    """Усі файли в заданих теках → [(абсолютний шлях, розмір)]."""
    found: list[tuple[str, int]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    found.append((p, os.path.getsize(p)))
                except OSError:
                    pass
    return found


def find_orphans(disk: list[tuple[str, int]], abs_set: set[str], base_set: set[str]):
    """Файли на диску, на які БД не посилається (ні шляхом, ні іменем)."""
    orphans = []
    for p, sz in disk:
        ap = _norm(p)
        if ap in abs_set or os.path.basename(ap) in base_set:
            continue
        orphans.append((p, sz))
    return orphans


def find_broken(db_path: str, table: str):
    """Рядки table з file_path, що вказує на відсутній файл."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f'SELECT id, file_path, source_type FROM {table} '
            f'WHERE file_path IS NOT NULL AND file_path != ""'
        ).fetchall()
    finally:
        con.close()
    return [r for r in rows if not os.path.isfile(r['file_path'])]


def _mb(n: int) -> str:
    return f'{n / 1048576:.1f} MB'


def _gb(n: int) -> str:
    return f'{n / 1073741824:.2f} GB'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Двостороння звірка медіа проти БД.')
    ap.add_argument('--db', default=str(PROJECT_ROOT / 'whisper_history.db'),
                    help='Шлях до SQLite (default: whisper_history.db у корені).')
    ap.add_argument('--dirs', nargs='+', default=DEFAULT_DIRS,
                    help=f'Медіа-теки для прямої звірки (default: {" ".join(DEFAULT_DIRS)}).')
    ap.add_argument('--apply', action='store_true',
                    help='ВИКОНАТИ чистку (видалити осиротілі файли + мертві '
                         'audio_downloads-рядки). Без прапора — лише звіт (dry-run).')
    ap.add_argument('--limit-examples', type=int, default=15,
                    help='Скільки прикладів показувати у звіті (default: 15).')
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f'❌ БД не знайдено: {args.db}', file=sys.stderr)
        return 2

    dirs = [(PROJECT_ROOT / d) if not os.path.isabs(d) else Path(d) for d in args.dirs]
    mode = 'APPLY (чистка)' if args.apply else 'DRY-RUN (лише звіт)'
    print(f'=== Звірка медіа проти БД — {mode} ===')
    print(f'БД: {args.db}')
    print(f'Теки: {", ".join(str(d) for d in dirs)}\n')

    abs_set, base_set = gather_referenced(args.db)

    # --- Пряма: осиротілі файли ----------------------------------------------
    disk = scan_dirs(dirs)
    orphans = find_orphans(disk, abs_set, base_set)
    disk_total = sum(s for _, s in disk)
    orphan_total = sum(s for _, s in orphans)
    print('─ ПРЯМА (диск → БД): осиротілі файли')
    print(f'  на диску: {len(disk)} файлів, {_gb(disk_total)}')
    print(f'  осиротілі: {len(orphans)} файлів, {_gb(orphan_total)}')
    for p, sz in sorted(orphans, key=lambda x: -x[1])[:args.limit_examples]:
        print(f'    {_mb(sz):>10}  {os.path.basename(p)}')
    if len(orphans) > args.limit_examples:
        print(f'    … ще {len(orphans) - args.limit_examples}')

    # --- Зворотна: биті посилання --------------------------------------------
    broken_audio = find_broken(args.db, 'audio_downloads')
    broken_tr = find_broken(args.db, 'transcriptions')
    print('\n─ ЗВОРОТНА (БД → диск): биті посилання')
    print(f'  audio_downloads (Аудіотека): {len(broken_audio)} мертвих рядків (можна прибрати)')
    for r in broken_audio[:args.limit_examples]:
        print(f'    id={r["id"]} [{r["source_type"]}] {os.path.basename(r["file_path"])}')
    print(f'  transcriptions (Історія): {len(broken_tr)} битих — ЗБЕРІГАЄМО '
          f'(текст+RAG цінні, аудіо відсутнє = норма)')

    # --- Дія (тільки з --apply) ----------------------------------------------
    if not args.apply:
        would = orphan_total
        print(f'\n[dry-run] Видалилось би: {len(orphans)} файлів ({_gb(would)}) '
              f'+ {len(broken_audio)} рядків Аудіотеки.')
        print('Запусти з --apply, щоб виконати. Транскрипти не чіпаються ніколи.')
        return 0

    # Захист: не видаляємо файли з тек, де трекінг file_path ненадійний
    # (documents/telegram_media/recordings) — навіть якщо їх передали через --dirs.
    def _is_unsafe(path: str) -> bool:
        ap = _norm(path)
        return any(_norm(str(PROJECT_ROOT / u)) in ap for u in UNSAFE_FOR_APPLY)

    freed = 0
    removed_files = 0
    skipped_unsafe = 0
    for p, sz in orphans:
        if _is_unsafe(p):
            skipped_unsafe += 1
            continue
        if os.path.isfile(p):  # повторна перевірка перед видаленням
            try:
                os.remove(p)
                freed += sz
                removed_files += 1
            except OSError as e:
                print(f'    ⚠ не видалено {p}: {e}')
    if skipped_unsafe:
        print(f'    ⚠ пропущено {skipped_unsafe} файлів у ненадійних теках '
              f'({", ".join(sorted(UNSAFE_FOR_APPLY))}) — file_path трекається '
              f'неповно, видалення небезпечне.')

    removed_rows = 0
    if broken_audio:
        con = sqlite3.connect(args.db)
        try:
            cur = con.cursor()
            for r in broken_audio:
                # safety: видаляємо рядок лише якщо файл СПРАВДІ відсутній
                if not os.path.isfile(r['file_path']):
                    cur.execute('DELETE FROM audio_downloads WHERE id = ?', (r['id'],))
                    removed_rows += 1
            con.commit()
        finally:
            con.close()

    print(f'\n✅ Видалено {removed_files} осиротілих файлів ({_gb(freed)}) '
          f'+ {removed_rows} мертвих рядків Аудіотеки.')
    print(f'   Транскрипти ({len(broken_tr)} битих) збережено.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
