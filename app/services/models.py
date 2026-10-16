"""Single source of truth for the Claude model IDs used in Recall
(REMEDIATION_PLAN Wave 3, T6.2).

The default model used to be hardcoded separately in ~10 places
(``os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")``) — and ``claude-opus-4-7``
WAS an invalid ID (not the same as outdated — simply absent from the
current model list), so ``pricing.py`` did not know about it and the cost estimate
quietly fell back to the default price of another model. Now: one constant
:data:`DEFAULT_MODEL`, one env override (``CLAUDE_MODEL``), imported by
the other modules (``text_polishing``, ``rag``, ``system`` blueprint).

IDs checked against the ``claude-api`` skill (cache 2026-06-24) — NOT from the model's memory.
"""
from __future__ import annotations

import os


# --- Current model IDs (skill claude-api) ---
FABLE_5 = "claude-fable-5"
OPUS_5 = "claude-opus-5"
OPUS_4_8 = "claude-opus-4-8"
OPUS_4_7 = "claude-opus-4-7"
OPUS_4_6 = "claude-opus-4-6"
SONNET_5 = "claude-sonnet-5"
SONNET_4_6 = "claude-sonnet-4-6"
HAIKU_4_5 = "claude-haiku-4-5-20251001"


# Default model for text/RAG/enrichment (polish, summarize, translate,
# extract_meeting_card, RAG chat). History: "claude-opus-4-7" (invalid ID,
# did not map in pricing.py) → T6.2: claude-opus-4-8 → 28.07.2026: claude-opus-5 —
# a drop-in replacement at the same price ($5/$25 MTok), owner decision. A cheaper
# default (e.g. claude-sonnet-5) — as before, one change here.
DEFAULT_MODEL = OPUS_5

# Copilot (Phase 19) uses TWO DIFFERENT models for different logic — regular
# verification (Sonnet) and arbitration of "gnarly"/uncertain cases (Opus, more expensive and
# slower). DO NOT collapse into one — see app/services/copilot/config.py.
# 28.07.2026: raised to the Claude 5 generation (Sonnet 4.6→5, Opus 4.8→5) — better
# verdicts at the same nominal rates (caveat: the Sonnet 5 tokenizer counts ≈+30%
# tokens for the same text). In Claude 5 thinking is on by default —
# live copilot calls turn it off explicitly (see THINKING_ON_BY_DEFAULT_MODELS
# and escalate.py), otherwise thinking eats the small max_tokens and adds latency.
COPILOT_NORMAL_MODEL_DEFAULT = SONNET_5
COPILOT_GNARLY_MODEL_DEFAULT = OPUS_5

# Models that support adaptive thinking + output_config.effort (Opus 4.6+,
# Sonnet 4.6+, Claude 5). On Opus 5 / Fable 5 thinking is on by
# default, an explicit {"type": "adaptive"} is accepted. Haiku does NOT support
# effort/adaptive thinking (400) — deliberately excluded.
ADAPTIVE_THINKING_MODELS: tuple[str, ...] = (
    FABLE_5, OPUS_5, OPUS_4_8, OPUS_4_7, OPUS_4_6, SONNET_5, SONNET_4_6,
)


def get_default_model() -> str:
    """Default model for text/RAG taking the CLAUDE_MODEL env override into account.

    Reads env every time (does not cache on import) — consistent with the previous
    behaviour of the call sites and allows changing CLAUDE_MODEL without restarting the
    process in tests.
    """
    return os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)


def supports_adaptive_thinking(model: str) -> bool:
    """Whether this model supports ``thinking: {"type": "adaptive"}`` + ``output_config.effort``."""
    return bool(model) and model.startswith(ADAPTIVE_THINKING_MODELS)


# Claude 5 models where thinking is ON by default, but it can be
# turned off with an explicit ``{"type": "disabled"}`` (legal at effort high and below).
# FABLE_5 is deliberately NOT included: there disabled returns 400 (thinking is always on).
THINKING_ON_BY_DEFAULT_MODELS: tuple[str, ...] = (OPUS_5, SONNET_5)


def needs_explicit_thinking_off(model: str) -> bool:
    """Whether this model needs ``thinking: {"type": "disabled"}`` passed explicitly in
    latency-sensitive calls (live copilot: forced tool use, small
    max_tokens). On Claude 5 thinking is on by default; without an explicit disabled it
    spends max_tokens (the cap is shared between thinking+answer) and adds seconds."""
    return bool(model) and model.startswith(THINKING_ON_BY_DEFAULT_MODELS)
