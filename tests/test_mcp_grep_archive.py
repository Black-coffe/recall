"""`grep_archive` — an MCP tool (grep-explainability, story 04).

A thin wrapper over `app.services.archive_grep.grep()` (story 01) — the body does not
reformat contract C1, it only catches `ValueError` (an invalid regex) and
returns {"error": ...} instead of a traceback.

The second half of the story: `search_archive`/`ask_archive` pass `explain` to
`_api` (params/body). Here that is checked with a mocked `_api`, without network.

Offline: a temporary SQLite via `init_database`, no network and no heavy
models. The key condition — importing `mcp_server` must not pull in `torch` even
transitively (via `archive_grep`/`retrieval`) — see `test_import_has_no_torch`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database

import mcp_server as m


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "grep_archive.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def _add_transcription(conn, *, source_type="meeting", source_name="test", meeting_date=None):
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, meeting_date) "
        "VALUES (?, ?, ?)",
        (source_type, source_name, meeting_date),
    )
    return cur.lastrowid


def _add_chunk(conn, transcription_id, chunk_index, text):
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, text) VALUES (?, ?, ?)",
        (transcription_id, chunk_index, text),
    )


def test_import_has_no_torch():
    """Direct proof, not an assumption: torch must not have loaded, even
    transitively via archive_grep/retrieval, merely because mcp_server was
    imported. Checked in a FRESH process — otherwise within a full
    `pytest -m "not slow"` other modules (whisper/ctranslate2 etc.) have already
    pulled torch into the same sys.modules earlier, and the check would prove
    nothing about mcp_server itself."""
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; import mcp_server; print('torch' in sys.modules)"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", proc.stdout + proc.stderr


def test_grep_archive_registered():
    # The tool count is a registry/bucket invariant, covered separately by
    # tests/test_mcp_about.py; here only the fact that grep_archive is registered.
    assert "grep_archive" in m._registered_tool_names()


def test_grep_archive_bucketed_as_direct_db():
    assert "grep_archive" in m._ABOUT_DIRECT_DB
    assert "grep_archive" not in m._ABOUT_PROXY


def test_about_mentions_grep_archive(db):
    text = m.about()
    assert "`grep_archive`" in text


def test_grep_archive_returns_c1_contract(db):
    with get_db_connection(db) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        _add_chunk(conn, tid, 0, "Оплата рахунку ID-4471 підтверджена вчора.")
        conn.commit()

    result = m.grep_archive("ID-4471")
    assert result["count"] == 1
    assert result["pattern"] == "ID-4471"
    assert result["regex"] is False
    assert "scanned" in result and "truncated" in result
    match = result["matches"][0]
    assert match["chunk_index"] == 0
    assert match["transcription_id"] == tid
    assert "ID-4471" in match["excerpt"]
    assert "context_before" in match and "context_after" in match


def test_grep_archive_docstring_covers_all_truncation_reasons():
    # finding 4 (Plan deltas D2): the docstring is the only text by which the model
    # picks the tool and interprets truncated; it must name all three reasons,
    # match_total and the chunk boundary, not advise "raise limit" on a deadline.
    doc = m.grep_archive.__doc__
    assert doc is not None
    for token in ("max_scan", "deadline", "limit", "match_total", "arbitrary_scan_caveat",
                  "chunk_boundary_caveat"):
        assert token in doc, f"docstring does not mention {token!r}"


def test_grep_archive_bad_regex_returns_error_not_traceback(db):
    result = m.grep_archive("(unclosed", regex=True)
    assert isinstance(result, dict)
    assert "error" in result
    assert "matches" not in result


def test_search_archive_puts_explain_in_params(db, monkeypatch):
    captured = {}

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        return {"query": params.get("q") if params else None, "chunks": []}

    monkeypatch.setattr(m, "_api", fake_api)

    m.search_archive("щось", explain=True)
    assert captured["params"]["explain"] is True

    m.search_archive("щось інше", explain=False)
    assert "explain" not in captured["params"]


def test_ask_archive_puts_explain_in_body(db, monkeypatch):
    captured = {}

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"answer": "..."}

    monkeypatch.setattr(m, "_api", fake_api)

    m.ask_archive("питання?", explain=True)
    assert captured["body"]["explain"] is True

    m.ask_archive("інше питання?", explain=False)
    assert "explain" not in captured["body"]
