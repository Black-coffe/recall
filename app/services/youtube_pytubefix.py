"""YouTube downloader (pytubefix backend).

КРИТИЧНО: цей модуль обгортає flow `_download_youtube_core` з app.py
БЕЗ зміни поведінки. pytubefix залишається основним backend'ом
(контракт стабільності в ROADMAP.md розділ 0).

Залежності інжектяться через параметри:
- download_progress_set(id, data): callback для оновлення статусу.
- add_log(id, stage, message, progress, status): callback для логів.
- get_db_conn(): context manager для БД (для save_to_library).

Це робить функцію тестабельною без імпорту app.py.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any, Callable, Optional

from app.services.youtube_ytdlp import download_audio_via_ytdlp
from app.utils.audio import trim_audio_file
from app.utils.proc import NO_WINDOW


logger = logging.getLogger(__name__)


# Quality → bitrate mapping (kbps)
_QUALITY_BITRATE = {
    'best': '320', 'high': '256', 'medium': '192', 'low': '128',
}


# Порядок клієнтів pytubefix. Дефолтний ANDROID_VR не потребує Node.js і бере
# більшість відео; для частини роликів YouTube віддає videoDetails лише клієнтам
# із PO-token (WEB-сімейство — токен генерує botGuard, для нього потрібен Node.js),
# інакше падає BotDetection. Перебираємо по черзі: перший, що відкрився, — той і йде далі.
#
# MWEB стоїть перед WEB не за красою: WEB на тих самих відео віддає SABR-потоки,
# а вони качаються лише через ServerAbrStream із сесійним PO-token. botGuard дає
# токен, привʼязаний до video_id, тож SABR помирає на «PoToken PENDING» — метадані
# при цьому вже отримані, і користувач бачить картку відео та помилку завантаження.
# MWEB на тому ж відео віддає звичайні URL-потоки тієї ж бітності.
YOUTUBE_CLIENTS = ('ANDROID_VR', 'MWEB', 'WEB', 'WEB_SAFARI')


def open_youtube(url: str, **kwargs: Any):
    """Створює pytubefix.YouTube, перебираючи клієнтів до першого робочого.

    Звернення до `yt.title` тригерить check_availability — саме там pytubefix
    кидає BotDetection/VideoUnavailable, тож клієнт вважається робочим лише
    після нього. Якщо не пройшов жоден — піднімаємо помилку останнього.
    """
    from pytubefix import YouTube  # lazy

    last_exc: Optional[BaseException] = None
    for client in YOUTUBE_CLIENTS:
        try:
            yt = YouTube(url, client=client, **kwargs)
            _ = yt.title
        except Exception as exc:
            logger.warning(f"[YouTube] Клієнт {client} не відкрив {url}: {type(exc).__name__}: {exc}")
            last_exc = exc
            continue
        if client != YOUTUBE_CLIENTS[0]:
            logger.info(f"[YouTube] Відкрито клієнтом {client} (дефолтний не пройшов)")
        return yt

    raise last_exc if last_exc else RuntimeError("Не вдалося відкрити відео")


def _classify_error(error_msg: str, default: str = "Помилка завантаження відео") -> str:
    """Перетворює технічне повідомлення pytubefix на зрозуміле користувачу."""
    low = error_msg.lower()
    if "detected as a bot" in low or "bot detection" in low:
        return "YouTube заблокував запит як автоматичний (bot detection)"
    if "sign in" in low:
        return "YouTube вимагає авторизації"
    if "unavailable" in low:
        return "Відео недоступне або видалене"
    if "private" in low:
        return "Приватне відео"
    if "copyright" in low:
        return "Відео заблоковано через авторські права"
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
    """Завантажує аудіо з YouTube через pytubefix.

    Сторонні ефекти:
    - download_progress_set(download_id, {...status...}) щоразу при оновленні стану.
    - add_log(download_id, stage, message, progress, status) для process logs.
    - file_save → youtube_folder/<id>_<title>.mp3
    - якщо save_to_library: INSERT у audio_downloads через get_db_conn().

    Args:
        url:                  YouTube URL.
        download_id:          UUID для відстеження.
        save_to_library:      зберегти в audio_downloads таблицю.
        quality:              best|high|medium|low.
        start_time, end_time: обрізка в секундах (опц.).
        youtube_folder:       папка куди зберігати (зазвичай app.config['YOUTUBE_FOLDER']).
        download_progress_set: callback для статусу.
        add_log:              callback для логування процесу.
        get_db_conn:          context manager-factory для БД.
    """
    logger.info(f"[YouTube] Початок завантаження: {url} (save_to_library={save_to_library})")

    # Параметри обрізки
    trim_params = None
    if start_time is not None and end_time is not None:
        trim_params = {"start": start_time, "end": end_time}
        logger.info(f"[YouTube] Обрізка після завантаження: {start_time}s - {end_time}s")
        add_log(download_id, "init",
                f"Запланована обрізка: {start_time:.1f}s - {end_time:.1f}s", 0, "processing")

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
            logger.info(f"[YouTube] Завантаження: {percent:.1f}%")

    def complete_callback(stream, file_path):
        download_progress_set(download_id, {"status": "processing", "percent": 100})
        logger.info(f"[YouTube] Завантаження завершено: {file_path}")

    safe_filename = "".join(c for c in download_id if c.isalnum() or c in ('_', '-'))
    add_log(download_id, "download", "Початок завантаження відео з YouTube...", 5, "processing")

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

        logger.info(f"[YouTube] Інформація отримана: {title}")

        audio_streams = yt.streams.filter(only_audio=True).order_by('abr').desc()
        # SABR-потік качається лише через ServerAbrStream і потребує сесійного
        # PO-token, якого botGuard не дає — тому беремо звичайний, навіть якщо він
        # трохи гірший за бітністю. SABR лишається останнім шансом, а не першим.
        audio_stream = next(
            (s for s in audio_streams if not getattr(s, 'is_sabr', False)),
            None,
        ) or audio_streams.first()
        if not audio_stream:
            raise Exception("Не вдалося знайти аудіо потік")

        if getattr(audio_stream, 'is_sabr', False):
            logger.warning("[YouTube] Усі аудіо-потоки SABR — завантаження може впасти на PO-token")
        logger.info(f"[YouTube] Обраний потік: {audio_stream.mime_type} - {audio_stream.abr}")

        temp_filename = f"{safe_filename}_{title[:50]}"
        temp_filename = "".join(c for c in temp_filename if c.isalnum() or c in ('_', '-', ' '))

        try:
            downloaded_file = audio_stream.download(
                output_path=youtube_folder,
                filename=temp_filename,
            )
        except Exception as exc:
            # Частину відео YouTube віддає лише клієнтам із валідним PO-token:
            # пускає перші ~768 КБ файлу і далі 403 — хоч би яким був клієнт,
            # розмір шматка чи свіжість URL. Такі ролики бере yt-dlp.
            logger.warning(
                f"[YouTube] pytubefix не завантажив ({type(exc).__name__}: {exc}) — пробуємо yt-dlp"
            )
            add_log(download_id, "download",
                    "YouTube не віддав потік напряму — пробуємо запасний завантажувач...",
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

        logger.info(f"[YouTube] Файл завантажено: {downloaded_file}")

        # Конвертація в MP3
        add_log(download_id, "download", "Конвертація в MP3...", 30, "processing")
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
                logger.info(f"[YouTube] Конвертовано в MP3: {mp3_file}")
            else:
                logger.warning(f"[YouTube] FFmpeg помилка: {result.stderr}")
        except Exception as conv_error:
            logger.warning(f"[YouTube] Помилка конвертації: {conv_error}")

        if downloaded_file and os.path.exists(downloaded_file):
            file_size = os.path.getsize(downloaded_file)
            file_size_mb = file_size / (1024 * 1024)
            logger.info(f"[YouTube] Файл успішно завантажено: {file_size_mb:.2f} MB")
            add_log(download_id, "download",
                    f"Завантаження завершено. Розмір: {file_size_mb:.1f} MB", 40, "processing")

            # Обрізка якщо потрібно
            final_file = downloaded_file
            if trim_params:
                add_log(download_id, "trim", "Початок обрізки аудіо...", 45, "processing")
                base_name = os.path.splitext(downloaded_file)[0]
                trimmed_file = f"{base_name}_trimmed.mp3"

                # add_log для trim має сигнатуру (stage, message, progress, status)
                # — обгортаємо щоб під'єднати download_id.
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
                        logger.info(f"[YouTube] Видалено оригінальний файл: {downloaded_file}")
                    except Exception as e:
                        logger.warning(f"[YouTube] Не вдалося видалити оригінал: {e}")
                    final_file = trimmed_file
                    file_size = os.path.getsize(final_file)
                    add_log(download_id, "trim", "Обрізка завершена успішно", 50, "processing")
                else:
                    logger.warning("[YouTube] Обрізка не вдалась, використовуємо повний файл")
                    add_log(download_id, "trim",
                            "Обрізка не вдалась, використовується повний файл",
                            50, "warning")

            # Збереження в бібліотеку
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
                        # T4.6: рядок soft-deleted раніше — youtube_id УНІКАЛЬНИЙ,
                        # тож повторний INSERT впаде на constraint. «Оживляємо»
                        # той самий рядок замість нового (undo-вікно вже спливло
                        # для попереднього видалення — трактуємо як нове завантаження).
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
            add_log(download_id, "complete", "Процес завершено успішно", 100, "completed")
            logger.info(f"[YouTube] Успішно завантажено: {title}")
        else:
            error_msg = "Файл не знайдено після завантаження"
            logger.error(f"[YouTube] Помилка: {error_msg}")
            try:
                logger.debug(f"[YouTube] Файли в папці: {os.listdir(youtube_folder)}")
            except OSError:
                pass
            download_progress_set(download_id, {"status": "error", "error": error_msg})

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[YouTube] Exception: {error_msg}", exc_info=True)
        user_msg = _classify_error(error_msg)
        download_progress_set(download_id, {"status": "error", "error": user_msg})
