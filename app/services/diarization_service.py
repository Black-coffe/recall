"""Speaker diarization via pyannote.audio (Phase 10.2).

Architecture:
- Singleton сервіс з lazy-load пайплайну (модель ~1.5GB, ~10s завантаження).
- ``diarize_audio()`` — універсальний прохід для будь-якого аудіо (file, YouTube).
- ``diarize_recording()`` — оптимізований для recording-режиму: mic + system
  пишуться окремо, тому mic.wav діарізуємо як "self" (1 голос гарантовано —
  це користувач за мікрофоном), system.wav — повна діарізація віддалених
  учасників. Цим уникаємо помилок коли pyannote плутає себе з кимось іншим
  у мікшованому потоці.

Мерджимо діарізаційні інтервали з whisper-сегментами через
``assign_speakers_to_whisper_segments()`` — для кожного whisper-сегмента
визначаємо домінантного спікера за overlap'ом.

Робочі pin'и (див. requirements.txt):
- pyannote.audio==3.3.2
- speechbrain==1.0.2
- huggingface_hub<1.0

Error handling: ``is_available()`` повертає False якщо HF_TOKEN не заданий
або import не вдався — caller просто пропустить діарізацію без падіння.
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
"""Зарезервований label для mic-стріму — мапиться на speakers.is_self=1 ('Ви')."""

UNKNOWN_LABEL = 'SPEAKER_UNKNOWN'
"""Whisper-сегмент без перетину з жодним діарізаційним інтервалом."""


# pyannote завантажує аудіо через torchaudio + soundfile (libsndfile),
# який НЕ підтримує AAC/M4A/MP4/WMA/OPUS контейнери. WAV/FLAC/OGG/AIFF/MP3
# (libsndfile 1.1+) працюють напряму. Все інше прозоро конвертуємо у WAV
# 16kHz mono через ffmpeg — pyannote всередині все одно ресемплить до 16kHz.
# УВАГА: '.mp3' тут НЕМАЄ навмисно, хоч soundfile його і ВМІЄ читати.
# pyannote у фазі спікер-ембедингів вирізає сотні коротких фрагментів через
# soundfile.seek, а seek у MP3 без індексу декодує файл з початку на КОЖЕН
# фрагмент → O(n²) по довжині запису. Профільовано py-spy 03.07.2026:
# 32-56-хв записи — 4-8 хв діаризації, 89-хв — 31.5 хв соло (див. §12
# REMEDIATION_PLAN). Разова ffmpeg-конверсія у WAV (секунди) прибирає це:
# WAV seek — O(1). Тому MP3 маршрутизуємо через конвертер нижче.
_SOUNDFILE_NATIVE_EXTS = frozenset({
    '.wav', '.wave', '.flac', '.ogg', '.oga', '.aiff', '.aif',
})


def _ensure_pyannote_compatible(audio_path: Path) -> tuple[Path, Optional[Path]]:
    """Якщо формат не читається soundfile-бекендом АБО читається повільно
    (MP3: O(n²) seek у фазі ембедингів — див. _SOUNDFILE_NATIVE_EXTS) —
    конвертуємо у WAV 16kHz mono.

    Returns:
        (path_to_use, temp_to_cleanup). Якщо конверсія не потрібна,
        temp_to_cleanup=None і path_to_use=audio_path як є.

    Raises:
        RuntimeError: ffmpeg недоступний або не зміг конвертувати.
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
            f'ffmpeg не знайдено у PATH — не можу конвертувати {ext} для діарізації'
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b'').decode('utf-8', errors='replace')[:500]
        raise RuntimeError(
            f'ffmpeg failed для {audio_path.name}: {stderr}'
        ) from e
    logger.info(
        'Diarization: %s сконвертовано у WAV 16kHz mono (%.1f MB)',
        ext, tmp.stat().st_size / 1024 / 1024,
    )
    return tmp, tmp


@dataclass
class DiarSegment:
    """Один інтервал з єдиним спікером."""
    start: float
    end: float
    speaker: str  # 'self' | 'SPEAKER_00' | 'SPEAKER_01' | ...

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class DiarizationResult:
    """Результат діарізаційного проходу.

    Phase 10.6: ``embeddings`` map {raw_label: list[float]} опціонально
    повертається коли pipeline викликаний з return_embeddings=True. Тип —
    звичайний list[float] для сериалізації; numpy не використовуємо у
    inter-module API щоб тести з мокнутим pipeline не вимагали numpy.
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

EMBEDDING_DTYPE_TAG = b'F32V'  # 4-byte magic щоб помітити формат у BLOB
DEFAULT_MATCH_THRESHOLD = 0.75
"""Cosine similarity threshold для auto-match. Емпірично:
    - той самий спікер: 0.80-0.95
    - різні спікери: 0.40-0.65
    - 0.75 — обережний default (мало false-positive на коротких записах)."""


def embedding_to_blob(emb: Sequence[float]) -> bytes:
    """Серіалізує embedding (list/tuple/np.array) у float32 BLOB з magic-tag.

    Format: 4-byte magic 'F32V' + N x float32 (little-endian).
    Magic дозволяє у майбутньому додати нові формати (F64V, INT8V, …)
    без міграції БД.
    """
    if emb is None:
        return b''
    floats = [float(x) for x in emb]
    if not floats:
        return b''
    return EMBEDDING_DTYPE_TAG + struct.pack(f'<{len(floats)}f', *floats)


def blob_to_embedding(blob: Optional[bytes]) -> Optional[list[float]]:
    """Зворотна операція. None якщо blob порожній/невалідний."""
    if not blob or len(blob) < 4:
        return None
    if blob[:4] != EMBEDDING_DTYPE_TAG:
        return None  # незнайомий формат — обережно ігноруємо
    payload = blob[4:]
    if len(payload) % 4 != 0:
        return None
    n = len(payload) // 4
    return list(struct.unpack(f'<{n}f', payload))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity у [-1, 1]. Без numpy — pure Python (~0.1ms на 256d)."""
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
    """Зважене усереднення двох embeddings + L2-нормалізація.

    Використовується для running average у speakers.embedding: при кожному
    PATCH'у з новим іменуванням ваги: a=usage_count (старий avg),
    b=1 (новий sample).
    """
    if a is None or len(a) == 0:
        return _l2_normalize(list(b))
    if len(a) != len(b):
        raise ValueError(f'Розмірності embeddings не співпадають: {len(a)} vs {len(b)}')
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
    """Lazy-singleton обгортка над pyannote.audio Pipeline.

    Використання::

        svc = DiarizationService.get_instance()
        if svc.is_available():
            result = svc.diarize_audio(Path('audio.wav'))

    Pipeline вантажиться при першому виклику ``diarize_*`` (не у __init__),
    щоб app startup не блокувався 10 секундами на завантаження моделі.
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

        # Pre-flight check (не вантажить модель)
        try:
            import pyannote.audio  # noqa: F401
        except ImportError as e:
            self._import_error = f'pyannote.audio not installed: {e}'

    def is_available(self) -> bool:
        """True якщо діарізація може бути запущена (token + import ok)."""
        return self._import_error is None and bool(self.hf_token)

    def unavailability_reason(self) -> Optional[str]:
        """Людиночитна причина недоступності, або None."""
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
        """Lazy-load пайплайну з потокобезпечним guard'ом."""
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

            logger.info('Завантажую pyannote pipeline %s (~1.5GB при першому запуску)…',
                        self.model_id)
            t0 = time.time()
            pipeline = Pipeline.from_pretrained(
                self.model_id,
                use_auth_token=self.hf_token,
            )
            device = self._resolve_device()
            pipeline.to(torch.device(device))
            logger.info('Pipeline готовий (device=%s, %.1fs)', device, time.time() - t0)
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
        """Універсальний діарізаційний прохід.

        Args:
            audio_path: WAV/MP3/будь-який підтримуваний pydub/ffmpeg формат.
            min_speakers / max_speakers: bounds для алгоритму кластерізації.
            num_speakers: точна кількість (override min/max).
            progress_callback: викликається з float у [0.0, 1.0] (опц.).
            return_embeddings: якщо True, у result.embeddings буде map
                {raw_label: list[float]} з voice fingerprints для voice-id.

        Returns:
            DiarizationResult з сегментами {start, end, speaker} де speaker
            — 'SPEAKER_00', 'SPEAKER_01', … (як зазначає pyannote).
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
        # інакше — звичайний Annotation
        embeddings_map: dict[str, list[float]] = {}
        if return_embeddings and isinstance(output, tuple) and len(output) == 2:
            annotation, embeddings_arr = output
            try:
                # embeddings_arr shape: [num_speakers, embedding_dim]
                # порядок labels у annotation.labels()
                labels_in_order = list(annotation.labels())
                for i, label in enumerate(labels_in_order):
                    if i < len(embeddings_arr):
                        # Конвертуємо у звичайний list[float] (numpy → Python)
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
        """Per-stream діарізація для recording-режиму.

        Логіка:
        - Якщо є ``mic_wav`` — діарізуємо, всі лейбли колапсуємо у ``'self'``.
          Робимо це через VAD-equivalent прохід (повний pipeline), бо нам
          цікаво лише "де є мовлення" — точна кластерізація між кількома
          голосами біля мікрофона зайва.
        - Якщо є ``system_wav`` — повна діарізація з ``max_speakers``
          обмеженням; лейбли залишаються як SPEAKER_00, SPEAKER_01, ….
        - Об'єднаний результат — список сегментів з обох потоків,
          відсортований за часом старту. Таймлайни mic/system вже
          синхронні (записувались паралельно), тому просто конкатенуємо.

        Якщо обидва None — повертає порожній результат.
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
            # Усереднюємо всі embeddings у one self-embedding (могло бути
            # кілька SPEAKER_NN у mic'у, наприклад 'ви + друг поряд' — але
            # ми колапсуємо у одну фіксовану особу 'self'). Беремо першу
            # для простоти — якщо кілька, можна усередняти.
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
                # Лейбли SPEAKER_NN з system stream — додаємо як є
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
    """Збагачує whisper-сегменти полем ``speaker``.

    Алгоритм: для кожного whisper-сегмента (з полями ``start``/``end``)
    знаходимо діарізаційний інтервал з найбільшим overlap'ом і записуємо
    його label як ``speaker``. При нульовому перетині — ``UNKNOWN_LABEL``.

    ``overlap_priority`` — tie-breaker при рівних overlap'ах. За замовч.
    'self' має пріоритет (якщо ви говорите одночасно з кимось у Meet —
    мікрофон ваш безперечний, отже ваш сегмент).

    Args:
        whisper_segments: ітерабельне з dict'ів {start, end, text, ...}.
        diarization_segments: list/iter з :class:`DiarSegment`.
        overlap_priority: послідовність labels від найвищого до найнижчого
            пріоритету для tie-breaking.

    Returns:
        Новий список збагачених dict'ів (input не мутує).
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
            # Сортуємо: спочатку за overlap (більше — краще), потім за priority
            def sort_key(item: tuple[str, float]):
                lbl, ov = item
                return (-ov, priority_index.get(lbl, len(priority_index)))
            speaker = min(overlap_per_label.items(), key=sort_key)[0]

        enriched.append({**ws, 'speaker': speaker})

    return enriched


def collect_unique_labels(segments: Iterable[dict]) -> list[str]:
    """Повертає унікальні speaker-labels у порядку першої появи."""
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
    """Шукає у saved_speakers найкращий match для new_embedding.

    Args:
        new_embedding: щойно витягнутий embedding (list[float]).
        saved_speakers: list of (speaker_id, embedding_blob) з БД.
            speaker_id — int, embedding_blob — bytes або None.
        threshold: мінімальна cosine similarity для match (default 0.75).

    Returns:
        (speaker_id, similarity) для найкращого match >= threshold.
        None якщо нічого не підходить.

    T6.8: логує (DEBUG) СИРУ similarity найкращого кандидата — і прийнятого,
    і відхиленого — щоб згодом зібрати розмічений validation-набір і
    перевірити поріг 0.75 на precision/recall (зараз він емпіричний, без
    такого тесту — див. DEFAULT_MATCH_THRESHOLD). Поріг тут НІКОЛИ не
    змінюється — лише спостереження, рішення (return/None) те саме, що й до
    цієї зміни.
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
    """Каркас для майбутньої валідації voice-merge порогу (T6.8).

    НЕ використовується в рантаймі і НЕ змінює :data:`DEFAULT_MATCH_THRESHOLD`
    — чиста функція для offline-аналізу, коли зʼявиться розмічений набір
    (напр. зібраний із DEBUG-логів :func:`find_matching_speaker` + ручна
    розмітка «це справді той самий спікер?»). Тоді результат покаже, чи 0.75
    справді оптимальний за precision/recall, замість здогадки.

    Args:
        pairs: розмічені приклади [(cosine_similarity, is_same_speaker), ...].
        thresholds: кандидатні пороги для порівняння.

    Returns:
        [{"threshold": float, "tp": int, "fp": int, "fn": int, "tn": int,
          "precision": float|None, "recall": float|None}, ...]
        precision/recall — None, якщо знаменник 0 (немає позитивних
        передбачень / немає позитивних прикладів).
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
    """Високорівневий orchestration: діарізація + збагачення segments.

    Маршрутизує:
    - source_type='recording' з manifest.streams_finalized → per-stream
      (mic.wav як 'self', system.wav як SPEAKER_NN). Якщо stream-WAV-ів
      немає (старий manifest) — fallback на mixed audio_path.
    - інакше — diarize_audio(audio_path) на змішаному аудіо.

    Args:
        audio_path: шлях до final mixed audio (MP3 / WAV).
        whisper_segments: список dict'ів з transcribe_with_progress.
        source_type: 'file' | 'youtube' | 'recording'.
        recording_manifest: manifest сесії (якщо recording).
        progress_callback: для SSE update (опц.).
        return_embeddings: якщо True, у tuple повертаємо також dict
            {raw_label: embedding} для voice fingerprinting.

    Returns:
        (enriched_segments, unique_labels, processing_time_sec, embeddings_map)
        embeddings_map порожній якщо return_embeddings=False.
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
            # Старий manifest без streams_finalized — fallback на mix
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
