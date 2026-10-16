"""Repository for the `transcriptions` table — the most duplicated query in the project
("read a row by id"), found in dozens of places with different column sets
(see the grep in T7.2). Centralizes reading/existence/deletion by id.

Deliberately does NOT touch app/blueprints/transcription.py, recording.py, video_analysis.py
(T7.2, first increment) — another worker (T7.1) is editing them in parallel. Those files
stay for the next increment.
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
    """Reads one `transcriptions` row by id.

    columns=None → `SELECT *` (as research.py did). For narrow cases
    (2-6 columns) pass an explicit list — do not pull transcript_text/segments
    if the caller does not use them (rows can be gigantic).

    include_deleted=False adds `AND deleted_at IS NULL` — a flag for
    future calls (soft-delete filter from transcription.py, outside the scope of
    this increment).
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
    """Reads several rows by a list of ids (`WHERE id IN (...)`).

    Empty `ids` → `[]` without touching the DB (avoids `IN ()` — invalid SQL).
    """
    if not ids:
        return []
    cols = ", ".join(columns) if columns else _ALL_COLUMNS
    placeholders = ",".join("?" * len(ids))
    sql = f"SELECT {cols} FROM transcriptions WHERE id IN ({placeholders})"
    return conn.execute(sql, list(ids)).fetchall()


def exists(conn: sqlite3.Connection, transcription_id: int) -> bool:
    """Checks that a row exists without pulling data beyond the fact itself."""
    row = conn.execute(
        "SELECT 1 FROM transcriptions WHERE id = ?", (transcription_id,)
    ).fetchone()
    return row is not None


def delete_by_id(conn: sqlite3.Connection, transcription_id: int) -> None:
    """Physical deletion of a row (hard delete).

    Does NOT manage related tables (transcription_speaker_map etc.) — the same
    contract as before in retention.py/transcription.py: the caller
    is responsible for the FK cleanup order and the commit.
    """
    conn.execute("DELETE FROM transcriptions WHERE id = ?", (transcription_id,))
