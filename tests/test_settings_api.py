"""T5.1 + T5.2 (REMEDIATION_PLAN Wave 3): tests for app/blueprints/settings_api.py.

Checks the acceptance criteria:
  - Saving the key writes ONLY ANTHROPIC_API_KEY to .env (no other
    line/arbitrary env variable is appended — this is not a generic
    "write any env variable" endpoint).
  - The status endpoint never returns the key itself (only {"configured": bool}).
  - Format validation rejects obvious garbage (not "sk-ant-...", spaces,
    line breaks — injection into .env).
  - The copilot toggle writes COPILOT_ENABLED, honestly says restart_required when
    the runtime (state.copilot_service) differs from the requested state.

Deliberately does NOT import app.py (a heavy Whisper import, permanently appends real
secrets from the root .env to os.environ — see the tests/test_auth_gate.py docstring
for the same pattern). A minimal Flask application with only
settings_bp + root_path=tmp_path, so that .env is written to a temporary directory
and not to the project's real .env.

The copilot-toggle tests EXPLICITLY monkeypatch ``app.state.copilot_service``
(instead of relying on its default ``None``) — in a full pytest run
``tests/test_endpoints_smoke.py`` (alphabetically earlier) imports the real
``app.py``, which MAY actually bring up ``CopilotService`` in the same
``app.state`` singleton module → without an explicit monkeypatch these tests become
dependent on the execution order of other files.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_settings_api.py -v
"""
from __future__ import annotations

import os

import pytest
from flask import Flask

from app.blueprints.settings_api import settings_bp


@pytest.fixture()
def app_module(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path))
    app.register_blueprint(settings_bp)
    return app


@pytest.fixture()
def client(app_module):
    return app_module.test_client()


VALID_KEY = "sk-ant-api03-" + "a" * 40


class TestAnthropicKeyStatus:
    def test_status_false_when_no_key(self, client, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        r = client.get("/api/settings/anthropic-key/status")
        assert r.status_code == 200
        assert r.get_json() == {"configured": False}

    def test_status_true_when_key_set(self, client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", VALID_KEY)
        r = client.get("/api/settings/anthropic-key/status")
        assert r.get_json() == {"configured": True}

    def test_status_never_echoes_the_key(self, client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", VALID_KEY)
        r = client.get("/api/settings/anthropic-key/status")
        assert VALID_KEY not in r.get_data(as_text=True)


class TestSaveAnthropicKey:
    def test_save_writes_only_the_named_key_to_env_file(self, client, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        r = client.post("/api/settings/anthropic-key", json={"api_key": VALID_KEY})
        assert r.status_code == 200
        assert r.get_json()["success"] is True

        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text(encoding="utf-8")
        lines = [l for l in content.splitlines() if l.strip()]
        # Exactly one line, and it is precisely ANTHROPIC_API_KEY — nothing extra
        # appended (the "not a generic env writer" check).
        assert len(lines) == 1
        assert lines[0].startswith("ANTHROPIC_API_KEY=")
        assert VALID_KEY in content

    def test_save_applies_immediately_to_os_environ(self, client, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client.post("/api/settings/anthropic-key", json={"api_key": VALID_KEY})
        assert os.environ.get("ANTHROPIC_API_KEY") == VALID_KEY

    def test_save_does_not_let_caller_pick_arbitrary_env_name(self, client, monkeypatch, tmp_path):
        """The body has no `key`/`name` parameter — the endpoint is hard-pinned
        to ANTHROPIC_API_KEY. Even if a client tries to slip in another field
        name, it is simply ignored (api_key is still a valid string)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("SOME_OTHER_SECRET", raising=False)
        r = client.post(
            "/api/settings/anthropic-key",
            json={"api_key": VALID_KEY, "name": "SOME_OTHER_SECRET", "key": "SOME_OTHER_SECRET"},
        )
        assert r.status_code == 200
        env_file = tmp_path / ".env"
        content = env_file.read_text(encoding="utf-8")
        assert "SOME_OTHER_SECRET" not in content
        assert os.environ.get("SOME_OTHER_SECRET") is None

    @pytest.mark.parametrize("bad", [
        "",
        "   ",
        "not-a-key",
        "sk-ant-short",  # 5 chars after prefix — below {8,300} minimum
        "sk-ant-api03-has spaces-in-it",
        "sk-ant-api03-line1\nline2",
    ])
    def test_save_rejects_malformed_keys(self, client, bad):
        r = client.post("/api/settings/anthropic-key", json={"api_key": bad})
        assert r.status_code == 400
        assert r.get_json()["success"] is False

    def test_save_rejects_non_string_body(self, client):
        r = client.post("/api/settings/anthropic-key", json={"api_key": 12345})
        assert r.status_code == 400

    def test_response_never_echoes_the_key(self, client, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        r = client.post("/api/settings/anthropic-key", json={"api_key": VALID_KEY})
        assert VALID_KEY not in r.get_data(as_text=True)


class TestClearAnthropicKey:
    def test_clear_removes_key(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", VALID_KEY)
        client.post("/api/settings/anthropic-key", json={"api_key": VALID_KEY})  # seed the .env file
        r = client.delete("/api/settings/anthropic-key")
        assert r.status_code == 200
        assert r.get_json()["configured"] is False
        assert os.environ.get("ANTHROPIC_API_KEY") is None


class TestTestConnection:
    def test_test_connection_without_saved_key_is_400(self, client, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        r = client.post("/api/settings/anthropic-key/test")
        assert r.status_code == 400
        data = r.get_json()
        assert data["ok"] is False


class TestCopilotToggle:
    def test_toggle_on_writes_env_and_reports_restart_required_when_off_live(self, client, monkeypatch, tmp_path):
        # Explicitly fix the runtime state (instead of relying on the default None —
        # in a full pytest run another module (test_endpoints_smoke.py) may
        # have already imported app.py and actually brought up CopilotService in the same
        # app.state singleton, making the test dependent on file order).
        from app import state as recall_state
        monkeypatch.setattr(recall_state, "copilot_service", None)
        r = client.post("/api/settings/copilot/toggle", json={"enabled": True})
        assert r.status_code == 200
        data = r.get_json()
        assert data["success"] is True
        assert data["restart_required"] is True
        assert data["live_enabled"] is False

        env_file = tmp_path / ".env"
        content = env_file.read_text(encoding="utf-8")
        assert "COPILOT_ENABLED=" in content

    def test_toggle_off_matches_already_off_live_state(self, client, monkeypatch, tmp_path):
        from app import state as recall_state
        monkeypatch.setattr(recall_state, "copilot_service", None)
        r = client.post("/api/settings/copilot/toggle", json={"enabled": False})
        assert r.status_code == 200
        data = r.get_json()
        assert data["restart_required"] is False  # off requested, off live — matches

    def test_toggle_on_matches_when_service_already_live(self, client, monkeypatch):
        """The mirror case: the runtime ALREADY brought up copilot_service (e.g. after
        a previous restart with COPILOT_ENABLED=1) — a request enabled=True does not
        require a restart, because it is already consistent with the actual state."""
        from app import state as recall_state
        monkeypatch.setattr(recall_state, "copilot_service", object())
        r = client.post("/api/settings/copilot/toggle", json={"enabled": True})
        data = r.get_json()
        assert data["live_enabled"] is True
        assert data["restart_required"] is False

    def test_toggle_rejects_non_bool(self, client):
        r = client.post("/api/settings/copilot/toggle", json={"enabled": "yes"})
        assert r.status_code == 400

    def test_toggle_only_writes_copilot_enabled_key(self, client, tmp_path):
        client.post("/api/settings/copilot/toggle", json={"enabled": True})
        env_file = tmp_path / ".env"
        lines = [l for l in env_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1
        assert lines[0].startswith("COPILOT_ENABLED=")
