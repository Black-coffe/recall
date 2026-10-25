"""`about()` must not diverge from the FastMCP registry.

Before this test `about()` carried a hardcoded "30 tools" and two hand-rewritten
buckets (14 direct-DB / 16 proxy), while 31 were registered: `list_comments`
was not mentioned in any bucket, and `research_summary` — an SSE proxy to app.py —
sat in direct-DB. A number "right today" does not save us: the next tool (grep_archive,
story 04) would again diverge from the text. So the invariant here is not a specific
number but equality between the registry and the sum of the buckets, from both sides.

Offline: a temporary SQLite via init_database, no network and no heavy models
(`about()` does not import torch and does no search, it only counts rows).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

import mcp_server as m
from app.db.migrations import init_database
from tests.test_mcp_registry import load_mcp_copy


def _full_registry() -> list[str]:
    """The buckets describe the full profile; the developer's .env must not shrink it."""
    return load_mcp_copy()._registered_tool_names()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "about.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def test_bucket_sum_matches_registered_count():
    """The sum of names in the two help buckets equals the number of registered tools."""
    registered = _full_registry()
    bucketed = list(m._ABOUT_DIRECT_DB) + list(m._ABOUT_PROXY)
    assert len(bucketed) == len(registered)


def test_every_registered_tool_is_bucketed():
    """A tool registered in FastMCP but not mentioned in any bucket is drift."""
    registered = set(_full_registry())
    bucketed = set(m._ABOUT_DIRECT_DB) | set(m._ABOUT_PROXY)
    assert registered - bucketed == set()


def test_no_bucketed_name_is_phantom():
    """A name in a bucket that is absent from the registry is drift too (a rename/removal)."""
    registered = set(_full_registry())
    bucketed = set(m._ABOUT_DIRECT_DB) | set(m._ABOUT_PROXY)
    assert bucketed - registered == set()


def test_buckets_do_not_overlap():
    """A tool cannot be both "standalone" and "proxy to app.py" at once."""
    assert set(m._ABOUT_DIRECT_DB) & set(m._ABOUT_PROXY) == set()


def test_research_summary_is_a_proxy():
    """research_summary calls _sse_collect on app.py — it is a proxy, not direct-DB."""
    assert "research_summary" in m._ABOUT_PROXY
    assert "research_summary" not in m._ABOUT_DIRECT_DB


def test_list_comments_is_bucketed():
    """list_comments used not to be mentioned in about() at all."""
    assert "list_comments" in m._ABOUT_DIRECT_DB


def test_about_text_reports_derived_count(db):
    """The resource text shows the current tool count, not "30" forever."""
    text = m.about()
    n = len(m._registered_tool_names())
    assert f"{n} tools" in text
    assert "30 tools" not in text or n == 30


def test_no_file_handler_attached_under_pytest():
    """mcp_server attaches its RotatingFileHandler to the ROOT logger on import —
    this module here (and in several other test_mcp_*.py) is imported at file
    level, i.e. already during collection. If the handler attaches, any
    logging.error/exception from any test in the same run would go to
    the owner's live server mcp_server.log (that is where the fake
    YouTube errors of test_youtube_pytubefix.py came from)."""
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        for h in logging.getLogger().handlers
    )


def test_about_adding_a_tool_changes_the_reported_count(db, monkeypatch):
    """Registering one more tool changes the number without editing the about() text."""
    before = len(m._registered_tool_names())

    @m.mcp.tool
    def _temp_probe_tool() -> str:
        return "probe"

    try:
        after_text = m.about()
    finally:
        m.mcp.remove_tool("_temp_probe_tool")

    assert f"{before + 1} tools" in after_text


def test_list_pipe_runs_is_direct_db():
    import mcp_server as m
    assert "list_pipe_runs" in m._ABOUT_DIRECT_DB
    assert "list_pipe_runs" in _full_registry()


def test_api_sends_the_english_language_header(monkeypatch):
    """Every proxied call asks the Recall server for English texts."""
    seen = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    def fake_request(method, url, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(m.httpx, "request", fake_request)
    m._api("GET", "/api/x")
    assert seen["headers"]["X-Recall-Lang"] == "en"


def test_process_language_is_english(monkeypatch):
    """The MCP process sets its own UI language to `en`."""
    monkeypatch.setenv("RECALL_UI_LANG", "uk")
    m._set_process_lang()
    assert m.os.environ["RECALL_UI_LANG"] == "en"
