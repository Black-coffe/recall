"""T8.2 (REMEDIATION_PLAN Wave 4): tests for request-id / correlation-id.

Checks the T8.2 acceptance criteria:
  (a) every /api/* response has an X-Request-Id header;
  (b) the id differs between requests (not a constant / not a forgotten default);
  (c) _RequestIdLogFilter (app.py) sets record.request_id from flask.g
      when there is an active request context, and "-" outside it — this very Filter
      is attached to the _file_handler/_stream_handler of the root logger, so
      request_id gets into ALL log records (app.py + blueprints/services),
      including the 401 rejections of the auth gate (app/core/auth.py), without changing
      auth.py itself.

Imports the real app.py via importlib (the same pattern as
tests/test_endpoints_smoke.py) — not via `import app`, because the `app/` package
shadows the `app.py` module by name. Skipped if the DB is not yet created
(first run), like test_endpoints_smoke.py.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_request_id.py -v
"""
import logging
import os
import sys

import pytest


@pytest.fixture(scope="module")
def app_module():
    """Import app.py once per module (slow because of Whisper init)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    db_path = os.path.join(project_root, "whisper_history.db")
    if not os.path.exists(db_path):
        pytest.skip(f"DB not found at {db_path}, request-id test skipped")

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
        # uuid4().hex[:12] — 12 hex characters.
        assert len(request_id) == 12
        int(request_id, 16)  # ValueError if not hex

    def test_x_request_id_unique_per_request(self, client):
        r1 = client.get("/api/health")
        r2 = client.get("/api/health")
        assert r1.headers["X-Request-Id"] != r2.headers["X-Request-Id"]

    def test_x_request_id_present_even_on_401(self, client, monkeypatch):
        """The auth gate (T1.2) is registered AFTER the request-id hook — even a
        401 response has X-Request-Id, so that all records related to the rejected
        request can be gathered in the logs."""
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
