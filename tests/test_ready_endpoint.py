"""config-registry-profiles-03: тести на /api/ready і headless-відповідь shell/sw.js.

Мінімальний Flask-застосунок з реальним `system_bp` (патерн — tests/test_auth_gate.py),
`state.робота` підкладається через monkeypatch (сама історія 02, що заповнює
`state.робота` у app.py, робиться паралельно й тут не потрібна).

Запуск:
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
    """Повний "все зелено" знімок робота за контрактом plan.md.

    `migrations` навмисно відсутній — робота-contract-03 вилучив його з
    контракту (звіт S3, знахідка 9): стан 'error' у нього недосяжний.
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
    # Файл має ІСНУВАТИ до тесту й нести схему — /api/ready і /api/health
    # більше не автостворюють БД і не вважають порожній файл придатним
    # (звіт S3, знахідка 7). Схема — мінімальна: одна таблиця `schema_versions`
    # з версією >= 1, без імпорту `app/db/migrations.py`. Тести
    # відсутньої/порожньої БД підміняють DATABASE окремо.
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
        """Старий `state` без робота → 503, checks={}, error-поле, без винятку."""
        monkeypatch.delattr(state_module, 'робота', raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 503
        data = r.get_json()
        assert data['ready'] is False
        assert data['checks'] == {}
        assert data['error'] == 'робота not initialised'

    def test_pending_state_is_503(self, client, monkeypatch):
        """Дефолтні pending/False значення → не готово, 503."""
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
        """Усі компоненти готові → 200, ready=true; SELECT 1 перекриває database='ok'."""
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 200
        data = r.get_json()
        assert data['ready'] is True
        assert data['checks']['database'] == 'ok'
        assert set(('ready', 'profile', 'version', 'checks')) <= set(data.keys())

    def test_whisper_preload_disabled_still_ready(self, client, monkeypatch):
        """RECALL_PRELOAD_WHISPER=0 → preload='disabled' теж вважається готовим."""
        snap = _ready_state()
        snap['whisper'] = {"preload": "disabled", "model": None}
        monkeypatch.setattr(state_module, 'робота', snap, raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 200
        assert r.get_json()['ready'] is True

    def test_job_queue_not_bound_is_not_ready(self, client, monkeypatch):
        """Один-єдиний незадоволений критерій зваленого правила = 503."""
        snap = _ready_state()
        snap['job_queue'] = {"bound": False, "recovered": 0}
        monkeypatch.setattr(state_module, 'робота', snap, raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['ready'] is False

    def test_boot_not_finished_is_not_ready(self, client, monkeypatch):
        """boot_finished=False — усе інше зелене, але boot ще не завершений (знахідка 9)."""
        snap = _ready_state()
        snap['boot_finished'] = False
        monkeypatch.setattr(state_module, 'робота', snap, raising=False)
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['ready'] is False

    def test_migrations_key_absent_from_contract(self, client, monkeypatch):
        """Контракт (робота-contract-03) більше не несе `migrations` ніде."""
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        data = client.get('/api/ready').get_json()
        assert 'migrations' not in data['checks']
        assert 'migrations' not in state_module.робота

    def test_missing_database_file_is_error_and_not_created(self, app_module, tmp_path, monkeypatch):
        """Неіснуючий шлях у DATABASE → 503, database='error', файл на диску НЕ зʼявляється."""
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
        """Відсутній ключ DATABASE у config логується інакше, ніж заблокований/неіснуючий файл."""
        del app_module.config['DATABASE']
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        with caplog.at_level(logging.WARNING):
            r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['checks']['database'] == 'error'
        assert any('DATABASE config відсутній' in rec.message for rec in caplog.records)
        assert not any('перевірка БД не пройшла' in rec.message for rec in caplog.records)

    def test_bad_database_path_logs_distinctly(self, app_module, tmp_path, caplog, monkeypatch):
        """Неіснуючий файл БД логується інакше, ніж відсутній ключ конфіга."""
        app_module.config['DATABASE'] = str(tmp_path / 'unreachable.db')
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        with caplog.at_level(logging.WARNING):
            r = client.get('/api/ready')
        assert r.status_code == 503
        assert any('перевірка БД не пройшла' in rec.message for rec in caplog.records)
        assert not any('DATABASE config відсутній' in rec.message for rec in caplog.records)

    def test_no_exception_text_leaks_to_client(self, app_module, tmp_path, monkeypatch):
        """Тіло відповіді не несе шлях чи текст винятку — лише status-слово."""
        missing_path = tmp_path / 'secret_path_leak.db'
        app_module.config['DATABASE'] = str(missing_path)
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        body_text = client.get('/api/ready').get_data(as_text=True)
        assert str(missing_path) not in body_text
        assert 'secret_path_leak' not in body_text

    def test_empty_file_without_schema_is_error(self, app_module, tmp_path, monkeypatch):
        """0-байтовий файл (валідний порожній sqlite) без `schema_versions` → error, не ok."""
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
        """`:memory:` не падає на `as_uri()` — керовано дає database='error'."""
        app_module.config['DATABASE'] = ':memory:'
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        client = app_module.test_client()
        r = client.get('/api/ready')
        assert r.status_code == 503
        assert r.get_json()['checks']['database'] == 'error'

    def test_health_missing_database_is_degraded_and_not_created(self, app_module, tmp_path, monkeypatch):
        """/api/health використовує той самий хелпер: не створює файл на диску."""
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
        """/api/health на 0-байтовому файлі (без схеми) теж 'error', не 'ok'."""
        empty_path = tmp_path / 'health_empty.db'
        empty_path.touch()
        app_module.config['DATABASE'] = str(empty_path)
        client = app_module.test_client()
        data = client.get('/api/health').get_json()
        assert data['database'] == 'error'
        assert data['status'] == 'degraded'

    def test_disclosure_matches_health_baseline(self, client, monkeypatch):
        """/api/ready проти /api/health звірене й зафіксоване (Area 2 звіту S3)."""
        monkeypatch.setattr(state_module, 'робота', _ready_state(), raising=False)
        ready_body = client.get('/api/ready').get_json()
        health_body = client.get('/api/health').get_json()
        assert set(ready_body.keys()) == {'ready', 'profile', 'version', 'checks'}
        assert set(health_body.keys()) == {
            'status', 'version', 'ffmpeg_available', 'gpu_available', 'database',
        }
        # /api/ready розкриває більше через checks: whisper-модель, стан telegram,
        # лічильники job_queue — /api/health цього не несе.
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
        """Невідомий /api/... лишається 404, як і раніше (не headless-payload)."""
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
