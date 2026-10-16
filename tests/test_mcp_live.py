"""Three MCP tools for a live call (story mcp-live-call-03, contract C4).

`get_live_transcript`/`ask_live` are thin HTTP proxies to C1/C2 (app/blueprints/
recording.py, app/blueprints/copilot.py) — here only the shape of the
`_api` call is checked (mocked, without network and without app.py). `get_live_copilot` is
direct-DB: the full stream of `copilot_events`, filters nothing. It carries two
fields of the show status: the raw `shown` (`payload.shown` at persist time — it lies in
verified_only mode for `insight_local`, story 07) and the derived
`operator_saw` (the truth, built from the `ref_event_id`/`verdict` chain).

Offline: a temporary SQLite via `init_database`, no network and no heavy
models (mcp_server lazily loads torch only in search_archive).
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from app.db.migrations import init_database

import mcp_server as m


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "live.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def _add_copilot_session(conn, *, recording_session_id, status="active"):
    cur = conn.execute(
        "INSERT INTO copilot_sessions (recording_session_id, status) VALUES (?, ?)",
        (recording_session_id, status),
    )
    return cur.lastrowid


def _add_event(conn, copilot_session_id, *, kind, event_id_hint=None,
                payload=None, source=None, confidence=None,
                operator_action=None, ts_offset_sec=None):
    cur = conn.execute(
        "INSERT INTO copilot_events (copilot_session_id, kind, topic_id, source, "
        "confidence, payload_json, tokens_in, tokens_out, operator_action, ts_offset_sec) "
        "VALUES (?, ?, NULL, ?, ?, ?, NULL, NULL, ?, ?)",
        (copilot_session_id, kind, source, confidence,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None,
         operator_action, ts_offset_sec),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------
# Registration and about()
# --------------------------------------------------------------------------

def test_three_new_tools_registered():
    names = m._registered_tool_names()
    assert {"get_live_transcript", "get_live_copilot", "ask_live"} <= set(names)


def test_get_live_copilot_is_direct_db():
    assert "get_live_copilot" in m._ABOUT_DIRECT_DB
    assert "get_live_copilot" not in m._ABOUT_PROXY


def test_get_live_transcript_and_ask_live_are_proxy():
    assert {"get_live_transcript", "ask_live"} <= set(m._ABOUT_PROXY)
    assert not ({"get_live_transcript", "ask_live"} & set(m._ABOUT_DIRECT_DB))


def test_about_bucket_invariant_holds():
    registered = set(m._registered_tool_names())
    bucketed = set(m._ABOUT_DIRECT_DB) | set(m._ABOUT_PROXY)
    assert registered == bucketed
    assert len(m._ABOUT_DIRECT_DB) + len(m._ABOUT_PROXY) == len(m._registered_tool_names())


# --------------------------------------------------------------------------
# get_live_copilot — direct-DB, the full stream, without app.py
# --------------------------------------------------------------------------

def test_get_live_copilot_returns_all_kinds_and_derives_operator_saw(db):
    """All 9 kinds from the schema (app/db/migrations.py v19) are present and
    handled — including both forms of `insight_verified` — and `operator_saw`
    corrects the lie of `shown` for a card shown later via the
    `ref_event_id`/`verdict` chain."""
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_123")
        _add_event(conn, cs_id, kind="topic_shift", source="local")
        _add_event(conn, cs_id, kind="topic_return", source="local")
        _add_event(conn, cs_id, kind="retrieval", source="local")
        shown_id = _add_event(conn, cs_id, kind="insight_local", source="local", confidence=0.7,
                              payload={"kind": "fact", "text": "показано одразу", "shown": True})
        suppressed_id = _add_event(conn, cs_id, kind="insight_local", source="local", confidence=0.4,
                                   payload={"kind": "fact", "text": "притишено назавжди", "shown": False})
        verified_later_id = _add_event(conn, cs_id, kind="insight_local", source="local", confidence=0.8,
                                       payload={"kind": "fact", "text": "чернетка", "shown": False})
        # Escalation verdict (verified_only): refers to verified_later_id,
        # verdict=real, WITHOUT the 'shown' key — not a card by itself.
        _add_event(conn, cs_id, kind="insight_verified", source="api", confidence=0.9,
                    payload={"ref_event_id": verified_later_id, "verdict": "real",
                             "text": "верифікований текст", "model": "sonnet"})
        # safety-sweep: another shape of the same kind, with its own 'shown', without ref_event_id.
        _add_event(conn, cs_id, kind="insight_verified", source="api", confidence=0.6,
                    payload={"kind": "fact", "text": "sweep-картка", "shown": True})
        _add_event(conn, cs_id, kind="escalation", source="local")
        _add_event(conn, cs_id, kind="operator_action", operator_action="dismiss")
        _add_event(conn, cs_id, kind="usage")
        _add_event(conn, cs_id, kind="safety_sweep", source="api")
        conn.commit()

    out = m.get_live_copilot(session_id="rec_123")
    assert out["available"] is True
    assert out["copilot_session_id"] == cs_id
    assert out["session_id"] == "rec_123"
    assert out["count"] == 12

    by_kind: dict = {}
    for e in out["events"]:
        by_kind.setdefault(e["kind"], []).append(e)
    assert set(by_kind) == m._COPILOT_EVENT_KINDS

    ev = next(e for e in by_kind["insight_local"] if e["id"] == shown_id)
    assert ev["shown"] is True and ev["operator_saw"] is True

    ev = next(e for e in by_kind["insight_local"] if e["id"] == suppressed_id)
    assert ev["shown"] is False and ev["operator_saw"] is False

    # payload.shown=False, but verified_only showed it later after all — shown
    # stays raw (False), operator_saw corrects the lie to True.
    ev = next(e for e in by_kind["insight_local"] if e["id"] == verified_later_id)
    assert ev["shown"] is False
    assert ev["operator_saw"] is True

    verdict_records = [e for e in by_kind["insight_verified"] if "ref_event_id" in e["payload"]]
    assert len(verdict_records) == 1
    # No 'shown' in the payload → not passed off as muted (not False, but None).
    assert verdict_records[0]["shown"] is None
    assert verdict_records[0]["operator_saw"] is None

    sweep_cards = [e for e in by_kind["insight_verified"] if "ref_event_id" not in e["payload"]]
    assert len(sweep_cards) == 1
    assert sweep_cards[0]["shown"] is True
    assert sweep_cards[0]["operator_saw"] is True

    # Kinds without a card carry None in both fields, not an invented True/False.
    for kind in ("topic_shift", "topic_return", "retrieval", "escalation",
                "operator_action", "usage", "safety_sweep"):
        for e in by_kind[kind]:
            assert e["shown"] is None
            assert e["operator_saw"] is None

    for e in out["events"]:
        for field in ("kind", "source", "confidence", "operator_action",
                      "ts_offset_sec", "payload", "shown", "operator_saw"):
            assert field in e


def test_get_live_copilot_unknown_kind_is_explicit_not_silent(db):
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_unk")
        _add_event(conn, cs_id, kind="usage")
        conn.commit()

    out = m.get_live_copilot(session_id="rec_unk", kinds="usage,not_a_real_kind")
    assert out["success"] is False
    assert "not_a_real_kind" in out["error"]
    assert "usage" in out["known_kinds"]


def test_get_live_copilot_since_event_id_and_truncation(db):
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_trunc")
        for i in range(m._LIVE_COPILOT_LIMIT + 5):
            _add_event(conn, cs_id, kind="usage")
        conn.commit()

    out = m.get_live_copilot(session_id="rec_trunc")
    assert out["truncated"] is True
    assert out["count"] == m._LIVE_COPILOT_LIMIT

    with get_db_connection(db) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM copilot_events WHERE copilot_session_id = ? ORDER BY id", (cs_id,)
        ).fetchall()]
    # 6 events skipped via since_event_id → the remainder (LIMIT - 1) is smaller than
    # the ceiling, i.e. NOT truncated.
    cursor_id = ids[5]

    since_first = m.get_live_copilot(session_id="rec_trunc", since_event_id=cursor_id)
    assert since_first["truncated"] is False
    assert since_first["count"] == m._LIVE_COPILOT_LIMIT - 1
    assert all(e["id"] > cursor_id for e in since_first["events"])


def test_get_live_copilot_kinds_filter(db):
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_kf")
        _add_event(conn, cs_id, kind="topic_shift")
        _add_event(conn, cs_id, kind="insight_local", payload={"shown": True})
        _add_event(conn, cs_id, kind="escalation")
        conn.commit()

    out = m.get_live_copilot(session_id="rec_kf", kinds="insight_local,escalation")
    assert out["count"] == 2
    assert {e["kind"] for e in out["events"]} == {"insight_local", "escalation"}


def test_get_live_copilot_session_id_none_resolves_via_active_recording(db, monkeypatch):
    """`session_id=None` resolves via the same source as
    `get_live_transcript` (`/api/recordings/active`) — and not simply "the newest
    active session in the DB" (the one hung from a crashed process does not take part here)."""
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        _add_copilot_session(conn, recording_session_id="rec_stale_crashed", status="active")
        cs_id = _add_copilot_session(conn, recording_session_id="rec_new", status="active")
        _add_event(conn, cs_id, kind="usage")
        conn.commit()

    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append(path)
        assert path == "/api/recordings/active"
        return {"success": True, "active": True, "session_id": "rec_new"}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_copilot()
    assert calls == ["/api/recordings/active"]
    assert out["available"] is True
    assert out["session_id"] == "rec_new"
    assert out["copilot_session_id"] == cs_id
    assert "session_resolution" not in out


def test_get_live_copilot_app_py_no_active_ignores_stale_db_session(db, monkeypatch):
    """app.py says "there is no active recording" — a hung `status='active'`
    copilot session of a crashed process does NOT replace this answer."""
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        _add_copilot_session(conn, recording_session_id="rec_crashed", status="active")
        conn.commit()

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        return {"success": True, "active": False, "session_id": None}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_copilot()
    assert out["available"] is False
    assert out["session_id"] is None
    assert out["reason"] == "no active recording session"


def test_get_live_copilot_no_session_found_degrades(db):
    out = m.get_live_copilot(session_id="rec_never_started")
    assert out["success"] is True
    assert out["available"] is False
    assert out["reason"]
    assert out["events"] == []


def test_get_live_copilot_falls_back_to_db_when_app_py_down(db, monkeypatch):
    """app.py is unreachable — the tool stays alive (direct-DB fallback), and says
    so honestly via `session_resolution`. What is checked is the property added by
    the tool itself (fallback resolution + message), not an echo of the fake `_api`
    (the fake does not return a session_id or events at all)."""
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_fallback", status="active")
        _add_event(conn, cs_id, kind="usage")
        conn.commit()

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        return {"error": "Recall (app.py) is not running at http://127.0.0.1:5050."}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_copilot()
    assert out["available"] is True
    assert out["session_id"] == "rec_fallback"
    assert out["copilot_session_id"] == cs_id
    assert "app.py" in out["session_resolution"]


def test_get_live_copilot_no_active_session_at_all(db, monkeypatch):
    monkeypatch.setattr(m, "_api", lambda *a, **kw:
                        {"error": "Recall (app.py) is not running at http://127.0.0.1:5050."})
    out = m.get_live_copilot()
    assert out["available"] is False
    assert out["events"] == []


def test_get_live_copilot_works_without_app_py(db, monkeypatch):
    """An explicit `session_id` — no HTTP call at all."""
    def _boom(*a, **kw):
        raise AssertionError("get_live_copilot must not go over the network")

    monkeypatch.setattr(m, "_api", _boom)
    out = m.get_live_copilot(session_id="rec_offline")
    assert out["available"] is False


# --------------------------------------------------------------------------
# get_live_transcript — proxy to C1
# --------------------------------------------------------------------------

def test_get_live_transcript_proxies_with_explicit_session(monkeypatch):
    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((method, path, params))
        return {"success": True, "session_id": "rec_1", "available": True,
                "segments": [], "count": 0}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_transcript(session_id="rec_1")
    assert calls == [("GET", "/api/recording/rec_1/live-transcript", None)]
    assert out["available"] is True


def test_get_live_transcript_has_no_lossy_time_param():
    """Story 10: `since_sec` loses segments of the slower track when
    polling by time — it must not exist on the MCP surface at all, only the
    `since_seq` cursor."""
    params = set(inspect.signature(m.get_live_transcript).parameters)
    assert "since_sec" not in params
    assert "since_seq" in params


def test_get_live_transcript_proxies_since_seq_and_returns_next_seq_verbatim(monkeypatch):
    """The cursor must be passed into the request parameters, and `next_seq` must be returned
    as exactly the value the endpoint gave, without recomputation by the tool (Story 09)."""
    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((method, path, params))
        # next_seq deliberately does NOT equal max(seq) of the returned segments — this way the test
        # catches the tool recomputing the cursor (forbidden by the story's Non-goals),
        # not just an echo of the maximum from segments.
        return {"success": True, "session_id": "rec_1", "available": True,
                "segments": [{"seq": 41, "start": 1.0, "text": "..."}],
                "count": 1, "next_seq": 17, "truncated": True}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_transcript(session_id="rec_1", since_seq=17)
    assert calls == [("GET", "/api/recording/rec_1/live-transcript", {"since_seq": 17})]
    assert out["next_seq"] == 17


def test_get_live_transcript_session_none_resolves_active(monkeypatch):
    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((method, path, params))
        if path == "/api/recordings/active":
            return {"success": True, "active": True, "session_id": "rec_active"}
        return {"success": True, "session_id": "rec_active", "available": True,
                "segments": [], "count": 0}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_transcript()
    assert calls[0] == ("GET", "/api/recordings/active", None)
    assert calls[1][1] == "/api/recording/rec_active/live-transcript"
    assert out["available"] is True


def test_get_live_transcript_no_active_session_degrades_gracefully(monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        return {"success": True, "active": False, "session_id": None}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_transcript()
    assert out["available"] is False
    assert out["reason"]
    assert out["segments"] == []


def test_get_live_transcript_app_down_returns_error_not_raise(monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        return {"error": "Recall (app.py) is not running at http://127.0.0.1:5050."}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_transcript()
    assert out["available"] is False
    assert "is not running" in out["reason"]


# --------------------------------------------------------------------------
# ask_live — proxy to C2
# --------------------------------------------------------------------------

def test_ask_live_proxies_body(monkeypatch):
    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((method, path, body))
        return {"success": True, "answer": "42", "available": True}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.ask_live("скільки коштує?", scope="archive", session_id="rec_9")
    assert calls == [("POST", "/api/copilot/live-ask",
                       {"question": "скільки коштує?", "session_id": "rec_9", "scope": "archive"})]
    assert out["answer"] == "42"


def test_ask_live_default_scope_and_session():
    import inspect
    sig = inspect.signature(m.ask_live)
    assert sig.parameters["scope"].default == "both"
    assert sig.parameters["session_id"].default is None


def test_ask_live_app_down_returns_error_not_raise(monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        return {"error": "Recall (app.py) is not running at http://127.0.0.1:5050."}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.ask_live("питання")
    assert "error" in out


# --------------------------------------------------------------------------
# get_active_recording — added fields (copilot_session_id/copilot_active/
# live_transcript_available)
# --------------------------------------------------------------------------

def test_get_active_recording_adds_copilot_fields(db, monkeypatch):
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_live", status="active")
        conn.commit()

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        if path == "/api/recordings/active":
            return {"success": True, "active": True, "session_id": "rec_live"}
        if path == "/api/recording/rec_live/live-transcript":
            return {"success": True, "session_id": "rec_live", "available": True,
                    "segments": [], "count": 0}
        raise AssertionError(f"unexpected call {path}")

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_active_recording()
    assert out["copilot_session_id"] == cs_id
    assert out["copilot_active"] is True
    assert out["live_transcript_available"] is True


def test_get_active_recording_no_active_session_passthrough(monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        return {"success": True, "active": False, "session_id": None}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_active_recording()
    assert out == {"success": True, "active": False, "session_id": None,
                   "count": 0, "sessions": []}


def test_get_active_recording_no_copilot_session_defaults(db, monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        if path == "/api/recordings/active":
            return {"success": True, "active": True, "session_id": "rec_no_copilot"}
        return {"success": True, "session_id": "rec_no_copilot", "available": False,
                "reason": "the live preview for this session is unavailable", "segments": [], "count": 0}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_active_recording()
    assert out["copilot_session_id"] is None
    assert out["copilot_active"] is False
    assert out["live_transcript_available"] is False


def test_get_active_recording_db_locked_degrades_instead_of_raising(db, monkeypatch):
    """The `copilot_sessions` lookup fails (DB locked longer than busy_timeout) —
    the tool still returns the answer about the active recording instead of raising."""
    import sqlite3 as _sqlite3

    class _BoomConn:
        def __enter__(self):
            raise _sqlite3.OperationalError("database is locked")

        def __exit__(self, *a):
            return False

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        if path == "/api/recordings/active":
            return {"success": True, "active": True, "session_id": "rec_locked"}
        if path == "/api/recording/rec_locked/live-transcript":
            return {"success": True, "session_id": "rec_locked", "available": False,
                    "reason": "n/a", "segments": [], "count": 0}
        raise AssertionError(f"unexpected call {path}")

    monkeypatch.setattr(m, "_api", fake_api)
    monkeypatch.setattr(m, "get_db_connection", lambda *a, **kw: _BoomConn())
    out = m.get_active_recording()
    assert out["active"] is True
    assert out["session_id"] == "rec_locked"
    assert out["copilot_session_id"] is None
    assert out["copilot_active"] is False


# --------------------------------------------------------------------------
# Ambiguity rule (C7, ADR-026 D4): several recordings — we do not guess
# --------------------------------------------------------------------------

_TWO = {"success": True, "active": True, "count": 2, "session_id": None, "state": None,
        "sessions": [
            {"session_id": "rec_Андрій", "state": "recording", "name": "Дзвінок з Андрієм",
             "auto_name": None, "elapsed_seconds": 12.0, "streams": ["mic"],
             "system_source": None, "copilot_active": True},
            {"session_id": "rec_Олена", "state": "recording", "name": None,
             "auto_name": "Zoom", "elapsed_seconds": 3.0, "streams": ["system"],
             "system_source": "zoom.exe", "copilot_active": False},
        ]}


def _only_active(payload, calls):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append(path)
        if path == "/api/recordings/active":
            return json.loads(json.dumps(payload))
        raise AssertionError(f"second call {path}")
    return fake_api


def _assert_ambiguous(out):
    assert out["success"] is False
    assert out["error_code"] == "AMBIGUOUS_SESSION"
    assert [s["session_id"] for s in out["sessions"]] == ["rec_Андрій", "rec_Олена"]
    assert out["hint"] == "pass session_id"


def test_ambiguous_live_transcript_makes_no_second_call(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", _only_active(_TWO, calls))
    _assert_ambiguous(m.get_live_transcript())
    assert calls == ["/api/recordings/active"]


def test_ambiguous_live_copilot_app_up(db, monkeypatch):
    from app.db.connection import get_db_connection
    with get_db_connection(db) as conn:
        _add_copilot_session(conn, recording_session_id="rec_Андрій", status="active")
        conn.commit()
    calls = []
    monkeypatch.setattr(m, "_api", _only_active(_TWO, calls))
    _assert_ambiguous(m.get_live_copilot())
    assert calls == ["/api/recordings/active"]


def test_ambiguous_ask_live_does_not_ask(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", _only_active(_TWO, calls))
    _assert_ambiguous(m.ask_live("що сказав Андрій?"))
    assert calls == ["/api/recordings/active"]


def test_explicit_session_id_calls_through_despite_many(db, monkeypatch):
    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((path, body))
        if path == "/api/recordings/active":
            raise AssertionError("explicit session_id must not resolve")
        return {"success": True, "available": True, "segments": [], "answer": "ok"}

    monkeypatch.setattr(m, "_api", fake_api)
    assert m.get_live_transcript(session_id="rec_Олена")["available"] is True
    assert m.ask_live("питання", session_id="rec_Олена")["answer"] == "ok"
    assert m.get_live_copilot(session_id="rec_Олена")["available"] is False  # direct-DB
    assert calls == [("/api/recording/rec_Олена/live-transcript", None),
                     ("/api/copilot/live-ask",
                      {"question": "питання", "session_id": "rec_Олена", "scope": "both"})]


@pytest.mark.parametrize("active", [
    {"success": True, "active": True, "count": 1, "session_id": "rec_1", "state": "recording",
     "sessions": [{"session_id": "rec_1"}]},
    {"success": True, "session_id": "rec_1"},
])
def test_single_session_resolves_both_shapes(active, monkeypatch):
    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((path, body))
        if path == "/api/recordings/active":
            return dict(active)
        return {"success": True, "available": True, "segments": [], "answer": "a"}

    monkeypatch.setattr(m, "_api", fake_api)
    assert m.get_live_transcript()["available"] is True
    assert calls[1][0] == "/api/recording/rec_1/live-transcript"
    calls.clear()
    m.ask_live("q")
    # the locally resolved session is passed explicitly
    assert calls[1] == ("/api/copilot/live-ask",
                        {"question": "q", "session_id": "rec_1", "scope": "both"})


@pytest.mark.parametrize("active", [
    {"success": True, "active": False, "count": 0, "sessions": [], "session_id": None,
     "state": None},
    {"success": True, "session_id": None},
])
def test_zero_sessions_is_todays_unavailable(active, db, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", _only_active(active, calls))
    out = m.get_live_transcript()
    assert out["available"] is False and out["reason"] == "no active recording session"
    out = m.get_live_copilot()
    assert out["available"] is False and out["reason"] == "no active recording session"


def test_ask_live_passes_server_ambiguity_through(monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        if path == "/api/recordings/active":
            return {"error": "HTTP 500"}
        return {"success": False, "error_code": "AMBIGUOUS_SESSION", "error": "HTTP 409",
                "status_code": 409, "sessions": _TWO["sessions"], "hint": "pass session_id"}

    monkeypatch.setattr(m, "_api", fake_api)
    _assert_ambiguous(m.ask_live("q"))


def test_get_active_recording_two_sessions_lists(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", _only_active(_TWO, calls))
    out = m.get_active_recording()
    assert out["count"] == 2
    assert len(out["sessions"]) == 2
    assert out["session_id"] is None
    assert calls == ["/api/recordings/active"]


def test_get_active_recording_one_session_adds_count(db, monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        if path == "/api/recordings/active":
            return {"success": True, "active": True, "count": 1, "session_id": "rec_1",
                    "state": "recording", "sessions": [{"session_id": "rec_1"}]}
        return {"success": True, "available": True, "segments": []}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_active_recording()
    assert out["count"] == 1
    assert out["session_id"] == "rec_1"
    assert out["copilot_session_id"] is None
    assert out["live_transcript_available"] is True


def _app_down(*a, **kw):
    return {"error": "Recall (app.py) is not running at http://127.0.0.1:5050."}


def test_live_copilot_db_fallback_two_active_is_ambiguous(db, monkeypatch):
    from app.db.connection import get_db_connection
    with get_db_connection(db) as conn:
        _add_copilot_session(conn, recording_session_id="rec_Андрій", status="active")
        _add_copilot_session(conn, recording_session_id="rec_Олена", status="active")
        conn.commit()
    monkeypatch.setattr(m, "_api", _app_down)
    out = m.get_live_copilot()
    assert out["error_code"] == "AMBIGUOUS_SESSION"
    assert sorted(s["session_id"] for s in out["sessions"]) == ["rec_Андрій", "rec_Олена"]


def test_live_copilot_db_fallback_one_active_is_todays_answer(db, monkeypatch):
    from app.db.connection import get_db_connection
    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_Андрій", status="active")
        _add_copilot_session(conn, recording_session_id="rec_old", status="ended")
        conn.commit()
    monkeypatch.setattr(m, "_api", _app_down)
    out = m.get_live_copilot()
    assert out["available"] is True
    assert out["session_id"] == "rec_Андрій"
    assert out["copilot_session_id"] == cs_id
    assert "app.py" in out["session_resolution"]
