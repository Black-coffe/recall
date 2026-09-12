"""Локальне питання-відповідь — «жива розмова + архів» (Історія 02, mcp-live-call).

Новий шлях, якого раніше не було: агент (MCP-клієнт) ставить уточнення, і його
розбирає ЛОКАЛЬНА модель (Ollama, $0), а не Claude. Джерела — живий транскрипт
поточного дзвінка (RAM-only, `live_transcribe.LiveTranscribeWorker`) і/або архів
(`retrieval.search`). На відміну від `rag.answer_question` (Claude, платний,
цитати з посиланнями [n]) і `copilot/dispatcher.py` (JSON-схеми для триажу
топіків) — тут проста пара запит/відповідь текстом, без стану і без запису
куди-небудь: питання агента НЕ повинно зʼявитись у віджеті оператора чи в БД
(власник обрав «тільки мені», план `docs/specs/mcp-live-call/plan.md` non-goals).

Чиста оркестрація: LLM (`local_llm`) і пошук (`retrieval.search`) інжектяться
(як у `copilot/dispatcher.py`) → модуль тестується офлайн, без Ollama/torch.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from app.services import local_llm, retrieval


logger = logging.getLogger(__name__)

# Живий транскрипт дзвінка може вирости на довгу розмову — обрізаємо хвостом
# (найсвіжіше найрелевантніше уточненню «щойно сказаному»), той самий підхід,
# що й `Dispatcher._clip` у копілоті.
_MAX_TRANSCRIPT_CHARS = 6000

# `generate()` тримає спільний `local_llm._GEN_LOCK` (одна Ollama, один GPU) —
# той самий замок, що й тіки копілота (`CopilotWorker._tick`, дефолтна
# каденція `COPILOT_TOPIC_TICK_SEC=6`с, `app/services/copilot/worker.py:127`).
# Заміряно на цій машині: Ollama віддає `qwen2.5:32b-instruct` (сконфігурований
# `qwen2.5:14b-instruct-q5_K_M` не встановлений) ≈9.6 токенів/с — 512 токенів це
# ~53с утримання `_GEN_LOCK`, вдесятеро довше за тік ~6с. Тому під час активного
# запису `ask_local` взагалі НЕ бере замок (Історія 08, `recording_active=True`
# нижче) — жодна межа токенів не встигає дати корисну відповідь швидше за один
# тік. Поза записом `_MAX_ANSWER_TOKENS` лишається прив'язаним до прийнятого
# тікового бюджету (`Dispatcher.__init__` дефолт `max_tokens=1024`,
# `app/services/copilot/dispatcher.py:183`) — вдвічі менше, як і раніше.
_MAX_ANSWER_TOKENS = 512

# Захист від неконтрольованого пошуку по архіву (і узгодження з `_RERANK_ENABLED`
# у `rag.py` нижче) — top_k із HTTP-шару теж обрізається (`copilot.py`), тут —
# другий рубіж для прямих викликів `ask_local`.
_MAX_TOP_K = 20

# `rag.answer_question` (`ask_archive`) вмикає rerank через той самий env var
# (`app/services/rag.py::_RERANK_ENABLED`) — читаємо його тут напряму (той самий
# підхід, що й у rag.py: "щоб модуль лишався standalone-тестованим"), а не
# імпортуємо rag.py, щоб не тягнути його залежності (Claude API) у простий
# локальний шлях. Раніше `ask_local` завжди йшов з rerank=False — розбіжність
# із ask_archive усунена цим прапорцем.
_RERANK_ENABLED = os.environ.get("RECALL_RERANK_ENABLED", "0").strip() in ("1", "true", "True")

_SYSTEM_PROMPT = (
    "Ти — локальний асистент оператора під час дзвінка. Тобі дають ПИТАННЯ і, "
    "за наявності, ЖИВИЙ ТРАНСКРИПТ поточної розмови та/або ФРАГМЕНТИ АРХІВУ "
    "минулих записів. Відповідай стисло і по суті, спираючись ВИКЛЮЧНО на надані "
    "дані. Якщо даних не вистачає для відповіді — прямо скажи про це, не вигадуй."
)

_SCOPES = ("call", "archive", "both")


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text[-_MAX_TRANSCRIPT_CHARS:] if len(text) > _MAX_TRANSCRIPT_CHARS else text


def _to_source(chunk: dict) -> dict:
    """Чанк `retrieval.search` → провенанс C2 (`chunk_id/transcription_id/title/date/snippet`)."""
    text = (chunk.get("text") or "").strip()
    snippet = text if len(text) <= 400 else text[:400].rstrip() + "…"
    return {
        "chunk_id": chunk.get("chunk_id"),
        "transcription_id": chunk.get("transcription_id"),
        "title": chunk.get("source_name"),
        "date": chunk.get("meeting_date"),
        "snippet": snippet,
    }


def _empty_reason(scope: str) -> str:
    if scope == "call":
        return ("живий транскрипт дзвінка недоступний (немає активної сесії "
                "запису або live-транскрипція ще не накопичила текст)")
    if scope == "archive":
        return "в архіві не знайдено релевантних фрагментів за цим питанням"
    return "немає ні живого транскрипту, ні релевантних фрагментів архіву"


def _build_prompt(question: str, transcript_text: str, sources: list[dict]) -> str:
    parts = [f"ПИТАННЯ: {question}"]
    if transcript_text:
        parts.append(f"ЖИВИЙ ТРАНСКРИПТ ПОТОЧНОЇ РОЗМОВИ:\n{transcript_text}")
    if sources:
        ctx = "\n\n".join(
            f"[{i}] {s.get('title') or '?'} ({s.get('date') or '?'})\n{s.get('snippet') or ''}"
            for i, s in enumerate(sources, 1)
        )
        parts.append(f"ФРАГМЕНТИ АРХІВУ:\n{ctx}")
    return "\n\n".join(parts)


def _raw_response(scope: str, used_transcript: str, sources: list[dict]) -> dict:
    """Історія 08: під час активного запису локальна модель не викликається
    взагалі — агент отримує сирі дані (живий транскрипт + знайдені чанки
    архіву) і робить висновок сам. ``mode="raw"`` — явний, самопояснювальний
    маркер (acceptance), щоб агент ніколи не сплутав сирі дані з відповіддю
    моделі. ``transcript_text`` тут повний (обрізаний `_clip`), а не лише
    лічильник символів — інакше «сирі дані» агенту нема з чого читати."""
    available = bool(used_transcript or sources)
    return {
        "answer": "", "model": "", "scope": scope,
        "used_transcript_chars": len(used_transcript), "sources": sources,
        "transcript_text": used_transcript or None,
        "available": available,
        "reason": None if available else _empty_reason(scope),
        "mode": "raw",
    }


def ask_local(
    db_path: str,
    question: str,
    *,
    transcript_text: Optional[str] = None,
    scope: str = "both",
    top_k: int = 6,
    llm: Any = local_llm,
    search_fn: Optional[Callable[..., dict]] = None,
    recording_active: bool = False,
) -> dict:
    """Питання → (живий транскрипт і/або архів) → локальна модель → відповідь.

    ``scope``: ``call`` (лише живий транскрипт) | ``archive`` (лише архів) |
    ``both`` (дефолт, обидва). ``call`` НЕ звертається до `search_fn`, ``archive``
    НЕ читає ``transcript_text`` — контракт C2/приймання цієї історії.

    ``recording_active`` (Історія 08): поки триває запис, `llm` НЕ отримує
    жодного виклику (ні `availability()`, ні `generate()`) — спільний замок
    генерації (`local_llm._GEN_LOCK`, той самий, що тримають тіки копілота)
    не береться ні на мить. Викликач (HTTP-шар `copilot.py`) визначає це за
    наявністю активної сесії запису, а НЕ приймає параметр запиту — агент не
    має способу форсувати генерацію під час дзвінка (non-goals/acceptance).
    Пошук по архіву (``search_fn``) виконується в обох режимах як завжди —
    скорочується лише генерація.

    Ніколи не кидає виняток назовні: недоступність Ollama чи порожній контекст —
    ``available=False`` з людською ``reason``, не 500.

    Returns ``{"answer", "model", "scope", "used_transcript_chars", "sources",
    "available", "reason", "mode"}`` — форма C2 без HTTP-обгортки (``success``/
    ``session_id`` додає блюпринт). ``mode`` — ``"raw"`` (запис іде, дані без
    відповіді моделі) або ``"generated"`` (звичайний шлях).
    """
    search_fn = search_fn or retrieval.search
    scope = scope if scope in _SCOPES else "both"
    question = (question or "").strip()
    try:
        top_k = int(top_k)
    except (TypeError, ValueError, OverflowError):  # напр. float('inf') — HTTP-шар
        top_k = 6                                    # вже відсіює це до 400, тут — фолбек
    top_k = min(max(top_k, 1), _MAX_TOP_K)

    used_transcript = _clip(transcript_text) if scope in ("call", "both") else ""

    sources: list[dict] = []
    if scope in ("archive", "both"):
        try:
            res = search_fn(db_path, question, top_k=top_k, rerank=_RERANK_ENABLED)
            chunks = res.get("chunks") or []
        except Exception as e:  # мережа/БД/embeddings — деградуємо, не падаємо
            logger.warning("[live_ask] пошук архіву впав: %s", e)
            chunks = []
        sources = [_to_source(c) for c in chunks]

    if recording_active:
        return _raw_response(scope, used_transcript, sources)

    if not used_transcript and not sources:
        return {
            "answer": "", "model": "", "scope": scope,
            "used_transcript_chars": 0, "sources": [],
            "available": False, "reason": _empty_reason(scope), "mode": "generated",
        }

    ok, reason = llm.availability()
    if not ok:
        return {
            "answer": "", "model": "", "scope": scope,
            "used_transcript_chars": len(used_transcript), "sources": sources,
            "available": False, "reason": reason, "mode": "generated",
        }

    prompt = _build_prompt(question, used_transcript, sources)
    try:
        resp = llm.generate(prompt, system=_SYSTEM_PROMPT, max_tokens=_MAX_ANSWER_TOKENS)
    except Exception as e:  # LocalLLMError / мережа — та сама деградація, що й dispatcher._run
        logger.warning("[live_ask] генерація впала: %s", e)
        return {
            "answer": "", "model": "", "scope": scope,
            "used_transcript_chars": len(used_transcript), "sources": sources,
            "available": False, "reason": f"локальна модель не відповіла: {e}",
            "mode": "generated",
        }

    answer = (resp.get("response") or "").strip()
    model = resp.get("model") or getattr(llm, "LOCAL_LLM_MODEL", "")
    return {
        "answer": answer, "model": model, "scope": scope,
        "used_transcript_chars": len(used_transcript), "sources": sources,
        "available": True, "reason": None, "mode": "generated",
    }
