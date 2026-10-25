"""MCP transcript-version tools: models, retranscribe, versions, activate. `_api` is faked."""
from __future__ import annotations

import pytest

import mcp_server as m
from tests.test_mcp_registry import load_mcp_copy

MODELS = [
    {"name": "tiny", "repo": "r/tiny", "info": {"x": 1}, "downloaded": False, "backend": "faster"},
    {"name": "medium", "repo": "r/medium", "info": {"y": 2}, "downloaded": True, "backend": "faster"},
]


@pytest.fixture()
def api(monkeypatch):
    calls = []
    replies = {}

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        return replies.get(path, {"success": True})

    monkeypatch.setattr(m, "_api", fake)
    return calls, replies


def test_list_whisper_models_downloaded_only(api):
    calls, replies = api
    replies["/api/models"] = MODELS
    out = m.list_whisper_models()
    assert out == {"ok": True, "backend": "faster",
                   "models": [{"name": "medium", "repo": "r/medium", "info": {"y": 2}}]}
    assert calls[0][:2] == ("GET", "/api/models")


def test_retranscribe_ok(api):
    calls, replies = api
    replies["/api/history/5/retranscribe"] = {"success": True, "job_id": "j9",
                                              "transcription_id": 5, "model": "medium"}
    out = m.retranscribe(5, "medium", diarize=True)
    assert out == {"ok": True, "job_id": "j9", "transcription_id": 5, "model": "medium"}
    method, path, kw = calls[0]
    assert (method, path) == ("POST", "/api/history/5/retranscribe")
    assert kw["write"] is True and kw["body"] == {"model": "medium", "diarize": True}


def test_retranscribe_already_running(api):
    _, replies = api
    replies["/api/history/5/retranscribe"] = {"ok": False, "error_code": "ALREADY_RUNNING",
                                              "job_id": "j3", "status_code": 409}
    assert m.retranscribe(5, "medium") == {"ok": True, "already_running": True, "job_id": "j3"}


def test_retranscribe_refusal_passes_through(api):
    _, replies = api
    replies["/api/history/5/retranscribe"] = {"ok": False, "error_code": "MODEL_NOT_INSTALLED",
                                              "error": "nope", "status_code": 400}
    out = m.retranscribe(5, "huge")
    assert out["error_code"] == "MODEL_NOT_INSTALLED" and out["ok"] is False


def test_transcript_versions_list_and_one(api):
    calls, replies = api
    replies["/api/history/5/versions"] = {"success": True, "active_version_id": None,
                                          "running_job_id": None, "versions": []}
    replies["/api/history/5/versions/2"] = {"success": True, "version": {"id": 2}}
    assert m.transcript_versions(5)["versions"] == []
    assert m.transcript_versions(5, 2) == {"ok": True, "version": {"id": 2}}
    assert [c[1] for c in calls] == ["/api/history/5/versions", "/api/history/5/versions/2"]


def test_activate(api):
    calls, replies = api
    replies["/api/history/5/versions/2/activate"] = {"success": True, "active_version_id": 2,
                                                     "reembed": "queued"}
    assert m.activate_transcript_version(5, 2) == {"ok": True, "active_version_id": 2,
                                                   "reembed": "queued"}
    assert calls[0][2]["write"] is True


def test_registry_and_profiles():
    for name, cls in (("list_whisper_models", "R"), ("transcript_versions", "R"),
                      ("retranscribe", "W"), ("activate_transcript_version", "W")):
        assert m._TOOL_CLASS[name]["class"] == cls
        assert name in m._ABOUT_PROXY
    assert m._TOOL_CLASS["retranscribe"]["args_allow"] == (
        "transcription_id", "model", "language", "diarize")
    assert m._TOOL_CLASS["retranscribe"]["limits"]["family"] == "transcripts"
    assert m._TOOL_CLASS["retranscribe"]["limits"]["paid"] is False
    ro = set(load_mcp_copy(readonly="1")._registered_tool_names())
    assert not {"retranscribe", "activate_transcript_version"} & ro
    read = set(load_mcp_copy(profile="read")._registered_tool_names())
    assert {"list_whisper_models", "transcript_versions"} <= read
    assert not {"retranscribe", "activate_transcript_version"} & read
