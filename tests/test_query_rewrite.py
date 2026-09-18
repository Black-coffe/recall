"""Юніт-тести переписування запиту (production-rag-wave-b-07).

Офлайн: жодного реального виклику Anthropic API — `text_polishing._get_client`
підмінюється фейком, що повертає скриптований JSON-текст.
"""
from __future__ import annotations

import json

import pytest

from app.services import query_rewrite, text_polishing


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeMessagesAPI:
    def __init__(self, response_text=None, error=None):
        self._response_text = response_text
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return _FakeMessage(self._response_text)


class _FakeClient:
    def __init__(self, response_text=None, error=None):
        self.messages = _FakeMessagesAPI(response_text=response_text, error=error)


def _mock_client(monkeypatch, response_text=None, error=None):
    fake = _FakeClient(response_text=response_text, error=error)
    monkeypatch.setattr(text_polishing, "_get_client", lambda: fake)
    return fake


# ============================================================
# Порожній/односкладовий запит -> [] без виклику API
# ============================================================

def test_empty_question_returns_empty_without_calling_api(monkeypatch):
    def _boom():
        raise AssertionError("_get_client НЕ мусить викликатись для порожнього запиту")
    monkeypatch.setattr(text_polishing, "_get_client", _boom)
    assert query_rewrite.rewrite_query("") == []
    assert query_rewrite.rewrite_query("   ") == []


def test_single_word_question_returns_empty_without_calling_api(monkeypatch):
    def _boom():
        raise AssertionError("_get_client НЕ мусить викликатись для односкладового запиту")
    monkeypatch.setattr(text_polishing, "_get_client", _boom)
    assert query_rewrite.rewrite_query("бюджет") == []


# ============================================================
# Валідний JSON -> варіанти
# ============================================================

def test_valid_json_returns_variants(monkeypatch):
    _mock_client(monkeypatch, response_text=json.dumps(
        ["скільки коштує проєкт", "витрати на проєкт"], ensure_ascii=False))
    out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == ["скільки коштує проєкт", "витрати на проєкт"]


def test_json_wrapped_in_markdown_fence_is_parsed(monkeypatch):
    _mock_client(monkeypatch, response_text='```json\n["варіант один"]\n```')
    assert query_rewrite.rewrite_query("який бюджет проєкту") == ["варіант один"]


def test_respects_max_variants(monkeypatch):
    _mock_client(monkeypatch, response_text=json.dumps(
        ["один", "два", "три", "чотири", "пʼять"]))
    out = query_rewrite.rewrite_query("який бюджет проєкту", max_variants=2)
    assert out == ["один", "два"]


def test_original_question_is_not_repeated(monkeypatch):
    _mock_client(monkeypatch, response_text=json.dumps(
        ["який бюджет проєкту", "витрати на проєкт"]))
    out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == ["витрати на проєкт"]


def test_case_insensitive_duplicate_of_original_is_dropped(monkeypatch):
    _mock_client(monkeypatch, response_text=json.dumps(
        ["Який Бюджет Проєкту", "витрати на проєкт"]))
    out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == ["витрати на проєкт"]


def test_duplicate_variants_deduped(monkeypatch):
    _mock_client(monkeypatch, response_text=json.dumps(
        ["витрати на проєкт", "Витрати На Проєкт", "щось інше"]))
    out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == ["витрати на проєкт", "щось інше"]


def test_non_string_and_blank_entries_are_skipped(monkeypatch):
    _mock_client(monkeypatch, response_text=json.dumps(
        ["витрати на проєкт", 42, "  ", None]))
    out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == ["витрати на проєкт"]


# ============================================================
# Збій API / невалідний JSON -> [] + logger.warning
# ============================================================

def test_api_error_returns_empty_and_logs_warning(monkeypatch, caplog):
    _mock_client(monkeypatch, error=RuntimeError("боум"))
    with caplog.at_level("WARNING"):
        out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == []
    assert any("переписування запиту не вдалось" in r.message for r in caplog.records)


def test_invalid_json_returns_empty_and_logs_warning(monkeypatch, caplog):
    _mock_client(monkeypatch, response_text="це не json взагалі")
    with caplog.at_level("WARNING"):
        out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == []
    assert any("переписування запиту не вдалось" in r.message for r in caplog.records)


def test_json_object_instead_of_array_returns_empty_and_logs_warning(monkeypatch, caplog):
    _mock_client(monkeypatch, response_text=json.dumps({"variant": "щось"}))
    with caplog.at_level("WARNING"):
        out = query_rewrite.rewrite_query("який бюджет проєкту")
    assert out == []
    assert any("не JSON-масив" in r.message for r in caplog.records)


# ============================================================
# Модель/температура запиту
# ============================================================

def test_default_model_used_when_not_specified(monkeypatch):
    fake = _mock_client(monkeypatch, response_text=json.dumps(["варіант один"]))
    query_rewrite.rewrite_query("який бюджет проєкту")
    assert fake.messages.calls[0]["model"] == query_rewrite.DEFAULT_MODEL
    assert fake.messages.calls[0]["temperature"] == 0


def test_explicit_model_overrides_default(monkeypatch):
    fake = _mock_client(monkeypatch, response_text=json.dumps(["варіант один"]))
    query_rewrite.rewrite_query("який бюджет проєкту", model="claude-custom")
    assert fake.messages.calls[0]["model"] == "claude-custom"


# ============================================================
# production-rag-wave-b-08, Minor 16: без власної копії `_strip_json_fence`
# ============================================================

def test_no_local_strip_json_fence_copy():
    assert not hasattr(query_rewrite, "_strip_json_fence"), (
        "query_rewrite не має мати власну копію _strip_json_fence — "
        "бере text_polishing._strip_json_fence тим самим лінивим імпортом")
