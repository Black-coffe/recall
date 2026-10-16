"""Audio library endpoints (Phase 5.7).

- POST /api/audio/download
- GET  /api/audio/downloads
- DELETE /api/audio/downloads/<id>             (soft-delete, T4.6)
- POST /api/audio/downloads/<id>/restore       (undo soft-delete, T4.6)
- POST /api/audio/check-duplicate
- POST /api/audio/open-explorer/<id>
- POST /api/audio/play/<id>
"""
import json
import logging
import os
import subprocess
import sys
import time
import uuid

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.core.i18n import tr
from app.services import categories as cat_svc
from app.services import record_meta
from app.utils.youtube_id import extract_youtube_id


logger = logging.getLogger(__name__)
audio_bp = Blueprint('audio_library', __name__)


def _get_db():
    """Helper to get a DB connection from the blueprint."""
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


def _parse_youtube_tags(raw) -> list:
    """`audio_downloads.tags` (legacy JSON column of YouTube tags) → list.

    For now it is always written as `'[]'` on download (tag extraction is not
    wired up) - an empty list here is honest, not a stub.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("[audio_library] audio_downloads.tags is not JSON - ignoring")
        return []
    return data if isinstance(data, list) else []


@audio_bp.route('/api/audio/download', methods=['POST'])
def download_audio_only():
    """Download audio from YouTube without transcription (with trim support)."""
    data = request.json
    url = data.get('url')
    quality = data.get('quality', 'best')
    start_time = data.get('start_time', None)
    end_time = data.get('end_time', None)

    if not url:
        return jsonify({"success": False, "error": tr('library.audio.err.no_url')}), 400
    youtube_id = extract_youtube_id(url)
    if not youtube_id:
        return jsonify({"success": False, "error": tr('library.audio.err.bad_url')}), 400

    # Check for a duplicate. T4.6: a soft-deleted row (deleted_at IS NOT NULL) does NOT
    # count as a duplicate - the user removed it, a repeated download must go through
    # (youtube_pytubefix._download_youtube_core "revives" the row on INSERT).
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
            "message": tr('library.audio.err.already'),
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
        logger.info(f"Starting the download with trim: {start_time}s - {end_time}s")
    return jsonify({"success": True, "download_id": download_id, "status": "started"})


@audio_bp.route('/api/audio/downloads', methods=['GET'])
def get_audio_downloads():
    """List of downloaded audio with pagination and filters.

    Query params:
    - ``page``, ``per_page`` - pagination.
    - ``search`` - search by title/author.
    - ``sort_by``, ``sort_order``.
    - ``source_type`` (Phase 9.9) - filter: 'youtube' | 'recording' | '' (all).
    - ``category_id`` (Phase 14) - filter by the direction of the audio's latest transcript.

    The response additionally includes:
    - ``counts`` - {all, youtube, recording} for the UI segmented control.
    - Recording-specific fields: ``source_type``, ``recording_session_id``,
      ``recording_segments``, ``recording_duration_sec``.
    """
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '')
    sort_by = request.args.get('sort_by', 'created_at')
    sort_order = request.args.get('sort_order', 'desc')
    source_type = request.args.get('source_type', '').strip().lower()
    per_page = min(per_page, 100)
    # C4: direction (expanding to projects), tags (OR), period -
    # `app/services/categories.py`, the same set of axes as /api/history.
    try:
        category_value = cat_svc.parse_filter(request.args.get('category_id'))
        tag_names = cat_svc.parse_tags(request.args.get('tags'))
        since_val = cat_svc.parse_date(request.args.get('since'))
        until_val = cat_svc.parse_date(request.args.get('until'))
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Phase 14: the direction (category) historically lives on transcriptions. The audio
    # is linked to its latest transcript via file_path, so the direction
    # is taken from it. Phase 21 (recording UX): an untranscribed recording has no
    # transcript yet - fallback to its OWN audio_downloads.category_id (filled from the
    # direction set at recording time). COALESCE: transcript direction > own.
    # We use this correlated subquery in the SELECT (to show the chip), in the
    # WHERE (the filter) and in the counts. Requires the alias `a`.
    cat_subq = (
        'COALESCE('
        '(SELECT t.category_id FROM transcriptions t WHERE t.file_path = a.file_path '
        'ORDER BY t.id DESC LIMIT 1), a.category_id)'
    )
    # Tags live only on transcriptions (not on audio_downloads) - so, unlike
    # the direction, there is NO fallback to an own value here: without a
    # transcript `tx_id_subq`/`tx_date_subq` give NULL and the row drops out of the
    # tag/period filter (the item stays visible without them, with `tags: []`).
    tx_id_subq = (
        '(SELECT t.id FROM transcriptions t WHERE t.file_path = a.file_path '
        'ORDER BY t.id DESC LIMIT 1)'
    )
    tx_date_subq = (
        '(SELECT COALESCE(t.meeting_date, t.created_at) FROM transcriptions t '
        'WHERE t.file_path = a.file_path ORDER BY t.id DESC LIMIT 1)'
    )
    # The main job on this page is to find what is NOT yet turned into text.
    # Without this filter the untranscribed get lost among the hundred already processed.
    has_tr_subq = (
        'EXISTS(SELECT 1 FROM transcriptions t '
        'WHERE t.file_path = a.file_path AND t.deleted_at IS NULL)'
    )
    _tr_raw = request.args.get('transcribed', '').strip()
    transcribed = _tr_raw if _tr_raw in ('0', '1') else None

    cat_frag, cat_params = cat_svc.filter_sql(cat_subq, category_value)
    tags_frag, tags_params = cat_svc.tags_sql(tx_id_subq, tag_names)
    date_frag, date_params = cat_svc.date_sql(tx_date_subq, since_val, until_val)
    extra_sql = cat_frag + tags_frag + date_frag
    extra_params = cat_params + tags_params + date_params

    with _get_db() as conn:
        c = conn.cursor()

        # Cross-cutting filters (search + direction + tags + period) -
        # applied to the counts by source_type as well, because the source chips must
        # reflect these narrowings. T4.6: soft-deleted (undo window) are never
        # shown - a mandatory system filter, independent of
        # query parameters.
        base_clauses = ['a.deleted_at IS NULL']
        base_params = []
        search_param = None
        if search:
            base_clauses.append('(a.title LIKE ? OR a.author LIKE ?)')
            search_param = f'%{search}%'
            base_params.extend([search_param, search_param])
        # base_clauses is never empty (a.deleted_at IS NULL is always first),
        # so extra_sql (each fragment starts with " AND ") can be appended
        # directly - the WHERE is guaranteed to exist.
        base_sql = ' WHERE ' + ' AND '.join(base_clauses) + extra_sql
        base_params_full = base_params + extra_params

        where_clauses = list(base_clauses)
        params = list(base_params)
        if source_type in ('youtube', 'recording', 'file'):
            where_clauses.append('a.source_type = ?')
            params.append(source_type)
        # Presence of a transcript is a separate slice, NOT in base_clauses: the source chips
        # must keep showing full counters, so it is visible what exactly
        # the remaining work consists of.
        if transcribed is not None:
            where_clauses.append(has_tr_subq if transcribed == '1' else f'NOT {has_tr_subq}')

        where_sql = ' WHERE ' + ' AND '.join(where_clauses) + extra_sql
        params_full = params + extra_params

        valid_sort_fields = ['created_at', 'title', 'author', 'duration', 'file_size']
        if sort_by not in valid_sort_fields:
            sort_by = 'created_at'
        sort_order = sort_order.lower() if sort_order.lower() in ('asc', 'desc') else 'desc'

        # Phase 10.8: separately fetch transcription_id (latest) for each audio.
        # The link goes through file_path. A subquery in the SELECT is cheap because file_path
        # does not have to be indexed - we will have tens of thousands of
        # transcriptions at most, not millions.
        query = (
            f'SELECT a.*, '
            f'(SELECT t.id FROM transcriptions t WHERE t.file_path = a.file_path AND t.deleted_at IS NULL '
            f'ORDER BY t.id DESC LIMIT 1) AS transcription_id, '
            # A SEPARATE alias: a.* already contains its own category_id (Phase 21), so
            # COALESCE cannot be aliased to 'category_id' as well - sqlite3.Row with
            # duplicate names returns the FIRST column (the own one), not the computed one.
            f'{cat_subq} AS resolved_category_id '
            f'FROM audio_downloads a{where_sql} '
            f'ORDER BY {sort_by} {sort_order} LIMIT ? OFFSET ?'
        )
        c.execute(query, params_full + [per_page, (page - 1) * per_page])
        downloads = c.fetchall()

        # Total taking the filters into account
        c.execute(f'SELECT COUNT(*) as total FROM audio_downloads a{where_sql}', params_full)
        total = c.fetchone()['total']

        # Counts by source_type - for the UI segmented control. Takes into account
        # the cross-cutting filters (search + direction + tags + period), but not the
        # source_type filter (because we want to know the counts across ALL sources within
        # the narrowing). `base_sql` already carries extra_sql (see above).
        c.execute(
            f'SELECT a.source_type, COUNT(*) AS n FROM audio_downloads a{base_sql} '
            f'GROUP BY a.source_type',
            base_params_full,
        )
        counts_by_type = {row['source_type']: row['n'] for row in c.fetchall()}

        # How many are still waiting for transcription - within search/direction/tags/
        # period, but without the source_type/transcribed filters, so that the number on the chip
        # does not "collapse" to zero after the filter is applied.
        # `base_sql` always starts with WHERE (a.deleted_at IS NULL is always first
        # in base_clauses), so " AND " further on is unconditional.
        c.execute(
            f'SELECT COUNT(*) AS n FROM audio_downloads a{base_sql}'
            f' AND NOT {has_tr_subq}',
            base_params_full,
        )
        untranscribed = c.fetchone()['n']

        # Tags - always those of the audio's latest transcript (the only place they
        # live); without a transcript - `[]`, not a missing key.
        tx_ids = [d['transcription_id'] for d in downloads if d['transcription_id'] is not None]
        tags_map = cat_svc.tags_for(conn, tx_ids)
        suggested_map = {tid: cat_svc.suggested_tags(conn, tid) for tid in tx_ids}

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
        # Phase 10.8: ID of the newest transcript for this audio (NULL if not transcribed yet).
        # The frontend shows "Відкрити транскрипт" instead of "Транскрибувати" when it is filled.
        'transcription_id': d['transcription_id'] if 'transcription_id' in d.keys() else None,
        # Phase 14+21: direction = COALESCE(transcript direction, the recording's own).
        'category_id': d['resolved_category_id'] if 'resolved_category_id' in d.keys() else None,
        # Phase 22: a video recording (screen capture). The frontend shows the 📹 «Відео» badge.
        'has_video': d['has_video'] if 'has_video' in d.keys() else 0,
        'primary_video_path': d['primary_video_path'] if 'primary_video_path' in d.keys() else None,
        # C4: tags of the latest transcript ([] without it) + Claude hints.
        'tags': tags_map.get(d['transcription_id'], []) if d['transcription_id'] is not None else [],
        'suggested_tags': (suggested_map.get(d['transcription_id'], [])
                           if d['transcription_id'] is not None else []),
        # Legacy YouTube tags (`audio_downloads.tags`, JSON) - a separate key,
        # so as not to confuse them with the archive tags (record_tags).
        'youtube_tags': _parse_youtube_tags(d['tags']) if 'tags' in d.keys() else [],
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
    """Soft-delete audio (T4.6, REMEDIATION_PLAN Wave 2, Option A).

    Sets ``deleted_at`` - the file is NOT erased from disk immediately (P0 from the audit:
    often the only copy of a recording). ``POST .../restore`` undoes it during the
    grace period (``RECALL_SOFTDELETE_GRACE_DAYS``); physically removed by
    ``app.services.retention.purge_soft_deleted`` at server start.
    """
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id FROM audio_downloads WHERE id = ? AND deleted_at IS NULL',
            (download_id,),
        )
        download = c.fetchone()
        if not download:
            return jsonify({'success': False, 'error': tr('library.audio.err.not_found')}), 404

        c.execute(
            'UPDATE audio_downloads SET deleted_at = ? WHERE id = ?',
            (time.time(), download_id),
        )
        conn.commit()
    return jsonify({'success': True, 'id': download_id})


@audio_bp.route('/api/audio/downloads/<int:download_id>', methods=['PATCH'])
def patch_audio_download(download_id):
    """Custom title/description of an Audio Library card (editable-title-description-02, contract C3).

    JSON ``{"title"?: str, "description"?: str|null}``. Unlike PATCH
    /api/history/<id>: ``audio_downloads.title`` is ``NOT NULL`` (provenance
    always had a name), so an empty/missing string here is a 400, not "clear
    to NULL". ``description`` can be cleared (``null``/empty string → NULL).
    Only by ``id`` - not by ``youtube_id``/``file_path``.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': tr('library.audio.err.json_object')}), 400
    if 'title' not in payload and 'description' not in payload:
        return jsonify({'success': False,
                        'error': tr('library.audio.err.need_field')}), 400

    updates: dict = {}
    if 'title' in payload:
        raw_title = payload['title']
        if not isinstance(raw_title, str) or not raw_title.strip():
            return jsonify({'success': False, 'error': tr('library.audio.err.title_empty')}), 400
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
            return jsonify({'success': False, 'error': tr('library.audio.err.not_found')}), 404

        if updates:
            assignments = ', '.join(f'{field} = ?' for field in updates)
            c.execute(
                f'UPDATE audio_downloads SET {assignments} WHERE id = ?',
                [*updates.values(), download_id],
            )
            conn.commit()

        # recording-meta-suggest-02: the same title in the Library and the manifest
        # of the session. Non-recordings: link=None - behaviour as before. Lazily: the
        # recording package pulls in WASAPI/finalize.
        linked_changed: list = []
        if updates:
            from app.services.recording import library as rec_library
            link = rec_library.linked_recording(conn, audio_id=download_id)
            if link:
                rs = state.recording_service
                linked_changed = rec_library.sync_recording_meta(
                    conn, link, updates, store=getattr(rs, 'store', None) if rs else None)

        item = c.execute(
            'SELECT * FROM audio_downloads WHERE id = ?', (download_id,),
        ).fetchone()

    for tid in linked_changed:
        # The title and description go into the chunks' context prefix - re-embed.
        record_meta.after_meta_update(tid, changed=True,
                                      db_path=current_app.config['DATABASE'])
    return jsonify({'success': True, 'item': dict(item)})


@audio_bp.route('/api/audio/downloads/<int:download_id>/restore', methods=['POST'])
def restore_audio_download(download_id):
    """Undo a soft-delete. 404 if the record does not exist or is not deleted."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id FROM audio_downloads WHERE id = ? AND deleted_at IS NOT NULL',
            (download_id,),
        )
        if not c.fetchone():
            return jsonify({'success': False, 'error': tr('library.audio.err.nothing_restore')}), 404
        c.execute(
            'UPDATE audio_downloads SET deleted_at = NULL WHERE id = ?',
            (download_id,),
        )
        conn.commit()
    return jsonify({'success': True, 'id': download_id})


@audio_bp.route('/api/audio/check-duplicate', methods=['POST'])
def check_duplicate_audio():
    """Check whether this YouTube video has already been downloaded."""
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'success': False, 'error': tr('library.audio.err.no_url')}), 400
    youtube_id = extract_youtube_id(url)
    if not youtube_id:
        return jsonify({'success': True, 'is_duplicate': False})

    with _get_db() as conn:
        c = conn.cursor()
        # T4.6: a soft-deleted one does not count as a duplicate (undo window ≠ a visible record).
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
    """Open the file in Windows Explorer."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT file_path FROM audio_downloads WHERE id = ? AND deleted_at IS NULL',
            (audio_id,),
        )
        audio = c.fetchone()
    if not audio:
        return jsonify({'success': False, 'error': tr('library.audio.err.audio_missing')}), 404
    file_path = audio['file_path']
    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': tr('library.audio.err.file_missing')}), 404

    try:
        if os.name == 'nt':
            subprocess.Popen(['explorer', '/select,', os.path.abspath(file_path)])
        else:
            folder = os.path.dirname(file_path)
            subprocess.Popen(['xdg-open', folder])
        logger.info(f"Opened in explorer: {file_path}")
        return jsonify({'success': True, 'message': tr('library.audio.err.opened')})
    except Exception as e:
        logger.error(f"Error opening in explorer: {e}")
        return jsonify({'success': False, 'error': tr('library.audio.err.open_failed')}), 500


@audio_bp.route('/api/audio/play/<int:audio_id>', methods=['POST'])
def play_audio_locally(audio_id):
    """Play the audio in the system player."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT file_path, title FROM audio_downloads WHERE id = ? AND deleted_at IS NULL',
            (audio_id,),
        )
        audio = c.fetchone()
    if not audio:
        return jsonify({'success': False, 'error': tr('library.audio.err.audio_missing')}), 404
    file_path = audio['file_path']
    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': tr('library.audio.err.file_missing')}), 404
    try:
        if os.name == 'nt':
            os.startfile(file_path)
        else:
            opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.Popen([opener, file_path])
        logger.info(f"Playing audio: {audio['title']}")
        return jsonify({'success': True, 'message': tr('library.audio.err.playing', title=audio['title'])})
    except Exception as e:
        logger.error(f"Error playing audio: {e}")
        return jsonify({'success': False, 'error': tr('library.audio.err.play_failed')}), 500
