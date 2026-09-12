"""Phase 12.26 (Big Bet): live transcribe під час recording.

Background thread що періодично читає PCM tail з активної recording-сесії,
транскрибує через faster-whisper (small model для скорості) і публікує
сегменти через SSE-broker.

Архітектура:
- Один worker per active session (запускається з RecordingService.start).
- Internal loop: every INTERVAL seconds → diff PCM bytes since last pass →
  wrap raw PCM як WAV у RAM → WhisperManager.transcribe → emit 'live_segment'.
- Stop trigger: worker.stop_event set on RecordingService.stop.
- Memory: накопичується preview-segments у self.segments — post-recording
  можна підглянути через get_preview(sid), але повний транскрипт робиться
  через звичайний transcribe pipeline (точна та з diariz).

Limitations (intentional MVP):
- Без diariz — швидко (small model, без pyannote).
- Mic-only stream (system audio ignore'ится для simplicity).
- Boundary words можуть "змиватись" — не використовуємо overlap window.
- Не зберігається у DB. Після stop UI повинен викликати звичайний
  transcribe для повного транскрипту з diariz.
"""
from __future__ import annotations

import io
import logging
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


# Period між transcribe passes. Більше — менше CPU, гірше latency.
# 8s — sweet spot: на 'small' model це ~1s обробки → user бачить нові слова
# через 8-9s.
DEFAULT_INTERVAL = 8.0

# Min PCM diff щоб transcribe — pad-проти transcribe пустоти на старті.
MIN_BYTES_FOR_PASS = 16000 * 2 * 2  # ~2s @ 16kHz mono int16


@dataclass
class _StreamState:
    """Phase 12.27: один worker thread per (session, stream).

    `speaker_label` додається до кожного emit-нутого segment'а:
    - 'self'  для mic stream (= seeded "Ви")
    - 'other' для system loopback (інший speaker або система)
    Frontend resolves це через SpeakersUI.resolve(speaker_label).

    `language` — ISO-код мови для Whisper decoder; ``None`` = auto-detect
    (per-chunk). Передається з UI через /api/recording/start.
    """
    session_id: str
    stream_name: str  # 'mic' або 'system'
    speaker_label: str  # 'self' або 'other'
    pcm_path: str
    sample_rate: int
    channels: int
    language: Optional[str] = None
    last_byte_offset: int = 0
    segments: list[dict] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    error: Optional[str] = None


class LiveTranscribeWorker:
    """Сервіс що крутить worker'ів. Один на активну сесію.

    Args:
        whisper_manager: instance of :class:`WhisperManager` (з faster-whisper backend).
        broker: SSEBroker для publish 'live_segment' events.
        model_name: модель Whisper для live (default 'small' — баланс
            швидкість/якість).
        interval: seconds між transcribe passes.
    """

    def __init__(
        self,
        whisper_manager,
        broker,
        model_name: str = 'small',
        interval: float = DEFAULT_INTERVAL,
    ):
        self._wm = whisper_manager
        self._broker = broker
        self._model_name = model_name
        self._interval = interval
        # Key = "session_id:stream_name" — кожен stream має свій worker thread
        self._streams: dict[str, _StreamState] = {}
        self._lock = threading.RLock()
        # Story 06: монотонний лічильник додавання сегментів, спільний для
        # ВСІХ доріжок сесії (session_id -> останній виданий номер). Кожна
        # доріжка транскрибується своїм thread'ом у своєму темпі, тому 'start'
        # не годиться як курсор опитування — system-сегмент з меншим 'start'
        # може бути доданий ПІЗНІШЕ за вже відданий mic-сегмент.
        self._next_seq: dict[str, int] = {}

    @staticmethod
    def _key(session_id: str, stream_name: str) -> str:
        return f"{session_id}:{stream_name}"

    def _alloc_seq(self, session_id: str) -> int:
        """Виділити наступний номер додавання (Story 06).

        Серіалізовано через ``self._lock`` — той самий лок, що і stream-стан —
        тому порядок видачі = порядок фактичного append'у в ``get_preview``,
        незалежно від того, який stream (mic/system) його отримав.
        """
        with self._lock:
            seq = self._next_seq.get(session_id, 0) + 1
            self._next_seq[session_id] = seq
            return seq

    def start(
        self,
        session_id: str,
        pcm_path: str,
        sample_rate: int = 16000,
        channels: int = 1,
        stream_name: str = 'mic',
        language: Optional[str] = None,
    ) -> None:
        """Запустити live-transcribe для одного stream'а сесії. Idempotent.

        Phase 12.27: stream_name визначає speaker_label у emit-нутих
        segments — 'mic' → 'self', 'system' → 'other'.

        ``language`` — ISO-код для Whisper decoder ('uk', 'ru', 'en', ...).
        ``None`` або ``'auto'`` → faster-whisper auto-detect per chunk.
        """
        speaker_label = 'self' if stream_name == 'mic' else 'other'
        if not language or language == 'auto':
            language = None
        key = self._key(session_id, stream_name)
        with self._lock:
            if key in self._streams:
                logger.debug("LiveTranscribeWorker already running for %s", key)
                return
            state = _StreamState(
                session_id=session_id,
                stream_name=stream_name,
                speaker_label=speaker_label,
                pcm_path=pcm_path,
                sample_rate=sample_rate,
                channels=channels,
                language=language,
            )
            t = threading.Thread(
                target=self._loop, args=(state,),
                name=f"live-transcribe-{session_id[:8]}-{stream_name}", daemon=True,
            )
            state.thread = t
            self._streams[key] = state
            t.start()
            logger.info(
                "LiveTranscribeWorker started for %s (stream=%s, label=%s, lang=%s, pcm=%s)",
                session_id, stream_name, speaker_label, language or 'auto', pcm_path,
            )

    def stop(self, session_id: str, wait: bool = False) -> Optional[list[dict]]:
        """Зупинити всі stream-worker'и сесії. Повертає aggregated segments."""
        all_segments: list[dict] = []
        threads = []
        with self._lock:
            keys_to_pop = [k for k in self._streams if k.startswith(f"{session_id}:")]
            for k in keys_to_pop:
                st = self._streams.pop(k)
                st.stop_event.set()
                all_segments.extend(st.segments)
                if st.thread:
                    threads.append(st.thread)
            self._next_seq.pop(session_id, None)
        if not keys_to_pop:
            return None
        if wait:
            for t in threads:
                t.join(timeout=10.0)
        return all_segments

    def get_preview(self, session_id: str, order_by: str = 'start') -> list[dict]:
        """Snapshot всіх preview-segments сесії (з усіх streams).

        ``order_by='start'`` (default) — хронологічний порядок, як і раніше
        (використовує UI/copilot). ``order_by='seq'`` — порядок фактичного
        додавання (Story 06): потрібен інкрементальному курсору, бо 'start'
        системної доріжки може бути МЕНШИЙ за вже відданий mic-сегмент.
        """
        with self._lock:
            segments = []
            for k, st in self._streams.items():
                if k.startswith(f"{session_id}:"):
                    segments.extend(st.segments)
        key = (lambda s: s.get('seq', 0)) if order_by == 'seq' else (lambda s: s.get('start', 0))
        return sorted(segments, key=key)

    def is_active(self, session_id: str) -> bool:
        prefix = f"{session_id}:"
        with self._lock:
            return any(k.startswith(prefix) for k in self._streams)

    # ============== internals ==============

    def _loop(self, state: _StreamState) -> None:
        try:
            while not state.stop_event.is_set():
                state.stop_event.wait(self._interval)
                if state.stop_event.is_set():
                    break
                try:
                    self._process_tail(state)
                except Exception as e:
                    logger.warning("[live] pass error for %s/%s: %s",
                                   state.session_id, state.stream_name, e, exc_info=True)
                    state.error = str(e)
        finally:
            logger.info("LiveTranscribeWorker stopped for %s/%s (segments=%d)",
                        state.session_id, state.stream_name, len(state.segments))

    def _process_tail(self, state: _StreamState) -> None:
        import os
        try:
            current_size = os.path.getsize(state.pcm_path)
        except FileNotFoundError:
            return  # PCM ще не створений
        diff = current_size - state.last_byte_offset
        if diff < MIN_BYTES_FOR_PASS:
            return

        # Read tail bytes
        with open(state.pcm_path, 'rb') as f:
            f.seek(state.last_byte_offset)
            pcm_bytes = f.read(diff)
        if not pcm_bytes:
            return

        # Wrap raw int16 PCM як WAV у тимчасовий файл. ModernWhisperManager
        # потребує файлу на диску (читає duration через ffprobe), не буфер.
        import tempfile
        wav_buf = self._wrap_pcm_as_wav(pcm_bytes, state.sample_rate, state.channels)
        if wav_buf is None:
            return
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, prefix='live_') as tf:
            tf.write(wav_buf.getvalue())
            tmp_path = tf.name
        try:
            # state.language=None → 'auto'; whisper_manager мапить 'auto'/falsy
            # у faster-whisper auto-detect (whisper_manager_new.py:83,145,200).
            result = self._wm.transcribe_with_progress(
                audio_path=tmp_path,
                model_name=self._model_name,
                language=state.language or 'auto',
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        if 'error' in result:
            logger.warning("[live] transcribe error: %s", result['error'])
            return

        # Зсунути offset на наступний pass
        bytes_consumed = diff
        # Time offset: total bytes / (rate * channels * 2 bytes/sample)
        time_offset = state.last_byte_offset / (state.sample_rate * state.channels * 2)
        state.last_byte_offset = current_size

        # Append + emit segments з speaker_label
        new_segments = []
        for seg in (result.get('segments') or []):
            absolute_start = time_offset + float(seg.get('start', 0))
            absolute_end = time_offset + float(seg.get('end', 0))
            seg_obj = {
                'start': round(absolute_start, 2),
                'end': round(absolute_end, 2),
                'text': (seg.get('text') or '').strip(),
                'speaker': state.speaker_label,
                'stream': state.stream_name,
            }
            if seg_obj['text']:
                seg_obj['seq'] = self._alloc_seq(state.session_id)
                state.segments.append(seg_obj)
                new_segments.append(seg_obj)

        if new_segments and self._broker is not None:
            try:
                self._broker.publish(
                    f'recording:{state.session_id}',
                    'live_segment',
                    {
                        'segments': new_segments,
                        'stream': state.stream_name,
                        'speaker': state.speaker_label,
                    },
                )
            except Exception as e:
                logger.debug("[live] broker publish error: %s", e)

    @staticmethod
    def _wrap_pcm_as_wav(pcm_bytes: bytes, rate: int, channels: int) -> Optional[io.BytesIO]:
        """Wrap raw int16 little-endian PCM як WAV у in-memory BytesIO."""
        if not pcm_bytes:
            return None
        # Sample width = 2 (int16). Чек на парність — якщо tail обірвався
        # посередині sample, відрізаємо.
        if len(pcm_bytes) % 2 == 1:
            pcm_bytes = pcm_bytes[:-1]
        if channels > 1 and len(pcm_bytes) % (channels * 2) != 0:
            cut = len(pcm_bytes) - (len(pcm_bytes) % (channels * 2))
            pcm_bytes = pcm_bytes[:cut]
        if not pcm_bytes:
            return None
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm_bytes)
        buf.seek(0)
        return buf
