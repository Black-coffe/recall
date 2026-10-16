"""Speaker diarization via pyannote.audio (Phase 10.2).

Architecture:
- Singleton service with a lazy-loaded pipeline (model ~1.5GB, ~10s to load).
- ``diarize_audio()`` - a universal pass for any audio (file, YouTube).
- ``diarize_recording()`` - optimized for recording mode: mic + system
  are recorded separately, so mic.wav is diarized as "self" (1 voice guaranteed -
  that is the user at the microphone), system.wav gets full diarization of the remote
  participants. This avoids errors when pyannote confuses the user with someone else
  in a mixed stream.

We merge the diarization intervals with the whisper segments via
``assign_speakers_to_whisper_segments()`` - for each whisper segment
we determine the dominant speaker by overlap.

Working pins (see requirements.txt):
- pyannote.audio==3.3.2
- speechbrain==1.0.2
- huggingface_hub<1.0

Error handling: ``is_available()`` returns False if HF_TOKEN is not set
or the import failed - the caller simply skips diarization without crashing.
"""
from __future__ import annotations

import logging
import math
import os
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from app.utils.proc import NO_WINDOW


logger = logging.getLogger(__name__)


SELF_LABEL = 'self'
"""Reserved label for the mic stream - mapped to speakers.is_self=1 ('Ви')."""

UNKNOWN_LABEL = 'SPEAKER_UNKNOWN'
"""A whisper segment with no overlap with any diarization interval."""


# pyannote loads audio via torchaudio + soundfile (libsndfile),
# which does NOT support the AAC/M4A/MP4/WMA/OPUS containers. WAV/FLAC/OGG/AIFF/MP3
# (libsndfile 1.1+) work directly. Everything else is transparently converted to WAV
# 16kHz mono via ffmpeg - pyannote resamples to 16kHz internally anyway.
# NOTE: '.mp3' is deliberately NOT here, although soundfile CAN read it.
# In the speaker-embedding phase pyannote cuts out hundreds of short fragments via
# soundfile.seek, and a seek in an MP3 without an index decodes the file from the start for EVERY
# fragment → O(n²) in the recording length. Profiled with py-spy on 03.07.2026:
# 32-56-minute recordings - 4-8 min of diarization, 89-minute - 31.5 min solo (see §12 of
# REMEDIATION_PLAN). A one-off ffmpeg conversion to WAV (seconds) removes this:
# a WAV seek is O(1). So MP3 is routed through the converter below.
_SOUNDFILE_NATIVE_EXTS = frozenset({
    '.wav', '.wave', '.flac', '.ogg', '.oga', '.aiff', '.aif',
})


def _ensure_pyannote_compatible(audio_path: Path) -> tuple[Path, Optional[Path]]:
    """If the format is not readable by the soundfile backend OR is read slowly
    (MP3: O(n²) seek in the embeddings phase - see _SOUNDFILE_NATIVE_EXTS) -
    convert to WAV 16kHz mono.

    Returns:
        (path_to_use, temp_to_cleanup). If no conversion is needed,
        temp_to_cleanup=None and path_to_use=audio_path as is.

    Raises:
        RuntimeError: ffmpeg is unavailable or could not convert.
    """
    ext = audio_path.suffix.lower()
    if ext in _SOUNDFILE_NATIVE_EXTS:
        return audio_path, None

    tmp = Path(tempfile.gettempdir()) / f'diar_{uuid.uuid4().hex}.wav'
    cmd = [
        'ffmpeg', '-y', '-i', str(audio_path),
        '-ac', '1', '-ar', '16000', '-vn',
        '-loglevel', 'error',
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, creationflags=NO_WINDOW)
    except FileNotFoundError as e:
        raise RuntimeError(
            f'ffmpeg not found in PATH - cannot convert {ext} for diarization'
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b'').decode('utf-8', errors='replace')[:500]
        raise RuntimeError(
            f'ffmpeg failed for {audio_path.name}: {stderr}'
        ) from e
    logger.info(
        'Diarization: %s converted to WAV 16kHz mono (%.1f MB)',
        ext, tmp.stat().st_size / 1024 / 1024,
    )
    return tmp, tmp


@dataclass
class DiarSegment:
    """A single interval with a single speaker."""
    start: float
    end: float
    speaker: str  # 'self' | 'SPEAKER_00' | 'SPEAKER_01' | ...

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class DiarizationResult:
    """Result of a diarization pass.

    Phase 10.6: the ``embeddings`` map {raw_label: list[float]} is optionally
    returned when the pipeline is called with return_embeddings=True. The type is
    a plain list[float] for serialization; we do not use numpy in the
    inter-module API so that tests with a mocked pipeline do not require numpy.
    """
    segments: list[DiarSegment]
    speakers: set[str] = field(default_factory=set)
    duration_sec: float = 0.0
    processing_time_sec: float = 0.0
    embeddings: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self):
        if not self.speakers:
            self.speakers = {s.speaker for s in self.segments}
        if not self.duration_sec and self.segments:
            self.duration_sec = max(s.end for s in self.segments)


# ---------------------------------------------------------------- embedding helpers

EMBEDDING_DTYPE_TAG = b'F32V'  # 4-byte magic to mark the format in the BLOB
DEFAULT_MATCH_THRESHOLD = 0.75
"""Cosine similarity threshold for auto-match. Empirically:
    - same speaker: 0.80-0.95
    - different speakers: 0.40-0.65
    - 0.75 - a cautious default (few false positives on short recordings)."""


def embedding_to_blob(emb: Sequence[float]) -> bytes:
    """Serializes an embedding (list/tuple/np.array) into a float32 BLOB with a magic tag.

    Format: 4-byte magic 'F32V' + N x float32 (little-endian).
    The magic allows adding new formats (F64V, INT8V, …) in the future
    without a DB migration.
    """
    if emb is None:
        return b''
    floats = [float(x) for x in emb]
    if not floats:
        return b''
    return EMBEDDING_DTYPE_TAG + struct.pack(f'<{len(floats)}f', *floats)


def blob_to_embedding(blob: Optional[bytes]) -> Optional[list[float]]:
    """The inverse operation. None if the blob is empty/invalid."""
    if not blob or len(blob) < 4:
        return None
    if blob[:4] != EMBEDDING_DTYPE_TAG:
        return None  # unknown format - ignore carefully
    payload = blob[4:]
    if len(payload) % 4 != 0:
        return None
    n = len(payload) // 4
    return list(struct.unpack(f'<{n}f', payload))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]. Without numpy - pure Python (~0.1ms for 256d)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def average_embeddings(
    a: Optional[Sequence[float]],
    b: Sequence[float],
    weight_a: float = 1.0,
    weight_b: float = 1.0,
) -> list[float]:
    """Weighted average of two embeddings + L2 normalization.

    Used for the running average in speakers.embedding: on every
    PATCH with a new naming the weights are: a=usage_count (the old avg),
    b=1 (the new sample).
    """
    if a is None or len(a) == 0:
        return _l2_normalize(list(b))
    if len(a) != len(b):
        raise ValueError(f'Embedding dimensions do not match: {len(a)} vs {len(b)}')
    total_w = weight_a + weight_b
    averaged = [(a[i] * weight_a + b[i] * weight_b) / total_w for i in range(len(a))]
    return _l2_normalize(averaged)


def _l2_normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        return v
    return [x / norm for x in v]


# ---------------------------------------------------------------- service

class DiarizationService:
    """Lazy-singleton wrapper over the pyannote.audio Pipeline.

    Usage::

        svc = DiarizationService.get_instance()
        if svc.is_available():
            result = svc.diarize_audio(Path('audio.wav'))

    The pipeline is loaded on the first ``diarize_*`` call (not in __init__),
    so that app startup is not blocked for 10 seconds by the model load.
    """

    _instance: Optional['DiarizationService'] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'DiarizationService':
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(
        self,
        hf_token: Optional[str] = None,
        device: str = 'auto',
        model_id: str = 'pyannote/speaker-diarization-3.1',
    ):
        self.hf_token = hf_token or os.environ.get('HF_TOKEN')
        self.model_id = model_id
        self._device = device
        self._pipeline = None
        self._pipeline_lock = threading.Lock()
        self._import_error: Optional[str] = None

        # Pre-flight check (does not load the model)
        try:
            import pyannote.audio  # noqa: F401
        except ImportError as e:
            self._import_error = f'pyannote.audio not installed: {e}'

    def is_available(self) -> bool:
        """True if diarization can be started (token + import ok)."""
        return self._import_error is None and bool(self.hf_token)

    def unavailability_reason(self) -> Optional[str]:
        """Human-readable reason for unavailability, or None."""
        if self._import_error:
            return self._import_error
        if not self.hf_token:
            return 'HF_TOKEN not set in environment / .env'
        return None

    def _resolve_device(self) -> str:
        if self._device != 'auto':
            return self._device
        try:
            import torch
            return 'cuda' if torch.cuda.is_available() else 'cpu'
        except ImportError:
            return 'cpu'

    def _ensure_pipeline(self):
        """Lazy-load the pipeline with a thread-safe guard."""
        if self._pipeline is not None:
            return self._pipeline
        with self._pipeline_lock:
            if self._pipeline is not None:
                return self._pipeline
            if not self.is_available():
                raise RuntimeError(
                    f'Diarization unavailable: {self.unavailability_reason()}'
                )
            from pyannote.audio import Pipeline
            import torch

            logger.info('Loading pyannote pipeline %s (~1.5GB on first run)…',
                        self.model_id)
            t0 = time.time()
            pipeline = Pipeline.from_pretrained(
                self.model_id,
                use_auth_token=self.hf_token,
            )
            device = self._resolve_device()
            pipeline.to(torch.device(device))
            logger.info('Pipeline ready (device=%s, %.1fs)', device, time.time() - t0)
            self._pipeline = pipeline
            return self._pipeline

    # ---------------------------------------------------------------- public

    def diarize_audio(
        self,
        audio_path: Path | str,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
        num_speakers: Optional[int] = None,
        progress_callback: Optional[Callable[[float], None]] = None,
        return_embeddings: bool = False,
    ) -> DiarizationResult:
        """Universal diarization pass.

        Args:
            audio_path: WAV/MP3/any format supported by pydub/ffmpeg.
            min_speakers / max_speakers: bounds for the clustering algorithm.
            num_speakers: the exact count (overrides min/max).
            progress_callback: called with a float in [0.0, 1.0] (optional).
            return_embeddings: if True, result.embeddings will hold a map
                {raw_label: list[float]} with voice fingerprints for voice-id.

        Returns:
            DiarizationResult with segments {start, end, speaker} where speaker
            is 'SPEAKER_00', 'SPEAKER_01', … (as pyannote names them).
        """
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise FileNotFoundError(f'Audio file not found: {audio_path}')

        pipeline = self._ensure_pipeline()
        kwargs: dict = {}
        if num_speakers is not None:
            kwargs['num_speakers'] = num_speakers
        else:
            if min_speakers is not None:
                kwargs['min_speakers'] = min_speakers
            if max_speakers is not None:
                kwargs['max_speakers'] = max_speakers
        if return_embeddings:
            kwargs['return_embeddings'] = True

        load_path, tmp_to_cleanup = _ensure_pyannote_compatible(audio_path)
        t0 = time.time()
        try:
            if progress_callback:
                from pyannote.audio.pipelines.utils.hook import ProgressHook

                class _CbHook(ProgressHook):
                    def __call__(self, step_name, step_artifact, file=None,
                                 total=None, completed=None):
                        super().__call__(step_name, step_artifact, file=file,
                                         total=total, completed=completed)
                        if total and completed is not None:
                            try:
                                progress_callback(min(1.0, completed / total))
                            except Exception:
                                pass

                with _CbHook() as hook:
                    output = pipeline(str(load_path), hook=hook, **kwargs)
            else:
                output = pipeline(str(load_path), **kwargs)
        finally:
            if tmp_to_cleanup is not None:
                try:
                    tmp_to_cleanup.unlink()
                except OSError:
                    pass

        # pipeline(..., return_embeddings=True) → tuple (Annotation, ndarray)
        # otherwise - a regular Annotation
        embeddings_map: dict[str, list[float]] = {}
        if return_embeddings and isinstance(output, tuple) and len(output) == 2:
            annotation, embeddings_arr = output
            try:
                # embeddings_arr shape: [num_speakers, embedding_dim]
                # order of labels in annotation.labels()
                labels_in_order = list(annotation.labels())
                for i, label in enumerate(labels_in_order):
                    if i < len(embeddings_arr):
                        # Convert to a regular list[float] (numpy → Python)
                        emb = embeddings_arr[i]
                        embeddings_map[str(label)] = [float(x) for x in emb]
            except Exception as e:
                logger.warning('Embedding extraction failed: %s', e)
        else:
            annotation = output

        segments = [
            DiarSegment(start=float(turn.start), end=float(turn.end), speaker=str(label))
            for turn, _, label in annotation.itertracks(yield_label=True)
        ]
        segments.sort(key=lambda s: s.start)

        return DiarizationResult(
            segments=segments,
            duration_sec=max((s.end for s in segments), default=0.0),
            processing_time_sec=round(time.time() - t0, 3),
            embeddings=embeddings_map,
        )

    def diarize_recording(
        self,
        mic_wav: Optional[Path | str],
        system_wav: Optional[Path | str],
        max_remote_speakers: Optional[int] = 8,
        progress_callback: Optional[Callable[[float], None]] = None,
        return_embeddings: bool = False,
    ) -> DiarizationResult:
        """Per-stream diarization for recording mode.

        Logic:
        - If there is a ``mic_wav`` - diarize it, collapse all labels into ``'self'``.
          We do this via a VAD-equivalent pass (the full pipeline), because we are
          only interested in "where there is speech" - exact clustering between several
          voices near the microphone is unnecessary.
        - If there is a ``system_wav`` - full diarization with a ``max_speakers``
          limit; labels stay as SPEAKER_00, SPEAKER_01, ….
        - The merged result is a list of segments from both streams,
          sorted by start time. The mic/system timelines are already
          in sync (recorded in parallel), so we simply concatenate.

        If both are None - returns an empty result.
        """
        mic_path = Path(mic_wav) if mic_wav else None
        sys_path = Path(system_wav) if system_wav else None

        all_segments: list[DiarSegment] = []
        total_time = 0.0
        max_end = 0.0
        embeddings_map: dict[str, list[float]] = {}

        # mic → self
        if mic_path and mic_path.is_file() and mic_path.stat().st_size > 0:
            mic_result = self.diarize_audio(mic_path, return_embeddings=return_embeddings)
            for seg in mic_result.segments:
                all_segments.append(DiarSegment(
                    start=seg.start, end=seg.end, speaker=SELF_LABEL,
                ))
            total_time += mic_result.processing_time_sec
            max_end = max(max_end, mic_result.duration_sec)
            # Average all embeddings into one self-embedding (there could be
            # several SPEAKER_NN in the mic, e.g. 'you + a friend nearby' - but
            # we collapse into one fixed person 'self'). We take the first one
            # for simplicity - if there are several, they could be averaged.
            if return_embeddings and mic_result.embeddings:
                first_emb = next(iter(mic_result.embeddings.values()), None)
                if first_emb:
                    embeddings_map[SELF_LABEL] = first_emb
            if progress_callback:
                try:
                    progress_callback(0.5 if sys_path else 1.0)
                except Exception:
                    pass

        # system → SPEAKER_NN
        if sys_path and sys_path.is_file() and sys_path.stat().st_size > 0:
            sys_result = self.diarize_audio(
                sys_path, max_speakers=max_remote_speakers,
                return_embeddings=return_embeddings,
            )
            for seg in sys_result.segments:
                all_segments.append(seg)
            total_time += sys_result.processing_time_sec
            max_end = max(max_end, sys_result.duration_sec)
            if return_embeddings and sys_result.embeddings:
                # SPEAKER_NN labels from the system stream - added as is
                embeddings_map.update(sys_result.embeddings)
            if progress_callback:
                try:
                    progress_callback(1.0)
                except Exception:
                    pass

        all_segments.sort(key=lambda s: s.start)
        return DiarizationResult(
            segments=all_segments,
            duration_sec=max_end,
            processing_time_sec=round(total_time, 3),
            embeddings=embeddings_map,
        )


# ---------------------------------------------------------------- merge logic

def assign_speakers_to_whisper_segments(
    whisper_segments: Iterable[dict],
    diarization_segments: Iterable[DiarSegment],
    overlap_priority: tuple[str, ...] = (SELF_LABEL,),
) -> list[dict]:
    """Enriches whisper segments with a ``speaker`` field.

    Algorithm: for every whisper segment (with ``start``/``end`` fields)
    we find the diarization interval with the largest overlap and write
    its label as ``speaker``. With zero overlap - ``UNKNOWN_LABEL``.

    ``overlap_priority`` - a tie-breaker for equal overlaps. By default
    'self' has priority (if you speak simultaneously with someone in Meet -
    the microphone is undeniably yours, hence the segment is yours).

    Args:
        whisper_segments: an iterable of dicts {start, end, text, ...}.
        diarization_segments: a list/iterable of :class:`DiarSegment`.
        overlap_priority: a sequence of labels from the highest to the lowest
            priority for tie-breaking.

    Returns:
        A new list of enriched dicts (the input is not mutated).
    """
    diar_list = list(diarization_segments)
    priority_index = {label: i for i, label in enumerate(overlap_priority)}

    enriched: list[dict] = []
    for ws in whisper_segments:
        ws_start = float(ws.get('start', 0.0))
        ws_end = float(ws.get('end', ws_start))
        ws_dur = max(0.0, ws_end - ws_start)

        # Acc overlap per label
        overlap_per_label: dict[str, float] = {}
        for ds in diar_list:
            ov = min(ws_end, ds.end) - max(ws_start, ds.start)
            if ov > 0:
                overlap_per_label[ds.speaker] = overlap_per_label.get(ds.speaker, 0.0) + ov

        if not overlap_per_label:
            speaker = UNKNOWN_LABEL
        else:
            # Sort: first by overlap (more is better), then by priority
            def sort_key(item: tuple[str, float]):
                lbl, ov = item
                return (-ov, priority_index.get(lbl, len(priority_index)))
            speaker = min(overlap_per_label.items(), key=sort_key)[0]

        enriched.append({**ws, 'speaker': speaker})

    return enriched


def collect_unique_labels(segments: Iterable[dict]) -> list[str]:
    """Returns the unique speaker labels in order of first appearance."""
    seen: dict[str, None] = {}
    for s in segments:
        sp = s.get('speaker')
        if sp and sp not in seen:
            seen[sp] = None
    return list(seen)


# ---------------------------------------------------------------- orchestration

def find_matching_speaker(
    new_embedding: Sequence[float],
    saved_speakers: Sequence[tuple[int, Optional[bytes]]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> Optional[tuple[int, float]]:
    """Looks in saved_speakers for the best match for new_embedding.

    Args:
        new_embedding: a freshly extracted embedding (list[float]).
        saved_speakers: a list of (speaker_id, embedding_blob) from the DB.
            speaker_id is an int, embedding_blob is bytes or None.
        threshold: the minimum cosine similarity for a match (default 0.75).

    Returns:
        (speaker_id, similarity) for the best match >= threshold.
        None if nothing fits.

    T6.8: logs (DEBUG) the RAW similarity of the best candidate - both accepted
    and rejected - to later collect a labeled validation set and
    check the 0.75 threshold on precision/recall (for now it is empirical, without
    such a test - see DEFAULT_MATCH_THRESHOLD). The threshold here is NEVER
    changed - only observation, the decision (return/None) is the same as before
    this change.
    """
    if not new_embedding:
        return None
    best_id: Optional[int] = None
    best_sim = -1.0
    n_candidates = 0
    for sid, blob in saved_speakers:
        emb = blob_to_embedding(blob)
        if not emb:
            continue
        n_candidates += 1
        sim = cosine_similarity(new_embedding, emb)
        if sim > best_sim:
            best_sim = sim
            best_id = sid
    if n_candidates == 0:
        return None
    accepted = best_id is not None and best_sim >= threshold
    logger.debug(
        'Voice-merge candidate: best_sim=%.4f threshold=%.2f candidates=%d '
        'decision=%s speaker_id=%s',
        best_sim, threshold, n_candidates,
        'accepted' if accepted else 'rejected', best_id if accepted else None,
    )
    if accepted:
        return (best_id, best_sim)
    return None


def precision_recall_at_thresholds(
    pairs: Sequence[tuple[float, bool]],
    thresholds: Sequence[float] = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90),
) -> list[dict]:
    """Scaffold for the future validation of the voice-merge threshold (T6.8).

    NOT used at runtime and does NOT change :data:`DEFAULT_MATCH_THRESHOLD`
    - a pure function for offline analysis, once a labeled set appears
    (e.g. collected from the DEBUG logs of :func:`find_matching_speaker` + manual
    labeling "is this really the same speaker?"). Then the result will show whether 0.75
    really is optimal by precision/recall, instead of a guess.

    Args:
        pairs: labeled examples [(cosine_similarity, is_same_speaker), ...].
        thresholds: candidate thresholds to compare.

    Returns:
        [{"threshold": float, "tp": int, "fp": int, "fn": int, "tn": int,
          "precision": float|None, "recall": float|None}, ...]
        precision/recall - None if the denominator is 0 (no positive
        predictions / no positive examples).
    """
    out: list[dict] = []
    for thr in thresholds:
        tp = fp = fn = tn = 0
        for sim, is_same in pairs:
            predicted = sim >= thr
            if predicted and is_same:
                tp += 1
            elif predicted and not is_same:
                fp += 1
            elif not predicted and is_same:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        out.append({
            "threshold": thr, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
        })
    return out


def run_diarization_for_audio(
    audio_path: Path | str,
    whisper_segments: list[dict],
    *,
    source_type: str = 'file',
    recording_manifest: Optional[dict] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    return_embeddings: bool = False,
) -> tuple[list[dict], list[str], float, dict[str, list[float]]]:
    """High-level orchestration: diarization + segment enrichment.

    Routes:
    - source_type='recording' with manifest.streams_finalized → per-stream
      (mic.wav as 'self', system.wav as SPEAKER_NN). If there are no stream WAVs
      (an old manifest) - fallback to the mixed audio_path.
    - otherwise - diarize_audio(audio_path) on the mixed audio.

    Args:
        audio_path: path to the final mixed audio (MP3 / WAV).
        whisper_segments: a list of dicts from transcribe_with_progress.
        source_type: 'file' | 'youtube' | 'recording'.
        recording_manifest: the session manifest (if a recording).
        progress_callback: for the SSE update (optional).
        return_embeddings: if True, the tuple also returns a dict
            {raw_label: embedding} for voice fingerprinting.

    Returns:
        (enriched_segments, unique_labels, processing_time_sec, embeddings_map)
        embeddings_map is empty if return_embeddings=False.
    """
    svc = DiarizationService.get_instance()
    if not svc.is_available():
        raise RuntimeError(
            f'Diarization unavailable: {svc.unavailability_reason()}'
        )

    diar_result: DiarizationResult

    if source_type == 'recording' and recording_manifest:
        streams = recording_manifest.get('streams_finalized') or {}
        mic_wav = (streams.get('mic') or {}).get('wav_path')
        sys_wav = (streams.get('system') or {}).get('wav_path')
        if mic_wav or sys_wav:
            diar_result = svc.diarize_recording(
                mic_wav=mic_wav,
                system_wav=sys_wav,
                progress_callback=progress_callback,
                return_embeddings=return_embeddings,
            )
        else:
            # Old manifest without streams_finalized - fallback to the mix
            diar_result = svc.diarize_audio(
                audio_path, progress_callback=progress_callback,
                return_embeddings=return_embeddings,
            )
    else:
        diar_result = svc.diarize_audio(
            audio_path, progress_callback=progress_callback,
            return_embeddings=return_embeddings,
        )

    enriched = assign_speakers_to_whisper_segments(
        whisper_segments, diar_result.segments,
    )
    labels = collect_unique_labels(enriched)
    return enriched, labels, diar_result.processing_time_sec, diar_result.embeddings
