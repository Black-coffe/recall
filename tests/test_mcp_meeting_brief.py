"""wave4-client-calls story 06: `get_meeting_brief` and `live_missed` (read-class HTTP proxies).

Offline: `_api` is faked, or `httpx.request` is replaced so the real `_api` -> `_handle_resp`
path runs against a canned response. No app.py, no network, no heavy models.
"""
from __future__ import annotations

import httpx
import pytest

import mcp_server as m
from app.services import live_missed as live_missed_service
from tests.test_mcp_registry import load_mcp_copy


def _fake_http(monkeypatch, status: int, body: dict, calls: list | None = None):
    def fake_request(method, url, **kw):
        if calls is not None:
            calls.append({"method": method, "url": url, **kw})
        return httpx.Response(status, json=body)
    monkeypatch.setattr(m.httpx, "request", fake_request)


def _no_http(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no request may be sent")
    monkeypatch.setattr(m.httpx, "request", boom)


# --- get_meeting_brief ---------------------------------------------------------------

def test_brief_by_person_returns_route_brief_unchanged(monkeypatch):
    brief = {"event": None, "hidden": 0, "people": [{"name": "Олена", "matched": True,
             "open_tasks": [{"id": 1, "task": "надіслати кошторис"}], "open_more": 0,
             "last_calls": []}]}
    seen = {}

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        seen.update(method=method, path=path, params=params)
        return {"success": True, "brief": brief}
    monkeypatch.setattr(m, "_api", fake_api)
    res = m.get_meeting_brief(person="Олена")
    assert res == {"ok": True, "brief": brief}
    assert seen == {"method": "GET", "path": "/api/meeting-brief", "params": {"person": "Олена"}}


def test_brief_by_event_id_sends_event_id(monkeypatch):
    seen = {}

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        seen["params"] = params
        return {"success": True, "brief": {"event": {"id": 7}, "people": []}}
    monkeypatch.setattr(m, "_api", fake_api)
    assert m.get_meeting_brief(event_id=7)["brief"]["event"]["id"] == 7
    assert seen["params"] == {"event_id": 7}


@pytest.mark.parametrize("kwargs", [{"event_id": 7, "person": "Олена"}, {}, {"person": "   "}])
def test_brief_needs_exactly_one_argument_before_any_request(monkeypatch, kwargs):
    _no_http(monkeypatch)
    res = m.get_meeting_brief(**kwargs)
    assert res["ok"] is False and res["error_code"] == "INVALID_ARGUMENT"


def test_brief_event_id_must_be_a_whole_number(monkeypatch):
    _no_http(monkeypatch)
    assert m.get_meeting_brief(event_id="abc")["error_code"] == "INVALID_ARGUMENT"


def test_brief_404_passes_through_handle_resp_as_not_found(monkeypatch):
    _fake_http(monkeypatch, 404, {"success": False, "error": "no such event",
                                  "error_code": "NOT_FOUND"})
    res = m.get_meeting_brief(event_id=99)
    assert res["ok"] is False and res["error_code"] == "NOT_FOUND"
    assert res["status_code"] == 404


def test_brief_app_down_is_app_unreachable(monkeypatch):
    def refused(*_a, **_k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(m.httpx, "request", refused)
    res = m.get_meeting_brief(person="Олена")
    assert res["ok"] is False and res["error_code"] == "APP_UNREACHABLE"


# --- live_missed ----------------------------------------------------------------------

def test_live_missed_posts_session_and_window(monkeypatch):
    seen = {}

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        seen.update(method=method, path=path, body=body)
        return {"success": True, "summary": "3 рядки"}
    monkeypatch.setattr(m, "_api", fake_api)
    res = m.live_missed(window="5m", session_id="rec_1")
    assert res["success"] is True
    assert seen == {"method": "POST", "path": "/api/copilot/live-missed",
                    "body": {"session_id": "rec_1", "window": "5m"}}


def test_live_missed_403_agent_session_keeps_envelope(monkeypatch):
    _fake_http(monkeypatch, 403, {"success": False, "error": "agent recording",
                                  "error_code": "AGENT_SESSION"})
    res = m.live_missed()
    assert res["ok"] is False and res["error_code"] == "AGENT_SESSION"


def test_live_missed_429_budget_keeps_spend_fields(monkeypatch):
    _fake_http(monkeypatch, 429, {"success": False, "error": "budget",
                                  "error_code": "LIVE_BUDGET_EXHAUSTED",
                                  "spent_usd": 0.5, "budget_usd": 0.5})
    res = m.live_missed(window="whole")
    assert res["ok"] is False and res["error_code"] == "LIVE_BUDGET_EXHAUSTED"
    assert res["spent_usd"] == 0.5 and res["budget_usd"] == 0.5


def test_live_missed_unknown_window_refused_before_request(monkeypatch):
    _no_http(monkeypatch)
    res = m.live_missed(window="1h")
    assert res["ok"] is False and res["error_code"] == "INVALID_ARGUMENT"


def test_live_missed_windows_match_the_service():
    assert m._LIVE_MISSED_WINDOWS == live_missed_service.WINDOWS


# --- registry ---------------------------------------------------------------------------

def test_both_tools_are_read_class_with_expected_kinds():
    mod = load_mcp_copy()
    brief, missed = mod._TOOL_CLASS["get_meeting_brief"], mod._TOOL_CLASS["live_missed"]
    assert brief["class"] == "R" and brief["limits"] is None
    assert brief["provenance_kind"] == mod._TOOL_CLASS["list_calendar_events"]["provenance_kind"]
    assert missed["class"] == "R" and missed["limits"] is None
    assert missed["provenance_kind"] == mod._TOOL_CLASS["ask_live"]["provenance_kind"]


def test_both_tools_registered_in_read_and_readonly_profiles():
    for profile, readonly in (("read", ""), ("full", "1"), ("read", "1")):
        names = set(load_mcp_copy(profile=profile, readonly=readonly)._registered_tool_names())
        assert {"get_meeting_brief", "live_missed"} <= names, (profile, readonly)


def test_both_tools_are_in_the_about_proxy_bucket():
    assert {"get_meeting_brief", "live_missed"} <= set(m._ABOUT_PROXY)
