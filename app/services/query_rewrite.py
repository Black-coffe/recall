"""Search query rewriting (production-rag-wave-b-07).

A single user question does not always match, lexically or semantically, the way
the archive phrases it. `rewrite_query` asks Claude for 1-3 alternative search
formulations of the same question; they go in as ADDITIONAL vector+FTS
sub-queries into `app.services.retrieval.search` (the `rewrite=` flag), fused by
the same RRF as the original.

Best-effort by construction: an API failure or invalid JSON does NOT break the
search; an empty list is returned and a `logger.warning` is written. The flag
default (`RAG_QUERY_REWRITE`) is off; it is enabled only selectively for
measurement by the gate (`evals/gate.py`).

Non-goals (Story 07): does NOT extract scope (project/people/period) from the
question, does NOT cache variants in the DB, does NOT rewrite with the local
model (Ollama) -- decision A11.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from app.services.models import HAIKU_4_5


logger = logging.getLogger(__name__)

#: Cheap fast model for the auxiliary call before the main RAG query -- not the
#: same Claude that composes the answer. Override via env for tuning without a
#: code change (same pattern as SUMMARY_MODEL_ENV in summaries.py).
DEFAULT_MODEL = os.environ.get("RAG_QUERY_REWRITE_MODEL", HAIKU_4_5)

_MAX_TOKENS = 300

_SYSTEM_PROMPT = (
    "Ти допомагаєш гібридному пошуку по архіву робочих дзвінків/переписки. "
    "Користувач ставить питання одним формулюванням, а в архіві та сама тема "
    "могла прозвучати іншими словами (синоніми, скорочення, інші терміни). "
    "Дай від 1 до {max_variants} альтернативних КОРОТКИХ пошукових формулювань "
    "ТОГО САМОГО питання — не відповідай на питання, не додавай пояснень, не "
    "повторюй оригінальне формулювання дослівно. Відповідай ЛИШЕ JSON-масивом "
    'рядків, напр. ["варіант 1", "варіант 2"]. Якщо гідних альтернатив немає — '
    "поверни порожній масив []."
)


def _clean_variants(raw: list, question: str, max_variants: int) -> list[str]:
    """Valid strings, without the original and without duplicates (casefold), truncated to
    `max_variants`."""
    seen = {question.strip().casefold()}
    out: list[str] = []
    for v in raw:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        key = v.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
        if len(out) >= max_variants:
            break
    return out


def rewrite_query(question: str, *, max_variants: int = 3,
                  model: Optional[str] = None) -> list[str]:
    """1-3 alternative search formulations of `question` from Claude.

    An empty or single-word query -> `[]` (nothing to rewrite -- one word is
    already lexically exact). An API failure/invalid JSON -> `[]` +
    `logger.warning` -- the call is best-effort, the search itself must not fail
    because of an auxiliary step.
    """
    question = (question or "").strip()
    if not question or len(question.split()) < 2:
        return []

    from app.services import text_polishing  # lazy: the anthropic client lives here

    try:
        client = text_polishing._get_client()
        resp = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            system=_SYSTEM_PROMPT.format(max_variants=max_variants),
            messages=[{"role": "user", "content": question}],
        )
        raw_text = "".join(b.text for b in resp.content if b.type == "text")
        parsed = json.loads(text_polishing._strip_json_fence(raw_text))
    except Exception as exc:
        logger.warning("[query_rewrite] query rewrite failed: %s", exc)
        return []

    if not isinstance(parsed, list):
        logger.warning("[query_rewrite] Claude returned a non-JSON array: %r", parsed)
        return []

    return _clean_variants(parsed, question, max_variants)
