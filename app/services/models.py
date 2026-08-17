"""Єдина точка правди для ID моделей Claude, що використовуються в Recall
(REMEDIATION_PLAN Волна 3, T6.2).

Раніше дефолтна модель була захардкоджена окремо у ~10 місцях
(``os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")``) — і ``claude-opus-4-7``
БУЛА невалідним ID (не тим самим, що застарілим — просто відсутнім у
поточному переліку моделей), тож ``pricing.py`` не знав про неї і оцінка
вартості тихо падала на дефолтну ціну іншої моделі. Тепер: одна константа
:data:`DEFAULT_MODEL`, один env override (``CLAUDE_MODEL``), імпортується
рештою модулів (``text_polishing``, ``rag``, ``system`` blueprint).

ID звірені зі skill ``claude-api`` (кеш 2026-06-24) — НЕ з памʼяті моделі.
"""
from __future__ import annotations

import os


# --- Актуальні ID моделей (skill claude-api) ---
FABLE_5 = "claude-fable-5"
OPUS_5 = "claude-opus-5"
OPUS_4_8 = "claude-opus-4-8"
OPUS_4_7 = "claude-opus-4-7"
OPUS_4_6 = "claude-opus-4-6"
SONNET_5 = "claude-sonnet-5"
SONNET_4_6 = "claude-sonnet-4-6"
HAIKU_4_5 = "claude-haiku-4-5-20251001"


# Дефолтна модель для тексту/RAG/enrichment (polish, summarize, translate,
# extract_meeting_card, RAG-чат). Історія: "claude-opus-4-7" (невалідний ID,
# не мапився у pricing.py) → T6.2: claude-opus-4-8 → 28.07.2026: claude-opus-5 —
# drop-in заміна за тією ж ціною ($5/$25 MTok), рішення власника. Дешевший
# дефолт (напр. claude-sonnet-5) — як і раніше, одна зміна тут.
DEFAULT_MODEL = OPUS_5

# Copilot (Phase 19) використовує ДВІ РІЗНІ моделі за різною логікою — звичайну
# верифікацію (Sonnet) і арбітраж «гнарлі»/uncertain кейсів (Opus, дорожче й
# повільніше). НЕ схлопувати в одну — див. app/services/copilot/config.py.
# 28.07.2026: підняті на покоління Claude 5 (Sonnet 4.6→5, Opus 4.8→5) — кращі
# вердикти за ті самі номінальні тарифи (нюанс: токенайзер Sonnet 5 рахує ≈+30%
# токенів на той самий текст). У Claude 5 thinking увімкнений за замовчуванням —
# live-виклики копілота вимикають його явно (див. THINKING_ON_BY_DEFAULT_MODELS
# і escalate.py), інакше thinking зʼїдає малий max_tokens і додає латентність.
COPILOT_NORMAL_MODEL_DEFAULT = SONNET_5
COPILOT_GNARLY_MODEL_DEFAULT = OPUS_5

# Моделі, що підтримують adaptive thinking + output_config.effort (Opus 4.6+,
# Sonnet 4.6+, Claude 5). На Opus 5 / Fable 5 thinking взагалі увімкнений за
# замовчуванням, явний {"type": "adaptive"} приймається. Haiku НЕ підтримує
# effort/adaptive thinking (400) — свідомо виключений.
ADAPTIVE_THINKING_MODELS: tuple[str, ...] = (
    FABLE_5, OPUS_5, OPUS_4_8, OPUS_4_7, OPUS_4_6, SONNET_5, SONNET_4_6,
)


def get_default_model() -> str:
    """Дефолтна модель для тексту/RAG з урахуванням env override CLAUDE_MODEL.

    Читає env щоразу (не кешує на import) — узгоджено з попередньою
    поведінкою місць виклику й дозволяє змінювати CLAUDE_MODEL без рестарту
    процесу в тестах.
    """
    return os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)


def supports_adaptive_thinking(model: str) -> bool:
    """Чи підтримує ця модель ``thinking: {"type": "adaptive"}`` + ``output_config.effort``."""
    return bool(model) and model.startswith(ADAPTIVE_THINKING_MODELS)


# Моделі Claude 5, де thinking УВІМКНЕНИЙ за замовчуванням, але його можна
# вимкнути явним ``{"type": "disabled"}`` (легально на ефорті high і нижче).
# FABLE_5 сюди свідомо НЕ входить: там disabled повертає 400 (thinking завжди on).
THINKING_ON_BY_DEFAULT_MODELS: tuple[str, ...] = (OPUS_5, SONNET_5)


def needs_explicit_thinking_off(model: str) -> bool:
    """Чи треба цій моделі явно передати ``thinking: {"type": "disabled"}`` у
    latency-чутливих викликах (live-копілот: форсований tool-use, малий
    max_tokens). На Claude 5 thinking on-by-default; без явного disabled він
    витрачає max_tokens (кап спільний на thinking+відповідь) і додає секунди."""
    return bool(model) and model.startswith(THINKING_ON_BY_DEFAULT_MODELS)
