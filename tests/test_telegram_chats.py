"""Telegram chat service, routes, privacy filter and gate entries (mcp-telegram-automation story 01).

The app carries the real Telegram blueprint wired like `app.py` (auth gate -> approval gate -> audit
hooks) over a temp DB from `init_database`; the listener is a stub, no window opens (`conftest`
denies the dialog unless `approve_dialogs` is requested). Fixtures are Cyrillic and invented.
"""
from __future__ import annotations

import pytest
from flask import Flask

from app import state as app_state
from app.blueprints import telegram as telegram_bp_mod
from app.blueprints.telegram import telegram_bp
from app.core import approval_gate, audit_hooks, auth
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories as cats
from app.services import privacy, telegram_chats as tc

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "tg_chats.db")
    init_database(path)
    return path


@pytest.fixture(autouse=True)
def _privacy_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_PRIVATE_CATEGORIES", "Закрите тест")
    words = tmp_path / "private.txt"
    words.write_text("таємниця\n", encoding="utf-8")
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(words))
    approval_gate.reset_counters()
    monkeypatch.setattr(app_state, "recording_service", None, raising=False)
    monkeypatch.setattr(approval_gate, "recording_active", lambda **kw: False)
    yield
    approval_gate.reset_counters()


@pytest.fixture
def dirs(db):
    with get_db_connection(db) as conn:
        private = cats.create_category(conn, "Закрите тест")
        child = cats.create_category(conn, "Родина тест", parent_id=private["id"])
        work = cats.create_category(conn, "Робота тест")
        other = cats.create_category(conn, "Навчання тест")
        conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, enabled, category_id) VALUES "
                     "(-1001, 'Робочий чат', 1, ?), (-1002, 'Родинний чат', 1, ?), "
                     "(-1003, 'Чат таємниця', 1, ?), (-1004, 'Вимкнений чат', 0, NULL)",
                     (work["id"], child["id"], other["id"]))
        conn.commit()
    return {"private": private["id"], "child": child["id"], "work": work["id"], "other": other["id"]}


@pytest.fixture
def listener(monkeypatch):
    state = {"ok": True, "dialogs": [
        {"id": -1005, "title": "Новий чат", "type": "supergroup", "username": "novyi",
         "phone": "+380000000", "monitored": False},
        {"id": -1001, "title": "Робочий чат", "type": "supergroup", "username": None, "monitored": True},
        {"id": -1002, "title": "Родинний чат", "type": "group", "username": None, "monitored": True},
        {"id": 77, "title": "Таємниця Петра", "type": "user", "username": None, "monitored": False},
    ], "calls": []}

    def _call(path, method="GET", body=None, timeout=60):
        state["calls"].append((path, body))
        if not state["ok"]:
            return False, {"error": "listener offline"}
        if path == "/dialogs":
            return True, {"dialogs": state["dialogs"], "limit": 200}
        if path == "/status":
            return True, {"me": {"id": 1}}
        return True, {"queued": True}

    monkeypatch.setattr(telegram_bp_mod, "_listener_call", _call)
    return state


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
    a = Flask(__name__, root_path=str(tmp_path))
    a.config.update(TESTING=True, DATABASE=db)
    a.register_blueprint(telegram_bp)
    auth.register(a)
    approval_gate.register(a)
    audit_hooks.register(a)
    return a.test_client()


def _chat(db, chat_id):
    with get_db_connection(db) as conn:
        row = conn.execute("SELECT * FROM tg_monitored_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None


def _events(db):
    with get_db_connection(db) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tg_chat_events ORDER BY id")]


# ------------------------------------------------------------------ service

def test_set_category_unmonitored_raises_and_creates_nothing(db, dirs):
    with pytest.raises(tc.NotMonitored):
        tc.set_category(db, -999, dirs["work"], source="mcp")
    assert _chat(db, -999) is None
    assert _events(db) == []


def test_set_category_journals_previous_value(db, dirs):
    out = tc.set_category(db, -1001, dirs["other"], source="mcp", actor="unknown")
    assert out["previous_category_id"] == dirs["work"] and out["category_name"] == "Навчання тест"
    assert _chat(db, -1001)["category_id"] == dirs["other"]
    ev = _events(db)
    assert len(ev) == 1
    assert (ev[0]["kind"], ev[0]["old_value"], ev[0]["new_value"], ev[0]["source"]) == \
        ("category", str(dirs["work"]), str(dirs["other"]), "mcp")
    assert out["event_id"] == ev[0]["id"]
    again = tc.set_category(db, -1001, dirs["other"], source="mcp")
    assert again["event_id"] is None and len(_events(db)) == 1


def test_set_category_out_of_private_is_refused(db, dirs):
    with pytest.raises(tc.PrivateCategoryChange):
        tc.set_category(db, -1002, dirs["work"], source="mcp")
    with pytest.raises(tc.PrivateCategoryChange):
        tc.set_category(db, -1002, None, source="mcp")
    assert _chat(db, -1002)["category_id"] == dirs["child"]
    assert _events(db) == []


def test_set_category_unknown_direction_is_value_error(db, dirs):
    with pytest.raises(ValueError):
        tc.set_category(db, -1001, 99999, source="mcp")


def test_set_enabled_false_journals(db, dirs):
    out = tc.set_enabled(db, -1001, False, source="mcp")
    assert out["enabled"] is False and _chat(db, -1001)["enabled"] == 0
    ev = _events(db)[-1]
    assert (ev["kind"], ev["old_value"], ev["new_value"]) == ("enabled", "1", "0")
    with pytest.raises(tc.NotMonitored):
        tc.set_enabled(db, -999, False, source="mcp")


def test_enable_chat_creates_only_from_dialogs(db, dirs, listener):
    with pytest.raises(tc.ListenerOffline):
        tc.enable_chat(db, -1005, dialogs=None, source="mcp")
    with pytest.raises(tc.NotMonitored):
        tc.enable_chat(db, -4242, dialogs=listener["dialogs"], source="mcp")
    assert _chat(db, -4242) is None
    out = tc.enable_chat(db, -1005, category_id=dirs["work"], dialogs=listener["dialogs"], source="mcp")
    assert out["created"] is True
    row = _chat(db, -1005)
    assert (row["title"], row["username"], row["chat_type"], row["enabled"], row["category_id"]) == \
        ("Новий чат", "novyi", "supergroup", 1, dirs["work"])
    known = tc.enable_chat(db, -1004, dialogs=None, source="mcp")
    assert known["created"] is False and _chat(db, -1004)["enabled"] == 1


def test_undo_restores_direction_and_refuses_twice(db, dirs):
    ev = tc.set_category(db, -1001, dirs["other"], source="mcp")["event_id"]
    out = tc.undo_event(db, ev, source="bot")
    assert out == {"chat_id": -1001, "kind": "category", "restored_value": dirs["work"]}
    assert _chat(db, -1001)["category_id"] == dirs["work"]
    rows = _events(db)
    assert rows[0]["undone_at"] and rows[0]["undone_by_event_id"] == rows[1]["id"]
    assert rows[1]["source"] == "bot"
    with pytest.raises(ValueError):
        tc.undo_event(db, ev, source="bot")


def test_undo_re_enables_and_refuses_when_not_last(db, dirs):
    first = tc.set_enabled(db, -1001, False, source="mcp")["event_id"]
    tc.set_enabled(db, -1001, True, source="ui")
    with pytest.raises(tc.UndoNotLast):
        tc.undo_event(db, first, source="bot")
    second = tc.set_enabled(db, -1003, False, source="mcp")["event_id"]
    assert tc.undo_event(db, second, source="bot")["restored_value"] is True
    assert _chat(db, -1003)["enabled"] == 1


def _spa_write(db, sql, *params):
    """An owner change through the unjournaled SPA routes: a bare UPDATE, no event row."""
    with get_db_connection(db) as conn:
        conn.execute(sql, params)
        conn.commit()


def test_undo_refuses_when_owner_changed_direction_without_journal(db, dirs):
    ev = tc.set_category(db, -1001, dirs["other"], source="mcp")["event_id"]
    _spa_write(db, "UPDATE tg_monitored_chats SET category_id = ? WHERE chat_id = -1001",
               dirs["child"])
    with pytest.raises(tc.UndoNotLast):
        tc.undo_event(db, ev, source="bot")
    assert _chat(db, -1001)["category_id"] == dirs["child"]
    assert not _events(db)[0]["undone_at"]


def test_undo_refuses_when_owner_re_enabled_without_journal(db, dirs):
    ev = tc.set_enabled(db, -1001, False, source="mcp")["event_id"]
    _spa_write(db, "UPDATE tg_monitored_chats SET enabled = 1 WHERE chat_id = -1001")
    with pytest.raises(tc.UndoNotLast):
        tc.undo_event(db, ev, source="bot")
    rows = _events(db)
    assert len(rows) == 1 and not rows[0]["undone_at"]


def test_undo_of_direction_runs_privacy_check(db, dirs):
    ev = tc.set_category(db, -1001, dirs["private"], source="ui")["event_id"]
    with pytest.raises(tc.PrivateCategoryChange):
        tc.undo_event(db, ev, source="bot")
    assert _chat(db, -1001)["category_id"] == dirs["private"]
    assert not _events(db)[0]["undone_at"]


def test_list_events_since_and_undone(db, dirs):
    ev = tc.set_enabled(db, -1001, False, source="mcp")["event_id"]
    tc.set_category(db, -1003, dirs["work"], source="ui")
    rows = tc.list_events(db, since="2000-01-01 00:00:00", source="mcp")
    assert [r["id"] for r in rows] == [ev] and rows[0]["chat_title"] == "Робочий чат"
    tc.undo_event(db, ev, source="bot")
    assert tc.list_events(db, since="2000-01-01 00:00:00", source="mcp") == []
    assert len(tc.list_events(db, since="2000-01-01 00:00:00", undone=True)) == 3


# ------------------------------------------------------------------ privacy filter

def test_filter_chats_hides_private_direction_and_stop_words(db, dirs, listener):
    chats = tc.get_chats(db)
    kept = {c["chat_id"] for c in privacy.filter_chats(chats, db_path=db)}
    assert kept == {-1001, -1004}
    dialogs = privacy.filter_chats(listener["dialogs"], db_path=db, key_id="id", key_category=None)
    assert {d["id"] for d in dialogs} == {-1005, -1001}


def test_filter_chats_unreadable_words_raises(db, dirs, tmp_path, monkeypatch):
    folder = tmp_path / "words_dir"
    folder.mkdir()
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(folder))
    with pytest.raises(privacy.PrivacyError):
        privacy.filter_chats(tc.get_chats(db), db_path=db)


# ------------------------------------------------------------------ routes

def test_get_chats_filtered_for_agent_full_for_ui(client, dirs):
    agent = client.get("/api/telegram/chats").get_json()["chats"]
    assert {c["chat_id"] for c in agent} == {-1001, -1004}
    ui = client.get("/api/telegram/chats", headers=UI).get_json()["chats"]
    assert {c["chat_id"] for c in ui} == {-1001, -1002, -1003, -1004}


def test_get_dialogs_filtered_and_projected_for_agent(client, dirs, listener):
    body = client.get("/api/telegram/dialogs").get_json()
    assert [set(d) for d in body["dialogs"]] == [{"chat_id", "title", "type", "monitored"}] * 2
    assert {d["chat_id"] for d in body["dialogs"]} == {-1005, -1001}
    ui = client.get("/api/telegram/dialogs", headers=UI).get_json()
    assert len(ui["dialogs"]) == 4 and ui["dialogs"][0]["phone"] == "+380000000"


def test_routes_answer_503_when_words_unreadable(client, dirs, listener, tmp_path, monkeypatch):
    folder = tmp_path / "words_dir2"
    folder.mkdir()
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(folder))
    for path in ("/api/telegram/chats", "/api/telegram/dialogs"):
        r = client.get(path)
        assert r.status_code == 503 and r.get_json()["error_code"] == "PRIVACY_CHECK_FAILED"


def test_patch_category_shapes_and_codes(client, db, dirs):
    r = client.patch("/api/telegram/chats/-1001/category", json={"category_id": dirs["other"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["previous_category_id"] == dirs["work"] and body["event_id"]
    r = client.patch("/api/telegram/chats/-999/category", json={"category_id": dirs["work"]})
    assert (r.status_code, r.get_json()["error_code"]) == (404, "NOT_MONITORED")
    r = client.patch("/api/telegram/chats/-1002/category", json={"category_id": dirs["work"]})
    assert (r.status_code, r.get_json()["error_code"]) == (403, "PRIVATE_CATEGORY_CHANGE")
    for bad in ({}, {"category_id": "x"}, {"category_id": True}, {"category_id": 99999}):
        r = client.patch("/api/telegram/chats/-1001/category", json=bad)
        assert (r.status_code, r.get_json()["error_code"]) == (400, "INVALID_ARGUMENT"), bad


def test_disable_route(client, db, dirs):
    r = client.post("/api/telegram/chats/-1001/disable")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is False and body["recovery"] == "telegram_enable_chat"
    assert _chat(db, -1001)["enabled"] == 0
    r = client.post("/api/telegram/chats/-999/disable")
    assert (r.status_code, r.get_json()["error_code"]) == (404, "NOT_MONITORED")


def test_enable_route_is_gated_denied_by_default(client, db, dirs, listener, dialog_stub):
    r = client.post("/api/telegram/chats/-1004/enable", json={})
    assert (r.status_code, r.get_json()["error_code"]) == (403, "APPROVAL_DENIED")
    assert _chat(db, -1004)["enabled"] == 0
    assert "Вимкнений чат" in dialog_stub.calls[-1][1]


def test_enable_route_refused_during_recording_before_dialog(client, db, dirs, dialog_stub, monkeypatch):
    monkeypatch.setattr(approval_gate, "recording_active", lambda **kw: True)
    r = client.post("/api/telegram/chats/-1004/enable", json={})
    assert (r.status_code, r.get_json()["error_code"]) == (409, "RECORDING_ACTIVE")
    assert dialog_stub.calls == []


def test_enable_route_approved_creates_from_dialogs(client, db, dirs, listener, approve_dialogs):
    r = client.post("/api/telegram/chats/-1005/enable", json={"category_id": dirs["work"]})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["created"] is True and body["enabled"] is True
    assert "Новий чат" in approve_dialogs.calls[-1][1]
    assert _chat(db, -1005)["category_id"] == dirs["work"]


def test_enable_route_listener_offline(client, db, dirs, listener, approve_dialogs):
    r = client.post("/api/telegram/chats/-1005/enable", json={}, headers=UI)
    assert r.status_code == 200
    listener["ok"] = False
    with get_db_connection(db) as conn:
        conn.execute("DELETE FROM tg_monitored_chats WHERE chat_id = -1005")
        conn.commit()
    # the summary cannot name a chat without the listener: refused before the dialog
    r = client.post("/api/telegram/chats/-1005/enable", json={})
    assert r.status_code == 400 and r.get_json()["error_code"] == "BAD_REQUEST"


def test_enable_route_view_answers_listener_offline(client, db, dirs, listener, approve_dialogs, monkeypatch):
    # the dialog was approved, then the listener went away before the view asked it
    calls = {"n": 0}
    real = telegram_bp_mod._listener_call

    def _flaky(path, method="GET", body=None, timeout=60):
        calls["n"] += 1
        if calls["n"] > 1:
            return False, {"error": "listener offline"}
        return real(path, method, body, timeout)

    monkeypatch.setattr(telegram_bp_mod, "_listener_call", _flaky)
    r = client.post("/api/telegram/chats/-1005/enable", json={})
    assert (r.status_code, r.get_json()["error_code"]) == (503, "LISTENER_OFFLINE")


def test_repair_dry_run_ungated_real_run_gated(client, db, dirs, listener, dialog_stub):
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO transcriptions (source_type, source_name, tg_chat_id, tg_message_id) "
                     "VALUES ('telegram', 'повідомлення', -1001, 1), ('telegram', 'повідомлення', -1001, 4)")
        conn.commit()
    r = client.post("/api/telegram/repair", json={"chat_id": -1001, "dry_run": True})
    assert r.status_code == 200 and r.get_json()["dry_run"] is True
    assert dialog_stub.calls == []
    r = client.post("/api/telegram/repair", json={"chat_id": -1001, "dry_run": False, "max_messages": 50})
    assert (r.status_code, r.get_json()["error_code"]) == (403, "APPROVAL_DENIED")
    assert "Робочий чат" in dialog_stub.calls[-1][1]
    assert not any(p == "/repair" for p, _ in listener["calls"])
    r = client.post("/api/telegram/repair", json={"chat_id": 12345, "dry_run": False})
    assert r.status_code == 400 and "error_code" not in r.get_json()
    assert len(dialog_stub.calls) == 1
    r = client.post("/api/telegram/repair", json={"chat_id": -1001}, headers=UI)
    assert r.status_code == 200 and len(dialog_stub.calls) == 1


def test_status_carries_bot_configured(client, listener, monkeypatch):
    monkeypatch.setattr("app.services.tg_bot.is_configured", lambda: True)
    assert client.get("/api/telegram/status").get_json()["bot_configured"] is True
    listener["ok"] = False
    monkeypatch.setattr("app.services.tg_bot.is_configured", lambda: False)
    assert client.get("/api/telegram/status").get_json()["bot_configured"] is False


def test_gate_tables_carry_the_new_entries():
    assert approval_gate._D_ROUTES["telegram.tg_enable_chat"] == "telegram_enable_chat"
    assert "telegram_enable_chat" in approval_gate.D_ACTIONS
    assert approval_gate._CONDITIONAL_D_ROUTES["telegram.tg_repair"][0] == "telegram_backfill"
