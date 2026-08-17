"""T4.6 (REMEDIATION_PLAN Волна 2, Варіант A): soft-delete одиничних видалень
+ undo (restore) для transcriptions і audio_downloads + grace-purge.

Покриваємо:
- DELETE /api/history/<id> ставить deleted_at (не стирає рядок/файл).
- Soft-deleted зникає з /api/history (list) і /api/history/<id> (get).
- POST /api/history/<id>/restore повертає видимість.
- DELETE /api/audio/downloads/<id> ставить deleted_at, файл НЕ стирається.
- Soft-deleted аудіо зникає з /api/audio/downloads (list + counts).
- POST /api/audio/downloads/<id>/restore повертає видимість.
- retrieval.search (vector+FTS RRF) не бачить чанки soft-deleted транскрипту.
- app.services.retention.purge_soft_deleted фізично прибирає прострочене
  (файл + рядок), живе (deleted_at IS NULL) чи ще у grace — не чіпає.
"""
from __future__ import annotations

import os
import time

import pytest
from flask import Flask

from app.blueprints.audio_library import audio_bp
from app.blueprints.transcription import transcription_bp
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import retention


@pytest.fixture
def app_and_db(tmp_path):
    db_path = str(tmp_path / 'soft_delete.db')
    init_database(db_path)
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.register_blueprint(audio_bp)
    app.config['TESTING'] = True
    app.config['DATABASE'] = db_path
    return app, db_path


@pytest.fixture
def client(app_and_db):
    app, db_path = app_and_db
    return app.test_client(), db_path


def _add_transcript(db_path, *, source_type='file', file_path=None, text='hello world'):
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, file_path, "
            "transcript_text, language) VALUES (?, 'T', ?, ?, 'uk')",
            (source_type, file_path, text),
        )
        conn.commit()
        return cur.lastrowid


def _add_audio(db_path, *, file_path, youtube_id='yt1'):
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path) "
            "VALUES ('u', ?, 'A', ?)",
            (youtube_id, file_path),
        )
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------- transcriptions

class TestTranscriptionSoftDelete:
    def test_delete_sets_deleted_at_not_physical(self, client):
        c, db_path = client
        tid = _add_transcript(db_path)
        r = c.delete(f'/api/history/{tid}')
        assert r.status_code == 200
        assert r.get_json()['success'] is True

        with get_db_connection(db_path) as conn:
            row = conn.execute(
                'SELECT deleted_at FROM transcriptions WHERE id = ?', (tid,)
            ).fetchone()
        assert row is not None, "рядок має лишитись у БД (soft-delete)"
        assert row['deleted_at'] is not None

    def test_deleted_file_preserved_on_disk(self, client, tmp_path):
        c, db_path = client
        f = tmp_path / 'audio.mp3'
        f.write_bytes(b'fake-audio')
        tid = _add_transcript(db_path, source_type='file', file_path=str(f))
        c.delete(f'/api/history/{tid}')
        assert f.exists(), "файл НЕ має стиратись одразу — тільки при purge"

    def test_deleted_disappears_from_list(self, client):
        c, db_path = client
        tid = _add_transcript(db_path)
        c.delete(f'/api/history/{tid}')
        r = c.get('/api/history')
        ids = [t['id'] for t in r.get_json()['transcriptions']]
        assert tid not in ids
        assert r.get_json()['total'] == 0

    def test_deleted_disappears_from_get_one(self, client):
        c, db_path = client
        tid = _add_transcript(db_path)
        c.delete(f'/api/history/{tid}')
        r = c.get(f'/api/history/{tid}')
        assert r.status_code == 404

    def test_deleted_disappears_from_search(self, client):
        c, db_path = client
        tid = _add_transcript(db_path, text='унікальнийтермін123')
        r = c.get('/api/history?search=унікальнийтермін123')
        assert r.get_json()['total'] == 1
        c.delete(f'/api/history/{tid}')
        r = c.get('/api/history?search=унікальнийтермін123')
        assert r.get_json()['total'] == 0

    def test_double_delete_returns_404(self, client):
        c, db_path = client
        tid = _add_transcript(db_path)
        assert c.delete(f'/api/history/{tid}').status_code == 200
        assert c.delete(f'/api/history/{tid}').status_code == 404

    def test_restore_brings_back_to_list(self, client):
        c, db_path = client
        tid = _add_transcript(db_path)
        c.delete(f'/api/history/{tid}')
        r = c.post(f'/api/history/{tid}/restore')
        assert r.status_code == 200
        assert r.get_json()['success'] is True

        with get_db_connection(db_path) as conn:
            row = conn.execute(
                'SELECT deleted_at FROM transcriptions WHERE id = ?', (tid,)
            ).fetchone()
        assert row['deleted_at'] is None

        r = c.get('/api/history')
        ids = [t['id'] for t in r.get_json()['transcriptions']]
        assert tid in ids
        assert c.get(f'/api/history/{tid}').status_code == 200

    def test_restore_without_delete_404(self, client):
        c, db_path = client
        tid = _add_transcript(db_path)
        r = c.post(f'/api/history/{tid}/restore')
        assert r.status_code == 404

    def test_restore_unknown_id_404(self, client):
        c, _ = client
        assert c.post('/api/history/999999/restore').status_code == 404


# ---------------------------------------------------------------- audio_downloads

class TestAudioSoftDelete:
    def test_delete_sets_deleted_at_not_physical(self, client, tmp_path):
        c, db_path = client
        f = tmp_path / 'a.mp3'
        f.write_bytes(b'x')
        aid = _add_audio(db_path, file_path=str(f))
        r = c.delete(f'/api/audio/downloads/{aid}')
        assert r.status_code == 200
        assert r.get_json()['success'] is True
        assert f.exists(), "аудіо-файл не має стиратись одразу"

        with get_db_connection(db_path) as conn:
            row = conn.execute(
                'SELECT deleted_at FROM audio_downloads WHERE id = ?', (aid,)
            ).fetchone()
        assert row is not None
        assert row['deleted_at'] is not None

    def test_deleted_disappears_from_list_and_counts(self, client, tmp_path):
        c, db_path = client
        f = tmp_path / 'a.mp3'
        f.write_bytes(b'x')
        aid = _add_audio(db_path, file_path=str(f))
        r = c.get('/api/audio/downloads')
        assert r.get_json()['total'] == 1
        assert r.get_json()['counts']['all'] == 1

        c.delete(f'/api/audio/downloads/{aid}')
        r = c.get('/api/audio/downloads')
        assert r.get_json()['total'] == 0
        assert r.get_json()['counts']['all'] == 0
        assert all(d['id'] != aid for d in r.get_json()['downloads'])

    def test_double_delete_404(self, client, tmp_path):
        c, db_path = client
        f = tmp_path / 'a.mp3'
        f.write_bytes(b'x')
        aid = _add_audio(db_path, file_path=str(f))
        assert c.delete(f'/api/audio/downloads/{aid}').status_code == 200
        assert c.delete(f'/api/audio/downloads/{aid}').status_code == 404

    def test_restore_brings_back(self, client, tmp_path):
        c, db_path = client
        f = tmp_path / 'a.mp3'
        f.write_bytes(b'x')
        aid = _add_audio(db_path, file_path=str(f))
        c.delete(f'/api/audio/downloads/{aid}')
        r = c.post(f'/api/audio/downloads/{aid}/restore')
        assert r.status_code == 200
        assert r.get_json()['success'] is True

        r = c.get('/api/audio/downloads')
        assert r.get_json()['total'] == 1

    def test_check_duplicate_ignores_soft_deleted(self, client, tmp_path):
        c, db_path = client
        f = tmp_path / 'a.mp3'
        f.write_bytes(b'x')
        yid = 'dQw4w9WgXcQ'  # 11-char valid-shaped id (extract_youtube_id вимагає це)
        aid = _add_audio(db_path, file_path=str(f), youtube_id=yid)
        url = f'https://youtube.com/watch?v={yid}'
        r = c.post('/api/audio/check-duplicate', json={'url': url})
        assert r.get_json()['is_duplicate'] is True

        c.delete(f'/api/audio/downloads/{aid}')
        r = c.post('/api/audio/check-duplicate', json={'url': url})
        assert r.get_json()['is_duplicate'] is False


# ---------------------------------------------------------------- retrieval (RAG)

def test_retrieval_excludes_soft_deleted_chunks(tmp_path):
    """Vector+FTS RRF-пошук не має повертати чанки soft-deleted транскрипту."""
    from app.services import retrieval

    db_path = str(tmp_path / 'retrieval.db')
    init_database(db_path)
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, language) "
            "VALUES ('file', 'T', 'дуже унікальний текст про жирафів', 'uk')"
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO chunks (transcription_id, chunk_index, text) VALUES (?, 0, ?)",
            (tid, 'дуже унікальний текст про жирафів'),
        )
        conn.commit()

    res = retrieval.search(db_path, 'жирафів', top_k=5)
    assert any(ch['transcription_id'] == tid for ch in res['chunks'])

    with get_db_connection(db_path) as conn:
        conn.execute(
            'UPDATE transcriptions SET deleted_at = ? WHERE id = ?', (time.time(), tid)
        )
        conn.commit()

    res = retrieval.search(db_path, 'жирафів', top_k=5)
    assert all(ch['transcription_id'] != tid for ch in res['chunks'])


# ---------------------------------------------------------------- purge (grace)

class TestPurgeSoftDeleted:
    def test_purge_removes_expired_transcription_and_file(self, tmp_path):
        db_path = str(tmp_path / 'purge.db')
        init_database(db_path)
        f = tmp_path / 'old.mp3'
        f.write_bytes(b'x')
        with get_db_connection(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, file_path, "
                "transcript_text, language, deleted_at) VALUES ('file', 'T', ?, 't', 'uk', ?)",
                (str(f), time.time() - 999999),  # давно протухле
            )
            tid = cur.lastrowid
            conn.commit()

        stats = retention.purge_soft_deleted(db_path, grace_days=7)
        assert stats['transcriptions_purged'] == 1
        assert not f.exists(), "прострочений soft-deleted файл має бути прибраний purge'ом"

        with get_db_connection(db_path) as conn:
            row = conn.execute('SELECT id FROM transcriptions WHERE id = ?', (tid,)).fetchone()
        assert row is None

    def test_purge_keeps_fresh_soft_deleted(self, tmp_path):
        """Soft-deleted, але ще в межах grace — purge НЕ чіпає."""
        db_path = str(tmp_path / 'purge2.db')
        init_database(db_path)
        f = tmp_path / 'fresh.mp3'
        f.write_bytes(b'x')
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, file_path, "
                "transcript_text, language, deleted_at) VALUES ('file', 'T', ?, 't', 'uk', ?)",
                (str(f), time.time()),  # щойно видалено
            )
            conn.commit()

        stats = retention.purge_soft_deleted(db_path, grace_days=7)
        assert stats['transcriptions_purged'] == 0
        assert f.exists()

    def test_purge_keeps_live_rows(self, tmp_path):
        db_path = str(tmp_path / 'purge3.db')
        init_database(db_path)
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, transcript_text, language) "
                "VALUES ('file', 'T', 't', 'uk')"
            )
            conn.commit()

        stats = retention.purge_soft_deleted(db_path, grace_days=7)
        assert stats['transcriptions_purged'] == 0
        with get_db_connection(db_path) as conn:
            n = conn.execute('SELECT COUNT(*) AS n FROM transcriptions').fetchone()['n']
        assert n == 1

    def test_purge_removes_expired_audio_and_file(self, tmp_path):
        db_path = str(tmp_path / 'purge4.db')
        init_database(db_path)
        f = tmp_path / 'old_audio.mp3'
        f.write_bytes(b'x')
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, deleted_at) "
                "VALUES ('u', 'yt1', 'A', ?, ?)",
                (str(f), time.time() - 999999),
            )
            conn.commit()

        stats = retention.purge_soft_deleted(db_path, grace_days=7)
        assert stats['audio_purged'] == 1
        assert not f.exists()

    def test_purge_removes_expired_speaker_merge_snapshot(self, tmp_path):
        db_path = str(tmp_path / 'purge5.db')
        init_database(db_path)
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO speaker_merges (keep_id, merged_speaker_ids, before_json, merged_at) "
                "VALUES (1, '[2]', '{}', ?)",
                (time.time() - 999999,),
            )
            conn.commit()

        stats = retention.purge_soft_deleted(db_path, grace_days=7)
        assert stats['merges_purged'] == 1
