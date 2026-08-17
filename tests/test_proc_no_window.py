"""Тести app/utils/proc.py — прапорець CREATE_NO_WINDOW для дочірніх процесів.

Навіщо окремий файл: втрата прапорця нічого не ламає функціонально. Ffmpeg
відпрацює, транскрипт буде той самий, усі інші тести лишаться зеленими — а
користувач на Windows побачить чорне вікно, що блимає весь дзвінок (бойовий
app.py іде через pythonw.exe, тобто БЕЗ консолі, і кожна консольна дитина
дістає власне вікно). Тому інваріант пінимо тут явно.

Патчі бібліотек мутують атрибути самих бібліотек — на POSIX вони no-op
(NO_WINDOW == 0), тому перевірки прапорця йдуть під skipif.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from app.utils import proc

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != 'nt', reason='CREATE_NO_WINDOW існує лише на Windows',
)


@pytest.fixture()
def popen_flags(monkeypatch):
    """Збирає creationflags, з якими реально створювались процеси.

    Спай ставиться на Popen.__init__, а не на subprocess.run: обгортки
    захоплюють посилання на subprocess.run у момент патчу, тож підміна
    самого run їх уже не видно.
    """
    seen: list = []
    real_init = subprocess.Popen.__init__

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get('creationflags', 'MISSING'))
        raise FileNotFoundError('спай: реальний процес не запускаємо')

    monkeypatch.setattr(subprocess.Popen, '__init__', spy)
    try:
        yield seen
    finally:
        monkeypatch.setattr(subprocess.Popen, '__init__', real_init)


def test_no_window_constant_matches_platform():
    if os.name == 'nt':
        assert proc.NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        # creationflags=0 на POSIX дозволений, будь-що інше — ValueError
        assert proc.NO_WINDOW == 0


@WINDOWS_ONLY
def test_pydub_spawns_without_console_window(popen_flags):
    pytest.importorskip('pydub')
    proc.silence_pydub_console_windows()
    proc.silence_pydub_console_windows()  # ідемпотентність

    import pydub.audio_segment as pas
    import pydub.utils as pu

    # Проксі мусить лишатись прозорим для решти атрибутів subprocess
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

    # Popen НЕ підмінений: image_to_string подає власний startupinfo зі SW_HIDE
    assert pt.subprocess.Popen is subprocess.Popen
    assert pt.subprocess.PIPE == subprocess.PIPE

    with pytest.raises(Exception):
        pt.get_tesseract_version()
    with pytest.raises(Exception):
        pt.get_languages()
    assert popen_flags == [proc.NO_WINDOW, proc.NO_WINDOW]
