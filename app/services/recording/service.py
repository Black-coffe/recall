"""RecordingService — orchestration рівень для recording (Phase 9.3).

Складає докупи :class:`WasapiRecorder` (audio capture) + :class:`ChunkedPcmWriter`
(disk persistence) + :class:`SessionStore` (manifest state) у єдиний
життєвий цикл сесії з підтримкою pause/resume і crash-recovery.

Ключові інваріанти:

- **Single active session**: одночасно може бути активна тільки одна
  сесія. ``start()`` кидає :class:`SessionConflictError` якщо сесія
  вже триває. Це навмисне обмеження single-instance use-case'у.
- **Per-session lock**: усі переходи стану (start/pause/resume/stop)
  серіалізовані через RLock. Не дає race'ів між UI-діями.
- **Audio thread isolation**: PortAudio callback'и НЕ викликають жодних
  методів service'у напряму. Дані з recorder'ів забирає окремий
  flush-thread, який тікає ~10Hz і періодично робить fsync+manifest.
- **SSE-friendly**: на кожен tick публікується ``level`` event
  (peak+rms для mic та system). На chunk-flush — ``chunk_saved``.
  На pause/resume/stop — ``status``. На errors — ``error``.

Lifecycle (FSM):

.. code-block:: text

      ┌─── start() ───┐
      ▼               │
    [recording] ──pause()──→ [paused] ──resume()─┘
      │                          │
      └────── stop() ─────► [stopping] ──finalize()──→ [finalized]
                                  │
                                  └──crash──→ [crashed] (recover_orphaned)

Finalize (Phase 9.4) — окремий job у :class:`JobQueue`. Service
тільки переводить status=stopping і шле фоновий job; коли той
завершується — status=finalized.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.services.recording.pcm_writer import ChunkedPcmWriter
from app.services.recording.recorder import (
    DeviceNotAvailableError,
    LevelMeter,
    WasapiRecorder,
    list_input_devices,
    resolve_device_params,
)
from app.services.recording.session_store import (
    STATUS_CRASHED,
    STATUS_FINALIZED,
    STATUS_PAUSED,
    STATUS_RECORDING,
    STATUS_STOPPING,
    STREAM_MIC,
    STREAM_SYSTEM,
    SessionStore,
    SessionStoreError,
)


logger = logging.getLogger(__name__)


# Скільки разів boot-recovery пробує дофіналізувати crashed-сесію
# (див. RecordingService.recover_unfinalized). 3 покриває ланцюжок
# рестартів; далі — ручне втручання, щоб не молотити важкий job вічно.
MAX_FINALIZE_ATTEMPTS = 3


# Tick rate для drain+level-publish. 10Hz = 100ms — достатньо плавно
# для VU без CPU overhead.
_TICK_INTERVAL = 0.1


class RecordingError(RuntimeError):
    """Базова помилка для service'у."""


class SessionConflictError(RecordingError):
    """Спроба запустити нову сесію коли є активна, або діяти на чужу."""


class SessionNotFoundError(RecordingError):
    """sid не знайдено в активних і не існує на диску."""


# ----------------------------------------------------------------- factories

# Factory-типи дозволяють DI у тестах: підставити mock recorder/writer
# без реальних WASAPI стрімів і файлових операцій.
RecorderFactory = Callable[..., Any]  # повертає об'єкт зі start/stop/close/drain/level/get_callback_error
WriterFactory = Callable[..., Any]    # повертає об'єкт зі append/flush/close/total_bytes


def _default_recorder_factory(**kwargs) -> WasapiRecorder:
    return WasapiRecorder(**kwargs)


def _default_writer_factory(path: Path) -> ChunkedPcmWriter:
    w = ChunkedPcmWriter(path)
    w.open()
    return w


# ----------------------------------------------------------------- internals

@dataclass
class _StreamState:
    """Стан одного потоку (mic або system) у активній сесії."""
    name: str  # 'mic' | 'system'
    recorder: Any  # WasapiRecorder-подібний
    writer: Any    # ChunkedPcmWriter-подібний
    device_name: str

    @property
    def level(self) -> LevelMeter:
        return self.recorder.level


@dataclass
class _ActiveSession:
    session_id: str
    streams: dict[str, _StreamState] = field(default_factory=dict)
    flush_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)
    paused_total_seconds: float = 0.0
    paused_at: Optional[float] = None
    last_chunk_ts: float = 0.0
    is_paused: bool = False
    video_supervisor: Optional[Any] = None

    def stream_iter(self):
        for name in (STREAM_MIC, STREAM_SYSTEM):
            ss = self.streams.get(name)
            if ss is not None:
                yield ss

    def elapsed_seconds(self) -> float:
        """Скільки секунд активного запису (без пауз)."""
        now = time.time()
        if self.is_paused and self.paused_at is not None:
            cur_pause = now - self.paused_at
        else:
            cur_pause = 0.0
        return max(0.0, (now - self.started_at) - self.paused_total_seconds - cur_pause)


# ----------------------------------------------------------------- service

class RecordingService:
    """Контролер запису. Singleton у app.state.recording_service."""

    def __init__(
        self,
        store: SessionStore,
        sse_broker: Optional[Any] = None,
        chunk_seconds: int = 5,
        sample_rate: int = 48000,
        channels: int = 2,
        recorder_factory: Optional[RecorderFactory] = None,
        writer_factory: Optional[WriterFactory] = None,
        finalize_callback: Optional[Callable[[str], None]] = None,
        video_supervisor_factory: Optional[Callable] = None,
    ):
        self._store = store
        self._broker = sse_broker
        self._chunk_seconds = chunk_seconds
        self._sample_rate = sample_rate
        self._channels = channels
        self._recorder_factory = recorder_factory or _default_recorder_factory
        self._writer_factory = writer_factory or _default_writer_factory
        self._finalize_callback = finalize_callback
        self._video_factory = video_supervisor_factory

        self._active: Optional[_ActiveSession] = None
        self._lock = threading.RLock()  # серіалізує start/pause/resume/stop

    # ----------------------------------------------------------- queries

    @property
    def active_session_id(self) -> Optional[str]:
        with self._lock:
            return self._active.session_id if self._active else None

    @property
    def store(self) -> SessionStore:
        """Public accessor для blueprint'у — потрібно щоб /save endpoint
        міг прочитати manifest після finalize."""
        return self._store

    def list_devices(self) -> list[dict]:
        """Енумерація mic + loopback девайсів (для UI dropdown)."""
        return [d.to_dict() for d in list_input_devices()]

    def get_state(self, session_id: str) -> dict:
        """Поточний snapshot стану сесії (для polling fallback'у)."""
        with self._lock:
            active = self._active
        if active is None or active.session_id != session_id:
            # Не активна — читаємо з manifest'у (можливо finalized)
            try:
                manifest = self._store.read(session_id)
            except SessionStoreError:
                raise SessionNotFoundError(session_id)
            return {
                'session_id': session_id,
                'status': manifest.get('status'),
                'is_active': False,
                'elapsed_seconds': 0.0,
                'streams': {
                    name: {
                        'enabled': manifest['streams'][name].get('enabled', False),
                        'device': manifest['streams'][name].get('device'),
                        'bytes': manifest['streams'][name].get('bytes', 0),
                        'error': manifest['streams'][name].get('error'),
                        'peak': 0.0,
                        'rms': 0.0,
                    }
                    for name in (STREAM_MIC, STREAM_SYSTEM)
                },
                'segments_count': len(manifest.get('segments', [])),
                'name': manifest.get('name'),
                'auto_name': manifest.get('auto_name'),
                'video_tracks': manifest.get('streams', {}).get('video', []),
            }

        # Active — складаємо live snapshot
        manifest = self._store.read(session_id)
        return {
            'session_id': session_id,
            'status': manifest.get('status'),
            'is_active': True,
            'is_paused': active.is_paused,
            'elapsed_seconds': active.elapsed_seconds(),
            'last_chunk_ts': active.last_chunk_ts,
            'streams': self._snapshot_streams(active, manifest),
            'segments_count': len(manifest.get('segments', [])),
            'name': manifest.get('name'),
            'auto_name': manifest.get('auto_name'),
            'video_tracks': manifest.get('streams', {}).get('video', []),
            **self._disk_info(),
        }

    # ----------------------------------------------------------- lifecycle

    def _video_call(self, active: '_ActiveSession', method: str, **kw) -> None:
        """Fire-and-forget bridge to VideoCaptureSupervisor.

        Double-guarded: VideoCaptureSupervisor's own methods never raise, but
        we wrap the call site too for defense-in-depth. Audio paths are
        never affected by any exception here.
        """
        sup = getattr(active, 'video_supervisor', None)
        if sup is None:
            return
        try:
            getattr(sup, method)(**kw)
        except Exception as e:
            logger.warning("video %s failed (audio unaffected): %s", method, e)

    def start(
        self,
        mic_device_index: Optional[int] = None,
        system_device_index: Optional[int] = None,
        mic_device_name: Optional[str] = None,
        system_device_name: Optional[str] = None,
        video_spec: Optional[dict] = None,
    ) -> str:
        """Розпочати нову сесію. Принаймні один з потоків мусить бути
        вказаний (mic_device_index або system_device_index).

        Повертає ``session_id``.
        Кидає :class:`SessionConflictError` якщо вже є active session.

        ``video_spec`` — опціональний словник ``{'enabled': bool, 'tracks': [...]}``
        для запуску відео-захоплення. Відео НІКОЛИ не блокує і не змінює
        аудіо-шлях: будь-яка помилка відео логується і відкидається.
        """
        with self._lock:
            if self._active is not None:
                raise SessionConflictError(
                    f"Активна сесія вже триває: {self._active.session_id}"
                )
            if mic_device_index is None and system_device_index is None:
                raise RecordingError(
                    "Потрібен хоча б один потік (mic_device_index або system_device_index)"
                )

            session_id = self._make_session_id()
            self._store.create(
                session_id=session_id,
                sample_rate=self._sample_rate,
                channels=self._channels,
                mic_device_name=mic_device_name,
                system_device_name=system_device_name,
            )

            active = _ActiveSession(session_id=session_id)
            try:
                if mic_device_index is not None:
                    self._open_stream(
                        active, STREAM_MIC, mic_device_index,
                        mic_device_name or f'mic#{mic_device_index}',
                    )
                if system_device_index is not None:
                    self._open_stream(
                        active, STREAM_SYSTEM, system_device_index,
                        system_device_name or f'sys#{system_device_index}',
                    )
            except Exception as e:
                logger.exception("start() failed: %s", e)
                self._cleanup_streams(active)
                self._store.update_status(session_id, STATUS_CRASHED)
                raise RecordingError(f"Не вдалось відкрити потік: {e}") from e

            for ss in active.stream_iter():
                ss.recorder.start()

            active.stop_event = threading.Event()
            t = threading.Thread(
                target=self._flush_loop,
                args=(active,),
                name=f'recording-flush-{session_id}',
                daemon=True,
            )
            active.flush_thread = t
            self._active = active
            t.start()
            self._publish(active, 'status', {'status': STATUS_RECORDING})
            logger.info("Recording session %s started", session_id)

            # --- Video capture (MUST be last; audio is already live) ---
            # Any failure here is logged and dropped; audio path is unchanged.
            if video_spec and video_spec.get('enabled') and self._video_factory is not None:
                try:
                    session_dir = self._store.session_dir(session_id)
                    sup = self._video_factory(
                        session_id=session_id,
                        session_dir=session_dir,
                        tracks_spec=video_spec.get('tracks', []),
                        audio_start_wallclock=active.started_at,
                        store=self._store,
                        broker=self._broker,
                    )
                    sup.start()
                    active.video_supervisor = sup
                except Exception as e:
                    logger.warning("video start failed (audio unaffected): %s", e)

            return session_id

    def pause(self, session_id: str) -> None:
        """Поставити паузу. Закриває WASAPI стріми (звільняє девайси для
        інших додатків) і закриває поточний segment."""
        with self._lock:
            active = self._require_active(session_id)
            if active.is_paused:
                return
            self._stop_streams(active)
            self._flush_writers(active, fsync=True)
            self._store.close_segment(session_id)
            self._store.update_status(session_id, STATUS_PAUSED)
            active.is_paused = True
            active.paused_at = time.time()
            self._publish(active, 'status', {'status': STATUS_PAUSED})
            self._video_call(active, 'pause')

    def resume(self, session_id: str) -> None:
        """Продовжити запис після pause. Перевідкриває WASAPI стріми
        і відкриває новий segment."""
        with self._lock:
            active = self._require_active(session_id)
            if not active.is_paused:
                return
            # Перевідкриваємо стріми; девайси/параметри ті самі
            for ss in active.stream_iter():
                # WasapiRecorder в нашій реалізації не дозволяє reopen
                # після close. Створюємо новий з тими ж параметрами.
                old_recorder = ss.recorder
                old_recorder.close()
                new_recorder = self._recorder_factory(
                    device_index=old_recorder.device_index,
                    sample_rate=old_recorder.sample_rate,
                    channels=old_recorder.channels,
                )
                new_recorder.open()
                new_recorder.start()
                ss.recorder = new_recorder

            self._store.open_segment(session_id)
            self._store.update_status(session_id, STATUS_RECORDING)
            if active.paused_at is not None:
                active.paused_total_seconds += time.time() - active.paused_at
            active.is_paused = False
            active.paused_at = None
            self._publish(active, 'status', {'status': STATUS_RECORDING})
            self._video_call(active, 'resume')

    def stop(self, session_id: str, name: Optional[str] = None) -> None:
        """Зупинити запис. Запускає finalize в фоні (якщо
        finalize_callback налаштований).

        Викликаючий може потім підписатись на SSE або polling'ом
        чекати ``status='finalized'``.
        """
        with self._lock:
            active = self._require_active(session_id)
            self._store.update_status(session_id, STATUS_STOPPING)
            self._publish(active, 'status', {'status': STATUS_STOPPING})

            # Зупинити flush thread
            active.stop_event.set()
            if active.flush_thread is not None:
                active.flush_thread.join(timeout=5.0)

            self._stop_streams(active)
            self._flush_writers(active, fsync=True)
            self._store.close_segment(session_id)
            for ss in active.stream_iter():
                ss.writer.close()
                self._store.update_stream_bytes(session_id, ss.name, ss.writer.total_bytes)

            if name is not None:
                self._store.set_name(session_id, name)

            self._video_call(active, 'stop')
            self._active = None

        # Finalize — поза lock'ом (може займати кілька секунд)
        if self._finalize_callback is not None:
            try:
                self._finalize_callback(session_id)
            except Exception as e:
                logger.exception("Finalize callback failed for %s: %s", session_id, e)

    def discard(self, session_id: str) -> None:
        """Скасувати поточний запис і видалити сесію без finalize."""
        with self._lock:
            active = self._active
            if active is not None and active.session_id == session_id:
                active.stop_event.set()
                if active.flush_thread is not None:
                    active.flush_thread.join(timeout=5.0)
                self._stop_streams(active)
                self._video_call(active, 'stop', graceful=False)
                for ss in active.stream_iter():
                    ss.writer.close()
                self._active = None
            self._store.delete(session_id)

    # ----------------------------------------------------------- recovery

    def recover_orphaned(self) -> list[str]:
        """Знаходить сесії з активним статусом на диску та позначає їх
        crashed. Повертає список session_id, які потрапили в recovery.

        Реальний finalize (PCM→WAV→MP3) робить :class:`FinalizePipeline`
        у Phase 9.4. Service тут лише позначає статус і викликає
        ``finalize_callback`` (якщо налаштований) для кожної.
        """
        recovered: list[str] = []
        for manifest in self._store.list_orphaned():
            sid = manifest['session_id']
            try:
                self._store.update_status(sid, STATUS_CRASHED)
                recovered.append(sid)
                logger.warning("Recovered orphaned recording session: %s", sid)
                # Best-effort video track recovery — never blocks audio recovery.
                try:
                    self._recover_orphaned_video(sid, manifest)
                except Exception as e:
                    logger.warning("video orphan recovery failed for %s: %s", sid, e)
                if self._finalize_callback is not None:
                    try:
                        self._finalize_callback(sid)
                    except Exception as e:
                        logger.exception(
                            "Recovery finalize failed for %s: %s", sid, e
                        )
            except SessionStoreError as e:
                logger.warning("Cannot recover %s: %s", sid, e)
        return recovered

    def recover_unfinalized(self) -> list[str]:
        """Перезапускає finalize для ``crashed``-сесій, що лишились без MP3.

        Другий прохід recovery (перший — :meth:`recover_orphaned`). Потрібен
        тому, що сам finalize-job теж смертний: рестарт під час фіналізації
        лишав сесію у ``crashed`` назавжди, і жоден інший механізм її не бачив
        (див. :meth:`SessionStore.list_unfinalized`).

        Захист від вічного циклу: кожна спроба інкрементить
        ``finalize_attempts`` у manifest ДО виклику callback'у. Після
        ``MAX_FINALIZE_ATTEMPTS`` сесія більше не ставиться в чергу — інакше
        сесія, яку finalize валить детерміновано (побитий PCM, нема місця на
        диску), крутила б важкий job на кожному старті. Лічильник скидає
        успішний finalize (``status='finalized'`` виводить сесію з вибірки).

        Повертає session_id, поставлені в чергу цього разу.
        """
        requeued: list[str] = []
        if self._finalize_callback is None:
            return requeued
        for manifest in self._store.list_unfinalized():
            sid = manifest['session_id']
            attempts = int(manifest.get('finalize_attempts', 0) or 0)
            if attempts >= MAX_FINALIZE_ATTEMPTS:
                logger.warning(
                    "Сесія %s: finalize провалився %d раз(и) — більше не пробуємо "
                    "автоматично. PCM на диску, потрібне ручне втручання.",
                    sid, attempts,
                )
                continue
            try:
                self._store.modify(
                    sid,
                    lambda m, _a=attempts: m.__setitem__('finalize_attempts', _a + 1),
                )
            except SessionStoreError as e:
                logger.warning("Cannot bump finalize_attempts для %s: %s", sid, e)
                continue
            logger.warning(
                "Незавершена сесія %s (crashed, спроба %d/%d) — повторний finalize",
                sid, attempts + 1, MAX_FINALIZE_ATTEMPTS,
            )
            try:
                self._finalize_callback(sid)
                requeued.append(sid)
            except Exception as e:
                logger.exception("Re-finalize %s не запустився: %s", sid, e)
        return requeued

    def _recover_orphaned_video(self, session_id: str, manifest: dict) -> None:
        """Mark each orphaned video track as finalized (file on disk) or crashed.

        Fragmented-MP4 files survive a hard kill because the encoder writes
        frames incrementally, so if the path exists we treat it as recoverable.
        Wraps every store call so it can never propagate into audio recovery.
        """
        tracks = manifest.get('streams', {}).get('video', [])
        if not tracks:
            return
        session_dir = self._store.session_dir(session_id)
        for track in tracks:
            track_id = track.get('track_id')
            if track_id is None:
                continue
            path_str = track.get('path')
            try:
                if path_str and (session_dir / Path(path_str).name).is_file():
                    self._store.update_video_track(
                        session_id, track_id, status='finalized'
                    )
                else:
                    self._store.update_video_track(
                        session_id, track_id, status='crashed'
                    )
            except Exception as e:
                logger.warning(
                    "video track %s orphan mark failed: %s", track_id, e
                )

    # ----------------------------------------------------------- helpers

    def _require_active(self, session_id: str) -> _ActiveSession:
        if self._active is None:
            raise SessionNotFoundError(f"Немає активної сесії {session_id}")
        if self._active.session_id != session_id:
            raise SessionConflictError(
                f"Запитано {session_id}, активна {self._active.session_id}"
            )
        return self._active

    def _make_session_id(self) -> str:
        return 'rec_' + uuid.uuid4().hex[:16]

    def _open_stream(
        self,
        active: _ActiveSession,
        stream_name: str,
        device_index: int,
        device_name: str,
    ) -> None:
        rate, ch = self._resolve_params(device_index)
        recorder = self._recorder_factory(
            device_index=device_index,
            sample_rate=rate,
            channels=ch,
        )
        recorder.open()
        writer_path = self._store.pcm_path(active.session_id, stream_name)
        writer = self._writer_factory(writer_path)
        active.streams[stream_name] = _StreamState(
            name=stream_name,
            recorder=recorder,
            writer=writer,
            device_name=device_name,
        )
        # Phase 9 fix: зберігаємо actual native rate/channels стріму у
        # manifest. Без цього finalize читав би top-level (target) values
        # — для FOX mic (88200/1) виходив би wav-хедер 48000/2, що
        # викривлює pitch і канали.
        self._store.set_stream_params(
            active.session_id, stream_name,
            sample_rate=rate, channels=ch,
        )

    def _resolve_params(self, device_index: int) -> tuple[int, int]:
        # У тестах де recorder_factory mock — resolve_device_params реальний
        # викликається тільки для реального WASAPI. Тут робимо try/except,
        # бо в mock-сценарії можемо хотіти конкретні rate/ch.
        try:
            return resolve_device_params(device_index, self._sample_rate, self._channels)
        except DeviceNotAvailableError:
            # CRITICAL: native WASAPI SEGV-не якщо ми спробуємо open() на
            # неіснуючий device. Не fallback'нути на defaults тут — abort.
            raise
        except Exception as e:
            logger.warning(
                "resolve_device_params failed for %s: %s — using defaults",
                device_index, e,
            )
            return self._sample_rate, self._channels

    def _stop_streams(self, active: _ActiveSession) -> None:
        for ss in active.stream_iter():
            try:
                ss.recorder.stop()
            except Exception as e:
                logger.warning("recorder.stop() error: %s", e)
            try:
                ss.recorder.close()
            except Exception as e:
                logger.warning("recorder.close() error: %s", e)

    def _flush_writers(self, active: _ActiveSession, fsync: bool) -> None:
        for ss in active.stream_iter():
            try:
                ss.writer.flush(fsync=fsync)
                self._store.update_stream_bytes(
                    active.session_id, ss.name, ss.writer.total_bytes,
                )
            except Exception as e:
                logger.warning("flush %s error: %s", ss.name, e)

    def _cleanup_streams(self, active: _ActiveSession) -> None:
        for ss in active.stream_iter():
            try:
                ss.recorder.close()
            except Exception:
                pass
            try:
                ss.writer.close()
            except Exception:
                pass

    def _disk_info(self) -> dict:
        """Free/total bytes on the volume that holds recordings.

        Best-effort: any OS error degrades to an empty dict so a disk-stat
        failure never disrupts the recording loop or the state endpoint.
        """
        try:
            du = shutil.disk_usage(self._store.base_dir)
            return {'disk_free': du.free, 'disk_total': du.total}
        except Exception:
            return {}

    def _snapshot_streams(self, active: _ActiveSession, manifest: dict) -> dict:
        out: dict[str, dict] = {}
        for name in (STREAM_MIC, STREAM_SYSTEM):
            ss = active.streams.get(name)
            mf_stream = manifest['streams'][name]
            if ss is None:
                out[name] = {
                    'enabled': mf_stream.get('enabled', False),
                    'device': mf_stream.get('device'),
                    'bytes': mf_stream.get('bytes', 0),
                    'error': mf_stream.get('error'),
                    'peak': 0.0, 'rms': 0.0,
                }
            else:
                snap = ss.recorder.level.snapshot()
                out[name] = {
                    'enabled': True,
                    'device': ss.device_name,
                    'bytes': ss.writer.total_bytes,
                    'error': mf_stream.get('error'),
                    'peak': snap['peak'],
                    'rms': snap['rms'],
                    'dropped': getattr(ss.recorder, 'dropped_frames', 0),
                }
        return out

    # ----------------------------------------------------------- flush thread

    def _flush_loop(self, active: _ActiveSession) -> None:
        """Главний цикл сесії: drain queues, publish levels, periodic flush.

        Запускається в окремому daemon thread'і. Виходить коли
        ``active.stop_event`` set'нутий.
        """
        last_flush = time.time()
        try:
            while not active.stop_event.is_set():
                if not active.is_paused:
                    self._drain_into_writers(active)
                    self._publish_levels(active)

                    now = time.time()
                    if now - last_flush >= self._chunk_seconds:
                        self._flush_writers(active, fsync=True)
                        active.last_chunk_ts = now
                        self._publish(active, 'chunk_saved', {
                            'ts': now,
                            'streams': {
                                ss.name: ss.writer.total_bytes
                                for ss in active.stream_iter()
                            },
                            **self._disk_info(),
                        })
                        last_flush = now

                    self._check_callback_errors(active)

                active.stop_event.wait(_TICK_INTERVAL)
        except Exception as e:
            logger.exception("Flush loop crashed for %s: %s", active.session_id, e)
            try:
                self._publish(active, 'error', {'message': str(e)})
            except Exception:
                pass

    def _drain_into_writers(self, active: _ActiveSession) -> None:
        for ss in active.stream_iter():
            try:
                frames = ss.recorder.drain()
            except Exception as e:
                logger.warning("drain %s error: %s", ss.name, e)
                continue
            for fr in frames:
                try:
                    ss.writer.append(fr.data)
                except Exception as e:
                    logger.warning("writer.append %s error: %s", ss.name, e)

    def _publish_levels(self, active: _ActiveSession) -> None:
        if self._broker is None:
            return
        snap = {}
        for ss in active.stream_iter():
            level = ss.recorder.level.snapshot()
            snap[ss.name] = {'peak': level['peak'], 'rms': level['rms']}
        self._publish(active, 'level', {
            'streams': snap,
            'elapsed': active.elapsed_seconds(),
        })

    def _check_callback_errors(self, active: _ActiveSession) -> None:
        for ss in active.stream_iter():
            err = ss.recorder.get_callback_error()
            if err is not None:
                error_str = str(err)
                self._store.set_stream_error(active.session_id, ss.name, error_str)
                self._publish(active, 'error', {
                    'stream': ss.name,
                    'message': error_str,
                })

    def _publish(self, active: _ActiveSession, event: str, data: dict) -> None:
        if self._broker is None:
            return
        try:
            self._broker.publish(
                f'recording:{active.session_id}',
                event,
                data,
            )
        except Exception as e:
            logger.debug("SSE publish error: %s", e)
