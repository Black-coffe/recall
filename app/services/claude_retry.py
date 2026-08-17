"""Єдиний retry-хелпер для викликів Claude API (REMEDIATION_PLAN Волна 3, T6.3).

Раніше кожен виклик ``client.messages.create/stream`` був загорнутий у
``try/except: return None`` (або взагалі нічим) — жодного повтору на
транзиентних помилках (429 rate-limit, 5xx/529 overloaded, timeout, обрив
з'єднання). Один випадковий 429 валив увесь polish/RAG/copilot-виклик.

Ретраїмо ЛИШЕ транзиентне: 429, 5xx (включно з 529 overloaded), мережеві
помилки/timeout. НЕ ретраїмо 4xx окрім 429 (400 invalid_request, 401 auth,
403 permission, 404 not_found тощо) — повтор того самого невалідного запиту
нічого не полагодить, лише витратить час і (для non-idempotent запитів)
гроші.

Для стрімінгу: ``call_with_retry`` очікує callable, що виконує ВЕСЬ виклик
заново (відкриває ``messages.stream(...)`` з нуля і повертає результат). Не
намагається резюмувати ВСЕРЕДИНІ вже відкритого стріму — якщо частину
відповіді вже віддано споживачу (наприклад, SSE-клієнту), повторювати
виклик небезпечно (дубльований/пошкоджений вивід); у такому разі виклик
має ловити помилку САМ і вирішувати, чи ретраїти, чи прокидати далі
(див. ``app/services/rag.py::answer_question_stream``).
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar


logger = logging.getLogger(__name__)

T = TypeVar("T")

# До 3 спроб разом (1 початкова + 2 повтори) — обмежений ліміт, не нескінченний.
DEFAULT_MAX_RETRIES = 2
DEFAULT_BASE_DELAY = 1.0    # секунди, подвоюється щоразу (exponential backoff)
DEFAULT_MAX_DELAY = 20.0    # верхня межа затримки між спробами


def is_retryable_error(exc: BaseException) -> bool:
    """Транзиентна помилка Claude API (429 / 5xx / 529 / мережа-timeout)?

    4xx окрім 429 (400/401/403/404/422 тощо) — НЕ транзиентні, повертає False.
    """
    try:
        import anthropic
    except ImportError:
        return False
    # Мережеві помилки / timeout — немає HTTP-відповіді взагалі, завжди транзиентні.
    # anthropic.APITimeoutError — підклас APIConnectionError.
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool):  # bool є підкласом int — відсікаємо явно
        return False
    if isinstance(status, int):
        return status == 429 or status >= 500
    return False


def backoff_delay(attempt: int, base_delay: float = DEFAULT_BASE_DELAY,
                   max_delay: float = DEFAULT_MAX_DELAY) -> float:
    """Затримка (сек) перед `attempt`-им повтором (0-indexed): exponential + jitter."""
    return min(base_delay * (2 ** attempt), max_delay) + random.uniform(0, 0.5)


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    what: str = "claude-call",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Виконати ``fn()`` з retry на транзиентних помилках, exponential backoff.

    ``fn`` викликається ПОВНІСТЮ ЗАНОВО на кожній спробі (для стрімінгу — весь
    ``with client.messages.stream(...) as stream: ...`` блок від початку, а не
    резюме середини). Після вичерпання спроб — останній виняток прокидається
    як є (caller лишає свою існуючу try/except-поведінку без змін).
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable_error(exc):
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "[%s] транзиентна помилка Claude API (спроба %d/%d): %s — повтор через %.1fс",
                what, attempt + 1, max_retries + 1, exc, delay,
            )
            sleep(delay)
            attempt += 1
