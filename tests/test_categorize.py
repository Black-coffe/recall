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


#: Поточна епоха, якою мокаємо `embeddings.EMBED_MODEL`/`EMBED_VERSION` — усі
#: фікстури нижче пишуть цю саму пару в `transcriptions`, якщо явно не просять
#: іншу (гард епохи, історія 05).
_EPOCH = ("test-model", 3)


@pytest.fixture(autouse=True)
def mock_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "EMBED_MODEL", _EPOCH[0])
    monkeypatch.setattr(embeddings, "EMBED_VERSION", _EPOCH[1])


def _add_tx(path, category_id=None, epoch=_EPOCH):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, category_id, "
        "embedding_model, embedding_version) VALUES ('file', 'x', 'x', ?, ?, ?)",
        (category_id, epoch[0] if epoch else None, epoch[1] if epoch else None),
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


# ============================================================
# Гард епохи (ADR-017, історія 05): `chunks` не має свого
# embedding_model/embedding_version — епоха живе на `transcriptions`.
# ============================================================

def test_epoch_homogeneous_suggests_as_before(db):
    """(а) увесь корпус в одній (поточній) епосі — поведінка не змінюється."""
    a = _add_tx(db, category_id=1)   # «Фонд Робота»
    b = _add_tx(db, category_id=2)   # «Акмекорп»
    _add_chunk(db, a, 0, [1, 0, 0, 0])
    _add_chunk(db, b, 0, [0, 1, 0, 0])
    target = _add_tx(db, category_id=None)
    _add_chunk(db, target, 0, [0.96, 0.1, 0, 0])

    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is not None
    assert res["suggestion"]["category_id"] == 1
    assert res["neighbors"] == 2


def test_epoch_mixed_when_target_is_stale(db):
    """(б) частина корпусу — епоха 2 (стара), частина — епоха 3 (поточна);
    сам запис лишився невереембеджений (епоха 2). Гард не дає порахувати
    косинус між вектором епохи 2 і корпусом епохи 3 — жодної підказки."""
    old_epoch = ("legacy-model", 2)
    a = _add_tx(db, category_id=1, epoch=old_epoch)     # «Фонд Робота», епоха 2
    b = _add_tx(db, category_id=2)                       # «Акмекорп», поточна епоха 3
    _add_chunk(db, a, 0, [1, 0, 0, 0])
    _add_chunk(db, b, 0, [0, 1, 0, 0])
    target = _add_tx(db, category_id=None, epoch=old_epoch)   # ще не переембеджений
    _add_chunk(db, target, 0, [0.96, 0.1, 0, 0])

    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is None
    assert res["reason"] == "epoch_mixed"


def test_epoch_mixed_not_confused_with_not_embedded(db):
    """Запис без жодного чанку лишається `not_embedded`, а не `epoch_mixed` —
    коди означають різне (нема вектора / вектор не тієї епохи)."""
    _add_tx(db, category_id=1)
    target = _add_tx(db, category_id=None, epoch=("legacy-model", 2))  # чанків немає
    res = categorize.suggest_category(db, target)
    assert res["suggestion"] is None
    assert res["reason"] == "not_embedded"
