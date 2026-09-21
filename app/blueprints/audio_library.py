"""Audio library endpoints (Phase 5.7).

- POST /api/audio/download
- GET  /api/audio/downloads
- DELETE /api/audio/downloads/<id>             (soft-delete, T4.6)
- POST /api/audio/downloads/<id>/restore       (undo soft-delete, T4.6)
- POST /api/audio/check-duplicate
- POST /api/audio/open-explorer/<id>
- POST /api/audio/play/<id>
"""
import logging
import os
import subprocess
import sys
import time
import uuid

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.services import record_meta
from app.utils.youtube_id import extract_youtube_id


logger = logging.getLogger(__name__)
audio_bp = Blueprint('audio_library', __name__)


def _get_db():
    """Helper для отримання DB connection з blueprint."""
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


@audio_bp.route('/api/audio/download', methods=['POST'])
def download_audio_only():
    """Скачати аудіо з YouTube без транскрипції (з підтримкою обрізки)."""
    data = request.json
    url = data.get('url')
    quality = data.get('quality', 'best')
    start_time = data.get('start_time', None)
    end_time = data.get('end_time', None)

    if not url:
        return jsonify({"success": False, "error": "URL не вказано"}), 400
    youtube_id = extract_youtube_id(url)
    if not youtube_id:
        return jsonify({"success": False, "error": "Невірний YouTube URL"}), 400

    # Перевіряємо дублікат. T4.6: soft-deleted (deleted_at IS NOT NULL) НЕ
    # рахується дублем — юзер його прибрав, повторне завантаження має пройти
    # (youtube_pytubefix._download_youtube_core «оживляє» рядок при INSERT).
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT * FROM audio_downloads WHERE youtube_id = ? AND deleted_at IS NULL',
            (youtube_id,),
        )
        existing = c.fetchone()

    if existing and not data.get('force', False):
        return jsonify({
            "success": True,
            "status": "exists",
            "message": "Це аудіо вже завантажено",
            "download_id": existing['id'],
            "info": {
                "title": existing['title'],
                "author": existing['author'],
                "duration": existing['duration'],
                "file_path": existing['file_path'],
                "created_at": existing['created_at'],
            },
        }), 200

    download_id = str(uuid.uuid4())[:20]
    state.download_progress.set(download_id, {"status": "starting", "percent": 0})

    flask_app = current_app._get_current_object()

    def _job_runner(job, *_a, **_kw):
        from app.blueprints.youtube import _download_youtube_core
        with flask_app.app_context():
            return _download_youtube_core(url, download_id, True, quality, start_time, end_time)

    state.job_queue.submit(
        "audio_download",
        _job_runner,
        job_id=download_id,
        meta={"url": url, "save_to_library": True, "quality": quality, "trim": (start_time, end_time)},
    )
    if start_time is not None and end_time is not None:
        logger.info(f"Запуск завантаження з обрізкою: {start_time}s - {end_time}s")
    return jsonify({"success": True, "download_id": download_id, "status": "started"})


@audio_bp.route('/api/audio/downloads', methods=['GET'])
def get_audio_downloads():
    """Список скачаних аудіо з пагінацією та фільтрами.

    Query params:
    - ``page``, ``per_page`` — пагінація.
    - ``search`` — поіск по title/author.
    - ``sort_by``, ``sort_order``.
    - ``source_type`` (Phase 9.9) — фільтр: 'youtube' | 'recording' | '' (всі).
    - ``category_id`` (Phase 14) — фільтр по напрямку останнього транскрипту аудіо.

    Response додатково включає:
    - ``counts`` — {all, youtube, recording} для UI segmented control.
    - Recording-specific поля: ``source_type``, ``recording_session_id``,
      ``recording_segments``, ``recording_duration_sec``.
    """
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '')
    sort_by = request.args.get('sort_by', 'created_at')
    sort_order = request.args.get('sort_order', 'desc')
    source_type = request.args.get('source_type', '').strip().lower()
    # Напрямок: приймаємо лише числовий id (підзапит повертає INTEGER без
    # affinity, тож порівнюємо з int-параметром, не текстом).
    _cat_raw = request.args.get('category_id', '').strip()
    category_id = int(_cat_raw) if _cat_raw.isdigit() else None
    per_page = min(per_page, 100)

    # Phase 14: напрямок (category) історично живе на transcriptions. Аудіо
    # лінкується до останнього свого транскрипту через file_path, тож напрямок
    # беремо з нього. Phase 21 (recording UX): нетранскрибований запис ще не має
    # транскрипту — fallback на ВЛАСНУ audio_downloads.category_id (заповнюється з
    # напрямку, заданого при записі). COALESCE: транскрипт-напрямок > власний.
    # Цей корелований subquery використовуємо і в SELECT (показати чип), і у
    # WHERE (фільтр), і в counts. Вимагає alias `a`.
    cat_subq = (
        'COALESCE('
        '(SELECT t.category_id FROM transcriptions t WHERE t.file_path = a.file_path '
        'ORDER BY t.id DESC LIMIT 1), a.category_id)'
    )
    # Головна робота на цій сторінці — знайти те, що ще НЕ перетворене на текст.
    # Без цього фільтра нетранскрибовані губляться серед сотні вже оброблених.
    has_tr_subq = (
        'EXISTS(SELECT 1 FROM transcriptions t '
        'WHERE t.file_path = a.file_path AND t.deleted_at IS NULL)'
    )
    _tr_raw = request.args.get('transcribed', '').strip()
    transcribed = _tr_raw if _tr_raw in ('0', '1') else None

    with _get_db() as conn:
        c = conn.cursor()

        # Cross-cutting фільтри (search + напрямок) — застосовуються і до
        # counts по source_type, бо чипи джерел мають відображати ці звуження.
        # T4.6: soft-deleted (undo-вікно) ніколи не показуємо — обов'язковий
        # системний фільтр, не залежить від query-параметрів.
        base_clauses = ['a.deleted_at IS NULL']
        base_params = []
        search_param = None
        if search:
            base_clauses.append('(a.title LIKE ? OR a.author LIKE ?)')
            search_param = f'%{search}%'
            base_params.extend([search_param, search_param])
        if category_id is not None:
            base_clauses.append(f'{cat_subq} = ?')
            base_params.append(category_id)

        where_clauses = list(base_clauses)
        params = list(base_params)
        if source_type in ('youtube', 'recording', 'file'):
            where_clauses.append('a.source_type = ?')
            params.append(source_type)
        # Наявність транскрипту — окремий зріз, НЕ в base_clauses: чипи джерел
        # мають і далі показувати повні лічильники, щоб було видно, з чого саме
        # складається залишок роботи.
        if transcribed is not None:
            where_clauses.append(has_tr_subq if transcribed == '1' else f'NOT {has_tr_subq}')

        where_sql = (' WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        valid_sort_fields = ['created_at', 'title', 'author', 'duration', 'file_size']
        if sort_by not in valid_sort_fields:
            sort_by = 'created_at'
        sort_order = sort_order.lower() if sort_order.lower() in ('asc', 'desc') else 'desc'

        # Phase 10.8: окремо беремо transcription_id (latest) для кожного аудіо.
        # Лінк через file_path. Subquery в SELECT дешевий бо file_path
        # індексованим бути не мусить — у нас десятки тисяч хіба
        # transcriptions буде, не мільйони.
        query = (
            f'SELECT a.*, '
            f'(SELECT t.id FROM transcriptions t WHERE t.file_path = a.file_path '
            f'ORDER BY t.id DESC LIMIT 1) AS transcription_id, '
            # ОКРЕМИЙ аліас: a.* вже містить власну category_id (Phase 21), тож
            # не можна аліасити COALESCE теж у 'category_id' — sqlite3.Row при
            # дублі імен повертає ПЕРШУ колонку (власну), а не обчислену.
            f'{cat_subq} AS resolved_category_id '
            f'FROM audio_downloads a{where_sql} '
            f'ORDER BY {sort_by} {sort_order} LIMIT ? OFFSET ?'
        )
        c.execute(query, params + [per_page, (page - 1) * per_page])
        downloads = c.fetchall()

        # Total з урахуванням фільтрів
        c.execute(f'SELECT COUNT(*) as total FROM audio_downloads a{where_sql}', params)
        total = c.fetchone()['total']

        # Counts по source_type — для UI segmented control. Враховує
        # cross-cutting фільтри (search + напрямок), але не source_type filter
        # (бо ми хочемо знати counts по ВСІХ джерелах у межах звуження).
        base_sql = (' WHERE ' + ' AND '.join(base_clauses)) if base_clauses else ''
        c.execute(
            f'SELECT a.source_type, COUNT(*) AS n FROM audio_downloads a{base_sql} '
            f'GROUP BY a.source_type',
            base_params,
        )
        counts_by_type = {row['source_type']: row['n'] for row in c.fetchall()}

        # Скільки ще чекає на транскрипцію — у межах search/напрямку, але без
        # фільтрів source_type/transcribed, щоб число на чипі не «схлопувалось»
        # у нуль після того, як фільтр застосували.
        c.execute(
            f'SELECT COUNT(*) AS n FROM audio_downloads a{base_sql}'
            f'{" AND " if base_clauses else " WHERE "}NOT {has_tr_subq}',
            base_params,
        )
        untranscribed = c.fetchone()['n']

    counts = {
        'all': sum(counts_by_type.values()),
        'youtube': counts_by_type.get('youtube', 0),
        'recording': counts_by_type.get('recording', 0),
        'file': counts_by_type.get('file', 0),
        'untranscribed': untranscribed,
    }

    results = [{
        'id': d['id'], 'title': d['title'], 'author': d['author'],
        'description': d['description'] if 'description' in d.keys() else None,
        'duration': d['duration'], 'thumbnail_url': d['thumbnail_url'],
        'file_path': d['file_path'], 'file_size': d['file_size'],
        'audio_quality': d['audio_quality'], 'created_at': d['created_at'],
        'youtube_url': d['youtube_url'], 'view_count': d['view_count'],
        'like_count': d['like_count'],
        # Phase 9.9: recording-specific fields
        'source_type': d['source_type'] if 'source_type' in d.keys() else 'youtube',
        'recording_session_id': d['recording_session_id'] if 'recording_session_id' in d.keys() else None,
        'recording_segments': d['recording_segments'] if 'recording_segments' in d.keys() else None,
        'recording_duration_sec': d['recording_duration_sec'] if 'recording_duration_sec' in d.keys() else None,
        # Phase 10.8: ID найновішого транскрипту для цього аудіо (NULL якщо ще не транскрибовано).
        # Frontend показує "Відкрити транскрипт" замість "Транскрибувати" коли заповнено.
        'transcription_id': d['transcription_id'] if 'transcription_id' in d.keys() else None,
        # Phase 14+21: напрямок = COALESCE(транскрипт-напрямок, власний запису).
        'category_id': d['resolved_category_id'] if 'resolved_category_id' in d.keys() else None,
        # Phase 22: відеозапис (захоплення екрану). Frontend показує 📹 Відео badge.
        'has_video': d['has_video'] if 'has_video' in d.keys() else 0,
        'primary_video_path': d['primary_video_path'] if 'primary_video_path' in d.keys() else None,
    } for d in downloads]

    return jsonify({
        'downloads': results,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page,
        'counts': counts,
        'active_filter': source_type or 'all',
    })


@audio_bp.route('/api/audio/downloads/<int:download_id>', methods=['DELETE'])
def delete_audio_download(download_id):
    """Soft-delete аудіо (T4.6, REMEDIATION_PLAN Волна 2, Варіант A).

    Ставить ``deleted_at`` — файл НЕ стирається з диска одразу (P0 з аудиту:
    часто єдина копія запису). ``POST .../restore`` скасовує протягом
    grace-періоду (``RECALL_SOFTDELETE_GRACE_DAYS``); фізично прибирає
    ``app.services.retention.purge_soft_deleted`` на старті сервера.
    """
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id FROM audio_downloads WHERE id = ? AND deleted_at IS NULL',
            (download_id,),
        )
        download = c.fetchone()
        if not download:
            return jsonify({'success': False, 'error': 'Запис не знайдено'}), 404

        c.execute(
            'UPDATE audio_downloads SET deleted_at = ? WHERE id = ?',
            (time.time(), download_id),
        )
        conn.commit()
    return jsonify({'success': True, 'id': download_id})


@audio_bp.route('/api/audio/downloads/<int:download_id>', methods=['PATCH'])
def patch_audio_download(download_id):
    """Власна назва/опис картки Аудіотеки (editable-title-description-02, контракт C3).

    JSON ``{"title"?: str, "description"?: str|null}``. На відміну від PATCH
    /api/history/<id>: ``audio_downloads.title`` — ``NOT NULL`` (провенанс
    завжди мав назву), тому порожній/відсутній рядок тут — 400, а не «прибрати
    в NULL». ``description`` можна очистити (``null``/порожній рядок → NULL).
    Лише за ``id`` — не за ``youtube_id``/``file_path``.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': 'Очікується JSON-обʼєкт'}), 400
    if 'title' not in payload and 'description' not in payload:
        return jsonify({'success': False,
                        'error': 'Потрібне хоча б одне поле: title або description'}), 400

    updates: dict = {}
    if 'title' in payload:
        raw_title = payload['title']
        if not isinstance(raw_title, str) or not raw_title.strip():
            return jsonify({'success': False, 'error': 'Назва не може бути порожньою'}), 400
        try:
            updates['title'] = record_meta.normalize_title(raw_title)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
    if 'description' in payload:
        try:
            updates['description'] = record_meta.normalize_description(payload['description'])
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute(
            'SELECT id, title, description FROM audio_downloads '
            'WHERE id = ? AND deleted_at IS NULL',
            (download_id,),
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Запис не знайдено'}), 404

        if updates:
            assignments = ', '.join(f'{field} = ?' for field in updates)
            c.execute(
                f'UPDATE audio_downloads SET {assignments} WHERE id = ?',
                [*updates.values(), download_id],
            )
            conn.commit()

        item = c.execute(
            'SELECT * FROM audio_downloads WHERE id = ?', (download_id,),
        ).fetchone()

    return jsonify({'success': True, 'item': dict(item)})


@audio_bp.route('/api/audio/downloads/<int:download_id>/restore', methods=['POST'])
def restore_audio_download(download_id):
    """Скасувати soft-delete (undo). 404 якщо запису нема або він не видалений."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id FROM audio_downloads WHERE id = ? AND deleted_at IS NOT NULL',
            (download_id,),
        )
        if not c.fetchone():
            return jsonify({'success': False, 'error': 'Немає що відновлювати'}), 404
        c.execute(
            'UPDATE audio_downloads SET deleted_at = NULL WHERE id = ?',
            (download_id,),
        )
        conn.commit()
    return jsonify({'success': True, 'id': download_id})


@audio_bp.route('/api/audio/check-duplicate', methods=['POST'])
def check_duplicate_audio():
    """Перевірити чи вже скачано це YouTube відео."""
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'success': False, 'error': 'URL не вказано'}), 400
    youtube_id = extract_youtube_id(url)
    if not youtube_id:
        return jsonify({'success': True, 'is_duplicate': False})

    with _get_db() as conn:
        c = conn.cursor()
        # T4.6: soft-deleted не рахується дублем (undo-вікно ≠ видимий запис).
        c.execute(
            'SELECT id, title, author, created_at FROM audio_downloads '
            'WHERE youtube_id = ? AND deleted_at IS NULL',
            (youtube_id,),
        )
        existing = c.fetchone()

    if existing:
        return jsonify({
            'is_duplicate': True,
            'download_info': {
                'id': existing['id'],
                'title': existing['title'],
                'author': existing['author'],
                'created_at': existing['created_at'],
            },
        })
    return jsonify({'is_duplicate': False})


@audio_bp.route('/api/audio/open-explorer/<int:audio_id>', methods=['POST'])
def open_audio_in_explorer(audio_id):
    """Відкрити файл у провіднику Windows."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT file_path FROM audio_downloads WHERE id = ? AND deleted_at IS NULL',
            (audio_id,),
        )
        audio = c.fetchone()
    if not audio:
        return jsonify({'success': False, 'error': 'Аудіо не знайдено'}), 404
    file_path = audio['file_path']
    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': 'Файл не знайдено на диску'}), 404

    try:
        if os.name == 'nt':
            subprocess.Popen(['explorer', '/select,', os.path.abspath(file_path)])
        else:
            folder = os.path.dirname(file_path)
            subprocess.Popen(['xdg-open', folder])
        logger.info(f"Opened in explorer: {file_path}")
        return jsonify({'success': True, 'message': 'Відкрито в провіднику'})
    except Exception as e:
        logger.error(f"Error opening in explorer: {e}")
        return jsonify({'success': False, 'error': 'Не вдалося відкрити провідник'}), 500


@audio_bp.route('/api/audio/play/<int:audio_id>', methods=['POST'])
def play_audio_locally(audio_id):
    """Відтворити аудіо у системному плеєрі."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT file_path, title FROM audio_downloads WHERE id = ? AND deleted_at IS NULL',
            (audio_id,),
        )
        audio = c.fetchone()
    if not audio:
        return jsonify({'success': False, 'error': 'Аудіо не знайдено'}), 404
    file_path = audio['file_path']
    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': 'Файл не знайдено на диску'}), 404
    try:
        if os.name == 'nt':
            os.startfile(file_path)
        else:
            opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.Popen([opener, file_path])
        logger.info(f"Playing audio: {audio['title']}")
        return jsonify({'success': True, 'message': f"Відтворення: {audio['title']}"})
    except Exception as e:
        logger.error(f"Error playing audio: {e}")
        return jsonify({'success': False, 'error': 'Не вдалося відтворити аудіо'}), 500
