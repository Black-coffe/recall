"""Tests for _ensure_pyannote_compatible - the m4a → WAV conversion fix.

soundfile/libsndfile (the pyannote backend) cannot read AAC containers
(.m4a, .mp4, .aac). The helper transparently converts them via ffmpeg.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import diarization_service as ds
from app.utils.proc import NO_WINDOW


@pytest.fixture
def fake_audio(tmp_path):
    def _make(ext: str) -> Path:
        p = tmp_path / f'sample{ext}'
        p.write_bytes(b'fake audio bytes')
        return p
    return _make


@pytest.mark.parametrize('ext', ['.wav', '.WAV', '.flac', '.ogg', '.aiff'])
def test_native_formats_passthrough(fake_audio, ext):
    """Soundfile-compatible formats are not converted - no temp file is created."""
    src = fake_audio(ext)
    with patch.object(ds.subprocess, 'run') as run_mock:
        path, tmp = ds._ensure_pyannote_compatible(src)
    assert path == src
    assert tmp is None
    run_mock.assert_not_called()


@pytest.mark.parametrize('ext', ['.m4a', '.mp4', '.aac', '.wma', '.opus', '.webm', '.mp3', '.MP3'])
def test_unsupported_formats_converted(fake_audio, ext, tmp_path):
    """Non-soundfile formats go through ffmpeg → WAV 16kHz mono.

    '.mp3' is here ON PURPOSE (although soundfile can read it): an MP3 seek in the
    pyannote embeddings phase is O(n²) in the recording length, a one-off WAV conversion
    saves tens of minutes on hour-long recordings (§12, 03.07.2026)."""
    src = fake_audio(ext)

    def fake_ffmpeg(cmd, check, capture_output, **kwargs):
        # cmd[-1] is the output path, we create the file so that stat() works
        Path(cmd[-1]).write_bytes(b'RIFF' + b'\x00' * 100)
        return MagicMock(returncode=0)

    with patch.object(ds.subprocess, 'run', side_effect=fake_ffmpeg) as run_mock:
        path, tmp = ds._ensure_pyannote_compatible(src)

    run_mock.assert_called_once()
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == 'ffmpeg'
    # Under pythonw.exe (the production app.py launch) a console child without this
    # flag flashes a black window. Losing the flag breaks nothing
    # functionally - so we pin it with a test.
    assert run_mock.call_args.kwargs.get('creationflags') == NO_WINDOW
    assert '-ac' in cmd and cmd[cmd.index('-ac') + 1] == '1'
    assert '-ar' in cmd and cmd[cmd.index('-ar') + 1] == '16000'
    assert str(src) in cmd
    assert path == tmp
    assert tmp is not None and tmp.suffix == '.wav'
    tmp.unlink(missing_ok=True)


def test_ffmpeg_missing_raises(fake_audio):
    """ffmpeg is missing - an explicit RuntimeError, no silent fallback."""
    src = fake_audio('.m4a')
    with patch.object(ds.subprocess, 'run', side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match='ffmpeg not found'):
            ds._ensure_pyannote_compatible(src)


def test_ffmpeg_error_raises(fake_audio):
    """ffmpeg returned non-zero - a RuntimeError with part of stderr."""
    src = fake_audio('.m4a')
    err = subprocess.CalledProcessError(
        returncode=1, cmd=['ffmpeg'], stderr=b'Invalid data found',
    )
    with patch.object(ds.subprocess, 'run', side_effect=err):
        with pytest.raises(RuntimeError, match='Invalid data'):
            ds._ensure_pyannote_compatible(src)
