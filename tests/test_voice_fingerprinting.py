"""Tests для voice fingerprinting (Phase 10.6).

Покриття:
- Pure-Python helpers: cosine_similarity, embedding_to_blob/blob_to_embedding,
  average_embeddings, find_matching_speaker
- Міграція v7 (embedding column у map)
- Не вантажимо pyannote — embedding це просто list[float] для тестів.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.diarization_service import (
    DEFAULT_MATCH_THRESHOLD,
    EMBEDDING_DTYPE_TAG,
    average_embeddings,
    blob_to_embedding,
    cosine_similarity,
    embedding_to_blob,
    find_matching_speaker,
)
from app.db.migrations import init_database


# ---------------------------------------------------------------- cosine

class TestCosineSimilarity:
    def test_identical_vectors_one(self):
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_minus_one(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_scaled_versions_one(self):
        assert cosine_similarity([1.0, 2.0], [2.0, 4.0]) == pytest.approx(1.0)
        assert cosine_similarity([1.0, 2.0], [10.0, 20.0]) == pytest.approx(1.0)

    def test_empty_returns_zero(self):
        assert cosine_similarity([], []) == 0.0
        assert cosine_similarity([1.0], []) == 0.0

    def test_different_dims_returns_zero(self):
        assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


# ---------------------------------------------------------------- serialization

class TestEmbeddingSerialization:
    def test_roundtrip_preserves_floats(self):
        original = [0.5, -0.3, 1.7, 2.0e-3, -100.0]
        blob = embedding_to_blob(original)
        restored = blob_to_embedding(blob)
        assert restored is not None
        assert len(restored) == len(original)
        for a, b in zip(original, restored):
            assert a == pytest.approx(b, rel=1e-5)  # float32 precision

    def test_blob_starts_with_magic(self):
        blob = embedding_to_blob([1.0, 2.0, 3.0])
        assert blob[:4] == EMBEDDING_DTYPE_TAG

    def test_empty_input_returns_empty_bytes(self):
        assert embedding_to_blob([]) == b''
        assert embedding_to_blob(None) == b''

    def test_blob_to_embedding_handles_none(self):
        assert blob_to_embedding(None) is None
        assert blob_to_embedding(b'') is None

    def test_blob_to_embedding_invalid_magic_returns_none(self):
        assert blob_to_embedding(b'XXXX' + b'\x00' * 16) is None

    def test_blob_to_embedding_truncated_returns_none(self):
        assert blob_to_embedding(b'\x00') is None

    def test_blob_size_proportional_to_dim(self):
        b256 = embedding_to_blob([0.0] * 256)
        b512 = embedding_to_blob([0.0] * 512)
        # 4-byte magic + N*4 bytes
        assert len(b256) == 4 + 256 * 4
        assert len(b512) == 4 + 512 * 4

    def test_can_handle_typical_pyannote_dim(self):
        # pyannote/wespeaker-voxceleb-resnet34-LM має 256-dim embeddings
        original = [float(i) / 256 for i in range(256)]
        blob = embedding_to_blob(original)
        restored = blob_to_embedding(blob)
        assert len(restored) == 256


# ---------------------------------------------------------------- average

class TestAverageEmbeddings:
    def test_first_assignment_normalizes_b(self):
        # a=None → результат = L2-normalize(b)
        b = [3.0, 4.0]  # length 5
        result = average_embeddings(None, b)
        # Norm should be 1.0
        norm = sum(x * x for x in result) ** 0.5
        assert norm == pytest.approx(1.0)

    def test_weighted_average(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        # Equal weights → midpoint, normalized
        result = average_embeddings(a, b, weight_a=1.0, weight_b=1.0)
        # midpoint = [0.5, 0.5], normalized → [0.707, 0.707]
        assert result[0] == pytest.approx(result[1])
        assert result[0] == pytest.approx(0.7071, abs=0.01)

    def test_heavy_a_weight_keeps_close_to_a(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        result = average_embeddings(a, b, weight_a=99.0, weight_b=1.0)
        # Result повинен бути ближче до a ніж до b
        assert result[0] > result[1]

    def test_dim_mismatch_raises(self):
        with pytest.raises(ValueError, match='Розмірності'):
            average_embeddings([1.0, 2.0], [1.0])


# ---------------------------------------------------------------- match

class TestFindMatchingSpeaker:
    def _to_blob(self, emb):
        return embedding_to_blob(emb)

    def test_returns_best_match_above_threshold(self):
        new_emb = [1.0, 0.0, 0.0]
        saved = [
            (1, self._to_blob([0.95, 0.31, 0.0])),  # cos ≈ 0.95
            (2, self._to_blob([0.0, 1.0, 0.0])),     # cos = 0
        ]
        result = find_matching_speaker(new_emb, saved, threshold=0.7)
        assert result is not None
        sid, sim = result
        assert sid == 1
        assert sim > 0.9

    def test_returns_none_below_threshold(self):
        new_emb = [1.0, 0.0]
        saved = [(1, self._to_blob([0.0, 1.0]))]  # cos = 0
        assert find_matching_speaker(new_emb, saved, threshold=0.7) is None

    def test_returns_none_for_empty_saved(self):
        assert find_matching_speaker([1.0, 0.0], []) is None

    def test_returns_none_for_empty_embedding(self):
        saved = [(1, self._to_blob([1.0]))]
        assert find_matching_speaker([], saved) is None

    def test_skips_speakers_with_null_embedding(self):
        saved = [
            (1, None),
            (2, self._to_blob([1.0, 0.0])),
        ]
        result = find_matching_speaker([1.0, 0.0], saved, threshold=0.7)
        assert result is not None
        assert result[0] == 2

    def test_picks_highest_similarity_when_multiple_match(self):
        new_emb = [1.0, 0.0]
        saved = [
            (1, self._to_blob([0.8, 0.6])),   # cos ≈ 0.8
            (2, self._to_blob([0.95, 0.31])), # cos ≈ 0.95
            (3, self._to_blob([0.71, 0.71])), # cos ≈ 0.71
        ]
        result = find_matching_speaker(new_emb, saved, threshold=0.7)
        assert result is not None
        assert result[0] == 2

    def test_default_threshold_constant_reasonable(self):
        # Sanity check: default не занадто низький (false-positive) і не високий (no-match)
        assert 0.7 <= DEFAULT_MATCH_THRESHOLD <= 0.85


# ---------------------------------------------------------------- migration v7

class TestMigrationV7:
    def test_embedding_column_added(self, tmp_path: Path):
        db = str(tmp_path / 'test.db')
        init_database(db)

        conn = sqlite3.connect(db)
        try:
            cols = {row[1]: row[2] for row in conn.execute(
                "PRAGMA table_info(transcription_speaker_map)"
            )}
            assert 'embedding' in cols
            assert 'BLOB' in cols['embedding'].upper()

            v = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
            assert v >= 7
        finally:
            conn.close()

    def test_can_insert_embedding(self, tmp_path: Path):
        db = str(tmp_path / 'test.db')
        init_database(db)

        conn = sqlite3.connect(db)
        try:
            # Створюємо transcription
            conn.execute(
                "INSERT INTO transcriptions (source_type, source_name) VALUES ('file', 'a.mp3')"
            )
            tid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

            blob = embedding_to_blob([0.1, 0.2, 0.3])
            conn.execute(
                'INSERT INTO transcription_speaker_map '
                '(transcription_id, raw_label, embedding) VALUES (?, ?, ?)',
                (tid, 'SPEAKER_00', blob),
            )
            conn.commit()

            row = conn.execute(
                'SELECT embedding FROM transcription_speaker_map WHERE transcription_id = ?',
                (tid,),
            ).fetchone()
            restored = blob_to_embedding(row[0])
            assert len(restored) == 3
        finally:
            conn.close()

    def test_migration_idempotent_v7(self, tmp_path: Path):
        db = str(tmp_path / 'test.db')
        init_database(db)
        init_database(db)
        init_database(db)

        conn = sqlite3.connect(db)
        try:
            cols = [row[1] for row in conn.execute(
                "PRAGMA table_info(transcription_speaker_map)"
            )]
            assert cols.count('embedding') == 1
        finally:
            conn.close()


# ---------------------------------------------------------------- integration

class TestVoiceFingerprintingFlow:
    """End-to-end test через PATCH endpoint з реальним Flask app."""

    @pytest.fixture
    def client(self, tmp_path: Path):
        from flask import Flask
        from app.blueprints.speakers import speakers_bp

        db_path = str(tmp_path / 'vf_test.db')
        init_database(db_path)
        app = Flask(__name__)
        app.config['DATABASE'] = db_path
        app.register_blueprint(speakers_bp)
        return app.test_client()

    def _create_transcription_with_embedding(self, client, raw_label='SPEAKER_00', embedding=None):
        """Helper: створює transcription + map row з embedding."""
        from app.db.connection import get_db_connection
        with get_db_connection(client.application.config['DATABASE']) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO transcriptions (source_type, source_name, transcript_text) "
                "VALUES ('file', 'a.mp3', '')"
            )
            tid = c.lastrowid
            blob = embedding_to_blob(embedding) if embedding else None
            c.execute(
                'INSERT INTO transcription_speaker_map '
                '(transcription_id, raw_label, embedding) VALUES (?, ?, ?)',
                (tid, raw_label, blob),
            )
            conn.commit()
            return tid

    def test_patch_saves_embedding_to_speaker(self, client):
        # Симулюємо що diarization зберіг embedding для SPEAKER_00
        emb = [0.5, 0.3, 0.7, 0.2]
        tid = self._create_transcription_with_embedding(client, embedding=emb)

        # User іменує "Андрій"
        r = client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'Андрій'}},
        )
        assert r.status_code == 200

        # Перевіряємо що embedding потрапив у speakers.embedding
        from app.db.connection import get_db_connection
        with get_db_connection(client.application.config['DATABASE']) as conn:
            row = conn.execute(
                "SELECT embedding FROM speakers WHERE name = 'Андрій'"
            ).fetchone()
            assert row['embedding'] is not None
            stored = blob_to_embedding(row['embedding'])
            # На першому assignment це L2-normalized version of emb
            norm = sum(x * x for x in stored) ** 0.5
            assert norm == pytest.approx(1.0)

    def test_patch_running_average_on_repeat(self, client):
        emb_v1 = [1.0, 0.0, 0.0]
        emb_v2 = [0.0, 1.0, 0.0]

        # First call
        tid1 = self._create_transcription_with_embedding(client, embedding=emb_v1)
        client.patch(f'/api/transcriptions/{tid1}/speakers',
                     json={'mapping': {'SPEAKER_00': 'X'}})

        # Second call з іншим embedding для того ж імені
        tid2 = self._create_transcription_with_embedding(client, embedding=emb_v2)
        client.patch(f'/api/transcriptions/{tid2}/speakers',
                     json={'mapping': {'SPEAKER_00': 'X'}})

        from app.db.connection import get_db_connection
        with get_db_connection(client.application.config['DATABASE']) as conn:
            row = conn.execute(
                "SELECT embedding, usage_count FROM speakers WHERE name = 'X'"
            ).fetchone()
            assert row['usage_count'] == 2
            stored = blob_to_embedding(row['embedding'])
            # Після двох усереднених — компоненти ≈ рівні (0.71, 0.71, 0)
            assert stored[0] > 0
            assert stored[1] > 0
            assert stored[2] == pytest.approx(0.0, abs=1e-5)

    def test_patch_without_embedding_doesnt_crash(self, client):
        """Якщо embedding=NULL у map (стара транскрипція до v7) — PATCH не падає."""
        tid = self._create_transcription_with_embedding(client, embedding=None)
        r = client.patch(
            f'/api/transcriptions/{tid}/speakers',
            json={'mapping': {'SPEAKER_00': 'NoEmbName'}},
        )
        assert r.status_code == 200
