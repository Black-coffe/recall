"""Single table of Claude rates ($/Mtok) — so cost estimates do not diverge.

The rate used to be duplicated in ``research.py`` and ``copilot/escalate.py``; a price change
of a model updated one copy and left the other stale. Now — one source.

T6.2 (Wave 3): ``claude-opus-4-7`` was NOT here before (the default in ~10 places
of code before the fix), and ``claude-opus-4-8`` had the price (15.0, 75.0) — taken from
who knows where, diverging from Anthropic's rates. Synchronized with the table of the skill
``claude-api`` (cache 2026-06-24) and with the models actually used in the code
(``app/services/models.py``), so the cost estimate does not quietly fall back to the default.
"""
from __future__ import annotations

from app.services import models as _models

# (input, output) $/Mtok. cache_read is priced at 0.1× input.
# Price source — skill claude-api (cache 2026-06-24). claude-sonnet-5 has an
# intro price of $2/$10 until 2026-08-31 (not modelled here — TODO, if exact accounting
# of the intro period is needed, add a date-dependent branch).
MODEL_PRICES: dict[str, tuple[float, float]] = {
    _models.HAIKU_4_5: (1.0, 5.0),
    _models.SONNET_4_6: (3.0, 15.0),
    _models.SONNET_5: (3.0, 15.0),        # TODO: $2/$10 intro price through 2026-08-31 not accounted for
    _models.OPUS_4_6: (5.0, 25.0),
    _models.OPUS_4_7: (5.0, 25.0),
    _models.OPUS_4_8: (5.0, 25.0),        # was (15.0, 75.0) — fixed per Anthropic rates
    _models.OPUS_5: (5.0, 25.0),          # drop-in price of Opus 4.8 (announced 24.07.2026)
    _models.FABLE_5: (10.0, 50.0),
}

# Fallback for a model outside the table — the same default as for text/RAG.
DEFAULT_PRICE_MODEL = _models.DEFAULT_MODEL


def estimate_cost(model: str, tokens_in: int, tokens_out: int,
                  cache_read: int = 0) -> float:
    """Estimate of $ per call. input (uncached) at the full price, cache_read at 0.1×."""
    pin, pout = MODEL_PRICES.get(model, MODEL_PRICES[DEFAULT_PRICE_MODEL])
    return round((tokens_in * pin + cache_read * pin * 0.1 + tokens_out * pout) / 1_000_000, 6)
