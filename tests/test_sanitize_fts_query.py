"""Тесты для sanitize_fts_query (Phase 4.2 FTS5 пошук)."""
from app.utils.fts import sanitize_fts_query as sanitize


def test_empty_returns_empty():
    assert sanitize("") == ""
    assert sanitize(None) == ""


def test_only_punctuation():
    assert sanitize("!!!") == ""
    assert sanitize("...") == ""


def test_single_word():
    assert sanitize("hello") == '"hello"'


def test_multiple_words_AND():
    assert sanitize("hello world") == '"hello" "world"'


def test_special_chars_stripped():
    """FTS5 спецсимволи (* " :) не потрапляють у результат як operators."""
    out = sanitize('hello*world')
    assert out == '"hello" "world"'


def test_quotes_neutralized():
    """Лапки в інпуті не повинні зламати FTS5 phrase syntax."""
    out = sanitize('say "hi" please')
    assert '"say"' in out
    assert '"hi"' in out
    assert '"please"' in out


def test_cyrillic_works():
    out = sanitize("Иисус Хрест")
    assert '"Иисус"' in out
    assert '"Хрест"' in out


def test_mixed_languages():
    out = sanitize("hello світ")
    assert '"hello"' in out
    assert '"світ"' in out
