"""One record title in the Library, the Media library and the session (spec recording-meta-suggest, story 02)."""
from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from app import state
from app.blueprints.audio_library import audio_bp
from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database
from app.services import reembed

SID = "rec_7aba448a2c394b79"
MP3 = r"E:\Projects\Recall\recordings\sessions\rec_7aba448a2c394b79\final.mp3"


class FakeStore:
    def __init__(self, fail=False):
        self.names: dict = {}
        self.descriptions: dict = {}
        self.fail = fail

    def read(self, session_id):
        if self.fail:
            raise RuntimeError("manifest vanished")
        return {"auto_name": "Запис 2026-10-14 08:54"}

    def set_name(self, session_id, name):
        if self.fail:
            raise RuntimeError("manifest vanished")
        self.names[session_id] = name
        return {}

    def set_description(self, session_id, description):
        if self.fail:
            raise RuntimeError("manifest vanished")
        self.descriptions[session_id] = description
        return {}


class FakeService:
    def __init__(self, store):
        self.store = store

    def active_session_ids(self):
        return []


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "sync.db")
    init_database(path)
    return path


@pytest.fixture
def reembeds(monkeypatch):
    calls: list = []
    monkeypatch.setattr(reembed, "schedule_record_reembed",
                        lambda tid, **kw: calls.append(tid) or "queued")
    return calls


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(state, "recording_service", FakeService(s), raising=False)
    return s


@pytest.fixture
def client(db, tmp_path):
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.register_blueprint(audio_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db
    (tmp_path / "transcripts").mkdir(exist_ok=True)
    app.config["TRANSCRIPTS_FOLDER"] = str(tmp_path / "transcripts")
    return app.test_client()


def _recording(db, sid=SID, path=MP3):
    conn = sqlite3.connect(db)
    aid = conn.execute(
        "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type, "
        "recording_session_id) VALUES ('', ?, 'Запис 2026-10-14 08:54', ?, 'recording', ?)",
        (f"recording_{sid}", path, sid)).lastrowid
    tid = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, file_path, transcript_text) "
        "VALUES ('recording', 'Запис 2026-10-14 08:54', ?, 'Так, Серёх, по часовому поясу')",
        (path,)).lastrowid
    conn.commit()
    conn.close()
    return aid, tid


def _youtube(db):
    path = r"E:\Projects\Recall\downloads\Барселона.mp3"
    conn = sqlite3.connect(db)
    aid = conn.execute(
        "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type) "
        "VALUES ('https://youtu.be/abc123', 'abc123', 'Барселона — огляд', ?, 'youtube')", (path,)).lastrowid
    tid = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, file_path, transcript_text) "
        "VALUES ('youtube', 'Барселона — огляд', ?, 'Огляд міста')", (path,)).lastrowid
    conn.commit()
    conn.close()
    return aid, tid


def _q(db, sql, *args):
    conn = sqlite3.connect(db)
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


def test_library_patch_reaches_audio_and_manifest(client, db, store, reembeds):
    aid, tid = _recording(db)
    r = client.patch(f"/api/history/{tid}",
                     json={"title": "1С з Сергієм: часовий пояс", "description": "Розібрали права."})
    assert r.status_code == 200
    assert _q(db, "SELECT title, description FROM audio_downloads WHERE id=?", aid) == \
        ("1С з Сергієм: часовий пояс", "Розібрали права.")
    assert store.names[SID] == "1С з Сергієм: часовий пояс"
    assert store.descriptions[SID] == "Розібрали права."
    assert reembeds == [tid]


def test_audio_patch_reaches_transcription_and_manifest(client, db, store, reembeds):
    aid, tid = _recording(db)
    r = client.patch(f"/api/audio/downloads/{aid}",
                     json={"title": "Супергриль із Сергієм", "description": "Уровни лёгкий/супер."})
    assert r.status_code == 200
    assert _q(db, "SELECT title, description FROM transcriptions WHERE id=?", tid) == \
        ("Супергриль із Сергієм", "Уровни лёгкий/супер.")
    assert store.names[SID] == "Супергриль із Сергієм"
    assert reembeds == [tid]


def test_clearing_library_title_restores_auto_name_in_audio(client, db, store, reembeds):
    aid, tid = _recording(db)
    client.patch(f"/api/history/{tid}", json={"title": "Тимчасова"})
    client.patch(f"/api/history/{tid}", json={"title": ""})
    assert _q(db, "SELECT title FROM audio_downloads WHERE id=?", aid)[0] == "Запис 2026-10-14 08:54"
    assert _q(db, "SELECT title FROM transcriptions WHERE id=?", tid)[0] is None
    assert store.names[SID] is None


def test_clearing_title_without_manifest_keeps_audio_title(client, db, monkeypatch, reembeds):
    monkeypatch.setattr(state, "recording_service", FakeService(FakeStore(fail=True)),
                        raising=False)
    aid, tid = _recording(db)
    client.patch(f"/api/history/{tid}", json={"title": "Тимчасова"})
    assert client.patch(f"/api/history/{tid}", json={"title": ""}).status_code == 200
    assert _q(db, "SELECT title FROM audio_downloads WHERE id=?", aid)[0] == "Тимчасова"


def test_media_list_ignores_soft_deleted_transcript(client, db, store, reembeds):
    aid, tid = _recording(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    body = client.get("/api/audio/downloads?source_type=recording").get_json()
    item = next(d for d in body["downloads"] if d["id"] == aid)
    assert item["transcription_id"] is None


def test_only_passed_fields_travel(client, db, store, reembeds):
    aid, tid = _recording(db)
    client.patch(f"/api/history/{tid}", json={"description": "Лише опис"})
    assert _q(db, "SELECT title, description FROM audio_downloads WHERE id=?", aid) == \
        ("Запис 2026-10-14 08:54", "Лише опис")
    assert SID not in store.names


def test_manifest_failure_does_not_break_patch(client, db, monkeypatch, reembeds):
    monkeypatch.setattr(state, "recording_service", FakeService(FakeStore(fail=True)),
                        raising=False)
    aid, tid = _recording(db)
    r = client.patch(f"/api/history/{tid}", json={"title": "Назва"})
    assert r.status_code == 200
    assert _q(db, "SELECT title FROM audio_downloads WHERE id=?", aid)[0] == "Назва"


def test_no_recording_service_still_syncs_db(client, db, monkeypatch, reembeds):
    monkeypatch.setattr(state, "recording_service", None, raising=False)
    aid, tid = _recording(db)
    assert client.patch(f"/api/audio/downloads/{aid}", json={"title": "Назва"}).status_code == 200
    assert _q(db, "SELECT title FROM transcriptions WHERE id=?", tid)[0] == "Назва"


def test_non_recording_untouched_both_ways(client, db, store, reembeds):
    aid, tid = _youtube(db)
    client.patch(f"/api/history/{tid}", json={"title": "Моя назва"})
    assert _q(db, "SELECT title FROM audio_downloads WHERE id=?", aid)[0] == "Барселона — огляд"
    client.patch(f"/api/audio/downloads/{aid}", json={"title": "Інша назва"})
    assert _q(db, "SELECT title FROM transcriptions WHERE id=?", tid)[0] == "Моя назва"
    assert store.names == {}


def test_audio_without_transcript(client, db, store, reembeds):
    conn = sqlite3.connect(db)
    aid = conn.execute(
        "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type, "
        "recording_session_id) VALUES ('', 'recording_rec_x', 'Запис 2026-10-14 11:00', 'x.mp3', "
        "'recording', 'rec_x')").lastrowid
    conn.commit()
    conn.close()
    assert client.patch(f"/api/audio/downloads/{aid}", json={"title": "Без транскрипту"}).status_code == 200
    assert store.names["rec_x"] == "Без транскрипту"
    assert reembeds == []
