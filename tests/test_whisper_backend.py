"""Тесты для backend abstraction (Phase 1)."""
import os
import sys

# Без CUDA-залежного імпорту — лише чиста логіка.
from whisper_manager_new import (
    _select_default_backend,
    OpenAIWhisperBackend,
    FasterWhisperBackend,
)


def test_select_backend_explicit_env(monkeypatch):
    monkeypatch.setenv("WHISPER_BACKEND", "openai")
    assert _select_default_backend() == "openai"

    monkeypatch.setenv("WHISPER_BACKEND", "faster")
    assert _select_default_backend() == "faster"


def test_select_backend_invalid_value_falls_back_to_autodetect(monkeypatch):
    monkeypatch.setenv("WHISPER_BACKEND", "garbage")
    # garbage не в ('faster', 'openai') → autodetect (faster якщо встановлений)
    result = _select_default_backend()
    assert result in ("faster", "openai")


def test_faster_backend_supports_batched():
    """Без реальної ініціалізації моделі — лише intospect методу."""
    # supports_batched() — це instance method, але тільки повертає True/False.
    # Створюємо backend (lazy import faster_whisper всередині).
    try:
        backend = FasterWhisperBackend()
        assert backend.supports_batched() is True
    except ImportError:
        # Якщо faster_whisper не встановлений у тестовому середовищі — пропускаємо
        import pytest
        pytest.skip("faster_whisper not installed")


def test_openai_backend_does_not_support_batched():
    try:
        backend = OpenAIWhisperBackend()
        assert backend.supports_batched() is False
    except ImportError:
        import pytest
        pytest.skip("whisper not installed")


def test_faster_backend_model_name_mapping():
    try:
        backend = FasterWhisperBackend()
        # Тестова перевірка маппінгу — large → large-v3
        assert backend._MODEL_NAME_MAP["large"] == "large-v3"
        assert backend._MODEL_NAME_MAP["turbo"] == "large-v3-turbo"
        assert backend._MODEL_NAME_MAP["base"] == "base"
    except ImportError:
        import pytest
        pytest.skip("faster_whisper not installed")
