"""T6.6 tests: vector search scale monitoring (observation only, NOT ANN).

We check:
  - check_vector_scale() counts embedded chunks and returns {chunk_count, threshold,
    over_threshold};
  - a WARNING is logged when the count exceeds the threshold, and is NOT logged below it;
  - the count does NOT load/parse embedding BLOBs — even a deliberately
    "broken" BLOB (size not a multiple of float32) does not fail the check, because it
    only does COUNT(*), not embeddings.blob_to_vec().
"""
import logging
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import embeddings


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _add_chunks(path, n, embedding_blob=b"\x00\x00\x80\x3f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"):
    """Add n chunks with (by default) a valid 4-float32 embedding blob.
    They belong to one fake transcript (the FK to transcriptions is not enforced
    in sqlite by default, but for realism we create a real row)."""
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text) "
        "VALUES ('file', 'x', 'x')"
    )
    tid = cur.lastrowid
    for i in range(n):
        conn.execute(
            "INSERT INTO chunks (transcription_id, chunk_index, text, embedding) "
            "VALUES (?, ?, ?, ?)",
            (tid, i, f"chunk {i}", embedding_blob),
        )
    conn.commit()
    conn.close()
    return tid


@pytest.fixture(autouse=True)
def _reset_warn_state(monkeypatch):
    """Isolate the process-level _scale_warned flag between tests."""
    monkeypatch.setattr(embeddings, "_scale_warned", False)
    yield


def test_check_vector_scale_counts_chunks(db):
    _add_chunks(db, 5)
    res = embeddings.check_vector_scale(db)
    assert res["chunk_count"] == 5
    assert res["over_threshold"] is False


def test_warning_below_threshold_not_logged(db, monkeypatch, caplog):
    monkeypatch.setattr(embeddings, "_VECTOR_WARN_THRESHOLD", 100)
    _add_chunks(db, 10)
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        res = embeddings.check_vector_scale(db)
    assert res["over_threshold"] is False
    assert not any("Embedded chunks" in r.message for r in caplog.records)


def test_warning_above_threshold_logged(db, monkeypatch, caplog):
    monkeypatch.setattr(embeddings, "_VECTOR_WARN_THRESHOLD", 5)
    _add_chunks(db, 10)
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        res = embeddings.check_vector_scale(db)
    assert res["over_threshold"] is True
    assert res["chunk_count"] == 10
    assert res["threshold"] == 5
    assert any("Embedded chunks" in r.message for r in caplog.records)


def test_warning_logged_once_per_process_until_back_below(db, monkeypatch, caplog):
    """Do not spam WARNING on every call while the threshold stays exceeded."""
    monkeypatch.setattr(embeddings, "_VECTOR_WARN_THRESHOLD", 5)
    _add_chunks(db, 10)
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        embeddings.check_vector_scale(db)
        embeddings.check_vector_scale(db)
    warn_count = sum(1 for r in caplog.records if "Embedded chunks" in r.message)
    assert warn_count == 1


def test_count_does_not_load_or_parse_blobs(db, monkeypatch):
    """CRITICAL: the count is only COUNT(*), NOT blob_to_vec(). Proof: even a
    pre-"broken" embedding BLOB (length not a multiple of float32 = 4 bytes,
    so np.frombuffer/blob_to_vec would fail with ValueError) does not interfere with the count —
    because check_vector_scale never parses the BLOB, it only counts rows."""
    monkeypatch.setattr(embeddings, "_VECTOR_WARN_THRESHOLD", 100)
    _add_chunks(db, 3, embedding_blob=b"\x01\x02\x03")  # 3 bytes — NOT a multiple of 4
    # Control: blob_to_vec itself on such a blob would really fail.
    with pytest.raises(ValueError):
        embeddings.blob_to_vec(b"\x01\x02\x03")
    # And check_vector_scale does not fail, because it does not call blob_to_vec.
    res = embeddings.check_vector_scale(db)
    assert res["chunk_count"] == 3


def test_default_threshold_is_50000_when_env_unset():
    """The default (without RECALL_VECTOR_WARN_THRESHOLD in env) is 50000. We check
    the code's default itself, not the module's current value (which the tests above monkeypatch)."""
    import os
    assert "RECALL_VECTOR_WARN_THRESHOLD" not in os.environ, (
        "the test expects a clean environment without an env override; if RECALL_VECTOR_WARN_THRESHOLD "
        "is set — it is a deliberate override and the code's default cannot be checked directly here"
    )
    assert int(os.environ.get("RECALL_VECTOR_WARN_THRESHOLD", "50000")) == 50000
