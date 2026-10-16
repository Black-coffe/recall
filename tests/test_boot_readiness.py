"""config-registry-fix-04: whisper робота (finding 8) and the comment about
SECRET_KEY (finding 14) in app.py — plus reconciling `migrations` with the
робота-contract-03 contract (finding 9, story 03).

Offline, without a heavy import of `app.py` (torch/Flask boot — the same motive as
in `tests/test_json_logs.py`/`tests/test_auth_gate.py`): criteria 5-7
are checked either on real, light modules (`whisper_manager_new.py`,
`app/state.py`), or statically on the source text of `app.py` — where the logic
itself stays inline code inside a module that is expensive to import whole.

Run:
    .venv/Scripts/python.exe -m pytest -q tests/test_boot_робота.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from whisper_manager_new import ModernWhisperManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_PY_SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")


class FakeBackend:
    """Minimal fake backend (the same protocol as in
    tests/test_whisper_manager.py) — does not touch the GPU/file system."""

    name = "fake"

    def load(self, model_name, device):
        return f"handle:{model_name}"

    def unload(self, handle):
        pass

    def supports_batched(self):
        return False


@pytest.fixture()
def manager():
    m = ModernWhisperManager(force_cpu=True)
    m._backend = FakeBackend()
    m._cache_size = 3  # the same default as WHISPER_MODEL_CACHE_SIZE in prod
    return m


# ============================================================
# Criterion 5 (finding 8): whisper.preload is not pinned to 503 forever
# ============================================================

class TestWhisperPreloadRaceFix:
    """`app.py::_watch_whisper_preload` checks once, right after
    join() of the warm-up thread. The old logic (`current_model_name == _m`) wrongly
    treated a successful warm-up as a failure as soon as SOMEONE ELSE in parallel
    loaded another model (typically a concurrent transcription) — because
    `current_model_name` is a "last used" pointer, not a sign
    that the warmed-up model is still available."""

    def test_old_pointer_check_is_fooled_by_concurrent_load(self, manager):
        """Reproduces finding 8 itself: the warm-up of model A finishes, then (before
        the робота watcher managed to check) a parallel
        transcription loads model B — the old criterion `current_model_name
        == 'A'` after that wrongly says "failure"."""
        manager.load_model("small")   # warm-up (preload) of model A
        manager.load_model("medium")  # parallel transcription — another model

        old_check_ok = manager.current_model_name == "small"
        assert old_check_ok is False, (
            "if this is not so — the scenario of finding 8 is no longer reproduced "
            "on this version of ModernWhisperManager, the test is outdated"
        )

    def test_cache_membership_check_survives_concurrent_load(self, manager):
        """The new criterion (app.py, `_m in whisper_manager._models`) stays
        true in the same scenario — the LRU cache (default size 3) holds
        both models at once, so ordinary concurrent traffic no longer
        drowns the warm-up."""
        manager.load_model("small")
        manager.load_model("medium")

        new_check_ok = "small" in manager._models
        assert new_check_ok is True

    def test_real_eviction_still_reports_failed(self, manager):
        """If the warmed-up model was really evicted from the cache (not merely the
        "last used" pointer changed) — the new criterion honestly says
        "unavailable", and does not self-heal/poll (a story Non-goal)."""
        manager._cache_size = 1
        manager.load_model("small")
        manager.load_model("medium")  # evicts "small" at cache size 1

        assert "small" not in manager._models

    def test_app_py_uses_cache_membership_not_pointer_equality(self):
        """Static check that the fixed criterion is really used
        (and not just the test duplicating the correct logic apart from the code).

        config-registry-fix-r3-01: the block itself moved from app.py into
        `app/services/whisper_preload.py::start_tracked_preload` — we check
        the source at the new address, and in app.py we only statically make sure that
        the inline watcher is gone from there (no duplicate copy of the logic)."""
        preload_source = (PROJECT_ROOT / "app" / "services" / "whisper_preload.py").read_text(
            encoding="utf-8"
        )
        assert "_m in getattr(whisper_manager, '_models', {})" in preload_source
        assert "getattr(whisper_manager, 'current_model_name', None) == _m" not in preload_source
        assert "_watch_whisper_preload" not in APP_PY_SOURCE


# ============================================================
# Criterion 6 (finding 14): the SECRET_KEY comment does not lie
# ============================================================

def test_secret_key_comment_describes_lazy_check():
    """`_finalize_secret_key()` (config.py) runs LAZILY from the first
    `get_config()` (config-registry-profiles S3) — not immediately on `import
    config`, as the comment in app.py:319-322 used to claim (finding 14)."""
    idx = APP_PY_SOURCE.index("SECRET_KEY check is now SINGLE")
    snippet = APP_PY_SOURCE[idx: idx + 700]
    assert "immediately on" not in snippet, (
        "the comment still claims the check runs immediately on import config"
    )
    assert "get_config()" in snippet
    assert "lazil" in snippet.lower()  # "lazy"/"lazily"


def test_secret_key_check_is_not_module_level_in_config():
    """Cross-check against config.py (read only, not our file): the call to
    `_finalize_secret_key()` really sits under a function, not at
    module level — the comment in app.py describes reality, not fiction."""
    config_source = (PROJECT_ROOT / "config.py").read_text(encoding="utf-8")
    # The only call of `_finalize_secret_key()` (without def) must be inside
    # `_initialize_dynamic_config`, which `get_config()` calls — not at the
    # module top level.
    call_matches = [
        m.start() for m in re.finditer(r"(?<!def )_finalize_secret_key\(\)", config_source)
    ]
    assert len(call_matches) == 1
    call_idx = call_matches[0]
    def_idx = config_source.index("def _initialize_dynamic_config")
    next_def_idx = config_source.index("\ndef ", def_idx + 1)
    assert def_idx < call_idx < next_def_idx


# ============================================================
# Criterion 7 (finding 9, half of this story): `migrations` is reconciled
# with the contract left by story 03
# ============================================================

def test_app_py_does_not_assign_migrations_робота():
    assert "робота['migrations']" not in APP_PY_SOURCE


# ============================================================
# Criterion N4 (config-registry-fix-r3-01): RECALL_BIND_ALL is read by one
# parser (settings.env_bool), not by its own inline one in app.py.
# ============================================================

def test_app_py_reads_bind_all_via_env_bool():
    assert "env_bool('RECALL_BIND_ALL')" in APP_PY_SOURCE
    assert "os.environ.get('RECALL_BIND_ALL'" not in APP_PY_SOURCE


def test_state_contract_has_no_migrations_key():
    """Cross-check against app/state.py (not our file, read only):
    story 03 removed `migrations` from the робота contract — our half
    (not writing a value there) is consistent with what is left there."""
    from app import state as state_module

    assert "migrations" not in state_module.робота
