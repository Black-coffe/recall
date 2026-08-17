"""Тести авто-підказки напрямку (Phase 15C, k-NN suggester).

Embeddings мокаються (EMBED_DIM=4), реальна модель не вантажиться.
"""
import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import categorize, embeddings


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return a / n if n else a


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "c.db")
    init_database(path)
    return path


@pytest.fixture(autouse=True)
def mock_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)


def _add_tx(path, category_id=None):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, category_id) "
        "VALUES ('file', 'x', 'x', ?)", (category_id,),
    )
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


def _add_chunk(path, tid, idx, vec):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, text, embedding) VALUES (?, ?, ?, ?)",
        (tid, idx, f"c{idx}", _unit(vec).tobytes()),
    )
    conn.commit(); conn.close()


def test_suggests_nearest_category(db):
    a = _add_tx(db, category_id=1)   # «Фонд»
    b = _add_tx(db, category_id=2)   # «Особисте»
    _add_chunk(db, a, 0, [1, 0, 0, 0])
    _add_chunk(db, b, 0, [0, 1, 0, 0])
    target = _add_tx(db, category_id=None)
    _add_chunk(db, target, 0, [0.96, 0.1, 0, 0])  # близько до A

    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is not None
    assert res["suggestion"]["category_id"] == 1
    assert res["suggestion"]["confidence"] > 0.5
    assert res["neighbors"] == 2


def test_insufficient_labels_cold_start(db):
    a = _add_tx(db, category_id=1)
    _add_chunk(db, a, 0, [1, 0, 0, 0])
    target = _add_tx(db, category_id=None)
    _add_chunk(db, target, 0, [1, 0, 0, 0])

    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is None
    assert res["reason"] == "insufficient_labels"
    assert res["labeled_categories"] == 1


def test_not_embedded(db):
    _add_tx(db, category_id=1)  # хоч якась розмітка
    target = _add_tx(db, category_id=None)  # без чанків
    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is None
    assert res["reason"] == "not_embedded"


def test_no_labeled_data(db):
    target = _add_tx(db, category_id=None)
    _add_chunk(db, target, 0, [1, 0, 0, 0])
    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is None
    assert res["reason"] == "no_labeled_data"


def test_embeddings_unavailable(db, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    target = _add_tx(db, category_id=None)
    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is None
    assert res["reason"] == "embeddings_unavailable"
