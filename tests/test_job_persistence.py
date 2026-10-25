"""Tests for JobQueue persistence (T2.1, REMEDIATION_PLAN Wave 1).

They check that:
  - without db_path JobQueue behaves as before (pure memory, the
    tests/test_job_queue.py tests do not break);
  - with db_path set, the job state is mirrored into the ``jobs`` table
    (migration v26, app/db/migrations.py);
  - an "application restart" (a new JobQueue with an empty dict cache, the same DB)
    via recover_crashed() marks unfinished jobs as 'crashed' and
    makes them immediately visible via list()/get() — just as recording
    SessionStore.recover_orphaned() does for recording sessions.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.job_queue import JobQueue


def test_recover_crashed_noop_without_db_path():
    """Without set_db_path/the constructor parameter — behaviour as before T2.1."""
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        queue = JobQueue(executor=executor)
        assert queue.recover_crashed() == []
        job = queue.submit("test", lambda j: 1)
        job._future.result(timeout=5)
        assert job.state == "completed"
    finally:
        executor.shutdown(wait=True)


def test_persist_writes_row_to_jobs_table(tmp_path: Path):
    db = str(tmp_path / "test.db")
    init_database(db)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        queue = JobQueue(executor=executor, db_path=db)
        job = queue.submit("youtube_download", lambda j: "ok", meta={"title": "т"})
        job._future.result(timeout=5)
        # _persist in the finally block of _runner runs synchronously before
        # the task thread returns, but future.result() only guarantees
        # the completion of the function itself — we give a small margin for the flush.
        deadline = time.time() + 2
        row = None
        while time.time() < deadline:
            with get_db_connection(db) as conn:
                row = conn.execute(
                    "SELECT id, kind, state, meta_json FROM jobs WHERE id = ?",
                    (job.id,),
                ).fetchone()
            if row is not None and row["state"] == "completed":
                break
            time.sleep(0.02)

        assert row is not None, "the job did not get into the jobs table"
        assert row["kind"] == "youtube_download"
        assert row["state"] == "completed"
        assert "т" in (row["meta_json"] or "")
    finally:
        executor.shutdown(wait=True)


def test_recover_crashed_marks_running_job_and_makes_it_visible(tmp_path: Path):
    """Emulates: the job was 'running' in a process that crashed, then a restart."""
    db = str(tmp_path / "test.db")
    init_database(db)

    executor1 = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    started = threading.Event()

    def long_job(job):
        started.set()
        # Keep the job in the 'running' state until the test explicitly releases it —
        # imitates a long transcription interrupted by a process crash.
        release.wait(timeout=5)
        return "done"

    try:
        queue1 = JobQueue(executor=executor1, db_path=db)
        job = queue1.submit(
            "transcription", long_job, meta={"file": "call.mp3"}
        )
        assert started.wait(timeout=5), "the job did not start in time"

        # Wait until _persist(state='running') is actually written to the DB
        # (it runs in the same task thread right after job.state='running').
        deadline = time.time() + 2
        persisted_running = False
        while time.time() < deadline:
            with get_db_connection(db) as conn:
                row = conn.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
            if row is not None and row["state"] == "running":
                persisted_running = True
                break
            time.sleep(0.02)
        assert persisted_running, "the 'running' state was not recorded in jobs before the 'restart'"

        # --- "Application restart" ---
        # A new JobQueue with an empty dict cache, the same SQLite DB — exactly
        # as after kill+restart of the app.py process.
        executor2 = ThreadPoolExecutor(max_workers=1)
        try:
            queue2 = JobQueue(executor=executor2, db_path=db)
            assert queue2.get(job.id) is None  # empty cache before recovery

            recovered = queue2.recover_crashed()
            assert job.id in recovered

            got = queue2.get(job.id)
            assert got is not None
            assert got.state == "crashed"
            assert got.error
            assert got.error_code == "JOB_CRASHED"
            assert got.error == tr("svc_jobs.error.crashed")

            # Visible via list() too — just as via /api/jobs
            ids = [j.id for j in queue2.list()]
            assert job.id in ids

            # A repeated recovery call is idempotent (the row is already crashed,
            # so it again does not fall into the queued/running selection).
            assert queue2.recover_crashed() == []
        finally:
            executor2.shutdown(wait=True)
    finally:
        # Clean up the first queue's thread so as not to hold a resource between tests.
        release.set()
        job._future.result(timeout=5)
        executor1.shutdown(wait=True)
