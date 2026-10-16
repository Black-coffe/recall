"""Tests of app/utils/proc.py — the CREATE_NO_WINDOW flag for child processes.

Why a separate file: losing the flag breaks nothing functionally. Ffmpeg
will work, the transcript will be the same, all other tests will stay green — while
a user on Windows will see a black window flashing throughout the call (the live
app.py runs via pythonw.exe, i.e. WITHOUT a console, and every console child
gets its own window). So the invariant is pinned here explicitly.

Library patches mutate the attributes of the libraries themselves — on POSIX they are no-ops
(NO_WINDOW == 0), so the flag checks run under skipif.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from app.utils import proc

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != 'nt', reason='CREATE_NO_WINDOW exists only on Windows',
)


@pytest.fixture()
def popen_flags(monkeypatch):
    """Collects the creationflags with which processes were really created.

    The spy is put on Popen.__init__, not on subprocess.run: wrappers
    capture the reference to subprocess.run at patch time, so replacing
    run itself is no longer visible to them.
    """
    seen: list = []
    real_init = subprocess.Popen.__init__

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get('creationflags', 'MISSING'))
        raise FileNotFoundError('spy: we do not launch a real process')

    monkeypatch.setattr(subprocess.Popen, '__init__', spy)
    try:
        yield seen
    finally:
        monkeypatch.setattr(subprocess.Popen, '__init__', real_init)


def test_no_window_constant_matches_platform():
    if os.name == 'nt':
        assert proc.NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        # creationflags=0 is allowed on POSIX, anything else is a ValueError
        assert proc.NO_WINDOW == 0


@WINDOWS_ONLY
def test_pydub_spawns_without_console_window(popen_flags):
    pytest.importorskip('pydub')
    proc.silence_pydub_console_windows()
    proc.silence_pydub_console_windows()  # idempotency

    import pydub.audio_segment as pas
    import pydub.utils as pu

    # The proxy must stay transparent for the rest of the subprocess attributes
    assert pas.subprocess.PIPE == subprocess.PIPE
    assert pas.subprocess.DEVNULL == subprocess.DEVNULL

    with pytest.raises(Exception):
        pu.Popen(['ffprobe', '-version'], stdout=subprocess.PIPE)
    assert popen_flags == [proc.NO_WINDOW]


@WINDOWS_ONLY
def test_openai_whisper_spawns_without_console_window(popen_flags):
    pytest.importorskip('whisper')
    proc.silence_openai_whisper_console_windows()
    proc.silence_openai_whisper_console_windows()

    import whisper.audio as wa

    with pytest.raises(Exception):
        wa.load_audio(r'C:\nonexistent\nothing.wav')
    assert popen_flags == [proc.NO_WINDOW]


@WINDOWS_ONLY
def test_pytesseract_version_probe_spawns_without_console_window(popen_flags):
    pytest.importorskip('pytesseract')
    proc.silence_pytesseract_console_windows()
    proc.silence_pytesseract_console_windows()

    import pytesseract.pytesseract as pt

    # Popen is NOT replaced: image_to_string passes its own startupinfo with SW_HIDE
    assert pt.subprocess.Popen is subprocess.Popen
    assert pt.subprocess.PIPE == subprocess.PIPE

    with pytest.raises(Exception):
        pt.get_tesseract_version()
    with pytest.raises(Exception):
        pt.get_languages()
    assert popen_flags == [proc.NO_WINDOW, proc.NO_WINDOW]
