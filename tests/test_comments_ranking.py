"""Comments layer, Wave 2: ranking priority, attaching, prompt.

The three priority layers are checked separately, because they also break separately:
  1. the RRF boost — a comment beats the transcript at equal relevance;
  2. `attach_comments` — a correction arrives even when it did NOT match itself;
  3. `_build_context` — the model sees it first and with an explicit mark.
"""
import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import comments, embeddings, rag, retrieval


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
    """EMBED_DIM=4; the query is always [1,0,0,0]; any text is encoded as
    [1,0,0,0] — i.e. the VECTOR relevance is the same for all. This is deliberate:
    that way the test measures the kind boost itself, not a random difference of cosines."""
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_query", lambda q: _unit([1.0, 0.0, 0.0, 0.0]))
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))
    return embeddings


def _add_tx(path, name="Дзвінок", date="2026-08-01", category_id=None):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "meeting_date, category_id) VALUES ('file', ?, 'x', ?, ?)",
        (name, date, category_id))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


def _add_chunk(path, tid, idx, text, vec=(1.0, 0.0, 0.0, 0.0)):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text, embedding) VALUES (?, ?, 0, 10, 'Ви', ?, ?)",
        (tid, idx, text, _unit(vec).tobytes()))
    conn.commit(); conn.close()


def _comment(path, tid, body, kind="note", **kw):
    c = comments.create(path, "transcription", tid, body, kind=kind, **kw)
    comments.index_comment(path, c["id"])
    return c


# --------------------------------------------------- layer 1: ranking boost

def test_comment_appears_in_results(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "обговорили бюджет проєкту")
    _comment(db, tid, "насправді бюджет 12 тисяч, а не 20")
    res = retrieval.search(db, "бюджет", top_k=5)
    kinds = [c["source_type"] for c in res["chunks"]]
    assert "comment" in kinds


def test_correction_outranks_transcript_at_equal_relevance(db, mock_embeddings):
    """The layer's main promise: at equal relevance the owner's
    correction ranks above the raw transcript."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    _comment(db, tid, "бюджет проєкту дванадцять тисяч", kind="correction")
    res = retrieval.search(db, "бюджет проєкту", top_k=5)
    assert res["chunks"][0]["source_type"] == "comment"
    assert res["chunks"][0]["comment_kind"] == "correction"


def test_kind_orders_comments_among_themselves(db, mock_embeddings):
    tid = _add_tx(db)
    _comment(db, tid, "бюджет питання відкрите", kind="question")
    _comment(db, tid, "бюджет виправлення суми", kind="correction")
    res = retrieval.search(db, "бюджет", top_k=5)
    got = [c["comment_kind"] for c in res["chunks"] if c["source_type"] == "comment"]
    assert got[0] == "correction"


def test_zero_weight_disables_boost(db, mock_embeddings):
    """The knob must be switchable off — otherwise tuning the balance is impossible."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    _comment(db, tid, "бюджет проєкту", kind="correction")
    boosted = retrieval.search(db, "бюджет проєкту", top_k=5)["chunks"]
    flat = retrieval.search(db, "бюджет проєкту", top_k=5, comment_weight=0.0)["chunks"]
    b = next(c for c in boosted if c["source_type"] == "comment")["score"]
    f = next(c for c in flat if c["source_type"] == "comment")["score"]
    assert b > f


def test_include_comments_false_excludes_them(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    _comment(db, tid, "бюджет уточнення")
    res = retrieval.search(db, "бюджет", top_k=5, include_comments=False)
    assert all(c["source_type"] != "comment" for c in res["chunks"])


def test_comment_chunk_ids_do_not_collide_with_transcript_ids(db, mock_embeddings):
    """The id spaces of the two tables overlap; in a shared result they must
    stay distinct, otherwise the proof of a copilot insight becomes ambiguous."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    _comment(db, tid, "бюджет уточнення")
    ids = [c["chunk_id"] for c in retrieval.search(db, "бюджет", top_k=5)["chunks"]]
    assert len(ids) == len(set(ids))
    assert any(i < 0 for i in ids) and any(i > 0 for i in ids)


def test_deleted_comment_leaves_search(db, mock_embeddings):
    tid = _add_tx(db)
    c = _comment(db, tid, "унікальнетутслово")
    assert retrieval.search(db, "унікальнетутслово", top_k=5)["chunks"]
    comments.delete(db, c["id"])
    assert retrieval.search(db, "унікальнетутслово", top_k=5)["chunks"] == []


def test_comment_respects_category_scope(db, mock_embeddings):
    """Under an active direction a comment of another direction must not leak."""
    t_in, t_out = _add_tx(db, "in", category_id=1), _add_tx(db, "out", category_id=2)
    _comment(db, t_in, "бюджет свій")
    _comment(db, t_out, "бюджет чужий")
    res = retrieval.search(db, "бюджет", top_k=5, category_id=1)
    bodies = [c["text"] for c in res["chunks"] if c["source_type"] == "comment"]
    assert bodies == ["бюджет свій"]


def test_comments_bypass_per_meeting_cap(db, mock_embeddings):
    """The cap "3 from one meeting" protects against a wordy call, not against
    clarifications: four comments on one recording must not choke each other
    under that cap (the share ceiling is a separate mechanism, see below)."""
    tid = _add_tx(db)
    for i in range(4):
        _comment(db, tid, f"бюджет уточнення номер {i}", kind="correction")
    res = retrieval.search(db, "бюджет уточнення", top_k=8, max_per_meeting=3,
                           comment_share=1.0)
    n = sum(1 for c in res["chunks"] if c["source_type"] == "comment")
    assert n == 4


# ------------------------------------------- comment share ceiling (2.5)

def test_comments_cannot_take_the_whole_top(db, mock_embeddings):
    """Found by measurement: without the ceiling comments took 115 slots out of 120, and on
    eight questions out of fifteen the whole top consisted of comments alone —
    the model got the clarifications WITHOUT the material they clarify."""
    tid = _add_tx(db)
    for i in range(10):
        _comment(db, tid, f"бюджет уточнення {i}", kind="correction")
    # Chunks are SPREAD across recordings: otherwise the per-meeting cap would itself trim the archive
    # to three, the top would be left underfilled, and overflowing with comments would be
    # legitimate — i.e. the test would measure the wrong ceiling.
    for i in range(10):
        t = _add_tx(db, f"запис {i}")
        _add_chunk(db, t, 0, f"бюджет обговорення {i}")
    res = retrieval.search(db, "бюджет", top_k=9)
    n_cm = sum(1 for c in res["chunks"] if c["source_type"] == "comment")
    assert len(res["chunks"]) == 9
    assert n_cm == 3                      # 9 * 0.34 → 3
    assert n_cm < len(res["chunks"])      # the archive stayed in the top


def test_cap_keeps_at_least_one_comment_on_tiny_k(db, mock_embeddings):
    """The only correction must get through even with a tiny window."""
    tid = _add_tx(db)
    _comment(db, tid, "бюджет виправлення", kind="correction")
    for i in range(5):
        _add_chunk(db, tid, i, f"бюджет обговорення {i}")
    res = retrieval.search(db, "бюджет", top_k=2)
    assert sum(1 for c in res["chunks"] if c["source_type"] == "comment") == 1


def test_capped_comments_still_rank_first(db, mock_embeddings):
    """The ceiling limits the COUNT, not the priority: those that passed stand on top."""
    tid = _add_tx(db)
    _comment(db, tid, "бюджет виправлення", kind="correction")
    for i in range(5):
        _add_chunk(db, tid, i, f"бюджет обговорення {i}")
    res = retrieval.search(db, "бюджет", top_k=6)
    assert res["chunks"][0]["source_type"] == "comment"


def test_overflow_comments_fill_empty_slots(db, mock_embeddings):
    """If there is no archive material for the query, it is better to return comments than
    emptiness — the extras are moved to the end, not thrown out."""
    tid = _add_tx(db)
    for i in range(5):
        _comment(db, tid, f"бюджет уточнення {i}", kind="correction")
    res = retrieval.search(db, "бюджет", top_k=5)
    assert len(res["chunks"]) == 5
    assert all(c["source_type"] == "comment" for c in res["chunks"])


def test_cap_is_tunable_and_disablable(db, mock_embeddings):
    tid = _add_tx(db)
    for i in range(6):
        _comment(db, tid, f"бюджет уточнення {i}", kind="correction")
    for i in range(6):
        t = _add_tx(db, f"запис {i}")
        _add_chunk(db, t, 0, f"бюджет обговорення {i}")
    tight = retrieval.search(db, "бюджет", top_k=6, comment_share=0.0)["chunks"]
    wide = retrieval.search(db, "бюджет", top_k=6, comment_share=1.0)["chunks"]
    assert sum(1 for c in tight if c["source_type"] == "comment") == 1   # the minimum
    assert sum(1 for c in wide if c["source_type"] == "comment") == 6


def test_cap_helper_preserves_order():
    """top_k=6, share=0.34 → 2 comments allowed; the third goes to the tail and
    does not vanish, and the order of the rest does not move."""
    ids = [-1, 5, -2, 6, -3, 7]
    assert retrieval._cap_comments(ids, top_k=6, share=0.34) == [-1, 5, -2, 6, 7, -3]
    assert retrieval._cap_comments(ids, top_k=6, share=1.0) == ids


def test_search_survives_db_without_comment_tables(tmp_path, mock_embeddings):
    """An old DB without migration v37 must not break search — this is the main path
    of the whole product."""
    path = str(tmp_path / "old.db")
    init_database(path)
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE comment_chunks_fts; DROP TABLE comment_chunks; "
                       "DROP TABLE comments;")
    conn.commit(); conn.close()
    tid = _add_tx(path)
    _add_chunk(path, tid, 0, "бюджет проєкту")
    res = retrieval.search(path, "бюджет", top_k=5)
    assert len(res["chunks"]) == 1


# ------------------------------------------------------- layer 2: auto-attach

def test_attach_pulls_correction_without_query_match(db, mock_embeddings):
    """What layer 2 exists for: «Іван більше не в проєкті» shares no
    words with «хто відповідає за фасад», so the boost cannot reach it."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "за фасад відповідає Іван")
    _comment(db, tid, "Іван більше не в проєкті", kind="correction")
    chunks = [{"transcription_id": tid, "source_type": "file", "text": "..."}]
    attached = retrieval.attach_comments(db, chunks)
    assert [a["text"] for a in attached] == ["Іван більше не в проєкті"]


def test_attach_skips_plain_notes(db, mock_embeddings):
    """Otherwise every answer would drag in all the card's notes and the layer would become noise."""
    tid = _add_tx(db)
    _comment(db, tid, "звичайна замітка", kind="note")
    attached = retrieval.attach_comments(
        db, [{"transcription_id": tid, "source_type": "file", "text": "..."}])
    assert attached == []


def test_attach_includes_pinned_of_any_kind(db, mock_embeddings):
    tid = _add_tx(db)
    _comment(db, tid, "закріплена замітка", kind="note", pinned=True)
    attached = retrieval.attach_comments(
        db, [{"transcription_id": tid, "source_type": "file", "text": "..."}])
    assert len(attached) == 1 and attached[0]["pinned"] is True


def test_attach_does_not_duplicate_a_direct_hit(db, mock_embeddings):
    tid = _add_tx(db)
    c = _comment(db, tid, "виправлення", kind="correction")
    chunks = [
        {"transcription_id": tid, "source_type": "file", "text": "..."},
        {"source_type": "comment", "comment_id": c["id"], "text": "виправлення"},
    ]
    assert retrieval.attach_comments(db, chunks) == []


def test_attach_respects_cap(db, mock_embeddings):
    tid = _add_tx(db)
    for i in range(6):
        _comment(db, tid, f"виправлення {i}", kind="correction")
    attached = retrieval.attach_comments(
        db, [{"transcription_id": tid, "source_type": "file", "text": "..."}], cap=3)
    assert len(attached) == 3


# ------------------------------------------------------------- layer 3: prompt

def test_context_puts_comments_first_and_labels_them():
    chunks = [
        {"source_type": "file", "source_name": "Дзвінок", "meeting_date": "2026-08-01",
         "text": "бюджет двадцять тисяч", "speaker": "Ви"},
        {"source_type": "comment", "comment_kind": "correction",
         "target_label": "Дзвінок", "meeting_date": "2026-08-02",
         "text": "бюджет дванадцять тисяч", "pinned": False},
    ]
    ctx = rag._build_context(chunks)
    assert ctx.startswith("[1] КОМЕНТАР ВЛАСНИКА (ВИПРАВЛЕННЯ)")
    assert "[2] Мітинг «Дзвінок»" in ctx


def test_context_numbers_attached_comments_too():
    chunks = [{"source_type": "file", "source_name": "Дзвінок",
               "meeting_date": "2026-08-01", "text": "текст"}]
    attached = [{"kind": "correction", "target_label": "Дзвінок", "date": "2026-08-02",
                 "text": "уточнення", "pinned": True, "anchor_time": 65.0}]
    ctx = rag._build_context(chunks, attached)
    assert "[1] КОМЕНТАР ВЛАСНИКА (ВИПРАВЛЕННЯ), закріплений" in ctx
    assert "до моменту ~01:05" in ctx
    assert "[2] Мітинг" in ctx


def test_context_unchanged_without_comments():
    """Regression: without comments the context must stay byte for byte the same,
    otherwise the layer would silently change the behaviour of all existing answers."""
    chunks = [{"source_type": "file", "source_name": "Дзвінок",
               "meeting_date": "2026-08-01", "text": "текст", "speaker": "Ви"}]
    assert rag._build_context(chunks) == (
        "[1] Мітинг «Дзвінок» (2026-08-01), спікер: Ви\nтекст")


def test_system_prompt_states_priority_rule():
    assert "КОМЕНТАРІ ВЛАСНИКА" in rag._RAG_SYSTEM_PROMPT
    assert "ВИЩИЙ пріоритет за сирий транскрипт" in rag._RAG_SYSTEM_PROMPT
    assert "«виправлення» скасовує" in rag._RAG_SYSTEM_PROMPT


# ------------------------------------------ regressions from the code review (merge)

def test_citation_numbers_match_the_sources_array():
    """Found by the review: the context put comments first, while `sources` went in
    result order — so [2] in the answer opened a foreign source, and the last
    number went outside the array altogether. Both now come from order_citables."""
    chunks = [
        {"source_type": "file", "source_name": "T1", "meeting_date": "2026-08-01",
         "text": "перший"},
        {"source_type": "file", "source_name": "T2", "meeting_date": "2026-08-02",
         "text": "другий"},
    ]
    attached = [{"kind": "correction", "target_label": "T1", "date": "2026-08-03",
                 "text": "виправлення", "pinned": True, "anchor_time": None}]
    sources = rag.order_citables(chunks, attached)
    ctx = rag._build_context(chunks, attached)
    assert len(sources) == 3
    # Every number in the context must lead to the same element as sources[n-1].
    assert sources[0]["text"] == "виправлення"
    assert ctx.startswith("[1] КОМЕНТАР ВЛАСНИКА")
    assert "[2] Мітинг «T1»" in ctx and sources[1]["source_name"] == "T1"
    assert "[3] Мітинг «T2»" in ctx and sources[2]["source_name"] == "T2"


def test_order_citables_is_identity_without_comments():
    chunks = [{"source_type": "file", "source_name": "T1", "text": "x"}]
    assert rag.order_citables(chunks, []) == chunks
    assert rag.order_citables(chunks) == chunks


def test_attached_comment_is_citable_and_openable():
    """An attached comment is numbered in the context, so the client must be able to
    open it — transcription_id must remain in it."""
    attached = [{"kind": "correction", "target_label": "T1", "date": "2026-08-03",
                 "text": "виправлення", "transcription_id": 42}]
    s = rag.order_citables([], attached)
    assert s[0]["transcription_id"] == 42 and s[0]["source_type"] == "comment"
