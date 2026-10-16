"""Two recordings end to end, offline (parallel-recordings-app-audio-09).

A real `RecordingService` (fake recorder / app-source / writer, a temporary
`SessionStore`) under the real recording + copilot + transcription blueprints on a
Flask test client, a real `CopilotService` on a temporary DB, and `mcp_server` with
`_api` pointed at the same test client. The owner's scenario in miniature:
a conference (system audio without a microphone) + a work call (microphone + system
without Zoom). No WASAPI, no torch, no live DB, no import of app.py.

The sessions here are marked A/B in the texts; the real sids are `rec_<hex>` from the service.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from flask import Flask

import mcp_server as m
from app import state
from app.blueprints import copilot as cp_bp
from app.blueprints.copilot import copilot_bp
from app.blueprints.recording import recording_bp
from app.blueprints.transcription import transcription_bp
from app.core.settings import by_name
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.copilot.service import CopilotService
from app.services.metrics import MetricsRegistry
from app.services.recording.service import RecordingService
from app.services.recording.session_store import STATUS_FINALIZED, SessionStore
# Fakes and autouse fixtures from the service tests (reset_fakes resets the instance
# counters, fake_device_params replaces the WASAPI request for device parameters).
from tests.test_recording_service import (  # noqa: F401 — fixtures are registered by the import
    FakeAppSource,
    FakeRecorder,
    FakeWriter,
    fake_device_params,
    reset_fakes,
)


SYS_IDX = 7   # the conference loopback endpoint (A)
MIC_IDX = 3   # the work call microphone (B)
LEGACY_CODES = ('WRONG_SESSION', 'SESSION_CONFLICT')


# ---------------------------------------------------------------- fakes

class _Broker:
    """An SSE broker that only remembers publications."""

    def __init__(self):
        self.published: list[tuple[str, str, dict]] = []

    def publish(self, channel, event, data):
        self.published.append((channel, event, data))

    def events(self, channel, event):
        return [d for c, e, d in list(self.published) if c == channel and e == event]


class _LiveWorker:
    """LiveTranscribeWorker without whisper: start/stop/is_active/get_preview."""

    def __init__(self):
        self.started: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, bool]] = []
        self._live: set[str] = set()

    def start(self, *, session_id, pcm_path, sample_rate, channels, stream_name, language):
        self.started.append((session_id, stream_name))
        self._live.add(session_id)

    def stop(self, session_id, wait=False):
        self.stopped.append((session_id, wait))
        self._live.discard(session_id)
        return None

    def is_active(self, session_id):
        return session_id in self._live

    def get_preview(self, session_id, order_by='start'):
        return [{'start': 1.0, 'end': 2.0, 'text': f'текст {session_id}',
                 'speaker': 'other', 'stream': 'system', 'seq': 1}]


class _CopilotWorker:
    def __init__(self):
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start(self, *, copilot_session_id, recording_session_id, config):
        self.started.append(recording_session_id)

    def stop(self, sid, wait=False):
        self.stopped.append(sid)


class _Whisper:
    def __init__(self):
        self.calls: list[str] = []
        self.decode_options: list = []

    def transcribe_with_progress(self, audio_path, model_name='base', language='uk',
                                 task='transcribe', progress_callback=None, **kwargs):
        self.calls.append(audio_path)
        self.decode_options.append(kwargs.get('decode_options'))
        return {'text': 'Андрій: бюджет конференції', 'language': 'uk', 'segments': []}


class _RecordingClient:
    """A test client that keeps the text of every response (a check for legacy codes)."""

    def __init__(self, client):
        self._c = client
        self.bodies: list[str] = []

    def _keep(self, r):
        self.bodies.append(r.get_data(as_text=True))
        return r

    def get(self, *a, **kw):
        return self._keep(self._c.get(*a, **kw))

    def post(self, *a, **kw):
        return self._keep(self._c.post(*a, **kw))

    def open(self, *a, **kw):
        return self._keep(self._c.open(*a, **kw))


# ---------------------------------------------------------------- env

@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    db_path = str(tmp_path / 'parallel.db')
    init_database(db_path)

    writers: dict[str, FakeWriter] = {}

    def writer_factory(path):
        w = FakeWriter(path)
        writers[str(path)] = w
        return w

    store = SessionStore(tmp_path / 'sessions')
    broker = _Broker()
    service = RecordingService(
        store=store, sse_broker=broker, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=writer_factory,
        max_active=2, app_source_factory=FakeAppSource,
    )
    copilot = CopilotService(db_path, is_recording_active=service.is_active)
    live = _LiveWorker()
    cworker = _CopilotWorker()
    whisper = _Whisper()

    for name, value in (
        ('recording_service', service), ('sse_broker', broker),
        ('live_transcribe_worker', live), ('copilot_service', copilot),
        ('copilot_worker', cworker), ('whisper_manager', whisper),
        ('metrics', MetricsRegistry()), ('job_queue', None),
        ('active_library_transcriptions', None),
    ):
        monkeypatch.setattr(state, name, value)

    # Sounding now: Zoom and Chrome (C3), without pycaw.
    monkeypatch.setattr(
        'app.services.recording.app_audio._default_enumerate_sessions',
        lambda: [(4242, 'active', 'Speakers', 'zoom.exe'),
                 (4343, 'active', 'Speakers', 'chrome.exe')],
    )
    # live-ask: without Ollama, just a call counter.
    asks: list[dict] = []

    def fake_ask(db, question, **kw):
        asks.append(kw)
        return {'available': True, 'answer': 'ok'}

    monkeypatch.setattr(cp_bp.live_ask, 'ask_local', fake_ask)
    # Diarization for source_type=recording is off, we do not touch pyannote.
    from app.services import diarization_service as ds
    off = SimpleNamespace(is_available=lambda: False, unavailability_reason=lambda: 'test')
    monkeypatch.setattr(ds.DiarizationService, 'get_instance', classmethod(lambda cls: off))

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['DATABASE'] = db_path
    for bp in (recording_bp, copilot_bp, transcription_bp):
        app.register_blueprint(bp)
    client = _RecordingClient(app.test_client())

    # MCP → the same Flask process; response parsing is the real _handle_resp.
    mcp_calls: list[tuple[str, str]] = []
    hooks: dict = {}

    def fake_api(method, path, *, params=None, body=None, write=False, timeout=60.0):
        mcp_calls.append((method, path))
        if method == 'GET':
            r = client.get(path, query_string=params)
        else:
            r = client.open(path, method=method, json=body)
        out = m._handle_resp(httpx.Response(
            r.status_code, content=r.data,
            headers={'content-type': r.headers.get('Content-Type', 'application/json')}))
        hook = hooks.pop(path, None)
        if hook is not None:
            hook()
        return out

    monkeypatch.setattr(m, '_api', fake_api)
    monkeypatch.setattr(m, 'DB_PATH', db_path)

    e = SimpleNamespace(
        db=db_path, store=store, broker=broker, service=service, copilot=copilot,
        live=live, cworker=cworker, whisper=whisper, client=client, writers=writers,
        asks=asks, mcp_calls=mcp_calls, hooks=hooks, tmp=tmp_path,
    )
    yield e
    for sid in service.active_session_ids():
        service.discard(sid)


def _start(env, **body):
    r = env.client.post('/api/recording/start', json=body)
    return r


def _copilot_rows(db_path):
    with get_db_connection(db_path) as conn:
        return {r['recording_session_id']: r['status'] for r in conn.execute(
            'SELECT recording_session_id, status FROM copilot_sessions ORDER BY id')}


def _written(w: FakeWriter) -> int:
    with w._lock:
        return len(w.buffer) + len(w.flushed)


def _wait_for(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _no_legacy_codes(env):
    for body in env.client.bodies:
        for code in LEGACY_CODES:
            assert code not in body, f'{code} in the response: {body[:300]}'


# ---------------------------------------------------------------- scenario

def test_owner_scenario_two_recordings_end_to_end(env):
    # --- A: the conference — system audio only (all on the endpoint), with the copilot.
    ra = _start(env, system_device_index=SYS_IDX, copilot={})
    assert ra.status_code == 200
    A = ra.get_json()['session_id']
    assert ra.get_json()['copilot'] == {'started': True}
    a_orig = [r for r in FakeRecorder.instances if r.device_index == SYS_IDX]
    assert len(a_orig) == 1

    # --- B starts while A is recording: microphone + system without Zoom, also asks for the copilot.
    rb = _start(env, mic_device_index=MIC_IDX,
                system_source={'mode': 'exclude', 'apps': ['zoom.exe']}, copilot={})
    assert rb.status_code == 200
    B = rb.get_json()['session_id']
    assert B != A
    assert rb.get_json()['copilot'] == {'started': False, 'error_code': 'COPILOT_BUSY',
                                        'owner_session_id': A}
    assert env.service.active_session_ids() == [A, B]
    assert _copilot_rows(env.db) == {A: 'active'}

    # --- who records Zoom: only A (B excluded it).
    apps = env.client.get('/api/recording/apps').get_json()
    assert apps['available'] is True
    by_exe = {a['exe']: a for a in apps['apps']}
    assert by_exe['zoom.exe']['recorded_by'] == [A]
    assert by_exe['chrome.exe']['recorded_by'] == [A, B]

    # --- A narrows to Zoom on the fly.
    r = env.client.post(f'/api/recording/{A}/system-source',
                        json={'mode': 'include', 'apps': ['zoom.exe']})
    assert r.status_code == 200, r.get_json()
    mf_a = env.store.read(A)
    assert SessionStore.stream_source(mf_a, 'system')['mode'] == 'include'
    events = mf_a['streams']['system']['source_events']
    assert len(events) == 1 and events[0]['reason'] == 'user'
    sse = env.broker.events(f'recording:{A}', 'source_changed')
    assert len(sse) == 1 and sse[0]['source']['mode'] == 'include'
    assert env.broker.events(f'recording:{B}', 'source_changed') == []
    a_src = FakeAppSource.app_instances[-1]
    assert a_src.spec.mode == 'include' and a_src.spec.apps == ('zoom.exe',)

    # --- the copilot moves A → B.
    r = env.client.post('/api/copilot/move', json={'from_session_id': A, 'to_session_id': B})
    assert r.status_code == 200 and r.get_json()['success'] is True
    assert _copilot_rows(env.db) == {A: 'ended', B: 'active'}
    assert env.cworker.stopped == [A] and env.cworker.started[-1] == B

    # --- stop B; during the stop (under B's lock) A must keep recording frames.
    a_writer = env.writers[str(env.store.pcm_path(A, 'system'))]
    b_mic = next(r for r in FakeRecorder.instances if r.device_index == MIC_IDX)
    seen_during_b_stop = {}
    orig_close = b_mic.close

    def slow_close():
        before = _written(a_writer)
        a_src.push_fake_audio(4096)
        seen_during_b_stop['grew'] = _wait_for(lambda: _written(a_writer) >= before + 4096)
        orig_close()

    b_mic.close = slow_close
    r = env.client.post(f'/api/recording/{B}/stop', json={})
    assert r.status_code == 200 and r.get_json()['status'] == 'stopping'
    assert seen_during_b_stop == {'grew': True}, "A's flush thread stood still while B was stopping"
    assert env.store.read(B)['status'] == 'stopping'
    st_a = env.client.get(f'/api/recording/{A}/state').get_json()['state']
    assert st_a['status'] == 'recording' and st_a['is_active'] is True
    assert env.service.active_session_ids() == [A]

    # --- Zoom closed → the UI returns A to the whole system audio.
    n_rec = len(FakeRecorder.instances)
    r = env.client.post(f'/api/recording/{A}/system-source',
                        json={'mode': 'all', 'reason': 'auto_restore'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['event']['reason'] == 'auto_restore'
    mf_a = env.store.read(A)
    events = mf_a['streams']['system']['source_events']
    assert [e['reason'] for e in events] == ['user', 'auto_restore']
    assert SessionStore.stream_source(mf_a, 'system') == {
        'mode': 'all', 'apps': [], 'device_index': SYS_IDX}
    reopened = FakeRecorder.instances[n_rec:]
    assert len(reopened) == 1
    assert reopened[0].device_index == SYS_IDX
    assert (reopened[0].sample_rate, reopened[0].channels) == \
        (a_orig[0].sample_rate, a_orig[0].channels)
    assert len(env.broker.events(f'recording:{A}', 'source_changed')) == 2

    _no_legacy_codes(env)


def test_third_start_is_session_limit_until_one_stops(env):
    A = _start(env, system_device_index=SYS_IDX).get_json()['session_id']
    B = _start(env, mic_device_index=MIC_IDX).get_json()['session_id']

    r = _start(env, mic_device_index=MIC_IDX + 1)
    assert r.status_code == 409
    body = r.get_json()
    assert body['error_code'] == 'SESSION_LIMIT'
    assert body['active'] == [A, B] and body['limit'] == 2

    assert env.client.post(f'/api/recording/{B}/stop', json={}).status_code == 200
    r = _start(env, mic_device_index=MIC_IDX + 1)
    assert r.status_code == 200
    C = r.get_json()['session_id']
    assert env.service.active_session_ids() == [A, C]
    _no_legacy_codes(env)


def test_two_live_recordings_mcp_never_guesses(env):
    A = _start(env, system_device_index=SYS_IDX).get_json()['session_id']
    B = _start(env, mic_device_index=MIC_IDX).get_json()['session_id']

    active = env.client.get('/api/recordings/active').get_json()
    assert active['count'] == 2 and active['session_id'] is None
    assert [s['session_id'] for s in active['sessions']] == [A, B]

    got = m.get_active_recording()
    assert got['count'] == 2 and got['session_id'] is None
    assert [s['session_id'] for s in got['sessions']] == [A, B]

    lt = m.get_live_transcript()
    assert lt['error_code'] == 'AMBIGUOUS_SESSION'
    assert [s['session_id'] for s in lt['sessions']] == [A, B]
    assert not any(p.endswith('/live-transcript') for _, p in env.mcp_calls)

    lt_a = m.get_live_transcript(session_id=A)
    assert lt_a['success'] is True and lt_a['available'] is True
    assert lt_a['session_id'] == A
    assert [s['text'] for s in lt_a['segments']] == [f'текст {A}']

    ask = m.ask_live('q')
    assert ask['error_code'] == 'AMBIGUOUS_SESSION'
    assert [s['session_id'] for s in ask['sessions']] == [A, B]

    # The server does not guess either: live-ask without a session_id with two recordings → 409.
    direct = m._api('POST', '/api/copilot/live-ask', body={'question': 'q', 'session_id': None})
    assert direct['status_code'] == 409 and direct['error_code'] == 'AMBIGUOUS_SESSION'
    assert env.asks == []
    _no_legacy_codes(env)


def test_ask_live_passes_server_ambiguity_through(env):
    """A race: at the moment of resolving the recordings there are zero (MCP sends session_id=None), by the time of
    live-ask two have started — the server's 409 reaches the agent as AMBIGUOUS_SESSION."""
    ids: list[str] = []

    def two_start():
        ids.append(env.service.start(system_device_index=SYS_IDX))
        ids.append(env.service.start(mic_device_index=MIC_IDX))

    env.hooks['/api/recordings/active'] = two_start
    ask = m.ask_live('q')
    assert ('POST', '/api/copilot/live-ask') in env.mcp_calls
    assert ask['error_code'] == 'AMBIGUOUS_SESSION'
    assert [s['session_id'] for s in ask['sessions']] == ids
    assert env.asks == []
    _no_legacy_codes(env)


def test_ask10_stop_waits_only_own_live_feed_and_transcribe_not_refused(env, monkeypatch):
    monkeypatch.setenv('RECALL_VOCAB_MODE', 'off')
    monkeypatch.setenv('WHISPER_MULTILINGUAL', '0')
    A = _start(env, system_device_index=SYS_IDX).get_json()['session_id']
    B = _start(env, mic_device_index=MIC_IDX,
               system_source={'mode': 'exclude', 'apps': ['zoom.exe']}).get_json()['session_id']

    assert env.client.post(f'/api/recording/{B}/stop', json={}).status_code == 200
    assert env.live.stopped == [(B, True)]
    assert env.live.is_active(A)

    assert int(by_name()['WHISPER_MAX_PARALLEL'].default) == 1

    # B is finalized (the finalize pipeline does not run here) — the full transcription
    # starts at once, while A is still recording.
    mp3 = env.tmp / 'b_final.mp3'
    mp3.write_bytes(b'fake mp3')

    def _finalized(mf):
        mf['status'] = STATUS_FINALIZED
        mf['final_mp3_path'] = str(mp3)

    env.store.modify(B, _finalized)
    r = env.client.post('/api/transcribe', data={
        'source_type': 'recording', 'recording_session_id': B, 'model': 'base'})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json().get('transcription_id')
    assert env.whisper.calls == [str(mp3)]
    assert env.whisper.decode_options == [{}], 'K1: the recording path passes options, the flags are off'
    assert env.service.active_session_ids() == [A]
    _no_legacy_codes(env)


def test_single_recording_path_unchanged(env):
    r = _start(env, system_device_index=SYS_IDX)
    assert r.status_code == 200
    sid = r.get_json()['session_id']

    sys_recs = [x for x in FakeRecorder.instances if x.device_index == SYS_IDX]
    assert len(FakeRecorder.instances) == 1 and len(sys_recs) == 1
    assert FakeAppSource.app_instances == []
    assert SessionStore.stream_source(env.store.read(sid), 'system')['mode'] == 'all'

    active = env.client.get('/api/recordings/active').get_json()
    assert active['count'] == 1 and active['session_id'] == sid
    assert active['state']['session_id'] == sid
    assert m.get_active_recording()['session_id'] == sid
    _no_legacy_codes(env)
