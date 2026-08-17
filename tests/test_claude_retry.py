"""Тести retry-хелпера для викликів Claude API (REMEDIATION_PLAN Волна 3, T6.3).

Покриваємо:
- is_retryable_error: 429/5xx/529/мережа → True; 400/401/403/404 → False.
- call_with_retry: 429 потім 200 → повертає результат другої спроби (1 retry).
- call_with_retry: невалідний запит (400) НЕ ретраїться — падає одразу.
- call_with_retry: ліміт спроб — не нескінченний, після вичерпання прокидає
  останній виняток.
- backoff_delay: монотонно зростає (exponential) і не перевищує max_delay.
"""
from __future__ import annotations

import pytest

from app.services.claude_retry import (
    DEFAULT_MAX_RETRIES,
    backoff_delay,
    call_with_retry,
    is_retryable_error,
)


class _FakeAPIStatusError(Exception):
    """Мінімальний двійник anthropic.APIStatusError — без залежності від SDK
    у тестовому import-графі (is_retryable_error перевіряє status_code
    duck-typed, не isinstance на конкретний статус-клас)."""

    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


class _FakeAPIConnectionError(Exception):
    """Двійник мережевої помилки (timeout/обрив) — без status_code взагалі."""


# ---------------------------------------------------------------- is_retryable_error

class TestIsRetryableError:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
    def test_retryable_statuses(self, status):
        assert is_retryable_error(_FakeAPIStatusError(status)) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
    def test_non_retryable_statuses(self, status):
        assert is_retryable_error(_FakeAPIStatusError(status)) is False

    def test_plain_exception_without_status_code_not_retryable(self):
        assert is_retryable_error(RuntimeError("no status here")) is False

    def test_real_anthropic_rate_limit_error_is_retryable(self):
        anthropic = pytest.importorskip("anthropic")
        # RateLimitError у SDK інстанціюється з (message, response=..., body=...);
        # найпростіше — сконструювати мінімальний об'єкт зі status_code, як
        # робить is_retryable_error (duck-typed по .status_code, не isinstance
        # конкретного підкласу окрім APIConnectionError).
        exc = anthropic.APIStatusError.__new__(anthropic.RateLimitError)
        exc.status_code = 429
        assert is_retryable_error(exc) is True

    def test_real_anthropic_bad_request_error_not_retryable(self):
        anthropic = pytest.importorskip("anthropic")
        exc = anthropic.APIStatusError.__new__(anthropic.BadRequestError)
        exc.status_code = 400
        assert is_retryable_error(exc) is False


# ---------------------------------------------------------------- call_with_retry

class TestCallWithRetry:
    def test_success_on_first_try_no_retry(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        result = call_with_retry(fn, sleep=lambda _s: None)
        assert result == "ok"
        assert len(calls) == 1

    def test_429_then_200_retries_once_and_succeeds(self):
        """Мок-клієнт: перший виклик 429, другий — успіх. Головний сценарій T6.3."""
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeAPIStatusError(429, "rate limited")
            return {"content": "success after retry"}

        slept = []
        result = call_with_retry(fn, sleep=slept.append)
        assert result == {"content": "success after retry"}
        assert calls["n"] == 2
        assert len(slept) == 1  # рівно один retry-sleep

    def test_529_overloaded_then_200_retries(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeAPIStatusError(529, "overloaded")
            return "recovered"

        assert call_with_retry(fn, sleep=lambda _s: None) == "recovered"
        assert calls["n"] == 2

    def test_400_bad_request_not_retried_raises_immediately(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _FakeAPIStatusError(400, "invalid_request_error")

        with pytest.raises(_FakeAPIStatusError):
            call_with_retry(fn, sleep=lambda _s: None)
        assert calls["n"] == 1  # жодного retry на невалідному запиті

    def test_network_error_is_retried(self):
        anthropic = pytest.importorskip("anthropic")
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise anthropic.APIConnectionError(request=None)
            return "ok"

        assert call_with_retry(fn, sleep=lambda _s: None) == "ok"
        assert calls["n"] == 2

    def test_retry_limit_is_bounded_not_infinite(self):
        """Персистентна 429 → рівно max_retries+1 спроб, потім прокидає виняток."""
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _FakeAPIStatusError(429, "always rate limited")

        with pytest.raises(_FakeAPIStatusError):
            call_with_retry(fn, max_retries=2, sleep=lambda _s: None)
        assert calls["n"] == 3  # 1 початкова спроба + 2 retry, не більше

    def test_default_max_retries_is_small_bounded_value(self):
        """Ліміт+backoff без нескінченного повтору — дефолт має бути малим числом."""
        assert 1 <= DEFAULT_MAX_RETRIES <= 3

    def test_streaming_style_callable_reexecuted_fully_on_retry(self):
        """Для стріму: fn() відкриває виклик ЗАНОВО щоразу (не резюмиться
        середина) — перевіряємо, що side-effect (напр. 'відкриття стріму')
        рахується стільки ж разів, скільки спроб."""
        opens = []

        def fn():
            opens.append(len(opens))  # симулює client.messages.stream(...) відкриття
            if len(opens) < 2:
                raise _FakeAPIStatusError(500, "server error")
            return "".join(str(x) for x in range(3))  # симулює зібраний текст стріму

        result = call_with_retry(fn, sleep=lambda _s: None)
        assert result == "012"
        assert opens == [0, 1]  # рівно 2 повних відкриття виклику


# ---------------------------------------------------------------- backoff_delay

class TestBackoffDelay:
    def test_grows_with_attempt_number(self):
        d0 = backoff_delay(0, base_delay=1.0, max_delay=100.0)
        d1 = backoff_delay(1, base_delay=1.0, max_delay=100.0)
        d2 = backoff_delay(2, base_delay=1.0, max_delay=100.0)
        # jitter (0..0.5) додається, тож порівнюємо мінімальні межі експоненти
        assert d0 >= 1.0
        assert d1 >= 2.0
        assert d2 >= 4.0

    def test_capped_at_max_delay(self):
        d = backoff_delay(10, base_delay=1.0, max_delay=5.0)
        assert d <= 5.0 + 0.5  # + jitter upper bound
