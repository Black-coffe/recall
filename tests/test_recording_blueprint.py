"""Integration tests for the recording REST API (Phase 9.5).

The tests use the Flask test_client + a mock RecordingService — without
real WASAPI streams. They cover the happy path and error cases for
every endpoint.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask import Flask

from app import state
from app.blueprints import recording as recording_module
from app.blueprints.recording import recording_bp
from app.db.connection import get_db_connection


@pytest.fixture(autouse=True)
def reset_devices_cache():
    """The module-level cache must not leak between tests."""
    recording_module._devices_cache['ts'] = 0.0
    recording_module._devices_cache['data'] = []
    yield
    recording_module._devices_cache['ts'] = 0.0
    recording_module._devices_cache['data'] = []


@pytest.fixture
def fake_service():
    """A mock RecordingService with pre-configured behaviour."""
    svc = MagicMock()
    svc.list_devices.return_value = [
        {'index': 1, 'name': 'TestMic', 'channels': 1, 'default_sample_rate': 48000,
         'kind': 'mic', 'is_default': True, 'host_api_name': 'WASAPI'},
        {'index': 2, 'name': 'TestSpeakers [Loopback]', 'channels': 2,
         'default_sample_rate': 48000, 'kind': 'loopback', 'is_default': True,
         'host_api_name': 'WASAPI'},
    ]
    svc.start.return_value = 'rec_test123'
    svc.active_session_id = None
    svc.active_session_ids.return_value = []
    svc.get_state.return_value = {
        'session_id': 'rec_test123', 'status': 'recording', 'is_active': True,
        'is_paused': False, 'elapsed_seconds': 1.5, 'last_chunk_ts': 0,
        'streams': {
            'mic': {'enabled': True, 'device': 'TestMic', 'bytes': 0,
                    'error': None, 'peak': 0.5, 'rms': 0.3},
            'system': {'enabled': True, 'device': 'TestSpeakers', 'bytes': 0,
                       'error': None, 'peak': 0.4, 'rms': 0.2},
        },
        'segments_count': 1, 'name': None, 'auto_name': 'Запис ...',
    }
    return svc


@pytest.fixture
def client(fake_service):
    """A Flask test_client with a replaced recording_service."""
    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config['TESTING'] = True

    # Replace the state singletons
    original_service = state.recording_service
    original_broker = state.sse_broker
    original_live_worker = state.live_transcribe_worker
    state.recording_service = fake_service
    state.sse_broker = MagicMock()
    state.live_transcribe_worker = None

    yield app.test_client()

    state.recording_service = original_service
    state.sse_broker = original_broker
    state.live_transcribe_worker = original_live_worker


# ---------------------------------------------------------------- /devices

def test_get_devices_returns_list(client, fake_service):
    r = client.get('/api/recording/devices')
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert len(body['devices']) == 2
    assert body['devices'][0]['kind'] == 'mic'
    assert body['devices'][1]['kind'] == 'loopback'


def test_get_devices_uses_cache_on_second_call(client, fake_service):
    client.get('/api/recording/devices')
    client.get('/api/recording/devices')
    # list_devices should have been called only once (the second time — from the cache)
    assert fake_service.list_devices.call_count == 1


def test_get_devices_force_bypasses_cache(client, fake_service):
    client.get('/api/recording/devices')
    client.get('/api/recording/devices?force=1')
    assert fake_service.list_devices.call_count == 2


def test_devices_503_when_service_disabled(fake_service):
    """If recording_service is None — 503."""
    app = Flask(__name__)
    app.register_blueprint(recording_bp)

    original = state.recording_service
    state.recording_service = None
    try:
        client = app.test_client()
        r = client.get('/api/recording/devices')
        assert r.status_code == 503
        assert r.get_json()['error_code'] == 'RECORDING_DISABLED'
    finally:
        state.recording_service = original


# ---------------------------------------------------------------- /start

def test_start_returns_session_id(client, fake_service):
    r = client.post('/api/recording/start', json={
        'mic_device_index': 1, 'system_device_index': 2,
        'mic_device_name': 'Mic A', 'system_device_name': 'Sys B',
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['session_id'] == 'rec_test123'
    assert '/api/recording/rec_test123/stream' in body['sse_url']
    fake_service.start.assert_called_once()


def test_start_without_streams_returns_400(client, fake_service):
    r = client.post('/api/recording/start', json={
        'mic_device_index': None, 'system_device_index': None,
    })
    assert r.status_code == 400
    assert r.get_json()['error_code'] == 'NO_STREAMS'


def test_start_twice_then_session_limit(client, fake_service):
    """Two sessions start; the third — 409 SESSION_LIMIT with the list and the limit."""
    from app.services.recording.service import SessionConflictError
    fake_service.start.side_effect = [
        'rec_a', 'rec_b',
        SessionConflictError("limit", active=['rec_a', 'rec_b'], limit=2),
    ]
    sids = [client.post('/api/recording/start', json={'mic_device_index': 1}).get_json()['session_id']
            for _ in range(2)]
    assert sids == ['rec_a', 'rec_b']

    r = client.post('/api/recording/start', json={'mic_device_index': 1})
    assert r.status_code == 409
    body = r.get_json()
    assert body['error_code'] == 'SESSION_LIMIT'
    assert body['active'] == ['rec_a', 'rec_b']
    assert body['limit'] == 2
    assert 'WRONG_SESSION' not in r.get_data(as_text=True)


def test_start_invalid_system_source_400_before_service(client, fake_service):
    r = client.post('/api/recording/start', json={
        'mic_device_index': 1,
        'system_source': {'mode': 'exclude', 'apps': ['Zoom.exe', 'x.exe']},
    })
    assert r.status_code == 400
    assert r.get_json()['error_code'] == 'UNSUPPORTED_SOURCE_SPEC'
    fake_service.start.assert_not_called()


def test_start_include_enables_system_without_device(client, fake_service):
    """include/exclude enables the system stream even without system_device_index."""
    r = client.post('/api/recording/start', json={
        'system_source': {'mode': 'include', 'apps': ['Zoom.exe']},
    })
    assert r.status_code == 200
    spec = fake_service.start.call_args.kwargs['system_source']
    assert spec.mode == 'include' and spec.apps == ('zoom.exe',)
    assert r.get_json()['system_source']['apps'] == ['zoom.exe']


def test_start_system_source_all_keeps_device_path(client, fake_service):
    r = client.post('/api/recording/start', json={
        'system_device_index': 2, 'system_source': {'mode': 'all'},
    })
    assert r.status_code == 200
    kw = fake_service.start.call_args.kwargs
    assert kw['system_source'] is None and kw['system_device_index'] == 2
    assert r.get_json()['system_source'] == {'mode': 'all', 'apps': [], 'device_index': 2}


def test_start_copilot_busy_keeps_recording(client, fake_service, monkeypatch):
    from app.services.copilot.service import CopilotBusyError
    cp = MagicMock()
    cp.start.side_effect = CopilotBusyError('A')
    monkeypatch.setattr(state, 'copilot_service', cp)
    monkeypatch.setattr(state, 'copilot_worker', None)

    r = client.post('/api/recording/start', json={'mic_device_index': 1, 'copilot': {}})
    assert r.status_code == 200
    body = r.get_json()
    assert body['session_id'] == 'rec_test123'
    assert body['copilot'] == {'started': False, 'error_code': 'COPILOT_BUSY',
                               'owner_session_id': 'A'}


def test_start_recording_error_returns_400(client, fake_service):
    from app.services.recording.service import RecordingError
    fake_service.start.side_effect = RecordingError("Cannot open device 99")
    r = client.post('/api/recording/start', json={'mic_device_index': 99})
    assert r.status_code == 400
    assert r.get_json()['error_code'] == 'RECORDING_START_FAILED'


# ---------------------------------------------------------------- /pause

def test_pause(client, fake_service):
    r = client.post('/api/recording/rec_test/pause')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'paused'
    fake_service.pause.assert_called_once_with('rec_test')


def test_pause_404(client, fake_service):
    from app.services.recording.service import SessionNotFoundError
    fake_service.pause.side_effect = SessionNotFoundError("nope")
    r = client.post('/api/recording/rec_unknown/pause')
    assert r.status_code == 404
    assert r.get_json()['error_code'] == 'NOT_FOUND'


# ---------------------------------------------------------------- /resume

def test_resume(client, fake_service):
    r = client.post('/api/recording/rec_test/resume')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'recording'
    fake_service.resume.assert_called_once_with('rec_test')


# ---------------------------------------------------------------- /stop

def test_stop_with_name(client, fake_service):
    r = client.post('/api/recording/rec_test/stop', json={'name': 'Зустріч'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['status'] == 'stopping'
    fake_service.stop.assert_called_once_with('rec_test', name='Зустріч')


def test_stop_without_name(client, fake_service):
    r = client.post('/api/recording/rec_test/stop', json={})
    assert r.status_code == 200
    fake_service.stop.assert_called_once_with('rec_test', name=None)


def test_stop_strips_whitespace_name(client, fake_service):
    r = client.post('/api/recording/rec_test/stop', json={'name': '   '})
    assert r.status_code == 200
    fake_service.stop.assert_called_once_with('rec_test', name=None)


def test_stop_404(client, fake_service):
    from app.services.recording.service import SessionNotFoundError
    fake_service.stop.side_effect = SessionNotFoundError("nope")
    r = client.post('/api/recording/rec_unknown/stop')
    assert r.status_code == 404


# ---------------------------------------------------------------- /discard

def test_discard(client, fake_service):
    r = client.post('/api/recording/rec_test/discard')
    assert r.status_code == 200
    body = r.get_json()
    assert body['status'] == 'discarded'
    fake_service.discard.assert_called_once_with('rec_test')


# ---------------------------------------------------------------- /state

def test_get_state(client, fake_service):
    r = client.get('/api/recording/rec_test/state')
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['state']['session_id'] == 'rec_test123'
    assert body['state']['streams']['mic']['peak'] == 0.5


def test_get_state_404(client, fake_service):
    from app.services.recording.service import SessionNotFoundError
    fake_service.get_state.side_effect = SessionNotFoundError("nope")
    r = client.get('/api/recording/rec_unknown/state')
    assert r.status_code == 404


# ---------------------------------------------------------------- /live-transcript

def test_live_transcript_worker_disabled(client, fake_service):
    """state.live_transcribe_worker is None → available:false, not 500."""
    state.live_transcribe_worker = None
    r = client.get('/api/recording/rec_test123/live-transcript')
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['available'] is False
    assert body['reason']
    assert body['segments'] == []


def test_live_transcript_404_unknown_session(client, fake_service):
    from app.services.recording.service import SessionNotFoundError
    fake_service.get_state.side_effect = SessionNotFoundError("nope")
    r = client.get('/api/recording/rec_unknown/live-transcript')
    assert r.status_code == 404
    assert r.get_json()['error_code'] == 'NOT_FOUND'


def test_live_transcript_finalized_session(client, fake_service):
    fake_service.get_state.return_value = {
        'session_id': 'rec_test123', 'status': 'finalized', 'is_active': False,
        'elapsed_seconds': 0.0,
    }
    worker = MagicMock()
    worker.is_active.return_value = False
    state.live_transcribe_worker = worker
    r = client.get('/api/recording/rec_test123/live-transcript')
    assert r.status_code == 200
    body = r.get_json()
    assert body['available'] is False
    assert 'транскрипт' in body['reason']


def test_live_transcript_returns_segments(client, fake_service):
    worker = MagicMock()
    worker.is_active.return_value = True
    worker.get_preview.return_value = [
        {'start': 1.0, 'end': 2.0, 'text': 'привіт', 'speaker': 'self', 'stream': 'mic', 'seq': 1},
        {'start': 3.5, 'end': 4.0, 'text': 'ок', 'speaker': 'other', 'stream': 'system', 'seq': 2},
    ]
    state.live_transcribe_worker = worker
    r = client.get('/api/recording/rec_test123/live-transcript')
    assert r.status_code == 200
    body = r.get_json()
    assert body['available'] is True
    assert body['reason'] is None
    assert body['count'] == 2
    assert body['last_sec'] == 3.5
    assert body['next_seq'] == 2
    assert body['truncated'] is False
    assert body['segments'][0] == {
        'start': 1.0, 'end': 2.0, 'text': 'привіт', 'speaker_label': 'self', 'stream': 'mic',
        'seq': 1,
    }


def test_live_transcript_since_sec_filters(client, fake_service):
    worker = MagicMock()
    worker.is_active.return_value = True
    worker.get_preview.return_value = [
        {'start': 1.0, 'end': 2.0, 'text': 'старе', 'speaker': 'self', 'stream': 'mic'},
        {'start': 5.0, 'end': 6.0, 'text': 'нове', 'speaker': 'self', 'stream': 'mic'},
    ]
    state.live_transcribe_worker = worker
    r = client.get('/api/recording/rec_test123/live-transcript?since_sec=3')
    body = r.get_json()
    assert body['count'] == 1
    assert body['segments'][0]['text'] == 'нове'
    assert body['last_sec'] == 5.0


def test_live_transcript_truncates_at_limit(client, fake_service):
    worker = MagicMock()
    worker.is_active.return_value = True
    worker.get_preview.return_value = [
        {'start': float(i), 'end': float(i) + 1, 'text': f't{i}', 'speaker': 'self', 'stream': 'mic'}
        for i in range(410)
    ]
    state.live_transcribe_worker = worker
    r = client.get('/api/recording/rec_test123/live-transcript')
    body = r.get_json()
    assert body['count'] == 400
    assert body['truncated'] is True


def test_live_transcript_seq_cursor_survives_stream_race(client, fake_service):
    """Story 06: the system track is transcribed more slowly than mic and its
    segment appears LATER, although its `start` is SMALLER than that of the already returned
    mic segment. A cursor by `seq` (the append order) is obliged to return
    it; a cursor by `start`/`since_sec` would lose it forever.
    """
    worker = MagicMock()
    worker.is_active.return_value = True

    # Step 1: only the mic segment is in RAM (start=105, added first).
    worker.get_preview.return_value = [
        {'start': 105.0, 'end': 106.0, 'text': 'mic перший', 'speaker': 'self',
         'stream': 'mic', 'seq': 1},
    ]
    state.live_transcribe_worker = worker
    r1 = client.get('/api/recording/rec_test123/live-transcript')
    body1 = r1.get_json()
    assert body1['count'] == 1
    assert body1['next_seq'] == 1

    # Step 2: the system track finally finishes its pass — its segment has a
    # SMALLER start (100 < 105), but was added LATER (seq=2).
    worker.get_preview.return_value = worker.get_preview.return_value + [
        {'start': 100.0, 'end': 101.0, 'text': 'system пізніше', 'speaker': 'other',
         'stream': 'system', 'seq': 2},
    ]

    # Polling with a seq cursor — the system segment arrives, it is not lost.
    r2 = client.get(f'/api/recording/rec_test123/live-transcript?since_seq={body1["next_seq"]}')
    body2 = r2.get_json()
    assert body2['count'] == 1
    assert body2['segments'][0]['text'] == 'system пізніше'
    assert body2['next_seq'] == 2

    # A control for "does not arrive twice": one more poll with the same cursor — empty.
    r3 = client.get(f'/api/recording/rec_test123/live-transcript?since_seq={body2["next_seq"]}')
    body3 = r3.get_json()
    assert body3['count'] == 0
    assert body3['segments'] == []
    assert body3['next_seq'] == body2['next_seq']

    # A control for the desired contrast: the same poll with the old time cursor
    # (since_sec = the start of the already returned mic segment) loses the system segment,
    # because its start=100 does not exceed 105 — exactly the bug this story fixes.
    r_broken = client.get('/api/recording/rec_test123/live-transcript?since_sec=105')
    body_broken = r_broken.get_json()
    assert body_broken['count'] == 0


# ---------------------------------------------------------------- /active

def test_active_session_when_idle(client, fake_service):
    fake_service.active_session_ids.return_value = []
    r = client.get('/api/recordings/active')
    assert r.status_code == 200
    body = r.get_json()
    assert body['active'] is False
    assert body['count'] == 0
    assert body['sessions'] == []
    assert body['session_id'] is None


def test_active_session_when_recording(client, fake_service):
    fake_service.active_session_ids.return_value = ['rec_active']
    r = client.get('/api/recordings/active')
    assert r.status_code == 200
    body = r.get_json()
    assert body['active'] is True
    assert body['count'] == 1
    assert body['session_id'] == 'rec_active'
    assert body['state']['status'] == 'recording'


def test_active_sessions_two_live(client, fake_service, monkeypatch):
    fake_service.active_session_ids.return_value = ['rec_a', 'rec_b']

    def _state(sid):
        return {'session_id': sid, 'status': 'recording', 'elapsed_seconds': 3.0,
                'name': None, 'auto_name': f'Запис {sid}',
                'streams': {'mic': {'enabled': sid == 'rec_b'}, 'system': {'enabled': True}},
                'system_source': {'mode': 'all', 'apps': [], 'device_index': 2}}
    fake_service.get_state.side_effect = _state
    cp = MagicMock()
    cp.get_by_recording.side_effect = (
        lambda sid: {'id': 1, 'status': 'active'} if sid == 'rec_a' else None)
    monkeypatch.setattr(state, 'copilot_service', cp)

    body = client.get('/api/recordings/active').get_json()
    assert body['count'] == 2
    assert body['session_id'] is None and body['state'] is None
    a, b = body['sessions']
    assert a['session_id'] == 'rec_a' and b['session_id'] == 'rec_b'
    assert a['streams'] == {'mic': False, 'system': True}
    assert b['streams'] == {'mic': True, 'system': True}
    assert a['system_source']['mode'] == 'all'
    assert a['copilot_active'] is True and b['copilot_active'] is False


def test_state_carries_system_source(client, fake_service):
    fake_service.get_state.return_value = {
        'status': 'recording',
        'system_source': {'mode': 'include', 'apps': ['zoom.exe'], 'device_index': None},
    }
    body = client.get('/api/recording/rec_test123/state').get_json()
    assert body['state']['system_source']['mode'] == 'include'


# ---------------------------------------------------------------- /system-source

def test_system_source_retarget(client, fake_service):
    fake_service.retarget_system.return_value = {
        'session_id': 'rec_a', 'stream': 'system',
        'source': {'mode': 'include', 'apps': ['zoom.exe'], 'device_index': None},
        'event': {'reason': 'user', 'gap_ms': 12.0}, 'applied_on_resume': False,
    }
    r = client.post('/api/recording/rec_a/system-source',
                    json={'mode': 'include', 'apps': ['zoom.exe']})
    assert r.status_code == 200
    sid, spec, reason = fake_service.retarget_system.call_args.args
    assert sid == 'rec_a' and spec.mode == 'include' and spec.apps == ('zoom.exe',)
    assert reason == 'user'
    body = r.get_json()
    assert body['source']['apps'] == ['zoom.exe']
    assert body['event']['gap_ms'] == 12.0
    assert body['applied_on_resume'] is False


def test_system_source_auto_restore_passed_through(client, fake_service):
    fake_service.retarget_system.return_value = {'source': {}, 'event': {}}
    r = client.post('/api/recording/rec_a/system-source',
                    json={'mode': 'all', 'reason': 'auto_restore'})
    assert r.status_code == 200
    assert fake_service.retarget_system.call_args.args[2] == 'auto_restore'


def test_system_source_bad_reason_400(client, fake_service):
    r = client.post('/api/recording/rec_a/system-source', json={'mode': 'all', 'reason': 'guess'})
    assert r.status_code == 400
    fake_service.retarget_system.assert_not_called()


def test_system_source_errors(client, fake_service):
    from app.services.recording.service import NoSystemStreamError, SessionNotFoundError
    fake_service.retarget_system.side_effect = SessionNotFoundError('nope')
    r = client.post('/api/recording/rec_x/system-source', json={'mode': 'all'})
    assert r.status_code == 404 and r.get_json()['error_code'] == 'NOT_FOUND'

    fake_service.retarget_system.side_effect = NoSystemStreamError('no system')
    r = client.post('/api/recording/rec_a/system-source', json={'mode': 'all'})
    assert r.status_code == 409 and r.get_json()['error_code'] == 'NO_SYSTEM_STREAM'

    r = client.post('/api/recording/rec_a/system-source',
                    json={'mode': 'exclude', 'apps': ['a.exe', 'b.exe']})
    assert r.status_code == 400 and r.get_json()['error_code'] == 'UNSUPPORTED_SOURCE_SPEC'


# ---------------------------------------------------------------- /apps

def test_apps_marks_recorded_by_live_sessions(client, fake_service, monkeypatch):
    from app.services.recording import app_audio
    fake_service.active_session_ids.return_value = ['rec_a', 'rec_b']
    specs = {'rec_a': {'mode': 'include', 'apps': ['zoom.exe'], 'device_index': None},
             'rec_b': {'mode': 'exclude', 'apps': ['zoom.exe'], 'device_index': None}}
    fake_service.get_state.side_effect = lambda sid: {'system_source': specs[sid]}
    real = app_audio.list_sounding_apps
    rows = [(100, 'active', 'Speakers', 'Zoom.exe'), (200, 'active', 'Speakers', 'chrome.exe')]
    monkeypatch.setattr(app_audio, 'list_sounding_apps', lambda live_specs=None: real(
        live_specs, enumerate_sessions=lambda: rows, own_pid=1))

    body = client.get('/api/recording/apps').get_json()
    assert body['success'] is True and body['available'] is True
    by = {a['exe']: a['recorded_by'] for a in body['apps']}
    assert by == {'zoom.exe': ['rec_a'], 'chrome.exe': ['rec_b']}


def test_apps_enumerator_failure_is_200_unavailable(client, fake_service, monkeypatch):
    from app.services.recording import app_audio

    def _boom(live_specs=None):
        raise RuntimeError('COM exploded')
    monkeypatch.setattr(app_audio, 'list_sounding_apps', _boom)
    r = client.get('/api/recording/apps')
    assert r.status_code == 200
    assert r.get_json()['available'] is False


# ---------------------------------------------------------------- /recovered (Phase 9.10)

def test_recovered_endpoint_empty_log(client, fake_service):
    """If recovery_log is empty — returns count=0."""
    original = state.recording_recovery_log
    state.recording_recovery_log = []
    try:
        r = client.get('/api/recordings/recovered')
        body = r.get_json()
        assert r.status_code == 200
        assert body['success'] is True
        assert body['count'] == 0
        assert body['recovered'] == []
    finally:
        state.recording_recovery_log = original


def test_recovered_endpoint_with_sessions(client, fake_service):
    """Recovered sessions with manifest details. Auto-ack clears the log."""
    original = state.recording_recovery_log
    state.recording_recovery_log = ['rec_a', 'rec_b']
    fake_service.store.read = lambda sid: {
        'session_id': sid,
        'name': f'Test {sid}',
        'auto_name': f'Запис {sid}',
        'status': 'crashed',
        'started_at': '2026-05-04T10:00:00+00:00',
        'final_mp3_path': None,
        'total_duration_sec': 5.0,
    }
    try:
        r = client.get('/api/recordings/recovered')
        body = r.get_json()
        assert r.status_code == 200
        assert body['count'] == 2
        ids = {item['session_id'] for item in body['recovered']}
        assert ids == {'rec_a', 'rec_b'}
        # Auto-ack: a repeated request must return 0
        r2 = client.get('/api/recordings/recovered')
        assert r2.get_json()['count'] == 0
    finally:
        state.recording_recovery_log = original


def test_recovered_endpoint_no_ack_keeps_log(client, fake_service):
    """?ack=0 does not clear the log."""
    original = state.recording_recovery_log
    state.recording_recovery_log = ['rec_keep']
    fake_service.store.read = lambda sid: {
        'session_id': sid, 'name': None, 'auto_name': 'X', 'status': 'crashed',
    }
    try:
        r = client.get('/api/recordings/recovered?ack=0')
        assert r.get_json()['count'] == 1
        # The log still contains rec_keep
        assert state.recording_recovery_log == ['rec_keep']
    finally:
        state.recording_recovery_log = original


# ---------------------------------------------------------------- /save (orphan fix)

@pytest.fixture
def save_client(fake_service, tmp_path):
    """A test_client with a real DB (for /save → register_recording)."""
    from app.db.migrations import init_database

    db_path = str(tmp_path / 'save.db')
    init_database(db_path)

    final_mp3 = tmp_path / 'final.mp3'
    final_mp3.write_bytes(b'\x00' * 4096)

    fake_service.store.read = lambda sid: {
        'session_id': sid,
        'status': 'finalized',
        'final_mp3_path': str(final_mp3),
        'total_duration_sec': 42.0,
        'segments': [{'index': 0}],
        'name': None,
        'auto_name': 'Запис auto',
    }
    fake_service.store.set_name = MagicMock()

    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config['TESTING'] = True
    app.config['DATABASE'] = db_path

    original_service = state.recording_service
    state.recording_service = fake_service
    try:
        yield app.test_client(), db_path
    finally:
        state.recording_service = original_service


def test_save_registers_recording_in_library(save_client):
    client, db_path = save_client
    r = client.post('/api/recording/rec_xyz/save', json={'name': 'Моя назва'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['name'] == 'Моя назва'
    assert body['already_registered'] is False
    assert body['download_id'] > 0

    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT title, source_type FROM audio_downloads "
            "WHERE recording_session_id = 'rec_xyz'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]['title'] == 'Моя назва'
    assert rows[0]['source_type'] == 'recording'


def test_save_is_idempotent_after_auto_register(save_client):
    """A simulation of the orphan fix: the finalize callback has already registered the recording,
    then the frontend calls /save → does not duplicate, only updates the name."""
    client, db_path = save_client

    # the finalize callback did the auto-registration (auto_name)
    from app.services.recording.library import register_recording
    auto = register_recording(db_path, 'rec_xyz', {
        'final_mp3_path': state.recording_service.store.read('rec_xyz')['final_mp3_path'],
        'total_duration_sec': 42.0, 'segments': [{'index': 0}], 'auto_name': 'Запис auto',
    })
    assert auto['created'] is True

    # now /save with a manual name
    r = client.post('/api/recording/rec_xyz/save', json={'name': 'Перейменовано'})
    body = r.get_json()
    assert body['already_registered'] is True
    assert body['download_id'] == auto['download_id']
    assert body['name'] == 'Перейменовано'

    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT title FROM audio_downloads WHERE recording_session_id = 'rec_xyz'"
        ).fetchall()
    assert len(rows) == 1  # no duplicate
    assert rows[0]['title'] == 'Перейменовано'


# ---------------------------------------------------------------- call autoprompt (K2)

def _watcher_with_prompt(live=None):
    """Real CallWatcher on fakes, ticked through a 31 s Zoom call → one «Записати?» prompt."""
    from app.services.recording import call_watch as cw
    t = [0.0]
    snap = {'zoom.exe': {'mic': {'active': True, 'peak': 0.3, 'endpoint': 'Мікрофон (FOX)'},
                         'out': {'active': True, 'peak': 0.3, 'endpoint': 'Навушники'}}}
    notifier = MagicMock()
    w = cw.CallWatcher(apps=frozenset({'zoom.exe'}), prompt_seconds=30, autostop_minutes=10,
                       base_url='http://127.0.0.1:5050', enumerate_calls=lambda: snap,
                       live_sessions=lambda: live or [], stop_fn=MagicMock(), notifier=notifier,
                       clock=lambda: t[0], wall=lambda: 1_760_000_000.0 + t[0])
    for _ in range(31):
        w.tick()
        t[0] += 1
    return w, t, snap


def _install(client, watcher):
    client.application.extensions['call_watcher'] = watcher


def test_autoprompt_disabled_without_watcher(client):
    r = client.get('/api/recording/autoprompt')
    assert r.status_code == 200
    assert r.get_json() == {'success': True, 'enabled': False, 'prompts': []}


def test_autoprompt_pending_lists_prompt(client):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    body = client.get('/api/recording/autoprompt').get_json()
    assert body['enabled'] is True
    [p] = body['prompts']
    assert p['kind'] == 'record' and p['exe'] == 'zoom.exe' and p['label'] == 'Zoom'


def test_autoprompt_record_starts_app_only_with_call_mic(client, fake_service):
    fake_service.list_devices.return_value = [
        {'index': 1, 'name': 'TestMic', 'kind': 'mic', 'is_default': True},
        {'index': 7, 'name': 'Мікрофон (FOX)', 'kind': 'mic', 'is_default': False},
    ]
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    token = w.pending()[0]['id']
    r = client.get(f'/api/recording/autoprompt/{token}/record')
    assert r.status_code == 302 and r.headers['Location'].endswith('/record')
    kw = fake_service.start.call_args.kwargs
    assert kw['system_source'].mode == 'include' and kw['system_source'].apps == ('zoom.exe',)
    assert kw['mic_device_index'] == 7 and kw['system_device_index'] is None
    # single use: the second click (or the other button) starts nothing
    fake_service.start.reset_mock()
    assert client.get(f'/api/recording/autoprompt/{token}/record').status_code == 404
    assert client.get(f'/api/recording/autoprompt/{token}/sources').status_code == 404
    fake_service.start.assert_not_called()


def test_autoprompt_record_falls_back_to_default_mic(client, fake_service):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    client.get(f"/api/recording/autoprompt/{w.pending()[0]['id']}/record")
    assert fake_service.start.call_args.kwargs['mic_device_index'] == 1


def test_autoprompt_unknown_token_starts_nothing(client, fake_service):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    r = client.get('/api/recording/autoprompt/forged-token/record')
    assert r.status_code == 404 and 'не дійсне' in r.get_data(as_text=True)
    fake_service.start.assert_not_called()


def test_autoprompt_record_session_limit_stops_nothing(client, fake_service):
    from app.services.recording.service import SessionConflictError
    fake_service.start.side_effect = SessionConflictError('limit', active=['a', 'b'], limit=2)
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    r = client.get(f"/api/recording/autoprompt/{w.pending()[0]['id']}/record")
    assert r.status_code == 409 and 'Нічого не зупинено' in r.get_data(as_text=True)
    fake_service.stop.assert_not_called()


def test_autoprompt_sources_redirects_with_preselection(client, fake_service):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    r = client.get(f"/api/recording/autoprompt/{w.pending()[0]['id']}/sources")
    assert r.status_code == 302
    from urllib.parse import parse_qs, urlparse
    loc = urlparse(r.headers['Location'])
    assert loc.path == '/record'
    assert parse_qs(loc.query) == {'app': ['zoom.exe'], 'mic': ['Мікрофон (FOX)']}
    fake_service.start.assert_not_called()


def test_autoprompt_stop_uses_the_stop_path(client, fake_service):
    live = [{'sid': 'rec_test123', 'system_source': {'mode': 'all'}, 'is_paused': False,
             'mic_peak': 0.0, 'sys_peak': 0.0}]
    w, t, snap = _watcher_with_prompt(live)
    snap.clear()                                  # the call ends → «Зупинити?» for rec_test123
    for _ in range(25):
        w.tick()
        t[0] += 1
    [p] = [p for p in w.pending() if p['kind'] == 'stop']
    _install(client, w)
    r = client.get(f"/api/recording/autoprompt/{p['id']}/stop")
    assert r.status_code == 302
    fake_service.stop.assert_called_once_with('rec_test123', name=None)


def test_autoprompt_dismiss(client):
    w, _, _ = _watcher_with_prompt()
    _install(client, w)
    pid = w.pending()[0]['id']
    assert client.post(f'/api/recording/autoprompt/{pid}/dismiss').status_code == 200
    assert client.post(f'/api/recording/autoprompt/{pid}/dismiss').status_code == 404
    assert client.get('/api/recording/autoprompt').get_json()['prompts'] == []


# ---------------------------------------------------------------- /live-video, /live-frame(s)

class _FakeLiveFrames:
    """Stands in for app.services.live_frames: records calls, returns canned answers."""

    def __init__(self):
        from app.services import live_frames as real
        self.LiveFramesError = real.LiveFramesError
        self.MAX_FRAMES_PER_CALL = real.MAX_FRAMES_PER_CALL
        self.calls = []
        self.raise_error = None
        self.status_body = {'session_id': 'rec_test123', 'available': True, 'reason': None,
                            'is_active': True, 'status': 'recording', 'elapsed_seconds': 50.0,
                            'tracks': [{'track_id': 'video_mon0', 'fps': 30.0,
                                        'readable_until': 41.9}]}
        self.frame = {'t': 12.5, 'video_t': 12.5, 'track_id': 'video_mon0', 'segment': 0,
                      'width': 640, 'height': 360, 'jpeg': b'\xff\xd8JPEG', 'text': 'masked',
                      'ocr': True}
        self.range_body = None

    def _maybe_raise(self):
        if self.raise_error is not None:
            raise self.raise_error

    def status(self, sid, **kw):
        self.calls.append(('status', sid, kw))
        self._maybe_raise()
        return self.status_body

    def frame_at(self, sid, t, **kw):
        self.calls.append(('frame_at', sid, t, kw))
        self._maybe_raise()
        return self.frame

    def frames_range(self, sid, start, end=None, **kw):
        self.calls.append(('frames_range', sid, start, end, kw))
        self._maybe_raise()
        if self.range_body is not None:
            return self.range_body
        return {'session_id': sid, 'available': True, 'track_id': 'video_mon0',
                'mode': kw.get('mode'), 'start': start, 'end': end, 'end_clamped': False,
                'readable_until': 41.9, 'frames': [dict(self.frame)], 'count': 1,
                'next_t': 13.5, 'scanned_until': 12.5, 'truncated': True}


@pytest.fixture
def fake_live(monkeypatch):
    import sys
    import app.services as services_pkg
    fake = _FakeLiveFrames()
    monkeypatch.setitem(sys.modules, 'app.services.live_frames', fake)
    monkeypatch.setattr(services_pkg, 'live_frames', fake, raising=False)
    return fake


def test_live_video_status_success(client, fake_live):
    r = client.get('/api/recording/rec_test123/live-video')
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['tracks'][0]['readable_until'] == 41.9
    assert fake_live.calls[0][0] == 'status'


def test_live_video_unknown_session_404(client, fake_live):
    fake_live.raise_error = fake_live.LiveFramesError('NOT_FOUND', 'nope')
    r = client.get('/api/recording/rec_unknown/live-video')
    assert r.status_code == 404
    body = r.get_json()
    assert body['success'] is False
    assert body['error_code'] == 'NOT_FOUND'
    assert body['error']


def test_live_video_no_video_404(client, fake_live):
    fake_live.raise_error = fake_live.LiveFramesError('NO_VIDEO', 'no video')
    r = client.get('/api/recording/rec_test123/live-video')
    assert r.status_code == 404
    assert r.get_json()['error_code'] == 'NO_VIDEO'


def test_live_video_finalized_names_transcription_tools(client, fake_live):
    fake_live.status_body = {'session_id': 'rec_test123', 'available': False,
                             'reason': 'finalized', 'is_active': False, 'status': 'finalized',
                             'elapsed_seconds': 0.0, 'tracks': []}
    r = client.get('/api/recording/rec_test123/live-video', headers={'Accept-Language': 'en'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['available'] is False
    assert 'get_frame' in body['reason'] and 'get_visual_timeline' in body['reason']
    assert body['reason_code'] == 'finalized'


def test_live_frame_returns_jpeg_with_headers(client, fake_live):
    r = client.get('/api/recording/rec_test123/live-frame?t=12.5')
    assert r.status_code == 200
    assert r.mimetype == 'image/jpeg'
    assert r.data == b'\xff\xd8JPEG'
    assert r.headers['X-Recall-Frame-T'] == '12.5'
    assert r.headers['X-Recall-Frame-Video-T'] == '12.5'
    assert fake_live.calls[0][2] == 12.5


def test_live_frame_error_is_json(client, fake_live):
    fake_live.raise_error = fake_live.LiveFramesError('PAST_LIVE_EDGE', 'late',
                                                      readable_until=41.9)
    r = client.get('/api/recording/rec_test123/live-frame?t=99')
    assert r.status_code == 409
    body = r.get_json()
    assert body['success'] is False
    assert body['error_code'] == 'PAST_LIVE_EDGE'
    assert body['error']


def test_live_frames_single_t_returns_one_frame(client, fake_live):
    import base64
    r = client.get('/api/recording/rec_test123/live-frames?t=12.5')
    assert r.status_code == 200
    body = r.get_json()
    assert body['count'] == 1
    frame = body['frames'][0]
    assert base64.b64decode(frame['image_b64']) == b'\xff\xd8JPEG'
    assert frame['text'] == 'masked'
    assert 'jpeg' not in frame
    assert fake_live.calls[0][0] == 'frame_at'


def test_live_frames_range_forwards_query_and_cursor(client, fake_live):
    r = client.get('/api/recording/rec_test123/live-frames?start=10&end=40&mode=changes'
                   '&min_res=0.5&limit=12&cursor=13.5&track=video_mon0&size=full')
    assert r.status_code == 200
    body = r.get_json()
    for key in ('frames', 'next_t', 'readable_until', 'scanned_until', 'truncated'):
        assert key in body
    assert body['frames'][0]['image_b64']
    assert body['frames'][0]['text'] == 'masked'
    name, _sid, start, end, kw = fake_live.calls[0]
    assert (name, start, end) == ('frames_range', 10.0, 40.0)
    assert kw['cursor'] == 13.5
    assert kw['mode'] == 'changes' and kw['min_res'] == 0.5 and kw['limit'] == 12
    assert kw['track'] == 'video_mon0' and kw['size'] == 'full'


def test_live_frames_past_edge_returns_409_with_readable_until(client, fake_live):
    fake_live.raise_error = fake_live.LiveFramesError('PAST_LIVE_EDGE', 'late',
                                                      readable_until=41.9)
    r = client.get('/api/recording/rec_test123/live-frames?start=50&end=60')
    assert r.status_code == 409
    body = r.get_json()
    assert body['success'] is False
    assert body['error_code'] == 'PAST_LIVE_EDGE'
    assert body['readable_until'] == 41.9


def test_live_frames_finalized_available_false(client, fake_live):
    fake_live.range_body = {'session_id': 'rec_test123', 'available': False,
                            'reason': 'finalized', 'frames': [], 'count': 0}
    r = client.get('/api/recording/rec_test123/live-frames?start=0')
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True and body['available'] is False
    assert body['frames'] == []


@pytest.mark.parametrize('query', [
    'start=0&limit=50', 'start=0&mode=foo', 'start=0&step=0', 't=abc', 'start=20&end=10',
], ids=['limit-too-big', 'bad-mode', 'zero-step', 'non-numeric-t', 'start-after-end'])
def test_live_frames_bad_query_400_before_service(client, fake_live, query):
    r = client.get(f'/api/recording/rec_test123/live-frames?{query}')
    assert r.status_code == 400
    assert r.get_json()['error_code'] == 'INVALID_ARGUMENT'
    assert fake_live.calls == []


def test_live_frame_non_numeric_t_400(client, fake_live):
    r = client.get('/api/recording/rec_test123/live-frame?t=abc')
    assert r.status_code == 400
    assert r.get_json()['error_code'] == 'INVALID_ARGUMENT'
    assert fake_live.calls == []
