"""Host/Origin checks on direct loopback requests, and the K5 proxied branch staying as it was."""
import pytest
import pathlib

from flask import Flask, jsonify

from app.core import auth
from app.blueprints.system import system_bp

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LOOP = {"REMOTE_ADDR": "127.0.0.1"}
_TS_HOST = "recall-box.example-tailnet.ts.net"
_PHONE = "100.64.0.7"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
    monkeypatch.delenv("RECALL_API_KEY", raising=False)
    monkeypatch.setenv("RECALL_PROXY_ALLOWED_IPS", _PHONE)


@pytest.fixture()
def client():
    app = Flask(__name__, template_folder=_ROOT / 'templates', static_folder=_ROOT / 'static')

    @app.route("/api/thing", methods=["GET", "POST"])
    def thing():
        return jsonify({"ok": True})

    app.register_blueprint(system_bp)
    auth.register(app)
    return app.test_client()


def _proxied(xff):
    return {"X-Forwarded-For": xff, "Tailscale-User-Login": "owner@example.com",
            "Host": _TS_HOST, "Origin": f"https://{_TS_HOST}"}


def test_k5_proxied_allowed_ip_passes_and_shell_has_token(client):
    h = _proxied(_PHONE)
    assert client.post("/api/thing", headers=h, environ_overrides=_LOOP).status_code == 200
    r = client.get("/", headers=h, environ_overrides=_LOOP)
    assert r.status_code == 200
    assert f'content="{auth.ui_token()}"'.encode() in r.data


def test_k5_proxied_unlisted_ip_refused_as_before(client):
    h = _proxied("100.64.0.9")
    assert client.post("/api/thing", headers=h, environ_overrides=_LOOP).status_code == 401
    r = client.get("/", headers=h, environ_overrides=_LOOP)
    assert auth.ui_token().encode() not in r.data


@pytest.mark.parametrize("hdr", ["Forwarded", "X-Real-IP"])
def test_new_proxy_headers_count_as_proxied(client, hdr):
    r = client.get("/api/thing", headers={hdr: "198.51.100.9"}, environ_overrides=_LOOP)
    assert r.status_code == 401


def test_foreign_host_direct(client):
    h = {"Host": "evil.example:5050"}
    assert client.get("/api/thing", headers=h, environ_overrides=_LOOP).status_code == 401
    for path in ("/", "/library"):
        r = client.get(path, headers=h, environ_overrides=_LOOP)
        assert r.status_code == 403
        assert r.get_json()["code"] == "HOST_REJECTED"


@pytest.mark.parametrize("host", ["127.0.0.1:5050", "localhost:5050", "[::1]:5050"])
def test_loopback_hosts_pass(client, host):
    assert client.get("/api/thing", headers={"Host": host}, environ_overrides=_LOOP).status_code == 200
    assert client.get("/", headers={"Host": host}, environ_overrides=_LOOP).status_code == 200


def test_foreign_origin_post_rejected(client):
    r = client.post("/api/thing", headers={"Host": "127.0.0.1:5050", "Origin": "https://evil.example"},
                    environ_overrides=_LOOP)
    assert r.status_code == 403
    assert r.get_json()["code"] == "ORIGIN_REJECTED"


def test_matching_origin_and_no_origin_pass(client):
    h = {"Host": "127.0.0.1:5050"}
    assert client.post("/api/thing", headers={**h, "Origin": "http://127.0.0.1:5050"},
                       environ_overrides=_LOOP).status_code == 200
    assert client.post("/api/thing", headers=h, environ_overrides=_LOOP).status_code == 200


def test_null_origin_needs_fetch_metadata(client):
    h = {"Host": "127.0.0.1:5050", "Origin": "null"}
    assert client.post("/api/thing", headers=h, environ_overrides=_LOOP).status_code == 403
    assert client.post("/api/thing", headers={**h, "Sec-Fetch-Site": "same-origin"},
                       environ_overrides=_LOOP).status_code == 200
