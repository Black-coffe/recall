"""JobQueue — a wrapper over ThreadPoolExecutor with states and cancel.

Each Job has:
  - id (UUID)
  - kind ('youtube_download' | 'transcription' | ...)
  - state (queued | running | completed | failed | cancelled | crashed)
  - created_at, started_at, finished_at
  - error (optional)
  - meta (arbitrary dict for the UI: title, url, file_path, ...)

Cancel is cooperative: the task function must check job.is_cancelled().

Persistence (T2.1): job state is mirrored into the ``jobs`` table (migration
v26, ``app/db/migrations.py``) — the in-memory dict stays a "hot" cache
on top of the DB (reading /api/jobs does not hit SQLite on every request), while the DB is
the source of truth that survives a process restart/crash. ``db_path`` is not set by
default (None) — persistence is off (e.g. in tests that do not
bring up a full DB); ``set_db_path()`` turns it on explicitly (called from
app.py in the same place where recording does its recovery). The DB write is
best-effort: a persistence error never fails the job itself (only a
warning) — the dict cache stays the source of truth for the current process.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


JOB_STATES = ("queued", "running", "completed", "failed", "cancelled", "crashed")


@dataclass
class Job:
    id: str
    kind: str
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _future: Optional[Future] = field(default=None, repr=False, compare=False)

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def to_dict(self) -> Dict[str, Any]:
        # We do not use asdict() — it does a deepcopy, which fails on
        # threading.Event/Future. We collect manually from the needed fields.
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "meta": dict(self.meta) if self.meta else {},
        }


class JobQueue:
    """Job registry + a convenient submit() on top of the executor(s).

    T2.5 (Wave 2): previously all jobs (transcription-related, backfill,
    doc_import, recording_finalize, ...) went through ONE
    ``ThreadPoolExecutor(max_workers=2)`` — a long batch (e.g.
    enrichment_backfill over the whole archive) could occupy both workers
    for a long time, and the latency-critical ``recording_finalize`` (the user has just
    pressed "stop" and is waiting for the finished file) waited in the queue behind it.

    Now a SECOND, separate ``live_executor`` is supported for a set of
    "live-critical" kinds (``live_kinds``) — by default only
    ``recording_finalize``. All other kinds still go to the main
    ``executor``. The job registry (``self._jobs``), listing/get/cancel/
    persistence — are SHARED by both pools (a single source of truth for
    /api/jobs); the only thing that changes is WHICH executor.submit() the
    task goes to.
    """

    def __init__(self, executor, max_history: int = 200, db_path: Optional[str] = None,
                 live_executor: Optional[Any] = None,
                 live_kinds: Optional[frozenset] = None):
        self._executor = executor
        self._live_executor = live_executor
        self._live_kinds = live_kinds or frozenset()
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._max_history = max_history
        self._db_path = db_path

    def _executor_for_kind(self, kind: str):
        if self._live_executor is not None and kind in self._live_kinds:
            return self._live_executor
        return self._executor

    # -----------------------------------------------------
    # Submit / lookup
    # -----------------------------------------------------

    def submit(self, kind: str, fn: Callable, *args, job_id: Optional[str] = None, meta: Optional[Dict] = None, **kwargs) -> Job:
        """Starts a task. fn receives the job as its first argument, then the passed args/kwargs."""
        jid = job_id or str(uuid.uuid4())[:20]
        job = Job(id=jid, kind=kind, meta=meta or {})

        with self._lock:
            self._jobs[jid] = job
            self._cleanup_locked()
        self._persist(job)

        def _runner():
            job.started_at = time.time()
            job.state = "running"
            self._persist(job)
            try:
                if job.is_cancelled():
                    job.state = "cancelled"
                    self._persist(job)
                    return None
                result = fn(job, *args, **kwargs)
                if job.is_cancelled():
                    job.state = "cancelled"
                else:
                    job.state = "completed"
                return result
            except Exception as e:
                job.state = "failed"
                job.error = str(e)
                logger.error(f"[Job {jid}] failed: {e}", exc_info=True)
                raise
            finally:
                job.finished_at = time.time()
                self._persist(job)

        job._future = self._executor_for_kind(kind).submit(_runner)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, kind: Optional[str] = None, active_only: bool = False) -> List[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        if active_only:
            jobs = [j for j in jobs if j.state in ("queued", "running")]
        # Newest first
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return False
        if job.state in ("completed", "failed", "cancelled"):
            return False
        job._cancel_event.set()
        # If the future has not started yet — Future.cancel() may return True.
        if job._future is not None:
            cancelled = job._future.cancel()
            if cancelled:
                job.state = "cancelled"
                job.finished_at = time.time()
                self._persist(job)
        return True

    # -----------------------------------------------------
    # Persistence (T2.1) — jobs table, migration v26
    # -----------------------------------------------------

    def set_db_path(self, db_path: str) -> None:
        """Turns on persistence in SQLite. Called once from app.py
        (start-up, after init_database()) — until this call submit()/cancel()
        work as before, in memory only (db_path=None by default)."""
        self._db_path = db_path

    def _persist(self, job: Job) -> None:
        """Best-effort upsert of the job's current state into the jobs table.

        Never raises — a DB write error (locked/missing table in
        old test DBs without migrations) must not break the background task, it is only
        logged as a warning. The dict (self._jobs) stays the source of truth
        for the live process regardless of persistence success.
        """
        if not self._db_path:
            return
        try:
            from app.db.connection import get_db_connection
            meta_json = json.dumps(job.meta, ensure_ascii=False) if job.meta else None
            with get_db_connection(self._db_path) as conn:
                conn.execute(
                    '''INSERT INTO jobs
                           (id, kind, state, created_at, started_at, finished_at,
                            error, meta_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           state = excluded.state,
                           started_at = excluded.started_at,
                           finished_at = excluded.finished_at,
                           error = excluded.error,
                           meta_json = excluded.meta_json,
                           updated_at = excluded.updated_at''',
                    (
                        job.id, job.kind, job.state, job.created_at, job.started_at,
                        job.finished_at, job.error, meta_json, time.time(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning("JobQueue: persist job %s failed: %s", job.id, e)

    def recover_crashed(self) -> List[str]:
        """Start-up recovery logic (T2.1) — analogous to recording
        ``SessionStore.recover_orphaned()``: all jobs that, at the moment of
        the previous process shutdown, had state IN ('queued','running')
        cannot be completed (executor/threads did not survive the restart) — so they
        are marked 'crashed' in the DB and loaded into the dict cache so they are immediately
        visible via /api/jobs instead of silently vanishing.

        Called once at app.py startup, AFTER ``set_db_path()`` and
        AFTER ``init_database()`` (the jobs table must already exist).
        Returns the list of job_ids that went through recovery (for logging,
        like ``recording_service.recover_orphaned()``).
        """
        if not self._db_path:
            return []
        recovered: List[str] = []
        try:
            from app.db.connection import get_db_connection
            now = time.time()
            error_msg = "Перервано рестартом застосунку"
            with get_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT id, kind, state, created_at, started_at, finished_at, "
                    "error, meta_json FROM jobs WHERE state IN ('queued', 'running')"
                ).fetchall()
                for row in rows:
                    jid = row['id']
                    conn.execute(
                        "UPDATE jobs SET state='crashed', finished_at=?, error=?, "
                        "updated_at=? WHERE id=?",
                        (now, error_msg, now, jid),
                    )
                    try:
                        meta = json.loads(row['meta_json']) if row['meta_json'] else {}
                    except (TypeError, ValueError):
                        meta = {}
                    job = Job(
                        id=jid,
                        kind=row['kind'],
                        state='crashed',
                        created_at=row['created_at'] or now,
                        started_at=row['started_at'],
                        finished_at=now,
                        error=error_msg,
                        meta=meta,
                    )
                    with self._lock:
                        self._jobs[jid] = job
                    recovered.append(jid)
                conn.commit()
        except Exception as e:
            logger.warning("JobQueue: recover_crashed failed: %s", e)
        return recovered

    # -----------------------------------------------------
    # Internal
    # -----------------------------------------------------

    def _cleanup_locked(self):
        """Removes old finished jobs on overflow (with _lock already held)."""
        if len(self._jobs) <= self._max_history:
            return
        finished = [
            (jid, j) for jid, j in self._jobs.items()
            if j.state in ("completed", "failed", "cancelled", "crashed")
        ]
        finished.sort(key=lambda x: x[1].finished_at or x[1].created_at)
        to_remove = len(self._jobs) - self._max_history
        for jid, _ in finished[:to_remove]:
            del self._jobs[jid]
