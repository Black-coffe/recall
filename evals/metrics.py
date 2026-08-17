"""Детермінована математика метрик для evals/ (REMEDIATION_PLAN Волна 3, T6.1).

Жодних мережевих/GPU-викликів тут немає навмисно — увесь цей модуль має
бути тестованим офлайн (``tests/test_eval_metrics.py``) на синтетичних
даних, щоб зміну формул recall@k / citation-rate можна було перевірити без
реального API-ключа чи БД. Побудова запиту (retrieval.search) і генерація
відповіді (rag.answer_question) — окремо, у ``run_eval.py``.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional


# Формат цитат у відповіді RAG узгоджений з системним промптом
# app/services/rag.py::_RAG_SYSTEM_PROMPT — "[n]" (можна кілька: [1][3]).
_CITATION_RE = re.compile(r"\[(\d+)\]")


def source_ids_from_chunks(chunks: Iterable[dict]) -> list[int]:
    """Витягти transcription_id зі списку чанків (як повертає retrieval.search),
    зберігаючи порядок і без дублів (перша поява — найвищий ранг)."""
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
    """Частка очікуваних джерел, що знайшлися у top-k retrieved.

    ``|expected ∩ retrieved| / |expected|``. Повертає ``None``, якщо
    ``expected_ids`` порожній (golden-item без цього критерію — не рахуємо
    ні як провал, ні як успіх, просто пропускаємо в агрегації).
    """
    expected = set(expected_ids)
    if not expected:
        return None
    retrieved = set(retrieved_ids)
    return len(expected & retrieved) / len(expected)


def source_name_hit(name_substrings: Iterable[str], chunks: Iterable[dict]) -> Optional[bool]:
    """Чи серед retrieved-чанків є джерело, чия ``source_name`` містить
    (case-insensitive) хоча б один із очікуваних підрядків.

    Стійкіша альтернатива ``expected_transcription_ids`` — ID транскрипту
    змінюється при переінджесті архіву, назва зазвичай ні. Повертає ``None``,
    якщо ``name_substrings`` порожній (критерій не заданий).
    """
    subs = [s.lower() for s in name_substrings if s]
    if not subs:
        return None
    names = [str(ch.get("source_name") or "").lower() for ch in chunks]
    return any(sub in name for name in names for sub in subs)


def has_citation(answer: str) -> bool:
    """Чи містить відповідь хоча б одне цитування у форматі ``[n]``."""
    return bool(_CITATION_RE.search(answer or ""))


def cited_indices(answer: str) -> list[int]:
    """Усі індекси цитат [n], що зустрічаються у відповіді (з дублями, у
    порядку появи) — корисно для перевірки, що цитати посилаються на реально
    надані фрагменти (1..found)."""
    return [int(m) for m in _CITATION_RE.findall(answer or "")]


def citations_in_range(answer: str, found: int) -> bool:
    """Чи ВСІ цитати [n] у відповіді посилаються на дійсні індекси фрагментів
    (1..found)? Порожня відповідь / без цитат → True (нема що перевіряти —
    used by evaluate_item лише коли found > 0 і has_citation вже True)."""
    idxs = cited_indices(answer)
    if not idxs:
        return True
    return all(1 <= i <= found for i in idxs)


def evaluate_item(golden: dict, retrieved_chunks: list[dict],
                   answer: Optional[str] = None) -> dict:
    """Порахувати метрики для ОДНОГО golden-запису проти реального результату.

    ``golden`` — один запис golden-set (див. evals/golden_set.example.json).
    ``retrieved_chunks`` — ``retrieval.search(...)["chunks"]`` (top-k).
    ``answer`` — текст відповіді RAG (``rag.answer_question(...)["answer"]``),
    якщо запускали і генерацію відповіді, інакше ``None`` (тільки retrieval).

    Returns dict з ключами (усі опційні метрики — ``None``, якщо критерій
    не заданий у golden-записі або відповідь не генерувалась):
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
    """Звести список ``evaluate_item(...)`` у підсумкові метрики для таблиці.

    Кожна метрика агрегується лише по записах, де вона не ``None`` (щоб
    golden-записи без певного критерію не псували середнє). ``*_n`` — на
    скількох записах порахована метрика (для інтерпретації середнього).
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
