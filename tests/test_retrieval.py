"""Tests for hybrid retrieval (Phase 15A): recency + diversification.

Embeddings are mocked (EMBED_DIM=4, a fixed query vector) -- the real e5-large
model (2GB) is not loaded. FTS5 is real (the chunks_ai triggers fill
chunks_fts on INSERT into chunks).
"""
import math
import sqlite3
from datetime import datetime

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import categories, embeddings, reranker, retrieval


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
    """is_available()=True, EMBED_DIM=4, embed_query -> a fixed [1,0,0,0]."""
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_query",
                        lambda q: _unit([1.0, 0.0, 0.0, 0.0]))
    return embeddings


def _add_tx(path, source_name, meeting_date=None, category_id=None,
            source_type="file", transcript="x",
            embedding_model=None, embedding_version=None):
    """embedding_model/embedding_version default to the CURRENT pair
    (embeddings.EMBED_MODEL/EMBED_VERSION) -- exactly what the new epoch filter in
    retrieval._vector_search (production-rag-wave-b-04) requires. Pass a different
    pair explicitly to check that a foreign epoch does not get into vector hits."""
    if embedding_model is None:
        embedding_model = embeddings.EMBED_MODEL
    if embedding_version is None:
        embedding_version = embeddings.EMBED_VERSION
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "meeting_date, category_id, embedding_model, embedding_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_type, source_name, transcript, meeting_date, category_id,
         embedding_model, embedding_version),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _add_chunk(path, tid, idx, text, vec):
    """vec -- a list of floats (will be normalized); None -> no embedding (FTS only)."""
    blob = _unit(vec).tobytes() if vec is not None else None
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, idx, idx * 1.0, idx * 1.0 + 1, None, text, blob),
    )
    conn.commit()
    conn.close()


# ============================================================
# _recency_factor
# ============================================================

def test_recency_factor_today_is_one():
    today = datetime.now().strftime("%Y-%m-%d")
    assert retrieval._recency_factor(today) == pytest.approx(1.0, abs=0.05)


def test_recency_factor_half_life():
    half = retrieval._RECENCY_HALF_LIFE_DAYS
    past = (datetime.now() - __import__("datetime").timedelta(days=half)).strftime("%Y-%m-%d")
    assert retrieval._recency_factor(past) == pytest.approx(0.5, abs=0.05)


def test_recency_factor_unknown_is_zero():
    assert retrieval._recency_factor(None) == 0.0
    assert retrieval._recency_factor("not-a-date") == 0.0


# ============================================================
# Base behaviour
# ============================================================

def test_empty_query_returns_empty(db, mock_embeddings):
    assert retrieval.search(db, "")["chunks"] == []
    assert retrieval.search(db, "   ")["chunks"] == []


def test_no_results_returns_empty(db, mock_embeddings):
    # No chunk in the DB
    res = retrieval.search(db, "zzznotfoundzzz")
    assert res["chunks"] == []
    assert res["vector_available"] is True


# ============================================================
# Diversification: one meeting does not eat the whole top
# ============================================================

def test_per_meeting_cap_limits_domination(db, mock_embeddings):
    big = _add_tx(db, "Великий мітинг")
    small = _add_tx(db, "Інший мітинг")
    q = [1.0, 0, 0, 0]  # all chunks are maximally relevant (cosine 1)
    for i in range(5):
        _add_chunk(db, big, i, f"alpha beta gamma {i}", q)
    _add_chunk(db, small, 0, "alpha beta gamma small", q)

    # The query word is absent from the text -> FTS is empty, only the vector works
    res = retrieval.search(db, "qqqzzz", top_k=4, max_per_meeting=2, recency_weight=0)
    tids = [c["transcription_id"] for c in res["chunks"]]
    assert len(res["chunks"]) == 4
    assert tids.count(big) <= 2 + 2  # cap=2, the shortfall may be topped up; but small must be present
    assert small in tids, "the small meeting must get in thanks to diversification"
    assert len(set(tids)) >= 2


def test_no_cap_lets_one_meeting_dominate(db, mock_embeddings):
    big = _add_tx(db, "Великий мітинг")
    small = _add_tx(db, "Інший мітинг")
    q = [1.0, 0, 0, 0]
    for i in range(5):
        _add_chunk(db, big, i, f"alpha {i}", q)
    _add_chunk(db, small, 0, "alpha small", q)

    res = retrieval.search(db, "qqqzzz", top_k=4, max_per_meeting=0, recency_weight=0)
    tids = [c["transcription_id"] for c in res["chunks"]]
    assert tids == [big, big, big, big]  # without the cap the big meeting takes all slots


# ============================================================
# Recency: at equal relevance the newer one wins
# ============================================================

def test_recency_promotes_newer(db, mock_embeddings):
    old = _add_tx(db, "Старий", meeting_date="2020-01-01")
    new = _add_tx(db, "Новий", meeting_date=datetime.now().strftime("%Y-%m-%d"))
    # OLD is slightly more relevant (cosine 1.0), NEW slightly less (cosine 0.95)
    _add_chunk(db, old, 0, "alpha", [1.0, 0, 0, 0])
    _add_chunk(db, new, 0, "alpha", [0.95, math.sqrt(1 - 0.95 ** 2), 0, 0])

    # Without recency the more relevant OLD wins
    res0 = retrieval.search(db, "qqqzzz", top_k=2, recency_weight=0)
    assert res0["chunks"][0]["transcription_id"] == old

    # With recency NEW (today) overtakes OLD despite slightly lower relevance
    res1 = retrieval.search(db, "qqqzzz", top_k=2, recency_weight=0.25)
    assert res1["chunks"][0]["transcription_id"] == new


# ============================================================
# FTS fallback: works without embeddings
# ============================================================

def test_fts_only_when_embeddings_unavailable(db, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db, "Лексичний")
    _add_chunk(db, tid, 0, "рішення про бюджет фонду", None)

    res = retrieval.search(db, "бюджет")
    assert res["vector_available"] is False
    assert len(res["chunks"]) == 1
    assert res["chunks"][0]["transcription_id"] == tid
    assert "fts" in res["chunks"][0]["matched_by"]


# ============================================================
# Direction filter (category_id)
# ============================================================

def test_category_filter(db, mock_embeddings):
    a = _add_tx(db, "Фонд", category_id=1)
    b = _add_tx(db, "Особисте", category_id=2)
    _add_chunk(db, a, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _add_chunk(db, b, 0, "спільне слово альфа", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz", category_id=1)
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tids == {a}


def test_category_none_filter(db, mock_embeddings):
    a = _add_tx(db, "Без напрямку", category_id=None)
    b = _add_tx(db, "З напрямком", category_id=1)
    _add_chunk(db, a, 0, "альфа", [1.0, 0, 0, 0])
    _add_chunk(db, b, 0, "альфа", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz", category_id="none")
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tids == {a}


# ============================================================
# T6.8: soft FTS relevance cutoff (long queries, at least 2 term matches)
# ============================================================

def test_fts_short_query_keeps_single_word_match(db, monkeypatch):
    """A 1-2 word query is NOT filtered -- the single match is itself the signal."""
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db, "Короткий запит")
    _add_chunk(db, tid, 0, "тут згадується бюджет фонду", None)

    res = retrieval.search(db, "бюджет")
    assert len(res["chunks"]) == 1
    assert res["chunks"][0]["transcription_id"] == tid


def test_fts_long_query_filters_single_term_overlap(db, monkeypatch):
    """A query of 3+ words: a chunk with ONE shared word is dropped, a chunk with
    2+ shared words stays -- recall is not zeroed, noise is removed."""
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    weak = _add_tx(db, "Слабкий збіг")
    strong = _add_tx(db, "Сильний збіг")
    # weak: only the word "бюджет" is shared with the query
    _add_chunk(db, weak, 0, "бюджет випадковий текст без стосунку", None)
    # strong: all three words of the query are present
    _add_chunk(db, strong, 0, "бюджет проєкту команди обговорили сьогодні", None)

    res = retrieval.search(db, "бюджет проєкту команди")
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert strong in tids
    assert weak not in tids


def test_fts_long_query_soft_fallback_when_filter_empties_results(db, monkeypatch):
    """If the cutoff filter eats ALL hits (no chunk has 2+ matches) --
    fall back to the unfiltered list: recall is NOT zeroed."""
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db, "Єдиний слабкий збіг")
    _add_chunk(db, tid, 0, "бюджет це все що тут є", None)

    res = retrieval.search(db, "бюджет проєкту команди")
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tid in tids, "the filter must not zero out the SINGLE (even if weak) hit"


# ============================================================
# T6.4: rerank=False (default) does NOT touch the reranker
# ============================================================

def test_rerank_default_off_does_not_call_reranker(db, mock_embeddings, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("reranker.rerank must NOT be called when rerank=False")
    monkeypatch.setattr(reranker, "rerank", _boom)

    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz")  # rerank not passed -> default False
    assert len(res["chunks"]) == 1
    assert "rerank_score" not in res["chunks"][0]


# ============================================================
# T6.4: rerank=True reranks the pool by the mock's score
# ============================================================

def test_rerank_true_reorders_pool_and_tags_score(db, mock_embeddings, monkeypatch):
    """3 chunks with the same vector relevance (cosine 1.0) -- RRF+recency
    does not tell them apart (the order is arbitrary/stable), but the mock reranker explicitly
    prefers the chunk with the text "c" -- and the final order must
    reflect that."""
    tid = _add_tx(db, "Мітинг")
    q = [1.0, 0, 0, 0]
    _add_chunk(db, tid, 0, "a", q)
    _add_chunk(db, tid, 1, "b", q)
    _add_chunk(db, tid, 2, "c", q)

    def _fake_rerank(query, candidates, text_key="text"):
        order = {"c": 3.0, "b": 2.0, "a": 1.0}
        scored = sorted(candidates, key=lambda c: order.get(c[text_key], 0.0), reverse=True)
        out = []
        for c in scored:
            c2 = dict(c)
            c2["rerank_score"] = order.get(c[text_key], 0.0)
            out.append(c2)
        return out
    monkeypatch.setattr(reranker, "rerank", _fake_rerank)

    res = retrieval.search(db, "qqqzzz", top_k=3, max_per_meeting=0, rerank=True)
    texts = [c["text"] for c in res["chunks"]]
    assert texts == ["c", "b", "a"]
    assert res["chunks"][0]["rerank_score"] == pytest.approx(3.0)


def test_rerank_true_tail_beyond_pool_untouched(db, mock_embeddings, monkeypatch):
    """The pool is limited by rerank_pool_size -- candidates outside the pool stay in the
    original RRF+recency order (reranker.rerank does not see the tail at all)."""
    tid = _add_tx(db, "Мітинг")
    q = [1.0, 0, 0, 0]
    for i in range(4):
        _add_chunk(db, tid, i, f"chunk{i}", q)

    seen_texts = []
    def _tracking_rerank(query, candidates, text_key="text"):
        seen_texts.extend(c[text_key] for c in candidates)
        return candidates  # order unchanged
    monkeypatch.setattr(reranker, "rerank", _tracking_rerank)

    res = retrieval.search(db, "qqqzzz", top_k=4, max_per_meeting=0,
                           rerank=True, rerank_pool_size=2)
    assert len(seen_texts) == 2, "the reranker must see only the pool (2), not all 4 candidates"
    assert len(res["chunks"]) == 4  # the tail is still returned (just not reranked)


def test_rerank_graceful_degradation_via_real_reranker(db, mock_embeddings, monkeypatch):
    """End-to-end path (search -> reranker.rerank), reranker unavailable --
    the result is identical to rerank=False (does not break the search, does not lose candidates)."""
    monkeypatch.setattr(reranker, "is_available", lambda: False)
    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz", rerank=True)
    assert len(res["chunks"]) == 1
    assert "rerank_score" not in res["chunks"][0]


# ============================================================
# Wave 4: diversification -- for Telegram the source is the CHAT, not the record
# ============================================================

def _row(source_type, tid, chat_id=None):
    return {"source_type": source_type, "transcription_id": tid, "tg_chat_id": chat_id}


def test_group_key_uses_chat_for_telegram():
    """In TG a record = one message, so the cap "3 per transcript" NEVER
    worked: eight messages from one chat took all eight slots, pushing out
    calls and documents."""
    a = retrieval._group_key(_row("telegram", 100, -1001234567890))
    b = retrieval._group_key(_row("telegram", 101, -1001234567890))
    assert a == b, "two messages of one chat -- one source"


def test_group_key_keeps_transcript_for_others():
    a = retrieval._group_key(_row("recording", 5))
    b = retrieval._group_key(_row("recording", 6))
    assert a != b


def test_group_key_falls_back_when_chat_id_missing():
    assert retrieval._group_key(_row("telegram", 7, None)) == ("tx", 7)


def test_diversify_caps_telegram_by_chat():
    ranked = [1, 2, 3, 4, 5]
    groups = {cid: ("tg", -100) for cid in ranked}
    picked = retrieval._diversify(ranked, groups, top_k=5, max_per_meeting=2)
    # the cap leaves 2, the rest is topped up by overflow -- but the order shows that the
    # limit worked (earlier it did not work at all)
    assert picked[:2] == [1, 2]


# ============================================================
# Wave 4.5: the source is the THREAD, and stitching the thread on output
# ============================================================

def _tg_row(tid, chat_id=-100, thread_id=None):
    return {"source_type": "telegram", "transcription_id": tid,
            "tg_chat_id": chat_id, "tg_thread_id": thread_id}


def test_group_key_prefers_thread_over_chat():
    """Several projects go on in a fund chat at once. A per-chat cap would choke them
    against each other, though these are different conversations."""
    a = retrieval._group_key(_tg_row(100, thread_id=7))
    b = retrieval._group_key(_tg_row(101, thread_id=8))
    assert a != b, "different threads of one chat -- different sources"


def test_group_key_same_thread_is_one_source():
    a = retrieval._group_key(_tg_row(100, thread_id=7))
    b = retrieval._group_key(_tg_row(101, thread_id=7))
    assert a == b


def test_group_key_falls_back_to_chat_before_threads_exist():
    """While threads are not laid out, the behaviour must stay as in Wave 4."""
    a = retrieval._group_key(_tg_row(100, thread_id=None))
    b = retrieval._group_key(_tg_row(101, thread_id=None))
    assert a == b == ("tg", -100)


def _seed_thread(path, msgs, thread_id=1, chat_id=-100):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO tg_threads (id, chat_id, label, status) "
                 "VALUES (?, ?, 'Узгодження зустрічі', 'open')", (thread_id, chat_id))
    for i, (msg_id, sender, text) in enumerate(msgs):
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "tg_chat_id, tg_message_id, tg_date, tg_sender, tg_thread_id) "
            "VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?)",
            (f"[TG] {text[:20]}", text, chat_id, msg_id,
             f"2026-06-0{i + 1}T10:00:00+00:00", sender, thread_id))
    conn.commit()
    conn.close()


def test_thread_context_brings_the_answer_with_the_question(db):
    """The live failure the wave was made for: the finding is a QUESTION, and the
    answer is the next reply, with no words in common with the query."""
    _seed_thread(db, [(1, "Юля", "Давайте в четвер о 10:00? Чи всім зручно?"),
                      (2, "Настя", "чт – з 09:00 до 11:00, після 16:45")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 1,
               "text": "Давайте в четвер о 10:00? Чи всім зручно?"}]
    retrieval.attach_thread_context(db, chunks)
    texts = [m["text"] for m in chunks[0]["thread"]["messages"]]
    assert any("09:00 до 11:00" in t for t in texts), "the answer must arrive together with the question"


def test_thread_context_marks_which_message_was_found(db):
    _seed_thread(db, [(1, "Юля", "питання"), (2, "Настя", "відповідь")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 2,
               "text": "відповідь"}]
    retrieval.attach_thread_context(db, chunks)
    hits = [m for m in chunks[0]["thread"]["messages"] if m["is_hit"]]
    assert len(hits) == 1 and hits[0]["tg_message_id"] == 2


def test_thread_context_window_is_centred_on_the_hit(db):
    _seed_thread(db, [(i, "A", f"повідомлення {i}") for i in range(1, 10)])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 8,
               "text": "повідомлення 8"}]
    retrieval.attach_thread_context(db, chunks, max_msgs=4)
    ids = [m["tg_message_id"] for m in chunks[0]["thread"]["messages"]]
    assert len(ids) == 4
    assert 8 in ids, "the window must contain the finding itself, not the start of the thread"


def test_thread_context_noop_without_threads(db):
    """Records without a thread stay as they were -- that is not an error but the state "not
    laid out yet"."""
    chunks = [{"source_type": "telegram", "tg_thread_id": None, "text": "x"}]
    retrieval.attach_thread_context(db, chunks)
    assert "thread" not in chunks[0]


def test_thread_context_skips_single_message_thread(db):
    """A thread of one message adds nothing -- we do not bloat the prompt."""
    _seed_thread(db, [(1, "Юля", "одне повідомлення")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 1,
               "text": "одне повідомлення"}]
    retrieval.attach_thread_context(db, chunks)
    assert "thread" not in chunks[0]


def test_thread_context_truncates_long_neighbor_message(db):
    """A neighbour longer than RAG_THREAD_MSG_CHARS is cut with a suffix, a shorter one is not."""
    long_neighbor = "а" * 3000
    _seed_thread(db, [(1, "Юля", long_neighbor), (2, "Настя", "коротка відповідь")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 2,
               "text": "коротка відповідь"}]
    retrieval.attach_thread_context(db, chunks)
    msgs = {m["tg_message_id"]: m for m in chunks[0]["thread"]["messages"]}
    assert msgs[1].get("truncated") is True
    assert "…[обрізано, ще" in msgs[1]["text"]
    assert len(msgs[1]["text"]) <= 1500
    assert "truncated" not in msgs[2]


def test_thread_context_caps_total_chars_of_stitching(db):
    """The sum of len(text) of all thread messages does not exceed RAG_THREAD_CHARS."""
    huge_hit = "б" * 156000
    _seed_thread(db, [(1, "Юля", "звичайне повідомлення"), (2, "Настя", huge_hit),
                      (3, "Юля", "ще одне звичайне")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 2,
               "text": huge_hit}]
    retrieval.attach_thread_context(db, chunks)
    thread = chunks[0]["thread"]
    total_kept = sum(len(m["text"]) for m in thread["messages"])
    assert total_kept <= 8000
    assert thread["chars_kept"] <= 8000
    assert thread["chars_total"] > 8000
    hit = next(m for m in thread["messages"] if m["is_hit"])
    assert hit["tg_message_id"] == 2
    assert hit.get("truncated") is True
    assert "…[обрізано, ще" in hit["text"]


def test_thread_context_hit_budget_never_smaller_than_neighbours(db):
    """A hit with a long text under the defaults gets 4000, neighbours -- no more than 1500."""
    huge = "в" * 156000
    _seed_thread(db, [(1, "Юля", huge), (2, "Настя", huge), (3, "Юля", huge)])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 2,
               "text": huge}]
    retrieval.attach_thread_context(db, chunks)
    msgs = chunks[0]["thread"]["messages"]
    hit = next(m for m in msgs if m["is_hit"])
    neighbours = [m for m in msgs if not m["is_hit"]]
    assert len(hit["text"]) == 4000
    for n in neighbours:
        assert len(n["text"]) <= 1500
    assert len(hit["text"]) >= max((len(n["text"]) for n in neighbours), default=0)


@pytest.mark.parametrize("stitch", [2, 6, 8])
@pytest.mark.parametrize("msg_chars", [300, 1500])
@pytest.mark.parametrize("thread_chars", [1000, 8000])
def test_thread_context_window_sum_never_exceeds_ceiling(db, monkeypatch, stitch,
                                                          msg_chars, thread_chars):
    """For any combination of STITCH/MSG/THREAD the window sum is <= THREAD."""
    monkeypatch.setenv("RAG_THREAD_MSG_CHARS", str(msg_chars))
    monkeypatch.setenv("RAG_THREAD_CHARS", str(thread_chars))
    huge = "г" * 156000
    _seed_thread(db, [(i, "A", huge) for i in range(1, 10)])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 5,
               "text": huge}]
    retrieval.attach_thread_context(db, chunks, max_msgs=stitch)
    thread = chunks[0]["thread"]
    total_kept = sum(len(m["text"]) for m in thread["messages"])
    assert thread["chars_kept"] == total_kept
    assert total_kept <= thread_chars
    hit = next(m for m in thread["messages"] if m["is_hit"])
    reserve = len(f"…[обрізано, ще {156000} симв.]")
    assert len(hit["text"]) > reserve or len(hit["text"]) == 156000


def test_thread_context_stitch_eight_partial_neighbour_and_dropped(db):
    """STITCH=8 under the defaults: the hit 4000, the nearest neighbours 1500 each, then a partial one and dropped."""
    huge = "д" * 156000
    _seed_thread(db, [(i, "A", huge) for i in range(1, 10)])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 5,
               "text": huge}]
    retrieval.attach_thread_context(db, chunks, max_msgs=8)
    thread = chunks[0]["thread"]
    total_kept = sum(len(m["text"]) for m in thread["messages"])
    assert thread["chars_kept"] == total_kept <= 8000
    hit = next(m for m in thread["messages"] if m["is_hit"])
    assert len(hit["text"]) == 4000
    assert thread["dropped"] >= 1


def test_thread_context_short_messages_untouched_by_repair(db):
    """A short window fits whole -- without truncation and without dropped."""
    _seed_thread(db, [(1, "Юля", "коротке"), (2, "Настя", "теж коротке")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 1,
               "text": "коротке"}]
    retrieval.attach_thread_context(db, chunks)
    thread = chunks[0]["thread"]
    assert thread["dropped"] == 0
    for m in thread["messages"]:
        assert m.get("truncated") is not True
    ids = [m["tg_message_id"] for m in thread["messages"]]
    assert ids == [1, 2]


# ============================================================
# Audio duplicates: duplicate_of excludes a record from the output (Wave A)
# ============================================================

def _mark_duplicate(path, tid, original_id):
    conn = sqlite3.connect(path)
    conn.execute("UPDATE transcriptions SET duplicate_of = ? WHERE id = ?",
                 (original_id, tid))
    conn.commit()
    conn.close()


def test_duplicate_excluded_from_vector_branch(db, mock_embeddings):
    """A copy with duplicate_of does not surface in the output -- only the original stays."""
    orig = _add_tx(db, "Дзвінок")
    copy = _add_tx(db, "Дзвінок (перезалив)")
    q = [1.0, 0, 0, 0]
    _add_chunk(db, orig, 0, "alpha beta", q)
    _add_chunk(db, copy, 0, "alpha beta", q)

    before = retrieval.search(db, "qqqzzz", top_k=5, recency_weight=0)
    assert {c["transcription_id"] for c in before["chunks"]} == {orig, copy}

    _mark_duplicate(db, copy, orig)
    after = retrieval.search(db, "qqqzzz", top_k=5, recency_weight=0)
    assert [c["transcription_id"] for c in after["chunks"]] == [orig]


def test_duplicate_excluded_from_fts_branch(db, mock_embeddings):
    """The same for the lexical branch: a word from the duplicate's text pulls out nothing."""
    orig = _add_tx(db, "Дзвінок")
    copy = _add_tx(db, "Дзвінок (перезалив)")
    _add_chunk(db, orig, 0, "барселона бюджет", None)
    _add_chunk(db, copy, 0, "барселона бюджет", None)
    _mark_duplicate(db, copy, orig)

    res = retrieval.search(db, "барселона", top_k=5, recency_weight=0)
    assert [c["transcription_id"] for c in res["chunks"]] == [orig]


# ============================================================
# production-rag-wave-b-04: embedding epoch (embedding_model, embedding_version)
# ============================================================

def test_other_embedding_version_excluded_from_vector_branch_but_kept_in_fts(db, mock_embeddings):
    """Vector hits are computed ONLY over chunks of the current pair (model, version) --
    a record of another epoch (e.g. not yet re-embedded after an EMBED_VERSION change)
    must not take part in cosines with the current query. FTS5 knows
    nothing about the model -- the same record stays lexically findable."""
    current = _add_tx(db, "Поточна епоха")
    stale = _add_tx(db, "Стара епоха",
                    embedding_version=embeddings.EMBED_VERSION + 1)
    q = [1.0, 0, 0, 0]
    _add_chunk(db, current, 0, "унікальний термін альфа", q)
    _add_chunk(db, stale, 0, "унікальний термін альфа", q)

    res = retrieval.search(db, "qqqzzz", top_k=5, recency_weight=0)
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tids == {current}, "a chunk of another version must not appear in vector hits"

    fts_res = retrieval.search(db, "унікальний термін", top_k=5, recency_weight=0)
    fts_tids = {c["transcription_id"] for c in fts_res["chunks"]}
    assert stale in fts_tids, "the FTS branch knows no epoch -- the record stays findable"


def test_other_embedding_model_excluded_from_vector_branch(db, mock_embeddings):
    other_model = _add_tx(db, "Інша модель", embedding_model="some/other-model")
    _add_chunk(db, other_model, 0, "альфа бета", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz", top_k=5, recency_weight=0)
    assert res["chunks"] == []


def test_vector_dim_mismatch_logs_error_not_silent(db, mock_embeddings, monkeypatch, caplog):
    """A mismatch of the BLOB dimension and EMBED_DIM is NO LONGER skipped silently --
    a trace must remain in the log (chunk id, size, EMBED_DIM)."""
    tid = _add_tx(db, "Пошкоджений вектор")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])
    # We substitute EMBED_DIM LIVE (after inserting a chunk of size 4) --
    # it imitates a dimension desync without violating the (embedding_model,
    # embedding_version) filter, which was supposed to narrow rows to the compatible ones.
    monkeypatch.setattr(embeddings, "EMBED_DIM", 8)
    with caplog.at_level("ERROR"):
        res = retrieval.search(db, "qqqzzz", top_k=5, recency_weight=0)
    assert res["chunks"] == []
    assert any("vector size" in r.message for r in caplog.records)


# ============================================================
# production-rag-wave-b-07: query rewriting (rewrite=)
# ============================================================

def test_rewrite_default_off_does_not_call_rewrite_query(db, mock_embeddings, monkeypatch):
    from app.services import query_rewrite

    def _boom(*a, **kw):
        raise AssertionError("rewrite_query must NOT be called when rewrite is disabled")
    monkeypatch.setattr(query_rewrite, "rewrite_query", _boom)
    monkeypatch.delenv("RAG_QUERY_REWRITE", raising=False)

    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz")  # rewrite not passed -> env disabled -> False
    assert len(res["chunks"]) == 1


def test_rewrite_false_matches_env_off_default(db, mock_embeddings, monkeypatch):
    """production-rag-wave-b-08 (Minor 19): proves what is actually
    compared -- `rewrite=False` (explicit) and a call without the flag under a disabled
    env give an IDENTICAL output (both paths lead to the same post-story
    code). That `rewrite_query` is NOT called is proven by the neighbouring
    `_boom` test (`test_rewrite_default_off_does_not_call_rewrite_query`), not
    duplicated here."""
    monkeypatch.delenv("RAG_QUERY_REWRITE", raising=False)
    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч", [1.0, 0, 0, 0])

    baseline = retrieval.search(db, "бюджет проєкту")
    explicit_off = retrieval.search(db, "бюджет проєкту", rewrite=False)
    assert explicit_off == baseline


def test_rewrite_true_merges_variant_fts_hit_via_rrf(db, mock_embeddings, monkeypatch):
    """rewrite=True: a variant finds a chunk that the original query does NOT find
    by ANY branch (vector disabled -- isolating the effect to FTS) -- and it still
    appears in the output thanks to RRF fusion with the variant's sub-query."""
    from app.services import query_rewrite
    monkeypatch.setattr(embeddings, "is_available", lambda: False)

    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "інший фрагмент без спільних слів", None)
    other = _add_tx(db, "Інший")
    _add_chunk(db, other, 0, "унікальнийтермінваріанта", None)

    monkeypatch.setattr(query_rewrite, "rewrite_query",
                        lambda q, **kw: ["унікальнийтермінваріанта"])

    baseline = retrieval.search(db, "qqqzzz", top_k=5)
    assert baseline["chunks"] == [], "the original query must not find a single chunk"

    res = retrieval.search(db, "qqqzzz", top_k=5, rewrite=True)
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tids == {other}, "a chunk found only by the variant must get into the output"


def test_rewrite_true_no_variants_behaves_like_off(db, mock_embeddings, monkeypatch):
    from app.services import query_rewrite
    monkeypatch.setattr(query_rewrite, "rewrite_query", lambda q, **kw: [])
    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])

    with_flag = retrieval.search(db, "qqqzzz", rewrite=True)
    without_flag = retrieval.search(db, "qqqzzz", rewrite=False)
    assert with_flag == without_flag


def test_rewrite_explain_rewrites_empty_when_disabled(db, mock_embeddings):
    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])
    res = retrieval.search(db, "qqqzzz", explain=True, rewrite=False)
    assert res["chunks"][0]["why"]["rewrites"] == []


def test_rewrite_explain_rewrites_lists_variants_when_enabled(db, mock_embeddings, monkeypatch):
    from app.services import query_rewrite
    monkeypatch.setattr(query_rewrite, "rewrite_query", lambda q, **kw: ["варіант один"])
    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])
    res = retrieval.search(db, "qqqzzz", explain=True, rewrite=True)
    assert res["chunks"][0]["why"]["rewrites"] == ["варіант один"]


def test_rewrite_env_flag_enables_call_without_explicit_param(db, mock_embeddings, monkeypatch):
    """RAG_QUERY_REWRITE=1 in env + rewrite not passed -> search calls
    rewrite_query -- exactly how the gate (evals/gate.py) measures the effect without a code change."""
    from app.services import query_rewrite
    called = []
    monkeypatch.setattr(query_rewrite, "rewrite_query",
                        lambda q, **kw: (called.append(q), [])[1])
    monkeypatch.setenv("RAG_QUERY_REWRITE", "1")

    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])

    retrieval.search(db, "qqqzzz")
    assert called == ["qqqzzz"]


def test_rewrite_true_survives_missing_comment_index(db, mock_embeddings, monkeypatch):
    """Opus UNASKED 6: an old DB without migration v37 (comment index) must not
    silently throw out the variants' sub-queries together with the comment lists -- the fallback
    drops ONLY the comment lists, not the positional slice `[:2]`."""
    from app.services import query_rewrite

    conn = sqlite3.connect(db)
    conn.executescript(
        "DROP TABLE comment_chunks_fts; DROP TABLE comment_chunks; DROP TABLE comments;")
    conn.commit()
    conn.close()

    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "інший фрагмент без спільних слів", None)
    other = _add_tx(db, "Інший")
    _add_chunk(db, other, 0, "унікальнийтермінваріанта", None)

    monkeypatch.setattr(query_rewrite, "rewrite_query",
                        lambda q, **kw: ["унікальнийтермінваріанта"])

    res = retrieval.search(db, "qqqzzz", top_k=5, rewrite=True)
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tids == {other}, "the variant's hit must stay in the output even without the comment index"


# ============================================================
# editable-title-description-03: title/display_name in the output
# ============================================================

def test_search_result_carries_title_and_display_name(db, mock_embeddings):
    tid = _add_tx(db, "rec_2026-09-17_final.mp3", meeting_date="2026-09-17")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET title = ? WHERE id = ?",
                 ("Планерка Фонду", tid))
    conn.commit()
    conn.close()
    _add_chunk(db, tid, 0, "домовились про кошторис на жовтень", [1, 0, 0, 0])

    hit = retrieval.search(db, "кошторис", top_k=3)["chunks"][0]
    assert hit["source_name"] == "rec_2026-09-17_final.mp3"   # provenance unchanged
    assert hit["title"] == "Планерка Фонду"
    assert hit["display_name"] == "Планерка Фонду"
    assert "why" in hit


def test_search_display_name_falls_back_to_source_name(db, mock_embeddings):
    tid = _add_tx(db, "Дзвінок з клієнтом", meeting_date="2026-09-17")
    _add_chunk(db, tid, 0, "домовились про кошторис на жовтень", [1, 0, 0, 0])

    hit = retrieval.search(db, "кошторис", top_k=3)["chunks"][0]
    assert hit["title"] is None
    assert hit["display_name"] == "Дзвінок з клієнтом"


# ============================================================
# directions-projects-tags-03: directions tree, tags, period
# ============================================================

def _node(path, name, parent_id=None):
    """id of a tree node by name (some of the names init_database has already seeded)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM categories WHERE name_norm = ?",
                       (categories.norm_name(name),)).fetchone()
    if row:
        cid = row["id"]
        conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (parent_id, cid))
    else:
        cid = categories.create_category(conn, name, parent_id=parent_id)["id"]
    conn.commit()
    conn.close()
    return cid


def _tag(path, tid, *names):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    categories.set_tags(conn, tid, add=list(names))
    conn.commit()
    conn.close()


def _tids(res):
    return {c["transcription_id"] for c in res["chunks"]}


def test_direction_expands_into_its_projects_in_both_branches(db, mock_embeddings):
    """direction id = itself + its projects, and the same in both branches."""
    fond = _node(db, "Фонд Робота")
    acmecorp = _node(db, "Акмекорп", parent_id=fond)
    own = _add_tx(db, "Планерка фонду", category_id=fond)
    proj = _add_tx(db, "Дзвінок Акмекорп", category_id=acmecorp)
    other = _add_tx(db, "Особисте", category_id=_node(db, "Особисте"))
    for tid in (own, proj, other):
        _add_chunk(db, tid, 0, "спільне слово альфа", [1.0, 0, 0, 0])

    res = retrieval.search(db, "альфа", category_id=fond)
    assert _tids(res) == {own, proj}
    by = {m for c in res["chunks"] for m in c["why"]["by"]}
    assert {"vector", "fts"} <= by


def test_project_id_returns_only_its_own_records(db, mock_embeddings):
    fond = _node(db, "Фонд Робота")
    acmecorp = _node(db, "Акмекорп", parent_id=fond)
    own = _add_tx(db, "Планерка фонду", category_id=fond)
    proj = _add_tx(db, "Дзвінок Акмекорп", category_id=acmecorp)
    _add_chunk(db, own, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _add_chunk(db, proj, 0, "спільне слово альфа", [1.0, 0, 0, 0])

    assert _tids(retrieval.search(db, "альфа", category_id=acmecorp)) == {proj}


def test_scope_tids_still_narrows_alongside_tree(db, mock_embeddings):
    """The second scope layer (Track 2) was not hurt by the tree."""
    fond = _node(db, "Фонд Робота")
    acmecorp = _node(db, "Акмекорп", parent_id=fond)
    own = _add_tx(db, "Планерка фонду", category_id=fond)
    proj = _add_tx(db, "Дзвінок Акмекорп", category_id=acmecorp)
    _add_chunk(db, own, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _add_chunk(db, proj, 0, "спільне слово альфа", [1.0, 0, 0, 0])

    res = retrieval.search(db, "альфа", category_id=fond, scope_tids=[proj])
    assert _tids(res) == {proj}


def test_tags_filter_keeps_only_tagged_records(db, mock_embeddings):
    a = _add_tx(db, "З тегом")
    b = _add_tx(db, "Без тега")
    _add_chunk(db, a, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _add_chunk(db, b, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _tag(db, a, "AI")

    assert _tids(retrieval.search(db, "альфа", tags=["AI"])) == {a}


def test_two_tags_are_or_not_and(db, mock_embeddings):
    a = _add_tx(db, "AI")
    b = _add_tx(db, "Інвестори")
    c = _add_tx(db, "Ні те ні те")
    for tid in (a, b, c):
        _add_chunk(db, tid, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _tag(db, a, "AI")
    _tag(db, b, "інвестори")

    assert _tids(retrieval.search(db, "альфа", tags=["AI", "інвестори"])) == {a, b}


def test_tag_with_category_is_and(db, mock_embeddings):
    fond = _node(db, "Фонд Робота")
    acmecorp = _node(db, "Акмекорп", parent_id=fond)
    inside = _add_tx(db, "Тег у напрямку", category_id=acmecorp)
    outside = _add_tx(db, "Тег поза напрямком", category_id=_node(db, "Особисте"))
    plain = _add_tx(db, "Напрямок без тега", category_id=acmecorp)
    for tid in (inside, outside, plain):
        _add_chunk(db, tid, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _tag(db, inside, "AI")
    _tag(db, outside, "AI")

    res = retrieval.search(db, "альфа", category_id=fond, tags=["AI"])
    assert _tids(res) == {inside}


def test_unknown_tag_returns_nothing_not_everything(db, mock_embeddings):
    """An unknown name narrows the output to empty -- "filter ignored"
    would mean the filter silently leaks."""
    a = _add_tx(db, "З тегом")
    _add_chunk(db, a, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _tag(db, a, "AI")

    assert retrieval.search(db, "альфа", tags=["неіснуючий"])["chunks"] == []


def test_since_and_until_are_inclusive(db, mock_embeddings):
    early = _add_tx(db, "Раніше", meeting_date="2026-09-01")
    mid = _add_tx(db, "Всередині", meeting_date="2026-09-10")
    late = _add_tx(db, "Пізніше", meeting_date="2026-09-20")
    for tid in (early, mid, late):
        _add_chunk(db, tid, 0, "спільне слово альфа", [1.0, 0, 0, 0])

    assert _tids(retrieval.search(db, "альфа", since="2026-09-01",
                                  until="2026-09-10")) == {early, mid}
    assert _tids(retrieval.search(db, "альфа", since="2026-09-20")) == {late}
    assert _tids(retrieval.search(db, "альфа", until="2026-09-01")) == {early}


def test_bad_date_raises_instead_of_silently_widening(db, mock_embeddings):
    with pytest.raises(ValueError):
        retrieval.search(db, "альфа", since="минулого тижня")


def _comment(path, tid, body):
    from app.services import comments
    c = comments.create(path, "transcription", tid, body, kind="note")
    comments.index_comment(path, c["id"])
    return c["id"]


@pytest.fixture
def mock_comment_index(monkeypatch):
    """index_comment encodes the comment text -- without a fake the real
    model (2 GB) would arrive here. The same vector for all: the test measures the FILTER, not the cosine."""
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))


def test_comments_follow_the_same_tag_and_date_axes(db, mock_embeddings, mock_comment_index):
    """A comment on a foreign record is cut off by the same axes as the record."""
    a = _add_tx(db, "Свій", meeting_date="2026-09-10")
    b = _add_tx(db, "Чужий", meeting_date="2026-09-20")
    _tag(db, a, "AI")
    _comment(db, a, "бюджет свій")
    _comment(db, b, "бюджет чужий")

    bodies = lambda res: [c["text"] for c in res["chunks"]
                          if c["source_type"] == "comment"]
    assert bodies(retrieval.search(db, "бюджет", top_k=5, tags=["AI"])) == ["бюджет свій"]
    assert bodies(retrieval.search(db, "бюджет", top_k=5,
                                   until="2026-09-10")) == ["бюджет свій"]
    assert bodies(retrieval.search(db, "бюджет", top_k=5,
                                   tags=["неіснуючий"])) == []


def test_comments_of_a_project_survive_direction_filter(db, mock_embeddings, mock_comment_index):
    fond = _node(db, "Фонд Робота")
    acmecorp = _node(db, "Акмекорп", parent_id=fond)
    proj = _add_tx(db, "Дзвінок Акмекорп", category_id=acmecorp)
    other = _add_tx(db, "Особисте", category_id=_node(db, "Особисте"))
    _comment(db, proj, "бюджет проєкту")
    _comment(db, other, "бюджет чужий")

    res = retrieval.search(db, "бюджет", top_k=5, category_id=fond)
    assert [c["text"] for c in res["chunks"]
            if c["source_type"] == "comment"] == ["бюджет проєкту"]


# ============================================================
# fts-filter-pushdown: the record filter is not pushed into FTS5
# ============================================================
# The form `rowid IN (SELECT ...)` hands FTS5 a list of rowids as a constraint of the virtual
# table (plan `INDEX ...:=M...`), and FTS5 runs MATCH separately for each id: on a snapshot of the
# live DB 14 s per query vs 0.03 s when the filter stays outside FTS5.

def _vtab_plan(path, sql):
    """EXPLAIN QUERY PLAN rows about the virtual table for the SQL that actually ran."""
    conn = sqlite3.connect(path)
    try:
        return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)
                if "VIRTUAL TABLE" in r[3]]
    finally:
        conn.close()


def _fts_rowid_pushed(plan_lines):
    """`INDEX 32:=M2` is a rowid constraint in FTS5; `INDEX 32:M2` is MATCH only.
    `=`, `<`, `>` in the FTS5 idxStr are a rowid constraint wherever they stand."""
    import re
    out = []
    for line in plan_lines:
        m = re.search(r"INDEX \d+:(\S*)", line)
        if m and set(m.group(1)) & set("=<>"):
            out.append(line)
    return out


def test_fts_search_keeps_record_filter_out_of_fts5(db, monkeypatch):
    from contextlib import contextmanager

    tid = _add_tx(db, "Дзвінок з інвестором")
    _add_chunk(db, tid, 0, "Тарас підписав договір оренди", None)
    _add_chunk(db, tid, 1, "Олена надіслала договір юристу", None)
    seen: list[str] = []
    real = retrieval.get_db_connection

    @contextmanager
    def tracing(path):
        with real(path) as conn:
            conn.set_trace_callback(seen.append)
            yield conn

    monkeypatch.setattr(retrieval, "get_db_connection", tracing)
    hits = retrieval._fts_search(db, "договір Тарас", 10)
    assert {h[0] for h in hits}, "the fixture must be findable"
    sql = next(s for s in seen if "chunks_fts MATCH" in s)
    plan = _vtab_plan(db, sql)
    assert plan and not _fts_rowid_pushed(plan), plan


def test_research_candidates_keep_record_filter_out_of_fts5(db):
    from app.db.connection import get_db_connection
    from app.services import research

    tid = _add_tx(db, "Зустріч щодо оренди", transcript="Тарас підписав договір оренди")
    _add_tx(db, "Інша зустріч", transcript="Олена говорила про бюджет")
    seen: list[str] = []
    with get_db_connection(db) as conn:
        conn.set_trace_callback(seen.append)
        ids = research._candidate_ids(conn, '"договір"', None)
    assert ids == [tid]
    sql = next(s for s in seen if "transcriptions_fts MATCH" in s)
    plan = _vtab_plan(db, sql)
    assert plan and not _fts_rowid_pushed(plan), plan


# ============================================================
# svc_retrieval catalog: comment target labels
# ============================================================

def test_comment_target_labels_are_translated():
    from app.core.i18n import tr
    assert tr("svc_retrieval.comment.target_record", lang="en", id=5) == "Record #5"
    assert tr("svc_retrieval.comment.target_audio", lang="en", id=5) == "Audio #5"
    assert tr("svc_retrieval.comment.target_record", lang="uk", id=5) == "Запис #5"
