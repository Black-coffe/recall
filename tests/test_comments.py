"""Шар коментарів, Волна 0: схема v37, CRUD, індексер, деградація.

Модель ембедингів мокається (EMBED_DIM=4) — реальний e5-large (2GB) не
вантажиться. FTS5 справжній: тригери comment_chunks_ai наповнюють
comment_chunks_fts при INSERT, і саме це перевіряє тест лексичного шляху.
"""
import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import comments, embeddings


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return a / n if n else a


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


@pytest.fixture
def mock_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))
    return embeddings


@pytest.fixture
def no_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    return embeddings


def _add_tx(path, name="Дзвінок", text="текст"):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text) "
        "VALUES ('file', ?, ?)", (name, text))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


def _add_audio(path, title="Аудіо"):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path) "
        "VALUES ('u', ?, ?, 'f.mp3')", (title, title))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return aid


# ---------------------------------------------------------------- схема (v37)

def test_v37_creates_tables_and_fts(db):
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
    assert {"comments", "comment_chunks", "comment_chunks_fts"} <= names
    assert {"comment_chunks_ai", "comment_chunks_ad", "comment_chunks_au"} <= names
    version = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
    assert version >= 37
    conn.close()


def test_v37_is_idempotent(db):
    init_database(db)   # другий прогін на тій самій БД не має падати
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM schema_versions WHERE version = 37").fetchone()[0]
    conn.close()
    assert n == 1


# ---------------------------------------------------------------------- CRUD

def test_create_and_list(db):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "Насправді сума 12k", kind="correction")
    assert c["kind"] == "correction"
    assert c["effective_weight"] == 1.0
    assert c["indexed"] is False
    rows = comments.list_for(db, "transcription", tid)
    assert [r["body"] for r in rows] == ["Насправді сума 12k"]


def test_comment_on_media_card_without_transcript(db):
    """Головний сценарій, заради якого індекс окремий: картка Медіатеки без
    транскрипта коментується і зберігається."""
    aid = _add_audio(db)
    c = comments.create(db, "audio_download", aid, "Тут з 14:20 говорить підрядник")
    assert c["target_type"] == "audio_download"
    assert comments.list_for(db, "audio_download", aid)[0]["id"] == c["id"]


def test_any_language_body_roundtrips(db):
    tid = _add_tx(db)
    for body in ("Уточнення українською", "Пояснение по-русски",
                 "English clarification", "日本語のメモ"):
        comments.create(db, "transcription", tid, body)
    got = {r["body"] for r in comments.list_for(db, "transcription", tid)}
    assert "日本語のメモ" in got and "Пояснение по-русски" in got


def test_pinned_sorts_first(db):
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "перший")
    comments.create(db, "transcription", tid, "закріплений", pinned=True)
    rows = comments.list_for(db, "transcription", tid)
    assert rows[0]["body"] == "закріплений"


def test_unknown_target_and_kind_rejected(db):
    tid = _add_tx(db)
    with pytest.raises(comments.CommentError):
        comments.create(db, "planet", 1, "текст")
    with pytest.raises(comments.CommentError):
        comments.create(db, "transcription", tid, "текст", kind="злий")
    with pytest.raises(comments.CommentError):
        comments.create(db, "transcription", tid, "   ")


def test_missing_target_rejected(db):
    with pytest.raises(comments.CommentError):
        comments.create(db, "transcription", 99999, "коментар у нікуди")


def test_recording_session_target_needs_no_row(db):
    """Сесія запису не має таблиці з INTEGER-ключем — існування не перевіряємо,
    інакше коментар під час дзвінка створити було б неможливо."""
    c = comments.create(db, "recording_session", 42, "оператор: клієнт передумав",
                        source="live", anchor_time=137.5)
    assert c["anchor_time"] == 137.5
    assert c["source"] == "live"


def test_weight_override_clamped(db):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "x", kind="note", weight=5.0)
    assert c["effective_weight"] == 1.0


def test_counts_for_batch(db):
    t1, t2 = _add_tx(db, "a"), _add_tx(db, "b")
    comments.create(db, "transcription", t1, "one")
    comments.create(db, "transcription", t1, "two")
    comments.create(db, "transcription", t2, "three")
    counts = comments.counts_for(db, "transcription", [t1, t2, 999])
    assert counts == {t1: {"n": 2, "corrections": 0},
                      t2: {"n": 1, "corrections": 0}}


def test_counts_for_handles_large_id_list(db):
    """>999 id мають розбиватись на пачки, інакше SQLite падає на ліміті
    параметрів (а «вибрати всі за фільтром» у Бібліотеці дає до 3000)."""
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "x")
    counts = comments.counts_for(db, "transcription", [tid] + list(range(5000, 8000)))
    assert counts == {tid: {"n": 1, "corrections": 0}}


def test_soft_delete_hides_and_restore_returns(db):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "помилка")
    assert comments.delete(db, c["id"]) is True
    assert comments.list_for(db, "transcription", tid) == []
    assert comments.restore(db, c["id"]) is True
    assert len(comments.list_for(db, "transcription", tid)) == 1


def test_retarget_moves_live_comments_to_transcript(db):
    tid = _add_tx(db)
    comments.create(db, "recording_session", 7, "по ходу дзвінка", source="live")
    comments.create(db, "recording_session", 7, "ще одне", source="live")
    moved = comments.retarget(db, "recording_session", 7, "transcription", tid)
    assert moved == 2
    assert len(comments.list_for(db, "transcription", tid)) == 2


def test_list_recent_filters(db):
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "а", kind="correction")
    comments.create(db, "transcription", tid, "б", kind="note")
    assert len(comments.list_recent(db, kind="correction")["comments"]) == 1
    assert len(comments.list_recent(db)["comments"]) == 2


# ------------------------------------------------------------------ індексер

def test_index_writes_chunks_and_vectors(db, mock_embeddings):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "Клієнт погодився на 12 тисяч")
    res = comments.index_comment(db, c["id"])
    assert res["status"] == "embedded"
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT text, embedding FROM comment_chunks WHERE comment_id = ?",
        (c["id"],)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] is not None
    assert comments.get(db, c["id"])["indexed"] is True


def test_index_is_idempotent(db, mock_embeddings):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "текст")
    comments.index_comment(db, c["id"])
    assert comments.index_comment(db, c["id"])["status"] == "skipped"
    assert comments.index_comment(db, c["id"], force=True)["status"] == "embedded"


def test_fts_finds_comment_text(db, mock_embeddings):
    """Лексичний шлях працює через справжні тригери FTS5."""
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "Підрядник зірвав дедлайн по фасаду")
    comments.index_comment(db, c["id"])
    conn = sqlite3.connect(db)
    hits = conn.execute(
        "SELECT rowid FROM comment_chunks_fts WHERE comment_chunks_fts MATCH ?",
        ('"фасаду"',)).fetchall()
    conn.close()
    assert len(hits) == 1


def test_degrades_without_model_but_stays_searchable(db, no_embeddings):
    """Без моделі коментар усе одно ріжеться і лягає у FTS, але лишається в
    черзі на векторизацію — тихої втрати немає."""
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "унікальнеслово тут")
    res = comments.index_comment(db, c["id"])
    assert res["status"] == "fts_only"
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT embedding FROM comment_chunks WHERE comment_id = ?", (c["id"],)).fetchone()
    hits = conn.execute(
        "SELECT rowid FROM comment_chunks_fts WHERE comment_chunks_fts MATCH ?",
        ('"унікальнеслово"',)).fetchall()
    conn.close()
    assert row[0] is None            # вектора немає
    assert len(hits) == 1            # але знайти можна
    assert c["id"] in comments.pending_ids(db)   # і робота лишилась видимою


def test_edit_body_drops_stale_index(db, mock_embeddings):
    """Правка тіла має знімати старий вектор одразу, інакше пошук віддавав би
    текст, якого на картці вже немає."""
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "стара редакція")
    comments.index_comment(db, c["id"])
    upd = comments.update(db, c["id"], body="нова редакція")
    assert upd["indexed"] is False
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM comment_chunks WHERE comment_id = ?",
                     (c["id"],)).fetchone()[0]
    hits = conn.execute(
        "SELECT rowid FROM comment_chunks_fts WHERE comment_chunks_fts MATCH ?",
        ('"стара"',)).fetchall()
    conn.close()
    assert n == 0 and hits == []
    assert c["id"] in comments.pending_ids(db)


def test_edit_kind_only_keeps_index(db, mock_embeddings):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "текст")
    comments.index_comment(db, c["id"])
    upd = comments.update(db, c["id"], kind="correction")
    assert upd["indexed"] is True and upd["effective_weight"] == 1.0


def test_delete_removes_from_index(db, mock_embeddings):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "тимчасове")
    comments.index_comment(db, c["id"])
    comments.delete(db, c["id"])
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM comment_chunks WHERE comment_id = ?",
                     (c["id"],)).fetchone()[0]
    conn.close()
    assert n == 0


def test_long_comment_splits_into_several_chunks(db, mock_embeddings):
    tid = _add_tx(db)
    body = ("Це довгий розбір ситуації з підрядником. " * 80)
    c = comments.create(db, "transcription", tid, body)
    res = comments.index_comment(db, c["id"])
    assert res["chunks"] > 1


def test_reindex_backfills_and_converges(db, mock_embeddings):
    tid = _add_tx(db)
    for i in range(3):
        comments.create(db, "transcription", tid, f"коментар {i}")
    first = comments.reindex(db)
    assert first["pending"] == 3 and first["embedded"] == 3
    # Другий прогін не має знаходити роботи — інакше бекфіл не сходиться.
    assert comments.reindex(db)["pending"] == 0
    assert comments.pending_ids(db) == []


def test_reindex_dry_run_writes_nothing(db, mock_embeddings):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "текст")
    res = comments.reindex(db, dry_run=True)
    assert res["dry_run"] is True and res["pending"] == 1
    assert comments.get(db, c["id"])["indexed"] is False


def test_stats_shape(db, mock_embeddings):
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "а", kind="correction")
    comments.create(db, "transcription", tid, "б")
    comments.reindex(db)
    s = comments.stats(db)
    assert s["total"] == 2 and s["indexed"] == 2 and s["pending"] == 0
    assert s["by_kind"]["correction"] == 1
