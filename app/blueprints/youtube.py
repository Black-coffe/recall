"""YouTube endpoints (Phase 5.6).

- POST /api/youtube/info       (metadata by URL)
- POST /api/youtube/download   (start the download in the background)
- GET  /api/youtube/progress/<download_id>
"""
import logging
import uuid

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.core.i18n import tr
from app.services.youtube_pytubefix import _classify_error, download_youtube_audio, open_youtube
from app.utils.youtube_id import extract_youtube_id


logger = logging.getLogger(__name__)
youtube_bp = Blueprint('youtube', __name__)


# add_log is borrowed from state - wired up in app.py during init


@youtube_bp.route('/api/youtube/info', methods=['POST'])
def get_youtube_info():
    """Get YouTube video metadata."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error_code": "INVALID_ARGUMENT",
                        "error": tr('capture.yt.err.json_object')}), 400
    url = data.get('url')
    if not url:
        return jsonify({"success": False, "error": tr('capture.yt.err.no_url')}), 400
    if not extract_youtube_id(url):
        return jsonify({"success": False, "error": tr('capture.yt.err.bad_url')}), 400

    try:
        yt = open_youtube(url)
        title = yt.title or tr('capture.yt.unknown_video')
        author = yt.author or tr('capture.yt.unknown_author')
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
        logger.error(f"YouTube info error for {url}: {type(e).__name__}: {e}")
        return jsonify({"success": False, "error": _classify_error(str(e), default=tr('capture.yt.err.info'))}), 500


def _download_youtube_core(url, download_id, save_to_library=False, quality='best',
                            start_time=None, end_time=None):
    """Adapter to app.services.youtube_pytubefix.download_youtube_audio."""
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
    """Download audio from YouTube (in the background via job_queue)."""
    # The rate limit is applied via limiter.limit() at registration time
    # - register the decorator explicitly in the factory or via limiter.shared_limit
    data = request.json
    url = data.get('url')
    start_time = data.get('start_time', None)
    end_time = data.get('end_time', None)

    if not url:
        return jsonify({"success": False, "error": tr('capture.yt.err.no_url')}), 400
    if not extract_youtube_id(url):
        return jsonify({"success": False, "error": tr('capture.yt.err.bad_url')}), 400
    if not state.ffmpeg_available:
        return jsonify({"success": False, "error": tr('capture.yt.err.no_ffmpeg')}), 500

    download_id = str(uuid.uuid4())[:20]
    state.download_progress.set(download_id, {"status": "starting", "percent": 0})
    logger.info(f"Created a new download with ID: {download_id}")

    # Capture current_app before submit - there is no request context in the background thread.
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
        logger.info(f"Download started in the background")
        if start_time is not None and end_time is not None:
            logger.info(f"Trim: {start_time}s - {end_time}s")
        return jsonify({"success": True, "download_id": download_id})
    except Exception as e:
        logger.error(f"[API] Failed to start the download: {e}")
        state.download_progress.set(download_id, {"status": "error", "error": tr('capture.yt.err.start')})
        return jsonify({"success": False, "error": tr('capture.yt.err.start')}), 500


@youtube_bp.route('/api/youtube/progress/<download_id>')
def get_download_progress(download_id):
    """Get the download progress."""
    progress = state.download_progress.get(download_id, {"status": "not_found"})
    return jsonify(progress)
