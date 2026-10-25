"""MCP write tools: `mailbox`, `settings`, headers, the secret guard and READONLY.

Offline: the real `_api` runs against an httpx mock transport; no network.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastmcp import Client

import mcp_server as m
from tests.test_mcp_registry import load_mcp_copy

SECRET = "abcd efgh ijkl mnop"


@pytest.fixture()
def wire(monkeypatch):
    """Route `_api` through a mock transport; `seen` collects every request."""
    seen: list[httpx.Request] = []
    responses: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=responses.get(request.url.path, {"success": True}))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(m.httpx, "request", lambda method, url, **kw: client.request(method, url, **kw))
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    seen_responses = responses
    return seen, seen_responses


def _body(req: httpx.Request):
    return json.loads(req.content) if req.content else None


def test_annotations_and_tool_class():
    # A full-profile copy: the developer's .env (RECALL_MCP_READONLY) must not hide the writes here.
    tools = {t.name: t for t in asyncio.run(load_mcp_copy().mcp.list_tools())}
    for name in ("mailbox", "settings"):
        ann = tools[name].annotations
        assert ann.readOnlyHint is False and ann.destructiveHint is False
    assert tools["mailbox"].annotations.openWorldHint is True
    # mcp-mail-and-keys: settings(test_anthropic_key) reaches Anthropic
    assert tools["settings"].annotations.openWorldHint is True
    assert tools["email_status"].annotations.readOnlyHint is True
    assert m._TOOL_CLASS["mailbox"]["args_allow"] == (
        "action", "account_id", "label_id", "enabled", "category_id", "months")
    assert "address" not in m._TOOL_CLASS["mailbox"]["args_allow"]


def test_headers_on_writes_only(wire):
    seen, responses = wire
    responses["/api/mail/connect-link"] = {"success": True, "connect_url": "http://127.0.0.1/x",
                                           "expires_in": 600, "already_configured": False}
    m.mailbox("connect", address="owner@example.test")
    m.settings("connect_anthropic_key")
    m._api("GET", "/api/mail/status")
    assert seen[0].headers["X-Recall-Actor"] == "mcp" and seen[0].headers["X-Recall-Tool"] == "mailbox"
    assert seen[0].headers["X-Recall-Lang"] == "en"
    assert seen[1].headers["X-Recall-Tool"] == "settings"
    assert "X-Recall-Actor" not in seen[2].headers and "X-Recall-Tool" not in seen[2].headers


def test_connect_shape(wire):
    seen, responses = wire
    responses["/api/mail/connect-link"] = {"success": True, "connect_url": "http://127.0.0.1:5050/connect/mail/t",
                                           "expires_in": 600, "already_configured": True,
                                           "address": "owner@example.test"}
    res = m.mailbox("connect", address="owner@example.test")
    assert res["connect_url"].endswith("/connect/mail/t") and res["expires_in"] == 600
    assert res["already_configured"] is True and res["poll"] == "email_status"
    assert "Never paste the password" in res["note"]
    assert (seen[0].method, seen[0].url.path, _body(seen[0])) == (
        "POST", "/api/mail/connect-link", {"address": "owner@example.test"})


def test_action_mapping(wire):
    seen, _ = wire
    m.mailbox("refresh_labels", account_id=3)
    m.mailbox("label_set", label_id=7, enabled=True)
    m.mailbox("label_set", label_id=7, clear_category=True)
    m.mailbox("label_set", label_id=7, category_id=2, enabled=False)
    res = m.mailbox("sync")
    m.mailbox("sync", account_id=3, months=6)
    got = [(r.method, r.url.path, _body(r)) for r in seen]
    assert got == [
        ("POST", "/api/mail/accounts/3/labels/refresh", {}),
        ("PATCH", "/api/mail/labels/7", {"enabled": True}),
        ("PATCH", "/api/mail/labels/7", {"category_id": None}),
        ("PATCH", "/api/mail/labels/7", {"category_id": 2, "enabled": False}),
        ("POST", "/api/mail/sync", {}),
        ("POST", "/api/mail/sync", {"account_id": 3, "months": 6}),
    ]
    assert res["poll"] == "email_status"
    m.settings("connect_anthropic_key")
    assert (seen[-1].method, seen[-1].url.path) == ("POST", "/api/settings/anthropic-key/connect-link")


@pytest.mark.parametrize("kwargs", [
    {"action": "delete"},
    {"action": "connect"},
    {"action": "connect", "address": "a@b.test", "months": 3},
    {"action": "refresh_labels"},
    {"action": "label_set", "label_id": 1},
    {"action": "label_set", "label_id": 1, "category_id": 2, "clear_category": True},
    {"action": "sync", "label_id": 1},
])
def test_invalid_action_sends_nothing(wire, kwargs):
    seen, _ = wire
    assert m.mailbox(**kwargs)["error_code"] == "INVALID_ACTION"
    assert seen == []


def test_settings_wrong_action(wire):
    seen, _ = wire
    assert m.settings("other")["error_code"] == "INVALID_ACTION"
    assert seen == []


def test_secret_rejected_without_http_and_log(wire, tmp_path, monkeypatch, caplog):
    seen, _ = wire
    full = load_mcp_copy()
    log = tmp_path / "calls.log"
    guard_then_log = full._CallLogMiddleware(str(log))
    full.mcp.add_middleware(guard_then_log)
    try:
        async def go():
            async with Client(full.mcp) as c:
                a = await c.call_tool("mailbox", {"action": "connect", "address": SECRET},
                                      raise_on_error=False)
                b = await c.call_tool("search_archive", {"query": "see sk-ant-api03-InventedValue123"},
                                      raise_on_error=False)
                return a, b
        with caplog.at_level(logging.DEBUG):
            a, b = asyncio.run(go())
    finally:
        full.mcp.middleware.remove(guard_then_log)
    for r in (a, b):
        assert r.structured_content["error_code"] == "SECRET_IN_ARGUMENT"
    assert seen == []
    text = (log.read_text(encoding="utf-8") if log.exists() else "") + caplog.text
    for piece in ("abcd", "efgh", "ijkl", "mnop", "InventedValue", "sk-ant"):
        assert piece not in text


def test_readonly_refuses_without_http(wire, monkeypatch):
    """The second belt: with RECALL_MCP_READONLY the write tools are not even registered
    (test_mcp_registry), and a direct call still refuses without HTTP."""
    seen, _ = wire
    monkeypatch.setattr(m, "_WRITE_DISABLED", True)
    for kw in ({"action": "connect", "address": "a@b.test"}, {"action": "refresh_labels", "account_id": 1},
               {"action": "label_set", "label_id": 1, "enabled": True}, {"action": "sync"}):
        assert m.mailbox(**kw)["error_code"] == "MCP_READONLY"
    assert m.settings("connect_anthropic_key")["error_code"] == "MCP_READONLY"
    assert seen == []
    assert m._api("GET", "/api/mail/status") == {"success": True}


def test_new_tools_in_about_proxy():
    assert "mailbox" in m._ABOUT_PROXY and "settings" in m._ABOUT_PROXY


# --- W ceiling (per family, per MCP process) ---------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_w_windows():
    m._W_WINDOWS.clear()
    yield
    m._W_WINDOWS.clear()


def _wire_status(monkeypatch, status_box: list):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_box[0], json={"success": status_box[0] < 400})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(m.httpx, "request", lambda method, url, **kw: client.request(method, url, **kw))
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    return seen


def test_eleventh_change_in_a_family_is_refused_without_http(monkeypatch):
    seen = _wire_status(monkeypatch, [200])
    clock = [1000.0]
    monkeypatch.setattr(m._time, "monotonic", lambda: clock[0])
    for _ in range(10):
        assert m.mailbox("sync").get("error_code") is None
        clock[0] += 1
    res = m.mailbox("sync")
    assert res["ok"] is False and res["error_code"] == "RATE_CEILING"
    assert res["status_code"] == 429 and res["family"] == "intake"
    assert isinstance(res["retry_after_sec"], int) and 1 <= res["retry_after_sec"] <= 600
    assert len(seen) == 10
    # another family is unaffected
    assert m.settings("connect_anthropic_key").get("error_code") is None
    assert len(seen) == 11
    # after the window the family passes again
    clock[0] += 600
    assert m.mailbox("sync").get("error_code") is None
    assert len(seen) == 12


def test_failed_replies_are_not_counted(monkeypatch):
    status = [400]
    seen = _wire_status(monkeypatch, status)
    for _ in range(12):
        assert m.mailbox("sync")["error_code"] != "RATE_CEILING"
    assert len(seen) == 12
    status[0] = 200
    for _ in range(10):
        assert m.mailbox("sync").get("error_code") is None
    assert m.mailbox("sync")["error_code"] == "RATE_CEILING"


def test_read_tools_have_no_counter(monkeypatch):
    _wire_status(monkeypatch, [200])
    for _ in range(15):
        m._api("GET", "/api/health")
    assert m._W_WINDOWS == {}
