#!/usr/bin/env python
"""Repair of media paths after the project root was moved/renamed.

When the project root moves (as `E:\\Projects\\Whisper` -> `E:\\Projects\\Recall`
in 2026), the DB keeps ABSOLUTE paths to the old place, and on disk there are session
manifests with the old root inside. All consumers are gated by `os.path.isfile(...)`
and fall away **silently**: the "original file" is not served, re-transcribe hits
a dead path, playback does not work. The transcript stays intact, so the breakage
is visible nowhere except an empty button.

Boot-reconcile (`app/services/recording/reconcile.py`) only heals
`source_type='recording'`; youtube/file rows are covered by nobody - and they are the ones
that have been idle for years. This script covers the rest.

HOW IT FIXES (a single deterministic rule):
    the path is split into components, the first project media dir is found
    (`uploads`, `youtube_downloads`, `recordings`, ...), and the tail from it is
    rebuilt from the CURRENT project root. A replacement is accepted only if
    the resulting file REALLY exists. The old root is hardcoded nowhere - works for
    any move, including a change of drive letter.

WHAT IT DELIBERATELY DOES NOT DO - search by file name. In `recordings/sessions/*/final.mp3`
all files are named the same (141 of them), so a basename match would substitute
a FOREIGN session for the transcript. What does not match by root substitution goes to the report, by hand.

DRY-RUN by default. `--apply` writes to the DB (with an auto-backup in `db_backups/`)
and rewrites the manifests (the original stays alongside, `.bak-pathfix`).

Examples:
    .venv/Scripts/python.exe scripts/repair_paths.py                # report
    .venv/Scripts/python.exe scripts/repair_paths.py --apply        # repair
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

# (table, column) - every place that stores a path to a file on disk
TARGETS = [
    ('transcriptions', 'file_path'),
    ('audio_downloads', 'file_path'),
    ('audio_downloads', 'primary_video_path'),
    ('recording_video_tracks', 'file_path'),
    ('video_keyframes', 'image_path'),
]

# project media dirs - the anchors from which the path tail is rebuilt
MEDIA_DIRS = ('uploads', 'youtube_downloads', 'recordings', 'telegram_media',
              'documents', 'transcripts', 'downloads')

_SPLIT = re.compile(r'[\\/]+')


def resolve(fp: str) -> str:
    """Absolute path as the app will see it (cwd = project root)."""
    return fp if os.path.isabs(fp) else str(PROJECT_ROOT / fp)


def rebuild(fp: str) -> str | None:
    """Rebuild a path from the current root. None if the file is still not found."""
    parts = [p for p in _SPLIT.split(fp) if p]
    for i, part in enumerate(parts):
        if part.lower() not in MEDIA_DIRS:
            continue
        cand = str(PROJECT_ROOT.joinpath(*parts[i:]))
        if os.path.isfile(cand):
            return cand
    return None


def _under_root(fp: str) -> bool:
    """Whether the path points inside the current project root."""
    try:
        return os.path.normcase(os.path.abspath(resolve(fp))).startswith(
            os.path.normcase(str(PROJECT_ROOT)) + os.sep)
    except (OSError, ValueError):
        return False


def plan(fp: str) -> tuple[str, str | None]:
    """-> ('ok' | 'fix' | 'lost', new_path | None)"""
    if os.path.isfile(resolve(fp)):
        return 'ok', None
    new = rebuild(fp)
    return ('fix', new) if new else ('lost', None)


# --------------------------------------------------------------------------- DB

def backup_db(db_path: str) -> str:
    """Consistent DB snapshot via SQLite itself (not a file copy - there is a WAL)."""
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
            raise RuntimeError(f'backup failed integrity_check: {state}')
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
            print(f'  {table}.{col}: skipped ({e})')
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
        print(f'  {table:<24}{col:<20}total={len(rows):<6}{dict(stats)}')

    print(f'\n  TOTAL: {dict(grand)}')

    for t, c, i, old, new in updates[:examples]:
        print(f'\n  {t}#{i}\n     was:  {old}\n     will be: {new}')
    if len(updates) > examples:
        print(f'\n  ... {len(updates) - examples} more paths')

    if lost:
        print(f'\n  LOST (file is not on disk): {len(lost)} - NOT touching, '
              f'the transcript is valuable on its own')
        by_dir = Counter()
        for t, _i, fp in lost:
            by_dir[(t, os.path.basename(os.path.dirname(fp)) or '?')] += 1
        for (t, parent), n in by_dir.most_common(10):
            print(f'    {t:<24}dir {parent:<22}{n}')

    if not apply or not updates:
        con.close()
        return len(updates)

    cur = con.cursor()
    written = 0
    for t, c, i, _old, new in updates:
        if os.path.isfile(new):  # re-check before writing
            cur.execute(f'UPDATE {t} SET {c} = ? WHERE id = ?', (new, i))
            written += cur.rowcount
    con.commit()
    print(f'\n  ROWS UPDATED: {written}')
    print(f'  integrity_check: {con.execute("PRAGMA integrity_check").fetchone()[0]}')
    con.close()
    return written


# -------------------------------------------------------------------- manifests

def repair_json(dirs: list[str], apply: bool) -> int:
    """Rebuild the paths inside the sessions' json manifests.

    A manifest is healed live in `SessionStore.read`, but stays old ON DISK -
    so any other consumer of the manifest runs into a dead path.
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
            print(f'  ! not read {f}: {e}')
            continue

        new_txt = txt
        for old in sorted(_iter_paths(data), key=len, reverse=True):
            if os.path.isfile(old):
                continue
            new = rebuild(old)
            if not new:
                # The path is already under the current root - nothing to repair, the file
                # is simply gone (finalize routinely removes the intermediate WAVs after
                # mixing). This is NOT a path breakage, hence a separate counter:
                # otherwise the report after every move scares with hundreds of "lost".
                if _under_root(old):
                    absent += 1
                else:
                    lost += 1
                continue
            # In json text the slashes are escaped, so we replace in the json representation.
            # Cyrillic may be written literally (ensure_ascii=False, as
            # session_store writes it) or as \uXXXX - we try both variants and
            # substitute the answer in THE SAME style so as not to mix them in a file.
            for ascii_only in (False, True):
                a = json.dumps(old, ensure_ascii=ascii_only)[1:-1]
                if a in new_txt:
                    b = json.dumps(new, ensure_ascii=ascii_only)[1:-1]
                    new_txt = new_txt.replace(a, b)
                    fixed += 1
                    break
            else:
                # the path is in the structure but was not found in the text - cannot
                # be silently skipped, otherwise the repair is "successful" and incomplete
                unmatched += 1
                print(f'  ! not found in the text of {f.name}: {old}')
        if new_txt != txt:
            json.loads(new_txt)  # check that the json is still valid
            changed.append((f, new_txt))

    print(f'  json reviewed: {len(files)}')
    print(f'  files with old paths: {len(changed)} '
          f'(paths rebuilt: {fixed}, old root not found: {lost})')
    if absent:
        print(f'  note: {absent} paths are already under the current root, but the file '
              f'is gone - not a breakage (finalize removes the intermediate WAVs)')
    if unmatched:
        print(f'  WARNING: {unmatched} paths could not be found in the file text - '
              f'check by hand, the repair is incomplete')

    if not apply or not changed:
        return len(changed)

    for f, new_txt in changed:
        shutil.copy2(f, f.with_suffix(f.suffix + '.bak-pathfix'))
        f.write_text(new_txt, encoding='utf-8')
    print(f'  FILES REWRITTEN: {len(changed)} (originals alongside, .bak-pathfix)')
    return len(changed)


def _iter_paths(node):
    """All string values that look like a path to a file."""
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
    ap = argparse.ArgumentParser(description='Repair of media paths after the root was moved.')
    ap.add_argument('--db', default=str(PROJECT_ROOT / 'whisper_history.db'))
    ap.add_argument('--json-dirs', nargs='+', default=['recordings'],
                    help='Where to look for json manifests (default: recordings).')
    ap.add_argument('--apply', action='store_true',
                    help='PERFORM the repair. Without the flag - report only (dry-run).')
    ap.add_argument('--no-backup', action='store_true',
                    help='Do not auto-backup the DB before writing (not recommended).')
    ap.add_argument('--skip-json', action='store_true', help='Do not touch the manifests.')
    ap.add_argument('--limit-examples', type=int, default=5)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f'DB not found: {args.db}', file=sys.stderr)
        return 2

    mode = 'APPLY (write)' if args.apply else 'DRY-RUN (report only)'
    print(f'=== Path repair - {mode} ===')
    print(f'project root: {PROJECT_ROOT}')
    print(f'DB: {args.db}\n')

    if args.apply and not args.no_backup:
        print(f'DB backup: {backup_db(args.db)}\n')

    print('- DB -')
    n_db = repair_db(args.db, args.apply, args.limit_examples)

    n_json = 0
    if not args.skip_json:
        print('\n- MANIFESTS -')
        n_json = repair_json(args.json_dirs, args.apply)

    if not args.apply:
        print(f'\n[dry-run] Would fix: {n_db} paths in the DB'
              f' + {n_json} manifest files. Run with --apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
