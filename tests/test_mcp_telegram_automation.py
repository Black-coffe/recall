"""MCP Telegram and automation tools (mcp-telegram-automation-06): the seven new tools, their
registry entries, gate mapping, argument guards and pass-through of the server's refusals.
`_api` is faked (the `fake_api` seam of test_mcp_archive_org); envelopes go through the real `_api`
over an httpx mock transport. Offline."""
from __future__ import annotations

import httpx
import pytest

import mcp_server as m
from app.db.migrations import init_database
from tests.test_mcp_registry import load_mcp_copy

NEW_R = ("telegram_chats", "telegram_repair_preview", "automation_status")
NEW_W = ("telegram", "pipes")
NEW_D = ("telegram_enable_chat", "telegram_backfill")


@pytest.fixture()
def api(monkeypatch):
    calls: list = []
    replies: dict = {}

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        reply = replies.get((method, path), replies.get(path, {"success": True}))
        return dict(reply)

    monkeypatch.setattr(m, "_api", fake)
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    return calls, replies


@pytest.fixture()
def wire(monkeypatch):
    """The real `_api` over a mock transport: replies by path as (status, body)."""
    seen: list[httpx.Request] = []
    replies: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = replies.get(request.url.path, (200, {"success": True}))
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(m.httpx, "request", lambda method, url, **kw: client.request(method, url, **kw))
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    m._W_WINDOWS.clear()
    yield seen, replies
    m._W_WINDOWS.clear()


# --- registry -----------------------------------------------------------------------------------

def test_seven_tools_registered_with_entries():
    full = load_mcp_copy()
    names = set(full._registered_tool_names())
    for name in NEW_R + NEW_W + NEW_D:
        assert name in names, name
        assert name in full._ABOUT_PROXY, name
    for name in NEW_R:
        assert full._TOOL_CLASS[name]["class"] == "R", name
    assert full._TOOL_CLASS["telegram_chats"]["provenance_kind"] == "telegram"
    assert full._TOOL_CLASS["telegram_chats"]["open_world"] is True
    assert full._TOOL_CLASS["telegram_repair_preview"]["provenance_kind"] == "metadata"
    assert full._TOOL_CLASS["telegram_repair_preview"]["open_world"] is True
    assert full._TOOL_CLASS["automation_status"]["provenance_kind"] == "system"
    assert full._TOOL_CLASS["telegram"]["limits"]["family"] == "intake"
    assert "tags" not in full._TOOL_CLASS["telegram"]["args_allow"]
    pipes = full._TOOL_CLASS["pipes"]
    assert (pipes["class"], pipes["open_world"]) == ("W", True)
    assert pipes["limits"] == {"family": "paid", "window_s": 86400, "max": 20, "paid": True}
    assert pipes["args_allow"] == ("action", "name", "wait_seconds")
    for name in NEW_D:
        e = full._TOOL_CLASS[name]
        assert (e["class"], e["open_world"]) == ("D", True), name
        assert e["limits"]["family"] == "intake", name
    assert len(names) == 89


def test_read_profile_registers_the_three_reads_only():
    names = set(load_mcp_copy("read")._registered_tool_names())
    assert set(NEW_R) <= names
    assert not (set(NEW_W + NEW_D) & names)


# --- telegram_chats -----------------------------------------------------------------------------

def test_telegram_chats_monitored_and_dialogs(api):
    calls, _ = api
    m.telegram_chats()
    m.telegram_chats(category_id=4)
    m.telegram_chats(scope="dialogs")
    assert [c[:2] for c in calls] == [("GET", "/api/telegram/chats"), ("GET", "/api/telegram/chats"),
                                      ("GET", "/api/telegram/dialogs")]
    assert calls[0][2].get("params") is None
    assert calls[1][2]["params"] == {"category_id": 4}
    assert m.telegram_chats(scope="all")["error_code"] == "INVALID_ARGUMENT"
    assert len(calls) == 3


# --- telegram (W) -------------------------------------------------------------------------------

def test_telegram_dispatches_three_actions(api):
    calls, _ = api
    assert m.telegram("set_category", -1001, category_id=7)["ok"] is True
    m.telegram("set_category", -1001, category_id=None)
    m.telegram("set_tags", -1001, tags=["AI", "фонд"])
    m.telegram("disable_chat", -1001)
    assert [(c[0], c[1], c[2]["body"]) for c in calls] == [
        ("PATCH", "/api/telegram/chats/-1001/category", {"category_id": 7}),
        ("PATCH", "/api/telegram/chats/-1001/category", {"category_id": None}),
        ("PUT", "/api/telegram/chats/-1001/tags", {"tags": ["AI", "фонд"]}),
        ("POST", "/api/telegram/chats/-1001/disable", {}),
    ]
    assert all(c[2]["write"] is True for c in calls)


def test_telegram_refuses_before_any_http(api):
    calls, _ = api
    assert m.telegram("delete_chat", 5)["error_code"] == "INVALID_ACTION"
    assert m.telegram("set_tags", 5, tags=[f"t{i}" for i in range(21)])["error_code"] == "INVALID_ARGUMENT"
    assert m.telegram("set_tags", 5)["error_code"] == "INVALID_ARGUMENT"
    assert calls == []
    m.telegram("set_tags", 5, tags=[f"t{i}" for i in range(20)])
    assert len(calls) == 1


def test_private_category_change_passes_through(wire):
    _, replies = wire
    replies["/api/telegram/chats/5/category"] = (403, {"success": False, "error": "private",
                                                        "error_code": "PRIVATE_CATEGORY_CHANGE"})
    res = m.telegram("set_category", 5, category_id=2)
    assert (res["ok"], res["error_code"], res["status_code"]) == (False, "PRIVATE_CATEGORY_CHANGE", 403)


# --- D tools through the gate -------------------------------------------------------------------

@pytest.mark.parametrize("code", ["APPROVAL_DENIED", "APPROVAL_PENDING", "APPROVAL_UNAVAILABLE",
                                  "RECORDING_ACTIVE"])
def test_d_tools_map_gate_refusals_with_the_hint(api, code):
    calls, replies = api
    replies["/api/telegram/chats/9/enable"] = {"ok": False, "error_code": code, "error": "x"}
    replies["/api/telegram/backfill"] = {"ok": False, "error_code": code, "error": "x"}
    for res in (m.telegram_enable_chat(9), m.telegram_backfill(9)):
        assert res["error_code"] == code
        assert res["hint"] == m._GATE_NOTE
    assert all(c[2]["write"] is True for c in calls)


def test_enable_chat_goes_through_gated(api, monkeypatch):
    seen = []
    monkeypatch.setattr(m, "_gated", lambda tool, method, path, body=None, timeout=90.0:
                        seen.append((tool, method, path, body)) or {"success": True})
    m.telegram_enable_chat(-1002, category_id=3)
    m.telegram_enable_chat(-1002)
    assert seen == [("telegram_enable_chat", "POST", "/api/telegram/chats/-1002/enable", {"category_id": 3}),
                    ("telegram_enable_chat", "POST", "/api/telegram/chats/-1002/enable", {})]


def test_backfill_clamps_limit_and_picks_the_route(api, monkeypatch):
    seen = []
    monkeypatch.setattr(m, "_gated", lambda tool, method, path, body=None, timeout=90.0:
                        seen.append((tool, path, body)) or {"success": True})
    m.telegram_backfill(-1003, mode="history", limit=10_000)
    m.telegram_backfill(-1003, mode="repair", limit=0)
    m.telegram_backfill(-1003, mode="repair", limit=120)
    assert seen == [
        ("telegram_backfill", "/api/telegram/backfill", {"chat_id": -1003, "limit": 500}),
        ("telegram_backfill", "/api/telegram/repair", {"chat_id": -1003, "dry_run": False, "max_messages": 1}),
        ("telegram_backfill", "/api/telegram/repair", {"chat_id": -1003, "dry_run": False,
                                                       "max_messages": 120}),
    ]
    assert m.telegram_backfill(-1003, mode="all")["error_code"] == "INVALID_ARGUMENT"
    assert len(seen) == 3


# --- repair preview and status ------------------------------------------------------------------

def test_repair_preview_is_a_dry_run_with_120_s(api):
    calls, replies = api
    replies["/api/telegram/repair"] = {"success": True, "dry_run": True, "missing_total": 3,
                                       "would_attempt": 3, "sample": [1, 2, 3], "live_last_id": 9}
    res = m.telegram_repair_preview(-1004)
    assert res["dry_run"] is True and res["missing_total"] == 3
    method, path, kw = calls[0]
    assert (method, path, kw["body"], kw["timeout"]) == ("POST", "/api/telegram/repair",
                                                        {"chat_id": -1004, "dry_run": True}, 120.0)
    assert not kw.get("write")


def test_telegram_status_passes_bot_configured(wire):
    _, replies = wire
    replies["/api/telegram/status"] = (200, {"alive": False, "bot_configured": True})
    assert m.telegram_status()["bot_configured"] is True


def test_automation_status_proxies_sections(api):
    calls, _ = api
    m.automation_status()
    m.automation_status(["calls", "pipes"])
    assert [c[:2] for c in calls] == [("GET", "/api/automation/status")] * 2
    assert calls[0][2].get("params") is None
    assert calls[1][2]["params"] == {"sections": "calls,pipes"}


# --- pipes --------------------------------------------------------------------------------------

def test_pipes_run_posts_and_waits_for_the_job(api):
    calls, replies = api
    replies[("POST", "/api/pipes/daily-brief/run")] = {"success": True, "job_id": "j7", "created": True,
                                                       "poll": "get_job_status"}
    replies[("GET", "/api/jobs/j7")] = {"id": "j7", "state": "completed", "result": {"status": "ok"}}
    res = m.pipes("run", "daily-brief", wait_seconds=5)
    assert (calls[0][0], calls[0][1], calls[0][2]["body"]) == ("POST", "/api/pipes/daily-brief/run", {})
    assert res["ok"] is True and res["job_id"] == "j7"
    assert res["job"]["result"] == {"status": "ok"}
    assert calls[1][2]["params"] == {"wait": 5}


def test_pipes_refuses_foreign_action_and_bad_name(api):
    calls, _ = api
    assert m.pipes("send", "daily-brief")["error_code"] == "INVALID_ACTION"
    assert m.pipes("run", "../x")["error_code"] == "INVALID_ARGUMENT"
    assert calls == []


@pytest.mark.parametrize("status,code,extra", [
    (429, "COOLDOWN", {"retry_after_sec": 300}), (409, "PIPES_DISABLED", {}),
    (503, "NOT_CONFIGURED", {}), (409, "ALREADY_RUNNING", {"job_id": "j1"}), (404, "NOT_FOUND", {})])
def test_pipes_refusals_pass_through(wire, status, code, extra):
    _, replies = wire
    replies["/api/pipes/daily-brief/run"] = (status, {"success": False, "error": "no",
                                                      "error_code": code, **extra})
    res = m.pipes("run", "daily-brief")
    assert (res["ok"], res["error_code"], res["status_code"]) == (False, code, status)
    for k, v in extra.items():
        assert res[k] == v


# --- about --------------------------------------------------------------------------------------

def test_about_names_tools_channel_event_and_brief_undo(tmp_path, monkeypatch):
    full = load_mcp_copy()
    db = str(tmp_path / "about.db")
    init_database(db)
    monkeypatch.setattr(full, "DB_PATH", db)
    text = full.about()
    for name in NEW_R + NEW_W + NEW_D:
        assert f"`{name}" in text, name
    assert "system:calls" in text and "pipe_run" in text
    assert "daily brief" in text
    assert "89 tools: 54 read-only + 35 write" in text
