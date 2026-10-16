"""Tests for the NL description of table sheets (Phase 16C).

Lightweight - text_polishing is imported without anthropic (the client is lazy). We check
the graceful branch (empty input → no API call). The happy path needs a key
and is checked manually.
"""
from app.services import text_polishing as tp


def test_describe_sheets_empty_returns_empty():
    res = tp.describe_sheets([])
    assert res["descriptions"] == []
    assert res["input_tokens"] == 0


def test_describe_sheets_none_returns_empty():
    res = tp.describe_sheets(None)
    assert res["descriptions"] == []
