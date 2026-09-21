"""Назва й опис на входах створення запису + PATCH Аудіотеки + display_name у читачів.

Спека `editable-title-description`, історія 02. Перевіряється кожен шлях, яким
у проєкті народжується рядок архіву або картка Аудіотеки:

* `POST /api/transcribe` (file + library) — whisper підмінений фейком;
* `POST /api/documents/upload` — парсер підмінений фейком;
* `POST /api/recording/<sid>/save` → manifest → `register_recording()` — без заліза;
* `PATCH /api/audio/downloads/<id>` — контракт C3;
* експорти й `target_name` коментаря — читачі, яким тепер потрібен `display_name`.

Бойовий `whisper_history.db` не відкривається: кожна фікстура піднімає свою
тимчасову БД через `init_database`. Фікстури кириличні — саме на кирилиці цей
проєкт уже ловив баги порівняння (памʼятка `plan-dictated-sql-ignored-own-memory`).
"""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from app import state
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.metrics import MetricsRegistry


TITLE = "Планірка з Андрієм"
DESCRIPTION = "Домовились про дедлайни на Q3 — переслухати з 12-ї хвилини."


# ============================================================
# Спільне
# ============================================================

def _tx_row(db_path: str, tid: int):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM transcriptions WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()


def _audio_row(db_path: str, aid: int):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM audio_downloads WHERE id = ?", (aid,)).fetchone()
    finally:
        conn.close()


class _FakeWhisper:
    """Мінімальний контракт whisper_manager для transcribe(). Лічильник
    викликів потрібен там, де 400 мусить прилетіти ДО транскрипції."""

    def __init__(self):
        self.calls = 0

    def transcribe_with_progress(self, audio_path, model_name="base",
                                 language="uk", task="transcribe",
                                 progress_callback=None):
        self.calls += 1
        return {"text": "Андрій: бюджет Барселони", "language": "uk", "segments": []}


@pytest.fixture
def tx_env(tmp_path: Path, monkeypatch):
    """transcription_bp на тимчасовій БД + фейковий whisper."""
    from app.blueprints.transcription import transcription_bp

    db_path = str(tmp_path / "create.db")
    init_database(db_path)

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()

    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db_path
    app.config["UPLOAD_FOLDER"] = str(uploads)
    app.config["TRANSCRIPTS_FOLDER"] = str(transcripts)

    whisper = _FakeWhisper()
    monkeypatch.setattr(state, "whisper_manager", whisper)
    monkeypatch.setattr(state, "metrics", MetricsRegistry())
    monkeypatch.setattr(state, "job_queue", None)
    monkeypatch.setattr(state, "active_library_transcriptions", None)
    monkeypatch.setattr(state, "recording_service", None)
    monkeypatch.setattr(state, "copilot_service", None)
    monkeypatch.setattr(state, "sse_broker", None)

    return app.test_client(), db_path, whisper, tmp_path


def _post_file(client, **extra):
    data = {
        "source_type": "file",
        "audio": (io.BytesIO(b"not-really-audio"), "дзвінок.mp3"),
    }
    data.update(extra)
    return client.post("/api/transcribe", data=data,
                       content_type="multipart/form-data")


# ============================================================
# 1. POST /api/transcribe — файл
# ============================================================

class TestTranscribeFile:
    def test_title_and_description_saved_verbatim(self, tx_env):
        client, db_path, whisper, _ = tx_env
        r = _post_file(client, title=TITLE, description=DESCRIPTION)
        assert r.status_code == 200, r.get_data(as_text=True)
        tid = r.get_json()["transcription_id"]

        row = _tx_row(db_path, tid)
        # Побайтово: кирилиця не перекодована й не обрізана.
        assert row["title"] == TITLE
        assert row["description"] == DESCRIPTION
        # source_name — провенанс, назва його не чіпає.
        assert row["source_name"] == "дзвінок.mp3"
        assert whisper.calls == 1

    def test_without_fields_both_null(self, tx_env):
        client, db_path, _, _ = tx_env
        r = _post_file(client)
        assert r.status_code == 200
        row = _tx_row(db_path, r.get_json()["transcription_id"])
        assert row["title"] is None
        assert row["description"] is None

    def test_blank_title_is_null_not_filename(self, tx_env):
        client, db_path, _, _ = tx_env
        r = _post_file(client, title="   ")
        assert r.status_code == 200
        row = _tx_row(db_path, r.get_json()["transcription_id"])
        assert row["title"] is None

    def test_too_long_title_rejected_before_transcription(self, tx_env):
        client, db_path, whisper, _ = tx_env
        r = _post_file(client, title="я" * 201)
        assert r.status_code == 400
        assert r.get_json()["success"] is False
        # Головне: whisper не запускався — 400 мусить прилетіти до роботи.
        assert whisper.calls == 0
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0] == 0
        conn.close()

    def test_too_long_description_rejected(self, tx_env):
        client, _, whisper, _ = tx_env
        r = _post_file(client, description="о" * 4001)
        assert r.status_code == 400
        assert whisper.calls == 0


# ============================================================
# 2. POST /api/documents/upload
# ============================================================

@pytest.fixture
def doc_env(tmp_path: Path, monkeypatch):
    from app.blueprints.documents import documents_bp
    from app.services import document_parser

    db_path = str(tmp_path / "docs.db")
    init_database(db_path)
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir()

    app = Flask(__name__)
    app.register_blueprint(documents_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db_path
    app.config["DOCUMENTS_FOLDER"] = str(docs_dir)

    calls = {"n": 0}

    def _fake_parse(path, filename=None):
        calls["n"] += 1
        return {
            "text": "Договір про дедлайни",
            "doc_type": "md",
            "page_count": 1,
            "byte_size": 42,
            "content_hash": f"hash-{calls['n']}",
            "char_count": 20,
            "parser_version": "test",
            "blocks": None,
            "meta": {},
        }

    monkeypatch.setattr(document_parser, "parse_document", _fake_parse)
    monkeypatch.setattr(state, "metrics", MetricsRegistry())
    monkeypatch.setattr(state, "job_queue", None)
    return app.test_client(), db_path, calls


def _post_doc(client, **extra):
    data = {"document": (io.BytesIO(b"# note"), "нотатка.md")}
    data.update(extra)
    return client.post("/api/documents/upload", data=data,
                       content_type="multipart/form-data")


class TestDocumentUpload:
    def test_title_and_description_saved(self, doc_env):
        client, db_path, _ = doc_env
        r = _post_doc(client, title=TITLE, description=DESCRIPTION)
        assert r.status_code == 200, r.get_data(as_text=True)
        row = _tx_row(db_path, r.get_json()["transcription_id"])
        assert row["source_type"] == "document"
        assert row["title"] == TITLE
        assert row["description"] == DESCRIPTION
        assert row["source_name"] == "нотатка.md"

    def test_without_fields_both_null(self, doc_env):
        client, db_path, _ = doc_env
        r = _post_doc(client)
        assert r.status_code == 200
        row = _tx_row(db_path, r.get_json()["transcription_id"])
        assert row["title"] is None
        assert row["description"] is None

    def test_too_long_title_rejected_before_parsing(self, doc_env):
        client, db_path, calls = doc_env
        r = _post_doc(client, title="я" * 201)
        assert r.status_code == 400
        assert calls["n"] == 0
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0] == 0
        conn.close()


# ============================================================
# 3. source_type=library — успадкування від Аудіотеки
# ============================================================

def _insert_audio(db_path: str, audio_file: Path, **overrides) -> int:
    fields = {
        "youtube_url": "file://local",
        "youtube_id": "local_1",
        "title": TITLE,
        "description": DESCRIPTION,
        "file_path": str(audio_file),
        "source_type": "file",
    }
    fields.update(overrides)
    cols = ", ".join(fields)
    ph = ", ".join("?" * len(fields))
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO audio_downloads ({cols}) VALUES ({ph})",
            list(fields.values()))
        conn.commit()
        return cur.lastrowid


class TestLibraryInheritance:
    def test_inherits_title_and_description(self, tx_env):
        client, db_path, _, tmp_path = tx_env
        audio = tmp_path / "lib.mp3"
        audio.write_bytes(b"fake")
        aid = _insert_audio(db_path, audio)

        r = client.post("/api/transcribe", data={
            "source_type": "library", "audio_download_id": str(aid)})
        assert r.status_code == 200, r.get_data(as_text=True)
        row = _tx_row(db_path, r.get_json()["transcription_id"])
        assert row["title"] == TITLE
        assert row["description"] == DESCRIPTION

    def test_form_title_overrides_library_copy(self, tx_env):
        client, db_path, _, tmp_path = tx_env
        audio = tmp_path / "lib2.mp3"
        audio.write_bytes(b"fake")
        aid = _insert_audio(db_path, audio, youtube_id="local_2")

        r = client.post("/api/transcribe", data={
            "source_type": "library", "audio_download_id": str(aid),
            "title": "Своя назва запису"})
        assert r.status_code == 200
        row = _tx_row(db_path, r.get_json()["transcription_id"])
        assert row["title"] == "Своя назва запису"
        # Опис у формі не передали — копія з Аудіотеки лишається.
        assert row["description"] == DESCRIPTION

    def test_library_row_untouched_after_transcribe(self, tx_env):
        """Без автосинхронізації: два рядки живуть окремо (гриль, відповідь 3)."""
        client, db_path, _, tmp_path = tx_env
        audio = tmp_path / "lib3.mp3"
        audio.write_bytes(b"fake")
        aid = _insert_audio(db_path, audio, youtube_id="local_3")

        client.post("/api/transcribe", data={
            "source_type": "library", "audio_download_id": str(aid),
            "title": "Інша назва"})
        assert _audio_row(db_path, aid)["title"] == TITLE


# ============================================================
# 4. Стоп-екран рекордера → manifest → Аудіотека
# ============================================================

@pytest.fixture
def rec_env(tmp_path: Path, monkeypatch):
    from app.blueprints.recording import recording_bp
    from app.services.recording.session_store import STATUS_FINALIZED, SessionStore

    db_path = str(tmp_path / "rec.db")
    init_database(db_path)

    store = SessionStore(tmp_path / "sessions")
    sid = "rec_testsession01"
    store.create(sid, 48000, 2, auto_name="Запис 2026-09-21 10:00")

    final_mp3 = tmp_path / "final.mp3"
    final_mp3.write_bytes(b"x" * 128)

    def _finalize(mf: dict) -> None:
        mf["status"] = STATUS_FINALIZED
        mf["final_mp3_path"] = str(final_mp3)
        mf["total_duration_sec"] = 12.5
    store.modify(sid, _finalize)

    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db_path

    class _Service:
        pass
    svc = _Service()
    svc.store = store

    monkeypatch.setattr(state, "recording_service", svc)
    monkeypatch.setattr(state, "live_transcribe_worker", None)
    monkeypatch.setattr(state, "copilot_service", None)
    monkeypatch.setattr(state, "copilot_worker", None)

    return app.test_client(), db_path, store, sid


class TestRecordingSave:
    def test_description_lands_in_manifest_and_library(self, rec_env):
        client, db_path, store, sid = rec_env
        r = client.post(f"/api/recording/{sid}/save",
                        json={"name": "Дзвінок із Андрієм", "description": DESCRIPTION})
        assert r.status_code == 200, r.get_data(as_text=True)

        assert store.read(sid)["description"] == DESCRIPTION

        aid = r.get_json()["download_id"]
        row = _audio_row(db_path, aid)
        assert row["title"] == "Дзвінок із Андрієм"
        assert row["description"] == DESCRIPTION

    def test_without_description_stays_null(self, rec_env):
        client, db_path, store, sid = rec_env
        r = client.post(f"/api/recording/{sid}/save", json={"name": "Без опису"})
        assert r.status_code == 200
        assert _audio_row(db_path, r.get_json()["download_id"])["description"] is None

    def test_too_long_description_rejected(self, rec_env):
        client, db_path, store, sid = rec_env
        r = client.post(f"/api/recording/{sid}/save",
                        json={"name": "Х", "description": "о" * 4001})
        assert r.status_code == 400
        assert "description" not in store.read(sid)
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM audio_downloads").fetchone()[0] == 0
        conn.close()

    def test_second_save_updates_existing_description(self, rec_env):
        """Ідемпотентність: авто-реєстрація при finalize вже могла вставити
        рядок — ручний save мусить дописати в нього опис, а не створити другий."""
        client, db_path, store, sid = rec_env
        first = client.post(f"/api/recording/{sid}/save", json={"name": "Перша"})
        assert first.status_code == 200
        second = client.post(f"/api/recording/{sid}/save",
                             json={"name": "Перша", "description": DESCRIPTION})
        assert second.status_code == 200
        assert second.get_json()["download_id"] == first.get_json()["download_id"]
        assert _audio_row(db_path, first.get_json()["download_id"])["description"] == DESCRIPTION


# ============================================================
# 5. PATCH /api/audio/downloads/<id> (контракт C3)
# ============================================================

@pytest.fixture
def audio_env(tmp_path: Path, monkeypatch):
    from app.blueprints.audio_library import audio_bp

    db_path = str(tmp_path / "audio.db")
    init_database(db_path)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"fake")
    aid = _insert_audio(db_path, audio, title="Стара назва", description="Старий опис")

    app = Flask(__name__)
    app.register_blueprint(audio_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db_path
    monkeypatch.setattr(state, "metrics", MetricsRegistry())
    return app.test_client(), db_path, aid


class TestAudioLibraryPatch:
    def test_rename(self, audio_env):
        client, db_path, aid = audio_env
        r = client.patch(f"/api/audio/downloads/{aid}",
                         json={"title": "Лекція про дедлайни"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["success"] is True
        assert body["item"]["title"] == "Лекція про дедлайни"
        assert _audio_row(db_path, aid)["title"] == "Лекція про дедлайни"

    def test_empty_title_is_400(self, audio_env):
        client, db_path, aid = audio_env
        r = client.patch(f"/api/audio/downloads/{aid}", json={"title": "   "})
        assert r.status_code == 400
        assert _audio_row(db_path, aid)["title"] == "Стара назва"

    def test_description_null_clears(self, audio_env):
        client, db_path, aid = audio_env
        r = client.patch(f"/api/audio/downloads/{aid}", json={"description": None})
        assert r.status_code == 200
        assert _audio_row(db_path, aid)["description"] is None
        # Назва не зачеплена.
        assert _audio_row(db_path, aid)["title"] == "Стара назва"

    def test_empty_body_is_400(self, audio_env):
        client, _, aid = audio_env
        assert client.patch(f"/api/audio/downloads/{aid}", json={}).status_code == 400

    def test_missing_row_is_404(self, audio_env):
        client, _, _ = audio_env
        r = client.patch("/api/audio/downloads/999999", json={"title": "Нема"})
        assert r.status_code == 404

    def test_too_long_title_is_400_without_leaking_internals(self, audio_env):
        client, db_path, aid = audio_env
        r = client.patch(f"/api/audio/downloads/{aid}", json={"title": "я" * 201})
        assert r.status_code == 400
        err = r.get_json()["error"]
        # Помилка — доменна (контракт C2), не str() випадкового винятку:
        # ні шляхів, ні SQL, ні назв таблиць у тексті.
        assert "200" in err
        for leak in ("Traceback", "sqlite3", "audio_downloads", "SELECT", "\\"):
            assert leak not in err
        assert _audio_row(db_path, aid)["title"] == "Стара назва"


class TestAudioLibraryListDescription:
    """Ремонт раунду 1 (ask 1): GET список мусить віддавати description
    (раніше було write-only, PATCH -> GET -> PATCH тихо стирав опис)."""

    def _item(self, client, aid):
        r = client.get("/api/audio/downloads")
        assert r.status_code == 200
        items = {d["id"]: d for d in r.get_json()["downloads"]}
        return items[aid]

    def test_description_present_in_list(self, audio_env):
        client, _, aid = audio_env
        item = self._item(client, aid)
        assert item["description"] == "Старий опис"

    def test_missing_description_is_null_key_present(self, tmp_path, monkeypatch):
        from app.blueprints.audio_library import audio_bp

        db_path = str(tmp_path / "audio2.db")
        init_database(db_path)
        audio = tmp_path / "b.mp3"
        audio.write_bytes(b"fake")
        aid = _insert_audio(db_path, audio, youtube_id="local_nodesc", description=None)

        app = Flask(__name__)
        app.register_blueprint(audio_bp)
        app.config["TESTING"] = True
        app.config["DATABASE"] = db_path
        monkeypatch.setattr(state, "metrics", MetricsRegistry())
        client = app.test_client()

        item = self._item(client, aid)
        assert "description" in item
        assert item["description"] is None

    def test_patch_get_patch_does_not_lose_description(self, audio_env):
        client, db_path, aid = audio_env
        new_description = "Умови кредиту, ставка 18%"
        r = client.patch(f"/api/audio/downloads/{aid}", json={
            "title": "Дзвінок із банком", "description": new_description,
        })
        assert r.status_code == 200

        item = self._item(client, aid)
        assert item["title"] == "Дзвінок із банком"
        assert item["description"] == new_description

        # Клієнт переклопачує ці самі значення в наступний PATCH (як робить
        # модалка audio.js, префілена з рядка списку).
        r2 = client.patch(f"/api/audio/downloads/{aid}", json={
            "title": item["title"], "description": item["description"],
        })
        assert r2.status_code == 200
        assert _audio_row(db_path, aid)["description"] == new_description

    def test_patch_null_then_list_shows_null(self, audio_env):
        client, _, aid = audio_env
        r = client.patch(f"/api/audio/downloads/{aid}", json={"description": None})
        assert r.status_code == 200
        item = self._item(client, aid)
        assert item["description"] is None

    def test_multiline_description_survives_list_roundtrip(self, audio_env):
        """Гейт-9 (знахідка 6): жоден тест у цьому класі досі не ніс `\\n` —
        PATCH з переносом рядка -> GET список мусить віддати рядок побайтово,
        а не з'їденим/екранованим переносом."""
        client, db_path, aid = audio_env
        new_description = "Умови кредиту\nставка 18%"
        r = client.patch(f"/api/audio/downloads/{aid}", json={
            "description": new_description,
        })
        assert r.status_code == 200

        item = self._item(client, aid)
        assert item["description"] == new_description
        assert "\n" in item["description"]
        assert _audio_row(db_path, aid)["description"] == new_description


# ============================================================
# 6. Читачі: експорт і target_name коментаря
# ============================================================

def _seed_transcript(db_path: str, *, title=None) -> int:
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "title) VALUES ('file', ?, ?, ?)",
            ("дзвінок_2026_09_21.mp3", "Андрій: бюджет Барселони", title))
        conn.commit()
        return cur.lastrowid


class TestExportShowsDisplayName:
    """`display_name` = title → source_name → «Запис #id». У контенті експорту
    має стояти саме він; `source_name` у JSON-пейлоаді лишається провенансом."""

    def _export(self, client, db_path, fmt, tid):
        return client.post(f"/api/export/{fmt}", json={
            "id": tid,
            "title": TITLE,
            "source_name": "дзвінок_2026_09_21.mp3",
            "text": "Андрій: бюджет Барселони",
            "segments": [{"start": 0.0, "end": 2.0, "text": "Андрій: бюджет Барселони"}],
            "language": "uk",
        })

    def test_md_heading_is_display_name(self, tx_env):
        client, db_path, _, _ = tx_env
        tid = _seed_transcript(db_path, title=TITLE)
        r = self._export(client, db_path, "md", tid)
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert body.startswith(f"# {TITLE}")

    def test_json_adds_display_name_and_keeps_source_name(self, tx_env):
        client, db_path, _, _ = tx_env
        tid = _seed_transcript(db_path, title=TITLE)
        r = self._export(client, db_path, "json", tid)
        assert r.status_code == 200
        payload = json.loads(r.get_data(as_text=True))
        assert payload["display_name"] == TITLE
        assert payload["source_name"] == "дзвінок_2026_09_21.mp3"

    def test_txt_filename_uses_display_name(self, tx_env):
        client, db_path, _, _ = tx_env
        tid = _seed_transcript(db_path, title=TITLE)
        r = self._export(client, db_path, "txt", tid)
        assert r.status_code == 200
        # Імʼя згенерованого файлу теж іде від display_name, а не source_name.
        # Werkzeug віддає кирилицю в RFC 5987-формі (filename*=UTF-8''%D0%9F…),
        # тож порівнюємо після unquote, а не з сирим заголовком.
        from urllib.parse import unquote
        disposition = unquote(r.headers.get("Content-Disposition", ""))
        assert "Планірка-з-Андрієм" in disposition
        assert "2026_09_21" not in disposition

    def test_without_title_falls_back_to_source_name(self, tx_env):
        client, db_path, _, _ = tx_env
        tid = _seed_transcript(db_path)
        r = client.post("/api/export/md", json={
            "id": tid, "source_name": "дзвінок_2026_09_21.mp3",
            "text": "текст", "segments": [], "language": "uk"})
        assert r.status_code == 200
        assert r.get_data(as_text=True).startswith("# дзвінок_2026_09_21.mp3")


class TestCommentTargetName:
    def test_target_name_prefers_title(self, tmp_path):
        from app.services import comments as comments_svc

        db_path = str(tmp_path / "comments.db")
        init_database(db_path)
        tid = _seed_transcript(db_path, title=TITLE)
        comments_svc.create(db_path, "transcription", tid, "Переслухати з 12-ї")

        feed = comments_svc.list_recent(db_path)
        assert feed["comments"][0]["target_name"] == TITLE

    def test_target_name_falls_back_to_source_name(self, tmp_path):
        from app.services import comments as comments_svc

        db_path = str(tmp_path / "comments2.db")
        init_database(db_path)
        tid = _seed_transcript(db_path)
        comments_svc.create(db_path, "transcription", tid, "Без назви")

        feed = comments_svc.list_recent(db_path)
        assert feed["comments"][0]["target_name"] == "дзвінок_2026_09_21.mp3"
