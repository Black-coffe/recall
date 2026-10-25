"""GET /api/automation/events on a minimal Flask app."""
import threading
import time

import pytest
from flask import Flask

from app import state
from app.blueprints import automation as auto
from app.services.recording.service import SessionNotFoundError
from app.services.sse_broker import SSEBroker


class _FakeRecording:
    def get_state(self, sid):
        if sid != "s1":
            raise SessionNotFoundError("nope")
        return {"status": "recording", "is_active": True}


@pytest.fixture
def client(monkeypatch):
    b = SSEBroker()
    monkeypatch.setattr(auto, "broker", b)
    monkeypatch.setattr(state, "recording_service", _FakeRecording(), raising=False)
    monkeypatch.setattr(auto, "_waiters", 0)
    app = Flask(__name__)
    app.register_blueprint(auto.automation_bp)
    c = app.test_client()
    c.broker = b
    return c


def test_no_loss_between_calls(client):
    b = client.broker
    r1 = client.get("/api/automation/events?channel=recording:s1&after=%s&timeout=0" % b.cursor("recording:s1")).get_json()
    assert r1["timed_out"] is True
    b.publish("recording:s1", "status", {"n": 1})
    b.publish("recording:s1", "live_segment", {"n": 2})
    r2 = client.get("/api/automation/events?channel=recording:s1&after=%s&timeout=0" % r1["next"]).get_json()
    assert [e["data"]["n"] for e in r2["events"]] == [1, 2]
    assert [e["provenance_kind"] for e in r2["events"]] == ["system", "transcript"]
    r3 = client.get("/api/automation/events?channel=recording:s1&after=%s&timeout=0" % r2["next"]).get_json()
    assert r3["events"] == []


def test_timeout_and_wake(client):
    b = client.broker
    cur = b.cursor("recording:s1")
    t0 = time.monotonic()
    r = client.get(f"/api/automation/events?channel=recording:s1&after={cur}&timeout=1").get_json()
    assert r["timed_out"] is True and time.monotonic() - t0 >= 0.9
    threading.Timer(0.2, lambda: b.publish("recording:s1", "error", {"m": 1})).start()
    t0 = time.monotonic()
    r = client.get(f"/api/automation/events?channel=recording:s1&after={cur}&timeout=5").get_json()
    assert r["timed_out"] is False and len(r["events"]) == 1
    assert time.monotonic() - t0 < 3
    assert b._subscribers == {}


def test_already_snapshot_and_errors(client):
    r = client.get("/api/automation/events?channel=recording:s1").get_json()
    assert r["already"] is True and r["state"]["status"] == "recording" and r["next"]
    r = client.get("/api/automation/events?channel=recording:zz")
    assert r.status_code == 404 and r.get_json()["error_code"] == "NOT_FOUND"
    r = client.get("/api/automation/events?channel=job:1")
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ARGUMENT"
    assert client.broker._subscribers == {}


def test_default_events_filter_and_explicit(client):
    b = client.broker
    cur = b.cursor("recording:s1")
    b.publish("recording:s1", "custom", 1)
    r = client.get(f"/api/automation/events?channel=recording:s1&after={cur}&timeout=0").get_json()
    assert r["events"] == []
    r = client.get(f"/api/automation/events?channel=recording:s1&after={cur}&events=custom&timeout=0").get_json()
    assert len(r["events"]) == 1


def test_fifth_waiter_gets_429(client, monkeypatch):
    monkeypatch.setattr(auto, "_waiters", auto.MAX_WAITERS)
    r = client.get("/api/automation/events?channel=recording:s1")
    assert r.status_code == 429 and r.get_json()["error_code"] == "TOO_MANY_WAITERS"
    assert auto._waiters == auto.MAX_WAITERS


def test_timeout_clamped(client, monkeypatch):
    class Stop(BaseException):
        pass

    seen = {}
    orig_sub = client.broker.subscribe
    cur = client.broker.cursor("system:x")

    def sub(*a, **k):
        q = orig_sub(*a, **k)

        def get(timeout=None):
            seen["t"] = timeout
            raise Stop

        q.get = get
        return q

    monkeypatch.setattr(client.broker, "subscribe", sub)
    with pytest.raises(Stop):
        client.get(f"/api/automation/events?channel=system:x&after={cur}&timeout=999")
    assert 24 < seen["t"] <= 25
    assert client.broker._subscribers == {}
