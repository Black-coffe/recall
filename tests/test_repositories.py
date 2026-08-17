"""Тести для app/repositories/* (T7.2, перший інкремент).

Той самий підхід, що й test_migrations.py: тимчасова SQLite БД у tmp_path
через init_database(), без flask app-контексту.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.migrations import init_database
from app.repositories import speakers as speakers_repo
from app.repositories import transcriptions as tx_repo


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "test.db")
    init_database(db)
    return db


def _insert_transcription(conn: sqlite3.Connection, **overrides) -> int:
    fields = {
        "source_type": "file",
        "source_name": "meet.mp3",
        "transcript_text": "hello world",
        "segments": '[{"start": 0, "end": 1, "text": "hi"}]',
        "language": "uk",
    }
    fields.update(overrides)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    cur = conn.execute(
        f"INSERT INTO transcriptions ({cols}) VALUES ({placeholders})",
        list(fields.values()),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- transcriptions.get_by_id

def test_get_by_id_returns_none_when_missing(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        assert tx_repo.get_by_id(conn, 999999) is None
    finally:
        conn.close()


def test_get_by_id_select_star_returns_all_columns(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        tid = _insert_transcription(conn)
        row = tx_repo.get_by_id(conn, tid)
        assert row is not None
        assert row["id"] == tid
        assert row["source_name"] == "meet.mp3"
        # SELECT * — усі колонки схеми присутні, у т.ч. ті, що ми явно не питали
        assert "transcript_text" in row.keys()
        assert "enriched_at" in row.keys()
    finally:
        conn.close()


def test_get_by_id_narrow_columns_returns_only_requested(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        tid = _insert_transcription(conn)
        row = tx_repo.get_by_id(conn, tid, columns=("id", "segments"))
        assert set(row.keys()) == {"id", "segments"}
        assert row["id"] == tid
    finally:
        conn.close()


def test_get_by_id_include_deleted_false_hides_soft_deleted_row(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        tid = _insert_transcription(conn)
        conn.execute(
            "UPDATE transcriptions SET deleted_at = ? WHERE id = ?", (1234567890.0, tid)
        )
        conn.commit()

        assert tx_repo.get_by_id(conn, tid, include_deleted=True) is not None
        assert tx_repo.get_by_id(conn, tid, include_deleted=False) is None
    finally:
        conn.close()


# ---------------------------------------------------------------- transcriptions.get_many_by_ids

def test_get_many_by_ids_empty_list_returns_empty_without_query(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        assert tx_repo.get_many_by_ids(conn, []) == []
    finally:
        conn.close()


def test_get_many_by_ids_returns_matching_rows_with_requested_columns(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        tid1 = _insert_transcription(conn, source_name="a.mp3")
        tid2 = _insert_transcription(conn, source_name="b.mp3")
        _insert_transcription(conn, source_name="c.mp3")  # не в вибірці

        rows = tx_repo.get_many_by_ids(conn, [tid1, tid2], columns=("id", "segments"))
        assert {r["id"] for r in rows} == {tid1, tid2}
        assert set(rows[0].keys()) == {"id", "segments"}
    finally:
        conn.close()


# ---------------------------------------------------------------- transcriptions.exists

def test_exists_true_for_present_row_false_otherwise(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        tid = _insert_transcription(conn)
        assert tx_repo.exists(conn, tid) is True
        assert tx_repo.exists(conn, tid + 999) is False
    finally:
        conn.close()


# ---------------------------------------------------------------- transcriptions.delete_by_id

def test_delete_by_id_removes_row(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        tid = _insert_transcription(conn)
        tx_repo.delete_by_id(conn, tid)
        conn.commit()
        assert tx_repo.exists(conn, tid) is False
    finally:
        conn.close()


# ---------------------------------------------------------------- speakers

def test_speakers_get_by_id_returns_none_when_missing(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        assert speakers_repo.get_by_id(conn, 999999) is None
    finally:
        conn.close()


def test_speakers_get_full_by_id_returns_full_column_set(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("INSERT INTO speakers (name, color) VALUES (?, ?)", ("Юля", "#ffcc00"))
        conn.commit()
        sid = cur.lastrowid

        row = speakers_repo.get_full_by_id(conn, sid)
        assert row is not None
        assert set(row.keys()) == set(speakers_repo.FULL_COLUMNS)
        assert row["name"] == "Юля"
        assert row["color"] == "#ffcc00"
        assert row["is_self"] == 0
    finally:
        conn.close()


def test_speakers_get_by_id_narrow_columns(tmp_path):
    conn = sqlite3.connect(_db(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("INSERT INTO speakers (name) VALUES (?)", ("Андрій",))
        conn.commit()
        sid = cur.lastrowid

        row = speakers_repo.get_by_id(conn, sid, columns=("id", "name"))
        assert set(row.keys()) == {"id", "name"}
        assert row["name"] == "Андрій"
    finally:
        conn.close()
