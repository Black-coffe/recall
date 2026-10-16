"""Fallback YouTube audio downloader (yt-dlp).

The primary path is pytubefix (`youtube_pytubefix.py`): it is faster and proven on
the whole archive. But YouTube serves some videos only to clients with a valid
PO-token: it serves the first ~768 KB of the file and then 403s - regardless of the client
(ANDROID_VR/WEB/MWEB), chunk size or URL freshness. The metadata still
arrives fine, so in the UI this looked like a video card plus a "Download
error". yt-dlp downloads such videos in full and without authentication.

This module is called ONLY as a fallback, when pytubefix has already failed, so we keep
it narrow: one audio track, no playlists, no postprocessors (the MP3 conversion
is done by our pipeline afterwards - with `creationflags`, so the console does not flash).
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
    """Download the best audio and return the path to the file.

    `filename` is without an extension: yt-dlp adds it according to the actual format
    (webm/m4a), as pytubefix did.
    """
    import yt_dlp  # lazy: heavy dependency, needed only on the fallback path

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
        raise RuntimeError("yt-dlp finished without a file")

    logger.info(f"[YouTube] yt-dlp downloaded: {file_path} ({os.path.getsize(file_path)} bytes)")
    return file_path
