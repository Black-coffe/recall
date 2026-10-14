"""Що з архіву не віддається назовні (spec mcp-visual-privacy; той самий стоп-лист бере K4).

Три питання, без моделі:
  * чи запис у приватному напрямку — ``RECALL_PRIVATE_CATEGORIES`` (назви через кому, дефолт
    «Юридичне,Особисте»); назва ловиться на будь-якому рівні дерева разом з усіма нащадками,
    а тезки ховаються всі (неоднозначність = приховати);
  * чи текст має слово з ``private.txt`` (по рядку на слово/імʼя, ``#`` — коментар;
    регістронезалежна підрядкова перевірка ``casefold()`` у Python, не SQLite LOWER);
  * режим ``RECALL_MCP_VISUAL``: images | text | off.

Fail closed: збій перевірки напрямку кидає ``PrivacyError`` — викликач ховає все, що перевіряв;
невідомий режим = off.
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
    """Перевірку не вдалося виконати — нічого з перевірюваного не віддаємо."""


def visual_mode() -> str:
    raw = settings.env('RECALL_MCP_VISUAL').strip().lower()
    if raw in MODES:
        return raw
    logger.warning("RECALL_MCP_VISUAL=%r невідомий — візуальне через MCP вимкнено (off)", raw)
    return 'off'


def private_category_names() -> list[str]:
    raw = settings.env('RECALL_PRIVATE_CATEGORIES')
    return [n.strip().casefold() for n in raw.split(',') if n.strip()]


def words_file() -> str:
    return settings.env('RECALL_PRIVATE_WORDS_FILE').strip() or os.path.join(ROOT, 'private.txt')


def stop_words() -> list[str]:
    """Слова з private.txt; файла нема — стоп-слів нема. Нечитабельний файл — PrivacyError."""
    path = words_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8-sig') as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise PrivacyError(f'private.txt не читається: {e}') from e
    return [w.strip().casefold() for w in lines if w.strip() and not w.strip().startswith('#')]


def has_stop_word(text: Optional[str], words: Optional[list[str]] = None) -> bool:
    if not text:
        return False
    words = stop_words() if words is None else words
    low = text.casefold()
    return any(w in low for w in words)


def private_category_ids(conn) -> set[int]:
    """id вузлів із назвою зі стоп-листа + усі їхні нащадки."""
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


def private_tids(db_path: str, tids: Iterable[int]) -> set[int]:
    """Які з записів лежать у приватному напрямку. Збій БД → PrivacyError."""
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
    except Exception as e:  # noqa: BLE001 — будь-який збій перевірки = приховати
        logger.warning("privacy: напрямок не перевірено (%s) — приховую", e)
        raise PrivacyError(str(e)) from e


def screen_chunk_allowed(chunk: dict, private: set, words: list[str], mode: str) -> bool:
    """Чанк із тексту з екрана (speaker='екран') можна віддати через MCP?
    Інші чанки фільтр не стосується."""
    if (chunk.get('speaker') or '') != SCREEN_SPEAKER:
        return True
    if mode == 'off':
        return False
    if chunk.get('transcription_id') in private:
        return False
    return not has_stop_word(chunk.get('text') or chunk.get('excerpt') or '', words)


def filter_screen_chunks(db_path: str, chunks: list) -> tuple[list, int]:
    """→ (дозволені чанки, скільки прибрано). Збій перевірки — прибрати всі екранні."""
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
