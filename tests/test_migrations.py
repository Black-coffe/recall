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
