"""Ask 9 conditional eviction gate and the non-ui D preconditions (mcp-archive-org story 05).

The app wires the real transcription and memory blueprints behind auth -> approval gate ->
audit hooks over a temp DB, as `tests/test_approval_gate.py` does; the dialog is the conftest
stub (answers No unless `approve_dialogs`). Fixtures are Cyrillic and invented.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from app import state as app_state
from app.blueprints.memory import memory_bp
from app.blueprints.transcription import transcription_bp
from app.core import approval_gate, audit_hooks, auth
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories as cats
from app.services import reembed, retranscribe
from app.services.job_queue import JobQueue

TITLE = "Дзвінок з фондом"
OLD = "Добрий день, обговорюємо грант фонду"
NEW = "Здравствуйте, обсуждаем грант фонда"
UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}
THREE = [("small", "2026-01-01 10:00:00"), ("medium", "2026-02-01 10:00:00"),
         ("large-v3", "2026-03-01 10:00:00")]


class FakeWhisper:
    def get_available_models(self):
        return [{"name": "large-v3", "downloaded": True}, {"name": "tiny", "downloaded": False}]

    def transcribe_with_progress(self, path, model, language, **kw):
        return {"text": NEW, "language": language, "segments": [{"start": 0.0, "end": 2.0, "text": NEW}]}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "archive_org_gate.db")
    init_database(path)
    return path


@pytest.fixture
def queue(monkeypatch):
    q = JobQueue(executor=ThreadPoolExecutor(max_workers=2), slow_executor=ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(app_state, "job_queue", q, raising=False)
    return q


@pytest.fixture
def client(db, tmp_path, monkeypatch, queue):
    monkeypatch.setenv("RECALL_LOCAL_TRUSTED", "1")
    monkeypatch.setenv("RECALL_PRIVATE_CATEGORIES", "Особисте,Здоровʼя")
    monkeypatch.setattr(app_state, "recording_service", None, raising=False)
    monkeypatch.setattr(app_state, "whisper_manager", FakeWhisper(), raising=False)
    monkeypatch.setattr(retranscribe, "recording_active", lambda **kw: False)
    monkeypatch.setattr(reembed, "schedule_record_reembed", lambda tid, *, db_path: "queued")
    a = Flask(__name__, root_path=str(tmp_path))
    a.config.update(TESTING=True, DATABASE=db)
    a.register_blueprint(transcription_bp)
    a.register_blueprint(memory_bp)
    auth.register(a)
    approval_gate.register(a)
    audit_hooks.register(a)
    approval_gate.reset_counters()
    yield a.test_client()
    approval_gate.reset_counters()


def _uk(key, **kw):
    return tr(key, lang="uk", **kw)


def _record(db, media, versions=(), file_path="auto"):
    segs = json.dumps([{"start": 0, "end": 2, "text": OLD}], ensure_ascii=False)
    with get_db_connection(db) as c:
        tid = c.execute(
            "INSERT INTO transcriptions (source_type, source_name, title, file_path, transcript_text, "
            "segments, language, model_used, created_at) VALUES ('file', 'фонд.mp3', ?, ?, ?, ?, 'uk', "
            "'small', '2026-01-01 10:00:00')",
            (TITLE, media if file_path == "auto" else file_path, OLD, segs)).lastrowid
        vids = []
        for i, (model, created) in enumerate(versions):
            vids.append(c.execute(
                "INSERT INTO transcript_versions (transcription_id, created_at, is_original, model_used, "
                "language, transcript_text, segments, speaker_map_json) VALUES (?, ?, ?, ?, 'uk', ?, ?, '[]')",
                (tid, created, 1 if i == 0 else 0, model, OLD, segs)).lastrowid)
        if vids:
            c.execute("UPDATE transcriptions SET active_version_id = ? WHERE id = ?", (vids[-1], tid))
        c.commit()
    return tid, vids


@pytest.fixture
def media(tmp_path):
    p = tmp_path / "фонд.mp3"
    p.write_bytes(b"x")
    return str(p)


def _versions(db, tid):
    with get_db_connection(db) as c:
        return [r[0] for r in c.execute(
            "SELECT id FROM transcript_versions WHERE transcription_id = ? ORDER BY id", (tid,))]


def _rerun(client, tid, headers=None, **body):
    return client.post(f"/api/history/{tid}/retranscribe", json={"model": "large-v3", **body},
                       headers=headers or {})


def _finish(queue, resp):
    job = queue.get(resp.get_json()["job_id"])
    try:
        job._future.result(timeout=10)
    except Exception:  # noqa: BLE001 - the job state carries the outcome
        pass
    return job


def _slots(actor="unknown"):
    return len(approval_gate._counts.get(actor, ()))


# ------------------------------------------------------------------ ask 9

def test_non_ui_rerun_dropping_a_version_asks_and_deny_submits_nothing(client, db, media, queue, dialog_stub):
    tid, vids = _record(db, media, THREE)
    r = _rerun(client, tid)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    text = dialog_stub.calls[-1][1]
    assert f"«{TITLE}»" in text
    assert _uk("approvals.extra.version_drop", version="small, 2026-01-01 10:00:00") in text
    assert _uk("approvals.action.transcript_version_evict") in text
    assert queue.list() == []
    assert _versions(db, tid) == vids


def test_approved_rerun_evicts_as_today(client, db, media, queue, approve_dialogs):
    tid, vids = _record(db, media, THREE)
    r = _rerun(client, tid)
    assert r.status_code == 202, r.get_json()
    assert len(approve_dialogs.calls) == 1
    job = _finish(queue, r)
    assert job.state == "completed", job.error_code
    left = _versions(db, tid)
    assert vids[0] not in left and len(left) == 3


@pytest.mark.parametrize("versions", [THREE[:2], ()], ids=["two_versions", "never_rerun"])
def test_non_ui_rerun_without_eviction_has_no_dialog_and_no_slot(versions, client, db, media, queue, dialog_stub):
    tid, _ = _record(db, media, versions)
    r = _rerun(client, tid)
    assert r.status_code == 202, r.get_json()
    assert dialog_stub.calls == []
    assert _slots() == 0
    assert _finish(queue, r).state == "completed"


def test_ui_rerun_with_three_versions_has_no_dialog_and_evicts(client, db, media, queue, dialog_stub):
    tid, vids = _record(db, media, THREE)
    r = _rerun(client, tid, headers=UI)
    assert r.status_code == 202, r.get_json()
    assert dialog_stub.calls == []
    assert _finish(queue, r).state == "completed"
    assert vids[0] not in _versions(db, tid)


def test_unapproved_non_ui_job_never_evicts(client, db, media, queue, dialog_stub, monkeypatch):
    # The gate saw nothing to drop (a race); the job still refuses to drop a version.
    tid, vids = _record(db, media, THREE)
    monkeypatch.setattr(retranscribe, "would_evict", lambda tid, **kw: False)
    r = _rerun(client, tid)
    assert r.status_code == 202, r.get_json()
    assert dialog_stub.calls == []
    job = _finish(queue, r)
    assert job.state == "failed" and job.error_code == "EVICTION_NOT_APPROVED"
    assert _versions(db, tid) == vids


def test_predicate_error_gates_the_request(client, db, media, queue, dialog_stub, monkeypatch):
    tid, _ = _record(db, media, THREE)

    def _boom(tid, **kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(retranscribe, "would_evict", _boom)
    r = _rerun(client, tid)
    assert r.status_code == 403 and r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert queue.list() == []


@pytest.mark.parametrize("case", ["unknown_record", "no_media", "not_installed", "no_model", "model_not_text",
                                  "bad_language", "running"])
def test_bad_rerun_refused_with_its_code_before_any_dialog(case, client, db, media, queue, dialog_stub,
                                                          monkeypatch):
    tid, _ = _record(db, media, THREE, file_path="auto" if case != "no_media" else None)
    body = {}
    if case == "unknown_record":
        tid = 99999
    if case == "not_installed":
        body = {"model": "tiny"}
    if case == "no_model":
        body = {"model": "  "}
    if case == "model_not_text":
        body = {"model": 123}
    if case == "bad_language":
        body = {"language": ["uk"]}
    if case == "running":
        monkeypatch.setitem(retranscribe._inflight, tid, "job-фонд")
    r = _rerun(client, tid, **body)
    expected = {"unknown_record": (404, "NOT_FOUND"), "no_media": (404, "NO_MEDIA"),
                "not_installed": (400, "MODEL_NOT_INSTALLED"), "no_model": (400, "MODEL_NOT_INSTALLED"),
                "model_not_text": (400, "MODEL_NOT_INSTALLED"), "bad_language": (400, "INVALID_ARGUMENT"),
                "running": (409, "ALREADY_RUNNING")}[case]
    assert (r.status_code, r.get_json()["error_code"]) == expected, r.get_json()
    if case == "running":
        assert r.get_json()["job_id"] == "job-фонд"
    assert dialog_stub.calls == []
    assert _slots() == 0
    assert queue.list() == []


# ------------------------------------------------------------------ D preconditions

def _cat(db, name):
    """The seeded direction of that name, or a new one."""
    with get_db_connection(db) as c:
        row = c.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
        return row[0] if row else cats.create_category(c, name)["id"]


def _alive(db, cid):
    with get_db_connection(db) as c:
        return c.execute("SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone() is not None


def test_non_ui_delete_of_private_direction_refused_without_dialog(client, db, dialog_stub):
    cid = _cat(db, "Особисте")
    r = client.delete(f"/api/memory/categories/{cid}")
    assert r.status_code == 403 and r.get_json()["error_code"] == "PRIVATE_CATEGORY_CHANGE"
    assert dialog_stub.calls == [] and _slots() == 0
    assert _alive(db, cid)


def test_non_ui_delete_of_open_direction_still_asks(client, db, dialog_stub):
    cid = _cat(db, "Фонди")
    r = client.delete(f"/api/memory/categories/{cid}")
    assert r.status_code == 403 and r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1


def test_ui_delete_of_private_direction_unaffected(client, db, dialog_stub):
    cid = _cat(db, "Особисте")
    r = client.delete(f"/api/memory/categories/{cid}", headers=UI)
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1


@pytest.mark.parametrize("target", ["open", "none"])
def test_non_ui_merge_exposing_private_direction_refused(target, client, db, dialog_stub):
    src = _cat(db, "Особисте")
    body = {"target_id": _cat(db, "Фонди")} if target == "open" else {}
    r = client.post(f"/api/memory/categories/{src}/merge", json=body)
    assert r.status_code == 403 and r.get_json()["error_code"] == "PRIVATE_CATEGORY_CHANGE"
    assert dialog_stub.calls == []
    assert _alive(db, src)


def test_non_ui_merge_private_into_private_still_asks(client, db, dialog_stub):
    src = _cat(db, "Особисте")
    dst = _cat(db, "Здоровʼя")
    r = client.post(f"/api/memory/categories/{src}/merge", json={"target_id": dst})
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1


def test_ui_merge_of_private_direction_unaffected(client, db, dialog_stub):
    src = _cat(db, "Особисте")
    r = client.post(f"/api/memory/categories/{src}/merge", json={"target_id": _cat(db, "Фонди")}, headers=UI)
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1


@pytest.fixture
def busy_job(queue):
    """A running job on a record; released at teardown."""
    release = threading.Event()
    holder = {}

    def _start(tid):
        job = queue.submit("reembed_point", lambda job: release.wait(10), meta={"transcription_id": tid})
        holder["job"] = job
        return job.id

    yield _start
    release.set()
    if "job" in holder:
        holder["job"]._future.result(timeout=10)


def test_non_ui_record_delete_with_active_job_refused_without_dialog(client, db, media, dialog_stub, busy_job):
    tid, _ = _record(db, media)
    job_id = busy_job(tid)
    r = client.delete(f"/api/history/{tid}")
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error_code"] == "JOB_ACTIVE" and r.get_json()["job_id"] == job_id
    assert dialog_stub.calls == [] and _slots() == 0


def test_ui_record_delete_with_active_job_unaffected(client, db, media, dialog_stub, busy_job):
    tid, _ = _record(db, media)
    busy_job(tid)
    r = client.delete(f"/api/history/{tid}", headers=UI)
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1


def test_non_ui_record_delete_without_job_still_asks(client, db, media, dialog_stub, busy_job):
    tid, _ = _record(db, media)
    other, _ = _record(db, media)
    busy_job(other)
    r = client.delete(f"/api/history/{tid}")
    assert r.get_json()["error_code"] == "APPROVAL_DENIED"
    assert len(dialog_stub.calls) == 1
