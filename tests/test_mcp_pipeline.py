"""MCP intake pipeline tools (mcp-pipeline-05): the twelve new tools, `get_job_status` lists and
waits, `video_analyze` at 60 s, the new rate families and the count. `_api` is faked (the
`fake_api` seam of test_mcp_archive_org); headers and ceilings go through the real `_api` over an
httpx mock transport. Offline."""
from __future__ import annotations

import httpx
import pytest

import mcp_server as m
from app.db.migrations import init_database
from tests.test_mcp_registry import load_mcp_copy

NEW_R = ("get_youtube_info", "list_audio_library")
NEW_W = ("audio_download", "audio_item", "transcribe", "document_ingest", "job_cancel", "transcript_ai",
         "models", "system_diagnostics")
NEW_D = ("document_import_folder", "document_reparse")
FAMILIES = {"audio_download": ("intake", 600, 10), "audio_item": ("transcripts", 600, 10),
            "transcribe": ("transcripts", 600, 10), "document_ingest": ("paid", 86400, 20),
            "job_cancel": ("system", 600, 10), "transcript_ai": ("paid", 86400, 20),
            "models": ("models", 86400, 2), "system_diagnostics": ("diagnostics", 600, 1),
            "document_import_folder": ("intake", 600, 10), "document_reparse": ("intake", 600, 10)}
PAID = {"transcribe", "document_ingest", "transcript_ai", "document_import_folder", "document_reparse"}


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
    """The real `_api` over a mock transport: every request is recorded, replies by path."""
    seen: list[httpx.Request] = []
    replies: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = replies.get(request.url.path, (200, {"success": True, "job_id": "j1"}))
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(m.httpx, "request", lambda method, url, **kw: client.request(method, url, **kw))
    monkeypatch.setattr(m, "_WRITE_DISABLED", False)
    m._W_WINDOWS.clear()
    yield seen, replies
    m._W_WINDOWS.clear()


# --- registry ---------------------------------------------------------------------------------

def test_twelve_tools_with_class_family_flags_and_allowlist():
    full = load_mcp_copy()
    names = set(full._registered_tool_names())
    for name in NEW_R + NEW_W + NEW_D:
        assert name in names, name
    for name in NEW_R:
        e = full._TOOL_CLASS[name]
        assert (e["class"], e["provenance_kind"], e["limits"]) == ("R", "youtube_metadata", None), name
    assert full._TOOL_CLASS["get_youtube_info"]["open_world"] is True
    for name in NEW_W:
        assert full._TOOL_CLASS[name]["class"] == "W", name
    for name in NEW_D:
        assert full._TOOL_CLASS[name]["class"] == "D", name
    for name, (family, window, cap) in FAMILIES.items():
        lim = full._TOOL_CLASS[name]["limits"]
        assert (lim["family"], lim["window_s"], lim["max"]) == (family, window, cap), name
        assert lim["paid"] is (name in PAID), name
    for name in ("transcript_ai", "models", "audio_download"):
        assert full._TOOL_CLASS[name]["open_world"] is True, name
    for name in ("audio_item", "job_cancel", "system_diagnostics"):
        assert full._TOOL_CLASS[name]["open_world"] is False, name
    for name in NEW_W + NEW_D:
        allow = full._TOOL_CLASS[name]["args_allow"]
        assert "title" not in allow and "description" not in allow, name
    assert "force" not in full._TOOL_CLASS["transcript_ai"]["args_allow"]


def test_read_profile_and_readonly_keep_the_new_writes_out():
    read = set(load_mcp_copy(profile="read")._registered_tool_names())
    assert set(NEW_R) <= read and not (set(NEW_W + NEW_D) & read)
    readonly = set(load_mcp_copy(readonly="1")._registered_tool_names())
    assert set(NEW_R) <= readonly and not (set(NEW_W + NEW_D) & readonly)


def test_no_force_model_or_delete_argument_on_the_new_tools():
    import inspect
    for name in NEW_W + NEW_D + NEW_R:
        params = set(inspect.signature(getattr(m, name)).parameters)
        assert not params & {"force", "check_duplicate", "delete", "destination", "library"}, name
    assert "model" not in inspect.signature(m.transcript_ai).parameters


# --- headers, timeouts, ceilings ---------------------------------------------------------------

@pytest.mark.parametrize("tool,call", [
    ("transcribe", lambda: m.transcribe("library", audio_download_id=3)),
    ("document_ingest", lambda: m.document_ingest(["C:/docs/a.pdf"])),
    ("audio_download", lambda: m.audio_download("https://youtu.be/x")),
    ("audio_item", lambda: m.audio_item(4, "restore")),
    ("job_cancel", lambda: m.job_cancel("j9")),
    ("transcript_ai", lambda: m.transcript_ai("summarize", 5)),
    ("models", lambda: m.models("check_updates")),
    ("system_diagnostics", lambda: m.system_diagnostics()),
    ("document_import_folder", lambda: m.document_import_folder("C:/docs")),
    ("document_reparse", lambda: m.document_reparse(6)),
])
def test_every_write_sends_actor_and_tool_headers(wire, tool, call):
    seen, _ = wire
    call()
    assert seen, tool
    assert seen[0].headers["X-Recall-Actor"] == "mcp"
    assert seen[0].headers["X-Recall-Tool"] == tool


def test_d_tools_go_through_the_gate_with_sixty_seconds(api, monkeypatch):
    gated: list = []
    real = m._gated

    def spy(tool, method, path, body=None, timeout=90.0):
        gated.append((tool, method, path, body, timeout))
        return real(tool, method, path, body, timeout=timeout)

    monkeypatch.setattr(m, "_gated", spy)
    m.document_import_folder("C:/docs", recursive=True, category_id=2)
    m.document_reparse(6)
    assert gated == [
        ("document_import_folder", "POST", "/api/documents/import-folder",
         {"path": "C:/docs", "recursive": True, "category_id": 2}, 60.0),
        ("document_reparse", "POST", "/api/documents/6/reparse-job", {}, 60.0)]
    calls, _ = api
    assert all(kw["write"] is True and kw["timeout"] == 60.0 for _, _, kw in calls)


def test_d_tool_results(api):
    calls, replies = api
    replies["/api/documents/import-folder"] = {"success": True, "started": True, "total": 4,
                                               "channel": "doc_import", "job_id": "d1"}
    replies["/api/documents/6/reparse-job"] = {"success": True, "job_id": "r1"}
    assert m.document_import_folder("C:/docs") == {"ok": True, "job_id": "d1", "total": 4}
    assert m.document_reparse(6) == {"ok": True, "job_id": "r1"}


def test_d_envelopes_carry_the_gate_hint(api):
    _, replies = api
    replies["/api/documents/6/reparse-job"] = {"ok": False, "error_code": "APPROVAL_DENIED",
                                               "error": "no", "status_code": 403}
    res = m.document_reparse(6)
    assert res["error_code"] == "APPROVAL_DENIED" and res["hint"] == m._GATE_NOTE
    replies["/api/documents/import-folder"] = {"ok": False, "error_code": "RECORDING_ACTIVE",
                                               "error": "rec", "status_code": 409}
    res = m.document_import_folder("C:/docs")
    assert res["error_code"] == "RECORDING_ACTIVE" and res["hint"] == m._GATE_NOTE


def test_video_analyze_waits_sixty_seconds(api, monkeypatch):
    monkeypatch.setattr(m, "_visual_refusal", lambda *a, **kw: None)
    calls, _ = api
    m.video_analyze(7, describe=True, vision_backend="claude")
    method, path, kw = calls[0]
    assert (method, path, kw["write"], kw["timeout"]) == ("POST", "/api/transcription/7/analyze-video",
                                                         True, 60.0)
    assert "dialog" in m.video_analyze.__doc__


def test_diagnostics_family_allows_one_per_ten_minutes(wire):
    _, replies = wire
    replies["/api/system/diagnostic-package/file"] = (200, {"success": True, "path": "p", "size": 1})
    assert m.system_diagnostics()["ok"] is True
    res = m.system_diagnostics()
    assert res["error_code"] == "RATE_CEILING" and res["family"] == "diagnostics"


def test_models_family_allows_two_a_day_across_both_actions(wire):
    seen, replies = wire
    replies["/api/models/check-updates"] = (200, {"latest": "1.2"})
    replies["/api/models/download-job"] = (202, {"success": True, "job_id": "m1", "created": True})
    assert m.models("check_updates")["latest"] == "1.2"
    assert m.models("download", model_name="small")["job_id"] == "m1"
    res = m.models("check_updates")
    assert res["error_code"] == "RATE_CEILING" and res["family"] == "models"
    assert len(seen) == 2


def test_paid_family_is_shared_by_transcript_ai_and_document_ingest(wire):
    seen, replies = wire
    replies["/api/transcription/5/ai-job"] = (202, {"success": True, "cached": False, "job_id": "a"})
    replies["/api/documents/ingest-paths"] = (202, {"success": True, "job_id": "d", "total": 1})
    for _ in range(10):
        assert m.transcript_ai("summarize", 5)["ok"] is True
        assert m.document_ingest(["C:/docs/a.pdf"])["ok"] is True
    res = m.transcript_ai("summarize", 5)
    assert res["error_code"] == "RATE_CEILING" and res["family"] == "paid"
    assert len(seen) == 20


def test_refusals_keep_their_code_through_the_real_api(wire):
    _, replies = wire
    for code, status in (("RECORDING_ACTIVE", 409), ("PATH_NOT_ALLOWED", 403), ("UNKNOWN_MODEL", 400),
                         ("APPROVAL_DENIED", 403)):
        replies["/api/transcribe/async"] = (status, {"success": False, "error_code": code, "error": "x"})
        res = m.transcribe("path", path="C:/x.mp3")
        assert (res["ok"], res["error_code"], res["status_code"]) == (False, code, status)
    replies["/api/models/download-job"] = (400, {"success": False, "error_code": "UNKNOWN_MODEL",
                                                 "error": "bad"})
    assert m.models("download", model_name="huge")["error_code"] == "UNKNOWN_MODEL"


# --- transcribe -------------------------------------------------------------------------------

def test_transcribe_waits_once_and_returns_the_job_result(api):
    calls, replies = api
    replies["/api/transcribe/async"] = {"success": True, "job_id": "t1", "created": True}
    replies["/api/jobs/t1"] = {"id": "t1", "kind": "transcription", "state": "completed", "error": None,
                               "meta": {"stage": "done", "actor": "mcp"},
                               "result": {"transcription_id": 42}}
    res = m.transcribe("youtube", url="https://youtu.be/x", wait_seconds=10)
    assert res["ok"] is True and res["job_id"] == "t1" and res["created"] is True
    assert res["job"]["result"]["transcription_id"] == 42
    assert res["job"]["phase"] == "done" and res["job"]["provenance"] == "mcp_agent"
    assert [(c[0], c[1]) for c in calls] == [("POST", "/api/transcribe/async"), ("GET", "/api/jobs/t1")]
    assert calls[0][2]["body"] == {"source": "youtube", "url": "https://youtu.be/x"}
    assert calls[1][2]["params"] == {"wait": 10}


def test_transcribe_without_wait_does_not_poll(api):
    calls, replies = api
    replies["/api/transcribe/async"] = {"success": True, "job_id": "t1", "created": True}
    assert m.transcribe("library", audio_download_id=3) == {"ok": True, "job_id": "t1", "created": True}
    assert len(calls) == 1


def test_transcribe_duplicate_maps_to_created_false(api):
    _, replies = api
    replies["/api/transcribe/async"] = {"success": True, "job_id": "t0", "created": False}
    assert m.transcribe("library", audio_download_id=3) == {"ok": True, "created": False, "job_id": "t0"}


def test_transcribe_not_finalized_names_wait_for(api):
    _, replies = api
    replies["/api/transcribe/async"] = {"ok": False, "error_code": "NOT_FINALIZED", "error": "wait",
                                        "status_code": 409, "session_id": "s7"}
    res = m.transcribe("recording", recording_session_id="s7")
    assert res["error_code"] == "NOT_FINALIZED"
    assert "wait_for(channel='recording:s7')" in res["hint"]


def test_transcribe_checks_source_and_ref_before_http(api):
    calls, _ = api
    assert m.transcribe("dropbox")["error_code"] == "INVALID_ARGUMENT"
    assert m.transcribe("path")["error_code"] == "INVALID_ARGUMENT"
    assert calls == []


# --- documents ---------------------------------------------------------------------------------

def test_document_ingest_six_paths_is_bulk_limit_before_http(api):
    calls, _ = api
    res = m.document_ingest([f"C:/docs/{i}.pdf" for i in range(6)])
    assert res["error_code"] == "BULK_LIMIT"
    assert calls == []


def test_document_ingest_five_paths_returns_job_and_items_when_waited(api):
    calls, replies = api
    paths = [f"C:/docs/{i}.pdf" for i in range(5)]
    replies["/api/documents/ingest-paths"] = {"success": True, "job_id": "d1", "total": 5}
    assert m.document_ingest(paths) == {"ok": True, "job_id": "d1", "total": 5}
    items = [{"path": p, "status": "created", "transcription_id": i} for i, p in enumerate(paths)]
    replies["/api/jobs/d1"] = {"id": "d1", "state": "completed", "meta": {}, "result": {"items": items}}
    res = m.document_ingest(paths, category_id=3, wait_seconds=25)
    assert res["items"] == items and res["total"] == 5
    assert calls[1][2]["body"] == {"paths": paths, "category_id": 3}


# --- jobs ------------------------------------------------------------------------------------

def test_job_list_honours_filters_and_marks_agent_rows(api):
    calls, replies = api
    replies["/api/jobs"] = {"jobs": [{"id": "a", "state": "running", "meta": {"actor": "mcp", "stage": "x"}},
                                     {"id": "b", "state": "queued", "meta": {"actor": "ui"}}]}
    res = m.get_job_status(kind="transcription", active_only=True)
    assert calls[0][1] == "/api/jobs"
    assert calls[0][2]["params"] == {"kind": "transcription", "active_only": "true"}
    assert res["jobs"][0]["provenance"] == "mcp_agent"
    assert "provenance" not in res["jobs"][1]


def test_job_by_id_passes_wait_log_tail_and_reads_the_fields(api):
    calls, replies = api
    replies["/api/jobs/j1"] = {"id": "j1", "state": "completed", "error": None, "error_code": None,
                               "cancel_ignored": True, "meta": {"stage": "saving"},
                               "result": {"transcription_id": 9}, "log": [{"message": "ok"}]}
    res = m.get_job_status("j1", wait_seconds=30, include_log=True, tail=500)
    assert calls[0][2]["params"] == {"wait": 25, "include_log": 1, "tail": 200}
    assert calls[0][2]["timeout"] > 25
    for key in ("state", "phase", "error_code", "result", "cancel_ignored", "log"):
        assert key in res, key
    assert res["phase"] == "saving"


def test_crashed_job_is_explained_and_docstring_covers_cancel(api):
    _, replies = api
    replies["/api/jobs/c1"] = {"id": "c1", "state": "crashed", "error_code": "JOB_CRASHED", "meta": {}}
    res = m.get_job_status("c1")
    assert "safe to retry" in res["hint"].lower()
    doc = m.get_job_status.__doc__
    assert "crashed" in doc and "safe to retry" in doc and "cooperative" in doc


def test_job_cancel_posts_to_the_cancel_route(api):
    calls, _ = api
    assert m.job_cancel("j5")["ok"] is True
    assert calls[0][:2] == ("POST", "/api/jobs/j5/cancel")


# --- transcript AI, models, diagnostics --------------------------------------------------------

def test_transcript_ai_sends_no_force_or_model_and_cache_is_free(api):
    calls, replies = api
    replies["/api/transcription/5/ai-job"] = {"success": True, "cached": True, "status": "done",
                                              "result": {"summary": "s"}}
    res = m.transcript_ai("summarize", 5)
    assert res == {"ok": True, "cached": True, "kind": "summarize", "result": {"summary": "s"}}
    assert "job_id" not in res
    assert calls[0][2]["body"] == {"kind": "summarize"}
    m.transcript_ai("translate", 5, target_lang="en")
    assert calls[1][2]["body"] == {"kind": "translate", "target_lang": "en"}


def test_transcript_ai_job_and_wait(api):
    calls, replies = api
    replies["/api/transcription/5/ai-job"] = {"success": True, "cached": False, "created": True, "job_id": "a1"}
    replies["/api/jobs/a1"] = {"id": "a1", "state": "completed", "meta": {}, "result": {"kind": "topics"}}
    res = m.transcript_ai("topics", 5, wait_seconds=5)
    assert res["cached"] is False and res["job_id"] == "a1" and res["job"]["result"] == {"kind": "topics"}


def test_models_actions(api):
    calls, replies = api
    replies["/api/models/download-job"] = {"success": True, "job_id": "m1", "created": False}
    assert m.models("download", model_name="small") == {"ok": True, "job_id": "m1", "created": False}
    assert calls[0][2]["body"] == {"model_name": "small"}
    replies["/api/models/check-updates"] = {"installed": "1.1", "latest": "1.2"}
    assert m.models("check_updates")["latest"] == "1.2"
    assert m.models("purge")["error_code"] == "INVALID_ACTION"
    assert m.models("download")["error_code"] == "INVALID_ARGUMENT"
    assert len(calls) == 2


def test_system_diagnostics_shape(api):
    _, replies = api
    replies["/api/system/diagnostic-package/file"] = {
        "success": True, "path": "logs/diagnostics/x.zip", "size": 10, "included_logs": ["a.log"],
        "app_version": "4.27.0", "git_commit": "abc"}
    assert m.system_diagnostics() == {"ok": True, "path": "logs/diagnostics/x.zip", "size": 10,
                                      "included_logs": ["a.log"], "app_version": "4.27.0",
                                      "git_commit": "abc"}


# --- audio library and YouTube -----------------------------------------------------------------

def test_audio_download_started_and_exists(api):
    calls, replies = api
    replies["/api/audio/download"] = {"success": True, "download_id": "dl1", "status": "started"}
    assert m.audio_download("https://youtu.be/x", quality="192")["job_id"] == "dl1"
    assert calls[0][2]["body"] == {"url": "https://youtu.be/x", "quality": "192"}
    replies["/api/audio/download"] = {"success": True, "status": "exists", "download_id": 8, "info": {}}
    res = m.audio_download("https://youtu.be/x")
    assert (res["status"], res["download_id"]) == ("exists", 8)


def test_audio_item_edit_and_restore(api):
    calls, _ = api
    assert m.audio_item(4, "edit", title="Новий")["ok"] is True
    assert calls[0][:2] == ("PATCH", "/api/audio/downloads/4") and calls[0][2]["body"] == {"title": "Новий"}
    m.audio_item(4, "restore")
    assert calls[1][:2] == ("POST", "/api/audio/downloads/4/restore")
    assert m.audio_item(4, "delete")["error_code"] == "INVALID_ACTION"
    assert m.audio_item(4, "edit")["error_code"] == "INVALID_ARGUMENT"


def test_list_audio_library_caps_per_page_and_marks_provenance(api):
    calls, replies = api
    replies["/api/audio/downloads"] = {"downloads": [{"id": 1, "source_type": "youtube"},
                                                     {"id": 2, "source_type": "recording"}],
                                       "total": 2, "page": 1, "per_page": 50, "total_pages": 1}
    res = m.list_audio_library(per_page=500, source_type="youtube")
    assert calls[0][2]["params"]["per_page"] == 50 and calls[0][2]["params"]["source_type"] == "youtube"
    assert calls[0][2].get("write") is not True
    assert [r["provenance"] for r in res["items"]] == ["youtube_metadata", "metadata"]
    assert "truncated" not in res


def test_list_audio_library_drops_rows_over_the_budget(api):
    _, replies = api
    replies["/api/audio/downloads"] = {"downloads": [{"id": i, "title": "т" * 2000} for i in range(20)],
                                       "total": 20, "page": 1, "per_page": 20, "total_pages": 1}
    res = m.list_audio_library(max_tokens=3000)
    assert res["truncated"] is True and 0 < res["shown"] < 20


def test_get_youtube_info_is_a_read_post(api):
    calls, replies = api
    replies["/api/youtube/info"] = {"success": True, "title": "T"}
    assert m.get_youtube_info("https://youtu.be/x")["title"] == "T"
    assert calls[0][2]["body"] == {"url": "https://youtu.be/x"} and calls[0][2].get("write") is not True


# --- about -----------------------------------------------------------------------------------

def test_about_names_the_new_tools_and_caveats(tmp_path, monkeypatch):
    full = load_mcp_copy()
    db = str(tmp_path / "about.db")
    init_database(db)
    monkeypatch.setattr(full, "DB_PATH", db)
    text = full.about()
    for name in NEW_R + NEW_W + NEW_D:
        assert name in full._ABOUT_PROXY, name
        assert f"`{name}" in text, name
    for caveat in ("PAID", "FREE", "cooperative", "NOT_FINALIZED", "Windows dialog first"):
        assert caveat in text, caveat
    assert f"{len(full._registered_tool_names())} tools" in text
