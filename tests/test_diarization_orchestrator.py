"""Tests для run_diarization_for_audio orchestrator (Phase 10.3).

Не вантажимо pyannote pipeline — мокаємо DiarizationService методи
diarize_audio / diarize_recording. Перевіряємо тільки routing-логіку:
- recording з manifest з streams_finalized → diarize_recording
- recording без streams_finalized → fallback на diarize_audio (mix)
- file/youtube → diarize_audio
- merge коректно проставляє speaker label на whisper-сегментах
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.diarization_service import (
    SELF_LABEL,
    DiarizationResult,
    DiarSegment,
    DiarizationService,
    run_diarization_for_audio,
)


@pytest.fixture
def fake_audio(tmp_path):
    """Створює фейковий audio file (порожній OK — service mock'ається)."""
    p = tmp_path / 'audio.mp3'
    p.write_bytes(b'fake mp3 content')
    return p


@pytest.fixture
def fake_recording_files(tmp_path):
    """Створює fake mic.wav + system.wav."""
    mic = tmp_path / 'mic.wav'
    sys_ = tmp_path / 'system.wav'
    mic.write_bytes(b'fake mic wav')
    sys_.write_bytes(b'fake sys wav')
    return mic, sys_


@pytest.fixture
def mocked_service(monkeypatch):
    """Singleton DiarizationService з мокнутими методами + token."""
    DiarizationService._instance = None  # reset singleton
    svc = DiarizationService(hf_token='hf_fake_token_for_test')
    svc._import_error = None  # pretend pyannote installed
    svc.diarize_audio = MagicMock(return_value=DiarizationResult(
        segments=[DiarSegment(0, 5, 'SPEAKER_00'),
                  DiarSegment(5, 10, 'SPEAKER_01')],
        processing_time_sec=0.5,
    ))
    svc.diarize_recording = MagicMock(return_value=DiarizationResult(
        segments=[DiarSegment(0, 3, SELF_LABEL),
                  DiarSegment(3, 8, 'SPEAKER_00'),
                  DiarSegment(8, 10, SELF_LABEL)],
        processing_time_sec=0.7,
    ))
    monkeypatch.setattr(DiarizationService, 'get_instance', staticmethod(lambda: svc))
    yield svc
    DiarizationService._instance = None


# ---------------------------------------------------------------- routing

def test_file_source_routes_to_diarize_audio(mocked_service, fake_audio):
    whisper_segs = [
        {'start': 0, 'end': 5, 'text': 'a'},
        {'start': 5, 'end': 10, 'text': 'b'},
    ]
    enriched, labels, _, _ = run_diarization_for_audio(
        fake_audio, whisper_segs, source_type='file',
    )
    mocked_service.diarize_audio.assert_called_once()
    mocked_service.diarize_recording.assert_not_called()
    assert labels == ['SPEAKER_00', 'SPEAKER_01']
    assert [s['speaker'] for s in enriched] == ['SPEAKER_00', 'SPEAKER_01']


def test_youtube_source_routes_to_diarize_audio(mocked_service, fake_audio):
    whisper_segs = [{'start': 0, 'end': 5, 'text': 'x'}]
    run_diarization_for_audio(
        fake_audio, whisper_segs, source_type='youtube',
    )
    mocked_service.diarize_audio.assert_called_once()
    mocked_service.diarize_recording.assert_not_called()


def test_recording_with_streams_finalized_routes_to_per_stream(
    mocked_service, fake_audio, fake_recording_files,
):
    mic, sys_ = fake_recording_files
    manifest = {
        'streams_finalized': {
            'mic': {'wav_path': str(mic)},
            'system': {'wav_path': str(sys_)},
        },
    }
    whisper_segs = [
        {'start': 0, 'end': 3, 'text': 'a'},
        {'start': 3, 'end': 8, 'text': 'b'},
        {'start': 8, 'end': 10, 'text': 'c'},
    ]
    enriched, labels, _, _ = run_diarization_for_audio(
        fake_audio, whisper_segs,
        source_type='recording', recording_manifest=manifest,
    )
    mocked_service.diarize_recording.assert_called_once()
    mocked_service.diarize_audio.assert_not_called()
    # Verify per-stream wav paths passed
    args, kwargs = mocked_service.diarize_recording.call_args
    assert kwargs.get('mic_wav') == str(mic)
    assert kwargs.get('system_wav') == str(sys_)
    # Speaker assignment: whisper segments get matching labels
    assert SELF_LABEL in labels
    assert 'SPEAKER_00' in labels


def test_recording_with_only_mic_stream(mocked_service, fake_audio, fake_recording_files):
    mic, _ = fake_recording_files
    manifest = {
        'streams_finalized': {
            'mic': {'wav_path': str(mic)},
            # No system
        },
    }
    whisper_segs = [{'start': 0, 'end': 5, 'text': 'a'}]
    run_diarization_for_audio(
        fake_audio, whisper_segs,
        source_type='recording', recording_manifest=manifest,
    )
    args, kwargs = mocked_service.diarize_recording.call_args
    assert kwargs.get('mic_wav') == str(mic)
    assert kwargs.get('system_wav') is None


def test_recording_without_streams_finalized_falls_back_to_mix(
    mocked_service, fake_audio,
):
    """Старі manifest'и без streams_finalized → fallback на diarize_audio(mix)."""
    manifest = {'status': 'finalized'}  # no streams_finalized
    whisper_segs = [{'start': 0, 'end': 5, 'text': 'a'}]
    run_diarization_for_audio(
        fake_audio, whisper_segs,
        source_type='recording', recording_manifest=manifest,
    )
    mocked_service.diarize_audio.assert_called_once_with(
        fake_audio, progress_callback=None, return_embeddings=False,
    )
    mocked_service.diarize_recording.assert_not_called()


def test_recording_with_empty_streams_finalized_falls_back(mocked_service, fake_audio):
    """streams_finalized присутнє але порожнє (no wav_path) → fallback."""
    manifest = {
        'streams_finalized': {
            'mic': {},  # no wav_path
            'system': {},
        },
    }
    whisper_segs = [{'start': 0, 'end': 5, 'text': 'a'}]
    run_diarization_for_audio(
        fake_audio, whisper_segs,
        source_type='recording', recording_manifest=manifest,
    )
    mocked_service.diarize_audio.assert_called_once()
    mocked_service.diarize_recording.assert_not_called()


def test_recording_without_manifest_falls_back(mocked_service, fake_audio):
    """source_type='recording' але manifest=None → fallback на diarize_audio."""
    whisper_segs = [{'start': 0, 'end': 5, 'text': 'a'}]
    run_diarization_for_audio(
        fake_audio, whisper_segs,
        source_type='recording', recording_manifest=None,
    )
    mocked_service.diarize_audio.assert_called_once()
    mocked_service.diarize_recording.assert_not_called()


def test_unavailable_service_raises(monkeypatch, fake_audio):
    """Якщо HF_TOKEN відсутній — orchestrator кидає RuntimeError.

    Відсутність токена створюємо самі: `DiarizationService(hf_token=None)`
    підхоплює `os.environ['HF_TOKEN']`, а той зʼявляється в оточенні від будь-
    якого імпорту, що читає `.env` (`config` тягне `load_dotenv`). Без цього
    тест проходив лише тоді, коли ніхто в прогоні раніше не імпортував config,
    і падав від сусіда — з повідомленням про ffmpeg, бо сервіс вважався живим
    і йшов далі до реального pyannote.
    """
    monkeypatch.delenv('HF_TOKEN', raising=False)
    DiarizationService._instance = None
    svc = DiarizationService(hf_token=None)
    svc._import_error = None
    monkeypatch.setattr(DiarizationService, 'get_instance', staticmethod(lambda: svc))

    with pytest.raises(RuntimeError, match='HF_TOKEN'):
        run_diarization_for_audio(
            fake_audio, [{'start': 0, 'end': 5, 'text': 'a'}],
            source_type='file',
        )
    DiarizationService._instance = None


def test_processing_time_propagated(mocked_service, fake_audio):
    _, _, processing_time, _ = run_diarization_for_audio(
        fake_audio, [{'start': 0, 'end': 5, 'text': 'a'}],
        source_type='file',
    )
    assert processing_time == 0.5  # from mock


def test_progress_callback_passed_to_diarize_audio(mocked_service, fake_audio):
    cb = MagicMock()
    run_diarization_for_audio(
        fake_audio, [{'start': 0, 'end': 5, 'text': 'a'}],
        source_type='file', progress_callback=cb,
    )
    args, kwargs = mocked_service.diarize_audio.call_args
    assert kwargs.get('progress_callback') is cb


def test_progress_callback_passed_to_diarize_recording(
    mocked_service, fake_audio, fake_recording_files,
):
    mic, sys_ = fake_recording_files
    manifest = {
        'streams_finalized': {
            'mic': {'wav_path': str(mic)},
            'system': {'wav_path': str(sys_)},
        },
    }
    cb = MagicMock()
    run_diarization_for_audio(
        fake_audio, [{'start': 0, 'end': 5, 'text': 'a'}],
        source_type='recording', recording_manifest=manifest,
        progress_callback=cb,
    )
    args, kwargs = mocked_service.diarize_recording.call_args
    assert kwargs.get('progress_callback') is cb


def test_unique_labels_returned_in_appearance_order(mocked_service, fake_audio):
    """labels мають бути у порядку першої появи у whisper-сегментах."""
    # Mock returns segments in order: SPEAKER_00, SPEAKER_01
    whisper_segs = [
        {'start': 0, 'end': 5, 'text': 'a'},   # → SPEAKER_00
        {'start': 5, 'end': 10, 'text': 'b'},  # → SPEAKER_01
    ]
    _, labels, _, _ = run_diarization_for_audio(
        fake_audio, whisper_segs, source_type='file',
    )
    assert labels == ['SPEAKER_00', 'SPEAKER_01']


# ---------------------------------------------------------------- service availability

def test_is_available_with_token():
    DiarizationService._instance = None
    svc = DiarizationService(hf_token='hf_xxx')
    svc._import_error = None
    assert svc.is_available() is True
    assert svc.unavailability_reason() is None
    DiarizationService._instance = None


def test_is_available_without_token():
    DiarizationService._instance = None
    svc = DiarizationService(hf_token=None)
    svc._import_error = None
    # Може бути None якщо у environment немає HF_TOKEN
    import os
    saved = os.environ.pop('HF_TOKEN', None)
    try:
        svc2 = DiarizationService(hf_token=None)
        svc2._import_error = None
        assert svc2.is_available() is False
        assert 'HF_TOKEN' in svc2.unavailability_reason()
    finally:
        if saved:
            os.environ['HF_TOKEN'] = saved
    DiarizationService._instance = None


def test_is_available_with_import_error():
    DiarizationService._instance = None
    svc = DiarizationService(hf_token='hf_xxx')
    svc._import_error = 'pyannote.audio not installed'
    assert svc.is_available() is False
    assert 'pyannote' in svc.unavailability_reason()
    DiarizationService._instance = None
