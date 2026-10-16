"""YouTube downloader (pytubefix backend).

CRITICAL: this module wraps the `_download_youtube_core` flow from app.py
WITHOUT changing behaviour. pytubefix remains the primary backend
(stability contract in ROADMAP.md section 0).

Dependencies are injected through parameters:
- download_progress_set(id, data): callback for status updates.
- add_log(id, stage, message, progress, status): callback for logs.
- get_db_conn(): context manager for the DB (for save_to_library).

This makes the function testable without importing app.py.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any, Callable, Optional

from app.core.i18n import tr
from app.services.youtube_ytdlp import download_audio_via_ytdlp
from app.utils.audio import trim_audio_file
from app.utils.proc import NO_WINDOW


logger = logging.getLogger(__name__)


# Quality → bitrate mapping (kbps)
_QUALITY_BITRATE = {
    'best': '320', 'high': '256', 'medium': '192', 'low': '128',
}


# pytubefix client order. The default ANDROID_VR needs no Node.js and handles
# most videos; for some videos YouTube serves videoDetails only to clients
# with a PO-token (the WEB family - botGuard generates the token and needs Node.js),
# otherwise BotDetection is raised. Try them in turn: the first one that opens wins.
#
# MWEB goes before WEB for a reason: on the same videos WEB serves SABR streams,
# which can only be downloaded through ServerAbrStream with a session PO-token. botGuard gives a
# token bound to video_id, so SABR dies on "PoToken PENDING" - the metadata
# has already been fetched by then, and the user sees the video card and a download error.
# On the same video MWEB serves ordinary URL streams of the same bitrate.
YOUTUBE_CLIENTS = ('ANDROID_VR', 'MWEB', 'WEB', 'WEB_SAFARI')


def open_youtube(url: str, **kwargs: Any):
    """Create a pytubefix.YouTube, trying clients until the first working one.

    Accessing `yt.title` triggers check_availability - that is where pytubefix
    raises BotDetection/VideoUnavailable, so a client counts as working only
    after it. If none passes, the last error is raised.
    """
    from pytubefix import YouTube  # lazy

    last_exc: Optional[BaseException] = None
    for client in YOUTUBE_CLIENTS:
        try:
            yt = YouTube(url, client=client, **kwargs)
            _ = yt.title
        except Exception as exc:
            logger.warning(f"[YouTube] Client {client} failed to open {url}: {type(exc).__name__}: {exc}")
            last_exc = exc
            continue
        if client != YOUTUBE_CLIENTS[0]:
            logger.info(f"[YouTube] Opened with client {client} (default did not pass)")
        return yt

    raise last_exc if last_exc else RuntimeError("Could not open the video")


def _classify_error(error_msg: str, default: str | None = None) -> str:
    """Turn a technical pytubefix message into one the user can understand."""
    if default is None:
        default = tr("svc_media.yt.err.default")
    low = error_msg.lower()
    if "detected as a bot" in low or "bot detection" in low:
        return tr("svc_media.yt.err.bot")
    if "sign in" in low:
        return tr("svc_media.yt.err.auth")
    if "unavailable" in low:
        return tr("svc_media.yt.err.unavailable")
    if "private" in low:
        return tr("svc_media.yt.err.private")
    if "copyright" in low:
        return tr("svc_media.yt.err.copyright")
    return default


def download_youtube_audio(
    url: str,
    download_id: str,
    *,
    save_to_library: bool = False,
    quality: str = 'best',
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    youtube_folder: str,
    download_progress_set: Callable[[str, dict], None],
    add_log: Callable[[str, str, str, Optional[int], str], None],
    get_db_conn: Callable[[], Any],
) -> None:
    """Download audio from YouTube via pytubefix.

    Side effects:
    - download_progress_set(download_id, {...status...}) on every state update.
    - add_log(download_id, stage, message, progress, status) for process logs.
    - file_save → youtube_folder/<id>_<title>.mp3
    - if save_to_library: INSERT into audio_downloads via get_db_conn().

    Args:
        url:                  YouTube URL.
        download_id:          UUID for tracking.
        save_to_library:      save into the audio_downloads table.
        quality:              best|high|medium|low.
        start_time, end_time: trim in seconds (optional).
        youtube_folder:       folder to save into (usually app.config['YOUTUBE_FOLDER']).
        download_progress_set: callback for status.
        add_log:              callback for process logging.
        get_db_conn:          context-manager factory for the DB.
    """
    logger.info(f"[YouTube] Starting download: {url} (save_to_library={save_to_library})")

    # Trim parameters
    trim_params = None
    if start_time is not None and end_time is not None:
        trim_params = {"start": start_time, "end": end_time}
        logger.info(f"[YouTube] Trim after download: {start_time}s - {end_time}s")
        add_log(download_id, "init",
                tr("svc_media.yt.log.trim_planned", start=f"{start_time:.1f}", end=f"{end_time:.1f}"), 0, "processing")

    bitrate = _QUALITY_BITRATE.get(quality, '192')

    def progress_callback(stream, chunk, bytes_remaining):
        total_size = stream.filesize
        bytes_downloaded = total_size - bytes_remaining
        percent = (bytes_downloaded / total_size) * 100
        download_progress_set(download_id, {
            "status": "downloading",
            "percent": round(percent, 2),
            "speed": "N/A",
            "eta": "N/A",
        })
        if int(percent) % 10 == 0:
            logger.info(f"[YouTube] Download: {percent:.1f}%")

    def complete_callback(stream, file_path):
        download_progress_set(download_id, {"status": "processing", "percent": 100})
        logger.info(f"[YouTube] Download finished: {file_path}")

    safe_filename = "".join(c for c in download_id if c.isalnum() or c in ('_', '-'))
    add_log(download_id, "download", tr("svc_media.yt.log.start"), 5, "processing")

    try:
        yt = open_youtube(
            url,
            on_progress_callback=progress_callback,
            on_complete_callback=complete_callback,
        )

        title = yt.title
        author = yt.author
        duration = yt.length
        thumbnail = yt.thumbnail_url
        video_id = yt.video_id

        logger.info(f"[YouTube] Info received: {title}")

        audio_streams = yt.streams.filter(only_audio=True).order_by('abr').desc()
        # A SABR stream can only be downloaded through ServerAbrStream and needs a session
        # PO-token, which botGuard does not provide - so take an ordinary stream even if it is
        # slightly worse in bitrate. SABR stays the last resort, not the first.
        audio_stream = next(
            (s for s in audio_streams if not getattr(s, 'is_sabr', False)),
            None,
        ) or audio_streams.first()
        if not audio_stream:
            raise Exception("Could not find an audio stream")

        if getattr(audio_stream, 'is_sabr', False):
            logger.warning("[YouTube] All audio streams are SABR - the download may fail on the PO-token")
        logger.info(f"[YouTube] Selected stream: {audio_stream.mime_type} - {audio_stream.abr}")

        temp_filename = f"{safe_filename}_{title[:50]}"
        temp_filename = "".join(c for c in temp_filename if c.isalnum() or c in ('_', '-', ' '))

        try:
            downloaded_file = audio_stream.download(
                output_path=youtube_folder,
                filename=temp_filename,
            )
        except Exception as exc:
            # YouTube serves some videos only to clients with a valid PO-token:
            # it lets through the first ~768 KB of the file and then 403s - whatever the client,
            # chunk size or URL freshness. yt-dlp handles such videos.
            logger.warning(
                f"[YouTube] pytubefix failed to download ({type(exc).__name__}: {exc}) - trying yt-dlp"
            )
            add_log(download_id, "download",
                    tr("svc_media.yt.log.fallback"),
                    10, "processing")

            def ytdlp_progress(done: int, total: int) -> None:
                percent = (done / total) * 100 if total else 0
                download_progress_set(download_id, {
                    "status": "downloading",
                    "percent": round(percent, 2),
                    "speed": "N/A",
                    "eta": "N/A",
                })

            downloaded_file = download_audio_via_ytdlp(
                url, youtube_folder, temp_filename, on_progress=ytdlp_progress,
            )
            complete_callback(audio_stream, downloaded_file)

        logger.info(f"[YouTube] File downloaded: {downloaded_file}")

        # Convert to MP3
        add_log(download_id, "download", tr("svc_media.yt.log.convert"), 30, "processing")
        mp3_file = os.path.splitext(downloaded_file)[0] + ".mp3"

        try:
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-i', downloaded_file,
                '-vn', '-acodec', 'libmp3lame', '-ab', f'{bitrate}k',
                mp3_file,
            ]
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300,
                creationflags=NO_WINDOW,
            )
            if result.returncode == 0:
                if os.path.exists(downloaded_file) and downloaded_file != mp3_file:
                    os.remove(downloaded_file)
                downloaded_file = mp3_file
                logger.info(f"[YouTube] Converted to MP3: {mp3_file}")
            else:
                logger.warning(f"[YouTube] FFmpeg error: {result.stderr}")
        except Exception as conv_error:
            logger.warning(f"[YouTube] Conversion error: {conv_error}")

        if downloaded_file and os.path.exists(downloaded_file):
            file_size = os.path.getsize(downloaded_file)
            file_size_mb = file_size / (1024 * 1024)
            logger.info(f"[YouTube] File downloaded successfully: {file_size_mb:.2f} MB")
            add_log(download_id, "download",
                    tr("svc_media.yt.log.done_size", size=f"{file_size_mb:.1f}"), 40, "processing")

            # Trim if needed
            final_file = downloaded_file
            if trim_params:
                add_log(download_id, "trim", tr("svc_media.yt.log.trim_start"), 45, "processing")
                base_name = os.path.splitext(downloaded_file)[0]
                trimmed_file = f"{base_name}_trimmed.mp3"

                # add_log for trim has the signature (stage, message, progress, status)
                # - wrap it to bind download_id.
                def _trim_log(stage, message, progress=None, status="processing"):
                    add_log(download_id, stage, message, progress, status)

                trim_success = trim_audio_file(
                    downloaded_file, trimmed_file,
                    trim_params["start"], trim_params["end"],
                    add_log=_trim_log,
                )
                if trim_success:
                    try:
                        os.remove(downloaded_file)
                        logger.info(f"[YouTube] Original file removed: {downloaded_file}")
                    except Exception as e:
                        logger.warning(f"[YouTube] Could not remove the original: {e}")
                    final_file = trimmed_file
                    file_size = os.path.getsize(final_file)
                    add_log(download_id, "trim", tr("svc_media.yt.log.trim_ok"), 50, "processing")
                else:
                    logger.warning("[YouTube] Trim failed, using the full file")
                    add_log(download_id, "trim",
                            tr("svc_media.yt.log.trim_fail"),
                            50, "warning")

            # Save to the library
            download_db_id = None
            if save_to_library:
                with get_db_conn() as conn:
                    c = conn.cursor()
                    c.execute(
                        'SELECT id, deleted_at FROM audio_downloads WHERE youtube_id = ?',
                        (video_id,),
                    )
                    existing = c.fetchone()
                    if not existing:
                        c.execute('''
                            INSERT INTO audio_downloads
                            (youtube_url, youtube_id, title, author, duration, thumbnail_url,
                             file_path, file_size, audio_quality, audio_format, download_time,
                             view_count, like_count, description, upload_date, tags)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            url, video_id, title, author, duration, thumbnail,
                            final_file, file_size, bitrate, 'mp3', time.time(),
                            0, 0, '', '', '[]',
                        ))
                        conn.commit()
                        download_db_id = c.lastrowid
                    elif existing['deleted_at'] is not None:
                        # T4.6: the row was soft-deleted earlier - youtube_id is UNIQUE,
                        # so a repeated INSERT would fail on the constraint. "Revive"
                        # the same row instead of a new one (the undo window has already expired
                        # for the previous deletion - treated as a new download).
                        c.execute('''
                            UPDATE audio_downloads
                            SET deleted_at = NULL, youtube_url = ?, title = ?, author = ?,
                                duration = ?, thumbnail_url = ?, file_path = ?, file_size = ?,
                                audio_quality = ?, audio_format = 'mp3', download_time = ?
                            WHERE id = ?
                        ''', (
                            url, title, author, duration, thumbnail,
                            final_file, file_size, bitrate, time.time(),
                            existing['id'],
                        ))
                        conn.commit()
                        download_db_id = existing['id']
                    else:
                        download_db_id = existing['id']

            progress_data = {
                "status": "completed",
                "percent": 100,
                "file_path": final_file,
                "info": {
                    "title": title,
                    "author": author,
                    "duration": duration,
                    "thumbnail": thumbnail,
                    "video_id": video_id,
                    "original_url": url,
                    "trimmed": trim_params is not None,
                },
            }
            if save_to_library:
                progress_data["database_id"] = download_db_id
                progress_data["info"]["file_size"] = file_size
                progress_data["info"]["quality"] = bitrate

            download_progress_set(download_id, progress_data)
            add_log(download_id, "complete", tr("svc_media.yt.log.complete"), 100, "completed")
            logger.info(f"[YouTube] Downloaded successfully: {title}")
        else:
            error_msg = tr("svc_media.yt.err.no_file")
            logger.error(f"[YouTube] Error: {error_msg}")
            try:
                logger.debug(f"[YouTube] Files in the folder: {os.listdir(youtube_folder)}")
            except OSError:
                pass
            download_progress_set(download_id, {"status": "error", "error": error_msg})

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[YouTube] Exception: {error_msg}", exc_info=True)
        user_msg = _classify_error(error_msg)
        download_progress_set(download_id, {"status": "error", "error": user_msg})
