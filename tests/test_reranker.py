"""Tests of the local cross-encoder reranker (T6.4, Wave 4).

The model is NEVER loaded in tests (neither bge-reranker-v2-m3 nor any
torch model at all); _get_model is mocked with a Fake object (pattern from
test_copilot_dispatcher.py). We check: re-ranking by the mock score,
graceful degradation when the model is unavailable/fails, "raw" order on
empty input.
"""
import logging

import pytest

from app.services import reranker


class FakeCrossEncoder:
    """CrossEncoder mock: predict returns scores from a text->score map (0.0 by
    default for an unknown text), or raises if fail=True."""

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
    """Every test is isolated from the lazy singleton and the "warned once" state."""
    monkeypatch.setattr(reranker, "_model", None)
    monkeypatch.setattr(reranker, "_warned_unavailable", False)
    yield


# ============================================================
# Basic behaviour / edge cases
# ============================================================

def test_rerank_empty_candidates_returns_empty():
    assert reranker.rerank("query", []) == []


def test_rerank_unavailable_returns_unchanged(monkeypatch, caplog):
    monkeypatch.setattr(reranker, "is_available", lambda: False)
    monkeypatch.setattr(reranker, "unavailability_reason", lambda: "torch not installed")
    candidates = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]

    with caplog.at_level(logging.WARNING):
        out = reranker.rerank("query", candidates)

    assert out == candidates  # the same original order/object
    assert "rerank" in caplog.text.lower() or "Unavailable" in caplog.text


def test_rerank_unavailable_warns_only_once(monkeypatch, caplog):
    monkeypatch.setattr(reranker, "is_available", lambda: False)
    candidates = [{"id": 1, "text": "a"}]

    with caplog.at_level(logging.WARNING):
        reranker.rerank("q", candidates)
        reranker.rerank("q", candidates)

    warn_count = sum(1 for r in caplog.records if r.levelno == logging.WARNING)
    assert warn_count == 1


# ============================================================
# Re-ranking by the mock score
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
    # the input list is NOT mutated
    assert candidates[0]["id"] == 1 and "rerank_score" not in candidates[0]
    # query is correctly passed into the (query, text) pairs
    assert fake.calls[0][0][0] == "запит"


def test_rerank_custom_text_key(monkeypatch):
    monkeypatch.setattr(reranker, "is_available", lambda: True)
    fake = FakeCrossEncoder({"foo": 1.0, "bar": 0.0})
    monkeypatch.setattr(reranker, "_get_model", lambda: fake)

    candidates = [{"id": 1, "body": "bar"}, {"id": 2, "body": "foo"}]
    out = reranker.rerank("q", candidates, text_key="body")

    assert [c["id"] for c in out] == [2, 1]


# ============================================================
# Graceful degradation on an inference error
# ============================================================

def test_rerank_model_failure_returns_unchanged(monkeypatch, caplog):
    monkeypatch.setattr(reranker, "is_available", lambda: True)
    fake = FakeCrossEncoder({}, fail=True)
    monkeypatch.setattr(reranker, "_get_model", lambda: fake)

    candidates = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]
    with caplog.at_level(logging.WARNING):
        out = reranker.rerank("q", candidates)

    assert out == candidates
    assert "Rerank error" in caplog.text
