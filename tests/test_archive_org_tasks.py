"""Agent (non-UI) task actions over the memory blueprint: `source='mcp'` events, dedupe,
postpone, undo of own events only. The UI path (per-boot token) is unchanged."""
from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.core import auth
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import commitments as cm

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}
TASK = "Надіслати кошторис Андрію"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "tasks.db")
    init_database(path)
    with get_db_connection(path) as conn:
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, meeting_date) "
                     "VALUES (1, 'recording', 'Планерка', '2026-10-01')")
        conn.execute("INSERT INTO action_items (transcription_id, task, due, status) "
                     "VALUES (1, ?, 'завтра', 'open')", (TASK,))
        conn.commit()
    return path


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.register_blueprint(memory_bp)
    app.config.update(TESTING=True, DATABASE=db)
    return app.test_client()


def _aid(db):
    with get_db_connection(db) as conn:
        return conn.execute("SELECT id FROM action_items WHERE task = ?", (TASK,)).fetchone()[0]


def _state(db, aid):
    with get_db_connection(db) as conn:
        return dict(conn.execute("SELECT status, due_date FROM action_items WHERE id = ?",
                                 (aid,)).fetchone())


def _events(db):
    with get_db_connection(db) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM action_item_events ORDER BY id")]


# ---------------------------------------------------------------- PATCH status

def test_agent_patch_writes_mcp_event_and_full_contract(client, db):
    aid = _aid(db)
    r = client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"})
    j = r.get_json()
    assert r.status_code == 200
    assert j["item_id"] == aid and j["status"] == "done" and j["created"] is True
    [ev] = _events(db)
    assert ev["id"] == j["event_id"] and ev["source"] == "mcp" and ev["actor"] == "mcp"
    assert set(j) >= {"success", "item_id", "status", "due_date", "event_id", "created"}


def test_agent_repeated_action_is_not_written_again(client, db):
    aid = _aid(db)
    first = client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"}).get_json()
    again = client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"}).get_json()
    assert again["created"] is False and again["event_id"] == first["event_id"]
    assert len(_events(db)) == 1


def test_agent_repeat_on_item_without_events_reports_no_event(client, db):
    aid = _aid(db)
    j = client.patch(f"/api/memory/action-items/{aid}", json={"status": "open"}).get_json()
    assert j["created"] is False and j["event_id"] is None and _events(db) == []


def test_ui_patch_still_writes_web_and_repeats(client, db):
    aid = _aid(db)
    r1 = client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"}, headers=UI)
    r2 = client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"}, headers=UI)
    assert r1.status_code == 200 and r2.get_json()["created"] is True
    assert [e["source"] for e in _events(db)] == ["web", "web"]


def test_patch_unknown_item_is_404(client, db):
    r = client.patch("/api/memory/action-items/99999", json={"status": "done"})
    assert r.status_code == 404 and r.get_json()["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------- postpone

@pytest.mark.parametrize("days", [1, 7, 30])
def test_agent_postpone_sets_due_date(client, db, days):
    aid = _aid(db)
    r = client.post(f"/api/memory/action-items/{aid}/postpone", json={"days": days})
    j = r.get_json()
    expected = date.fromordinal(date.today().toordinal() + days).isoformat()
    assert r.status_code == 200 and j["due_date"] == expected and j["status"] == "open"
    assert _state(db, aid)["due_date"] == expected
    [ev] = _events(db)
    assert ev["source"] == "mcp" and ev["action"] == f"postpone:{days}"


@pytest.mark.parametrize("days", [0, 2, "7", True, None, "сім"])
def test_postpone_bad_days_is_400_and_writes_nothing(client, db, days):
    aid = _aid(db)
    r = client.post(f"/api/memory/action-items/{aid}/postpone", json={"days": days})
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ARGUMENT"
    assert _events(db) == [] and _state(db, aid)["due_date"] is None


def test_agent_postpone_repeated_is_created_false(client, db):
    aid = _aid(db)
    first = client.post(f"/api/memory/action-items/{aid}/postpone", json={"days": 7}).get_json()
    again = client.post(f"/api/memory/action-items/{aid}/postpone", json={"days": 7}).get_json()
    assert again["created"] is False and again["event_id"] == first["event_id"]


def test_ui_postpone_writes_web_event(client, db):
    aid = _aid(db)
    r = client.post(f"/api/memory/action-items/{aid}/postpone", json={"days": 1}, headers=UI)
    assert r.status_code == 200 and _events(db)[0]["source"] == "web"


# ---------------------------------------------------------------- undo

def test_agent_undoes_own_last_event(client, db):
    aid = _aid(db)
    ev = client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"}).get_json()
    r = client.post(f"/api/memory/action-events/{ev['event_id']}/undo")
    j = r.get_json()
    assert r.status_code == 200 and j["undone_event_id"] == ev["event_id"]
    assert j["item_id"] == aid and j["status"] == "open" and _state(db, aid)["status"] == "open"
    assert _events(db)[-1]["source"] == "mcp" and _events(db)[-1]["undo_of"] == ev["event_id"]


def test_agent_cannot_undo_web_or_brief_event(client, db):
    aid = _aid(db)
    web = cm.apply_action(db, aid, "done", source="web", actor="web")
    r = client.post(f"/api/memory/action-events/{web['event_id']}/undo")
    assert r.status_code == 403 and r.get_json()["error_code"] == "NOT_MCP_EVENT"
    bot = cm.apply_action(db, aid, "cancelled", source="bot", actor="tg:42")
    r = client.post(f"/api/memory/action-events/{bot['event_id']}/undo")
    assert r.status_code == 403 and r.get_json()["error_code"] == "NOT_MCP_EVENT"
    assert _state(db, aid)["status"] == "cancelled" and len(_events(db)) == 2


def test_agent_undo_of_non_last_event_is_409_and_changes_nothing(client, db):
    aid = _aid(db)
    first = client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"}).get_json()
    client.patch(f"/api/memory/action-items/{aid}", json={"status": "cancelled"})
    r = client.post(f"/api/memory/action-events/{first['event_id']}/undo")
    assert r.status_code == 409 and r.get_json()["error_code"] == "UNDO_NOT_LAST"
    assert _state(db, aid)["status"] == "cancelled" and len(_events(db)) == 2


def test_undo_unknown_event_is_404(client, db):
    r = client.post("/api/memory/action-events/424242/undo")
    assert r.status_code == 404 and r.get_json()["error_code"] == "NOT_FOUND"


def test_ui_undoes_a_brief_event(client, db):
    aid = _aid(db)
    bot = cm.apply_action(db, aid, "done", source="bot", actor="tg:42")
    r = client.post(f"/api/memory/action-events/{bot['event_id']}/undo", headers=UI)
    assert r.status_code == 200 and _state(db, aid)["status"] == "open"
    assert _events(db)[-1]["source"] == "web"


def test_service_only_source_is_checked_before_last_rule(db):
    aid = _aid(db)
    web = cm.apply_action(db, aid, "done", source="web")
    cm.apply_action(db, aid, "open", source="mcp")
    with pytest.raises(PermissionError):
        cm.undo_action(db, web["event_id"], source="mcp", only_source="mcp")
    assert _state(db, aid)["status"] == "open"
