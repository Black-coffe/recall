"""Audit log of mutating API requests (table ``mcp_actions``, migration v53)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from app.db.connection import get_db_connection

_COLUMNS = (
    "at", "request_id", "actor", "claimed_actor", "claimed_tool", "method", "path",
    "endpoint", "action", "object_ref", "summary", "decision", "http_status",
    "error_code", "duration_ms",
)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def _insert(conn: sqlite3.Connection, row: dict) -> int:
    data = dict(row)
    if not data.get("at"):
        data["at"] = now_iso()
    cur = conn.execute(
        f"INSERT INTO mcp_actions ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
        tuple(data.get(c) for c in _COLUMNS),
    )
    return int(cur.lastrowid)


def insert(conn_or_path: Union[str, sqlite3.Connection], row: dict) -> int:
    """Insert one audit row; returns its id. A path opens (and commits) its own connection."""
    if isinstance(conn_or_path, sqlite3.Connection):
        return _insert(conn_or_path, row)
    with get_db_connection(conn_or_path) as conn:
        new_id = _insert(conn, row)
        conn.commit()
    return new_id


def query(
    db_path: str,
    *,
    since: Optional[str] = None,
    actor: Optional[str] = None,
    decision: Optional[str] = None,
    action: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status_min: Optional[int] = None,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    """Newest first; ``limit`` is clamped to 1..200."""
    where: list[str] = []
    params: list[Any] = []
    if since:
        where.append("at >= ?")
        params.append(since)
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if decision:
        where.append("decision = ?")
        params.append(decision)
    if action:
        where.append("action = ?")
        params.append(action)
    if path_prefix:
        where.append("substr(path, 1, ?) = ?")
        params.extend([len(path_prefix), path_prefix])
    if status_min is not None:
        where.append("http_status >= ?")
        params.append(int(status_min))
    if before_id is not None:
        where.append("id < ?")
        params.append(int(before_id))
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(200, limit))
    sql = "SELECT * FROM mcp_actions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def purge_older_than(db_path: str, days: float = 90) -> int:
    """Delete rows older than ``days``; returns how many."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(_TS_FORMAT)
    with get_db_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM mcp_actions WHERE at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0
