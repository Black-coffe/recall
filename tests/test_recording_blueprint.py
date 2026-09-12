"""Integration tests для recording REST API (Phase 9.5).

Тести використовують Flask test_client + mock RecordingService — без
реальних WASAPI стрімів. Покривають happy path і error-кейси для
кожного endpoint'у.
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
    """Module-level кеш не повинен текти між тестами."""
    recording_module._devices_cache['ts'] = 0.0
    recording_module._devices_cache['data'] = []
    yield
    recording_module._devices_cache['ts'] = 0.0
    recording_module._devices_cache['data'] = []


@pytest.fixture
def fake_service():
    """Mock RecordingService із заздалегідь налаштованою поведінкою."""
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
    """Flask test_client з підміненим recording_service."""
    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config['TESTING'] = True

    # Підмінюємо state singletons
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
    # list_devices мав викликатися лише раз (другий — з кешу)
    assert fake_service.list_devices.call_count == 1


def test_get_devices_force_bypasses_cache(client, fake_service):
    client.get('/api/recording/devices')
    client.get('/api/recording/devices?force=1')
    assert fake_service.list_devices.call_count == 2


def test_devices_503_when_service_disabled(fake_service):
    """Якщо recording_service is None — 503."""
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


def test_start_conflict_returns_409(client, fake_service):
    from app.services.recording.service import SessionConflictError
    fake_service.start.side_effect = SessionConflictError("active already")
    fake_service.active_session_id = 'rec_other'

    r = client.post('/api/recording/start', json={'mic_device_index': 1})
    assert r.status_code == 409
    body = r.get_json()
    assert body['error_code'] == 'SESSION_CONFLICT'
    assert body['active_session_id'] == 'rec_other'


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
    """state.live_transcribe_worker is None → available:false, не 500."""
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
    """Story 06: system-доріжка транскрибується повільніше за mic і її
    сегмент з'являється ПІЗНІШЕ, хоч його `start` МЕНШИЙ за вже відданий
    mic-сегмент. Курсор по `seq` (порядок append'у) зобов'язаний його
    повернути; курсор по `start`/`since_sec` — загубив би назавжди.
    """
    worker = MagicMock()
    worker.is_active.return_value = True

    # Крок 1: у RAM лежить лише mic-сегмент (start=105, доданий першим).
    worker.get_preview.return_value = [
        {'start': 105.0, 'end': 106.0, 'text': 'mic перший', 'speaker': 'self',
         'stream': 'mic', 'seq': 1},
    ]
    state.live_transcribe_worker = worker
    r1 = client.get('/api/recording/rec_test123/live-transcript')
    body1 = r1.get_json()
    assert body1['count'] == 1
    assert body1['next_seq'] == 1

    # Крок 2: system-доріжка нарешті домальовує свій пасс — її сегмент має
    # МЕНШИЙ start (100 < 105), але доданий ПІЗНІШЕ (seq=2).
    worker.get_preview.return_value = worker.get_preview.return_value + [
        {'start': 100.0, 'end': 101.0, 'text': 'system пізніше', 'speaker': 'other',
         'stream': 'system', 'seq': 2},
    ]

    # Опит курсором по seq — system-сегмент приходить, не губиться.
    r2 = client.get(f'/api/recording/rec_test123/live-transcript?since_seq={body1["next_seq"]}')
    body2 = r2.get_json()
    assert body2['count'] == 1
    assert body2['segments'][0]['text'] == 'system пізніше'
    assert body2['next_seq'] == 2

    # Контроль на "не приходить двічі": ще один опит тим самим курсором — порожньо.
    r3 = client.get(f'/api/recording/rec_test123/live-transcript?since_seq={body2["next_seq"]}')
    body3 = r3.get_json()
    assert body3['count'] == 0
    assert body3['segments'] == []
    assert body3['next_seq'] == body2['next_seq']

    # Контроль на бажаний контраст: той самий опит старим курсором по часу
    # (since_sec = start вже відданого mic-сегмента) губить system-сегмент,
    # бо його start=100 не перевищує 105 — саме той баг, який чинить ця історія.
    r_broken = client.get('/api/recording/rec_test123/live-transcript?since_sec=105')
    body_broken = r_broken.get_json()
    assert body_broken['count'] == 0


# ---------------------------------------------------------------- /active

def test_active_session_when_idle(client, fake_service):
    fake_service.active_session_id = None
    r = client.get('/api/recordings/active')
    assert r.status_code == 200
    body = r.get_json()
    assert body['active'] is False
    assert body['session_id'] is None


def test_active_session_when_recording(client, fake_service):
    fake_service.active_session_id = 'rec_active'
    r = client.get('/api/recordings/active')
    assert r.status_code == 200
    body = r.get_json()
    assert body['active'] is True
    assert body['session_id'] == 'rec_active'
    assert 'state' in body


# ---------------------------------------------------------------- /recovered (Phase 9.10)

def test_recovered_endpoint_empty_log(client, fake_service):
    """Якщо recovery_log порожній — повертає count=0."""
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
    """Recovered sessions з manifest details. Auto-ack очищає лог."""
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
        # Auto-ack: повторний запит має повернути 0
        r2 = client.get('/api/recordings/recovered')
        assert r2.get_json()['count'] == 0
    finally:
        state.recording_recovery_log = original


def test_recovered_endpoint_no_ack_keeps_log(client, fake_service):
    """?ack=0 не очищує лог."""
    original = state.recording_recovery_log
    state.recording_recovery_log = ['rec_keep']
    fake_service.store.read = lambda sid: {
        'session_id': sid, 'name': None, 'auto_name': 'X', 'status': 'crashed',
    }
    try:
        r = client.get('/api/recordings/recovered?ack=0')
        assert r.get_json()['count'] == 1
        # Лог досі містить rec_keep
        assert state.recording_recovery_log == ['rec_keep']
    finally:
        state.recording_recovery_log = original


# ---------------------------------------------------------------- /save (orphan fix)

@pytest.fixture
def save_client(fake_service, tmp_path):
    """test_client з реальною БД (для /save → register_recording)."""
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
    """Симуляція orphan-фіксу: finalize-callback вже зареєстрував запис,
    потім фронтенд кличе /save → не дублює, лише оновлює назву."""
    client, db_path = save_client

    # finalize-callback зробив авто-реєстрацію (auto_name)
    from app.services.recording.library import register_recording
    auto = register_recording(db_path, 'rec_xyz', {
        'final_mp3_path': state.recording_service.store.read('rec_xyz')['final_mp3_path'],
        'total_duration_sec': 42.0, 'segments': [{'index': 0}], 'auto_name': 'Запис auto',
    })
    assert auto['created'] is True

    # тепер /save з ручною назвою
    r = client.post('/api/recording/rec_xyz/save', json={'name': 'Перейменовано'})
    body = r.get_json()
    assert body['already_registered'] is True
    assert body['download_id'] == auto['download_id']
    assert body['name'] == 'Перейменовано'

    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT title FROM audio_downloads WHERE recording_session_id = 'rec_xyz'"
        ).fetchall()
    assert len(rows) == 1  # без дубля
    assert rows[0]['title'] == 'Перейменовано'
