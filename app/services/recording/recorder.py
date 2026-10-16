"""WASAPI recorder primitive (Phase 9.1).

A thin wrapper around PyAudioWPatch (a PyAudio fork with WASAPI
loopback support). It solves these tasks:

- Enumeration of input devices: both ordinary microphones and loopback copies of
  output devices (for recording what the system plays).
- Opening a callback stream on a specific device with buffering of
  frames in a :class:`queue.Queue`.
- Signal level metering (RMS + peak) in a rolling 100ms window — the UI
  reads them for the VU bars.

The higher level (session storage, finalization, REST API) is in the next
subphases 9.2-9.5.

Why a separate module, not the whole pipeline in one:
- The recorder primitive = a thin wrapper around a C library. It is tested
  in isolation: did it open, does it compute levels, did it close correctly.
- Session storage and finalization are pure Python without C dependencies,
  tested without audio devices.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import Optional

try:
    import pyaudiowpatch as pyaudio
    _HAS_PYAUDIO = True
except ImportError:
    pyaudio = None  # type: ignore[assignment]
    _HAS_PYAUDIO = False


logger = logging.getLogger(__name__)


# Int16 PCM: 2 bytes per sample; float normalization for levels — by /32768.
_SAMPLE_WIDTH_BYTES = 2
_INT16_MAX = 32768.0
_LEVEL_WINDOW_SECONDS = 0.1  # 100ms rolling RMS+peak


class RecorderError(RuntimeError):
    """Error in recorder initialization or operation."""


@dataclass(frozen=True)
class DeviceInfo:
    """A simplified description of a WASAPI input device.

    `kind`:
    - ``"mic"`` — an ordinary input (microphone, line-in).
    - ``"loopback"`` — a special index of the loopback copy of an output device
      (PyAudioWPatch creates them for every output device).
    """
    index: int
    name: str
    channels: int
    default_sample_rate: int
    kind: str  # "mic" | "loopback"
    is_default: bool = False
    host_api_name: str = "WASAPI"

    def to_dict(self) -> dict:
        return {
            'index': self.index,
            'name': self.name,
            'channels': self.channels,
            'default_sample_rate': self.default_sample_rate,
            'kind': self.kind,
            'is_default': self.is_default,
            'host_api_name': self.host_api_name,
        }


@dataclass
class AudioFrame:
    """A chunk of audio data from the recorder. Bytes = native int16 LE PCM."""
    data: bytes
    timestamp: float
    frame_count: int


# ---------------------------------------------------------------- public api

def list_input_devices() -> list[DeviceInfo]:
    """Returns WASAPI input devices: microphones first, then loopbacks.

    Loopback devices are available only on Windows through PyAudioWPatch — these are
    «virtual inputs» that read what plays on the corresponding
    output device.

    If PyAudioWPatch is not installed — returns an empty list and
    logs a warning. The calling code must handle an empty list as
    «recording feature unavailable».
    """
    if not _HAS_PYAUDIO:
        logger.warning("pyaudiowpatch is unavailable — recording disabled")
        return []

    pa = pyaudio.PyAudio()
    try:
        wasapi_host_index = _find_wasapi_host_api(pa)
        if wasapi_host_index is None:
            logger.warning("WASAPI host API not found")
            return []

        default_input_index = _safe_default_input(pa, wasapi_host_index)
        default_output_index = _safe_default_output(pa, wasapi_host_index)

        devices: list[DeviceInfo] = []

        # Microphones (ordinary WASAPI inputs).
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get('hostApi') != wasapi_host_index:
                continue
            if info.get('maxInputChannels', 0) <= 0:
                continue
            # Loopback copies come from a separate generator — skip them.
            if info.get('isLoopbackDevice', False):
                continue
            devices.append(DeviceInfo(
                index=i,
                name=str(info.get('name', f'Device {i}')),
                channels=int(info['maxInputChannels']),
                default_sample_rate=int(info.get('defaultSampleRate', 48000)),
                kind='mic',
                is_default=(i == default_input_index),
            ))

        # Loopback (system audio).
        try:
            loopbacks = list(pa.get_loopback_device_info_generator())
        except OSError as e:
            logger.warning("Failed to get loopback devices: %s", e)
            loopbacks = []

        for info in loopbacks:
            host_api = info.get('hostApi')
            if host_api != wasapi_host_index:
                continue
            # Loopback indexes are separate from ordinary input/output.
            idx = int(info['index'])
            # Is this the loopback of the default output?
            # PyAudioWPatch assigns the loopback copy an index = (output_index +
            # offset). We check by name (it contains « (loopback)»).
            is_default = False
            if default_output_index is not None:
                default_name = pa.get_device_info_by_index(default_output_index).get('name', '')
                if default_name and default_name in str(info.get('name', '')):
                    is_default = True
            devices.append(DeviceInfo(
                index=idx,
                name=str(info.get('name', f'Loopback {idx}')),
                channels=int(info.get('maxInputChannels', 2)),
                default_sample_rate=int(info.get('defaultSampleRate', 48000)),
                kind='loopback',
                is_default=is_default,
            ))

        return devices
    finally:
        pa.terminate()


class DeviceNotAvailableError(RecorderError):
    """The device does not exist / has no input channels. Differs from format
    issues — a fallback to defaults is unsafe for it (WASAPI native SEGV)."""


def resolve_device_params(
    device_index: int,
    requested_sample_rate: int,
    requested_channels: int,
) -> tuple[int, int]:
    """Adjusts the requested parameters to the real capabilities of the device.

    If the device does not support the requested number of channels (for example,
    a mono microphone at requested=2) — we return ``min(requested, max_supported)``.

    If the requested sample_rate is not supported according to
    ``is_format_supported`` — a fallback to the device's ``defaultSampleRate``.

    Returns ``(sample_rate, channels)`` ready for ``pa.open(...)``.

    Raises:
        DeviceNotAvailableError: if the device does not exist — the caller MUST abort,
            because native WASAPI then SEGVs on ``pa.open()``.
        RecorderError: other errors.
    """
    if not _HAS_PYAUDIO:
        raise RecorderError("pyaudiowpatch is not installed")

    pa = pyaudio.PyAudio()
    try:
        try:
            info = pa.get_device_info_by_index(device_index)
        except OSError as e:
            raise DeviceNotAvailableError(f"Device {device_index} not found: {e}") from e

        max_channels = int(info.get('maxInputChannels', 0))
        if max_channels <= 0:
            raise DeviceNotAvailableError(f"Device {device_index} has no input channels")

        channels = min(requested_channels, max_channels)
        sample_rate = int(requested_sample_rate)

        try:
            ok = pa.is_format_supported(
                rate=sample_rate,
                input_device=device_index,
                input_channels=channels,
                input_format=pyaudio.paInt16,
            )
        except (ValueError, OSError):
            ok = False

        if not ok:
            fallback_rate = int(info.get('defaultSampleRate', 48000))
            logger.info(
                "Sample rate %d is not supported by device %d (%s), "
                "fallback to %d",
                sample_rate, device_index, info.get('name'), fallback_rate,
            )
            sample_rate = fallback_rate

        return sample_rate, channels
    finally:
        pa.terminate()


# ---------------------------------------------------------------- LevelMeter

class LevelMeter:
    """Rolling peak + RMS in a ~100ms window.

    Thread-safe: updates are called from the audio callback (the PortAudio
    thread), reads — from the UI/SSE thread.

    Level semantics:
    - ``peak`` (0..1): the maximum absolute sample value over the window.
    - ``rms`` (0..1): the RMS value over the window (it correlates better with
      perceived loudness).

    Both values are normalized: for int16 we divide by 32768.
    """

    def __init__(self, sample_rate: int, channels: int,
                 window_seconds: float = _LEVEL_WINDOW_SECONDS):
        self._sample_rate = sample_rate
        self._channels = channels
        self._window_samples = max(1, int(sample_rate * window_seconds))
        # A buffer of sample magnitudes (float, normalized 0..1) for RMS/peak.
        self._buf: deque[float] = deque(maxlen=self._window_samples)
        self._sq_sum: float = 0.0  # for O(1) RMS
        self._lock = threading.Lock()

    def update(self, pcm_bytes: bytes) -> None:
        """Feed the recorder's PCM bytes. int16 LE assumed."""
        if not pcm_bytes:
            return
        # We take only every Nth sample to lower CPU at high rates.
        # 100ms @ 48kHz stereo = 4800*2 samples → we recompute <=480.
        stride = max(1, len(pcm_bytes) // (_SAMPLE_WIDTH_BYTES * 480))
        # int16 LE: 2 bytes, signed
        with self._lock:
            i = 0
            n = len(pcm_bytes)
            stride_bytes = _SAMPLE_WIDTH_BYTES * stride
            while i < n - 1:
                # manual int16 LE → so as not to pull numpy into the hot path
                lo = pcm_bytes[i]
                hi = pcm_bytes[i + 1]
                val = lo | (hi << 8)
                if val >= 0x8000:
                    val -= 0x10000
                norm = abs(val) / _INT16_MAX
                if len(self._buf) == self._window_samples:
                    old = self._buf[0]
                    self._sq_sum -= old * old
                self._buf.append(norm)
                self._sq_sum += norm * norm
                i += stride_bytes
            # Guard against drift from float errors
            if self._sq_sum < 0:
                self._sq_sum = 0.0

    @property
    def peak(self) -> float:
        with self._lock:
            return max(self._buf) if self._buf else 0.0

    @property
    def rms(self) -> float:
        with self._lock:
            if not self._buf:
                return 0.0
            return math.sqrt(self._sq_sum / len(self._buf))

    def snapshot(self) -> dict:
        """An atomic slice of peak/rms for SSE."""
        with self._lock:
            if not self._buf:
                return {'peak': 0.0, 'rms': 0.0}
            return {
                'peak': max(self._buf),
                'rms': math.sqrt(self._sq_sum / len(self._buf)),
            }


# ---------------------------------------------------------------- recorder

class WasapiRecorder:
    """A context manager for a single WASAPI stream.

    Usage::

        with WasapiRecorder(device_index=12, sample_rate=48000, channels=2) as rec:
            rec.start()
            time.sleep(5)
            for frame in rec.drain():
                save_to_disk(frame.data)
            print(rec.level.snapshot())

    Key invariants:
    - The stream is opened in callback mode: PortAudio calls
      ``_callback`` at its own rate. Frames land in a Queue
      of limited size.
    - If the consumer cannot keep up (Queue full) — old frames are DROPPED
      and ``dropped_frames`` is incremented (but this is a warning, not an
      error — the system carries on anyway).
    - LevelMeter is updated synchronously in the callback: the levels are "fresh"
      even if the consumer lags behind.
    - Closing — either through the context manager or :meth:`close`. A double
      close is safe.
    """

    def __init__(
        self,
        device_index: int,
        sample_rate: int = 48000,
        channels: int = 2,
        chunk_frames: int = 1024,
        queue_max: int = 200,
    ):
        if not _HAS_PYAUDIO:
            raise RecorderError("pyaudiowpatch is not installed")
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_frames = chunk_frames

        self._pa: Optional["pyaudio.PyAudio"] = None  # type: ignore[name-defined]
        self._stream = None
        self._queue: Queue[AudioFrame] = Queue(maxsize=queue_max)
        self._closed = False
        self._started = False
        self.level = LevelMeter(sample_rate=sample_rate, channels=channels)

        # Metrics
        self.dropped_frames = 0
        self.total_bytes = 0
        self.last_callback_ts: float = 0.0

        self._callback_error: Optional[Exception] = None

    # ---------- lifecycle

    def open(self) -> None:
        """Creates PyAudio and opens the stream (without starting)."""
        if self._stream is not None:
            return
        self._pa = pyaudio.PyAudio()
        try:
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.chunk_frames,
                stream_callback=self._callback,
                start=False,
            )
        except OSError as e:
            self._pa.terminate()
            self._pa = None
            raise RecorderError(
                f"Failed to open the WASAPI stream (device={self.device_index}, "
                f"rate={self.sample_rate}, channels={self.channels}): {e}"
            ) from e

    def start(self) -> None:
        """Start the actual reception of frames."""
        if self._stream is None:
            self.open()
        if self._started:
            return
        assert self._stream is not None
        self._stream.start_stream()
        self._started = True

    def stop(self) -> None:
        """Stop reception but do not close the stream (start() can be called again)."""
        if self._stream is not None and self._started:
            try:
                self._stream.stop_stream()
            except OSError as e:
                logger.warning("stop_stream() error: %s", e)
            self._started = False

    def close(self) -> None:
        """Stop and release resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._stream is not None:
            try:
                if self._started:
                    self._stream.stop_stream()
            except OSError:
                pass
            try:
                self._stream.close()
            except OSError as e:
                logger.warning("stream.close() error: %s", e)
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except OSError as e:
                logger.warning("pa.terminate() error: %s", e)
            self._pa = None
        self._started = False

    # ---------- callback

    def _callback(self, in_data, frame_count, time_info, status):
        """PortAudio callback. Called in the audio thread (NOT main).

        No blocking I/O here — only enqueue + level update.
        """
        try:
            self.last_callback_ts = time.time()
            self.total_bytes += len(in_data) if in_data else 0
            if in_data:
                self.level.update(in_data)
                frame = AudioFrame(
                    data=in_data,
                    timestamp=self.last_callback_ts,
                    frame_count=frame_count,
                )
                try:
                    self._queue.put_nowait(frame)
                except Full:
                    # The consumer lags behind — drop the oldest frame.
                    try:
                        self._queue.get_nowait()
                    except Empty:
                        pass
                    self.dropped_frames += 1
                    try:
                        self._queue.put_nowait(frame)
                    except Full:
                        # Very unlikely but better not to block the callback
                        self.dropped_frames += 1
        except Exception as e:  # pragma: no cover — audio thread protection
            self._callback_error = e
            logger.exception("WasapiRecorder callback error: %s", e)
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    # ---------- consumer api

    def drain(self, timeout: float = 0.0) -> list[AudioFrame]:
        """Take all ready frames from the queue.

        Blocks up to ``timeout`` seconds for the first frame; the rest are taken
        non-blocking. Returns [] if there is nothing.
        """
        out: list[AudioFrame] = []
        try:
            first = self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
            out.append(first)
        except Empty:
            return out
        # The rest non-blocking
        while True:
            try:
                out.append(self._queue.get_nowait())
            except Empty:
                break
        return out

    def get_callback_error(self) -> Optional[Exception]:
        """Returns the exception from the audio thread, if there was one. Does not clear it."""
        return self._callback_error

    def drain_events(self) -> list[dict]:
        """AudioSource protocol (ADR-026): an endpoint stream has no source events."""
        return []

    # ---------- context manager

    def __enter__(self) -> "WasapiRecorder":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ---------------------------------------------------------------- internals

def _find_wasapi_host_api(pa) -> Optional[int]:
    """Returns the host_api index for WASAPI or None."""
    if pyaudio is None:
        return None
    for i in range(pa.get_host_api_count()):
        info = pa.get_host_api_info_by_index(i)
        if info.get('type') == pyaudio.paWASAPI:
            return i
    return None


def _safe_default_input(pa, wasapi_host_index: int) -> Optional[int]:
    try:
        info = pa.get_host_api_info_by_index(wasapi_host_index)
        idx = info.get('defaultInputDevice', -1)
        return int(idx) if idx >= 0 else None
    except (KeyError, OSError):
        return None


def _safe_default_output(pa, wasapi_host_index: int) -> Optional[int]:
    try:
        info = pa.get_host_api_info_by_index(wasapi_host_index)
        idx = info.get('defaultOutputDevice', -1)
        return int(idx) if idx >= 0 else None
    except (KeyError, OSError):
        return None
