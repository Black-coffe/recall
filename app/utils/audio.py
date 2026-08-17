"""Аудіо/відео утиліти: trim через pydub, extract via FFmpeg.

Залежності зовнішні передаються через параметр `add_log` (Optional callable).
Це дозволяє використовувати функції без прямого імпорту process_logs store.
"""
import logging
import os
import subprocess
from typing import Callable, Optional

from pydub import AudioSegment

from app.utils.proc import NO_WINDOW

logger = logging.getLogger(__name__)


# Тип callable для логування процесу: (stage, message, progress, status) -> None
LogFn = Optional[Callable[[str, str, Optional[int], str], None]]


def _log_safe(add_log: LogFn, stage: str, message: str,
              progress: Optional[int] = None, status: str = "processing") -> None:
    if add_log is None:
        return
    try:
        add_log(stage, message, progress, status)
    except Exception:
        pass


def trim_audio_file(
    input_path: str,
    output_path: str,
    start_time: float,
    end_time: float,
    add_log: LogFn = None,
) -> bool:
    """Обрізає аудіофайл через pydub. Експортує MP3 192kbps.

    Args:
        input_path:  шлях до вихідного файлу.
        output_path: шлях для збереження обрізаного.
        start_time:  початок у секундах.
        end_time:    кінець у секундах.
        add_log:     опціональний callback (stage, message, progress, status).

    Returns:
        True якщо успіх, False інакше.
    """
    try:
        _log_safe(add_log, "trim", "Загрузка аудіофайлу в память...", 0)
        logger.info(f"Загрузка аудіо для обрізки: {input_path}")
        audio = AudioSegment.from_file(input_path)

        start_ms = int(start_time * 1000)
        end_ms = int(end_time * 1000)
        audio_duration = len(audio)
        if end_ms > audio_duration:
            end_ms = audio_duration

        _log_safe(add_log, "trim",
                  f"Обрізка аудіо: {start_time:.1f}с - {end_time:.1f}с", 50)
        logger.info(f"Обрізка: {start_ms}ms - {end_ms}ms з {audio_duration}ms")
        trimmed = audio[start_ms:end_ms]

        _log_safe(add_log, "trim", "Збереження обрізаного файлу...", 75)
        trimmed.export(output_path, format="mp3", bitrate="192k")
        logger.info(f"Обрізаний файл збережено: {output_path}")

        original_mb = os.path.getsize(input_path) / (1024 * 1024)
        trimmed_mb = os.path.getsize(output_path) / (1024 * 1024)
        _log_safe(add_log, "trim",
                  f"Обрізка завершена. Розмір: {original_mb:.1f}MB → {trimmed_mb:.1f}MB",
                  100)
        return True
    except Exception as e:
        logger.error(f"Помилка при обрізці аудіо: {e}")
        _log_safe(add_log, "trim", f"Помилка: {e}", -1, "error")
        return False


def extract_audio_from_video(
    video_path: str,
    audio_path: str,
    add_log: LogFn = None,
) -> bool:
    """Витягує аудіо з відео через FFmpeg → MP3 192kbps.

    Args:
        video_path:  шлях до відео.
        audio_path:  шлях для збереження mp3.
        add_log:     опціональний callback.

    Returns:
        True якщо успіх.
    """
    try:
        _log_safe(add_log, "extract_audio", "Витягування аудіо з відео...", 10)
        logger.info(f"Витягування аудіо з відео: {video_path} -> {audio_path}")
        command = [
            'ffmpeg',
            '-i', video_path,
            '-vn',
            '-acodec', 'libmp3lame',
            '-b:a', '192k',
            '-y',
            audio_path,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=NO_WINDOW,
        )
        if result.returncode != 0:
            logger.error(f"FFmpeg помилка: {result.stderr}")
            _log_safe(add_log, "extract_audio",
                      f"Помилка FFmpeg: {result.stderr}", 0, "error")
            return False
        _log_safe(add_log, "extract_audio", "Аудіо успішно витягнуто", 25)
        logger.info(f"Аудіо успішно витягнуто: {audio_path}")
        return True
    except Exception as e:
        logger.error(f"Помилка витягування аудіо: {e}")
        _log_safe(add_log, "extract_audio", f"Помилка: {e}", 0, "error")
        return False
