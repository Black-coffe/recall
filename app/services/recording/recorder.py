"""WASAPI recorder primitive (Phase 9.1).

Тонкий wrapper навколо PyAudioWPatch (форк PyAudio з підтримкою WASAPI
loopback). Розв'язує задачі:

- Енумерація input-девайсів: і звичайні мікрофони, і loopback-копії
  output-девайсів (для запису того, що грає система).
- Відкриття callback-стріму на конкретний девайс із буферизацією
  кадрів у :class:`queue.Queue`.
- Метеринг рівнів сигналу (RMS + peak) у rolling-вікні 100ms — UI
  читає їх для VU-смуг.

Вищий рівень (session storage, фіналізація, REST API) — у наступних
підфазах 9.2-9.5.

Чому окремий модуль, а не весь pipeline в одному:
- Recorder primitive = тонка обгортка навколо C-бібліотеки. Тестується
  ізольовано: чи відкрив, чи рахує рівні, чи коректно закрив.
- Session storage та фіналізація — pure Python без C-залежностей,
  тестуються без аудіо-девайсів.
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


# Int16 PCM: 2 байти на семпл; float-нормалізація для рівнів — на /32768.
_SAMPLE_WIDTH_BYTES = 2
_INT16_MAX = 32768.0
_LEVEL_WINDOW_SECONDS = 0.1  # 100ms rolling RMS+peak


class RecorderError(RuntimeError):
    """Помилка ініціалізації або роботи рекордера."""


@dataclass(frozen=True)
class DeviceInfo:
    """Спрощений опис WASAPI input-девайсу.

    `kind`:
    - ``"mic"`` — звичайний input (мікрофон, лінійний вхід).
    - ``"loopback"`` — спеціальний індекс loopback-копії output-девайсу
      (PyAudioWPatch створює їх для кожного output-девайсу).
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
    """Чанк аудіо-даних з рекордера. Bytes = native int16 LE PCM."""
    data: bytes
    timestamp: float
    frame_count: int


# ---------------------------------------------------------------- public api

def list_input_devices() -> list[DeviceInfo]:
    """Повертає WASAPI input-девайси: спочатку мікрофони, потім loopback'и.

    Loopback-девайси доступні лише на Windows через PyAudioWPatch — це
    «віртуальні input'и», які зчитують те, що грає на відповідному
    output-девайсі.

    Якщо PyAudioWPatch не встановлений — повертає порожній список і
    логує warning. Викликаючий код має обробити пустий список як
    «recording feature unavailable».
    """
    if not _HAS_PYAUDIO:
        logger.warning("pyaudiowpatch недоступний — recording disabled")
        return []

    pa = pyaudio.PyAudio()
    try:
        wasapi_host_index = _find_wasapi_host_api(pa)
        if wasapi_host_index is None:
            logger.warning("WASAPI host API не знайдено")
            return []

        default_input_index = _safe_default_input(pa, wasapi_host_index)
        default_output_index = _safe_default_output(pa, wasapi_host_index)

        devices: list[DeviceInfo] = []

        # Мікрофони (звичайні WASAPI input'и).
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get('hostApi') != wasapi_host_index:
                continue
            if info.get('maxInputChannels', 0) <= 0:
                continue
            # Loopback-копії приходять окремим генератором — пропускаємо.
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
            logger.warning("Не вдалось отримати loopback-девайси: %s", e)
            loopbacks = []

        for info in loopbacks:
            host_api = info.get('hostApi')
            if host_api != wasapi_host_index:
                continue
            # Loopback indexes — окремі від звичайних input/output.
            idx = int(info['index'])
            # Чи це loopback default output'у?
            # PyAudioWPatch присвоює loopback-копії індекс = (output_index +
            # offset). Перевіряємо за іменем (містить « (loopback)»).
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
    """Девайс не існує / не має input-каналів. Відрізняється від format
    issues — fallback на defaults для нього unsafe (WASAPI native SEGV)."""


def resolve_device_params(
    device_index: int,
    requested_sample_rate: int,
    requested_channels: int,
) -> tuple[int, int]:
    """Коригує запитувані параметри під реальні можливості девайсу.

    Якщо девайс не підтримує запитувану кількість каналів (наприклад,
    моно-мікрофон при requested=2) — повертаємо ``min(requested, max_supported)``.

    Якщо запитуваний sample_rate не підтримується через
    ``is_format_supported`` — fallback на ``defaultSampleRate`` девайсу.

    Повертає ``(sample_rate, channels)`` готовий до ``pa.open(...)``.

    Raises:
        DeviceNotAvailableError: якщо девайс не існує — caller MUST abort,
            bo native WASAPI потім SEGV-не на ``pa.open()``.
        RecorderError: інші помилки.
    """
    if not _HAS_PYAUDIO:
        raise RecorderError("pyaudiowpatch не встановлений")

    pa = pyaudio.PyAudio()
    try:
        try:
            info = pa.get_device_info_by_index(device_index)
        except OSError as e:
            raise DeviceNotAvailableError(f"Девайс {device_index} не знайдено: {e}") from e

        max_channels = int(info.get('maxInputChannels', 0))
        if max_channels <= 0:
            raise DeviceNotAvailableError(f"Девайс {device_index} не має input-каналів")

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
                "Sample rate %d не підтримується девайсом %d (%s), "
                "fallback на %d",
                sample_rate, device_index, info.get('name'), fallback_rate,
            )
            sample_rate = fallback_rate

        return sample_rate, channels
    finally:
        pa.terminate()


# ---------------------------------------------------------------- LevelMeter

class LevelMeter:
    """Rolling peak + RMS у вікні ~100ms.

    Thread-safe: оновлення викликаються з audio callback (PortAudio
    thread), читання — з UI/SSE thread'у.

    Семантика рівнів:
    - ``peak`` (0..1): максимум абсолютного значення семплу за вікно.
    - ``rms`` (0..1): RMS-значення за вікно (краще корелює зі сприйнятою
      гучністю).

    Обидва значення нормовані: для int16 ділимо на 32768.
    """

    def __init__(self, sample_rate: int, channels: int,
                 window_seconds: float = _LEVEL_WINDOW_SECONDS):
        self._sample_rate = sample_rate
        self._channels = channels
        self._window_samples = max(1, int(sample_rate * window_seconds))
        # Буфер модулей семплів (float, нормованих 0..1) для RMS/peak.
        self._buf: deque[float] = deque(maxlen=self._window_samples)
        self._sq_sum: float = 0.0  # для O(1) RMS
        self._lock = threading.Lock()

    def update(self, pcm_bytes: bytes) -> None:
        """Скормити рекордерські PCM-байти. int16 LE assumed."""
        if not pcm_bytes:
            return
        # Беремо тільки кожен N-й семпл щоб знизити CPU при високих rate.
        # 100ms @ 48kHz stereo = 4800*2 семплів → ми перерахуємо <=480.
        stride = max(1, len(pcm_bytes) // (_SAMPLE_WIDTH_BYTES * 480))
        # int16 LE: 2 байти, signed
        with self._lock:
            i = 0
            n = len(pcm_bytes)
            stride_bytes = _SAMPLE_WIDTH_BYTES * stride
            while i < n - 1:
                # ручний int16 LE → щоб не тягнути numpy у hot-path
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
            # Запобігаємо drift'у через float-помилки
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
        """Атомарний зріз peak/rms для SSE."""
        with self._lock:
            if not self._buf:
                return {'peak': 0.0, 'rms': 0.0}
            return {
                'peak': max(self._buf),
                'rms': math.sqrt(self._sq_sum / len(self._buf)),
            }


# ---------------------------------------------------------------- recorder

class WasapiRecorder:
    """Контекстний менеджер для одного WASAPI стріму.

    Usage::

        with WasapiRecorder(device_index=12, sample_rate=48000, channels=2) as rec:
            rec.start()
            time.sleep(5)
            for frame in rec.drain():
                save_to_disk(frame.data)
            print(rec.level.snapshot())

    Ключові інваріанти:
    - Стрім відкривається у callback-режимі: PortAudio викликає
      ``_callback`` зі своєю частотою. Кадри потрапляють у Queue
      обмеженого розміру.
    - Якщо споживач не встигає (Queue full) — старі кадри ДРОПАЮТЬСЯ
      і інкрементується ``dropped_frames`` (але це попередження, а не
      помилка — система все одно піде далі).
    - LevelMeter оновлюється в callback'у синхронно: рівні «свіжі»
      навіть якщо споживач відстає.
    - Закриття — або через context manager, або :meth:`close`. Подвійний
      close безпечний.
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
            raise RecorderError("pyaudiowpatch не встановлений")
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

        # Метрики
        self.dropped_frames = 0
        self.total_bytes = 0
        self.last_callback_ts: float = 0.0

        self._callback_error: Optional[Exception] = None

    # ---------- lifecycle

    def open(self) -> None:
        """Створює PyAudio та відкриває стрім (без старту)."""
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
                f"Не вдалось відкрити WASAPI стрім (device={self.device_index}, "
                f"rate={self.sample_rate}, channels={self.channels}): {e}"
            ) from e

    def start(self) -> None:
        """Розпочати фактичний приймом кадрів."""
        if self._stream is None:
            self.open()
        if self._started:
            return
        assert self._stream is not None
        self._stream.start_stream()
        self._started = True

    def stop(self) -> None:
        """Зупинити приймом, але не закривати стрім (можна start() знову)."""
        if self._stream is not None and self._started:
            try:
                self._stream.stop_stream()
            except OSError as e:
                logger.warning("stop_stream() error: %s", e)
            self._started = False

    def close(self) -> None:
        """Зупинити і звільнити ресурси. Idempotent."""
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
        """PortAudio callback. Викликається у audio thread (НЕ main).

        Жодного блокуючого I/O тут — тільки enqueue + level update.
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
                    # Споживач відстає — дропаємо найстаріший кадр.
                    try:
                        self._queue.get_nowait()
                    except Empty:
                        pass
                    self.dropped_frames += 1
                    try:
                        self._queue.put_nowait(frame)
                    except Full:
                        # Дуже маловірогідно але краще не блокувати callback
                        self.dropped_frames += 1
        except Exception as e:  # pragma: no cover — захист audio thread
            self._callback_error = e
            logger.exception("WasapiRecorder callback error: %s", e)
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    # ---------- consumer api

    def drain(self, timeout: float = 0.0) -> list[AudioFrame]:
        """Дістати усі готові кадри з черги.

        Блокує до ``timeout`` секунд на перший кадр; решту вибирає
        non-blocking. Повертає [] якщо нічого нема.
        """
        out: list[AudioFrame] = []
        try:
            first = self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
            out.append(first)
        except Empty:
            return out
        # Решту non-blocking
        while True:
            try:
                out.append(self._queue.get_nowait())
            except Empty:
                break
        return out

    def get_callback_error(self) -> Optional[Exception]:
        """Повертає виключення з audio thread, якщо було. Не очищає."""
        return self._callback_error

    # ---------- context manager

    def __enter__(self) -> "WasapiRecorder":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ---------------------------------------------------------------- internals

def _find_wasapi_host_api(pa) -> Optional[int]:
    """Повертає host_api index для WASAPI або None."""
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
