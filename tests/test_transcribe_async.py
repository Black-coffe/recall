"""`POST /api/transcribe/async`: a `transcription` job per source, the recording guard, dedupe, path refusals.

Spec `mcp-pipeline`, story 01. Whisper, diarization and the YouTube download are faked at the
service seam; the job queue is real, driven by a manual executor so a test decides when a
queued job runs. Fixtures are Cyrillic (memory note `plan-dictated-sql-ignored-own-memory`).
"""
from __future__ import annotations

import os
import sqlite3
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from app import state
from app.core import auth
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import transcription_run
from app.services.job_queue import JobQueue
from app.services.metrics import MetricsRegistry
from app.services.recording.state import RECORDING_BLOCKED


TEXT = "Андрій: бюджет Барселони на третій квартал"
UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


class _ManualExecutor:
    """Collects submitted callables; ``run_all`` runs them in order in this thread."""

    def __init__(self):
        self.pending: list = []

    def submit(self, fn):
        fut = Future()
        self.pending.append((fn, fut))
        return fut

    def run_all(self):
        while self.pending:
            fn, fut = self.pending.pop(0)
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except Exception as e:  # noqa: BLE001 - the job queue already recorded it
                fut.set_exception(e)


class _Whisper:
    def __init__(self):
        self.calls: list = []
        self.on_call = None

    def get_available_models(self):
        return [{"name": "base", "downloaded": True}, {"name": "large-v3", "downloaded": True},
                {"name": "medium", "downloaded": False}]

    def transcribe_with_progress(self, audio_path, model_name="base", language="uk",
                                 progress_callback=None, **kwargs):
        self.calls.append(audio_path)
        if progress_callback:
            progress_callback({"progress": 0.5})
        if self.on_call:
            self.on_call()
        return {"text": TEXT, "language": "uk", "segments": []}


class _Store:
    def __init__(self):
        self.manifests: dict = {}

    def read(self, sid):
        return dict(self.manifests[sid])


class _Recorder:
    def __init__(self):
        self.store = _Store()
        self.active: list = []

    def active_session_ids(self):
        return list(self.active)


class _Progress(dict):
    def set(self, key, value):
        self[key] = value


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    from app.blueprints.transcription import transcription_bp
    from app.services import diarization_service as ds

    db_path = str(tmp_path / "async.db")
    init_database(db_path)
    uploads = tmp_path / "завантаження"
    uploads.mkdir()
    root = tmp_path / "імпорт"
    root.mkdir()
    monkeypatch.setenv("RECALL_IMPORT_ROOTS", str(root))

    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config.update(TESTING=True, DATABASE=db_path, UPLOAD_FOLDER=str(uploads),
                      MAX_CONTENT_LENGTH=1000)

    executor = _ManualExecutor()
    queue = JobQueue(executor, slow_executor=executor)
    whisper = _Whisper()
    recorder = _Recorder()
    logs: list = []
    for name, value in (
        ("whisper_manager", whisper), ("metrics", MetricsRegistry()), ("job_queue", queue),
        ("active_library_transcriptions", {}), ("recording_service", recorder),
        ("copilot_service", None), ("sse_broker", None), ("download_progress", _Progress()),
        ("add_log", lambda *a: logs.append(a)),
    ):
        monkeypatch.setattr(state, name, value)
    off = SimpleNamespace(is_available=lambda: False, unavailability_reason=lambda: "test")
    monkeypatch.setattr(ds.DiarizationService, "get_instance", classmethod(lambda cls: off))
    monkeypatch.setattr(transcription_run, "_inflight", {})

    return SimpleNamespace(client=app.test_client(), db=db_path, uploads=uploads, root=root,
                           tmp=tmp_path, executor=executor, queue=queue, whisper=whisper,
                           recorder=recorder, logs=logs)


def _library_row(env, name="Дзвінок з Андрієм.mp3") -> int:
    audio = env.tmp / name
    audio.write_bytes(b"not-really-audio")
    with get_db_connection(env.db) as conn:
        cur = conn.execute(
            "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type) "
            "VALUES (?, ?, ?, ?, 'file')",
            ("file://local", f"local_{name}", "Дзвінок з Андрієм", str(audio)))
        conn.commit()
        return cur.lastrowid


def _finalized_recording(env, sid="rec_планірка", status="finalized"):
    mp3 = env.tmp / f"{sid}.mp3"
    mp3.write_bytes(b"fake mp3")
    env.recorder.store.manifests[sid] = {"status": status, "final_mp3_path": str(mp3),
                                         "name": "Планірка"}
    return sid, mp3


def _post(env, body, headers=None):
    return env.client.post("/api/transcribe/async", json=body, headers=headers or {})


def _tx_count(env) -> int:
    conn = sqlite3.connect(env.db)
    try:
        return conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]
    finally:
        conn.close()


def _tx_row(env, tid):
    conn = sqlite3.connect(env.db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM transcriptions WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()


def _finish(env, job_id):
    env.executor.run_all()
    return env.queue.get(job_id)


# ============================================================ each source

class TestSources:
    def test_library_job_saves_and_shows_in_active_while_it_runs(self, env):
        aid = _library_row(env)
        r = _post(env, {"source": "library", "audio_download_id": aid})
        assert r.status_code == 202, r.get_data(as_text=True)
        body = r.get_json()
        assert body["created"] is True and body["success"] is True
        job_id = body["job_id"]

        queued = env.client.get("/api/transcribe/active").get_json()["active"]
        assert [(e["audio_download_id"], e["job_id"]) for e in queued] == [(aid, job_id)]

        seen = []
        env.whisper.on_call = lambda: seen.append(
            env.client.get("/api/transcribe/active").get_json()["active"])
        job = _finish(env, job_id)

        assert job.state == "completed", job.error
        assert seen and seen[0][0]["audio_download_id"] == aid
        assert env.client.get("/api/transcribe/active").get_json()["active"] == []
        tid = job.meta["transcription_id"]
        assert job._future.result()["transcription_id"] == tid
        assert _tx_row(env, tid)["transcript_text"] == TEXT
        assert job.meta["stage"] == "done" and job.meta["progress"] == 100

    def test_recording_job_transcribes_the_final_mp3(self, env):
        sid, mp3 = _finalized_recording(env)
        r = _post(env, {"source": "recording", "recording_session_id": sid})
        assert r.status_code == 202, r.get_data(as_text=True)
        job = _finish(env, r.get_json()["job_id"])
        assert job.state == "completed", job.error
        assert env.whisper.calls == [str(mp3)]
        row = _tx_row(env, job._future.result()["transcription_id"])
        assert row["source_type"] == "recording" and row["source_name"] == "Планірка"

    def test_youtube_job_downloads_then_transcribes(self, env, monkeypatch):
        from app.blueprints import youtube as yt
        audio = env.tmp / "ролик.mp3"
        audio.write_bytes(b"x")
        downloads = []

        def fake_core(url, download_id, save_to_library, quality, start, end):
            downloads.append(url)
            state.download_progress.set(download_id, {
                "status": "completed", "file_path": str(audio),
                "info": {"title": "Ролик про бюджет", "video_id": "dQw4w9WgXcQ",
                         "original_url": url}})

        monkeypatch.setattr(yt, "_download_youtube_core", fake_core)
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        r = _post(env, {"source": "youtube", "url": url})
        assert r.status_code == 202, r.get_data(as_text=True)
        job = _finish(env, r.get_json()["job_id"])
        assert job.state == "completed", job.error
        assert downloads == [url]
        row = _tx_row(env, job._future.result()["transcription_id"])
        assert row["source_type"] == "youtube" and row["youtube_title"] == "Ролик про бюджет"

    def test_path_job_copies_into_uploads_and_keeps_the_original(self, env):
        original = env.root / "дзвінок з фондом.mp3"
        original.write_bytes(b"audio-bytes")
        r = _post(env, {"source": "path", "path": str(original)})
        assert r.status_code == 202, r.get_data(as_text=True)
        copies = list(env.uploads.iterdir())
        assert len(copies) == 1, "the copy is made before the job starts"
        job = _finish(env, r.get_json()["job_id"])
        assert job.state == "completed", job.error
        assert original.read_bytes() == b"audio-bytes"
        assert env.whisper.calls == [str(copies[0])]
        row = _tx_row(env, job._future.result()["transcription_id"])
        assert row["source_name"] == "дзвінок з фондом.mp3" and row["source_type"] == "file"

    def test_stages_move_through_the_plan(self, env):
        aid = _library_row(env)
        job_id = _post(env, {"source": "library", "audio_download_id": aid}).get_json()["job_id"]
        job = env.queue.get(job_id)
        assert job.meta["stage"] == "queued"
        stages = []
        env.whisper.on_call = lambda: stages.append(job.meta["stage"])
        _finish(env, job_id)
        assert stages == ["transcribing"]
        assert job.meta["stage"] == "done"

    def test_actor_is_the_classified_one(self, env):
        aid = _library_row(env)
        ui = _post(env, {"source": "library", "audio_download_id": aid}, headers=UI).get_json()
        assert env.queue.get(ui["job_id"]).meta["actor"] == "ui"
        _finish(env, ui["job_id"])
        agent = _post(env, {"source": "library", "audio_download_id": aid},
                      headers={"X-Recall-Actor": "ui"}).get_json()
        assert env.queue.get(agent["job_id"]).meta["actor"] == "unknown"


# ============================================================ dedupe

def test_second_call_for_the_same_source_joins_the_job(env):
    aid = _library_row(env)
    first = _post(env, {"source": "library", "audio_download_id": aid})
    second = _post(env, {"source": "library", "audio_download_id": str(aid)})
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.get_json() == {"success": True, "job_id": first.get_json()["job_id"],
                                 "created": False}
    _finish(env, first.get_json()["job_id"])
    assert _tx_count(env) == 1
    third = _post(env, {"source": "library", "audio_download_id": aid})
    assert third.status_code == 202, "a finished job no longer dedupes"


# ============================================================ recording guard

class TestRecordingGuard:
    def test_non_ui_submit_during_recording_is_refused(self, env):
        aid = _library_row(env)
        env.recorder.active = ["rec_live"]
        r = _post(env, {"source": "library", "audio_download_id": aid})
        assert r.status_code == 409
        assert r.get_json()["error_code"] == "RECORDING_ACTIVE"
        assert env.queue.list() == []
        assert env.client.get("/api/transcribe/active").get_json()["active"] == []

    def test_recording_started_before_the_job_fails_it_without_a_row(self, env):
        original = env.root / "нарада.mp3"
        original.write_bytes(b"audio")
        r = _post(env, {"source": "path", "path": str(original)})
        assert r.status_code == 202
        env.recorder.active = ["rec_live"]
        job = _finish(env, r.get_json()["job_id"])
        assert job.state == "failed"
        assert job.error_code == "RECORDING_ACTIVE"
        assert _tx_count(env) == 0
        assert env.whisper.calls == []
        assert list(env.uploads.iterdir()) == [], "the unused import copy is removed"
        assert original.exists()

    def test_blocked_set_unchanged(self):
        assert "transcribe" in RECORDING_BLOCKED


# ============================================================ path refusals

class TestPathRefusals:
    def test_outside_the_roots_is_403(self, env):
        outside = env.tmp / "чужа тека"
        outside.mkdir()
        f = outside / "дзвінок.mp3"
        f.write_bytes(b"x")
        r = _post(env, {"source": "path", "path": str(f)})
        assert r.status_code == 403
        assert r.get_json()["error_code"] == "PATH_NOT_ALLOWED"

    def test_prefix_sibling_of_a_root_is_403(self, env):
        sibling = Path(str(env.root) + "-зле")
        sibling.mkdir()
        f = sibling / "дзвінок.mp3"
        f.write_bytes(b"x")
        r = _post(env, {"source": "path", "path": str(f)})
        assert r.status_code == 403
        assert r.get_json()["error_code"] == "PATH_NOT_ALLOWED"
        assert list(env.uploads.iterdir()) == []

    def test_unknown_extension_is_400(self, env):
        f = env.root / "нотатки.exe"
        f.write_bytes(b"x")
        r = _post(env, {"source": "path", "path": str(f)})
        assert r.status_code == 400
        assert r.get_json()["error_code"] == "UNSUPPORTED_EXTENSION"

    def test_file_above_the_limit_is_413(self, env):
        f = env.root / "довгий запис.mp3"
        f.write_bytes(b"x" * 2000)
        r = _post(env, {"source": "path", "path": str(f)})
        assert r.status_code == 413
        assert r.get_json()["error_code"] == "FILE_TOO_LARGE"
        assert list(env.uploads.iterdir()) == []
        assert env.queue.list() == []


# ============================================================ recording not finalized

def test_unfinalized_recording_is_409_with_session_id(env):
    sid, _ = _finalized_recording(env, sid="rec_ще_пише", status="recording")
    r = _post(env, {"source": "recording", "recording_session_id": sid})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error_code"] == "NOT_FINALIZED" and body["session_id"] == sid
    assert env.queue.list() == []


# ============================================================ meta_source

class TestMetaSource:
    def _run(self, env, headers):
        aid = _library_row(env, name=f"запис-{len(headers)}.mp3")
        r = _post(env, {"source": "library", "audio_download_id": aid,
                        "title": "Планірка з Андрієм", "description": "Дедлайни на Q3"},
                  headers=headers)
        assert r.status_code == 202, r.get_data(as_text=True)
        job = _finish(env, r.get_json()["job_id"])
        return _tx_row(env, job._future.result()["transcription_id"])

    def test_agent_title_is_marked_mcp(self, env):
        row = self._run(env, {})
        assert row["title"] == "Планірка з Андрієм"
        assert row["meta_source"] == "mcp"

    def test_ui_title_leaves_meta_source_null(self, env):
        row = self._run(env, UI)
        assert row["title"] == "Планірка з Андрієм"
        assert row["meta_source"] is None


# ============================================================ import roots helper

def test_import_roots_refuses_everything_when_unset(monkeypatch, tmp_path):
    from app.services import import_roots
    monkeypatch.delenv("RECALL_IMPORT_ROOTS", raising=False)
    assert import_roots.roots() == []
    with pytest.raises(import_roots.PathNotAllowed):
        import_roots.resolve_allowed(str(tmp_path / "дзвінок.mp3"))


def test_import_roots_accepts_a_file_inside(monkeypatch, tmp_path):
    from app.services import import_roots
    root = tmp_path / "архів"
    root.mkdir()
    monkeypatch.setenv("RECALL_IMPORT_ROOTS", f"{tmp_path / 'інша'};{root}")
    inside = root / "дзвінок.mp3"
    assert os.path.normcase(import_roots.resolve_allowed(str(inside))) == \
        os.path.normcase(os.path.realpath(inside))
