"""started_by provenance, the guards module and owner-session refusals (mcp-recording-live-01)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask import Flask

from app import state
from app.blueprints import recording as rec_bp
from app.blueprints.recording import recording_bp
from app.core import auth
from app.core.error_codes import ERROR_CODES
from app.core.i18n import tr
from app.services.recording import guards
from app.services.recording.service import RecordingService, SessionNotFoundError
from app.services.recording.session_store import SessionStore
from tests.test_recording_blueprint import _install, _watcher_with_prompt

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


# ---------------------------------------------------------------- guard table

@pytest.mark.parametrize('actor, label, expected', [
    ('ui', 'mcp', 'ui'),
    ('ui', None, 'ui'),
    ('unknown', 'mcp', 'mcp'),
    ('unknown', 'hook', 'hook'),
    ('unknown', 'curl', 'unknown'),
    ('unknown', None, 'unknown'),
    ('unknown', 'ui', 'unknown'),
])
def test_started_by_for(actor, label, expected):
    assert guards.started_by_for(actor, label) == expected


@pytest.mark.parametrize('manifest, owner', [
    (None, True),
    ({}, True),
    ({'started_by': 'ui'}, True),
    ({'started_by': 'call_prompt'}, True),
    ({'started_by': 'auto_call'}, True),
    ({'started_by': 'mcp'}, False),
    ({'started_by': 'hook'}, False),
    ({'started_by': 'unknown'}, False),
])
def test_is_owner_session(manifest, owner):
    assert guards.is_owner_session(manifest) is owner


@pytest.mark.parametrize('manifest, live, elapsed, expected', [
    ({'started_by': 'ui'}, False, 999.0, 'owner'),
    ({}, True, 1.0, 'owner'),
    ({'started_by': 'mcp'}, False, 999.0, 'not_live'),
    ({'started_by': 'mcp'}, True, 300.0, 'too_old'),
    ({'started_by': 'mcp'}, True, 299.9, None),
    ({'started_by': 'unknown'}, True, 0.0, None),
])
def test_cancel_own_check(manifest, live, elapsed, expected):
    assert guards.cancel_own_check(manifest, live, elapsed) == expected


def test_error_codes_registered():
    assert ERROR_CODES['OWNER_SESSION'] == 403
    assert ERROR_CODES['NOTIFY_UNAVAILABLE'] == 503
    assert ERROR_CODES['CANCEL_WINDOW_CLOSED'] == 409


# ---------------------------------------------------------------- manifest + service

def test_store_create_writes_started_by(tmp_path: Path):
    store = SessionStore(tmp_path / 's')
    assert store.create('rec_a', 48000, 2, mic_device_name='m')['started_by'] == 'ui'
    store.create('rec_b', 48000, 2, mic_device_name='m', started_by='mcp')
    assert store.read('rec_b')['started_by'] == 'mcp'


def test_service_start_passes_started_by(tmp_path: Path, monkeypatch):
    store = SessionStore(tmp_path / 's')
    svc = RecordingService(store=store, sse_broker=None)
    seen = {}

    class _Stop(Exception):
        pass

    def spy(**kw):
        seen.update(kw)
        raise _Stop()

    monkeypatch.setattr(store, 'create', spy)
    with pytest.raises(_Stop):
        svc.start(mic_device_index=1, started_by='hook')
    assert seen['started_by'] == 'hook'


def test_get_state_exposes_provenance(tmp_path: Path):
    store = SessionStore(tmp_path / 's')
    svc = RecordingService(store=store, sse_broker=None)
    store.create('rec_old', 48000, 2, mic_device_name='m')
    store.modify('rec_old', lambda m: m.pop('started_by'))
    st = svc.get_state('rec_old')
    assert st['started_by'] == 'ui' and st['max_duration_min'] is None and st['expires_at'] is None
    store.create('rec_new', 48000, 2, mic_device_name='m', started_by='mcp')
    store.modify('rec_new', lambda m: m.update(max_duration_min=180, expires_at='2026-10-25T12:00:00Z'))
    st = svc.get_state('rec_new')
    assert st['started_by'] == 'mcp' and st['max_duration_min'] == 180
    assert st['expires_at'] == '2026-10-25T12:00:00Z'


# ---------------------------------------------------------------- blueprint

@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / 'sessions')


@pytest.fixture
def svc(store):
    s = MagicMock()
    s.store = store
    s.start.return_value = 'rec_new'
    s.active_session_ids.return_value = []
    s.list_devices.return_value = [{'index': 1, 'name': 'TestMic', 'kind': 'mic', 'is_default': True}]
    s.retarget_system.return_value = {'source': {'mode': 'all'}, 'event': {}, 'applied_on_resume': False}
    s.get_state.return_value = {'session_id': 'rec_new', 'status': 'recording', 'is_active': True,
                                'streams': {}, 'name': None, 'auto_name': 'x'}
    return s


@pytest.fixture
def client(svc):
    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config['TESTING'] = True
    saved = (state.recording_service, state.sse_broker, state.live_transcribe_worker)
    state.recording_service = svc
    state.sse_broker = MagicMock()
    state.live_transcribe_worker = None
    yield app.test_client()
    state.recording_service, state.sse_broker, state.live_transcribe_worker = saved


START = {'mic_device_index': 1}


@pytest.mark.parametrize('headers, body, expected', [
    ({}, START, 'unknown'),
    ({'X-Recall-Actor': 'mcp'}, START, 'mcp'),
    ({'X-Recall-Actor': ' HOOK '}, START, 'hook'),
    ({'X-Recall-Actor': 'curl'}, START, 'unknown'),
    (UI, START, 'ui'),
    ({}, {**START, 'started_by': 'ui'}, 'unknown'),
    ({'X-Recall-Actor': 'ui'}, START, 'unknown'),
])
def test_start_started_by(client, svc, headers, body, expected):
    r = client.post('/api/recording/start', json=body, headers=headers)
    assert r.status_code == 200, r.get_json()
    assert svc.start.call_args.kwargs['started_by'] == expected


def test_autoprompt_record_is_call_prompt(client, svc):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    r = client.get(f"/api/recording/autoprompt/{w.pending()[0]['id']}/record")
    assert r.status_code == 302
    assert svc.start.call_args.kwargs['started_by'] == 'call_prompt'


ROUTES = [
    ('pause', None, 'pause'),
    ('resume', None, 'resume'),
    ('system-source', {'mode': 'all'}, 'retarget_system'),
]


@pytest.mark.parametrize('route, body, method', ROUTES)
@pytest.mark.parametrize('owner', ['ui', 'call_prompt'])
@pytest.mark.parametrize('lang', ['uk', 'en'])
def test_agent_refused_on_owner_session(client, svc, store, route, body, method, owner, lang):
    store.create('rec_o', 48000, 2, mic_device_name='m', started_by=owner)
    r = client.post(f'/api/recording/rec_o/{route}', json=body,
                    headers={'X-Recall-Actor': 'mcp', 'X-Recall-Lang': lang})
    assert r.status_code == 403
    j = r.get_json()
    assert j['success'] is False and j['error_code'] == 'OWNER_SESSION' and j['started_by'] == owner
    assert j['error'] == tr('capture.err.owner_session', lang)
    getattr(svc, method).assert_not_called()


@pytest.mark.parametrize('route, body, method', ROUTES)
def test_agent_refused_on_legacy_manifest(client, svc, store, route, body, method):
    store.create('rec_l', 48000, 2, mic_device_name='m')
    store.modify('rec_l', lambda m: m.pop('started_by'))
    r = client.post(f'/api/recording/rec_l/{route}', json=body)
    assert r.status_code == 403 and r.get_json()['started_by'] == 'ui'
    getattr(svc, method).assert_not_called()


@pytest.mark.parametrize('route, body, method', ROUTES)
def test_agent_session_passes(client, svc, store, route, body, method):
    store.create('rec_g', 48000, 2, mic_device_name='m', started_by='mcp')
    r = client.post(f'/api/recording/rec_g/{route}', json=body, headers={'X-Recall-Actor': 'mcp'})
    assert r.status_code == 200 and r.get_json()['success'] is True
    getattr(svc, method).assert_called_once()


@pytest.mark.parametrize('route, body, method', ROUTES)
def test_ui_never_refused(client, svc, store, route, body, method):
    store.create('rec_u', 48000, 2, mic_device_name='m', started_by='ui')
    r = client.post(f'/api/recording/rec_u/{route}', json=body, headers=UI)
    assert r.status_code == 200
    getattr(svc, method).assert_called_once()


@pytest.mark.parametrize('route, body, method', ROUTES)
def test_unknown_sid_is_404(client, svc, route, body, method):
    getattr(svc, method).side_effect = SessionNotFoundError('nope')
    r = client.post(f'/api/recording/rec_none/{route}', json=body)
    assert r.status_code == 404 and r.get_json()['error_code'] == 'NOT_FOUND'


def test_active_carries_provenance(client, svc):
    svc.active_session_ids.return_value = ['rec_a']
    svc.get_state.return_value = {'session_id': 'rec_a', 'streams': {}, 'started_by': 'mcp',
                                  'expires_at': '2026-10-25T12:00:00Z'}
    [s] = client.get('/api/recordings/active').get_json()['sessions']
    assert s['started_by'] == 'mcp' and s['expires_at'] == '2026-10-25T12:00:00Z'
    svc.get_state.return_value = {'session_id': 'rec_a', 'streams': {}}
    [s] = client.get('/api/recordings/active').get_json()['sessions']
    assert s['started_by'] == 'ui' and s['expires_at'] is None


def test_recording_kept_wrapper_delegates(client, monkeypatch):
    seen = {}

    def fake(session_id, store, autosave, db_path):
        seen['sid'] = session_id
        return True

    monkeypatch.setattr(guards, 'recording_kept', fake)
    with client.application.test_request_context():
        assert rec_bp._recording_kept('rec_k') is True
    assert seen['sid'] == 'rec_k'


# ---------------------------------------------------------------- call-prompt token routes

@pytest.mark.parametrize('actor', ['mcp', 'hook', 'ui', ''])
def test_autoprompt_record_refuses_named_agent_and_keeps_token(client, svc, actor):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    token = w.pending()[0]['id']
    r = client.get(f'/api/recording/autoprompt/{token}/record', headers={'X-Recall-Actor': actor})
    assert r.status_code == 403
    assert tr('capture.rec.page.agent_refused') in r.get_data(as_text=True)
    svc.start.assert_not_called()
    # the owner's toast button still works with the same token
    assert client.get(f'/api/recording/autoprompt/{token}/record').status_code == 302
    assert svc.start.call_args.kwargs['started_by'] == 'call_prompt'


def test_autoprompt_stop_refuses_named_agent(client, svc, monkeypatch):
    stops = []
    monkeypatch.setattr(rec_bp, 'stop_session', lambda sid, name=None: stops.append(sid))
    w, t, snap = _watcher_with_prompt([{'sid': 'rec_s', 'system_source': {'mode': 'all'},
                                        'is_paused': False, 'mic_peak': 0.0, 'sys_peak': 0.0}])
    snap.clear()
    for _ in range(25):
        w.tick()
        t[0] += 1
    [p] = [p for p in w.pending() if p['kind'] == 'stop']
    _install(client, w)
    r = client.get(f"/api/recording/autoprompt/{p['id']}/stop", headers={'X-Recall-Actor': 'mcp'})
    assert r.status_code == 403 and stops == []
    assert client.get(f"/api/recording/autoprompt/{p['id']}/stop", headers=UI).status_code == 302
    assert stops == ['rec_s']


def test_autoprompt_list_hides_tokens_from_non_ui(client):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    for headers in ({}, {'X-Recall-Actor': 'mcp'}):
        [p] = client.get('/api/recording/autoprompt', headers=headers).get_json()['prompts']
        assert 'id' not in p and 'actions' not in p
        assert p['kind'] == 'record' and p['exe'] == 'zoom.exe'
    [p] = client.get('/api/recording/autoprompt', headers=UI).get_json()['prompts']
    assert p['id'] == w.pending()[0]['id'] and p['actions']['record'].endswith('/record')


# ---------------------------------------------------------------- discard keeps the timer when refused

def test_refused_discard_keeps_duration_timer(client, monkeypatch):
    from app.services.recording import stop_control
    cancelled = []
    monkeypatch.setattr(stop_control, 'cancel_timer', lambda sid: cancelled.append(sid))
    monkeypatch.setattr(rec_bp, '_recording_kept', lambda sid: True)
    r = client.post('/api/recording/rec_k/discard', headers=UI)
    assert r.status_code == 409 and r.get_json()['error_code'] == 'RECORDING_KEPT'
    assert cancelled == []
    monkeypatch.setattr(rec_bp, '_recording_kept', lambda sid: False)
    assert client.post('/api/recording/rec_k/discard', headers=UI).status_code == 200
    assert cancelled == ['rec_k']
