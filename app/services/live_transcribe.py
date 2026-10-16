"""Phase 12.26 (Big Bet): live transcribe during recording.

A background thread that periodically reads the PCM tail of an active recording session,
transcribes it through faster-whisper (the small model for speed) and publishes
the segments through the SSE broker.

Architecture:
- One worker per active session (started from RecordingService.start).
- Internal loop: every INTERVAL seconds → diff PCM bytes since last pass →
  wrap raw PCM as WAV in RAM → WhisperManager.transcribe → emit 'live_segment'.
- Stop trigger: worker.stop_event set on RecordingService.stop.
- Memory: preview segments accumulate in self.segments — after the recording
  they can be peeked at via get_preview(sid), but the full transcript is made
  through the regular transcribe pipeline (accurate and with diarization).

Limitations (intentional MVP):
- No diarization — fast (small model, no pyannote).
- Mic-only stream (system audio is ignored for simplicity).
- Boundary words may get "smeared" — we do not use an overlap window.
- Not stored in the DB. After stop the UI must call the regular
  transcribe for the full transcript with diarization.
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


# Period between transcribe passes. Longer — less CPU, worse latency.
# 8s — the sweet spot: on the 'small' model this is ~1s of processing → the user sees new words
# after 8-9s.
DEFAULT_INTERVAL = 8.0

# Min PCM diff to transcribe — a guard against transcribing emptiness at the start.
MIN_BYTES_FOR_PASS = 16000 * 2 * 2  # ~2s @ 16kHz mono int16


@dataclass
class _StreamState:
    """Phase 12.27: one worker thread per (session, stream).

    `speaker_label` is added to every emitted segment:
    - 'self'  for the mic stream (= seeded "Ви")
    - 'other' for system loopback (another speaker or the system)
    Frontend resolves this through SpeakersUI.resolve(speaker_label).

    `language` — the ISO code of the language for the Whisper decoder; ``None`` = auto-detect
    (per chunk). Passed from the UI through /api/recording/start.
    """
    session_id: str
    stream_name: str  # 'mic' or 'system'
    speaker_label: str  # 'self' or 'other'
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
    """A service that runs workers. One per active session.

    Args:
        whisper_manager: an instance of :class:`WhisperManager` (with the faster-whisper backend).
        broker: SSEBroker for publishing 'live_segment' events.
        model_name: the Whisper model for live (default 'small' — a balance of
            speed/quality).
        interval: seconds between transcribe passes.
        db_path: the graph DB for the name dictionary (K1); ``None`` → the live
            ``Config.BASE_DIR / Config.DATABASE``, read only when
            ``RECALL_VOCAB_MODE`` is enabled.
    """

    def __init__(
        self,
        whisper_manager,
        broker,
        model_name: str = 'small',
        interval: float = DEFAULT_INTERVAL,
        db_path: Optional[str] = None,
    ):
        self._wm = whisper_manager
        self._broker = broker
        self._model_name = model_name
        self._interval = interval
        self._db_path = db_path
        # Key = "session_id:stream_name" — every stream has its own worker thread
        self._streams: dict[str, _StreamState] = {}
        self._lock = threading.RLock()
        # Story 06: a monotonic segment-append counter shared by
        # ALL tracks of a session (session_id -> the last issued number). Each
        # track is transcribed by its own thread at its own pace, so 'start'
        # does not work as a polling cursor — a system segment with a smaller 'start'
        # may be added LATER than an already returned mic segment.
        self._next_seq: dict[str, int] = {}

    @staticmethod
    def _key(session_id: str, stream_name: str) -> str:
        return f"{session_id}:{stream_name}"

    def _decode_options(self) -> dict:
        """Decoding options of the live path (K1). Flags off → ``{}`` without the DB."""
        from app.services import vocabulary
        db_path = self._db_path
        if db_path is None and vocabulary.mode() != 'off':
            from config import Config
            db_path = str(Config.BASE_DIR / Config.DATABASE)
        return vocabulary.decode_options_for('live', db_path)

    def _alloc_seq(self, session_id: str) -> int:
        """Allocate the next append number (Story 06).

        Serialized through ``self._lock`` — the same lock as the stream state —
        so the issue order = the order of the actual append in ``get_preview``,
        regardless of which stream (mic/system) received it.
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
        """Start live-transcribe for one stream of a session. Idempotent.

        Phase 12.27: stream_name determines the speaker_label in the emitted
        segments — 'mic' → 'self', 'system' → 'other'.

        ``language`` — the ISO code for the Whisper decoder ('uk', 'ru', 'en', ...).
        ``None`` or ``'auto'`` → faster-whisper auto-detect per chunk.
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
        """Stop all stream workers of a session. Returns aggregated segments."""
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
        """A snapshot of all preview segments of a session (from all streams).

        ``order_by='start'`` (default) — chronological order, as before
        (used by the UI/copilot). ``order_by='seq'`` — the order of the actual
        append (Story 06): needed by the incremental cursor, because the 'start'
        of the system track may be SMALLER than an already returned mic segment.
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
            return  # the PCM is not created yet
        diff = current_size - state.last_byte_offset
        if diff < MIN_BYTES_FOR_PASS:
            return

        # Read tail bytes
        with open(state.pcm_path, 'rb') as f:
            f.seek(state.last_byte_offset)
            pcm_bytes = f.read(diff)
        if not pcm_bytes:
            return

        # Wrap raw int16 PCM as WAV in a temporary file. ModernWhisperManager
        # needs a file on disk (it reads the duration through ffprobe), not a buffer.
        import tempfile
        wav_buf = self._wrap_pcm_as_wav(pcm_bytes, state.sample_rate, state.channels)
        if wav_buf is None:
            return
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, prefix='live_') as tf:
            tf.write(wav_buf.getvalue())
            tmp_path = tf.name
        try:
            # state.language=None → 'auto'; whisper_manager maps 'auto'/falsy
            # to faster-whisper auto-detect (whisper_manager_new.py:83,145,200).
            result = self._wm.transcribe_with_progress(
                audio_path=tmp_path,
                model_name=self._model_name,
                language=state.language or 'auto',
                decode_options=self._decode_options(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        if 'error' in result:
            logger.warning("[live] transcribe error: %s", result['error'])
            return

        # Shift the offset to the next pass
        bytes_consumed = diff
        # Time offset: total bytes / (rate * channels * 2 bytes/sample)
        time_offset = state.last_byte_offset / (state.sample_rate * state.channels * 2)
        state.last_byte_offset = current_size

        # Append + emit segments with speaker_label
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
        """Wrap raw int16 little-endian PCM as WAV in an in-memory BytesIO."""
        if not pcm_bytes:
            return None
        # Sample width = 2 (int16). An evenness check — if the tail was cut off
        # in the middle of a sample, we trim it.
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
