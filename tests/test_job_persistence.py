"""Тести для персистентності JobQueue (T2.1, REMEDIATION_PLAN Волна 1).

Перевіряють, що:
  - без db_path JobQueue поводиться як і раніше (чиста пам'ять, тести
    tests/test_job_queue.py не ламаються);
  - зі встановленим db_path стан job'а дублюється у таблицю ``jobs``
    (міграція v26, app/db/migrations.py);
  - "рестарт застосунку" (нова JobQueue з порожнім dict-кешем, та сама БД)
    через recover_crashed() позначає незавершені job'и як 'crashed' і
    робить їх одразу видимими через list()/get() — так само, як recording
    SessionStore.recover_orphaned() робить для сесій запису.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.job_queue import JobQueue


def test_recover_crashed_noop_without_db_path():
    """Без set_db_path/конструктор-параметра — поведінка як до T2.1."""
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
        # _persist у finally-блоці _runner виконується синхронно перед
        # поверненням з потоку задачі, але future.result() гарантує лише
        # завершення самої функції — даємо невеликий запас на flush.
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

        assert row is not None, "job не потрапив у таблицю jobs"
        assert row["kind"] == "youtube_download"
        assert row["state"] == "completed"
        assert "т" in (row["meta_json"] or "")
    finally:
        executor.shutdown(wait=True)


def test_recover_crashed_marks_running_job_and_makes_it_visible(tmp_path: Path):
    """Емулює: job був 'running' у процесі, який упав, потім рестарт."""
    db = str(tmp_path / "test.db")
    init_database(db)

    executor1 = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    started = threading.Event()

    def long_job(job):
        started.set()
        # Тримаємо job у стані 'running', доки тест явно не відпустить —
        # імітує довгу транскрипцію, перервану крахом процесу.
        release.wait(timeout=5)
        return "done"

    try:
        queue1 = JobQueue(executor=executor1, db_path=db)
        job = queue1.submit(
            "transcription", long_job, meta={"file": "call.mp3"}
        )
        assert started.wait(timeout=5), "job не стартував вчасно"

        # Дочекатись, поки _persist(state='running') реально запишеться в БД
        # (виконується в тому ж потоці задачі одразу після job.state='running').
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
        assert persisted_running, "стан 'running' не зафіксувався в jobs до 'рестарту'"

        # --- "Рестарт застосунку" ---
        # Нова JobQueue з порожнім dict-кешем, та сама SQLite БД — рівно
        # так, як після kill+перезапуску процесу app.py.
        executor2 = ThreadPoolExecutor(max_workers=1)
        try:
            queue2 = JobQueue(executor=executor2, db_path=db)
            assert queue2.get(job.id) is None  # порожній кеш до recovery

            recovered = queue2.recover_crashed()
            assert job.id in recovered

            got = queue2.get(job.id)
            assert got is not None
            assert got.state == "crashed"
            assert got.error

            # Видимо і через list() — так само, як через /api/jobs
            ids = [j.id for j in queue2.list()]
            assert job.id in ids

            # Повторний виклик recovery — ідемпотентний (рядок вже crashed,
            # тому знову не потрапляє у вибірку queued/running).
            assert queue2.recover_crashed() == []
        finally:
            executor2.shutdown(wait=True)
    finally:
        # Прибираємо потік першої черги, щоб не тримати ресурс між тестами.
        release.set()
        job._future.result(timeout=5)
        executor1.shutdown(wait=True)
