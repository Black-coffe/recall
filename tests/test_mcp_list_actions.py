"""MCP `list_mcp_actions`: filters, id cursor, clamping, unverified labels, missing table.

Offline: a temp SQLite with the v53 table created from its DDL; no app.py.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mcp_server as m
from tests.test_mcp_registry import load_mcp_copy

DDL = """
CREATE TABLE mcp_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, request_id TEXT, actor TEXT NOT NULL,
  claimed_actor TEXT, claimed_tool TEXT, method TEXT NOT NULL, path TEXT NOT NULL, endpoint TEXT,
  action TEXT, object_ref TEXT, summary TEXT, decision TEXT, http_status INTEGER, error_code TEXT, duration_ms INTEGER);
CREATE INDEX idx_mcp_actions_at ON mcp_actions(at);
"""

COLS = ("at", "request_id", "actor", "claimed_actor", "claimed_tool", "method", "path", "endpoint",
        "action", "object_ref", "summary", "decision", "http_status", "error_code", "duration_ms")


def _seed(path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    for r in rows:
        conn.execute(f"INSERT INTO mcp_actions ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                     tuple(r.get(c) for c in COLS))
    conn.commit()
    conn.close()


def _row(i: int, **kw) -> dict:
    base = {"at": f"2026-10-{(i % 28) + 1:02d}T10:00:00.000000Z", "request_id": f"req-{i:032d}",
            "actor": "unknown", "claimed_actor": "mcp", "claimed_tool": "mailbox", "method": "DELETE",
            "path": f"/api/history/{i}", "endpoint": "history.delete_transcription",
            "action": "transcript_delete", "object_ref": f"transcription:{i}",
            "summary": f"Видалити запис «Дзвінок {i}» (знімок зустрічі) " + "я" * 400,
            "decision": "approved", "http_status": 200, "error_code": None, "duration_ms": 12}
    base.update(kw)
    return base


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "audit.db")
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def test_registry_entry_and_profiles():
    entry = m._TOOL_CLASS["list_mcp_actions"]
    assert (entry["class"], entry["provenance_kind"], entry["open_world"], entry["limits"]) == \
        ("R", "system", False, None)
    assert "list_mcp_actions" in load_mcp_copy()._registered_tool_names()
    assert "list_mcp_actions" in load_mcp_copy("read")._registered_tool_names()
    full = load_mcp_copy()
    assert len(full._registered_tool_names()) == 89  # + system_status and 4 D tools (mcp-mail-and-keys), + 4 retranscribe-versions tools, + 13 archive-org tools, + 12 mcp-pipeline tools, + 7 mcp-telegram-automation tools


def test_default_call_newest_first_with_unverified_claims(db):
    _seed(db, [_row(i) for i in range(1, 6)])
    res = m.list_mcp_actions()
    assert [a["id"] for a in res["actions"]] == [5, 4, 3, 2, 1]
    assert res["count"] == 5 and res["next_before_id"] is None
    for a in res["actions"]:
        assert a["claimed"] == {"actor": "mcp", "tool": "mailbox", "trusted": False}
        assert not any(k.startswith("claimed_") for k in a)
        assert len(a["summary"]) == 200 and a["summary"].startswith("Видалити запис «Дзвінок")


def test_filters_narrow(db):
    _seed(db, [
        _row(1, actor="ui", decision="approved", path="/api/history/1", http_status=200, at="2026-10-01T00:00:00.000000Z"),
        _row(2, actor="unknown", decision="denied:no", path="/api/taxonomy/categories/2", action="taxonomy_delete",
             http_status=403, at="2026-10-02T00:00:00.000000Z"),
        _row(3, actor="unknown", decision="denied:timeout", path="/api/history/3", http_status=403,
             at="2026-10-03T00:00:00.000000Z"),
        _row(4, actor="ui", decision="approved", path="/api/audio/4", action="audio_delete", http_status=500,
             at="2026-10-04T00:00:00.000000Z"),
    ])

    def ids(**kw):
        return sorted(a["id"] for a in m.list_mcp_actions(**kw)["actions"])

    assert ids(actor="ui") == [1, 4]
    assert ids(decision="denied:no") == [2]
    assert ids(action="audio_delete") == [4]
    assert ids(path_prefix="/api/history") == [1, 3]
    assert ids(status_min=400) == [2, 3, 4]
    assert ids(status_min=500) == [4]
    assert ids(since="2026-10-03T00:00:00.000000Z") == [3, 4]
    assert ids(actor="unknown", path_prefix="/api/history") == [3]


def test_limit_clamped_and_cursor_pages_without_overlap(db):
    _seed(db, [_row(i, summary="Видалити запис") for i in range(1, 251)])
    assert m.list_mcp_actions(limit=500)["count"] == 200
    assert m.list_mcp_actions(limit=0)["count"] == 1
    seen, before = [], None
    while True:
        page = m.list_mcp_actions(limit=100, before_id=before)
        seen += [a["id"] for a in page["actions"]]
        before = page["next_before_id"]
        if before is None:
            break
    assert seen == list(range(250, 0, -1))


def test_missing_table_gives_reason(db):
    sqlite3.connect(db).close()
    res = m.list_mcp_actions()
    assert res["actions"] == [] and "mcp_actions" in res["reason"]


def test_full_page_fits_default_budget(db):
    _seed(db, [_row(i, object_ref="transcription:" + "9" * 30, endpoint="taxonomy.merge_categories_endpoint",
                    error_code="APPROVAL_DENIED") for i in range(1, 301)])
    res = m.list_mcp_actions(limit=200)
    size = len(json.dumps(res, ensure_ascii=False, default=str))
    assert size <= m.DEFAULT_MAX_TOKENS * m._CHARS_PER_TOKEN
    # a heavy page is cut, never silently: the cursor continues right after the last row shown
    assert 0 < res["count"] <= 200 and res["next_before_id"] == res["actions"][-1]["id"]


def test_realistic_200_row_page_fits_whole(db):
    _seed(db, [_row(i, summary="Видалити запис «Дзвінок» (3 сегменти)") for i in range(1, 301)])
    res = m.list_mcp_actions(limit=200)
    assert res["count"] == 200
    assert len(json.dumps(res, ensure_ascii=False, default=str)) <= m.DEFAULT_MAX_TOKENS * m._CHARS_PER_TOKEN
