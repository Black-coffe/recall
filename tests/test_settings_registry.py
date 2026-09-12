"""Тести реєстру env-змінних `app/core/settings.py` (config-registry-profiles-01).

Покриває акцептанс story 01:
  - повнота реєстру проти реальних `os.environ.get/getenv/[...]`-читань у коді
    (ті самі файли/паттерн, що в `## Map slice` історії);
  - дрейф закоміченого `.env.example` проти згенерованого з реєстру;
  - `profile()` (RECALL_PROFILE → desktop/headless, регістронезалежно, інше
    значення → ValueError);
  - `config.py` без побічних ефектів на `import` (важкі імпорти
    pyaudiowpatch/telethon, SECRET_KEY hard-fail/автоген) — вони переїжджають
    у перший виклик `get_config()`;
  - headless форсує RECORDING_ENABLED/RECORDING_VIDEO_ENABLED у False БЕЗ
    спроби викликати `_detect_recording_enabled` (а отже й без спроби
    імпорту pyaudiowpatch).

Навмисно НЕ імпортує app.py (важкий Whisper-імпорт + `load_dotenv()` реального
кореневого .env, що дописує справжні секрети в os.environ на решту
pytest-сесії — той самий ризик, що описаний у tests/test_auth_gate.py). Тести
цього файлу, що імпортують `config` (яке саме теж кличе `load_dotenv()`),
роблять це свідомо — так само робить кожен інший тест, що торкається
config.py; існуючий ризик, не новий.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.core import settings

REPO_ROOT = Path(__file__).resolve().parents[1]

# Ті самі файли/паттерн, що в `## Map slice` історії config-registry-profiles-01.
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
    # Читання через реєстр (config-registry-fix-r3-05): `<будь-який алiас>.env(`,
    # `.env_bool(`, `.env_int(`, `.env_float(` з літеральним іменем у лапках —
    # напр. `settings.env_bool('RECALL_BIND_ALL')`, `_settings.env('RECALL_LOG_FORMAT')`.
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
        """config-registry-fix-01, знахідка 10: `test_every_scanned_env_name_is_registered`
        зеленіє вхолосту, якщо `_SCAN_TARGETS` перестане резолвитись (found=0)
        або скан деградує далі (131 → 99 сталось непоміченим, бо `_ENV_READ_RE`
        бачив лише `os.environ.get/getenv/[...]`, а не читання через реєстр).

        config-registry-fix-r3-05: регекс розширено на `<будь-який алiас>.env(`,
        `.env_bool(`, `.env_int(`, `.env_float(` — тепер видимі й читання через
        `settings.env*`/`_settings.env*` (app.py, config.py, app/core/auth.py,
        app/core/logger.py, app/services/whisper_preload.py тощо).

        Поріг = 133 — виміряно на коміті цієї історії командою:
            .venv/Scripts/python.exe -c "
            import sys; sys.path.insert(0, 'tests')
            from test_settings_registry import _scan_env_names
            print(len(_scan_env_names()))"
        Склад: 132 імені зі `settings.REGISTRY` (усе, що реєстр знає, скан бачить
        через os.environ-читання і/або .env*-читання) + 1 ім'я з
        `settings.IGNORED_ENV_NAMES` (`WERKZEUG_RUN_MAIN`, читається через
        `os.environ.get` у коді, реєстру не стосується). Будь-яке зникле
        читання (регрес регексу чи повернення `_SCAN_TARGETS`) валить це
        число нижче 133 — тест мусить червоніти."""
        found = _scan_env_names()
        assert len(found) >= 133, (
            f"скан env-змінних повернув підозріло мало назв ({len(found)} < 133) — "
            "перевір, чи _SCAN_TARGETS досі резолвиться у файли і чи _ENV_READ_RE "
            "досі бачить читання через settings.env/env_bool/env_int/env_float "
            "(config-registry-fix-r3-05)"
        )

    def test_every_scanned_env_name_is_registered(self):
        found = _scan_env_names()
        registered = set(settings.by_name().keys())
        missing = found - registered - settings.IGNORED_ENV_NAMES
        assert not missing, (
            "env-змінні знайдені статичним скануванням коду, але відсутні у "
            f"app.core.settings.REGISTRY: {sorted(missing)}"
        )


class TestRegistryGates:
    def test_no_empty_gates(self):
        """config-registry-fix-05, знахідка 3: `gates` — обовʼязкова фраза, що
        описує, що саме флаг вмикає/вимикає/обмежує у коді. Порожній або
        пробільний `gates` — та сама галочка без змісту, яку ця історія знімає."""
        empty = [s.name for s in settings.REGISTRY if not s.gates.strip()]
        assert not empty, (
            f"записи реєстру без непорожнього gates: {empty}"
        )


class TestEnvExampleProfileDependentFormat:
    """config-registry-fix-r3-05: старий формат профіле-залежного запису
    (`# desktop: RECALL_LOCAL_TRUSTED=1`) — синтаксично невалідний dotenv-рядок
    (двокрапка й пробіл перед `=`). Поточний `render_env_example()` пише
    пояснювальний коментар окремим рядком і поруч — валідний `# NAME=value`."""

    # Точна форма старого зламаного профіле-залежного рядка:
    # `# desktop: NAME=value` / `# headless: NAME=value` — двокрапка одразу
    # після ключового слова профілю, перед NAME=. Навмисно вузько, щоб не
    # ловити прозові двокрапки в документаційних коментарях (напр.
    # "На RTX 3090: tiny=4" чи "svidomyj opt-in: RECALL_BIND_ALL=1").
    _BROKEN_ASSIGNMENT_RE = re.compile(r"^#\s*(?:desktop|headless):\s*[A-Z][A-Z_0-9]*=")

    def test_no_colon_before_equals_sign(self):
        """Старий формат писав NAME=value одразу після 'desktop:'/'headless:'
        на тому самому рядку — синтаксично невалідний dotenv-рядок після
        зняття '# '. Новий формат розносить пояснення і валідний рядок
        `# NAME=value` по різних рядках (перевіряється окремим тестом)."""
        generated = settings.render_env_example()
        for line in generated.splitlines():
            assert not self._BROKEN_ASSIGNMENT_RE.match(line), (
                f"рядок має старий невалідний формат '# desktop: NAME=...': {line!r}"
            )

    def test_profile_dependent_entries_have_parseable_commented_line(self):
        generated = settings.render_env_example()
        lines = generated.splitlines()
        profile_dependent = [s for s in settings.REGISTRY if s.default_headless is not None]
        assert profile_dependent, "тест припускає хоча б один default_headless-запис у реєстрі"
        for s in profile_dependent:
            expected = f"# {s.name}={s.default}"
            assert expected in lines, (
                f"немає валідного закоментованого рядка {expected!r} для "
                f"профіле-залежного запису {s.name} — знайдено: "
                f"{[l for l in lines if s.name in l]}"
            )
            uncommented = expected[2:]  # зняти '# '
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
            ".env.example розійшовся з реєстром — перегенеруй командою:\n"
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
    """Свіжий `import config`, ізольований від інших тестів модуля reload'ом,
    щоб перевірка «import не кидає / не пише» не залежала від того, чи
    config вже імпортувався раніше в pytest-сесії."""
    sys.modules.pop("config", None)
    import config as config_module

    yield config_module
    sys.modules.pop("config", None)


class TestConfigNoImportSideEffects:
    def test_import_does_not_touch_dynamic_state(self, fresh_config):
        """`import config` сам собою не кидає (інакше фікстура впала б вище)
        і лишає PROFILE/HEADLESS/RECORDING_ENABLED плейсхолдерами — реальні
        значення виставляються лише при першому get_config()."""
        assert fresh_config.Config.PROFILE == "desktop"
        assert fresh_config.Config.HEADLESS is False
        assert fresh_config.current_config.RECORDING_ENABLED is False
        assert fresh_config._config_initialized is False

    def test_first_get_config_raises_on_network_exposed_insecure_key(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-production")
        monkeypatch.setenv("RECALL_BIND_ALL", "1")
        monkeypatch.delenv("FLASK_DEBUG", raising=False)
        sys.modules.pop("config", None)
        import config as config_module  # не кидає при імпорті

        with pytest.raises(RuntimeError):
            config_module.get_config()
        sys.modules.pop("config", None)

    def test_debug_true_does_not_bypass_network_exposed_guard(self, monkeypatch):
        """config-registry-fix-01, знахідка 2: розширений truthy-набір
        settings.env_bool ('1'/'true'/'yes'/'on') робить FLASK_DEBUG=1 →
        DEBUG=True (раніше '1'.lower()=='true' було False). Guard SECRET_KEY
        раніше стояв за `not current_config.DEBUG` і при DEBUG=True пропускав
        hard-fail НАВІТЬ при мережевому bind — тихий небезпечний старт.
        Мережевий bind мусить hard-fail-ити незалежно від DEBUG."""
        monkeypatch.setenv("FLASK_DEBUG", "1")
        monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-in-production")
        monkeypatch.setenv("RECALL_BIND_ALL", "1")
        sys.modules.pop("config", None)
        import config as config_module

        assert config_module.current_config.DEBUG is True  # підтвердження причини
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
                "_detect_recording_enabled НЕ повинен викликатись у headless "
                "(а отже й pyaudiowpatch НЕ повинен імпортуватись)"
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
