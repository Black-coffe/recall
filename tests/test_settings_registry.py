"""Tests of the env variable registry `app/core/settings.py` (config-registry-profiles-01).

Covers the acceptance of story 01:
  - registry completeness against the real `os.environ.get/getenv/[...]` reads in the code
    (the same files/pattern as in the story's `## Map slice`);
  - drift of the committed `.env.example` against the one generated from the registry;
  - `profile()` (RECALL_PROFILE → desktop/headless, case-insensitive, any other
    value → ValueError);
  - `config.py` without side effects on `import` (heavy imports of
    pyaudiowpatch/telethon, SECRET_KEY hard-fail/autogen) — they move
    into the first call of `get_config()`;
  - headless forces RECORDING_ENABLED/RECORDING_VIDEO_ENABLED to False WITHOUT
    trying to call `_detect_recording_enabled` (and therefore without trying
    to import pyaudiowpatch).

Deliberately does NOT import app.py (heavy Whisper import + `load_dotenv()` of the real
root .env, which appends real secrets to os.environ for the rest of the
pytest session — the same risk described in tests/test_auth_gate.py). The tests
of this file that import `config` (which itself also calls `load_dotenv()`)
do so consciously — every other test that touches
config.py does the same; an existing risk, not a new one.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.core import settings

REPO_ROOT = Path(__file__).resolve().parents[1]

# The same files/pattern as in the story's `## Map slice` of config-registry-profiles-01.
_SCAN_TARGETS = [
    REPO_ROOT / "app",
    REPO_ROOT / "app.py",
    REPO_ROOT / "config.py",
    REPO_ROOT / "mcp_server.py",
    REPO_ROOT / "telegram_listener.py",
    REPO_ROOT / "telegram_login.py",
    REPO_ROOT / "whisper_manager_new.py",
]

_ENV_READ_RE = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*['\"]([A-Z_0-9]+)['\"]"
    r"|os\.environ\[['\"]([A-Z_0-9]+)['\"]"
    # Reading through the registry (config-registry-fix-r3-05): `<any alias>.env(`,
    # `.env_bool(`, `.env_int(`, `.env_float(` with a literal name in quotes —
    # e.g. `settings.env_bool('RECALL_BIND_ALL')`, `_settings.env('RECALL_LOG_FORMAT')`.
    r"|\benv(?:_bool|_int|_float)?\(\s*['\"]([A-Z_0-9]+)['\"]"
)


def _scan_env_names() -> set[str]:
    names: set[str] = set()
    files: list[Path] = []
    for target in _SCAN_TARGETS:
        if target.is_dir():
            files.extend(target.rglob("*.py"))
        elif target.is_file():
            files.append(target)
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in _ENV_READ_RE.finditer(text):
            names.add(m.group(1) or m.group(2) or m.group(3))
    return names


class TestRegistryCompleteness:
    def test_scan_does_not_go_silently_dark(self):
        """config-registry-fix-01, finding 10: `test_every_scanned_env_name_is_registered`
        goes green vacuously if `_SCAN_TARGETS` stops resolving (found=0)
        or the scan degrades further (131 → 99 happened unnoticed, because `_ENV_READ_RE`
        saw only `os.environ.get/getenv/[...]`, not reads through the registry).

        config-registry-fix-r3-05: the regex is extended to `<any alias>.env(`,
        `.env_bool(`, `.env_int(`, `.env_float(` — now reads through
        `settings.env*`/`_settings.env*` (app.py, config.py, app/core/auth.py,
        app/core/logger.py, app/services/whisper_preload.py, etc.) are visible too.

        Threshold = 133 — measured on this story's commit with the command:
            .venv/Scripts/python.exe -c "
            import sys; sys.path.insert(0, 'tests')
            from test_settings_registry import _scan_env_names
            print(len(_scan_env_names()))"
        Composition: 132 names from `settings.REGISTRY` (everything the registry knows, the scan sees
        through os.environ reads and/or .env* reads) + 1 name from
        `settings.IGNORED_ENV_NAMES` (`WERKZEUG_RUN_MAIN`, read through
        `os.environ.get` in the code, not related to the registry). Any vanished
        read (a regex regression or the return of `_SCAN_TARGETS`) pushes this
        number below 133 — the test must go red."""
        found = _scan_env_names()
        assert len(found) >= 133, (
            f"env variable scan returned suspiciously few names ({len(found)} < 133) — "
            "check whether _SCAN_TARGETS still resolves to files and whether _ENV_READ_RE "
            "still sees reads through settings.env/env_bool/env_int/env_float "
            "(config-registry-fix-r3-05)"
        )

    def test_every_scanned_env_name_is_registered(self):
        found = _scan_env_names()
        registered = set(settings.by_name().keys())
        missing = found - registered - settings.IGNORED_ENV_NAMES
        assert not missing, (
            "env variables found by static code scanning, but missing from "
            f"app.core.settings.REGISTRY: {sorted(missing)}"
        )


class TestParallelRecordingSettings:
    """ADR-026 C8: the limit on simultaneous recordings is in the registry; the whisper semaphore stays 1."""

    def test_recording_max_active_registered(self):
        s = settings.by_name()["RECORDING_MAX_ACTIVE"]
        assert s.kind == "int" and int(s.default) == 2
        assert s.profile == "desktop" and "RECORDING_ENABLED" in s.gates

    def test_env_example_and_config_expose_max_active(self):
        assert "RECORDING_MAX_ACTIVE=2" in (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        import config
        assert config.Config.RECORDING_MAX_ACTIVE == max(1, settings.env_int("RECORDING_MAX_ACTIVE"))

    def test_whisper_max_parallel_default_stays_one(self):
        assert int(settings.by_name()["WHISPER_MAX_PARALLEL"].default) == 1


class TestRegistryGates:
    def test_no_empty_gates(self):
        """config-registry-fix-05, finding 3: `gates` is a mandatory phrase that
        describes what exactly the flag turns on/off/limits in the code. An empty or
        whitespace-only `gates` is the same empty tick-box this story removes."""
        empty = [s.name for s in settings.REGISTRY if not s.gates.strip()]
        assert not empty, (
            f"registry entries without a non-empty gates: {empty}"
        )


class TestEnvExampleProfileDependentFormat:
    """config-registry-fix-r3-05: the old format of a profile-dependent entry
    (`# desktop: RECALL_LOCAL_TRUSTED=1`) is a syntactically invalid dotenv line
    (a colon and a space before `=`). The current `render_env_example()` writes an
    explanatory comment on a separate line and next to it a valid `# NAME=value`."""

    # The exact form of the old broken profile-dependent line:
    # `# desktop: NAME=value` / `# headless: NAME=value` — a colon right
    # after the profile keyword, before NAME=. Deliberately narrow, so as not to
    # catch prose colons in documentation comments (e.g.
    # "On RTX 3090: tiny=4" or "conscious opt-in: RECALL_BIND_ALL=1").
    _BROKEN_ASSIGNMENT_RE = re.compile(r"^#\s*(?:desktop|headless):\s*[A-Z][A-Z_0-9]*=")

    def test_no_colon_before_equals_sign(self):
        """The old format wrote NAME=value right after 'desktop:'/'headless:'
        on the same line — a syntactically invalid dotenv line after
        removing '# '. The new format separates the explanation and the valid line
        `# NAME=value` onto different lines (checked by a separate test)."""
        generated = settings.render_env_example()
        for line in generated.splitlines():
            assert not self._BROKEN_ASSIGNMENT_RE.match(line), (
                f"line has the old invalid format '# desktop: NAME=...': {line!r}"
            )

    def test_profile_dependent_entries_have_parseable_commented_line(self):
        generated = settings.render_env_example()
        lines = generated.splitlines()
        profile_dependent = [s for s in settings.REGISTRY if s.default_headless is not None]
        assert profile_dependent, "the test assumes at least one default_headless entry in the registry"
        for s in profile_dependent:
            expected = f"# {s.name}={s.default}"
            assert expected in lines, (
                f"no valid commented-out line {expected!r} for "
                f"the profile-dependent entry {s.name} — found: "
                f"{[l for l in lines if s.name in l]}"
            )
            uncommented = expected[2:]  # strip '# '
            name, _, value = uncommented.partition("=")
            assert name == s.name
            assert value == s.default


class TestEnvExampleDrift:
    def test_env_example_matches_registry(self):
        generated = settings.render_env_example().replace("\r\n", "\n")
        committed = (REPO_ROOT / ".env.example").read_text(
            encoding="utf-8"
        ).replace("\r\n", "\n")
        assert generated == committed, (
            ".env.example has diverged from the registry — regenerate with the command:\n"
            "  .venv/Scripts/python.exe -m app.core.settings env-example > .env.example"
        )


class TestProfile:
    def test_default_is_desktop(self, monkeypatch):
        monkeypatch.delenv("RECALL_PROFILE", raising=False)
        assert settings.profile() == "desktop"

    def test_headless(self, monkeypatch):
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        assert settings.profile() == "headless"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("RECALL_PROFILE", "HEADLESS")
        assert settings.profile() == "headless"

    def test_invalid_value_raises(self, monkeypatch):
        monkeypatch.setenv("RECALL_PROFILE", "server")
        with pytest.raises(ValueError):
            settings.profile()


@pytest.fixture()
def fresh_config():
    """A fresh `import config`, isolated from other tests of the module by reload,
    so that the check "import does not raise / does not write" does not depend on whether
    config was already imported earlier in the pytest session."""
    sys.modules.pop("config", None)
    import config as config_module

    yield config_module
    sys.modules.pop("config", None)


class TestConfigNoImportSideEffects:
    def test_import_does_not_touch_dynamic_state(self, fresh_config):
        """`import config` by itself does not raise (otherwise the fixture would have failed above)
        and leaves PROFILE/HEADLESS/RECORDING_ENABLED as placeholders — the real
        values are set only on the first get_config()."""
        assert fresh_config.Config.PROFILE == "desktop"
        assert fresh_config.Config.HEADLESS is False
        assert fresh_config.current_config.RECORDING_ENABLED is False
        assert fresh_config._config_initialized is False

    def test_first_get_config_raises_on_network_exposed_insecure_key(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-production")
        monkeypatch.setenv("RECALL_BIND_ALL", "1")
        monkeypatch.delenv("FLASK_DEBUG", raising=False)
        sys.modules.pop("config", None)
        import config as config_module  # does not raise on import

        with pytest.raises(RuntimeError):
            config_module.get_config()
        sys.modules.pop("config", None)

    def test_debug_true_does_not_bypass_network_exposed_guard(self, monkeypatch):
        """config-registry-fix-01, finding 2: the extended truthy set of
        settings.env_bool ('1'/'true'/'yes'/'on') makes FLASK_DEBUG=1 →
        DEBUG=True (earlier '1'.lower()=='true' gave False). The SECRET_KEY guard
        used to stand behind `not current_config.DEBUG` and with DEBUG=True skipped
        the hard-fail EVEN with a network bind — a quiet insecure start.
        A network bind must hard-fail regardless of DEBUG."""
        monkeypatch.setenv("FLASK_DEBUG", "1")
        monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-production")
        monkeypatch.setenv("RECALL_BIND_ALL", "1")
        sys.modules.pop("config", None)
        import config as config_module

        assert config_module.current_config.DEBUG is True  # confirms the cause
        with pytest.raises(RuntimeError):
            config_module.get_config()
        sys.modules.pop("config", None)

    def test_headless_forces_recording_off_without_calling_detect(self, monkeypatch):
        monkeypatch.setenv("RECALL_PROFILE", "headless")
        monkeypatch.setenv("SECRET_KEY", "a-real-non-default-secret-key-value-abc123")
        sys.modules.pop("config", None)
        import config as config_module

        def _boom():
            raise AssertionError(
                "_detect_recording_enabled must NOT be called in headless "
                "(and therefore pyaudiowpatch must NOT be imported)"
            )

        monkeypatch.setattr(
            config_module.Config, "_detect_recording_enabled", staticmethod(_boom)
        )
        cfg = config_module.get_config()

        assert cfg.PROFILE == "headless"
        assert cfg.HEADLESS is True
        assert cfg.RECORDING_ENABLED is False
        assert cfg.RECORDING_VIDEO_ENABLED is False
        sys.modules.pop("config", None)
