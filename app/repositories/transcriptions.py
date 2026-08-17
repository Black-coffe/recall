"""Repository для таблиці `transcriptions` — найдубльованіший запит проєкту
("читання рядка за id"), зустрічався в десятках місць з різним набором колонок
(див. grep у T7.2). Централізує читання/існування/видалення за id.

Навмисно НЕ чіпає app/blueprints/transcription.py, recording.py, video_analysis.py
(T7.2, перший інкремент) — їх паралельно править інший воркер (T7.1). Ті файли
лишаються на наступний інкремент.
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

_ALL_COLUMNS = "*"


def get_by_id(
    conn: sqlite3.Connection,
    transcription_id: int,
    columns: Optional[Sequence[str]] = None,
    *,
    include_deleted: bool = True,
) -> Optional[sqlite3.Row]:
    """Читає один рядок `transcriptions` за id.

    columns=None → `SELECT *` (як робив research.py). Для вузьких випадків
    (2-6 колонок) передавай явний список — не тягни transcript_text/segments,
    якщо викликач їх не використовує (рядки можуть бути гігантськими).

    include_deleted=False додає `AND deleted_at IS NULL` — прапорець для
    майбутніх викликів (soft-delete фільтр з transcription.py, поза межами
    цього інкременту).
    """
    cols = ", ".join(columns) if columns else _ALL_COLUMNS
    sql = f"SELECT {cols} FROM transcriptions WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    return conn.execute(sql, (transcription_id,)).fetchone()


def get_many_by_ids(
    conn: sqlite3.Connection,
    ids: Sequence[int],
    columns: Optional[Sequence[str]] = None,
) -> list[sqlite3.Row]:
    """Читає декілька рядків за списком id (`WHERE id IN (...)`).

    Порожній `ids` → `[]` без звернення до БД (уникає `IN ()` — невалідний SQL).
    """
    if not ids:
        return []
    cols = ", ".join(columns) if columns else _ALL_COLUMNS
    placeholders = ",".join("?" * len(ids))
    sql = f"SELECT {cols} FROM transcriptions WHERE id IN ({placeholders})"
    return conn.execute(sql, list(ids)).fetchall()


def exists(conn: sqlite3.Connection, transcription_id: int) -> bool:
    """Перевірка існування рядка без тягнення даних окрім самого факту."""
    row = conn.execute(
        "SELECT 1 FROM transcriptions WHERE id = ?", (transcription_id,)
    ).fetchone()
    return row is not None


def delete_by_id(conn: sqlite3.Connection, transcription_id: int) -> None:
    """Фізичне видалення рядка (hard delete).

    НЕ керує пов'язаними таблицями (transcription_speaker_map тощо) — той
    самий контракт, що й раніше в retention.py/transcription.py: виклик
    відповідає за порядок FK-очищення й commit.
    """
    conn.execute("DELETE FROM transcriptions WHERE id = ?", (transcription_id,))
