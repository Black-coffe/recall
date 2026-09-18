"""Переписування пошукового запиту (production-rag-wave-b-07).

Один запит користувача не завжди лексично/семантично збігається з тим, як
питання сформульовано в архіві. `rewrite_query` просить Claude 1-3
альтернативних пошукових формулювання того самого питання — вони йдуть
ДОДАТКОВИМИ vector+FTS підзапитами в `app.services.retrieval.search`
(флаг `rewrite=`), злиті тим самим RRF, що й оригінал.

Best-effort за побудовою: збій API чи невалідний JSON НЕ валить пошук —
повертається порожній список і `logger.warning`. Дефолт прапорця
(`RAG_QUERY_REWRITE`) — вимкнено, вмикається лише точково для вимірювання
гейтом (`evals/gate.py`).

Non-goals (Історія 07): НЕ витягує скоуп (проєкт/люди/період) з питання,
НЕ кешує варіанти в БД, НЕ переписує локальною моделлю (Ollama) — рішення A11.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from app.services.models import HAIKU_4_5


logger = logging.getLogger(__name__)

#: Дешева швидка модель для допоміжного виклику перед основним RAG-запитом —
#: не той самий Claude, що формує відповідь. Override через env для тюнінгу
#: без зміни коду (той самий патерн, що SUMMARY_MODEL_ENV у summaries.py).
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
    """Валідні рядки, без оригіналу і без дублів (casefold), обрізані до
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
    """1-3 альтернативних пошукових формулювання `question` від Claude.

    Порожній чи односкладовий запит → `[]` (переписувати нема чого — одне
    слово вже й так лексично точне). Збій API/невалідний JSON → `[]` +
    `logger.warning` — виклик best-effort, сам пошук не має падати через
    допоміжний крок.
    """
    question = (question or "").strip()
    if not question or len(question.split()) < 2:
        return []

    from app.services import text_polishing  # lazy: anthropic-клієнт тут

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
        logger.warning("[query_rewrite] переписування запиту не вдалось: %s", exc)
        return []

    if not isinstance(parsed, list):
        logger.warning("[query_rewrite] Claude повернув не JSON-масив: %r", parsed)
        return []

    return _clean_variants(parsed, question, max_variants)
