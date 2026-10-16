"""T1.2 (REMEDIATION_PLAN Wave 1): tests for the auth gate app/core/auth.py.

Checks the T1.2 acceptance criteria:
  (a) RECALL_LOCAL_TRUSTED=0 + no key + non-localhost → 401 on /api/*.
  (b) RECALL_LOCAL_TRUSTED=0 + the correct X-Recall-Api-Key → NOT 401.
  (c) default local-trusted (RECALL_LOCAL_TRUSTED not set) + localhost
      (a plain test_client, remote_addr=127.0.0.1) → works without a key.
  (d) /api/health is open without a key even with LOCAL_TRUSTED=0 + non-localhost.

Deliberately does NOT import the whole app.py (like tests/test_endpoints_smoke.py) —
app.py calls load_dotenv() on import, which permanently appends real secrets
from .env (e.g. HF_TOKEN) to os.environ for the rest of the pytest session and breaks
tests from another module that depend on an "empty" env (observed on
tests/test_diarization_orchestrator.py::test_unavailable_service_raises —
started failing when this file ran BEFORE it because of alphabetical order).
app/core/auth.py is self-contained (Flask + os.environ, no dependencies on
the rest of app.py), so a minimal Flask application is enough to unit-test the gate.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_auth_gate.py -v
"""
import pytest
from flask import Flask, jsonify

from app.core import auth as auth_module


_NON_LOCAL_ADDR = "203.0.113.5"  # TEST-NET-3 (RFC 5737) — guaranteed not localhost


@pytest.fixture()
def app_module():
    """Minimal Flask application with a couple of /api/* routes + the gate."""
    app = Flask(__name__)

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/api/models")
    def models():
        return jsonify([])

    @app.route("/api/history")
    def history():
        return jsonify([])

    @app.route("/api/ready")
    def ready():
        return jsonify({"ready": True})

    @app.route("/")
    def shell():
        return "shell"

    auth_module.register(app)
    return app


@pytest.fixture()
def client(app_module):
    return app_module.test_client()


class TestAuthGate:
    def test_non_localhost_without_key_is_401(self, client, monkeypatch):
        """(a) RECALL_LOCAL_TRUSTED=0, no key, request not from localhost → 401."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/models",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 401
        data = r.get_json()
        assert data["success"] is False

    def test_non_localhost_with_correct_key_passes(self, client, monkeypatch):
        """(b) RECALL_LOCAL_TRUSTED=0 + the correct X-Recall-Api-Key → not 401."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.setenv("RECALL_API_KEY", "s3cr3t-test-key")
        r = client.get(
            "/api/models",
            headers={"X-Recall-Api-Key": "s3cr3t-test-key"},
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401

    def test_non_localhost_with_wrong_key_is_401(self, client, monkeypatch):
        """A wrong key is blocked all the same."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.setenv("RECALL_API_KEY", "s3cr3t-test-key")
        r = client.get(
            "/api/models",
            headers={"X-Recall-Api-Key": "wrong-key"},
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 401

    def test_non_localhost_with_bearer_auth_header_passes(self, client, monkeypatch):
        """Authorization: Bearer <key> is accepted as an alternative to the header."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.setenv("RECALL_API_KEY", "s3cr3t-test-key")
        r = client.get(
            "/api/models",
            headers={"Authorization": "Bearer s3cr3t-test-key"},
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401

    def test_default_local_trusted_localhost_works_without_key(self, client, monkeypatch):
        """(c) Default mode (RECALL_LOCAL_TRUSTED not set) + localhost
        (test_client, remote_addr=127.0.0.1 by default) → without a key."""
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/models")
        assert r.status_code != 401

    def test_health_open_even_with_local_trusted_disabled(self, client, monkeypatch):
        """(d) /api/health is open without a key even with LOCAL_TRUSTED=0
        and requests not from localhost — the health check stays available for monitoring."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/health",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 200

    def test_options_preflight_not_blocked(self, client, monkeypatch):
        """OPTIONS (CORS preflight) must not run into the auth gate even
        when LOCAL_TRUSTED=0 and the request is not from localhost."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.options(
            "/api/models",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401

    def test_spa_shell_not_gated(self, client, monkeypatch):
        """Non-/api/* paths (the SPA shell) stay accessible even with
        LOCAL_TRUSTED=0 and requests not from localhost."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401


class TestAuthGateProfileDefaults:
    """config-registry-profiles S2: the default of RECALL_LOCAL_TRUSTED depends on
    RECALL_PROFILE, until the operator sets RECALL_LOCAL_TRUSTED explicitly."""

    def test_headless_without_key_is_401_even_from_localhost(self, client, monkeypatch):
        """headless without RECALL_API_KEY and without an explicit RECALL_LOCAL_TRUSTED → default
        local-trusted "0" (fail-closed), so even localhost gets 401."""
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/history")  # test_client sends from 127.0.0.1 by default
        assert r.status_code == 401

    def test_headless_with_explicit_local_trusted_passes(self, client, monkeypatch):
        """An explicit RECALL_LOCAL_TRUSTED=1 in headless is a conscious override, respected
        (Assumption 1 of the plan) — localhost works without a key again."""
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/history")
        assert r.status_code == 200

    def test_desktop_default_still_local_trusted(self, client, monkeypatch):
        """desktop (profile not set or 'desktop') keeps the previous UX —
        the local-trusted default stays "1"."""
        monkeypatch.delenv("RECALL_PROFILE", raising=False)
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/history")
        assert r.status_code == 200

    def test_ready_open_in_headless_without_key(self, client, monkeypatch):
        """/api/ready stays without auth in headless (the same class as /api/health)."""
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/ready",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 200

    def test_ready_open_in_desktop_without_key(self, client, monkeypatch):
        """/api/ready stays without auth in the desktop profile too with LOCAL_TRUSTED=0."""
        monkeypatch.delenv("RECALL_PROFILE", raising=False)
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/ready",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 200


# ------------- proxied access (K5-04, owner 15.10.2026)

_VPS = "100.64.0.9"      # the vps-prod server in the owner's tailnet
_PHONE = "100.64.0.7"


class TestProxiedAccess:
    """Owner: "After Tailscale access is turned on, the vps-prod server will be able to open all of Recall
    without a password until you yourself close its access." — tailscale serve arrives as 127.0.0.1."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        monkeypatch.delenv("RECALL_PROXY_ALLOWED_IPS", raising=False)

    def _get(self, client, path="/api/history", **headers):
        return client.get(path, headers=headers, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})

    def test_vps_prod_through_serve_is_401_by_default(self, client):
        r = self._get(client, **{"X-Forwarded-For": _VPS,
                                 "Tailscale-User-Login": "owner@example.com"})
        assert r.status_code == 401

    def test_vps_prod_401_when_only_phone_allowed(self, client, monkeypatch):
        monkeypatch.setenv("RECALL_PROXY_ALLOWED_IPS", _PHONE)
        assert self._get(client, **{"X-Forwarded-For": _VPS}).status_code == 401

    def test_allowed_ip_passes(self, client, monkeypatch):
        monkeypatch.setenv("RECALL_PROXY_ALLOWED_IPS", f"{_VPS.replace('54', '1')}, {_PHONE}")
        assert self._get(client, **{"X-Forwarded-For": _PHONE}).status_code == 200

    def test_valid_key_passes_through_proxy(self, client, monkeypatch):
        monkeypatch.setenv("RECALL_API_KEY", "k")
        r = self._get(client, **{"X-Forwarded-For": _VPS, "X-Recall-Api-Key": "k"})
        assert r.status_code == 200

    def test_identity_header_alone_counts_as_proxied(self, client, monkeypatch):
        monkeypatch.setenv("RECALL_PROXY_ALLOWED_IPS", _PHONE)
        r = self._get(client, **{"Tailscale-User-Login": "owner@example.com"})
        assert r.status_code == 401

    def test_direct_local_unchanged_and_health_shell_open(self, client):
        assert self._get(client).status_code == 200
        assert self._get(client, "/api/health", **{"X-Forwarded-For": _VPS}).status_code == 200
        assert self._get(client, "/", **{"X-Forwarded-For": _VPS}).status_code == 200

    def test_xff_chain_is_not_trusted(self, client, monkeypatch):
        monkeypatch.setenv("RECALL_PROXY_ALLOWED_IPS", _PHONE)
        assert self._get(client, **{"X-Forwarded-For": f"{_PHONE}, {_VPS}"}).status_code == 401
        assert self._get(client, **{"X-Forwarded-For": f"{_VPS}, {_PHONE}"}).status_code == 401
