"""Cross-platform audio capture boundary (ADR-026 D1/D2, plan C1).

``RecordingService`` reaches every audio source through the :class:`AudioSource` protocol
(today's ``WasapiRecorder`` duck type + ``drain_events``). System audio of a recording is
described by one :class:`SystemSpec` — ``all`` / ``include:[apps]`` / ``exclude:[app]``:

- ``all`` at session start stays ``WasapiRecorder`` via the service's ``recorder_factory``
  (byte-for-byte today's path; not built here).
- ``include`` / ``exclude`` → :class:`AppAudioSource`: one :class:`ProcessLoopbackSource` per
  target PID, summed by :class:`MixedSource`, targets re-resolved by a watcher thread.

The native backend lives in ``process_loopback.py`` and loads lazily on the first client
start — importing this module pulls in no COM/WASAPI code.
"""
from __future__ import annotations

import logging
import ntpath
import os
import threading
import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Callable, Literal, Optional, Protocol

import numpy as np

from app.services.recording.process_loopback import ClientFactory, default_client_factory
from app.services.recording.recorder import AudioFrame, LevelMeter

logger = logging.getLogger(__name__)

MAX_INCLUDE_CLIENTS = 4
PROC_LOOPBACK_RATE, PROC_LOOPBACK_CHANNELS = 48000, 2  # s16le, fixed (ADR D2)

_MODES = ('all', 'include', 'exclude')
_SAMPLE_WIDTH = 2
_MIX_LAG_LIMIT_SECONDS = 0.5   # a part lagging more than this is zero-filled
_PAD_THRESHOLD_SECONDS = 0.05  # wall-clock deficit above this is padded with silence


class UnsupportedSourceSpecError(ValueError):
    code = 'UNSUPPORTED_SOURCE_SPEC'


class AudioSource(Protocol):
    sample_rate: int
    channels: int
    level: LevelMeter
    dropped_frames: int

    def open(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...  # idempotent
    def drain(self, timeout: float = 0.0) -> list[AudioFrame]: ...
    def get_callback_error(self) -> Optional[Exception]: ...
    def drain_events(self) -> list[dict]: ...


# ---------------------------------------------------------------- specs

def _norm_app(name: str) -> str:
    return ntpath.basename(str(name).strip()).strip().lower()


@dataclass(frozen=True)
class MicSpec:
    device_index: int
    device_name: str


@dataclass(frozen=True)
class SystemSpec:
    mode: Literal['all', 'include', 'exclude']
    apps: tuple[str, ...] = ()
    device_index: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> 'SystemSpec':
        if not isinstance(d, dict):
            raise UnsupportedSourceSpecError("system_source must be an object")
        mode = str(d.get('mode') or 'all').strip().lower()
        raw_apps = d.get('apps') or []
        if isinstance(raw_apps, str) or not isinstance(raw_apps, (list, tuple)):
            raise UnsupportedSourceSpecError("system_source.apps must be a list")
        apps: list[str] = []
        for a in raw_apps:
            n = _norm_app(a)
            if n and n not in apps:
                apps.append(n)
        dev = d.get('device_index')
        try:
            dev = int(dev) if dev is not None else None
        except (TypeError, ValueError) as e:
            raise UnsupportedSourceSpecError("system_source.device_index must be int") from e
        spec = cls(mode=mode, apps=tuple(apps), device_index=dev)  # type: ignore[arg-type]
        spec.validate()
        return spec

    def to_dict(self) -> dict:
        return {'mode': self.mode, 'apps': list(self.apps), 'device_index': self.device_index}

    def validate(self) -> None:
        n = len(self.apps)
        if self.mode not in _MODES:
            raise UnsupportedSourceSpecError(f"unknown system source mode: {self.mode!r}")
        if self.mode == 'all' and n:
            raise UnsupportedSourceSpecError("mode 'all' takes no apps")
        if self.mode == 'exclude' and n != 1:
            raise UnsupportedSourceSpecError("mode 'exclude' takes exactly one app")
        if self.mode == 'include' and not (0 < n <= MAX_INCLUDE_CLIENTS):
            raise UnsupportedSourceSpecError(
                f"mode 'include' takes 1..{MAX_INCLUDE_CLIENTS} apps")


def resolve_root_pids(exe: str, *, own_pid: int | None = None) -> list[int]:
    """PIDs of ``exe`` whose parent is not the same exe (lowercase basename compare)."""
    import psutil

    exe = _norm_app(exe)
    found: dict[int, Optional[int]] = {}
    for p in psutil.process_iter(['pid', 'name', 'ppid']):
        info = p.info
        if (info.get('name') or '').lower() == exe:
            found[int(info['pid'])] = info.get('ppid')
    return sorted(pid for pid, ppid in found.items() if ppid not in found and pid != own_pid)


# ---------------------------------------------------------------- one client

class ProcessLoopbackSource:
    """ONE native process-loopback client ``(pid, mode)`` at a fixed s16le format."""

    def __init__(self, pid: int, mode: Literal['include', 'exclude'], sample_rate: int,
                 channels: int, *, client_factory: ClientFactory = default_client_factory,
                 queue_max: int = 200):
        self.pid = int(pid)
        self.mode = mode
        self.sample_rate = sample_rate
        self.channels = channels
        self.level = LevelMeter(sample_rate=sample_rate, channels=channels)
        self.dropped_frames = 0
        self.total_bytes = 0
        self._factory = client_factory
        self._client = None
        self._queue: Queue[AudioFrame] = Queue(maxsize=queue_max)
        self._started = False
        self._closed = False
        self._callback_error: Optional[Exception] = None

    def open(self) -> None:
        if self._client is None and not self._closed:
            self._client = self._factory(self.pid, self.mode, self.sample_rate, self.channels)

    def start(self) -> None:
        if self._started or self._closed:
            return
        self.open()
        self._client.start(self._on_frame, self._on_error)
        self._started = True

    def stop(self) -> None:
        if self._client is not None and self._started:
            try:
                self._client.stop()
            except Exception as e:
                logger.warning("process loopback stop() error: %s", e)
            self._client = None  # a native client cannot restart; start() opens a new one
            self._started = False

    def close(self) -> None:
        if self._closed:
            return
        self.stop()
        self._closed = True
        self._client = None

    def _on_frame(self, data: bytes, _ts: float) -> None:
        if not data:
            return
        self.total_bytes += len(data)
        self.level.update(data)
        block = self.channels * _SAMPLE_WIDTH
        frame = AudioFrame(data=data, timestamp=time.time(), frame_count=len(data) // block)
        try:
            self._queue.put_nowait(frame)
        except Full:
            try:
                self._queue.get_nowait()
            except Empty:
                pass
            self.dropped_frames += 1
            try:
                self._queue.put_nowait(frame)
            except Full:
                self.dropped_frames += 1

    def _on_error(self, exc: Exception) -> None:
        self._callback_error = exc

    def drain(self, timeout: float = 0.0) -> list[AudioFrame]:
        out: list[AudioFrame] = []
        try:
            out.append(self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait())
        except Empty:
            return out
        while True:
            try:
                out.append(self._queue.get_nowait())
            except Empty:
                return out

    def get_callback_error(self) -> Optional[Exception]:
        return self._callback_error

    def drain_events(self) -> list[dict]:
        return []


# ---------------------------------------------------------------- mix

class MixedSource:
    """Saturating int16 sum of parts over the common frame count; a part lagging more than
    500 ms behind the leader is zero-filled so the mix never stalls."""

    def __init__(self, parts: list[ProcessLoopbackSource], sample_rate: int, channels: int):
        self.sample_rate = sample_rate
        self.channels = channels
        self.level = LevelMeter(sample_rate=sample_rate, channels=channels)
        self._block = channels * _SAMPLE_WIDTH
        self._lag_limit = int(sample_rate * _MIX_LAG_LIMIT_SECONDS) * self._block
        self._lock = threading.Lock()
        self._parts: list[ProcessLoopbackSource] = []
        self._bufs: dict[int, bytearray] = {}
        self._retired_dropped = 0
        self._retired_error: Optional[Exception] = None
        self.set_parts(parts)

    @property
    def dropped_frames(self) -> int:
        return self._retired_dropped + sum(p.dropped_frames for p in self._parts)

    def set_parts(self, parts: list[ProcessLoopbackSource]) -> None:
        """Replace the part list (no open/close — the owner manages lifecycles)."""
        with self._lock:
            keep = {id(p) for p in parts}
            for p in self._parts:
                if id(p) not in keep:
                    self._retired_dropped += p.dropped_frames
                    self._retired_error = self._retired_error or p.get_callback_error()
            self._bufs = {id(p): self._bufs.get(id(p), bytearray()) for p in parts}
            self._parts = list(parts)

    def open(self) -> None:
        for p in self._parts:
            p.open()

    def start(self) -> None:
        for p in self._parts:
            p.start()

    def stop(self) -> None:
        for p in self._parts:
            p.stop()

    def close(self) -> None:
        for p in self._parts:
            p.close()

    def pending_bytes(self) -> int:
        with self._lock:
            return max((len(b) for b in self._bufs.values()), default=0)

    def drain(self, timeout: float = 0.0) -> list[AudioFrame]:
        with self._lock:
            if not self._parts:
                return []
            for p in self._parts:
                for f in p.drain():
                    self._bufs[id(p)].extend(f.data)
            bufs = [self._bufs[id(p)] for p in self._parts]
            lead = max(len(b) for b in bufs)
            for b in bufs:
                if lead - len(b) > self._lag_limit:
                    b.extend(bytes(lead - len(b)))
            common = min(len(b) for b in bufs)
            common -= common % self._block
            if common <= 0:
                return []
            if len(bufs) == 1:
                data = bytes(bufs[0][:common])
            else:
                acc = np.zeros(common // _SAMPLE_WIDTH, dtype=np.int32)
                for b in bufs:
                    acc += np.frombuffer(bytes(b[:common]), dtype='<i2')
                data = np.clip(acc, -32768, 32767).astype('<i2').tobytes()
            for b in bufs:
                del b[:common]
        self.level.update(data)
        return [AudioFrame(data=data, timestamp=time.time(), frame_count=common // self._block)]

    def get_callback_error(self) -> Optional[Exception]:
        for p in self._parts:
            err = p.get_callback_error()
            if err is not None:
                return err
        return self._retired_error

    def drain_events(self) -> list[dict]:
        return []


# ---------------------------------------------------------------- app source

class AppAudioSource:
    """What the service holds for ``include`` / ``exclude`` (and ``all`` realised as
    EXCLUDE-own-PID on a stream that started as include/exclude).

    Output is kept on wall clock: whenever the emitted byte count falls behind active
    wall-clock time by more than 50 ms (app absent, stream drift, retarget gap), silence is
    appended. A watcher re-resolves the app's PIDs every ``watch_interval`` seconds and swaps
    clients (open new first, then close dead), pushing a ``pid_change`` event.
    """

    def __init__(self, spec: SystemSpec, sample_rate: int, channels: int, *,
                 client_factory: ClientFactory = default_client_factory,
                 pid_resolver: Callable[[str], list[int]] = resolve_root_pids,
                 watch_interval: float = 2.0, own_pid: int | None = None,
                 clock: Callable[[], float] = time.perf_counter):
        spec.validate()
        self.spec = spec
        self.sample_rate = sample_rate
        self.channels = channels
        self._factory = client_factory
        self._resolver = pid_resolver
        self._watch_interval = watch_interval
        self._own_pid = os.getpid() if own_pid is None else int(own_pid)
        self._clock = clock
        self._client_mode = 'include' if spec.mode == 'include' else 'exclude'
        self._block = channels * _SAMPLE_WIDTH
        self._pad_threshold = int(sample_rate * _PAD_THRESHOLD_SECONDS) * self._block

        self._lock = threading.RLock()
        self._mixer = MixedSource([], sample_rate, channels)
        self.level = self._mixer.level
        self._parts: dict[int, ProcessLoopbackSource] = {}
        self._events: list[dict] = []
        self._app_pids: list[int] = []
        self._warned_pids: tuple[int, ...] = ()
        self._opened = False
        self._started = False
        self._closed = False
        self._run_started = 0.0
        self._active_seconds = 0.0
        self._emitted = 0
        self._watch_stop = threading.Event()
        self._watch_thread: Optional[threading.Thread] = None

    @property
    def dropped_frames(self) -> int:
        return self._mixer.dropped_frames

    # ---------- targets

    def _resolve_app_pids(self) -> list[int]:
        pids: list[int] = []
        for app in self.spec.apps:
            for pid in self._resolver(app):
                pid = int(pid)
                if pid != self._own_pid and pid not in pids:
                    pids.append(pid)
        return pids  # resolver order kept: the best target (sounding tree) comes first

    def _targets(self, app_pids: list[int]) -> list[int]:
        if self.spec.mode == 'all':
            return [self._own_pid]
        if self.spec.mode == 'include':
            return app_pids
        if not app_pids:
            return [self._own_pid]  # app absent: EXCLUDE own PID == all system audio
        if len(app_pids) > 1 and tuple(app_pids) != self._warned_pids:
            # once per change of the candidate set, not on every watcher tick
            self._warned_pids = tuple(app_pids)
            logger.warning("exclude %s: %d process trees, excluding pid %s only",
                           self.spec.apps[0], len(app_pids), app_pids[0])
        return app_pids[:1]

    def _new_part(self, pid: int) -> ProcessLoopbackSource:
        return ProcessLoopbackSource(pid, self._client_mode, self.sample_rate, self.channels,
                                     client_factory=self._factory)

    # ---------- lifecycle

    def open(self) -> None:
        with self._lock:
            if self._opened or self._closed:
                return
            app_pids = self._resolve_app_pids() if self.spec.mode != 'all' else []
            if self.spec.mode == 'include' and len(app_pids) > MAX_INCLUDE_CLIENTS:
                raise UnsupportedSourceSpecError(
                    f"include resolves to {len(app_pids)} processes "
                    f"(max {MAX_INCLUDE_CLIENTS})")
            self._app_pids = app_pids
            for pid in self._targets(app_pids):
                part = self._new_part(pid)
                part.open()
                self._parts[pid] = part
            self._mixer.set_parts(list(self._parts.values()))
            self._opened = True

    def start(self) -> None:
        with self._lock:
            if self._started or self._closed:
                return
            self.open()
            for part in self._parts.values():
                part.start()
            self._run_started = self._clock()
            self._started = True
            if self.spec.mode != 'all' and self._watch_interval > 0:
                self._watch_stop.clear()
                self._watch_thread = threading.Thread(
                    target=self._watch, daemon=True, name='app-audio-watcher')
                self._watch_thread.start()

    def stop(self) -> None:
        self._stop_watcher()
        with self._lock:
            if not self._started:
                return
            for part in self._parts.values():
                part.stop()
            self._active_seconds += self._clock() - self._run_started
            self._started = False

    def close(self) -> None:
        if self._closed:
            return
        self.stop()
        with self._lock:
            for part in self._parts.values():
                part.close()
            self._parts.clear()
            self._closed = True

    def _stop_watcher(self) -> None:
        self._watch_stop.set()
        t = self._watch_thread
        if t is not None and t is not threading.current_thread():
            t.join(5.0)
        self._watch_thread = None

    # ---------- watcher

    def _watch(self) -> None:
        while not self._watch_stop.wait(self._watch_interval):
            try:
                self.refresh_pids()
            except Exception as e:
                logger.warning("app audio watcher (%s) error: %s", self.spec.apps, e)

    def refresh_pids(self) -> Optional[dict]:
        """One watcher tick: re-resolve and swap clients if the target set changed."""
        if self.spec.mode == 'all' or self._closed:
            return None
        app_pids = self._resolve_app_pids()  # psutil walk outside the lock
        if self.spec.mode == 'include' and len(app_pids) > MAX_INCLUDE_CLIENTS:
            logger.warning("include %s: %d processes, keeping first %d",
                           self.spec.apps, len(app_pids), MAX_INCLUDE_CLIENTS)
            app_pids = app_pids[:MAX_INCLUDE_CLIENTS]
        with self._lock:
            if self._closed or not self._opened:
                return None
            new_targets = self._targets(app_pids)
            if set(new_targets) == set(self._parts):
                return None
            t0 = time.perf_counter()
            added: dict[int, ProcessLoopbackSource] = {}
            try:
                for pid in new_targets:
                    if pid not in self._parts:
                        part = self._new_part(pid)
                        added[pid] = part
                        part.open()
                        if self._started:
                            part.start()
            except Exception as e:
                logger.warning("app audio: opening clients for %s failed: %s", new_targets, e)
                for part in added.values():
                    part.close()
                return None
            for pid in [p for p in self._parts if p not in new_targets]:
                self._parts.pop(pid).close()
            self._parts.update(added)
            self._mixer.set_parts([self._parts[p] for p in new_targets])
            gap_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            event = {
                'kind': 'pid_change',
                'exe': ','.join(self.spec.apps),
                'old_pids': list(self._app_pids),
                'new_pids': list(app_pids),
                'gap_ms': gap_ms,
            }
            self._app_pids = app_pids
            self._events.append(event)
            return event

    # ---------- consumer api

    def drain(self, timeout: float = 0.0) -> list[AudioFrame]:
        frames = self._mixer.drain()
        if not frames and timeout > 0:
            time.sleep(timeout)
            frames = self._mixer.drain()
        with self._lock:
            self._emitted += sum(len(f.data) for f in frames)
            elapsed = self._active_seconds
            if self._started:
                elapsed += self._clock() - self._run_started
            expected = int(elapsed * self.sample_rate) * self._block
            deficit = expected - self._emitted - self._mixer.pending_bytes()
            deficit -= deficit % self._block
            if deficit > self._pad_threshold:
                frames.append(AudioFrame(data=bytes(deficit), timestamp=time.time(),
                                         frame_count=deficit // self._block))
                self._emitted += deficit
        return frames

    def get_callback_error(self) -> Optional[Exception]:
        return self._mixer.get_callback_error()

    def drain_events(self) -> list[dict]:
        with self._lock:
            out, self._events = self._events, []
        return out


def make_app_source(spec: SystemSpec, sample_rate: int, channels: int) -> AudioSource:
    """Default ``app_source_factory``: an opened :class:`AppAudioSource`."""
    src = AppAudioSource(spec, sample_rate, channels)
    src.open()
    return src
