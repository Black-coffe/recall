"""Co-pilot — escalation to the Claude API (Phase 19, Step 5).

Closes the local-first cascade: the local model (dispatcher, Step 3) gives a **draft**
hint, and Claude (Sonnet, the "big brother") **verifies** it — real/refuted/
uncertain — and polishes the text. Cheap because: (1) we escalate only what the local model
marked important (not the whole transcript); (2) prompt caching on the stable system block
(as in rag.py); (3) forced tool-use → a compact structured output (few
output tokens).

Two modes:
- :meth:`Escalator.verify` — verify one local draft against its evidence.
- :meth:`Escalator.sweep` — safety-sweep: an independent Sonnet pass over the current
  window + topic chunks (insurance against the local model's "silent" false negatives, Step 5).

Everything degrades: no key / API down → the method returns None, the co-pilot stays
local ($0). The client and tool schema are injected via ``client_factory`` →
tested offline without network.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from app.services import pricing, text_polishing
from app.services import models as _models
from app.services.claude_retry import call_with_retry
from app.services.copilot.config import INSIGHT_KINDS as _KINDS


logger = logging.getLogger(__name__)


# Fallback when the caller did not pass model explicitly (worker.py always passes
# ws.config.get("model_api") — see app/services/copilot/config.py). NOT
# pricing.DEFAULT_PRICE_MODEL (that is the general app default, currently Opus) — here
# we keep exactly the "normal" copilot model (Sonnet), so as not to collapse
# the normal/gnarly tiers into one by default (T6.2).
DEFAULT_MODEL = _models.COPILOT_NORMAL_MODEL_DEFAULT

# Prices are shared with research.py (app/services/pricing.py) so they do not diverge.
cost_estimate = pricing.estimate_cost

_VERDICT_TOOL = {
    "name": "report_verdict",
    "description": "Повернути вердикт перевірки підказки ко-пілота за доказами архіву.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["real", "refuted", "uncertain"],
                        "description": "real — підтверджено доказами; refuted — суперечить/не підтверджено; uncertain — даних бракує."},
            "confidence": {"type": "number", "description": "0..1 впевненість у вердикті."},
            "text": {"type": "string", "description": "Відполірований стислий текст підказки для оператора (1-2 речення, мовою розмови)."},
        },
        "required": ["verdict", "confidence", "text"],
    },
}

_FINDINGS_TOOL = {
    "name": "report_findings",
    "description": "Повернути знайдені під час safety-проходу підказки оператору.",
    "input_schema": {
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
    },
}

_SYS_VERIFY = (
    "Ти — старший аналітик-верифікатор живого ко-пілота ділового дзвінка. Локальна "
    "модель позначила підказку оператору (можливе протиріччя / питання / уточнення / "
    "важливий факт), спираючись на фрагменти архіву минулих дзвінків і документів. "
    "Перевір її СУВОРО лише за наданими фрагментами:\n"
    "- real — твердження підтверджується доказами;\n"
    "- refuted — суперечить доказам або не підтверджується ними;\n"
    "- uncertain — наданих даних бракує для висновку.\n"
    "Відполіруй текст підказки: стисло, конкретно, мовою розмови, без води. "
    "Поверни результат ВИКЛЮЧНО через інструмент report_verdict."
)

_SYS_SWEEP = (
    "Ти — старший аналітик живого ко-пілота дзвінка (safety-прохід). Тобі дано "
    "свіже вікно поточної розмови і релевантні фрагменти архіву (кожен з chunk_id). "
    "Незалежно знайди те, що оператору варто знати ПРЯМО ЗАРАЗ: протиріччя між "
    "розмовою і архівом (contradiction), важливі підтверджені факти (fact), що "
    "перепитати (question/clarification). Для кожного — РЕАЛЬНІ evidence_chunk_ids з "
    "наданих фрагментів (не вигадуй). Якщо нічого вартого уваги — порожній список. "
    "Поверни ВИКЛЮЧНО через report_findings."
)


def _fmt_chunks(chunks: list[dict], limit_chars: int = 400) -> str:
    lines = []
    for c in chunks:
        src = c.get("source_name") or "?"
        date = c.get("meeting_date") or ""
        txt = (c.get("text") or "").strip().replace("\n", " ")
        if len(txt) > limit_chars:
            txt = txt[:limit_chars].rstrip() + "…"
        lines.append(f"[chunk_id={c.get('chunk_id')}] ({src}, {date}) {txt}")
    return "\n".join(lines)


class Escalator:
    """Escalation client to Claude. ``client_factory`` is injected → offline test."""

    def __init__(self, *, client_factory: Callable[[], Any] = text_polishing._get_client,
                 timeout: float = 60.0):
        self._client_factory = client_factory
        self._timeout = timeout

    def _request(self, *, model: str, system: str, tool: dict, user: str,
                 max_tokens: int) -> Optional[dict]:
        """One forced tool-use call. Returns {"input", usage...} or None."""
        try:
            client = self._client_factory()
        except Exception as e:  # no key, etc.
            logger.info("[copilot] escalation unavailable: %s", e)
            return None
        kwargs = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        # Claude 5 (Opus 5 / Sonnet 5): thinking on-by-default. Here the live verdict
        # on a call is a forced tool-use with a small max_tokens; adaptive thinking
        # would eat the token budget (the cap is shared) and add seconds → turn it off explicitly.
        # A forced tool_choice guarantees a tool block, so the known failure mode
        # of disabled thinking ("tool call as text") is not a threat here.
        if _models.needs_explicit_thinking_off(model):
            kwargs["thinking"] = {"type": "disabled"}
        try:
            # Retry on transient errors (429/5xx/timeout) — T6.3. This is
            # NOT a stream (messages.create), so the whole call can be safely repeated
            # from scratch — nothing has been handed to the caller yet.
            resp = call_with_retry(
                lambda: client.with_options(timeout=self._timeout).messages.create(**kwargs),
                what="copilot-escalate",
            )
        except Exception as e:
            logger.warning("[copilot] escalation API failure: %s", e)
            return None
        tool_input = None
        for block in (resp.content or []):
            if getattr(block, "type", None) == "tool_use":
                tool_input = block.input
                break
        if tool_input is None:
            logger.warning("[copilot] escalation: the model did not call the tool")
            return None
        usage = getattr(resp, "usage", None)
        tin = getattr(usage, "input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        cread = getattr(usage, "cache_read_input_tokens", 0) or 0
        return {
            "input": tool_input,
            "model": getattr(resp, "model", model),
            "tokens_in": tin, "tokens_out": tout, "cache_read": cread,
            "cost": cost_estimate(model, tin, tout, cread),
        }

    def _verify_once(self, *, insight: dict, evidence_chunks: list[dict],
                     profile: str, model: str, lens: str = "") -> Optional[dict]:
        """One verification vote (optionally with a lens for diversity)."""
        user = (
            (f"Профіль напрямку: {profile}\n" if profile else "")
            + f"Чернетка підказки (тип={insight.get('kind')}):\n{insight.get('text', '')}\n\n"
            + f"Докази з архіву:\n{_fmt_chunks(evidence_chunks)}"
            + (f"\n\nОсобливий фокус цієї перевірки: {lens}" if lens else "")
        )
        out = self._request(model=model, system=_SYS_VERIFY, tool=_VERDICT_TOOL,
                            user=user, max_tokens=600)
        if out is None:
            return None
        data = out["input"] if isinstance(out["input"], dict) else {}
        verdict = data.get("verdict")
        if verdict not in ("real", "refuted", "uncertain"):
            verdict = "uncertain"
        try:
            conf = max(0.0, min(1.0, float(data.get("confidence"))))
        except (TypeError, ValueError):
            conf = 0.5
        return {
            "verdict": verdict, "confidence": round(conf, 3),
            "text": (data.get("text") or insight.get("text") or "").strip(),
            "model": out["model"], "tokens_in": out["tokens_in"],
            "tokens_out": out["tokens_out"], "cache_read": out["cache_read"],
            "cost": out["cost"],
        }

    def verify(self, *, insight: dict, evidence_chunks: list[dict], profile: str = "",
               model: str = DEFAULT_MODEL, votes: int = 1,
               on_vote: Optional[Callable[[dict], None]] = None,
               can_continue: Optional[Callable[[], bool]] = None) -> Optional[dict]:
        """Verify the local draft. ``votes`` > 1 → multi-lens (hard
        mode, Step 6): several independent votes with different focuses, a majority
        verdict. Returns a verdict dict (+ summed token accounting) or None.

        ``on_vote(result)`` is called AFTER each vote (to account tokens/$
        immediately), ``can_continue()`` is checked BEFORE each next vote —
        returning False → we stop spending (budget exhausted mid multi-vote,
        Step 9 fix). The first vote always runs (the input is already gated by the caller)."""
        if not evidence_chunks:
            return None
        votes = max(1, int(votes or 1))
        results = []
        for i in range(votes):
            if i > 0 and can_continue is not None and not can_continue():
                break  # budget exhausted by the previous vote → do not spend further
            r = self._verify_once(insight=insight, evidence_chunks=evidence_chunks,
                                  profile=profile, model=model,
                                  lens=_LENSES[i % len(_LENSES)] if votes > 1 else "")
            if r is None:
                continue
            results.append(r)
            if on_vote is not None:
                on_vote(r)
        if not results:
            return None
        return _aggregate_votes(results, insight)

    def sweep(self, *, window: str, chunks: list[dict], profile: str = "",
              model: str = DEFAULT_MODEL) -> Optional[dict]:
        """Sonnet safety pass over the window+chunks. Returns {"insights", accounting} or None."""
        if not chunks:
            return None
        valid = {int(c["chunk_id"]) for c in chunks if c.get("chunk_id") is not None}
        user = (
            (f"Профіль напрямку: {profile}\n" if profile else "")
            + f"Свіже вікно розмови:\n{window}\n\n"
            + f"Фрагменти архіву:\n{_fmt_chunks(chunks)}"
        )
        out = self._request(model=model, system=_SYS_SWEEP, tool=_FINDINGS_TOOL,
                            user=user, max_tokens=900)
        if out is None:
            return None
        data = out["input"] if isinstance(out["input"], dict) else {}
        insights = []
        for it in (data.get("insights") or []):
            if not isinstance(it, dict):
                continue
            kind, text = it.get("kind"), (it.get("text") or "").strip()
            if kind not in _KINDS or not text:
                continue
            ids = [int(x) for x in (it.get("evidence_chunk_ids") or [])
                   if _intable(x) and int(x) in valid]
            if not ids:
                continue
            try:
                conf = max(0.0, min(1.0, float(it.get("confidence"))))
            except (TypeError, ValueError):
                conf = 0.6
            insights.append({"kind": kind, "text": text, "evidence_chunk_ids": ids,
                             "confidence": round(conf, 3)})
        return {
            "insights": insights, "model": out["model"], "tokens_in": out["tokens_in"],
            "tokens_out": out["tokens_out"], "cache_read": out["cache_read"],
            "cost": out["cost"],
        }


# Lenses for multi-vote verification (hard mode): different focuses → catch
# different failure modes (confirmation / refutation / relevance).
_LENSES = [
    "",
    "Спробуй СПРОСТУВАТИ твердження: чи є в доказах щось, що йому прямо суперечить?",
    "Оціни РЕЛЕВАНТНІСТЬ: це актуально оператору зараз, чи стара/неважлива інформація?",
]


def _aggregate_votes(results: list[dict], insight: dict) -> dict:
    """Merge several votes into the final verdict (majority) + summed accounting."""
    n = len(results)
    real = [r for r in results if r["verdict"] == "real"]
    refuted = [r for r in results if r["verdict"] == "refuted"]
    if len(real) > n / 2:
        side, verdict = real, "real"
    elif len(refuted) > n / 2:
        side, verdict = refuted, "refuted"
    else:
        side, verdict = results, "uncertain"
    conf = round(sum(r["confidence"] for r in side) / max(1, len(side)), 3)
    # text: from a representative of the winning side (otherwise — from the first vote)
    text = (side[0]["text"] if side else results[0]["text"]) or insight.get("text", "")
    return {
        "verdict": verdict, "confidence": conf, "text": text,
        "model": results[0]["model"], "votes": n,
        "tokens_in": sum(r["tokens_in"] for r in results),
        "tokens_out": sum(r["tokens_out"] for r in results),
        "cache_read": sum(r["cache_read"] for r in results),
        "cost": round(sum(r["cost"] for r in results), 6),
    }


def _intable(x: Any) -> bool:
    try:
        int(x)
        return True
    except (TypeError, ValueError):
        return False
