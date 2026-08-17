"""Тести локального cross-encoder reranker (T6.4, Волна 4).

Модель НІКОЛИ не завантажується в тестах (ані bge-reranker-v2-m3, ані
торч-моделі взагалі) — _get_model мокається Fake-об'єктом (образок з
test_copilot_dispatcher.py). Перевіряємо: переранжування за скором мока,
graceful degradation при недоступності/помилці моделі, "raw"-порядок при
порожньому вводі.
"""
import logging

import pytest

from app.services import reranker


class FakeCrossEncoder:
    """Мок CrossEncoder: predict повертає скори за мапою text->score (0.0 за
    замовчуванням для незнайомого тексту), або кидає виняток якщо fail=True."""

    def __init__(self, scores_by_text: dict, fail: bool = False):
        self._scores = scores_by_text
        self._fail = fail
        self.calls: list[list[list[str]]] = []

    def predict(self, pairs):
        self.calls.append(pairs)
        if self._fail:
            raise RuntimeError("boom: model inference failed")
        return [self._scores.get(text, 0.0) for _query, text in pairs]


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Кожен тест ізольований від lazy-singleton і "попереджено раз" стану."""
    monkeypatch.setattr(reranker, "_model", None)
    monkeypatch.setattr(reranker, "_warned_unavailable", False)
    yield


# ============================================================
# Базова поведінка / edge-кейси
# ============================================================

def test_rerank_empty_candidates_returns_empty():
    assert reranker.rerank("query", []) == []


def test_rerank_unavailable_returns_unchanged(monkeypatch, caplog):
    monkeypatch.setattr(reranker, "is_available", lambda: False)
    monkeypatch.setattr(reranker, "unavailability_reason", lambda: "torch not installed")
    candidates = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]

    with caplog.at_level(logging.WARNING):
        out = reranker.rerank("query", candidates)

    assert out == candidates  # той самий вихідний порядок/обʼєкт
    assert "rerank" in caplog.text.lower() or "Недоступний" in caplog.text


def test_rerank_unavailable_warns_only_once(monkeypatch, caplog):
    monkeypatch.setattr(reranker, "is_available", lambda: False)
    candidates = [{"id": 1, "text": "a"}]

    with caplog.at_level(logging.WARNING):
        reranker.rerank("q", candidates)
        reranker.rerank("q", candidates)

    warn_count = sum(1 for r in caplog.records if r.levelno == logging.WARNING)
    assert warn_count == 1


# ============================================================
# Переранжування за скором мока
# ============================================================

def test_rerank_reorders_by_mock_score(monkeypatch):
    monkeypatch.setattr(reranker, "is_available", lambda: True)
    fake = FakeCrossEncoder({"низька релевантність": 0.1, "висока релевантність": 0.9,
                             "середня релевантність": 0.5})
    monkeypatch.setattr(reranker, "_get_model", lambda: fake)

    candidates = [
        {"id": 1, "text": "низька релевантність"},
        {"id": 2, "text": "висока релевантність"},
        {"id": 3, "text": "середня релевантність"},
    ]
    out = reranker.rerank("запит", candidates)

    assert [c["id"] for c in out] == [2, 3, 1]
    assert out[0]["rerank_score"] == pytest.approx(0.9)
    assert out[-1]["rerank_score"] == pytest.approx(0.1)
    # вхідний список НЕ мутований
    assert candidates[0]["id"] == 1 and "rerank_score" not in candidates[0]
    # query коректно проброшений у пари (query, text)
    assert fake.calls[0][0][0] == "запит"


def test_rerank_custom_text_key(monkeypatch):
    monkeypatch.setattr(reranker, "is_available", lambda: True)
    fake = FakeCrossEncoder({"foo": 1.0, "bar": 0.0})
    monkeypatch.setattr(reranker, "_get_model", lambda: fake)

    candidates = [{"id": 1, "body": "bar"}, {"id": 2, "body": "foo"}]
    out = reranker.rerank("q", candidates, text_key="body")

    assert [c["id"] for c in out] == [2, 1]


# ============================================================
# Graceful degradation при помилці inference
# ============================================================

def test_rerank_model_failure_returns_unchanged(monkeypatch, caplog):
    monkeypatch.setattr(reranker, "is_available", lambda: True)
    fake = FakeCrossEncoder({}, fail=True)
    monkeypatch.setattr(reranker, "_get_model", lambda: fake)

    candidates = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]
    with caplog.at_level(logging.WARNING):
        out = reranker.rerank("q", candidates)

    assert out == candidates
    assert "Помилка rerank" in caplog.text
