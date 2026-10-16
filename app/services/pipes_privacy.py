"""Private-data filter before the pipes bot (K4, story 02).

The bot is a channel through which archive content goes to a third party (the Bot API is not end-to-end
encrypted), so a pipe goes through two steps, both without a model:

1. ``filter_data`` — BEFORE render (both Claude and the plain list): items from
   private directions (``RECALL_PRIVATE_CATEGORIES`` with descendants) and items with a
   stop word from ``private.txt`` are dropped. The stop list is NOT its own — the shared core
   ``app/services/privacy.py`` (the same one as for MCP).
2. ``scrub_text`` — AFTER render: IBAN, cards, phones, email and amounts with a
   currency → ``•••``; stop words are checked a second time — whatever survived
   blocks sending.

Fail closed: a direction was not verified (DB failure) → an item with a source is hidden.
An item without a source (digest topics) is checked against stop words only.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.core.i18n import server_lang, tr_n
from app.db.connection import get_db_connection
from app.services import privacy
from app.services.privacy import PrivacyError

logger = logging.getLogger(__name__)

MASK = "•••"

# The archive mixes Ukrainian and Russian: currencies and multipliers in both languages.
_CURRENCY = (r"(?:грн\.?|грив\w*|₴|UAH|USD|EUR|RUB|дол\.?|долар\w*|доллар\w*|бакс\w*|"
             r"євро|евро|руб\.?|рубл\w*|\$|€)")
_AMOUNT = r"(?:\d{1,3}(?:[  ,]\d{3})+|\d+)(?:[.,]\d+)?"
_MULT = (r"(?:\s?(?:k|к|тис\.?|тисяч\w*|тыс\.?|тысяч\w*|млн\.?|мільйон\w*|миллион\w*|"
         r"млрд\.?|m))?")

_MASKS = (
    # IBAN: UA + 27 characters, spaces in groups are allowed.
    re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"),
    # Email.
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    # Card: 16 digits, in groups separated by space/hyphen or contiguous.
    re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"),
    # Phone: international +… or Ukrainian 0XX XXX XX XX.
    re.compile(r"\+\d[\d\s()\-]{8,}\d"),
    re.compile(r"(?<![\d-])0\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}(?![\d-])"),
    re.compile(r"\(0\d{2}\)\s?\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)"),
    # Amount: currency before the number ($5k, €300) or after (12 000 UAH, 5k USD).
    re.compile(rf"[$€₴]\s?{_AMOUNT}{_MULT}", re.IGNORECASE),
    re.compile(rf"(?<![\d\w]){_AMOUNT}{_MULT}\s?{_CURRENCY}(?!\w)", re.IGNORECASE),
)


def _texts(item: Any) -> str:
    if isinstance(item, dict):
        return " ".join(str(v) for v in item.values() if isinstance(v, (str, int, float)))
    return str(item)


def _source_tid(item: Any, task_tids: dict[int, int]) -> Optional[int]:
    if not isinstance(item, dict):
        return None
    if item.get("transcription_id") is not None:
        return int(item["transcription_id"])
    if item.get("target_type") == "transcription" and item.get("target_id") is not None:
        return int(item["target_id"])
    if "task" in item and item.get("id") is not None:
        return task_tids.get(int(item["id"]))
    return None


def _task_tids(db_path: str, data: dict) -> dict[int, int]:
    """task id → recording, for items without transcription_id (dropped commitments)."""
    ids = {int(it["id"]) for res in data.values() if isinstance(res, dict)
           for v in res.values() if isinstance(v, list)
           for it in v if isinstance(it, dict) and "task" in it
           and it.get("transcription_id") is None and it.get("id") is not None}
    if not ids:
        return {}
    try:
        with get_db_connection(db_path) as conn:
            q = ",".join("?" * len(ids))
            return {r[0]: r[1] for r in conn.execute(
                f"SELECT id, transcription_id FROM action_items WHERE id IN ({q})", sorted(ids))}
    except Exception as e:  # noqa: BLE001 — failure = source unknown, hidden below
        logger.warning("pipes_privacy: task source not determined (%s) — hiding", e)
        raise PrivacyError(str(e)) from e


def filter_data(data: dict, *, db_path: str) -> tuple[dict, int]:
    """→ (data without private items, how many were hidden).

    ``PrivacyError`` from ``stop_words`` (an unreadable private.txt) propagates
    out — the pipe then sends nothing."""
    words = privacy.stop_words()
    lists = [it for res in data.values() if isinstance(res, dict)
             for v in res.values() if isinstance(v, list) for it in v]
    category_ok = True
    try:
        task_tids = _task_tids(db_path, data)
        tids = {_source_tid(it, task_tids) for it in lists}
        tids.discard(None)
        private = privacy.private_tids(db_path, tids)
    except PrivacyError:
        category_ok, task_tids, private = False, {}, set()

    hidden = 0

    def keep(item: Any) -> bool:
        nonlocal hidden
        if privacy.has_stop_word(_texts(item), words):
            hidden += 1
            return False
        has_source = isinstance(item, dict) and (
            item.get("transcription_id") is not None or item.get("target_id") is not None
            or ("task" in item and item.get("id") is not None))
        if has_source:
            tid = _source_tid(item, task_tids)
            if not category_ok or tid is None or tid in private:
                hidden += 1
                return False
        return True

    out: dict = {}
    for tool, res in data.items():
        if not isinstance(res, dict):
            out[tool] = res
            continue
        out[tool] = {k: ([it for it in v if keep(it)] if isinstance(v, list) else v)
                     for k, v in res.items()}
    if not category_ok:
        logger.warning("pipes_privacy: directions not verified — items with a source hidden")
    return out, hidden


def mask(text: str) -> str:
    """IBAN, cards, phones, email and amounts with a currency → ``•••``."""
    for rx in _MASKS:
        text = rx.sub(MASK, text)
    return text


def leaked_words(text: str, words: Optional[list[str]] = None) -> list[str]:
    """Stop words present in the text. ``words`` not passed → ``private.txt`` (``PrivacyError`` propagates out)."""
    low = text.casefold()
    return [w for w in (privacy.stop_words() if words is None else words) if w in low]


def scrub_text(text: str) -> tuple[str, list[str]]:
    """→ (text with masks, stop words that survived render).

    A non-empty list = cancel sending. ``PrivacyError`` propagates out."""
    text = mask(text)
    return text, leaked_words(text)


def hidden_line(n: int) -> str:
    return tr_n("telegram.brief.hidden", n, lang=server_lang())
