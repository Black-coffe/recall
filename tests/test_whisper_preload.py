"""Тести для app.services.whisper_preload (Фикс 2, 03.07.2026).

Офлайн, без реального WhisperManager — мок з методом load_model().
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from app.services import whisper_preload


class _FakeWhisperManager:
    def __init__(self, raise_error: Exception | None = None, delay: float = 0.0):
        self.calls: list[str] = []
        self._raise_error = raise_error
        self._delay = delay

    def load_model(self, model_name: str):
        if self._delay:
            time.sleep(self._delay)
        self.calls.append(model_name)
        if self._raise_error is not None:
            raise self._raise_error
        return object()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('RECALL_PRELOAD_WHISPER', raising=False)
    monkeypatch.delenv('RECALL_PRELOAD_WHISPER_MODEL', raising=False)


def test_preload_enabled_defaults_true():
    assert whisper_preload.preload_enabled() is True


def test_preload_enabled_false_when_flag_is_0(monkeypatch):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER', '0')
    assert whisper_preload.preload_enabled() is False


def test_resolve_preload_model_defaults_to_frontend_default():
    # Дзеркалить DEFAULT_MODEL з static/js/recall/views/{upload,record,...}.js
    assert whisper_preload.resolve_preload_model() == 'large-v3-turbo'


def test_resolve_preload_model_env_override(monkeypatch):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER_MODEL', 'medium')
    assert whisper_preload.resolve_preload_model() == 'medium'


def test_start_background_preload_calls_load_model_with_resolved_model():
    manager = _FakeWhisperManager()
    thread = whisper_preload.start_background_preload(manager, model_name='small')
    assert thread is not None
    thread.join(timeout=5)
    assert manager.calls == ['small']


def test_start_background_preload_uses_env_model_when_not_passed(monkeypatch):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER_MODEL', 'tiny')
    manager = _FakeWhisperManager()
    thread = whisper_preload.start_background_preload(manager)
    assert thread is not None
    thread.join(timeout=5)
    assert manager.calls == ['tiny']


def test_start_background_preload_noop_when_manager_missing():
    thread = whisper_preload.start_background_preload(None)
    assert thread is None


def test_start_background_preload_noop_when_flag_is_0(monkeypatch):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER', '0')
    manager = _FakeWhisperManager()
    thread = whisper_preload.start_background_preload(manager)
    assert thread is None
    assert manager.calls == []


def test_start_background_preload_swallows_exceptions_and_logs_warning(caplog):
    manager = _FakeWhisperManager(raise_error=RuntimeError('GPU OOM'))
    with caplog.at_level(logging.WARNING):
        thread = whisper_preload.start_background_preload(manager, model_name='medium')
        assert thread is not None
        thread.join(timeout=5)  # must not raise / propagate to the joiner
    assert manager.calls == ['medium']
    assert any('не вдалося прогріти' in r.message for r in caplog.records)


def test_start_background_preload_does_not_block_caller():
    manager = _FakeWhisperManager(delay=0.3)
    t0 = time.time()
    thread = whisper_preload.start_background_preload(manager, model_name='base')
    elapsed = time.time() - t0
    assert elapsed < 0.2  # start_background_preload returns immediately
    assert thread is not None
    thread.join(timeout=5)
    assert manager.calls == ['base']


# ============================================================
# config-registry-fix-r3-01: start_tracked_preload — детермінований доказ
# порядку станів loading -> ready|failed|disabled (без полінгу дочірнього
# процесу). Фейк блокується на threading.Event, `_models` — dict, як у
# реальному ModernWhisperManager.
# ============================================================

class _GatedFakeManager:
    """`load_model` не повертається, поки тест сам не відпустить `_event`
    (`.release()`) — дає змогу спостерігати стан 'loading' детерміновано."""

    def __init__(self, raise_error: Exception | None = None):
        self._models: dict = {}
        self._event = threading.Event()
        self._raise_error = raise_error

    def release(self) -> None:
        self._event.set()

    def load_model(self, model_name: str):
        self._event.wait(timeout=5)
        if self._raise_error is not None:
            raise self._raise_error
        self._models[model_name] = object()
        return self._models[model_name]


def test_start_tracked_preload_reports_loading_then_ready(monkeypatch):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER_MODEL', 'medium')
    manager = _GatedFakeManager()
    робота: dict = {}

    watcher = whisper_preload.start_tracked_preload(manager, робота)
    assert watcher is not None
    # ДО відпускання події load_model ще не повернувся - стан лишається 'loading'.
    assert робота['whisper'] == {'preload': 'loading', 'model': 'medium'}

    manager.release()
    watcher.join(timeout=5)
    assert робота['whisper'] == {'preload': 'ready', 'model': 'medium'}


def test_start_tracked_preload_reports_failed_when_load_model_raises(monkeypatch):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER_MODEL', 'medium')
    manager = _GatedFakeManager(raise_error=RuntimeError('GPU OOM'))
    робота: dict = {}

    watcher = whisper_preload.start_tracked_preload(manager, робота)
    assert watcher is not None
    manager.release()
    watcher.join(timeout=5)
    assert робота['whisper'] == {'preload': 'failed', 'model': None}


def test_start_tracked_preload_disabled_when_flag_is_0(monkeypatch):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER', '0')
    manager = _GatedFakeManager()
    робота: dict = {}

    result = whisper_preload.start_tracked_preload(manager, робота)
    assert result is None
    assert робота['whisper'] == {'preload': 'disabled', 'model': None}


@pytest.mark.parametrize('value', ['yes', 'on', 'TRUE'])
def test_preload_enabled_true_values(monkeypatch, value):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER', value)
    assert whisper_preload.preload_enabled() is True


@pytest.mark.parametrize('value', ['0', 'false', 'no'])
def test_preload_enabled_false_values(monkeypatch, value):
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER', value)
    assert whisper_preload.preload_enabled() is False


def test_preload_enabled_garbage_value_is_now_false_documented_flip(monkeypatch):
    """config-registry-fix-r3-01: старий парсер (`not in ('0','false','no')`)
    трактував будь-яке нерозпізнане значення як True; після переходу на
    `settings.env_bool` воно тепер False. Перевертання задокументоване в
    `## Implementation notes` цієї історії — тест фіксує його явно."""
    monkeypatch.setenv('RECALL_PRELOAD_WHISPER', 'ага')
    assert whisper_preload.preload_enabled() is False
