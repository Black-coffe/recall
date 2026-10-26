"""Copilot routes driven by an agent (mcp-recording-live-05): owner guard, escalate refusal,
actor stamp, owner-only feedback stats, spend clamp, worker stop, reingest dedupe, generic 500s.

A light Flask app with the copilot blueprint over a temporary DB (real migrations);
`app.state` is substituted, so no app.py and no live DB.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from flask import Flask

from app.blueprints.copilot import copilot_bp
from app.core import auth
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.copilot.service import CopilotService
from app.services.recording.session_store import SessionStoreError

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}
MCP = {'X-Recall-Actor': 'mcp'}

MANIFESTS = {
    'rec-ui': {'started_by': 'ui'},
    'rec-mcp': {'started_by': 'mcp'},
}


class _Store:
    def read(self, sid):
        if sid not in MANIFESTS:
            raise SessionStoreError(f"no manifest {sid}")
        return MANIFESTS[sid]


class _Worker:
    def __init__(self):
        self.stopped, self.configs, self.started = [], [], []

    def start(self, **kw):
        self.started.append(kw)

    def stop(self, sid, wait=False):
        self.stopped.append((sid, wait))

    def update_config(self, sid, cfg):
        self.configs.append((sid, cfg))


class _Jobs:
    def __init__(self):
        self.submitted = []

    def submit(self, kind, fn, meta=None):
        self.submitted.append((kind, meta))


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / 'copilot_agent.db')
    init_database(p)
    return p


@pytest.fixture
def client(monkeypatch, db_path):
    svc = CopilotService(db_path, is_recording_active=lambda sid: False)
    worker, jobs = _Worker(), _Jobs()
    monkeypatch.setattr('app.state.copilot_service', svc)
    monkeypatch.setattr('app.state.copilot_worker', worker)
    monkeypatch.setattr('app.state.recording_service', SimpleNamespace(store=_Store()))
    monkeypatch.setattr('app.state.job_queue', jobs)
    monkeypatch.setattr('app.services.enrichment.any_available', lambda: True)
    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(copilot_bp)
    c = app.test_client()
    c.svc, c.worker, c.jobs, c.db = svc, worker, jobs, db_path
    return c


def _session(client, rec_sid, **settings):
    return client.svc.start(settings, recording_session_id=rec_sid)['copilot_session_id']


ROUTES = [
    ('settings', {'mode': 'hard'}),
    ('action', {'action': 'pin'}),
    ('stop', {}),
    ('reingest', {}),
]


# ------------------------------------------------------------- owner guard

@pytest.mark.parametrize('route,body', ROUTES)
@pytest.mark.parametrize('rec_sid', ['rec-ui', 'rec-missing', None])
def test_agent_refused_on_owner_session(client, route, body, rec_sid):
    cs = _session(client, rec_sid)
    r = client.post(f'/api/copilot/{cs}/{route}', json=body, headers=MCP)
    assert r.status_code == 403
    assert r.get_json()['error_code'] == 'OWNER_SESSION'
    assert r.get_json()['error'] == tr('capture.err.owner_session')


@pytest.mark.parametrize('route,body', ROUTES)
def test_agent_passes_on_agent_session(client, route, body):
    cs = _session(client, 'rec-mcp')
    r = client.post(f'/api/copilot/{cs}/{route}', json=body, headers=MCP)
    assert r.status_code == 200, r.get_json()


@pytest.mark.parametrize('route,body', ROUTES)
@pytest.mark.parametrize('rec_sid', ['rec-ui', 'rec-mcp'])
def test_ui_never_refused(client, route, body, rec_sid):
    cs = _session(client, rec_sid)
    r = client.post(f'/api/copilot/{cs}/{route}', json=body, headers=UI)
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------- action: escalate + actor

def _payloads(db_path):
    with get_db_connection(db_path) as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM copilot_events WHERE kind = 'operator_action' ORDER BY id")]


def test_escalate_from_agent_is_bad_action(client):
    cs = _session(client, 'rec-mcp')
    r = client.post(f'/api/copilot/{cs}/action', json={'action': 'escalate', 'event_id': 1},
                    headers=MCP)
    assert r.status_code == 400
    assert r.get_json()['error_code'] == 'BAD_ACTION'
    assert _payloads(client.db) == []


def test_escalate_from_ui_is_logged(client):
    cs = _session(client, 'rec-ui')
    r = client.post(f'/api/copilot/{cs}/action', json={'action': 'escalate'}, headers=UI)
    assert r.status_code == 200


@pytest.mark.parametrize('headers,expected', [
    (UI, 'ui'),
    (MCP, 'mcp'),
    ({'X-Recall-Actor': 'hook'}, 'hook'),
    ({'X-Recall-Actor': 'ui'}, 'unknown'),
    ({}, 'unknown'),
])
def test_actor_stamped_and_client_value_overwritten(client, headers, expected):
    cs = _session(client, 'rec-mcp')
    r = client.post(f'/api/copilot/{cs}/action',
                    json={'action': 'thumbs_up', 'payload': {'actor': 'ui', 'note': 'x'}},
                    headers=headers)
    assert r.status_code == 200
    p = _payloads(client.db)[-1]
    assert p['actor'] == expected
    assert p['note'] == 'x'


# ------------------------------------------------------------- feedback stats

def test_feedback_stats_count_owner_only(client):
    svc = client.svc
    cs = _session(client, 'rec-mcp')
    ins = svc.log_event(cs, 'insight_local', confidence=0.65, payload={'text': 'факт'})
    svc.log_action(cs, 'thumbs_up', ref_event_id=ins, payload={'actor': 'ui'})
    svc.log_action(cs, 'thumbs_up', ref_event_id=ins, payload={'actor': 'mcp'})
    svc.log_action(cs, 'thumbs_down', ref_event_id=ins)   # legacy row: no actor = owner
    stats = svc.get_feedback_stats()
    assert stats['thumbs_up'] == 1
    assert stats['thumbs_down'] == 1
    buckets = svc.get_feedback_by_confidence_bucket()['buckets']
    assert len(buckets) == 1
    assert buckets[0]['thumbs_up'] == 1
    assert buckets[0]['total'] == 2


# ------------------------------------------------------------- spend clamp

@pytest.mark.parametrize('headers', [UI, MCP])
def test_agent_session_stays_local_after_high_importance(client, headers):
    cs = _session(client, 'rec-mcp', api_enabled=False, budget_usd=0)
    r = client.post(f'/api/copilot/{cs}/settings', json={'importance': 'high'}, headers=headers)
    assert r.status_code == 200
    cfg = r.get_json()['config']
    assert cfg['importance'] == 'high'
    assert cfg['api_enabled'] is False and cfg['budget_usd'] == 0
    assert cfg['model_api'] is None
    snap = client.svc.get_state(cs)
    assert snap['api_enabled'] == 0 and snap['budget_usd'] == 0
    assert snap['config']['api_enabled'] is False


def test_owner_session_high_importance_enables_api(client):
    cs = _session(client, 'rec-ui', importance='low')
    r = client.post(f'/api/copilot/{cs}/settings', json={'importance': 'high'}, headers=UI)
    cfg = r.get_json()['config']
    assert cfg['api_enabled'] is True and cfg['budget_usd'] > 0


@pytest.mark.parametrize('headers', [UI, MCP, {}])
def test_start_on_agent_recording_is_clamped(client, headers):
    body = {'recording_session_id': 'rec-mcp', 'importance': 'high', 'api_enabled': True,
            'budget_usd': 5, 'escalate': True}
    r = client.post('/api/copilot/start', json=body, headers=headers)
    assert r.status_code == 200, r.get_json()
    cfg = r.get_json()['config']
    assert cfg['api_enabled'] is False and cfg['budget_usd'] == 0 and cfg['model_api'] is None
    snap = client.svc.get_state(r.get_json()['copilot_session_id'])
    assert snap['api_enabled'] == 0 and snap['budget_usd'] == 0


def test_start_on_owner_recording_keeps_spend(client):
    body = {'recording_session_id': 'rec-ui', 'importance': 'high', 'api_enabled': True,
            'budget_usd': 5}
    cfg = client.post('/api/copilot/start', json=body, headers=UI).get_json()['config']
    assert cfg['api_enabled'] is True and cfg['budget_usd'] == 5


@pytest.mark.parametrize('settings', [None, {'importance': 'high', 'api_enabled': True,
                                             'budget_usd': 5, 'escalate': True}])
@pytest.mark.parametrize('headers', [UI, MCP])
def test_move_onto_agent_recording_is_clamped(client, settings, headers):
    _session(client, 'rec-ui', importance='high', api_enabled=True, budget_usd=5)
    body = {'from_session_id': 'rec-ui', 'to_session_id': 'rec-mcp'}
    if settings is not None:
        body['settings'] = settings
    r = client.post('/api/copilot/move', json=body, headers=headers)
    assert r.get_json()['success'] is True, r.get_json()
    cfg = client.worker.started[-1]['config']
    assert cfg['api_enabled'] is False and cfg['budget_usd'] == 0 and cfg['model_api'] is None
    snap = client.svc.get_state(r.get_json()['copilot_session_id'])
    assert snap['api_enabled'] == 0 and snap['budget_usd'] == 0


# ------------------------------------------------------------- stop

def test_stop_stops_worker_then_ends(client):
    cs = _session(client, 'rec-mcp')
    r = client.post(f'/api/copilot/{cs}/stop', json={'transcription_id': 7}, headers=MCP)
    body = r.get_json()
    assert body['success'] is True and body['worker_stopped'] is True and body['ended'] is True
    assert client.worker.stopped == [('rec-mcp', False)]
    snap = client.svc.get_state(cs)
    assert snap['status'] == 'ended' and snap['transcription_id'] == 7


def test_stop_without_worker_still_ends(client, monkeypatch):
    monkeypatch.setattr('app.state.copilot_worker', None)
    cs = _session(client, 'rec-mcp')
    body = client.post(f'/api/copilot/{cs}/stop', json={}, headers=UI).get_json()
    assert body['worker_stopped'] is False and body['ended'] is True


def test_stop_without_recording_still_ends(client):
    cs = _session(client, None)
    body = client.post(f'/api/copilot/{cs}/stop', json={}, headers=UI).get_json()
    assert body['worker_stopped'] is False and body['ended'] is True
    assert client.worker.stopped == []


# ------------------------------------------------------------- reingest dedupe + migration

def _copilot_rows(db_path):
    with get_db_connection(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE source_type = 'copilot'").fetchone()[0]


def test_reingest_is_deduplicated(client):
    cs = _session(client, 'rec-mcp')
    first = client.post(f'/api/copilot/{cs}/reingest', headers=MCP).get_json()
    assert first['success'] is True and first['created'] is True
    snap = client.svc.get_state(cs)
    assert snap['reingest_transcription_id'] == first['transcription_id']
    assert snap['reingest_by'] == 'mcp'
    second = client.post(f'/api/copilot/{cs}/reingest', headers=UI).get_json()
    assert second == {'success': True, 'transcription_id': first['transcription_id'],
                      'created': False}
    assert _copilot_rows(client.db) == 1
    assert len(client.jobs.submitted) == 1


def test_v57_reingest_columns_are_additive_and_idempotent(tmp_path):
    db = str(tmp_path / 'v57.db')
    init_database(db)
    init_database(db)
    conn = sqlite3.connect(db)
    try:
        cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(copilot_sessions)")}
        assert cols['reingest_transcription_id'] == 'INTEGER'
        assert cols['reingest_by'] == 'TEXT'
        assert conn.execute("SELECT COUNT(*) FROM schema_versions WHERE version = 57"
                            ).fetchone()[0] == 1
    finally:
        conn.close()


# ------------------------------------------------------------- generic 500s

def test_start_and_action_500_are_generic(client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError(r'C:\secret\path.db locked')
    cs = _session(client, 'rec-ui')
    monkeypatch.setattr(client.svc, 'start', boom)
    monkeypatch.setattr(client.svc, 'log_action', boom)
    for url, body in (('/api/copilot/start', {}), (f'/api/copilot/{cs}/action', {'action': 'pin'})):
        r = client.post(url, json=body, headers=UI)
        assert r.status_code == 500
        assert r.get_json()['error'] == tr('core.error.internal')
        assert 'secret' not in r.get_data(as_text=True)
