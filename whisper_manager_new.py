#!/usr/bin/env python3
"""
ModernWhisperManager — a Whisper model manager with a pluggable backend.

Backends:
- "faster" — faster-whisper (CTranslate2). 3-5x faster on GPU, lower VRAM.
- "openai" — the official openai-whisper (reference implementation, fallback).

API contract preserved: app.py does not change its calls.
Parallelism: load_model is serialized, transcribe runs under a semaphore.
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


def _join_names_within_budget(names, count_tokens: Callable[[str], int], budget: int) -> str:
    """Join names with commas, truncating the list at a name boundary within ``budget`` tokens.

    faster-whisper itself cuts ``hotwords`` to ``max_length // 2 - 1`` tokens from the tail
    in the middle of a name, and ``initial_prompt`` in sequential mode — from the head
    (``previous_tokens[-(max_length // 2 - 1):]``), i.e. it loses the first, most important
    names. So we truncate the list ourselves, by whole names, within the same budget."""
    kept: List[str] = []
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        if count_tokens(", ".join(kept + [name])) > budget:
            break
        kept.append(name)
    return ", ".join(kept)


# ============================================================================
# Backend abstraction
# ============================================================================

class WhisperBackend(ABC):
    """Base interface of a Whisper backend."""

    name: str = "abstract"

    @abstractmethod
    def load(self, model_name: str, device: str) -> Any:
        """Load the model into memory. Returns a handle that is later passed to transcribe."""

    @abstractmethod
    def transcribe(
        self,
        model_handle: Any,
        audio_path: str,
        language: Optional[str],
        task: str,
        progress_callback: Optional[Callable] = None,
        decode_options: Optional[Dict] = None,
    ) -> Dict:
        """Transcribe. Must return {text, segments, language}.

        ``decode_options`` — vocabulary/language (K1, see ``transcribe_with_progress``)."""

    @abstractmethod
    def supports_batched(self) -> bool:
        """Whether the backend supports batched inference for long files."""

    def unload(self, model_handle: Any) -> None:
        """Free resources (optional)."""
        del model_handle


class OpenAIWhisperBackend(WhisperBackend):
    """Reference openai-whisper. Used as a fallback."""

    name = "openai"

    def __init__(self):
        import whisper  # lazy
        # whisper.audio calls ffmpeg for every chunk with its own `run` — without this
        # under pythonw.exe the console flashes every 5 minutes of audio.
        silence_openai_whisper_console_windows()
        self._whisper = whisper

    def load(self, model_name: str, device: str) -> Any:
        return self._whisper.load_model(model_name, device=device)

    def transcribe(self, model_handle, audio_path, language, task, progress_callback=None,
                   decode_options=None):
        if decode_options:
            logger.debug("openai backend ignores decode_options %s", sorted(decode_options))
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
    """faster-whisper (CTranslate2). The main backend for speed."""

    name = "faster"

    # Mapping of our short model names to faster-whisper model names.
    # large-v3-turbo is supported since faster-whisper >= 1.0.3.
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
        # On GPU we use float16, on CPU — int8 (fast and good enough quality).
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info(f"[faster-whisper] Loading {fw_name} on {device} (compute_type={compute_type})")
        model = self._WhisperModel(fw_name, device=device, compute_type=compute_type)
        return model

    @staticmethod
    def _decode_kwargs(model_handle, decode_options: Optional[Dict], batched: bool,
                       language: Optional[str] = None) -> Dict:
        """``decode_options`` → faster-whisper kwargs. Empty options → ``{}``, i.e. the
        call is byte for byte as before K1.

        - ``hotwords`` / ``initial_prompt``: a list of names → a string truncated at the
          name boundary at ``max_length // 2 - 1`` (223) tokens by this model's tokenizer;
        - ``multilingual``: the language is detected once per window (up to 30 s of speech in
          sequential mode, a VAD chunk up to ``chunk_length`` in batched) — only
          when no language is set (``auto``/empty). An explicit language ("uk") is law: in
          faster-whisper multilingual would override it on every window, and it was measured
          only on mixed calls (4.8.1, owner's decision);
        - ``chunk_length``: batched only (in sequential mode it is the feature-extractor
          window length, which we do not touch)."""
        if not decode_options:
            return {}
        kwargs: Dict[str, Any] = {}
        budget = getattr(model_handle, "max_length", 448) // 2 - 1
        tokenizer = getattr(model_handle, "hf_tokenizer", None)
        for key in ("hotwords", "initial_prompt"):
            names = decode_options.get(key)
            if not names:
                continue
            if tokenizer is None:
                logger.warning("[faster-whisper] the model has no tokenizer — %s skipped", key)
                continue
            text = _join_names_within_budget(
                names,
                lambda t: len(tokenizer.encode(" " + t, add_special_tokens=False).ids),
                budget,
            )
            if text:
                kwargs[key] = text
                logger.debug("[faster-whisper] %s: %d of %d names", key, text.count(", ") + 1, len(names))
        if "hotwords" in kwargs and not batched:
            # faster-whisper 1.2.1 get_prompt (transcribe.py:1542-1550) puts hotwords NEXT TO
            # previous_tokens[-223:], not instead of them: from the 3rd window the prompt is 1+222+223+3 = 449 > 448,
            # and CTranslate2 fails with "maximum decoding length must be > 0" — the whole transcription is lost.
            # The batched path itself passes condition_on_previous_text=False (:547); the same here
            # (context reset after each window, :1373-1383) keeps the prompt <= 1+223+3.
            kwargs["condition_on_previous_text"] = False
        if decode_options.get("multilingual") and (not language or language == "auto"):
            kwargs["multilingual"] = True
        if batched and decode_options.get("chunk_length"):
            kwargs["chunk_length"] = int(decode_options["chunk_length"])
        return kwargs

    def transcribe(self, model_handle, audio_path, language, task, progress_callback=None,
                   decode_options=None):
        if progress_callback:
            progress_callback({"status": "processing", "progress": 5, "message": "Starting faster-whisper..."})

        # faster-whisper API:
        #   model.transcribe(...) -> (segments_generator, info)
        # segments is a generator, evaluated lazily; for progress we need to consume it.
        # Silero VAD via onnxruntime (bundled with faster-whisper) removes pauses and silence
        # before inference — fewer hallucinations (phantom phrases in silence), ~5-10%
        # faster, plus more accurate segment boundaries. (Phase 4.1)
        segments_iter, info = model_handle.transcribe(
            audio_path,
            language=language if language and language != "auto" else None,
            task=task,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=5,
            word_timestamps=False,
            **self._decode_kwargs(model_handle, decode_options, batched=False, language=language),
        )

        # info.duration — total file duration in seconds.
        total_duration = max(getattr(info, "duration", 0.0) or 0.0, 0.001)
        detected_language = getattr(info, "language", language) or language

        all_segments = []
        all_text_parts = []
        last_progress_report = 0.0
        report_step = 2.0  # update progress no more often than once per 2 seconds of real audio time

        for seg in segments_iter:
            seg_dict = {
                "id": seg.id,
                "start": float(seg.start),
                "end": float(seg.end),
                "text": seg.text,
            }
            all_segments.append(seg_dict)
            all_text_parts.append(seg.text)

            # Streaming: push each segment right away — the frontend sees the text growing (Phase 2)
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

    def transcribe_batched(self, model_handle, audio_path, language, task, batch_size=8, progress_callback=None,
                           decode_options=None):
        """Batched inference for long files via BatchedInferencePipeline."""
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
            **self._decode_kwargs(model_handle, decode_options, batched=True, language=language),
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

            # Real-time segment streaming (Phase 2)
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
    """Chooses the default backend: env > faster-whisper availability > openai."""
    explicit = os.environ.get("WHISPER_BACKEND", "").strip().lower()
    if explicit in ("faster", "openai"):
        return explicit
    # Autodetect
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
    """Whisper model manager with a pluggable backend and parallel transcription.

    Threading model:
    - load_model() is serialized via _load_lock — only one load at a time.
    - transcribe_with_progress() runs under _inference_semaphore.

    IMPORTANT about parallelism: faster-whisper `WhisperModel.transcribe()` is NOT
    thread-safe for a single model instance — two threads hitting the same
    handle produce either garbled output or a native process crash
    (0xC0000409). For us this is a real path: live preview starts a SEPARATE
    worker thread per stream (mic + system, see recording.py), and both
    transcribe through one shared 'small' handle. So the default semaphore = 1
    (inference is serialized). Raising WHISPER_MAX_PARALLEL>1 is safe ONLY
    if each thread has its own model instance (not the case now — the cache is shared).
    Ref: https://github.com/SYSTRAN/faster-whisper/discussions/406
    """

    def __init__(self, force_cpu: bool = False, backend: Optional[str] = None):
        # Serialization of load_model
        self._load_lock = threading.RLock()

        # Serialization of transcribe. Default 1 — faster-whisper is not thread-safe
        # on a shared model handle (see the class docstring). Do not set >1 without
        # per-thread model instances.
        max_parallel = max(1, int(os.environ.get("WHISPER_MAX_PARALLEL", "1")))
        self._inference_semaphore = threading.Semaphore(max_parallel)

        # Backend
        self._backend_name = (backend or _select_default_backend()).lower()
        self._backend: WhisperBackend = _create_backend(self._backend_name)
        logger.info(f"WhisperManager backend: {self._backend.name}")

        # Cache of loaded models (weights in RAM/VRAM).
        #
        # Previously exactly ONE model was kept: every swap = del CUDA model +
        # cuda.empty_cache() + load the new one. In the record → live-transcribe('small')
        # → full-transcribe('medium') scenario this evicted models back and forth on every operation,
        # and a frequent del/reload of a CTranslate2 model on CUDA after a long session sometimes
        # crashed the process natively (exit 0xC0000409). Now — an LRU cache for several
        # models: 'small' (live) and 'medium' (full) coexist peacefully, there is no swap
        # in the normal flow at all. On 24GB VRAM this is well within limits.
        self.models_dir = Path.home() / ".cache" / "whisper"
        self._cache_size = max(1, int(os.environ.get("WHISPER_MODEL_CACHE_SIZE", "3")))
        self._models: "OrderedDict[str, Any]" = OrderedDict()  # name -> handle, LRU (oldest on the left)
        # For backward compatibility — they point to the last used model.
        self.current_model: Optional[Any] = None
        self.current_model_name: Optional[str] = None

        # Model information (for the UI)
        self.models_info = {
            "tiny": {"size": "39 MB", "params": "39M", "speed": "~32x", "memory": "~1 GB"},
            "base": {"size": "74 MB", "params": "74M", "speed": "~16x", "memory": "~1 GB"},
            "small": {"size": "244 MB", "params": "244M", "speed": "~6x", "memory": "~2 GB"},
            "medium": {"size": "769 MB", "params": "769M", "speed": "~2x", "memory": "~5 GB"},
            "large": {"size": "1550 MB", "params": "1550M", "speed": "~1x", "memory": "~10 GB"},
            # Available only for the faster backend
            "large-v3-turbo": {"size": "809 MB", "params": "809M", "speed": "~4x large", "memory": "~6 GB"},
        }

        self.logger = logger  # for compatibility with the previous API

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
    # Public API (compatibility contract with app.py)
    # -----------------------------------------------------------

    def get_available_models(self) -> List[Dict]:
        """List of models for the UI. For the faster backend models live in the HF cache, not the whisper cache."""
        available = []

        if self._backend.name == "faster":
            # DYNAMIC catalog: we read the list from faster_whisper._MODELS, so it
            # grows automatically with a library upgrade. META gives the description; downloaded
            # is determined for real by the presence of the repo in the HF cache (not "always true").
            fw = _hf_models_map()
            order = list(FASTER_MODELS_META.keys())
            for name, repo in fw.items():
                if name in FASTER_MODEL_ALIASES:
                    continue
                info = dict(FASTER_MODELS_META.get(name, {}))
                # Unknown future models: .en and distil-* are English-focused by default.
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

        # OpenAI backend: look in ~/.cache/whisper
        if self.models_dir.exists():
            for model_name in self.models_info.keys():
                # large-v3-turbo does not exist in openai-whisper
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
        """Finds the .pt file for openai-whisper (large -> large-v3.pt etc.)."""
        direct = self.models_dir / f"{model_name}.pt"
        if direct.exists():
            return direct
        for pt_file in sorted(self.models_dir.glob(f"{model_name}*.pt"), reverse=True):
            return pt_file
        return None

    def download_model(self, model_name: str) -> bool:
        """Download a model. For the faster backend — just load (HF Hub downloads it itself)."""
        if not self._is_valid_model(model_name):
            logger.error(f"Unknown model: {model_name}")
            return False

        try:
            logger.info(f"Downloading model {model_name} via {self._backend.name}...")
            with self._load_lock:
                # A brief load to download the weights; then we unload.
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
        """Free space in the cache for a new model (LRU eviction). Under _load_lock.

        Before destroying a CUDA model we call synchronize(); afterwards — gc.collect()
        and empty_cache(). We want any unfinished GPU work and Python
        reference cycles to resolve around the native CTranslate2 destructor, rather than
        intersect with it — this lowers the chance of a native process crash on eviction.
        In the normal flow (record→live 'small' → full 'medium') no eviction happens:
        both models live peacefully in the cache."""
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
            del old_handle  # last reference → the CTranslate2 destructor runs here
            evicted = True
        if evicted:
            gc.collect()
            if torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass

    def _is_valid_model(self, name: str) -> bool:
        """Whether we know this model: curated models_info OR the dynamic
        faster_whisper._MODELS (new library models are accepted automatically)."""
        if name in self.models_info:
            return True
        if self._backend.name == "faster":
            return name in _hf_models_map()
        return False

    def load_model(self, model_name: str):
        """Load a model (thread-safe). LRU cache of several models."""
        with self._load_lock:
            try:
                if not isinstance(model_name, str):
                    raise ValueError(f"Model name must be string, got {type(model_name)}")
                if not self._is_valid_model(model_name):
                    raise ValueError(f"Unknown model: {model_name}")

                # Cache hit — update the LRU and return.
                cached = self._models.get(model_name)
                if cached is not None:
                    self._models.move_to_end(model_name)
                    self.current_model = cached
                    self.current_model_name = model_name
                    logger.debug(f"Model {model_name} already loaded (cached)")
                    return cached

                # Memory check
                if hasattr(psutil, "virtual_memory"):
                    available_gb = psutil.virtual_memory().available / (1024 ** 3)
                    required = {"tiny": 1, "base": 1, "small": 2, "medium": 5, "large": 10, "large-v3-turbo": 6}.get(model_name, 10)
                    if available_gb < required:
                        logger.warning(f"Low memory for {model_name}: need {required}GB, have {available_gb:.1f}GB")

                # Free space for the new model (LRU eviction).
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
        decode_options: Optional[Dict] = None,
    ) -> Dict:
        """Transcription. Parallel (under the semaphore), does not hold _load_lock.

        ``decode_options`` (K1, ``app.services.vocabulary.decode_options_for``):
        ``hotwords`` / ``initial_prompt`` — a list of names (the manager joins and
        truncates it at a name boundary to 223 tokens itself), ``multilingual: bool``,
        ``chunk_length: int`` (batched only). ``None``/``{}`` — as before K1.
        The openai backend ignores the options."""
        try:
            if not os.path.exists(audio_path):
                error_msg = f"File not found: {audio_path}"
                logger.error(error_msg)
                return {"error": error_msg}

            # Model load — a short critical section
            model_handle = self.load_model(model_name)
            if model_handle is None:
                return {"error": f"Failed to load model {model_name}"}

            file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
            duration_seconds = self._get_duration_ffprobe(audio_path)
            logger.info(f"Transcribing {file_size_mb:.1f}MB ({duration_seconds:.1f}s) via {self._backend.name}")

            if progress_callback:
                progress_callback({"status": "processing", "progress": 0, "message": "Starting transcription..."})

            # The transcription itself — parallel (semaphore)
            with self._inference_semaphore:
                # Local reference: if someone changes current_model in parallel, our handle will not vanish.
                local_handle = model_handle

                start = time.time()
                if self._backend.name == "faster" and duration_seconds > 600 and self._backend.supports_batched():
                    # Long file + faster backend: use BatchedInferencePipeline.
                    result = self._backend.transcribe_batched(
                        local_handle, audio_path, language, task,
                        batch_size=8, progress_callback=progress_callback,
                        decode_options=decode_options,
                    )
                elif self._backend.name == "openai" and duration_seconds > 600:
                    # OpenAI backend on long files — the old manual slicing via pydub.
                    if decode_options:
                        logger.debug("openai backend ignores decode_options %s", sorted(decode_options))
                    result = self._transcribe_chunked_openai(
                        local_handle, audio_path, language, task, progress_callback,
                    )
                else:
                    result = self._backend.transcribe(
                        local_handle, audio_path, language, task, progress_callback,
                        decode_options=decode_options,
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
    # OpenAI backend chunked fallback (only for long files on the old backend)
    # -----------------------------------------------------------

    def _transcribe_chunked_openai(self, model_handle, audio_path, language, task, progress_callback=None):
        """Manual slicing for openai-whisper. Not needed for faster-whisper — it does batched itself."""
        chunk_length_ms = 300_000  # 5 min
        overlap_ms = 5_000  # 5 sec

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
        """Duration via ffprobe (without loading into RAM)."""
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
        """Force reset CUDA to recover from errors. Unloads ALL models."""
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
        """Clean up temporary chunks (if left over from previous runs)."""
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
