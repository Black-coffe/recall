"""Прапорці запуску дочірніх процесів (Windows).

Консольний дочірній процес (ffmpeg/ffprobe/explorer) на Windows успадковує
консоль батька. Якщо у батька консолі НЕМА — а її нема, коли app.py запущено
через ``pythonw.exe`` — Windows створює для дитини НОВУ консоль: чорне вікно
блимає й зникає на кожен виклик.

Під час запису це помітно найбільше: live-транскрипція кожні 8 с на потік
(мікрофон + системний звук) пише тимчасовий WAV і питає його тривалість через
``ffprobe`` → вікно блимає кожні кілька секунд усю розмову.

``CREATE_NO_WINDOW`` знімає це незалежно від способу запуску (з консолі чи без).
На POSIX константи нема — підставляємо 0, а ``creationflags=0`` там дозволений.

Використання::

    from app.utils.proc import NO_WINDOW
    subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)

Свої виклики закриті цим параметром. Бібліотеки, які запускають ffmpeg/tesseract
самі й прапорця не передають, латаються функціями ``silence_*`` нижче: підміна
робиться в атрибутах САМОЇ бібліотеки, глобальний ``subprocess`` не чіпаємо.
"""
from __future__ import annotations

import functools
import logging
import subprocess

logger = logging.getLogger(__name__)

#: 0x08000000 на Windows, 0 на решті платформ.
NO_WINDOW: int = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

_pydub_patched = False
_whisper_patched = False
_pytesseract_patched = False


def _no_window(fn):
    """Обгортка: дописує creationflags=NO_WINDOW, якщо викликач його не задав."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        kwargs.setdefault('creationflags', NO_WINDOW)
        return fn(*args, **kwargs)
    return _wrapper


def silence_pydub_console_windows() -> None:
    """Прибрати блимання консолі у ffmpeg/ffprobe, які запускає pydub.

    Свої виклики ми закриваємо параметром ``creationflags=NO_WINDOW``, але
    pydub кличе ffmpeg сам (``AudioSegment.from_file``/``export``,
    ``utils.mediainfo_json``) і прапорця не передає. Підміняємо ``Popen`` лише
    у двох модулях pydub — глобальний ``subprocess`` не чіпаємо.

    Ідемпотентна, безпечна для повторного виклику, ніколи не кидає.
    """
    global _pydub_patched
    if _pydub_patched or not NO_WINDOW:
        return
    try:
        import pydub.audio_segment as _pas
        import pydub.utils as _pu
    except Exception:
        logger.debug('pydub не імпортується — патч no-window пропущено', exc_info=True)
        return

    class _NoWindowPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('creationflags', NO_WINDOW)
            super().__init__(*args, **kwargs)

    class _SubprocessProxy:
        """Проксі на модуль subprocess із підміненим лише Popen."""
        Popen = _NoWindowPopen

        def __getattr__(self, name):
            return getattr(subprocess, name)

    _pas.subprocess = _SubprocessProxy()   # audio_segment робить subprocess.Popen(...)
    _pu.Popen = _NoWindowPopen             # utils робить `from subprocess import Popen`
    _pydub_patched = True
    logger.debug('pydub: ffmpeg/ffprobe запускаються без консольного вікна')


def silence_openai_whisper_console_windows() -> None:
    """Те саме для openai-whisper (бекенд ``WHISPER_BACKEND=openai``).

    ``whisper/audio.py`` робить ``from subprocess import run`` і кличе ffmpeg
    на КОЖЕН шматок аудіо (chunked-транскрипція ріже по 5 хв) — без прапорця це
    вікно на кожен чанк. Дефолтний бекенд ``faster`` сюди не заходить: там PyAV,
    без дочірніх процесів. Підміняємо лише імʼя ``run`` у модулі whisper.audio.

    Кличеться з ``OpenAIWhisperBackend.__init__`` — там, де whisper реально
    вантажиться, щоб не тягнути важкий імпорт на старті app.py.

    Ідемпотентна, ніколи не кидає.
    """
    global _whisper_patched
    if _whisper_patched or not NO_WINDOW:
        return
    try:
        import whisper.audio as _wa
    except Exception:
        logger.debug('whisper.audio не імпортується — патч no-window пропущено',
                     exc_info=True)
        return
    _wa.run = _no_window(subprocess.run)
    _whisper_patched = True
    logger.debug('openai-whisper: ffmpeg запускається без консольного вікна')


def silence_pytesseract_console_windows() -> None:
    """Те саме для pytesseract.

    Основний шлях OCR (``image_to_string``) сам ставить ``SW_HIDE`` через
    ``subprocess_args()`` — його не чіпаємо. А ось ``get_tesseract_version`` і
    ``get_languages`` кличуть ``subprocess.run``/``check_output`` голяка, повз
    той хелпер. Обидві під ``@run_once``, тож це один спалах на процес — але
    саме на першому інджесті документа, коли користувач дивиться на екран.

    Ідемпотентна, ніколи не кидає.
    """
    global _pytesseract_patched
    if _pytesseract_patched or not NO_WINDOW:
        return
    try:
        import pytesseract.pytesseract as _pt
    except Exception:
        logger.debug('pytesseract не імпортується — патч no-window пропущено',
                     exc_info=True)
        return

    class _SubprocessProxy:
        """Проксі на subprocess із підміненими run/check_output.

        Popen НЕ чіпаємо: ``image_to_string`` подає туди власний startupinfo
        зі SW_HIDE — вікна там і так нема.
        """
        run = staticmethod(_no_window(subprocess.run))
        check_output = staticmethod(_no_window(subprocess.check_output))

        def __getattr__(self, name):
            return getattr(subprocess, name)

    _pt.subprocess = _SubprocessProxy()
    _pytesseract_patched = True
    logger.debug('pytesseract: version/list-langs без консольного вікна')


__all__ = [
    'NO_WINDOW',
    'silence_pydub_console_windows',
    'silence_openai_whisper_console_windows',
    'silence_pytesseract_console_windows',
]
