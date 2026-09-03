"""Запасний завантажувач аудіо з YouTube (yt-dlp).

Основний шлях — pytubefix (`youtube_pytubefix.py`), він швидший і перевірений на
всьому архіві. Але частину роликів YouTube віддає лише клієнтам із валідним
PO-token: віддає перші ~768 КБ файлу і далі 403 — незалежно від клієнта
(ANDROID_VR/WEB/MWEB), розміру шматка й свіжості URL. Метадані при цьому
приходять нормально, тож у UI це виглядало як картка відео плюс «Помилка
завантаження». yt-dlp такі відео бере повністю й без авторизації.

Модуль викликається ТІЛЬКИ як фолбек, коли pytubefix уже впав, тому тримаємо
його вузьким: одне аудіо, без плейлистів, без постпроцесорів (конвертацію в MP3
робить наш пайплайн далі — і робить її з `creationflags`, щоб не блимало консоллю).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional


logger = logging.getLogger(__name__)


def download_audio_via_ytdlp(
    url: str,
    output_path: str,
    filename: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Завантажує найкраще аудіо і повертає шлях до файлу.

    `filename` — без розширення: його підставить yt-dlp за фактичним форматом
    (webm/m4a), як це робив pytubefix.
    """
    import yt_dlp  # lazy: важка залежність, потрібна лише на запасному шляху

    finished: Dict[str, str] = {}

    def hook(status: Dict[str, Any]) -> None:
        state = status.get('status')
        if state == 'downloading' and on_progress:
            total = status.get('total_bytes') or status.get('total_bytes_estimate') or 0
            on_progress(status.get('downloaded_bytes') or 0, total)
        elif state == 'finished':
            finished['path'] = status.get('filename', '')

    options = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_path, filename + '.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'retries': 3,
        'fragment_retries': 3,
        'progress_hooks': [hook],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = finished.get('path') or ydl.prepare_filename(info)

    if not file_path or not os.path.exists(file_path):
        raise RuntimeError("yt-dlp завершився без файлу")

    logger.info(f"[YouTube] yt-dlp завантажив: {file_path} ({os.path.getsize(file_path)} байт)")
    return file_path
