"""Один живий власник ко-пілота + явний перенос (parallel-recordings-app-audio-06, C6).

Сервіс — на тимчасовій БД (реальні міграції); HTTP — легкий Flask-app з одним
блюпринтом, `app.state` підмінено (без app.py, без бойової БД).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

from app.blueprints import copilot as cp_bp
from app.blueprints.copilot import copilot_bp, end_for_recording
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.copilot.service import CopilotBusyError, CopilotService


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "copilot_owner.db")
    init_database(p)
    return p


def _rows(db_path):
    with get_db_connection(db_path) as conn:
        return {r["recording_session_id"]: r["status"] for r in conn.execute(
            "SELECT recording_session_id, status FROM copilot_sessions ORDER BY id")}


def _count(db_path):
    with get_db_connection(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM copilot_sessions").fetchone()[0]


# ------------------------------------------------------------- service

def test_start_busy_when_other_recording_live(db_path):
    svc = CopilotService(db_path, is_recording_active={"запис-А": True}.get)
    svc.start({}, recording_session_id="запис-А")
    with pytest.raises(CopilotBusyError) as ei:
        svc.start({}, recording_session_id="запис-Б")
    assert ei.value.owner_session_id == "запис-А"
    assert _count(db_path) == 1
    assert _rows(db_path) == {"запис-А": "active"}


def test_start_ends_stale_row_when_recording_gone(db_path):
    svc = CopilotService(db_path, is_recording_active={"запис-А": False}.get)
    svc.start({}, recording_session_id="запис-А")
    svc.start({}, recording_session_id="запис-Б")
    assert _rows(db_path) == {"запис-А": "ended", "запис-Б": "active"}


def test_start_without_checker_is_conservative(db_path):
    svc = CopilotService(db_path)
    svc.start({}, recording_session_id="запис-А")
    with pytest.raises(CopilotBusyError):
        svc.start({}, recording_session_id="запис-Б")
    assert _count(db_path) == 1


def test_start_same_recording_not_busy(db_path):
    svc = CopilotService(db_path)
    svc.start({}, recording_session_id="запис-А")
    svc.start({}, recording_session_id="запис-А")
    assert _count(db_path) == 2


# ------------------------------------------------------------- HTTP

class _Worker:
    def __init__(self):
        self.stopped, self.started = [], []

    def stop(self, sid, wait=False):
        self.stopped.append(sid)

    def start(self, *, copilot_session_id, recording_session_id, config):
        self.started.append(recording_session_id)


@pytest.fixture
def client(monkeypatch, db_path):
    svc = CopilotService(db_path, is_recording_active=lambda sid: True)
    worker = _Worker()
    rec = SimpleNamespace(is_active=lambda sid: sid in ("запис-А", "запис-Б"),
                          active_session_ids=lambda: ["запис-А", "запис-Б"])
    monkeypatch.setattr("app.state.copilot_service", svc)
    monkeypatch.setattr("app.state.copilot_worker", worker)
    monkeypatch.setattr("app.state.recording_service", rec)
    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(copilot_bp)
    c = app.test_client()
    c.svc, c.worker, c.rec = svc, worker, rec
    return c


def test_http_start_busy_is_409(client):
    client.svc.start({}, recording_session_id="запис-А")
    r = client.post('/api/copilot/start', json={"recording_session_id": "запис-Б"})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error_code"] == "COPILOT_BUSY"
    assert body["owner_session_id"] == "запис-А"


def test_http_move_happy_path(client, db_path):
    client.svc.start({"mode": "sales"}, recording_session_id="запис-А")
    r = client.post('/api/copilot/move',
                    json={"from_session_id": "запис-А", "to_session_id": "запис-Б"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True and body["ended_from"] is True
    assert body["recording_session_id"] == "запис-Б"
    assert _rows(db_path) == {"запис-А": "ended", "запис-Б": "active"}
    assert client.worker.stopped == ["запис-А"]
    assert client.worker.started == ["запис-Б"]


def test_http_move_start_failure(client, db_path, monkeypatch):
    client.svc.start({}, recording_session_id="запис-А")

    def boom(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(client.svc, "start", boom)
    r = client.post('/api/copilot/move',
                    json={"from_session_id": "запис-А", "to_session_id": "запис-Б"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is False
    assert body["error_code"] == "COPILOT_MOVE_FAILED"
    assert body["ended_from"] is True
    assert _rows(db_path) == {"запис-А": "ended"}


def test_http_move_from_without_copilot_404(client):
    r = client.post('/api/copilot/move',
                    json={"from_session_id": "запис-А", "to_session_id": "запис-Б"})
    assert r.status_code == 404
    assert r.get_json()["error_code"] == "NO_COPILOT"


def test_http_move_to_not_live_404(client):
    client.svc.start({}, recording_session_id="запис-А")
    r = client.post('/api/copilot/move',
                    json={"from_session_id": "запис-А", "to_session_id": "чужий"})
    assert r.status_code == 404
    assert r.get_json()["error_code"] == "NOT_FOUND"


def test_http_move_disabled_is_503(client, monkeypatch):
    monkeypatch.setattr("app.state.copilot_service", None)
    r = client.post('/api/copilot/move',
                    json={"from_session_id": "запис-А", "to_session_id": "запис-Б"})
    assert r.status_code == 503


# ------------------------------------------------------------- live-ask

@pytest.fixture
def ask_calls(monkeypatch):
    calls = []

    def fake(db_path, question, **kw):
        calls.append(kw)
        return {"available": True, "answer": "ok"}

    monkeypatch.setattr(cp_bp.live_ask, "ask_local", fake)
    return calls


def test_live_ask_ambiguous_session_409(client, ask_calls):
    r = client.post('/api/copilot/live-ask', json={"question": "що там?", "session_id": None})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error_code"] == "AMBIGUOUS_SESSION"
    assert body["sessions"] == [{"session_id": "запис-А"}, {"session_id": "запис-Б"}]
    assert ask_calls == []


def test_live_ask_single_session_resolved(client, ask_calls, monkeypatch):
    monkeypatch.setattr(client.rec, "active_session_ids", lambda: ["запис-А"])
    r = client.post('/api/copilot/live-ask', json={"question": "що там?", "scope": "archive"})
    assert r.status_code == 200
    assert r.get_json()["session_id"] == "запис-А"
    assert len(ask_calls) == 1 and ask_calls[0]["recording_active"] is True


def test_live_ask_explicit_session_unchanged(client, ask_calls):
    r = client.post('/api/copilot/live-ask',
                    json={"question": "що там?", "session_id": "запис-Б", "scope": "archive"})
    assert r.status_code == 200
    assert r.get_json()["session_id"] == "запис-Б"
    assert len(ask_calls) == 1


# ------------------------------------------------------------- end_for_recording

def test_end_for_recording(client, db_path):
    res = client.svc.start({}, recording_session_id="запис-А")
    assert end_for_recording("запис-А") == str(res["copilot_session_id"])
    assert client.worker.stopped == ["запис-А"]
    assert _rows(db_path) == {"запис-А": "ended"}
    assert end_for_recording("невідомий") is None
