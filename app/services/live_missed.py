"""«What did I miss» during a live call (wave4-client-calls-03).

The live preview segments of a recording (`live_transcribe_worker.get_preview`) are cut to a
window and Claude Sonnet is asked once for 3-5 lines: what was decided, what was promised, what
was asked of the owner. The cost is estimated BEFORE the call (`pricing.estimate_cost`) and
reserved against an in-memory per-session total; over `LIVE_MISSED_BUDGET_USD` the call is refused.

Never the local model: `local_llm.generate` holds `_GEN_LOCK`, the same lock as the copilot
ticks, so a summary there would silence the copilot for the length of the answer. Nothing is
persisted (no event, no ask_log): the per-session spend lives in this process only.
"""
from __future__ import annotations

import functools
import logging
import threading
from typing import Any, Optional

from app.core import settings as _settings
from app.services import models as _models
from app.services import pricing, text_polishing
from app.services.claude_retry import call_with_retry
from app.services.copilot.escalate import DEFAULT_MODEL as SUMMARY_MODEL


logger = logging.getLogger(__name__)

WINDOWS = ("since_self", "2m", "5m", "10m", "whole")
_MINUTES = {"2m": 120.0, "5m": 300.0, "10m": 600.0}

# The `whole` window is the tail of the preview, capped at this many characters.
MAX_CHARS = 30000
_MAX_TOKENS = 400
# Conservative chars-per-token for the pre-call estimate (Cyrillic tokenizes densely).
_CHARS_PER_TOKEN = 2.5

_SPEAKERS = {"self": "Me", "other": "Other"}

SYSTEM_PROMPT = (
    "You help the owner of a live call catch up on what they missed. You get a fragment of the "
    "live transcript; lines marked [Me] are the owner, [Other] the other side. Reply with 3 to 5 "
    "short lines, one per line, no preamble: what was decided, what was promised (and by whom), "
    "and what was asked of the owner. Use only the transcript; if something did not happen, do "
    "not invent it. Write in the language of the transcript."
)

_lock = threading.Lock()
_spent: dict[str, float] = {}


class BudgetExceeded(Exception):
    """The call would push the session's spend over `LIVE_MISSED_BUDGET_USD`."""

    def __init__(self, spent_usd: float, budget_usd: float):
        super().__init__(f"live-missed budget exhausted: {spent_usd} of {budget_usd}")
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd


class Unavailable(Exception):
    """No `ANTHROPIC_API_KEY`, or the Claude call failed after retries."""


def spent(session_id: str) -> float:
    with _lock:
        return round(_spent.get(session_id, 0.0), 6)


def budget() -> float:
    return _settings.env_float("LIVE_MISSED_BUDGET_USD")


def _time(seg: dict, field: str = "end") -> float:
    try:
        return float(seg.get(field, seg.get("start", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _line(seg: dict) -> str:
    return f"[{_SPEAKERS.get(seg.get('speaker'), '?')}] {(seg.get('text') or '').strip()}"


def select_window(segments: list[dict], window: str, now: Optional[float] = None) -> dict:
    """Cut the preview to a window. Returns ``{segments, since, truncated}``.

    ``since_self``: after the owner's last ``self`` segment (``since`` = its end), all with
    ``since=None`` when the owner has not spoken. ``2m|5m|10m``: segments that end within the
    window before ``now`` (default: the end of the last segment; times are the preview's
    ``start``/``end`` seconds). ``whole``: everything, cut to the last ``MAX_CHARS`` characters.
    """
    if window not in WINDOWS:
        raise ValueError(f"unknown window {window!r}")
    segs = [s for s in (segments or []) if (s.get("text") or "").strip()]
    since: Optional[float] = None
    truncated = False
    if window == "since_self":
        last = max((i for i, s in enumerate(segs) if s.get("speaker") == "self"), default=None)
        if last is not None:
            since = _time(segs[last])
            segs = segs[last + 1:]
    elif window in _MINUTES:
        ref = now if now is not None else (max(_time(s) for s in segs) if segs else 0.0)
        since = ref - _MINUTES[window]
        segs = [s for s in segs if _time(s) >= since]
    else:  # whole
        kept, total = [], 0
        for s in reversed(segs):
            total += len(_line(s)) + 1
            if total > MAX_CHARS and kept:
                break
            kept.append(s)
        truncated = len(kept) < len(segs) or total > MAX_CHARS
        segs = list(reversed(kept))
    return {"segments": segs, "since": since, "truncated": truncated}


def window_text(segments: list[dict]) -> str:
    text = "\n".join(_line(s) for s in segments)
    return text[-MAX_CHARS:]


def _create(**kwargs) -> Any:
    client = text_polishing._get_client()
    return client.with_options(timeout=60.0).messages.create(**kwargs)


def _lines(text: str) -> list[str]:
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if line:
            out.append(line)
    return out[:5]


def summarize(session_id: str, window: str = "since_self", *,
              segments: list[dict], now: Optional[float] = None) -> dict:
    """One Sonnet call over the window. Raises `ValueError` (window), `Unavailable`
    (no key / API failure) or `BudgetExceeded` (before any call is made)."""
    picked = select_window(segments, window, now)
    text = window_text(picked["segments"])
    cap = budget()
    base = {"window": window, "segments_used": len(picked["segments"]),
            "since": picked["since"], "truncated": picked["truncated"], "budget_usd": cap}
    if not text:
        return {**base, "text": "", "lines": [], "cost_usd": 0.0, "spent_usd": spent(session_id)}
    if not text_polishing.is_available():
        raise Unavailable("ANTHROPIC_API_KEY is not set")

    user = f"Live transcript fragment:\n{text}"
    est_in = int((len(SYSTEM_PROMPT) + len(user)) / _CHARS_PER_TOKEN) + 1
    estimate = pricing.estimate_cost(SUMMARY_MODEL, est_in, _MAX_TOKENS)
    with _lock:
        current = _spent.get(session_id, 0.0)
        if current + estimate > cap:
            raise BudgetExceeded(round(current, 6), cap)
        _spent[session_id] = current + estimate   # reserve; settled below

    kwargs: dict[str, Any] = dict(model=SUMMARY_MODEL, max_tokens=_MAX_TOKENS, system=SYSTEM_PROMPT,
                                  messages=[{"role": "user", "content": user}])
    if _models.needs_explicit_thinking_off(SUMMARY_MODEL):
        kwargs["thinking"] = {"type": "disabled"}
    try:
        resp = call_with_retry(functools.partial(_create, **kwargs), what="live-missed")
    except Exception as e:
        with _lock:
            _spent[session_id] = max(0.0, _spent.get(session_id, 0.0) - estimate)
        logger.warning("[live_missed] Claude call failed for %s: %s", session_id, e)
        raise Unavailable(str(e)) from e

    usage = getattr(resp, "usage", None)
    tin = getattr(usage, "input_tokens", 0) or 0
    tout = getattr(usage, "output_tokens", 0) or 0
    cost = pricing.estimate_cost(SUMMARY_MODEL, tin, tout) if (tin or tout) else estimate
    with _lock:
        _spent[session_id] = max(0.0, _spent.get(session_id, 0.0) - estimate) + cost
    answer = "".join(getattr(b, "text", "") for b in (getattr(resp, "content", None) or [])
                     if getattr(b, "type", None) == "text").strip()
    lines = _lines(answer)
    return {**base, "text": "\n".join(lines), "lines": lines, "cost_usd": cost,
            "spent_usd": spent(session_id)}
