"""MCP archive-organisation tools (mcp-archive-org-06): the 13 new tools, the read parameters,
the `list_recent` soft-delete filter and the registry. `_api` is faked; reads use a temp SQLite."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

import mcp_server as m
from app.db.migrations import init_database
from tests.test_mcp_registry import load_mcp_copy

NEW_W = ("task", "bookmark", "comment", "taxonomy", "speaker", "transcript")
NEW_D = ("taxonomy_delete", "taxonomy_merge", "speaker_merge", "speaker_delete",
         "transcript_delete", "enrich_backfill")
FAMILIES = {"task": "tasks", "bookmark": "transcripts", "comment": "comments", "taxonomy": "taxonomy",
            "speaker": "speakers", "transcript": "transcripts", "taxonomy_delete": "taxonomy",
            "taxonomy_merge": "taxonomy", "speaker_merge": "speakers", "speaker_delete": "speakers",
            "transcript_delete": "transcripts", "enrich_backfill": "transcripts"}


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
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "org.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


# --- registry ---------------------------------------------------------------------------------

def test_thirteen_tools_with_class_family_and_allowlist():
    full = load_mcp_copy()
    names = set(full._registered_tool_names())
    for name in NEW_W + NEW_D + ("transcript_suggest_meta",):
        assert name in names, name
    for name in NEW_W:
        assert full._TOOL_CLASS[name]["class"] == "W"
    for name in NEW_D:
        assert full._TOOL_CLASS[name]["class"] == "D"
    assert full._TOOL_CLASS["transcript_suggest_meta"]["class"] == "R"
    assert full._TOOL_CLASS["transcript_suggest_meta"]["provenance_kind"] == "transcript"
    for name, family in FAMILIES.items():
        entry = full._TOOL_CLASS[name]
        assert entry["limits"]["family"] == family
        assert entry["args_allow"], name
        assert "note" not in entry["args_allow"] and "body" not in entry["args_allow"]


def test_enrich_backfill_is_open_world_and_paid():
    entry = m._TOOL_CLASS["enrich_backfill"]
    assert entry["open_world"] is True and entry["limits"]["paid"] is True


# --- W tools ----------------------------------------------------------------------------------

def test_task_done_loops_over_ids_and_stops_on_a_refusal(api):
    calls, replies = api
    replies[("PATCH", "/api/memory/action-items/2")] = {"ok": False, "error_code": "NOT_FOUND",
                                                       "error": "no", "status_code": 404}
    out = m.task("done", item_ids=[1, 2, 3])
    assert [c[1] for c in calls] == ["/api/memory/action-items/1", "/api/memory/action-items/2"]
    assert calls[0][2]["body"] == {"status": "done"} and calls[0][2]["write"] is True
    assert out["ok"] is False and out["error_code"] == "NOT_FOUND" and len(out["results"]) == 2


def test_task_postpone_and_undo(api):
    calls, _ = api
    assert m.task("postpone", item_ids=[4], days=7)["ok"] is True
    assert calls[0][:2] == ("POST", "/api/memory/action-items/4/postpone")
    assert calls[0][2]["body"] == {"days": 7}
    m.task("undo", event_id=9)
    assert calls[1][:2] == ("POST", "/api/memory/action-events/9/undo")
    assert m.task("postpone", item_ids=[4], days=3)["error_code"] == "INVALID_ARGUMENT"
    assert len(calls) == 2


@pytest.mark.parametrize("call", [
    lambda: m.task("done", item_ids=[1, 2, 3, 4, 5, 6]),
    lambda: m.taxonomy("assign_category", ids=list(range(1, 7)), category_id=1),
    lambda: m.taxonomy("set_tags", ids=list(range(1, 7)), add=["x"]),
    lambda: m.speaker_merge(list(range(1, 7)), 9),
])
def test_more_than_five_ids_is_bulk_limit_before_any_http(api, call):
    calls, _ = api
    out = call()
    assert out["error_code"] == "BULK_LIMIT" and out["ok"] is False
    assert calls == []


def test_bookmark_actions(api):
    calls, replies = api
    replies[("DELETE", "/api/bookmarks/3")] = {"success": True, "deleted_snapshot": {"id": 3, "note": "n"}}
    m.bookmark("create", transcription_id=5, segment_index=2, note="важливо")
    m.bookmark("update", bookmark_id=3, note="x")
    out = m.bookmark("delete", bookmark_id=3)
    assert [c[:2] for c in calls] == [("POST", "/api/transcription/5/bookmarks"),
                                      ("PATCH", "/api/bookmarks/3"), ("DELETE", "/api/bookmarks/3")]
    assert out["deleted_snapshot"]["id"] == 3 and out["ok"] is True


def test_comment_actions(api):
    calls, _ = api
    m.comment("create", target_type="transcription", target_id=5, body="b", kind="note")
    m.comment("update", comment_id=7, body="b2")
    m.comment("delete", comment_id=7)
    m.comment("restore", comment_id=7)
    assert [c[:2] for c in calls] == [("POST", "/api/comments"), ("PATCH", "/api/comments/7"),
                                      ("DELETE", "/api/comments/7"), ("POST", "/api/comments/7/restore")]
    assert calls[0][2]["body"] == {"target_type": "transcription", "target_id": 5, "body": "b", "kind": "note"}
    assert m.comment("update", comment_id=7)["error_code"] == "INVALID_ARGUMENT"


def test_taxonomy_duplicate_maps_to_existing_id(api):
    _, replies = api
    replies[("POST", "/api/memory/categories")] = {
        "ok": False, "status_code": 409, "code": "duplicate", "error": "dup",
        "conflict": {"id": 12, "path": "Маркетинг"}}
    out = m.taxonomy("create_category", name="Маркетинг")
    assert out == {"ok": True, "created": False, "existing_id": 12, "existing_path": "Маркетинг"}


def test_taxonomy_assign_and_set_tags(api):
    calls, _ = api
    m.taxonomy("assign_category", ids=[1, 2], category_id=4)
    assert calls[0][:2] == ("POST", "/api/memory/transcriptions/bulk-category")
    assert calls[0][2]["body"] == {"ids": [1, 2], "category_id": 4}
    m.taxonomy("set_tags", ids=[1, 2], add=["ai"])
    assert [c[:2] for c in calls[1:]] == [("PUT", "/api/memory/1/tags"), ("PUT", "/api/memory/2/tags")]
    assert calls[1][2]["body"] == {"add": ["ai"]}


def test_speaker_actions(api):
    calls, replies = api
    replies[("POST", "/api/speakers")] = {"ok": False, "status_code": 409, "error": "exists",
                                          "existing_speaker": {"id": 8}}
    assert m.speaker("create", name="Андрій") == {"ok": True, "created": False, "existing_id": 8}
    m.speaker("rename", speaker_id=8, name="Андрій К")
    m.speaker("map_transcript", transcription_id=5, mapping={"SPEAKER_00": 8})
    m.speaker("unmerge", merge_id=2)
    assert [c[:2] for c in calls[1:]] == [("PUT", "/api/speakers/8"),
                                          ("PATCH", "/api/transcriptions/5/speakers"),
                                          ("POST", "/api/speakers/unmerge/2")]
    assert calls[2][2]["body"] == {"mapping": {"SPEAKER_00": 8}}


def test_transcript_actions(api):
    calls, _ = api
    m.transcript("edit_meta", 5, title="Нова назва")
    m.transcript("restore", 5)
    assert calls[0][:2] == ("PATCH", "/api/history/5") and calls[0][2]["body"] == {"title": "Нова назва"}
    assert calls[1][:2] == ("POST", "/api/history/5/restore")
    assert m.transcript("edit_meta", 5)["error_code"] == "INVALID_ARGUMENT"


def test_suggest_meta_is_a_read(api):
    calls, _ = api
    m.transcript_suggest_meta(5)
    assert calls[0][:2] == ("POST", "/api/history/5/suggest-meta")
    assert not calls[0][2].get("write")


# --- D tools ----------------------------------------------------------------------------------

@pytest.fixture()
def gated(monkeypatch):
    seen: list = []

    def fake(tool, method, path, body=None, timeout=90.0):
        seen.append((tool, method, path, body, timeout))
        return {"success": True}

    monkeypatch.setattr(m, "_gated", fake)
    return seen


def test_d_tools_use_the_gate_with_a_60_second_timeout(gated):
    m.taxonomy_delete("category", 3)
    m.taxonomy_delete("tag", 4)
    m.taxonomy_merge("category", 3, 5)
    m.taxonomy_merge("tag", 4, 6)
    m.speaker_merge([2, 3], 1)
    m.speaker_delete(2)
    m.transcript_delete(9)
    assert [g[1:3] for g in gated] == [
        ("DELETE", "/api/memory/categories/3"), ("DELETE", "/api/memory/tags/4"),
        ("POST", "/api/memory/categories/3/merge"), ("POST", "/api/memory/tags/merge"),
        ("POST", "/api/speakers/merge"), ("DELETE", "/api/speakers/2"), ("DELETE", "/api/history/9")]
    assert {g[4] for g in gated} == {60.0}
    assert gated[3][3] == {"keep_id": 6, "merge_id": 4}


def test_enrich_backfill_never_sends_a_model(gated):
    m.enrich_backfill(limit=20, force=True)
    assert gated[0][3] == {"limit": 20, "force": True}
    assert "model" not in gated[0][3]
    assert m.enrich_backfill(limit=201)["error_code"] == "INVALID_ARGUMENT"
    assert len(gated) == 1


# --- server refusals pass through ---------------------------------------------------------------

@pytest.mark.parametrize("code,status", [
    ("NOT_MCP_EVENT", 403), ("UNDO_NOT_LAST", 409), ("OWNER_AUTHORED", 403), ("USE_UI", 403),
    ("SPEAKER_ID_REQUIRED", 400), ("IS_SELF", 403), ("JOB_ACTIVE", 409),
    ("PRIVATE_CATEGORY_CHANGE", 403), ("BULK_LIMIT", 400), ("EVICTION_NOT_APPROVED", 409)])
def test_server_refusals_keep_their_code(code, status):
    resp = httpx.Response(status, json={"success": False, "error": "refused", "error_code": code})
    out = m._handle_resp(resp)
    assert out["ok"] is False and out["error_code"] == code and out["status_code"] == status


def test_retranscribe_waits_up_to_60_seconds_and_says_why(api):
    calls, _ = api
    m.retranscribe(5, "medium")
    assert calls[0][2]["timeout"] == 60.0
    assert "dialog" in m.retranscribe.__doc__


# --- reads ------------------------------------------------------------------------------------

def _insert_record(path: str, name: str, deleted=False) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, deleted_at) "
        "VALUES ('file', ?, 'текст', ?)", (name, 1.0 if deleted else None))
    conn.commit()
    conn.close()
    return cur.lastrowid


def test_list_recent_hides_soft_deleted_by_default(db):
    live = _insert_record(db, "Живий дзвінок")
    gone = _insert_record(db, "Видалений дзвінок", deleted=True)
    default = [i["id"] for i in m.list_recent()["items"]]
    assert live in default and gone not in default
    only = m.list_recent(deleted=True)["items"]
    assert [i["id"] for i in only] == [gone] and only[0]["deleted_at"] == 1.0


def test_search_history_passes_deleted(api):
    calls, replies = api
    replies["/api/history"] = {"transcriptions": []}
    m.search_history()
    m.search_history(deleted=True)
    assert "deleted" not in calls[0][2]["params"] and calls[1][2]["params"]["deleted"] == 1


def test_list_categories_include_tags(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO tags (name, name_norm) VALUES ('ШІ', 'ші')")
    conn.commit()
    conn.close()
    assert isinstance(m.list_categories(), list)
    out = m.list_categories(include_tags=True)
    assert isinstance(out["categories"], list) and [t["name"] for t in out["tags"]] == ["ШІ"]


def test_list_comments_source_confirmed_and_derived(db):
    tid = _insert_record(db, "Дзвінок")
    conn = sqlite3.connect(db)
    cid = conn.execute("INSERT INTO comments (target_type, target_id, body, source, confirmed_at) "
                       "VALUES ('transcription', ?, 'уточнення', 'mcp', '2026-10-20 10:00:00')",
                       (tid,)).lastrowid
    conn.execute("INSERT INTO action_items (transcription_id, task, comment_id, status) "
                 "VALUES (?, 'Надіслати кошторис', ?, 'open')", (tid, cid))
    conn.commit()
    conn.close()
    plain = m.list_comments()[0]
    assert plain["source"] == "mcp" and plain["confirmed_at"] == "2026-10-20 10:00:00"
    assert "derived" not in plain
    derived = m.list_comments(include_derived=True)[0]["derived"]["action_items"]
    assert [a["task"] for a in derived] == ["Надіслати кошторис"]


def test_get_transcript_include_ai_stays_inside_the_budget(db):
    tid = _insert_record(db, "Дзвінок з перекладом")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET topics_json = ?, translations_json = ? WHERE id = ?",
                 (json.dumps(["бюджет"], ensure_ascii=False),
                  json.dumps({"en": "x" * 60000}), tid))
    conn.commit()
    conn.close()
    assert "topics" not in m.get_transcript(tid)
    full = m.get_transcript(tid, include_ai=True, max_tokens=0)
    assert full["topics"] == ["бюджет"] and "translations" in full
    small = m.get_transcript(tid, include_ai=True, max_tokens=2000)
    assert small["topics"] == ["бюджет"]
    assert "translations" not in small and small["budget"]["ai_omitted"] == ["translations"]
    assert small["budget"]["approx_tokens_used"] <= 2000


def test_list_speakers_samples_for(api):
    calls, replies = api
    replies["/api/speakers"] = {"speakers": [{"id": 8}], "total": 1}
    replies["/api/speakers/8/samples"] = {"success": True, "samples": [{"text": "привіт"}]}
    out = m.list_speakers(samples_for=[8])
    assert out["samples"] == {"8": [{"text": "привіт"}]}
    assert m.list_speakers(samples_for=list(range(1, 7)))["error_code"] == "BULK_LIMIT"


def test_list_action_items_carries_last_event(db):
    tid = _insert_record(db, "Дзвінок із задачею")
    conn = sqlite3.connect(db)
    aid = conn.execute("INSERT INTO action_items (transcription_id, task, status) VALUES (?, 'Задача', 'open')",
                       (tid,)).lastrowid
    conn.execute("INSERT INTO action_item_events (item_id, at, source, action) VALUES (?, 't1', 'web', 'done')",
                 (aid,))
    ev = conn.execute("INSERT INTO action_item_events (item_id, at, source, action) VALUES (?, 't2', 'mcp', 'open')",
                      (aid,)).lastrowid
    conn.commit()
    conn.close()
    items = [{"id": aid}, {"id": aid + 1000}]
    m._attach_last_events(items)
    assert items[0]["last_event"] == {"event_id": ev, "action": "open", "source": "mcp", "at": "t2"}
    assert items[1]["last_event"] is None


# --- about ------------------------------------------------------------------------------------

def test_about_names_the_new_tools_and_the_caveats(db):
    full = load_mcp_copy()
    full.DB_PATH = db
    text = full.about()
    for name in NEW_W + NEW_D:
        assert f"`{name}`" in text, name
    assert "HARD" in text and "deleted_snapshot" in text
    assert "no search boost until the owner" in text
