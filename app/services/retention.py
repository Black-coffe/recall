"""Purge прострочених soft-deleted записів (T4.6, REMEDIATION_PLAN Волна 2).

Одиничний DELETE (transcriptions/audio_downloads) ставить `deleted_at`
(epoch-секунди) замість фізичного видалення — undo-вікно для фронта
(restore-ендпоінти). merge_speakers (Phase 12.8) зберігає before-снапшот у
``speaker_merges`` для un-merge. Обидва механізми мають грейс-період —
безстроково висячі soft-deleted записи ніхто не просив; ``purge_soft_deleted``
фізично прибирає прострочене (файл з диска + рядок БД / снапшот).

Викликається на старті app.py (той самий патерн, що й
``recording_service.recover_orphaned()`` і ``job_queue.recover_crashed()``)
— НЕ періодичний фон-таск: досить одного проходу при рестарті сервера.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from app.db.connection import get_db_connection
from app.repositories import transcriptions as tx_repo


logger = logging.getLogger(__name__)

_DEFAULT_GRACE_DAYS = 7
_GRACE_ENV = "RECALL_SOFTDELETE_GRACE_DAYS"


def _grace_seconds() -> float:
    try:
        days = float(os.environ.get(_GRACE_ENV, str(_DEFAULT_GRACE_DAYS)))
    except (TypeError, ValueError):
        days = _DEFAULT_GRACE_DAYS
    return max(0.0, days) * 86400.0


def _safe_remove(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError as e:
        logger.warning("[retention] не вдалося видалити файл %s: %s", path, e)
    return False


def purge_soft_deleted(db_path: str, grace_days: Optional[float] = None) -> dict:
    """Фізично прибрати soft-deleted записи/снапшоти, старші за grace-період.

    Returns {"grace_days", "transcriptions_purged", "audio_purged",
    "merges_purged"}.
    """
    grace_sec = (max(0.0, grace_days) * 86400.0) if grace_days is not None else _grace_seconds()
    cutoff = time.time() - grace_sec
    stats = {
        "grace_days": round(grace_sec / 86400.0, 3),
        "transcriptions_purged": 0,
        "audio_purged": 0,
        "merges_purged": 0,
    }

    try:
        with get_db_connection(db_path) as conn:
            # --- transcriptions ---
            rows = conn.execute(
                "SELECT id, file_path, source_type FROM transcriptions "
                "WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                # Той самий контракт, що й у попередньому фізичному DELETE
                # (transcription.py): file_path youtube/file-джерел видаляємо
                # з диска, 'recording' — файл належить Audio Library (там
                # своя soft-delete/purge доріжка), не чіпаємо.
                if row["source_type"] in ("youtube", "file") and row["file_path"]:
                    _safe_remove(row["file_path"])
                conn.execute(
                    "DELETE FROM transcription_speaker_map WHERE transcription_id = ?",
                    (row["id"],),
                )
                tx_repo.delete_by_id(conn, row["id"])
                stats["transcriptions_purged"] += 1

            # --- audio_downloads ---
            arows = conn.execute(
                "SELECT id, file_path FROM audio_downloads "
                "WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            ).fetchall()
            for row in arows:
                _safe_remove(row["file_path"])
                conn.execute("DELETE FROM audio_downloads WHERE id = ?", (row["id"],))
                stats["audio_purged"] += 1

            # --- speaker_merges snapshots (undo-вікно спливло) ---
            cur = conn.execute(
                "DELETE FROM speaker_merges WHERE merged_at < ?", (cutoff,),
            )
            stats["merges_purged"] = cur.rowcount or 0

            conn.commit()
    except Exception as e:
        logger.error("[retention] purge_soft_deleted помилка: %s", e, exc_info=True)
        return stats

    if stats["transcriptions_purged"] or stats["audio_purged"] or stats["merges_purged"]:
        logger.info(
            "[retention] purge (grace=%.1fd): transcriptions=%d audio=%d merges=%d",
            stats["grace_days"], stats["transcriptions_purged"],
            stats["audio_purged"], stats["merges_purged"],
        )
    return stats
