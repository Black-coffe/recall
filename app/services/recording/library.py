"""Registration of a finalized recording in the Audio Library (audio_downloads).

A shared helper for two callers:

- **the server finalize callback** (``app.py``) — auto-registration right after
  finalize, including recovery finalization of orphaned sessions. It does not depend on the
  frontend: the recording appears in the library even if the tab is closed,
  ``/save`` returned 504 on a long recording, or stop came from another client.
- **the HTTP endpoint** ``POST /api/recording/<sid>/save`` — manual «save +
  name + (optionally) transcribe».

Previously the insert into ``audio_downloads`` lived ONLY in ``/save`` — any break
between finalize and the frontend round trip left the recording an "orphan" (finalized on
disk but invisible in the UI). This helper makes registration part of the server
finalize, and ``/save`` remains only a convenience.

Idempotent by ``youtube_id = f'recording_{session_id}'`` (a UNIQUE column),
so a repeated finalize / save / recovery does not produce duplicates.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection
from app.services import record_meta

logger = logging.getLogger(__name__)


def _lookup_category(conn, session_id: str) -> Optional[int]:
    """The direction of a recording — from the copilot session bound to this recording.

    The user sets the direction at the start of the recording (stored in
    ``copilot_sessions.category_id``). We take it so that the recording row has
    its own category even before transcription (the filter by direction in the library).
    Soft: if there is no copilot_sessions row / no binding — return None.
    """
    try:
        row = conn.execute(
            'SELECT category_id FROM copilot_sessions '
            'WHERE recording_session_id = ? AND category_id IS NOT NULL '
            'ORDER BY id DESC LIMIT 1',
            (session_id,),
        ).fetchone()
        return row['category_id'] if row else None
    except Exception:
        return None


def _find_existing(c, session_id: str, youtube_id: str):
    """The audio_downloads row of this recording (by sid or youtube_id), or None.

    Called twice: before the INSERT (fast path) and after a lost insert
    race (auto-register at finalize vs manual /save come from different
    threads milliseconds apart — see ON CONFLICT in register_recording).
    """
    return c.execute(
        'SELECT id, title, description, category_id, has_video FROM audio_downloads '
        'WHERE recording_session_id = ? OR youtube_id = ? LIMIT 1',
        (session_id, youtube_id),
    ).fetchone()


def _bind_calendar(conn, download_id: int, session_id: str, manifest: dict,
                   explicit: str, resolved_name: str) -> str:
    """k6-calendar-ics: bind the row to its calendar event; returns the name the row now has.

    Title order: the owner's name (explicit or ``manifest.name``) is never replaced; an
    automatic name becomes the event title. Never fails a save: a calendar problem is logged.
    """
    from app.services import calendar_bind
    try:
        window = calendar_bind.recording_window(manifest.get('started_at'),
                                                manifest.get('total_duration_sec'))
        if window is None:
            return resolved_name
        ev = calendar_bind.bind_download(conn, download_id, *window)
        title = ((ev['title'] if ev is not None else '') or '').strip()
        owner_named = bool(explicit or (manifest.get('name') or '').strip())
        if title and not owner_named:
            row = conn.execute('SELECT title FROM audio_downloads WHERE id = ?', (download_id,)).fetchone()
            auto = {manifest.get('auto_name'), f'Запис {session_id[:8]}'}
            if row is not None and row['title'] in auto:
                conn.execute('UPDATE audio_downloads SET title = ? WHERE id = ?', (title, download_id))
                resolved_name = title
            elif row is not None and row['title'] == title:
                resolved_name = title          # an earlier save already applied the event title
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("register_recording: calendar binding skipped for %s: %s",
                       session_id, type(exc).__name__)
    return resolved_name


def register_recording(
    db_path: str,
    session_id: str,
    manifest: dict,
    *,
    name: Optional[str] = None,
    category_id: Optional[int] = None,
) -> Optional[dict]:
    """Idempotently write a finalized recording into ``audio_downloads``.

    Args:
        db_path: path to SQLite (``app.config['DATABASE']``).
        session_id: the ``rec_...`` sid of the session.
        manifest: the read session manifest (must have ``final_mp3_path``).
        name: explicit name. If empty — ``manifest.name`` is used →
            ``auto_name`` → fallback ``Запис <sid[:8]>``.
        category_id: explicit direction. If None — taken from the recording's
            copilot session (``copilot_sessions.category_id``).

    Returns:
        ``{download_id, name, created, file_path, duration_sec, file_size,
        category_id}`` — ``created=True`` if the row was just inserted, ``False``
        if it already existed (another caller/an earlier finalize/recovery). ``None``
        if ``final.mp3`` is missing (an empty recording) — nothing to register.

    Idempotency:
        The key is ``youtube_id = 'recording_' + session_id`` (UNIQUE). Before the
        insert we check for an existing row by ``recording_session_id`` OR
        ``youtube_id``. If it exists and an explicit ``name`` was passed, we update ``title``
        (a manual save outranks the auto name); an empty ``category_id`` is also
        back-filled.

        Race-safe: the SELECT check is not atomic (auto-register at finalize and
        manual /save come from different threads milliseconds apart —
        both see "no row"), so the INSERT goes with ``ON CONFLICT(youtube_id)
        DO NOTHING``: whoever lost the race does not fail with IntegrityError, but re-reads
        the winner's row and takes the existing branch (``created=False``).
    """
    final_mp3 = manifest.get('final_mp3_path')
    if not final_mp3 or not Path(final_mp3).is_file():
        logger.info(
            "register_recording: final.mp3 is missing for %s — nothing to register",
            session_id,
        )
        return None

    explicit = (name or '').strip()
    resolved_name = (
        explicit
        or manifest.get('name')
        or manifest.get('auto_name')
        or f'Запис {session_id[:8]}'
    )
    # editable-title-description-02: the description is carried by the manifest (there is no separate
    # argument like `name` — the stop-screen form writes it straight into the manifest
    # via SessionStore.set_description() before register_recording() is called).
    manifest_description = manifest.get('description') or None
    duration = float(manifest.get('total_duration_sec', 0.0) or 0.0)
    file_size = Path(final_mp3).stat().st_size
    youtube_id = f'recording_{session_id}'

    video_tracks = manifest.get('streams', {}).get('video', [])
    primary = manifest.get('primary_video_path')
    has_video = 1 if video_tracks else 0

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        if category_id is None:
            category_id = _lookup_category(conn, session_id)

        existing = _find_existing(c, session_id, youtube_id)

        download_id = None
        if existing is None:
            # The insert is race-safe: auto-register at finalize and manual /save
            # may come from different threads milliseconds apart — both
            # pass the SELECT above as "no row". ON CONFLICT DO NOTHING
            # guarantees that the one who lost the race does not fail with IntegrityError,
            # but moves to the existing row (the existing branch below).
            c.execute(
                '''INSERT INTO audio_downloads
                     (youtube_url, youtube_id, title, description, author, duration,
                      file_path, file_size, audio_format,
                      source_type, recording_session_id,
                      recording_segments, recording_duration_sec, category_id,
                      has_video, primary_video_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(youtube_id) DO NOTHING''',
                (
                    f'recording://{session_id}',
                    youtube_id,
                    resolved_name,
                    manifest_description,
                    'Локальний запис',
                    int(duration) if duration > 0 else None,
                    final_mp3,
                    file_size,
                    'mp3',
                    'recording',
                    session_id,
                    len(manifest.get('segments', []) or []),
                    duration,
                    category_id,
                    has_video,
                    primary,
                ),
            )
            conn.commit()
            if c.rowcount == 0:
                logger.info(
                    "register_recording: lost the insert race for %s "
                    "(auto-finalize vs /save) — switching to the existing row",
                    session_id,
                )
                existing = _find_existing(c, session_id, youtube_id)
            else:
                download_id = c.lastrowid
                resolved_name = _bind_calendar(conn, download_id, session_id, manifest,
                                               explicit, resolved_name)

        if existing is not None:
            # Already registered. If an explicit name came and it differs —
            # we update it (a manual /save takes priority over auto-registration).
            # The category is back-filled if it is not there yet.
            sets, vals = [], []
            if explicit and existing['title'] != explicit:
                sets.append('title = ?'); vals.append(explicit)
                resolved_name = explicit
            existing_description = existing['description'] if 'description' in existing.keys() else None
            if manifest_description is not None and existing_description != manifest_description:
                sets.append('description = ?'); vals.append(manifest_description)
            existing_cat = existing['category_id'] if 'category_id' in existing.keys() else None
            if category_id is not None and existing_cat is None:
                sets.append('category_id = ?'); vals.append(category_id)
                existing_cat = category_id
            existing_has_video = existing['has_video'] if 'has_video' in existing.keys() else None
            if has_video and not existing_has_video:
                sets.append('has_video = ?'); vals.append(has_video)
            # primary_video_path becomes known only after finalize_video (this is the second
            # register, when has_video is already =1) — so we backfill regardless of the
            # has_video transition, otherwise the path would never be written.
            if primary:
                sets.append('primary_video_path = ?'); vals.append(primary)
            if sets:
                vals.append(existing['id'])
                c.execute(f"UPDATE audio_downloads SET {', '.join(sets)} WHERE id = ?", vals)
                conn.commit()
            existing_download_id = existing['id']
            resolved_name = _bind_calendar(conn, existing_download_id, session_id, manifest,
                                           explicit, resolved_name)
            for t in video_tracks:
                region = t.get('region') or {}
                rx, ry, rw, rh = region.get('x'), region.get('y'), region.get('w'), region.get('h')
                c.execute(
                    '''INSERT OR IGNORE INTO recording_video_tracks
                        (recording_session_id, audio_download_id, track_id, monitor_index, monitor_label,
                         mode, file_path, codec, fps, start_offset_sec, duration_sec, status,
                         region_x, region_y, region_w, region_h)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (session_id, existing_download_id, t.get('track_id'), t.get('monitor_index'), t.get('monitor_label'),
                     t.get('mode', 'full'), t.get('path'), t.get('codec'), t.get('fps'),
                     t.get('start_offset_sec'), t.get('duration_sec'), t.get('status', 'recording'),
                     rx, ry, rw, rh),
                )
                c.execute(
                    '''UPDATE recording_video_tracks SET audio_download_id=?, file_path=?, duration_sec=?, status=?,
                        region_x=?, region_y=?, region_w=?, region_h=?
                        WHERE recording_session_id=? AND track_id=?''',
                    (existing_download_id, t.get('path'), t.get('duration_sec'), t.get('status', 'recording'),
                     rx, ry, rw, rh,
                     session_id, t.get('track_id')),
                )
            if video_tracks:
                conn.commit()
            return {
                'download_id': existing_download_id,
                'name': resolved_name,
                'created': False,
                'file_path': final_mp3,
                'duration_sec': duration,
                'file_size': file_size,
                'category_id': existing_cat,
            }

        for t in video_tracks:
            region = t.get('region') or {}
            rx, ry, rw, rh = region.get('x'), region.get('y'), region.get('w'), region.get('h')
            c.execute(
                '''INSERT OR IGNORE INTO recording_video_tracks
                    (recording_session_id, audio_download_id, track_id, monitor_index, monitor_label,
                     mode, file_path, codec, fps, start_offset_sec, duration_sec, status,
                     region_x, region_y, region_w, region_h)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (session_id, download_id, t.get('track_id'), t.get('monitor_index'), t.get('monitor_label'),
                 t.get('mode', 'full'), t.get('path'), t.get('codec'), t.get('fps'),
                 t.get('start_offset_sec'), t.get('duration_sec'), t.get('status', 'recording'),
                 rx, ry, rw, rh),
            )
            c.execute(
                '''UPDATE recording_video_tracks SET audio_download_id=?, file_path=?, duration_sec=?, status=?,
                    region_x=?, region_y=?, region_w=?, region_h=?
                    WHERE recording_session_id=? AND track_id=?''',
                (download_id, t.get('path'), t.get('duration_sec'), t.get('status', 'recording'),
                 rx, ry, rw, rh,
                 session_id, t.get('track_id')),
            )
        if video_tracks:
            conn.commit()

    logger.info(
        "Recording %s registered in the library: download_id=%s, name=%r, category=%s",
        session_id, download_id, resolved_name, category_id,
    )
    return {
        'download_id': download_id,
        'name': resolved_name,
        'created': True,
        'file_path': final_mp3,
        'duration_sec': duration,
        'file_size': file_size,
        'category_id': category_id,
    }


# ============================================================
# A single recording name in the Library, the Media library and the session (recording-meta-suggest-02)
# ============================================================

def linked_recording(conn, *, transcription_id: Optional[int] = None,
                     audio_id: Optional[int] = None) -> Optional[dict]:
    """The link of a recording: ``{audio_id, session_id, transcription_ids}`` or None.

    None means it is not a recording (YouTube, file, TG, document): then PATCH behaves
    as before. Audio ↔ transcript are linked by ``file_path`` (the same way the Media library
    list joins them), audio ↔ session — by ``recording_session_id``.
    """
    if transcription_id is not None:
        tx = conn.execute(
            'SELECT file_path FROM transcriptions WHERE id = ? AND deleted_at IS NULL',
            (transcription_id,),
        ).fetchone()
        if tx is None or not tx['file_path']:
            return None
        audio = conn.execute(
            'SELECT id, recording_session_id, file_path FROM audio_downloads '
            'WHERE file_path = ? AND deleted_at IS NULL '
            "AND recording_session_id IS NOT NULL AND recording_session_id != '' "
            'ORDER BY id DESC LIMIT 1',
            (tx['file_path'],),
        ).fetchone()
    elif audio_id is not None:
        audio = conn.execute(
            'SELECT id, recording_session_id, file_path FROM audio_downloads '
            'WHERE id = ? AND deleted_at IS NULL',
            (audio_id,),
        ).fetchone()
    else:
        return None
    if audio is None or not audio['recording_session_id']:
        return None
    tx_ids = [r['id'] for r in conn.execute(
        'SELECT id FROM transcriptions WHERE file_path = ? AND deleted_at IS NULL',
        (audio['file_path'],),
    ).fetchall()] if audio['file_path'] else []
    return {
        'audio_id': audio['id'],
        'session_id': audio['recording_session_id'],
        'transcription_ids': tx_ids,
    }


def sync_recording_meta(conn, link: dict, updates: dict, *, store=None) -> list[int]:
    """Spread the already normalized ``title``/``description`` over all sides of a recording.

    ``updates`` holds only the keys that came in the PATCH. ``audio_downloads.title``
    is NOT NULL: an empty name from the Library does not touch it (the
    auto name stays there), and ``manifest.name`` becomes ``None`` (→ ``auto_name``).
    Returns the ids of transcripts whose meta really changed (for re-embed).
    A manifest failure (session deleted, store disabled) is logged and does not fail the PATCH:
    the DB is already consistent, and the manifest is secondary.
    """
    audio_set = {k: v for k, v in updates.items()
                 if k == 'description' or (k == 'title' and v)}
    if 'title' in updates and not updates['title'] and store is not None:
        # The name was removed in the Library: in the Media library we also restore the session's
        # auto name, rather than keep the old custom one (NOT NULL does not allow setting NULL).
        try:
            auto = (store.read(link['session_id']) or {}).get('auto_name')
        except Exception:  # noqa: BLE001 — no manifest: leave as is
            logger.warning('sync_recording_meta: %s — manifest could not be read',
                           link['session_id'], exc_info=True)
            auto = None
        if auto:
            audio_set['title'] = auto
    if audio_set:
        assignments = ', '.join(f'{field} = ?' for field in audio_set)
        conn.execute(f'UPDATE audio_downloads SET {assignments} WHERE id = ?',
                     [*audio_set.values(), link['audio_id']])
        conn.commit()

    changed: list[int] = []
    for tid in link['transcription_ids']:
        res = record_meta.update_meta(conn, tid, **updates)
        if res and res.get('changed'):
            changed.append(tid)

    if store is None:
        logger.info('sync_recording_meta: %s — store unavailable, manifest not updated',
                    link['session_id'])
        return changed
    try:
        if 'title' in updates:
            store.set_name(link['session_id'], updates['title'])
        if 'description' in updates:
            store.set_description(link['session_id'], updates['description'])
    except Exception:  # noqa: BLE001 — the manifest is secondary, the DB is already consistent
        logger.warning('sync_recording_meta: %s — manifest not updated',
                       link['session_id'], exc_info=True)
    return changed
