"""Repository для таблиці `speakers` — другий за дублюванням запит у T7.2:
`SELECT id, name, color, is_self, usage_count, created_at, updated_at FROM
speakers WHERE id = ?` повторювався тричі дослівно в app/blueprints/speakers.py
(create/update — читання рядка одразу після INSERT/UPDATE для серіалізації
відповіді).
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

_ALL_COLUMNS = "*"

# Повний набір колонок, що йде в API-серіалізацію спікера (create/update
# handlers). Явний список — не SELECT * (стабільний порядок, не залежить
# від майбутніх ALTER TABLE).
FULL_COLUMNS = ("id", "name", "color", "is_self", "usage_count", "created_at", "updated_at")


def get_by_id(
    conn: sqlite3.Connection,
    speaker_id: int,
    columns: Optional[Sequence[str]] = None,
) -> Optional[sqlite3.Row]:
    """Читає один рядок `speakers` за id. columns=None → SELECT *."""
    cols = ", ".join(columns) if columns else _ALL_COLUMNS
    return conn.execute(
        f"SELECT {cols} FROM speakers WHERE id = ?", (speaker_id,)
    ).fetchone()


def get_full_by_id(conn: sqlite3.Connection, speaker_id: int) -> Optional[sqlite3.Row]:
    """Читає повний набір колонок, потрібних для серіалізації API-відповіді
    (`_serialize_speaker` у speakers.py). Заміняє потрійний дубль SELECT."""
    return get_by_id(conn, speaker_id, FULL_COLUMNS)
