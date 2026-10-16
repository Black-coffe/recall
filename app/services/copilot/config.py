"""Co-pilot — session settings + the mode x importance matrix (Phase 19, Step 1).

Pure logic without I/O (easy to test). At the start of a call the operator sets
"raw" settings (vector/mode/importance/budget/local-only), and
:func:`resolve_settings` merges them with the matrix defaults into a full snapshot that
CopilotService persists into ``copilot_sessions.config_json``.

The matrix is the SINGLE source of truth for behavioural parameters. At Step 1 the fields
``cadence_sec`` / ``escalate_threshold`` / ``top_k`` / ``safety_sweep_sec`` /
``verify_votes`` are only stored; wiring up the effect — Steps 2-6 (the values
are read FROM HERE, not hardcoded).
"""
from __future__ import annotations

import os
from typing import Any, Optional

from app.services import local_llm
from app.services import models as _models


MODES = ("light", "medium", "hard")
IMPORTANCE = ("low", "medium", "high")

# Co-pilot insight kinds — the SINGLE source (dispatcher schemas, escalator, export).
INSIGHT_KINDS = ("contradiction", "question", "clarification", "fact")

DEFAULT_MODE = "medium"
DEFAULT_IMPORTANCE = "medium"

# --- Mode: how aggressive the analysis is (cadence, escalation threshold, depth) ---
# min_unanchored_conf — confidence threshold for insights WITHOUT proofs from the archive
# (primary triage observations). A 7B local model without evidence is unreliable and floods the
# widget with noise (runtime test 2026-06-02: 82% of cards without proof), so such signals
# are gated: only high-signal kinds (contradiction/fact) with conf >= threshold;
# question/clarification without proof are always dropped. light — silences almost everything.
# T6.8: the thresholds below are a draft default from a single manual test (runtime test
# 2026-06-02, see the min_unanchored_conf comment). The feedback→calibration loop
# is now closed PARTIALLY: CopilotService.get_feedback_by_confidence_bucket()
# (app/services/copilot/service.py) aggregates operator_action (👍/👎/pin/dismiss)
# BY the insight confidence buckets — it gives numbers for a periodic MANUAL review of
# escalate_threshold / min_unanchored_conf. This function changes NOTHING here
# automatically (deliberately: defaults without precision/recall data are risky).
# max_cards / min_card_gap_sec — the operator attention budget (Track 3). Measurements on
# 80 real sessions: 5743 local insights ≈ 72 cards per call with ZERO
# operator reactions over the whole history (operator_action = NULL in 9706 events with
# working 👍/👎 buttons). So the widget was not read — not for lack of signals but
# because of their number. We cap the stream and space it out in time: better 3-5 cards
# you can read than 72 that blend into noise.
MODE_PARAMS: dict[str, dict] = {
    "light":  {"cadence_sec": 90, "escalate_threshold": 0.85, "top_k": 5,
               "safety_sweep_sec": 0,   "verify_votes": 1, "min_unanchored_conf": 0.90,
               "max_cards": 3, "min_card_gap_sec": 240},
    "medium": {"cadence_sec": 50, "escalate_threshold": 0.65, "top_k": 7,
               "safety_sweep_sec": 420, "verify_votes": 1, "min_unanchored_conf": 0.80,
               "max_cards": 5, "min_card_gap_sec": 150},
    "hard":   {"cadence_sec": 28, "escalate_threshold": 0.45, "top_k": 9,
               "safety_sweep_sec": 240, "verify_votes": 3, "min_unanchored_conf": 0.70,
               "max_cards": 8, "min_card_gap_sec": 90},
}

# --- Importance: scales the budget and the API layer ---
# low → local only by default ($0); medium/high → Claude verification.
# use_gnarly — whether to use the arbiter model for "gnarly" (uncertain) cases:
# on high — Opus (T6.2: models are no longer hardcoded here — see
# _copilot_api_model()/_copilot_gnarly_model() below; env-override
# COPILOT_MODEL_API / COPILOT_MODEL_API_GNARLY). Deliberately TWO DIFFERENT models
# (normal=Sonnet, gnarly=Opus) — do not collapse into one.
IMPORTANCE_PARAMS: dict[str, dict] = {
    "low":    {"api_default": False, "budget_usd": 0.0, "use_api": False, "use_gnarly": False},
    "medium": {"api_default": True,  "budget_usd": 1.0, "use_api": True,  "use_gnarly": False},
    "high":   {"api_default": True,  "budget_usd": 3.0, "use_api": True,  "use_gnarly": True},
}


def _copilot_api_model() -> str:
    """The "normal" copilot verification model (Sonnet by default), env-override
    ``COPILOT_MODEL_API``. Read every time — not cached at import."""
    return os.environ.get("COPILOT_MODEL_API", _models.COPILOT_NORMAL_MODEL_DEFAULT)


def _copilot_gnarly_model() -> str:
    """Arbiter model for "gnarly"/uncertain cases (Opus by default),
    env-override ``COPILOT_MODEL_API_GNARLY``."""
    return os.environ.get("COPILOT_MODEL_API_GNARLY", _models.COPILOT_GNARLY_MODEL_DEFAULT)

# Cost-governor: soft warning threshold (share of the budget) before the hard stop.
BUDGET_WARN_RATIO = 0.8


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    try:
        if v is None or (isinstance(v, str) and not v.strip().isdigit()):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_scope_list(raw) -> list:
    """Project/participant names: a string "A, B" or a list → a list of strings."""
    if not raw:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [str(x).strip() for x in items if str(x).strip()]


def resolve_settings(raw: Optional[dict]) -> dict:
    """Merge raw UI settings with the matrix defaults into a full snapshot.

    Input (everything optional):
      mode, importance, api_enabled (bool|None), budget_usd (number|None),
      category_id (int|str|None).

    Returns a normalized dict, ready both for persist (config_json) and for
    later steps. An explicit user choice takes priority over the matrix default.
    """
    raw = raw or {}

    mode = raw.get("mode") if raw.get("mode") in MODES else DEFAULT_MODE
    importance = raw.get("importance") if raw.get("importance") in IMPORTANCE else DEFAULT_IMPORTANCE

    mp = MODE_PARAMS[mode]
    imp = IMPORTANCE_PARAMS[importance]

    # api_enabled: the explicit user toggle wins; otherwise — the importance default.
    raw_api = raw.get("api_enabled")
    api_enabled = bool(raw_api) if raw_api is not None else imp["api_default"]

    # Budget: an explicit positive value wins; otherwise the importance default.
    # "Local only" → the API budget is irrelevant (0).
    budget = _as_float(raw.get("budget_usd"))
    budget_usd = budget if (budget is not None and budget >= 0) else imp["budget_usd"]
    if not api_enabled:
        budget_usd = 0.0

    return {
        "mode": mode,
        "importance": importance,
        "api_enabled": api_enabled,
        "budget_usd": round(budget_usd, 4),
        "category_id": _as_int(raw.get("category_id")),
        # Track 2: the call's fine scope — projects/participants it is about.
        # Categories are too coarse: "Фонд Робота" covers ~more than half of the archive, so the copilot
        # pulled neighbouring directions into hints. The list of names ("Acmecorp, Адам")
        # is resolved into entities at search time (app.services.scope.resolve_scope).
        "scope_projects": _as_scope_list(raw.get("scope_projects")),
        # --- Track 3: how many cards can be shown at all per call ---
        "max_cards": _as_int(raw.get("max_cards")) or mp["max_cards"],
        "min_card_gap_sec": (_as_float(raw.get("min_card_gap_sec"))
                             if _as_float(raw.get("min_card_gap_sec")) is not None
                             else mp["min_card_gap_sec"]),
        # verified_only: show ONLY what Claude confirmed (verdict=real).
        # Default — on when the API is available; with "local only" it turns off
        # by itself, otherwise the copilot would go fully silent (degradation must be soft).
        "verified_only": (bool(raw["verified_only"]) if raw.get("verified_only") is not None
                          else bool(api_enabled)),
        # behavioural parameters (the effect is wired up in Steps 2-6)
        "cadence_sec": mp["cadence_sec"],
        "escalate_threshold": mp["escalate_threshold"],
        "top_k": mp["top_k"],
        "safety_sweep_sec": mp["safety_sweep_sec"],
        "verify_votes": mp["verify_votes"],
        "min_unanchored_conf": mp["min_unanchored_conf"],
        # cost-governor
        "budget_warn_ratio": BUDGET_WARN_RATIO,
        # models
        "model_local": local_llm.LOCAL_LLM_MODEL,
        "model_api": _copilot_api_model() if (api_enabled and imp["use_api"]) else None,
        "model_api_gnarly": _copilot_gnarly_model() if (api_enabled and imp["use_gnarly"]) else None,
    }
