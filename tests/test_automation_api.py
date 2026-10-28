"""Automation routes: /api/automation/status projection and POST /api/pipes/<name>/run."""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from flask import Flask

from app import state
from app.blueprints import automation as auto
from app.services import tg_bot
from app.services.job_queue import JobQueue
from app.services.pipes import Cron, Pipe

PIPE_NAME = "mcp-actions"


class _FakeWatcher:
    apps = frozenset({"Telegram.exe"})
    prompt_seconds = 20.0
    autostop_sec = 600.0

    def __init__(self, pending):
        self._pending = pending

    def pending(self):
        return self._pending


def _pipe(name=PIPE_NAME):
    return Pipe(name=name, schedule=Cron.parse("0 9 * * *"), tools=["weekly_digest"],
                channel="telegram", budget_usd=0.0, skip_if_empty=True, prompt="x")


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE pipe_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pipe TEXT NOT NULL, trigger TEXT NOT NULL,
        scheduled_for TEXT, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
        rendered_by TEXT, cost_usd REAL DEFAULT 0, error TEXT, chars INTEGER DEFAULT 0)""")
    conn.execute("CREATE TABLE tg_monitored_chats (chat_id INTEGER PRIMARY KEY, enabled INTEGER)")
    conn.execute("INSERT INTO tg_monitored_chats VALUES (1, 1), (2, 0)")
    conn.commit()
    conn.close()


def _insert_run(db, *, status, started, finished=None, pipe=PIPE_NAME):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO pipe_runs (pipe, trigger, started_at, finished_at, status) VALUES (?,?,?,?,?)",
                 (pipe, "manual", started.strftime("%Y-%m-%d %H:%M:%S"),
                  finished.strftime("%Y-%m-%d %H:%M:%S") if finished else None, status))
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    _create_db(db)
    pool = ThreadPoolExecutor(max_workers=2)
    slow_pool = ThreadPoolExecutor(max_workers=1)
    queue = JobQueue(executor=pool, slow_executor=slow_pool)
    monkeypatch.setattr(state, "job_queue", queue, raising=False)
    monkeypatch.setattr(auto, "load_pipes", lambda *a, **k: [_pipe()])
    monkeypatch.setattr(auto, "_listener_call", lambda *a, **k: (False, {"error": "offline"}))
    monkeypatch.setattr(tg_bot, "is_configured", lambda: True)
    app = Flask(__name__)
    app.config["DATABASE"] = db
    app.register_blueprint(auto.automation_bp)
    app.extensions.pop("call_watcher", None)
    app.extensions.pop("pipe_scheduler", None)
    client = app.test_client()
    client.app = app
    client.db = db
    client.queue = queue
    yield client
    pool.shutdown(wait=False)
    slow_pool.shutdown(wait=False)


def _status(client, query=""):
    r = client.get("/api/automation/status" + query)
    return r.status_code, r.get_json()


# --- status: sections and error code -------------------------------------------------

def test_status_returns_all_sections_by_default(env):
    code, body = _status(env)
    assert code == 200
    assert set(body["sections"]) == {"calls", "pipes", "pollers", "telegram"}


def test_status_returns_only_requested_sections(env):
    code, body = _status(env, "?sections=pollers,telegram")
    assert code == 200
    assert set(body["sections"]) == {"pollers", "telegram"}


def test_status_unknown_section_is_400_invalid_argument(env):
    code, body = _status(env, "?sections=calls,nope")
    assert code == 400
    assert body["error_code"] == "INVALID_ARGUMENT"


# --- calls projection allowlist -----------------------------------------------------

def test_calls_pending_projection_drops_ids_actions_and_endpoints(env):
    now = datetime.now().timestamp()
    created = datetime.fromtimestamp(now - 42).isoformat()
    env.app.extensions["call_watcher"] = _FakeWatcher([{
        "id": "SECRET-PROMPT-TOKEN", "kind": "record", "exe": "Telegram.exe", "label": "Telegram",
        "session_id": "sess-7", "mic_endpoint": "MIC-ENDPOINT-XYZ",
        "created_at": created,
        "actions": {"record": "http://127.0.0.1/ACTION-URL-ABC"},
    }])
    code, body = _status(env, "?sections=calls")
    assert code == 200
    pending = body["sections"]["calls"]["pending"]
    assert len(pending) == 1
    assert set(pending[0]) == {"kind", "exe", "label", "age_sec", "session_id"}
    assert pending[0]["age_sec"] >= 42
    raw = env.get("/api/automation/status?sections=calls").get_data(as_text=True)
    for leaked in ("SECRET-PROMPT-TOKEN", "MIC-ENDPOINT-XYZ", "ACTION-URL-ABC", "mic_endpoint", "actions"):
        assert leaked not in raw


def test_calls_without_watcher_is_disabled_empty(env):
    code, body = _status(env, "?sections=calls")
    assert body["sections"]["calls"] == {"enabled": False, "apps": [], "pending": []}


def test_pipes_disabled_without_scheduler_and_pollers_reflect_extensions(env):
    env.app.extensions["brief_buttons"] = object()
    _, body = _status(env, "?sections=pipes,pollers")
    assert body["sections"]["pipes"]["enabled"] is False
    assert body["sections"]["pipes"]["pipes"][0]["name"] == PIPE_NAME
    assert body["sections"]["pollers"] == {"scheduler": False, "brief_buttons": True, "mail": False,
                                           "calendar": False}


def test_pipes_section_reports_last_run_and_cooldown(env):
    now = datetime.now()
    _insert_run(env.db, status="ok", started=now - timedelta(minutes=1), finished=now - timedelta(minutes=1))
    _, body = _status(env, "?sections=pipes")
    entry = body["sections"]["pipes"]["pipes"][0]
    assert entry["last_run"]["status"] == "ok"
    assert entry["cooldown_sec"] > 0


def test_telegram_section_counts_and_offline_listener(env):
    _, body = _status(env, "?sections=telegram")
    assert body["sections"]["telegram"] == {"listener_online": False, "monitored": 2, "enabled": 1,
                                            "bot_configured": True}


# --- POST /api/pipes/<name>/run ------------------------------------------------------

def _enable_scheduler(env):
    env.app.extensions["pipe_scheduler"] = object()


def test_run_unknown_pipe_is_404_not_found(env):
    _enable_scheduler(env)
    r = env.post("/api/pipes/nope/run")
    assert r.status_code == 404
    assert r.get_json()["error_code"] == "NOT_FOUND"


def test_run_without_scheduler_is_409_pipes_disabled(env):
    r = env.post(f"/api/pipes/{PIPE_NAME}/run")
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "PIPES_DISABLED"


def test_run_unconfigured_bot_is_503_not_configured(env, monkeypatch):
    _enable_scheduler(env)
    monkeypatch.setattr(tg_bot, "is_configured", lambda: False)
    r = env.post(f"/api/pipes/{PIPE_NAME}/run")
    assert r.status_code == 503
    assert r.get_json()["error_code"] == "NOT_CONFIGURED"


def test_run_during_cooldown_is_429_with_retry_after(env):
    _enable_scheduler(env)
    now = datetime.now()
    _insert_run(env.db, status="ok", started=now - timedelta(minutes=1), finished=now - timedelta(minutes=1))
    r = env.post(f"/api/pipes/{PIPE_NAME}/run")
    body = r.get_json()
    assert r.status_code == 429
    assert body["error_code"] == "COOLDOWN"
    assert 0 < body["retry_after_sec"] <= 600


def test_run_with_fresh_running_row_is_409_already_running(env):
    _enable_scheduler(env)
    _insert_run(env.db, status="running", started=datetime.now() - timedelta(minutes=1))
    r = env.post(f"/api/pipes/{PIPE_NAME}/run")
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "ALREADY_RUNNING"


def test_run_with_active_job_is_409_with_job_id(env, monkeypatch):
    _enable_scheduler(env)
    import threading
    gate = threading.Event()
    monkeypatch.setattr(auto, "run_pipe", lambda *a, **k: gate.wait(5) or {"status": "ok"})
    first = env.post(f"/api/pipes/{PIPE_NAME}/run")
    assert first.status_code == 202
    r = env.post(f"/api/pipes/{PIPE_NAME}/run")
    gate.set()
    assert r.status_code == 409
    body = r.get_json()
    assert body["error_code"] == "ALREADY_RUNNING"
    assert body["job_id"] == first.get_json()["job_id"]


def _wait_done(queue, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = queue.get(job_id)
        if job is not None and job.state not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_run_accepted_submits_pipe_run_job_with_meta(env, monkeypatch):
    _enable_scheduler(env)
    monkeypatch.setattr(auto, "run_pipe", lambda *a, **k: {"id": 5, "status": "ok"})
    r = env.post(f"/api/pipes/{PIPE_NAME}/run")
    body = r.get_json()
    assert r.status_code == 202
    assert body["success"] is True and body["created"] is True and body["poll"] == "get_job_status"
    job = _wait_done(env.queue, body["job_id"])
    assert job.kind == "pipe_run"
    assert job.meta["pipe"] == PIPE_NAME
    assert job.meta["trigger"] == "mcp"
    assert isinstance(job.meta["actor"], str)


def test_job_calls_run_pipe_with_fixed_recipient_and_stores_result(env, monkeypatch):
    _enable_scheduler(env)
    calls = []

    def fake_run_pipe(pipe, **kwargs):
        calls.append((pipe, kwargs))
        return {"id": 9, "pipe": pipe.name, "status": "ok", "trigger": kwargs["trigger"]}

    monkeypatch.setattr(auto, "run_pipe", fake_run_pipe)
    body = env.post(f"/api/pipes/{PIPE_NAME}/run").get_json()
    job = _wait_done(env.queue, body["job_id"])
    assert len(calls) == 1
    pipe, kwargs = calls[0]
    assert pipe.name == PIPE_NAME
    assert kwargs["db_path"] == env.db
    assert kwargs["deliver"] is tg_bot.send
    assert kwargs["buttons"] is True
    assert kwargs["trigger"] == "mcp"
    assert job.result == {"id": 9, "pipe": PIPE_NAME, "status": "ok", "trigger": "mcp"}
