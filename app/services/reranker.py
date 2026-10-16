"""Local cross-encoder reranker (T6.4, Wave 4).

The second stage of re-ranking RAG candidates: after the hybrid RRF merge
(vector + FTS) and the freshness/diversification heuristics (`app/services/retrieval.py`)
the top pool of candidates is run through a cross-encoder (`BAAI/bge-reranker-v2-m3`),
which scores the REAL semantic relevance of a (query, chunk text) pair, unlike
RRF, which merely merges two rankings without a common scale.

Optional and off by default (`RECALL_RERANK_ENABLED`, read by
`retrieval.py`/`rag.py`); applied ONLY to the RAG chat "Ask the archive"
(the highest cost of a mistake among all consumers of `retrieval.search`). Copilot,
categorize and MCP search call `search()` without `rerank=True`, so their behaviour
does not change.

Why bge-reranker-v2-m3 locally rather than an LLM rerank on Haiku: $0 (in the spirit of
the product, like the e5 embeddings), private (nothing leaves the machine), and the same
sentence-transformers stack as `embeddings.py`: `CrossEncoder` is already in the
installed sentence-transformers==3.4.1, no new dependency is needed.

Lazy model load on the first real call (the same pattern as
`embeddings._get_model`): the heavy model is NOT loaded while rerank is not
enabled and has never been called. Graceful degradation: if the model/
dependencies are unavailable or inference failed, a WARNING (once per process) and
the candidates are returned in the ORIGINAL order without re-ranking ("everything
degrades", as for embeddings/copilot).

GPU: there is NO process-wide GPU semaphore in the project yet (the open
finding REMEDIATION_PLAN §12 "GPU semaphore", tied to T2.5, is not
implemented). CrossEncoder is loaded as a lazy singleton (like `embeddings._model`)
and lives next to e5/whisper/pyannote on the same GPU without explicit VRAM arbitration;
this is the same (not yet solved at project level) compromise that already exists for
e5 embeddings. Rerank is enabled ONLY for the RAG chat (low frequency relative to
transcription/embedding), so the additional VRAM pressure is limited; if needed
(a weak GPU) `RERANK_DEVICE=cpu` can be forced.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# bge-reranker-v2-m3: multilingual (including UA/RU), light (568M), the same
# Hugging Face ecosystem as e5-large. Override via env for tests
# / experiments with another model.
RERANK_MODEL = os.environ.get("RECALL_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

_model = None
_model_lock = threading.Lock()
_unavailable_reason: Optional[str] = None
_warned_unavailable = False


def is_available() -> bool:
    """Whether rerank can be computed (sentence-transformers + torch import).
    Does NOT check whether the model is already loaded/downloaded; that becomes clear only at the
    real _get_model() (lazy)."""
    global _unavailable_reason
    try:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception as e:  # pragma: no cover
        _unavailable_reason = str(e)
        return False


def unavailability_reason() -> str:
    return _unavailable_reason or "sentence-transformers/torch unavailable"


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        import torch
        from sentence_transformers import CrossEncoder
        device = os.environ.get("RERANK_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        logger.info("[reranker] Loading %s on %s …", RERANK_MODEL, device)
        t0 = time.time()
        # use_safetensors=True is the same protection as embeddings.EMBED_MODEL: bge-reranker-v2-m3
        # has ONLY model.safetensors (checked: there is no pytorch_model.bin), so this is declarative,
        # but explicitly insures against transformers>=4.56, which blocks torch.load(.bin) on torch<2.6
        # (CVE-2025-32434), if someone ever switches RECALL_RERANK_MODEL to a .bin-only repo.
        _model = CrossEncoder(RERANK_MODEL, device=device,
                              automodel_args={"use_safetensors": True})
        logger.info("[reranker] Model ready in %.1fs", time.time() - t0)
        return _model


def rerank(query: str, candidates: list[dict], text_key: str = "text") -> list[dict]:
    """Re-rank candidates (a list of dicts with a text_key field) by their
    real relevance to query through the cross-encoder.

    Returns a NEW list of copies of the input dicts with an added
    "rerank_score" (float) field, sorted descending by that score.

    Graceful degradation: if the model is unavailable (import) or inference
    failed, a WARNING is logged (unavailability once per process, to avoid spam) and
    candidates are returned in the ORIGINAL order, UNCHANGED (the same list).
    """
    global _warned_unavailable
    if not candidates:
        return candidates
    if not is_available():
        if not _warned_unavailable:
            logger.warning(
                "[reranker] Unavailable (%s); rerank skipped, candidates "
                "returned in the original order.", unavailability_reason(),
            )
            _warned_unavailable = True
        return candidates
    try:
        model = _get_model()
        pairs = [[query, c.get(text_key) or ""] for c in candidates]
        t0 = time.time()
        scores = model.predict(pairs)
        elapsed = time.time() - t0
        logger.info(
            "[reranker] rerank: %d candidates in %.3fs (%.1fms/candidate)",
            len(candidates), elapsed, elapsed * 1000 / max(len(candidates), 1),
        )
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda cs: float(cs[1]), reverse=True)
        out = []
        for c, s in scored:
            c2 = dict(c)
            c2["rerank_score"] = float(s)
            out.append(c2)
        return out
    except Exception as exc:  # guard against unexpected model failures (OOM, etc.)
        logger.warning(
            "[reranker] Rerank error (%s); candidates returned in the "
            "original order.", exc, exc_info=True,
        )
        return candidates
