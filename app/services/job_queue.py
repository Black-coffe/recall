"""JobQueue — обёртка над ThreadPoolExecutor с состояниями и cancel.

Каждый Job имеет:
  - id (UUID)
  - kind ('youtube_download' | 'transcription' | ...)
  - state (queued | running | completed | failed | cancelled | crashed)
  - created_at, started_at, finished_at
  - error (опционально)
  - meta (произвольный dict для UI: title, url, file_path, ...)

Cancel — кооперативный: внутри функции-задачи нужно проверять job.is_cancelled().

Персистентність (T2.1): стан job'ів дублюється у таблицю ``jobs`` (міграція
v26, ``app/db/migrations.py``) — dict у пам'яті лишається "гарячим" кешем
поверх БД (читання /api/jobs не ходить у SQLite на кожен запит), а БД —
джерело істини, що переживає рестарт/крах процесу. ``db_path`` не задано за
замовчуванням (None) — персистентність вимкнена (напр. у тестах, що не
піднімають повну БД); ``set_db_path()`` вмикає її явно (викликається з
app.py в тому самому місці, де recording робить свій recovery). Запис у БД —
best-effort: помилка персистентності ніколи не валить сам job (лише
warning) — dict-кеш лишається джерелом правди для поточного процесу.
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
        # Не використовуємо asdict() — він робить deepcopy, що падає на
        # threading.Event/Future. Збираємо вручну з потрібних полів.
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
    """Реестр задач + удобный submit() поверх executor'а(ов).

    T2.5 (Волна 2): раніше всі job'и (транскрипція-суміжні, backfill,
    doc_import, recording_finalize, ...) йшли крізь ОДИН
    ``ThreadPoolExecutor(max_workers=2)`` — довгий batch (напр.
    enrichment_backfill по всьому архіву) міг зайняти обидва воркери
    надовго, і latency-критичний ``recording_finalize`` (юзер щойно
    натиснув "стоп" і чекає на готовий файл) чекав у черзі за ним.

    Тепер підтримується ДРУГИЙ, окремий ``live_executor`` для набору
    "live-critical" kind'ів (``live_kinds``) — типово лише
    ``recording_finalize``. Усі інші kind'и, як і раніше, ідуть у основний
    ``executor``. Реєстр job'ів (``self._jobs``), listing/get/cancel/
    персистентність — СПІЛЬНІ для обох пулів (єдине джерело правди для
    /api/jobs), змінюється лише те, У ЯКИЙ executor.submit() потрапляє
    задача.
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
        """Запустить задачу. fn получает первым аргументом job, далее переданные args/kwargs."""
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
        # Свежие сверху
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
        # Если future ещё не стартовал — Future.cancel() может вернуть True.
        if job._future is not None:
            cancelled = job._future.cancel()
            if cancelled:
                job.state = "cancelled"
                job.finished_at = time.time()
                self._persist(job)
        return True

    # -----------------------------------------------------
    # Persistence (T2.1) — таблиця jobs, міграція v26
    # -----------------------------------------------------

    def set_db_path(self, db_path: str) -> None:
        """Вмикає персистентність у SQLite. Викликається один раз з app.py
        (start-up, після init_database()) — до цього виклику submit()/cancel()
        працюють як і раніше, лише в пам'яті (db_path=None за замовчуванням)."""
        self._db_path = db_path

    def _persist(self, job: Job) -> None:
        """Best-effort upsert поточного стану job'а у таблицю jobs.

        Ніколи не кидає — помилка запису в БД (locked/відсутня таблиця у
        старих тестових БД без міграцій) не має ламати фонову задачу, лише
        логується warning'ом. dict (self._jobs) лишається джерелом правди
        для живого процесу незалежно від успіху персистентності.
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
            logger.warning("JobQueue: persist job %s не вдався: %s", job.id, e)

    def recover_crashed(self) -> List[str]:
        """Старт-логіка recovery (T2.1) — аналог recording
        ``SessionStore.recover_orphaned()``: усі job'и, які на момент
        попереднього завершення процесу мали state IN ('queued','running'),
        не можуть бути завершені (executor/потоки не пережили рестарт) — тож
        позначаються 'crashed' у БД і підвантажуються у dict-кеш, щоб одразу
        бути видимими через /api/jobs, а не мовчки зникнути.

        Викликається один раз при старті app.py, ПІСЛЯ ``set_db_path()`` і
        ПІСЛЯ ``init_database()`` (таблиця jobs мусить вже існувати).
        Повертає список job_id, які потрапили в recovery (для логування,
        як ``recording_service.recover_orphaned()``).
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
            logger.warning("JobQueue: recover_crashed не вдався: %s", e)
        return recovered

    # -----------------------------------------------------
    # Internal
    # -----------------------------------------------------

    def _cleanup_locked(self):
        """Удаляет старые завершённые job'ы при переполнении (под уже взятым _lock)."""
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
