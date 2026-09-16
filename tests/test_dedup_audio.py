"""Дублі аудіо/YouTube/записів: хеш на інжесті + офлайн-прохід (Хвиля A).

Тестуються саме ті дві функції, якими користується інжест (`hash_for` +
`find_original` — рівно та пара, що стоїть перед INSERT у
`app/blueprints/transcription.py`), CLI-прохід `mark` — і сам ендпойнт
`POST /api/transcribe` на library-шляху: `transcription_bp` піднімається
окремим Flask-застосунком на тимчасовій БД із фейковим whisper, бойовий
`whisper_history.db` не відкривається.
"""
import sqlite3

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import dedup_audio


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _add(path, text, source_type="file", source_name="Дзвінок",
         content_hash=None, deleted_at=None):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "content_hash, deleted_at) VALUES (?, ?, ?, ?, ?)",
        (source_type, source_name, text, content_hash, deleted_at),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _dup_of(path, tid):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT duplicate_of FROM transcriptions WHERE id = ?",
                       (tid,)).fetchone()
    conn.close()
    return row[0]


def _hash_of(path, tid):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT content_hash FROM transcriptions WHERE id = ?",
                       (tid,)).fetchone()
    conn.close()
    return row[0]


# ============================================================
# Міграція
# ============================================================

def test_migration_is_idempotent(db):
    """Повторний init_database на тій самій БД не падає і колонку не дублює."""
    init_database(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transcriptions)")]
    conn.close()
    assert cols.count("duplicate_of") == 1


# ============================================================
# hash_for
# ============================================================

@pytest.mark.parametrize("source_type", ["file", "youtube", "recording"])
def test_hash_for_covers_audio_sources(source_type):
    assert dedup_audio.hash_for(source_type, "Привіт, Андрію") is not None


@pytest.mark.parametrize("source_type", ["telegram", "document", "copilot"])
def test_hash_for_skips_non_audio_sources(source_type):
    """Telegram і документи свідомо поза дедупом — у них свої правила."""
    assert dedup_audio.hash_for(source_type, "Привіт, Андрію") is None


def test_hash_for_empty_text_is_none():
    """Порожнеча не робить записи дублями одне одного."""
    assert dedup_audio.hash_for("file", "") is None
    assert dedup_audio.hash_for("file", "   \n\n ") is None
    assert dedup_audio.hash_for("file", None) is None


def test_hash_is_of_normalized_text():
    """Різні переноси рядків і трейлінг-пробіли — той самий дзвінок."""
    a = dedup_audio.hash_for("file", "Андрій: бюджет\nМикола: добре")
    b = dedup_audio.hash_for("file", "Андрій: бюджет   \r\nМикола: добре\n\n\n")
    assert a == b


def test_hash_differs_for_different_text():
    assert (dedup_audio.hash_for("file", "бюджет Барселони")
            != dedup_audio.hash_for("file", "бюджет Барселони і Києва"))


# ============================================================
# find_original — шлях інжесту
# ============================================================

def test_find_original_none_when_unique(db):
    with get_db_connection(db) as conn:
        assert dedup_audio.find_original(conn, dedup_audio.hash_for("file", "унікальне")) is None


def test_find_original_none_for_empty_hash(db):
    with get_db_connection(db) as conn:
        assert dedup_audio.find_original(conn, None) is None


def test_find_original_finds_existing_record(db):
    h = dedup_audio.hash_for("file", "Андрій: бюджет")
    first = _add(db, "Андрій: бюджет", content_hash=h, source_name="Оригінал")
    with get_db_connection(db) as conn:
        found = dedup_audio.find_original(conn, h)
    assert found["id"] == first
    assert found["source_name"] == "Оригінал"


def test_find_original_chain_stays_flat(db):
    """Третя копія вказує на оригінал, а не на другу копію."""
    h = dedup_audio.hash_for("file", "Андрій: бюджет")
    first = _add(db, "Андрій: бюджет", content_hash=h)
    second = _add(db, "Андрій: бюджет", content_hash=h)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET duplicate_of = ? WHERE id = ?", (first, second))
    conn.commit()
    conn.close()

    with get_db_connection(db) as conn:
        assert dedup_audio.find_original(conn, h)["id"] == first


def test_find_original_ignores_soft_deleted(db):
    """Видалений запис не може бути оригіналом — інакше копія посилалась би в нікуди."""
    h = dedup_audio.hash_for("file", "Андрій: бюджет")
    _add(db, "Андрій: бюджет", content_hash=h, deleted_at="2026-09-01 10:00:00")
    with get_db_connection(db) as conn:
        assert dedup_audio.find_original(conn, h) is None


def test_find_original_ignores_non_audio_rows(db):
    """Той самий текст у Telegram не робить аудіо-запис дублем."""
    h = dedup_audio.hash_for("file", "Андрій: бюджет")
    _add(db, "Андрій: бюджет", source_type="telegram", content_hash=h)
    with get_db_connection(db) as conn:
        assert dedup_audio.find_original(conn, h) is None


def test_find_original_excludes_self(db):
    h = dedup_audio.hash_for("file", "Андрій: бюджет")
    tid = _add(db, "Андрій: бюджет", content_hash=h)
    with get_db_connection(db) as conn:
        assert dedup_audio.find_original(conn, h, exclude_id=tid) is None


# ============================================================
# mark_duplicates — офлайн-прохід
# ============================================================

def test_mark_dry_run_writes_nothing(db):
    first = _add(db, "Андрій: бюджет")
    second = _add(db, "Андрій: бюджет")

    stats = dedup_audio.mark_duplicates(db, dry_run=True)

    assert stats["groups"] == 1
    assert stats["duplicates"] == 1
    assert stats["marked"] == 0
    assert stats["pending_marks"] == 1
    assert _dup_of(db, second) is None
    assert _hash_of(db, first) is None      # хеші теж не дописані


def test_mark_writes_duplicate_of_and_hashes(db):
    first = _add(db, "Андрій: бюджет")
    second = _add(db, "Андрій: бюджет   \r\n")

    stats = dedup_audio.mark_duplicates(db, dry_run=False)

    assert stats["marked"] == 1
    assert _dup_of(db, second) == first
    assert _dup_of(db, first) is None       # оригінал лишається чистим
    assert _hash_of(db, first) == _hash_of(db, second) is not None


def test_mark_is_idempotent(db):
    _add(db, "Андрій: бюджет")
    _add(db, "Андрій: бюджет")
    dedup_audio.mark_duplicates(db, dry_run=False)

    again = dedup_audio.mark_duplicates(db, dry_run=False)

    assert again["groups"] == 1             # група нікуди не зникла
    assert again["marked"] == 0             # але писати вже нічого
    assert again["pending_hashes"] == 0


def test_mark_picks_smallest_id_as_original(db):
    ids = [_add(db, "Андрій: бюджет") for _ in range(3)]
    dedup_audio.mark_duplicates(db, dry_run=False)
    assert [_dup_of(db, i) for i in ids] == [None, ids[0], ids[0]]


def test_mark_leaves_unique_records_alone(db):
    a = _add(db, "перший дзвінок")
    b = _add(db, "другий дзвінок")
    stats = dedup_audio.mark_duplicates(db, dry_run=False)
    assert stats["groups"] == 0
    assert _dup_of(db, a) is None and _dup_of(db, b) is None


def test_mark_does_not_touch_telegram(db):
    _add(db, "той самий текст", source_type="telegram")
    _add(db, "той самий текст", source_type="telegram")
    stats = dedup_audio.mark_duplicates(db, dry_run=False)
    assert stats["groups"] == 0


def test_mark_groups_across_audio_source_types(db):
    """Той самий дзвінок, залитий файлом і як YouTube, — одна група."""
    first = _add(db, "Андрій: бюджет", source_type="file")
    second = _add(db, "Андрій: бюджет", source_type="youtube")
    dedup_audio.mark_duplicates(db, dry_run=False)
    assert _dup_of(db, second) == first


def test_mark_ignores_soft_deleted(db):
    _add(db, "Андрій: бюджет")
    _add(db, "Андрій: бюджет", deleted_at="2026-09-01 10:00:00")
    stats = dedup_audio.mark_duplicates(db, dry_run=False)
    assert stats["groups"] == 0


# ============================================================
# CLI
# ============================================================

def test_cli_dry_run_after_subcommand(db, capsys):
    first = _add(db, "Андрій: бюджет")
    second = _add(db, "Андрій: бюджет")

    assert dedup_audio.main(["--db", db, "mark", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert f"оригінал #{first}" in out
    assert f"дубль #{second}" in out
    assert _dup_of(db, second) is None


def test_cli_dry_run_before_subcommand(db, capsys):
    """--dry-run рятує з будь-якого місця рядка (урок test-log-isolation-03)."""
    _add(db, "Андрій: бюджет")
    second = _add(db, "Андрій: бюджет")

    assert dedup_audio.main(["--db", db, "--dry-run", "mark"]) == 0

    capsys.readouterr()
    assert _dup_of(db, second) is None


def test_cli_without_dry_run_writes(db, capsys):
    first = _add(db, "Андрій: бюджет")
    second = _add(db, "Андрій: бюджет")

    assert dedup_audio.main(["--db", db, "mark"]) == 0

    capsys.readouterr()
    assert _dup_of(db, second) == first


# ============================================================
# POST /api/transcribe (library-шлях) — ендпойнт-рівень (ремонт 4, review #7)
# ============================================================
#
# Піднімаємо лише transcription_bp на порожній тимчасовій БД (patern
# tests/test_audio_library_category.py) — жодного дотику до бойового
# whisper_history.db. Whisper підмінюється фейком через app.state
# (memory `mcp-stdio-no-heavy-models`: важке в тестах не імпортуємо).

class _FakeWhisperManager:
    """Повертає той самий текст на кожен виклик — саме так ловиться дубль."""

    def __init__(self, text):
        self._text = text

    def transcribe_with_progress(self, audio_path, model_name="base",
                                  language="uk", task="transcribe",
                                  progress_callback=None):
        return {"text": self._text, "language": "uk", "segments": []}


@pytest.fixture
def endpoint_client(tmp_path, monkeypatch):
    """transcription_bp на тимчасовій БД + фейковий whisper, job_queue=None."""
    from flask import Flask

    from app import state
    from app.blueprints.transcription import transcription_bp
    from app.services.metrics import MetricsRegistry

    db_path = str(tmp_path / "endpoint.db")
    init_database(db_path)

    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db_path

    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"not-really-audio")

    monkeypatch.setattr(state, "whisper_manager",
                         _FakeWhisperManager("Андрій: бюджет Барселони"))
    monkeypatch.setattr(state, "metrics", MetricsRegistry())
    monkeypatch.setattr(state, "job_queue", None)
    monkeypatch.setattr(state, "active_library_transcriptions", None)
    monkeypatch.setattr(state, "recording_service", None)
    monkeypatch.setattr(state, "copilot_service", None)
    monkeypatch.setattr(state, "sse_broker", None)

    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, "
            "source_type) VALUES (?, ?, ?, ?, 'file')",
            ("file://local", "local_1", "Дзвінок з Андрієм", str(audio_path)),
        )
        audio_download_id = cur.lastrowid
        conn.commit()

    return app.test_client(), db_path, audio_download_id


def _post_transcribe(client, audio_download_id):
    return client.post("/api/transcribe", data={
        "source_type": "library",
        "audio_download_id": str(audio_download_id),
    })


def _row(db_path, tid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, content_hash, duplicate_of FROM transcriptions WHERE id = ?",
        (tid,),
    ).fetchone()
    conn.close()
    return row


def _chunk_count(db_path, tid):
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE transcription_id = ?", (tid,),
    ).fetchone()[0]
    conn.close()
    return count


def test_endpoint_second_ingest_is_marked_duplicate(endpoint_client):
    client, db_path, audio_download_id = endpoint_client

    r1 = _post_transcribe(client, audio_download_id)
    assert r1.status_code == 200
    data1 = r1.get_json()
    assert data1.get("success", True) is not False
    id1 = data1["transcription_id"]

    r2 = _post_transcribe(client, audio_download_id)
    assert r2.status_code == 200
    data2 = r2.get_json()
    id2 = data2["transcription_id"]

    assert id1 != id2

    row1 = _row(db_path, id1)
    assert row1["content_hash"]
    assert row1["duplicate_of"] is None

    row2 = _row(db_path, id2)
    assert row2["duplicate_of"] == id1
    assert row2["content_hash"] == row1["content_hash"]

    assert data2["enrichment"]["status"] == "skipped_duplicate"
    assert _chunk_count(db_path, id2) == 0


def test_endpoint_dedup_ignores_whitespace(endpoint_client, monkeypatch):
    """Пробіли/переноси навколо й усередині тексту не заважають дедупу.

    Регістр НЕ входить у цю перевірку: `_normalize_text` (document_parser.py)
    не робить casefold — див. `## Findings` цієї історії.
    """
    client, db_path, audio_download_id = endpoint_client
    from app import state

    r1 = _post_transcribe(client, audio_download_id)
    id1 = r1.get_json()["transcription_id"]

    monkeypatch.setattr(
        state, "whisper_manager",
        _FakeWhisperManager("  Андрій: бюджет Барселони  \r\n\n"),
    )
    r2 = _post_transcribe(client, audio_download_id)
    data2 = r2.get_json()
    id2 = data2["transcription_id"]

    row2 = _row(db_path, id2)
    assert row2["duplicate_of"] == id1
    assert data2["enrichment"]["status"] == "skipped_duplicate"


class _SpyJobQueue:
    """Лічильник submit'ів: сам job НЕ виконується (черга підмінена цілком)."""

    def __init__(self):
        self.submitted = []

    def submit(self, kind, fn, meta=None):
        self.submitted.append((kind, meta))
        return len(self.submitted)


def test_endpoint_duplicate_does_not_submit_enrichment_job(endpoint_client,
                                                           monkeypatch):
    """Для дубля enrichment-job не ставиться — при тому що для оригіналу ставиться.

    Контрольне плече (перший запис) обовʼязкове: без нього перевірка була б
    вакуумною — з `job_queue=None` черга порожня для будь-якого запису.
    """
    client, db_path, audio_download_id = endpoint_client

    from app import state
    from app.services import enrichment

    spy = _SpyJobQueue()
    monkeypatch.setattr(state, "job_queue", spy)
    monkeypatch.setattr(enrichment, "any_available", lambda: True)

    r1 = _post_transcribe(client, audio_download_id)
    id1 = r1.get_json()["transcription_id"]
    assert [m.get("transcription_id") for _, m in spy.submitted] == [id1], (
        "оригінал мусить отримати enrichment-job"
    )

    r2 = _post_transcribe(client, audio_download_id)
    data2 = r2.get_json()
    id2 = data2["transcription_id"]

    assert _row(db_path, id2)["duplicate_of"] == id1
    assert data2["enrichment"]["status"] == "skipped_duplicate"
    assert [m.get("transcription_id") for _, m in spy.submitted] == [id1], (
        f"для дубля #{id2} enrichment-job не мав ставитись"
    )
