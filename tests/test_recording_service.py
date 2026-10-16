"""Tests for app.services.recording.service (Phase 9.3).

The tests use fake recorder/writer factories — real
WASAPI streams and files are not involved. This makes the tests fast and
independent of the presence of audio devices on CI.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

import pytest

from app.services.recording.recorder import AudioFrame, LevelMeter
from app.services.recording.service import (
    RecordingError,
    RecordingService,
    SessionConflictError,
    SessionNotFoundError,
)
from app.services.recording.session_store import (
    STATUS_CRASHED,
    STATUS_PAUSED,
    STATUS_RECORDING,
    STATUS_STOPPING,
    STREAM_MIC,
    STREAM_SYSTEM,
    SessionStore,
)


# ---------------------------------------------------------------- fakes

class FakeRecorder:
    """Mock WasapiRecorder — without a real WASAPI."""

    instances: list["FakeRecorder"] = []

    def __init__(self, device_index, sample_rate=48000, channels=2, **_):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.channels = channels
        self.level = LevelMeter(sample_rate=sample_rate, channels=channels)
        self.dropped_frames = 0
        self._frames: deque[AudioFrame] = deque()
        self._is_open = False
        self._is_started = False
        self._is_closed = False
        self._callback_error = None
        self.events: list[dict] = []
        self.drain_delay = 0.0  # simulates a slow drain (session isolation)
        FakeRecorder.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []

    def open(self):
        self._is_open = True

    def start(self):
        if not self._is_open:
            self.open()
        self._is_started = True

    def stop(self):
        self._is_started = False

    def close(self):
        self._is_open = False
        self._is_started = False
        self._is_closed = True

    def drain(self, timeout=0.0):
        if self.drain_delay:
            time.sleep(self.drain_delay)
        out = list(self._frames)
        self._frames.clear()
        return out

    def get_callback_error(self):
        return self._callback_error

    def drain_events(self):
        out, self.events = self.events, []
        return out

    def push_fake_audio(self, num_bytes: int = 1024, value: int = 3000,
                        timestamp: float | None = None):
        """Simulates the callback: adds a frame and updates the LevelMeter."""
        # Create fake int16 samples — a sine wave so that the level is non-zero
        import struct
        n = num_bytes // 2
        data = struct.pack(f'<{n}h', *([value] * n))
        ts = time.time() if timestamp is None else timestamp
        frame = AudioFrame(data=data, timestamp=ts, frame_count=n)
        self._frames.append(frame)
        self.level.update(data)


class FakeAppSource(FakeRecorder):
    """Mock AppAudioSource: created through app_source_factory(spec, rate, ch)."""

    app_instances: list["FakeAppSource"] = []

    def __init__(self, spec, sample_rate, channels):
        super().__init__(device_index=None, sample_rate=sample_rate, channels=channels)
        FakeRecorder.instances.remove(self)  # system recorders are counted separately
        self.spec = spec
        FakeAppSource.app_instances.append(self)


class FakeWriter:
    """Mock ChunkedPcmWriter without file operations."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.buffer = bytearray()
        self.flushed = bytearray()
        self._total = 0
        self._closed = False
        self._lock = threading.Lock()
        self.flush_count = 0

    def open(self): pass

    def append(self, data: bytes):
        with self._lock:
            if self._closed:
                raise RuntimeError("closed")
            self.buffer.extend(data)

    def flush(self, fsync: bool = True):
        with self._lock:
            n = len(self.buffer)
            self.flushed.extend(self.buffer)
            self.buffer.clear()
            self._total += n
            self.flush_count += 1
            return n

    def close(self):
        with self._lock:
            if self._closed:
                return
            n = len(self.buffer)
            self.flushed.extend(self.buffer)
            self.buffer.clear()
            self._total += n
            self._closed = True

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeRecorder.reset()
    FakeAppSource.app_instances = []
    yield
    FakeRecorder.reset()
    FakeAppSource.app_instances = []


@pytest.fixture(autouse=True)
def fake_device_params(monkeypatch):
    """The device parameters are a hardware boundary too, and must be faked.

    `RecordingService._open_stream` asks `resolve_device_params()` BEFORE
    creating the recorder through `recorder_factory` — so replacing the factory is not enough.
    The real function goes to WASAPI, and on a machine where index 5/10/20 has no
    input channels it raises `DeviceNotAvailableError`, which `_resolve_params`
    deliberately does NOT swallow (native WASAPI SEGVs on `open()` of a nonexistent device).
    Because of this the whole file failed depending on which sound devices are in the
    machine, contrary to the promise in the module docstring («independent of the presence of
    audio devices»).

    The fake returns exactly what was asked — «the device supports the target parameters».
    A test that deliberately checks a device refusal must replace this itself.
    """
    monkeypatch.setattr(
        'app.services.recording.service.resolve_device_params',
        lambda device_index, sample_rate, channels: (sample_rate, channels),
    )


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / 'sessions')


@pytest.fixture
def service(store: SessionStore) -> RecordingService:
    return RecordingService(
        store=store,
        sse_broker=None,  # no SSE
        chunk_seconds=1,  # a short tick for tests
        sample_rate=48000,
        channels=2,
        recorder_factory=FakeRecorder,
        writer_factory=FakeWriter,
        app_source_factory=FakeAppSource,
    )


# ---------------------------------------------------------------- start

def test_start_returns_session_id_and_marks_recording(
    service: RecordingService, store: SessionStore
):
    sid = service.start(mic_device_index=10, system_device_index=20,
                        mic_device_name='MicX', system_device_name='SysY')
    assert sid.startswith('rec_')
    assert service.active_session_id == sid

    mf = store.read(sid)
    assert mf['status'] == STATUS_RECORDING
    assert mf['streams'][STREAM_MIC]['device'] == 'MicX'
    assert mf['streams'][STREAM_SYSTEM]['device'] == 'SysY'
    # cleanup
    service.discard(sid)


def test_start_without_streams_raises(service: RecordingService):
    with pytest.raises(RecordingError):
        service.start(mic_device_index=None, system_device_index=None)


def test_start_beyond_max_active_raises_session_limit(service: RecordingService):
    a = service.start(mic_device_index=10)
    b = service.start(mic_device_index=11)
    try:
        with pytest.raises(SessionConflictError) as ei:
            service.start(mic_device_index=12)
        assert ei.value.code == 'SESSION_LIMIT'
        assert ei.value.active == [a, b]
        assert ei.value.limit == 2
    finally:
        service.discard(a)
        service.discard(b)


def test_start_with_only_mic(service: RecordingService, store: SessionStore):
    sid = service.start(mic_device_index=5, mic_device_name='Mic only')
    try:
        mf = store.read(sid)
        assert mf['streams'][STREAM_MIC]['enabled'] is True
        assert mf['streams'][STREAM_SYSTEM]['enabled'] is False
    finally:
        service.discard(sid)


# ---------------------------------------------------------------- pause/resume

def test_pause_resume_flow(service: RecordingService, store: SessionStore):
    sid = service.start(mic_device_index=10, system_device_index=20)
    try:
        service.pause(sid)
        mf = store.read(sid)
        assert mf['status'] == STATUS_PAUSED
        # The last segment is closed
        assert mf['segments'][-1]['end_ts'] is not None

        service.resume(sid)
        mf = store.read(sid)
        assert mf['status'] == STATUS_RECORDING
        # The new segment is open
        assert len(mf['segments']) == 2
        assert mf['segments'][-1]['end_ts'] is None
    finally:
        service.discard(sid)


def test_pause_idempotent(service: RecordingService, store: SessionStore):
    sid = service.start(mic_device_index=10)
    try:
        service.pause(sid)
        service.pause(sid)  # second pause is no-op
        mf = store.read(sid)
        assert mf['status'] == STATUS_PAUSED
    finally:
        service.discard(sid)


def test_resume_without_pause_is_noop(service: RecordingService, store: SessionStore):
    sid = service.start(mic_device_index=10)
    try:
        service.resume(sid)  # no pause was set — should not raise/break
        mf = store.read(sid)
        assert mf['status'] == STATUS_RECORDING
    finally:
        service.discard(sid)


# ---------------------------------------------------------------- stop

def test_stop_finalizes_state(service: RecordingService, store: SessionStore):
    sid = service.start(mic_device_index=10, system_device_index=20)
    service.stop(sid, name='Test recording')
    assert service.active_session_id is None
    mf = store.read(sid)
    assert mf['status'] == STATUS_STOPPING  # finalize callback is not set
    assert mf['name'] == 'Test recording'


def test_stop_invokes_finalize_callback(store: SessionStore):
    finalize_calls: list[str] = []
    svc = RecordingService(
        store=store, sse_broker=None, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
        finalize_callback=finalize_calls.append,
    )
    sid = svc.start(mic_device_index=10)
    svc.stop(sid)
    assert finalize_calls == [sid]


def test_stop_without_active_session(service: RecordingService):
    with pytest.raises(SessionNotFoundError):
        service.stop('rec_nonexistent')


def test_unknown_sid_is_not_found_on_every_per_session_method(service: RecordingService):
    """WRONG_SESSION is gone: a foreign sid with a live session — simply not found."""
    from app.services.recording.audio_source import SystemSpec
    sid = service.start(mic_device_index=10, system_device_index=20)
    try:
        for call in (
            lambda: service.pause('rec_other'),
            lambda: service.resume('rec_other'),
            lambda: service.stop('rec_other'),
            lambda: service.get_state('rec_other'),
            lambda: service.retarget_system('rec_other', SystemSpec('all')),
        ):
            with pytest.raises(SessionNotFoundError):
                call()
        assert service.is_active(sid)
    finally:
        service.discard(sid)


# ---------------------------------------------------------------- discard

def test_discard_removes_session(service: RecordingService, store: SessionStore):
    sid = service.start(mic_device_index=10)
    service.discard(sid)
    assert service.active_session_id is None
    assert store.exists(sid) is False


# ---------------------------------------------------------------- get_state

def test_get_state_active_session(service: RecordingService):
    sid = service.start(mic_device_index=10, system_device_index=20,
                        mic_device_name='M', system_device_name='S')
    try:
        # Simulate audio so that the level is non-zero
        FakeRecorder.instances[0].push_fake_audio(2048)
        time.sleep(0.05)
        state = service.get_state(sid)
        assert state['session_id'] == sid
        assert state['is_active'] is True
        assert state['streams'][STREAM_MIC]['enabled'] is True
        assert state['streams'][STREAM_SYSTEM]['enabled'] is True
        assert 'peak' in state['streams'][STREAM_MIC]
        assert 'rms' in state['streams'][STREAM_MIC]
    finally:
        service.discard(sid)


def test_get_state_finalized_session(service: RecordingService, store: SessionStore):
    sid = service.start(mic_device_index=10)
    service.stop(sid, name='Done')
    state = service.get_state(sid)
    assert state['is_active'] is False
    assert state['name'] == 'Done'


def test_get_state_nonexistent(service: RecordingService):
    with pytest.raises(SessionNotFoundError):
        service.get_state('rec_nope')


# ---------------------------------------------------------------- flush thread

def test_flush_loop_drains_audio_into_writer(
    service: RecordingService, store: SessionStore
):
    """Check that flush_loop really takes frames from the recorder,
    writes to the writer and updates the manifest. We wait up to 3 seconds
    (chunk_seconds=1)."""
    sid = service.start(mic_device_index=10)
    try:
        rec = FakeRecorder.instances[0]
        for _ in range(5):
            rec.push_fake_audio(num_bytes=4096)

        # We wait for the invariant: manifest.streams.mic.bytes >= 4096*5.
        # This guarantees both the flush and update_stream_bytes.
        deadline = time.time() + 3.0
        last_bytes = 0
        while time.time() < deadline:
            mf = store.read(sid)
            last_bytes = mf['streams'][STREAM_MIC]['bytes']
            if last_bytes >= 4096 * 5:
                break
            time.sleep(0.05)

        assert last_bytes >= 4096 * 5, (
            f"manifest.bytes={last_bytes}, expected >= {4096 * 5}"
        )

        active_writer = next(iter(service._sessions[sid].streams.values())).writer
        assert active_writer.flush_count >= 1
    finally:
        service.discard(sid)


def test_flush_loop_publishes_levels_to_sse():
    """If a broker is set — level events are published."""
    class MockBroker:
        def __init__(self):
            self.events: list[tuple[str, str, dict]] = []
            self._lock = threading.Lock()

        def publish(self, channel, event, data):
            with self._lock:
                self.events.append((channel, event, data))

    broker = MockBroker()
    store = SessionStore(Path(__file__).parent.parent / '.tmp_test_sse')
    # Clean up tmp
    import shutil
    shutil.rmtree(store.base_dir, ignore_errors=True)
    store.base_dir.mkdir(parents=True)

    svc = RecordingService(
        store=store, sse_broker=broker, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
    )
    sid = svc.start(mic_device_index=10)
    try:
        FakeRecorder.instances[0].push_fake_audio(4096)
        time.sleep(0.4)  # enough for level events to appear (10Hz)
        events = list(broker.events)
        kinds = {ev[1] for ev in events}
        assert 'status' in kinds  # initial status=recording
        assert 'level' in kinds
    finally:
        svc.discard(sid)
        shutil.rmtree(store.base_dir, ignore_errors=True)


# ---------------------------------------------------------------- recovery

def test_recover_orphaned_marks_crashed(store: SessionStore):
    # Create «orphaned» sessions on disk BEFORE creating the service
    store.create('rec_orphan_a', 48000, 2)  # status=recording
    store.create('rec_orphan_b', 48000, 2)
    store.update_status('rec_orphan_b', STATUS_PAUSED)
    store.create('rec_finalized', 48000, 2)
    store.update_status('rec_finalized', 'finalized')

    svc = RecordingService(
        store=store, sse_broker=None, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
    )
    recovered = svc.recover_orphaned()
    assert set(recovered) == {'rec_orphan_a', 'rec_orphan_b'}
    assert store.read('rec_orphan_a')['status'] == STATUS_CRASHED
    assert store.read('rec_orphan_b')['status'] == STATUS_CRASHED
    assert store.read('rec_finalized')['status'] == 'finalized'


def test_recover_orphaned_invokes_finalize_callback(store: SessionStore):
    store.create('rec_orphan', 48000, 2)
    finalized: list[str] = []
    svc = RecordingService(
        store=store, sse_broker=None, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
        finalize_callback=finalized.append,
    )
    svc.recover_orphaned()
    assert finalized == ['rec_orphan']


def test_recover_orphaned_no_active_sessions(store: SessionStore):
    svc = RecordingService(
        store=store, sse_broker=None,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
    )
    assert svc.recover_orphaned() == []


# ---------------------------------------------------------------- list_devices

def test_list_devices_returns_list(service: RecordingService):
    """Just check that the method does not fail — the real WASAPI is used."""
    devices = service.list_devices()
    assert isinstance(devices, list)
    # We do not check the content — on CI there may be 0 devices


# ------------------------------------------- recovery: an unfinished finalize

def _crash_without_mp3(store: SessionStore, sid: str) -> None:
    """A session that fell over BEFORE final.mp3 was created — the state after a restart
    that killed the finalize job itself."""
    store.create(sid, 48000, 2)
    store.update_status(sid, STATUS_CRASHED)


def test_recover_unfinalized_requeues_crashed_without_mp3(store: SessionStore):
    _crash_without_mp3(store, 'rec_stuck')
    finalized: list[str] = []
    svc = RecordingService(
        store=store, sse_broker=None, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
        finalize_callback=finalized.append,
    )
    assert svc.recover_unfinalized() == ['rec_stuck']
    assert finalized == ['rec_stuck']
    assert store.read('rec_stuck')['finalize_attempts'] == 1


def test_recover_unfinalized_skips_sessions_with_live_mp3(store: SessionStore):
    """crashed, but the MP3 is on disk — there is nothing to finalize, that is reconcile's job."""
    store.create('rec_has_mp3', 48000, 2)
    mp3 = store.session_dir('rec_has_mp3') / 'final.mp3'
    mp3.write_bytes(b'id3')
    store.modify('rec_has_mp3', lambda m: m.update({
        'status': STATUS_CRASHED, 'final_mp3_path': str(mp3),
    }))
    finalized: list[str] = []
    svc = RecordingService(
        store=store, sse_broker=None, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
        finalize_callback=finalized.append,
    )
    assert svc.recover_unfinalized() == []
    assert finalized == []


def test_recover_unfinalized_stops_after_max_attempts(store: SessionStore):
    """A deterministically broken recording must not grind a heavy job at every start."""
    from app.services.recording.service import MAX_FINALIZE_ATTEMPTS

    _crash_without_mp3(store, 'rec_broken')
    finalized: list[str] = []
    svc = RecordingService(
        store=store, sse_broker=None, chunk_seconds=1,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
        finalize_callback=finalized.append,
    )
    for _ in range(MAX_FINALIZE_ATTEMPTS + 2):
        svc.recover_unfinalized()
    assert len(finalized) == MAX_FINALIZE_ATTEMPTS
    assert store.read('rec_broken')['finalize_attempts'] == MAX_FINALIZE_ATTEMPTS


def test_recover_unfinalized_noop_without_callback(store: SessionStore):
    _crash_without_mp3(store, 'rec_stuck')
    svc = RecordingService(
        store=store, sse_broker=None,
        recorder_factory=FakeRecorder, writer_factory=FakeWriter,
    )
    assert svc.recover_unfinalized() == []


# ------------------------------------ multi-session + SystemSpec (ADR-026, story 04)

from app.services.recording.audio_source import SystemSpec, UnsupportedSourceSpecError
from app.services.recording.service import NoSystemStreamError

_BPS = 48000 * 2 * 2  # bytes/s of the 48000/2 s16le format


class _Broker:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []
        self._lock = threading.Lock()

    def publish(self, channel, event, data):
        with self._lock:
            self.events.append((channel, event, data))

    def of(self, kind):
        with self._lock:
            return [e for e in self.events if e[1] == kind]


def _svc(store, **kw):
    kw.setdefault('recorder_factory', FakeRecorder)
    kw.setdefault('writer_factory', FakeWriter)
    kw.setdefault('app_source_factory', FakeAppSource)
    return RecordingService(store=store, chunk_seconds=1, **kw)


def _written(svc, sid, stream=STREAM_SYSTEM) -> bytes:
    w = svc._sessions[sid].streams[stream].writer
    with w._lock:
        return bytes(w.flushed) + bytes(w.buffer)


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_two_sessions_are_independent_and_stop_does_not_wait_for_neighbour(store):
    svc = _svc(store)
    a = svc.start(mic_device_index=10)
    b = svc.start(mic_device_index=11)
    rec_b = FakeRecorder.instances[1]
    try:
        assert svc.active_session_ids() == [a, b]
        svc.pause(a)
        assert store.read(a)['status'] == STATUS_PAUSED
        assert store.read(b)['status'] == STATUS_RECORDING
        svc.resume(a)
        assert store.read(a)['status'] == STATUS_RECORDING

        rec_b.drain_delay = 1.0  # B's flush thread «hangs» in drain under B's lock
        time.sleep(0.25)
        t0 = time.perf_counter()
        svc.stop(a)
        took = time.perf_counter() - t0
        assert took < 0.5, f"stop(A) waited for B: {took:.2f}s"
        assert svc.active_session_ids() == [b]
        assert store.read(b)['status'] == STATUS_RECORDING
    finally:
        rec_b.drain_delay = 0.0
        svc.discard(b)


def test_active_session_id_only_when_exactly_one(service: RecordingService):
    assert service.active_session_ids() == []
    assert service.active_session_id is None
    a = service.start(mic_device_index=10)
    assert service.active_session_id == a
    b = service.start(mic_device_index=11)
    try:
        assert service.active_session_ids() == [a, b]
        assert service.active_session_id is None
        assert service.is_active(a) and service.is_active(b)
        assert not service.is_active('rec_nope')
        service.stop(a)
        assert not service.is_active(a)
        assert service.active_session_id == b
    finally:
        service.discard(b)


def test_start_all_without_spec_uses_recorder_factory_on_endpoint(service, store):
    sid = service.start(system_device_index=3, system_device_name='Speakers')
    try:
        assert [r.device_index for r in FakeRecorder.instances] == [3]
        assert FakeAppSource.app_instances == []
        sys_mf = store.read(sid)['streams'][STREAM_SYSTEM]
        assert sys_mf['source'] == {'mode': 'all', 'apps': [], 'device_index': 3}
        assert sys_mf['source_events'] == []
        state = service.get_state(sid)
        assert state['system_source'] == {'mode': 'all', 'apps': [], 'device_index': 3}
        assert state['source_events'] == 0
    finally:
        service.discard(sid)


def test_start_exclude_uses_app_source_and_never_touches_devices(
    service, store, monkeypatch,
):
    def _boom(*a, **k):
        raise AssertionError("resolve_device_params hit for include/exclude")
    monkeypatch.setattr('app.services.recording.service.resolve_device_params', _boom)
    spec = SystemSpec('exclude', ('zoom.exe',))
    sid = service.start(system_source=spec)
    try:
        assert FakeRecorder.instances == []
        assert len(FakeAppSource.app_instances) == 1
        src = FakeAppSource.app_instances[0]
        assert (src.spec, src.sample_rate, src.channels) == (spec, 48000, 2)
        sys_mf = store.read(sid)['streams'][STREAM_SYSTEM]
        assert sys_mf['source'] == spec.to_dict()
        assert sys_mf['enabled'] is True
        assert (sys_mf['sample_rate'], sys_mf['channels']) == (48000, 2)
    finally:
        service.discard(sid)


def test_start_invalid_spec_creates_nothing(service, store):
    with pytest.raises(UnsupportedSourceSpecError):
        service.start(system_source=SystemSpec('exclude', ('a.exe', 'b.exe')))
    assert store.list_all() == []
    with pytest.raises(RecordingError) as ei:
        service.start(system_source=SystemSpec('all'))
    assert ei.value.code == 'NO_SYSTEM_DEVICE'


def test_app_source_start_failure_cleans_up_like_open_failure(store):
    class FailingAppSource(FakeAppSource):
        def start(self):
            raise OSError("process-loopback activation failed")

    svc = _svc(store, app_source_factory=FailingAppSource)
    with pytest.raises(RecordingError):
        svc.start(mic_device_index=3, system_source=SystemSpec('exclude', ('zoom.exe',)))
    assert FakeRecorder.instances[0]._is_closed is True
    assert FakeAppSource.app_instances[0]._is_closed is True
    assert store.list_all()[0]['status'] == STATUS_CRASHED
    assert svc.active_session_ids() == []


def _retarget_with_hole(store, monkeypatch, recorded_seconds: float):
    """A live 'all' → include; the old one recorded recorded_seconds from 2.0 active."""
    broker = _Broker()
    created: list[FakeAppSource] = []

    def factory(spec, rate, ch):
        src = FakeAppSource(spec, rate, ch)
        # a frame captured by the new source BEFORE the moment of the swap — must be dropped
        src.push_fake_audio(400, value=7, timestamp=time.time() - 0.001)
        created.append(src)
        return src

    svc = _svc(store, sse_broker=broker, app_source_factory=factory)
    monkeypatch.setattr(RecordingService, '_stream_elapsed',
                        staticmethod(lambda active, ss: 2.0))
    sid = svc.start(system_device_index=20, system_device_name='Speakers')
    old = FakeRecorder.instances[0]
    old.push_fake_audio(int(recorded_seconds * _BPS), value=1)
    res = svc.retarget_system(sid, SystemSpec('include', ('zoom.exe',)), 'user')
    return svc, broker, sid, old, created, res


def test_retarget_pads_hole_and_orders_frames(store, monkeypatch):
    svc, broker, sid, old, created, res = _retarget_with_hole(store, monkeypatch, 1.7)
    try:
        assert old._is_closed
        assert len(created) == 1 and created[0]._is_started
        created[0].push_fake_audio(1000, value=9)
        assert _wait(lambda: _written(svc, sid).endswith(b'\x09\x00' * 500))

        residual = int(1.7 * _BPS)
        pad = int(2.0 * _BPS) - residual
        assert _written(svc, sid) == (
            b'\x01\x00' * (residual // 2) + bytes(pad) + b'\x09\x00' * 500
        )
        ev = store.read(sid)['streams'][STREAM_SYSTEM]['source_events'][-1]
        assert ev['from'] == {'mode': 'all', 'apps': [], 'device_index': 20}
        assert ev['to'] == {'mode': 'include', 'apps': ['zoom.exe'], 'device_index': None}
        assert ev['gap_ms'] == pytest.approx(300, abs=1)
        assert ev['padded_bytes'] == pad
        assert ev['offset_bytes'] == residual + pad
        assert ev['reason'] == 'user'
        assert res['applied_on_resume'] is False
        assert res['source'] == ev['to']
        assert store.read(sid)['streams'][STREAM_SYSTEM]['source'] == ev['to']
        assert len(broker.of('source_changed')) == 1
        assert svc.get_state(sid)['source_events'] == 1
    finally:
        svc.discard(sid)


def test_retarget_short_hole_is_not_padded(store, monkeypatch):
    svc, _b, sid, _old, _c, res = _retarget_with_hole(store, monkeypatch, 1.99)
    try:
        assert res['event']['padded_bytes'] == 0
        assert res['event']['gap_ms'] == pytest.approx(10, abs=1)
    finally:
        svc.discard(sid)


def test_retarget_back_to_all_keeps_stream_format(service, store):
    """An 'all' stream returns to its own endpoint; an include stream — to 'all' through the app source."""
    a = service.start(system_device_index=20)
    b = service.start(system_source=SystemSpec('include', ('zoom.exe',)))
    try:
        service.retarget_system(a, SystemSpec('exclude', ('zoom.exe',)))
        service.retarget_system(a, SystemSpec('all'), 'auto_restore')
        assert [r.device_index for r in FakeRecorder.instances] == [20, 20]
        assert store.read(a)['streams'][STREAM_SYSTEM]['source']['device_index'] == 20

        service.retarget_system(b, SystemSpec('all'))
        last = FakeAppSource.app_instances[-1]
        assert last.spec == SystemSpec('all')
        assert len(FakeRecorder.instances) == 2  # the endpoint for b was not opened
        assert store.read(b)['streams'][STREAM_SYSTEM]['source_events'][-1]['to'] == {
            'mode': 'all', 'apps': [], 'device_index': None}
    finally:
        service.discard(a)
        service.discard(b)


def test_retarget_while_paused_applies_on_resume(service, store):
    sid = service.start(system_device_index=20)
    try:
        service.pause(sid)
        res = service.retarget_system(sid, SystemSpec('include', ('zoom.exe',)))
        assert res['applied_on_resume'] is True
        assert res['event']['applied_on_resume'] is True
        assert FakeAppSource.app_instances == []
        service.resume(sid)
        assert len(FakeAppSource.app_instances) == 1
        assert FakeAppSource.app_instances[0].spec == SystemSpec('include', ('zoom.exe',))
        assert FakeAppSource.app_instances[0]._is_started
    finally:
        service.discard(sid)


def test_retarget_errors_change_nothing(service, store):
    mic_only = service.start(mic_device_index=10)
    sysd = service.start(system_device_index=20)
    try:
        with pytest.raises(NoSystemStreamError) as ei:
            service.retarget_system(mic_only, SystemSpec('all'))
        assert ei.value.code == 'NO_SYSTEM_STREAM'
        before = store.read(sysd)['streams'][STREAM_SYSTEM]
        with pytest.raises(UnsupportedSourceSpecError):
            service.retarget_system(sysd, SystemSpec('exclude', ()))
        after = store.read(sysd)['streams'][STREAM_SYSTEM]
        assert after['source'] == before['source']
        assert after['source_events'] == []
        assert FakeAppSource.app_instances == []
        assert not FakeRecorder.instances[1]._is_closed
    finally:
        service.discard(mic_only)
        service.discard(sysd)


def test_pid_change_event_lands_in_manifest_and_sse(store):
    broker = _Broker()
    svc = _svc(store, sse_broker=broker)
    sid = svc.start(system_source=SystemSpec('exclude', ('zoom.exe',)))
    try:
        FakeAppSource.app_instances[0].events.append({
            'kind': 'pid_change', 'exe': 'zoom.exe',
            'old_pids': [111], 'new_pids': [222], 'gap_ms': 40.0,
        })
        assert _wait(lambda: store.read(sid)['streams'][STREAM_SYSTEM]['source_events'])
        time.sleep(0.25)
        events = store.read(sid)['streams'][STREAM_SYSTEM]['source_events']
        assert len(events) == 1
        ev = events[0]
        assert ev['reason'] == 'pid_change'
        assert ev['gap_ms'] == 40.0
        assert ev['exe'] == 'zoom.exe'
        assert 'old_pids' not in ev and 'new_pids' not in ev
        assert len(broker.of('source_changed')) == 1
    finally:
        svc.discard(sid)


def test_recover_orphaned_with_source_events_unchanged(store):
    store.create('rec_src', 48000, 2, system_device_name='S',
                 system_source={'mode': 'all', 'apps': [], 'device_index': 20})
    store.set_stream_source('rec_src', STREAM_SYSTEM,
                            {'mode': 'include', 'apps': ['zoom.exe'], 'device_index': None},
                            {'reason': 'user', 'gap_ms': 12.0, 'padded_bytes': 0})
    finalized: list[str] = []
    svc = _svc(store, finalize_callback=finalized.append)
    assert svc.recover_orphaned() == ['rec_src']
    assert store.read('rec_src')['status'] == STATUS_CRASHED
    assert finalized == ['rec_src']
