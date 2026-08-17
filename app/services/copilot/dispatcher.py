"""Co-pilot — локальний диспетчер-LLM (Phase 19, Крок 3).

Локальна сильна модель (Ollama, $0 API) як «відсікач сміття»: на каденс/подію
дивиться свіже вікно розмови і вирішує структурованим JSON —
  1. **триаж**: коротка мітка теми, чи треба шукати в архіві, який запит,
     первинні спостереження (протиріччя/питання/уточнення/факти);
  2. **аналіз-сверка**: якщо RAG щось знайшов — модель бачить РЕАЛЬНІ фрагменти
     архіву (з ``chunk_id``) і формулює обґрунтовані інсайти з ``evidence_chunk_ids``.

Два виклики (а не один) навмисно: у першому модель ще не бачила архів і не може
посилатися на конкретні чанки; у другому — бачить і прив'язує докази. Обидва
локальні ($0). Ескалація в Claude — Крок 5 (тут лише виставляється прапор
``escalate`` за порогом режиму, ефект підключається далі).

Чиста оркестрація: LLM (``local_llm``) і пошук (``retrieval.search``) інжектяться
→ модуль тестується офлайн з підставними, без Ollama/мережі/GPU. Стан сесії
(rolling-summary, показані чанки, кеш тем) живе у :class:`CopilotWorker`, не тут —
диспетчер сам по собі без стану, крім конфіга.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from app.services import local_llm, retrieval
from app.services.copilot.config import INSIGHT_KINDS as _KINDS


logger = logging.getLogger(__name__)

# --- JSON-схеми (Ollama structured outputs) ------------------------------------
# Тримаємо мінімальними: чим строгіша схема, тим стабільніший вивід малих квантів.
TRIAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "topic_label": {"type": "string"},
        "topic_status": {"type": "string", "enum": ["continue", "shift", "return"]},
        "return_to_index": {"type": "integer"},
        "needs_retrieval": {"type": "boolean"},
        "retrieval_query": {"type": "string"},
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "text": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "text", "confidence"],
            },
        },
    },
    "required": ["topic_label", "needs_retrieval"],
}

ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "text": {"type": "string"},
                    "evidence_chunk_ids": {"type": "array", "items": {"type": "integer"}},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "text", "confidence"],
            },
        },
    },
    "required": ["insights"],
}

# --- Системні промпти (UA) -----------------------------------------------------
_SYS_TRIAGE = (
    "Ти — локальний диспетчер живого ко-пілота ділового дзвінка. Аналізуєш свіже "
    "вікно розмови (репліки сторін) разом з коротким конспектом сесії, профілем "
    "напрямку та СПИСКОМ уже відомих тем сесії (індекс: назва, позначено поточну). "
    "Твоє завдання:\n"
    "1. Визнач спадкоємність теми відносно списку:\n"
    "   - topic_status='continue' — вікно ПРОДОВЖУЄ поточну тему;\n"
    "   - topic_status='return' — повернення до РАНІШЕ обговореної теми зі списку; "
    "тоді return_to_index = її індекс зі списку;\n"
    "   - topic_status='shift' — РОЗМОВА перейшла на НОВУ тему, якої ще не було.\n"
    "   Не дроби надмірно: дрібні відступи в межах тієї ж теми — це 'continue'. "
    "Зсув — лише коли предмет розмови справді змінився.\n"
    "   topic_label — стисла (2-5 слів) назва теми, що ЗАРАЗ обговорюється.\n"
    "2. needs_retrieval — true, ЯКЩО для перевірки фактів/сум/домовленостей варто "
    "звіритися з архівом минулих дзвінків і документів; інакше false.\n"
    "3. retrieval_query — якщо needs_retrieval, короткий пошуковий запит до архіву "
    "(ключові сутності/факти), українською.\n"
    "4. observations — лише ЯВНІ сигнали з вікна: можливі протиріччя (contradiction), "
    "відкриті питання (question), потрібні уточнення (clarification), важливі факти "
    "(fact). НЕ вигадуй; якщо сигналів нема — порожній список. confidence 0..1. "
    "МАКСИМУМ 3 найважливіші observations, кожна стисло (1 речення).\n"
    "Відповідай ВИКЛЮЧНО валідним JSON за схемою, без пояснень."
)

_SYS_ANALYSIS = (
    "Ти — аналітик живого ко-пілота. Тобі дано свіже вікно поточної розмови і "
    "релевантні фрагменти з архіву (кожен має chunk_id). Знайди:\n"
    "- contradiction — де те, що звучить ЗАРАЗ, розходиться з архівом (суми, дати, "
    "домовленості, факти);\n"
    "- fact — важливий підтверджений архівом факт, корисний оператору зараз;\n"
    "- question / clarification — що варто перепитати у співрозмовника.\n"
    "Для КОЖНОГО інсайту вкажи evidence_chunk_ids — РЕАЛЬНІ chunk_id з наданих "
    "фрагментів (не вигадуй id; якщо доказів нема — не давай інсайт). confidence "
    "0..1 — наскільки впевнений. Якщо релевантного нема — порожній список insights. "
    "Відповідай ВИКЛЮЧНО валідним JSON за схемою."
)

_SYS_SUMMARY = (
    "Ти стисло конспектуєш діловий дзвінок для контексту ко-пілота. Онови конспект: "
    "збережи ключові факти, рішення, цифри, відкриті питання. Пиши українською, "
    "стисло (до ~120 слів), без вступів — лише суть. Поверни JSON {\"summary\": \"...\"}."
)

_SUMMARY_SCHEMA: dict = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _fmt_chunks(chunks: list[dict], limit_chars: int = 320) -> str:
    """Згорнути RAG-чанки у компактний контекст для LLM (id + провенанс + текст).

    Коментар оператора позначається окремо і НЕ як «(джерело, дата)»: 7B-модель
    зважує рядки за тим, як вони підписані, і без явної мітки репліка, яку
    оператор щойно вписав руками, читалась би як ще один старий фрагмент
    архіву — тобто найсвіжіший і найточніший сигнал у вікні втрачав би вагу
    рівно там, де він потрібен.

    АЛЕ мітка вішається лише на ЖИВІ коментарі цього дзвінка (`live_operator`,
    їх кладе `worker._operator_comments`). Архівні коментарі теж мають
    `source_type == 'comment'` — вони приходять із RAG-пошуку, бо шар
    коментарів увімкнений у `retrieval.search` за замовчуванням. Підписати
    торішнє уточнення до ЧУЖОГО проєкту як «оператор щойно сказав» —
    вивернути сенс мітки навиворіт, ще й позбавивши модель джерела й дати.
    Тому в них лишається звичайний провенанс, лише з позначкою, що це
    коментар власника, а не репліка з розмови."""
    lines = []
    for c in chunks:
        txt = (c.get("text") or "").strip().replace("\n", " ")
        if len(txt) > limit_chars:
            txt = txt[:limit_chars].rstrip() + "…"
        cid = c.get("chunk_id")
        if c.get("source_type") == "comment":
            if c.get("live_operator"):
                when = c.get("anchor_label") or ""
                lines.append(f"[chunk_id={cid}] "
                             f"(КОМЕНТАР ОПЕРАТОРА{', ' + when if when else ''}) {txt}")
            else:
                src = c.get("target_label") or c.get("source_name") or "?"
                date = c.get("meeting_date") or ""
                lines.append(f"[chunk_id={cid}] "
                             f"(КОМЕНТАР ВЛАСНИКА до «{src}», {date}) {txt}")
            continue
        src = c.get("source_name") or "?"
        date = c.get("meeting_date") or ""
        lines.append(f"[chunk_id={cid}] ({src}, {date}) {txt}")
    return "\n".join(lines)


def _tokens(raw: Optional[dict]) -> tuple[Optional[int], Optional[int]]:
    """(tokens_in, tokens_out) з сирого Ollama-респонсу для обліку в історії."""
    if not raw:
        return None, None
    return raw.get("prompt_eval_count"), raw.get("eval_count")


class Dispatcher:
    """Локальний диспетчер. Без стану (крім конфіга): теми/summary/кеш — у воркері.

    ``llm`` і ``search_fn`` інжектяться → офлайн-тест без Ollama/БД.
    """

    def __init__(self, *, db_path: str, llm: Any = local_llm,
                 search_fn: Optional[Callable[..., dict]] = None,
                 top_k: int = 7, max_tokens: int = 1024, window_chars: int = 1600):
        self._db_path = db_path
        self._llm = llm
        self._search = search_fn or retrieval.search
        self._top_k = top_k
        self._max_tokens = max_tokens
        self._window_chars = window_chars

    def retrieve(self, query: str, *, top_k: Optional[int] = None,
                 category_id: Optional[int] = None,
                 scope_tids: Optional[list] = None) -> dict:
        """RAG-пошук в архіві (обгортка над інжектованим search_fn). category_id —
        обмежити напрямком сесії. Returns {"query", "chunks", "vector_available"}."""
        return self._search(self._db_path, query, top_k=top_k or self._top_k,
                            category_id=category_id, scope_tids=scope_tids)

    # ------------------------------------------------------------- LLM-кроки

    def triage(self, *, window: str, summary: str = "", profile: str = "",
               topics: Optional[list[dict]] = None,
               current_index: Optional[int] = None) -> Optional[dict]:
        """Перший прохід: спадкоємність теми (LLM вирішує continue/shift/return) +
        чи шукати + запит + спостереження. None на збій LLM."""
        prompt = (
            (f"Профіль напрямку: {profile}\n" if profile else "")
            + (f"Конспект сесії:\n{summary}\n\n" if summary else "")
            + self._fmt_topics(topics, current_index)
            + f"Свіже вікно розмови:\n{self._clip(window)}"
        )
        out = self._run(prompt, TRIAGE_SCHEMA, _SYS_TRIAGE)
        if out is None:
            return None
        data = out["data"]
        data["tokens_in"], data["tokens_out"] = out["tokens"]
        # нормалізація
        data["topic_label"] = (data.get("topic_label") or "").strip()
        st = data.get("topic_status")
        data["topic_status"] = st if st in ("continue", "shift", "return") else "continue"
        ri = data.get("return_to_index")
        data["return_to_index"] = ri if isinstance(ri, int) else None
        data["needs_retrieval"] = bool(data.get("needs_retrieval"))
        data["retrieval_query"] = (data.get("retrieval_query") or "").strip()
        data["observations"] = self._clean_insights(data.get("observations"), with_evidence=False)
        return data

    @staticmethod
    def _fmt_topics(topics: Optional[list[dict]], current_index: Optional[int]) -> str:
        """Список відомих тем для промпта триажу (позначає поточну)."""
        if not topics:
            return "Відомих тем ще нема (це початок розмови).\n\n"
        lines = ["Відомі теми сесії (індекс: назва):"]
        for t in topics:
            mark = "  ← поточна" if t.get("index") == current_index else ""
            lines.append(f"  {t.get('index')}: {t.get('label')}{mark}")
        return "\n".join(lines) + "\n\n"

    def analyze(self, *, window: str, chunks: list[dict],
                profile: str = "") -> Optional[dict]:
        """Другий прохід: інсайти, обґрунтовані наданими чанками. None на збій LLM."""
        if not chunks:
            return {"insights": [], "tokens_in": None, "tokens_out": None}
        valid_ids = {int(c["chunk_id"]) for c in chunks if c.get("chunk_id") is not None}
        prompt = (
            (f"Профіль напрямку: {profile}\n" if profile else "")
            + f"Свіже вікно розмови:\n{self._clip(window)}\n\n"
            + f"Фрагменти з архіву:\n{_fmt_chunks(chunks)}"
        )
        out = self._run(prompt, ANALYSIS_SCHEMA, _SYS_ANALYSIS)
        if out is None:
            return None
        data = out["data"]
        insights = self._clean_insights(data.get("insights"), with_evidence=True,
                                        valid_ids=valid_ids)
        return {"insights": insights, "tokens_in": out["tokens"][0],
                "tokens_out": out["tokens"][1]}

    def summarize(self, *, prev_summary: str, window: str) -> Optional[str]:
        """Оновити rolling-summary сесії. None на збій (тоді лишаємо старий)."""
        prompt = (
            (f"Попередній конспект:\n{prev_summary}\n\n" if prev_summary else "")
            + f"Нові репліки:\n{self._clip(window)}"
        )
        out = self._run(prompt, _SUMMARY_SCHEMA, _SYS_SUMMARY, max_tokens=300)
        if out is None:
            return None
        return (out["data"].get("summary") or "").strip() or None

    # ------------------------------------------------------------- internals

    def _run(self, prompt: str, schema: dict, system: str,
             max_tokens: Optional[int] = None) -> Optional[dict]:
        try:
            res = self._llm.generate_json(
                prompt, schema=schema, system=system,
                max_tokens=max_tokens or self._max_tokens,
            )
        except Exception as e:  # LocalLLMError / мережа / невалідний JSON після ретраю
            logger.warning("[copilot] диспетчер LLM збій: %s", e)
            return None
        if not isinstance(res.get("data"), dict):
            return None
        return {"data": res["data"], "tokens": _tokens(res.get("raw"))}

    def _clean_insights(self, items: Any, *, with_evidence: bool,
                        valid_ids: Optional[set] = None) -> list[dict]:
        """Відфільтрувати/нормалізувати інсайти від моделі (захист від сміття)."""
        out: list[dict] = []
        if not isinstance(items, list):
            return out
        for it in items:
            if not isinstance(it, dict):
                continue
            kind = it.get("kind")
            text = (it.get("text") or "").strip()
            if kind not in _KINDS or not text:
                continue
            try:
                conf = float(it.get("confidence"))
            except (TypeError, ValueError):
                conf = 0.5
            conf = max(0.0, min(1.0, conf))
            ins = {"kind": kind, "text": text, "confidence": round(conf, 3)}
            if with_evidence:
                ids = []
                for cid in (it.get("evidence_chunk_ids") or []):
                    try:
                        cid = int(cid)
                    except (TypeError, ValueError):
                        continue
                    if valid_ids is None or cid in valid_ids:
                        ids.append(cid)
                # інсайт з евіденс-схеми, але без жодного реального доказу — відкидаємо
                # (модель «галюцинувала» прив'язку); спостереження без евіденс ок.
                if not ids:
                    continue
                ins["evidence_chunk_ids"] = ids
            out.append(ins)
        return out

    def _clip(self, text: str) -> str:
        text = (text or "").strip()
        return text[-self._window_chars:] if len(text) > self._window_chars else text
