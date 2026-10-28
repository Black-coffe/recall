"""Connect link issuers and the local secret pages. No network, in-memory keyring."""
from __future__ import annotations

import logging

import keyring
import pytest
from flask import Flask

from app.blueprints.connect import connect_bp
from app.blueprints.mail import mail_bp
from app.blueprints.settings_api import settings_bp
from app.db.migrations import init_database
from app.services import connect_links as cl
from app.services import mail_accounts as ma
from app.services import mail_imap

ADDR = "ivan.test@example.com"
PW = "zq-distinct-pass-4417"
KEY = "sk-ant-api03-ZZtestkeyZZ12345678"
BAD_KEY = "garbage-bad-key-9317"
BASE = "http://127.0.0.1:5050"
FOLDERS = [{"folder": "INBOX", "display_name": "INBOX", "flags": [], "system": False},
           {"folder": "Work", "display_name": "Work", "flags": [], "system": False}]


class FakeClient:
    def list_folders(self):
        return list(FOLDERS)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def app(tmp_path):
    db = str(tmp_path / "c.db")
    init_database(db)
    a = Flask(__name__, root_path=str(tmp_path), template_folder=str(
        __import__("pathlib").Path(__file__).resolve().parent.parent / "templates"))
    a.register_blueprint(mail_bp)
    a.register_blueprint(settings_bp)
    a.register_blueprint(connect_bp)
    a.config.update(TESTING=True, DATABASE=db)
    return a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    cl._links.clear()
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def imap_ok(monkeypatch):
    monkeypatch.setattr(mail_imap, "connect", lambda a, p, **kw: FakeClient())


def _fail_with(monkeypatch, code):
    def boom(a, p, **kw):
        raise mail_imap.MailError(code)
    monkeypatch.setattr(mail_imap, "connect", boom)


def _path(url):
    return url.replace(BASE, "")


def _issue_mail(client, **headers):
    r = client.post("/api/mail/connect-link", json={"address": ADDR}, headers=headers)
    assert r.status_code == 200
    return _path(r.get_json()["connect_url"])


def _issue_key(client, **headers):
    r = client.post("/api/settings/anthropic-key/connect-link", json={}, headers=headers)
    assert r.status_code == 200
    return _path(r.get_json()["connect_url"])


def _token(path):
    return path.rsplit("/", 1)[1]


def _get(client, path, **kw):
    kw.setdefault("base_url", BASE)
    return client.get(path, **kw)


def _post(client, path, secret, **kw):
    kw.setdefault("base_url", BASE)
    return client.post(path, data={"secret": secret}, **kw)


def test_issue_responses(client):
    r = client.post("/api/mail/connect-link", json={"address": " Ivan.Test@Example.com "},
                    base_url=BASE).get_json()
    assert r["address"] == ADDR and r["expires_in"] == 600 and r["already_configured"] is False
    assert r["connect_url"].startswith(BASE + "/connect/mail/")
    k = client.post("/api/settings/anthropic-key/connect-link", json={}, base_url=BASE).get_json()
    assert k["already_configured"] is False and "/connect/anthropic_key/" in k["connect_url"]


def test_issue_rejects_bad_and_secret_address(client, caplog):
    caplog.set_level(logging.DEBUG)
    bad = client.post("/api/mail/connect-link", json={"address": "nope"})
    assert bad.status_code == 400 and bad.get_json()["error_code"] == "INVALID_ADDRESS"
    r = client.post("/api/mail/connect-link", json={"address": "abcd efgh ijkl mnop"})
    assert r.status_code == 400 and r.get_json()["error_code"] == "SECRET_IN_ARGUMENT"
    assert "abcd efgh" not in r.get_data(as_text=True)
    assert "abcd efgh" not in caplog.text


def test_key_route_goes_through_save(client, monkeypatch):
    calls = []
    from app.services import settings_secrets
    monkeypatch.setattr(settings_secrets, "save_anthropic_key",
                        lambda k: calls.append(k) or {"configured": True})
    r = client.post("/api/settings/anthropic-key", json={"api_key": KEY})
    assert r.get_json() == {"success": True, "configured": True} and calls == [KEY]


def test_get_page_headers_and_text(client):
    path = _issue_mail(client, **{"X-Recall-Actor": "mcp"})
    r = _get(client, path)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and ADDR in body and "Requested by MCP agent at" in body
    assert 'type="password"' in body and 'autocomplete="off"' in body
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert cl.peek("mail", _token(path)) is not None


def test_banner_names_its_own_kind(client):
    cal = client.post("/api/settings/calendars/connect-link", json={"label": "Google"})
    assert cal.status_code == 200
    body = _get(client, _path(cal.get_json()["connect_url"])).get_data(as_text=True)
    assert "connect this calendar" in body
    assert "mailbox" not in body and "this key" not in body
    mail = _get(client, _issue_mail(client)).get_data(as_text=True)
    assert "connect this mailbox" in mail and "this key" not in mail
    key = _get(client, _issue_key(client)).get_data(as_text=True)
    assert "set this key" in key and "mailbox" not in key


def test_already_configured_line(client):
    keyring.set_password(ma.KEYRING_SERVICE, ADDR, "old")
    body = _get(client, _issue_mail(client)).get_data(as_text=True)
    assert "saving replaces it" in body


def test_dead_and_unknown_kind(client):
    assert _get(client, "/connect/mail/nope").status_code == 410
    assert _get(client, "/connect/other/nope").status_code == 404


@pytest.mark.parametrize("kw", [
    {"environ_overrides": {"REMOTE_ADDR": "10.0.0.5"}},
    {"base_url": "http://recall.example.ts.net"},
    {"base_url": "http://evil.test:5050"},
    {"headers": {"X-Forwarded-For": "100.64.0.1"}},
    {"headers": {"Tailscale-User-Login": "x@y"}},
])
def test_guard_blocks_get_and_post(client, kw):
    path = _issue_mail(client)
    opts = {"base_url": BASE} | kw
    assert client.get(path, **opts).status_code == 403
    assert client.post(path, data={"secret": PW}, **opts).status_code == 403
    assert cl.peek("mail", _token(path)) is not None


def test_foreign_origin_post(client, imap_ok):
    path = _issue_mail(client)
    assert _post(client, path, PW, headers={"Origin": "http://evil.test"}).status_code == 403
    assert _post(client, path, PW, headers={"Origin": BASE}).status_code == 200


def test_null_origin_from_own_form_is_accepted(client, imap_ok):
    path = _issue_mail(client)
    cross = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
    assert _post(client, path, PW, headers=cross).status_code == 403
    own = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    assert _post(client, path, PW, headers=own).status_code == 200


def test_mail_success(client, imap_ok, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="werkzeug")
    path = _issue_mail(client)
    r = _post(client, path, PW)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "INBOX" in body and "Work" in body and PW not in body
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) == PW
    assert _get(client, path).status_code == 410
    assert PW not in caplog.text


def test_mail_auth_failed_budget(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="werkzeug")
    _fail_with(monkeypatch, mail_imap.AUTH_FAILED)
    path = _issue_mail(client)
    r = _post(client, path, PW)
    assert r.status_code == 400 and "Attempts left: 2" in r.get_data(as_text=True)
    assert _post(client, path, PW).status_code == 400
    assert _post(client, path, PW).status_code == 410
    assert _get(client, path).status_code == 410
    assert keyring.get_password(ma.KEYRING_SERVICE, ADDR) is None
    assert PW not in caplog.text


def test_mail_connect_failure_is_502_and_counts(client, monkeypatch):
    _fail_with(monkeypatch, mail_imap.CONNECT_FAILED)
    path = _issue_mail(client)
    assert _post(client, path, PW).status_code == 502
    assert cl.peek("mail", _token(path))["failures"] == 1


def test_key_success_and_rejected(client, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="werkzeug")
    path = _issue_key(client)
    r = _post(client, path, BAD_KEY)
    assert r.status_code == 400 and BAD_KEY not in r.get_data(as_text=True)
    assert cl.peek("anthropic_key", _token(path))["failures"] == 1
    r = _post(client, path, KEY)
    assert r.status_code == 200 and KEY not in r.get_data(as_text=True)
    assert KEY in (tmp_path / ".env").read_text()
    assert _get(client, path).status_code == 410
    assert KEY not in caplog.text


@pytest.mark.parametrize('hdr', ['Forwarded', 'X-Real-IP'])
def test_new_proxy_headers_are_refused(client, hdr):
    assert client.get('/connect/mail/anything', headers={hdr: '198.51.100.9'}).status_code == 403


def test_null_origin_without_fetch_metadata_is_refused(client):
    r = client.post('/connect/mail/anything', data={'secret': 'x'}, headers={'Origin': 'null'})
    assert r.status_code == 403
