"""Transcription endpoints (Phase 5.8).

- POST   /api/transcribe                      (file/youtube → transcript)
- POST   /api/transcribe/async                (the same as a `transcription` job)
- GET    /api/history                         (list with filters + FTS)
- GET    /api/history/<id>                    (get one)
- PATCH  /api/history/<id>                    (custom title/description, v44)
- DELETE /api/history/<id>                    (soft-delete one, T4.6)
- POST   /api/history/<id>/restore            (undo soft-delete, T4.6)
- POST   /api/history/bulk_delete             (mass, STAYS physical)
- POST   /api/history/bulk_export             (mass JSON/TXT)
- POST   /api/transcription/<id>/polish       (Claude API)
- POST   /api/export/<format>                 (TXT/SRT/JSON of a single transcript)
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from datetime import datetime

from flask import Blueprint, current_app, g, jsonify, request, send_file
from werkzeug.utils import secure_filename

from app.services.recording.state import recording_active
from app import state
from app.core import auth
from app.core.i18n import tr
from app.repositories import transcriptions as tx_repo
from app.services import categories as cat_svc
from app.services import dedup_audio, meta_suggest, record_meta, transcript_ai, transcription_run, vocabulary
from app.utils.audio import extract_audio_from_video as _extract_audio_raw
from app.utils.files import allowed_file, format_srt_timestamp, is_video_file
from app.utils.fts import sanitize_fts_query
from app.utils.paths import safe_path_within, safe_path_within_any


logger = logging.getLogger(__name__)
transcription_bp = Blueprint('transcription', __name__)


def _get_db():
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


def _make_extract_log(process_id):
    if not process_id:
        return None
    def _log(stage, message, progress=None, status="processing"):
        state.add_log(process_id, stage, message, progress, status)
    return _log


# ============================================================
# T7.1: source_type resolution - one function per /api/transcribe scenario.
#
# Each resolve_*_source() reads the current Flask request (form/files), builds
# the matching TranscribeParams and calls transcription_run.resolve(), which
# returns (filepath, source_name, meta). meta has the fixed keys of
# transcription_run.empty_source_meta(); a library row may inherit another
# source_type - the caller must use meta['source_type'] from then on.
#
# On a validation failure, they raise _SourceResolutionError carrying the
# exact (jsonify payload, status) the sync route answers with.
# ============================================================

class _SourceResolutionError(Exception):
    """Carries a ready-to-return (jsonify payload, status) from a resolve_*_source()."""

    def __init__(self, payload: dict, status: int):
        super().__init__(payload.get('error', 'source resolution error'))
        self.payload = payload
        self.status = status


_empty_source_meta = transcription_run.empty_source_meta


def _sync_params(source: str, source_ref=None, **kw) -> transcription_run.TranscribeParams:
    return transcription_run.TranscribeParams(
        source=source, source_ref=source_ref,
        db_path=current_app.config['DATABASE'],
        upload_folder=current_app.config.get('UPLOAD_FOLDER'),
        **kw,
    )


def _resolve_sync(params) -> tuple[str, str, dict]:
    try:
        return transcription_run.resolve(params)
    except transcription_run.TranscribeError as e:
        raise _SourceResolutionError(e.sync_body(), e.sync_status)


def resolve_file_source() -> tuple[str, str, dict]:
    """source_type='file': multipart upload, extracting audio if it's a video."""
    if 'audio' not in request.files:
        raise _SourceResolutionError({"success": False, "error": tr('transcript.err.file_not_found')}, 400)
    file = request.files['audio']
    if file.filename == '':
        raise _SourceResolutionError({"success": False, "error": tr('transcript.err.file_not_selected')}, 400)
    if not allowed_file(file.filename):
        raise _SourceResolutionError({"success": False, "error": tr('transcript.err.file_format_unsupported')}, 400)

    filename = secure_filename(file.filename)
    if not filename:
        filename = "audio_file"
    original_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'mp3'
    if not filename.endswith(f'.{original_ext}'):
        filename = f"{filename}.{original_ext}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{filename}"
    upload_dir = current_app.config['UPLOAD_FOLDER']
    filepath = safe_path_within(upload_dir, os.path.join(upload_dir, filename))
    if filepath is None:
        logger.error(f"Path traversal attempt: {filename}")
        raise _SourceResolutionError({"success": False, "error": tr('transcript.err.filename_invalid')}, 400)

    file.save(filepath)
    logger.info(f"File saved: {filepath}")
    return _resolve_sync(_sync_params('file', filepath, original_name=file.filename))


def resolve_youtube_source() -> tuple[str, str, dict]:
    """source_type='youtube': previously completed /api/youtube/download by download_id."""
    return _resolve_sync(_sync_params('youtube', download_id=request.form.get('download_id') or ''))


def resolve_recording_source() -> tuple[str, str, dict]:
    """source_type='recording': Phase 9.8, a finalized server-side recording session."""
    return _resolve_sync(_sync_params('recording', request.form.get('recording_session_id')))


def resolve_library_source() -> tuple[str, str, dict]:
    """source_type='library': Phase 9.10+, any Audio Library (audio_downloads) row by id.

    Inherits the row's original source_type ('youtube'/'recording'/'file') for
    the transcriptions.source_type INSERT further downstream."""
    return _resolve_sync(_sync_params('library', request.form.get('audio_download_id')))


_SOURCE_RESOLVERS = {
    'file': resolve_file_source,
    'youtube': resolve_youtube_source,
    'recording': resolve_recording_source,
    'library': resolve_library_source,
}


@transcription_bp.route('/api/transcribe', methods=['POST'])
def transcribe():
    """Transcribe audio (file upload or YouTube via download_id), synchronously."""
    logger.info(f"=== DEBUG /api/transcribe ===")
    logger.info(f"request.form: {dict(request.form)}")
    logger.info(f"request.files keys: {list(request.files.keys())}")
    logger.info(f"content_type: {request.content_type}")
    logger.info(f"content_length: {request.content_length}")

    source_type = request.form.get('source_type', 'file')

    # editable-title-description-02: custom title/description from the form -
    # validate BEFORE the resolver starts (file/download/transcription).
    try:
        form_title = record_meta.normalize_title(request.form.get('title'))
        form_description = record_meta.normalize_description(request.form.get('description'))
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    resolver = _SOURCE_RESOLVERS.get(source_type)
    if resolver is None:
        return jsonify({"success": False, "error": tr('transcript.err.source_type_invalid')}), 400
    try:
        resolved = resolver()
    except _SourceResolutionError as e:
        return jsonify(e.payload), e.status

    _cat_raw = request.form.get('category_id')
    params = _sync_params(
        source_type,
        model=request.form.get('model') or 'large-v3',
        language=request.form.get('language', 'uk'),
        diarize=request.form.get('diarize', '').lower() in ('1', 'true', 'yes'),
        category_id=int(_cat_raw) if (_cat_raw and _cat_raw.isdigit()) else None,
        title=form_title,
        description=form_description,
        recording_session_id=request.form.get('recording_session_id'),
    )
    try:
        result = transcription_run.run(params, resolved)
    except transcription_run.TranscribeError as e:
        return jsonify(e.sync_body()), e.sync_status
    return jsonify(result)


def _async_bad(key: str, **kw):
    return jsonify({"success": False, "error": tr(key, **kw), "error_code": "INVALID_ARGUMENT"}), 400


_ASYNC_REF_FIELD = {
    'library': 'audio_download_id',
    'recording': 'recording_session_id',
    'youtube': 'url',
    'path': 'path',
}


@transcription_bp.route('/api/transcribe/async', methods=['POST'])
def transcribe_async():
    """Queue a transcription job (library | recording | youtube | path); 202 with its job_id.

    A second call for the same source while its job is queued or running answers 200 with that
    job_id and `created: false`. A non-UI caller is refused while a recording runs."""
    data = request.get_json(silent=True) or {}
    source = data.get('source')
    field = _ASYNC_REF_FIELD.get(source) if isinstance(source, str) else None
    if field is None:
        return _async_bad('transcript.err.async_source_invalid')
    ref = data.get(field)
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        return _async_bad('transcript.err.async_ref_required', source=source, field=field)
    try:
        title = record_meta.normalize_title(data.get('title'))
        description = record_meta.normalize_description(data.get('description'))
    except ValueError as e:
        return jsonify({"success": False, "error": str(e), "error_code": "INVALID_ARGUMENT"}), 400
    cat = data.get('category_id')
    if cat is not None and cat != '':
        if isinstance(cat, bool) or not str(cat).isdigit():
            return _async_bad('transcript.err.category_id_invalid')
        cat = int(cat)
    else:
        cat = None
    diarize = data.get('diarize')
    if diarize is not None and not isinstance(diarize, bool):
        diarize = str(diarize).lower() in ('1', 'true', 'yes')

    actor = auth.classify_request()[0]
    params = transcription_run.TranscribeParams(
        source=source,
        source_ref=ref.strip() if isinstance(ref, str) else ref,
        model=data.get('model') or 'large-v3',
        language=data.get('language') or 'uk',
        diarize=diarize,
        category_id=cat,
        title=title,
        description=description,
        recording_session_id=ref if source == 'recording' else None,
        actor=actor,
        db_path=current_app.config['DATABASE'],
        upload_folder=current_app.config.get('UPLOAD_FOLDER'),
        non_ui=actor != 'ui',
        max_bytes=current_app.config.get('MAX_CONTENT_LENGTH'),
    )
    try:
        out = transcription_run.submit(params)
    except transcription_run.TranscribeError as e:
        return jsonify(e.async_body()), e.status
    return jsonify({"success": True, **out}), (202 if out['created'] else 200)


@transcription_bp.route('/api/transcribe/active', methods=['GET'])
def list_active_library_transcriptions():
    """Current /api/transcribe jobs started from the Audio Library.

    The frontend polls this endpoint to mark cards `.is-transcribing`
    (survive F5). The key is audio_download_id, so one card = one
    active job at most.
    """
    store = state.active_library_transcriptions or {}
    return jsonify({'active': list(store.values())})


# ===== History =====

@transcription_bp.route('/api/history', methods=['GET'])
def get_history():
    """History list with filters + FTS5 search."""
    page = max(1, request.args.get('page', 1, type=int))
    per_page = max(1, min(100, request.args.get('per_page', 10, type=int)))
    source_filter = request.args.get('source_type')
    language_filter = request.args.get('language')
    search_query = request.args.get('search', '')
    speaker_id_filter = request.args.get('speaker_id', type=int)  # Phase 12.7
    # Contextual facet filters (appear in the UI for a specific source):
    # Telegram - sender + chat/group/channel; YouTube - author/channel. LIKE on
    # metadata (not the transcript), so they complement, not replace, the FTS search.
    tg_sender_q = (request.args.get('tg_sender') or '').strip()
    tg_chat_q = (request.args.get('tg_chat') or '').strip()
    yt_author_q = (request.args.get('yt_author') or '').strip()
    # `deleted=1`: only the soft-deleted records (undo window), each with `deleted_at`.
    deleted_only = request.args.get('deleted') == '1'
    # C4: direction (expanding to projects), tags (OR), period - a shared
    # set of filter axes for all list surfaces (`app/services/categories.py`).
    try:
        category_value = cat_svc.parse_filter(request.args.get('category_id'))
        tag_names = cat_svc.parse_tags(request.args.get('tags'))
        since_val = cat_svc.parse_date(request.args.get('since'))
        until_val = cat_svc.parse_date(request.args.get('until'))
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    with _get_db() as conn:
        c = conn.cursor()
        # T4.6: soft-deleted (undo window) are never shown in lists/search -
        # a mandatory system filter, independent of query parameters.
        where_clauses = ['deleted_at IS NOT NULL' if deleted_only else 'deleted_at IS NULL']
        params = []
        fts_query = None  # Phase 12.2: lifted out of the if-block for later use in the snippet
        if source_filter:
            where_clauses.append('source_type = ?'); params.append(source_filter)
        if language_filter:
            where_clauses.append('language = ?'); params.append(language_filter)
        if speaker_id_filter:
            # Phase 12.7: filter by speaker_id - JOIN with the map, distinct so as not to
            # duplicate for several raw_labels with the same speaker_id (rare, but it happens).
            where_clauses.append(
                'id IN (SELECT DISTINCT transcription_id FROM transcription_speaker_map WHERE speaker_id = ?)'
            )
            params.append(speaker_id_filter)
        if tg_sender_q:
            where_clauses.append('tg_sender LIKE ?'); params.append(f'%{tg_sender_q}%')
        if tg_chat_q:
            where_clauses.append('tg_chat_title LIKE ?'); params.append(f'%{tg_chat_q}%')
        if yt_author_q:
            where_clauses.append('youtube_author LIKE ?'); params.append(f'%{yt_author_q}%')
        if search_query:
            fts_query = sanitize_fts_query(search_query)
            if fts_query:
                # Search over the Library also sees the COMMENTS on a recording, not just its
                # text (Wave 2.5). Otherwise there was a hole: the owner writes
                # «Барселона» as a clarification to a call where that word was not spoken -
                # semantic search finds that comment, while the Library, in which the
                # recording has to be opened, says "nothing found".
                #
                # Via a separate comments FTS, not LIKE: the same tokenization
                # as for transcripts, so «бюджету» finds «бюджет».
                # A missing table (an old DB) must not break search - so the
                # branch checks for its presence instead of relying on except.
                cond = ('id IN (SELECT rowid FROM transcriptions_fts '
                        'WHERE transcriptions_fts MATCH ?)')
                params.append(fts_query)
                has_cm = bool(c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='comment_chunks_fts'").fetchone())
                if has_cm:
                    cond = ('(' + cond + ' OR id IN ('
                            'SELECT c2.target_id FROM comments c2 '
                            'JOIN comment_chunks cc ON cc.comment_id = c2.id '
                            'JOIN comment_chunks_fts f ON f.rowid = cc.id '
                            "WHERE c2.target_type = 'transcription' "
                            'AND c2.deleted_at IS NULL '
                            'AND comment_chunks_fts MATCH ?))')
                    params.append(fts_query)
                where_clauses.append(cond)
            else:
                where_clauses.append('1 = 0')

        where_sql = 'WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''
        # The C4 direction/tags/period axes are separate fragments (each starts
        # with " AND "), added AFTER the .replace() below, so that `category_id IN
        # (SELECT id FROM …)` does not suffer from that string hack (it looks for the
        # substring `id IN`, not an anchor at the start of the clause).
        cat_frag, cat_params = cat_svc.filter_sql('t.category_id', category_value)
        tags_frag, tags_params = cat_svc.tags_sql('t.id', tag_names)
        date_frag, date_params = cat_svc.date_sql(
            'COALESCE(t.meeting_date, t.created_at)', since_val, until_val)
        extra_sql = cat_frag + tags_frag + date_frag
        extra_params = cat_params + tags_params + date_params

        count_sql = f'SELECT COUNT(*) FROM transcriptions t {where_sql}{extra_sql}'
        total = c.execute(count_sql, params + extra_params).fetchone()[0]

        offset = (page - 1) * per_page
        # Phase 12.2: when search is active, use the FTS5 snippet()
        # to highlight a fragment around the match. Otherwise - a regular preview of
        # the first 200 characters.
        # A shared set of columns for both branches. The Library list renders
        # a row BY SOURCE TYPE (a telegram message ≠ a call ≠ a document), and
        # each shape needs its own fields: TG - chat/sender/link, call -
        # duration/speakers/how many tasks, document - pages/file name.
        # Two correlated subqueries per page of 20 rows - cheap.
        # `title`/`description` (v44) - next to `source_name`, not instead of it:
        # source_name stays provenance, display_name is computed by record_meta.
        cols = '''t.id, t.created_at, t.source_type, t.source_name, t.title, t.description,
                       t.youtube_thumbnail,
                       t.language, t.model_used, t.processing_time, t.category_id, t.doc_type,
                       t.tg_chat_title, t.tg_sender, t.tg_link,
                       t.youtube_duration, t.youtube_author,
                       t.page_count, t.original_filename, t.meeting_date, t.enriched_at,
                       t.duplicate_of, t.file_path, t.active_version_id,
                       (SELECT COUNT(*) FROM action_items ai
                         WHERE ai.transcription_id = t.id
                           AND ai.status = 'open' AND ai.dup_of IS NULL) AS open_tasks,
                       (SELECT COUNT(DISTINCT m.speaker_id) FROM transcription_speaker_map m
                         WHERE m.transcription_id = t.id) AS speaker_count'''
        if deleted_only:
            cols += ', t.deleted_at'
        if fts_query:
            select_sql = f'''
                SELECT {cols},
                       (SELECT snippet(transcriptions_fts, 0, '«MARK»', '«/MARK»', '…', 16)
                        FROM transcriptions_fts
                        WHERE transcriptions_fts MATCH ? AND rowid = t.id) as transcript_preview
                FROM transcriptions t
                {where_sql.replace('id IN', 't.id IN')}{extra_sql}
                ORDER BY t.created_at DESC
                LIMIT ?
                OFFSET ?
            '''
            transcriptions = c.execute(
                select_sql, [fts_query] + params + extra_params + [per_page, offset]).fetchall()
        else:
            # The alias `t` is mandatory here too: without it `ai.transcription_id = id`
            # in the subquery would bind to action_items.id, not to the transcript.
            # Unqualified names in where_sql are resolved to `t` further on.
            select_sql = f'''
                SELECT {cols},
                       SUBSTR(t.transcript_text, 1, 200) as transcript_preview
                FROM transcriptions t
                {where_sql}{extra_sql}
                ORDER BY t.created_at DESC
                LIMIT ?
                OFFSET ?
            '''
            transcriptions = c.execute(
                select_sql, params + extra_params + [per_page, offset]).fetchall()

        ids = [row['id'] for row in transcriptions]
        tags_map = cat_svc.tags_for(conn, ids)
        suggested_map = {tid: cat_svc.suggested_tags(conn, tid) for tid in ids}

    items = []
    for t in transcriptions:
        item = dict(t)
        item['has_media'] = _has_media(item.pop('file_path', None))
        item['display_name'] = record_meta.display_name(item)
        item['tags'] = tags_map.get(item['id'], [])
        item['suggested_tags'] = suggested_map.get(item['id'], [])
        items.append(item)

    return jsonify({
        'transcriptions': items,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page if total > 0 else 0,
    })


@transcription_bp.route('/api/history/<int:transcription_id>', methods=['GET'])
def get_transcription(transcription_id):
    """A single transcript with all its data."""
    with _get_db() as conn:
        transcription = conn.execute(
            'SELECT * FROM transcriptions WHERE id = ? AND deleted_at IS NULL',
            (transcription_id,),
        ).fetchone()
        if not transcription:
            return jsonify({"success": False, "error": tr('transcript.err.tx_not_found')}), 404
        # Phase 10.3: speaker map (raw_label → speaker info) for UI chip rendering.
        speaker_rows = conn.execute('''
            SELECT m.raw_label, m.speaker_id, s.name, s.color, s.is_self
            FROM transcription_speaker_map m
            LEFT JOIN speakers s ON s.id = m.speaker_id
            WHERE m.transcription_id = ?
        ''', (transcription_id,)).fetchall()

        # Links for the new transcript page: the entities and action items
        # of this recording. Empty lists if enrichment has not run yet.
        # Additive - existing consumers ignore the new keys.
        entity_rows = conn.execute('''
            SELECT e.id, e.type, e.canonical_name, e.role,
                   me.mention_count, me.salience, me.role_in_meeting
            FROM meeting_entities me
            JOIN entities e ON e.id = me.entity_id
            WHERE me.transcription_id = ?
            ORDER BY me.salience DESC, me.mention_count DESC
        ''', (transcription_id,)).fetchall()
        action_item_rows = conn.execute('''
            SELECT id, task, owner_name, owner_entity_id, due, status, created_at
            FROM action_items
            WHERE transcription_id = ?
            ORDER BY (status = 'open') DESC, created_at ASC
        ''', (transcription_id,)).fetchall()
        record_tags = cat_svc.tags_for(conn, [transcription_id]).get(transcription_id, [])
        record_suggested = cat_svc.suggested_tags(conn, transcription_id)
    result = dict(transcription)
    result['has_media'] = _has_media(result.get('file_path'))
    result['segments'] = json.loads(result['segments']) if result['segments'] else []
    result['speakers'] = [
        {
            'raw_label': r['raw_label'],
            'speaker_id': r['speaker_id'],
            'name': r['name'],
            'color': r['color'],
            'is_self': bool(r['is_self']) if r['is_self'] is not None else False,
        }
        for r in speaker_rows
    ]
    result['entities'] = [dict(r) for r in entity_rows]
    result['action_items'] = [dict(r) for r in action_item_rows]
    result['tags'] = record_tags
    result['suggested_tags'] = record_suggested
    # title/description come from SELECT * (v44); display_name is a derived field.
    result['display_name'] = record_meta.display_name(result)
    return jsonify(result)


def _has_media(path) -> bool:
    return bool(path) and os.path.isfile(path)


def _retranscribe_error(code, **extra):
    from app.services import retranscribe
    status, body = retranscribe.error_body(code, **extra)
    return jsonify(body), status


@transcription_bp.route('/api/history/<int:transcription_id>/retranscribe', methods=['POST'])
def retranscribe_transcription(transcription_id):
    """Queue a re-run of the record with another installed model; the result becomes a version."""
    from app.services import retranscribe
    data = request.get_json(silent=True) or {}
    model = data.get('model')
    language = data.get('language')
    diarize = data.get('diarize')
    if not isinstance(model, str) or not model.strip():
        return _retranscribe_error('MODEL_NOT_INSTALLED')
    if language is not None and not isinstance(language, str):
        return jsonify({'success': False, 'error': tr('transcript.err.bad_language'),
                        'error_code': 'INVALID_ARGUMENT'}), 400
    try:
        res = retranscribe.submit(
            transcription_id, model=model.strip(), language=language or None,
            diarize=diarize if isinstance(diarize, bool) else None,
            db_path=current_app.config['DATABASE'],
            # The approval gate clears this for a non-UI caller, and sets
            # `approved_action` when the owner approved dropping a version (ask 9).
            allow_evict=(g.get('approved_action') == 'transcript_version_evict'
                         or g.get('evict_allowed', True)))
    except retranscribe.RetranscribeError as e:
        return _retranscribe_error(e.code, **e.extra)
    return jsonify({'success': True, **res}), 202


def _record_missing(conn, tid):
    return conn.execute('SELECT 1 FROM transcriptions WHERE id = ? AND deleted_at IS NULL',
                        (tid,)).fetchone() is None


@transcription_bp.route('/api/history/<int:transcription_id>/versions', methods=['GET'])
def list_transcript_versions(transcription_id):
    from app.services import retranscribe, transcript_versions as tv
    with _get_db() as conn:
        if _record_missing(conn, transcription_id):
            return _retranscribe_error('NOT_FOUND')
        listing = tv.list_versions(conn, transcription_id)
    return jsonify({'success': True, 'active_version_id': listing['active_version_id'],
                    'running_job_id': retranscribe.running_job_id(transcription_id),
                    'versions': listing['versions']})


@transcription_bp.route('/api/history/<int:transcription_id>/versions/<int:version_id>',
                        methods=['GET'])
def get_transcript_version(transcription_id, version_id):
    from app.services import transcript_versions as tv
    with _get_db() as conn:
        if _record_missing(conn, transcription_id):
            return _retranscribe_error('NOT_FOUND')
        version = tv.get_version(conn, transcription_id, version_id)
    if version is None:
        return jsonify({'success': False, 'error': tr('transcript.err.version_not_found'),
                        'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'version': version})


@transcription_bp.route(
    '/api/history/<int:transcription_id>/versions/<int:version_id>/activate', methods=['POST'])
def activate_transcript_version(transcription_id, version_id):
    """Make a kept version the main one; search and RAG are re-embedded for this record only."""
    from app.services import reembed, retranscribe, transcript_versions as tv
    live = retranscribe.running_job_id(transcription_id)
    if live:
        return _retranscribe_error('ALREADY_RUNNING', job_id=live)
    db_path = current_app.config['DATABASE']
    with _get_db() as conn:
        if _record_missing(conn, transcription_id):
            return _retranscribe_error('NOT_FOUND')
        try:
            tv.activate(conn, transcription_id, version_id)
            conn.commit()
        except LookupError:
            conn.rollback()
            return jsonify({'success': False, 'error': tr('transcript.err.version_not_found'),
                            'error_code': 'NOT_FOUND'}), 404
        except Exception:
            conn.rollback()
            raise
    status = reembed.schedule_record_reembed(transcription_id, db_path=db_path)
    return jsonify({'success': True, 'active_version_id': version_id, 'reembed': status})


@transcription_bp.route('/api/history/<int:transcription_id>', methods=['PATCH'])
def patch_transcription_meta(transcription_id):
    """Change the custom title/description of a record after saving (editable-title-description spec).

    JSON ``{"title"?: str|null, "description"?: str|null}`` - only the
    passed keys are changed. An empty string means "remove" (``NULL``), NOT a copy of
    ``source_name``: the record's provenance stays untouched in any case.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": tr('transcript.err.json_object_expected')}), 400
    if 'title' not in payload and 'description' not in payload:
        return jsonify({"success": False,
                        "error": tr('transcript.err.title_or_description')}), 400

    kwargs = {}
    if 'title' in payload:
        kwargs['title'] = payload['title']
    if 'description' in payload:
        kwargs['description'] = payload['description']

    if auth.classify_request()[0] != 'ui':
        from app.services import job_queue
        job_id = job_queue.active_job_for_transcription(transcription_id)
        if job_id:
            return jsonify({"success": False, "error": tr('transcript.err.job_active'),
                            "error_code": "JOB_ACTIVE", "job_id": job_id}), 409
        kwargs['meta_source'] = 'mcp'
    else:
        kwargs['meta_source'] = None

    # Lazily, as in recording.py: the recording package pulls in WASAPI/finalize.
    from app.services.recording import library as rec_library

    linked_changed: list = []
    try:
        with _get_db() as conn:
            record = record_meta.update_meta(conn, transcription_id, **kwargs)
            # recording-meta-suggest-02: a recording's title is one in the Library,
            # the Media library and the session manifest. Non-recordings: link=None - as before.
            link = (rec_library.linked_recording(conn, transcription_id=transcription_id)
                    if record is not None else None)
            if link:
                linked_changed = rec_library.sync_recording_meta(
                    conn, link, {k: record[k] for k in ('title', 'description') if k in kwargs},
                    store=_recording_store())
    except ValueError as e:
        # A controlled normalization error (length) - its text was written for a person.
        return jsonify({"success": False, "error": str(e)}), 400

    if record is None:
        return jsonify({"success": False, "error": tr('transcript.err.tx_not_found')}), 404

    changed = record.pop('changed', False)
    # db_path explicitly from the request (the same one the connection above was opened with): without
    # it the re-embed would go to Config.DATABASE and rewrite the chunks of another DB.
    record_meta.after_meta_update(transcription_id, changed=changed,
                                  db_path=current_app.config['DATABASE'])
    for other_id in linked_changed:
        if other_id != transcription_id:
            record_meta.after_meta_update(other_id, changed=True,
                                          db_path=current_app.config['DATABASE'])
    return jsonify({"success": True, "record": record})


def _recording_store():
    """The recording's SessionStore or None (recording disabled / service not up)."""
    rs = state.recording_service
    return getattr(rs, 'store', None) if rs is not None else None


@transcription_bp.route('/api/history/<int:transcription_id>/suggest-meta', methods=['POST'])
def suggest_transcription_meta(transcription_id):
    """Title and description variants from the local model (recording-meta-suggest spec).

    Saves nothing - the chosen variant goes through a regular PATCH. While a recording is in progress,
    the model is not called at all: 409 ``RECORDING_ACTIVE``.
    """
    if recording_active():
        return jsonify({
            'success': False,
            'error_code': 'RECORDING_ACTIVE',
            'error': tr('transcript.err.recording_active'),
        }), 409

    try:
        with _get_db() as conn:
            result = meta_suggest.suggest(conn, transcription_id)
    except meta_suggest.SuggestError as e:
        return jsonify({'success': False, 'error': str(e)}), e.status
    return jsonify({'success': True, **result})


@transcription_bp.route('/api/history/<int:transcription_id>', methods=['DELETE'])
def delete_transcription(transcription_id):
    """Soft-delete a single transcript (T4.6, REMEDIATION_PLAN Wave 2, Option A).

    Sets ``deleted_at`` instead of a physical delete - the file (youtube/file
    source) is NOT erased immediately, it stays available for
    ``POST /api/history/<id>/restore`` during the grace period
    (``RECALL_SOFTDELETE_GRACE_DAYS``, default 7d; physically removed by
    ``app.services.retention.purge_soft_deleted`` at server start).
    Bulk-delete (``/api/history/bulk_delete``) stays physical - undo
    is deliberately supported only for a single delete.
    """
    try:
        with _get_db() as conn:
            row = conn.execute(
                'SELECT id FROM transcriptions WHERE id = ? AND deleted_at IS NULL',
                (transcription_id,),
            ).fetchone()
            if not row:
                return jsonify({"success": False, "error": tr('transcript.err.tx_not_found')}), 404
            conn.execute(
                'UPDATE transcriptions SET deleted_at = ? WHERE id = ?',
                (time.time(), transcription_id),
            )
            conn.commit()
        return jsonify({"success": True, "id": transcription_id})
    except Exception as e:
        logger.error(f"Transcription delete error: {e}")
        return jsonify({"success": False, "error": tr('transcript.err.delete_failed')}), 500


@transcription_bp.route('/api/history/<int:transcription_id>/restore', methods=['POST'])
def restore_transcription(transcription_id):
    """Undo a soft-delete. 404 if the record does not exist or is not deleted."""
    try:
        with _get_db() as conn:
            row = conn.execute(
                'SELECT id FROM transcriptions WHERE id = ? AND deleted_at IS NOT NULL',
                (transcription_id,),
            ).fetchone()
            if not row:
                return jsonify({"success": False, "error": tr('transcript.err.nothing_to_restore')}), 404
            conn.execute(
                'UPDATE transcriptions SET deleted_at = NULL WHERE id = ?',
                (transcription_id,),
            )
            conn.commit()
        return jsonify({"success": True, "id": transcription_id})
    except Exception as e:
        logger.error(f"Transcription restore error: {e}")
        return jsonify({"success": False, "error": tr('transcript.err.restore_failed')}), 500


@transcription_bp.route('/api/history/bulk_delete', methods=['POST'])
def bulk_delete_transcriptions():
    """Bulk delete."""
    try:
        data = request.json
        ids = data.get('ids', [])
        if not ids:
            return jsonify({"success": False, "error": tr('transcript.err.delete_ids_missing')}), 400
        try:
            ids = [int(id) for id in ids]
        except ValueError:
            return jsonify({"success": False, "error": tr('transcript.err.ids_format_invalid')}), 400

        with _get_db() as conn:
            placeholders = ','.join('?' * len(ids))
            files_info = conn.execute(
                f'SELECT id, file_path, source_type FROM transcriptions WHERE id IN ({placeholders})',
                ids,
            ).fetchall()
            # Phase 10.3: clean the speaker map before deleting transcriptions
            conn.execute(
                f'DELETE FROM transcription_speaker_map WHERE transcription_id IN ({placeholders})',
                ids,
            )
            conn.execute(
                f'DELETE FROM transcript_versions WHERE transcription_id IN ({placeholders})',
                ids,
            )
            conn.execute(f'DELETE FROM transcriptions WHERE id IN ({placeholders})', ids)
            conn.commit()

        deleted_files = 0
        # Phase 12.11: file + youtube → delete. recording → no.
        for file_info in files_info:
            if file_info['source_type'] in ('youtube', 'file') and file_info['file_path']:
                try:
                    if os.path.exists(file_info['file_path']):
                        os.remove(file_info['file_path'])
                        deleted_files += 1
                except Exception as e:
                    logger.error(f"File delete error {file_info['file_path']}: {e}")
        return jsonify({
            "message": tr('transcript.msg.deleted_n', n=len(files_info)),
            "deleted_files": deleted_files,
        })
    except Exception as e:
        logger.error(f"Bulk delete error: {e}")
        return jsonify({"success": False, "error": tr('transcript.err.bulk_delete_failed')}), 500


@transcription_bp.route('/api/history/bulk_export', methods=['POST'])
def bulk_export_transcriptions():
    """Bulk export (JSON/TXT/MD).

    Phase 10.9: for diarized transcripts TXT and MD contain speaker
    prefixes (Name: text). JSON includes the resolved speakers map per item.
    """
    try:
        data = request.json
        ids = data.get('ids', [])
        export_format = data.get('format', 'json')
        if not ids:
            return jsonify({"success": False, "error": tr('transcript.err.export_ids_missing')}), 400
        try:
            ids = [int(id) for id in ids]
        except ValueError:
            return jsonify({"success": False, "error": tr('transcript.err.ids_format_invalid')}), 400

        with _get_db() as conn:
            placeholders = ','.join('?' * len(ids))
            transcriptions = conn.execute(
                f'SELECT * FROM transcriptions WHERE id IN ({placeholders})',
                ids,
            ).fetchall()
            if not transcriptions:
                return jsonify({"success": False, "error": tr('transcript.err.tx_not_found')}), 404

            # Phase 10.9: pull the speaker maps for all transcripts in a single query
            speaker_maps_rows = conn.execute(f'''
                SELECT m.transcription_id, m.raw_label, s.name, s.is_self
                FROM transcription_speaker_map m
                LEFT JOIN speakers s ON s.id = m.speaker_id
                WHERE m.transcription_id IN ({placeholders})
            ''', ids).fetchall()

        # Build per-transcription speaker map: {tid: [{raw_label, name, is_self}]}
        speaker_maps_by_tid: dict[int, list[dict]] = {}
        for r in speaker_maps_rows:
            speaker_maps_by_tid.setdefault(r['transcription_id'], []).append({
                'raw_label': r['raw_label'],
                'name': r['name'],
                'is_self': bool(r['is_self']) if r['is_self'] is not None else False,
            })

        def _resolver(speakers_list):
            """Returns a resolve(raw_label) → display name function."""
            display = {s['raw_label']: s['name'] for s in speakers_list if s.get('name')}
            def _r(raw_label: str) -> str:
                if not raw_label:
                    return ''
                if raw_label in display:
                    return display[raw_label]
                if raw_label == 'self':
                    return tr('svc_media.export.you')
                if raw_label == 'SPEAKER_UNKNOWN':
                    return '?'
                m = re.match(r'^SPEAKER_(\d+)$', raw_label)
                return tr('svc_media.export.speaker_n', n=int(m.group(1)) + 1) if m else raw_label
            return _r

        def _fmt_ts(seconds: float) -> str:
            mins, secs = divmod(int(seconds), 60)
            hrs, mins = divmod(mins, 60)
            return f'{hrs:02d}:{mins:02d}:{secs:02d}' if hrs else f'{mins:02d}:{secs:02d}'

        export_data = []
        for t in transcriptions:
            item = dict(t)
            item['segments'] = json.loads(item['segments']) if item['segments'] else []
            item['speakers'] = speaker_maps_by_tid.get(item['id'], [])
            # The owner's comments (Wave 5) - one query per record. Cheap
            # (index on target) and negligible at bulk-export scale compared with
            # fetching the transcripts themselves.
            item['comments'] = _export_comments(item['id'])
            item['display_name'] = record_meta.display_name(item)
            export_data.append(item)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if export_format == 'json':
            export_path = os.path.join(current_app.config['TRANSCRIPTS_FOLDER'], f"bulk_export_{timestamp}.json")
            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2, default=str)
        elif export_format == 'txt':
            export_path = os.path.join(current_app.config['TRANSCRIPTS_FOLDER'], f"bulk_export_{timestamp}.txt")
            with open(export_path, 'w', encoding='utf-8') as f:
                for item in export_data:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"{tr('svc_media.export.source')}: {item['display_name']}\n")
                    f.write(f"{tr('svc_media.export.date')}: {item['created_at']}\n")
                    f.write(f"{tr('svc_media.export.language')}: {item['language']}\n")
                    f.write(f"{'='*60}\n\n")
                    f.write(_comments_text(item.get('comments') or []))
                    segs = item['segments']
                    has_sp = any(s.get('speaker') for s in segs)
                    if has_sp and segs:
                        resolve = _resolver(item['speakers'])
                        prev = None
                        for seg in segs:
                            sp = resolve(seg.get('speaker', ''))
                            text = (seg.get('text') or '').strip()
                            if not text:
                                continue
                            if sp != prev:
                                if prev is not None:
                                    f.write('\n\n')
                                f.write(f'{sp}: {text}')
                                prev = sp
                            else:
                                f.write(f' {text}')
                        f.write('\n')
                    else:
                        f.write(item.get('transcript_text', ''))
                    f.write("\n\n")
        elif export_format == 'docx':
            try:
                from docx import Document
                from docx.shared import Pt
            except ImportError:
                return jsonify({
                    "success": False,
                    "error": tr('transcript.err.docx_missing'),
                }), 500
            export_path = os.path.join(current_app.config['TRANSCRIPTS_FOLDER'], f"bulk_export_{timestamp}.docx")
            doc = Document()
            for idx, item in enumerate(export_data):
                if idx > 0:
                    doc.add_page_break()
                doc.add_heading(item['display_name'] or tr('svc_media.export.transcript'), level=1)
                meta = doc.add_paragraph()
                meta.add_run(f'{tr("svc_media.export.date")}: {item["created_at"]}   ·   '
                             f'{tr("svc_media.export.language")}: {item["language"]}').italic = True
                for c in (item.get('comments') or []):
                    p = doc.add_paragraph()
                    ts = _cm_ts(c.get('anchor_time'))
                    p.add_run(f'{_cm_label(c.get("kind"))}'
                              f'{" · " + ts if ts else ""}: ').bold = True
                    p.add_run(c.get('body') or '')
                    p.paragraph_format.space_after = Pt(6)
                segs = item['segments']
                has_sp = any(s.get('speaker') for s in segs)
                if has_sp and segs:
                    from collections import Counter
                    resolve = _resolver(item['speakers'])
                    spk_counts = Counter(
                        resolve(s.get('speaker', ''))
                        for s in segs if (s.get('text') or '').strip()
                    )
                    sp_p = doc.add_paragraph()
                    sp_p.add_run(f'{tr("svc_media.export.speakers")}: ').bold = True
                    sp_p.add_run(', '.join(f'{n} ({c})' for n, c in spk_counts.most_common()))
                    doc.add_paragraph()

                    prev = None
                    turn: list[str] = []
                    turn_start = 0.0
                    def _flush():
                        if prev and turn:
                            h = doc.add_heading(level=3)
                            h.add_run(prev).bold = True
                            h.add_run(f'   ·   {_fmt_ts(turn_start)}').italic = True
                            p = doc.add_paragraph(' '.join(turn))
                            p.paragraph_format.space_after = Pt(8)
                    for seg in segs:
                        sp = resolve(seg.get('speaker', ''))
                        text = (seg.get('text') or '').strip()
                        if not text:
                            continue
                        if sp != prev:
                            _flush()
                            prev = sp
                            turn = [text]
                            turn_start = float(seg.get('start', 0.0))
                        else:
                            turn.append(text)
                    _flush()
                else:
                    doc.add_paragraph(item.get('transcript_text', ''))
            doc.save(export_path)
        elif export_format == 'md':
            export_path = os.path.join(current_app.config['TRANSCRIPTS_FOLDER'], f"bulk_export_{timestamp}.md")
            with open(export_path, 'w', encoding='utf-8') as f:
                for item in export_data:
                    f.write(f'# {item["display_name"]}\n\n')
                    f.write(f'**{tr("svc_media.export.date")}:** {item["created_at"]} · '
                            f'**{tr("svc_media.export.language")}:** {item["language"]}\n\n')
                    f.write(_comments_md(item.get('comments') or []))
                    segs = item['segments']
                    has_sp = any(s.get('speaker') for s in segs)
                    if has_sp and segs:
                        from collections import Counter
                        resolve = _resolver(item['speakers'])
                        spk_counts = Counter(
                            resolve(s.get('speaker', ''))
                            for s in segs if (s.get('text') or '').strip()
                        )
                        spk_line = ', '.join(f'**{n}** ({c})' for n, c in spk_counts.most_common())
                        f.write(f'**{tr("svc_media.export.speakers")}:** {spk_line}\n\n---\n\n')

                        prev = None
                        turn: list[str] = []
                        turn_start = 0.0
                        def _flush():
                            if prev and turn:
                                f.write(f'### {prev} · `{_fmt_ts(turn_start)}`\n\n')
                                f.write(' '.join(turn))
                                f.write('\n\n')
                        for seg in segs:
                            sp = resolve(seg.get('speaker', ''))
                            text = (seg.get('text') or '').strip()
                            if not text:
                                continue
                            if sp != prev:
                                _flush()
                                prev = sp
                                turn = [text]
                                turn_start = float(seg.get('start', 0.0))
                            else:
                                turn.append(text)
                        _flush()
                    else:
                        f.write('---\n\n')
                        f.write(item.get('transcript_text', ''))
                        f.write('\n\n')
        else:
            return jsonify({"success": False, "error": tr('transcript.err.export_format_unsupported')}), 400

        response = send_file(export_path, as_attachment=True)

        @response.call_on_close
        def _cleanup_export():
            try:
                if os.path.exists(export_path):
                    os.remove(export_path)
            except Exception:
                logger.debug("export cleanup failed for %s (best-effort)", export_path, exc_info=True)
        return response
    except Exception as e:
        logger.error(f"Bulk export error: {e}")
        return jsonify({"success": False, "error": tr('transcript.err.bulk_export_failed')}), 500


@transcription_bp.route('/api/transcription/<int:transcription_id>/audio', methods=['GET'])
def get_transcription_audio(transcription_id):
    """Phase 12.3: stream the transcription's source audio file for the player.

    Range headers are supported by send_file automatically - the browser can seek
    into the middle of the file for click-on-segment-to-play.
    """
    with _get_db() as conn:
        row = conn.execute(
            'SELECT file_path, source_type FROM transcriptions WHERE id = ? AND deleted_at IS NULL',
            (transcription_id,),
        ).fetchone()
    if not row:
        return jsonify({"success": False, "error": tr('transcript.err.tx_not_found2')}), 404
    file_path = row['file_path']
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"success": False, "error": tr('transcript.err.audio_unavailable')}), 404
    # Path-traversal protection: file_path must be in the known directories
    allowed_roots = [
        current_app.config.get('UPLOAD_FOLDER', ''),
        os.path.join(current_app.config.get('BASE_DIR', '.'), 'youtube_downloads'),
        os.path.join(current_app.config.get('BASE_DIR', '.'), 'recordings'),
    ]
    abs_path = safe_path_within_any(allowed_roots, file_path)
    if abs_path is None:
        logger.warning(f"Audio request blocked (path outside allowed roots): {file_path}")
        return jsonify({"success": False, "error": "Access denied"}), 403
    return send_file(abs_path, conditional=True)


# ============================================================
# Phase 12.9: Manual segment merge / split
# ============================================================

def _save_segments(conn, transcription_id: int, segments: list[dict]) -> None:
    """Saves the segments JSON and updates transcript_text.

    transcript_text = ' '.join(seg.text) - used in FTS5 and
    the /api/history list. The FTS triggers are updated automatically.
    If polished_text exists - it is NOT overwritten (keeps the Claude version).
    """
    transcript_text = ' '.join(s.get('text', '').strip() for s in segments).strip()
    conn.execute(
        'UPDATE transcriptions SET segments = ?, transcript_text = ? WHERE id = ?',
        (json.dumps(segments, ensure_ascii=False), transcript_text, transcription_id),
    )
    # Invalidate the summary cache (the text changed).
    conn.execute(
        'UPDATE transcriptions SET summary_json = NULL, summary_at = NULL, '
        'summary_model = NULL WHERE id = ?',
        (transcription_id,),
    )


@transcription_bp.route('/api/transcription/<int:transcription_id>/segments/merge', methods=['POST'])
def merge_segments(transcription_id):
    """Merge several consecutive segments into one.

    Body: {"indices": [i, i+1, ...]} - must be consecutive numbers.
    Speaker is taken from the first, time = [first.start, last.end],
    text = ' '.join of all trimmed texts, words = concat (if present).
    """
    data = request.get_json(silent=True) or {}
    indices = data.get('indices')
    if not isinstance(indices, list) or len(indices) < 2:
        return jsonify({'success': False, 'error': tr('transcript.err.indices_list')}), 400
    try:
        indices = sorted(set(int(x) for x in indices))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': tr('transcript.err.indices_numbers')}), 400
    # Sequence check
    if any(indices[k+1] - indices[k] != 1 for k in range(len(indices) - 1)):
        return jsonify({'success': False, 'error': tr('transcript.err.indices_sequential')}), 400

    with _get_db() as conn:
        row = conn.execute(
            'SELECT segments FROM transcriptions WHERE id = ?',
            (transcription_id,),
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': tr('transcript.err.tx_not_found2')}), 404
        segments = json.loads(row['segments']) if row['segments'] else []
        if not segments:
            return jsonify({'success': False, 'error': tr('transcript.err.no_segments')}), 400
        if indices[0] < 0 or indices[-1] >= len(segments):
            return jsonify({'success': False, 'error': tr('transcript.err.indices_range')}), 400

        first, last = segments[indices[0]], segments[indices[-1]]
        merged = {
            'id': first.get('id', indices[0]),
            'start': first.get('start', 0.0),
            'end': last.get('end', last.get('start', 0.0)),
            'text': ' '.join((segments[i].get('text') or '').strip() for i in indices).strip(),
            'speaker': first.get('speaker'),
        }
        # Concat words if present
        words: list = []
        for i in indices:
            w = segments[i].get('words')
            if isinstance(w, list):
                words.extend(w)
        if words:
            merged['words'] = words

        new_segments = segments[:indices[0]] + [merged] + segments[indices[-1] + 1:]
        # Re-id sequentially
        for k, s in enumerate(new_segments):
            s['id'] = k
        _save_segments(conn, transcription_id, new_segments)
        conn.commit()

    logger.info(f"[segments/merge] tx={transcription_id}: merged {len(indices)} segs at {indices[0]}")
    return jsonify({'success': True, 'segments': new_segments, 'merged_index': indices[0]})


@transcription_bp.route('/api/transcription/<int:transcription_id>/segments/split', methods=['POST'])
def split_segment(transcription_id):
    """Split segments[index] at the split_at_seconds point.

    Body: {
        "index": int,
        "split_at_seconds": float,    // absolute time (between seg.start and seg.end)
        "right_speaker": str|null     // optional - change the speaker of the right half
    }

    The text is split proportionally: if words[] exist - at the nearest word
    boundary, otherwise by the character ratio (split_at - start) / duration.
    """
    data = request.get_json(silent=True) or {}
    try:
        index = int(data.get('index'))
        split_at = float(data.get('split_at_seconds'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': tr('transcript.err.split_args_invalid')}), 400
    right_speaker = data.get('right_speaker')  # None = lefthand speaker

    with _get_db() as conn:
        row = conn.execute(
            'SELECT segments FROM transcriptions WHERE id = ?',
            (transcription_id,),
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': tr('transcript.err.tx_not_found2')}), 404
        segments = json.loads(row['segments']) if row['segments'] else []
        if index < 0 or index >= len(segments):
            return jsonify({'success': False, 'error': tr('transcript.err.index_range')}), 400

        seg = segments[index]
        start = float(seg.get('start', 0))
        end = float(seg.get('end', start))
        if not (start < split_at < end):
            return jsonify({
                'success': False,
                'error': tr('transcript.err.split_inside', split_at=f'{split_at:.2f}', start=f'{start:.2f}', end=f'{end:.2f}'),
            }), 400

        text = (seg.get('text') or '').strip()
        words = seg.get('words') if isinstance(seg.get('words'), list) else None

        left_text = ''
        right_text = ''
        left_words = None
        right_words = None
        if words:
            left_words = []
            right_words = []
            for w in words:
                w_start = float(w.get('start', start))
                if w_start < split_at:
                    left_words.append(w)
                else:
                    right_words.append(w)
            left_text = ' '.join((w.get('word') or w.get('text') or '').strip() for w in left_words).strip()
            right_text = ' '.join((w.get('word') or w.get('text') or '').strip() for w in right_words).strip()
        if not left_text and not right_text:
            # Fallback — character ratio
            ratio = (split_at - start) / max(0.001, end - start)
            cut = max(1, min(len(text) - 1, int(round(len(text) * ratio))))
            # Snap to a word boundary (whitespace)
            while cut > 0 and not text[cut].isspace():
                cut -= 1
            if cut == 0:
                cut = max(1, int(round(len(text) * ratio)))
            left_text = text[:cut].strip()
            right_text = text[cut:].strip()

        left_seg = {
            'id': index,
            'start': start,
            'end': split_at,
            'text': left_text,
            'speaker': seg.get('speaker'),
        }
        if left_words:
            left_seg['words'] = left_words
        right_seg = {
            'id': index + 1,
            'start': split_at,
            'end': end,
            'text': right_text,
            'speaker': right_speaker if right_speaker else seg.get('speaker'),
        }
        if right_words:
            right_seg['words'] = right_words

        new_segments = segments[:index] + [left_seg, right_seg] + segments[index + 1:]
        for k, s in enumerate(new_segments):
            s['id'] = k
        _save_segments(conn, transcription_id, new_segments)
        conn.commit()

    logger.info(f"[segments/split] tx={transcription_id}: split index {index} at {split_at:.2f}s")
    return jsonify({'success': True, 'segments': new_segments, 'left_index': index})


# ============================================================
# T7.7: the five Claude endpoints below (polish/summarize/sentiment/translate/topics)
# are thin views over `app/services/transcript_ai.py`, which owns the cache columns,
# the Claude calls and the write-back. `_load_speaker_context` and
# `_call_claude_endpoint` stay importable here for their existing tests.
# ============================================================

_load_speaker_context = transcript_ai._load_speaker_context


def _call_claude_endpoint(label: str, fn, *args, error_msg: str | None = None, **kwargs):
    """Run a text_polishing.* call with the standard error handling.

    Returns (result, None) on success, or (None, (response, status)) on
    error — callers do ``result, err = _call_claude_endpoint(...); if err:
    return err``. error_msg is intentionally per-call — the five original
    endpoints don't all use the same wording, and this doesn't change that.
    """
    try:
        return fn(*args, **kwargs), None
    except RuntimeError as e:
        return None, (jsonify({"success": False, "error": str(e)}), 400)
    except Exception as e:
        logger.error(f"[{label}] error: {e}", exc_info=True)
        return None, (jsonify({"success": False, "error": error_msg or tr('transcript.err.claude_api_logs')}), 500)


def _ai_sync(kind: str, transcription_id: int, **kw):
    """A sync Claude route: ``{force, model}`` from the body, ``transcript_ai.run`` inline."""
    data = request.get_json(silent=True) or {}
    try:
        out = transcript_ai.run(kind, transcription_id, model=data.get('model'),
                                force=bool(data.get('force', False)),
                                db_path=current_app.config['DATABASE'], **kw)
    except transcript_ai.TranscriptAIError as e:
        return jsonify(e.sync_body()), e.status
    return jsonify({"success": True, "cached": out["cached"], **out["result"]})


@transcription_bp.route('/api/transcription/<int:transcription_id>/polish', methods=['POST'])
def polish_transcription(transcription_id):
    """Improve the transcript via the Claude API. Caches the result in the DB.

    Phase 10.7: for diarized transcripts (segments have a speaker field)
    polish_diarized_transcript is used automatically, which keeps the
    speaker markup in the 'Name: text' format.
    """
    return _ai_sync('polish', transcription_id)


@transcription_bp.route('/api/transcription/<int:transcription_id>/summarize', methods=['POST'])
def summarize_transcription(transcription_id):
    """Phase 12.5: generate summary + key points + action items via Claude.

    Caches the JSON in summary_json. Body: {"force": bool, "model": str}.
    """
    return _ai_sync('summarize', transcription_id)


@transcription_bp.route('/api/transcription/<int:transcription_id>/sentiment', methods=['POST'])
def sentiment_transcription(transcription_id):
    """Phase 12.25: per-speaker sentiment analysis. Requires a diarized transcript."""
    return _ai_sync('sentiment', transcription_id)


@transcription_bp.route('/api/transcription/<int:transcription_id>/translate', methods=['POST'])
def translate_transcription(transcription_id):
    """Phase 12.11: transcript translation via Claude.

    Body: {"target_lang": "en"|"uk"|"ru", "force": bool, "model": str}.
    Caches in translations_json: {lang_code: {text, model, created_at}}.
    """
    data = request.get_json(silent=True) or {}
    return _ai_sync('translate', transcription_id, target_lang=data.get('target_lang'))


@transcription_bp.route('/api/transcription/<int:transcription_id>/topics', methods=['POST'])
def extract_topics_endpoint(transcription_id):
    """Phase 12.19: extract topic tags via Claude. Cache in topics_json."""
    return _ai_sync('topics', transcription_id)


_AI_JOB_FIELDS = frozenset({'kind', 'target_lang'})


@transcription_bp.route('/api/transcription/<int:transcription_id>/ai-job', methods=['POST'])
def transcript_ai_job(transcription_id):
    """Run one Claude action in the background. Body: ``{kind, target_lang?}`` only.

    A cached result answers 200 at once with no job; otherwise 202 with the job_id, or 200
    ``created: false`` with the live job for the same (record, kind, language). No forced
    regeneration here. ``enrich`` is refused while a recording runs."""
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - _AI_JOB_FIELDS:
        return _async_bad('transcript.err.ai_unknown_field')
    kind = data.get('kind')
    if kind not in transcript_ai.KINDS:
        return _async_bad('transcript.err.ai_kind_invalid')
    target_lang = data.get('target_lang')
    if kind == 'translate' and target_lang not in transcript_ai.TARGET_LANGS:
        return _async_bad('transcript.err.ai_target_lang_invalid')
    try:
        out = transcript_ai.submit(kind, transcription_id,
                                   target_lang=target_lang if kind == 'translate' else None,
                                   actor=auth.classify_request()[0],
                                   db_path=current_app.config['DATABASE'])
    except transcript_ai.TranscriptAIError as e:
        return jsonify(e.body()), e.status
    if out['cached']:
        return jsonify({"success": True, "cached": True, "status": "done", "result": out['result']})
    if not out['created']:
        return jsonify({"success": True, "cached": False, "created": False, "job_id": out['job_id']})
    return jsonify({"success": True, "cached": False, "created": True, "job_id": out['job_id']}), 202


#: Comment kinds that have an export label. Labelled server-side, not in the UI: the export
#: is read by a person or another system, and a bare "correction" slug in the middle of a
#: document would look like service junk.
_CM_KINDS = ("correction", "decision", "note", "context", "question")


def _cm_label(kind) -> str:
    return tr(f"svc_media.export.kind.{kind if kind in _CM_KINDS else 'note'}")


def _export_comments(transcription_id) -> list[dict]:
    """The owner's comments for the export. Empty if there are none, there is no id, or
    the layer has not been migrated yet - the export must not fail because of an add-on."""
    if not transcription_id:
        return []
    try:
        from app.services import comments as comments_svc
        return comments_svc.list_for(current_app.config['DATABASE'],
                                     'transcription', int(transcription_id))
    except Exception:
        logger.debug("[export] comments not attached for tx=%s",
                     transcription_id, exc_info=True)
        return []


def _cm_ts(seconds) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _comments_text(rows: list[dict], heading: str | None = None) -> str:
    """Comments block for text exports.

    Goes BEFORE the transcript, not as an appendix at the end: the point of the layer is that
    the owner's clarifications weigh more than the raw transcript, and in the document this should
    read in the same order in which search works. An explicit frame is needed so that
    the export does not confuse what was said on the call with what was added afterwards.
    """
    if not rows:
        return ""
    heading = heading or tr("svc_media.export.comments.heading").upper()
    out = [heading, "=" * len(heading),
           f"({tr('svc_media.export.comments.note')})", ""]
    for c in rows:
        ts = _cm_ts(c.get("anchor_time"))
        head = f"[{_cm_label(c.get('kind'))}]"
        if c.get("pinned"):
            head += f" [{tr('svc_media.export.comments.pinned')}]"
        if ts:
            head += f" ~{ts}"
        head += f" · {str(c.get('created_at') or '')[:10]}"
        out.append(head)
        out.append(c.get("body") or "")
        out.append("")
    out.append("-" * 60)
    out.append("")
    return "\n".join(out)


def _comments_md(rows: list[dict]) -> str:
    if not rows:
        return ""
    out = [f"## {tr('svc_media.export.comments.heading')}", "",
           f"_{tr('svc_media.export.comments.note')}_", ""]
    for c in rows:
        ts = _cm_ts(c.get("anchor_time"))
        bits = [f"**{_cm_label(c.get('kind'))}**"]
        if c.get("pinned"):
            bits.append(tr("svc_media.export.comments.pinned"))
        if ts:
            bits.append(f"`{ts}`")
        bits.append(str(c.get("created_at") or "")[:10])
        out.append("- " + " · ".join(bits))
        out.append(f"  {(c.get('body') or '').strip()}")
        out.append("")
    out.append("---")
    out.append("")
    return "\n".join(out)


@transcription_bp.route('/api/export/<format>', methods=['POST'])
def export_transcript(format):
    """Export a single transcript (TXT/SRT/MD/DOCX/JSON/PDF).

    Phase 10.5: if segments have a 'speaker' field (raw_label) and the body contains
    'speakers' (a list with resolved name), add the prefix 'Name: …' in TXT/SRT.
    For unnamed labels - the fallback 'Спікер N' (by order of appearance).
    """
    data = request.json
    text = data.get('text', '')
    segments = data.get('segments', [])
    # editable-title-description-02: the heading in the content and the name of the
    # generated file - display_name (title → source_name → «Запис #id»);
    # the source_name field in the JSON payload stays untouched (provenance).
    # When the payload has neither title, nor source_name, nor id - we take the old
    # neutral fallback: `display_name` would give «Запис #None» in the document
    # heading and in the file name.
    if any(data.get(k) for k in ('title', 'source_name', 'id')):
        record_display_name = record_meta.display_name(data)
    else:
        record_display_name = 'transcript'
    speakers_list = data.get('speakers', []) or []

    # Build raw_label → display_name map (Phase 10.5)
    speaker_display: dict[str, str] = {}
    for s in speakers_list:
        rl = s.get('raw_label')
        if rl and s.get('name'):
            speaker_display[rl] = s['name']
    # Fallback for unnamed: SPEAKER_NN → "Спікер N+1", self → "Ви", UNKNOWN → "?"
    def resolve_speaker(raw_label: str) -> str:
        if not raw_label:
            return ''
        if raw_label in speaker_display:
            return speaker_display[raw_label]
        if raw_label == 'self':
            return tr('svc_media.export.you')
        if raw_label == 'SPEAKER_UNKNOWN':
            return '?'
        m = re.match(r'^SPEAKER_(\d+)$', raw_label)
        if m:
            return tr('svc_media.export.speaker_n', n=int(m.group(1)) + 1)
        return raw_label

    has_speakers = any(s.get('speaker') for s in segments)

    # The owner's comments (Wave 5). They go into txt/md/docx/json - i.e. the formats
    # a person reads or a system re-ingests. SRT deliberately has none:
    # the subtitle track is synchronous with the audio, and inserting a line that was not spoken
    # on the tape would spoil the format itself.
    export_comments = _export_comments(data.get('id'))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_source_name = re.sub(r'[^\w\s-]', '', record_display_name)
    safe_source_name = re.sub(r'[-\s]+', '-', safe_source_name)[:50]

    transcripts_dir = os.path.abspath(current_app.config['TRANSCRIPTS_FOLDER'])

    if format == 'txt':
        export_path = os.path.abspath(os.path.join(transcripts_dir, f"{timestamp}_{safe_source_name}.txt"))
        if not export_path.startswith(transcripts_dir):
            logger.error(f"Path traversal attempt on export: {export_path}")
            return jsonify({"success": False, "error": tr('transcript.err.filename_invalid')}), 400
        with open(export_path, 'w', encoding='utf-8') as f:
            f.write(_comments_text(export_comments))
            if has_speakers and segments:
                # Group consecutive segments by speaker for readability
                prev_speaker = None
                for seg in segments:
                    sp = resolve_speaker(seg.get('speaker', ''))
                    seg_text = seg.get('text', '').strip()
                    if not seg_text:
                        continue
                    if sp != prev_speaker:
                        if prev_speaker is not None:
                            f.write('\n\n')
                        f.write(f'{sp}: ')
                        prev_speaker = sp
                    else:
                        f.write(' ')
                    f.write(seg_text)
                f.write('\n')
            else:
                f.write(text)
    elif format == 'srt':
        export_path = os.path.abspath(os.path.join(transcripts_dir, f"{timestamp}_{safe_source_name}.srt"))
        if not export_path.startswith(transcripts_dir):
            logger.error(f"Path traversal attempt on export: {export_path}")
            return jsonify({"success": False, "error": tr('transcript.err.filename_invalid')}), 400
        with open(export_path, 'w', encoding='utf-8') as f:
            for i, segment in enumerate(segments, 1):
                f.write(f"{i}\n")
                f.write(f"{format_srt_timestamp(segment['start'])} --> {format_srt_timestamp(segment['end'])}\n")
                seg_text = segment['text']
                sp_label = segment.get('speaker')
                if has_speakers and sp_label:
                    f.write(f"{resolve_speaker(sp_label)}: {seg_text}\n\n")
                else:
                    f.write(f"{seg_text}\n\n")
    elif format == 'md':
        # Phase 10.9: Markdown is the best format for dialog transcripts.
        # A heading with a speaker summary, secondary headers per turn with a timestamp.
        export_path = os.path.abspath(os.path.join(transcripts_dir, f"{timestamp}_{safe_source_name}.md"))
        if not export_path.startswith(transcripts_dir):
            logger.error(f"Path traversal attempt on export: {export_path}")
            return jsonify({"success": False, "error": tr('transcript.err.filename_invalid')}), 400

        def _fmt_ts(seconds: float) -> str:
            mins, secs = divmod(int(seconds), 60)
            hrs, mins = divmod(mins, 60)
            return f'{hrs:02d}:{mins:02d}:{secs:02d}' if hrs else f'{mins:02d}:{secs:02d}'

        with open(export_path, 'w', encoding='utf-8') as f:
            f.write(f'# {record_display_name}\n\n')
            f.write(_comments_md(export_comments))
            if has_speakers and segments:
                # Speaker summary
                from collections import Counter
                spk_counts = Counter(resolve_speaker(s.get('speaker', '')) for s in segments if s.get('text', '').strip())
                spk_line = ', '.join(f'**{name}** ({n})' for name, n in spk_counts.most_common())
                f.write(f'**{tr("svc_media.export.speakers")}:** {spk_line}\n\n---\n\n')

                # Group consecutive segments by speaker
                prev_speaker = None
                turn_text: list[str] = []
                turn_start = 0.0
                def _flush():
                    if prev_speaker and turn_text:
                        f.write(f'### {prev_speaker} · `{_fmt_ts(turn_start)}`\n\n')
                        f.write(' '.join(turn_text))
                        f.write('\n\n')
                for seg in segments:
                    sp = resolve_speaker(seg.get('speaker', ''))
                    seg_text = (seg.get('text') or '').strip()
                    if not seg_text:
                        continue
                    if sp != prev_speaker:
                        _flush()
                        prev_speaker = sp
                        turn_text = [seg_text]
                        turn_start = float(seg.get('start', 0.0))
                    else:
                        turn_text.append(seg_text)
                _flush()
            else:
                # Fallback: plain text without speakers
                f.write(text or '')
                f.write('\n')
    elif format == 'docx':
        # Phase 10.11: DOCX via python-docx - for a Word archive, handing over to
        # a client, printing. Keeps the speaker structure as H3 headings.
        try:
            from docx import Document
            from docx.shared import Pt, RGBColor
        except ImportError:
            return jsonify({
                "success": False,
                "error": tr('transcript.err.docx_missing_hint'),
            }), 500

        export_path = os.path.abspath(os.path.join(transcripts_dir, f"{timestamp}_{safe_source_name}.docx"))
        if not export_path.startswith(transcripts_dir):
            logger.error(f"Path traversal attempt on export: {export_path}")
            return jsonify({"success": False, "error": tr('transcript.err.filename_invalid')}), 400

        def _fmt_ts(seconds: float) -> str:
            mins, secs = divmod(int(seconds), 60)
            hrs, mins = divmod(mins, 60)
            return f'{hrs:02d}:{mins:02d}:{secs:02d}' if hrs else f'{mins:02d}:{secs:02d}'

        doc = Document()
        doc.add_heading(record_display_name or tr('svc_media.export.transcript'), level=1)

        if export_comments:
            doc.add_heading(tr('svc_media.export.comments.heading'), level=2)
            note = doc.add_paragraph()
            note.add_run(tr('svc_media.export.comments.note')).italic = True
            for c in export_comments:
                p = doc.add_paragraph()
                head = _cm_label(c.get('kind'))
                ts = _cm_ts(c.get('anchor_time'))
                p.add_run(f'{head}{" · " + ts if ts else ""}: ').bold = True
                p.add_run(c.get('body') or '')
                p.paragraph_format.space_after = Pt(6)
            doc.add_paragraph()

        if has_speakers and segments:
            from collections import Counter
            spk_counts = Counter(
                resolve_speaker(s.get('speaker', ''))
                for s in segments if (s.get('text') or '').strip()
            )
            summary_p = doc.add_paragraph()
            summary_p.add_run(f'{tr("svc_media.export.speakers")}: ').bold = True
            spk_parts = [f'{name} ({n})' for name, n in spk_counts.most_common()]
            summary_p.add_run(', '.join(spk_parts))
            doc.add_paragraph()  # spacer

            # Group consecutive same-speaker segments into turns
            prev_speaker = None
            turn_text: list[str] = []
            turn_start = 0.0
            def _flush():
                if prev_speaker and turn_text:
                    h = doc.add_heading(level=3)
                    run = h.add_run(prev_speaker)
                    run.bold = True
                    h.add_run(f'   ·   {_fmt_ts(turn_start)}').italic = True
                    p = doc.add_paragraph(' '.join(turn_text))
                    p.paragraph_format.space_after = Pt(8)
            for seg in segments:
                sp = resolve_speaker(seg.get('speaker', ''))
                seg_text = (seg.get('text') or '').strip()
                if not seg_text:
                    continue
                if sp != prev_speaker:
                    _flush()
                    prev_speaker = sp
                    turn_text = [seg_text]
                    turn_start = float(seg.get('start', 0.0))
                else:
                    turn_text.append(seg_text)
            _flush()
        else:
            doc.add_paragraph(text or '')

        doc.save(export_path)
    elif format == 'json':
        export_path = os.path.abspath(os.path.join(transcripts_dir, f"{timestamp}_{safe_source_name}.json"))
        if not export_path.startswith(transcripts_dir):
            logger.error(f"Path traversal attempt on export: {export_path}")
            return jsonify({"success": False, "error": tr('transcript.err.filename_invalid')}), 400
        with open(export_path, 'w', encoding='utf-8') as f:
            # A separate field, not mixed in with the segments: the JSON consumer must
            # see that this is a different layer - deliberately written about the recording, not
            # recognized from it.
            payload = dict(data)
            payload['display_name'] = record_display_name
            if export_comments:
                payload['comments'] = export_comments
            json.dump(payload, f, ensure_ascii=False, indent=2)
    elif format == 'pdf':
        # Phase 12.13: PDF export via reportlab. Cyrillic-friendly font (Arial from Windows).
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import cm
            from reportlab.lib.colors import HexColor
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
        except ImportError:
            return jsonify({"success": False, "error": tr('transcript.err.reportlab_missing')}), 500

        export_path = os.path.abspath(os.path.join(transcripts_dir, f"{timestamp}_{safe_source_name}.pdf"))
        if not export_path.startswith(transcripts_dir):
            logger.error(f"Path traversal attempt on export: {export_path}")
            return jsonify({"success": False, "error": tr('transcript.err.filename_invalid')}), 400

        # Register Arial for cyrillic. It is present on Windows; on other OSes fallback to Helvetica.
        # registerFont is global state; repeated calls with the same name are OK (no-op).
        font_name = 'Helvetica'
        font_bold = 'Helvetica-Bold'
        registered = getattr(pdfmetrics, '_fonts', None) or {}
        for ttf, bold_ttf, name, name_bold in [
            ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf', 'CyrArial', 'CyrArial-Bold'),
            ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
             '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 'DejaVu', 'DejaVu-Bold'),
        ]:
            if not os.path.exists(ttf):
                continue
            try:
                if name not in registered:
                    pdfmetrics.registerFont(TTFont(name, ttf))
                font_name = name
                if os.path.exists(bold_ttf):
                    if name_bold not in registered:
                        pdfmetrics.registerFont(TTFont(name_bold, bold_ttf))
                    font_bold = name_bold
                else:
                    font_bold = name  # fallback - the same font (no bold variant)
                logger.debug(f"[pdf] fonts: regular={font_name}, bold={font_bold}")
                break
            except Exception as e:
                logger.warning(f"[pdf] font registration failed for {name}: {e}")
                continue

        doc = SimpleDocTemplate(
            export_path, pagesize=A4,
            leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm,
            title=record_display_name, author='Whisper UI',
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'TitleC', parent=styles['Title'],
            fontName=font_bold, fontSize=18, alignment=0, spaceAfter=12,
        )
        meta_style = ParagraphStyle(
            'Meta', parent=styles['Normal'],
            fontName=font_name, fontSize=9, textColor=HexColor('#64748b'), spaceAfter=24,
        )
        speaker_style = ParagraphStyle(
            'Speaker', parent=styles['Normal'],
            fontName=font_bold, fontSize=10, textColor=HexColor('#3B82F6'),
            spaceBefore=10, spaceAfter=2,
        )
        body_style = ParagraphStyle(
            'Body', parent=styles['Normal'],
            fontName=font_name, fontSize=11, leading=15, spaceAfter=6,
        )

        def _esc(s: str) -> str:
            # Reportlab paragraph parses minimal HTML; escape < > & so as not to break it.
            return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        story = []
        story.append(Paragraph(_esc(record_display_name), title_style))
        meta_parts = [datetime.now().strftime('%Y-%m-%d %H:%M')]
        if data.get('language'):
            meta_parts.append(f"{tr('svc_media.export.language').lower()}: {data['language']}")
        if data.get('model'):
            meta_parts.append(f"{tr('svc_media.export.model').lower()}: {data['model']}")
        story.append(Paragraph(' · '.join(meta_parts), meta_style))

        if has_speakers and segments:
            prev_speaker = None
            chunk: list[str] = []
            for seg in segments:
                sp = resolve_speaker(seg.get('speaker', ''))
                seg_text = (seg.get('text') or '').strip()
                if not seg_text:
                    continue
                if sp != prev_speaker:
                    if chunk:
                        story.append(Paragraph(_esc(' '.join(chunk)), body_style))
                        chunk = []
                    story.append(Paragraph(_esc(sp), speaker_style))
                    prev_speaker = sp
                chunk.append(seg_text)
            if chunk:
                story.append(Paragraph(_esc(' '.join(chunk)), body_style))
        else:
            for paragraph in (text or '').split('\n\n'):
                if paragraph.strip():
                    story.append(Paragraph(_esc(paragraph), body_style))
                    story.append(Spacer(1, 6))

        doc.build(story)
    else:
        return jsonify({"success": False, "error": tr('transcript.err.format_unsupported')}), 400

    response = send_file(export_path, as_attachment=True)

    @response.call_on_close
    def _cleanup_export():
        try:
            if os.path.exists(export_path):
                os.remove(export_path)
        except Exception:
            logger.debug("export cleanup failed for %s (best-effort)", export_path, exc_info=True)
    return response


# ============================================================
# Phase 23 (Story B-B): Video analysis endpoints
# ============================================================

def _allowed_roots_for_media():
    """Return the list of allowed absolute root paths for media serving.

    Covers uploads, youtube_downloads, recordings/sessions (screen video +
    keyframe images written by video_analysis service).
    """
    base = current_app.config.get('BASE_DIR', '.')
    # realpath (not abspath) so symlinks are resolved on both sides of the
    # comparison — prevents a symlinked file inside an allowed root from
    # pointing outside it (defence-in-depth; served paths come from our DB).
    roots = [
        os.path.realpath(current_app.config.get('UPLOAD_FOLDER', '')),
        os.path.realpath(os.path.join(str(base), 'youtube_downloads')),
        os.path.realpath(os.path.join(str(base), 'recordings')),
    ]
    # Also include RECORDING_DIR explicitly (recordings/sessions) in case
    # it is configured to a non-default location.
    rec_dir = current_app.config.get('RECORDING_DIR')
    if rec_dir:
        roots.append(os.path.realpath(str(rec_dir)))
    return [r for r in roots if r]


def _check_path_allowed(path: str) -> bool:
    """Return True if path (resolved via realpath) is under an allowed media root."""
    return safe_path_within_any(_allowed_roots_for_media(), path) is not None


_ANALYZE_FLOAT = ('step', 'min_res', 'threshold', 'settle')


def _running_video_job(tid):
    """The already-running analysis task for this recording (so as not to queue a duplicate)."""
    if state.job_queue is None:
        return None
    for job in state.job_queue.list(kind='video_analysis', active_only=True):
        if (job.meta or {}).get('transcription_id') == tid:
            return job
    return None


def _max_frames_default():
    from app.services import video_frames
    try:
        from app.core import settings as _s
        return _s.env_int('RECORDING_VIDEO_ANALYSIS_MAX_FRAMES') or video_frames.DEFAULT_MAX_FRAMES
    except Exception:
        logger.debug("RECORDING_VIDEO_ANALYSIS_MAX_FRAMES could not be read", exc_info=True)
        return video_frames.DEFAULT_MAX_FRAMES


@transcription_bp.route('/api/transcription/<int:tid>/analyze-video', methods=['POST'])
def analyze_video_endpoint(tid):
    """Slicing a recording into key frames + OCR (+ description only with describe) as a background task.

    Body (JSON, all optional): force, method ('thumbs'|'bisect'), step, min_res,
    threshold, settle, ignore_regions ([[x, y, w, h]] as fractions of the frame), max_frames,
    ocr (default true), describe (default false), vision_backend ('local'|'claude').
    → {"success": true, "job_id"} or {"success": true, "ready": true} (already computed with
    the same parameters), or {"success": false, "error_code": "NO_VIDEO"}.
    """
    data = request.get_json(silent=True) or {}
    vision_backend = data.get('vision_backend')
    if vision_backend not in ('local', 'claude', None):
        return jsonify({'success': False, 'error': tr('transcript.err.vision_backend')}), 400
    method = data.get('method')
    if method not in ('thumbs', 'bisect', None):
        return jsonify({'success': False, 'error': tr('transcript.err.method_invalid')}), 400
    # An explicit vision_backend without describe is a request for a description: that is what the UI checkbox
    # «Claude-опис» (transcript.js) sends, and before the new pipeline it described frames.
    describe = bool(data['describe']) if 'describe' in data else vision_backend is not None
    kwargs = {'method': method, 'ocr': bool(data.get('ocr', True)), 'describe': describe}
    try:
        for k in _ANALYZE_FLOAT:
            if data.get(k) is not None:
                kwargs[k] = float(data[k])
        if data.get('max_frames') is not None:
            kwargs['max_frames'] = int(data['max_frames'])
        regions = data.get('ignore_regions')
        if regions:
            kwargs['ignore_regions'] = [[float(v) for v in r][:4] for r in regions]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': tr('transcript.err.params_numbers')}), 400
    force = bool(data.get('force', False))

    from app.services import video_analysis
    db_path = current_app.config['DATABASE']
    if video_analysis.resolve_recording_video(db_path, tid) is None:
        return jsonify({'success': False, 'reason': 'no video', 'error_code': 'NO_VIDEO',
                        'error': tr('transcript.err.no_video_in_record', tid=tid)})
    if state.job_queue is None:
        return jsonify({'success': False, 'reason': 'job_queue_unavailable'}), 503

    running = _running_video_job(tid)
    if running is not None:
        return jsonify({'success': True, 'job_id': running.id, 'already_running': True,
                        'progress': (running.meta or {}).get('progress')})
    if not force:
        try:
            vf = video_analysis.vf
            params = video_analysis._params(
                method=kwargs['method'] or vf.DEFAULT_METHOD,
                step=kwargs.get('step') or vf.DEFAULT_STEP,
                min_res=kwargs.get('min_res') or vf.DEFAULT_MIN_RES,
                threshold=kwargs.get('threshold', vf.DEFAULT_THRESHOLD),
                settle=kwargs.get('settle', vf.DEFAULT_SETTLE),
                ignore_regions=kwargs.get('ignore_regions'),
                max_frames=kwargs.get('max_frames') or _max_frames_default(),
                ocr=kwargs['ocr'], describe=kwargs['describe'], vision_backend=vision_backend)
            with _get_db() as conn:
                row = conn.execute('SELECT video_analysis_at, video_analysis_params '
                                   'FROM transcriptions WHERE id = ?', (tid,)).fetchone()
            if row and row['video_analysis_at'] and \
                    json.loads(row['video_analysis_params'] or 'null') == params:
                return jsonify({'success': True, 'ready': True})
        except Exception:
            logger.debug("check for a ready analysis tid=%s failed - queueing the task", tid,
                         exc_info=True)

    def _video_job(job, _db=db_path, _tid=tid, _force=force, _vb=vision_backend, _kw=kwargs):
        return video_analysis.analyze_video(_db, _tid, force=_force, job=job,
                                            vision_backend=_vb, **_kw)

    job = state.job_queue.submit('video_analysis', _video_job,
                                 meta={'transcription_id': tid})
    return jsonify({'success': True, 'job_id': job.id})



@transcription_bp.route('/api/transcription/<int:tid>/recording-info', methods=['GET'])
def recording_info(tid):
    """Phase 23 B-B: Return video/recording availability metadata for a transcription.

    Never 500 — degrades gracefully to has_video=False on any error.
    """
    result = {
        'has_video': False,
        'recording_session_id': None,
        'primary_video_available': False,
        'start_offset_sec': 0.0,
        'video_analysis_at': None,
        'keyframes_count': 0,
    }
    try:
        from app.services import video_analysis
        db_path = current_app.config['DATABASE']

        # Fetch video_analysis_at and video_keyframes_count from transcriptions.
        with _get_db() as conn:
            row = conn.execute(
                'SELECT video_analysis_at, video_keyframes_count '
                'FROM transcriptions WHERE id = ?',
                (tid,),
            ).fetchone()
        if row:
            result['video_analysis_at'] = row['video_analysis_at']
            result['keyframes_count'] = row['video_keyframes_count'] or 0

        video_info = video_analysis.resolve_recording_video(db_path, tid)
        if video_info is None:
            return jsonify(result)

        result['has_video'] = True
        result['recording_session_id'] = video_info.get('recording_session_id')
        result['start_offset_sec'] = float(video_info.get('start_offset_sec') or 0.0)

        primary_path = video_info.get('primary_video_path')
        result['primary_video_available'] = bool(
            primary_path and os.path.isfile(primary_path)
        )
    except Exception as exc:
        logger.warning("recording_info error for tid=%s (degrading): %s", tid, exc)

    return jsonify(result)


@transcription_bp.route('/api/transcription/<int:tid>/video', methods=['GET'])
def get_transcription_video(tid):
    """Phase 23 B-B: Stream screen recording video for a transcription.

    Supports HTTP Range requests (seek) via send_file conditional=True.
    """
    try:
        from app.services import video_analysis
        db_path = current_app.config['DATABASE']
        video_info = video_analysis.resolve_recording_video(db_path, tid)
    except Exception as exc:
        logger.error("resolve_recording_video error for tid=%s: %s", tid, exc)
        return jsonify({'success': False, 'error': tr('transcript.err.video_lookup_failed')}), 500

    if video_info is None:
        return jsonify({'success': False, 'error': tr('transcript.err.video_not_found')}), 404

    primary_path = video_info.get('primary_video_path')
    if not primary_path or not os.path.isfile(primary_path):
        return jsonify({'success': False, 'error': tr('transcript.err.video_unavailable')}), 404

    abs_path = os.path.realpath(primary_path)
    if not _check_path_allowed(abs_path):
        logger.warning("Video request blocked (path outside allowed roots): %s", abs_path)
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    return send_file(abs_path, mimetype='video/mp4', conditional=True)


@transcription_bp.route('/api/transcription/<int:tid>/keyframes', methods=['GET'])
def list_keyframes(tid):
    """Phase 23 B-B: List keyframes extracted for a transcription.

    Returns {"keyframes": [{"id", "ts_offset_sec", "ocr_excerpt"}]} ordered
    by ts_offset_sec.  Never 500 — returns empty list on missing table/data.
    """
    try:
        with _get_db() as conn:
            rows = conn.execute(
                'SELECT id, ts_offset_sec, ocr_text, vision_text '
                'FROM video_keyframes '
                'WHERE transcription_id = ? '
                'ORDER BY ts_offset_sec ASC',
                (tid,),
            ).fetchall()
        keyframes = [
            {
                'id': r['id'],
                'ts_offset_sec': r['ts_offset_sec'],
                'ocr_excerpt': (r['ocr_text'] or '')[:80],
                'vision_excerpt': (r['vision_text'] or '')[:200],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("list_keyframes error for tid=%s: %s", tid, exc)
        keyframes = []

    return jsonify({'keyframes': keyframes})


@transcription_bp.route('/api/transcription/<int:tid>/keyframe/<int:kf_id>', methods=['GET'])
def get_keyframe_image(tid, kf_id):
    """Representative frame of a segment. Images are not stored - cut from the video
    on the fly, secret boxes are painted over."""
    try:
        with _get_db() as conn:
            row = conn.execute(
                'SELECT ts_offset_sec, video_time_sec FROM video_keyframes '
                'WHERE id = ? AND transcription_id = ?', (kf_id, tid)).fetchone()
    except Exception as exc:
        logger.error("keyframe lookup error kf_id=%s tid=%s: %s", kf_id, tid, exc)
        return jsonify({'success': False, 'error': tr('transcript.err.db_error')}), 500
    if not row:
        return jsonify({'success': False, 'error': tr('transcript.err.keyframe_not_found')}), 404
    from app.services import video_analysis
    info = video_analysis.resolve_recording_video(current_app.config['DATABASE'], tid) or {}
    t = (float(row['video_time_sec']) + float(info.get('start_offset_sec') or 0.0)) \
        if row['video_time_sec'] is not None else float(row['ts_offset_sec'])
    return _frame_response(tid, t, request.args.get('size', 'thumb'))


_TIMELINE_STATUS = {'NOT_FOUND': 404, 'NO_VIDEO': 404, 'BAD_REQUEST': 400, 'NOT_ANALYZED': 409}


def _timeline_error(exc, extra=None):
    body = {'success': False, 'error_code': exc.code, 'error': str(exc)}
    body.update(extra or {})
    return jsonify(body), _TIMELINE_STATUS.get(exc.code, 400)


def _frame_response(tid, t, size):
    from app.services import video_timeline as vt
    try:
        img, meta = vt.render_frame(current_app.config['DATABASE'], tid, t, size=size)
    except vt.TimelineError as exc:
        return _timeline_error(exc)
    except Exception:
        logger.exception("frame tid=%s t=%s not served", tid, t)
        return jsonify({'success': False, 'error': tr('transcript.err.frame_cut_failed')}), 500
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=82)
    buf.seek(0)
    resp = send_file(buf, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Recall-Redacted-Boxes'] = str(meta.get('redacted_boxes', 0))
    return resp


def _float_arg(name):
    v = request.args.get(name)
    if v in (None, ''):
        return None
    return float(v)


def _video_job_status(tid):
    job = _running_video_job(tid)
    if job is None:
        return None
    return {'status': 'running', 'job_id': job.id, 'progress': (job.meta or {}).get('progress')}


@transcription_bp.route('/api/transcription/<int:tid>/frame', methods=['GET'])
def get_frame_image(tid):
    """Frame at moment t (timeline time, s) as JPEG; size=thumb|full. Secrets are painted over."""
    try:
        t = _float_arg('t')
    except ValueError:
        return jsonify({'success': False, 'error': tr('transcript.err.t_number')}), 400
    if t is None:
        return jsonify({'success': False, 'error': tr('transcript.err.t_required')}), 400
    return _frame_response(tid, t, request.args.get('size', 'thumb'))


@transcription_bp.route('/api/transcription/<int:tid>/frames', methods=['GET'])
def get_frames_json(tid):
    """Frames as base64-JPEG for MCP: one by t or segment representatives in [from, to]."""
    from app.services import video_timeline as vt
    try:
        res = vt.frames(current_app.config['DATABASE'], tid, t=_float_arg('t'),
                        start=_float_arg('from'), end=_float_arg('to'),
                        limit=int(request.args.get('limit', 6)),
                        size=request.args.get('size', 'thumb'),
                        include_camera=request.args.get('include_camera') in ('1', 'true'))
    except ValueError:
        return jsonify({'success': False, 'error': tr('transcript.err.tfl_numbers')}), 400
    except vt.TimelineError as exc:
        return _timeline_error(exc, _video_job_status(tid))
    except Exception:
        logger.exception("frames tid=%s not served", tid)
        return jsonify({'success': False, 'error': tr('transcript.err.frames_cut_failed')}), 500
    return jsonify(res)


@transcription_bp.route('/api/transcription/<int:tid>/visual-timeline', methods=['GET'])
def get_visual_timeline(tid):
    """The "frame + phrases" timeline with pagination; format=json|markdown."""
    from app.services import video_timeline as vt
    try:
        res = vt.build(current_app.config['DATABASE'], tid, start=_float_arg('from'),
                       end=_float_arg('to'),
                       with_transcript=request.args.get('with_transcript', '1') not in ('0', 'false'),
                       include_camera=request.args.get('include_camera') in ('1', 'true'),
                       shift=_float_arg('offset_sec') or 0.0,
                       limit=int(request.args.get('limit', 50)),
                       offset=int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'success': False, 'error': tr('transcript.err.numeric_params')}), 400
    except vt.TimelineError as exc:
        return _timeline_error(exc)
    running = _video_job_status(tid)
    if running:
        res.update(running)
    if request.args.get('format') == 'markdown':
        return jsonify({'transcription_id': tid, 'status': res.get('status'),
                        'next_offset': res.get('next_offset'), 'total': res.get('total'),
                        'markdown': vt.to_markdown(res)})
    return jsonify(res)


@transcription_bp.route('/api/transcription/<int:tid>/frames-for-text', methods=['GET'])
def get_frames_for_text(tid):
    """A phrase (query) or a segment index (segment_index) → frames in the ±window s window."""
    from app.services import video_timeline as vt
    try:
        seg = request.args.get('segment_index')
        res = vt.frames_for_text(current_app.config['DATABASE'], tid,
                                 query=request.args.get('query'),
                                 segment_index=int(seg) if seg not in (None, '') else None,
                                 window=_float_arg('window') or 5.0,
                                 shift=_float_arg('offset_sec') or 0.0)
    except ValueError:
        return jsonify({'success': False, 'error': tr('transcript.err.numeric_params')}), 400
    except vt.TimelineError as exc:
        return _timeline_error(exc)
    return jsonify(res)


@transcription_bp.route('/api/video/search', methods=['GET'])
def search_visual_endpoint():
    """Search over OCR and frame descriptions across the whole archive (text is already masked)."""
    from app.services import video_timeline as vt
    ids = [x for x in (request.args.get('transcription_ids') or '').split(',') if x.strip()]
    try:
        res = vt.search_visual(current_app.config['DATABASE'], request.args.get('q', ''),
                               transcription_ids=[int(x) for x in ids] or None,
                               date_from=request.args.get('date_from'),
                               date_to=request.args.get('date_to'),
                               limit=int(request.args.get('limit', 20)))
    except ValueError:
        return jsonify({'success': False, 'error': tr('transcript.err.ids_limit_numbers')}), 400
    except vt.TimelineError as exc:
        return _timeline_error(exc)
    return jsonify(res)
