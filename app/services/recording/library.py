"""Реєстрація фіналізованого запису в Audio Library (audio_downloads).

Спільний хелпер для двох викликачів:

- **серверний finalize-callback** (``app.py``) — авто-реєстрація одразу після
  finalize, у т.ч. при recovery-фіналізації осиротілих сесій. Не залежить від
  фронтенду: запис з'являється в бібліотеці навіть якщо вкладку закрито,
  ``/save`` віддав 504 на довгому записі, або stop прийшов з іншого клієнта.
- **HTTP-endpoint** ``POST /api/recording/<sid>/save`` — ручне «зберегти +
  назвати + (опц.) транскрибувати».

Раніше insert у ``audio_downloads`` жив ТІЛЬКИ в ``/save`` — будь-який розрив
між finalize і фронтенд-раунд-тріпом лишав запис «сиротою» (фіналізований на
диску, але невидимий у UI). Цей хелпер робить реєстрацію частиною серверного
finalize, а ``/save`` лишається лише зручністю.

Ідемпотентний за ``youtube_id = f'recording_{session_id}'`` (UNIQUE-колонка),
тому повторний finalize / save / recovery не плодять дублів.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection
from app.services import record_meta

logger = logging.getLogger(__name__)


def _lookup_category(conn, session_id: str) -> Optional[int]:
    """Напрямок запису — з copilot-сесії, привʼязаної до цього recording'у.

    Юзер задає напрямок при старті запису (зберігається у
    ``copilot_sessions.category_id``). Беремо його, щоб recording-рядок мав
    власну категорію ще до транскрипції (фільтр по напрямку в бібліотеці).
    М'яко: якщо copilot_sessions немає / нема привʼязки — повертаємо None.
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
    """Рядок audio_downloads цього запису (за sid або youtube_id), або None.

    Викликається двічі: до INSERT (швидкий шлях) і після програної гонки
    вставки (auto-register при finalize vs ручний /save приходять з різних
    потоків з розривом у мілісекунди — див. ON CONFLICT у register_recording).
    """
    return c.execute(
        'SELECT id, title, description, category_id, has_video FROM audio_downloads '
        'WHERE recording_session_id = ? OR youtube_id = ? LIMIT 1',
        (session_id, youtube_id),
    ).fetchone()


def register_recording(
    db_path: str,
    session_id: str,
    manifest: dict,
    *,
    name: Optional[str] = None,
    category_id: Optional[int] = None,
) -> Optional[dict]:
    """Ідемпотентно вписати фіналізований запис у ``audio_downloads``.

    Args:
        db_path: шлях до SQLite (``app.config['DATABASE']``).
        session_id: ``rec_...`` sid сесії.
        manifest: прочитаний manifest сесії (мусить мати ``final_mp3_path``).
        name: явна назва. Якщо порожня — береться ``manifest.name`` →
            ``auto_name`` → fallback ``Запис <sid[:8]>``.
        category_id: явний напрямок. Якщо None — підтягуємо з copilot-сесії
            запису (``copilot_sessions.category_id``).

    Returns:
        ``{download_id, name, created, file_path, duration_sec, file_size,
        category_id}`` — ``created=True`` якщо рядок щойно вставлено, ``False``
        якщо вже існував (інший викликач/попередній finalize/recovery). ``None``
        якщо ``final.mp3`` відсутній (порожній запис) — реєструвати нічого.

    Idempotency:
        Ключ — ``youtube_id = 'recording_' + session_id`` (UNIQUE). Перед
        insert'ом перевіряємо існуючий рядок за ``recording_session_id`` АБО
        ``youtube_id``. Якщо існує і передали явну ``name`` — оновлюємо ``title``
        (ручний save важливіший за авто-назву); порожню ``category_id`` теж
        дозаповнюємо.

        Race-safe: SELECT-перевірка не атомарна (auto-register при finalize і
        ручний /save приходять з різних потоків з розривом у мілісекунди —
        обидва бачать «рядка нема»), тому INSERT іде з ``ON CONFLICT(youtube_id)
        DO NOTHING``: хто програв гонку — не падає з IntegrityError, а повторно
        читає рядок переможця і йде existing-гілкою (``created=False``).
    """
    final_mp3 = manifest.get('final_mp3_path')
    if not final_mp3 or not Path(final_mp3).is_file():
        logger.info(
            "register_recording: final.mp3 відсутній для %s — нічого реєструвати",
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
    # editable-title-description-02: опис несе manifest (немає окремого
    # аргумента, як у `name` — форма стоп-екрана пише його прямо в manifest
    # через SessionStore.set_description() до виклику register_recording()).
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
            # Вставка race-safe: auto-register при finalize і ручний /save
            # можуть прийти з різних потоків з розривом у мілісекунди — обидва
            # проходять SELECT вище як "нема рядка". ON CONFLICT DO NOTHING
            # гарантує, що той хто програв гонку не падає з IntegrityError,
            # а переходить на існуючий рядок (гілка existing нижче).
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
                    "register_recording: програно гонку вставки для %s "
                    "(auto-finalize vs /save) — переходжу на існуючий рядок",
                    session_id,
                )
                existing = _find_existing(c, session_id, youtube_id)
            else:
                download_id = c.lastrowid

        if existing is not None:
            # Вже зареєстровано. Якщо прийшла явна назва і вона інша —
            # оновимо (ручний /save має пріоритет над авто-реєстрацією).
            # Категорію дозаповнюємо, якщо її ще нема.
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
            # primary_video_path стає відомий лише після finalize_video (це другий
            # register, коли has_video вже =1) — тож бекфілимо незалежно від
            # переходу has_video, інакше шлях ніколи не запишеться.
            if primary:
                sets.append('primary_video_path = ?'); vals.append(primary)
            if sets:
                vals.append(existing['id'])
                c.execute(f"UPDATE audio_downloads SET {', '.join(sets)} WHERE id = ?", vals)
                conn.commit()
            existing_download_id = existing['id']
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
        "Recording %s зареєстровано в бібліотеці: download_id=%s, name=%r, category=%s",
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
# Одна назва запису в Бібліотеці, Медіатеці і сесії (recording-meta-suggest-02)
# ============================================================

def linked_recording(conn, *, transcription_id: Optional[int] = None,
                     audio_id: Optional[int] = None) -> Optional[dict]:
    """Звʼязка запису: ``{audio_id, session_id, transcription_ids}`` або None.

    None — це не запис (YouTube, файл, TG, документ): тоді PATCH поводиться,
    як раніше. Аудіо ↔ транскрипт звʼязані за ``file_path`` (так само їх
    зводить список Медіатеки), аудіо ↔ сесія — за ``recording_session_id``.
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
    """Розкласти вже нормалізовані ``title``/``description`` на всі боки запису.

    ``updates`` — лише ті ключі, що прийшли в PATCH. ``audio_downloads.title``
    — NOT NULL: порожня назва з Бібліотеки його не чіпає (там лишається
    авто-імʼя), а ``manifest.name`` стає ``None`` (→ ``auto_name``).
    Повертає id транскриптів, чия мета реально змінилась (для re-embed).
    Збій manifest-а (сесію видалено, store вимкнено) логується і не валить PATCH:
    база вже узгоджена, а manifest вторинний.
    """
    audio_set = {k: v for k, v in updates.items()
                 if k == 'description' or (k == 'title' and v)}
    if 'title' in updates and not updates['title'] and store is not None:
        # Назву в Бібліотеці прибрали: у Медіатеці теж повертаємо авто-імʼя
        # сесії, а не лишаємо стару власну (NOT NULL не дає поставити NULL).
        try:
            auto = (store.read(link['session_id']) or {}).get('auto_name')
        except Exception:  # noqa: BLE001 — нема manifest-а: лишаємо як є
            logger.warning('sync_recording_meta: %s — manifest не прочитано',
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
        logger.info('sync_recording_meta: %s — store недоступний, manifest не оновлено',
                    link['session_id'])
        return changed
    try:
        if 'title' in updates:
            store.set_name(link['session_id'], updates['title'])
        if 'description' in updates:
            store.set_description(link['session_id'], updates['description'])
    except Exception:  # noqa: BLE001 — manifest вторинний, БД уже узгоджена
        logger.warning('sync_recording_meta: %s — manifest не оновлено',
                       link['session_id'], exc_info=True)
    return changed
