"""Tests for JobQueue (Phase 2)."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.services.job_queue import Job, JobQueue


def test_submit_completes():
    executor = ThreadPoolExecutor(max_workers=2)
    queue = JobQueue(executor=executor)

    def job_fn(job):
        return 42

    job = queue.submit("test", job_fn, meta={"key": "value"})
    # Wait for completion
    job._future.result(timeout=5)
    assert job.state == "completed"
    assert job.error is None
    assert job.meta["key"] == "value"


def test_failed_job_has_error_state():
    executor = ThreadPoolExecutor(max_workers=2)
    queue = JobQueue(executor=executor)

    def bad_job(job):
        raise ValueError("boom")

    job = queue.submit("test", bad_job)
    try:
        job._future.result(timeout=5)
    except ValueError:
        pass
    assert job.state == "failed"
    assert "boom" not in (job.error or "")
    assert job.error
    assert job.error_code == "JOB_FAILED"


def test_cancel_before_start():
    executor = ThreadPoolExecutor(max_workers=1)
    queue = JobQueue(executor=executor)

    # Block the executor with the first task
    block = []

    def slow(job):
        while not block:
            time.sleep(0.05)

    queue.submit("test", slow)
    # Now create the second — it will be queued
    job2 = queue.submit("test", lambda j: 1)
    # Cancel — the Future has not started yet, it must work
    ok = queue.cancel(job2.id)
    assert ok is True
    # Unblock and wait
    block.append(True)


def test_list_filter_active_only():
    executor = ThreadPoolExecutor(max_workers=2)
    queue = JobQueue(executor=executor)

    job1 = queue.submit("kind1", lambda j: 1)
    job1._future.result(timeout=5)  # wait for completion

    job2 = queue.submit("kind1", lambda j: 1)
    job2._future.result(timeout=5)

    # Both completed → active_only=True must return empty
    assert queue.list(kind="kind1", active_only=True) == []
    # Without a filter — both
    assert len(queue.list(kind="kind1")) == 2


def test_live_kind_routes_to_live_executor_and_isnt_blocked_by_batch():
    """T2.5 (Wave 2): a 'live' kind must not wait behind a long batch task.

    One batch task occupies the SINGLE batch worker (max_workers=1) for a long time.
    Without separating the pools the next task (even a live kind) would wait in the queue
    behind it. With live_executor — the live-kind task runs at once, in parallel
    to the batch.
    """
    batch_executor = ThreadPoolExecutor(max_workers=1)
    live_executor = ThreadPoolExecutor(max_workers=1)
    queue = JobQueue(
        executor=batch_executor,
        live_executor=live_executor,
        live_kinds=frozenset({"recording_finalize"}),
    )

    batch_release = threading.Event()
    batch_started = threading.Event()

    def slow_batch(job):
        batch_started.set()
        batch_release.wait(timeout=5)
        return "batch-done"

    live_ran = threading.Event()

    def fast_live(job):
        live_ran.set()
        return "live-done"

    batch_job = queue.submit("enrichment_backfill", slow_batch)
    assert batch_started.wait(timeout=5), "batch job never started"

    live_job = queue.submit("recording_finalize", fast_live)
    # the live job must finish quickly, NOT waiting for the batch (a separate pool).
    assert live_ran.wait(timeout=5), "live job was blocked behind batch job"
    live_job._future.result(timeout=5)
    assert live_job.state == "completed"

    batch_release.set()
    batch_job._future.result(timeout=5)
    assert batch_job.state == "completed"


def test_non_live_kind_still_uses_default_executor():
    executor = ThreadPoolExecutor(max_workers=2)
    live_executor = ThreadPoolExecutor(max_workers=1)
    queue = JobQueue(
        executor=executor,
        live_executor=live_executor,
        live_kinds=frozenset({"recording_finalize"}),
    )
    job = queue.submit("doc_import", lambda j: 1)
    job._future.result(timeout=5)
    assert job.state == "completed"


def test_to_dict_excludes_internal():
    executor = ThreadPoolExecutor(max_workers=2)
    queue = JobQueue(executor=executor)
    job = queue.submit("k", lambda j: 1, meta={"foo": "bar"})
    d = job.to_dict()
    # Internal fields (Future, Event) must not get into the dict
    assert "_future" not in d
    assert "_cancel_event" not in d
    # But meta must
    assert d["meta"] == {"foo": "bar"}


def _run(queue, kind, fn):
    job = queue.submit(kind, fn)
    try:
        job._future.result(timeout=5)
    except Exception:  # noqa: BLE001 - the failure is asserted on the job
        pass
    return job


def test_failed_job_hides_raw_exception_text(caplog):
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))

    def bad(job):
        raise RuntimeError(r"C:\secret\path boom")

    with caplog.at_level(logging.ERROR):
        job = _run(queue, "test", bad)
    assert job.state == "failed"
    assert job.error_code == "JOB_FAILED"
    assert "secret" not in str(job.to_dict())
    assert "secret" in caplog.text  # the traceback is in the log


def test_typed_error_code_is_kept():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))

    class NoVideo(Exception):
        error_code = "NO_VIDEO"

    def bad(job):
        raise NoVideo("x")

    job = _run(queue, "test", bad)
    assert job.error_code == "NO_VIDEO"
    assert job.to_dict()["error_code"] == "NO_VIDEO"


def test_late_cancel_on_non_aware_job_is_ignored():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    started, release = threading.Event(), threading.Event()

    def fn(job):
        started.set()
        release.wait(5)

    job = queue.submit("test", fn)
    assert started.wait(5)
    assert queue.cancel(job.id) is True
    release.set()
    job._future.result(timeout=5)
    assert job.state == "completed"
    d = job.to_dict()
    assert d["cancel_requested"] is True and d["cancel_ignored"] is True


def test_cancel_aware_job_reports_cancelled():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    started = threading.Event()

    def fn(job):
        started.set()
        while not job.is_cancelled():
            time.sleep(0.01)

    job = queue.submit("test", fn)
    assert started.wait(5)
    queue.cancel(job.id)
    job._future.result(timeout=5)
    assert job.state == "cancelled"
    assert job.to_dict()["cancel_ignored"] is False


def test_slow_kind_runs_on_its_own_executor():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    started, release = threading.Event(), threading.Event()

    def slow(job):
        started.set()
        release.wait(5)

    slow_job = queue.submit("reembed_batch", slow)
    assert started.wait(5)
    other = queue.submit("test", lambda job: 1)
    other._future.result(timeout=5)  # default executor is free while the slow one is blocked
    assert other.state == "completed" and slow_job.state == "running"
    release.set()
    slow_job._future.result(timeout=5)


def test_minutes_long_kinds_go_to_the_slow_executor():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    for kind in ("video_analysis", "youtube_download", "audio_download"):
        assert queue._executor_for_kind(kind) is queue._slow_executor, kind
    assert queue._executor_for_kind("enrichment") is queue._executor


def test_every_slow_kind_is_actually_submitted():
    """A kind in SLOW_KINDS that nothing submits is a dead entry that hides the real slow ones."""
    import re
    from pathlib import Path
    from app.services.job_queue import SLOW_KINDS
    root = Path(__file__).resolve().parent.parent
    source = "\n".join(p.read_text(encoding="utf-8") for p in
                       [root / "app.py", *(root / "app").rglob("*.py")])
    submitted = set(re.findall(r"""\.submit\(\s*['"]([a-z_]+)['"]""", source))
    assert SLOW_KINDS <= submitted, sorted(SLOW_KINDS - submitted)


def test_cancel_on_crashed_job_is_refused():
    """A job recovered as `crashed` after a restart is finished: a cancel is not accepted."""
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    job = Job(id="crashed-1", kind="test", state="crashed", finished_at=time.time())
    queue._jobs[job.id] = job
    assert queue.cancel(job.id) is False
    assert job.state == "crashed" and job.cancel_requested is False


def test_cancel_route_reports_ignored_for_crashed_job():
    from flask import Flask
    from app import state
    from app.blueprints.events import events_bp
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    job = Job(id="crashed-2", kind="test", state="crashed", finished_at=time.time())
    queue._jobs[job.id] = job
    old = getattr(state, "job_queue", None)
    state.job_queue = queue
    try:
        app = Flask(__name__)
        app.register_blueprint(events_bp)
        resp = app.test_client().post(f"/api/jobs/{job.id}/cancel")
    finally:
        state.job_queue = old
    assert resp.status_code == 400
    assert resp.get_json()["cancel_ignored"] is True


def test_cancel_route_reports_ignored_for_finished_job():
    from flask import Flask
    from app import state
    from app.blueprints.events import events_bp
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    job = _run(queue, "test", lambda j: 1)
    old = getattr(state, "job_queue", None)
    state.job_queue = queue
    try:
        app = Flask(__name__)
        app.register_blueprint(events_bp)
        resp = app.test_client().post(f"/api/jobs/{job.id}/cancel")
    finally:
        state.job_queue = old
    body = resp.get_json()
    assert body["cancel_ignored"] is True
    assert body["success"] is False and "error" in body


def test_finished_job_keeps_task_result():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))
    job = _run(queue, "test", lambda j: {"transcription_id": 7, "items": [1, 2]})
    d = job.to_dict()
    assert d["result"] == {"transcription_id": 7, "items": [1, 2]}
    assert d["result_truncated"] is False


def test_failed_job_has_no_result():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1))

    def bad(job):
        raise RuntimeError("boom")

    d = _run(queue, "test", bad).to_dict()
    assert d["result"] is None and d["error_code"] == "JOB_FAILED"


def test_find_active_matches_meta_of_running_job_only():
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=2))
    release = threading.Event()
    running = queue.submit("dl", lambda j: release.wait(5), meta={"model": "small", "actor": "ui"})
    done = _run(queue, "dl", lambda j: 1)
    done.meta["model"] = "tiny"
    try:
        assert queue.find_active("dl", model="small") is running
        assert queue.find_active("dl", model="small", actor="ui") is running
        assert queue.find_active("dl", model="tiny") is None
        assert queue.find_active("other", model="small") is None
    finally:
        release.set()
        running._future.result(timeout=5)
    assert queue.find_active("dl", model="small") is None
