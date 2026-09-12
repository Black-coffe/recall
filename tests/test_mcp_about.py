"""`about()` не має право розходитись із реєстром FastMCP.

До цього тесту `about()` носив хардкод «30 тулзів» і два вручну переписані
бакети (14 direct-DB / 16 проксі), тоді як зареєстровано було 31: `list_comments`
не згадувався в жодному бакеті, а `research_summary` — SSE-проксі на app.py —
стояв у direct-DB. Число «правильне сьогодні» не рятує: наступна тулза (grep_archive,
історія 04) знову розійшлася б з текстом. Тому інваріант тут — не конкретне
число, а рівність між реєстром і сумою бакетів, з обох боків.

Офлайн: тимчасова SQLite через init_database, без мережі й без важких моделей
(`about()` не імпортує torch і не займається пошуком, лише лічить рядки).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server as m
from app.db.migrations import init_database


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "about.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def test_bucket_sum_matches_registered_count():
    """Сума імен у двох бакетах довідки дорівнює кількості зареєстрованих тулзів."""
    registered = m._registered_tool_names()
    bucketed = list(m._ABOUT_DIRECT_DB) + list(m._ABOUT_PROXY)
    assert len(bucketed) == len(registered)


def test_every_registered_tool_is_bucketed():
    """Тулза, зареєстрована в FastMCP, але не згадана в жодному бакеті, — дрейф."""
    registered = set(m._registered_tool_names())
    bucketed = set(m._ABOUT_DIRECT_DB) | set(m._ABOUT_PROXY)
    assert registered - bucketed == set()


def test_no_bucketed_name_is_phantom():
    """Імʼя в бакеті, якого нема в реєстрі, — так само дрейф (перейменування/видалення)."""
    registered = set(m._registered_tool_names())
    bucketed = set(m._ABOUT_DIRECT_DB) | set(m._ABOUT_PROXY)
    assert bucketed - registered == set()


def test_buckets_do_not_overlap():
    """Тулза не може одночасно бути «автономною» і «проксі на app.py»."""
    assert set(m._ABOUT_DIRECT_DB) & set(m._ABOUT_PROXY) == set()


def test_research_summary_is_a_proxy():
    """research_summary кличе _sse_collect на app.py — це проксі, не direct-DB."""
    assert "research_summary" in m._ABOUT_PROXY
    assert "research_summary" not in m._ABOUT_DIRECT_DB


def test_list_comments_is_bucketed():
    """list_comments раніше не згадувався в about() узагалі."""
    assert "list_comments" in m._ABOUT_DIRECT_DB


def test_about_text_reports_derived_count(db):
    """Текст ресурсу показує актуальне число тулзів, не «30» назавжди."""
    text = m.about()
    n = len(m._registered_tool_names())
    assert f"{n} тулзів" in text
    assert "30 тулзів" not in text or n == 30


def test_about_adding_a_tool_changes_the_reported_count(db, monkeypatch):
    """Підкладена ще одна тулза змінює число без правки тексту about()."""
    before = len(m._registered_tool_names())

    @m.mcp.tool
    def _temp_probe_tool() -> str:
        return "probe"

    try:
        after_text = m.about()
    finally:
        m.mcp.remove_tool("_temp_probe_tool")

    assert f"{before + 1} тулзів" in after_text
