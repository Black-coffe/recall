"""config-registry-profiles-03: tests for /api/ready and the headless shell/sw.js response.

A minimal Flask app with the real `system_bp` (pattern — tests/test_auth_gate.py),
`state.робота` is supplied via monkeypatch (story 02 itself, which fills
`state.робота` in app.py, is done in parallel and is not needed here).

Run:
    .venv/Scripts/python.exe -m pytest -q tests/test_ready_endpoint.py
"""
import logging
import os
import sqlite3

import pytest
from flask import Flask

from app import state as state_module
from app.blueprints.system import system_bp


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ready_state():
    """The full "all green" робота snapshot per the plan.md contract.

    `migrations` is deliberately absent — робота-contract-03 removed it from
    the contract (report S3, finding 9): the 'error' state is unreachable for it.
    """
    return {
        "database": "ok",
        "job_queue": {"bound": True, "recovered": 0},
        "whisper": {"preload": "ready", "model": "medium"},
        "embeddings": {"loaded": True},
        "telegram": "disabled",
        "boot_finished": True,
    }


@pytest.fixture()
def app_module(tmp_path):
    app = Flask(
        __name__,
        root_path=PROJECT_ROOT,
        template_folder=os.path.join(PROJECT_ROOT, 'templates'),
        static_folder=os.path.join(PROJECT_ROOT, 'static'),
    )
    db_path = tmp_path / 'ready_test.db'
    # The file must EXIST before the test and carry a schema — /api/ready and /api/health
    # no longer auto-create the DB and no longer consider an empty file usable
    # (report S3, finding 7). The schema is minimal: one table `schema_versions`
    # with version >= 1, without importing `app/db/migrations.py`. Tests of
    # a missing/empty DB substitute DATABASE separately.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        'CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, '
        'applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, description TEXT)'
    )
    conn.execute("INSERT INTO schema_versions (version, description) VALUES (1, 'init')")
    conn.commit()
    conn.close()
    app.config['DATABASE'] = str(db_path)
    app.config['PROFILE'] = 'desktop'
    app.config['HEADLESS'] = False
    app.register_blueprint(system_bp)
    return app


@pytest.fixture()
def client(app_module):
    return app_module.test_client()


class TestReadyEndpoint:
    def test_no_робота_state_is_503(self, client, monkeypatch):
        """Old `state` without робота → 503, checks={}, error field, no exception."""
        monkeypatch.delattr(state_module, 'робота', raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 503
        data = r.get_json()
        assert data['ready'] is False
        assert data['checks'] == {}
        assert data['error'] == 'ready-state not initialised'

    def test_pending_state_is_503(self, client, monkeypatch):
        """Default pending/False values → not ready, 503."""
        pending = {
            "database": "pending",
            "job_queue": {"bound": False, "recovered": 0},
            "whisper": {"preload": "loading", "model": None},
            "embeddings": {"loaded": False},
            "telegram": "starting",
            "boot_finished": False,
        }
        monkeypatch.setattr(state_module, 'робота', pending, raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 503
        data = r.get_json()
        assert data['ready'] is False
        assert 'error' not in data

    def test_ready_state_is_200(self, client, monkeypatch):
        """All components ready → 200, ready=true; SELECT 1 overrides database='ok'."""
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 200
        data = r.get_json()
        assert data['ready'] is True
        assert data['checks']['database'] == 'ok'
        assert set(('ready', 'profile', 'version', 'checks')) <= set(data.keys())

    def test_whisper_preload_disabled_still_ready(self, client, monkeypatch):
        """RECALL_PRELOAD_WHISPER=0 → preload='disabled' also counts as ready."""
        snap = _ready_state()
        snap['whisper'] = {"preload": "disabled", "model": None}
        monkeypatch.setattr(state_module, 'робота', snap, raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 200
        assert r.get_json()['ready'] is True

    def test_job_queue_not_bound_is_not_ready(self, client, monkeypatch):
        """A single unsatisfied criterion of the broken rule = 503."""
        snap = _ready_state()
        snap['job_queue'] = {"bound": False, "recovered": 0}
        monkeypatch.setattr(state_module, 'робота', snap, raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['ready'] is False

    def test_boot_not_finished_is_not_ready(self, client, monkeypatch):
        """boot_finished=False — everything else green, but boot is not finished yet (finding 9)."""
        snap = _ready_state()
        snap['boot_finished'] = False
        monkeypatch.setattr(state_module, 'робота', snap, raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['ready'] is False

    def test_migrations_key_absent_from_contract(self, client, monkeypatch):
        """The contract (робота-contract-03) no longer carries `migrations` anywhere."""
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        data = client.get('/api/ready').get_json()
        assert 'migrations' not in data['checks']
        assert 'migrations' not in state_module.робота

    def test_missing_database_file_is_error_and_not_created(self, app_module, tmp_path, monkeypatch):
        """A nonexistent path in DATABASE → 503, database='error', the file is NOT created on disk."""
        missing_path = tmp_path / 'does_not_exist.db'
        assert not missing_path.exists()
        app_module.config['DATABASE'] = str(missing_path)
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['checks']['database'] == 'error'
        assert not missing_path.exists()

    def test_missing_database_config_key_logs_distinctly(self, app_module, caplog, monkeypatch):
        """A missing DATABASE key in config is logged differently from a blocked/nonexistent file."""
        del app_module.config['DATABASE']
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        with caplog.at_level(logging.WARNING):
            r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['checks']['database'] == 'error'
        assert any('DATABASE config is missing' in rec.message for rec in caplog.records)
        assert not any('DB check failed' in rec.message for rec in caplog.records)

    def test_bad_database_path_logs_distinctly(self, app_module, tmp_path, caplog, monkeypatch):
        """A nonexistent DB file is logged differently from a missing config key."""
        app_module.config['DATABASE'] = str(tmp_path / 'unreachable.db')
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        with caplog.at_level(logging.WARNING):
            r = client.get('/api/ready')
        assert r.status_code == 503
        assert any('DB check failed' in rec.message for rec in caplog.records)
        assert not any('DATABASE config is missing' in rec.message for rec in caplog.records)

    def test_no_exception_text_leaks_to_client(self, app_module, tmp_path, monkeypatch):
        """The response body carries no path or exception text — only a status word."""
        missing_path = tmp_path / 'secret_path_leak.db'
        app_module.config['DATABASE'] = str(missing_path)
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        body_text = client.get('/api/ready').get_data(as_text=True)
        assert str(missing_path) not in body_text
        assert 'secret_path_leak' not in body_text

    def test_empty_file_without_schema_is_error(self, app_module, tmp_path, monkeypatch):
        """A 0-byte file (a valid empty sqlite) without `schema_versions` → error, not ok."""
        empty_path = tmp_path / 'empty.db'
        empty_path.touch()
        assert empty_path.stat().st_size == 0
        app_module.config['DATABASE'] = str(empty_path)
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['checks']['database'] == 'error'

    def test_memory_database_is_error_without_traceback(self, app_module, monkeypatch):
        """`:memory:` does not fail on `as_uri()` — it gives database='error' in a controlled way."""
        app_module.config['DATABASE'] = ':memory:'
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['checks']['database'] == 'error'

    def test_health_missing_database_is_degraded_and_not_created(self, app_module, tmp_path, monkeypatch):
        """/api/health uses the same helper: does not create a file on disk."""
        missing_path = tmp_path / 'health_does_not_exist.db'
        assert not missing_path.exists()
        app_module.config['DATABASE'] = str(missing_path)
        client = app_module.test_client()
        r = client.get('/api/health')
        data = r.get_json()
        assert data['database'] == 'error'
        assert data['status'] == 'degraded'
        assert not missing_path.exists()

    def test_health_empty_file_is_degraded(self, app_module, tmp_path):
        """/api/health on a 0-byte file (no schema) is also 'error', not 'ok'."""
        empty_path = tmp_path / 'health_empty.db'
        empty_path.touch()
        app_module.config['DATABASE'] = str(empty_path)
        client = app_module.test_client()
        data = client.get('/api/health').get_json()
        assert data['database'] == 'error'
        assert data['status'] == 'degraded'

    def test_disclosure_matches_health_baseline(self, client, monkeypatch):
        """/api/ready compared against /api/health and pinned down (Area 2 of report S3)."""
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        ready_body = client.get('/api/ready').get_json()
        health_body = client.get('/api/health').get_json()
        assert set(ready_body.keys()) == {'ready', 'profile', 'version', 'checks'}
        assert set(health_body.keys()) == {
            'status', 'version', 'ffmpeg_available', 'gpu_available', 'database',
        }
        # /api/ready exposes more via checks: whisper model, telegram state,
        # job_queue counters — /api/health does not carry this.
        assert set(ready_body['checks'].keys()) == {
            'database', 'job_queue', 'whisper', 'embeddings', 'telegram', 'boot_finished',
        }


class TestHeadlessProfile:
    def test_headless_shell_root_is_404_json(self, client, app_module):
        app_module.config['HEADLESS'] = True
        r = client.get('/')
        assert r.status_code == 404
        data = r.get_json()
        assert data == {"error": "headless", "hint": "API-only profile"}

    def test_headless_deep_link_is_404_json(self, client, app_module):
        app_module.config['HEADLESS'] = True
        r = client.get('/transcript/1')
        assert r.status_code == 404
        data = r.get_json()
        assert data == {"error": "headless", "hint": "API-only profile"}

    def test_headless_sw_js_is_404_json(self, client, app_module):
        app_module.config['HEADLESS'] = True
        r = client.get('/sw.js')
        assert r.status_code == 404
        data = r.get_json()
        assert data == {"error": "headless", "hint": "API-only profile"}

    def test_headless_unknown_api_stays_404(self, client, app_module):
        """An unknown /api/... stays 404, as before (not a headless payload)."""
        app_module.config['HEADLESS'] = True
        r = client.get('/api/does-not-exist')
        assert r.status_code == 404

    def test_desktop_shell_root_renders_shell(self, client, app_module):
        app_module.config['HEADLESS'] = False
        r = client.get('/')
        assert r.status_code == 200
        assert 'class="rc-app' in r.get_data(as_text=True)

    def test_desktop_sw_js_served(self, client, app_module):
        app_module.config['HEADLESS'] = False
        r = client.get('/sw.js')
        assert r.status_code == 200
