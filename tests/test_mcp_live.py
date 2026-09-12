"""Три MCP-тулзи до живого дзвінка (історія mcp-live-call-03, контракт C4).

`get_live_transcript`/`ask_live` — тонкі HTTP-проксі на C1/C2 (app/blueprints/
recording.py, app/blueprints/copilot.py) — тут перевіряється лише форма
виклику `_api` (замокана, без мережі й без app.py). `get_live_copilot` —
direct-DB: повний потік `copilot_events`, нічого не фільтрує. Несе два поля
статусу показу: сире `shown` (`payload.shown` на момент персисту — бреше в
режимі verified_only для `insight_local`, історія 07) і виведений
`operator_saw` (правда, побудована по ланцюжку `ref_event_id`/`verdict`).

Офлайн: тимчасова SQLite через `init_database`, без мережі й без важких
моделей (mcp_server ліниво вантажить torch лише в search_archive).
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
# Реєстрація й about()
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
# get_live_copilot — direct-DB, повний потік, без app.py
# --------------------------------------------------------------------------

def test_get_live_copilot_returns_all_kinds_and_derives_operator_saw(db):
    """Усі 9 видів зі схеми (app/db/migrations.py v19) представлені й
    оброблені — включно з обома формами `insight_verified` — і `operator_saw`
    виправляє брехню `shown` для картки, показаної пізніше через ланцюжок
    `ref_event_id`/`verdict`."""
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
        # Вердикт ескалації (verified_only): посилається на verified_later_id,
        # verdict=real, БЕЗ ключа 'shown' — сам по собі не картка.
        _add_event(conn, cs_id, kind="insight_verified", source="api", confidence=0.9,
                    payload={"ref_event_id": verified_later_id, "verdict": "real",
                             "text": "верифікований текст", "model": "sonnet"})
        # safety-sweep: інша форма того самого kind, зі своїм 'shown', без ref_event_id.
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

    # payload.shown=False, але verified_only таки показав пізніше — shown
    # лишається сирим (False), operator_saw виправляє брехню на True.
    ev = next(e for e in by_kind["insight_local"] if e["id"] == verified_later_id)
    assert ev["shown"] is False
    assert ev["operator_saw"] is True

    verdict_records = [e for e in by_kind["insight_verified"] if "ref_event_id" in e["payload"]]
    assert len(verdict_records) == 1
    # Немає 'shown' у payload → не видається за притишене (не False, а None).
    assert verdict_records[0]["shown"] is None
    assert verdict_records[0]["operator_saw"] is None

    sweep_cards = [e for e in by_kind["insight_verified"] if "ref_event_id" not in e["payload"]]
    assert len(sweep_cards) == 1
    assert sweep_cards[0]["shown"] is True
    assert sweep_cards[0]["operator_saw"] is True

    # Види без картки несуть None в обох полях, а не вигаданий True/False.
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
    # 6 подій пропущено через since_event_id → залишок (LIMIT - 1) менший за
    # потолок, тобто НЕ обрізається.
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
    """`session_id=None` розв'язується через те саме джерело, що й
    `get_live_transcript` (`/api/recordings/active`) — а не просто «найновіша
    active-сесія в БД» (та, зависла від впалого процесу, тут не бере участі)."""
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
    """app.py каже «немає активного запису» — зависла `status='active'`
    copilot-сесія впалого процесу НЕ підміняє цю відповідь."""
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
    assert out["reason"] == "немає активної сесії запису"


def test_get_live_copilot_no_session_found_degrades(db):
    out = m.get_live_copilot(session_id="rec_never_started")
    assert out["success"] is True
    assert out["available"] is False
    assert out["reason"]
    assert out["events"] == []


def test_get_live_copilot_falls_back_to_db_when_app_py_down(db, monkeypatch):
    """app.py недосяжний — тулза лишається живою (direct-DB фолбек), і каже
    про це чесно `session_resolution`. Перевіряється властивість, яку додає
    сама тулза (fallback-резолюція + повідомлення), а не echo фейкового `_api`
    (фейк тут узагалі не повертає ні session_id, ні подій)."""
    from app.db.connection import get_db_connection

    with get_db_connection(db) as conn:
        cs_id = _add_copilot_session(conn, recording_session_id="rec_fallback", status="active")
        _add_event(conn, cs_id, kind="usage")
        conn.commit()

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        return {"error": "Recall (app.py) не запущений на http://127.0.0.1:5050."}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_copilot()
    assert out["available"] is True
    assert out["session_id"] == "rec_fallback"
    assert out["copilot_session_id"] == cs_id
    assert "app.py" in out["session_resolution"]


def test_get_live_copilot_no_active_session_at_all(db, monkeypatch):
    monkeypatch.setattr(m, "_api", lambda *a, **kw:
                        {"error": "Recall (app.py) не запущений на http://127.0.0.1:5050."})
    out = m.get_live_copilot()
    assert out["available"] is False
    assert out["events"] == []


def test_get_live_copilot_works_without_app_py(db, monkeypatch):
    """Явний `session_id` — жодного HTTP-виклику."""
    def _boom(*a, **kw):
        raise AssertionError("get_live_copilot не має ходити по мережі")

    monkeypatch.setattr(m, "_api", _boom)
    out = m.get_live_copilot(session_id="rec_offline")
    assert out["available"] is False


# --------------------------------------------------------------------------
# get_live_transcript — прокси на C1
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
    """Story 10: `since_sec` губить сегменти повільнішої доріжки при
    опитуванні по часу — на MCP-поверхні його не має бути зовсім, лише
    `since_seq`-курсор."""
    params = set(inspect.signature(m.get_live_transcript).parameters)
    assert "since_sec" not in params
    assert "since_seq" in params


def test_get_live_transcript_proxies_since_seq_and_returns_next_seq_verbatim(monkeypatch):
    """Курсор має пробрасуватись у параметри запиту, а `next_seq` — повертатись
    саме те значення, що видав ендпоінт, без перерахунку тулзою (Story 09)."""
    calls = []

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((method, path, params))
        # next_seq свідомо НЕ дорівнює max(seq) виданих сегментів — так тест
        # ловить перерахунок курсору тулзою (заборонено Non-goals історії),
        # а не просто ехо максимуму з segments.
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
        return {"error": "Recall (app.py) не запущений на http://127.0.0.1:5050."}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_live_transcript()
    assert out["available"] is False
    assert "не запущений" in out["reason"]


# --------------------------------------------------------------------------
# ask_live — прокси на C2
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
        return {"error": "Recall (app.py) не запущений на http://127.0.0.1:5050."}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.ask_live("питання")
    assert "error" in out


# --------------------------------------------------------------------------
# get_active_recording — доповнені поля (copilot_session_id/copilot_active/
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
    assert out == {"success": True, "active": False, "session_id": None}


def test_get_active_recording_no_copilot_session_defaults(db, monkeypatch):
    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        if path == "/api/recordings/active":
            return {"success": True, "active": True, "session_id": "rec_no_copilot"}
        return {"success": True, "session_id": "rec_no_copilot", "available": False,
                "reason": "live-прев'ю для цієї сесії недоступне", "segments": [], "count": 0}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.get_active_recording()
    assert out["copilot_session_id"] is None
    assert out["copilot_active"] is False
    assert out["live_transcript_available"] is False


def test_get_active_recording_db_locked_degrades_instead_of_raising(db, monkeypatch):
    """`copilot_sessions`-лукап падає (БД заблокована довше busy_timeout) —
    тулза й далі повертає відповідь про активний запис, а не кидає виключення."""
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
