#!/usr/bin/env python
"""Ремонт шляхів до медіа після переїзду/перейменування кореня проєкту.

Коли корінь проєкту переїжджає (як `E:\\Projects\\Whisper` → `E:\\Projects\\Recall`
у 2026), у БД лишаються АБСОЛЮТНІ шляхи на старе місце, а на диску — манифести
сесій зі старим коренем усередині. Усі споживачі гейтяться `os.path.isfile(...)`
і **мовчки** відвалюються: «оригінальний файл» не віддається, re-transcribe б'ється
об мертвий шлях, програвання не працює. Транскрипт при цьому цілий — тому поломку
не видно ніде, крім порожньої кнопки.

Boot-reconcile (`app/services/recording/reconcile.py`) лікує лише
`source_type='recording'`; рядки youtube/file не покриті ніким — саме вони й
простоюють роками. Цей скрипт закриває решту.

ЯК ЧИНИТЬ (єдине правило, детерміноване):
    шлях розбирається на компоненти, шукається перша медіа-тека проєкту
    (`uploads`, `youtube_downloads`, `recordings`, …), і хвіст від неї
    перебудовується від ПОТОЧНОГО кореня проєкту. Заміна приймається, лише якщо
    отриманий файл РЕАЛЬНО існує. Старий корінь ніде не зашитий — працює для
    будь-якого переїзду, включно зі зміною букви диска.

ЧОГО НЕ РОБИТЬ СВІДОМО — пошуку за іменем файлу. У `recordings/sessions/*/final.mp3`
всі файли звуться однаково (141 штука), тож збіг за basename підставив би
транскрипту ЧУЖУ сесію. Що не зійшлось підміною кореня — іде у звіт, руками.

За замовчуванням DRY-RUN. `--apply` пише в БД (з авто-бекапом у `db_backups/`)
і переписує манифести (оригінал поруч, `.bak-pathfix`).

Приклади:
    .venv/Scripts/python.exe scripts/repair_paths.py                # звіт
    .venv/Scripts/python.exe scripts/repair_paths.py --apply        # ремонт
    .venv/Scripts/python.exe scripts/repair_paths.py --apply --no-backup
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# (таблиця, колонка) — усі місця, де зберігається шлях до файлу на диску
TARGETS = [
    ('transcriptions', 'file_path'),
    ('audio_downloads', 'file_path'),
    ('audio_downloads', 'primary_video_path'),
    ('recording_video_tracks', 'file_path'),
    ('video_keyframes', 'image_path'),
]

# медіа-теки проєкту — точки, від яких перебудовується хвіст шляху
MEDIA_DIRS = ('uploads', 'youtube_downloads', 'recordings', 'telegram_media',
              'documents', 'transcripts', 'downloads')

_SPLIT = re.compile(r'[\\/]+')


def resolve(fp: str) -> str:
    """Абсолютний шлях так, як його побачить застосунок (cwd = корінь проєкту)."""
    return fp if os.path.isabs(fp) else str(PROJECT_ROOT / fp)


def rebuild(fp: str) -> str | None:
    """Перебудувати шлях від поточного кореня. None — якщо файл так і не знайдено."""
    parts = [p for p in _SPLIT.split(fp) if p]
    for i, part in enumerate(parts):
        if part.lower() not in MEDIA_DIRS:
            continue
        cand = str(PROJECT_ROOT.joinpath(*parts[i:]))
        if os.path.isfile(cand):
            return cand
    return None


def _under_root(fp: str) -> bool:
    """Чи вказує шлях усередину поточного кореня проєкту."""
    try:
        return os.path.normcase(os.path.abspath(resolve(fp))).startswith(
            os.path.normcase(str(PROJECT_ROOT)) + os.sep)
    except (OSError, ValueError):
        return False


def plan(fp: str) -> tuple[str, str | None]:
    """-> ('ok' | 'fix' | 'lost', новий_шлях | None)"""
    if os.path.isfile(resolve(fp)):
        return 'ok', None
    new = rebuild(fp)
    return ('fix', new) if new else ('lost', None)


# --------------------------------------------------------------------------- БД

def backup_db(db_path: str) -> str:
    """Консистентний знімок БД засобами SQLite (не копія файлу — є WAL)."""
    out_dir = PROJECT_ROOT / 'db_backups'
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst_path = out_dir / f'whisper_history_pre_path_repair_{stamp}.db'
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
        state = dst.execute('PRAGMA integrity_check').fetchone()[0]
        if state != 'ok':
            raise RuntimeError(f'бекап не пройшов integrity_check: {state}')
    finally:
        dst.close()
        src.close()
    return str(dst_path)


def repair_db(db_path: str, apply: bool, examples: int) -> int:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    updates: list[tuple[str, str, int, str, str]] = []
    lost: list[tuple[str, int, str]] = []
    grand = Counter()

    for table, col in TARGETS:
        try:
            rows = con.execute(
                f'SELECT id, {col} AS fp FROM {table} '
                f'WHERE {col} IS NOT NULL AND {col} != ""'
            ).fetchall()
        except sqlite3.OperationalError as e:
            print(f'  {table}.{col}: пропущено ({e})')
            continue
        stats = Counter()
        for r in rows:
            status, new = plan(r['fp'])
            stats[status] += 1
            grand[status] += 1
            if status == 'fix':
                updates.append((table, col, r['id'], r['fp'], new))
            elif status == 'lost':
                lost.append((table, r['id'], r['fp']))
        print(f'  {table:<24}{col:<20}всього={len(rows):<6}{dict(stats)}')

    print(f'\n  ПІДСУМОК: {dict(grand)}')

    for t, c, i, old, new in updates[:examples]:
        print(f'\n  {t}#{i}\n     було:  {old}\n     стане: {new}')
    if len(updates) > examples:
        print(f'\n  … ще {len(updates) - examples} шляхів')

    if lost:
        print(f'\n  ВТРАЧЕНІ (файла нема на диску): {len(lost)} — НЕ чіпаємо, '
              f'транскрипт цінний сам по собі')
        by_dir = Counter()
        for t, _i, fp in lost:
            by_dir[(t, os.path.basename(os.path.dirname(fp)) or '?')] += 1
        for (t, parent), n in by_dir.most_common(10):
            print(f'    {t:<24}тека {parent:<22}{n}')

    if not apply or not updates:
        con.close()
        return len(updates)

    cur = con.cursor()
    written = 0
    for t, c, i, _old, new in updates:
        if os.path.isfile(new):  # повторна перевірка перед записом
            cur.execute(f'UPDATE {t} SET {c} = ? WHERE id = ?', (new, i))
            written += cur.rowcount
    con.commit()
    print(f'\n  ОНОВЛЕНО рядків: {written}')
    print(f'  integrity_check: {con.execute("PRAGMA integrity_check").fetchone()[0]}')
    con.close()
    return written


# -------------------------------------------------------------------- манифести

def repair_json(dirs: list[str], apply: bool) -> int:
    """Перебудувати шляхи всередині json-манифестів сесій.

    Манифест лікується live у `SessionStore.read`, але НА ДИСКУ лишається старим —
    тож будь-який інший споживач манифеста впирається в мертвий шлях.
    """
    files: list[Path] = []
    for d in dirs:
        base = Path(d) if os.path.isabs(d) else PROJECT_ROOT / d
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files.extend(base.rglob('*.json'))

    changed: list[tuple[Path, str]] = []
    fixed = lost = unmatched = absent = 0
    for f in files:
        try:
            txt = f.read_text(encoding='utf-8')
            data = json.loads(txt)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f'  ! не прочитано {f}: {e}')
            continue

        new_txt = txt
        for old in sorted(_iter_paths(data), key=len, reverse=True):
            if os.path.isfile(old):
                continue
            new = rebuild(old)
            if not new:
                # Шлях уже під поточним коренем — ремонтувати нічого, файлу
                # просто нема (finalize штатно прибирає проміжні WAV після
                # мікшування). Це НЕ поломка шляху, тому окремий лічильник:
                # інакше звіт після кожного переїзду лякає сотнями «втрачених».
                if _under_root(old):
                    absent += 1
                else:
                    lost += 1
                continue
            # У json-тексті слеші екрановані, тож міняємо в json-представленні.
            # Кирилиця може бути записана і літерально (ensure_ascii=False, як
            # пише session_store), і як \uXXXX — пробуємо обидва варіанти й
            # відповідь підставляємо ТИМ САМИМ стилем, щоб не змішати їх у файлі.
            for ascii_only in (False, True):
                a = json.dumps(old, ensure_ascii=ascii_only)[1:-1]
                if a in new_txt:
                    b = json.dumps(new, ensure_ascii=ascii_only)[1:-1]
                    new_txt = new_txt.replace(a, b)
                    fixed += 1
                    break
            else:
                # шлях є в структурі, але не знайшовся в тексті — мовчки
                # пропустити не можна, інакше ремонт «успішний» і неповний
                unmatched += 1
                print(f'  ! не знайдено в тексті {f.name}: {old}')
        if new_txt != txt:
            json.loads(new_txt)  # перевірка, що json лишився валідним
            changed.append((f, new_txt))

    print(f'  переглянуто json: {len(files)}')
    print(f'  файлів зі старими шляхами: {len(changed)} '
          f'(шляхів перебудовано: {fixed}, зі старим коренем не знайдено: {lost})')
    if absent:
        print(f'  довідково: {absent} шляхів уже під поточним коренем, але файлу '
              f'нема — це не поломка (finalize прибирає проміжні WAV)')
    if unmatched:
        print(f'  УВАГА: {unmatched} шляхів не вдалося знайти в тексті файлу — '
              f'перевір вручну, ремонт неповний')

    if not apply or not changed:
        return len(changed)

    for f, new_txt in changed:
        shutil.copy2(f, f.with_suffix(f.suffix + '.bak-pathfix'))
        f.write_text(new_txt, encoding='utf-8')
    print(f'  ПЕРЕПИСАНО файлів: {len(changed)} (оригінали поруч, .bak-pathfix)')
    return len(changed)


def _iter_paths(node):
    """Усі рядки-значення, схожі на шлях до файлу."""
    if isinstance(node, dict):
        for v in node.values():
            yield from _iter_paths(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_paths(v)
    elif isinstance(node, str) and ('\\' in node or '/' in node) and os.path.splitext(node)[1]:
        yield node


# --------------------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Ремонт шляхів до медіа після переїзду кореня.')
    ap.add_argument('--db', default=str(PROJECT_ROOT / 'whisper_history.db'))
    ap.add_argument('--json-dirs', nargs='+', default=['recordings'],
                    help='Де шукати json-манифести (default: recordings).')
    ap.add_argument('--apply', action='store_true',
                    help='ВИКОНАТИ ремонт. Без прапора — лише звіт (dry-run).')
    ap.add_argument('--no-backup', action='store_true',
                    help='Не робити авто-бекап БД перед записом (не рекомендується).')
    ap.add_argument('--skip-json', action='store_true', help='Не чіпати манифести.')
    ap.add_argument('--limit-examples', type=int, default=5)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f'БД не знайдено: {args.db}', file=sys.stderr)
        return 2

    mode = 'APPLY (запис)' if args.apply else 'DRY-RUN (лише звіт)'
    print(f'=== Ремонт шляхів — {mode} ===')
    print(f'корінь проєкту: {PROJECT_ROOT}')
    print(f'БД: {args.db}\n')

    if args.apply and not args.no_backup:
        print(f'бекап БД: {backup_db(args.db)}\n')

    print('- БД -')
    n_db = repair_db(args.db, args.apply, args.limit_examples)

    n_json = 0
    if not args.skip_json:
        print('\n- МАНИФЕСТИ -')
        n_json = repair_json(args.json_dirs, args.apply)

    if not args.apply:
        print(f'\n[dry-run] Полагодилось би: {n_db} шляхів у БД'
              f' + {n_json} файлів манифестів. Запусти з --apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
