"""Boot-time reconcile осиротілих записів у бібліотеку (audio_downloads).

Кожен старт Recall проходить по ``recordings/sessions/*`` і гарантує, що
кожна фіналізована сесія **присутня і коректна** в Медіатеці
(``audio_downloads``) — БЕЗ транскрибації. Транскрибувати чи ні — юзер
вирішує сам у UI (кнопка «Транскрибувати» на рядку Медіатеки).

Робить три речі (усі ідемпотентні, безпечні для повтору на кожному старті
і навіть двічі під debug-reloader'ом):

1. **Реєстрація сиріт** — фіналізовану сесію, якої зовсім немає в
   ``audio_downloads``, вписуємо (через :func:`register_recording`).
   Назва: ``manifest.name`` → ``auto_name`` → ``Запис <sid[:8]>`` (як у
   register_recording). Якщо власної назви нема — лишиться дата-плейсхолдер
   (``auto_name`` = ``Запис YYYY-MM-DD HH:MM``), тобто «дата створення як назва».
2. **Лікування мертвих шляхів** — після переїзду/перейменування кореня
   проєкту (Whisper → Recall) ``file_path`` у БД вказує на неіснуючий файл.
   :meth:`SessionStore.read` лікує шлях у поверненому manifest (live, не на
   диск), тож беремо звідти актуальний ``final_mp3_path`` і оновлюємо
   ``audio_downloads.file_path`` (+ ``transcriptions.file_path`` цього
   запису), інакше play/транскрибація з бібліотеки б'ються об мертвий шлях.
3. **Синхронізація назви** — якщо в бібліотеці досі auto-name плейсхолдер, а
   в manifest вже є справжня назва (юзер перейменував сесію після
   авто-реєстрації), підтягуємо назву в ``audio_downloads.title``. Реальну
   (не-плейсхолдерну) назву в бібліотеці НІКОЛИ не перезаписуємо.

Ніколи не транскрибує і нічого не видаляє.
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

# auto-name плейсхолдери, які МОЖНА перезаписати справжньою назвою з manifest:
#   "Запис 2026-06-26 14:05"  (auto_name)
#   "Запис 2026-06-26 14:05" з 'T' замість пробілу
_AUTO_DATE_TITLE = re.compile(r'^Запис\s+\d{4}-\d{2}-\d{2}[ T]\d{2}[:_]\d{2}\b')


def _is_placeholder_title(title, manifest) -> bool:
    """True, якщо назва в бібліотеці — авто-плейсхолдер (можна оновити)."""
    t = (title or '').strip()
    if not t:
        return True
    if _AUTO_DATE_TITLE.match(t):
        return True
    auto = (manifest.get('auto_name') or '').strip()
    if auto and t == auto:
        return True
    # fallback "Запис <sid[:8]>" з register_recording
    if t.startswith('Запис ') and len(t) <= len('Запис ') + 8:
        return True
    return False


def _healed_final(manifest) -> str | None:
    """Актуальний шлях до фінального mp3 (manifest уже path-healed через
    SessionStore.read). None — якщо файлу нема (порожній/аборт-запис)."""
    fp = manifest.get('final_mp3_path')
    return fp if fp and os.path.isfile(fp) else None


def _reconcile_one(db_path: str, store, sid: str, stats: dict) -> None:
    manifest = store.read(sid)  # path-healed live
    if manifest.get('status') != STATUS_FINALIZED:
        return  # у бібліотеку йдуть лише фіналізовані сесії
    final = _healed_final(manifest)

    with get_db_connection(db_path) as conn:
        row = conn.execute(
            'SELECT id, title, file_path FROM audio_downloads '
            'WHERE recording_session_id = ? OR youtube_id = ? LIMIT 1',
            (sid, f'recording_{sid}'),
        ).fetchone()

    # --- сирота: зовсім немає в бібліотеці ---
    if row is None:
        reg = register_recording(db_path, sid, manifest)
        if reg is None:
            stats['skipped_no_file'] += 1
        elif reg.get('created'):
            stats['registered'] += 1
            logger.info(
                "reconcile: сироту %s підтягнуто в бібліотеку (id=%s, name=%r)",
                sid, reg.get('download_id'), reg.get('name'),
            )
        return

    # --- існує: лікуємо шлях + синхронізуємо назву ---
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

    # --- заодно лікуємо мертвий file_path у transcriptions цього запису ---
    # (щоб «оригінальний файл»/re-transcribe з Архіву теж не бились об старий
    #  Whisper-шлях). Тільки якщо знаємо живий final.
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
    """Пройти всі сесії і привести Медіатеку у відповідність до диска.

    Args:
        db_path: шлях до SQLite (``app.config['DATABASE']``).
        store: :class:`SessionStore` (енумерація + path-healing).

    Returns:
        dict-статистика: ``scanned, registered, path_healed, title_synced,
        tx_path_healed, skipped_no_file, errors``.
    """
    stats = {
        'scanned': 0, 'registered': 0, 'path_healed': 0, 'title_synced': 0,
        'tx_path_healed': 0, 'skipped_no_file': 0, 'errors': 0,
    }
    try:
        sessions = store.list_all()
    except Exception as e:
        logger.warning("reconcile_recordings: не вдалося перелічити сесії: %s", e)
        return stats

    # Приглушуємо legacy-warning "manifest version=1" від session_store: sweep
    # читає ВСІ сесії щостарту, тож без цього кожен бут — стіна з ~75 WARNING'ів
    # про старі манифести (відомо-безпечні). Реальні помилки читання ми ловимо
    # нижче per-session і логуємо самі; зіпсовані манифести list_all() вже
    # відсіяв з власним warning.
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
                # одна зламана сесія не має зривати весь sweep
                stats['errors'] += 1
                logger.warning(
                    "reconcile_recordings: сесію %s пропущено через помилку: %s", sid, e,
                )
    finally:
        _ss_logger.setLevel(_prev_level)

    changed = stats['registered'] + stats['path_healed'] + stats['title_synced'] + stats['tx_path_healed']
    if changed:
        logger.info("reconcile_recordings: приведено бібліотеку до диска — %s", stats)
    else:
        logger.debug("reconcile_recordings: змін не потрібно (%s)", stats)
    return stats
