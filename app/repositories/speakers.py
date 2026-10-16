"""Repository for the `speakers` table — the second most duplicated query in T7.2:
`SELECT id, name, color, is_self, usage_count, created_at, updated_at FROM
speakers WHERE id = ?` was repeated three times verbatim in app/blueprints/speakers.py
(create/update — reading the row right after INSERT/UPDATE to serialize
the response).
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

_ALL_COLUMNS = "*"

# The full set of columns that goes into the API serialization of a speaker (create/update
# handlers). An explicit list — not SELECT * (stable order, independent
# of future ALTER TABLE).
FULL_COLUMNS = ("id", "name", "color", "is_self", "usage_count", "created_at", "updated_at")


def get_by_id(
    conn: sqlite3.Connection,
    speaker_id: int,
    columns: Optional[Sequence[str]] = None,
) -> Optional[sqlite3.Row]:
    """Reads one `speakers` row by id. columns=None → SELECT *."""
    cols = ", ".join(columns) if columns else _ALL_COLUMNS
    return conn.execute(
        f"SELECT {cols} FROM speakers WHERE id = ?", (speaker_id,)
    ).fetchone()


def get_full_by_id(conn: sqlite3.Connection, speaker_id: int) -> Optional[sqlite3.Row]:
    """Reads the full set of columns needed to serialize an API response
    (`_serialize_speaker` in speakers.py). Replaces the triple SELECT duplicate."""
    return get_by_id(conn, speaker_id, FULL_COLUMNS)
