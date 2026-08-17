"""Phase 12.9: тести merge_segments + split_segment endpoints.

Покриваємо:
- Merge consecutive segments — time/text/speaker/words concat.
- Split segment — proportional text division з/без words[].
- Validation: non-consecutive indices, out of bounds, single index.
- summary_json кеш інвалідується.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database


SAMPLE_SEGMENTS = [
    {"id": 0, "start": 0.0, "end": 2.0, "text": "Привіт усім.", "speaker": "SPEAKER_00"},
    {"id": 1, "start": 2.0, "end": 4.0, "text": "Як справи?", "speaker": "SPEAKER_00"},
    {"id": 2, "start": 4.0, "end": 6.0, "text": "Дуже добре.", "speaker": "self"},
    {"id": 3, "start": 6.0, "end": 8.0, "text": "Дякую за запитання.", "speaker": "self"},
]


@pytest.fixture
def app_and_tx(tmp_path: Path):
    db_path = str(tmp_path / 'segments_test.db')
    init_database(db_path)
    transcripts_dir = str(tmp_path / 'transcripts')
    os.makedirs(transcripts_dir, exist_ok=True)
    upload_dir = str(tmp_path / 'uploads')
    os.makedirs(upload_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, segments, "
              "language, summary_json) VALUES (?, ?, ?, ?, ?, ?)",
              ('file', 'test', 'Привіт усім. Як справи? Дуже добре. Дякую за запитання.',
               json.dumps(SAMPLE_SEGMENTS), 'uk', '{"summary":"cached"}'))
    tx_id = c.lastrowid
    conn.commit()
    conn.close()

    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.config['TRANSCRIPTS_FOLDER'] = transcripts_dir
    app.config['UPLOAD_FOLDER'] = upload_dir
    app.config['BASE_DIR'] = str(tmp_path)
    app.register_blueprint(transcription_bp)
    return app.test_client(), db_path, tx_id


def _read_segments(db_path: str, tx_id: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    row = conn.execute('SELECT segments FROM transcriptions WHERE id = ?', (tx_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row and row[0] else []


class TestMergeSegments:
    def test_basic_merge_two(self, app_and_tx):
        client, db_path, tx_id = app_and_tx
        r = client.post(f'/api/transcription/{tx_id}/segments/merge',
                        json={'indices': [0, 1]})
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        segs = _read_segments(db_path, tx_id)
        assert len(segs) == 3
        # Перший — merged
        assert segs[0]['start'] == 0.0
        assert segs[0]['end'] == 4.0
        assert segs[0]['speaker'] == 'SPEAKER_00'
        assert 'Привіт усім' in segs[0]['text']
        assert 'Як справи' in segs[0]['text']

    def test_merge_three(self, app_and_tx):
        client, db_path, tx_id = app_and_tx
        r = client.post(f'/api/transcription/{tx_id}/segments/merge',
                        json={'indices': [1, 2, 3]})
        assert r.status_code == 200
        segs = _read_segments(db_path, tx_id)
        assert len(segs) == 2
        assert segs[1]['start'] == 2.0
        assert segs[1]['end'] == 8.0

    def test_non_consecutive_rejected(self, app_and_tx):
        client, _, tx_id = app_and_tx
        r = client.post(f'/api/transcription/{tx_id}/segments/merge',
                        json={'indices': [0, 2]})
        assert r.status_code == 400
        assert 'послідовні' in r.get_json()['error']

    def test_single_index_rejected(self, app_and_tx):
        client, _, tx_id = app_and_tx
        r = client.post(f'/api/transcription/{tx_id}/segments/merge',
                        json={'indices': [0]})
        assert r.status_code == 400

    def test_out_of_bounds_rejected(self, app_and_tx):
        client, _, tx_id = app_and_tx
        r = client.post(f'/api/transcription/{tx_id}/segments/merge',
                        json={'indices': [3, 4, 5]})
        assert r.status_code == 400

    def test_unknown_transcription_404(self, app_and_tx):
        client, _, _ = app_and_tx
        r = client.post('/api/transcription/99999/segments/merge',
                        json={'indices': [0, 1]})
        assert r.status_code == 404

    def test_summary_cache_invalidated(self, app_and_tx):
        """Phase 12.9 spec: після segment edit summary_json має скидатись."""
        client, db_path, tx_id = app_and_tx
        client.post(f'/api/transcription/{tx_id}/segments/merge',
                    json={'indices': [0, 1]})
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT summary_json FROM transcriptions WHERE id = ?',
                           (tx_id,)).fetchone()
        conn.close()
        assert row[0] is None

    def test_transcript_text_updated(self, app_and_tx):
        """Після merge transcript_text має містити склеєний текст усіх сегментів."""
        client, db_path, tx_id = app_and_tx
        client.post(f'/api/transcription/{tx_id}/segments/merge',
                    json={'indices': [0, 1]})
        conn = sqlite3.connect(db_path)
        text = conn.execute('SELECT transcript_text FROM transcriptions WHERE id = ?',
                            (tx_id,)).fetchone()[0]
        conn.close()
        assert 'Привіт усім' in text
        assert 'Як справи' in text


class TestSplitSegment:
    def test_basic_split(self, app_and_tx):
        client, db_path, tx_id = app_and_tx
        # Split seg 0 (0.0-2.0) at 1.0
        r = client.post(f'/api/transcription/{tx_id}/segments/split',
                        json={'index': 0, 'split_at_seconds': 1.0})
        assert r.status_code == 200
        segs = _read_segments(db_path, tx_id)
        assert len(segs) == 5
        assert segs[0]['start'] == 0.0
        assert segs[0]['end'] == 1.0
        assert segs[1]['start'] == 1.0
        assert segs[1]['end'] == 2.0
        assert segs[0]['speaker'] == segs[1]['speaker']

    def test_split_with_right_speaker(self, app_and_tx):
        client, db_path, tx_id = app_and_tx
        r = client.post(f'/api/transcription/{tx_id}/segments/split',
                        json={'index': 0, 'split_at_seconds': 1.0,
                              'right_speaker': 'self'})
        assert r.status_code == 200
        segs = _read_segments(db_path, tx_id)
        assert segs[0]['speaker'] == 'SPEAKER_00'  # left unchanged
        assert segs[1]['speaker'] == 'self'  # right overridden

    def test_split_outside_bounds_rejected(self, app_and_tx):
        client, _, tx_id = app_and_tx
        # split_at = 5.0 але segment 0 — це 0.0-2.0
        r = client.post(f'/api/transcription/{tx_id}/segments/split',
                        json={'index': 0, 'split_at_seconds': 5.0})
        assert r.status_code == 400

    def test_split_at_boundary_rejected(self, app_and_tx):
        client, _, tx_id = app_and_tx
        # split_at = end exactly — не валідний
        r = client.post(f'/api/transcription/{tx_id}/segments/split',
                        json={'index': 0, 'split_at_seconds': 2.0})
        assert r.status_code == 400

    def test_split_invalid_index(self, app_and_tx):
        client, _, tx_id = app_and_tx
        r = client.post(f'/api/transcription/{tx_id}/segments/split',
                        json={'index': 99, 'split_at_seconds': 1.0})
        assert r.status_code == 400

    def test_split_proportional_text_no_words(self, app_and_tx):
        """Без words[] текст ділиться приблизно по character ratio з прив'язкою
        до word boundary."""
        client, db_path, tx_id = app_and_tx
        # Split seg 0 ('Привіт усім.', 0.0-2.0) посередині (1.0)
        client.post(f'/api/transcription/{tx_id}/segments/split',
                    json={'index': 0, 'split_at_seconds': 1.0})
        segs = _read_segments(db_path, tx_id)
        # Текст має розділитись на дві частини, обидві непорожні
        assert segs[0]['text']
        assert segs[1]['text']
        # Обидва містять частину оригіналу
        combined = segs[0]['text'] + ' ' + segs[1]['text']
        assert 'Привіт' in combined and 'усім' in combined
