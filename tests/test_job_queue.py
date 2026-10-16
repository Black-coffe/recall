"""Tests for JobQueue (Phase 2)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.services.job_queue import JobQueue


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
    assert "boom" in (job.error or "")


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
