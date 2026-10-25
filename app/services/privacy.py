"""What of the archive is not given out (spec mcp-visual-privacy; K4 takes the same stop list).

Three questions, no model:
  * whether a recording is in a private direction — ``RECALL_PRIVATE_CATEGORIES`` (names comma-separated, default
    «Юридичне,Особисте»); a name is caught at any level of the tree together with all descendants,
    and namesakes are all hidden (ambiguity = hide);
  * whether the text has a word from ``private.txt`` (one word/name per line, ``#`` is a comment;
    case-insensitive substring check with ``casefold()`` in Python, not SQLite LOWER);
  * the ``RECALL_MCP_VISUAL`` mode: images | text | off.

Fail closed: a direction check failure raises ``PrivacyError`` — the caller hides everything it was checking;
an unknown mode = off.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from app.core import settings
from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODES = ('images', 'text', 'off')
SCREEN_SPEAKER = 'екран'


class PrivacyError(Exception):
    """The check could not be performed — nothing of what is being checked is given out."""


def visual_mode() -> str:
    raw = settings.env('RECALL_MCP_VISUAL').strip().lower()
    if raw in MODES:
        return raw
    logger.warning("RECALL_MCP_VISUAL=%r is unknown — visual via MCP disabled (off)", raw)
    return 'off'


def private_category_names() -> list[str]:
    raw = settings.env('RECALL_PRIVATE_CATEGORIES')
    return [n.strip().casefold() for n in raw.split(',') if n.strip()]


def words_file() -> str:
    return settings.env('RECALL_PRIVATE_WORDS_FILE').strip() or os.path.join(ROOT, 'private.txt')


def stop_words() -> list[str]:
    """Words from private.txt; no file — no stop words. An unreadable file — PrivacyError."""
    path = words_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8-sig') as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise PrivacyError(f'private.txt is not readable: {e}') from e
    return [w.strip().casefold() for w in lines if w.strip() and not w.strip().startswith('#')]


def has_stop_word(text: Optional[str], words: Optional[list[str]] = None) -> bool:
    if not text:
        return False
    words = stop_words() if words is None else words
    low = text.casefold()
    return any(w in low for w in words)


def private_category_ids(conn) -> set[int]:
    """Ids of nodes whose name is in the stop list + all their descendants."""
    names = set(private_category_names())
    if not names:
        return set()
    rows = conn.execute('SELECT id, name, parent_id FROM categories').fetchall()
    children: dict = {}
    for r in rows:
        children.setdefault(r[2], []).append(r[0])
    stack = [r[0] for r in rows if (r[1] or '').casefold() in names]
    out: set[int] = set()
    while stack:
        cid = stack.pop()
        if cid in out:
            continue
        out.add(cid)
        stack.extend(children.get(cid, []))
    return out


def category_change_exposes(db_path: str, old_category_id: Optional[int],
                            new_category_id: Optional[int]) -> bool:
    """True when a move takes something out of a private direction into an open one (or none).
    DB failure -> PrivacyError."""
    try:
        with get_db_connection(db_path) as conn:
            cats = private_category_ids(conn)
    except Exception as e:  # noqa: BLE001 - any check failure = refuse
        logger.warning("privacy: direction change not verified (%s)", e)
        raise PrivacyError(str(e)) from e
    if old_category_id not in cats:
        return False
    return new_category_id is None or new_category_id not in cats


def private_tids(db_path: str, tids: Iterable[int]) -> set[int]:
    """Which of the records lie in a private direction. DB failure → PrivacyError."""
    tids = sorted({int(t) for t in tids if t is not None})
    if not tids:
        return set()
    try:
        with get_db_connection(db_path) as conn:
            cats = private_category_ids(conn)
            if not cats:
                return set()
            out: set[int] = set()
            for i in range(0, len(tids), 500):
                part = tids[i:i + 500]
                q = ','.join('?' * len(part))
                out.update(r[0] for r in conn.execute(
                    f'SELECT id FROM transcriptions WHERE id IN ({q}) AND category_id IN '
                    f'({",".join("?" * len(cats))})', [*part, *sorted(cats)]))
            return out
    except Exception as e:  # noqa: BLE001 — any check failure = hide
        logger.warning("privacy: direction not verified (%s) — hiding", e)
        raise PrivacyError(str(e)) from e


def filter_chats(rows: list, *, db_path: str, key_id: str = 'chat_id', key_title: str = 'title',
                 key_username: str = 'username', key_category: Optional[str] = 'category_id') -> list:
    """Telegram chats or dialogs a non-UI caller may see. A row is dropped when its title or
    username has a stop word, its own direction (``key_category``) is private, or its id is a
    monitored chat in a private direction (descendants included). Unreadable stop words or a
    failed direction check -> ``PrivacyError`` (the caller refuses the whole call)."""
    words = stop_words()
    try:
        with get_db_connection(db_path) as conn:
            cats = private_category_ids(conn)
            private_ids: set = set()
            if cats:
                marks = ','.join('?' * len(cats))
                private_ids = {r[0] for r in conn.execute(
                    f'SELECT chat_id FROM tg_monitored_chats WHERE category_id IN ({marks})',
                    sorted(cats))}
    except Exception as e:  # noqa: BLE001 - any check failure = refuse
        logger.warning("privacy: chats not verified (%s)", e)
        raise PrivacyError(str(e)) from e
    kept = []
    for row in rows:
        if has_stop_word(row.get(key_title), words) or has_stop_word(row.get(key_username), words):
            continue
        if key_category and row.get(key_category) in cats:
            continue
        if row.get(key_id) in private_ids:
            continue
        kept.append(row)
    return kept


def screen_chunk_allowed(chunk: dict, private: set, words: list[str], mode: str) -> bool:
    """May a chunk from on-screen text (speaker='екран') be given out via MCP?
    Other chunks are not affected by the filter."""
    if (chunk.get('speaker') or '') != SCREEN_SPEAKER:
        return True
    if mode == 'off':
        return False
    if chunk.get('transcription_id') in private:
        return False
    return not has_stop_word(chunk.get('text') or chunk.get('excerpt') or '', words)


def filter_screen_chunks(db_path: str, chunks: list) -> tuple[list, int]:
    """→ (allowed chunks, how many removed). Check failure — remove all on-screen ones."""
    screen = [c for c in chunks if (c.get('speaker') or '') == SCREEN_SPEAKER]
    if not screen:
        return chunks, 0
    try:
        mode = visual_mode()
        words = stop_words()
        private = private_tids(db_path, [c.get('transcription_id') for c in screen])
        kept = [c for c in chunks if screen_chunk_allowed(c, private, words, mode)]
    except PrivacyError:
        kept = [c for c in chunks if (c.get('speaker') or '') != SCREEN_SPEAKER]
    return kept, len(chunks) - len(kept)
