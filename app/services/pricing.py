"""Єдина таблиця тарифів Claude ($/Mtok) — щоб оцінки вартості не розходились.

Раніше тариф дублювався у ``research.py`` і ``copilot/escalate.py``; зміна ціни
моделі оновлювала одну копію й лишала другу застарілою. Тепер — одне джерело.

T6.2 (Волна 3): раніше тут НЕ було ``claude-opus-4-7`` (дефолт у ~10 місцях
коду до фіксу), і ``claude-opus-4-8`` мала ціну (15.0, 75.0) — невідомо звідки
взяту, розбіжну з тарифами Anthropic. Синхронізовано з таблицею skill
``claude-api`` (кеш 2026-06-24) і з реально вживаними в коді моделями
(``app/services/models.py``), щоб оцінка вартості не тихо падала на дефолт.
"""
from __future__ import annotations

from app.services import models as _models

# (input, output) $/Mtok. cache_read тарифікуємо за 0.1× input.
# Джерело цін — skill claude-api (кеш 2026-06-24). claude-sonnet-5 має
# інтро-ціну $2/$10 до 2026-08-31 (тут не моделюємо — TODO, якщо знадобиться
# точний облік інтро-періоду, додати дату-залежну гілку).
MODEL_PRICES: dict[str, tuple[float, float]] = {
    _models.HAIKU_4_5: (1.0, 5.0),
    _models.SONNET_4_6: (3.0, 15.0),
    _models.SONNET_5: (3.0, 15.0),        # TODO: $2/$10 intro price through 2026-08-31 не враховано
    _models.OPUS_4_6: (5.0, 25.0),
    _models.OPUS_4_7: (5.0, 25.0),
    _models.OPUS_4_8: (5.0, 25.0),        # було (15.0, 75.0) — виправлено за тарифами Anthropic
    _models.OPUS_5: (5.0, 25.0),          # drop-in ціна Opus 4.8 (анонс 24.07.2026)
    _models.FABLE_5: (10.0, 50.0),
}

# Фолбек для моделі поза таблицею — той самий дефолт, що й для тексту/RAG.
DEFAULT_PRICE_MODEL = _models.DEFAULT_MODEL


def estimate_cost(model: str, tokens_in: int, tokens_out: int,
                  cache_read: int = 0) -> float:
    """Оцінка $ за виклик. input (uncached) за повну ціну, cache_read за 0.1×."""
    pin, pout = MODEL_PRICES.get(model, MODEL_PRICES[DEFAULT_PRICE_MODEL])
    return round((tokens_in * pin + cache_read * pin * 0.1 + tokens_out * pout) / 1_000_000, 6)
