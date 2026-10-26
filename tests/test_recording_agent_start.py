"""Agent-class start and the background stop (mcp-recording-live-04).

An agent start validates everything, shows the start toast and only then opens a stream;
no toast → 503 NOTIFY_UNAVAILABLE with nothing written. The duration limit is validated,
written to the manifest and armed on a timer; the copilot is clamped to local-only.
POST /stop answers at once and reports a repeat as ``already``.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask import Flask

from app import state
from app.blueprints.recording import recording_bp
from app.core import auth
from app.db.migrations import init_database
from app.services.copilot.service import CopilotService
from app.services.recording import stop_control
from app.services.recording.service import RecordingService
from app.services.recording.session_store import SessionStore

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}
MCP = {'X-Recall-Actor': 'MCP'}


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / 'sessions')


@pytest.fixture
def svc(store):
    s = MagicMock()
    s.store = store
    s._max_active = 2
    s.active_session_ids.return_value = []
    s.list_devices.return_value = [
        {'index': 1, 'name': 'TestMic', 'kind': 'mic', 'is_default': True},
        {'index': 5, 'name': 'OtherMic', 'kind': 'mic', 'is_default': False},
    ]
    live: set[str] = set()
    s.live = live

    def start(**kw):
        sid = f'rec_{len(live) + s.start.call_count}'
        store.create(sid, 48000, 1, mic_device_name=kw.get('mic_device_name'),
                     started_by=kw['started_by'])
        live.add(sid)
        return sid

    s.start.side_effect = start
    s.is_active.side_effect = lambda sid: sid in live
    s.get_state.side_effect = RecordingService(store=store, sse_broker=None).get_state
    return s


@pytest.fixture
def client(svc, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config['TESTING'] = True
    monkeypatch.setattr(state, 'recording_service', svc)
    monkeypatch.setattr(state, 'sse_broker', MagicMock())
    monkeypatch.setattr(state, 'live_transcribe_worker', None)
    monkeypatch.setattr(state, 'copilot_service', None)
    return app.test_client()


def _start(client, body, headers=MCP):
    return client.post('/api/recording/start', json=body, headers=headers)


def _wait_stopped(sid, timeout=2.0):
    deadline = time.time() + timeout
    while stop_control.is_stopping(sid) and time.time() < deadline:
        time.sleep(0.01)
    assert not stop_control.is_stopping(sid)


# ---------------------------------------------------------------- toast before start

def test_agent_start_shows_toast_then_starts_with_limit(client, svc, store, notice_stub):
    r = _start(client, {'mic': 'default'})
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()['session_id']
    assert len(notice_stub.calls) == 1
    kw = svc.start.call_args.kwargs
    assert kw['mic_device_index'] == 1 and kw['mic_device_name'] == 'TestMic'
    assert kw['started_by'] == 'mcp'
    mf = store.read(sid)
    assert mf['max_duration_min'] == 180 and mf['expires_at']
    assert sid in stop_control._timers


def test_notice_down_refuses_and_writes_nothing(client, svc, store, notice_down):
    r = _start(client, {'mic': 'default'})
    assert r.status_code == 503
    assert r.get_json()['error_code'] == 'NOTIFY_UNAVAILABLE'
    assert len(notice_down.calls) == 1
    svc.start.assert_not_called()
    assert not any(store.base_dir.iterdir())
    assert svc.live == set()
    assert stop_control._timers == {}


@pytest.mark.parametrize('headers', [UI, None], ids=['ui_token', 'k2_prompt'])
def test_owner_start_posts_no_toast_and_no_limit(client, svc, store, notice_stub, headers):
    if headers is None:
        from app.blueprints import recording as rec_bp
        with client.application.test_request_context():
            resp = rec_bp._start_session({'mic_device_index': 1}, started_by='call_prompt')
        body = resp.get_json()
    else:
        r = _start(client, {'mic_device_index': 1}, headers=headers)
        assert r.status_code == 200
        body = r.get_json()
    assert notice_stub.calls == []
    mf = store.read(body['session_id'])
    assert 'max_duration_min' not in mf and 'expires_at' not in mf
    assert stop_control._timers == {}


def test_toast_receives_what_is_recorded(client, svc, notice_stub, monkeypatch):
    from app.services.recording import screens
    monkeypatch.setattr(screens, 'preflight', lambda spec, cfg: (True, [{'monitor_index': 0}], None))
    r = _start(client, {
        'mic': 'default', 'system_source': {'mode': 'include', 'apps': ['Zoom.exe']},
        'language': 'uk', 'name': '  Weekly sync  ', 'video': {'enabled': True},
    }, headers={'X-Recall-Actor': 'Hook«x»'})
    assert r.status_code == 200, r.get_json()
    kw = notice_stub.calls[0]
    assert kw['mic_name'] == 'TestMic'
    assert kw['system_apps'] == ['zoom.exe'] and kw['system_all'] is False
    assert kw['language'] == 'uk' and kw['name'] == 'Weekly sync'
    assert kw['with_screen'] is True
    assert kw['claimed_label'] == 'hookx'
    assert svc.store.read(r.get_json()['session_id'])['name'] == 'Weekly sync'


def test_toast_all_system_without_screen(client, notice_stub):
    r = _start(client, {'mic': None, 'system_device_index': 2})
    assert r.status_code == 200, r.get_json()
    kw = notice_stub.calls[0]
    assert kw['mic_name'] is None and kw['system_all'] is True and kw['system_apps'] == []
    assert kw['with_screen'] is False and kw['language'] == 'auto'


def test_video_alone_is_valid_with_server_defaults(client, svc, notice_stub, monkeypatch):
    from app.services.recording import screens
    monkeypatch.setattr(screens, 'preflight', lambda spec, cfg: (True, [{'monitor_index': 0}], None))
    r = _start(client, {'video': {'enabled': True}})
    assert r.status_code == 200, r.get_json()
    kw = svc.start.call_args.kwargs
    assert kw['mic_device_index'] == 1 and kw['system_device_index'] is None
    assert notice_stub.calls[0]['with_screen'] is True


def test_with_screen_without_tracks_captures_the_primary_monitor(client, svc, notice_stub, monkeypatch):
    # The MCP `with_screen` flag sends no tracks; preflight here behaves like the real one
    # (it only sanitizes the tracks it is given), so an empty list would mean audio-only.
    from app.services.recording import screens
    monkeypatch.setattr(screens, 'enumerate_monitors', lambda: [
        {'monitor_index': 0, 'is_primary': False}, {'monitor_index': 1, 'is_primary': True}])
    monkeypatch.setattr(screens, 'preflight',
                        lambda spec, cfg: (True, list(spec.get('tracks') or []), None))
    r = _start(client, {'video': {'enabled': True}})
    assert r.status_code == 200, r.get_json()
    spec = svc.start.call_args.kwargs['video_spec']
    assert spec == {'enabled': True, 'tracks': [{'monitor_index': 1, 'mode': 'full'}]}
    assert notice_stub.calls[0]['with_screen'] is True


def test_screen_unavailable_toast_does_not_promise_screen(client, svc, notice_stub, monkeypatch):
    from app.services.recording import screens
    monkeypatch.setattr(screens, 'enumerate_monitors', lambda: [{'monitor_index': 0, 'is_primary': True}])
    monkeypatch.setattr(screens, 'preflight', lambda spec, cfg: (False, [], 'no ddagrab'))
    r = _start(client, {'video': {'enabled': True}})
    assert r.status_code == 200, r.get_json()
    assert svc.start.call_args.kwargs['video_spec'] is None
    assert notice_stub.calls[0]['with_screen'] is False


def test_system_mode_all_records_the_default_loopback(client, svc, notice_stub):
    svc.list_devices.return_value = [
        {'index': 1, 'name': 'TestMic', 'kind': 'mic', 'is_default': True},
        {'index': 6, 'name': 'OtherOut', 'kind': 'loopback', 'is_default': False},
        {'index': 7, 'name': 'Speakers', 'kind': 'loopback', 'is_default': True},
    ]
    r = _start(client, {'mic': None, 'system_source': {'mode': 'all', 'apps': []}})
    assert r.status_code == 200, r.get_json()
    kw = svc.start.call_args.kwargs
    assert kw['system_device_index'] == 7 and kw['system_device_name'] == 'Speakers'
    assert kw['mic_device_index'] is None and kw['system_source'] is None
    assert notice_stub.calls[0]['system_all'] is True


def test_system_mode_all_without_loopback_is_refused(client, svc, notice_stub):
    r = _start(client, {'mic': 'default', 'system_source': {'mode': 'all', 'apps': []}})
    assert r.status_code == 400 and r.get_json()['error_code'] == 'NO_STREAMS'
    assert notice_stub.calls == []
    svc.start.assert_not_called()


def test_default_mic_missing_and_no_system_is_no_streams(client, svc, notice_stub):
    svc.list_devices.return_value = []
    r = _start(client, {'mic': 'default'})
    assert r.status_code == 400 and r.get_json()['error_code'] == 'NO_STREAMS'
    assert notice_stub.calls == []


def test_unknown_mic_value_is_refused(client, svc, notice_stub):
    r = _start(client, {'mic': 'OtherMic'})
    assert r.status_code == 400 and r.get_json()['error_code'] == 'INVALID_ARGUMENT'
    svc.start.assert_not_called()


# ---------------------------------------------------------------- duration limit

@pytest.mark.parametrize('value', [0, 241, 'abc', True, 12.5, -1])
def test_max_duration_bounds(client, svc, notice_stub, value):
    r = _start(client, {'mic': 'default', 'max_duration_min': value})
    assert r.status_code == 400
    assert r.get_json()['error_code'] == 'INVALID_ARGUMENT'
    svc.start.assert_not_called()
    assert notice_stub.calls == []


@pytest.mark.parametrize('value', [1, 240, '30'])
def test_max_duration_accepted_and_on_state(client, store, value):
    r = _start(client, {'mic': 'default', 'max_duration_min': value})
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()['session_id']
    st = client.get(f'/api/recording/{sid}/state').get_json()['state']
    assert st['max_duration_min'] == int(value) and st['expires_at']


def test_owner_sent_limit_is_kept(client, store, notice_stub):
    r = _start(client, {'mic_device_index': 1, 'max_duration_min': 30}, headers=UI)
    assert r.status_code == 200
    assert store.read(r.get_json()['session_id'])['max_duration_min'] == 30
    assert notice_stub.calls == []


class _FakeTimer:
    made: list['_FakeTimer'] = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.cancelled, self.daemon = seconds, fn, False, False
        _FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def fake_timer(monkeypatch):
    _FakeTimer.made = []
    real = stop_control.arm_timer

    def arm(sid, minutes, **kw):
        real(sid, minutes, timer_factory=_FakeTimer, **kw)

    monkeypatch.setattr(stop_control, 'arm_timer', arm)
    return _FakeTimer


def test_expiry_stops_a_live_session(client, svc, fake_timer):
    sid = _start(client, {'mic': 'default', 'max_duration_min': 5}).get_json()['session_id']
    [t] = fake_timer.made
    assert t.seconds == 300.0
    t.fn()
    _wait_stopped(sid)
    svc.stop.assert_called_once_with(sid, name=None)


def test_expiry_does_nothing_for_a_stopped_session(client, svc, fake_timer):
    sid = _start(client, {'mic': 'default'}).get_json()['session_id']
    svc.live.discard(sid)
    fake_timer.made[0].fn()
    _wait_stopped(sid)
    svc.stop.assert_not_called()


# ---------------------------------------------------------------- copilot clamp

@pytest.mark.parametrize('key, value', [('api_enabled', False), ('budget_usd', 0), ('escalate', True)])
def test_agent_copilot_spend_keys_refused(client, svc, notice_stub, key, value):
    r = _start(client, {'mic': 'default', 'copilot': {'mode': 'light', key: value}})
    assert r.status_code == 400 and r.get_json()['error_code'] == 'INVALID_ARGUMENT'
    svc.start.assert_not_called()
    assert notice_stub.calls == []


def test_agent_copilot_clamped_to_local(client, tmp_path, monkeypatch):
    db = str(tmp_path / 'cp.db')
    init_database(db)
    cp = CopilotService(db)
    monkeypatch.setattr(state, 'copilot_service', cp)
    monkeypatch.setattr(state, 'copilot_worker', None)
    r = _start(client, {'mic': 'default', 'copilot': {'mode': 'hard', 'importance': 'high'}})
    assert r.status_code == 200, r.get_json()
    cid = r.get_json()['copilot_session_id']
    cfg = cp.get_state(cid)['config']
    assert cfg['api_enabled'] is False and cfg['budget_usd'] == 0
    assert cfg['mode'] == 'hard' and cfg['importance'] == 'high'


# ---------------------------------------------------------------- background stop

def test_stop_answers_at_once_then_already(client, svc, store):
    sid = _start(client, {'mic': 'default'}).get_json()['session_id']
    release = threading.Event()
    threads = []

    def slow_stop(session_id, name=None):
        threads.append(threading.current_thread().name)
        release.wait(5)
        svc.live.discard(session_id)
        store.update_status(session_id, 'stopping')

    svc.stop.side_effect = slow_stop
    t0 = time.monotonic()
    r = client.post(f'/api/recording/{sid}/stop', json={'name': 'X'})
    assert time.monotonic() - t0 < 0.9
    assert r.status_code == 200
    assert r.get_json() == {**r.get_json(), 'success': True, 'session_id': sid,
                            'status': 'stopping', 'already': False}
    assert threads == [f'rec-stop-{sid}']

    r2 = client.post(f'/api/recording/{sid}/stop', json={})
    assert r2.status_code == 200 and r2.get_json()['already'] is True

    release.set()
    _wait_stopped(sid)
    r3 = client.post(f'/api/recording/{sid}/stop', json={})
    assert r3.status_code == 200
    assert r3.get_json()['already'] is True and r3.get_json()['status'] == 'stopping'
    svc.stop.assert_called_once_with(sid, name='X')


def test_stop_unknown_sid_404(client):
    r = client.post('/api/recording/rec_nope/stop', json={})
    assert r.status_code == 404 and r.get_json()['error_code'] == 'NOT_FOUND'


def test_stop_session_disarms_timer(client, svc):
    sid = _start(client, {'mic': 'default'}).get_json()['session_id']
    assert sid in stop_control._timers
    from app.blueprints.recording import stop_session
    stop_session(sid)
    assert sid not in stop_control._timers


def test_discard_disarms_timer(client, svc):
    sid = _start(client, {'mic': 'default'}).get_json()['session_id']
    assert sid in stop_control._timers
    r = client.post(f'/api/recording/{sid}/discard')
    assert r.status_code == 200
    assert sid not in stop_control._timers
