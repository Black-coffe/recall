"""Тести офлайн-проходу re-embed (app/services/reembed.py) — Волна B, історія 05.

Офлайн, без torch/GPU: `embeddings.is_available` і `embed_texts` підмінені
фейком (нулі потрібної розмірності), `optimize_chunk_index` — лічильником.
Перевіряємо вибірку застарілих записів, ідемпотентність проходу, гард бойової
БД і код виходу CLI.
"""
from __future__ import annotations

import logging
import sqlite3
import time

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import embeddings, reembed


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "snapshot.db")
    init_database(path)
    return path


@pytest.fixture
def fake_embedder(monkeypatch):
    """Модель не вантажиться: ембедер віддає нулі, але памʼятає, що бачив."""
    seen: list[list[str]] = []

    def _fake(texts, batch_size=32):
        seen.append(list(texts))
        return np.zeros((len(texts), embeddings.EMBED_DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake)
    return seen


@pytest.fixture
def optimize_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(reembed, "optimize_chunk_index", lambda p: calls.append(p))
    return calls


def _add_tx(path, text="Обговорили кошторис і терміни здачі проєкту.",
            model="старий/ембедер", version=1, deleted_at=None, duplicate_of=None,
            source_name="Планерка"):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "embedding_model, embedding_version, deleted_at, duplicate_of) "
        "VALUES ('file', ?, ?, ?, ?, ?, ?)",
        (source_name, text, model, version, deleted_at, duplicate_of))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


# ============================================================
# Вибірка застарілих
# ============================================================

def test_stale_ids_picks_other_epoch_and_null_pair(db):
    stale_model = _add_tx(db, model="старий/ембедер", version=embeddings.EMBED_VERSION)
    stale_version = _add_tx(db, model=embeddings.EMBED_MODEL, version=1)
    never = _add_tx(db, model=None, version=None)
    current = _add_tx(db, model=embeddings.EMBED_MODEL, version=embeddings.EMBED_VERSION)

    ids = reembed.stale_ids(db)
    assert ids == sorted([stale_model, stale_version, never])
    assert current not in ids


def test_stale_ids_skips_deleted_duplicates_and_empty_text(db):
    _add_tx(db, deleted_at=1700000000)
    keeper = _add_tx(db)
    _add_tx(db, duplicate_of=keeper)
    _add_tx(db, text="   ")
    assert reembed.stale_ids(db) == [keeper]


def test_stale_ids_respects_limit(db):
    first = _add_tx(db)
    _add_tx(db)
    assert reembed.stale_ids(db, limit=1) == [first]


# ============================================================
# dry-run
# ============================================================

def test_dry_run_counts_records_and_chars_without_writing(db, fake_embedder):
    body = "Кирилиця у тексті запису про бюджет."
    _add_tx(db, text=body)
    res = reembed.run(db, dry_run=True)
    assert res["dry_run"] is True
    assert res["total"] == 1
    assert res["chars"] == len(body)
    assert res["model"] == embeddings.EMBED_MODEL

    conn = sqlite3.connect(db)
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    assert chunks == 0
    assert fake_embedder == []


# ============================================================
# run
# ============================================================

def test_run_reembeds_stale_and_is_idempotent(db, fake_embedder, optimize_calls):
    stale = _add_tx(db)
    current = _add_tx(db, model=embeddings.EMBED_MODEL, version=embeddings.EMBED_VERSION)

    res = reembed.run(db)
    assert res["done"] == 1 and res["skipped"] == 0 and res["failed"] == 0
    assert res["optimized"] is True
    assert optimize_calls == [db]          # рівно один раз

    conn = sqlite3.connect(db)
    rows = dict(conn.execute(
        "SELECT id, embedding_model FROM transcriptions").fetchall())
    reembedded = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE transcription_id = ?", (stale,)).fetchone()[0]
    untouched = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE transcription_id = ?", (current,)).fetchone()[0]
    conn.close()
    assert rows[stale] == embeddings.EMBED_MODEL
    assert reembedded > 0 and untouched == 0

    # повторний прохід — роботи немає, optimize не викликається вдруге
    second = reembed.run(db)
    assert second["total"] == 0 and second["done"] == 0
    assert second["optimized"] is False
    assert optimize_calls == [db]


def test_run_writes_context_prefix_into_chunks(db, fake_embedder, optimize_calls):
    """Прохід переносить архів на нову пару І дає чанкам контекстний префікс
    одним рухом — заради цього він і робиться на знімку."""
    tid = _add_tx(db, source_name="Планерка Фонду")
    reembed.run(db)

    conn = sqlite3.connect(db)
    prefix = conn.execute(
        "SELECT context_prefix FROM chunks WHERE transcription_id = ?", (tid,)).fetchone()[0]
    conn.close()
    assert prefix.startswith("[дзвінок] Планерка Фонду")
    assert fake_embedder and fake_embedder[0][0].startswith(prefix)


def test_run_limit_leaves_rest_for_next_pass(db, fake_embedder, optimize_calls):
    _add_tx(db)
    _add_tx(db)
    res = reembed.run(db, limit=1)
    assert res["done"] == 1
    assert len(reembed.stale_ids(db)) == 1


def test_run_does_not_optimize_when_nothing_done(db, fake_embedder, optimize_calls):
    _add_tx(db, model=embeddings.EMBED_MODEL, version=embeddings.EMBED_VERSION)
    res = reembed.run(db)
    assert res["done"] == 0 and res["optimized"] is False
    assert optimize_calls == []


def test_run_survives_one_broken_record(db, fake_embedder, optimize_calls, monkeypatch):
    bad = _add_tx(db)
    good = _add_tx(db)
    real = embeddings.chunk_and_embed_transcription

    def _flaky(db_path, tid, force=False):
        if tid == bad:
            raise RuntimeError("зламаний запис")
        return real(db_path, tid, force=force)

    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription", _flaky)
    res = reembed.run(db)
    assert res["failed"] == 1 and res["done"] == 1
    assert reembed.stale_ids(db) == [bad]
    assert good not in reembed.stale_ids(db)


def test_run_without_embeddings_reports_unavailable(db, monkeypatch, optimize_calls):
    _add_tx(db)
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    res = reembed.run(db)
    assert res["status"] == "unavailable"
    assert res["done"] == 0 and optimize_calls == []


# ============================================================
# Гард бойової БД + CLI
# ============================================================

def test_is_live_db_compares_absolute_paths(db, monkeypatch):
    monkeypatch.setattr(reembed, "_live_db_path", lambda: str(__import__("pathlib")
                                                              .Path(db).resolve()))
    assert reembed.is_live_db(db) is True
    assert reembed.is_live_db(db + ".copy") is False


def test_cli_refuses_live_db_without_yes_live(db, monkeypatch, fake_embedder, capsys):
    monkeypatch.setattr(reembed, "is_live_db", lambda p: True)
    rc = reembed.main(["run", "--db", db])
    assert rc == 2
    assert "Відмова" in capsys.readouterr().err

    conn = sqlite3.connect(db)
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    assert chunks == 0


def test_cli_allows_live_db_with_yes_live(db, monkeypatch, fake_embedder, optimize_calls):
    monkeypatch.setattr(reembed, "is_live_db", lambda p: True)
    _add_tx(db)
    assert reembed.main(["run", "--db", db, "--yes-live"]) == 0
    assert optimize_calls == [db]


def test_cli_missing_db_returns_2(tmp_path, capsys):
    rc = reembed.main(["run", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "не знайдено" in capsys.readouterr().err


def test_cli_dry_run_accepted_before_and_after_subcommand(db, fake_embedder, capsys):
    _add_tx(db)
    assert reembed.main(["--dry-run", "run", "--db", db]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert reembed.main(["run", "--db", db, "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    conn.close()


def test_cli_prints_pair_and_comment_reminder(db, fake_embedder, optimize_calls, capsys):
    _add_tx(db)
    assert reembed.main(["run", "--db", db]) == 0
    out = capsys.readouterr().out
    assert embeddings.EMBED_MODEL in out
    assert "comments reindex" in out


# ============================================================
# Міграція v43: старі рядки з NULL-префіксом далі знаходяться
# ============================================================

def test_legacy_chunks_without_prefix_stay_searchable(db):
    """Перебудова chunks_fts не має «загубити» наявний архів: рядок із
    NULL `context_prefix` знаходиться за своїм текстом як і раніше."""
    tid = _add_tx(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, text) VALUES (?, 0, ?)",
        (tid, "домовились про кошторис на вересень"))
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", ('"кошторис"',)).fetchall()
    conn.close()
    assert len(hits) == 1


def _downgrade_to_pre_v43(path: str) -> None:
    """Відкотити БД у стан до v43: одноколонковий `chunks_fts` зі старими
    тригерами і без запису версії. Колонку `chunks.context_prefix` лишаємо
    (SQLite не вміє DROP COLUMN у старих версіях, а міграція й так перевіряє
    наявність окремо) — важливо саме те, що ІНДЕКС старої форми."""
    conn = sqlite3.connect(path)
    for trg in ("chunks_ai", "chunks_ad", "chunks_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    conn.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5("
                 "text, content='chunks', content_rowid='id', "
                 "tokenize='unicode61 remove_diacritics 1')")
    conn.execute("CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN "
                 "INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END")
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    # Версія БД рахується як МАКСИМУМ у schema_versions, тож знести треба всі
    # записи від 43 і вище — інакше поява наступної міграції (v44) лишає
    # current_version=44, гілка v43 не виконується, і тест перевіряє порожнечу.
    conn.execute("DELETE FROM schema_versions WHERE version >= 43")
    conn.commit()
    conn.close()


def test_migration_v43_rebuilds_index_over_existing_rows(db):
    """Апгрейд наявної БД: рядки, що вже лежали в chunks, після перебудови
    індексу мають знаходитись і за текстом (як раніше), і за новим префіксом."""
    tid = _add_tx(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, text, context_prefix) "
        "VALUES (?, 0, ?, ?)",
        (tid, "домовились про кошторис", "[переписка] Барселона · 2026-09-16"))
    conn.commit()
    conn.close()

    _downgrade_to_pre_v43(db)
    conn = sqlite3.connect(db)
    # до міграції термін із префікса не шукається — індекс його не знає
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                        ('"Барселона"',)).fetchone()[0] == 0
    conn.close()

    init_database(db)

    conn = sqlite3.connect(db)
    by_text = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                           ('"кошторис"',)).fetchone()[0]
    by_prefix = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                             ('"Барселона"',)).fetchone()[0]
    conn.close()
    assert by_text == 1 and by_prefix == 1


def test_chunk_delete_and_update_keep_fts_in_sync(db):
    """Тригери переписані на дві колонки — `delete` має йти зі СТАРИМИ
    значеннями, інакше в індексі лишається привид після UPDATE/DELETE."""
    tid = _add_tx(db)
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, text, context_prefix) "
        "VALUES (?, 0, ?, ?)", (tid, "оплата у пʼятницю", "[дзвінок] Стара назва"))
    cid = cur.lastrowid
    conn.execute("UPDATE chunks SET context_prefix = ?, text = ? WHERE id = ?",
                 ("[дзвінок] Нова назва", "оплата у понеділок", cid))
    conn.commit()

    def _n(term):
        return conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                            (f'"{term}"',)).fetchone()[0]

    assert _n("Стара") == 0 and _n("пʼятницю") == 0
    assert _n("Нова") == 1 and _n("понеділок") == 1

    conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
    conn.commit()
    assert _n("Нова") == 0 and _n("понеділок") == 0
    conn.close()


def test_migration_v43_is_idempotent(db):
    """Повторний init_database не перебудовує індекс і не втрачає рядки."""
    tid = _add_tx(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, text, context_prefix) "
        "VALUES (?, 0, ?, ?)", (tid, "оплата у пʼятницю", "[дзвінок] Планерка"))
    conn.commit()
    conn.close()

    init_database(db)

    conn = sqlite3.connect(db)
    by_text = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
                           ('"оплата"',)).fetchall()
    by_prefix = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
                             ('"Планерка"',)).fetchall()
    versions = conn.execute("SELECT COUNT(*) FROM schema_versions WHERE version = 43").fetchone()[0]
    conn.close()
    assert len(by_text) == 1 and len(by_prefix) == 1 and versions == 1


def test_optimize_chunk_index_works_after_rebuild(db, fake_embedder):
    """`optimize` на перебудованому індексі має проходити мовчки — саме він
    рятує пошук після масового проходу (9с → 64с без нього)."""
    from app.services.enrichment import optimize_chunk_index

    _add_tx(db)
    reembed.run(db)
    optimize_chunk_index(db)

    conn = sqlite3.connect(db)
    hits = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
                        ('"кошторис"',)).fetchall()
    conn.close()
    assert hits


# ============================================================
# editable-title-description-03: точковий re-embed одного запису
# ============================================================

class FakeJob:
    def __init__(self, kind, meta):
        self.kind = kind
        self.meta = meta

    def is_cancelled(self):
        return False


class FakeQueue:
    """Черга, що виконує задачу одразу і памʼятає, з чим її покликали."""

    def __init__(self):
        self.calls = []

    def submit(self, kind, fn, *args, meta=None, **kwargs):
        job = FakeJob(kind, meta or {})
        self.calls.append({"kind": kind, "args": args, "meta": job.meta})
        self.result = fn(job, *args, **kwargs)
        return job


class FakeRecorder:
    """Живі сесії — `sids`; `active_session_id` навмисно відсутній: при двох
    записах він None, тож прапорець мусить читати `active_session_ids()`."""

    def __init__(self, active=False):
        self.sids = ["sess-1"] if active else []

    def active_session_ids(self):
        return list(self.sids)


@pytest.fixture
def scheduler_env(monkeypatch):
    """Фейкова черга + відсутній запис + доступні ембединги; набір очікуючих
    id чистимо, щоб тести не текли один в одного."""
    from app import state

    queue = FakeQueue()
    recorder = FakeRecorder(active=False)
    monkeypatch.setattr(state, "job_queue", queue, raising=False)
    monkeypatch.setattr(state, "recording_service", recorder, raising=False)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(reembed, "_DEFER_SECONDS", 0.05)
    reembed._pending_ids.clear()
    yield queue, recorder
    reembed._pending_ids.clear()


def test_schedule_record_reembed_queues_per_record_embed(db, scheduler_env, monkeypatch):
    queue, _ = scheduler_env
    seen = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: seen.append((path, tid, force))
                        or {"status": "embedded"})
    tid = _add_tx(db, source_name="Планерка Фонду")

    assert reembed.schedule_record_reembed(tid, db_path=db) == "queued"
    assert queue.calls[0]["kind"] == "reembed_record"
    assert queue.calls[0]["meta"]["transcription_id"] == tid
    # шлях БД їде і в аргументах задачі, і в meta — саме той, що передали
    assert queue.calls[0]["args"] == (tid, db)
    assert queue.calls[0]["meta"]["db_path"] == db
    # force=True обовʼязковий: пара (модель, версія) не змінилась, без нього
    # запис вважався б уже обробленим і новий префікс не доїхав би у вектори.
    assert seen == [(db, tid, True)]


def test_schedule_record_reembed_defers_while_recording(db, scheduler_env, monkeypatch):
    queue, recorder = scheduler_env
    calls = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: calls.append(tid)
                        or {"status": "embedded"})
    recorder.sids = ["sess-live"]
    tid = _add_tx(db)

    assert reembed.schedule_record_reembed(tid, db_path=db) == "deferred"
    assert queue.calls == [] and calls == []          # слот черги не зайнятий
    # другий виклик під час очікування не плодить другої задачі
    assert reembed.schedule_record_reembed(tid, db_path=db) == "pending"

    recorder.sids = []
    deadline = time.time() + 3.0
    while not queue.calls and time.time() < deadline:
        time.sleep(0.02)
    assert [c["meta"]["transcription_id"] for c in queue.calls] == [tid]
    assert calls == [tid]
    assert tid not in reembed._pending_ids


def test_recording_active_with_two_live_sessions(scheduler_env):
    """ADR-022 відкладення: два живі записи — теж «запис триває»."""
    _, recorder = scheduler_env
    assert reembed._recording_active() is False
    recorder.sids = ["rec_a", "rec_b"]
    assert reembed._recording_active() is True
    recorder.sids = []
    assert reembed._recording_active() is False


def test_schedule_record_reembed_skips_without_torch(db, scheduler_env, monkeypatch):
    queue, _ = scheduler_env
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    monkeypatch.setattr(embeddings, "unavailability_reason", lambda: "torch не встановлено")
    assert reembed.schedule_record_reembed(_add_tx(db), db_path=db) == "skipped"
    assert queue.calls == []


def test_schedule_record_reembed_skips_without_queue(db, scheduler_env, monkeypatch):
    from app import state
    monkeypatch.setattr(state, "job_queue", None, raising=False)
    tid = _add_tx(db)
    assert reembed.schedule_record_reembed(tid, db_path=db) == "skipped"
    assert tid not in reembed._pending_ids


def test_after_meta_update_schedules_only_on_real_change(monkeypatch, db):
    from app.services import record_meta

    seen = []
    monkeypatch.setattr(reembed, "schedule_record_reembed",
                        lambda tid, **kw: seen.append((tid, kw)) or "queued")
    record_meta.after_meta_update(77, changed=True, db_path=db)
    assert seen == [(77, {"db_path": db})]
    record_meta.after_meta_update(78, changed=False, db_path=db)
    assert seen == [(77, {"db_path": db})]


def test_schedule_record_reembed_requires_db_path(db, scheduler_env):
    """Дефолту «None = бойова БД» немає: без шляху — TypeError, не тиха бойова."""
    queue, _ = scheduler_env
    with pytest.raises(TypeError):
        reembed.schedule_record_reembed(_add_tx(db))
    assert queue.calls == []


class SlowQueue:
    """Черга, що НЕ стартує задачу одразу — задачу запускає тест вручну."""

    def __init__(self):
        self.jobs = []

    def submit(self, kind, fn, *args, meta=None, **kwargs):
        job = FakeJob(kind, meta or {})
        self.jobs.append((job, fn, args, kwargs))
        return job

    def run_next(self):
        job, fn, args, kwargs = self.jobs.pop(0)
        return fn(job, *args, **kwargs)


def test_pending_is_released_when_job_starts_not_at_submit(db, scheduler_env, monkeypatch):
    """Виклик, що отримав `"pending"`, покритий задачею, яка ще не читала рядок.

    Id знімається з `_pending_ids` на старті тіла задачі: доки задача не
    почалась, другий виклик отримує `"pending"` (і це правда — його правку
    задача ще побачить), а перший виклик ПІСЛЯ старту читання дає нову задачу.
    """
    from app import state

    queue = SlowQueue()
    monkeypatch.setattr(state, "job_queue", queue, raising=False)
    reads = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: reads.append((path, tid))
                        or {"status": "embedded"})
    tid = _add_tx(db, source_name="Планерка Фонду")

    assert reembed.schedule_record_reembed(tid, db_path=db) == "queued"
    assert reembed.schedule_record_reembed(tid, db_path=db) == "pending"
    assert len(queue.jobs) == 1 and reads == []

    queue.run_next()                       # задача стартувала і прочитала рядок
    assert reads == [(db, tid)]
    assert tid not in reembed._pending_ids

    assert reembed.schedule_record_reembed(tid, db_path=db) == "queued"
    assert len(queue.jobs) == 1


# ============================================================
# directions-projects-tags-01: точковий re-embed СПИСКУ (C3)
# ============================================================

def _make_current(path, tid):
    """Перевести запис у поточну епоху і дати йому чанк (інакше він `stale`)."""
    conn = sqlite3.connect(path)
    conn.execute("UPDATE transcriptions SET embedding_model = ?, embedding_version = ? "
                 "WHERE id = ?", (embeddings.EMBED_MODEL, embeddings.EMBED_VERSION, tid))
    conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) "
                 "VALUES (?, 0, 'кошторис')", (tid,))
    conn.commit()
    conn.close()
    return tid


def _add_current(path, **kw):
    return _make_current(path, _add_tx(path, **kw))


def test_schedule_records_reembed_queues_one_task_per_record(db, scheduler_env, monkeypatch):
    queue, _ = scheduler_env
    seen = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: seen.append((path, tid, force))
                        or {"status": "embedded"})
    ids = [_add_current(db) for _ in range(3)]

    res = reembed.schedule_records_reembed(ids, db_path=db, reason="category_bulk")
    assert res == {"mode": "point", "count": 3, "stale": 0}
    assert [c["kind"] for c in queue.calls] == ["reembed_record"] * 3
    assert [c["meta"]["transcription_id"] for c in queue.calls] == ids
    assert seen == [(db, tid, True) for tid in ids]


def test_schedule_records_reembed_skips_ids_from_another_epoch(db, scheduler_env, monkeypatch):
    queue, _ = scheduler_env
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: {"status": "embedded"})
    live = _add_current(db)
    old_pair = _add_tx(db, model="старий/ембедер", version=1)
    never_embedded = _add_tx(db, model=embeddings.EMBED_MODEL,
                             version=embeddings.EMBED_VERSION)   # пара є, чанків немає

    res = reembed.schedule_records_reembed([live, old_pair, never_embedded],
                                           db_path=db, reason="tags_changed")
    assert res == {"mode": "point", "count": 1, "stale": 2}
    assert [c["meta"]["transcription_id"] for c in queue.calls] == [live]


def test_schedule_records_reembed_batches_above_threshold(db, scheduler_env,
                                                          optimize_calls, monkeypatch):
    queue, _ = scheduler_env
    seen = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: seen.append((tid, force))
                        or {"status": "embedded"})
    ids = [_add_current(db) for _ in range(reembed._BATCH_THRESHOLD + 1)]

    res = reembed.schedule_records_reembed(ids, db_path=db, reason="category_tree_changed")
    assert res == {"mode": "batch", "count": len(ids), "stale": 0}
    assert [c["kind"] for c in queue.calls] == ["reembed_batch"]      # рівно одна задача
    assert queue.calls[0]["meta"]["ids"] == ids
    assert queue.calls[0]["meta"]["db_path"] == db
    assert queue.calls[0]["meta"]["reason"] == "category_tree_changed"
    assert [tid for tid, _ in seen] == ids
    # масовий DELETE+INSERT по chunks кришить chunks_fts — optimize рівно раз
    assert optimize_calls == [db]
    assert not (set(ids) & reembed._pending_ids)


def test_batch_releases_each_pending_id_before_reading_its_row(db, scheduler_env,
                                                               optimize_calls, monkeypatch):
    """Id знімається з `_pending_ids` ДО читання рядка — інакше виклик, що
    отримав «уже заплановано», спирався б на задачу зі старими даними."""
    from app import state

    queue = SlowQueue()
    monkeypatch.setattr(state, "job_queue", queue, raising=False)
    pending_at_read = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: pending_at_read.append(
                            (tid, tid in reembed._pending_ids)) or {"status": "embedded"})
    ids = [_add_current(db) for _ in range(reembed._BATCH_THRESHOLD + 1)]

    reembed.schedule_records_reembed(ids, db_path=db, reason="category_bulk")
    assert set(ids) <= reembed._pending_ids          # поки задача не стартувала
    assert pending_at_read == []

    queue.run_next()
    assert pending_at_read == [(tid, False) for tid in ids]
    assert not (set(ids) & reembed._pending_ids)


# ============================================================
# directions-projects-tags-15: `_pending_ids` відпускається при скасуванні/
# винятку; батч не дублює вже заплановані id
# ============================================================

class CancelAfterN:
    """`is_cancelled()` каже «ні» перші `n` разів, далі — «так»."""

    def __init__(self, n):
        self.n = n
        self.calls = 0

    def is_cancelled(self):
        self.calls += 1
        return self.calls > self.n


def test_batch_job_cancellation_releases_pending_tail(db, scheduler_env, optimize_calls,
                                                       monkeypatch):
    seen = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: seen.append(tid)
                        or {"status": "embedded"})
    ids = [_add_tx(db) for _ in range(25)]
    reembed._pending_ids.update(ids)

    result = reembed._batch_reembed_job(CancelAfterN(3), ids, db, "category_bulk")

    assert seen == ids[:3]
    assert result["done"] == 3
    assert optimize_calls == [db]
    assert reembed._pending_ids == set()

    # рестарту процесу не треба: наступний точковий виклик для необробленого
    # (4-го) id одразу створює задачу, а не повертає "pending"
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: {"status": "embedded"})
    assert reembed.schedule_record_reembed(ids[3], db_path=db) == "queued"


def test_batch_job_survives_one_broken_record_and_releases_pending(db, scheduler_env,
                                                                    optimize_calls, monkeypatch):
    ids = [_add_tx(db) for _ in range(5)]
    reembed._pending_ids.update(ids)
    seen = []

    def _fake(path, tid, force=False):
        seen.append(tid)
        if tid == ids[1]:
            raise RuntimeError("boom")
        return {"status": "embedded"}

    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription", _fake)

    result = reembed._batch_reembed_job(FakeJob("reembed_batch", {}), ids, db, "category_bulk")

    assert seen == ids                      # поганий запис не зупиняє решту
    assert result["done"] == 4 and result["failed"] == 1
    assert optimize_calls == [db]
    assert reembed._pending_ids == set()


def test_batch_job_releases_pending_tail_on_iteration_exception(db, scheduler_env,
                                                                 optimize_calls, monkeypatch):
    """Виняток не з `chunk_and_embed_transcription`, а з самого механізму
    циклу (тут — перевірка скасування) — `finally` усе одно відпускає хвіст."""
    ids = [_add_tx(db) for _ in range(5)]
    reembed._pending_ids.update(ids)
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: {"status": "embedded"})

    class PoisonedJob:
        def __init__(self):
            self.calls = 0

        def is_cancelled(self):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("queue fault")
            return False

    with pytest.raises(RuntimeError):
        reembed._batch_reembed_job(PoisonedJob(), ids, db, "category_bulk")

    assert reembed._pending_ids == set()
    assert optimize_calls == []             # виняток вийшов раніше за optimize


def test_schedule_records_reembed_batch_excludes_already_pending_ids(db, scheduler_env,
                                                                      monkeypatch):
    """5 з 25 живих id уже чекають на власну точкову задачу — батч бере лише
    решту 20, дубля точковій задачі не ставить."""
    from app import state

    queue = SlowQueue()
    monkeypatch.setattr(state, "job_queue", queue, raising=False)
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: {"status": "embedded"})
    ids = [_add_current(db) for _ in range(reembed._BATCH_THRESHOLD + 5)]
    already_pending = ids[:5]
    reembed._pending_ids.update(already_pending)

    res = reembed.schedule_records_reembed(ids, db_path=db, reason="category_bulk")
    assert res == {"mode": "batch", "count": 20, "stale": 0}
    assert len(queue.jobs) == 1
    submitted_ids = queue.jobs[0][2][0]
    assert submitted_ids == ids[5:]
    assert reembed._pending_ids == set(ids)          # кожен id рівно один раз


def test_schedule_records_reembed_batch_none_when_all_already_pending(db, scheduler_env,
                                                                       monkeypatch):
    queue, _ = scheduler_env
    ids = [_add_current(db) for _ in range(reembed._BATCH_THRESHOLD + 1)]
    reembed._pending_ids.update(ids)

    res = reembed.schedule_records_reembed(ids, db_path=db, reason="category_bulk")
    assert res == {"mode": "none", "count": 0, "stale": 0}
    assert queue.calls == []
    assert reembed._pending_ids == set(ids)          # нічого не чіпали


def test_schedule_records_reembed_defers_batch_while_recording(db, scheduler_env, monkeypatch):
    queue, recorder = scheduler_env
    calls = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: calls.append(tid)
                        or {"status": "embedded"})
    recorder.sids = ["sess-live"]
    ids = [_add_current(db) for _ in range(reembed._BATCH_THRESHOLD + 1)]

    res = reembed.schedule_records_reembed(ids, db_path=db, reason="category_bulk")
    assert res["mode"] == "batch"
    assert queue.calls == [] and calls == []          # слот черги не зайнятий

    recorder.sids = []
    deadline = time.time() + 3.0
    while not queue.calls and time.time() < deadline:
        time.sleep(0.02)
    assert [c["kind"] for c in queue.calls] == ["reembed_batch"]
    assert calls == ids


def test_schedule_records_reembed_is_quiet_without_ids_or_embeddings(db, scheduler_env,
                                                                     monkeypatch):
    queue, _ = scheduler_env
    none = {"mode": "none", "count": 0, "stale": 0}
    assert reembed.schedule_records_reembed([], db_path=db, reason="x") == none

    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    monkeypatch.setattr(embeddings, "unavailability_reason", lambda: "torch не встановлено")
    assert reembed.schedule_records_reembed([_add_current(db)], db_path=db,
                                            reason="x") == none
    assert queue.calls == []


def test_schedule_records_reembed_requires_db_path(db, scheduler_env):
    """Дефолту «None = бойова БД» немає — як і в одиночного планувальника."""
    queue, _ = scheduler_env
    with pytest.raises(TypeError):
        reembed.schedule_records_reembed([_add_current(db)], reason="x")
    assert queue.calls == []


# ============================================================
# directions-projects-tags-17: гард формату префікса + epoch-check + хвіст батчу
# ============================================================

def _stamp_epoch(path, model, version, prefix_format):
    """Рядок, який лишає по собі бекфіл v46 (наявні пари = формат 1)."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO embedding_epochs (model, version, prefix_format) "
                 "VALUES (?, ?, ?)", (model, version, prefix_format))
    conn.commit()
    conn.close()


def test_run_refuses_older_format_epoch_without_scanning(db, fake_embedder, optimize_calls,
                                                          monkeypatch):
    """Пара збігається, формат — ні: `stale_ids` віддав би 0, тож прохід не
    стартує взагалі (і `--dry-run` теж), а не робить вигляд, що роботи немає."""
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db)

    def _boom(*a, **kw):
        raise AssertionError("stale_ids не має викликатись при format_mismatch")

    monkeypatch.setattr(reembed, "stale_ids", _boom)

    dry = reembed.run(db, dry_run=True)
    assert dry["status"] == "epoch_mismatch" and dry["reason"] == "format_mismatch"
    assert dry["total"] == 0
    assert reembed.run(db)["status"] == "epoch_mismatch"
    assert optimize_calls == [] and fake_embedder == []


def test_run_proceeds_when_epoch_is_new_and_stamps_format(db, fake_embedder, optimize_calls,
                                                           monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 5)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db)

    assert reembed.run(db)["done"] == 1
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT version, prefix_format FROM embedding_epochs "
                        "ORDER BY version").fetchall()
    conn.close()
    assert rows == [(4, 1), (5, embeddings.PREFIX_FORMAT)]


def test_cli_run_returns_nonzero_on_format_mismatch(db, fake_embedder, monkeypatch, capsys):
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db)

    assert reembed.main(["run", "--db", db]) == 1
    assert "Відмова" in capsys.readouterr().err


def test_epoch_check_reports_mismatch_with_exit_1(db, monkeypatch, capsys):
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db, model=embeddings.EMBED_MODEL, version=4)

    assert reembed.main(["epoch-check", "--db", db]) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "format_mismatch" in output
    assert f"{embeddings.EMBED_MODEL} / v4" in output      # перелік пар архіву
    assert f"PREFIX_FORMAT={embeddings.PREFIX_FORMAT}" in output


def test_epoch_check_is_read_only_and_passes_on_new_epoch(db, monkeypatch, capsys):
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 5)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db, model=embeddings.EMBED_MODEL, version=4)

    assert reembed.main(["epoch-check", "--db", db]) == 0
    assert "new_epoch" in capsys.readouterr().out

    conn = sqlite3.connect(db)
    try:
        # перевірка нічого не записала: епоха 5 зʼявиться лише з першими чанками
        assert conn.execute("SELECT COUNT(*) FROM embedding_epochs").fetchone()[0] == 1
    finally:
        conn.close()


def test_epoch_check_missing_db_returns_2(tmp_path, capsys):
    assert reembed.main(["epoch-check", "--db", str(tmp_path / "nope.db")]) == 2
    assert "не знайдено" in capsys.readouterr().err


def test_batch_cancellation_logs_remaining_ids_as_a_list(db, scheduler_env, optimize_calls,
                                                          monkeypatch, caplog):
    """Скасований хвіст у чергу НЕ повертається — тож лог мусить назвати кожен
    id, який лишився непереембедженим (мінор 7)."""
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: {"status": "embedded"})
    ids = [_add_tx(db) for _ in range(25)]
    reembed._pending_ids.update(ids)

    with caplog.at_level(logging.WARNING, logger="app.services.reembed"):
        reembed._batch_reembed_job(CancelAfterN(3), ids, db, "category_bulk")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "cancelled" in message
    assert str(sorted(ids[3:])) in message            # усі 22 — списком
    assert len(ids[3:]) == 22
    assert reembed._pending_ids == set()


def test_batch_exception_names_its_cause_in_the_tail_warning(db, scheduler_env,
                                                              optimize_calls,
                                                              monkeypatch, caplog):
    ids = [_add_tx(db) for _ in range(5)]
    reembed._pending_ids.update(ids)
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: {"status": "embedded"})

    class PoisonedJob:
        def __init__(self):
            self.calls = 0

        def is_cancelled(self):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("queue fault")
            return False

    with caplog.at_level(logging.WARNING, logger="app.services.reembed"):
        with pytest.raises(RuntimeError):
            reembed._batch_reembed_job(PoisonedJob(), ids, db, "category_bulk")

    message = [r for r in caplog.records if r.levelno == logging.WARNING][0].getMessage()
    assert "RuntimeError" in message
    assert str(sorted(ids[2:])) in message
