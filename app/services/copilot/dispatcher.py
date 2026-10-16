"""Co-pilot — local dispatcher LLM (Phase 19, Step 3).

A strong local model (Ollama, $0 API) as a "garbage filter": on a cadence/event it
looks at the fresh conversation window and decides with structured JSON —
  1. **triage**: a short topic label, whether to search the archive, which query,
     primary observations (contradictions/questions/clarifications/facts);
  2. **analysis-reconciliation**: if RAG found something, the model sees REAL archive
     fragments (with ``chunk_id``) and formulates grounded insights with ``evidence_chunk_ids``.

Two calls (rather than one) on purpose: in the first the model has not seen the archive yet and cannot
reference specific chunks; in the second it sees them and binds the evidence. Both
are local ($0). Escalation to Claude is Step 5 (here only the ``escalate``
flag is set by the mode threshold; the effect is wired up further on).

Pure orchestration: the LLM (``local_llm``) and search (``retrieval.search``) are injected
→ the module is tested offline with stubs, without Ollama/network/GPU. Session state
(rolling-summary, shown chunks, topic cache) lives in :class:`CopilotWorker`, not here —
the dispatcher itself is stateless, except for the config.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from app.services import local_llm, retrieval
from app.services.copilot.config import INSIGHT_KINDS as _KINDS


logger = logging.getLogger(__name__)

# --- JSON schemas (Ollama structured outputs) ---------------------------------
# Keep them minimal: the stricter the schema, the more stable the output of small quants.
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

# --- System prompts (UA) -------------------------------------------------------
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
    """Collapse RAG chunks into a compact context for the LLM (id + provenance + text).

    The operator comment is marked separately and NOT as "(source, date)": a 7B model
    weighs lines by how they are signed, and without an explicit label a line the
    operator has just typed by hand would read as yet another old archive fragment
    — i.e. the freshest and most accurate signal in the window would lose weight
    exactly where it is needed.

    BUT the label is attached only to LIVE comments of this call (`live_operator`,
    put there by `worker._operator_comments`). Archive comments also have
    `source_type == 'comment'` — they come from the RAG search, because the comment
    layer is enabled in `retrieval.search` by default. Signing last year's
    clarification about SOMEONE ELSE's project as "the operator just said" would
    turn the label's meaning inside out, and deprive the model of source and date.
    So they keep the regular provenance, only marked as
    the owner's comment, not a line from the conversation."""
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
    """(tokens_in, tokens_out) from the raw Ollama response for history accounting."""
    if not raw:
        return None, None
    return raw.get("prompt_eval_count"), raw.get("eval_count")


class Dispatcher:
    """Local dispatcher. Stateless (except the config): topics/summary/cache live in the worker.

    ``llm`` and ``search_fn`` are injected → offline test without Ollama/DB.
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
        """RAG search in the archive (wrapper over the injected search_fn). category_id —
        restricts to the session direction. Returns {"query", "chunks", "vector_available"}."""
        return self._search(self._db_path, query, top_k=top_k or self._top_k,
                            category_id=category_id, scope_tids=scope_tids)

    # ------------------------------------------------------------- LLM steps

    def triage(self, *, window: str, summary: str = "", profile: str = "",
               topics: Optional[list[dict]] = None,
               current_index: Optional[int] = None) -> Optional[dict]:
        """First pass: topic continuity (the LLM decides continue/shift/return) +
        whether to search + query + observations. None on LLM failure."""
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
        # normalization
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
        """List of known topics for the triage prompt (marks the current one)."""
        if not topics:
            return "Відомих тем ще нема (це початок розмови).\n\n"
        lines = ["Відомі теми сесії (індекс: назва):"]
        for t in topics:
            mark = "  ← поточна" if t.get("index") == current_index else ""
            lines.append(f"  {t.get('index')}: {t.get('label')}{mark}")
        return "\n".join(lines) + "\n\n"

    def analyze(self, *, window: str, chunks: list[dict],
                profile: str = "") -> Optional[dict]:
        """Second pass: insights grounded in the supplied chunks. None on LLM failure."""
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
        """Update the session rolling-summary. None on failure (then the old one is kept)."""
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
        except Exception as e:  # LocalLLMError / network / invalid JSON after retry
            logger.warning("[copilot] dispatcher LLM failure: %s", e)
            return None
        if not isinstance(res.get("data"), dict):
            return None
        return {"data": res["data"], "tokens": _tokens(res.get("raw"))}

    def _clean_insights(self, items: Any, *, with_evidence: bool,
                        valid_ids: Optional[set] = None) -> list[dict]:
        """Filter/normalize insights from the model (protection against garbage)."""
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
                # an insight from the evidence schema but without any real proof — drop it
                # (the model "hallucinated" the binding); an observation without evidence is ok.
                if not ids:
                    continue
                ins["evidence_chunk_ids"] = ids
            out.append(ins)
        return out

    def _clip(self, text: str) -> str:
        text = (text or "").strip()
        return text[-self._window_chars:] if len(text) > self._window_chars else text
