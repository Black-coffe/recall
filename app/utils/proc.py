"""Child process launch flags (Windows).

A console child process (ffmpeg/ffprobe/explorer) on Windows inherits the
parent's console. If the parent has NO console — and it has none when app.py is started
via ``pythonw.exe`` — Windows creates a NEW console for the child: a black window
flashes and disappears on every call.

During recording this is the most noticeable: live transcription every 8 s per stream
(microphone + system audio) writes a temporary WAV and asks for its duration via
``ffprobe`` → the window flashes every few seconds through the whole conversation.

``CREATE_NO_WINDOW`` removes this regardless of how it is launched (with or without a console).
POSIX has no such constant — we substitute 0, and ``creationflags=0`` is allowed there.

Usage::

    from app.utils.proc import NO_WINDOW
    subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)

Our own calls are covered by this parameter. Libraries that launch ffmpeg/tesseract
themselves and do not pass the flag are patched by the ``silence_*`` functions below: the substitution
is done in the attributes of the library ITSELF, the global ``subprocess`` is not touched.
"""
from __future__ import annotations

import functools
import logging
import subprocess

logger = logging.getLogger(__name__)

#: 0x08000000 on Windows, 0 on other platforms.
NO_WINDOW: int = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

_pydub_patched = False
_whisper_patched = False
_pytesseract_patched = False


def _no_window(fn):
    """Wrapper: appends creationflags=NO_WINDOW if the caller did not set it."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        kwargs.setdefault('creationflags', NO_WINDOW)
        return fn(*args, **kwargs)
    return _wrapper


def silence_pydub_console_windows() -> None:
    """Remove the console flashing of ffmpeg/ffprobe launched by pydub.

    We cover our own calls with the ``creationflags=NO_WINDOW`` parameter, but
    pydub calls ffmpeg itself (``AudioSegment.from_file``/``export``,
    ``utils.mediainfo_json``) and does not pass the flag. We substitute ``Popen`` only
    in two pydub modules — the global ``subprocess`` is not touched.

    Idempotent, safe to call repeatedly, never raises.
    """
    global _pydub_patched
    if _pydub_patched or not NO_WINDOW:
        return
    try:
        import pydub.audio_segment as _pas
        import pydub.utils as _pu
    except Exception:
        logger.debug('pydub cannot be imported — no-window patch skipped', exc_info=True)
        return

    class _NoWindowPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('creationflags', NO_WINDOW)
            super().__init__(*args, **kwargs)

    class _SubprocessProxy:
        """Proxy to the subprocess module with only Popen replaced."""
        Popen = _NoWindowPopen

        def __getattr__(self, name):
            return getattr(subprocess, name)

    _pas.subprocess = _SubprocessProxy()   # audio_segment does subprocess.Popen(...)
    _pu.Popen = _NoWindowPopen             # utils does `from subprocess import Popen`
    _pydub_patched = True
    logger.debug('pydub: ffmpeg/ffprobe are launched without a console window')


def silence_openai_whisper_console_windows() -> None:
    """Same for openai-whisper (the ``WHISPER_BACKEND=openai`` backend).

    ``whisper/audio.py`` does ``from subprocess import run`` and calls ffmpeg
    for EVERY audio chunk (chunked transcription cuts every 5 min) — without the flag that is
    a window per chunk. The default ``faster`` backend does not get here: it uses PyAV,
    with no child processes. We replace only the ``run`` name in the whisper.audio module.

    Called from ``OpenAIWhisperBackend.__init__`` — where whisper is actually
    loaded, to avoid pulling the heavy import in at app.py startup.

    Idempotent, never raises.
    """
    global _whisper_patched
    if _whisper_patched or not NO_WINDOW:
        return
    try:
        import whisper.audio as _wa
    except Exception:
        logger.debug('whisper.audio cannot be imported — no-window patch skipped',
                     exc_info=True)
        return
    _wa.run = _no_window(subprocess.run)
    _whisper_patched = True
    logger.debug('openai-whisper: ffmpeg is launched without a console window')


def silence_pytesseract_console_windows() -> None:
    """Same for pytesseract.

    The main OCR path (``image_to_string``) sets ``SW_HIDE`` itself via
    ``subprocess_args()`` — we do not touch it. But ``get_tesseract_version`` and
    ``get_languages`` call ``subprocess.run``/``check_output`` bare, bypassing
    that helper. Both are under ``@run_once``, so it is one flash per process — but
    exactly on the first document ingest, when the user is looking at the screen.

    Idempotent, never raises.
    """
    global _pytesseract_patched
    if _pytesseract_patched or not NO_WINDOW:
        return
    try:
        import pytesseract.pytesseract as _pt
    except Exception:
        logger.debug('pytesseract cannot be imported — no-window patch skipped',
                     exc_info=True)
        return

    class _SubprocessProxy:
        """Proxy to subprocess with only run/check_output replaced.

        We do NOT touch Popen: ``image_to_string`` passes its own startupinfo
        with SW_HIDE there — there is no window there anyway.
        """
        run = staticmethod(_no_window(subprocess.run))
        check_output = staticmethod(_no_window(subprocess.check_output))

        def __getattr__(self, name):
            return getattr(subprocess, name)

    _pt.subprocess = _SubprocessProxy()
    _pytesseract_patched = True
    logger.debug('pytesseract: version/list-langs without a console window')


__all__ = [
    'NO_WINDOW',
    'silence_pydub_console_windows',
    'silence_openai_whisper_console_windows',
    'silence_pytesseract_console_windows',
]
