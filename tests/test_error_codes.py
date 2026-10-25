"""The error-code table covers every literal, and MCP errors share one envelope."""
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import mcp_server
from app.core.error_codes import ERROR_CODES, code_for_status, status_for

ROOT = Path(__file__).resolve().parent.parent
# `error_code=`/`"error_code":` plus the positional helpers `_err('CODE', ...)` and
# `_internal_error_response('CODE')` — a code passed by position must not slip past the scan.
_LITERAL = re.compile(
    r"""(?:error_code["']?\s*[:=]|\b_err\(|\b_internal_error_response\()\s*["']([A-Z][A-Z_]+)["']""")

C9_CODES = [
    "HOST_REJECTED", "ORIGIN_REJECTED", "JOB_FAILED", "JOB_CRASHED", "INVALID_ARGUMENT", "NOT_FOUND",
    "TOO_MANY_WAITERS", "MCP_READONLY", "SECRET_IN_ARGUMENT", "INVALID_ACTION", "PRIVATE",
    "MCP_VISUAL_OFF", "MCP_VISUAL_TEXT_ONLY", "PRIVACY_CHECK_FAILED", "AMBIGUOUS_SESSION",
    "APP_UNREACHABLE", "BAD_REQUEST", "UNAUTHORIZED", "FORBIDDEN", "CONFLICT", "RATE_LIMITED",
    "INTERNAL_ERROR", "UNAVAILABLE",
]


def _unknown_codes(text: str) -> set:
    return {c for c in _LITERAL.findall(text) if c not in ERROR_CODES}


def _sources():
    yield ROOT / "mcp_server.py"
    yield from sorted((ROOT / "app" / "blueprints").glob("*.py"))
    yield ROOT / "app" / "core" / "auth.py"
    yield ROOT / "app" / "core" / "approval_gate.py"


def test_every_literal_is_in_the_table():
    missing = {}
    for path in _sources():
        bad = _unknown_codes(path.read_text(encoding="utf-8"))
        if bad:
            missing[path.name] = sorted(bad)
    assert not missing


def test_scan_fires_on_unknown_code():
    assert _unknown_codes('{"error_code": "NOPE_X"}') == {"NOPE_X"}
    assert _unknown_codes("jsonify(error_code='NOPE_Y')") == {"NOPE_Y"}
    assert _unknown_codes('error_code="NOT_FOUND"') == set()
    assert _unknown_codes("return _err('NOPE_Z', 'mail.err.x', 400)") == {"NOPE_Z"}
    assert _unknown_codes("return _internal_error_response('NOPE_W')") == {"NOPE_W"}
    assert _unknown_codes("_err('INVALID_ADDRESS', 'k', 400)") == set()


def test_mail_error_codes_are_in_the_table():
    from app.services import mail_imap
    for code in (mail_imap.AUTH_FAILED, mail_imap.CONNECT_FAILED,
                 mail_imap.FOLDER_MISSING, mail_imap.PROTOCOL):
        assert code in ERROR_CODES, code
    for code in ("INVALID_ADDRESS", "NO_PASSWORD", "MAIL_POLLER_OFF",
                 "PRIVATE_CATEGORY_CHANGE", "DB_ERROR"):
        assert code in ERROR_CODES, code


def test_c9_codes_present():
    for code in C9_CODES:
        assert code in ERROR_CODES
    assert status_for("MCP_READONLY") == 403
    assert code_for_status(404) == "NOT_FOUND"
    assert code_for_status(500) == "INTERNAL_ERROR"


def test_table_does_not_pull_flask():
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; import app.core.error_codes; "
         "print('flask' in sys.modules)"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "False", out.stderr
    src = (ROOT / "app" / "core" / "error_codes.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(app|flask)", src, re.M)


def _mock(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(mcp_server.httpx, "request",
                        lambda method, url, **kw: client.request(
                            method, url, params=kw.get("params"), json=kw.get("json"),
                            headers=kw.get("headers")))


def test_http_404_json(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(
        404, json={"success": False, "error": "x", "error_code": "NOT_FOUND"}))
    res = mcp_server._api("GET", "/api/x")
    assert res["ok"] is False and res["error_code"] == "NOT_FOUND"
    assert res["status_code"] == 404 and res["success"] is False and res["error"] == "x"


def test_http_500_html_has_no_body_text(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(500, text="<html>Traceback secret/path</html>"))
    res = mcp_server._api("GET", "/api/x")
    assert res["ok"] is False and res["error_code"] == "INTERNAL_ERROR"
    assert res["status_code"] == 500
    assert "Traceback" not in str(res) and "secret" not in str(res)


def test_http_json_error_without_code_gets_status_code(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(409, json={"error": "busy"}))
    res = mcp_server._api("GET", "/api/x")
    assert res["error_code"] == "CONFLICT" and res["error"] == "busy"


def test_connection_error(monkeypatch):
    def boom(r):
        raise httpx.ConnectError("down")
    _mock(monkeypatch, boom)
    res = mcp_server._api("GET", "/api/x")
    assert res["ok"] is False and res["error_code"] == "APP_UNREACHABLE"


def test_success_unchanged(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"a": 1, "success": True}))
    assert mcp_server._api("GET", "/api/x") == {"a": 1, "success": True}


def test_mcp_side_errors_use_envelope(monkeypatch):
    monkeypatch.setattr(mcp_server, "_WRITE_DISABLED", True)
    ro = mcp_server._api("POST", "/api/x", write=True)
    assert ro["ok"] is False and ro["error_code"] == "MCP_READONLY"
    inv = mcp_server._invalid_action("bad")
    assert inv["ok"] is False and inv["error_code"] == "INVALID_ACTION"
    amb = mcp_server._ambiguity_payload([{"session_id": "a"}, {"session_id": "b"}])
    assert amb["ok"] is False and amb["error_code"] == "AMBIGUOUS_SESSION" and amb["error"]
    monkeypatch.setattr(mcp_server._privacy, "visual_mode", lambda: "off")
    vis = mcp_server._visual_refusal(None)
    assert vis["ok"] is False and vis["error_code"] == "MCP_VISUAL_OFF"


def test_direct_db_not_found_envelope(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT);")
    monkeypatch.setattr(mcp_server, "DB_PATH", str(db))
    res = mcp_server.get_entity.fn(999) if hasattr(mcp_server.get_entity, "fn") else mcp_server.get_entity(999)
    assert res["ok"] is False and res["error_code"] == "NOT_FOUND"


def test_get_job_status_failed_job_is_generic(monkeypatch):
    job = {"id": "j1", "state": "failed", "error": "Job failed", "error_code": "JOB_FAILED"}
    _mock(monkeypatch, lambda r: httpx.Response(200, json=job))
    fn = getattr(mcp_server.get_job_status, "fn", mcp_server.get_job_status)
    res = fn("j1")
    assert res["error"] == "Job failed" and res["error_code"] == "JOB_FAILED"


# --- the catch-all: an unhandled tool exception is the envelope, not raw text -------------

def _call(mod, name: str, args: dict):
    import asyncio
    from fastmcp import Client

    async def go():
        async with Client(mod.mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)
    return asyncio.run(go())


def test_uncaught_tool_exception_is_enveloped_without_raw_text(tmp_path, monkeypatch):
    from tests.test_mcp_registry import load_mcp_copy
    mod = load_mcp_copy()
    import sqlite3
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # a DB without tables: `no such table: transcriptions`
    monkeypatch.setattr(mod, "DB_PATH", str(db))
    for name, args in (("get_transcript", {"transcription_id": 1}), ("list_recent", {})):
        res = _call(mod, name, args)
        body = res.structured_content
        assert body["ok"] is False and body["error_code"] == "INTERNAL_ERROR", (name, body)
        text = str(body) + "".join(getattr(c, "text", "") for c in (res.content or []))
        assert "no such table" not in text and "transcriptions" not in text, (name, text)


@pytest.mark.parametrize("name,args", [
    ("list_categories", {}), ("list_entities", {}), ("list_dropped_commitments", {}),
])
def test_list_returning_tool_failure_is_the_envelope(tmp_path, monkeypatch, name, args):
    """A tool with a `{"result": [...]}` output schema still answers the envelope, not
    "Output validation error", and names the real exception type, not ToolError."""
    from tests.test_mcp_registry import load_mcp_copy
    mod = load_mcp_copy()
    import sqlite3
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(mod, "DB_PATH", str(db))
    res = _call(mod, name, args)
    body = res.structured_content
    assert res.is_error is True
    assert body["ok"] is False and body["error_code"] == "INTERNAL_ERROR", (name, body)
    assert "OperationalError" in body["error"] and "ToolError" not in body["error"]
    text = "".join(getattr(c, "text", "") for c in (res.content or []))
    assert "Output validation" not in text and "no such table" not in text


def test_list_returning_tool_bad_argument_is_invalid_argument():
    from tests.test_mcp_registry import load_mcp_copy
    mod = load_mcp_copy()
    res = _call(mod, "list_entities", {"limit": "many"})
    body = res.structured_content
    assert body["ok"] is False and body["error_code"] == "INVALID_ARGUMENT", body


def test_bad_argument_type_is_invalid_argument():
    from tests.test_mcp_registry import load_mcp_copy
    mod = load_mcp_copy()
    res = _call(mod, "get_transcript", {"transcription_id": "not-a-number"})
    body = res.structured_content
    assert body["ok"] is False and body["error_code"] == "INVALID_ARGUMENT"
    assert "transcription_id" in body["error"]


def test_research_export_filter_error_is_enveloped(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "t.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(mcp_server, "DB_PATH", str(db))
    fn = getattr(mcp_server.research_export, "fn", mcp_server.research_export)
    res = fn("term", since="вчора")
    assert isinstance(res, dict) and res["ok"] is False
    assert res["error_code"] == "INVALID_ARGUMENT"
