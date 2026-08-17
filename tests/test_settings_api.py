"""T5.1 + T5.2 (REMEDIATION_PLAN Волна 3): тести на app/blueprints/settings_api.py.

Перевіряє критерії приймання:
  - Збереження ключа пише ТІЛЬКИ ANTHROPIC_API_KEY у .env (жоден інший
    рядок/довільна env-змінна НЕ дописується — це не generic
    "запиши будь-яку env-змінну" ендпоінт).
  - Статус-ендпоінт ніколи не повертає сам ключ (лише {"configured": bool}).
  - Валідація формату відкидає явний мусор (не "sk-ant-...", пробіли,
    переноси рядків — injection у .env).
  - Copilot-тумблер пише COPILOT_ENABLED, чесно каже restart_required, коли
    рантайм (state.copilot_service) розходиться із запитаним станом.

Навмисно НЕ імпортує app.py (важкий Whisper-імпорт, назавжди дописує реальні
секрети з кореневого .env у os.environ — див. tests/test_auth_gate.py docstring
для того самого паттерна). Мінімальний Flask-застосунок лише з
settings_bp + root_path=tmp_path, щоб .env писався у тимчасову директорію,
а не в реальний .env проєкту.

Copilot-toggle тести ЯВНО monkeypatch'ать ``app.state.copilot_service``
(замість покладатись на його дефолт ``None``) — у повному прогоні pytest
``tests/test_endpoints_smoke.py`` (алфавітно раніше) імпортує справжній
``app.py``, який МОЖЕ реально підняти ``CopilotService`` в те саме
``app.state``-синглтон-модуль → без explicit monkeypatch ці тести стають
залежними від порядку виконання інших файлів.

Запуск:
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
        # Рівно один рядок, і це саме ANTHROPIC_API_KEY — нічого зайвого
        # не дописано (перевірка "не generic env-writer").
        assert len(lines) == 1
        assert lines[0].startswith("ANTHROPIC_API_KEY=")
        assert VALID_KEY in content

    def test_save_applies_immediately_to_os_environ(self, client, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client.post("/api/settings/anthropic-key", json={"api_key": VALID_KEY})
        assert os.environ.get("ANTHROPIC_API_KEY") == VALID_KEY

    def test_save_does_not_let_caller_pick_arbitrary_env_name(self, client, monkeypatch, tmp_path):
        """Body не має параметра `key`/`name` — ендпоінт жорстко прибитий
        до ANTHROPIC_API_KEY. Навіть якщо клієнт спробує підсунути інше ім'я
        поля, воно просто ігнорується (api_key все одно валідний рядок)."""
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
        # Явно фіксуємо рантайм-стан (замість покладатись на дефолт None —
        # у повному прогоні pytest інший модуль (test_endpoints_smoke.py) міг
        # уже імпортувати app.py і реально підняти CopilotService у той самий
        # app.state-синглтон, роблячи тест залежним від порядку файлів).
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
        """Дзеркальний випадок: рантайм УЖЕ підняв copilot_service (напр. після
        попереднього рестарту з COPILOT_ENABLED=1) — запит enabled=True не
        вимагає рестарту, бо вже узгоджено з фактичним станом."""
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
