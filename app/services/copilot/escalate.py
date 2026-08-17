"""Co-pilot — ескалація в Claude API (Phase 19, Крок 5).

Замикає local-first cascade: локальна модель (диспетчер, Крок 3) дає **чернетку**
підказки, а Claude (Sonnet, «старший брат») її **верифікує** — real/refuted/
uncertain — і полірує текст. Дешево, бо: (1) ескалюємо лише позначене локалкою
важливим (не весь транскрипт); (2) prompt caching на стабільному system-блоці
(як у rag.py); (3) форсований tool-use → стислий структурований вихід (мало
вихідних токенів).

Два режими:
- :meth:`Escalator.verify` — перевірити одну чернетку локалки за її доказами.
- :meth:`Escalator.sweep` — safety-sweep: незалежний прохід Sonnet по поточному
  вікну + чанках теми (страховка від «тихих» хибнонегативів локалки, Крок 5).

Усе деградує: нема ключа / API впав → метод повертає None, ко-пілот лишається
локальним ($0). Клієнт і tool-схема інжектяться через ``client_factory`` →
тестується офлайн без мережі.
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


# Фолбек, коли caller не передав model явно (worker.py завжди передає
# ws.config.get("model_api") — див. app/services/copilot/config.py). НЕ
# pricing.DEFAULT_PRICE_MODEL (це загальний app-дефолт, зараз Opus) — тут
# зберігаємо саме "звичайну" copilot-модель (Sonnet), щоб не схлопнути
# normal/gnarly тіри в один за замовчуванням (T6.2).
DEFAULT_MODEL = _models.COPILOT_NORMAL_MODEL_DEFAULT

# Тарифи — спільні з research.py (app/services/pricing.py), щоб не розходились.
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
    """Клієнт ескалації в Claude. ``client_factory`` інжектиться → офлайн-тест."""

    def __init__(self, *, client_factory: Callable[[], Any] = text_polishing._get_client,
                 timeout: float = 60.0):
        self._client_factory = client_factory
        self._timeout = timeout

    def _request(self, *, model: str, system: str, tool: dict, user: str,
                 max_tokens: int) -> Optional[dict]:
        """Один форсований tool-use виклик. Повертає {"input", usage...} або None."""
        try:
            client = self._client_factory()
        except Exception as e:  # нема ключа тощо
            logger.info("[copilot] ескалація недоступна: %s", e)
            return None
        kwargs = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        # Claude 5 (Opus 5 / Sonnet 5): thinking on-by-default. Тут live-вердикт
        # на дзвінку — форсований tool-use з малим max_tokens; adaptive thinking
        # зʼїв би токен-бюджет (кап спільний) і додав секунди → вимикаємо явно.
        # Форсований tool_choice гарантує tool-блок, тож відомий failure mode
        # disabled-thinking («tool call текстом») тут не загрожує.
        if _models.needs_explicit_thinking_off(model):
            kwargs["thinking"] = {"type": "disabled"}
        try:
            # Retry на транзиентних помилках (429/5xx/timeout) — T6.3. Тут
            # НЕ стрім (messages.create), тож увесь виклик безпечно повторити
            # заново — нічого ще не віддано caller'у.
            resp = call_with_retry(
                lambda: client.with_options(timeout=self._timeout).messages.create(**kwargs),
                what="copilot-escalate",
            )
        except Exception as e:
            logger.warning("[copilot] ескалація API збій: %s", e)
            return None
        tool_input = None
        for block in (resp.content or []):
            if getattr(block, "type", None) == "tool_use":
                tool_input = block.input
                break
        if tool_input is None:
            logger.warning("[copilot] ескалація: модель не викликала інструмент")
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
        """Один голос верифікації (опційно з лінзою для різноманіття)."""
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
        """Верифікувати чернетку локалки. ``votes`` > 1 → мульти-лінза (жорсткий
        режим, Крок 6): кілька незалежних голосів з різними фокусами, мажоритарний
        вердикт. Returns verdict-dict (+сумарний облік токенів) або None.

        ``on_vote(result)`` викликається ПІСЛЯ кожного голосу (для облікy токенів/$
        одразу), ``can_continue()`` перевіряється ПЕРЕД кожним наступним голосом —
        повертає False → припиняємо витрати (бюджет вичерпано посеред мульти-голосу,
        Крок 9 fix). Перший голос завжди виконується (вхід уже відгейтовано caller'ом)."""
        if not evidence_chunks:
            return None
        votes = max(1, int(votes or 1))
        results = []
        for i in range(votes):
            if i > 0 and can_continue is not None and not can_continue():
                break  # бюджет вичерпано попереднім голосом → не витрачаємо далі
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
        """Safety-прохід Sonnet по вікну+чанках. Returns {"insights", облік} або None."""
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


# Лінзи для мульти-голосової перевірки (жорсткий режим): різні фокуси → ловлять
# різні режими помилок (підтвердження / спростування / актуальність).
_LENSES = [
    "",
    "Спробуй СПРОСТУВАТИ твердження: чи є в доказах щось, що йому прямо суперечить?",
    "Оціни РЕЛЕВАНТНІСТЬ: це актуально оператору зараз, чи стара/неважлива інформація?",
]


def _aggregate_votes(results: list[dict], insight: dict) -> dict:
    """Звести кілька голосів у фінальний вердикт (мажоритарно) + сумарний облік."""
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
    # текст: з представника переможної сторони (інакше — з першого голосу)
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
