"""Approval gate for a recording's stop and cancel (mcp-recording-live story 03).

A non-``ui`` stop or cancel reaches the dialog in the middle of a call; the owner's own Stop and
Discard stay undialogued. The dialog reads the manifest and the live snapshot. Preconditions
(already stopping, owner session, cancel window, kept recording) answer before any dialog.
A fake recording service over a temp DB; no real window opens (``tests/conftest.py``).
"""
from __future__ import annotations

import time

import pytest
from flask import Flask, g

from app import state as app_state
from app.blueprints import recording as recording_mod
from app.blueprints.recording import recording_bp
from app.blueprints.transcription import transcription_bp
from app.core import approval_gate, audit_hooks, auth
from app.core.i18n import tr
from app.db.migrations import init_database
from app.repositories import mcp_actions
from app.services import approval_summaries
from app.services.recording import stop_control
from app.services.recording.service import SessionNotFoundError
from app.services.recording.session_store import SessionStoreError

SID = "rec-abc123"


class _Store:
    def __init__(self, manifests):
        self.manifests = manifests

    def read(self, sid):
        if sid not in self.manifests:
            raise SessionStoreError(sid)
        return dict(self.manifests[sid])

    def modify(self, sid, fn):
        fn(self.manifests[sid])


class _Service:
    def __init__(self, manifests, live, elapsed=90.0):
        self._store = _Store(manifests)
        self.live = set(live)
        self.elapsed = elapsed
        self.discarded = []

    @property
    def store(self):
        return self._store

    def active_session_ids(self):
        return sorted(self.live)

    def is_active(self, sid):
        return sid in self.live

    def get_state(self, sid):
        if sid not in self._store.manifests:
            raise SessionNotFoundError(sid)
        return {"session_id": sid, "status": self._store.manifests[sid]["status"],
                "is_active": sid in self.live,
                "elapsed_seconds": self.elapsed if sid in self.live else 0.0}

    def discard(self, sid):
        self.discarded.append(sid)
        self.live.discard(sid)


def _manifest(started_by="mcp", status="recording", name=None, auto_name="Дзвінок 25.10 14:00"):
    return {"session_id": SID, "status": status, "started_by": started_by, "name": name,
            "auto_name": auto_name}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "recording_gate.db")
    init_database(path)
    return path


@pytest.fixture
def audits():
    return []


@pytest.fixture
def stops(monkeypatch):
    calls = []
    monkeypatch.setattr(recording_mod, "stop_session", lambda sid, name=None: calls.append((sid, name)))
    return calls


@pytest.fixture
def service(monkeypatch):
    svc = _Service({SID: _manifest()}, live={SID})
    monkeypatch.setattr(app_state, "recording_service", svc, raising=False)
    return svc


@pytest.fixture
def client(db, tmp_path, monkeypatch, audits, stops):
    monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
    monkeypatch.setattr(app_state, "recording_autosave", None, raising=False)
    monkeypatch.setattr(app_state, "live_transcribe_worker", None, raising=False)
    monkeypatch.setattr(recording_mod, "_end_copilot_for", lambda sid: None)
    a = Flask(__name__, root_path=str(tmp_path))
    a.config.update(TESTING=True, DATABASE=db)
    a.register_blueprint(recording_bp)
    a.register_blueprint(transcription_bp)
    auth.register(a)
    approval_gate.register(a)
    audit_hooks.register(a)

    @a.after_request
    def _capture(response):
        audits.append(dict(getattr(g, "recall_audit", None) or {}))
        return response

    return a.test_client()


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    approval_gate.reset_counters()
    stop_control.reset()
    yield
    approval_gate.reset_counters()
    stop_control.reset()


def _ui():
    return {auth.UI_TOKEN_HEADER: auth.ui_token()}


def _stop(client, headers=None, json=None):
    return client.post(f"/api/recording/{SID}/stop", json=json or {}, headers=headers or {})


def _cancel(client, headers=None):
    return client.post(f"/api/recording/{SID}/discard", headers=headers or {})


def _wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ------------------------------------------------------------------ tables

def test_tables_declare_the_two_recording_actions():
    assert approval_gate.RECORDING_ALLOWED_ACTIONS == frozenset({"recording_stop", "recording_cancel_own"})
    assert {"recording_stop", "recording_cancel_own"} <= set(approval_gate.D_ACTIONS)
    assert approval_gate._CONDITIONAL_D_ROUTES["recording.stop_recording"][0] == "recording_stop"
    assert approval_gate._CONDITIONAL_D_ROUTES["recording.discard_recording"][0] == "recording_cancel_own"
    assert "recording.stop_recording" not in approval_gate._D_ROUTES
    assert "recording.discard_recording" not in approval_gate._D_ROUTES
    assert approval_summaries._KIND_TABLE["recording"] == "recording_sessions"


# ------------------------------------------------------------------ stop

def test_ui_stop_has_no_dialog(client, service, dialog_stub, stops):
    r = _stop(client, headers=_ui())
    assert r.status_code == 200
    assert dialog_stub.calls == []
    assert _wait_for(lambda: stops)


def test_agent_stop_mid_call_reaches_the_dialog_and_denial_is_403(client, service, dialog_stub, stops, audits):
    r = _stop(client, json={"name": "Секретна назва"})
    assert r.status_code == 403
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1
    assert stops == []
    assert audits[-1]["decision"] == "denied:no"


def test_agent_stop_dialog_reads_the_manifest_not_the_request(client, service, dialog_stub):
    service.elapsed = 185.0
    _stop(client, json={"name": "Назва з тіла"},
          headers={"X-Recall-Note": "Нотатка агента", "X-Recall-Actor": "mcp"})
    text = dialog_stub.calls[0][1]
    assert tr("approvals.action.recording_stop", lang="uk") in text
    assert "Дзвінок 25.10 14:00" in text
    assert SID in text
    assert tr("approvals.impact.recording_stop", lang="uk", count=3) in text
    assert tr("approvals.extra.started_by_agent", lang="uk") in text
    assert "Нотатка агента" in text
    assert "Назва з тіла" not in text


def test_cyrillic_note_from_mcp_reaches_the_dialog_intact(client, service, dialog_stub, monkeypatch):
    import mcp_server
    name = "Дзвінок з фондом «бюджет»"
    sent = {}
    monkeypatch.setattr(mcp_server, "_live_or_given", lambda sid: (SID, None))
    monkeypatch.setattr(mcp_server, "_gated", lambda *a, extra_headers=None, **kw: sent.update(extra_headers))
    mcp_server.recording_stop(SID, name=name)
    wire = sent["X-Recall-Note"]
    assert wire.isascii()   # survives the latin-1 decode a WSGI server applies to header values
    _stop(client, headers={"X-Recall-Note": wire.encode("ascii").decode("latin-1"), "X-Recall-Actor": "mcp"})
    text = dialog_stub.calls[0][1]
    assert "Дзвінок з фондом бюджет" in text
    assert "%D0" not in text and "Ð" not in text


def test_owner_session_dialog_says_owner_and_uses_the_manifest_name(client, service, dialog_stub):
    service.store.manifests[SID] = _manifest(started_by="ui", name="Планерка з фондом")
    _stop(client)
    text = dialog_stub.calls[0][1]
    assert "Планерка з фондом" in text
    assert tr("approvals.extra.started_by_owner", lang="uk") in text


def test_approved_agent_stop_runs_the_view_and_writes_one_audit_row(client, service, approve_dialogs, stops, db):
    r = _stop(client)
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    assert len(approve_dialogs.calls) == 1
    assert _wait_for(lambda: stops)
    rows = mcp_actions.query(db, limit=5)
    assert len(rows) == 1
    assert rows[0]["action"] == "recording_stop"
    assert rows[0]["decision"] == "approved"
    assert rows[0]["object_ref"] == f"recording_sessions:{SID}"


def test_other_d_action_mid_call_is_still_refused(client, service, dialog_stub, audits):
    r = client.delete("/api/history/1")
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "RECORDING_ACTIVE"
    assert dialog_stub.calls == []


def test_empty_allowed_set_refuses_the_mid_call_stop(client, service, dialog_stub, monkeypatch):
    monkeypatch.setattr(approval_gate, "RECORDING_ALLOWED_ACTIONS", frozenset())
    r = _stop(client)
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "RECORDING_ACTIVE"
    assert dialog_stub.calls == []


@pytest.mark.parametrize("case", ["not_live", "stopping"])
def test_stop_already_ending_answers_already_without_dialog_or_slot(client, service, dialog_stub, stops,
                                                                    monkeypatch, case):
    if case == "not_live":
        service.live.clear()
        service.store.manifests[SID]["status"] = "finalized"
        expected = "finalized"
    else:
        monkeypatch.setattr(stop_control, "is_stopping", lambda sid: sid == SID)
        expected = "stopping"
    for _ in range(approval_gate.D_PER_MINUTE + 1):
        r = _stop(client)
        assert r.status_code == 200
        body = r.get_json()
        assert body == {"success": True, "session_id": SID, "status": expected, "already": True}
    assert dialog_stub.calls == []
    assert stops == []


def test_stop_of_unknown_session_skips_dialog(client, service, dialog_stub):
    r = client.post("/api/recording/no-such/stop", json={})
    assert dialog_stub.calls == []
    assert r.status_code in (200, 404)


# ------------------------------------------------------------------ cancel

def test_ui_discard_has_no_dialog(client, service, dialog_stub):
    r = _cancel(client, headers=_ui())
    assert r.status_code == 200
    assert dialog_stub.calls == []
    assert service.discarded == [SID]


def test_agent_cancel_in_window_reaches_the_dialog(client, service, dialog_stub):
    service.elapsed = 42.4
    r = _cancel(client)
    assert r.status_code == 403
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    text = dialog_stub.calls[0][1]
    assert tr("approvals.action.recording_cancel_own", lang="uk") in text
    assert tr("approvals.impact.recording_cancel_own", lang="uk", count=42) in text
    assert service.discarded == []


def test_approved_agent_cancel_discards(client, service, approve_dialogs, db):
    r = _cancel(client)
    assert r.status_code == 200
    assert service.discarded == [SID]
    rows = mcp_actions.query(db, limit=5)
    assert len(rows) == 1 and rows[0]["action"] == "recording_cancel_own"


@pytest.mark.parametrize("started_by", ["ui", "call_prompt", None])
def test_cancel_of_owner_session_is_403_without_dialog(client, service, dialog_stub, started_by):
    service.store.manifests[SID] = _manifest(started_by=started_by)
    r = _cancel(client)
    assert r.status_code == 403
    assert r.get_json()["error_code"] == "OWNER_SESSION"
    assert dialog_stub.calls == []
    assert service.discarded == []


@pytest.mark.parametrize("case", ["not_live", "too_old"])
def test_cancel_outside_window_is_409_without_dialog(client, service, dialog_stub, case):
    if case == "not_live":
        service.live.clear()
    else:
        service.elapsed = 300.0
    r = _cancel(client)
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "CANCEL_WINDOW_CLOSED"
    assert dialog_stub.calls == []
    assert service.discarded == []


def test_cancel_of_kept_recording_is_409_without_dialog(client, service, dialog_stub, monkeypatch):
    class _Autosave:
        def info(self, sid):
            return {"status": "running"}

    monkeypatch.setattr(app_state, "recording_autosave", _Autosave(), raising=False)
    r = _cancel(client)
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "RECORDING_KEPT"
    assert dialog_stub.calls == []
    assert service.discarded == []


def test_cancel_of_unknown_session_skips_dialog(client, service, dialog_stub):
    client.post("/api/recording/no-such/discard")
    assert dialog_stub.calls == []


def test_english_texts_exist():
    for key in ("approvals.action.recording_stop", "approvals.action.recording_cancel_own",
                "approvals.kind.recording", "approvals.extra.started_by_owner",
                "approvals.extra.started_by_agent"):
        assert tr(key, lang="en") != key
    assert tr("approvals.impact.recording_stop", lang="en", count=3) == "3 min recorded so far; the capture ends."
    assert tr("approvals.extra.started_by_agent", lang="en") == "Started by: agent."
