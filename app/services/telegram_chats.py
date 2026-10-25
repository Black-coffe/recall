"""Chat changes an agent may make on the Telegram side, with a journal for undo (mcp-telegram-automation).

Every change of a monitored chat's direction or enabled flag made here writes one row to
``tg_chat_events`` (migration v56) carrying the value the chat had before, so the daily MCP summary
can offer "restore the previous one". Values are stored as text: ``category_id`` or ``''`` for none,
``'0'|'1'`` for enabled.

The listener learns the enabled set by polling ``tg_monitored_chats WHERE enabled = 1``
(``telegram_listener.py``), so a DB write is all the SPA toggle does and all this service does.
No Flask import: the blueprint and the brief buttons call it with a ``db_path``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.db.connection import get_db_connection
from app.services import categories as cat_svc
from app.services import privacy

SOURCES = ("mcp", "ui", "bot")


class NotMonitored(LookupError):
    """The chat has no ``tg_monitored_chats`` row (and none could be created)."""


class PrivateCategoryChange(Exception):
    """The move would take a chat out of a private direction."""


class ListenerOffline(Exception):
    """The listener's dialogs are needed and the listener did not answer."""


class UndoNotLast(Exception):
    """A later change of the same chat and kind exists (a journal row, or a current value that is no
    longer the event's ``new_value``); undoing this one would overwrite it."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _text(value) -> str:
    return "" if value is None else str(int(value))


def _category_from_text(value) -> Optional[int]:
    return int(value) if value not in (None, "") else None


def _journal(conn, chat_id: int, kind: str, old, new, source: str, actor) -> int:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    cur = conn.execute(
        "INSERT INTO tg_chat_events (at, chat_id, kind, old_value, new_value, source, actor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (_now(), chat_id, kind, old, new, source, actor))
    return cur.lastrowid


def _row(conn, chat_id: int):
    return conn.execute("SELECT * FROM tg_monitored_chats WHERE chat_id = ?", (chat_id,)).fetchone()


def _check_category(conn, category_id) -> Optional[str]:
    """Name of an existing direction; ``None`` for "no direction"; ``ValueError`` for an unknown id."""
    if category_id is None:
        return None
    row = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown category_id {category_id}")
    return row["name"]


def is_monitored(db_path: str, chat_id: int) -> bool:
    with get_db_connection(db_path) as conn:
        return _row(conn, chat_id) is not None


def get_chats(db_path: str, *, category_id=None) -> list[dict]:
    """Monitored chats with ``category_name`` and ``tags``, as ``GET /api/telegram/chats`` returns them.
    A bad ``category_id`` raises ``ValueError`` (``categories.filter_sql``)."""
    frag, params = cat_svc.filter_sql("m.category_id", category_id)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT m.chat_id, m.title, m.username, m.chat_type, m.enabled, "
            "m.category_id, m.last_message_id, c.name AS category_name "
            "FROM tg_monitored_chats m LEFT JOIN categories c ON c.id = m.category_id "
            f"WHERE 1=1{frag} ORDER BY m.enabled DESC, m.title", params
        ).fetchall()
        chats = []
        for r in rows:
            d = dict(r)
            d["tags"] = cat_svc.chat_tags(conn, d["chat_id"])
            chats.append(d)
    return chats


def _set_category(conn, db_path: str, chat_id: int, category_id, source: str, actor) -> dict:
    row = _row(conn, chat_id)
    if row is None:
        raise NotMonitored(chat_id)
    name = _check_category(conn, category_id)
    old = row["category_id"]
    out = {"chat_id": chat_id, "category_id": category_id, "category_name": name,
           "previous_category_id": old, "event_id": None}
    if old == category_id:
        return out
    if privacy.category_change_exposes(db_path, old, category_id):
        raise PrivateCategoryChange(chat_id)
    conn.execute("UPDATE tg_monitored_chats SET category_id = ? WHERE chat_id = ?", (category_id, chat_id))
    out["event_id"] = _journal(conn, chat_id, "category", _text(old), _text(category_id), source, actor)
    return out


def set_category(db_path: str, chat_id: int, category_id: Optional[int], *, source: str,
                 actor: Optional[str] = None) -> dict:
    """Change the chat's direction only (no re-categorisation of stored messages, no re-embed).
    ``PrivacyError`` from the check propagates."""
    with get_db_connection(db_path) as conn:
        out = _set_category(conn, db_path, chat_id, category_id, source, actor)
        conn.commit()
    return out


def _set_enabled(conn, chat_id: int, enabled: bool, source: str, actor) -> dict:
    row = _row(conn, chat_id)
    if row is None:
        raise NotMonitored(chat_id)
    new, old = int(bool(enabled)), int(row["enabled"] or 0)
    out = {"chat_id": chat_id, "enabled": bool(new), "event_id": None}
    if new == old:
        return out
    conn.execute("UPDATE tg_monitored_chats SET enabled = ? WHERE chat_id = ?", (new, chat_id))
    out["event_id"] = _journal(conn, chat_id, "enabled", str(old), str(new), source, actor)
    return out


def set_enabled(db_path: str, chat_id: int, enabled: bool, *, source: str,
                actor: Optional[str] = None) -> dict:
    with get_db_connection(db_path) as conn:
        out = _set_enabled(conn, chat_id, enabled, source, actor)
        conn.commit()
    return out


def _dialog_for(dialogs: list, chat_id: int) -> Optional[dict]:
    for d in dialogs or []:
        if isinstance(d, dict) and (d.get("id", d.get("chat_id"))) == chat_id:
            return d
    return None


def enable_chat(db_path: str, chat_id: int, *, category_id: Optional[int] = None,
                dialogs: Optional[list] = None, source: str, actor: Optional[str] = None) -> dict:
    """Enable a monitored chat, or create it from the listener's dialogs. History is not loaded."""
    with get_db_connection(db_path) as conn:
        if _row(conn, chat_id) is not None:
            if category_id is not None:
                _set_category(conn, db_path, chat_id, category_id, source, actor)
            res = _set_enabled(conn, chat_id, True, source, actor)
            conn.commit()
            return {"chat_id": chat_id, "enabled": True, "created": False, "event_id": res["event_id"]}
        if dialogs is None:
            raise ListenerOffline(chat_id)
        d = _dialog_for(dialogs, chat_id)
        if d is None:
            raise NotMonitored(chat_id)
        _check_category(conn, category_id)
        conn.execute(
            "INSERT INTO tg_monitored_chats (chat_id, title, username, chat_type, enabled, category_id) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (chat_id, d.get("title"), d.get("username"), d.get("chat_type", d.get("type")), category_id))
        event_id = _journal(conn, chat_id, "enabled", None, "1", source, actor)
        conn.commit()
    return {"chat_id": chat_id, "enabled": True, "created": True, "event_id": event_id}


def undo_event(db_path: str, event_id: int, *, source: str, actor: Optional[str] = None) -> dict:
    """Restore the value a journal row recorded as previous. Never creates a chat row."""
    with get_db_connection(db_path) as conn:
        ev = conn.execute("SELECT * FROM tg_chat_events WHERE id = ?", (event_id,)).fetchone()
        if ev is None:
            raise LookupError(f"event {event_id}")
        if ev["undone_at"]:
            raise ValueError("undone")
        later = conn.execute(
            "SELECT 1 FROM tg_chat_events WHERE chat_id = ? AND kind = ? AND id > ?",
            (ev["chat_id"], ev["kind"], ev["id"])).fetchone()
        if later is not None:
            raise UndoNotLast(event_id)
        chat_id = ev["chat_id"]
        # The SPA routes change chats without a journal row: refuse when the chat no longer holds
        # the value this event set, or undo would overwrite the owner's later change.
        row = _row(conn, chat_id)
        current = None if row is None else (
            _text(row["category_id"]) if ev["kind"] == "category" else str(int(row["enabled"] or 0)))
        if current != (ev["new_value"] or ""):
            raise UndoNotLast(event_id)
        if ev["kind"] == "category":
            restored = _category_from_text(ev["old_value"])
            res = _set_category(conn, db_path, chat_id, restored, source, actor)
        else:
            if ev["old_value"] in (None, ""):
                raise ValueError("created")  # the event created the row; undo never deletes it
            restored = ev["old_value"] == "1"
            res = _set_enabled(conn, chat_id, restored, source, actor)
        conn.execute("UPDATE tg_chat_events SET undone_at = ?, undone_by_event_id = ? WHERE id = ?",
                     (_now(), res["event_id"], ev["id"]))
        conn.commit()
    return {"chat_id": chat_id, "kind": ev["kind"], "restored_value": restored}


def list_events(db_path: str, *, since: str, source: Optional[str] = None,
                undone: bool = False) -> list[dict]:
    """Journal rows at or after ``since`` with the chat's title and current direction;
    undone rows only when ``undone`` is true."""
    sql = ("SELECT e.*, m.title AS chat_title, m.category_id AS category_id "
           "FROM tg_chat_events e LEFT JOIN tg_monitored_chats m ON m.chat_id = e.chat_id "
           "WHERE e.at >= ?")
    params: list = [since]
    if source is not None:
        sql += " AND e.source = ?"
        params.append(source)
    if not undone:
        sql += " AND e.undone_at IS NULL"
    with get_db_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY e.id", params).fetchall()]
