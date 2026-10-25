"""Record meta for agents, the deleted history filter, the job lookup and the eviction rule
(mcp-archive-org story 05). Temp DB, no gate; the actor is the `X-Recall-UI` token or nothing.
Fixtures are Cyrillic and invented.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from app import state as app_state
from app.blueprints.transcription import transcription_bp
from app.core import auth
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import job_queue as jq
from app.services import reembed, retranscribe
from app.services import transcript_versions as tv
from app.services.job_queue import JobQueue

TITLE = "Дзвінок з фондом"
OLD = "Добрий день, обговорюємо грант фонду"
UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


class FakeWhisper:
    def get_available_models(self):
        return [{"name": "large-v3", "downloaded": True}]

    def transcribe_with_progress(self, path, model, language, **kw):
        return {"text": "Нова версія", "language": language,
                "segments": [{"start": 0.0, "end": 2.0, "text": "Нова версія"}]}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "archive_org_tx.db")
    init_database(path)
    return path


@pytest.fixture
def queue(monkeypatch):
    q = JobQueue(executor=ThreadPoolExecutor(max_workers=2), slow_executor=ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(app_state, "job_queue", q, raising=False)
    return q


@pytest.fixture
def client(db, monkeypatch, queue):
    monkeypatch.setattr(app_state, "recording_service", None, raising=False)
    monkeypatch.setattr(reembed, "schedule_record_reembed", lambda tid, *, db_path: "queued")
    a = Flask(__name__)
    a.config.update(TESTING=True, DATABASE=db)
    a.register_blueprint(transcription_bp)
    return a.test_client()


def _record(db, title=TITLE, deleted=False, file_path=None):
    segs = json.dumps([{"start": 0, "end": 2, "text": OLD}], ensure_ascii=False)
    with get_db_connection(db) as c:
        tid = c.execute(
            "INSERT INTO transcriptions (source_type, source_name, title, file_path, transcript_text, segments, "
            "language, model_used, created_at, deleted_at) VALUES ('file', 'фонд.mp3', ?, ?, ?, ?, 'uk', 'small', "
            "'2026-01-01 10:00:00', ?)", (title, file_path, OLD, segs, time.time() if deleted else None)).lastrowid
        c.commit()
    return tid


def _versions(db, tid, specs, active=-1):
    """Insert version rows (model, created_at); point the record at ``specs[active]``."""
    segs = json.dumps([{"start": 0, "end": 2, "text": OLD}], ensure_ascii=False)
    with get_db_connection(db) as c:
        vids = [c.execute(
            "INSERT INTO transcript_versions (transcription_id, created_at, is_original, model_used, language, "
            "transcript_text, segments, speaker_map_json) VALUES (?, ?, ?, ?, 'uk', ?, ?, '[]')",
            (tid, created, 1 if i == 0 else 0, model, OLD, segs)).lastrowid
            for i, (model, created) in enumerate(specs)]
        c.execute("UPDATE transcriptions SET active_version_id = ? WHERE id = ?", (vids[active], tid))
        c.commit()
    return vids


def _row(db, tid):
    with get_db_connection(db) as c:
        return dict(c.execute("SELECT * FROM transcriptions WHERE id = ?", (tid,)).fetchone())


@pytest.fixture
def busy_job(queue):
    release = threading.Event()
    jobs = []

    def _start(tid, kind="reembed_point"):
        job = queue.submit(kind, lambda job: release.wait(10), meta={"transcription_id": tid})
        jobs.append(job)
        return job

    yield _start
    release.set()
    for job in jobs:
        job._future.result(timeout=10)


# ------------------------------------------------------------------ PATCH meta

def test_non_ui_meta_edit_marks_mcp_and_ui_edit_clears_it(client, db):
    tid = _record(db)
    r = client.patch(f"/api/history/{tid}", json={"title": "Дзвінок з фондом, підсумок"})
    assert r.status_code == 200, r.get_json()
    assert _row(db, tid)["meta_source"] == "mcp"
    r = client.patch(f"/api/history/{tid}", json={"description": "Грант на осінь"}, headers=UI)
    assert r.status_code == 200
    row = _row(db, tid)
    assert row["meta_source"] is None and row["description"] == "Грант на осінь"


def test_ui_meta_edit_leaves_meta_source_empty(client, db):
    tid = _record(db)
    client.patch(f"/api/history/{tid}", json={"title": "Нарада з фондом"}, headers=UI)
    assert _row(db, tid)["meta_source"] is None


def test_non_ui_meta_edit_with_active_job_refused_and_nothing_written(client, db, busy_job):
    tid = _record(db)
    job = busy_job(tid)
    r = client.patch(f"/api/history/{tid}", json={"title": "Чужа назва"})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error_code"] == "JOB_ACTIVE" and r.get_json()["job_id"] == job.id
    row = _row(db, tid)
    assert row["title"] == TITLE and row["meta_source"] is None


def test_ui_meta_edit_ignores_active_job(client, db, busy_job):
    tid = _record(db)
    busy_job(tid)
    r = client.patch(f"/api/history/{tid}", json={"title": "Нарада з фондом"}, headers=UI)
    assert r.status_code == 200
    assert _row(db, tid)["title"] == "Нарада з фондом"


def test_get_record_carries_meta_source(client, db):
    tid = _record(db)
    client.patch(f"/api/history/{tid}", json={"title": "Дзвінок з фондом, підсумок"})
    body = client.get(f"/api/history/{tid}").get_json()
    assert body["meta_source"] == "mcp"


# ------------------------------------------------------------------ history filter

def test_history_deleted_filter_returns_only_soft_deleted(client, db):
    live = _record(db, title="Живий запис")
    gone = _record(db, title="Видалений запис", deleted=True)
    default = client.get("/api/history?per_page=100").get_json()["transcriptions"]
    assert [t["id"] for t in default] == [live]
    assert "deleted_at" not in default[0]
    only = client.get("/api/history?deleted=1&per_page=100").get_json()
    assert [t["id"] for t in only["transcriptions"]] == [gone]
    assert only["transcriptions"][0]["deleted_at"] and only["total"] == 1


# ------------------------------------------------------------------ job lookup

def test_active_job_for_transcription(db, queue, busy_job):
    tid, other = _record(db), _record(db, title="Інший запис")
    assert jq.active_job_for_transcription(tid) is None
    job = busy_job(tid)
    assert jq.active_job_for_transcription(tid) == job.id
    assert jq.active_job_for_transcription(other) is None
    done = queue.submit("reembed_point", lambda job: None, meta={"transcription_id": other})
    done._future.result(timeout=10)
    assert jq.active_job_for_transcription(other) is None


def test_active_job_lookup_without_queue(monkeypatch):
    monkeypatch.setattr(app_state, "job_queue", None, raising=False)
    assert jq.active_job_for_transcription(1) is None


# ------------------------------------------------------------------ eviction rule

THREE = [("small", "2026-01-01 10:00:00"), ("medium", "2026-02-01 10:00:00"),
         ("large-v3", "2026-03-01 10:00:00")]


def test_evict_candidate_names_the_oldest_non_active(db):
    tid = _record(db)
    vids = _versions(db, tid, THREE)
    with get_db_connection(db) as c:
        cand = tv.evict_candidate(c, tid)
    assert cand == {"id": vids[0], "model_used": "small", "created_at": "2026-01-01 10:00:00"}


def test_evict_candidate_skips_the_active_oldest(db):
    tid = _record(db)
    vids = _versions(db, tid, THREE, active=0)
    with get_db_connection(db) as c:
        assert tv.evict_candidate(c, tid)["id"] == vids[1]


@pytest.mark.parametrize("specs", [THREE[:2], THREE[:1]], ids=["two", "one"])
def test_evict_candidate_none_while_room_left(specs, db):
    tid = _record(db)
    _versions(db, tid, specs)
    with get_db_connection(db) as c:
        assert tv.evict_candidate(c, tid) is None


def test_evict_candidate_counts_the_lazy_original(db):
    tid = _record(db)  # never re-run: write_back would create the original row
    with get_db_connection(db) as c:
        assert tv.evict_candidate(c, tid) is None
        assert tv.evict_candidate(c, tid, keep=1) is None  # original and new are both safe
        assert c.execute("SELECT COUNT(*) FROM transcript_versions").fetchone()[0] == 0


def test_evict_candidate_counts_the_original_a_null_pointer_will_create(db):
    tid = _record(db)
    vids = _versions(db, tid, THREE[:2])
    with get_db_connection(db) as c:
        c.execute("UPDATE transcriptions SET active_version_id = NULL WHERE id = ?", (tid,))
        cand = tv.evict_candidate(c, tid)
        previous = tv.write_back(c, tid)  # creates the original row
        vid = tv.insert_version(c, tid, model_used="large-v3", language="uk", processing_time=1.0,
                                transcript_text="Нова", segments=[], speaker_map=[])
        tv.activate(c, tid, vid)
        assert tv.evict(c, tid, protect=(previous,)) == [cand["id"]] == [vids[0]]
        c.rollback()


def test_evict_candidate_matches_what_evict_drops(db):
    tid = _record(db)
    vids = _versions(db, tid, THREE)
    with get_db_connection(db) as c:
        cand = tv.evict_candidate(c, tid)
        previous = tv.write_back(c, tid)
        vid = tv.insert_version(c, tid, model_used="large-v3", language="uk", processing_time=1.0,
                                transcript_text="Нова", segments=[], speaker_map=[])
        tv.activate(c, tid, vid)
        assert tv.evict(c, tid, protect=(previous,)) == [cand["id"]] == [vids[0]]
        c.rollback()


def test_job_without_allow_evict_fails_and_writes_nothing(db, queue, tmp_path, monkeypatch):
    media = tmp_path / "фонд.mp3"
    media.write_bytes(b"x")
    monkeypatch.setattr(app_state, "whisper_manager", FakeWhisper(), raising=False)
    monkeypatch.setattr(retranscribe, "recording_active", lambda **kw: False)
    monkeypatch.setattr(reembed, "schedule_record_reembed", lambda tid, *, db_path: "queued")
    tid = _record(db, file_path=str(media))
    vids = _versions(db, tid, THREE)
    before = _row(db, tid)
    res = retranscribe.submit(tid, model="large-v3", db_path=db, allow_evict=False)
    job = queue.get(res["job_id"])
    with pytest.raises(retranscribe.RetranscribeError):
        job._future.result(timeout=10)
    assert job.state == "failed" and job.error_code == "EVICTION_NOT_APPROVED"
    with get_db_connection(db) as c:
        left = [r[0] for r in c.execute("SELECT id FROM transcript_versions WHERE transcription_id = ? "
                                        "ORDER BY id", (tid,))]
    assert left == vids
    after = _row(db, tid)
    assert after["active_version_id"] == before["active_version_id"]
    assert after["transcript_text"] == OLD
