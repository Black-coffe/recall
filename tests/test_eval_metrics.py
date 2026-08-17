"""Юніт-тести детермінованої математики evals/metrics.py (REMEDIATION_PLAN
Волна 3, T6.1) — синтетичні дані, БЕЗ реального Claude API чи GPU/embeddings.

Перевіряє тільки recall@k / citation-rate обчислення над готовими списками
чанків і рядками-відповідями — не саму retrieval/RAG-логіку (та вже покрита
tests/test_retrieval.py) і не мережеві виклики (evals/run_eval.py).
"""
from evals import metrics


def _chunk(tid, source_name="Мітинг"):
    return {"transcription_id": tid, "source_name": source_name}


# ============================================================
# source_ids_from_chunks
# ============================================================

def test_source_ids_from_chunks_dedupes_preserving_order():
    chunks = [_chunk(5), _chunk(3), _chunk(5), _chunk(7)]
    assert metrics.source_ids_from_chunks(chunks) == [5, 3, 7]


def test_source_ids_from_chunks_skips_missing_id():
    chunks = [{"source_name": "x"}, _chunk(1)]
    assert metrics.source_ids_from_chunks(chunks) == [1]


# ============================================================
# recall_at_k
# ============================================================

def test_recall_at_k_full_hit():
    assert metrics.recall_at_k([1, 2], [1, 2, 3]) == 1.0


def test_recall_at_k_partial_hit():
    assert metrics.recall_at_k([1, 2], [1, 9, 10]) == 0.5


def test_recall_at_k_zero_hit():
    assert metrics.recall_at_k([1, 2], [9, 10]) == 0.0


def test_recall_at_k_none_when_no_expected():
    assert metrics.recall_at_k([], [1, 2, 3]) is None


def test_recall_at_k_duplicate_expected_not_double_counted():
    # {1,1,2} як set -> {1,2}; retrieved має обидва -> recall=1.0, не 1.5
    assert metrics.recall_at_k([1, 1, 2], [1, 2]) == 1.0


# ============================================================
# source_name_hit
# ============================================================

def test_source_name_hit_true_case_insensitive_substring():
    chunks = [_chunk(1, source_name="Синк з Маркетингом (квітень)")]
    assert metrics.source_name_hit(["синк з маркетингом"], chunks) is True


def test_source_name_hit_false_when_no_match():
    chunks = [_chunk(1, source_name="Ретро команди")]
    assert metrics.source_name_hit(["синк з маркетингом"], chunks) is False


def test_source_name_hit_none_when_no_criteria():
    chunks = [_chunk(1, source_name="Ретро команди")]
    assert metrics.source_name_hit([], chunks) is None


def test_source_name_hit_handles_missing_source_name():
    chunks = [{"transcription_id": 1}]  # без source_name
    assert metrics.source_name_hit(["x"], chunks) is False


# ============================================================
# has_citation / cited_indices / citations_in_range
# ============================================================

def test_has_citation_true():
    assert metrics.has_citation("Рішення ухвалили [1][3].") is True


def test_has_citation_false_for_empty_or_no_marker():
    assert metrics.has_citation("") is False
    assert metrics.has_citation("Просто текст без цитат.") is False
    assert metrics.has_citation(None) is False


def test_cited_indices_extracts_all_with_duplicates_in_order():
    assert metrics.cited_indices("Факт [2]. Ще факт [1][2].") == [2, 1, 2]


def test_citations_in_range_true_when_all_within_found():
    assert metrics.citations_in_range("Дивись [1] і [3].", found=3) is True


def test_citations_in_range_false_when_out_of_bounds():
    # Модель зацитувала [5], а фрагментів надано лише 3 -> "галюцинована" цитата.
    assert metrics.citations_in_range("Дивись [5].", found=3) is False


def test_citations_in_range_true_when_no_citations_present():
    assert metrics.citations_in_range("Без цитат.", found=3) is True


# ============================================================
# evaluate_item
# ============================================================

def test_evaluate_item_retrieval_only_no_answer():
    golden = {
        "id": "q1",
        "expected_transcription_ids": [1, 2],
        "expected_source_name_contains": ["синк"],
    }
    chunks = [_chunk(1, "Синк команди"), _chunk(9, "Інше")]
    row = metrics.evaluate_item(golden, chunks, answer=None)
    assert row["id"] == "q1"
    assert row["recall_at_k"] == 0.5
    assert row["source_name_hit"] is True
    assert row["has_citation"] is None
    assert row["citations_valid"] is None


def test_evaluate_item_with_answer_valid_citations():
    golden = {"id": "q2", "expected_transcription_ids": [1]}
    chunks = [_chunk(1), _chunk(2)]
    row = metrics.evaluate_item(golden, chunks, answer="Рішення таке [1].")
    assert row["recall_at_k"] == 1.0
    assert row["has_citation"] is True
    assert row["citations_valid"] is True


def test_evaluate_item_with_answer_hallucinated_citation():
    golden = {"id": "q3", "expected_transcription_ids": [1]}
    chunks = [_chunk(1)]  # found=1
    row = metrics.evaluate_item(golden, chunks, answer="Дивись джерело [4].")
    assert row["has_citation"] is True
    assert row["citations_valid"] is False


def test_evaluate_item_negative_case_no_expected_sources():
    golden = {"id": "neg", "expected_transcription_ids": [], "expected_source_name_contains": []}
    chunks = []
    row = metrics.evaluate_item(golden, chunks, answer="У архіві не знайдено релевантної інформації.")
    assert row["recall_at_k"] is None
    assert row["source_name_hit"] is None
    assert row["has_citation"] is False


# ============================================================
# aggregate
# ============================================================

def test_aggregate_basic_means():
    results = [
        {"recall_at_k": 1.0, "source_name_hit": True, "has_citation": True, "citations_valid": True},
        {"recall_at_k": 0.0, "source_name_hit": False, "has_citation": False, "citations_valid": True},
    ]
    agg = metrics.aggregate(results)
    assert agg["total_items"] == 2
    assert agg["recall_at_k"]["mean"] == 0.5
    assert agg["recall_at_k"]["n"] == 2
    assert agg["source_name_hit_rate"]["mean"] == 0.5
    assert agg["citation_rate"]["mean"] == 0.5
    assert agg["citations_valid_rate"]["mean"] == 1.0


def test_aggregate_skips_none_values_not_zero():
    # Один запис без recall-критерію (None) НЕ повинен тягнути середнє до 0 —
    # він просто виключається зі знаменника.
    results = [
        {"recall_at_k": 1.0, "source_name_hit": None, "has_citation": None, "citations_valid": None},
        {"recall_at_k": None, "source_name_hit": None, "has_citation": None, "citations_valid": None},
    ]
    agg = metrics.aggregate(results)
    assert agg["recall_at_k"]["mean"] == 1.0
    assert agg["recall_at_k"]["n"] == 1
    assert agg["source_name_hit_rate"] == {"mean": None, "n": 0}


def test_aggregate_empty_results_list():
    agg = metrics.aggregate([])
    assert agg["total_items"] == 0
    assert agg["recall_at_k"] == {"mean": None, "n": 0}
