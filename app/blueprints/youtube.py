"""YouTube endpoints (Phase 5.6).

- POST /api/youtube/info       (метадані по URL)
- POST /api/youtube/download   (запуск скачування у background)
- GET  /api/youtube/progress/<download_id>
"""
import logging
import uuid

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.services.youtube_pytubefix import download_youtube_audio
from app.utils.youtube_id import extract_youtube_id


logger = logging.getLogger(__name__)
youtube_bp = Blueprint('youtube', __name__)


# add_log запозичується з state — підв'язується в app.py під час init


@youtube_bp.route('/api/youtube/info', methods=['POST'])
def get_youtube_info():
    """Отримати метадані YouTube відео."""
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"success": False, "error": "URL не вказано"}), 400
    if not extract_youtube_id(url):
        return jsonify({"success": False, "error": "Невірний YouTube URL"}), 400

    try:
        from pytubefix import YouTube
        yt = YouTube(url)
        title = yt.title or 'Невідоме відео'
        author = yt.author or 'Невідомий автор'
        duration = yt.length or 0
        thumbnail = yt.thumbnail_url or ''
        video_id = yt.video_id or ''
        description = yt.description or ''
        if description:
            description = (description[:200] + '...') if len(description) > 200 else description
        return jsonify({
            "success": True,
            "title": title,
            "author": author,
            "duration": duration,
            "thumbnail": thumbnail,
            "video_id": video_id,
            "description": description,
        })
    except Exception as e:
        logger.error(f"YouTube info error for {url}: {e}")
        return jsonify({"success": False, "error": "Помилка отримання інформації про відео"}), 500


def _download_youtube_core(url, download_id, save_to_library=False, quality='best',
                            start_time=None, end_time=None):
    """Адаптер до app.services.youtube_pytubefix.download_youtube_audio."""
    from app.db.connection import get_db_connection

    def get_db_conn():
        return get_db_connection(current_app.config['DATABASE'])

    return download_youtube_audio(
        url=url,
        download_id=download_id,
        save_to_library=save_to_library,
        quality=quality,
        start_time=start_time,
        end_time=end_time,
        youtube_folder=current_app.config['YOUTUBE_FOLDER'],
        download_progress_set=state.download_progress.set,
        add_log=state.add_log,
        get_db_conn=get_db_conn,
    )


@youtube_bp.route('/api/youtube/download', methods=['POST'])
def download_youtube():
    """Завантажити аудіо з YouTube (background через job_queue)."""
    # Rate limit застосовується через limiter.limit() в register-моменті
    # — реєструємо decorator явно у factory або через limiter.shared_limit
    data = request.json
    url = data.get('url')
    start_time = data.get('start_time', None)
    end_time = data.get('end_time', None)

    if not url:
        return jsonify({"success": False, "error": "URL не вказано"}), 400
    if not extract_youtube_id(url):
        return jsonify({"success": False, "error": "Невірний YouTube URL"}), 400
    if not state.ffmpeg_available:
        return jsonify({"success": False, "error": "FFmpeg не встановлено."}), 500

    download_id = str(uuid.uuid4())[:20]
    state.download_progress.set(download_id, {"status": "starting", "percent": 0})
    logger.info(f"Створено нове завантаження з ID: {download_id}")

    # Захоплюємо current_app до submit — у фоновому потоці немає request context.
    flask_app = current_app._get_current_object()

    try:
        def _job_runner(job, *_a, **_kw):
            with flask_app.app_context():
                return _download_youtube_core(url, download_id, False, 'best', start_time, end_time)

        state.job_queue.submit(
            "youtube_download",
            _job_runner,
            job_id=download_id,
            meta={"url": url, "save_to_library": False, "trim": (start_time, end_time)},
        )
        logger.info(f"Завантаження запущено в фоновому режимі")
        if start_time is not None and end_time is not None:
            logger.info(f"Обрізка: {start_time}s - {end_time}s")
        return jsonify({"success": True, "download_id": download_id})
    except Exception as e:
        logger.error(f"[API] Помилка запуску завантаження: {e}")
        state.download_progress.set(download_id, {"status": "error", "error": "Помилка запуску завантаження"})
        return jsonify({"success": False, "error": "Помилка запуску завантаження"}), 500


@youtube_bp.route('/api/youtube/progress/<download_id>')
def get_download_progress(download_id):
    """Отримати прогрес завантаження."""
    progress = state.download_progress.get(download_id, {"status": "not_found"})
    return jsonify(progress)
