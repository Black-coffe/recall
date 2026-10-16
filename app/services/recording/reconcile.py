"""Boot-time reconcile of orphaned recordings into the library (audio_downloads).

On every Recall start it walks ``recordings/sessions/*`` and guarantees that
every finalized session is **present and correct** in the Media library
(``audio_downloads``) — WITHOUT transcription. Whether to transcribe is
decided by the user in the UI (the «Транскрибувати» button on a Media library row).

It does three things (all idempotent, safe to repeat on every start
and even twice under the debug reloader):

1. **Registering orphans** — a finalized session that is not in
   ``audio_downloads`` at all is inserted (via :func:`register_recording`).
   Name: ``manifest.name`` → ``auto_name`` → ``Запис <sid[:8]>`` (as in
   register_recording). If there is no custom name, the date placeholder stays
   (``auto_name`` = ``Запис YYYY-MM-DD HH:MM``), i.e. "creation date as the name".
2. **Healing dead paths** — after a move/rename of the project root
   (Whisper → Recall) ``file_path`` in the DB points to a nonexistent file.
   :meth:`SessionStore.read` heals the path in the returned manifest (live, not to
   disk), so we take the current ``final_mp3_path`` from there and update
   ``audio_downloads.file_path`` (+ the ``transcriptions.file_path`` of this
   recording), otherwise play/transcription from the library hit a dead path.
3. **Name synchronization** — if the library still has an auto-name placeholder and
   the manifest already has a real name (the user renamed the session after
   auto-registration), we pull the name into ``audio_downloads.title``. A real
   (non-placeholder) name in the library is NEVER overwritten.

It never transcribes and never deletes anything.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from app.db.connection import get_db_connection
from app.services.recording.library import register_recording
from app.services.recording.session_store import STATUS_FINALIZED

logger = logging.getLogger(__name__)

# auto-name placeholders that CAN be overwritten with a real name from the manifest:
#   "Запис 2026-06-26 14:05"  (auto_name)
#   "Запис 2026-06-26 14:05" with 'T' instead of a space
_AUTO_DATE_TITLE = re.compile(r'^Запис\s+\d{4}-\d{2}-\d{2}[ T]\d{2}[:_]\d{2}\b')


def _is_placeholder_title(title, manifest) -> bool:
    """True if the library title is an auto placeholder (it can be updated)."""
    t = (title or '').strip()
    if not t:
        return True
    if _AUTO_DATE_TITLE.match(t):
        return True
    auto = (manifest.get('auto_name') or '').strip()
    if auto and t == auto:
        return True
    # fallback "Запис <sid[:8]>" from register_recording
    if t.startswith('Запис ') and len(t) <= len('Запис ') + 8:
        return True
    return False


def _healed_final(manifest) -> str | None:
    """The current path to the final mp3 (the manifest is already path-healed through
    SessionStore.read). None — if there is no file (an empty/aborted recording)."""
    fp = manifest.get('final_mp3_path')
    return fp if fp and os.path.isfile(fp) else None


def _reconcile_one(db_path: str, store, sid: str, stats: dict) -> None:
    manifest = store.read(sid)  # path-healed live
    if manifest.get('status') != STATUS_FINALIZED:
        return  # only finalized sessions go into the library
    final = _healed_final(manifest)

    with get_db_connection(db_path) as conn:
        row = conn.execute(
            'SELECT id, title, file_path FROM audio_downloads '
            'WHERE recording_session_id = ? OR youtube_id = ? LIMIT 1',
            (sid, f'recording_{sid}'),
        ).fetchone()

    # --- orphan: not in the library at all ---
    if row is None:
        reg = register_recording(db_path, sid, manifest)
        if reg is None:
            stats['skipped_no_file'] += 1
        elif reg.get('created'):
            stats['registered'] += 1
            logger.info(
                "reconcile: orphan %s pulled into the library (id=%s, name=%r)",
                sid, reg.get('download_id'), reg.get('name'),
            )
        return

    # --- exists: heal the path + synchronize the name ---
    sets, vals = [], []
    cur_fp = row['file_path']
    if final and cur_fp != final and (not cur_fp or not os.path.isfile(cur_fp)):
        sets.append('file_path = ?'); vals.append(final)
        try:
            sets.append('file_size = ?'); vals.append(Path(final).stat().st_size)
        except OSError:
            pass
        stats['path_healed'] += 1

    mname = (manifest.get('name') or '').strip()
    if mname and row['title'] != mname and _is_placeholder_title(row['title'], manifest):
        sets.append('title = ?'); vals.append(mname)
        stats['title_synced'] += 1

    if sets:
        vals.append(row['id'])
        with get_db_connection(db_path) as conn:
            conn.execute(
                f"UPDATE audio_downloads SET {', '.join(sets)} WHERE id = ?", vals,
            )
            conn.commit()

    # --- also heal the dead file_path in the transcriptions of this recording ---
    # (so that the «original file»/re-transcribe from the Archive does not hit the old
    #  Whisper path either). Only if we know the live final.
    if final:
        with get_db_connection(db_path) as conn:
            trows = conn.execute(
                "SELECT id, file_path FROM transcriptions "
                "WHERE source_type = 'recording' AND file_path LIKE ?",
                (f'%{sid}%',),
            ).fetchall()
            healed = 0
            for tr in trows:
                fp = tr['file_path']
                if fp and fp != final and not os.path.isfile(fp):
                    conn.execute(
                        'UPDATE transcriptions SET file_path = ? WHERE id = ?',
                        (final, tr['id']),
                    )
                    healed += 1
            if healed:
                conn.commit()
                stats['tx_path_healed'] += healed


def reconcile_recordings(db_path: str, store) -> dict:
    """Walk all sessions and bring the Media library in line with the disk.

    Args:
        db_path: path to SQLite (``app.config['DATABASE']``).
        store: :class:`SessionStore` (enumeration + path healing).

    Returns:
        dict statistics: ``scanned, registered, path_healed, title_synced,
        tx_path_healed, skipped_no_file, errors``.
    """
    stats = {
        'scanned': 0, 'registered': 0, 'path_healed': 0, 'title_synced': 0,
        'tx_path_healed': 0, 'skipped_no_file': 0, 'errors': 0,
    }
    try:
        sessions = store.list_all()
    except Exception as e:
        logger.warning("reconcile_recordings: could not list sessions: %s", e)
        return stats

    # We silence the legacy warning "manifest version=1" from session_store: the sweep
    # reads ALL sessions on every start, so without this every boot is a wall of ~75 WARNINGs
    # about old manifests (known safe). Real read errors we catch
    # below per session and log ourselves; corrupted manifests list_all() has already
    # filtered out with its own warning.
    _ss_logger = logging.getLogger('app.services.recording.session_store')
    _prev_level = _ss_logger.level
    _ss_logger.setLevel(logging.ERROR)
    try:
        for m in sessions:
            sid = m.get('session_id')
            if not sid:
                continue
            stats['scanned'] += 1
            try:
                _reconcile_one(db_path, store, sid, stats)
            except Exception as e:
                # one broken session must not break the whole sweep
                stats['errors'] += 1
                logger.warning(
                    "reconcile_recordings: session %s skipped due to an error: %s", sid, e,
                )
    finally:
        _ss_logger.setLevel(_prev_level)

    changed = stats['registered'] + stats['path_healed'] + stats['title_synced'] + stats['tx_path_healed']
    if changed:
        logger.info("reconcile_recordings: library brought in line with the disk — %s", stats)
    else:
        logger.debug("reconcile_recordings: no changes needed (%s)", stats)
    return stats
