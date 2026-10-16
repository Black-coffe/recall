"""Tests for the backend abstraction (Phase 1)."""
import os
import sys

# Without CUDA-dependent imports — pure logic only.
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
    # garbage not in ('faster', 'openai') → autodetect (faster if installed)
    result = _select_default_backend()
    assert result in ("faster", "openai")


def test_faster_backend_supports_batched():
    """Without real model initialization — only method introspection."""
    # supports_batched() is an instance method, but it only returns True/False.
    # Create the backend (lazy import of faster_whisper inside).
    try:
        backend = FasterWhisperBackend()
        assert backend.supports_batched() is True
    except ImportError:
        # If faster_whisper is not installed in the test environment — skip
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
        # Test check of the mapping — large → large-v3
        assert backend._MODEL_NAME_MAP["large"] == "large-v3"
        assert backend._MODEL_NAME_MAP["turbo"] == "large-v3-turbo"
        assert backend._MODEL_NAME_MAP["base"] == "base"
    except ImportError:
        import pytest
        pytest.skip("faster_whisper not installed")
