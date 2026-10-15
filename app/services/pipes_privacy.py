"""Фільтр приватного перед ботом труб (K4, story 02).

Бот — канал, яким зміст архіву йде третій стороні (Bot API не наскрізно
шифрований), тому труба проходить два кроки, обидва без моделі:

1. ``filter_data`` — ДО рендеру (і Claude, і простого списку): пункти з
   приватних напрямків (``RECALL_PRIVATE_CATEGORIES`` з нащадками) і пункти зі
   стоп-словом з ``private.txt`` викидаються. Стоп-лист НЕ свій — спільне ядро
   ``app/services/privacy.py`` (той самий, що для MCP).
2. ``scrub_text`` — ПІСЛЯ рендеру: IBAN, картки, телефони, email і суми з
   валютою → ``•••``; стоп-слова перевіряються вдруге — що пережило, те
   блокує відправку.

Fail closed: напрямок не перевірився (збій БД) → пункт із джерелом прихований.
Пункт без джерела (теми зі зводу) перевіряється лише стоп-словами.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.db.connection import get_db_connection
from app.services import privacy
from app.services.privacy import PrivacyError

logger = logging.getLogger(__name__)

MASK = "•••"

# Архів змішує українську й російську: валюти й множники в обох мовах.
_CURRENCY = (r"(?:грн\.?|грив\w*|₴|UAH|USD|EUR|RUB|дол\.?|долар\w*|доллар\w*|бакс\w*|"
             r"євро|евро|руб\.?|рубл\w*|\$|€)")
_AMOUNT = r"(?:\d{1,3}(?:[  ,]\d{3})+|\d+)(?:[.,]\d+)?"
_MULT = (r"(?:\s?(?:k|к|тис\.?|тисяч\w*|тыс\.?|тысяч\w*|млн\.?|мільйон\w*|миллион\w*|"
         r"млрд\.?|m))?")

_MASKS = (
    # IBAN: UA + 27 знаків, допускаємо пробіли групами.
    re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"),
    # Email.
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    # Картка: 16 цифр, групами через пробіл/дефіс або підряд.
    re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"),
    # Телефон: міжнародний +… або український 0XX XXX XX XX.
    re.compile(r"\+\d[\d\s()\-]{8,}\d"),
    re.compile(r"(?<![\d-])0\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}(?![\d-])"),
    re.compile(r"\(0\d{2}\)\s?\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)"),
    # Сума: валюта перед числом ($5k, €300) або після (12 000 грн, 5к USD).
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
    """id задачі → запис, для пунктів без transcription_id (забуті обіцянки)."""
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
    except Exception as e:  # noqa: BLE001 — збій = джерело невідоме, нижче ховаємо
        logger.warning("pipes_privacy: джерело задач не визначено (%s) — приховую", e)
        raise PrivacyError(str(e)) from e


def filter_data(data: dict, *, db_path: str) -> tuple[dict, int]:
    """→ (дані без приватних пунктів, скільки приховано).

    ``PrivacyError`` зі ``stop_words`` (нечитабельний private.txt) летить
    назовні — труба тоді не шле нічого."""
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
        logger.warning("pipes_privacy: напрямки не перевірено — пункти з джерелом приховано")
    return out, hidden


def mask(text: str) -> str:
    """IBAN, картки, телефони, email і суми з валютою → ``•••``."""
    for rx in _MASKS:
        text = rx.sub(MASK, text)
    return text


def leaked_words(text: str, words: Optional[list[str]] = None) -> list[str]:
    """Стоп-слова, що є в тексті. ``words`` не передано → ``private.txt`` (``PrivacyError`` назовні)."""
    low = text.casefold()
    return [w for w in (privacy.stop_words() if words is None else words) if w in low]


def scrub_text(text: str) -> tuple[str, list[str]]:
    """→ (текст із масками, стоп-слова, що пережили рендер).

    Непорожній список = відправку скасувати. ``PrivacyError`` летить назовні."""
    text = mask(text)
    return text, leaked_words(text)


def hidden_line(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = "пункт приховано"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "пункти приховано"
    else:
        word = "пунктів приховано"
    return f"🔒 {n} {word}, дивись у Recall"
