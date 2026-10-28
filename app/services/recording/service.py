"""RecordingService — the orchestration level for recording (Phase 9.3).

Puts together :class:`WasapiRecorder` (audio capture) + :class:`ChunkedPcmWriter`
(disk persistence) + :class:`SessionStore` (manifest state) into a single
session lifecycle with pause/resume and crash recovery support.

Key invariants:

- **N sessions** (ADR-026 D1): up to ``max_active`` sessions at once; more —
  :class:`SessionConflictError` with ``code='SESSION_LIMIT'``. The registry
  ``dict[sid, _ActiveSession]`` is under ``_registry_lock`` (the dict itself only).
- **Per-session lock**: state transitions of one session (pause/resume/stop/
  retarget) and the tick of its flush thread are serialized by ``_ActiveSession.lock``;
  a slow session does not block its neighbour.
- **System source = SystemSpec**: ``all`` → the unchanged ``recorder_factory``
  (byte for byte as before ADR-026), ``include``/``exclude`` → ``app_source_factory``.
  ``retarget_system()`` changes the source on the fly with a mark in the manifest.
- **Audio thread isolation**: PortAudio callbacks do NOT call any service
  methods directly. Data from the recorders is taken by a separate
  flush thread, which ticks at ~10Hz and periodically does fsync+manifest.
- **SSE-friendly**: on every tick a ``level`` event is published
  (peak+rms for mic and system). On chunk flush — ``chunk_saved``.
  On pause/resume/stop — ``status``. On errors — ``error``.

Lifecycle (FSM):

.. code-block:: text

      ┌─── start() ───┐
      ▼               │
    [recording] ──pause()──→ [paused] ──resume()─┘
      │                          │
      └────── stop() ─────► [stopping] ──finalize()──→ [finalized]
                                  │
                                  └──crash──→ [crashed] (recover_orphaned)

Finalize (Phase 9.4) is a separate job in :class:`JobQueue`. The service
only moves the status to stopping and sends a background job; when that one
completes — status=finalized.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.i18n import tr
from app.services.recording.audio_source import (
    PROC_LOOPBACK_CHANNELS,
    PROC_LOOPBACK_RATE,
    MicSpec,
    SystemSpec,
    UnsupportedSourceSpecError,
    make_app_source,
)
from app.services.recording.pcm_writer import ChunkedPcmWriter
from app.services.recording.recorder import (
    DeviceNotAvailableError,
    LevelMeter,
    is_access_denied,
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
    _now_iso,
)


logger = logging.getLogger(__name__)


# How many times boot recovery tries to finish finalizing a crashed session
# (see RecordingService.recover_unfinalized). 3 covers a chain of
# restarts; beyond that — manual intervention, so as not to grind a heavy job forever.
MAX_FINALIZE_ATTEMPTS = 3


# Tick rate for drain+level-publish. 10Hz = 100ms — smooth enough
# for the VU meter without CPU overhead.
_TICK_INTERVAL = 0.1

# A gap at retarget shorter than this is not padded with silence (ADR-026 D3).
_PAD_THRESHOLD_SECONDS = 0.02


class RecordingError(RuntimeError):
    """Base error for the service."""

    code = 'RECORDING_ERROR'

    def __init__(self, message: str = '', *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class SessionConflictError(RecordingError):
    """The limit of simultaneous sessions is reached (``code='SESSION_LIMIT'``)."""

    code = 'SESSION_LIMIT'

    def __init__(self, message: str = '', *, active: Optional[list] = None,
                 limit: Optional[int] = None):
        super().__init__(message)
        self.active = list(active or [])
        self.limit = limit


class SessionNotFoundError(RecordingError):
    """The sid is not among the active ones (and, for get_state, does not exist on disk)."""

    code = 'NOT_FOUND'


class NoSystemStreamError(RecordingError):
    """Retarget of a session that started without a system stream."""

    code = 'NO_SYSTEM_STREAM'


class DeviceAccessDeniedError(RecordingError):
    """Windows refused a capture stream at start (``code='DEVICE_ACCESS_DENIED'``).

    ``stream`` is ``'mic'`` or ``'system'``. The message is internal English.
    """

    code = 'DEVICE_ACCESS_DENIED'

    def __init__(self, message: str = '', *, stream: str):
        super().__init__(message)
        self.stream = stream


# ----------------------------------------------------------------- factories

# Factory types allow DI in tests: substitute a mock recorder/writer
# without real WASAPI streams and file operations.
RecorderFactory = Callable[..., Any]  # returns an object with start/stop/close/drain/level/get_callback_error
WriterFactory = Callable[..., Any]    # returns an object with append/flush/close/total_bytes


def _default_recorder_factory(**kwargs) -> WasapiRecorder:
    return WasapiRecorder(**kwargs)


def _default_writer_factory(path: Path) -> ChunkedPcmWriter:
    w = ChunkedPcmWriter(path)
    w.open()
    return w


# ----------------------------------------------------------------- internals

@dataclass
class _StreamState:
    """The state of one stream (mic or system) in an active session."""
    name: str  # 'mic' | 'system'
    recorder: Any  # AudioSource: WasapiRecorder or AppAudioSource
    writer: Any    # ChunkedPcmWriter-like
    device_name: str
    spec: Any = None             # MicSpec | SystemSpec — the current source
    origin_mode: str = 'all'     # the mode at start; determines the .pcm format forever
    origin_device_index: Optional[int] = None  # the endpoint of the 'all' stream to return to 'all'
    sample_rate: int = 48000     # the .pcm format (unchanged while the file exists)
    channels: int = 2
    lead_seconds: float = 0.0    # active session time before the stream started
    drop_before: float = 0.0     # frames with timestamp < this (a duplicate at retarget) are dropped

    @property
    def level(self) -> LevelMeter:
        return self.recorder.level

    @property
    def bytes_per_second(self) -> int:
        return int(self.sample_rate) * int(self.channels) * 2


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
    lock: threading.RLock = field(default_factory=threading.RLock)
    closed: bool = False  # stop/discard finished — the session is no longer alive

    def stream_iter(self):
        for name in (STREAM_MIC, STREAM_SYSTEM):
            ss = self.streams.get(name)
            if ss is not None:
                yield ss

    def elapsed_seconds(self) -> float:
        """How many seconds of active recording (without pauses)."""
        now = time.time()
        if self.is_paused and self.paused_at is not None:
            cur_pause = now - self.paused_at
        else:
            cur_pause = 0.0
        return max(0.0, (now - self.started_at) - self.paused_total_seconds - cur_pause)


# ----------------------------------------------------------------- service

class RecordingService:
    """The recording controller. A singleton in app.state.recording_service."""

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
        *,
        max_active: int = 2,
        app_source_factory: Optional[Callable[[SystemSpec, int, int], Any]] = None,
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
        self._max_active = max(1, int(max_active))
        self._app_source_factory = app_source_factory or make_app_source

        self._sessions: dict[str, _ActiveSession] = {}
        self._registry_lock = threading.Lock()  # the dict only, never I/O under it
        self._start_lock = threading.Lock()     # serializes start(): the limit without a race

    # ----------------------------------------------------------- queries

    def active_session_ids(self) -> list[str]:
        """All live sessions in start order."""
        with self._registry_lock:
            return list(self._sessions)

    @property
    def active_session_id(self) -> Optional[str]:
        """The only live session; ``None`` when there are 0 or more than one."""
        ids = self.active_session_ids()
        return ids[0] if len(ids) == 1 else None

    def is_active(self, session_id: str) -> bool:
        with self._registry_lock:
            return session_id in self._sessions

    @property
    def _active(self) -> Optional[_ActiveSession]:
        """The only live session or None — only for the root test_video_capture.py,
        which peeks into the service's private state. New code takes _sessions."""
        with self._registry_lock:
            if len(self._sessions) != 1:
                return None
            return next(iter(self._sessions.values()))

    @property
    def store(self) -> SessionStore:
        """Public accessor for the blueprint — needed so that the /save endpoint
        can read the manifest after finalize."""
        return self._store

    def list_devices(self) -> list[dict]:
        """Enumeration of mic + loopback devices (for the UI dropdown)."""
        return [d.to_dict() for d in list_input_devices()]

    def get_state(self, session_id: str) -> dict:
        """The current snapshot of the session state (for the polling fallback)."""
        with self._registry_lock:
            active = self._sessions.get(session_id)
        if active is None:
            # Not active — read from the manifest (possibly finalized)
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
                **self._source_info(manifest),
                **self._provenance_info(manifest),
            }

        # Active — build the live snapshot
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
            **self._source_info(manifest),
            **self._provenance_info(manifest),
            **self._disk_info(),
        }

    @staticmethod
    def _provenance_info(manifest: dict) -> dict:
        """Who started the session and its duration limit (mcp-recording-live)."""
        return {
            'started_by': manifest.get('started_by') or 'ui',
            'max_duration_min': manifest.get('max_duration_min'),
            'expires_at': manifest.get('expires_at'),
        }

    @staticmethod
    def _source_info(manifest: dict) -> dict:
        sys_mf = (manifest.get('streams') or {}).get(STREAM_SYSTEM) or {}
        if not sys_mf.get('enabled'):
            return {'system_source': None, 'source_events': 0}
        return {
            'system_source': SessionStore.stream_source(manifest, STREAM_SYSTEM),
            'source_events': len(sys_mf.get('source_events') or []),
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
        system_source: Optional[SystemSpec] = None,
        started_by: str = 'ui',
    ) -> str:
        """Start a new session. At least one of the streams must be
        specified (mic_device_index or a system stream).

        System stream: ``system_source=None`` + ``system_device_index`` →
        ``SystemSpec('all', device_index=idx)`` through ``recorder_factory``
        (the path to ADR-026 unchanged); ``include``/``exclude`` →
        ``app_source_factory(spec, 48000, 2)``, devices are not touched.

        Returns ``session_id``. Raises :class:`SessionConflictError`
        (``SESSION_LIMIT``) when there are already ``max_active`` live sessions;
        :class:`UnsupportedSourceSpecError` on an invalid spec.

        ``video_spec`` — an optional dict ``{'enabled': bool, 'tracks': [...]}``
        for starting video capture. Video NEVER blocks or changes
        the audio path: any video error is logged and discarded.
        """
        sys_spec = self._resolve_system_spec(system_source, system_device_index)
        if mic_device_index is None and sys_spec is None:
            raise RecordingError(tr("svc_recording.err.no_stream"))
        with self._start_lock:
            active_ids = self.active_session_ids()
            if len(active_ids) >= self._max_active:
                raise SessionConflictError(
                    tr("svc_recording.err.limit", limit=self._max_active,
                       active=', '.join(active_ids)),
                    active=active_ids, limit=self._max_active,
                )

            if sys_spec is not None and sys_spec.mode != 'all' and not system_device_name:
                system_device_name = f"{sys_spec.mode}: {', '.join(sys_spec.apps)}"

            session_id = self._make_session_id()
            self._store.create(
                session_id=session_id,
                sample_rate=self._sample_rate,
                channels=self._channels,
                mic_device_name=mic_device_name,
                system_device_name=system_device_name,
                system_source=sys_spec.to_dict() if sys_spec is not None else None,
                started_by=started_by,
            )

            active = _ActiveSession(session_id=session_id)
            opening = STREAM_MIC
            try:
                if mic_device_index is not None:
                    mic_name = mic_device_name or f'mic#{mic_device_index}'
                    self._open_stream(
                        active, STREAM_MIC, MicSpec(mic_device_index, mic_name), mic_name,
                    )
                if sys_spec is not None:
                    opening = STREAM_SYSTEM
                    self._open_stream(
                        active, STREAM_SYSTEM, sys_spec,
                        system_device_name or f'sys#{sys_spec.device_index}',
                    )
            except Exception as e:
                logger.exception("start() failed: %s", e)
                self._cleanup_streams(active)
                self._store.update_status(session_id, STATUS_CRASHED)
                self._raise_if_access_denied(e, opening)
                raise RecordingError(tr("svc_recording.err.open_stream", error=e)) from e

            starting = STREAM_MIC
            try:
                for ss in active.stream_iter():
                    starting = ss.name
                    ss.recorder.start()
                    ss.lead_seconds = active.elapsed_seconds()
            except Exception as e:
                # AppAudioSource activates the process loopback exactly in start():
                # a failure here is cleaned up the same way as an _open_stream failure.
                logger.exception("start() failed on recorder.start: %s", e)
                self._cleanup_streams(active)
                self._store.update_status(session_id, STATUS_CRASHED)
                self._raise_if_access_denied(e, starting)
                raise RecordingError(tr("svc_recording.err.start_stream", error=e)) from e

            active.stop_event = threading.Event()
            t = threading.Thread(
                target=self._flush_loop,
                args=(active,),
                name=f'recording-flush-{session_id}',
                daemon=True,
            )
            active.flush_thread = t
            with self._registry_lock:
                self._sessions[session_id] = active
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

    def _resolve_system_spec(
        self,
        system_source: Optional[SystemSpec],
        system_device_index: Optional[int],
    ) -> Optional[SystemSpec]:
        """The SystemSpec the system stream is opened with, or ``None`` (no stream)."""
        spec = system_source
        if spec is None:
            if system_device_index is None:
                return None
            spec = SystemSpec('all', device_index=int(system_device_index))
        spec.validate()
        if spec.mode == 'all' and spec.device_index is None:
            if system_device_index is None:
                raise RecordingError(
                    tr("svc_recording.err.all_needs_device"),
                    code='NO_SYSTEM_DEVICE',
                )
            spec = replace(spec, device_index=int(system_device_index))
        return spec

    def pause(self, session_id: str) -> None:
        """Pause. Closes the WASAPI streams (frees the devices for
        other applications) and closes the current segment."""
        active = self._require_active(session_id)
        with active.lock:
            self._check_live(active)
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
        """Resume recording after pause. Reopens the sources by the stream's current
        spec (the same dispatch as start) and opens a new segment."""
        active = self._require_active(session_id)
        with active.lock:
            self._check_live(active)
            if not active.is_paused:
                return
            for ss in active.stream_iter():
                # WasapiRecorder in our implementation does not allow reopen
                # after close. We create a new source with the same parameters.
                ss.recorder.close()
                new_recorder = self._build_source(ss, ss.spec)
                new_recorder.open()
                new_recorder.start()
                ss.recorder = new_recorder
                ss.drop_before = 0.0

            self._store.open_segment(session_id)
            self._store.update_status(session_id, STATUS_RECORDING)
            if active.paused_at is not None:
                active.paused_total_seconds += time.time() - active.paused_at
            active.is_paused = False
            active.paused_at = None
            self._publish(active, 'status', {'status': STATUS_RECORDING})
            self._video_call(active, 'resume')

    def retarget_system(
        self,
        session_id: str,
        spec: SystemSpec,
        reason: str = 'user',
    ) -> dict:
        """Retarget the system stream of a live session (ADR-026 D3).

        The new source is opened FIRST, then the remainder of the old one is written
        into the writer, the old one is closed; frames of the new one older than the moment of the swap
        are dropped; a gap > 20 ms is padded with silence. One mark in
        ``source_events`` + SSE ``source_changed``. On pause only the
        spec changes — resume() will open the already new source.
        """
        active = self._require_active(session_id)
        spec.validate()  # UnsupportedSourceSpecError — nothing has been changed
        with active.lock:
            self._check_live(active)
            ss = active.streams.get(STREAM_SYSTEM)
            if ss is None:
                raise NoSystemStreamError(
                    tr("svc_recording.err.no_system_stream", session_id=session_id)
                )
            new_spec = spec
            if spec.mode == 'all':
                # The .pcm format is unchanged: a stream from 'all' returns to its own
                # endpoint; a stream from include/exclude — EXCLUDE-own-PID.
                new_spec = SystemSpec(
                    'all',
                    device_index=(ss.origin_device_index if ss.origin_mode == 'all' else None),
                )
            frm = ss.spec.to_dict()

            if active.is_paused:
                ss.spec = new_spec
                event = self._source_event(
                    active, ss, frm, new_spec.to_dict(), reason,
                    gap_ms=0.0, padded_bytes=0, offset_bytes=ss.writer.total_bytes,
                )
                event['applied_on_resume'] = True
            else:
                new_src = self._build_source(ss, new_spec)
                try:
                    new_src.open()
                    new_src.start()
                except Exception as e:
                    try:
                        new_src.close()
                    except Exception as ce:
                        logger.debug("close of failed retarget source: %s", ce)
                    raise RecordingError(tr("svc_recording.err.open_new_source", error=e)) from e

                old = ss.recorder
                self._write_frames(ss, self._safe_drain(ss, old))
                try:
                    old.stop()
                except Exception as e:
                    logger.warning("retarget: old.stop() error: %s", e)
                self._write_frames(ss, self._safe_drain(ss, old))
                try:
                    old.close()
                except Exception as e:
                    logger.warning("retarget: old.close() error: %s", e)
                swap_ts = time.time()
                ss.recorder = new_src
                ss.spec = new_spec
                ss.drop_before = swap_ts

                gap_ms, padded, offset = self._pad_silence(active, ss)
                event = self._source_event(
                    active, ss, frm, new_spec.to_dict(), reason,
                    gap_ms=gap_ms, padded_bytes=padded, offset_bytes=offset,
                )

            self._store.set_stream_source(session_id, STREAM_SYSTEM, new_spec.to_dict(), event)
            self._publish(active, 'source_changed', {
                'session_id': session_id,
                'stream': STREAM_SYSTEM,
                'source': new_spec.to_dict(),
                'event': event,
            })
            logger.info("Recording %s: system source %s -> %s (%s)",
                        session_id, frm.get('mode'), new_spec.mode, reason)
            return {
                'session_id': session_id,
                'stream': STREAM_SYSTEM,
                'source': new_spec.to_dict(),
                'event': event,
                'applied_on_resume': bool(event.get('applied_on_resume', False)),
            }

    def stop(self, session_id: str, name: Optional[str] = None) -> None:
        """Stop the recording. Starts finalize in the background (if
        finalize_callback is configured).

        The caller can then subscribe to SSE or wait for
        ``status='finalized'`` by polling.
        """
        active = self._require_active(session_id)
        with active.lock:
            self._check_live(active)
            self._store.update_status(session_id, STATUS_STOPPING)
            self._publish(active, 'status', {'status': STATUS_STOPPING})

            # Stop the flush thread (it takes the lock with a timeout and will see stop_event)
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
            active.closed = True
            with self._registry_lock:
                self._sessions.pop(session_id, None)

        # Finalize — outside the lock (may take several seconds)
        if self._finalize_callback is not None:
            try:
                self._finalize_callback(session_id)
            except Exception as e:
                logger.exception("Finalize callback failed for %s: %s", session_id, e)

    def discard(self, session_id: str) -> None:
        """Cancel the current recording and delete the session without finalize."""
        with self._registry_lock:
            active = self._sessions.get(session_id)
        if active is not None:
            with active.lock:
                if not active.closed:
                    active.stop_event.set()
                    if active.flush_thread is not None:
                        active.flush_thread.join(timeout=5.0)
                    self._stop_streams(active)
                    self._video_call(active, 'stop', graceful=False)
                    for ss in active.stream_iter():
                        ss.writer.close()
                    active.closed = True
                with self._registry_lock:
                    self._sessions.pop(session_id, None)
        self._store.delete(session_id)

    # ----------------------------------------------------------- recovery

    def recover_orphaned(self) -> list[str]:
        """Finds sessions with an active status on disk and marks them
        crashed. Returns the list of session_ids that went into recovery.

        The real finalize (PCM→WAV→MP3) is done by :class:`FinalizePipeline`
        in Phase 9.4. The service here only marks the status and calls
        ``finalize_callback`` (if configured) for each one.
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
        """Restarts finalize for ``crashed`` sessions left without an MP3.

        The second recovery pass (the first is :meth:`recover_orphaned`). Needed
        because the finalize job itself is mortal too: a restart during finalization
        left the session in ``crashed`` forever, and no other mechanism saw it
        (see :meth:`SessionStore.list_unfinalized`).

        Protection against an endless loop: every attempt increments
        ``finalize_attempts`` in the manifest BEFORE the callback is called. After
        ``MAX_FINALIZE_ATTEMPTS`` the session is no longer queued — otherwise a
        session that finalize deterministically breaks (corrupted PCM, no space on
        disk) would spin a heavy job at every start. The counter is reset by
        a successful finalize (``status='finalized'`` takes the session out of the selection).

        Returns the session_ids queued this time.
        """
        requeued: list[str] = []
        if self._finalize_callback is None:
            return requeued
        for manifest in self._store.list_unfinalized():
            sid = manifest['session_id']
            attempts = int(manifest.get('finalize_attempts', 0) or 0)
            if attempts >= MAX_FINALIZE_ATTEMPTS:
                logger.warning(
                    "Session %s: finalize failed %d time(s) — not retrying "
                    "automatically. PCM is on disk, manual intervention is needed.",
                    sid, attempts,
                )
                continue
            try:
                self._store.modify(
                    sid,
                    lambda m, _a=attempts: m.__setitem__('finalize_attempts', _a + 1),
                )
            except SessionStoreError as e:
                logger.warning("Cannot bump finalize_attempts for %s: %s", sid, e)
                continue
            logger.warning(
                "Unfinished session %s (crashed, attempt %d/%d) — finalizing again",
                sid, attempts + 1, MAX_FINALIZE_ATTEMPTS,
            )
            try:
                self._finalize_callback(sid)
                requeued.append(sid)
            except Exception as e:
                logger.exception("Re-finalize %s did not start: %s", sid, e)
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
        with self._registry_lock:
            active = self._sessions.get(session_id)
        if active is None:
            raise SessionNotFoundError(tr("svc_recording.err.no_active_session", session_id=session_id))
        return active

    @staticmethod
    def _check_live(active: _ActiveSession) -> None:
        """Under the session lock: stop/discard could have completed while we waited."""
        if active.closed:
            raise SessionNotFoundError(
                tr("svc_recording.err.no_active_session", session_id=active.session_id))

    @staticmethod
    def _raise_if_access_denied(exc: BaseException, stream: str) -> None:
        """Re-raise a start failure as :class:`DeviceAccessDeniedError` when Windows refused it."""
        if is_access_denied(exc):
            raise DeviceAccessDeniedError(
                f"Access to the {stream} capture stream was denied: {exc}", stream=stream,
            ) from exc

    def _make_session_id(self) -> str:
        return 'rec_' + uuid.uuid4().hex[:16]

    def _open_stream(
        self,
        active: _ActiveSession,
        stream_name: str,
        spec: Any,
        device_name: str,
    ) -> None:
        if isinstance(spec, SystemSpec) and spec.mode != 'all':
            # include/exclude: a fixed process loopback format, devices are not touched
            rate, ch = PROC_LOOPBACK_RATE, PROC_LOOPBACK_CHANNELS
        else:
            rate, ch = self._resolve_params(spec.device_index)
        ss = _StreamState(
            name=stream_name,
            recorder=None,
            writer=None,
            device_name=device_name,
            spec=spec,
            origin_mode=spec.mode if isinstance(spec, SystemSpec) else 'all',
            origin_device_index=spec.device_index,
            sample_rate=rate,
            channels=ch,
        )
        recorder = self._build_source(ss, spec)
        recorder.open()
        ss.recorder = recorder
        writer_path = self._store.pcm_path(active.session_id, stream_name)
        ss.writer = self._writer_factory(writer_path)
        active.streams[stream_name] = ss
        # Phase 9 fix: store the actual native rate/channels of the stream in the
        # manifest. Without this finalize would read the top-level (target) values
        # — for the FOX mic (88200/1) the wav header would come out 48000/2, which
        # distorts the pitch and the channels.
        self._store.set_stream_params(
            active.session_id, stream_name,
            sample_rate=rate, channels=ch,
        )

    def _resolve_params(self, device_index: int) -> tuple[int, int]:
        # In tests where recorder_factory is a mock, resolve_device_params is really
        # called only for real WASAPI. Here we do try/except,
        # because in a mock scenario we may want specific rate/ch.
        try:
            return resolve_device_params(device_index, self._sample_rate, self._channels)
        except DeviceNotAvailableError:
            # CRITICAL: native WASAPI SEGVs if we try open() on a
            # nonexistent device. Do not fall back to defaults here — abort.
            raise
        except Exception as e:
            logger.warning(
                "resolve_device_params failed for %s: %s — using defaults",
                device_index, e,
            )
            return self._sample_rate, self._channels

    def _build_source(self, ss: _StreamState, spec: Any) -> Any:
        """A single dispatch spec → source (start, resume, retarget). Does not open.

        ``MicSpec`` and ``SystemSpec('all')`` on a stream that started as ``all`` —
        ``recorder_factory`` with the same endpoint and format (Invariant 2).
        The rest — ``app_source_factory`` in the stream's .pcm format; ``all`` on a stream
        of include/exclude is implemented there as EXCLUDE-own-PID.
        """
        if isinstance(spec, MicSpec) or (spec.mode == 'all' and ss.origin_mode == 'all'):
            return self._recorder_factory(
                device_index=spec.device_index,
                sample_rate=ss.sample_rate,
                channels=ss.channels,
            )
        return self._app_source_factory(spec, ss.sample_rate, ss.channels)

    @staticmethod
    def _safe_drain(ss: _StreamState, src: Any) -> list:
        try:
            return src.drain()
        except Exception as e:
            logger.warning("drain %s error: %s", ss.name, e)
            return []

    @staticmethod
    def _write_frames(ss: _StreamState, frames: list) -> None:
        for fr in frames:
            if ss.drop_before and fr.timestamp < ss.drop_before:
                continue  # duplicate: the new source was writing while the old one was still being drained
            try:
                ss.writer.append(fr.data)
            except Exception as e:
                logger.warning("writer.append %s error: %s", ss.name, e)

    @staticmethod
    def _stream_elapsed(active: _ActiveSession, ss: _StreamState) -> float:
        """Active seconds that the stream should have recorded (without pauses)."""
        return max(0.0, active.elapsed_seconds() - ss.lead_seconds)

    def _pad_silence(self, active: _ActiveSession, ss: _StreamState) -> tuple[float, int, int]:
        """The gap between what was recorded and the active time → silence, if > 20 ms.

        Returns ``(gap_ms, padded_bytes, offset_bytes)``; offset is the byte in the .pcm
        from which the new source writes.
        """
        try:
            ss.writer.flush(fsync=False)
        except Exception as e:
            logger.warning("flush %s before pad error: %s", ss.name, e)
        total = int(ss.writer.total_bytes)
        block = int(ss.channels) * 2
        bps = ss.bytes_per_second
        expected = int(self._stream_elapsed(active, ss) * bps) // block * block
        deficit = max(0, expected - total)
        gap_ms = round(deficit * 1000.0 / bps, 1) if bps else 0.0
        padded = 0
        if deficit > int(_PAD_THRESHOLD_SECONDS * bps):
            try:
                ss.writer.append(bytes(deficit))
                padded = deficit
            except Exception as e:
                logger.warning("pad %s error: %s", ss.name, e)
        return gap_ms, padded, total + padded

    @staticmethod
    def _source_event(active: _ActiveSession, ss: _StreamState, frm: dict, to: dict,
                      reason: str, *, gap_ms: float, padded_bytes: int,
                      offset_bytes: int) -> dict:
        return {
            'ts': _now_iso(),
            'elapsed_sec': round(active.elapsed_seconds(), 3),
            'offset_bytes': int(offset_bytes),
            'from': frm,
            'to': to,
            'gap_ms': gap_ms,
            'padded_bytes': int(padded_bytes),
            'reason': reason,
        }

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
        """The main loop of a session: drain queues, publish levels, periodic flush.

        Runs in a separate daemon thread. Exits when
        ``active.stop_event`` is set.
        """
        last_flush = time.time()
        try:
            while not active.stop_event.is_set():
                # The session lock with a timeout: stop() holds it while it joins
                # this thread — then we exit on stop_event rather than hang.
                if active.lock.acquire(timeout=_TICK_INTERVAL):
                    try:
                        if not active.stop_event.is_set() and not active.is_paused:
                            last_flush = self._tick(active, last_flush)
                    finally:
                        active.lock.release()
                active.stop_event.wait(_TICK_INTERVAL)
        except Exception as e:
            logger.exception("Flush loop crashed for %s: %s", active.session_id, e)
            try:
                # Same rule as jobs: the raw exception text stays in the log.
                self._publish(active, 'error', {
                    'message': tr("svc_jobs.error.recording_failed"),
                    'error_code': 'RECORDING_FAILED',
                })
            except Exception as pe:
                logger.debug("SSE publish of flush crash failed: %s", pe)

    def _tick(self, active: _ActiveSession, last_flush: float) -> float:
        """One tick of the flush thread under the session lock. Returns the new last_flush."""
        self._drain_into_writers(active)
        self._drain_source_events(active)
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
        return last_flush

    def _drain_into_writers(self, active: _ActiveSession) -> None:
        for ss in active.stream_iter():
            self._write_frames(ss, self._safe_drain(ss, ss.recorder))

    def _drain_source_events(self, active: _ActiveSession) -> None:
        """``pid_change`` from the source → a mark in the manifest + SSE. The source does not touch the store."""
        for ss in active.stream_iter():
            try:
                events = ss.recorder.drain_events() or []
            except Exception as e:
                logger.warning("drain_events %s error: %s", ss.name, e)
                continue
            if not isinstance(ss.spec, SystemSpec):
                continue
            for ev in events:
                if ev.get('kind') != 'pid_change':
                    continue
                src = ss.spec.to_dict()
                entry = self._source_event(
                    active, ss, src, src, 'pid_change',
                    gap_ms=float(ev.get('gap_ms') or 0.0), padded_bytes=0,
                    offset_bytes=ss.writer.total_bytes,
                )
                entry['exe'] = ev.get('exe')  # PIDs are not written to the manifest
                try:
                    self._store.set_stream_source(active.session_id, ss.name, src, entry)
                except Exception as e:
                    logger.warning("source event %s not saved: %s", ss.name, e)
                    continue
                self._publish(active, 'source_changed', {
                    'session_id': active.session_id,
                    'stream': ss.name,
                    'source': src,
                    'event': entry,
                })

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
                logger.warning("Stream %s of %s failed: %s", ss.name, active.session_id, error_str)
                self._store.set_stream_error(active.session_id, ss.name, error_str)
                # Same rule as jobs: the raw callback text goes to the log and the manifest only.
                self._publish(active, 'error', {
                    'stream': ss.name,
                    'message': tr("svc_jobs.error.stream_failed"),
                    'error_code': 'RECORDING_STREAM_FAILED',
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
