"""Co-pilot — налаштування сесії + матриця режим×важливість (Phase 19, Крок 1).

Чиста логіка без I/O (легко тестується). Оператор на старті дзвінка задає
«сирі» налаштування (вектор/режим/важливість/бюджет/лише-локально), а
:func:`resolve_settings` зводить їх з дефолтами матриці у повний знімок, який
CopilotService персистить у ``copilot_sessions.config_json``.

Матриця — ЄДИНЕ джерело правди для поведінкових параметрів. На Кроці 1 поля
``cadence_sec`` / ``escalate_threshold`` / ``top_k`` / ``safety_sweep_sec`` /
``verify_votes`` лише зберігаються; підключення ефекту — Кроки 2-6 (значення
там читаються ЗВІДСИ, не хардкодяться).
"""
from __future__ import annotations

import os
from typing import Any, Optional

from app.services import local_llm
from app.services import models as _models


MODES = ("light", "medium", "hard")
IMPORTANCE = ("low", "medium", "high")

# Типи інсайтів ко-пілота — ЄДИНЕ джерело (диспетчер-схеми, ескалатор, експорт).
INSIGHT_KINDS = ("contradiction", "question", "clarification", "fact")

DEFAULT_MODE = "medium"
DEFAULT_IMPORTANCE = "medium"

# --- Режим: наскільки агресивний аналіз (каденс, поріг ескалації, глибина) ---
# min_unanchored_conf — поріг впевненості для інсайтів БЕЗ пруфів з архіву
# (первинні спостереження триажу). Локалка 7B без доказів ненадійна й заливає
# віджет шумом (рантайм-тест 2026-06-02: 82% карток без пруфа), тож такі сигнали
# гейтимо: лише високосигнальні види (contradiction/fact) з conf >= порога;
# question/clarification без пруфа відкидаємо завжди. light — майже все глушить.
# T6.8: пороги нижче — чорновий дефолт з одного ручного тесту (рантайм-тест
# 2026-06-02, див. коментар min_unanchored_conf). Цикл feedback→калібрування
# тепер замкнутий ЧАСТКОВО: CopilotService.get_feedback_by_confidence_bucket()
# (app/services/copilot/service.py) агрегує operator_action (👍/👎/pin/dismiss)
# ЗА confidence-бакетами інсайту — дає числа для періодичного РУЧНОГО перегляду
# escalate_threshold / min_unanchored_conf. Ця функція НІЧОГО тут не змінює
# автоматично (свідомо: дефолти без даних для precision/recall — ризиковано).
# max_cards / min_card_gap_sec — бюджет уваги оператора (Трек 3). Заміри на
# 80 реальних сесіях: 5743 локальних інсайти ≈ 72 картки за дзвінок при НУЛІ
# реакцій оператора за всю історію (operator_action = NULL у 9706 подіях при
# робочих кнопках 👍/👎). Тобто віджет не читали — не через брак сигналів, а
# через їх кількість. Обмежуємо потік і рознесимо в часі: краще 3-5 карток,
# які встигнеш прочитати, ніж 72, які зіллються в шум.
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

# --- Важливість: масштабує бюджет і шар API ---
# low → за замовч. лише локально ($0); medium/high → Claude-верифікація.
# use_gnarly — чи задіювати модель-арбітра для «гнарлі» (uncertain) кейсів:
# на high — Opus (T6.2: моделі більше не хардкодяться тут — див.
# _copilot_api_model()/_copilot_gnarly_model() нижче; env-override
# COPILOT_MODEL_API / COPILOT_MODEL_API_GNARLY). Свідомо ДВІ РІЗНІ моделі
# (normal=Sonnet, gnarly=Opus) — не схлопувати в одну.
IMPORTANCE_PARAMS: dict[str, dict] = {
    "low":    {"api_default": False, "budget_usd": 0.0, "use_api": False, "use_gnarly": False},
    "medium": {"api_default": True,  "budget_usd": 1.0, "use_api": True,  "use_gnarly": False},
    "high":   {"api_default": True,  "budget_usd": 3.0, "use_api": True,  "use_gnarly": True},
}


def _copilot_api_model() -> str:
    """«Звичайна» copilot-модель верифікації (Sonnet за замовч.), env-override
    ``COPILOT_MODEL_API``. Читається щоразу — не кешується на import."""
    return os.environ.get("COPILOT_MODEL_API", _models.COPILOT_NORMAL_MODEL_DEFAULT)


def _copilot_gnarly_model() -> str:
    """Модель-арбітр для «гнарлі»/uncertain кейсів (Opus за замовч.),
    env-override ``COPILOT_MODEL_API_GNARLY``."""
    return os.environ.get("COPILOT_MODEL_API_GNARLY", _models.COPILOT_GNARLY_MODEL_DEFAULT)

# Cost-governor: м'який поріг попередження (частка бюджету) перед хард-стопом.
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
    """Назви проєктів/учасників: рядок «A, B» або список → список рядків."""
    if not raw:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [str(x).strip() for x in items if str(x).strip()]


def resolve_settings(raw: Optional[dict]) -> dict:
    """Звести сирі UI-налаштування з дефолтами матриці у повний знімок.

    Вхід (усе опціональне):
      mode, importance, api_enabled (bool|None), budget_usd (number|None),
      category_id (int|str|None).

    Повертає нормалізований dict, готовий і для persist (config_json), і для
    подальших кроків. Явний вибір користувача має пріоритет над дефолтом матриці.
    """
    raw = raw or {}

    mode = raw.get("mode") if raw.get("mode") in MODES else DEFAULT_MODE
    importance = raw.get("importance") if raw.get("importance") in IMPORTANCE else DEFAULT_IMPORTANCE

    mp = MODE_PARAMS[mode]
    imp = IMPORTANCE_PARAMS[importance]

    # api_enabled: явний тумблер користувача переважає; інакше — дефолт важливості.
    raw_api = raw.get("api_enabled")
    api_enabled = bool(raw_api) if raw_api is not None else imp["api_default"]

    # Бюджет: явне додатне значення переважає; інакше дефолт важливості.
    # «Лише локально» → бюджет API нерелевантний (0).
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
        # Трек 2: тонкий скоуп дзвінка — проєкти/учасники, про яких він.
        # Категорії мало: «Робота» покриває ~більша частина архіву, тож копілот
        # тягнув у підказки сусідні напрямки. Список назв («Acmecorp, Адам»)
        # резолвиться у сутності при пошуку (app.services.scope.resolve_scope).
        "scope_projects": _as_scope_list(raw.get("scope_projects")),
        # --- Трек 3: скільки карток узагалі можна показати за дзвінок ---
        "max_cards": _as_int(raw.get("max_cards")) or mp["max_cards"],
        "min_card_gap_sec": (_as_float(raw.get("min_card_gap_sec"))
                             if _as_float(raw.get("min_card_gap_sec")) is not None
                             else mp["min_card_gap_sec"]),
        # verified_only: показувати ЛИШЕ те, що Claude підтвердив (verdict=real).
        # Дефолт — увімкнено, коли API доступний; на «лише локально» вимикається
        # само, інакше копілот замовк би повністю (деградація має бути мʼякою).
        "verified_only": (bool(raw["verified_only"]) if raw.get("verified_only") is not None
                          else bool(api_enabled)),
        # поведінкові параметри (ефект підключається у Кроках 2-6)
        "cadence_sec": mp["cadence_sec"],
        "escalate_threshold": mp["escalate_threshold"],
        "top_k": mp["top_k"],
        "safety_sweep_sec": mp["safety_sweep_sec"],
        "verify_votes": mp["verify_votes"],
        "min_unanchored_conf": mp["min_unanchored_conf"],
        # cost-governor
        "budget_warn_ratio": BUDGET_WARN_RATIO,
        # моделі
        "model_local": local_llm.LOCAL_LLM_MODEL,
        "model_api": _copilot_api_model() if (api_enabled and imp["use_api"]) else None,
        "model_api_gnarly": _copilot_gnarly_model() if (api_enabled and imp["use_gnarly"]) else None,
    }
