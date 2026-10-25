"""mcp-pipeline story 04: job results, wait and log; lock-free model download; diagnostics to a file.

Offline: no app.py import, no network. The faster-whisper download is faked at the
``faster_whisper.utils.download_model`` seam; the HF cache points at tmp_path.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from flask import Flask

import whisper_manager_new
from app import state
from app.blueprints import events as events_mod
from app.blueprints.events import events_bp
from app.blueprints.system import system_bp
from app.db.migrations import init_database
from app.services import diagnostics, model_download
from app.services.job_queue import RESULT_MAX_BYTES, SLOW_KINDS, JobQueue


# ------------------------------------------------------------------ fixtures

class FakeLogs:
    def __init__(self):
        self.data = {}

    def get(self, pid, last_n=None):
        return list(self.data.get(pid, []))


@pytest.fixture()
def queue(monkeypatch):
    q = JobQueue(executor=ThreadPoolExecutor(max_workers=2))
    monkeypatch.setattr(state, "job_queue", q)
    logs = FakeLogs()
    monkeypatch.setattr(state, "process_logs", logs)
    monkeypatch.setattr(state, "add_log", lambda pid, stage, msg, progress=None, status="processing":
                        logs.data.setdefault(pid, []).append({"stage": stage, "message": msg}))
    return q


@pytest.fixture()
def client(queue, tmp_path):
    app = Flask(__name__, root_path=str(tmp_path))
    app.register_blueprint(events_bp)
    app.register_blueprint(system_bp)
    return app.test_client()


def _wait(job):
    try:
        job._future.result(timeout=10)
    except Exception:  # noqa: BLE001 - the state is asserted by the caller
        pass
    return job


# ------------------------------------------------------------------ job result

def test_result_is_the_task_return_value(queue):
    job = _wait(queue.submit("test", lambda j: {"transcription_id": 42}))
    assert job.to_dict()["result"] == {"transcription_id": 42}
    assert job.to_dict()["result_truncated"] is False


def test_non_json_result_is_stored_json_safe_and_flagged(queue):
    job = _wait(queue.submit("test", lambda j: {"when": datetime(2026, 10, 21), "n": 1}))
    d = job.to_dict()
    json.dumps(d)
    assert d["result"]["n"] == 1 and isinstance(d["result"]["when"], str)
    assert d["result_truncated"] is True


def test_oversized_result_is_cut_and_flagged(queue):
    job = _wait(queue.submit("test", lambda j: {"items": ["x" * 100] * 400}))
    d = job.to_dict()
    assert d["result_truncated"] is True
    assert isinstance(d["result"], str)
    assert len(d["result"].encode("utf-8")) <= RESULT_MAX_BYTES


def test_failed_job_result_null_and_error_code(queue):
    def bad(job):
        raise RuntimeError("boom")

    d = _wait(queue.submit("test", bad)).to_dict()
    assert d["result"] is None and d["error_code"] == "JOB_FAILED"


def test_persisted_job_keeps_result_after_restart(tmp_path):
    db = str(tmp_path / "jobs.db")
    init_database(db)
    q1 = JobQueue(executor=ThreadPoolExecutor(max_workers=1), db_path=db)
    job = _wait(q1.submit("test", lambda j: {"items": [{"path": "C:/a.pdf", "status": "created"}]},
                          meta={"title": "т"}))
    q2 = JobQueue(executor=ThreadPoolExecutor(max_workers=1), db_path=db)
    got = q2.get(job.id)
    assert got is not None and got.state == "completed"
    assert got.to_dict()["result"] == {"items": [{"path": "C:/a.pdf", "status": "created"}]}
    assert got.meta == {"title": "т"}


def test_crashed_job_result_null(tmp_path):
    db = str(tmp_path / "jobs.db")
    init_database(db)
    q1 = JobQueue(executor=ThreadPoolExecutor(max_workers=1), db_path=db)
    release, started = threading.Event(), threading.Event()

    def long(job):
        started.set()
        release.wait(5)
        return {"x": 1}

    job = q1.submit("test", long)
    assert started.wait(5)
    deadline = time.time() + 2
    while time.time() < deadline:
        from app.db.connection import get_db_connection
        with get_db_connection(db) as conn:
            row = conn.execute("SELECT state FROM jobs WHERE id=?", (job.id,)).fetchone()
        if row and row["state"] == "running":
            break
        time.sleep(0.02)
    q2 = JobQueue(executor=ThreadPoolExecutor(max_workers=1), db_path=db)
    assert q2.get(job.id) is None  # a running row is left to recovery
    assert job.id in q2.recover_crashed()
    d = q2.get(job.id).to_dict()
    assert d["state"] == "crashed" and d["result"] is None and d["error_code"] == "JOB_CRASHED"
    release.set()
    _wait(job)


def test_find_active_by_meta(queue):
    release = threading.Event()
    job = queue.submit("model_download", lambda j: release.wait(5), meta={"model": "small"})
    try:
        assert queue.find_active("model_download", model="small") is job
        assert queue.find_active("model_download", model="tiny") is None
    finally:
        release.set()
        _wait(job)
    assert queue.find_active("model_download", model="small") is None


# ------------------------------------------------------------------ GET /api/jobs/<id>

def test_wait_returns_as_soon_as_job_finishes(client, queue):
    job = queue.submit("test", lambda j: (time.sleep(0.4), {"ok": True})[1])
    t0 = time.monotonic()
    r = client.get(f"/api/jobs/{job.id}?wait=5")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.get_json()["state"] == "completed"
    assert r.get_json()["result"] == {"ok": True}
    assert elapsed < 2.0


def test_wait_gives_up_after_the_timeout(client, queue):
    release = threading.Event()
    job = queue.submit("test", lambda j: release.wait(10))
    try:
        t0 = time.monotonic()
        body = client.get(f"/api/jobs/{job.id}?wait=0.5").get_json()
        elapsed = time.monotonic() - t0
        assert body["state"] == "running"
        assert 0.4 <= elapsed < 2.0
    finally:
        release.set()
        _wait(job)


def test_wait_is_clamped(client, queue, monkeypatch):
    release = threading.Event()
    job = queue.submit("test", lambda j: release.wait(10))
    try:
        t0 = time.monotonic()
        client.get(f"/api/jobs/{job.id}?wait=-5")
        assert time.monotonic() - t0 < 0.3
        monkeypatch.setattr(events_mod, "_WAIT_MAX_S", 0.3)
        t0 = time.monotonic()
        client.get(f"/api/jobs/{job.id}?wait=100")
        assert time.monotonic() - t0 < 1.5
    finally:
        release.set()
        _wait(job)


def test_upper_wait_bound_is_25_seconds():
    assert events_mod._WAIT_MAX_S == 25.0


def test_include_log_returns_tail(client, queue):
    def logging_job(job):
        for i in range(5):
            state.add_log(job.id, "step", f"m{i}")
        return None

    job = _wait(queue.submit("test", logging_job))
    body = client.get(f"/api/jobs/{job.id}?include_log=1&tail=3").get_json()
    assert [e["message"] for e in body["log"]] == ["m2", "m3", "m4"]
    assert "log" not in client.get(f"/api/jobs/{job.id}").get_json()


def test_include_log_empty_when_kind_never_logged(client, queue):
    job = _wait(queue.submit("test", lambda j: 1))
    assert client.get(f"/api/jobs/{job.id}?include_log=1&tail=3").get_json()["log"] == []


def test_list_filters_kind_and_active(client, queue):
    release = threading.Event()
    a = queue.submit("kind_a", lambda j: release.wait(5))
    b = _wait(queue.submit("kind_b", lambda j: 1))
    try:
        ids = [j["id"] for j in client.get("/api/jobs?kind=kind_a").get_json()["jobs"]]
        assert ids == [a.id]
        ids = [j["id"] for j in client.get("/api/jobs?active_only=true").get_json()["jobs"]]
        assert a.id in ids and b.id not in ids
        assert [j["id"] for j in client.get("/api/jobs?kind=kind_b&active_only=1").get_json()["jobs"]] == []
    finally:
        release.set()
        _wait(a)


# ------------------------------------------------------------------ model download

@pytest.fixture()
def hf_cache(tmp_path, monkeypatch):
    cache = tmp_path / "hf"
    cache.mkdir()
    monkeypatch.setattr(whisper_manager_new, "_hf_cache_base", lambda: cache)
    return cache


@pytest.fixture()
def faster_manager(monkeypatch, hf_cache):
    wm = whisper_manager_new.ModernWhisperManager(force_cpu=True, backend="faster")
    monkeypatch.setattr(state, "whisper_manager", wm)
    return wm


@pytest.fixture()
def fake_fw_download(monkeypatch):
    """Fakes faster_whisper.utils.download_model: writes the snapshot the loader would find."""
    from faster_whisper import utils as fw_utils
    calls = []
    gate = threading.Event()
    gate.set()

    def fake(size_or_id, output_dir=None, local_files_only=False, cache_dir=None, revision=None,
             use_auth_token=None):
        calls.append({"repo": size_or_id, "cache_dir": cache_dir, "output_dir": output_dir})
        assert gate.wait(10)
        snap = os.path.join(cache_dir, "models--" + size_or_id.replace("/", "--"), "snapshots", "abc")
        os.makedirs(snap, exist_ok=True)
        open(os.path.join(snap, "model.bin"), "wb").close()
        return snap

    monkeypatch.setattr(fw_utils, "download_model", fake)
    return calls, gate


def test_model_download_is_a_slow_kind(queue):
    assert "model_download" in SLOW_KINDS
    assert queue._executor_for_kind("model_download") is queue._slow_executor


def test_submit_downloads_repo_into_loader_cache(queue, faster_manager, fake_fw_download, hf_cache):
    calls, _ = fake_fw_download
    sub = model_download.submit("small", actor="mcp")
    assert sub["created"] is True
    job = _wait(queue.get(sub["job_id"]))
    assert job.state == "completed", job.error
    assert job.kind == "model_download"
    assert job.meta["model"] == "small" and job.meta["actor"] == "mcp" and job.meta["stage"] == "done"
    assert calls == [{"repo": "Systran/faster-whisper-small", "cache_dir": str(hf_cache), "output_dir": None}]
    assert job.to_dict()["result"] == {"model": "small", "backend": "faster"}


def test_second_submit_while_running_joins(queue, faster_manager, fake_fw_download):
    _, gate = fake_fw_download
    gate.clear()
    first = model_download.submit("small")
    try:
        second = model_download.submit("small")
        assert second == {"job_id": first["job_id"], "created": False}
    finally:
        gate.set()
        _wait(queue.get(first["job_id"]))


def test_unknown_model_is_refused(queue, faster_manager):
    with pytest.raises(model_download.ModelDownloadError) as exc:
        model_download.submit("no-such-model")
    assert exc.value.error_code == "UNKNOWN_MODEL"
    assert queue.list() == []


def test_download_completes_while_load_lock_is_held(queue, faster_manager, fake_fw_download):
    held, release = threading.Event(), threading.Event()

    def holder():
        with faster_manager._load_lock:
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(5)
        job = queue.get(model_download.submit("small")["job_id"])
        job._future.result(timeout=5)
        assert job.state == "completed"
        assert not release.is_set()  # the lock was held for the whole download
    finally:
        release.set()
        t.join(5)


def test_models_list_reports_downloaded_after_job(client, queue, faster_manager, fake_fw_download):
    def small():
        return next(m for m in client.get("/api/models").get_json() if m["name"] == "small")

    assert small()["downloaded"] is False
    r = client.post("/api/models/download-job", json={"model_name": "small"})
    assert r.status_code == 202
    body = r.get_json()
    assert body["success"] is True and body["created"] is True and body["job_id"]
    _wait(queue.get(body["job_id"]))
    assert small()["downloaded"] is True


def test_openai_backend_uses_manager_download(queue, monkeypatch):
    class FakeOpenAI:
        backend_name = "openai"
        calls = []

        def get_available_models(self):
            return [{"name": "base", "downloaded": False, "backend": "openai"}]

        def download_model(self, name):
            self.calls.append(name)
            return True

    wm = FakeOpenAI()
    monkeypatch.setattr(state, "whisper_manager", wm)
    job = _wait(queue.get(model_download.submit("base")["job_id"]))
    assert job.state == "completed" and wm.calls == ["base"]


def test_download_job_route_refuses_bad_bodies(client, faster_manager):
    assert client.post("/api/models/download-job").status_code == 400
    r = client.post("/api/models/download-job", json={"model_name": "nope"})
    assert r.status_code == 400 and r.get_json()["error_code"] == "UNKNOWN_MODEL"


def test_sync_download_route_keeps_its_shape(client, queue, faster_manager, fake_fw_download):
    r = client.post("/api/download_model", json={"model_name": "small"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True and body["message"] and body["job_id"]
    assert queue.get(body["job_id"]).kind == "model_download"


def test_sync_download_route_failure_shape(client, queue, faster_manager, monkeypatch):
    from faster_whisper import utils as fw_utils

    def broken(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(fw_utils, "download_model", broken)
    r = client.post("/api/download_model", json={"model_name": "small"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["success"] is False and body["error"] and "network down" not in body["error"]


@pytest.mark.parametrize("kwargs", [
    {},
    {"data": "not json", "content_type": "application/json"},
    {"data": "model_name=small", "content_type": "text/plain"},
    {"json": ["small"]},
    {"json": {"model_name": 5}},
])
def test_sync_download_route_bad_body_is_400(client, faster_manager, kwargs):
    r = client.post("/api/download_model", **kwargs)
    assert r.status_code == 400
    assert r.get_json()["success"] is False


# ------------------------------------------------------------------ diagnostics

@pytest.fixture()
def diag_app(tmp_path, monkeypatch):
    app = Flask(__name__, root_path=str(tmp_path))
    app.register_blueprint(system_bp)
    monkeypatch.setattr(state, "whisper_manager", None)
    (tmp_path / "whisper_ui.log").write_text("INFO started\nANTHROPIC_API_KEY=sk-ant-api03-SECRETSECRET123\n",
                                             encoding="utf-8")
    return app


def test_build_package_entries_and_redaction(diag_app):
    with diag_app.app_context():
        data = diagnostics.build_package()
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert set(zf.namelist()) == {"logs/whisper_ui.log", "system_info.json", "manifest.json"}
    content = zf.read("logs/whisper_ui.log").decode("utf-8")
    assert "SECRETSECRET123" not in content and "REDACTED" in content


def test_write_package_writes_named_zip(diag_app, tmp_path):
    with diag_app.app_context():
        out = diagnostics.write_package()
    assert set(out) == {"path", "size", "included_logs", "app_version", "git_commit"}
    assert os.path.dirname(out["path"]) == os.path.abspath(tmp_path / "logs" / "diagnostics")
    assert re.fullmatch(r"\d{8}-\d{6}\.zip", os.path.basename(out["path"]))
    assert os.path.getsize(out["path"]) == out["size"]
    assert out["included_logs"] == ["whisper_ui.log"]
    zipfile.ZipFile(out["path"]).testzip()


def test_sixth_package_prunes_to_five(diag_app, tmp_path):
    base = datetime(2026, 10, 21, 10, 0, 0)
    paths = []
    with diag_app.app_context():
        for i in range(6):
            paths.append(diagnostics.write_package(now=base + timedelta(seconds=i))["path"])
            time.sleep(0.02)
    left = sorted(os.listdir(tmp_path / "logs" / "diagnostics"))
    assert len(left) == 5
    assert os.path.basename(paths[0]) not in left
    assert left == sorted(os.path.basename(p) for p in paths[1:])


def test_diagnostic_file_route(diag_app, tmp_path):
    r = diag_app.test_client().post("/api/system/diagnostic-package/file")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True and os.path.isfile(body["path"]) and body["size"] > 0
    assert body["included_logs"] == ["whisper_ui.log"]
    assert "app_version" in body and "git_commit" in body


def test_diagnostic_get_still_streams_zip(diag_app):
    r = diag_app.test_client().get("/api/system/diagnostic-package")
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("application/zip")
    assert not (os.path.exists(os.path.join(diag_app.root_path, "logs", "diagnostics")))
