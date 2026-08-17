"""Phase 12.12: тести bookmarks + saved searches."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from app.blueprints.bookmarks import bookmarks_bp
from app.db.migrations import init_database


SAMPLE_SEGMENTS = [
    {"id": 0, "start": 0.0, "end": 5.0, "text": "Перший сегмент"},
    {"id": 1, "start": 5.0, "end": 10.0, "text": "Другий сегмент"},
    {"id": 2, "start": 10.0, "end": 15.0, "text": "Третій сегмент"},
]


@pytest.fixture
def client_and_tx(tmp_path: Path):
    db_path = str(tmp_path / 'bookmarks_test.db')
    init_database(db_path)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, segments, language) "
              "VALUES (?, ?, ?, ?, ?)",
              ('file', 'test', 'Перший сегмент Другий сегмент Третій сегмент',
               json.dumps(SAMPLE_SEGMENTS), 'uk'))
    tx_id = c.lastrowid
    conn.commit()
    conn.close()

    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(bookmarks_bp)
    return app.test_client(), tx_id


class TestBookmarks:
    def test_create_and_list(self, client_and_tx):
        c, tx_id = client_and_tx
        r = c.post(f'/api/transcription/{tx_id}/bookmarks',
                   json={'segment_index': 1, 'note': 'важливе'})
        assert r.status_code == 201
        assert r.get_json()['success'] is True

        r2 = c.get(f'/api/transcription/{tx_id}/bookmarks')
        bookmarks = r2.get_json()['bookmarks']
        assert len(bookmarks) == 1
        assert bookmarks[0]['segment_index'] == 1
        assert bookmarks[0]['segment_start'] == 5.0
        assert bookmarks[0]['note'] == 'важливе'

    def test_create_without_note(self, client_and_tx):
        c, tx_id = client_and_tx
        r = c.post(f'/api/transcription/{tx_id}/bookmarks',
                   json={'segment_index': 0})
        assert r.status_code == 201
        bookmarks = c.get(f'/api/transcription/{tx_id}/bookmarks').get_json()['bookmarks']
        assert bookmarks[0]['note'] is None

    def test_duplicate_returns_existing_with_update(self, client_and_tx):
        c, tx_id = client_and_tx
        c.post(f'/api/transcription/{tx_id}/bookmarks',
               json={'segment_index': 0, 'note': 'first'})
        # Друге POST на той же segment_index — оновлює note
        r = c.post(f'/api/transcription/{tx_id}/bookmarks',
                   json={'segment_index': 0, 'note': 'updated'})
        assert r.status_code == 200
        bookmarks = c.get(f'/api/transcription/{tx_id}/bookmarks').get_json()['bookmarks']
        assert len(bookmarks) == 1
        assert bookmarks[0]['note'] == 'updated'

    def test_invalid_segment_index_rejected(self, client_and_tx):
        c, tx_id = client_and_tx
        r = c.post(f'/api/transcription/{tx_id}/bookmarks',
                   json={'segment_index': 99})
        assert r.status_code == 400

    def test_negative_segment_index_rejected(self, client_and_tx):
        c, tx_id = client_and_tx
        r = c.post(f'/api/transcription/{tx_id}/bookmarks',
                   json={'segment_index': -1})
        assert r.status_code == 400

    def test_unknown_transcription_404(self, client_and_tx):
        c, _ = client_and_tx
        r = c.post('/api/transcription/99999/bookmarks',
                   json={'segment_index': 0})
        assert r.status_code == 404

    def test_delete_bookmark(self, client_and_tx):
        c, tx_id = client_and_tx
        r = c.post(f'/api/transcription/{tx_id}/bookmarks',
                   json={'segment_index': 0})
        bid = r.get_json()['id']
        r = c.delete(f'/api/bookmarks/{bid}')
        assert r.status_code == 200
        bookmarks = c.get(f'/api/transcription/{tx_id}/bookmarks').get_json()['bookmarks']
        assert len(bookmarks) == 0

    def test_patch_note(self, client_and_tx):
        c, tx_id = client_and_tx
        bid = c.post(f'/api/transcription/{tx_id}/bookmarks',
                     json={'segment_index': 0}).get_json()['id']
        r = c.patch(f'/api/bookmarks/{bid}', json={'note': 'updated note'})
        assert r.status_code == 200
        bookmarks = c.get(f'/api/transcription/{tx_id}/bookmarks').get_json()['bookmarks']
        assert bookmarks[0]['note'] == 'updated note'

    def test_bookmarks_sorted_by_time(self, client_and_tx):
        c, tx_id = client_and_tx
        # Create в reverse порядку — повинні повернутись sorted ASC
        c.post(f'/api/transcription/{tx_id}/bookmarks', json={'segment_index': 2})
        c.post(f'/api/transcription/{tx_id}/bookmarks', json={'segment_index': 0})
        c.post(f'/api/transcription/{tx_id}/bookmarks', json={'segment_index': 1})
        bookmarks = c.get(f'/api/transcription/{tx_id}/bookmarks').get_json()['bookmarks']
        starts = [b['segment_start'] for b in bookmarks]
        assert starts == [0.0, 5.0, 10.0]


class TestSavedSearches:
    def test_create_and_list(self, client_and_tx):
        c, _ = client_and_tx
        r = c.post('/api/saved-searches',
                   json={'name': 'My search', 'query': {'search': 'hello', 'speaker_id': 5}})
        assert r.status_code == 201

        r2 = c.get('/api/saved-searches')
        searches = r2.get_json()['searches']
        assert len(searches) == 1
        assert searches[0]['name'] == 'My search'
        assert searches[0]['query'] == {'search': 'hello', 'speaker_id': 5}

    def test_duplicate_name_rejected(self, client_and_tx):
        c, _ = client_and_tx
        c.post('/api/saved-searches', json={'name': 'Same', 'query': {}})
        r = c.post('/api/saved-searches', json={'name': 'Same', 'query': {}})
        assert r.status_code == 409

    def test_empty_name_rejected(self, client_and_tx):
        c, _ = client_and_tx
        r = c.post('/api/saved-searches', json={'name': '', 'query': {}})
        assert r.status_code == 400

    def test_long_name_rejected(self, client_and_tx):
        c, _ = client_and_tx
        r = c.post('/api/saved-searches', json={'name': 'x' * 200, 'query': {}})
        assert r.status_code == 400

    def test_use_count_increments(self, client_and_tx):
        c, _ = client_and_tx
        sid = c.post('/api/saved-searches', json={'name': 'Hot', 'query': {'search': 'x'}}).get_json()['id']
        c.post(f'/api/saved-searches/{sid}/use')
        c.post(f'/api/saved-searches/{sid}/use')
        c.post(f'/api/saved-searches/{sid}/use')
        searches = c.get('/api/saved-searches').get_json()['searches']
        assert searches[0]['use_count'] == 3
        assert searches[0]['last_used_at'] is not None

    def test_delete_search(self, client_and_tx):
        c, _ = client_and_tx
        sid = c.post('/api/saved-searches', json={'name': 'Tmp', 'query': {}}).get_json()['id']
        r = c.delete(f'/api/saved-searches/{sid}')
        assert r.status_code == 200
        searches = c.get('/api/saved-searches').get_json()['searches']
        assert len(searches) == 0

    def test_searches_sorted_by_use_count(self, client_and_tx):
        c, _ = client_and_tx
        sid_a = c.post('/api/saved-searches', json={'name': 'A', 'query': {}}).get_json()['id']
        sid_b = c.post('/api/saved-searches', json={'name': 'B', 'query': {}}).get_json()['id']
        # B використовуємо двічі
        c.post(f'/api/saved-searches/{sid_b}/use')
        c.post(f'/api/saved-searches/{sid_b}/use')
        c.post(f'/api/saved-searches/{sid_a}/use')
        searches = c.get('/api/saved-searches').get_json()['searches']
        assert searches[0]['name'] == 'B'  # higher use_count first
        assert searches[1]['name'] == 'A'
