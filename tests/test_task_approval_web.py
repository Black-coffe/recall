"""ADR-056 over the web API: approve / reject / edit through PATCH, the proposal queue
through `GET ?status=proposed`, and the rule that only the owner's SPA starts the clock."""
from __future__ import annotations

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.core import auth
from app.db.connection import get_db_connection
from app.db.migrations import init_database

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "approval.db")
    init_database(path)
    with get_db_connection(path) as conn:
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, meeting_date) "
                     "VALUES (1, 'recording', 'Планерка', '2026-10-01')")
        rows = [
            # task, status, due_date, importance
            ("Надіслати кошторис", "proposed", None, 1),
            ("Подзвонити Олені", "proposed", "2026-11-05", 2),
            ("Підписати договір", "proposed", None, 3),
            ("Оновити сайт", "proposed", "2026-11-01", 1),
            ("Відкрита задача", "open", None, None),
            ("Минула пропозиція", "expired", None, 2),
            ("Стара задача", "legacy", None, None),
        ]
        for task, status, due_date, imp in rows:
            conn.execute("INSERT INTO action_items (transcription_id, task, status, due_date, "
                         "for_whom, confidence, importance) VALUES (1, ?, ?, ?, 'owner', 0.8, ?)",
                         (task, status, due_date, imp))
        conn.commit()
    return path


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.register_blueprint(memory_bp)
    app.config.update(TESTING=True, DATABASE=db)
    return app.test_client()


def _id(db, task):
    with get_db_connection(db) as conn:
        return conn.execute("SELECT id FROM action_items WHERE task = ?", (task,)).fetchone()[0]


def _row(db, aid):
    with get_db_connection(db) as conn:
        return dict(conn.execute("SELECT * FROM action_items WHERE id = ?", (aid,)).fetchone())


def _events(db, action=None):
    sql, p = "SELECT * FROM action_item_events", ()
    if action:
        sql, p = sql + " WHERE action = ?", (action,)
    with get_db_connection(db) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", p)]


# ---------------------------------------------------------------- PATCH

def test_approve_proposed_opens_it_with_a_web_event(client, db):
    aid = _id(db, "Надіслати кошторис")
    r = client.patch(f"/api/memory/action-items/{aid}", json={"action": "approve"}, headers=UI)
    assert r.status_code == 200 and r.get_json()["status"] == "open"
    row = _row(db, aid)
    assert row["status"] == "open" and row["decided_at"]
    [ev] = _events(db)
    assert ev["source"] == "web" and ev["action"] == "approve"


def test_approve_on_open_is_an_invalid_transition(client, db):
    aid = _id(db, "Відкрита задача")
    r = client.patch(f"/api/memory/action-items/{aid}", json={"action": "approve"}, headers=UI)
    assert r.status_code == 409 and r.get_json()["error_code"] == "INVALID_TRANSITION"
    assert _row(db, aid)["status"] == "open" and _events(db) == []


def test_reject_expired_marks_it_rejected(client, db):
    aid = _id(db, "Минула пропозиція")
    r = client.patch(f"/api/memory/action-items/{aid}", json={"action": "reject"}, headers=UI)
    assert r.status_code == 200
    assert _row(db, aid)["status"] == "rejected"


def test_edit_changes_text_and_date_and_undo_restores_them(client, db):
    aid = _id(db, "Подзвонити Олені")
    r = client.patch(f"/api/memory/action-items/{aid}", headers=UI,
                     json={"action": "edit", "task": "Подзвонити Олені щодо бюджету",
                           "due_date": "2026-11-20"})
    assert r.status_code == 200
    row = _row(db, aid)
    assert (row["task"], row["due_date"], row["status"]) == (
        "Подзвонити Олені щодо бюджету", "2026-11-20", "proposed")
    u = client.post(f"/api/memory/action-events/{r.get_json()['event_id']}/undo", headers=UI)
    assert u.status_code == 200
    row = _row(db, aid)
    assert (row["task"], row["due_date"]) == ("Подзвонити Олені", "2026-11-05")


@pytest.mark.parametrize("body", [
    {"status": "done", "action": "approve"},
    {"action": "archive"},
    {"action": "edit"},
    {"action": "edit", "due_date": "20.11.2026"},
], ids=["status-and-action", "unknown-action", "empty-edit", "bad-date"])
def test_bad_action_bodies_are_invalid_arguments(client, db, body):
    aid = _id(db, "Надіслати кошторис")
    r = client.patch(f"/api/memory/action-items/{aid}", json=body, headers=UI)
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ARGUMENT"
    assert _row(db, aid)["status"] == "proposed" and _events(db) == []


def test_old_status_body_still_works(client, db):
    aid = _id(db, "Відкрита задача")
    assert client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"},
                        headers=UI).status_code == 200
    assert _row(db, aid)["status"] == "done"


# ---------------------------------------------------------------- GET

def test_agent_listing_proposals_starts_no_clock(client, db):
    r = client.get("/api/memory/action-items?status=proposed")
    assert r.status_code == 200 and len(r.get_json()["action_items"]) == 4
    assert _events(db, "show") == []
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_items "
                            "WHERE first_shown_at IS NOT NULL").fetchone()[0] == 0


def test_ui_listing_proposals_marks_them_shown_once_in_queue_order(client, db):
    r = client.get("/api/memory/action-items?status=proposed", headers=UI)
    items = r.get_json()["action_items"]
    assert [a["task"] for a in items] == [
        "Оновити сайт", "Подзвонити Олені", "Підписати договір", "Надіслати кошторис"]
    assert {"for_whom", "confidence", "importance", "first_shown_at", "decided_at"} <= set(items[0])
    assert all(a["first_shown_at"] for a in items)
    first = {a["id"]: _row(db, a["id"])["first_shown_at"] for a in items}
    again = client.get("/api/memory/action-items?status=proposed", headers=UI).get_json()
    assert {a["id"]: a["first_shown_at"] for a in again["action_items"]} == first
    assert len(_events(db, "show")) == 4
    assert {e["source"] for e in _events(db, "show")} == {"web"}


def test_status_filters(client, db):
    def tasks(qs):
        r = client.get("/api/memory/action-items" + qs, headers=UI)
        assert r.status_code == 200
        return {a["task"] for a in r.get_json()["action_items"]}
    assert tasks("") == {"Відкрита задача"}
    assert tasks("?status=legacy") == {"Стара задача"}
    assert tasks("?status=expired") == {"Минула пропозиція"}
    assert "Стара задача" not in tasks("?status=all")
    assert client.get("/api/memory/action-items?status=bogus").status_code == 400
