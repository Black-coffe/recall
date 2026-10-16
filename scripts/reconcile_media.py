#!/usr/bin/env python
"""Two-way reconciliation of media files against the DB (dry-run by default).

Direct pass (disk -> DB):
    Files in the media dirs that NO row of ``audio_downloads`` or
    ``transcriptions`` references (by absolute path OR
    file name) -> orphaned (deletion candidates).

Reverse pass (DB -> disk):
    DB rows whose ``file_path`` points to a file missing on disk.
    - ``audio_downloads``: dead catalog rows of the Audio library - can be removed
      (no file, the entry cannot be played/transcribed).
    - ``transcriptions``: NEVER deleted - the text, entities and RAG chunks
      are valuable; missing original audio of old downloads is normal.
      Warning only.

By default the script ONLY REPORTS (dry-run). The ``--apply`` flag performs
the cleanup: deletes orphaned files + dead ``audio_downloads`` rows. Transcripts
are never touched under any circumstances.

Examples:
    .venv/Scripts/python.exe scripts/reconcile_media.py            # dry-run
    .venv/Scripts/python.exe scripts/reconcile_media.py --apply    # cleanup
    .venv/Scripts/python.exe scripts/reconcile_media.py --dirs youtube_downloads uploads
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Dirs with RELIABLE file_path tracking - only these are scanned by default:
#   youtube_downloads -> audio_downloads.file_path (complete);
#   uploads           -> transcriptions.file_path (source_type='file', complete).
#
# DELIBERATELY EXCLUDED from the default (file_path is tracked unreliably -> the direct pass
# would give false "orphans", --apply would delete needed files):
#   documents/      - Phase 16: text is in the archive, but file_path is NOT stored
#                     (0 rows) -> all PDF/DOCX would look like orphans;
#   telegram_media/ - only part of the media has file_path (text-only TG messages
#                     have no files) -> many false positives;
#   recordings/     - own lifecycle (non-finalized sessions, finalize cleans
#                     the intermediate WAVs itself).
# They can be passed explicitly via --dirs, but ONLY for a report and with manual review.
DEFAULT_DIRS = ['youtube_downloads', 'uploads']
# Dirs for which --apply is UNSAFE (no reliable file_path tracking).
UNSAFE_FOR_APPLY = {'documents', 'telegram_media', 'recordings'}


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def gather_referenced(db_path: str) -> tuple[set[str], set[str]]:
    """All file_path values from audio_downloads + transcriptions -> (abs paths, basenames)."""
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
    """All files in the given dirs -> [(absolute path, size)]."""
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
    """Files on disk that the DB does not reference (neither by path nor by name)."""
    orphans = []
    for p, sz in disk:
        ap = _norm(p)
        if ap in abs_set or os.path.basename(ap) in base_set:
            continue
        orphans.append((p, sz))
    return orphans


def find_broken(db_path: str, table: str):
    """Rows of table with a file_path that points to a missing file."""
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
    ap = argparse.ArgumentParser(description='Two-way reconciliation of media against the DB.')
    ap.add_argument('--db', default=str(PROJECT_ROOT / 'whisper_history.db'),
                    help='Path to SQLite (default: whisper_history.db in the root).')
    ap.add_argument('--dirs', nargs='+', default=DEFAULT_DIRS,
                    help=f'Media dirs for the direct pass (default: {" ".join(DEFAULT_DIRS)}).')
    ap.add_argument('--apply', action='store_true',
                    help='PERFORM the cleanup (delete orphaned files + dead '
                         'audio_downloads rows). Without the flag - report only (dry-run).')
    ap.add_argument('--limit-examples', type=int, default=15,
                    help='How many examples to show in the report (default: 15).')
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f'ERROR: DB not found: {args.db}', file=sys.stderr)
        return 2

    dirs = [(PROJECT_ROOT / d) if not os.path.isabs(d) else Path(d) for d in args.dirs]
    mode = 'APPLY (cleanup)' if args.apply else 'DRY-RUN (report only)'
    print(f'=== Media vs DB reconciliation - {mode} ===')
    print(f'DB: {args.db}')
    print(f'Dirs: {", ".join(str(d) for d in dirs)}\n')

    abs_set, base_set = gather_referenced(args.db)

    # --- Direct: orphaned files ----------------------------------------------
    disk = scan_dirs(dirs)
    orphans = find_orphans(disk, abs_set, base_set)
    disk_total = sum(s for _, s in disk)
    orphan_total = sum(s for _, s in orphans)
    print('- DIRECT (disk -> DB): orphaned files')
    print(f'  on disk: {len(disk)} files, {_gb(disk_total)}')
    print(f'  orphaned: {len(orphans)} files, {_gb(orphan_total)}')
    for p, sz in sorted(orphans, key=lambda x: -x[1])[:args.limit_examples]:
        print(f'    {_mb(sz):>10}  {os.path.basename(p)}')
    if len(orphans) > args.limit_examples:
        print(f'    ... {len(orphans) - args.limit_examples} more')

    # --- Reverse: broken references ------------------------------------------
    broken_audio = find_broken(args.db, 'audio_downloads')
    broken_tr = find_broken(args.db, 'transcriptions')
    print('\n- REVERSE (DB -> disk): broken references')
    print(f'  audio_downloads (Audio library): {len(broken_audio)} dead rows (can be removed)')
    for r in broken_audio[:args.limit_examples]:
        print(f'    id={r["id"]} [{r["source_type"]}] {os.path.basename(r["file_path"])}')
    print(f'  transcriptions (History): {len(broken_tr)} broken - KEEPING '
          f'(text+RAG are valuable, missing audio is normal)')

    # --- Action (only with --apply) ------------------------------------------
    if not args.apply:
        would = orphan_total
        print(f'\n[dry-run] Would delete: {len(orphans)} files ({_gb(would)}) '
              f'+ {len(broken_audio)} Audio library rows.')
        print('Run with --apply to perform it. Transcripts are never touched.')
        return 0

    # Guard: do not delete files from dirs where file_path tracking is unreliable
    # (documents/telegram_media/recordings) - even if they were passed via --dirs.
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
        if os.path.isfile(p):  # re-check before deleting
            try:
                os.remove(p)
                freed += sz
                removed_files += 1
            except OSError as e:
                print(f'    WARNING: not deleted {p}: {e}')
    if skipped_unsafe:
        print(f'    WARNING: skipped {skipped_unsafe} files in unreliable dirs '
              f'({", ".join(sorted(UNSAFE_FOR_APPLY))}) - file_path is tracked '
              f'incompletely, deletion is unsafe.')

    removed_rows = 0
    if broken_audio:
        con = sqlite3.connect(args.db)
        try:
            cur = con.cursor()
            for r in broken_audio:
                # safety: delete a row only if the file is REALLY missing
                if not os.path.isfile(r['file_path']):
                    cur.execute('DELETE FROM audio_downloads WHERE id = ?', (r['id'],))
                    removed_rows += 1
            con.commit()
        finally:
            con.close()

    print(f'\nDone: deleted {removed_files} orphaned files ({_gb(freed)}) '
          f'+ {removed_rows} dead Audio library rows.')
    print(f'   Transcripts ({len(broken_tr)} broken) kept.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
