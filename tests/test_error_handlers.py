"""T7.4 (REMEDIATION_PLAN Волна 4): тести на єдиний error-handler middleware
(app/core/error_handlers.py).

Перевіряє критерії приймання T7.4:
  (a) необроблений виняток у view-функції → 500 з узагальненим тілом
      (без str(exc)/internal message) + request_id.
  (b) traceback реально йде в лог через logger.exception (перевіряємо caplog).
  (c) HTTPException (напр. 404 на неіснуючому роуті) НЕ підмінюється на 500 —
      error_handlers.register() пропускає їх без змін.
  (d) конкретніший @app.errorhandler(<code>) (як 429 у app.py) лишається
      пріоритетнішим за generic @app.errorhandler(Exception).

Навмисно НЕ імпортує app.py (важкий, довгий, side-effects через load_dotenv —
той самий підхід, що tests/test_auth_gate.py) — error_handlers.py
самодостатній (Flask + logging), тож досить мінімального застосунку.

Запуск:
    .venv/Scripts/python.exe -m pytest tests/test_error_handlers.py -v
"""
import logging

import pytest
from flask import Flask, g, jsonify

from app.core import error_handlers


@pytest.fixture()
def app_module():
    """Мінімальний Flask-застосунок: request_id hook (як app.py) + error-handler."""
    app = Flask(__name__)

    @app.before_request
    def _assign_request_id():
        g.request_id = "testreqid001"

    @app.route("/api/boom")
    def boom():
        raise ValueError("something with a secret path C:\\Users\\secret\\file.db")

    @app.route("/api/ok")
    def ok():
        return jsonify({"success": True})

    @app.route("/api/not-found-explicit")
    def not_found_explicit():
        from flask import abort
        abort(404)

    error_handlers.register(app)
    return app


@pytest.fixture()
def client(app_module):
    return app_module.test_client()


class TestErrorHandlerMiddleware:
    def test_unhandled_exception_returns_generic_500(self, client):
        r = client.get("/api/boom")
        assert r.status_code == 500
        body = r.get_json()
        assert body["success"] is False
        assert "secret" not in body["error"]
        assert "C:\\Users" not in body["error"]
        assert body["error"] == error_handlers.GENERIC_MESSAGE

    def test_unhandled_exception_response_includes_request_id(self, client):
        r = client.get("/api/boom")
        body = r.get_json()
        assert body["request_id"] == "testreqid001"

    def test_unhandled_exception_logs_traceback(self, client, caplog):
        with caplog.at_level(logging.ERROR, logger="app.core.error_handlers"):
            client.get("/api/boom")
        records = [rec for rec in caplog.records if rec.name == "app.core.error_handlers"]
        assert len(records) == 1
        assert records[0].exc_info is not None
        # Traceback text should contain the real exception detail (it's fine
        # for the LOG to have it — only the client response must not).
        assert "something with a secret path" in caplog.text

    def test_healthy_route_unaffected(self, client):
        r = client.get("/api/ok")
        assert r.status_code == 200
        assert r.get_json() == {"success": True}

    def test_http_exception_passed_through_unchanged(self, client):
        """404 через flask.abort() лишається 404, не підміняється на 500."""
        r = client.get("/api/not-found-explicit")
        assert r.status_code == 404

    def test_unmatched_route_is_still_404(self, client):
        r = client.get("/api/this-route-does-not-exist")
        assert r.status_code == 404


class TestErrorHandlerPriority:
    def test_specific_code_handler_takes_priority_over_generic(self, app_module):
        """Якщо є конкретніший @app.errorhandler(400), Flask обирає його,
        а не generic @app.errorhandler(Exception) з error_handlers.py —
        це те, на що покладається app.py (@app.errorhandler(429)/(413))."""
        from werkzeug.exceptions import BadRequest

        @app_module.route("/api/bad-request-custom")
        def bad_request_custom():
            raise BadRequest("custom bad request")

        @app_module.errorhandler(400)
        def _custom_400(e):
            return jsonify({"success": False, "error": "custom-400-handler", "code": 400}), 400

        client = app_module.test_client()
        r = client.get("/api/bad-request-custom")
        assert r.status_code == 400
        assert r.get_json()["error"] == "custom-400-handler"
