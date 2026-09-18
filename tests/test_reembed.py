"""Тести офлайн-проходу re-embed (app/services/reembed.py) — Волна B, історія 05.

Офлайн, без torch/GPU: `embeddings.is_available` і `embed_texts` підмінені
фейком (нулі потрібної розмірності), `optimize_chunk_index` — лічильником.
Перевіряємо вибірку застарілих записів, ідемпотентність проходу, гард бойової
БД і код виходу CLI.
"""
from __future__ import annotations

import sqlite3

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
    conn.execute("DELETE FROM schema_versions WHERE version = 43")
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
