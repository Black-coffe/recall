"""mcp-mail-and-keys story 01: mail actions that cost money or lose state, behind the dialog.

The routes here are the gated twins of the plain mail routes: enrich on, backfill beyond
12 months, soft disconnect. The plain routes refuse the gated variant for every caller (UI,
MCP, curl), so the dialog cannot be skipped by leaving a header out. In-memory keyring
(tests/conftest.py); no network; Cyrillic fixtures.
"""
from __future__ import annotations

import keyring
import pytest
from flask import Flask

from app.blueprints.mail import mail_bp
from app.core import approval_gate, auth
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import mail_accounts as ma

ADDR = "олена.тест@example.com"
MCP = {"X-Recall-Actor": "mcp", "X-Recall-Tool": "mailbox"}


class Poller:
    def __init__(self):
        self.calls = []

    def trigger(self, account_id=None, months=None, first_months_cap=None):
        self.calls.append((account_id, months))
        return True

    def is_alive(self):
        return True


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "mail_gate.db")
    init_database(p)
    return p


@pytest.fixture
def app(db, tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
    a = Flask(__name__, root_path=str(tmp_path))
    a.config.update(TESTING=True, DATABASE=db)
    a.register_blueprint(mail_bp)
    a.extensions["mail_poller"] = Poller()
    return a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def account(db):
    """Two labels: «Вхідні» enabled with a cursor and a direction, «Робота» never enabled."""
    keyring.set_password(ma.KEYRING_SERVICE, ADDR, "pw-тест-1")
    with get_db_connection(db) as conn:
        aid = conn.execute("INSERT INTO mail_accounts (address) VALUES (?)", (ADDR,)).lastrowid
        conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES (41, 'Пошта', 'пошта', NULL)")
        inbox = conn.execute("INSERT INTO mail_labels (account_id, folder, display_name, enabled, category_id, "
                             "last_uid, synced_since) VALUES (?, 'INBOX', 'Вхідні', 1, 41, 777, '2025-10-01')",
                             (aid,)).lastrowid
        work = conn.execute("INSERT INTO mail_labels (account_id, folder, display_name) VALUES (?, 'Work', 'Робота')",
                            (aid,)).lastrowid
        conn.commit()
    yield {"id": aid, "inbox": inbox, "work": work}
    try:
        keyring.delete_password(ma.KEYRING_SERVICE, ADDR)
    except keyring.errors.PasswordDeleteError:
        pass


def test_every_new_route_is_in_the_gate_table():
    assert approval_gate._D_ROUTES["mail.mail_backfill"] == "mailbox_backfill"
    assert approval_gate._D_ROUTES["mail.mail_label_enrich"] == "mailbox_label_enrich"
    assert approval_gate._D_ROUTES["mail.mail_accounts_disconnect"] == "mailbox_disconnect"
    assert approval_gate._D_ROUTES["mail.mail_accounts_delete"] == "mailbox_delete"


@pytest.mark.parametrize("headers", [{}, MCP], ids=["no_header", "mcp_header"])
def test_plain_patch_refuses_enrich_on_for_every_caller(client, db, account, headers):
    r = client.patch(f"/api/mail/labels/{account['work']}", json={"enrich": True}, headers=headers)
    assert r.status_code == 400 and r.get_json()["error_code"] == "USE_GATED_ROUTE"
    assert f"/api/mail/labels/{account['work']}/enrich" in r.get_json()["error"]
    assert ma.list_labels(db, account["id"])[1]["enrich"] == 0


def test_plain_patch_still_turns_enrich_off(client, db, account):
    ma.update_label(db, account["inbox"], enrich=True)
    r = client.patch(f"/api/mail/labels/{account['inbox']}", json={"enrich": False}, headers=MCP)
    assert r.status_code == 200 and r.get_json()["label"]["enrich"] == 0


def test_enrich_route_turns_it_on(client, db, account):
    r = client.post(f"/api/mail/labels/{account['work']}/enrich", json={})
    assert r.status_code == 200 and r.get_json()["label"]["enrich"] == 1
    assert client.post("/api/mail/labels/9999/enrich", json={}).status_code == 404


@pytest.mark.parametrize("headers", [{}, MCP], ids=["no_header", "mcp_header"])
def test_sync_refuses_more_than_twelve_months_for_every_caller(client, app, account, headers):
    r = client.post("/api/mail/sync", json={"account_id": account["id"], "months": 13}, headers=headers)
    assert r.status_code == 400 and r.get_json()["error_code"] == "USE_GATED_ROUTE"
    assert app.extensions["mail_poller"].calls == []


def test_backfill_route_takes_only_thirteen_to_one_twenty(client, app, account):
    for bad in (12, 121, "24", True, None):
        assert client.post("/api/mail/backfill", json={"months": bad}).status_code == 400
    r = client.post("/api/mail/backfill", json={"account_id": account["id"], "months": 36})
    assert r.status_code == 200 and r.get_json()["queued"] is True
    assert app.extensions["mail_poller"].calls == [(account["id"], 36)]
    assert client.post("/api/mail/backfill", json={"account_id": 9999, "months": 36}).status_code == 404


def test_soft_disconnect_keeps_cursors_and_names_the_disabled_labels(client, db, account):
    r = client.delete(f"/api/mail/accounts/{account['id']}/credentials")
    assert r.status_code == 200
    body = r.get_json()
    assert body["labels_disabled"] == 1 and body["disabled_label_ids"] == [account["inbox"]]
    assert body["records_kept"] is True and body["recovery"]
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) is None
    inbox = ma.list_labels(db, account["id"])[0]
    assert inbox["enabled"] == 0 and inbox["category_id"] == 41
    with get_db_connection(db) as conn:
        row = conn.execute("SELECT last_uid, synced_since FROM mail_labels WHERE id = ?",
                           (account["inbox"],)).fetchone()
    assert (row["last_uid"], row["synced_since"]) == (777, "2025-10-01")
    # a reconnect reuses the row: no second account, the cursor is still there
    again = ma.add_account(db, ADDR, "pw-тест-2")
    assert again["id"] == account["id"]
    assert len(ma.list_accounts(db)) == 1
    assert client.delete("/api/mail/accounts/9999/credentials").status_code == 404
