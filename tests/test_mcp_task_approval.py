"""MCP side of the task approval queue (task-approval-06): `list_action_items` statuses and the
approval fields with no `show` written, `task(approve|reject|edit)` through the PATCH proxy, and
the `about()` text. Offline: a temp SQLite for reads, a faked `_api` / `httpx.request` for writes."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

import mcp_server as m
from app.db.migrations import init_database
from tests.test_mcp_registry import load_mcp_copy


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "approval.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


@pytest.fixture()
def api(monkeypatch):
    calls: list = []
    replies: dict = {}

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        return dict(replies.get((method, path), {"success": True}))

    monkeypatch.setattr(m, "_api", fake)
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    return calls, replies


def _seed(path: str, rows: list[dict]) -> list[int]:
    conn = sqlite3.connect(path)
    tid = conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text) "
                       "VALUES ('file', 'Call', 'text')").lastrowid
    ids = []
    for r in rows:
        cols = {"transcription_id": tid, "task": "Task", "status": "proposed", **r}
        ids.append(conn.execute(
            f"INSERT INTO action_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(cols.values())).lastrowid)
    conn.commit()
    conn.close()
    return ids


def _events(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM action_item_events").fetchone()[0]
    finally:
        conn.close()


# --- list_action_items ------------------------------------------------------------------------

def test_proposed_follows_the_queue_and_writes_nothing(db):
    ids = _seed(db, [{"task": f"P{i}", "importance": i % 3, "for_whom": "Olena",
                      "confidence": 0.8} for i in range(12)]
                + [{"task": "dated", "due_date": "2026-11-01"},
                   {"task": "shown", "first_shown_at": "2026-10-26T09:00:00"},
                   {"task": "approved", "status": "open"}])
    out = m.list_action_items(status="proposed")
    assert len(out["items"]) == 10
    from app.services import commitments
    assert [i["id"] for i in out["items"]] == [r["id"] for r in commitments.proposal_queue(db)]
    assert out["items"][0]["task"] == "dated"
    assert out["coverage"] == {"window_matched": 14, "truncated": True}
    row = out["items"][0]
    for key in ("for_whom", "confidence", "importance", "first_shown_at", "decided_at", "last_event"):
        assert key in row
    assert _events(db) == 0
    conn = sqlite3.connect(db)
    shown = dict(conn.execute("SELECT id, first_shown_at FROM action_items").fetchall())
    conn.close()
    assert shown[ids[13]] == "2026-10-26T09:00:00"
    assert all(v is None for k, v in shown.items() if k != ids[13])
    assert len(m.list_action_items(status="proposed", limit=50)["items"]) == 14


def test_default_open_legacy_by_name_and_all(db):
    ids = _seed(db, [{"task": "live", "status": "open"}, {"task": "old", "status": "legacy"},
                     {"task": "queued"}])
    assert [i["id"] for i in m.list_action_items(window="all")["items"]] == [ids[0]]
    assert [i["id"] for i in m.list_action_items(window="all", status="legacy")["items"]] == [ids[1]]
    assert sorted(i["id"] for i in m.list_action_items(window="all", status="all")["items"]) == ids
    assert _events(db) == 0


def test_unknown_status_and_filtered_queue_are_invalid(db):
    assert m.list_action_items(status="maybe")["error_code"] == "INVALID_ARGUMENT"
    assert m.list_action_items(status="proposed", owner="Olena")["error_code"] == "INVALID_ARGUMENT"


# --- task -------------------------------------------------------------------------------------

def test_approve_and_reject_send_the_action_body(api):
    calls, _ = api
    assert m.task("approve", item_ids=[3, 4])["ok"] is True
    assert m.task("reject", item_ids=[5])["ok"] is True
    assert [(c[0], c[1], c[2]["body"], c[2]["write"]) for c in calls] == [
        ("PATCH", "/api/memory/action-items/3", {"action": "approve"}, True),
        ("PATCH", "/api/memory/action-items/4", {"action": "approve"}, True),
        ("PATCH", "/api/memory/action-items/5", {"action": "reject"}, True)]


def test_edit_body_and_its_refusals_before_any_request(api):
    calls, _ = api
    assert m.task("edit", item_ids=[7], text=" New wording ", due="2026-11-03")["ok"] is True
    assert calls[0][1] == "/api/memory/action-items/7"
    assert calls[0][2]["body"] == {"action": "edit", "task": "New wording", "due_date": "2026-11-03"}
    m.task("edit", item_ids=[7], due="2026-12-01")
    assert calls[1][2]["body"] == {"action": "edit", "due_date": "2026-12-01"}
    for kw in ({}, {"text": "  "}, {"due": "next friday"}):
        assert m.task("edit", item_ids=[7], **kw)["error_code"] == "INVALID_ARGUMENT"
    assert m.task("edit", item_ids=[7, 8], text="x")["error_code"] == "INVALID_ARGUMENT"
    assert len(calls) == 2


def test_foreign_action_is_invalid_action(api):
    calls, _ = api
    assert m.task("approve_all", item_ids=[1])["error_code"] == "INVALID_ACTION"
    assert calls == []


def test_invalid_transition_reaches_the_envelope_with_the_mcp_actor(monkeypatch):
    seen: list = []

    def fake_request(method, url, **kw):
        seen.append((method, url, kw["headers"], kw["json"]))
        return httpx.Response(409, json={"success": False, "error": "not allowed",
                                         "error_code": "INVALID_TRANSITION"})

    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    monkeypatch.setattr(m.httpx, "request", fake_request)
    out = m.task("approve", item_ids=[9])
    assert out["ok"] is False and out["error_code"] == "INVALID_TRANSITION"
    assert out["results"][0]["status_code"] == 409
    method, url, headers, body = seen[0]
    assert method == "PATCH" and url.endswith("/api/memory/action-items/9")
    assert headers["X-Recall-Actor"] == "mcp" and headers["X-Recall-Tool"] == "task"
    assert body == {"action": "approve"}


# --- about ------------------------------------------------------------------------------------

def test_about_describes_the_queue(db):
    full = load_mcp_copy()
    full.DB_PATH = db
    text = full.about()
    for status in ("proposed", "open", "done", "cancelled", "stale", "rejected", "expired", "legacy"):
        assert status in text, status
    assert "does NOT start the 3-day clock" in text
    assert "reached by name only" in text
    assert "task(approve|reject" in text
