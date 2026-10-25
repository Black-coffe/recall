"""Mail HTTP API: /api/mail/* (gmail-ingest-05). No network, in-memory keyring."""
from __future__ import annotations

import json
import logging

import keyring
import pytest
from flask import Flask

from app.blueprints.mail import mail_bp
from app.core.i18n_catalog import mail as mail_catalog
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import mail_accounts as ma
from app.services import mail_imap

PW = "zq-distinct-pass-4417"
PW_SPACED = "zq-d istinct pa ss-4417"
PASTED = "abcd efgh ijkl mnop"
ADDR = "ivan.test@example.com"

FOLDERS = [
    {"folder": "INBOX", "display_name": "INBOX", "flags": [], "system": False},
    {"folder": "Work", "display_name": "Work", "flags": [], "system": False},
]


class FakeClient:
    def list_folders(self):
        return list(FOLDERS)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePoller:
    interval_min = 7

    def __init__(self):
        self.calls = []

    def trigger(self, account_id=None, months=None):
        self.calls.append((account_id, months))
        return True

    def is_alive(self):
        return True


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "mail_api.db")
    init_database(p)
    return p


@pytest.fixture
def app(db):
    a = Flask(__name__)
    a.register_blueprint(mail_bp)
    a.config["TESTING"] = True
    a.config["DATABASE"] = db
    return a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seen(monkeypatch):
    """Passwords the fake connect saw."""
    log = []

    def fake_connect(address, password, **kw):
        log.append(password)
        return FakeClient()

    monkeypatch.setattr(mail_imap, "connect", fake_connect)
    return log


def _no_password(resp, *passwords):
    body = resp.get_data(as_text=True)
    for p in passwords:
        assert p not in body


def _add(client, password=PW):
    r = client.post("/api/mail/accounts", json={"address": ADDR, "app_password": password})
    _no_password(r, password, PW_SPACED)
    return r


def test_add_account_success(client, seen):
    r = _add(client)
    assert r.status_code == 201
    data = r.get_json()
    assert data["account"]["address"] == ADDR
    assert {lb["folder"] for lb in data["labels"]} == {"INBOX", "Work"}
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) == PW
    _no_password(client.get("/api/mail/accounts"), PW)
    _no_password(client.get("/api/mail/status"), PW)


def test_add_account_strips_spaces(client, seen):
    assert _add(client, PASTED).status_code == 201
    assert seen == ["abcdefghijklmnop"]
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) == "abcdefghijklmnop"


def test_add_account_invalid_input(client, seen):
    r = client.post("/api/mail/accounts", json={"address": "nope", "app_password": PW})
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ADDRESS"
    r = client.post("/api/mail/accounts", json={"address": ADDR, "app_password": "  "})
    assert r.status_code == 400 and r.get_json()["error_code"] == "EMPTY_PASSWORD"
    assert seen == []


@pytest.mark.parametrize("code,status", [("AUTH_FAILED", 400), ("CONNECT_FAILED", 502)])
def test_add_account_login_failure_stores_nothing(client, db, monkeypatch, code, status):
    def boom(address, password, **kw):
        raise mail_imap.MailError(code, "raw detail " + password, password)

    monkeypatch.setattr(mail_imap, "connect", boom)
    r = _add(client)
    assert r.status_code == status
    body = r.get_json()
    assert body["error_code"] == code and body["error"] and "raw detail" not in body["error"]
    assert ma.list_accounts(db) == []
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) is None


def test_delete_account(client, seen, db):
    acc = _add(client).get_json()["account"]
    assert client.delete(f"/api/mail/accounts/{acc['id']}").status_code == 200
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) is None
    assert client.delete(f"/api/mail/accounts/{acc['id']}").status_code == 404
    assert client.delete("/api/mail/accounts/9999").status_code == 404


def test_labels_list_and_refresh(client, seen):
    acc = _add(client).get_json()["account"]
    FOLDERS.append({"folder": "Extra", "display_name": "Extra", "flags": [], "system": False})
    try:
        r = client.get(f"/api/mail/accounts/{acc['id']}/labels")
        assert len(r.get_json()["labels"]) == 2
        r = client.get(f"/api/mail/accounts/{acc['id']}/labels?refresh=1")
        assert len(r.get_json()["labels"]) == 3
    finally:
        FOLDERS.pop()
    assert client.get("/api/mail/accounts/9999/labels").status_code == 404


def test_label_patch(client, seen, app, db):
    acc = _add(client).get_json()["account"]
    label = client.get(f"/api/mail/accounts/{acc['id']}/labels").get_json()["labels"][0]
    with get_db_connection(db) as conn:
        cid = conn.execute("SELECT id FROM categories WHERE parent_id IS NULL LIMIT 1").fetchone()[0]
    url = f"/api/mail/labels/{label['id']}"
    r = client.patch(url, json={"enabled": True, "category_id": cid, "enrich": False})
    assert r.status_code == 200
    got = r.get_json()["label"]
    assert got["enabled"] and got["category_id"] == cid and not got["enrich"]
    assert client.patch(url, json={"category_id": None}).get_json()["label"]["category_id"] is None
    assert client.patch(url, json={"category_id": 99999}).status_code == 400
    assert client.patch(url, json={"enabled": "yes"}).status_code == 400
    assert client.patch(url, json={"enrich": 1}).status_code == 400
    assert client.patch("/api/mail/labels/9999", json={"enabled": True}).status_code == 404


def test_label_enable_triggers_poller_only_when_present(client, seen, app):
    acc = _add(client).get_json()["account"]
    label_id = client.get(f"/api/mail/accounts/{acc['id']}/labels").get_json()["labels"][0]["id"]
    assert client.patch(f"/api/mail/labels/{label_id}", json={"enabled": True}).status_code == 200
    poller = FakePoller()
    app.extensions["mail_poller"] = poller
    client.patch(f"/api/mail/labels/{label_id}", json={"enabled": True})
    assert poller.calls == [(acc["id"], None)]


def test_sync(client, seen, app):
    acc = _add(client).get_json()["account"]
    r = client.post("/api/mail/sync", json={"months": 3})
    assert r.status_code == 503 and r.get_json()["error_code"] == "MAIL_POLLER_OFF"
    poller = FakePoller()
    app.extensions["mail_poller"] = poller
    for bad in (0, 121, "3", 1.5, True):
        assert client.post("/api/mail/sync", json={"months": bad}).status_code == 400
    r = client.post("/api/mail/sync", json={"account_id": acc["id"], "months": 12})
    assert r.status_code == 202 and r.get_json()["queued"] is True
    assert poller.calls == [(acc["id"], 12)]


def test_status(client, seen, app):
    _add(client)
    off = client.get("/api/mail/status").get_json()
    assert off["poller"]["running"] is False and off["accounts"][0]["address"] == ADDR
    app.extensions["mail_poller"] = FakePoller()
    on = client.get("/api/mail/status").get_json()
    assert on["poller"] == {"running": True, "interval_min": 7}


def test_no_password_in_logs(client, seen, db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    acc = _add(client, PASTED).get_json()["account"]
    client.delete(f"/api/mail/accounts/{acc['id']}")

    def boom(address, password, **kw):
        raise mail_imap.MailError("AUTH_FAILED", "bad " + password, password)

    monkeypatch.setattr(mail_imap, "connect", boom)
    _add(client, PW)
    _add(client, PASTED)
    text = "\n".join(r.getMessage() + str(r.exc_text) for r in caplog.records)
    for secret in (PW, PW_SPACED, PASTED, "abcdefghijklmnop"):
        assert secret not in text


def test_catalog_uk_en_same_vars():
    import string

    fmt = string.Formatter()
    for key, entry in mail_catalog.MESSAGES.items():
        assert set(entry) == {"uk", "en"}, key
        names = [{f for _, f, _, _ in fmt.parse(entry[lang]) if f} for lang in ("uk", "en")]
        assert names[0] == names[1], key
    json.dumps(mail_catalog.MESSAGES)


MCP = {"X-Recall-Actor": "mcp", "X-Recall-Tool": "email_label_set"}


class CapPoller(FakePoller):
    def trigger(self, account_id=None, months=None, first_months_cap=None):
        self.calls.append((account_id, months, first_months_cap))


def _label_id(client, folder="Work"):
    data = _add(client).get_json()
    return next(lb["id"] for lb in data["labels"] if lb["folder"] == folder)


def test_connect_account_stores_nothing_on_list_failure(db, monkeypatch):
    def boom(address, password, **kw):
        raise mail_imap.MailError(mail_imap.AUTH_FAILED)

    monkeypatch.setattr(mail_imap, "connect", boom)
    with pytest.raises(mail_imap.MailError) as ei:
        ma.connect_account(db, ADDR, PW)
    assert ei.value.code == mail_imap.AUTH_FAILED
    assert ma.list_accounts(db) == []
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) is None
    with pytest.raises(ValueError, match="INVALID_ADDRESS"):
        ma.connect_account(db, "nope", PW)
    with pytest.raises(ValueError, match="EMPTY_PASSWORD"):
        ma.connect_account(db, ADDR, "   ")


def test_labels_refresh_route(client, seen, app):
    assert client.post("/api/mail/accounts/99/labels/refresh").status_code == 404
    aid = _add(client).get_json()["account"]["id"]
    FOLDERS.append({"folder": "Extra", "display_name": "Extra", "flags": [], "system": False})
    try:
        r = client.post(f"/api/mail/accounts/{aid}/labels/refresh")
    finally:
        FOLDERS.pop()
    assert r.status_code == 200 and "Extra" in {lb["folder"] for lb in r.get_json()["labels"]}
    keyring.delete_password(ma.KEYRING_SERVICE, ADDR)
    r = client.post(f"/api/mail/accounts/{aid}/labels/refresh")
    assert r.status_code == 409 and r.get_json()["error_code"] == "NO_PASSWORD"


def test_mcp_patch_refusals_and_cap(client, seen, app, db, monkeypatch):
    monkeypatch.setenv("MAIL_BACKFILL_MONTHS", "24")
    poller = app.extensions["mail_poller"] = CapPoller()
    lid = _label_id(client)
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) VALUES "
                     "(15, 'Юридичне', 'юридичне', NULL)")
        conn.execute("UPDATE mail_labels SET category_id = 15 WHERE id = ?", (lid,))
        conn.commit()
    url = f"/api/mail/labels/{lid}"
    r = client.patch(url, json={"category_id": 1}, headers=MCP)
    assert r.status_code == 403 and r.get_json()["error_code"] == "PRIVATE_CATEGORY_CHANGE"
    assert ma.list_labels(db)[1]["category_id"] == 15
    r = client.patch(url, json={"enrich": True}, headers=MCP)
    assert r.status_code == 400 and r.get_json()["error_code"] == "USE_GATED_ROUTE"
    from app.services import privacy
    monkeypatch.setattr(privacy, "category_change_exposes",
                        lambda *a: (_ for _ in ()).throw(privacy.PrivacyError("x")))
    r = client.patch(url, json={"category_id": 1}, headers=MCP)
    assert r.status_code == 503 and r.get_json()["error_code"] == "PRIVACY_CHECK_FAILED"
    monkeypatch.undo()
    assert client.patch(url, json={"category_id": 1}).status_code == 200
    r = client.patch(url, json={"enabled": True}, headers=MCP)
    assert r.get_json()["backfill_months"] == 12
    assert poller.calls[-1] == (1, None, 12)


def test_sync_months_cap_holds_for_every_caller(client, app):
    app.extensions["mail_poller"] = FakePoller()
    for headers in (MCP, {}):
        r = client.post("/api/mail/sync", json={"months": 13}, headers=headers)
        assert r.status_code == 400 and r.get_json()["error_code"] == "USE_GATED_ROUTE"
        assert "/api/mail/backfill" in r.get_json()["error"]
    assert client.post("/api/mail/sync", json={"months": 12}, headers=MCP).status_code == 202


def test_mcp_request_logged_once_without_password(client, seen, caplog):
    caplog.set_level(logging.INFO)
    r = client.post("/api/mail/accounts", json={"address": ADDR, "app_password": PW}, headers=MCP)
    assert r.status_code == 201
    lines = [x.getMessage() for x in caplog.records if "actor=mcp" in x.getMessage()]
    assert lines == ["actor=mcp tool=email_label_set route=mail.mail_accounts_add status=201"]
    assert all(PW not in x.getMessage() for x in caplog.records)
