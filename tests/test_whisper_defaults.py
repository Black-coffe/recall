"""One default Whisper model everywhere (whisper-best-default, 2026-10-21).

The measurement in docs/specs/whisper-best-default/measurement.md picked large-v3 as the
quality default and large-v3-turbo for live call transcription. The default is a literal
repeated across the registry, the routes, the warm-up and the SPA, so a revert of any one
of them is silent; this file pins all of them to the same value.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.core import settings
from app.services import transcription_run, whisper_preload
from app.services.live_transcribe import LiveTranscribeWorker

REPO = Path(__file__).resolve().parents[1]
DEFAULT = "large-v3"
LIVE = "large-v3-turbo"


def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_registry_defaults_are_the_quality_model():
    for key in ("TELEGRAM_WHISPER_MODEL", "RECALL_PRELOAD_WHISPER_MODEL", "RECORDING_AUTOSAVE_MODEL"):
        assert settings.by_name()[key].default == DEFAULT, key


def test_preload_default_matches():
    assert whisper_preload.DEFAULT_PRELOAD_MODEL == DEFAULT


def test_transcribe_params_default_matches():
    assert transcription_run.TranscribeParams(source="file").model == DEFAULT


def test_transcribe_routes_fall_back_to_the_default():
    src = _src("app/blueprints/transcription.py")
    assert "request.form.get('model') or 'large-v3'" in src
    assert "data.get('model') or 'large-v3'" in src
    assert "'base'" not in src


def test_telegram_blueprint_fallback_matches():
    assert "cfg.get('TELEGRAM_WHISPER_MODEL', 'large-v3')" in _src("app/blueprints/telegram.py")


def test_live_worker_runs_turbo():
    sig = inspect.signature(LiveTranscribeWorker.__init__)
    assert sig.parameters["model_name"].default == LIVE
    assert re.search(r"LiveTranscribeWorker\([^)]*model_name='large-v3-turbo'", _src("app.py"), re.S)


def test_catalog_recommends_only_the_default():
    from whisper_manager_new import FASTER_MODELS_META
    recommended = [name for name, info in FASTER_MODELS_META.items() if info.get("recommended")]
    assert recommended == [DEFAULT]


SPA_VIEWS = ("upload", "youtube", "audio", "record", "settings")


def test_every_spa_view_preselects_the_server_default():
    for view in SPA_VIEWS:
        src = _src(f"static/js/recall/views/{view}.js")
        found = re.findall(r"const DEFAULT_MODEL = '([^']+)';", src)
        assert found == [whisper_preload.DEFAULT_PRELOAD_MODEL], view


def test_onboarding_hint_names_the_default_in_both_languages():
    src = _src("static/js/recall/i18n/core.js")
    hints = re.findall(r'"core\.onb\.model\.desc": "([^\n]*)",', src)
    assert len(hints) == 2
    for hint in hints:
        assert f'rc-mono\\">{DEFAULT}<' in hint, hint
