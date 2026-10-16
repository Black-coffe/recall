"""Single retry helper for Claude API calls (REMEDIATION_PLAN Wave 3, T6.3).

Previously every ``client.messages.create/stream`` call was wrapped in
``try/except: return None`` (or in nothing at all) — no retry on
transient errors (429 rate-limit, 5xx/529 overloaded, timeout, dropped
connection). One random 429 would fail the whole polish/RAG/copilot call.

We retry ONLY the transient: 429, 5xx (including 529 overloaded), network
errors/timeout. We do NOT retry 4xx other than 429 (400 invalid_request, 401 auth,
403 permission, 404 not_found, etc.) — repeating the same invalid request
fixes nothing, only wastes time and (for non-idempotent requests)
money.

For streaming: ``call_with_retry`` expects a callable that performs the WHOLE call
anew (opens ``messages.stream(...)`` from scratch and returns the result). It does not
try to resume INSIDE an already opened stream — if part of the
answer has already been handed to the consumer (for example, an SSE client), repeating
the call is unsafe (duplicated/corrupted output); in that case the caller
must catch the error ITSELF and decide whether to retry or to propagate
(see ``app/services/rag.py::answer_question_stream``).
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar


logger = logging.getLogger(__name__)

T = TypeVar("T")

# Up to 3 attempts in total (1 initial + 2 retries) — a bounded limit, not infinite.
DEFAULT_MAX_RETRIES = 2
DEFAULT_BASE_DELAY = 1.0    # seconds, doubles each time (exponential backoff)
DEFAULT_MAX_DELAY = 20.0    # upper bound of the delay between attempts


def is_retryable_error(exc: BaseException) -> bool:
    """Is this a transient Claude API error (429 / 5xx / 529 / network-timeout)?

    4xx other than 429 (400/401/403/404/422 etc.) are NOT transient, returns False.
    """
    try:
        import anthropic
    except ImportError:
        return False
    # Network errors / timeout — no HTTP response at all, always transient.
    # anthropic.APITimeoutError is a subclass of APIConnectionError.
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool):  # bool is a subclass of int — exclude explicitly
        return False
    if isinstance(status, int):
        return status == 429 or status >= 500
    return False


def backoff_delay(attempt: int, base_delay: float = DEFAULT_BASE_DELAY,
                   max_delay: float = DEFAULT_MAX_DELAY) -> float:
    """Delay (sec) before the `attempt`-th retry (0-indexed): exponential + jitter."""
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
    """Run ``fn()`` with retry on transient errors, exponential backoff.

    ``fn`` is called COMPLETELY ANEW on every attempt (for streaming — the whole
    ``with client.messages.stream(...) as stream: ...`` block from the start, not
    a resume of the middle). After attempts are exhausted, the last exception is re-raised
    as is (the caller keeps its existing try/except behaviour unchanged).
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
                "[%s] transient Claude API error (attempt %d/%d): %s — retry in %.1fs",
                what, attempt + 1, max_retries + 1, exc, delay,
            )
            sleep(delay)
            attempt += 1
