"""Unit tests of ModernWhisperManager (whisper_manager_new.py) — T7.5.

Offline, WITHOUT real inference/GPU: the instance._backend is replaced by a fake
(the WhisperBackend protocol: load/unload/transcribe/supports_batched), so
no real model is pulled in or loaded onto GPU/CPU. Covers:
  - The model LRU cache (_evict_to_fit): eviction of the oldest, a cache hit is not
    counted as a new load, a touch on a cache hit moves it to the MRU end.
  - Validation of an unknown model (without network requests to HF).
  - The inference semaphore (_inference_semaphore): the default serializes parallel
    transcribe_with_progress; WHISPER_MAX_PARALLEL=N allows N at once.

torch is in the venv (CUDA is available on this machine), but the test forces force_cpu=True
and runs NOTHING real on the GPU — all "inference" goes through the fake backend.
"""
from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import pytest

from whisper_manager_new import FasterWhisperBackend, ModernWhisperManager


# ============================================================
# Fake backend (the WhisperBackend protocol, without a real model)
# ============================================================

class FakeBackend:
    """Minimal fake backend: does not touch the GPU/model file system."""

    name = "fake"

    def __init__(self, transcribe_fn=None):
        self.load_calls: list[str] = []
        self.unload_calls: list[str] = []
        self._transcribe_fn = transcribe_fn

    def load(self, model_name, device):
        self.load_calls.append(model_name)
        return f"handle:{model_name}"

    def unload(self, handle):
        self.unload_calls.append(handle)

    def supports_batched(self):
        return False

    def transcribe(self, model_handle, audio_path, language, task, progress_callback=None,
                   decode_options=None):
        if self._transcribe_fn:
            return self._transcribe_fn(model_handle, audio_path, language, task, progress_callback)
        return {"text": "ok", "segments": [], "language": language}


@pytest.fixture()
def manager():
    """force_cpu=True — avoid any real CUDA detection on paths
    we are not interested in; _backend is replaced by the fake right away (the LRU/semaphore logic
    in ModernWhisperManager does not depend on a specific backend)."""
    m = ModernWhisperManager(force_cpu=True)
    m._backend = FakeBackend()
    return m


# ============================================================
# Model LRU cache
# ============================================================

def test_load_model_caches_and_does_not_reload_on_cache_hit(manager):
    h1 = manager.load_model("tiny")
    h2 = manager.load_model("tiny")
    assert h1 == h2 == "handle:tiny"
    assert manager._backend.load_calls == ["tiny"]  # only 1 real load
    assert manager.current_model_name == "tiny"


def test_load_model_lru_evicts_least_recently_used(manager):
    manager._cache_size = 2
    manager.load_model("tiny")
    manager.load_model("small")
    manager.load_model("medium")  # must evict "tiny" (the oldest)

    assert "tiny" not in manager._models
    assert set(manager._models.keys()) == {"small", "medium"}
    assert manager._backend.unload_calls == ["handle:tiny"]


def test_load_model_cache_hit_touches_lru_order(manager):
    manager._cache_size = 2
    manager.load_model("tiny")
    manager.load_model("small")
    manager.load_model("tiny")   # cache hit → "tiny" moves to the MRU end
    manager.load_model("medium")  # now "small" must be evicted, NOT "tiny"

    assert "tiny" in manager._models
    assert "small" not in manager._models
    assert manager._backend.unload_calls == ["handle:small"]


def test_load_model_no_eviction_while_under_cache_size(manager):
    manager._cache_size = 5
    for name in ("tiny", "small", "medium"):
        manager.load_model(name)
    assert set(manager._models.keys()) == {"tiny", "small", "medium"}
    assert manager._backend.unload_calls == []


def test_load_model_unknown_model_returns_none_without_backend_call(manager):
    result = manager.load_model("no-such-model-xyz")
    assert result is None
    assert manager._backend.load_calls == []  # an invalid name is filtered out BEFORE the backend call
    assert manager.current_model_name is None


def test_load_model_rejects_non_string_name(manager):
    assert manager.load_model(123) is None
    assert manager._backend.load_calls == []


# ============================================================
# Inference semaphore
# ============================================================

def _make_manager_with_env(monkeypatch, max_parallel, transcribe_fn):
    if max_parallel is None:
        monkeypatch.delenv("WHISPER_MAX_PARALLEL", raising=False)
    else:
        monkeypatch.setenv("WHISPER_MAX_PARALLEL", str(max_parallel))
    m = ModernWhisperManager(force_cpu=True)
    m._backend = FakeBackend(transcribe_fn=transcribe_fn)
    m._get_duration_ffprobe = lambda audio_path: 1.0  # avoid a real ffprobe/pydub
    return m


def test_default_semaphore_is_one(monkeypatch):
    monkeypatch.delenv("WHISPER_MAX_PARALLEL", raising=False)
    m = ModernWhisperManager(force_cpu=True)
    assert m._inference_semaphore._value == 1


def test_semaphore_env_override_sets_value(monkeypatch):
    monkeypatch.setenv("WHISPER_MAX_PARALLEL", "4")
    m = ModernWhisperManager(force_cpu=True)
    assert m._inference_semaphore._value == 4


def test_semaphore_serializes_concurrent_transcriptions_by_default(monkeypatch, tmp_path: Path):
    """WHISPER_MAX_PARALLEL=1 (default): two parallel transcribe_with_progress
    calls must NEVER run backend.transcribe at the same time."""
    lock = threading.Lock()
    active = 0
    max_active_seen = 0

    def transcribe_fn(handle, audio_path, language, task, progress_callback=None):
        nonlocal active, max_active_seen
        with lock:
            active += 1
            max_active_seen = max(max_active_seen, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return {"text": "ok", "segments": [], "language": language}

    m = _make_manager_with_env(monkeypatch, None, transcribe_fn)
    audio1 = tmp_path / "a.wav"
    audio2 = tmp_path / "b.wav"
    audio1.write_bytes(b"\x00")
    audio2.write_bytes(b"\x00")

    results = {}

    def run(key, path):
        results[key] = m.transcribe_with_progress(str(path), model_name="tiny", language="uk")

    t1 = threading.Thread(target=run, args=("a", audio1))
    t2 = threading.Thread(target=run, args=("b", audio2))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert max_active_seen == 1, "semaphore=1 should have fully serialized both calls"
    assert "error" not in results["a"] and "error" not in results["b"]


def test_semaphore_env_override_allows_true_concurrency(monkeypatch, tmp_path: Path):
    """WHISPER_MAX_PARALLEL=2: allows EXACTLY that many concurrent transcribes.
    We prove it deterministically via a Barrier — if the semaphore regresses to 1, both
    threads get stuck on the barrier and transcribe returns an error (the test fails)."""
    barrier = threading.Barrier(2, timeout=3)

    def transcribe_fn(handle, audio_path, language, task, progress_callback=None):
        barrier.wait()  # both threads must get here AT THE SAME TIME
        return {"text": "ok", "segments": [], "language": language}

    m = _make_manager_with_env(monkeypatch, 2, transcribe_fn)
    audio1 = tmp_path / "a.wav"
    audio2 = tmp_path / "b.wav"
    audio1.write_bytes(b"\x00")
    audio2.write_bytes(b"\x00")

    results = {}

    def run(key, path):
        results[key] = m.transcribe_with_progress(str(path), model_name="tiny", language="uk")

    t1 = threading.Thread(target=run, args=("a", audio1))
    t2 = threading.Thread(target=run, args=("b", audio2))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results["a"].get("text") == "ok", results["a"]
    assert results["b"].get("text") == "ok", results["b"]


# ============================================================
# reset_cuda — a measured smoke test (must not fail without CUDA models in the cache)
# ============================================================

def test_reset_cuda_clears_model_cache(manager):
    manager.load_model("tiny")
    assert manager._models
    manager.reset_cuda()
    assert manager._models == {}
    assert manager.current_model is None
    assert manager.current_model_name is None


# ============================================================
# K1: decode_options → faster-whisper (sequential and batched call)
# ============================================================

BUDGET = 448 // 2 - 1  # max_length // 2 - 1 — the hotwords/prompt limit in faster-whisper


class _FakeTokenizer:
    """1 token per word: "Імʼя1, Імʼя2" → 2 tokens."""

    def encode(self, text, add_special_tokens=False):
        return types.SimpleNamespace(ids=[0] * len(text.split()))


class _FakeWhisperModel:
    max_length = 448

    def __init__(self):
        self.hf_tokenizer = _FakeTokenizer()
        self.calls: list[dict] = []

    def transcribe(self, audio_path, **kwargs):
        self.calls.append(kwargs)
        return iter([]), types.SimpleNamespace(duration=1.0, language="uk")


class _FakePipeline:
    calls: list[dict] = []

    def __init__(self, model):
        self.model = model

    def transcribe(self, audio_path, **kwargs):
        _FakePipeline.calls.append(kwargs)
        return iter([]), types.SimpleNamespace(duration=1.0, language="uk")


@pytest.fixture()
def faster(tmp_path: Path):
    """The real FasterWhisperBackend with a fake model (without GPU and weights)."""
    backend = FasterWhisperBackend()
    backend._BatchedInferencePipeline = _FakePipeline
    _FakePipeline.calls = []
    m = ModernWhisperManager(force_cpu=True)
    m._backend = backend
    model = _FakeWhisperModel()
    m._models["tiny"] = model
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00")

    def run(duration, decode_options=None, language="uk", **kw):
        m._get_duration_ffprobe = lambda _p: duration
        args = {"decode_options": decode_options} if decode_options is not None else {}
        result = m.transcribe_with_progress(str(audio), model_name="tiny", language=language, **args, **kw)
        assert "error" not in result, result
        return (_FakePipeline.calls if duration > 600 else model.calls)[-1]

    return run


SEQ, BATCHED = 5.0, 700.0


@pytest.mark.parametrize("duration", [SEQ, BATCHED])
@pytest.mark.parametrize("mode", ["hotwords", "initial_prompt"])
def test_vocabulary_reaches_both_call_modes(faster, duration, mode):
    other = {"hotwords": "initial_prompt", "initial_prompt": "hotwords"}[mode]
    kwargs = faster(duration, {mode: ["Acmecorp", "Адам", "Робота Fund"]})
    assert kwargs[mode] == "Acmecorp, Адам, Робота Fund"
    assert other not in kwargs


@pytest.mark.parametrize("duration", [SEQ, BATCHED])
def test_names_cut_at_name_boundary_within_budget(faster, duration):
    names = [f"Імʼя{i}" for i in range(300)]
    text = faster(duration, {"hotwords": names})["hotwords"]
    assert len(_FakeTokenizer().encode(" " + text).ids) <= BUDGET
    kept = text.split(", ")
    assert kept == names[:len(kept)], "only whole names, rank order preserved"
    assert len(kept) == BUDGET


def test_multilingual_and_chunk_length_reach_batched(faster):
    kwargs = faster(BATCHED, {"multilingual": True, "chunk_length": 10}, language="auto")
    assert kwargs["multilingual"] is True
    assert kwargs["chunk_length"] == 10


def test_chunk_length_never_reaches_sequential(faster):
    kwargs = faster(SEQ, {"multilingual": True, "chunk_length": 10}, language="auto")
    assert kwargs["multilingual"] is True
    assert "chunk_length" not in kwargs


@pytest.mark.parametrize("duration", [SEQ, BATCHED])
@pytest.mark.parametrize("language", ["uk", "ru", "en"])
def test_explicit_language_is_never_overridden_by_multilingual(faster, duration, language):
    """4.8.1, owner's decision: "uk" means uk. multilingual in faster-whisper overrides the given
    language on every window, and it was measured only on mixed calls — so only for "auto"."""
    kwargs = faster(duration, {"multilingual": True}, language=language)
    assert "multilingual" not in kwargs
    assert kwargs["language"] == language


@pytest.mark.parametrize("language", ["auto", ""])
def test_multilingual_applies_when_language_not_chosen(faster, language):
    assert faster(SEQ, {"multilingual": True}, language=language)["multilingual"] is True


BASE_SEQ = {
    "language": "uk", "task": "transcribe", "vad_filter": True,
    "vad_parameters": {"min_silence_duration_ms": 500}, "beam_size": 5, "word_timestamps": False,
}


@pytest.mark.parametrize("decode_options", [None, {}])
def test_without_decode_options_call_is_unchanged(faster, decode_options):
    assert faster(SEQ, decode_options) == BASE_SEQ
    assert faster(BATCHED, decode_options) == {**BASE_SEQ, "batch_size": 8}


# ============================================================
# Repair round 1: hotwords prompt + previous context
# ============================================================

class _PromptTokenizer:
    """The minimum that the real faster_whisper get_prompt reads."""

    sot_prev = 1
    sot_sequence = (2, 3, 4)
    no_timestamps = 5
    timestamp_begin = 6

    def encode(self, text):
        return [0] * len(text.split())


def _prompt_len(kwargs, previous_tokens):
    from faster_whisper.transcribe import WhisperModel
    fake_self = types.SimpleNamespace(max_length=448)
    return len(WhisperModel.get_prompt(fake_self, _PromptTokenizer(), previous_tokens,
                                       hotwords=kwargs.get("hotwords")))


def test_sequential_hotwords_never_overflow_decoding_length(faster):
    """The full vocabulary (223 tokens) + 223 tokens of previous text is 449 > 448, CTranslate2
    fails and the transcription is lost. The sequential path with hotwords does not pull in previous text."""
    kwargs = faster(SEQ, {"hotwords": [f"Імʼя{i}" for i in range(300)]})
    assert kwargs["condition_on_previous_text"] is False
    previous = [] if kwargs["condition_on_previous_text"] is False else [0] * 223
    assert _prompt_len(kwargs, previous) <= 448 - 200, "budget is left for the window text"
    assert _prompt_len(kwargs, [0] * 223) > 448, "without a context reset the prompt overflows"


def test_batched_and_initial_prompt_keep_previous_text_default(faster):
    assert "condition_on_previous_text" not in faster(BATCHED, {"hotwords": ["Acmecorp"]})
    assert "condition_on_previous_text" not in faster(SEQ, {"initial_prompt": ["Acmecorp"]})
