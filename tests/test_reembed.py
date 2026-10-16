"""Tests of the offline re-embed pass (app/services/reembed.py), Wave B, story 05.

Offline, without torch/GPU: `embeddings.is_available` and `embed_texts` are replaced
by a fake (zeros of the needed dimension), `optimize_chunk_index` by a counter.
We check the selection of stale records, idempotency of the pass, the live
DB guard and the CLI exit code.
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
    """The model is not loaded: the embedder returns zeros but remembers what it saw."""
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
# Selecting stale records
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
    assert optimize_calls == [db]          # exactly once

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

    # a repeated pass has no work, optimize is not called a second time
    second = reembed.run(db)
    assert second["total"] == 0 and second["done"] == 0
    assert second["optimized"] is False
    assert optimize_calls == [db]


def test_run_writes_context_prefix_into_chunks(db, fake_embedder, optimize_calls):
    """The pass moves the archive to the new pair AND gives the chunks a context prefix
    in one move: that is what it is done on a snapshot for."""
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
            raise RuntimeError("broken record")
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
# Live DB guard + CLI
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
    assert "Refusal" in capsys.readouterr().err

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
    assert "not found" in capsys.readouterr().err


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
# Migration v43: old rows with a NULL prefix are still found
# ============================================================

def test_legacy_chunks_without_prefix_stay_searchable(db):
    """Rebuilding chunks_fts must not "lose" the existing archive: a row with
    a NULL `context_prefix` is found by its text as before."""
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
    """Roll the DB back to the pre-v43 state: a single-column `chunks_fts` with the old
    triggers and no version record. We leave the `chunks.context_prefix` column
    (SQLite cannot DROP COLUMN in old versions, and the migration checks
    its presence separately anyway); what matters is that the INDEX has the old shape."""
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
    # The DB version is the MAXIMUM in schema_versions, so all records from 43
    # upward must be removed; otherwise the appearance of the next migration (v44) leaves
    # current_version=44, the v43 branch does not run, and the test checks emptiness.
    conn.execute("DELETE FROM schema_versions WHERE version >= 43")
    conn.commit()
    conn.close()


def test_migration_v43_rebuilds_index_over_existing_rows(db):
    """Upgrade of an existing DB: rows that already lay in chunks must, after the index
    rebuild, be found both by text (as before) and by the new prefix."""
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
    # before the migration a term from the prefix is not searchable: the index does not know it
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
    """Triggers are rewritten for two columns: `delete` must go with the OLD
    values, otherwise a ghost stays in the index after UPDATE/DELETE."""
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
    """A repeated init_database does not rebuild the index and does not lose rows."""
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
    """`optimize` on a rebuilt index must pass silently: it is what
    saves search after a mass pass (9s -> 64s without it)."""
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
# editable-title-description-03: single re-embed of one record
# ============================================================

class FakeJob:
    def __init__(self, kind, meta):
        self.kind = kind
        self.meta = meta

    def is_cancelled(self):
        return False


class FakeQueue:
    """A queue that runs the task at once and remembers what it was called with."""

    def __init__(self):
        self.calls = []

    def submit(self, kind, fn, *args, meta=None, **kwargs):
        job = FakeJob(kind, meta or {})
        self.calls.append({"kind": kind, "args": args, "meta": job.meta})
        self.result = fn(job, *args, **kwargs)
        return job


class FakeRecorder:
    """Live sessions are `sids`; `active_session_id` is deliberately absent: with two
    recordings it is None, so the flag must read `active_session_ids()`."""

    def __init__(self, active=False):
        self.sids = ["sess-1"] if active else []

    def active_session_ids(self):
        return list(self.sids)


@pytest.fixture
def scheduler_env(monkeypatch):
    """A fake queue + no recording + embeddings available; the set of pending
    ids is cleaned so that tests do not leak into each other."""
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
    # the DB path goes both in the task arguments and in meta: exactly the one passed
    assert queue.calls[0]["args"] == (tid, db)
    assert queue.calls[0]["meta"]["db_path"] == db
    # force=True is mandatory: the (model, version) pair did not change, without it
    # the record would be considered already processed and the new prefix would not reach the vectors.
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
    assert queue.calls == [] and calls == []          # the queue slot is not taken
    # a second call during the wait does not spawn a second task
    assert reembed.schedule_record_reembed(tid, db_path=db) == "pending"

    recorder.sids = []
    deadline = time.time() + 3.0
    while not queue.calls and time.time() < deadline:
        time.sleep(0.02)
    assert [c["meta"]["transcription_id"] for c in queue.calls] == [tid]
    assert calls == [tid]
    assert tid not in reembed._pending_ids


def test_recording_active_with_two_live_sessions(scheduler_env):
    """ADR-022 deferral: two live recordings also count as "a recording is in progress"."""
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
    """There is no "None = live DB" default: without a path it is a TypeError, not a quiet live DB."""
    queue, _ = scheduler_env
    with pytest.raises(TypeError):
        reembed.schedule_record_reembed(_add_tx(db))
    assert queue.calls == []


class SlowQueue:
    """A queue that does NOT start the task at once; the test starts the task manually."""

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
    """A call that got `"pending"` is covered by a task that has not read the row yet.

    The id is removed from `_pending_ids` at the start of the task body: until the task
    has started, a second call gets `"pending"` (and that is true: the task
    will still see its edit), while a first call AFTER the read starts gives a new task.
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

    queue.run_next()                       # the task started and read the row
    assert reads == [(db, tid)]
    assert tid not in reembed._pending_ids

    assert reembed.schedule_record_reembed(tid, db_path=db) == "queued"
    assert len(queue.jobs) == 1


# ============================================================
# directions-projects-tags-01: single re-embed of a LIST (C3)
# ============================================================

def _make_current(path, tid):
    """Move a record into the current epoch and give it a chunk (otherwise it is `stale`)."""
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
                             version=embeddings.EMBED_VERSION)   # the pair is there, no chunks

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
    assert [c["kind"] for c in queue.calls] == ["reembed_batch"]      # exactly one task
    assert queue.calls[0]["meta"]["ids"] == ids
    assert queue.calls[0]["meta"]["db_path"] == db
    assert queue.calls[0]["meta"]["reason"] == "category_tree_changed"
    assert [tid for tid, _ in seen] == ids
    # a mass DELETE+INSERT over chunks shatters chunks_fts: optimize exactly once
    assert optimize_calls == [db]
    assert not (set(ids) & reembed._pending_ids)


def test_batch_releases_each_pending_id_before_reading_its_row(db, scheduler_env,
                                                               optimize_calls, monkeypatch):
    """The id is removed from `_pending_ids` BEFORE the row is read; otherwise a call that
    got "already scheduled" would rely on a task with stale data."""
    from app import state

    queue = SlowQueue()
    monkeypatch.setattr(state, "job_queue", queue, raising=False)
    pending_at_read = []
    monkeypatch.setattr(embeddings, "chunk_and_embed_transcription",
                        lambda path, tid, force=False: pending_at_read.append(
                            (tid, tid in reembed._pending_ids)) or {"status": "embedded"})
    ids = [_add_current(db) for _ in range(reembed._BATCH_THRESHOLD + 1)]

    reembed.schedule_records_reembed(ids, db_path=db, reason="category_bulk")
    assert set(ids) <= reembed._pending_ids          # while the task has not started
    assert pending_at_read == []

    queue.run_next()
    assert pending_at_read == [(tid, False) for tid in ids]
    assert not (set(ids) & reembed._pending_ids)


# ============================================================
# directions-projects-tags-15: `_pending_ids` is released on cancellation/
# an exception; the batch does not duplicate already scheduled ids
# ============================================================

class CancelAfterN:
    """`is_cancelled()` says "no" the first `n` times, then "yes"."""

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

    # no process restart needed: the next single call for the unprocessed
    # (4th) id creates a task at once, rather than returning "pending"
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

    assert seen == ids                      # a bad record does not stop the rest
    assert result["done"] == 4 and result["failed"] == 1
    assert optimize_calls == [db]
    assert reembed._pending_ids == set()


def test_batch_job_releases_pending_tail_on_iteration_exception(db, scheduler_env,
                                                                 optimize_calls, monkeypatch):
    """An exception not from `chunk_and_embed_transcription` but from the mechanics of
    the loop itself (here the cancellation check): `finally` still releases the tail."""
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
    assert optimize_calls == []             # the exception came out before optimize


def test_schedule_records_reembed_batch_excludes_already_pending_ids(db, scheduler_env,
                                                                      monkeypatch):
    """5 of 25 live ids already wait for their own single task; the batch takes only
    the remaining 20 and does not put a duplicate of the single task."""
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
    assert reembed._pending_ids == set(ids)          # each id exactly once


def test_schedule_records_reembed_batch_none_when_all_already_pending(db, scheduler_env,
                                                                       monkeypatch):
    queue, _ = scheduler_env
    ids = [_add_current(db) for _ in range(reembed._BATCH_THRESHOLD + 1)]
    reembed._pending_ids.update(ids)

    res = reembed.schedule_records_reembed(ids, db_path=db, reason="category_bulk")
    assert res == {"mode": "none", "count": 0, "stale": 0}
    assert queue.calls == []
    assert reembed._pending_ids == set(ids)          # nothing was touched


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
    assert queue.calls == [] and calls == []          # the queue slot is not taken

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
    """There is no "None = live DB" default, just like for the single scheduler."""
    queue, _ = scheduler_env
    with pytest.raises(TypeError):
        reembed.schedule_records_reembed([_add_current(db)], reason="x")
    assert queue.calls == []


# ============================================================
# directions-projects-tags-17: prefix format guard + epoch-check + batch tail
# ============================================================

def _stamp_epoch(path, model, version, prefix_format):
    """The row the v46 backfill leaves behind (existing pairs = format 1)."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO embedding_epochs (model, version, prefix_format) "
                 "VALUES (?, ?, ?)", (model, version, prefix_format))
    conn.commit()
    conn.close()


def test_run_refuses_older_format_epoch_without_scanning(db, fake_embedder, optimize_calls,
                                                          monkeypatch):
    """The pair matches, the format does not: `stale_ids` would give 0, so the pass does not
    start at all (and `--dry-run` neither), rather than pretending there is no work."""
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db)

    def _boom(*a, **kw):
        raise AssertionError("stale_ids must not be called on format_mismatch")

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
    assert "Refusal" in capsys.readouterr().err


def test_epoch_check_reports_mismatch_with_exit_1(db, monkeypatch, capsys):
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db, model=embeddings.EMBED_MODEL, version=4)

    assert reembed.main(["epoch-check", "--db", db]) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "format_mismatch" in output
    assert f"{embeddings.EMBED_MODEL} / v4" in output      # the list of archive pairs
    assert f"PREFIX_FORMAT={embeddings.PREFIX_FORMAT}" in output


def test_epoch_check_is_read_only_and_passes_on_new_epoch(db, monkeypatch, capsys):
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 5)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    _add_tx(db, model=embeddings.EMBED_MODEL, version=4)

    assert reembed.main(["epoch-check", "--db", db]) == 0
    assert "new_epoch" in capsys.readouterr().out

    conn = sqlite3.connect(db)
    try:
        # the check wrote nothing: epoch 5 will appear only with the first chunks
        assert conn.execute("SELECT COUNT(*) FROM embedding_epochs").fetchone()[0] == 1
    finally:
        conn.close()


def test_epoch_check_missing_db_returns_2(tmp_path, capsys):
    assert reembed.main(["epoch-check", "--db", str(tmp_path / "nope.db")]) == 2
    assert "not found" in capsys.readouterr().err


def test_batch_cancellation_logs_remaining_ids_as_a_list(db, scheduler_env, optimize_calls,
                                                          monkeypatch, caplog):
    """A cancelled tail is NOT returned to the queue, so the log must name every
    id that stayed un-re-embedded (minor 7)."""
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
    assert str(sorted(ids[3:])) in message            # all 22 as a list
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
