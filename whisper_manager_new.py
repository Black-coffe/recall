#!/usr/bin/env python3
"""
ModernWhisperManager — менеджер моделей Whisper с pluggable backend.

Backends:
- "faster" — faster-whisper (CTranslate2). 3-5x быстрее на GPU, ниже VRAM.
- "openai" — официальный openai-whisper (референсная реализация, fallback).

Контракт API сохранён: app.py не меняет вызовы.
Параллельность: load_model сериализован, transcribe идёт под семафором.
"""

import os
import gc
import logging
import subprocess
import tempfile
import shutil
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any

import psutil
import torch
from pydub import AudioSegment

from app.utils.proc import NO_WINDOW, silence_openai_whisper_console_windows


logger = logging.getLogger(__name__)


# ============================================================================
# faster-whisper model catalog (DYNAMIC)
# ----------------------------------------------------------------------------
# The list of selectable models is read live from faster_whisper.utils._MODELS
# (see get_available_models) so it NEVER goes stale — when faster-whisper is
# upgraded and learns new models, they appear automatically. This META only
# supplies display info (size/speed/multilingual); a model missing from META
# still shows up, just with minimal info derived from its name.
# ============================================================================
FASTER_MODELS_META = {
    "tiny":             {"size": "39 MB",   "params": "39M",   "speed": "~32x", "memory": "~1 GB",  "multilingual": True},
    "tiny.en":          {"size": "39 MB",   "params": "39M",   "speed": "~32x", "memory": "~1 GB",  "multilingual": False},
    "base":             {"size": "74 MB",   "params": "74M",   "speed": "~16x", "memory": "~1 GB",  "multilingual": True},
    "base.en":          {"size": "74 MB",   "params": "74M",   "speed": "~16x", "memory": "~1 GB",  "multilingual": False},
    "small":            {"size": "244 MB",  "params": "244M",  "speed": "~6x",  "memory": "~2 GB",  "multilingual": True},
    "small.en":         {"size": "244 MB",  "params": "244M",  "speed": "~6x",  "memory": "~2 GB",  "multilingual": False},
    "distil-small.en":  {"size": "166 MB",  "params": "166M",  "speed": "~12x", "memory": "~2 GB",  "multilingual": False, "distil": True},
    "medium":           {"size": "769 MB",  "params": "769M",  "speed": "~2x",  "memory": "~5 GB",  "multilingual": True},
    "medium.en":        {"size": "769 MB",  "params": "769M",  "speed": "~2x",  "memory": "~5 GB",  "multilingual": False},
    "distil-medium.en": {"size": "394 MB",  "params": "394M",  "speed": "~6x",  "memory": "~3 GB",  "multilingual": False, "distil": True},
    "large-v1":         {"size": "1550 MB", "params": "1550M", "speed": "~1x",  "memory": "~10 GB", "multilingual": True},
    "large-v2":         {"size": "1550 MB", "params": "1550M", "speed": "~1x",  "memory": "~10 GB", "multilingual": True},
    "large-v3":         {"size": "1550 MB", "params": "1550M", "speed": "~1x",  "memory": "~10 GB", "multilingual": True},
    "distil-large-v2":  {"size": "756 MB",  "params": "756M",  "speed": "~6x",  "memory": "~6 GB",  "multilingual": False, "distil": True},
    "distil-large-v3":  {"size": "756 MB",  "params": "756M",  "speed": "~6x",  "memory": "~6 GB",  "multilingual": False, "distil": True},
    "distil-large-v3.5":{"size": "756 MB",  "params": "756M",  "speed": "~6x",  "memory": "~6 GB",  "multilingual": False, "distil": True},
    "large-v3-turbo":   {"size": "809 MB",  "params": "809M",  "speed": "~4x",  "memory": "~6 GB",  "multilingual": True,  "recommended": True},
}
# Bare aliases that duplicate a canonical model — hidden from the catalog.
FASTER_MODEL_ALIASES = {"large", "turbo"}


def _hf_models_map():
    """name -> HF repo for the installed faster-whisper. Empty dict on failure."""
    try:
        from faster_whisper.utils import _MODELS
        return dict(_MODELS)
    except Exception:
        return {}


def _hf_cache_base():
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        return Path(HF_HUB_CACHE)
    except Exception:
        return Path.home() / ".cache" / "huggingface" / "hub"


def _repo_cached(repo: str) -> bool:
    """Is this HF repo present (downloaded) in the local hub cache?"""
    if not repo:
        return False
    folder = _hf_cache_base() / ("models--" + repo.replace("/", "--"))
    snaps = folder / "snapshots"
    if not snaps.exists():
        return False
    for snap in snaps.iterdir():
        if any(snap.glob("*.bin")) or any(snap.glob("*.safetensors")):
            return True
    return False


# ============================================================================
# Backend abstraction
# ============================================================================

class WhisperBackend(ABC):
    """Базовый интерфейс backend'а Whisper."""

    name: str = "abstract"

    @abstractmethod
    def load(self, model_name: str, device: str) -> Any:
        """Загрузить модель в память. Возвращает handle, который потом передаётся в transcribe."""

    @abstractmethod
    def transcribe(
        self,
        model_handle: Any,
        audio_path: str,
        language: Optional[str],
        task: str,
        progress_callback: Optional[Callable] = None,
    ) -> Dict:
        """Транскрибировать. Должен вернуть {text, segments, language}."""

    @abstractmethod
    def supports_batched(self) -> bool:
        """Поддерживает ли backend batched inference для длинных файлов."""

    def unload(self, model_handle: Any) -> None:
        """Освободить ресурсы (опционально)."""
        del model_handle


class OpenAIWhisperBackend(WhisperBackend):
    """Референсный openai-whisper. Используется как fallback."""

    name = "openai"

    def __init__(self):
        import whisper  # lazy
        # whisper.audio кличе ffmpeg на кожен чанк власним `run` — без цього
        # під pythonw.exe блимає консоль на кожні 5 хв аудіо.
        silence_openai_whisper_console_windows()
        self._whisper = whisper

    def load(self, model_name: str, device: str) -> Any:
        return self._whisper.load_model(model_name, device=device)

    def transcribe(self, model_handle, audio_path, language, task, progress_callback=None):
        if progress_callback:
            progress_callback({"status": "processing", "progress": 50, "message": "Transcribing audio..."})

        options = {
            "language": language if language and language != "auto" else None,
            "task": task,
            "fp16": torch.cuda.is_available(),
            "verbose": False,
        }
        result = model_handle.transcribe(audio_path, **options)

        if progress_callback:
            progress_callback({"status": "processing", "progress": 90, "message": "Processing results..."})

        return {
            "text": result.get("text", ""),
            "segments": result.get("segments", []),
            "language": result.get("language", language),
        }

    def supports_batched(self) -> bool:
        return False


class FasterWhisperBackend(WhisperBackend):
    """faster-whisper (CTranslate2). Основной backend для скорости."""

    name = "faster"

    # Маппинг наших коротких имён моделей в имена для faster-whisper.
    # large-v3-turbo поддерживается с faster-whisper >= 1.0.3.
    _MODEL_NAME_MAP = {
        "tiny": "tiny",
        "base": "base",
        "small": "small",
        "medium": "medium",
        "large": "large-v3",
        "large-v3-turbo": "large-v3-turbo",
        "turbo": "large-v3-turbo",
    }

    def __init__(self):
        from faster_whisper import WhisperModel, BatchedInferencePipeline  # lazy
        self._WhisperModel = WhisperModel
        self._BatchedInferencePipeline = BatchedInferencePipeline

    def load(self, model_name: str, device: str) -> Any:
        fw_name = self._MODEL_NAME_MAP.get(model_name, model_name)
        # На GPU используем float16, на CPU — int8 (быстро и достаточно качественно).
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info(f"[faster-whisper] Loading {fw_name} on {device} (compute_type={compute_type})")
        model = self._WhisperModel(fw_name, device=device, compute_type=compute_type)
        return model

    def transcribe(self, model_handle, audio_path, language, task, progress_callback=None):
        if progress_callback:
            progress_callback({"status": "processing", "progress": 5, "message": "Starting faster-whisper..."})

        # faster-whisper API:
        #   model.transcribe(...) -> (segments_generator, info)
        # segments — это generator, считается ленивенько; для прогресса нам нужно его потреблять.
        # Silero VAD через onnxruntime (bundled с faster-whisper) убирает паузы и тишину
        # перед инференсом — меньше галлюцинаций (фантомных фраз в тишине), на ~5-10%
        # быстрее, плюс точнее границы сегментов. (Phase 4.1)
        segments_iter, info = model_handle.transcribe(
            audio_path,
            language=language if language and language != "auto" else None,
            task=task,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=5,
            word_timestamps=False,
        )

        # info.duration — общая длительность файла в секундах.
        total_duration = max(getattr(info, "duration", 0.0) or 0.0, 0.001)
        detected_language = getattr(info, "language", language) or language

        all_segments = []
        all_text_parts = []
        last_progress_report = 0.0
        report_step = 2.0  # обновляем прогресс не чаще, чем раз в 2 секунды реального аудио-времени

        for seg in segments_iter:
            seg_dict = {
                "id": seg.id,
                "start": float(seg.start),
                "end": float(seg.end),
                "text": seg.text,
            }
            all_segments.append(seg_dict)
            all_text_parts.append(seg.text)

            # Streaming: пушим каждый сегмент сразу — фронт видит текст растущим (Phase 2)
            if progress_callback:
                pct = min(95, int(5 + (seg.end / total_duration) * 90))
                progress_callback({
                    "status": "processing",
                    "progress": pct,
                    "message": f"Transcribing... {seg.end:.0f}s / {total_duration:.0f}s",
                    "segment": seg_dict,
                })
                last_progress_report = seg.end

        if progress_callback:
            progress_callback({"status": "processing", "progress": 95, "message": "Finalizing..."})

        return {
            "text": "".join(all_text_parts).strip(),
            "segments": all_segments,
            "language": detected_language,
        }

    def transcribe_batched(self, model_handle, audio_path, language, task, batch_size=8, progress_callback=None):
        """Batched inference для длинных файлов через BatchedInferencePipeline."""
        if progress_callback:
            progress_callback({"status": "processing", "progress": 5, "message": "Starting batched transcription..."})

        pipeline = self._BatchedInferencePipeline(model=model_handle)
        segments_iter, info = pipeline.transcribe(
            audio_path,
            language=language if language and language != "auto" else None,
            task=task,
            batch_size=batch_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=5,
            word_timestamps=False,
        )

        total_duration = max(getattr(info, "duration", 0.0) or 0.0, 0.001)
        detected_language = getattr(info, "language", language) or language

        all_segments = []
        all_text_parts = []

        for seg in segments_iter:
            seg_dict = {
                "id": seg.id,
                "start": float(seg.start),
                "end": float(seg.end),
                "text": seg.text,
            }
            all_segments.append(seg_dict)
            all_text_parts.append(seg.text)

            # Streaming сегментов в реальном времени (Phase 2)
            if progress_callback:
                pct = min(95, int(5 + (seg.end / total_duration) * 90))
                progress_callback({
                    "status": "processing",
                    "progress": pct,
                    "message": f"Batched transcription... {seg.end:.0f}s / {total_duration:.0f}s",
                    "segment": seg_dict,
                })

        if progress_callback:
            progress_callback({"status": "processing", "progress": 95, "message": "Finalizing..."})

        return {
            "text": "".join(all_text_parts).strip(),
            "segments": all_segments,
            "language": detected_language,
        }

    def supports_batched(self) -> bool:
        return True


def _select_default_backend() -> str:
    """Выбирает backend по умолчанию: env > наличие faster-whisper > openai."""
    explicit = os.environ.get("WHISPER_BACKEND", "").strip().lower()
    if explicit in ("faster", "openai"):
        return explicit
    # Автодетект
    try:
        import faster_whisper  # noqa: F401
        return "faster"
    except ImportError:
        return "openai"


def _create_backend(name: str) -> WhisperBackend:
    if name == "faster":
        try:
            return FasterWhisperBackend()
        except ImportError as e:
            logger.warning(f"faster-whisper not available ({e}), falling back to openai-whisper")
            return OpenAIWhisperBackend()
    return OpenAIWhisperBackend()


# ============================================================================
# Modern manager
# ============================================================================

class ModernWhisperManager:
    """Менеджер моделей Whisper с pluggable backend и параллельной транскрипцией.

    Threading model:
    - load_model() сериализован через _load_lock — только одна загрузка в момент.
    - transcribe_with_progress() идёт под _inference_semaphore.

    ВАЖНО про параллельность: faster-whisper `WhisperModel.transcribe()` НЕ
    потокобезопасен для одного instance модели — два потока, дёргающих один и
    тот же handle, дают либо garbled-вывод, либо нативный краш процесса
    (0xC0000409). У нас это реальный путь: live-preview поднимает ОТДЕЛЬНЫЙ
    worker-поток на каждый stream (mic + system, см. recording.py), и оба
    транскрибируют через один shared handle 'small'. Поэтому default semaphore = 1
    (сериализуем инференс). Поднимать WHISPER_MAX_PARALLEL>1 безопасно ТОЛЬКО
    если у каждого потока свой instance модели (сейчас не так — кеш shared).
    Ref: https://github.com/SYSTRAN/faster-whisper/discussions/406
    """

    def __init__(self, force_cpu: bool = False, backend: Optional[str] = None):
        # Сериализация load_model
        self._load_lock = threading.RLock()

        # Сериализация transcribe. Default 1 — faster-whisper не потокобезопасен
        # на shared model handle (см. docstring класса). Не ставь >1 без
        # per-thread instances модели.
        max_parallel = max(1, int(os.environ.get("WHISPER_MAX_PARALLEL", "1")))
        self._inference_semaphore = threading.Semaphore(max_parallel)

        # Backend
        self._backend_name = (backend or _select_default_backend()).lower()
        self._backend: WhisperBackend = _create_backend(self._backend_name)
        logger.info(f"WhisperManager backend: {self._backend.name}")

        # Кеш загруженных моделей (веса в RAM/VRAM).
        #
        # Раньше держали ровно ОДНУ модель: каждый swap = del CUDA-модели +
        # cuda.empty_cache() + load новой. В сценарии record → live-transcribe('small')
        # → full-transcribe('medium') это эвиктило модели туда-сюда на каждой операции,
        # и редкий del/reload CTranslate2-модели на CUDA после долгой сессии иногда
        # ронял процесс нативно (exit 0xC0000409). Теперь — LRU-кеш на несколько
        # моделей: 'small' (live) и 'medium' (full) спокойно сосуществуют, swap'а
        # в обычном флоу нет вообще. На 24GB VRAM это с запасом.
        self.models_dir = Path.home() / ".cache" / "whisper"
        self._cache_size = max(1, int(os.environ.get("WHISPER_MODEL_CACHE_SIZE", "3")))
        self._models: "OrderedDict[str, Any]" = OrderedDict()  # name -> handle, LRU (старые слева)
        # Для обратной совместимости — указывают на последнюю использованную модель.
        self.current_model: Optional[Any] = None
        self.current_model_name: Optional[str] = None

        # Информация о моделях (для UI)
        self.models_info = {
            "tiny": {"size": "39 MB", "params": "39M", "speed": "~32x", "memory": "~1 GB"},
            "base": {"size": "74 MB", "params": "74M", "speed": "~16x", "memory": "~1 GB"},
            "small": {"size": "244 MB", "params": "244M", "speed": "~6x", "memory": "~2 GB"},
            "medium": {"size": "769 MB", "params": "769M", "speed": "~2x", "memory": "~5 GB"},
            "large": {"size": "1550 MB", "params": "1550M", "speed": "~1x", "memory": "~10 GB"},
            # Доступно только для faster-backend
            "large-v3-turbo": {"size": "809 MB", "params": "809M", "speed": "~4x large", "memory": "~6 GB"},
        }

        self.logger = logger  # для совместимости с предыдущим API

        # Device
        if force_cpu:
            self.device = "cpu"
            logger.info("Whisper device: cpu (forced)")
        else:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"Whisper device: {self.device}")

        if not force_cpu and torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            logger.info(f"GPU: {gpu_name}, Memory: {gpu_memory:.1f} GB")

    # -----------------------------------------------------------
    # Backend introspection
    # -----------------------------------------------------------
    @property
    def backend_name(self) -> str:
        return self._backend.name

    def is_faster_backend(self) -> bool:
        return isinstance(self._backend, FasterWhisperBackend)

    # -----------------------------------------------------------
    # Public API (контракт совместимости с app.py)
    # -----------------------------------------------------------

    def get_available_models(self) -> List[Dict]:
        """Список моделей для UI. Для faster-backend модели лежат в HF cache, не whisper cache."""
        available = []

        if self._backend.name == "faster":
            # ДИНАМІЧНИЙ каталог: читаємо список із faster_whisper._MODELS, тож він
            # автоматично росте з апгрейдом бібліотеки. META дає опис; downloaded
            # визначаємо реально за наявністю репо в HF-кеші (а не "завжди true").
            fw = _hf_models_map()
            order = list(FASTER_MODELS_META.keys())
            for name, repo in fw.items():
                if name in FASTER_MODEL_ALIASES:
                    continue
                info = dict(FASTER_MODELS_META.get(name, {}))
                # Невідомі майбутні моделі: .en та distil-* — англо-фокусні за умовч.
                info.setdefault("multilingual", not (name.endswith(".en") or name.startswith("distil")))
                available.append({
                    "name": name,
                    "repo": repo,
                    "info": info,
                    "downloaded": _repo_cached(repo),
                    "backend": "faster",
                })
            # recommended → multilingual → curated order → name
            available.sort(key=lambda m: (
                0 if m["info"].get("recommended") else 1,
                0 if m["info"].get("multilingual") else 1,
                order.index(m["name"]) if m["name"] in order else 999,
                m["name"],
            ))
            return available

        # OpenAI backend: смотрим в ~/.cache/whisper
        if self.models_dir.exists():
            for model_name in self.models_info.keys():
                # large-v3-turbo не существует в openai-whisper
                if model_name == "large-v3-turbo":
                    continue
                model_file = self._find_model_file(model_name)
                available.append({
                    "name": model_name,
                    "info": self.models_info[model_name],
                    "path": str(model_file) if model_file else None,
                    "downloaded": model_file is not None,
                    "backend": "openai",
                })
        else:
            for model_name in self.models_info.keys():
                if model_name == "large-v3-turbo":
                    continue
                available.append({
                    "name": model_name,
                    "info": self.models_info[model_name],
                    "downloaded": False,
                    "backend": "openai",
                })

        return available

    def _find_model_file(self, model_name: str) -> Optional[Path]:
        """Находит .pt файл для openai-whisper (large -> large-v3.pt и т.д.)."""
        direct = self.models_dir / f"{model_name}.pt"
        if direct.exists():
            return direct
        for pt_file in sorted(self.models_dir.glob(f"{model_name}*.pt"), reverse=True):
            return pt_file
        return None

    def download_model(self, model_name: str) -> bool:
        """Скачать модель. Для faster-backend — просто load (HF Hub скачает сам)."""
        if not self._is_valid_model(model_name):
            logger.error(f"Unknown model: {model_name}")
            return False

        try:
            logger.info(f"Downloading model {model_name} via {self._backend.name}...")
            with self._load_lock:
                # Кратковременная загрузка, чтобы скачать веса; затем выгружаем.
                handle = self._backend.load(model_name, device="cpu")
                del handle
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            logger.info(f"Model {model_name} downloaded successfully")
            return True
        except Exception as e:
            logger.error(f"Model download error: {e}")
            return False

    def _evict_to_fit(self) -> None:
        """Освободить место в кеше под новую модель (LRU-эвикция). Под _load_lock.

        Перед уничтожением CUDA-модели делаем synchronize(); после — gc.collect()
        и empty_cache(). Хотим, чтобы любая незавершённая GPU-работа и питоновские
        циклы ссылок разрулились вокруг нативного деструктора CTranslate2, а не
        пересеклись с ним — это снижает шанс нативного краха процесса при эвикции.
        В обычном флоу (record→live 'small' → full 'medium') эвикции не происходит:
        обе модели спокойно живут в кеше."""
        evicted = False
        while len(self._models) >= self._cache_size:
            old_name = next(iter(self._models))
            old_handle = self._models.pop(old_name)
            logger.info(f"Evicting cached model {old_name} (cache size limit {self._cache_size})")
            if torch.cuda.is_available():
                try:
                    torch.cuda.synchronize()
                except Exception:
                    pass
            try:
                self._backend.unload(old_handle)
            except Exception as e:
                logger.warning(f"Error unloading model {old_name}: {e}")
            del old_handle  # последняя ссылка → деструктор CTranslate2 отрабатывает здесь
            evicted = True
        if evicted:
            gc.collect()
            if torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass

    def _is_valid_model(self, name: str) -> bool:
        """Чи знаємо ми таку модель: curated models_info АБО динамічний
        faster_whisper._MODELS (нові моделі бібліотеки приймаються автоматично)."""
        if name in self.models_info:
            return True
        if self._backend.name == "faster":
            return name in _hf_models_map()
        return False

    def load_model(self, model_name: str):
        """Загрузить модель (потокобезпечно). LRU-кеш на несколько моделей."""
        with self._load_lock:
            try:
                if not isinstance(model_name, str):
                    raise ValueError(f"Model name must be string, got {type(model_name)}")
                if not self._is_valid_model(model_name):
                    raise ValueError(f"Unknown model: {model_name}")

                # Cache hit — обновляем LRU и возвращаем.
                cached = self._models.get(model_name)
                if cached is not None:
                    self._models.move_to_end(model_name)
                    self.current_model = cached
                    self.current_model_name = model_name
                    logger.debug(f"Model {model_name} already loaded (cached)")
                    return cached

                # Проверка памяти
                if hasattr(psutil, "virtual_memory"):
                    available_gb = psutil.virtual_memory().available / (1024 ** 3)
                    required = {"tiny": 1, "base": 1, "small": 2, "medium": 5, "large": 10, "large-v3-turbo": 6}.get(model_name, 10)
                    if available_gb < required:
                        logger.warning(f"Low memory for {model_name}: need {required}GB, have {available_gb:.1f}GB")

                # Освобождаем место под новую модель (LRU-эвикция).
                self._evict_to_fit()

                logger.info(f"Loading model {model_name} via {self._backend.name}...")
                handle = self._backend.load(model_name, self.device)
                self._models[model_name] = handle
                self.current_model = handle
                self.current_model_name = model_name

                if self.device == "cuda":
                    logger.info(f"Model loaded on GPU: {torch.cuda.get_device_name(0)}")
                else:
                    logger.info("Model loaded on CPU")

                return handle

            except torch.cuda.OutOfMemoryError:
                logger.warning(f"GPU OOM loading {model_name}, falling back to CPU")
                self.reset_cuda()
                self.device = "cpu"
                try:
                    handle = self._backend.load(model_name, "cpu")
                    self._models[model_name] = handle
                    self.current_model = handle
                    self.current_model_name = model_name
                    logger.info(f"Model {model_name} loaded on CPU (fallback)")
                    return handle
                except Exception as cpu_err:
                    logger.error(f"CPU fallback also failed: {cpu_err}")
                    self.current_model = None
                    self.current_model_name = None
                    return None

            except Exception as e:
                logger.error(f"Model loading error: {e}")
                self._models.pop(model_name, None)
                self.current_model = None
                self.current_model_name = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                return None

    def transcribe_with_progress(
        self,
        audio_path: str,
        model_name: str = "base",
        language: str = "uk",
        task: str = "transcribe",
        progress_callback: Optional[Callable] = None,
    ) -> Dict:
        """Транскрибация. Параллельная (под semaphore), не держит _load_lock."""
        try:
            if not os.path.exists(audio_path):
                error_msg = f"File not found: {audio_path}"
                logger.error(error_msg)
                return {"error": error_msg}

            # Загрузка модели — короткая критическая секция
            model_handle = self.load_model(model_name)
            if model_handle is None:
                return {"error": f"Failed to load model {model_name}"}

            file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
            duration_seconds = self._get_duration_ffprobe(audio_path)
            logger.info(f"Transcribing {file_size_mb:.1f}MB ({duration_seconds:.1f}s) via {self._backend.name}")

            if progress_callback:
                progress_callback({"status": "processing", "progress": 0, "message": "Starting transcription..."})

            # Сама транскрипция — параллельная (semaphore)
            with self._inference_semaphore:
                # Локальная ссылка: если кто-то параллельно поменяет current_model, наш handle не пропадёт.
                local_handle = model_handle

                start = time.time()
                if self._backend.name == "faster" and duration_seconds > 600 and self._backend.supports_batched():
                    # Длинный файл + faster backend: используем BatchedInferencePipeline.
                    result = self._backend.transcribe_batched(
                        local_handle, audio_path, language, task,
                        batch_size=8, progress_callback=progress_callback,
                    )
                elif self._backend.name == "openai" and duration_seconds > 600:
                    # OpenAI backend на длинных файлах — старая ручная нарезка через pydub.
                    result = self._transcribe_chunked_openai(
                        local_handle, audio_path, language, task, progress_callback,
                    )
                else:
                    result = self._backend.transcribe(
                        local_handle, audio_path, language, task, progress_callback,
                    )
                elapsed = time.time() - start
                logger.info(f"Transcription completed in {elapsed:.1f}s ({duration_seconds / max(elapsed, 0.001):.1f}x realtime)")

            if progress_callback:
                progress_callback({"status": "completed", "progress": 100, "message": "Transcription completed!"})

            formatted = {
                "text": result.get("text", ""),
                "segments": result.get("segments", []),
                "language": result.get("language", language),
                "duration": None,
            }
            if formatted["segments"]:
                formatted["duration"] = formatted["segments"][-1].get("end", 0)

            logger.info(
                f"Transcription done. Duration: {(formatted.get('duration') or 0):.1f}s, "
                f"Segments: {len(formatted['segments'])}"
            )
            return formatted

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Transcription error: {error_msg}", exc_info=True)
            return {"error": error_msg}

    # -----------------------------------------------------------
    # OpenAI-backend chunked fallback (только для длинных файлов на старом backend)
    # -----------------------------------------------------------

    def _transcribe_chunked_openai(self, model_handle, audio_path, language, task, progress_callback=None):
        """Ручная нарезка для openai-whisper. Для faster-whisper это не нужно — он умеет batched сам."""
        chunk_length_ms = 300_000  # 5 мин
        overlap_ms = 5_000  # 5 сек

        with tempfile.TemporaryDirectory(prefix="whisper_chunks_") as temp_dir:
            audio = AudioSegment.from_file(audio_path)
            total_duration = len(audio)
            chunks = []
            start = 0
            chunk_count = 0

            while start < total_duration:
                end = min(start + chunk_length_ms, total_duration)
                chunk = audio[start:end]
                chunk_path = os.path.join(temp_dir, f"chunk_{chunk_count:04d}.wav")
                chunk.export(chunk_path, format="wav")
                chunks.append({"path": chunk_path, "start_time": start / 1000, "end_time": end / 1000})
                start = end - overlap_ms if end < total_duration else end
                chunk_count += 1

            del audio
            gc.collect()
            logger.info(f"Split into {len(chunks)} chunks (openai backend)")

            all_segments = []
            full_text = []
            for i, chunk_info in enumerate(chunks):
                if progress_callback:
                    pct = int((i / len(chunks)) * 100)
                    progress_callback({
                        "status": "processing", "progress": pct,
                        "message": f"Processing chunk {i + 1}/{len(chunks)}...",
                        "current_chunk": i + 1, "total_chunks": len(chunks),
                    })
                try:
                    chunk_result = self._backend.transcribe(
                        model_handle, chunk_info["path"], language, task, progress_callback=None,
                    )
                except Exception as e:
                    logger.warning(f"Chunk {i + 1}/{len(chunks)} failed: {e}, skipping")
                    continue

                chunk_start = chunk_info["start_time"]
                filtered = []
                for seg in chunk_result.get("segments", []):
                    if i > 0 and seg["start"] < (overlap_ms / 1000):
                        continue
                    adj = dict(seg)
                    adj["start"] = seg["start"] + chunk_start
                    adj["end"] = seg["end"] + chunk_start
                    filtered.append(adj)
                all_segments.extend(filtered)
                full_text.append(" ".join(s["text"].strip() for s in filtered))

            return {
                "text": " ".join(t for t in full_text if t),
                "segments": all_segments,
                "language": language,
            }

    # -----------------------------------------------------------
    # Audio metadata
    # -----------------------------------------------------------

    def _get_duration_ffprobe(self, audio_path: str) -> float:
        """Длительность через ffprobe (без загрузки в RAM)."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    audio_path,
                ],
                capture_output=True, text=True, timeout=30,
                creationflags=NO_WINDOW,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
            logger.warning(f"ffprobe failed, falling back to pydub: {e}")
        try:
            audio = AudioSegment.from_file(audio_path)
            duration = len(audio) / 1000
            del audio
            gc.collect()
            return duration
        except Exception as e:
            logger.warning(f"Could not determine duration: {e}")
            return 0

    def get_audio_duration(self, audio_path: str) -> float:
        if not os.path.exists(audio_path):
            logger.error(f"File not found: {audio_path}")
            return 0
        return self._get_duration_ffprobe(audio_path)

    # -----------------------------------------------------------
    # System info / cleanup
    # -----------------------------------------------------------

    def get_system_info(self) -> Dict:
        info = {
            "cuda_available": torch.cuda.is_available(),
            "device": self.device,
            "pytorch_version": torch.__version__,
            "cuda_version": torch.version.cuda if torch.cuda.is_available() else None,
            "cpu_threads": torch.get_num_threads(),
            "backend": self._backend.name,
            "max_parallel_inference": self._inference_semaphore._value if hasattr(self._inference_semaphore, "_value") else None,
            "loaded_models": list(self._models.keys()),
            "model_cache_size": self._cache_size,
        }
        if torch.cuda.is_available():
            info["cuda_device"] = torch.cuda.get_device_name(0)
            info["cuda_memory"] = f"{torch.cuda.get_device_properties(0).total_memory / 1024 ** 3:.1f} GB"
        if hasattr(psutil, "virtual_memory"):
            mem = psutil.virtual_memory()
            info["system_memory"] = f"{mem.total / (1024 ** 3):.1f} GB"
            info["available_memory"] = f"{mem.available / (1024 ** 3):.1f} GB"
            info["memory_percent"] = f"{mem.percent}%"
        return info

    def reset_cuda(self):
        """Force reset CUDA для восстановления после ошибок. Выгружает ВСЕ модели."""
        with self._load_lock:
            try:
                if torch.cuda.is_available():
                    try:
                        torch.cuda.synchronize()
                    except Exception:
                        pass
                for name, handle in list(self._models.items()):
                    try:
                        self._backend.unload(handle)
                    except Exception:
                        pass
                self._models.clear()
                self.current_model = None
                self.current_model_name = None
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                    torch.cuda.reset_peak_memory_stats()
                    logger.info("CUDA state reset successfully")
                    return True
                return False
            except Exception as e:
                logger.error(f"CUDA reset error: {e}")
                return False

    def cleanup_temp_files(self):
        """Чистка временных чанков (если остались от прошлых запусков)."""
        try:
            temp_dir = tempfile.gettempdir()
            for item in os.listdir(temp_dir):
                if item.startswith("whisper_chunks_"):
                    chunk_dir = os.path.join(temp_dir, item)
                    if os.path.isdir(chunk_dir):
                        shutil.rmtree(chunk_dir)
                        logger.info(f"Cleaned up temp directory: {chunk_dir}")
        except Exception as e:
            logger.warning(f"Cleanup warning: {e}")
