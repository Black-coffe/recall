"""config-registry-fix-04: whisper-робота (знахідка 8) і коментар про
SECRET_KEY (знахідка 14) в app.py — плюс узгодження `migrations` з контрактом
робота-contract-03 (знахідка 9, історія 03).

Офлайн, без важкого імпорту `app.py` (torch/Flask boot — той самий мотив, що
й у `tests/test_json_logs.py`/`tests/test_auth_gate.py`): критерії 5-7
перевіряються або на реальних, легких модулях (`whisper_manager_new.py`,
`app/state.py`), або статично на джерельному тексті `app.py` — там, де сама
логіка лишається inline-кодом усередині модуля, який імпортувати цілком дорого.

Запуск:
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
    """Мінімальний фейк-бекенд (той самий протокол, що в
    tests/test_whisper_manager.py) — не торкається GPU/файлової системи."""

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
    m._cache_size = 3  # той самий дефолт, що WHISPER_MODEL_CACHE_SIZE у проді
    return m


# ============================================================
# Критерій 5 (знахідка 8): whisper.preload не пришпилюється до 503 назавжди
# ============================================================

class TestWhisperPreloadRaceFix:
    """`app.py::_watch_whisper_preload` звіряється один раз, одразу після
    join() потоку прогріву. Стара логіка (`current_model_name == _m`) хибно
    трактувала успішний прогрів як провал, щойно ХТОСЬ ІНШИЙ паралельно
    завантажував іншу модель (типово — конкурентна транскрипція) — бо
    `current_model_name` це "останній використаний" покажчик, а не ознака
    того, що прогріта модель досі доступна."""

    def test_old_pointer_check_is_fooled_by_concurrent_load(self, manager):
        """Відтворює саму знахідку 8: прогрів моделі A завершується, потім (до
        того, як робота-спостерігач встиг перевірити) паралельна
        транскрипція вантажить модель B — старий критерій `current_model_name
        == 'A'` після цього хибно каже "провал"."""
        manager.load_model("small")   # прогрів (preload) моделі A
        manager.load_model("medium")  # паралельна транскрипція — інша модель

        old_check_ok = manager.current_model_name == "small"
        assert old_check_ok is False, (
            "якщо це не так — сценарій знахідки 8 більше не відтворюється "
            "на цій версії ModernWhisperManager, тест застарів"
        )

    def test_cache_membership_check_survives_concurrent_load(self, manager):
        """Новий критерій (app.py, `_m in whisper_manager._models`) лишається
        істинним у тому самому сценарії — LRU-кеш (дефолт розміром 3) тримає
        обидві моделі одночасно, тож звичайний конкурентний трафік більше не
        топить прогрів."""
        manager.load_model("small")
        manager.load_model("medium")

        new_check_ok = "small" in manager._models
        assert new_check_ok is True

    def test_real_eviction_still_reports_failed(self, manager):
        """Якщо прогріту модель реально витіснено з кешу (не просто змінився
        "останній використаний" покажчик) — новий критерій чесно каже
        "недоступна", а не самолікується/поллить (Non-goal історії)."""
        manager._cache_size = 1
        manager.load_model("small")
        manager.load_model("medium")  # витісняє "small" за розміром кешу 1

        assert "small" not in manager._models

    def test_app_py_uses_cache_membership_not_pointer_equality(self):
        """Статична перевірка, що виправлений критерій дійсно використовується
        (а не лишень тест дублює правильну логіку окремо від коду).

        config-registry-fix-r3-01: сам блок переїхав із app.py у
        `app/services/whisper_preload.py::start_tracked_preload` — перевіряємо
        джерело за новою адресою, а в app.py лишень статично впевнюємось, що
        інлайновий спостерігач звідти зник (нема повторної копії логіки)."""
        preload_source = (PROJECT_ROOT / "app" / "services" / "whisper_preload.py").read_text(
            encoding="utf-8"
        )
        assert "_m in getattr(whisper_manager, '_models', {})" in preload_source
        assert "getattr(whisper_manager, 'current_model_name', None) == _m" not in preload_source
        assert "_watch_whisper_preload" not in APP_PY_SOURCE


# ============================================================
# Критерій 6 (знахідка 14): коментар про SECRET_KEY не бреше
# ============================================================

def test_secret_key_comment_describes_lazy_check():
    """`_finalize_secret_key()` (config.py) виконується ЛІНИВО з першого
    `get_config()` (config-registry-profiles S3) — не одразу при `import
    config`, як досі стверджував коментар в app.py:319-322 (знахідка 14)."""
    idx = APP_PY_SOURCE.index("SECRET_KEY тепер ЄДИНА")
    snippet = APP_PY_SOURCE[idx: idx + 700]
    assert "одразу при" not in snippet, (
        "коментар досі стверджує, що перевірка виконується одразу при import config"
    )
    assert "get_config()" in snippet
    assert "ліни" in snippet.lower()  # "лінива"/"ліниво"


def test_secret_key_check_is_not_module_level_in_config():
    """Крос-перевірка проти config.py (лише читання, не наш файл): виклик
    `_finalize_secret_key()` дійсно лежить під функцією, а не на рівні
    модуля — коментар в app.py описує реальність, не вигадку."""
    config_source = (PROJECT_ROOT / "config.py").read_text(encoding="utf-8")
    # Єдиний виклик `_finalize_secret_key()` (без def) має бути всередині
    # `_initialize_dynamic_config`, яку викликає `get_config()` — не на
    # верхньому рівні модуля.
    call_matches = [
        m.start() for m in re.finditer(r"(?<!def )_finalize_secret_key\(\)", config_source)
    ]
    assert len(call_matches) == 1
    call_idx = call_matches[0]
    def_idx = config_source.index("def _initialize_dynamic_config")
    next_def_idx = config_source.index("\ndef ", def_idx + 1)
    assert def_idx < call_idx < next_def_idx


# ============================================================
# Критерій 7 (знахідка 9, половина цієї історії): `migrations` узгоджений
# з контрактом, який лишила історія 03
# ============================================================

def test_app_py_does_not_assign_migrations_робота():
    assert "робота['migrations']" not in APP_PY_SOURCE


# ============================================================
# Критерій N4 (config-registry-fix-r3-01): RECALL_BIND_ALL читається одним
# парсером (settings.env_bool), а не власним інлайновим у app.py.
# ============================================================

def test_app_py_reads_bind_all_via_env_bool():
    assert "env_bool('RECALL_BIND_ALL')" in APP_PY_SOURCE
    assert "os.environ.get('RECALL_BIND_ALL'" not in APP_PY_SOURCE


def test_state_contract_has_no_migrations_key():
    """Крос-перевірка проти app/state.py (не наш файл, лише читання):
    історія 03 вилучила `migrations` з контракту робота — наша половина
    (не писати туди значення) узгоджена з тим, що там лишилось."""
    from app import state as state_module

    assert "migrations" not in state_module.робота
