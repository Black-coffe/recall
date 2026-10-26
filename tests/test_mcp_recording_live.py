"""MCP recording and live-call tools (mcp-recording-live-06): the six new tools, their registry
entries, argument shaping, the call-prompt projection, the gated stop/cancel and the extended
`get_active_recording`. `_api` is faked for routing; header and refusal pass-through go through the
real `_api` over an httpx mock transport. Offline."""
from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest

import mcp_server as m
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from tests.test_mcp_registry import load_mcp_copy

NEW_R = ("recording_devices", "get_copilot_timeline")
NEW_W = ("recording", "copilot")
NEW_D = ("recording_stop", "recording_cancel_own")


@pytest.fixture()
def api(monkeypatch):
    calls: list = []
    replies: dict = {}

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        reply = replies.get((method, path), replies.get(path, {"success": True}))
        return json.loads(json.dumps(reply))

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


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "rec.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


# --- registry -----------------------------------------------------------------------------------

def test_six_tools_registered_with_entries():
    full = load_mcp_copy()
    names = set(full._registered_tool_names())
    for name in NEW_R + NEW_W + NEW_D:
        assert name in names, name
        assert name in full._ABOUT_PROXY, name
    reg = full._TOOL_CLASS
    assert (reg["recording_devices"]["class"], reg["recording_devices"]["provenance_kind"]) == ("R", "metadata")
    assert (reg["get_copilot_timeline"]["class"], reg["get_copilot_timeline"]["provenance_kind"]) == ("R", "copilot")
    rec = reg["recording"]
    assert rec["class"] == "W" and rec["limits"]["family"] == "recording"
    assert rec["args_allow"] == ("action", "mic", "system_mode", "language", "max_duration_min",
                                 "copilot.mode", "with_screen")
    assert "name" not in rec["args_allow"] and "system_apps" not in rec["args_allow"]
    assert reg["copilot"]["class"] == "W" and reg["copilot"]["limits"]["family"] == "recording"
    for name in NEW_D:
        assert reg[name]["class"] == "D", name


def test_d_tools_derive_destructive_and_user_interaction():
    import asyncio
    full = load_mcp_copy()
    tools = {t.name: t for t in asyncio.run(full.mcp.list_tools())}
    for name in NEW_D:
        assert tools[name].annotations.destructiveHint is True, name
        assert tools[name].meta["anthropic/requiresUserInteraction"] is True, name
    read = set(load_mcp_copy(profile="read")._registered_tool_names())
    assert set(NEW_R) <= read and not (set(NEW_W + NEW_D) & read)


# --- recording_devices --------------------------------------------------------------------------

def test_call_prompts_are_projected_without_tokens(api):
    calls, replies = api
    replies["/api/recording/autoprompt"] = {"success": True, "enabled": True, "prompts": [{
        "id": "tok-secret", "kind": "record", "exe": "zoom.exe", "label": "Zoom", "session_id": None,
        "mic_endpoint": "Mic (USB)", "created_at": "2026-10-26T10:00:00+00:00",
        "actions": {"record": "http://127.0.0.1:5050/api/recording/autoprompt/tok-secret/record"}}]}
    replies["/api/recording/screens"] = {"success": True, "video_available": True, "screens": []}
    out = m.recording_devices("call_prompts")
    assert out["video_available"] is True
    row = out["prompts"][0]
    assert set(row) == {"kind", "exe", "label", "created_at", "session_id"}
    for leaked in ("id", "actions", "mic_endpoint"):
        assert leaked not in row
    assert "tok-secret" not in json.dumps(out)


def test_device_kinds_proxy_their_gets(api):
    calls, _ = api
    for kind in ("audio", "screens", "apps"):
        m.recording_devices(kind)
    assert [c[:2] for c in calls] == [("GET", "/api/recording/devices"), ("GET", "/api/recording/screens"),
                                      ("GET", "/api/recording/apps")]
    assert m.recording_devices("cameras")["error_code"] == "INVALID_ARGUMENT"
    assert len(calls) == 3


# --- recording (W) ------------------------------------------------------------------------------

def test_start_shapes_the_body(api):
    calls, _ = api
    m.recording("start", system_apps=["zoom.exe"], system_mode="include", language="uk", name="Дзвінок",
                copilot={"mode": "light"}, max_duration_min=60)
    method, path, kw = calls[0]
    assert (method, path, kw["write"]) == ("POST", "/api/recording/start", True)
    assert kw["body"] == {"mic": "default", "system_source": {"mode": "include", "apps": ["zoom.exe"]},
                          "language": "uk", "name": "Дзвінок", "copilot": {"mode": "light"},
                          "max_duration_min": 60}


def test_screen_only_with_explicit_flag(api):
    calls, _ = api
    m.recording("start")
    m.recording("start", with_screen=False)
    m.recording("start", with_screen=True, mic=3)
    assert calls[0][2]["body"] == {"mic": "default"}
    assert "video" not in calls[1][2]["body"]
    assert calls[2][2]["body"] == {"mic_device_index": 3, "video": {"enabled": True}}


@pytest.mark.parametrize("status,code", [(403, "OWNER_SESSION"), (409, "SESSION_LIMIT"),
                                         (503, "NOTIFY_UNAVAILABLE")])
def test_start_refusals_pass_through(wire, status, code):
    _, replies = wire
    replies["/api/recording/start"] = (status, {"success": False, "error": "no", "error_code": code})
    out = m.recording("start")
    assert (out["ok"], out["error_code"], out["status_code"]) == (False, code, status)


def test_pause_resume_retarget_routes(api):
    calls, _ = api
    m.recording("pause", session_id="rec_a1")
    m.recording("resume", session_id="rec_a1")
    m.recording("retarget", session_id="rec_a1", system_mode="exclude", system_apps=["chrome.exe"])
    assert [(c[0], c[1], c[2]["body"]) for c in calls] == [
        ("POST", "/api/recording/rec_a1/pause", {}),
        ("POST", "/api/recording/rec_a1/resume", {}),
        ("POST", "/api/recording/rec_a1/system-source", {"mode": "exclude", "apps": ["chrome.exe"]}),
    ]
    assert m.recording("pause", session_id="../x")["error_code"] == "INVALID_ARGUMENT"
    assert m.recording("rewind")["error_code"] == "INVALID_ACTION"
    assert len(calls) == 3


# --- copilot (W) --------------------------------------------------------------------------------

def test_copilot_routes(api):
    calls, _ = api
    m.copilot(7, "settings", mode="hard")
    m.copilot(7, "card", card_action="pin", event_id=12)
    m.copilot(7, "stop", transcription_id=4)
    m.copilot(7, "reingest")
    assert [(c[1], c[2]["body"]) for c in calls] == [
        ("/api/copilot/7/settings", {"mode": "hard"}),
        ("/api/copilot/7/action", {"action": "pin", "event_id": 12}),
        ("/api/copilot/7/stop", {"transcription_id": 4}),
        ("/api/copilot/7/reingest", {}),
    ]


def test_copilot_escalate_refused_before_any_request(api):
    calls, _ = api
    assert m.copilot(7, "card", card_action="escalate", event_id=1)["error_code"] == "INVALID_ARGUMENT"
    assert calls == []


# --- recording_stop / recording_cancel_own (D) --------------------------------------------------

def test_stop_sends_note_and_name(wire):
    seen, replies = wire
    name = "Дзвінок з фондом про бюджет на наступний квартал і план робіт команди на рік вперед"
    replies["/api/recording/rec_a1/stop"] = (200, {"success": True, "session_id": "rec_a1",
                                                    "status": "stopping", "already": False})
    out = m.recording_stop("rec_a1", name=name)
    req = seen[0]
    assert (req.method, req.url.path) == ("POST", "/api/recording/rec_a1/stop")
    assert req.headers["X-Recall-Tool"] == "recording_stop"
    wire_note = dict((k.lower(), v) for k, v in req.headers.raw)[b"x-recall-note"].decode("ascii")
    assert urllib.parse.unquote(wire_note) == name[:60].strip()
    assert json.loads(req.content) == {"name": name}
    assert out["status"] == "stopping"


def test_stop_already_body_is_returned_as_is(wire):
    _, replies = wire
    body = {"success": True, "session_id": "rec_a1", "status": "finalized", "already": True}
    replies["/api/recording/rec_a1/stop"] = (200, body)
    assert m.recording_stop("rec_a1") == body


def test_stop_without_id_picks_the_single_live_session(api):
    calls, replies = api
    replies["/api/recordings/active"] = {"success": True, "active": True, "count": 1,
                                         "sessions": [{"session_id": "rec_one"}], "session_id": "rec_one"}
    m.recording_stop()
    assert calls[-1][1] == "/api/recording/rec_one/stop"


def test_stop_without_id_ambiguous_or_none(api):
    calls, replies = api
    replies["/api/recordings/active"] = {"success": True, "active": True, "count": 2, "session_id": None,
                                         "sessions": [{"session_id": "rec_a"}, {"session_id": "rec_b"}]}
    assert m.recording_stop()["error_code"] == "AMBIGUOUS_SESSION"
    replies["/api/recordings/active"] = {"success": True, "active": False, "count": 0, "sessions": []}
    assert m.recording_stop()["error_code"] == "NOT_FOUND"
    assert all(c[1] == "/api/recordings/active" for c in calls)


def test_cancel_own_is_gated(wire):
    seen, replies = wire
    replies["/api/recording/rec_a1/discard"] = (409, {"success": False, "error": "late",
                                                       "error_code": "CANCEL_WINDOW_CLOSED"})
    out = m.recording_cancel_own("rec_a1")
    assert seen[0].headers["X-Recall-Tool"] == "recording_cancel_own"
    assert out["error_code"] == "CANCEL_WINDOW_CLOSED"
    assert m.recording_cancel_own("rec/../x")["error_code"] == "INVALID_ARGUMENT"
    assert len(seen) == 1


# --- get_copilot_timeline (R) -------------------------------------------------------------------

def test_timeline_list_caps_limit(api):
    calls, replies = api
    replies["/api/copilot/sessions"] = {"success": True, "sessions": [{"id": 1}]}
    out = m.get_copilot_timeline(limit=500, offset=3)
    assert calls[0][2]["params"] == {"limit": 100, "offset": 3}
    assert out["sessions"] == [{"id": 1}]


def test_timeline_by_id_with_comment_provenance(api, db):
    calls, replies = api
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO comments (target_type, target_id, target_key, body, kind, source) "
                     "VALUES ('recording_session', 0, 'rec_a1', 'owner note', 'note', 'ui')")
        conn.execute("INSERT INTO comments (target_type, target_id, target_key, body, kind, source) "
                     "VALUES ('recording_session', 0, 'rec_a1', 'agent note', 'note', 'mcp')")
        conn.commit()
    replies["/api/copilot/5/timeline"] = {"success": True, "timeline": {
        "session": {"id": 5, "recording_session_id": "rec_a1", "transcription_id": None},
        "topics": [], "events": [], "aggregates": {}}}
    out = m.get_copilot_timeline(copilot_session_id=5)
    prov = {c["body"]: c["provenance"] for c in out["timeline"]["comments"]}
    assert prov == {"owner note": "owner", "agent note": "mcp_agent"}


def test_timeline_md_export_and_by_transcription(api):
    calls, replies = api
    replies["/api/copilot/5/export"] = {"status_code": 200, "text": "# Session\n" + "x" * 500}
    out = m.get_copilot_timeline(copilot_session_id=5, format="md", max_chars=100)
    assert calls[0][2]["params"] == {"format": "md"}
    assert len(out["text"]) == 100 and out["truncated"] is True
    replies["/api/copilot/by-transcription/9"] = {"success": True, "found": False}
    assert m.get_copilot_timeline(transcription_id=9)["found"] is False
    assert calls[-1][1] == "/api/copilot/by-transcription/9"


def test_timeline_budget_drops_events(api):
    _, replies = api
    events = [{"id": i, "kind": "insight_local", "payload": {"text": "ж" * 300}} for i in range(200)]
    replies["/api/copilot/5/timeline"] = {"success": True, "timeline": {
        "session": {"id": 5}, "topics": [], "events": events, "aggregates": {}}}
    out = m.get_copilot_timeline(copilot_session_id=5, max_tokens=2000)
    assert out["truncated"] is True and out["events_total"] == 200
    assert out["events_shown"] == len(out["timeline"]["events"]) < 200
    assert m._json_len(out) <= 2000 * m._CHARS_PER_TOKEN


def test_timeline_copilot_disabled_passes_through(wire):
    _, replies = wire
    replies["/api/copilot/sessions"] = (503, {"success": False, "error": "off",
                                              "error_code": "COPILOT_DISABLED"})
    out = m.get_copilot_timeline()
    assert (out["error_code"], out["status_code"]) == ("COPILOT_DISABLED", 503)


# --- get_active_recording -----------------------------------------------------------------------

def test_active_recording_by_session_id(api, db):
    calls, replies = api
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path) VALUES ('', 'recording_rec_x', 'r', 'x.mp3')")
        conn.commit()
        lib_id = conn.execute("SELECT id FROM audio_downloads").fetchone()[0]
    replies["/api/recording/rec_x/state"] = {"success": True, "state": {"status": "finalized",
                                                                         "started_by": "mcp"}}
    out = m.get_active_recording(session_id="rec_x")
    assert (out["started_by"], out["library_download_id"]) == ("mcp", lib_id)
    replies["/api/recording/rec_y/state"] = {"success": True, "state": {"status": "recording"}}
    out = m.get_active_recording(session_id="rec_y")
    assert (out["started_by"], out["library_download_id"]) == ("ui", None)


def test_active_recording_include_recovered_never_acks(wire):
    seen, replies = wire
    replies["/api/recordings/active"] = (200, {"success": True, "active": False, "count": 0, "sessions": []})
    replies["/api/recordings/recovered"] = (200, {"success": True, "recovered": [{"session_id": "rec_r"}],
                                                  "count": 1})
    out = m.get_active_recording(include_recovered=True)
    rec_req = [r for r in seen if r.url.path == "/api/recordings/recovered"][0]
    assert rec_req.url.query == b"ack=0"
    assert out["recovered"] == [{"session_id": "rec_r"}]
