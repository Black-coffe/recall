"""Тести для app.services.recording.service (Phase 9.3).

Тести використовують fake recorder/writer factories — реальні
WASAPI streams і файли не задіяні. Це робить тести швидкими і
не залежними від наявності аудіо-девайсів на CI.
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
    """Mock WasapiRecorder — без реального WASAPI."""

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
        out = list(self._frames)
        self._frames.clear()
        return out

    def get_callback_error(self):
        return self._callback_error

    def push_fake_audio(self, num_bytes: int = 1024):
        """Імітація callback'у: додає кадр і оновлює LevelMeter."""
        # Створимо фейкові int16 семпли — синусоїда щоб level був ненульовий
        import struct
        n = num_bytes // 2
        data = struct.pack(f'<{n}h', *([3000] * n))
        frame = AudioFrame(data=data, timestamp=time.time(), frame_count=n)
        self._frames.append(frame)
        self.level.update(data)


class FakeWriter:
    """Mock ChunkedPcmWriter без файлових операцій."""
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
    yield
    FakeRecorder.reset()


@pytest.fixture(autouse=True)
def fake_device_params(monkeypatch):
    """Параметри девайса — теж hardware-межа, і її треба фейкати.

    `RecordingService._open_stream` питає `resolve_device_params()` ДО того, як
    створити рекордер через `recorder_factory` — тобто підміни фабрики замало.
    Реальна функція йде в WASAPI, і на машині, де індекс 5/10/20 не має
    input-каналів, кидає `DeviceNotAvailableError`, який `_resolve_params`
    свідомо НЕ глушить (native WASAPI SEGV-ить на `open()` неіснуючого девайса).
    Через це весь файл падав залежно від того, які звукові пристрої стоять у
    машині, всупереч обіцянці в докстрингу модуля («не залежними від наявності
    аудіо-девайсів»).

    Фейк повертає рівно те, що попросили — «девайс підтримує цільові параметри».
    Тест, який навмисно перевірятиме відмову девайса, має підмінити це сам.
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
        sse_broker=None,  # без SSE
        chunk_seconds=1,  # короткий tick для тестів
        sample_rate=48000,
        channels=2,
        recorder_factory=FakeRecorder,
        writer_factory=FakeWriter,
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


def test_second_concurrent_start_raises_conflict(service: RecordingService):
    sid = service.start(mic_device_index=10)
    try:
        with pytest.raises(SessionConflictError):
            service.start(mic_device_index=11)
    finally:
        service.discard(sid)


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
        # Останній segment закритий
        assert mf['segments'][-1]['end_ts'] is not None

        service.resume(sid)
        mf = store.read(sid)
        assert mf['status'] == STATUS_RECORDING
        # Новий segment відкритий
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
    assert mf['status'] == STATUS_STOPPING  # finalize callback не задано
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


def test_stop_wrong_session_id(service: RecordingService):
    sid = service.start(mic_device_index=10)
    try:
        with pytest.raises(SessionConflictError):
            service.stop('rec_other_session')
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
        # Симулюємо аудіо щоб level був ненульовий
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
    """Перевіряємо що flush_loop фактично забирає кадри з recorder'у,
    пише у writer і оновлює manifest. Чекаємо до 3 секунди
    (chunk_seconds=1)."""
    sid = service.start(mic_device_index=10)
    try:
        rec = FakeRecorder.instances[0]
        for _ in range(5):
            rec.push_fake_audio(num_bytes=4096)

        # Чекаємо інваріант: manifest.streams.mic.bytes >= 4096*5.
        # Це гарантує і flush, і update_stream_bytes.
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

        active_writer = next(iter(service._active.streams.values())).writer
        assert active_writer.flush_count >= 1
    finally:
        service.discard(sid)


def test_flush_loop_publishes_levels_to_sse():
    """Якщо broker задано — публікуються level events."""
    class MockBroker:
        def __init__(self):
            self.events: list[tuple[str, str, dict]] = []
            self._lock = threading.Lock()

        def publish(self, channel, event, data):
            with self._lock:
                self.events.append((channel, event, data))

    broker = MockBroker()
    store = SessionStore(Path(__file__).parent.parent / '.tmp_test_sse')
    # Чистимо tmp
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
        time.sleep(0.4)  # достатньо щоб були level events (10Hz)
        events = list(broker.events)
        kinds = {ev[1] for ev in events}
        assert 'status' in kinds  # initial status=recording
        assert 'level' in kinds
    finally:
        svc.discard(sid)
        shutil.rmtree(store.base_dir, ignore_errors=True)


# ---------------------------------------------------------------- recovery

def test_recover_orphaned_marks_crashed(store: SessionStore):
    # Створюємо «осиротілі» сесії на диску ДО створення сервісу
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
    """Просто перевіряємо що метод не падає — реальний WASAPI використовується."""
    devices = service.list_devices()
    assert isinstance(devices, list)
    # Не перевіряємо вміст — на CI може бути 0 девайсів


# ------------------------------------------- recovery: незавершений finalize

def _crash_without_mp3(store: SessionStore, sid: str) -> None:
    """Сесія, що впала ДО створення final.mp3 — стан після рестарту,
    який убив сам finalize-job."""
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
    """crashed, але MP3 на диску є — фіналізувати нічого, це робота reconcile."""
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
    """Детерміновано битий запис не має молотити важкий job на кожному старті."""
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
