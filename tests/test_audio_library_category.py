"""Тести напрямку (category) в Аудіотеці — COALESCE(транскрипт-напрямок, власний).

Phase 21 (recording UX): нетранскрибований запис має показуватись з власним
напрямком (audio_downloads.category_id) і ловитись фільтром по напрямку ще до
транскрипції. Коли транскрипт зʼявляється — його напрямок має пріоритет.
"""
from __future__ import annotations

import pytest
from flask import Flask

from app.blueprints.audio_library import audio_bp
from app.db.connection import get_db_connection
from app.db.migrations import init_database


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / 'lib.db')
    init_database(db_path)
    app = Flask(__name__)
    app.register_blueprint(audio_bp)
    app.config['TESTING'] = True
    app.config['DATABASE'] = db_path
    return app.test_client(), db_path


def _cat(db_path, name):
    with get_db_connection(db_path) as conn:
        cur = conn.execute('INSERT INTO categories (name, name_norm) VALUES (?, ?)',
                           (name, name.casefold()))
        conn.commit()
        return cur.lastrowid


def _add_recording(db_path, *, yid, title, file_path, category_id=None):
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, "
            "source_type, recording_session_id, category_id) "
            "VALUES (?, ?, ?, ?, 'recording', ?, ?)",
            (f'recording://{yid}', f'recording_{yid}', title, file_path, yid, category_id),
        )
        conn.commit()


def _add_transcript(db_path, *, file_path, category_id):
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, file_path, "
            "transcript_text, language, category_id) "
            "VALUES ('recording', 'T', ?, 'txt', 'uk', ?)",
            (file_path, category_id),
        )
        conn.commit()


def _downloads(client, **params):
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    return client.get('/api/audio/downloads' + ('?' + qs if qs else '')).get_json()


def test_untranscribed_recording_shows_own_category(client):
    cl, db = client
    cid = _cat(db, 'AI')
    _add_recording(db, yid='rec_a', title='Запис A', file_path='/x/a.mp3', category_id=cid)

    data = _downloads(cl)
    row = next(d for d in data['downloads'] if d['title'] == 'Запис A')
    assert row['category_id'] == cid           # власний напрямок видно
    assert row['transcription_id'] is None      # транскрипту ще нема


def test_filter_by_direction_catches_untranscribed_recording(client):
    cl, db = client
    cid = _cat(db, 'Фонд')
    other = _cat(db, 'Інше')
    _add_recording(db, yid='rec_a', title='Запис A', file_path='/x/a.mp3', category_id=cid)
    _add_recording(db, yid='rec_b', title='Запис B', file_path='/x/b.mp3', category_id=other)

    data = _downloads(cl, category_id=cid)
    titles = [d['title'] for d in data['downloads']]
    assert titles == ['Запис A']               # фільтр по напрямку ловить нетранскрибований


def test_transcript_category_wins_over_own(client):
    cl, db = client
    own = _cat(db, 'AI')
    trans = _cat(db, 'Фонд')
    _add_recording(db, yid='rec_a', title='Запис A', file_path='/x/a.mp3', category_id=own)
    _add_transcript(db, file_path='/x/a.mp3', category_id=trans)

    data = _downloads(cl)
    row = next(d for d in data['downloads'] if d['title'] == 'Запис A')
    assert row['category_id'] == trans          # транскрипт-напрямок має пріоритет
    assert row['transcription_id'] is not None

    # і фільтр по транскрипт-напрямку теж ловить
    assert [d['title'] for d in _downloads(cl, category_id=trans)['downloads']] == ['Запис A']
    # а по власному (вже перекритому) — ні
    assert _downloads(cl, category_id=own)['downloads'] == []