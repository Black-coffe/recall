"""Локальний cross-encoder reranker (T6.4, Волна 4).

Другий етап переранжирування RAG-кандидатів: після гібридного RRF-злиття
(вектор + FTS) і евристик свіжості/диверсифікації (`app/services/retrieval.py`)
топ-пул кандидатів прогонятимемо через cross-encoder (`BAAI/bge-reranker-v2-m3`),
що оцінює РЕАЛЬНУ семантичну релевантність пари (запит, текст чанку) — на
відміну від RRF, який лише зливає два ранжирування без спільної шкали.

Опційно і вимкнено за замовчуванням (`RECALL_RERANK_ENABLED`, читає
`retrieval.py`/`rag.py`) — застосовується ЛИШЕ до RAG-чату «Запитай архів»
(найвища ціна помилки з-поміж усіх споживачів `retrieval.search`). Copilot,
categorize, MCP-пошук викликають `search()` без `rerank=True` — їх поведінка
не змінюється.

Чому bge-reranker-v2-m3 локально, а не LLM-rerank на Haiku: $0 (у дусі
продукту, як e5-embeddings), приватно (нічого не йде назовні), і той самий
sentence-transformers стек, що й `embeddings.py` — `CrossEncoder` уже є у
встановленому sentence-transformers==3.4.1, нова залежність НЕ потрібна.

Lazy-load моделі при першому реальному виклику (той самий патерн, що
`embeddings._get_model`) — тяжка модель НЕ вантажиться, поки rerank не
увімкнено і жодного разу не викликано. Graceful degradation: якщо модель/
залежності недоступні або впав inference — WARNING (раз на процес) і
повертаємо кандидатів у ВИХІДНОМУ порядку без переранжирування («усе
деградує», як для embeddings/copilot).

GPU: окремого GPU-семафора на рівні процесу в проєкті ще НЕМА (відкрита
знахідка REMEDIATION_PLAN §12 "GPU-семафор", повʼязана з T2.5 — не
реалізована). CrossEncoder вантажиться lazy-singleton'ом (як `embeddings._model`)
і живе поруч з e5/whisper/pyannote на тій самій GPU без явної арбітрації VRAM —
це той самий (поки не вирішений на рівні проєкту) компроміс, що вже є для
e5-embeddings. Rerank вмикається ЛИШЕ для RAG-чату (низька частота відносно
транскрипції/embedding), тому додатковий тиск на VRAM обмежений; за потреби
(слабкий GPU) можна форсувати `RERANK_DEVICE=cpu`.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# bge-reranker-v2-m3: мультимовний (включно з UA/RU), лёгкий (568M), той
# самий Hugging Face екосистема, що e5-large. Override через env для тестів
# /ексериментів з іншою моделлю.
RERANK_MODEL = os.environ.get("RECALL_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

_model = None
_model_lock = threading.Lock()
_unavailable_reason: Optional[str] = None
_warned_unavailable = False


def is_available() -> bool:
    """Чи можна рахувати rerank (sentence-transformers + torch імпортуються).
    НЕ перевіряє, чи модель уже завантажена/скачана — це з'ясується лише при
    реальному _get_model() (lazy)."""
    global _unavailable_reason
    try:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception as e:  # pragma: no cover
        _unavailable_reason = str(e)
        return False


def unavailability_reason() -> str:
    return _unavailable_reason or "sentence-transformers/torch недоступні"


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
        logger.info("[reranker] Завантаження %s на %s …", RERANK_MODEL, device)
        t0 = time.time()
        # use_safetensors=True — той самий захист, що embeddings.EMBED_MODEL: bge-reranker-v2-m3
        # має ЛИШЕ model.safetensors (перевірено — немає pytorch_model.bin), тож це декларативно,
        # але явно страхує від transformers>=4.56, який блокує torch.load(.bin) на torch<2.6
        # (CVE-2025-32434), якщо колись хтось перемкне RECALL_RERANK_MODEL на .bin-only репо.
        _model = CrossEncoder(RERANK_MODEL, device=device,
                              automodel_args={"use_safetensors": True})
        logger.info("[reranker] Модель готова за %.1fs", time.time() - t0)
        return _model


def rerank(query: str, candidates: list[dict], text_key: str = "text") -> list[dict]:
    """Переранжувати candidates (список словників з полем text_key) за
    реальною релевантністю до query через cross-encoder.

    Повертає НОВИЙ список копій вхідних словників з доданим полем
    "rerank_score" (float), відсортований спадно за цим скором.

    Graceful degradation: якщо модель недоступна (import) або inference
    впав — лог WARNING (недоступність — раз на процес, щоб не спамити) і
    повертає candidates у ВИХІДНОМУ порядку, БЕЗ змін (той самий список).
    """
    global _warned_unavailable
    if not candidates:
        return candidates
    if not is_available():
        if not _warned_unavailable:
            logger.warning(
                "[reranker] Недоступний (%s) — rerank пропущено, кандидати "
                "повертаються у вихідному порядку.", unavailability_reason(),
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
            "[reranker] rerank: %d кандидатів за %.3fs (%.1fмс/кандидат)",
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
    except Exception as exc:  # захист від несподіваних збоїв моделі (OOM, тощо)
        logger.warning(
            "[reranker] Помилка rerank (%s) — кандидати повертаються у "
            "вихідному порядку.", exc, exc_info=True,
        )
        return candidates
