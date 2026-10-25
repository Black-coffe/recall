"""mcp-mail-and-keys story 02: the first D tools, `settings(set|test_anthropic_key)`, `system_status`.

Offline: `_api` is faked, so nothing reaches app.py; the dialog itself is app.py's (covered by
tests/test_mail_gate.py and tests/test_approval_gate.py). Here: registration and annotations
per profile, argument checks before any HTTP call, the route each tool hits, and pass-through
of the gate's refusals.
"""
from __future__ import annotations

import asyncio

import pytest

import mcp_server as m
from tests.test_mcp_registry import load_mcp_copy

D_TOOLS = ("mailbox_backfill", "mailbox_label_enrich", "mailbox_disconnect", "settings_remove_anthropic_key")


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        seen.append({"method": method, "path": path, "body": body, "write": write,
                     "tool": m._CURRENT_TOOL.get(), "timeout": timeout})
        return {"success": True}

    monkeypatch.setattr(m, "_api", fake_api)
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    return seen


def _tools(mod) -> dict:
    return {t.name: t for t in asyncio.run(mod.mcp.list_tools())}


def test_d_tools_register_only_in_full_profile_with_user_interaction():
    full = _tools(load_mcp_copy("full"))
    read = _tools(load_mcp_copy("read"))
    for name in D_TOOLS:
        assert m._TOOL_CLASS[name]["class"] == "D"
        assert name in full and name not in read
        tool = full[name]
        assert tool.annotations.destructiveHint is True and tool.annotations.readOnlyHint is False
        assert tool.meta["anthropic/requiresUserInteraction"] is True
    assert "system_status" in read and read["system_status"].annotations.readOnlyHint is True


@pytest.mark.parametrize("months", [12, 121, True, "24"])
def test_backfill_checks_months_before_any_call(calls, months):
    res = m.mailbox_backfill(months)
    assert res["error_code"] == "INVALID_ARGUMENT" and calls == []


def test_backfill_hits_the_gated_route(calls):
    res = m.mailbox_backfill(36, account_id=2)
    assert calls == [{"method": "POST", "path": "/api/mail/backfill", "body": {"months": 36, "account_id": 2},
                      "write": True, "tool": "mailbox_backfill", "timeout": 90.0}]
    assert res["poll"] == "email_status"


def test_enrich_on_is_gated_off_is_plain(calls):
    m.mailbox_label_enrich(7, True)
    m.mailbox_label_enrich(7, False)
    assert [(c["method"], c["path"], c["body"]) for c in calls] == [
        ("POST", "/api/mail/labels/7/enrich", {}), ("PATCH", "/api/mail/labels/7", {"enrich": False})]
    assert m.mailbox_label_enrich(7, 1)["error_code"] == "INVALID_ARGUMENT"


def test_disconnect_and_key_removal_routes(calls):
    m.mailbox_disconnect(3)
    m.settings_remove_anthropic_key()
    assert [(c["method"], c["path"], c["tool"]) for c in calls] == [
        ("DELETE", "/api/mail/accounts/3/credentials", "mailbox_disconnect"),
        ("DELETE", "/api/settings/anthropic-key", "settings_remove_anthropic_key")]


def test_gate_refusal_passes_through_with_a_hint(monkeypatch):
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    monkeypatch.setattr(m, "_api", lambda *a, **k: {"ok": False, "error_code": "APPROVAL_DENIED",
                                                    "error": "Відмовлено", "status_code": 403})
    res = m.mailbox_disconnect(3)
    assert res["error_code"] == "APPROVAL_DENIED" and "Windows dialog" in res["hint"]


def test_readonly_mode_blocks_d_tools(monkeypatch, calls):
    monkeypatch.setattr(m, "_WRITE_DISABLED", True)
    for res in (m.mailbox_backfill(24), m.mailbox_label_enrich(1, True), m.mailbox_disconnect(1),
                m.settings_remove_anthropic_key()):
        assert res["error_code"] == "MCP_READONLY"
    assert calls == []


@pytest.mark.parametrize("key, value, path, body", [
    ("ui_lang", "uk", "/api/settings/ui-lang", {"value": "uk"}),
    ("copilot_enabled", False, "/api/settings/copilot/toggle", {"enabled": False}),
])
def test_settings_set(calls, key, value, path, body):
    m.settings("set", key=key, value=value)
    assert (calls[-1]["path"], calls[-1]["body"], calls[-1]["tool"]) == (path, body, "settings")


@pytest.mark.parametrize("key, value", [("ui_lang", "ru"), ("copilot_enabled", 1), ("copilot_enabled", "true"),
                                        ("anthropic_key", "x"), (None, None)])
def test_settings_set_rejects_before_any_call(calls, key, value):
    assert m.settings("set", key=key, value=value)["error_code"] == "INVALID_ARGUMENT"
    assert calls == []


def test_settings_test_key_and_unknown_action(calls):
    m.settings("test_anthropic_key")
    assert calls[-1]["path"] == "/api/settings/anthropic-key/test"
    assert m.settings("remove")["error_code"] == "INVALID_ACTION"
    assert m.settings("test_anthropic_key", key="ui_lang")["error_code"] == "INVALID_ARGUMENT"


def test_system_status_sections(monkeypatch):
    def fake_api(method, path, **kw):
        if path == "/api/ready":
            return {"ok": False, "error_code": "SERVICE_UNAVAILABLE", "error": "HTTP 503",
                    "status_code": 503, "ready": False, "warming": ["whisper"]}
        return {"path": path}

    monkeypatch.setattr(m, "_api", fake_api)
    res = m.system_status()
    assert res["ok"] is True
    assert res["data"]["ready"] == {"ready": False, "warming": ["whisper"]}
    assert res["data"]["health"] == {"path": "/api/health"}
    models = m.system_status("models")["data"]["models"]
    assert set(models) == {"models", "update_status"}
    assert m.system_status("everything")["error_code"] == "INVALID_ARGUMENT"


def test_system_status_passes_a_real_error_through(monkeypatch):
    monkeypatch.setattr(m, "_api", lambda *a, **k: {"ok": False, "error_code": "APP_UNREACHABLE", "error": "x"})
    assert m.system_status("hardware")["error_code"] == "APP_UNREACHABLE"
