"""Phase 12.12: Bookmarks (стар на сегменти) + saved searches.

Endpoints:
- GET    /api/transcription/<id>/bookmarks
- POST   /api/transcription/<id>/bookmarks   {segment_index, note?}
- DELETE /api/bookmarks/<id>
- PATCH  /api/bookmarks/<id>                 {note}
- GET    /api/saved-searches
- POST   /api/saved-searches                 {name, query: {search?, source_type?, language?, speaker_id?}}
- DELETE /api/saved-searches/<id>
- POST   /api/saved-searches/<id>/use        — інкремент use_count
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, current_app, jsonify, request

from app.repositories import transcriptions as tx_repo


logger = logging.getLogger(__name__)
bookmarks_bp = Blueprint('bookmarks', __name__)


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
        return jsonify({'success': False, 'error': 'segment_index мусить бути int >= 0'}), 400

    with _get_db() as conn:
        tx = tx_repo.get_by_id(conn, transcription_id, columns=('segments',))
        if not tx:
            return jsonify({'success': False, 'error': 'Транскрипт не знайдено'}), 404
        try:
            segments = json.loads(tx['segments']) if tx['segments'] else []
        except json.JSONDecodeError:
            segments = []
        if seg_idx >= len(segments):
            return jsonify({'success': False, 'error': 'segment_index виходить за межі'}), 400
        seg_start = float(segments[seg_idx].get('start', 0))

        c = conn.cursor()
        try:
            c.execute(
                'INSERT INTO segment_bookmarks (transcription_id, segment_index, segment_start, note) '
                'VALUES (?, ?, ?, ?)',
                (transcription_id, seg_idx, seg_start, note),
            )
        except Exception as e:
            # UNIQUE conflict — bookmark вже є; просто оновлюємо note якщо передано
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
        c = conn.cursor()
        c.execute('DELETE FROM segment_bookmarks WHERE id = ?', (bookmark_id,))
        if c.rowcount == 0:
            return jsonify({'success': False, 'error': 'Не знайдено'}), 404
        conn.commit()
    return jsonify({'success': True})


@bookmarks_bp.route('/api/bookmarks/<int:bookmark_id>', methods=['PATCH'])
def update_bookmark(bookmark_id):
    data = request.get_json(silent=True) or {}
    note = data.get('note')
    if note is not None and not isinstance(note, str):
        return jsonify({'success': False, 'error': 'note мусить бути string'}), 400
    with _get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE segment_bookmarks SET note = ? WHERE id = ?', (note, bookmark_id))
        if c.rowcount == 0:
            return jsonify({'success': False, 'error': 'Не знайдено'}), 404
        conn.commit()
    return jsonify({'success': True})


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
        return jsonify({'success': False, 'error': 'name 1-100 символів'}), 400
    if not isinstance(query, dict):
        return jsonify({'success': False, 'error': 'query мусить бути object'}), 400

    with _get_db() as conn:
        c = conn.cursor()
        try:
            c.execute(
                'INSERT INTO saved_searches (name, query_json) VALUES (?, ?)',
                (name, json.dumps(query, ensure_ascii=False)),
            )
        except Exception:
            return jsonify({'success': False, 'error': 'Search з таким ім\'ям уже існує'}), 409
        sid = c.lastrowid
        conn.commit()
    return jsonify({'success': True, 'id': sid}), 201


@bookmarks_bp.route('/api/saved-searches/<int:search_id>', methods=['DELETE'])
def delete_saved_search(search_id):
    with _get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM saved_searches WHERE id = ?', (search_id,))
        if c.rowcount == 0:
            return jsonify({'success': False, 'error': 'Не знайдено'}), 404
        conn.commit()
    return jsonify({'success': True})


@bookmarks_bp.route('/api/saved-searches/<int:search_id>/use', methods=['POST'])
def touch_saved_search(search_id):
    """Інкрементує use_count + оновлює last_used_at. Викликається фронтендом
    коли користувач клікає на saved search."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE saved_searches SET use_count = use_count + 1, '
            'last_used_at = CURRENT_TIMESTAMP WHERE id = ?',
            (search_id,),
        )
        if c.rowcount == 0:
            return jsonify({'success': False, 'error': 'Не знайдено'}), 404
        conn.commit()
    return jsonify({'success': True})
