"""`grep_archive` — MCP-тулза (grep-explainability, історія 04).

Тонка обгортка над `app.services.archive_grep.grep()` (історія 01) — тіло не
переформатовує контракт C1, лише ловить `ValueError` (некоректний regex) і
повертає {"error": ...} замість traceback.

Друга половина історії: `search_archive`/`ask_archive` передають `explain` у
`_api` (params/body). Тут це перевіряється із замоканим `_api`, без мережі.

Офлайн: тимчасова SQLite через `init_database`, без мережі й без важких
моделей. Ключова умова — імпорт `mcp_server` не має тягнути `torch` навіть
транзитивно (через `archive_grep`/`retrieval`) — див. `test_import_has_no_torch`.
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
    """Прямий доказ, а не припущення: torch не мав завантажитись, навіть
    транзитивно через archive_grep/retrieval, лише тому, що mcp_server
    імпортований. Перевіряється у СВІЖОМУ процесі — інакше в межах повного
    `pytest -m "not slow"` інші модулі (whisper/ctranslate2 тощо) уже
    затягнули torch у той самий sys.modules раніше, і перевірка нічого б
    не довела про сам mcp_server."""
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; import mcp_server; print('torch' in sys.modules)"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", proc.stdout + proc.stderr


def test_grep_archive_registered():
    # Число тулз — інваріант реєстру й бакетів, окремо покритий
    # tests/test_mcp_about.py; тут лише факт реєстрації grep_archive.
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
    # знахідка 4 (Plan deltas D2): докстрінг — єдиний текст, за яким модель
    # обирає тулзу і тлумачить truncated; мусить називати всі три причини,
    # match_total і межу чанків, а не радити "підніми limit" на дедлайні.
    doc = m.grep_archive.__doc__
    assert doc is not None
    for token in ("max_scan", "deadline", "limit", "match_total", "arbitrary_scan_caveat",
                  "chunk_boundary_caveat"):
        assert token in doc, f"докстрінг не згадує {token!r}"


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
