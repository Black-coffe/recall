"""`why` на кожному результаті retrieval.search() (Історія 02, C2/C3).

Embeddings мокаються (EMBED_DIM=4) — так само як у test_retrieval.py /
test_comments_ranking.py. FTS5 справжній.
"""
import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import comments, embeddings, retrieval
from evals.metrics import source_ids_from_chunks


OLD_KEYS = {
    "chunk_id", "transcription_id", "source_name", "source_type", "doc_type",
    "meeting_date", "speaker", "start_time", "end_time", "page", "section",
    "text", "score", "matched_by",
}


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
    monkeypatch.setattr(embeddings, "embed_query",
                        lambda q: _unit([1.0, 0.0, 0.0, 0.0]))
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


def _add_chunk(path, tid, idx, text, vec=(1.0, 0.0, 0.0, 0.0), has_vec=True):
    conn = sqlite3.connect(path)
    blob = _unit(vec).tobytes() if has_vec else None
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text, embedding) VALUES (?, ?, 0, 10, 'Ви', ?, ?)",
        (tid, idx, text, blob))
    conn.commit(); conn.close()


def _comment(path, tid, body, kind="note", **kw):
    c = comments.create(path, "transcription", tid, body, kind=kind, **kw)
    comments.index_comment(path, c["id"])
    return c


# ============================================================
# Компактний why на кожному елементі
# ============================================================

def test_every_chunk_has_why_with_required_keys(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    _add_chunk(db, tid, 1, "інша тема геть без спільних слів")
    res = retrieval.search(db, "бюджет проєкту", top_k=5)
    assert res["chunks"], "має бути хоч один результат"
    for c in res["chunks"]:
        why = c["why"]
        assert set(why.keys()) >= {"src", "rrf", "rec", "by", "top"}
        assert why["top"] in ("rerank", "recency", "rrf", "comment_boost")
        assert "rr" not in why  # rerank не вмикали


def test_rr_present_iff_rerank_score_present(db, mock_embeddings, monkeypatch):
    from app.services import reranker
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    _add_chunk(db, tid, 1, "бюджет команди")
    monkeypatch.setattr(reranker, "rerank", lambda q, items, text_key="text": [
        {**it, "rerank_score": 0.9 - 0.1 * i} for i, it in enumerate(items)
    ])
    res = retrieval.search(db, "бюджет", top_k=5, rerank=True)
    for c in res["chunks"]:
        assert ("rr" in c["why"]) == ("rerank_score" in c)
        if "rr" in c["why"]:
            assert c["why"]["rr"] == c["rerank_score"]


def test_rrf_and_rec_are_distinct_from_score(db, mock_embeddings):
    tid = _add_tx(db, date="2026-01-01")  # старий, щоб recency != 1
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч гривень")
    res = retrieval.search(db, "бюджет проєкту", top_k=5)
    c = res["chunks"][0]
    why = c["why"]
    assert why["rrf"] != c["score"]
    assert why["rec"] != c["score"]
    assert why["rrf"] != why["rec"]


def test_comment_entry_has_why_with_comment_boost_possible(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    _comment(db, tid, "бюджет проєкту дванадцять тисяч", kind="correction")
    res = retrieval.search(db, "бюджет проєкту", top_k=5)
    comment_entries = [c for c in res["chunks"] if c["source_type"] == "comment"]
    assert comment_entries
    for c in comment_entries:
        assert "why" in c
        assert c["why"]["top"] in ("rerank", "recency", "rrf", "comment_boost")


# ============================================================
# Історія 09: `top` називає стадію, яка ФАКТИЧНО зрушила результат
# ============================================================

def test_zero_recency_weight_never_reports_recency(db, mock_embeddings):
    """rec_w=0 → свіжість помножила скор рівно на 1.0 — це не мало бути
    прочитане як «recency зрушила результат» (знахідка 6/09)."""
    tid = _add_tx(db, date="2020-01-01")  # старий запис — recency01 > 0, але вага 0
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    res = retrieval.search(db, "бюджет проєкту", top_k=5, recency_weight=0.0)
    assert res["chunks"]
    for c in res["chunks"]:
        assert c["why"]["top"] != "recency"


def test_rerank_actually_reordered_pool_reports_rerank(db, mock_embeddings, monkeypatch):
    """Кандидат отримує `top == "rerank"` лише коли rerank ДІЙСНО переставив
    його позицію в пулі — не просто тому, що він у пулі опинився."""
    from app.services import reranker
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту альфа")
    _add_chunk(db, tid, 1, "бюджет проєкту бета")

    def _reverse_rerank(q, items, text_key="text"):
        # Розвертає пул — гарантовано змінює позицію КОЖНОГО елемента
        # (за умови, що їх більше одного).
        rev = list(reversed(items))
        return [{**it, "rerank_score": 0.9 - 0.1 * i} for i, it in enumerate(rev)]

    monkeypatch.setattr(reranker, "rerank", _reverse_rerank)
    res = retrieval.search(db, "бюджет проєкту", top_k=5, rerank=True, recency_weight=0.0)
    assert len(res["chunks"]) >= 2
    for c in res["chunks"]:
        assert c["why"]["top"] == "rerank"


def test_nonzero_comment_weight_reports_comment_boost(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    _comment(db, tid, "бюджет проєкту дванадцять тисяч", kind="correction")
    res = retrieval.search(db, "бюджет проєкту", top_k=5, comment_weight=0.6, recency_weight=0.0)
    comment_entries = [c for c in res["chunks"] if c["source_type"] == "comment"]
    assert comment_entries
    for c in comment_entries:
        assert c["why"]["top"] == "comment_boost"


# ============================================================
# explain=True
# ============================================================

def test_explain_false_has_no_stages(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    res = retrieval.search(db, "бюджет проєкту", top_k=5, explain=False)
    for c in res["chunks"]:
        assert "stages" not in c["why"]


def test_explain_true_adds_stages_with_raw_scores(db, mock_embeddings):
    tid = _add_tx(db)
    # чанк, знайдений і вектором, і FTS (спільний термін "бюджет")
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч гривень")
    res = retrieval.search(db, "бюджет проєкту", top_k=5, explain=True)
    c = res["chunks"][0]
    why = c["why"]
    assert "stages" in why
    stages = why["stages"]
    assert "vector" in stages
    assert "fts" in stages
    assert "pos" in stages["vector"] and "sim" in stages["vector"]
    assert "pos" in stages["fts"] and "bm25" in stages["fts"]
    # сирі скори стадій — не позиції, і не дорівнюють rrf/score
    assert stages["vector"]["sim"] not in (why["rrf"], c["score"])
    assert stages["fts"]["bm25"] not in (why["rrf"], c["score"])


def test_stage_raw_scores_are_not_swapped(db, mock_embeddings):
    """Знахідка 09.3: `sim`/`bm25` перевірялись лише на «не дорівнює rrf», тож
    перестановка сирих скорів між стадіями лишалась непоміченою. Тут звіряємо
    значення точно з тим, що повертають самі `_vector_search`/`_fts_search`."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч гривень")
    query = "бюджет проєкту"
    vec_raw = dict(retrieval._vector_search(db, query, 40, None, None))
    fts_raw = dict(retrieval._fts_search(db, query, 40, None, None))
    res = retrieval.search(db, query, top_k=5, explain=True)
    c = res["chunks"][0]
    stages = c["why"]["stages"]
    assert stages["vector"]["sim"] == pytest.approx(vec_raw[c["chunk_id"]])
    assert stages["fts"]["bm25"] == pytest.approx(fts_raw[c["chunk_id"]])
    # sanity: сирі значення різних стадій дійсно різні — перестановка мала б
    # чим упіймати.
    assert stages["vector"]["sim"] != stages["fts"]["bm25"]


def test_weights_final_raw_and_search_capped_have_value_asserts(db, mock_embeddings):
    """Знахідка 09.3: `weights`, `final_raw`, `capped` не мали ЖОДНОГО ассерту
    в наборі. `capped` перейменований на `search_capped` (знахідка 12/09)."""
    tid = _add_tx(db, date="2026-01-01")
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч гривень")
    res = retrieval.search(db, "бюджет проєкту", top_k=5, explain=True,
                           recency_weight=0.25, comment_weight=0.6)
    c = res["chunks"][0]
    why = c["why"]
    assert why["weights"] == {"recency": 0.25, "comment": 0.6}
    expected_final = why["rrf"] * (1.0 + 0.25 * why["rec"])
    assert why["final_raw"] == pytest.approx(expected_final, rel=1e-3)
    assert why["search_capped"] == {"comment_share": False, "diversity": False}
    assert "capped" not in why


def test_vector_only_candidate_has_no_fts_stage(db, mock_embeddings):
    tid = _add_tx(db)
    # унікальне слово, що НЕ токенізується у FTS-запиті іншими чанками,
    # а embedding робить його top-1 за вектором.
    _add_chunk(db, tid, 0, "zzzcompletelyunmatchedzzz", vec=(1.0, 0.0, 0.0, 0.0))
    res = retrieval.search(db, "щось геть інше без спільних слів взагалі", top_k=5, explain=True)
    # запит не збігається лексично з текстом чанка → лише вектор
    assert res["chunks"], "вектор має знайти хоч щось"
    c = res["chunks"][0]
    assert "vector" in c["why"]["stages"]
    assert "fts" not in c["why"]["stages"]


def test_fts_only_candidate_has_no_vector_stage(db, mock_embeddings, monkeypatch):
    # Вимикаємо вектор повністю — залишається тільки FTS.
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "унікальнийтермінбезвектора проєкту", has_vec=False)
    res = retrieval.search(db, "унікальнийтермінбезвектора", top_k=5, explain=True)
    assert res["chunks"]
    c = res["chunks"][0]
    assert "fts" in c["why"]["stages"]
    assert "vector" not in c["why"]["stages"]


def test_order_identical_with_and_without_explain(db, mock_embeddings):
    tid1 = _add_tx(db, name="Перший", date="2026-08-01")
    tid2 = _add_tx(db, name="Другий", date="2026-01-01")
    _add_chunk(db, tid1, 0, "бюджет проєкту двадцять тисяч")
    _add_chunk(db, tid2, 0, "бюджет команди тридцять тисяч")
    _comment(db, tid1, "бюджет проєкту дванадцять тисяч", kind="correction")
    plain = retrieval.search(db, "бюджет проєкту команди", top_k=5, explain=False)
    explained = retrieval.search(db, "бюджет проєкту команди", top_k=5, explain=True)
    plain_order = [c["chunk_id"] for c in plain["chunks"]]
    explained_order = [c["chunk_id"] for c in explained["chunks"]]
    assert plain_order == explained_order
    assert len(plain_order) == len(explained_order)


# ============================================================
# Сумісність зі старим контрактом
# ============================================================

def test_old_keys_still_present(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    _comment(db, tid, "уточнення бюджету", kind="correction")
    res = retrieval.search(db, "бюджет проєкту", top_k=5, explain=True)
    for c in res["chunks"]:
        assert OLD_KEYS <= set(c.keys())


def test_source_ids_from_chunks_unaffected(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    res = retrieval.search(db, "бюджет проєкту", top_k=5, explain=True)
    ids = source_ids_from_chunks(res["chunks"])
    assert tid in ids
