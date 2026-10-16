"""Integration tests for DiarizationService with the REAL pyannote.audio.

These tests LOAD the model (~1.5GB on first run, then cached)
and run real inference - slow (10-30s).

Run:
    .venv/Scripts/python.exe -m pytest tests/test_diarization_integration.py -m slow

Or all tests including slow:
    .venv/Scripts/python.exe -m pytest -m ""

Skip-condition (auto):
- pyannote.audio is not installed
- HF_TOKEN is missing from the env
- The pyannote/* license is not accepted (a 401 on load)

Strategy:
- Synthetic audio fixture via numpy + wave (no external dependencies).
  It is not real speech - pyannote may return 0 segments - but
  loading the pipeline and inference must not crash.
- If tests/fixtures/diarization/<name>.wav holds REAL speech -
  test_real_speech_detects_speakers (optional) checks that 1+ speaker is
  found. The user can put their own file there for richer coverage.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest


pytestmark = pytest.mark.slow


# ---------------------------------------------------------------- skip helpers

def _skip_if_unavailable():
    """Skip all tests if pyannote / the token are unavailable."""
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        pytest.skip('pyannote.audio not installed')

    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get('HF_TOKEN'):
        pytest.skip('HF_TOKEN not set — cannot load gated pyannote models')


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope='module')
def synthetic_speech_wav(tmp_path_factory):
    """Creates a short synthetic WAV without additional dependencies.

    Generates a 5s mono 16kHz signal with:
    - Voiced frequencies (formants) so it looks speech-like
    - An envelope at ~4Hz (syllable rate)
    - Silence in the middle (test of VAD-shift detection)

    Not real speech - pyannote may return 0 segments.
    The test just checks that the pipeline does not crash.
    """
    import math
    import struct

    sr = 16000
    duration = 5.0
    n_samples = int(sr * duration)

    samples = []
    for i in range(n_samples):
        t = i / sr
        # Silence 2.0–3.0s
        if 2.0 <= t < 3.0:
            samples.append(0)
            continue
        # The "speech-like" component
        envelope = (math.sin(2 * math.pi * 4 * t)) ** 2  # 4Hz syllable rate
        s = (
            math.sin(2 * math.pi * 120 * t) * 0.3 +
            math.sin(2 * math.pi * 800 * t) * 0.15 +
            math.sin(2 * math.pi * 1200 * t) * 0.1
        ) * envelope
        samples.append(int(max(-1.0, min(1.0, s)) * 32767))

    out = tmp_path_factory.mktemp('audio') / 'synthetic.wav'
    with wave.open(str(out), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b''.join(struct.pack('<h', s) for s in samples))
    return out


@pytest.fixture
def real_speech_wav():
    """Optional fixture - real speech for richer coverage.

    The user can put a WAV or MP3 at tests/fixtures/diarization/sample.wav.
    If the file is absent - the test is skipped.
    """
    path = Path(__file__).parent / 'fixtures' / 'diarization' / 'sample.wav'
    if not path.is_file():
        pytest.skip(f'No real speech fixture at {path} — drop a multi-speaker WAV/MP3 here for richer coverage')
    return path


@pytest.fixture
def fresh_service():
    """Reset the singleton between tests so that state does not leak."""
    from app.services.diarization_service import DiarizationService
    DiarizationService._instance = None
    yield
    DiarizationService._instance = None


# ---------------------------------------------------------------- pipeline load

def test_pipeline_loads_with_token(fresh_service):
    """The first ensure_pipeline call loads the model from the HF cache."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    assert svc.is_available(), svc.unavailability_reason()
    pipeline = svc._ensure_pipeline()
    assert pipeline is not None


def test_pipeline_singleton(fresh_service):
    """Repeated get_instance() calls return the same object."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    a = DiarizationService.get_instance()
    b = DiarizationService.get_instance()
    assert a is b


# ---------------------------------------------------------------- inference

def test_diarize_synthetic_doesnt_crash(synthetic_speech_wav, fresh_service):
    """pyannote must process synthetic audio without exceptions.

    It may return 0 segments (the synthetic is not speech-like enough),
    the main thing is that it does not crash and returns a DiarizationResult.
    """
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService, DiarizationResult

    svc = DiarizationService.get_instance()
    result = svc.diarize_audio(synthetic_speech_wav)

    assert isinstance(result, DiarizationResult)
    assert result.processing_time_sec > 0
    # All segments are inside the file duration
    for seg in result.segments:
        assert 0 <= seg.start < seg.end <= 6.0  # 5s + slack
        assert seg.speaker.startswith('SPEAKER_')


def test_diarize_recording_per_stream(synthetic_speech_wav, fresh_service, tmp_path):
    """diarize_recording with mic.wav → all labels are self; system.wav → SPEAKER_NN."""
    _skip_if_unavailable()
    from app.services.diarization_service import (
        DiarizationService,
        SELF_LABEL,
    )

    # Reuse the synthetic as mic + a copy as system (different paths, the same content)
    mic = synthetic_speech_wav
    system = tmp_path / 'system.wav'
    system.write_bytes(mic.read_bytes())

    svc = DiarizationService.get_instance()
    result = svc.diarize_recording(mic_wav=mic, system_wav=system)

    # If the synthetic generates 0 segments - the test passes as a no-op.
    # If there are segments - they must be either self (from mic) or SPEAKER_NN (from system).
    for seg in result.segments:
        if seg.speaker == SELF_LABEL:
            pass  # mic stream
        else:
            assert seg.speaker.startswith('SPEAKER_')


def test_diarize_empty_streams_returns_empty(fresh_service):
    """mic_wav=None and system_wav=None → an empty result, not a crash."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    result = svc.diarize_recording(mic_wav=None, system_wav=None)
    assert result.segments == []
    assert result.processing_time_sec == 0.0


def test_diarize_missing_file_raises(fresh_service):
    """A non-existent file → FileNotFoundError."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    with pytest.raises(FileNotFoundError):
        svc.diarize_audio('/nonexistent/path.wav')


# ---------------------------------------------------------------- real speech (opt)

def test_real_speech_detects_speakers(real_speech_wav, fresh_service):
    """Optional: if fixtures/diarization/sample.wav contains speech - 1+ speaker.

    Skipped if the file is absent. The user can put a multi-speaker WAV
    to check that the pipeline really tells voices apart.
    """
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    result = svc.diarize_audio(real_speech_wav)

    assert len(result.segments) > 0, 'pyannote returned 0 segments on real speech'
    speakers = {s.speaker for s in result.segments}
    assert len(speakers) >= 1
    # All segments have valid times
    for seg in result.segments:
        assert seg.duration > 0
        assert seg.speaker.startswith('SPEAKER_')


# ---------------------------------------------------------------- end-to-end

def test_end_to_end_orchestrator_with_real_pipeline(synthetic_speech_wav, fresh_service):
    """run_diarization_for_audio with the real pipeline (file source)."""
    _skip_if_unavailable()
    from app.services.diarization_service import run_diarization_for_audio

    whisper_segs = [
        {'start': 0.0, 'end': 1.5, 'text': 'a'},
        {'start': 1.5, 'end': 3.0, 'text': 'b'},
        {'start': 3.0, 'end': 4.5, 'text': 'c'},
    ]
    enriched, labels, ptime, embeddings = run_diarization_for_audio(
        synthetic_speech_wav, whisper_segs, source_type='file',
        return_embeddings=True,
    )
    assert len(enriched) == len(whisper_segs)
    # Every segment has a speaker label (may be SPEAKER_UNKNOWN if diar is empty)
    for s in enriched:
        assert 'speaker' in s
    assert ptime > 0
    # embeddings - a dict (may be empty if the synthetic gave 0 segments)
    assert isinstance(embeddings, dict)
