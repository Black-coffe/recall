"""Тести єдиної точки правди для моделей Claude (REMEDIATION_PLAN Волна 3, T6.2).

Покриваємо:
- app.services.models: дефолт, env-override CLAUDE_MODEL, adaptive-thinking gate.
- app.services.pricing: усі реально вживані моделі мають ціну (немає "дір").
- app.services.copilot.config: normal (Sonnet) і gnarly (Opus) моделі лишаються
  РІЗНИМИ (не схлопнуті), і обидві env-override'яться незалежно.
"""
from __future__ import annotations

import pytest

from app.services import models
from app.services import pricing
from app.services.copilot import config as copilot_config


class TestDefaultModel:
    def test_default_model_is_opus_5_not_stale_generations(self):
        assert models.DEFAULT_MODEL == "claude-opus-5"
        assert models.DEFAULT_MODEL != "claude-opus-4-7"

    def test_get_default_model_no_env_override(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        assert models.get_default_model() == "claude-opus-5"

    def test_get_default_model_respects_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
        assert models.get_default_model() == "claude-sonnet-5"


class TestAdaptiveThinkingGate:
    @pytest.mark.parametrize("model", [
        "claude-fable-5", "claude-opus-5",
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-5", "claude-sonnet-4-6",
    ])
    def test_supported_models(self, model):
        assert models.supports_adaptive_thinking(model) is True

    @pytest.mark.parametrize("model", [
        "claude-haiku-4-5-20251001", "claude-haiku-4-5", "", None,
    ])
    def test_unsupported_models(self, model):
        assert models.supports_adaptive_thinking(model) is False


class TestPricingSync:
    """pricing.py не повинен мати 'дір' де cost estimate тихо падає на дефолт."""

    @pytest.mark.parametrize("model", [
        models.DEFAULT_MODEL,                  # app-wide default (opus-5)
        models.COPILOT_NORMAL_MODEL_DEFAULT,    # copilot "normal" (sonnet-5)
        models.COPILOT_GNARLY_MODEL_DEFAULT,    # copilot "gnarly" (opus-5)
        models.HAIKU_4_5,                       # research.py summary default
        models.SONNET_4_6,
    ])
    def test_actually_used_models_have_explicit_price(self, model):
        assert model in pricing.MODEL_PRICES, (
            f"{model} реально вживається кодом, але відсутній у MODEL_PRICES — "
            "cost estimate тихо впаде на DEFAULT_PRICE_MODEL"
        )

    def test_opus_4_8_price_is_not_the_old_wrong_value(self):
        # Було (15.0, 75.0) — невідомо звідки взяте, розбіжне з тарифами Anthropic.
        assert pricing.MODEL_PRICES["claude-opus-4-8"] != (15.0, 75.0)

    def test_estimate_cost_known_model_uses_its_own_price(self):
        cost = pricing.estimate_cost("claude-opus-4-8", 1_000_000, 0)
        pin, _ = pricing.MODEL_PRICES["claude-opus-4-8"]
        assert cost == pytest.approx(pin, rel=1e-6)

    def test_estimate_cost_unknown_model_falls_back_to_default_price_model(self):
        cost_unknown = pricing.estimate_cost("claude-totally-unknown-model", 1_000_000, 0)
        cost_default = pricing.estimate_cost(pricing.DEFAULT_PRICE_MODEL, 1_000_000, 0)
        assert cost_unknown == cost_default


class TestThinkingOffGate:
    """Claude 5 (Opus 5 / Sonnet 5): thinking on-by-default → live-виклики
    копілота мають явно вимикати його. Fable 5 — НЕ можна (disabled = 400)."""

    @pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5"])
    def test_claude5_needs_explicit_off(self, model):
        assert models.needs_explicit_thinking_off(model) is True

    @pytest.mark.parametrize("model", [
        "claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001", "", None,
    ])
    def test_others_do_not(self, model):
        assert models.needs_explicit_thinking_off(model) is False

    def test_escalator_passes_thinking_disabled_for_claude5_only(self):
        """Регресія: _request мусить слати thinking=disabled для Claude 5 і НЕ
        слати поле взагалі для старших поколінь."""
        from app.services.copilot.escalate import Escalator

        captured: dict = {}

        class _Block:
            type = "tool_use"
            input = {"verdict": "real", "confidence": 0.9}

        class _Resp:
            content = [_Block()]
            model = "x"
            usage = None

        class _Messages:
            def create(self, **kw):
                captured.clear()
                captured.update(kw)
                return _Resp()

        class _Client:
            messages = _Messages()

            def with_options(self, **kw):
                return self

        esc = Escalator(client_factory=lambda: _Client())
        tool = {"name": "report_verdict"}

        esc._request(model="claude-opus-5", system="s", tool=tool, user="u", max_tokens=100)
        assert captured.get("thinking") == {"type": "disabled"}

        esc._request(model="claude-opus-4-8", system="s", tool=tool, user="u", max_tokens=100)
        assert "thinking" not in captured


class TestCopilotModelsNotCollapsed:
    """Copilot normal (Sonnet) і gnarly (Opus) — свідомо РІЗНІ моделі."""

    def test_medium_importance_uses_normal_model_no_gnarly(self):
        cfg = copilot_config.resolve_settings({"mode": "medium", "importance": "medium"})
        assert cfg["model_api"] == copilot_config._copilot_api_model()
        assert cfg["model_api_gnarly"] is None

    def test_high_importance_uses_both_normal_and_gnarly_and_they_differ(self):
        cfg = copilot_config.resolve_settings({"mode": "medium", "importance": "high"})
        assert cfg["model_api"] is not None
        assert cfg["model_api_gnarly"] is not None
        assert cfg["model_api"] != cfg["model_api_gnarly"]

    def test_low_importance_has_no_api_models(self):
        cfg = copilot_config.resolve_settings({"mode": "medium", "importance": "low"})
        assert cfg["model_api"] is None
        assert cfg["model_api_gnarly"] is None

    def test_normal_and_gnarly_env_overrides_are_independent(self, monkeypatch):
        monkeypatch.setenv("COPILOT_MODEL_API", "claude-sonnet-5")
        monkeypatch.delenv("COPILOT_MODEL_API_GNARLY", raising=False)
        cfg = copilot_config.resolve_settings({"mode": "medium", "importance": "high"})
        assert cfg["model_api"] == "claude-sonnet-5"
        # gnarly лишається дефолтним (Opus) — env-override normal не чіпає gnarly.
        assert cfg["model_api_gnarly"] == models.COPILOT_GNARLY_MODEL_DEFAULT
