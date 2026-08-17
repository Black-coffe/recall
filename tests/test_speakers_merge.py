"""Phase 12.8: тести merge_speakers endpoint.

Покриваємо:
- Merge без map links (просто видаляємо дублі).
- Merge з re-link transcription_speaker_map.
- Захист is_self.
- Aggregation usage_count.
- Validation помилки (keep_id у merge_ids, merge_ids порожній).
- Embedding averaging при merge.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest
from flask import Flask

from app.blueprints.speakers import speakers_bp
from app.db.migrations import init_database


def _make_emb_blob(values: list[float]) -> bytes:
    # Формат той же що embedding_to_blob: 'F32V' magic + float32 array.
    return b'F32V' + struct.pack(f'<{len(values)}f', *values)


@pytest.fixture
def client(tmp_path: Path):
    db_path = str(tmp_path / 'merge_test.db')
    init_database(db_path)
    # Seed test data
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # 3 speakers (Ви seeded). Returnamemo IDs у dict — порядок INSERT'у залежить
    # від AUTOINCREMENT, тому беремо lastrowid.
    c.execute("INSERT INTO speakers (name, usage_count, embedding) VALUES (?, ?, ?)",
              ('Андрій', 5, _make_emb_blob([1.0] * 256)))
    andriy_id = c.lastrowid
    c.execute("INSERT INTO speakers (name, usage_count) VALUES (?, ?)",
              ('Андрій (дубль)', 2))
    dup_id = c.lastrowid
    c.execute("INSERT INTO speakers (name, usage_count, embedding) VALUES (?, ?, ?)",
              ('Юлія', 3, _make_emb_blob([0.5] * 256)))
    yulia_id = c.lastrowid
    # Test transcript + map links
    c.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, language) "
              "VALUES ('file', 'tx1', 'test', 'uk')")
    tx1 = c.lastrowid
    c.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) "
              "VALUES (?, ?, ?)", (tx1, 'SPEAKER_00', andriy_id))
    c.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) "
              "VALUES (?, ?, ?)", (tx1, 'SPEAKER_01', dup_id))
    conn.commit()
    conn.close()

    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(speakers_bp)
    return app.test_client(), db_path, {'andriy': andriy_id, 'dup': dup_id, 'yulia': yulia_id}


class TestMergeSpeakers:
    def test_basic_merge_relinks_map(self, client):
        c, db_path, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['dup']]})
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        assert data['merged_count'] == 1
        assert data['relinked_map_rows'] == 1

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT raw_label, speaker_id FROM transcription_speaker_map ORDER BY raw_label"
        ).fetchall()
        assert rows == [('SPEAKER_00', ids['andriy']), ('SPEAKER_01', ids['andriy'])]
        andriy_dup = conn.execute("SELECT id FROM speakers WHERE name = 'Андрій (дубль)'").fetchone()
        assert andriy_dup is None
        andriy = conn.execute("SELECT usage_count FROM speakers WHERE id = ?",
                              (ids['andriy'],)).fetchone()
        assert andriy[0] == 7  # 5 + 2
        conn.close()

    def test_merge_self_protected(self, client):
        c, _, ids = client
        # 'Ви' є seeded з id=1
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [1]})
        assert r.status_code == 400
        err = r.get_json()['error']
        assert 'is_self' in err or 'Ви' in err

    def test_keep_id_in_merge_ids_rejected(self, client):
        c, _, ids = client
        r = c.post('/api/speakers/merge',
                   json={'keep_id': ids['andriy'], 'merge_ids': [ids['andriy'], ids['dup']]})
        assert r.status_code == 400

    def test_empty_merge_ids_rejected(self, client):
        c, _, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': []})
        assert r.status_code == 400

    def test_unknown_keep_id_404(self, client):
        c, _, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': 99999, 'merge_ids': [ids['dup']]})
        assert r.status_code == 404

    def test_unknown_merge_id_404(self, client):
        c, _, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [99999]})
        assert r.status_code == 404

    def test_invalid_keep_id_type(self, client):
        c, _, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': 'abc', 'merge_ids': [ids['dup']]})
        assert r.status_code == 400

    def test_merge_aggregates_usage_count(self, client):
        c, db_path, ids = client
        # Merge Юлія (usage=3) → Андрій (usage=5)
        c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['yulia']]})
        conn = sqlite3.connect(db_path)
        usage = conn.execute("SELECT usage_count FROM speakers WHERE id = ?",
                             (ids['andriy'],)).fetchone()[0]
        assert usage == 8  # 5 + 3
        conn.close()

    def test_merge_returns_merge_id(self, client):
        c, _, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['dup']]})
        data = r.get_json()
        assert isinstance(data.get('merge_id'), int)

    def test_embedding_averaging(self, client):
        c, db_path, ids = client
        # Merge Юлія (embedding=[0.5*256], weight=3) → Андрій (embedding=[1.0*256], weight=5).
        # average_embeddings робить L2-normalize, тому всі компоненти однакові
        # → після normalize кожен = 1/sqrt(256) = 0.0625.
        c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['yulia']]})
        conn = sqlite3.connect(db_path)
        blob = conn.execute("SELECT embedding FROM speakers WHERE id = ?",
                            (ids['andriy'],)).fetchone()[0]
        conn.close()
        assert blob[:4] == b'F32V'
        floats = struct.unpack(f'<{(len(blob)-4)//4}f', blob[4:])
        # Усі компоненти рівні (бо raw avg був uniform), після L2 normalize → 1/sqrt(256)=0.0625
        expected = 1.0 / (256 ** 0.5)
        assert abs(floats[0] - expected) < 0.001
        assert abs(floats[100] - expected) < 0.001
        # Перевіримо що loss-less encode/decode: vector unit length
        norm_sq = sum(x * x for x in floats)
        assert abs(norm_sq - 1.0) < 0.001


class TestUnmergeSpeakers:
    """T4.6 (REMEDIATION_PLAN Волна 2): undo для merge_speakers зі снапшоту."""

    def test_unmerge_restores_deleted_speaker(self, client):
        c, db_path, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['dup']]})
        merge_id = r.get_json()['merge_id']

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT id FROM speakers WHERE id = ?", (ids['dup'],)).fetchone() is None
        conn.close()

        r = c.post(f'/api/speakers/unmerge/{merge_id}')
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        assert ids['dup'] in data['restored_speaker_ids']

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT name, usage_count FROM speakers WHERE id = ?",
                           (ids['dup'],)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 'Андрій (дубль)'
        assert row[1] == 2

    def test_unmerge_restores_map_relink_full(self, client):
        c, db_path, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['dup']]})
        merge_id = r.get_json()['merge_id']

        c.post(f'/api/speakers/unmerge/{merge_id}')
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT raw_label, speaker_id FROM transcription_speaker_map ORDER BY raw_label"
        ).fetchall()
        conn.close()
        assert rows == [('SPEAKER_00', ids['andriy']), ('SPEAKER_01', ids['dup'])]

    def test_unmerge_restores_keep_usage_count(self, client):
        c, db_path, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['dup']]})
        merge_id = r.get_json()['merge_id']
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT usage_count FROM speakers WHERE id = ?",
                            (ids['andriy'],)).fetchone()[0] == 7
        conn.close()

        c.post(f'/api/speakers/unmerge/{merge_id}')
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT usage_count FROM speakers WHERE id = ?",
                            (ids['andriy'],)).fetchone()[0] == 5
        conn.close()

    def test_double_unmerge_returns_409(self, client):
        c, _, ids = client
        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['dup']]})
        merge_id = r.get_json()['merge_id']
        assert c.post(f'/api/speakers/unmerge/{merge_id}').status_code == 200
        r2 = c.post(f'/api/speakers/unmerge/{merge_id}')
        assert r2.status_code == 409

    def test_unmerge_unknown_id_404(self, client):
        c, _, _ids = client
        assert c.post('/api/speakers/unmerge/999999').status_code == 404

    def test_unmerge_restores_entities_speaker_link(self, client):
        c, db_path, ids = client
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO entities (type, canonical_name, normalized_name, speaker_id) "
            "VALUES ('person', 'Дубль', 'дубль', ?)", (ids['dup'],)
        )
        eid = conn.execute("SELECT id FROM entities WHERE canonical_name = 'Дубль'").fetchone()[0]
        conn.commit()
        conn.close()

        r = c.post('/api/speakers/merge', json={'keep_id': ids['andriy'], 'merge_ids': [ids['dup']]})
        merge_id = r.get_json()['merge_id']

        conn = sqlite3.connect(db_path)
        # FK ON DELETE SET NULL мав обнулити speaker_id при видаленні speakers-рядка
        assert conn.execute("SELECT speaker_id FROM entities WHERE id = ?", (eid,)).fetchone()[0] is None
        conn.close()

        c.post(f'/api/speakers/unmerge/{merge_id}')
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT speaker_id FROM entities WHERE id = ?", (eid,)).fetchone()[0] == ids['dup']
        conn.close()


class TestSpeakerSamplesPreview:
    """T4.6: GET /api/speakers/<id>/samples — легкий merge-preview."""

    def test_samples_returns_segment_text(self, client):
        c, db_path, ids = client
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE transcriptions SET segments = ? WHERE source_name = 'tx1'",
            ('[{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0, "text": "Привіт усім"}]',),
        )
        conn.commit()
        conn.close()

        r = c.get(f'/api/speakers/{ids["andriy"]}/samples')
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        assert data['speaker_id'] == ids['andriy']
        assert any(s['text'] == 'Привіт усім' for s in data['samples'])

    def test_samples_unknown_speaker_404(self, client):
        c, _, _ids = client
        assert c.get('/api/speakers/999999/samples').status_code == 404

    def test_samples_respects_limit(self, client):
        c, db_path, ids = client
        segs = [
            {"speaker": "SPEAKER_00", "start": float(i), "end": float(i + 1), "text": f"фраза {i}"}
            for i in range(10)
        ]
        import json as _json
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE transcriptions SET segments = ? WHERE source_name = 'tx1'",
            (_json.dumps(segs),),
        )
        conn.commit()
        conn.close()

        r = c.get(f'/api/speakers/{ids["andriy"]}/samples?limit=3')
        assert len(r.get_json()['samples']) == 3
