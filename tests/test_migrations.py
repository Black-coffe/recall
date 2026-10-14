"""Tests для DB migrations (Phase 9.6 та існуючі версії).

Тести працюють з тимчасовими SQLite БД у tmp_path — реальна
whisper_history.db не зачіпається.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.migrations import init_database


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    """Повертає {col_name: {type, notnull, dflt_value}} для колонок table."""
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
        # Recording-specific колонки nullable (бо для YouTube=NULL)
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
    """Повторні init_database() не повинні падати чи дублювати колонки."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)  # повторно
    init_database(db)  # ще раз

    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, 'audio_downloads')
        # Кожна колонка має зустрітися рівно раз — pragma не вертає duplicates
        assert list(cols).count('source_type') == 1
        assert _schema_version(conn) >= 5
    finally:
        conn.close()


def test_v5_existing_youtube_rows_get_source_type_youtube(tmp_path: Path):
    """Якщо БД вже існує з v4 і має YouTube-завантаження, після v5
    усі вони повинні мати source_type='youtube'."""
    db = str(tmp_path / 'pre_v5.db')

    # Створюємо БД у стані v4 (без source_type у audio_downloads)
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
    # Додаємо існуючі YouTube-завантаження ДО міграції v5
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

    # Запускаємо міграцію
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
    """Можна вставити recording-row з source_type='recording' і recording_*
    колонками. Перевіряємо що SELECT WHERE source_type='recording' працює."""
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
    """Рекординги мусять мати унікальний youtube_id surrogate
    (наприклад, 'recording_<sid>') — два recording'а отримають різні
    sid'и, отже UNIQUE не блокуватиме."""
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

        # Сід "Ви" створено з is_self=1
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
        # PK на (transcription_id, raw_label)
        pk_rows = conn.execute(
            "PRAGMA table_info(transcription_speaker_map)"
        ).fetchall()
        pk_cols = {row[1] for row in pk_rows if row[5] > 0}
        assert pk_cols == {'transcription_id', 'raw_label'}
    finally:
        conn.close()


def test_v6_speakers_name_unique_case_insensitive_ascii(tmp_path: Path):
    """SQLite COLLATE NOCASE працює тільки для ASCII.
    Cyrillic case-insensitivity робиться нормалізацією на app-level."""
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
    """Однакові Cyrillic-імена все одно блокуються (exact-case)."""
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
        # Створюємо transcription
        conn.execute('''
            INSERT INTO transcriptions
              (source_type, source_name, transcript_text)
            VALUES ('file', 'meet.mp3', 'hello world')
        ''')
        tid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        # Створюємо speaker і мапимо SPEAKER_00 → нього
        conn.execute("INSERT INTO speakers (name, usage_count) VALUES ('Юля', 0)")
        sid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        conn.execute('''
            INSERT INTO transcription_speaker_map
              (transcription_id, raw_label, speaker_id)
            VALUES (?, ?, ?)
        ''', (tid, 'SPEAKER_00', sid))

        # Unnamed label з NULL speaker_id (ще не іменований)
        conn.execute('''
            INSERT INTO transcription_speaker_map
              (transcription_id, raw_label, speaker_id)
            VALUES (?, ?, NULL)
        ''', (tid, 'SPEAKER_01'))
        conn.commit()

        # JOIN читання: SPEAKER_00 → Юля, SPEAKER_01 → NULL
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
    """Не можна двічі мапити той самий raw_label у одному transcript-і."""
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
        # Сід "Ви" не повинен дублюватися
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
        # усі document-колонки nullable (для аудіо-транскриптів = NULL)
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

        # FTS5-тригер з v3 повинен проіндексувати документ
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
            assert col in cols, f"jobs.{col} відсутня"
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
    """Повторний init_database() (напр. другий рестарт застосунку) не падає
    'table already exists' і не дублює версію в schema_versions."""
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
    """Крайній випадок атомарності: якщо schema_versions "відкотили" (рядок
    26 видалено вручну/зовнішнім інструментом), а таблиця jobs фізично вже
    існує — повторний init_database() не падає 'table already exists' і
    відновлює версійний рядок (CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE
    ідемпотентні незалежно один від одного).

    T4.6 додав v27 ПІСЛЯ v26 — щоб MAX(version) справді впав нижче 26 (а не
    лишився 27 через рядок наступної міграції), видаляємо весь хвіст версій
    ≥26, не лише 26. Так тест і надалі перевіряє саме "втрату
    version-бухгалтерії при фізично вже застосованій схемі", а не залежить
    від того, яка міграція є найновішою на момент запуску."""
    db = str(tmp_path / 'test.db')
    init_database(db)
    assert _schema_version(_conn := sqlite3.connect(db)) >= 26
    _conn.execute("DELETE FROM schema_versions WHERE version >= 26")
    _conn.commit()
    _conn.close()

    # current_version тепер < 26 знову (MAX(version) впав), тож блок v26
    # (і наступні) виконаються повторно поверх уже існуючих таблиць.
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
        # nullable (default = живий запис)
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
            assert col in cols, f"speaker_merges.{col} відсутня"
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
    """Живі рядки (deleted_at IS NULL) лишаються видимими — migration не
    зачіпає жодного існуючого запису."""
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
    """Спека directions-projects-tags, історія 01: дерево напрямків + словник тегів."""
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
        # source за замовчуванням — ручний тег
        assert conn.execute("SELECT source FROM record_tags").fetchone()[0] == 'manual'
        conn.execute("INSERT INTO tg_chat_tags (chat_id, tag_id) VALUES (777, ?)", (tag_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tg_chat_tags (chat_id, tag_id) VALUES (777, ?)",
                         (tag_id,))
    finally:
        conn.close()


def test_v45_applies_on_top_of_a_v44_database(tmp_path: Path):
    """БД рівня v44 доганяє схему, не втрачаючи даних і не чіпаючи name_norm."""
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
    """Повернути БД у стан «до v46»: таблиці немає, версія 45."""
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE IF EXISTS embedding_epochs")
    conn.execute("DELETE FROM schema_versions WHERE version >= 46")
    conn.commit()
    conn.close()


def test_v46_stamps_existing_pairs_as_prefix_format_1(tmp_path: Path):
    """Усе, що існувало ДО v46, побудовано старим форматом префікса — бекфіл
    саме це й записує, а повторна міграція рядка не множить."""
    db = str(tmp_path / 'test.db')
    init_database(db)

    conn = sqlite3.connect(db)
    for name in ('Планерка з фондом', 'Дзвінок про кошторис'):
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "embedding_model, embedding_version) VALUES ('file', ?, ?, ?, 4)",
            (name, 'Обговорили терміни здачі.', 'intfloat/multilingual-e5-large'))
    # запис без ембедингу і запис до v28 (версія NULL) у таблицю не потрапляють
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

    init_database(db)          # повторна міграція — той самий рядок
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
    """Повернути `categories` у стан «до v47»: inline UNIQUE на `name` (v15),
    глобальний UNIQUE-індекс на `name_norm` (v20), без індексу братів."""
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
    """Рядки всіх чотирьох таблиць, що посилаються на напрямок, + проєкт."""
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
    """Перебудова `categories` не міняє id і не зачіпає жодного посилання —
    попри `ON DELETE SET NULL` на `tg_monitored_chats.category_id` (v18)."""
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
            assert kept == 1, f"{table}: посилання на напрямок обнулилось"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _schema_version(conn) >= 47
    finally:
        conn.close()


def test_v47_drops_global_name_unique_and_adds_sibling_index(tmp_path: Path):
    """DDL без UNIQUE на `name`, `slug` унікальний, є `idx_categories_parent_name`."""
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
        # однойменні проєкти різних напрямків співіснують…
        conn.execute("INSERT INTO categories (name, name_norm, slug, parent_id) "
                     "VALUES ('Маркетинг', 'маркетинг', 'm1', ?)", (fund,))
        conn.execute("INSERT INTO categories (name, name_norm, slug, parent_id) "
                     "VALUES ('Маркетинг', 'маркетинг', 'm2', ?)", (free,))
        # …а два однойменні брати — ні
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO categories (name, name_norm, slug, parent_id) "
                         "VALUES ('Маркетинг', 'маркетинг', 'm3', ?)", (fund,))
        # напрямки між собою теж (COALESCE(parent_id, 0) = 0)
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
    """Доказ пастки, заради якої міграція гасить `foreign_keys`: з увімкненими
    FK `DROP TABLE categories` спрацьовує як DELETE і мовчки зануляє
    `tg_monitored_chats.category_id` (`ON DELETE SET NULL`, v18). Відкат
    транзакції лишає БД цілою — і той самий рядок далі на місці."""
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
    """Запис вимірювання (хвиля 7, історія 22): гард `PRAGMA foreign_keys=OFF`
    у v47 на чинному шляху нічого не несе.

    `init_database` відкриває СИРИЙ `sqlite3.connect` (`migrations.py:139`), а
    там `foreign_keys` за замовчуванням вимкнені; `PRAGMA foreign_keys = ON`
    ставить `app/db/connection.py` — інше зʼєднання, не те, на якому йде
    перебудова. Тому гард лишається захисним (на випадок виклику
    `init_database` на FK-ON зʼєднанні), а пастку, заради якої він написаний,
    доводить `test_v47_drop_table_with_fk_on_would_null_chat_category` на
    зібраному руками FK-ON зʼєднанні. `init_database` заради тесту не
    рефакторимо — ціна більша за доказ."""
    from app.db.connection import get_db_connection

    db = str(tmp_path / 'test.db')
    init_database(db)

    raw = sqlite3.connect(db)
    try:
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0, \
            "шлях міграції: FK і так вимкнені — гард захисний"
    finally:
        raw.close()

    with get_db_connection(db) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, \
            "зʼєднання застосунку FK вмикає — але міграція йде не ним"


def test_v48_video_timeline_columns_are_additive_and_idempotent(tmp_path: Path):
    db = str(tmp_path / 'test.db')
    init_database(db)
    init_database(db)                      # повторний прогін — без помилок
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
