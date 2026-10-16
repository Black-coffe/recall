"""Tests for DB migrations (Phase 9.6 and the existing versions).

The tests work with temporary SQLite DBs in tmp_path - the real
whisper_history.db is not touched.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.migrations import init_database


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    """Returns {col_name: {type, notnull, dflt_value}} for the columns of table."""
    cols = {}
    for row in conn.execute(f"PRAGMA table_info({table})"):
        cols[row[1]] = {
            'type': row[2],
            'notnull': bool(row[3]),
            'default': row[4],
        }
    return cols


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()
    return row[0] if row[0] is not None else 0


# ---------------------------------------------------------------- v5

def test_v5_adds_source_type_with_default_youtube(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'audio_downloads')
        assert 'source_type' in cols
        assert cols['source_type']['notnull'] is True
        assert cols['source_type']['default'] == "'youtube'"
        assert _schema_version(conn) >= 5
    finally:
        conn.close()


def test_v5_adds_recording_columns(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'audio_downloads')
        assert 'recording_session_id' in cols
        assert 'recording_segments' in cols
        assert 'recording_duration_sec' in cols
        # Recording-specific columns are nullable (because NULL for YouTube)
        assert cols['recording_session_id']['notnull'] is False
    finally:
        conn.close()


def test_v5_creates_indexes(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='audio_downloads'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert 'idx_audio_source_type' in names
        assert 'idx_audio_recording_session_id' in names
    finally:
        conn.close()


def test_v5_migration_is_idempotent(tmp_path: Path):
    """Repeated init_database() calls must not fail or duplicate columns."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)  # again
    init_database(db)  # once more

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'audio_downloads')
        # Each column must appear exactly once - pragma does not return duplicates
        assert list(cols).count('source_type') == 1
        assert _schema_version(conn) >= 5
    finally:
        conn.close()


def test_v5_existing_youtube_rows_get_source_type_youtube(tmp_path: Path):
    """If the DB already exists at v4 and has YouTube downloads, after v5
    all of them must have source_type='youtube'."""
    db = str(tmp_path / 'pre_v5.db')

    # Create the DB in the v4 state (without source_type in audio_downloads)
    conn = sqlite3.connect(db)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE schema_versions
                    (version INTEGER PRIMARY KEY,
                     applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                     description TEXT)''')
    conn.execute('''CREATE TABLE audio_downloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        youtube_url TEXT NOT NULL,
        youtube_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        author TEXT,
        duration INTEGER,
        thumbnail_url TEXT,
        file_path TEXT NOT NULL,
        file_size INTEGER,
        audio_quality TEXT,
        audio_format TEXT,
        download_time REAL,
        view_count INTEGER,
        like_count INTEGER,
        description TEXT,
        upload_date TEXT,
        tags TEXT
    )''')
    # Add existing YouTube downloads BEFORE the v5 migration
    conn.execute(
        '''INSERT INTO audio_downloads
           (youtube_url, youtube_id, title, file_path)
           VALUES (?, ?, ?, ?)''',
        ('https://youtu.be/abc', 'abc12345678', 'Old YouTube video', '/path/old.mp3')
    )
    conn.execute("INSERT INTO schema_versions (version) VALUES (1)")
    conn.execute("INSERT INTO schema_versions (version) VALUES (2)")
    conn.execute("INSERT INTO schema_versions (version) VALUES (3)")
    conn.execute("INSERT INTO schema_versions (version) VALUES (4)")
    conn.commit()
    conn.close()

    # Run the migration
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT source_type FROM audio_downloads WHERE youtube_id = 'abc12345678'"
        ).fetchone()
        assert row[0] == 'youtube'
    finally:
        conn.close()


def test_v5_can_insert_recording_row(tmp_path: Path):
    """A recording row with source_type='recording' and recording_*
    columns can be inserted. We check that SELECT WHERE source_type='recording' works."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute('''
            INSERT INTO audio_downloads
              (youtube_url, youtube_id, title, file_path,
               source_type, recording_session_id,
               recording_segments, recording_duration_sec,
               author, duration, file_size, audio_format)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'recording://rec_abc123',
            'recording_rec_abc123',
            'Зустріч 2026-05-04',
            '/path/to/final.mp3',
            'recording',
            'rec_abc123',
            2,
            245.7,
            'Локальний запис',
            245,
            5_242_880,
            'mp3',
        ))
        conn.commit()

        recordings = conn.execute(
            "SELECT title, recording_session_id, recording_duration_sec "
            "FROM audio_downloads WHERE source_type = 'recording'"
        ).fetchall()
        assert len(recordings) == 1
        assert recordings[0][0] == 'Зустріч 2026-05-04'
        assert recordings[0][1] == 'rec_abc123'
        assert recordings[0][2] == 245.7

        # Counts by source_type
        counts = dict(conn.execute(
            "SELECT source_type, COUNT(*) FROM audio_downloads GROUP BY source_type"
        ).fetchall())
        assert counts == {'recording': 1}
    finally:
        conn.close()


def test_v5_unique_youtube_id_does_not_block_multiple_recordings(tmp_path: Path):
    """Recordings must have a unique youtube_id surrogate
    (for example, 'recording_<sid>') - two recordings get different
    sids, hence UNIQUE will not block."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        for sid in ('rec_aaa', 'rec_bbb'):
            conn.execute('''
                INSERT INTO audio_downloads
                  (youtube_url, youtube_id, title, file_path,
                   source_type, recording_session_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                f'recording://{sid}',
                f'recording_{sid}',
                f'Test {sid}',
                f'/path/{sid}.mp3',
                'recording',
                sid,
            ))
        conn.commit()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM audio_downloads WHERE source_type='recording'"
        ).fetchone()[0]
        assert cnt == 2
    finally:
        conn.close()


# ---------------------------------------------------------------- v6

def test_v6_creates_speakers_table_with_self_seed(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'speakers')
        for required in ('id', 'name', 'color', 'is_self', 'usage_count',
                         'embedding', 'created_at', 'updated_at'):
            assert required in cols, f'speakers.{required} missing'

        # The seed "Ви" was created with is_self=1
        rows = conn.execute(
            "SELECT name, is_self FROM speakers WHERE is_self = 1"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 'Ви'
        assert _schema_version(conn) >= 6
    finally:
        conn.close()


def test_v6_creates_transcription_speaker_map_table(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'transcription_speaker_map')
        for required in ('transcription_id', 'raw_label', 'speaker_id'):
            assert required in cols
        # PK on (transcription_id, raw_label)
        pk_rows = conn.execute(
            "PRAGMA table_info(transcription_speaker_map)"
        ).fetchall()
        pk_cols = {row[1] for row in pk_rows if row[5] > 0}
        assert pk_cols == {'transcription_id', 'raw_label'}
    finally:
        conn.close()


def test_v6_speakers_name_unique_case_insensitive_ascii(tmp_path: Path):
    """SQLite COLLATE NOCASE works only for ASCII.
    Cyrillic case-insensitivity is done by normalization at the app level."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO speakers (name) VALUES ('John')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO speakers (name) VALUES ('JOHN')")
            conn.commit()
    finally:
        conn.close()


def test_v6_speakers_name_exact_match_unique(tmp_path: Path):
    """Identical Cyrillic names are still blocked (exact-case)."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO speakers (name) VALUES ('Андрій')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO speakers (name) VALUES ('Андрій')")
            conn.commit()
    finally:
        conn.close()


def test_v6_can_map_raw_label_to_speaker(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        # Create a transcription
        conn.execute('''
            INSERT INTO transcriptions
              (source_type, source_name, transcript_text)
            VALUES ('file', 'meet.mp3', 'hello world')
        ''')
        tid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        # Create a speaker and map SPEAKER_00 -> it
        conn.execute("INSERT INTO speakers (name, usage_count) VALUES ('Юля', 0)")
        sid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        conn.execute('''
            INSERT INTO transcription_speaker_map
              (transcription_id, raw_label, speaker_id)
            VALUES (?, ?, ?)
        ''', (tid, 'SPEAKER_00', sid))

        # Unnamed label with a NULL speaker_id (not named yet)
        conn.execute('''
            INSERT INTO transcription_speaker_map
              (transcription_id, raw_label, speaker_id)
            VALUES (?, ?, NULL)
        ''', (tid, 'SPEAKER_01'))
        conn.commit()

        # JOIN read: SPEAKER_00 -> «Юля», SPEAKER_01 -> NULL
        rows = conn.execute('''
            SELECT m.raw_label, s.name
            FROM transcription_speaker_map m
            LEFT JOIN speakers s ON s.id = m.speaker_id
            WHERE m.transcription_id = ?
            ORDER BY m.raw_label
        ''', (tid,)).fetchall()
        assert rows == [('SPEAKER_00', 'Юля'), ('SPEAKER_01', None)]
    finally:
        conn.close()


def test_v6_pk_prevents_duplicate_raw_label(tmp_path: Path):
    """The same raw_label cannot be mapped twice in one transcript."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute('''
            INSERT INTO transcriptions (source_type, source_name)
            VALUES ('file', 'a.mp3')
        ''')
        tid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        conn.execute('''
            INSERT INTO transcription_speaker_map (transcription_id, raw_label)
            VALUES (?, 'SPEAKER_00')
        ''', (tid,))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('''
                INSERT INTO transcription_speaker_map (transcription_id, raw_label)
                VALUES (?, 'SPEAKER_00')
            ''', (tid,))
            conn.commit()
    finally:
        conn.close()


def test_v6_migration_is_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        # The seed "Ви" must not be duplicated
        cnt = conn.execute(
            "SELECT COUNT(*) FROM speakers WHERE name = 'Ви'"
        ).fetchone()[0]
        assert cnt == 1
        assert _schema_version(conn) >= 6
    finally:
        conn.close()


def test_v6_indexes_created(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name IN ('speakers', 'transcription_speaker_map')"
        ).fetchall()
        names = {r[0] for r in idx_rows}
        assert 'idx_speakers_name' in names
        assert 'idx_speakers_usage' in names
        assert 'idx_tspeaker_map_speaker' in names
    finally:
        conn.close()


# ---------------------------------------------------------------- v16

def test_v16_adds_document_columns(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'transcriptions')
        for required in ('doc_type', 'original_filename', 'page_count',
                         'byte_size', 'content_hash', 'parsed_at', 'parser_version'):
            assert required in cols, f'transcriptions.{required} missing'
        # all document columns are nullable (NULL for audio transcripts)
        assert cols['doc_type']['notnull'] is False
        assert _schema_version(conn) >= 16
    finally:
        conn.close()


def test_v16_content_hash_index(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='transcriptions'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert 'idx_transcriptions_content_hash' in names
    finally:
        conn.close()


def test_v16_can_insert_document_row(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute('''
            INSERT INTO transcriptions
              (source_type, source_name, file_path, transcript_text,
               doc_type, original_filename, page_count, byte_size,
               content_hash, parsed_at, parser_version)
            VALUES ('document', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'звіт.pdf', '/documents/20260528_zvit.pdf', 'текст документа',
            'pdf', 'звіт.pdf', 12, 204800, 'a' * 64, '2026-05-28T10:00:00', 1,
        ))
        conn.commit()

        row = conn.execute(
            "SELECT source_type, doc_type, page_count, transcript_text "
            "FROM transcriptions WHERE source_type='document'"
        ).fetchone()
        assert row[0] == 'document'
        assert row[1] == 'pdf'
        assert row[2] == 12
        assert 'текст документа' in row[3]

        # the FTS5 trigger from v3 must index the document
        fts = conn.execute(
            "SELECT COUNT(*) FROM transcriptions_fts WHERE transcriptions_fts MATCH 'документа'"
        ).fetchone()[0]
        assert fts == 1
    finally:
        conn.close()


def test_v16_migration_is_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'transcriptions')
        assert list(cols).count('content_hash') == 1
        assert _schema_version(conn) >= 16
    finally:
        conn.close()


# ---------------------------------------------------------------- v17

def test_v17_adds_provenance_columns(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        chunk_cols = _columns(conn, 'chunks')
        assert 'page' in chunk_cols
        assert 'section' in chunk_cols
        tcols = _columns(conn, 'transcriptions')
        assert 'structure_json' in tcols
        assert _schema_version(conn) >= 17
    finally:
        conn.close()


def test_v17_can_insert_chunk_with_provenance(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute('''
            INSERT INTO transcriptions (source_type, source_name, transcript_text,
                                        doc_type, structure_json)
            VALUES ('document', 'deck.pptx', 'slide text', 'pptx', '[{"text":"s","page":1}]')
        ''')
        tid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('''
            INSERT INTO chunks (transcription_id, chunk_index, text, page, section)
            VALUES (?, 0, 'slide text', 3, 'Бюджет')
        ''', (tid,))
        conn.commit()
        row = conn.execute(
            "SELECT page, section FROM chunks WHERE transcription_id = ?", (tid,)
        ).fetchone()
        assert row[0] == 3
        assert row[1] == 'Бюджет'
    finally:
        conn.close()


def test_v17_migration_is_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        chunk_cols = _columns(conn, 'chunks')
        assert list(chunk_cols).count('page') == 1
        assert _schema_version(conn) >= 17
    finally:
        conn.close()


# ---------------------------------------------------------------- v26 (T2.1)

def test_v26_creates_jobs_table(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'jobs')
        for col in ('id', 'kind', 'state', 'created_at', 'started_at',
                    'finished_at', 'error', 'meta_json', 'updated_at'):
            assert col in cols, f"jobs.{col} is missing"
        assert cols['kind']['notnull'] is True
        assert cols['state']['notnull'] is True
        assert _schema_version(conn) >= 26
    finally:
        conn.close()


def test_v26_creates_indexes(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert 'idx_jobs_state' in names
        assert 'idx_jobs_created_at' in names
    finally:
        conn.close()


def test_v26_can_insert_and_upsert_job_row(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO jobs (id, kind, state, created_at, updated_at) "
            "VALUES ('job1', 'transcription', 'queued', 1.0, 1.0)"
        )
        conn.commit()
        conn.execute(
            "INSERT INTO jobs (id, kind, state, created_at, updated_at) "
            "VALUES ('job1', 'transcription', 'running', 1.0, 2.0) "
            "ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
            "updated_at = excluded.updated_at"
        )
        conn.commit()
        row = conn.execute(
            "SELECT state FROM jobs WHERE id = 'job1'"
        ).fetchone()
        assert row[0] == 'running'
    finally:
        conn.close()


def test_v26_migration_is_idempotent(tmp_path: Path):
    """A repeated init_database() (e.g. a second app restart) does not fail with
    'table already exists' and does not duplicate the version in schema_versions."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'jobs')
        assert list(cols).count('state') == 1
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE version = 26"
        ).fetchone()
        assert version_rows[0] == 1
        assert _schema_version(conn) >= 26
    finally:
        conn.close()


def test_v26_rerun_after_version_row_missing_is_safe(tmp_path: Path):
    """Edge case of atomicity: if schema_versions was "rolled back" (row
    26 deleted manually/by an external tool), while the jobs table physically
    already exists - a repeated init_database() does not fail with 'table already exists' and
    restores the version row (CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE
    are idempotent independently of each other).

    T4.6 added v27 AFTER v26 - so that MAX(version) really drops below 26 (and does not
    stay 27 because of the next migration's row), we delete the whole tail of versions
    >=26, not just 26. This way the test keeps checking exactly "the loss of
    version bookkeeping with a physically already applied schema", and does not depend
    on which migration is the newest at the moment of the run."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    assert _schema_version(_conn := sqlite3.connect(db)) >= 26
    _conn.execute("DELETE FROM schema_versions WHERE version >= 26")
    _conn.commit()
    _conn.close()

    # current_version is < 26 again (MAX(version) dropped), so the v26 block
    # (and the following ones) run again on top of the already existing tables.
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'jobs')
        assert 'id' in cols
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE version = 26"
        ).fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- v27 (T4.6)

def test_v27_adds_deleted_at_columns(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        t_cols = _columns(conn, 'transcriptions')
        a_cols = _columns(conn, 'audio_downloads')
        assert 'deleted_at' in t_cols
        assert 'deleted_at' in a_cols
        # nullable (default = live record)
        assert t_cols['deleted_at']['notnull'] is False
        assert a_cols['deleted_at']['notnull'] is False
        assert _schema_version(conn) >= 27
    finally:
        conn.close()


def test_v27_deleted_at_indexes_created(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name IN ('transcriptions', 'audio_downloads')"
            ).fetchall()
        }
        assert 'idx_transcriptions_deleted_at' in names
        assert 'idx_audio_deleted_at' in names
    finally:
        conn.close()


def test_v27_creates_speaker_merges_table(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'speaker_merges')
        for col in ('id', 'keep_id', 'merged_speaker_ids', 'before_json',
                    'merged_at', 'restored_at'):
            assert col in cols, f"speaker_merges.{col} is missing"
        assert cols['keep_id']['notnull'] is True
        assert cols['before_json']['notnull'] is True
        assert cols['restored_at']['notnull'] is False
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='speaker_merges'"
            ).fetchall()
        }
        assert 'idx_speaker_merges_merged_at' in names
    finally:
        conn.close()


def test_v27_deleted_at_defaults_to_null_for_existing_rows(tmp_path: Path):
    """Live rows (deleted_at IS NULL) stay visible - the migration does not
    touch any existing record."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, language) "
        "VALUES ('file', 'tx1', 'test', 'uk')"
    )
    conn.commit()
    row = conn.execute("SELECT deleted_at FROM transcriptions WHERE source_name = 'tx1'").fetchone()
    assert row[0] is None
    conn.close()


def test_v27_migration_is_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        t_cols = _columns(conn, 'transcriptions')
        assert list(t_cols).count('deleted_at') == 1
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE version = 27"
        ).fetchone()
        assert version_rows[0] == 1
        assert _schema_version(conn) >= 27
    finally:
        conn.close()


# ---------------------------------------------------------------- v45

def test_v45_adds_parent_id_tag_tables_and_suggestions(tmp_path: Path):
    """Spec directions-projects-tags, story 01: direction tree + tag vocabulary."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        assert 'parent_id' in _columns(conn, 'categories')
        assert 'suggested_tags_json' in _columns(conn, 'transcriptions')
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {'tags', 'record_tags', 'tg_chat_tags'} <= tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert 'idx_categories_parent' in indexes
        assert 'idx_record_tags_tag' in indexes
        assert _schema_version(conn) >= 45
    finally:
        conn.close()


def test_v45_tag_tables_enforce_their_keys(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO tags (name, name_norm) VALUES ('Інвестори', 'інвестори')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tags (name, name_norm) VALUES ('інвестори', 'інвестори')")
        tag_id = conn.execute("SELECT id FROM tags").fetchone()[0]
        conn.execute("INSERT INTO record_tags (transcription_id, tag_id) VALUES (1, ?)",
                     (tag_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO record_tags (transcription_id, tag_id) VALUES (1, ?)",
                         (tag_id,))
        # source defaults to a manual tag
        assert conn.execute("SELECT source FROM record_tags").fetchone()[0] == 'manual'
        conn.execute("INSERT INTO tg_chat_tags (chat_id, tag_id) VALUES (777, ?)", (tag_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tg_chat_tags (chat_id, tag_id) VALUES (777, ?)",
                         (tag_id,))
    finally:
        conn.close()


def test_v45_applies_on_top_of_a_v44_database(tmp_path: Path):
    """A v44-level DB catches up with the schema without losing data or touching name_norm."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO categories (name, name_norm) VALUES "
                 "('Юридичне', 'юридичне')")
    conn.execute("DELETE FROM schema_versions WHERE version >= 45")
    conn.commit()
    conn.close()

    init_database(db)

    conn = sqlite3.connect(db)
    try:
        assert 'parent_id' in _columns(conn, 'categories')
        assert conn.execute(
            "SELECT name_norm FROM categories WHERE name = 'Юридичне'"
        ).fetchone()[0] == 'юридичне'
        assert _schema_version(conn) >= 45
    finally:
        conn.close()


def test_v45_migration_is_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        assert list(_columns(conn, 'categories')).count('parent_id') == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE version = 45").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- v46

def _rollback_to_v45(db: str) -> None:
    """Return the DB to the "before v46" state: no table, version 45."""
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE IF EXISTS embedding_epochs")
    conn.execute("DELETE FROM schema_versions WHERE version >= 46")
    conn.commit()
    conn.close()


def test_v46_stamps_existing_pairs_as_prefix_format_1(tmp_path: Path):
    """Everything that existed BEFORE v46 was built with the old prefix format - the backfill
    writes exactly that, and a repeated migration does not multiply the row."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    for name in ('Планерка з фондом', 'Дзвінок про кошторис'):
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "embedding_model, embedding_version) VALUES ('file', ?, ?, ?, 4)",
            (name, 'Обговорили терміни здачі.', 'intfloat/multilingual-e5-large'))
    # a record without an embedding and a pre-v28 record (version NULL) do not get into the table
    conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text) "
                 "VALUES ('file', 'Ще не ембеджено', 'Текст.')")
    conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                 "embedding_model) VALUES ('file', 'Легасі', 'Текст.', 'старий/ембедер')")
    conn.commit()
    conn.close()
    _rollback_to_v45(db)

    init_database(db)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT model, version, prefix_format FROM embedding_epochs "
                            "ORDER BY version").fetchall()
        assert rows == [('intfloat/multilingual-e5-large', 4, 1)]
        assert _schema_version(conn) >= 46
    finally:
        conn.close()

    init_database(db)          # repeated migration - the same row
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM embedding_epochs").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE version = 46").fetchone()[0] == 1
    finally:
        conn.close()


def test_v46_on_empty_database_creates_table_without_rows(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'embedding_epochs')
        assert set(cols) == {'model', 'version', 'prefix_format', 'created_at'}
        assert cols['prefix_format']['notnull'] is True
        assert conn.execute("SELECT COUNT(*) FROM embedding_epochs").fetchone()[0] == 0
    finally:
        conn.close()


def test_v46_pair_is_unique(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO embedding_epochs (model, version, prefix_format) "
                     "VALUES ('intfloat/multilingual-e5-large', 5, 2)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO embedding_epochs (model, version, prefix_format) "
                         "VALUES ('intfloat/multilingual-e5-large', 5, 1)")
    finally:
        conn.close()


# ---------------------------------------------------------------- v47

def _rollback_to_v46(db: str) -> None:
    """Return `categories` to the "before v47" state: inline UNIQUE on `name` (v15),
    a global UNIQUE index on `name_norm` (v20), without the siblings index."""
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(categories)")]
    conn.execute("DROP INDEX IF EXISTS idx_categories_parent_name")
    conn.execute('''CREATE TABLE categories_old (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        slug TEXT UNIQUE,
        color TEXT,
        icon TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        name_norm TEXT,
        parent_id INTEGER
    )''')
    cols_sql = ", ".join(cols)
    conn.execute(f"INSERT INTO categories_old ({cols_sql}) "
                 f"SELECT {cols_sql} FROM categories")
    conn.execute("DROP TABLE categories")
    conn.execute("ALTER TABLE categories_old RENAME TO categories")
    conn.execute("CREATE UNIQUE INDEX idx_categories_name_norm ON categories(name_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id)")
    conn.execute("DELETE FROM schema_versions WHERE version >= 47")
    conn.commit()
    conn.close()


def _seed_refs(db: str) -> dict:
    """Rows of all four tables that reference a direction, + a project."""
    conn = sqlite3.connect(db)
    fund = conn.execute("SELECT id FROM categories WHERE name = 'Фонд Робота'"
                        ).fetchone()[0]
    conn.execute("INSERT INTO categories (name, name_norm, slug, sort_order, parent_id) "
                 "VALUES ('Акмекорп', 'акмекорп', 'acmecorp', 9, ?)", (fund,))
    proj = conn.execute("SELECT id FROM categories WHERE name = 'Акмекорп'").fetchone()[0]
    conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                 "category_id) VALUES (-100, 'Акмекорп чат', 'group', 1, ?)", (proj,))
    conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                 "category_id) VALUES ('telegram', '[TG] Акмекорп', 'Текст.', ?)", (proj,))
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, "
                 "source_type, category_id) VALUES ('u', 'y1', 'Запис', 'f.mp3', "
                 "'recording', ?)", (proj,))
    conn.execute("INSERT INTO copilot_sessions (recording_session_id, category_id) "
                 "VALUES ('rec_1', ?)", (proj,))
    conn.commit()
    conn.close()
    return {"work": fund, "proj": proj}


def test_v47_rebuild_keeps_ids_and_all_category_refs(tmp_path: Path):
    """Rebuilding `categories` changes no id and breaks no reference -
    despite `ON DELETE SET NULL` on `tg_monitored_chats.category_id` (v18)."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    ids = _seed_refs(db)
    _rollback_to_v46(db)

    init_database(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT id FROM categories WHERE name = 'Акмекорп'"
                            ).fetchone()[0] == ids["proj"]
        assert conn.execute("SELECT parent_id FROM categories WHERE id = ?",
                            (ids["proj"],)).fetchone()[0] == ids["work"]
        for table in ('tg_monitored_chats', 'transcriptions', 'audio_downloads',
                      'copilot_sessions'):
            kept = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE category_id = ?",
                (ids["proj"],)).fetchone()[0]
            assert kept == 1, f"{table}: the direction reference was nulled"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _schema_version(conn) >= 47
    finally:
        conn.close()


def test_v47_drops_global_name_unique_and_adds_sibling_index(tmp_path: Path):
    """DDL without UNIQUE on `name`, `slug` is unique, `idx_categories_parent_name` exists."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    _rollback_to_v46(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='categories'"
        ).fetchone()[0]
        unique_single = []
        for idx in conn.execute("PRAGMA index_list(categories)").fetchall():
            if not idx[2]:
                continue
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info('{idx[1]}')")]
            unique_single.append((idx[1], cols))
        all_cols = [cols for _, cols in unique_single]
        assert ['name'] not in all_cols, ddl
        assert ['name_norm'] not in all_cols, ddl
        assert ['slug'] in all_cols, ddl
        assert any(n == 'idx_categories_parent_name' for n, _ in unique_single)

        fund = conn.execute("SELECT id FROM categories WHERE name = 'Фонд Робота'"
                            ).fetchone()[0]
        free = conn.execute("SELECT id FROM categories WHERE name = 'Фріланс'").fetchone()[0]
        # same-named projects of different directions coexist...
        conn.execute("INSERT INTO categories (name, name_norm, slug, parent_id) "
                     "VALUES ('Маркетинг', 'маркетинг', 'm1', ?)", (fund,))
        conn.execute("INSERT INTO categories (name, name_norm, slug, parent_id) "
                     "VALUES ('Маркетинг', 'маркетинг', 'm2', ?)", (free,))
        # ...but two same-named siblings do not
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO categories (name, name_norm, slug, parent_id) "
                         "VALUES ('Маркетинг', 'маркетинг', 'm3', ?)", (fund,))
        # directions among themselves too (COALESCE(parent_id, 0) = 0)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO categories (name, name_norm, slug) "
                         "VALUES ('Фріланс', 'фріланс', 'm4')")
    finally:
        conn.close()


def test_v47_migration_is_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    ids = _seed_refs(db)
    init_database(db)
    init_database(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_versions WHERE version = 47"
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT id FROM categories WHERE name = 'Акмекорп'"
                            ).fetchone()[0] == ids["proj"]
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                            "AND name='categories_new'").fetchone()[0] == 0
    finally:
        conn.close()


def test_v47_drop_table_with_fk_on_would_null_chat_category(tmp_path: Path):
    """Proof of the trap for which the migration turns off `foreign_keys`: with FKs
    enabled `DROP TABLE categories` acts as a DELETE and silently nulls
    `tg_monitored_chats.category_id` (`ON DELETE SET NULL`, v18). Rolling back the
    transaction leaves the DB intact - and the same row is still in place."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    ids = _seed_refs(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE categories")
        assert conn.execute("SELECT category_id FROM tg_monitored_chats WHERE chat_id = -100"
                            ).fetchone()[0] is None
        conn.rollback()
    finally:
        conn.close()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT category_id FROM tg_monitored_chats WHERE chat_id = -100"
                            ).fetchone()[0] == ids["proj"]
    finally:
        conn.close()


def test_v47_fk_guard_is_defensive_on_the_init_database_path(tmp_path: Path):
    """Measurement record (wave 7, story 22): the `PRAGMA foreign_keys=OFF` guard
    in v47 carries nothing on the current path.

    `init_database` opens a RAW `sqlite3.connect` (`migrations.py:139`), and
    there `foreign_keys` is off by default; `PRAGMA foreign_keys = ON`
    is set by `app/db/connection.py` - a different connection, not the one the
    rebuild runs on. So the guard stays defensive (for the case of calling
    `init_database` on an FK-ON connection), while the trap it was written for
    is proven by `test_v47_drop_table_with_fk_on_would_null_chat_category` on
    a hand-built FK-ON connection. We do not refactor `init_database` for the test -
    the cost is bigger than the proof."""
    from app.db.connection import get_db_connection

    db = str(tmp_path / 'test.db')
    init_database(db)

    raw = sqlite3.connect(db)
    try:
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0, \
            "migration path: FKs are off anyway - the guard is defensive"
    finally:
        raw.close()

    with get_db_connection(db) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, \
            "the app connection turns FKs on - but the migration does not run on it"


def test_v48_video_timeline_columns_are_additive_and_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)                      # repeated run - no errors
    conn = sqlite3.connect(db)
    try:
        vk = _columns(conn, 'video_keyframes')
        for col in ('end_sec', 'frame_type', 'duplicate_of', 'change_score',
                    'ocr_boxes_json', 'video_time_sec', 'image_path', 'ocr_text'):
            assert col in vk, col
        assert 'video_analysis_params' in _columns(conn, 'transcriptions')
        assert _schema_version(conn) >= 48
    finally:
        conn.close()


def test_v50_pipe_runs_is_additive_and_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)                      # repeated run - no errors
    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'pipe_runs')
        for col in ('pipe', 'trigger', 'scheduled_for', 'started_at', 'finished_at',
                    'status', 'rendered_by', 'cost_usd', 'error', 'chars'):
            assert col in cols, col
        assert _schema_version(conn) >= 50
    finally:
        conn.close()
