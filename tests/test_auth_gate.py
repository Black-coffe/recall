"""T1.2 (REMEDIATION_PLAN Волна 1): тести на auth-гейт app/core/auth.py.

Перевіряє критерії приймання T1.2:
  (a) RECALL_LOCAL_TRUSTED=0 + без ключа + не-localhost → 401 на /api/*.
  (b) RECALL_LOCAL_TRUSTED=0 + правильний X-Recall-Api-Key → НЕ 401.
  (c) дефолт local-trusted (RECALL_LOCAL_TRUSTED не задано) + localhost
      (звичайний test_client, remote_addr=127.0.0.1) → працює без ключа.
  (d) /api/health відкритий без ключа навіть при LOCAL_TRUSTED=0 + не-localhost.

Навмисно НЕ імпортує весь app.py (як tests/test_endpoints_smoke.py) —
app.py на імпорт кличе load_dotenv(), що назавжди дописує реальні секрети
з .env (напр. HF_TOKEN) у os.environ на решту pytest-сесії й ламає
тести з іншого модуля, що залежать від "порожнього" env (спостережено на
tests/test_diarization_orchestrator.py::test_unavailable_service_raises —
почав падати, коли цей файл через алфавітний порядок виконувався ДО нього).
app/core/auth.py самодостатній (Flask + os.environ, без залежностей на
решту app.py), тож для юніт-тесту гейта досить мінімального Flask-застосунку.

Запуск:
    .venv/Scripts/python.exe -m pytest tests/test_auth_gate.py -v
"""
import pytest
from flask import Flask, jsonify

from app.core import auth as auth_module


_NON_LOCAL_ADDR = "203.0.113.5"  # TEST-NET-3 (RFC 5737) — гарантовано не localhost


@pytest.fixture()
def app_module():
    """Мінімальний Flask-застосунок з парою /api/*-роутів + гейтом."""
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
        """(a) RECALL_LOCAL_TRUSTED=0, немає ключа, запит не з localhost → 401."""
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
        """(b) RECALL_LOCAL_TRUSTED=0 + правильний X-Recall-Api-Key → не 401."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.setenv("RECALL_API_KEY", "s3cr3t-test-key")
        r = client.get(
            "/api/models",
            headers={"X-Recall-Api-Key": "s3cr3t-test-key"},
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401

    def test_non_localhost_with_wrong_key_is_401(self, client, monkeypatch):
        """Неправильний ключ так само блокується."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.setenv("RECALL_API_KEY", "s3cr3t-test-key")
        r = client.get(
            "/api/models",
            headers={"X-Recall-Api-Key": "wrong-key"},
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 401

    def test_non_localhost_with_bearer_auth_header_passes(self, client, monkeypatch):
        """Authorization: Bearer <key> приймається як альтернатива заголовку."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.setenv("RECALL_API_KEY", "s3cr3t-test-key")
        r = client.get(
            "/api/models",
            headers={"Authorization": "Bearer s3cr3t-test-key"},
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401

    def test_default_local_trusted_localhost_works_without_key(self, client, monkeypatch):
        """(c) Дефолтний режим (RECALL_LOCAL_TRUSTED не задано) + localhost
        (test_client, remote_addr=127.0.0.1 за замовчуванням) → без ключа."""
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/models")
        assert r.status_code != 401

    def test_health_open_even_with_local_trusted_disabled(self, client, monkeypatch):
        """(d) /api/health відкритий без ключа навіть при LOCAL_TRUSTED=0
        і запиті не з localhost — health-check лишається для моніторингу."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/health",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 200

    def test_options_preflight_not_blocked(self, client, monkeypatch):
        """OPTIONS (CORS preflight) не повинен впиратись у auth-гейт навіть
        коли LOCAL_TRUSTED=0 і запит не з localhost."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.options(
            "/api/models",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401

    def test_spa_shell_not_gated(self, client, monkeypatch):
        """Не-/api/* шляхи (SPA-шелл) лишаються доступними навіть при
        LOCAL_TRUSTED=0 і запиті не з localhost."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code != 401


class TestAuthGateProfileDefaults:
    """config-registry-profiles S2: дефолт RECALL_LOCAL_TRUSTED залежить від
    RECALL_PROFILE, доки оператор не задав RECALL_LOCAL_TRUSTED явно."""

    def test_headless_without_key_is_401_even_from_localhost(self, client, monkeypatch):
        """headless без RECALL_API_KEY і без явного RECALL_LOCAL_TRUSTED → дефолт
        local-trusted "0" (fail-closed), тож навіть localhost отримує 401."""
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/history")  # test_client за замовчуванням шле з 127.0.0.1
        assert r.status_code == 401

    def test_headless_with_explicit_local_trusted_passes(self, client, monkeypatch):
        """Явний RECALL_LOCAL_TRUSTED=1 у headless — свідомий override, поважається
        (Assumption 1 плану) — localhost знову працює без ключа."""
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/history")
        assert r.status_code == 200

    def test_desktop_default_still_local_trusted(self, client, monkeypatch):
        """desktop (профіль не задано або 'desktop') зберігає попередній UX —
        дефолт local-trusted лишається "1"."""
        monkeypatch.delenv("RECALL_PROFILE", raising=False)
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get("/api/history")
        assert r.status_code == 200

    def test_ready_open_in_headless_without_key(self, client, monkeypatch):
        """/api/ready лишається без auth у headless (той самий клас, що /api/health)."""
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        monkeypatch.delenv("RECALL_LOCAL_TRUSTED", raising=False)
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/ready",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 200

    def test_ready_open_in_desktop_without_key(self, client, monkeypatch):
        """/api/ready лишається без auth і у desktop-профілі при LOCAL_TRUSTED=0."""
        monkeypatch.delenv("RECALL_PROFILE", raising=False)
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/ready",
            environ_overrides={"REMOTE_ADDR": _NON_LOCAL_ADDR},
        )
        assert r.status_code == 200
