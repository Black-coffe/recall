"""Integration tests для DiarizationService з РЕАЛЬНИМ pyannote.audio.

Ці тести ВАНТАЖАТЬ модель (~1.5GB при першому запуску, потім кеш)
і запускають справжню інференцію — повільно (10-30s).

Запуск:
    .venv/Scripts/python.exe -m pytest tests/test_diarization_integration.py -m slow

Або всі тести включно зі slow:
    .venv/Scripts/python.exe -m pytest -m ""

Skip-condition (auto):
- pyannote.audio не встановлений
- HF_TOKEN відсутній у env
- Не прийнята ліцензія pyannote/* (буде 401 при load)

Strategy:
- Synthetic audio fixture через numpy + wave (без зовнішніх залежностей).
  Це не справжня мова — pyannote може повернути 0 сегментів — але
  вантаження pipeline і інференція не повинні крашитись.
- Якщо у tests/fixtures/diarization/<name>.wav лежить РЕАЛЬНЕ мовлення —
  test_real_speech_detects_speakers (опц.) перевіряє що 1+ спікера
  знайдено. Користувач може покласти свій файл туди для richer coverage.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest


pytestmark = pytest.mark.slow


# ---------------------------------------------------------------- skip helpers

def _skip_if_unavailable():
    """Skip всі тести якщо pyannote / token недоступні."""
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
    """Створює короткий синтетичний WAV без додаткових залежностей.

    Генерує 5s mono 16kHz signal з:
    - Воїд-частотами (формантами) щоб виглядало мов-подібно
    - Огинаючою на ~4Hz (rate складів)
    - Тиша посередині (тест VAD-shift detection)

    Не справжня мова — pyannote може повернути 0 сегментів.
    Тест просто перевіряє що pipeline не крашиться.
    """
    import math
    import struct

    sr = 16000
    duration = 5.0
    n_samples = int(sr * duration)

    samples = []
    for i in range(n_samples):
        t = i / sr
        # Тиша 2.0–3.0s
        if 2.0 <= t < 3.0:
            samples.append(0)
            continue
        # Складова "мовно-подібна"
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
    """Опц. фікстура — реальне мовлення для richer coverage.

    Користувач може покласти WAV або MP3 у tests/fixtures/diarization/sample.wav.
    Якщо файлу немає — тест skip'ається.
    """
    path = Path(__file__).parent / 'fixtures' / 'diarization' / 'sample.wav'
    if not path.is_file():
        pytest.skip(f'No real speech fixture at {path} — drop a multi-speaker WAV/MP3 here for richer coverage')
    return path


@pytest.fixture
def fresh_service():
    """Reset singleton між тестами щоб state не текла."""
    from app.services.diarization_service import DiarizationService
    DiarizationService._instance = None
    yield
    DiarizationService._instance = None


# ---------------------------------------------------------------- pipeline load

def test_pipeline_loads_with_token(fresh_service):
    """Перший виклик ensure_pipeline вантажить модель з HF cache."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    assert svc.is_available(), svc.unavailability_reason()
    pipeline = svc._ensure_pipeline()
    assert pipeline is not None


def test_pipeline_singleton(fresh_service):
    """Повторні get_instance() повертають той самий обʼєкт."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    a = DiarizationService.get_instance()
    b = DiarizationService.get_instance()
    assert a is b


# ---------------------------------------------------------------- inference

def test_diarize_synthetic_doesnt_crash(synthetic_speech_wav, fresh_service):
    """pyannote має обробити синтетичне аудіо без виключень.

    Може повернути 0 сегментів (синтез не схожий на мову достатньо),
    головне — не крашиться і повертає DiarizationResult.
    """
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService, DiarizationResult

    svc = DiarizationService.get_instance()
    result = svc.diarize_audio(synthetic_speech_wav)

    assert isinstance(result, DiarizationResult)
    assert result.processing_time_sec > 0
    # Усі сегменти всередині duration файлу
    for seg in result.segments:
        assert 0 <= seg.start < seg.end <= 6.0  # 5s + slack
        assert seg.speaker.startswith('SPEAKER_')


def test_diarize_recording_per_stream(synthetic_speech_wav, fresh_service, tmp_path):
    """diarize_recording з mic.wav → всі лейбли self; system.wav → SPEAKER_NN."""
    _skip_if_unavailable()
    from app.services.diarization_service import (
        DiarizationService,
        SELF_LABEL,
    )

    # Reuse synthetic як mic + копія як system (різні шляхи, той самий контент)
    mic = synthetic_speech_wav
    system = tmp_path / 'system.wav'
    system.write_bytes(mic.read_bytes())

    svc = DiarizationService.get_instance()
    result = svc.diarize_recording(mic_wav=mic, system_wav=system)

    # Якщо synthetic generates 0 segments — тест пройде як no-op.
    # Якщо є сегменти — мають бути або self (з mic) або SPEAKER_NN (з system).
    for seg in result.segments:
        if seg.speaker == SELF_LABEL:
            pass  # mic stream
        else:
            assert seg.speaker.startswith('SPEAKER_')


def test_diarize_empty_streams_returns_empty(fresh_service):
    """mic_wav=None і system_wav=None → порожній результат, не крах."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    result = svc.diarize_recording(mic_wav=None, system_wav=None)
    assert result.segments == []
    assert result.processing_time_sec == 0.0


def test_diarize_missing_file_raises(fresh_service):
    """Неіснуючий файл → FileNotFoundError."""
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    with pytest.raises(FileNotFoundError):
        svc.diarize_audio('/nonexistent/path.wav')


# ---------------------------------------------------------------- real speech (opt)

def test_real_speech_detects_speakers(real_speech_wav, fresh_service):
    """Опц.: якщо у fixtures/diarization/sample.wav є мовлення — 1+ спікер.

    Skipped якщо файлу немає. Користувач може покласти multi-speaker WAV
    щоб перевірити що pipeline дійсно розрізняє голоси.
    """
    _skip_if_unavailable()
    from app.services.diarization_service import DiarizationService

    svc = DiarizationService.get_instance()
    result = svc.diarize_audio(real_speech_wav)

    assert len(result.segments) > 0, 'pyannote повернув 0 сегментів на real speech'
    speakers = {s.speaker for s in result.segments}
    assert len(speakers) >= 1
    # Усі сегменти мають валідні часи
    for seg in result.segments:
        assert seg.duration > 0
        assert seg.speaker.startswith('SPEAKER_')


# ---------------------------------------------------------------- end-to-end

def test_end_to_end_orchestrator_with_real_pipeline(synthetic_speech_wav, fresh_service):
    """run_diarization_for_audio з реальним pipeline (file source)."""
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
    # Кожен сегмент має speaker label (може бути SPEAKER_UNKNOWN якщо diar empty)
    for s in enriched:
        assert 'speaker' in s
    assert ptime > 0
    # embeddings — dict (може бути порожній якщо synthetic дав 0 segments)
    assert isinstance(embeddings, dict)
