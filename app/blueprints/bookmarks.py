"""Phase 12.12: Bookmarks (a star on segments) + saved searches.

Endpoints:
- GET    /api/transcription/<id>/bookmarks
- POST   /api/transcription/<id>/bookmarks   {segment_index, note?}
- DELETE /api/bookmarks/<id>
- PATCH  /api/bookmarks/<id>                 {note}
- GET    /api/bookmarks/<id>/clip?pad=<0..60> - zip of clip.mp3 + clip.txt
- GET    /api/saved-searches
- POST   /api/saved-searches                 {name, query: {search?, source_type?, language?, speaker_id?}}
- DELETE /api/saved-searches/<id>
- POST   /api/saved-searches/<id>/use        - increments use_count
"""
from __future__ import annotations

import io
import json
import logging

from flask import Blueprint, current_app, jsonify, request, send_file

from app.core import auth
from app.core.i18n import tr
from app.repositories import transcriptions as tx_repo
from app.services import bookmark_clip


logger = logging.getLogger(__name__)
bookmarks_bp = Blueprint('bookmarks', __name__)

_AGENT_NOTE_MAX = 2000


def _note_too_long(note):
    """Non-UI callers (agents) are capped; the UI is unchanged."""
    if isinstance(note, str) and len(note) > _AGENT_NOTE_MAX and auth.classify_request()[0] != 'ui':
        return (jsonify({'success': False, 'error_code': 'INVALID_ARGUMENT',
                         'error': tr('transcript.bm.note_too_long', max=_AGENT_NOTE_MAX)}), 400)
    return None


def _get_db():
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


# ---------------------------------------------------------------- bookmarks

@bookmarks_bp.route('/api/transcription/<int:transcription_id>/bookmarks', methods=['GET'])
def list_bookmarks(transcription_id):
    with _get_db() as conn:
        rows = conn.execute(
            'SELECT id, segment_index, segment_start, note, created_at '
            'FROM segment_bookmarks WHERE transcription_id = ? '
            'ORDER BY segment_start ASC',
            (transcription_id,),
        ).fetchall()
    return jsonify({'bookmarks': [dict(r) for r in rows]})


@bookmarks_bp.route('/api/transcription/<int:transcription_id>/bookmarks', methods=['POST'])
def create_bookmark(transcription_id):
    data = request.get_json(silent=True) or {}
    seg_idx = data.get('segment_index')
    note = data.get('note') or None
    if not isinstance(seg_idx, int) or seg_idx < 0:
        return jsonify({'success': False, 'error': tr('transcript.bm.seg_index_int')}), 400
    too_long = _note_too_long(note)
    if too_long:
        return too_long

    with _get_db() as conn:
        tx = tx_repo.get_by_id(conn, transcription_id, columns=('segments',))
        if not tx:
            return jsonify({'success': False, 'error': tr('transcript.err.tx_not_found2')}), 404
        try:
            segments = json.loads(tx['segments']) if tx['segments'] else []
        except json.JSONDecodeError:
            segments = []
        if seg_idx >= len(segments):
            return jsonify({'success': False, 'error': tr('transcript.bm.seg_index_range')}), 400
        seg_start = float(segments[seg_idx].get('start', 0))

        c = conn.cursor()
        try:
            c.execute(
                'INSERT INTO segment_bookmarks (transcription_id, segment_index, segment_start, note) '
                'VALUES (?, ?, ?, ?)',
                (transcription_id, seg_idx, seg_start, note),
            )
        except Exception as e:
            # UNIQUE conflict - the bookmark already exists; just update the note if given
            existing = conn.execute(
                'SELECT id FROM segment_bookmarks WHERE transcription_id = ? AND segment_index = ?',
                (transcription_id, seg_idx),
            ).fetchone()
            if existing:
                if note is not None:
                    conn.execute(
                        'UPDATE segment_bookmarks SET note = ? WHERE id = ?',
                        (note, existing['id']),
                    )
                conn.commit()
                return jsonify({'success': True, 'id': existing['id'], 'updated': True})
            return jsonify({'success': False, 'error': str(e)}), 500
        bid = c.lastrowid
        conn.commit()

    return jsonify({'success': True, 'id': bid}), 201


@bookmarks_bp.route('/api/bookmarks/<int:bookmark_id>', methods=['DELETE'])
def delete_bookmark(bookmark_id):
    with _get_db() as conn:
        row = conn.execute(
            'SELECT id, transcription_id, segment_index, note, created_at '
            'FROM segment_bookmarks WHERE id = ?', (bookmark_id,)).fetchone()
        if row is None:
            return jsonify({'success': False, 'error': tr('transcript.err.not_found')}), 404
        conn.execute('DELETE FROM segment_bookmarks WHERE id = ?', (bookmark_id,))
        conn.commit()
    return jsonify({'success': True, 'deleted_snapshot': dict(row)})


@bookmarks_bp.route('/api/bookmarks/<int:bookmark_id>', methods=['PATCH'])
def update_bookmark(bookmark_id):
    data = request.get_json(silent=True) or {}
    note = data.get('note')
    if note is not None and not isinstance(note, str):
        return jsonify({'success': False, 'error': tr('transcript.bm.note_string')}), 400
    too_long = _note_too_long(note)
    if too_long:
        return too_long
    with _get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE segment_bookmarks SET note = ? WHERE id = ?', (note, bookmark_id))
        if c.rowcount == 0:
            return jsonify({'success': False, 'error': tr('transcript.err.not_found')}), 404
        conn.commit()
    return jsonify({'success': True})


@bookmarks_bp.route('/api/bookmarks/<int:bookmark_id>/clip', methods=['GET'])
def bookmark_clip_zip(bookmark_id):
    """The audio around a bookmarked segment plus its text, as one zip attachment.
    The file is resolved and refused as in ``GET /api/transcription/<id>/audio``."""
    raw_pad = request.args.get('pad')
    try:
        pad = bookmark_clip.DEFAULT_PAD if raw_pad in (None, '') else float(raw_pad)
    except ValueError:
        pad = -1.0
    if not (0 <= pad <= bookmark_clip.MAX_PAD):
        return jsonify({'success': False, 'error_code': 'INVALID_ARGUMENT',
                        'error': tr('transcript.clip.pad_range', max=int(bookmark_clip.MAX_PAD))}), 400

    with _get_db() as conn:
        bm = conn.execute(
            'SELECT b.transcription_id, b.segment_index, b.note, t.segments, t.file_path '
            'FROM segment_bookmarks b JOIN transcriptions t ON t.id = b.transcription_id '
            'WHERE b.id = ? AND t.deleted_at IS NULL', (bookmark_id,)).fetchone()
    if bm is None:
        return jsonify({'success': False, 'error_code': 'NOT_FOUND',
                        'error': tr('transcript.err.not_found')}), 404
    try:
        segments = json.loads(bm['segments']) if bm['segments'] else []
    except json.JSONDecodeError:
        segments = []
    idx = bm['segment_index']
    if not (0 <= idx < len(segments)):
        return jsonify({'success': False, 'error_code': 'NOT_FOUND',
                        'error': tr('transcript.bm.seg_index_range')}), 404
    seg = segments[idx]

    abs_path, reason = bookmark_clip.resolve_audio(bm['file_path'], current_app.config)
    if reason == 'no_media':
        return jsonify({'success': False, 'error_code': 'NO_MEDIA',
                        'error': tr('transcript.err.audio_unavailable')}), 404
    if reason == 'forbidden':
        # Same body as the audio route's refusal.
        return jsonify({"success": False, "error": "Access denied"}), 403

    start = float(seg.get('start', 0) or 0)
    end = float(seg.get('end', start) or start)
    try:
        mp3 = bookmark_clip.cut(abs_path, start, end, pad=pad)
    except bookmark_clip.ClipUnavailable as e:
        logger.warning("bookmark clip %s failed: %s", bookmark_id, e)
        return jsonify({'success': False, 'error_code': 'UNAVAILABLE',
                        'error': tr('transcript.clip.unavailable')}), 503

    text = bookmark_clip.clip_text(seg, bm['note'], tr('transcript.clip.note_label'))
    data = bookmark_clip.build_zip(mp3, text)
    return send_file(io.BytesIO(data), mimetype='application/zip', as_attachment=True,
                     download_name=f"clip-{bm['transcription_id']}-{idx}.zip")


# ---------------------------------------------------------------- saved searches

@bookmarks_bp.route('/api/saved-searches', methods=['GET'])
def list_saved_searches():
    with _get_db() as conn:
        rows = conn.execute(
            'SELECT id, name, query_json, created_at, last_used_at, use_count '
            'FROM saved_searches ORDER BY use_count DESC, last_used_at DESC, name ASC'
        ).fetchall()
    return jsonify({'searches': [
        {
            'id': r['id'],
            'name': r['name'],
            'query': json.loads(r['query_json']) if r['query_json'] else {},
            'created_at': r['created_at'],
            'last_used_at': r['last_used_at'],
            'use_count': r['use_count'],
        } for r in rows
    ]})


@bookmarks_bp.route('/api/saved-searches', methods=['POST'])
def create_saved_search():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    query = data.get('query') or {}
    if not name or len(name) > 100:
        return jsonify({'success': False, 'error': tr('transcript.bm.name_len')}), 400
    if not isinstance(query, dict):
        return jsonify({'success': False, 'error': tr('transcript.bm.query_object')}), 400

    with _get_db() as conn:
        c = conn.cursor()
        try:
            c.execute(
                'INSERT INTO saved_searches (name, query_json) VALUES (?, ?)',
                (name, json.dumps(query, ensure_ascii=False)),
            )
        except Exception:
            return jsonify({'success': False, 'error': tr('transcript.bm.search_exists')}), 409
        sid = c.lastrowid
        conn.commit()
    return jsonify({'success': True, 'id': sid}), 201


@bookmarks_bp.route('/api/saved-searches/<int:search_id>', methods=['DELETE'])
def delete_saved_search(search_id):
    with _get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM saved_searches WHERE id = ?', (search_id,))
        if c.rowcount == 0:
            return jsonify({'success': False, 'error': tr('transcript.err.not_found')}), 404
        conn.commit()
    return jsonify({'success': True})


@bookmarks_bp.route('/api/saved-searches/<int:search_id>/use', methods=['POST'])
def touch_saved_search(search_id):
    """Increments use_count + updates last_used_at. Called by the frontend
    when the user clicks a saved search."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE saved_searches SET use_count = use_count + 1, '
            'last_used_at = CURRENT_TIMESTAMP WHERE id = ?',
            (search_id,),
        )
        if c.rowcount == 0:
            return jsonify({'success': False, 'error': tr('transcript.err.not_found')}), 404
        conn.commit()
    return jsonify({'success': True})
