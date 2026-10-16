"""Deterministic metric math for evals/ (REMEDIATION_PLAN Wave 3, T6.1).

There are deliberately no network/GPU calls here: this whole module must
be testable offline (``tests/test_eval_metrics.py``) on synthetic
data, so a change to the recall@k / citation-rate formulas can be checked without a
real API key or DB. Query building (retrieval.search) and answer generation
(rag.answer_question) live separately, in ``run_eval.py``.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional


# The citation format in the RAG answer matches the system prompt
# app/services/rag.py::_RAG_SYSTEM_PROMPT: "[n]" (several allowed: [1][3]).
_CITATION_RE = re.compile(r"\[(\d+)\]")


def source_ids_from_chunks(chunks: Iterable[dict]) -> list[int]:
    """Extract transcription_id from a list of chunks (as returned by retrieval.search),
    keeping order and without duplicates (first occurrence = highest rank)."""
    seen: set[int] = set()
    out: list[int] = []
    for ch in chunks:
        tid = ch.get("transcription_id")
        if tid is None or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def recall_at_k(expected_ids: Iterable[int], retrieved_ids: Iterable[int]) -> Optional[float]:
    """Share of expected sources found in the top-k retrieved.

    ``|expected ∩ retrieved| / |expected|``. Returns ``None`` if
    ``expected_ids`` is empty (a golden item without this criterion is counted
    neither as a failure nor as a success, it is just skipped in aggregation).
    """
    expected = set(expected_ids)
    if not expected:
        return None
    retrieved = set(retrieved_ids)
    return len(expected & retrieved) / len(expected)


def source_name_hit(name_substrings: Iterable[str], chunks: Iterable[dict]) -> Optional[bool]:
    """Whether any retrieved chunk has a source whose ``source_name`` contains
    (case-insensitive) at least one of the expected substrings.

    A more robust alternative to ``expected_transcription_ids``: the transcript ID
    changes when the archive is re-ingested, the name usually does not. Returns ``None``
    if ``name_substrings`` is empty (criterion not set).
    """
    subs = [s.lower() for s in name_substrings if s]
    if not subs:
        return None
    names = [str(ch.get("source_name") or "").lower() for ch in chunks]
    return any(sub in name for name in names for sub in subs)


def has_citation(answer: str) -> bool:
    """Whether the answer contains at least one citation in the ``[n]`` format."""
    return bool(_CITATION_RE.search(answer or ""))


def cited_indices(answer: str) -> list[int]:
    """All citation indices [n] found in the answer (with duplicates, in
    order of appearance) - useful for checking that citations point to fragments
    that were actually provided (1..found)."""
    return [int(m) for m in _CITATION_RE.findall(answer or "")]


def citations_in_range(answer: str, found: int) -> bool:
    """Whether ALL citations [n] in the answer point to valid fragment indices
    (1..found)? Empty answer / no citations -> True (nothing to check -
    used by evaluate_item only when found > 0 and has_citation is already True)."""
    idxs = cited_indices(answer)
    if not idxs:
        return True
    return all(1 <= i <= found for i in idxs)


def evaluate_item(golden: dict, retrieved_chunks: list[dict],
                   answer: Optional[str] = None) -> dict:
    """Compute metrics for ONE golden record against the actual result.

    ``golden`` is one golden-set record (see evals/golden_set.example.json).
    ``retrieved_chunks`` is ``retrieval.search(...)["chunks"]`` (top-k).
    ``answer`` is the RAG answer text (``rag.answer_question(...)["answer"]``),
    if answer generation was run too, otherwise ``None`` (retrieval only).

    Returns a dict with keys (all optional metrics are ``None`` if the criterion
    is not set in the golden record or no answer was generated):
      id, recall_at_k, source_name_hit, has_citation, citations_valid
    """
    retrieved_ids = source_ids_from_chunks(retrieved_chunks)
    result = {
        "id": golden.get("id"),
        "recall_at_k": recall_at_k(
            golden.get("expected_transcription_ids") or [], retrieved_ids),
        "source_name_hit": source_name_hit(
            golden.get("expected_source_name_contains") or [], retrieved_chunks),
        "has_citation": None,
        "citations_valid": None,
    }
    if answer is not None:
        result["has_citation"] = has_citation(answer)
        result["citations_valid"] = citations_in_range(answer, len(retrieved_chunks))
    return result


def aggregate(results: list[dict]) -> dict:
    """Fold a list of ``evaluate_item(...)`` results into summary metrics for the table.

    Each metric is aggregated only over records where it is not ``None`` (so
    golden records without a given criterion do not skew the mean). ``*_n`` is how
    many records the metric was computed on (for interpreting the mean).
    """
    def _agg(key: str, as_bool: bool = False) -> dict:
        vals = [r[key] for r in results if r.get(key) is not None]
        if not vals:
            return {"mean": None, "n": 0}
        if as_bool:
            vals = [1.0 if v else 0.0 for v in vals]
        return {"mean": sum(vals) / len(vals), "n": len(vals)}

    return {
        "total_items": len(results),
        "recall_at_k": _agg("recall_at_k"),
        "source_name_hit_rate": _agg("source_name_hit", as_bool=True),
        "citation_rate": _agg("has_citation", as_bool=True),
        "citations_valid_rate": _agg("citations_valid", as_bool=True),
    }
