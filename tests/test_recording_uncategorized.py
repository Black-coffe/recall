"""GET /api/recordings/uncategorized (spec recording-autosave, Ask 4).

The autosave files a recording only when the k-NN is confident; the rest must reach the user.
Only recent, live recording transcripts without a direction are listed, with up to three
candidates; Telegram/documents and filed or deleted recordings are not.
"""
from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from app.blueprints.recording import recording_bp
from app.services import categorize


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / 'archive.db')
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE transcriptions (id INTEGER PRIMARY KEY, source_type TEXT, title TEXT,
            source_name TEXT, category_id INTEGER, deleted_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    rows = [
        (8933, 'recording', None, 'Запис 2026-10-19 13:28', None, None),        # listed
        (8934, 'recording', 'Дзвінок з Женею', 'Запис 2026-10-19 15:00', None, None),  # listed
        (8935, 'recording', 'Планерка фонду', 'x', 1, None),                    # filed
        (8936, 'recording', 'Видалений', 'x', None, '2026-10-19'),              # deleted
        (8937, 'telegram', 'Повідомлення', 'x', None, None),                    # not a recording
    ]
    conn.executemany('INSERT INTO transcriptions (id, source_type, title, source_name, category_id, '
                     'deleted_at) VALUES (?, ?, ?, ?, ?, ?)', rows)
    conn.execute("INSERT INTO transcriptions (id, source_type, title, category_id, created_at) "
                 "VALUES (100, 'recording', 'Старий тест', NULL, '2026-05-05 07:48:21')")   # too old
    conn.commit()
    conn.close()

    def fake_suggest(_db, tid):
        return {'suggestion': None, 'candidates': [
            {'category_id': 27, 'name': 'БАУФ - Женя AI', 'score': 0.5},
            {'category_id': 3, 'name': 'AI / професія', 'score': 0.3},
            {'category_id': 1, 'name': 'Фонд Робота', 'score': 0.1},
            {'category_id': 2, 'name': 'Особисте', 'score': 0.1},
        ]}
    monkeypatch.setattr(categorize, 'suggest_category', fake_suggest)

    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config.update(TESTING=True, DATABASE=db)
    return app.test_client()


def test_lists_only_recent_unfiled_live_recordings_newest_first(client):
    items = client.get('/api/recordings/uncategorized').get_json()['items']
    assert [i['transcription_id'] for i in items] == [8934, 8933]
    assert items[0]['title'] == 'Дзвінок з Женею'
    assert items[1]['title'] == 'Запис 2026-10-19 13:28'     # falls back to the source name


def test_offers_at_most_three_candidates(client):
    items = client.get('/api/recordings/uncategorized').get_json()['items']
    assert [c['category_id'] for c in items[0]['candidates']] == [27, 3, 1]


def test_a_failing_suggester_still_lists_the_recording(client, monkeypatch):
    def boom(_db, _tid):
        raise RuntimeError('embeddings down')
    monkeypatch.setattr(categorize, 'suggest_category', boom)
    items = client.get('/api/recordings/uncategorized').get_json()['items']
    assert len(items) == 2 and items[0]['candidates'] == []
