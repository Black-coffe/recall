"""Audio/video utilities: trim via pydub, extract via FFmpeg.

External dependencies are passed through the `add_log` parameter (Optional callable).
This lets the functions be used without directly importing the process_logs store.
"""
import logging
import os
import subprocess
from typing import Callable, Optional

from pydub import AudioSegment

from app.core.i18n import tr
from app.utils.proc import NO_WINDOW

logger = logging.getLogger(__name__)


# Callable type for process logging: (stage, message, progress, status) -> None
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
    """Trims an audio file via pydub. Exports MP3 192kbps.

    Args:
        input_path:  path to the source file.
        output_path: path to save the trimmed file.
        start_time:  start in seconds.
        end_time:    end in seconds.
        add_log:     optional callback (stage, message, progress, status).

    Returns:
        True on success, False otherwise.
    """
    try:
        _log_safe(add_log, "trim", tr("svc_boot.audio.trim.loading"), 0)
        logger.info(f"Loading audio for trimming: {input_path}")
        audio = AudioSegment.from_file(input_path)

        start_ms = int(start_time * 1000)
        end_ms = int(end_time * 1000)
        audio_duration = len(audio)
        if end_ms > audio_duration:
            end_ms = audio_duration

        _log_safe(add_log, "trim",
                  tr("svc_boot.audio.trim.cutting", start=f"{start_time:.1f}", end=f"{end_time:.1f}"), 50)
        logger.info(f"Trimming: {start_ms}ms - {end_ms}ms of {audio_duration}ms")
        trimmed = audio[start_ms:end_ms]

        _log_safe(add_log, "trim", tr("svc_boot.audio.trim.saving"), 75)
        trimmed.export(output_path, format="mp3", bitrate="192k")
        logger.info(f"Trimmed file saved: {output_path}")

        original_mb = os.path.getsize(input_path) / (1024 * 1024)
        trimmed_mb = os.path.getsize(output_path) / (1024 * 1024)
        _log_safe(add_log, "trim",
                  tr("svc_boot.audio.trim.done", original=f"{original_mb:.1f}", trimmed=f"{trimmed_mb:.1f}"),
                  100)
        return True
    except Exception as e:
        logger.error(f"Error while trimming audio: {e}")
        _log_safe(add_log, "trim", tr("svc_boot.audio.error", error=e), -1, "error")
        return False


def extract_audio_from_video(
    video_path: str,
    audio_path: str,
    add_log: LogFn = None,
) -> bool:
    """Extracts audio from a video via FFmpeg → MP3 192kbps.

    Args:
        video_path:  path to the video.
        audio_path:  path to save the mp3.
        add_log:     optional callback.

    Returns:
        True on success.
    """
    try:
        _log_safe(add_log, "extract_audio", tr("svc_boot.audio.extract.start"), 10)
        logger.info(f"Extracting audio from video: {video_path} -> {audio_path}")
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
            logger.error(f"FFmpeg error: {result.stderr}")
            _log_safe(add_log, "extract_audio",
                      tr("svc_boot.audio.extract.ffmpeg_error", error=result.stderr), 0, "error")
            return False
        _log_safe(add_log, "extract_audio", tr("svc_boot.audio.extract.done"), 25)
        logger.info(f"Audio extracted successfully: {audio_path}")
        return True
    except Exception as e:
        logger.error(f"Error extracting audio: {e}")
        _log_safe(add_log, "extract_audio", tr("svc_boot.audio.error", error=e), 0, "error")
        return False
