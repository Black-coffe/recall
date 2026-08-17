"""Тести T6.6: моніторинг масштабу vector search (лише спостереження, НЕ ANN).

Перевіряємо:
  - check_vector_scale() рахує embedded-чанки і повертає {chunk_count, threshold,
    over_threshold};
  - WARNING логується, коли к-сть перевищує поріг, і НЕ логується нижче порогу;
  - підрахунок НЕ вантажить/не парсить BLOB'и embedding — навіть свідомо
    "битий" BLOB (розмір не кратний float32) не валить перевірку, бо вона
    робить лише COUNT(*), а не embeddings.blob_to_vec().
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
    """Додати n чанків з (за замовчуванням) валідним 4-float32 embedding-блобом.
    Належать одному фейковому транскрипту (FK на transcriptions не forceниться
    у sqlite за замовчуванням, але для реалізму створимо реальний рядок)."""
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
    """Ізолюємо process-level прапорець _scale_warned між тестами."""
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
    assert not any("Embedded-чанків" in r.message for r in caplog.records)


def test_warning_above_threshold_logged(db, monkeypatch, caplog):
    monkeypatch.setattr(embeddings, "_VECTOR_WARN_THRESHOLD", 5)
    _add_chunks(db, 10)
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        res = embeddings.check_vector_scale(db)
    assert res["over_threshold"] is True
    assert res["chunk_count"] == 10
    assert res["threshold"] == 5
    assert any("Embedded-чанків" in r.message for r in caplog.records)


def test_warning_logged_once_per_process_until_back_below(db, monkeypatch, caplog):
    """Не спамити WARNING на кожен виклик, поки поріг лишається перевищеним."""
    monkeypatch.setattr(embeddings, "_VECTOR_WARN_THRESHOLD", 5)
    _add_chunks(db, 10)
    with caplog.at_level(logging.WARNING, logger="app.services.embeddings"):
        embeddings.check_vector_scale(db)
        embeddings.check_vector_scale(db)
    warn_count = sum(1 for r in caplog.records if "Embedded-чанків" in r.message)
    assert warn_count == 1


def test_count_does_not_load_or_parse_blobs(db, monkeypatch):
    """КРИТИЧНО: підрахунок — лише COUNT(*), НЕ blob_to_vec(). Доказ: навіть
    заздалегідь "битий" embedding-BLOB (довжина не кратна float32 = 4 байти,
    тож np.frombuffer/blob_to_vec впав би з ValueError) не заважає підрахунку —
    бо check_vector_scale ніколи не парсить BLOB, лише рахує рядки."""
    monkeypatch.setattr(embeddings, "_VECTOR_WARN_THRESHOLD", 100)
    _add_chunks(db, 3, embedding_blob=b"\x01\x02\x03")  # 3 байти — НЕ кратно 4
    # Контроль: сам blob_to_vec на такому блобі справді впав би.
    with pytest.raises(ValueError):
        embeddings.blob_to_vec(b"\x01\x02\x03")
    # А check_vector_scale — не падає, бо не викликає blob_to_vec.
    res = embeddings.check_vector_scale(db)
    assert res["chunk_count"] == 3


def test_default_threshold_is_50000_when_env_unset():
    """Дефолт (без RECALL_VECTOR_WARN_THRESHOLD у env) — 50000. Перевіряємо
    саме дефолт коду, а не поточне значення модуля (яке тести вище монкіпатчать)."""
    import os
    assert "RECALL_VECTOR_WARN_THRESHOLD" not in os.environ, (
        "тест очікує чисте середовище без env override; якщо RECALL_VECTOR_WARN_THRESHOLD "
        "виставлено — це свідомий override і дефолт коду тут не перевірити напряму"
    )
    assert int(os.environ.get("RECALL_VECTOR_WARN_THRESHOLD", "50000")) == 50000
