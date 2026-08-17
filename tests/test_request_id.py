"""T8.2 (REMEDIATION_PLAN Волна 4): тести на request-id / correlation-id.

Перевіряє критерії приймання T8.2:
  (a) кожна відповідь /api/* має заголовок X-Request-Id;
  (b) id різний для різних запитів (не константа/не забутий дефолт);
  (c) _RequestIdLogFilter (app.py) проставляє record.request_id з flask.g,
      коли є активний request-контекст, і "-" поза ним — саме цей Filter
      підключений до _file_handler/_stream_handler root-логера, тож
      request_id потрапляє у ВСІ лог-записи (app.py + blueprints/services),
      включно з 401-відмовами auth-гейта (app/core/auth.py), без зміни
      самого auth.py.

Імпортує реальний app.py через importlib (той самий патерн, що
tests/test_endpoints_smoke.py) — не через `import app`, бо пакет `app/`
затіняє модуль `app.py` за іменем. Скіпається, якщо БД ще не створена
(перший запуск), як і test_endpoints_smoke.py.

Запуск:
    .venv/Scripts/python.exe -m pytest tests/test_request_id.py -v
"""
import logging
import os
import sys

import pytest


@pytest.fixture(scope="module")
def app_module():
    """Імпортуємо app.py один раз на module (довго через Whisper init)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    db_path = os.path.join(project_root, "whisper_history.db")
    if not os.path.exists(db_path):
        pytest.skip(f"БД не знайдено за шляхом {db_path}, тест request-id пропущено")

    import importlib.util
    app_py_path = os.path.join(project_root, "app.py")
    spec = importlib.util.spec_from_file_location("whisper_app_module_reqid", app_py_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["whisper_app_module_reqid"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def client(app_module):
    return app_module.app.test_client()


class TestRequestIdHeader:
    def test_x_request_id_present_on_response(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert "X-Request-Id" in r.headers
        request_id = r.headers["X-Request-Id"]
        # uuid4().hex[:12] — 12 hex-символів.
        assert len(request_id) == 12
        int(request_id, 16)  # ValueError, якщо не hex

    def test_x_request_id_unique_per_request(self, client):
        r1 = client.get("/api/health")
        r2 = client.get("/api/health")
        assert r1.headers["X-Request-Id"] != r2.headers["X-Request-Id"]

    def test_x_request_id_present_even_on_401(self, client, monkeypatch):
        """Auth-гейт (T1.2) реєструється ПІСЛЯ request-id хука — навіть
        401-відповідь має X-Request-Id, щоб можна було зібрати в логах
        усі записи, повʼязані з відмовленим запитом."""
        monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "0")
        monkeypatch.delenv("RECALL_API_KEY", raising=False)
        r = client.get(
            "/api/models",
            environ_overrides={"REMOTE_ADDR": "203.0.113.5"},
        )
        assert r.status_code == 401
        assert "X-Request-Id" in r.headers


class TestRequestIdLogFilter:
    def test_filter_reads_request_id_from_g_in_request_context(self, app_module):
        from flask import g

        filter_ = app_module._RequestIdLogFilter()
        with app_module.app.test_request_context("/api/health"):
            g.request_id = "abc123def456"
            record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
            assert filter_.filter(record) is True
            assert record.request_id == "abc123def456"

    def test_filter_defaults_to_dash_outside_request_context(self, app_module):
        filter_ = app_module._RequestIdLogFilter()
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
        assert filter_.filter(record) is True
        assert record.request_id == "-"
