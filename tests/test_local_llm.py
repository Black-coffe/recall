"""Тести резолву моделі Ollama (app/services/local_llm.py).

Приводом став реальний збій: на машині стоїть LOCAL_LLM_MODEL, якої в Ollama
немає, але `availability()` рапортувала OK — підстановка родини діяла лише в
перевірці, а `generate()` йшов точним іменем і ловив HTTP 404. Копілот
повідомляв «локальна модель є» і падав у рантаймі. Мережі тут немає: підміняємо
кеш тегів.
"""
import time

import pytest

from app.services import local_llm


#: Модель, відносно якої написані очікування нижче. Пінимо явно, бо
#: `local_llm.LOCAL_LLM_MODEL` читається з `os.environ` НА ІМПОРТІ, а `.env`
#: потрапляє в оточення тоді, коли його підвантажить перший модуль, що це
#: робить (напр. `mcp_server`). Тобто результат цих тестів залежав від того,
#: який ще файл прогнали раніше в тій самій сесії — і від вмісту `.env` на
#: конкретній машині. Пін прибирає обидві залежності.
_PINNED_MODEL = "qwen2.5:14b-instruct-q5_K_M"


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    monkeypatch.setattr(local_llm, "LOCAL_LLM_MODEL", _PINNED_MODEL)
    local_llm._tags_cache.update(ts=0.0, models=[])
    local_llm._avail_cache.update(ts=0.0, ok=False, reason="не перевірено")
    yield
    local_llm._tags_cache.update(ts=0.0, models=[])
    local_llm._avail_cache.update(ts=0.0, ok=False, reason="не перевірено")


def _tags(models):
    local_llm._tags_cache.update(ts=time.time(), models=models)


# ============================================================
# Резолв
# ============================================================

def test_exact_tag_wins():
    _tags(["qwen2.5:7b-instruct", "qwen2.5:32b-instruct"])
    assert local_llm.resolve_model("qwen2.5:7b-instruct") == "qwen2.5:7b-instruct"


def test_family_substitution_when_exact_tag_missing():
    """Інший квант-тег тієї ж моделі — це та сама модель; підстановка задумана."""
    _tags(["qwen2.5:7b-instruct"])
    assert local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M") == "qwen2.5:7b-instruct"


def test_substitution_is_deterministic():
    """Вибір не має залежати від порядку видачі /api/tags."""
    _tags(["qwen2.5:7b-instruct", "qwen2.5:32b-instruct"])
    first = local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M")
    _tags(["qwen2.5:32b-instruct", "qwen2.5:7b-instruct"])
    assert local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M") == first


def test_foreign_family_is_not_substituted():
    """Родина — це не «будь-яка модель». gemma замість qwen — не заміна."""
    _tags(["gemma2:27b"])
    with pytest.raises(local_llm.LocalLLMError):
        local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M")


def test_missing_model_raises_actionable_error():
    """Замість HTTP 404 із надр Ollama — причина, з якою можна щось зробити."""
    _tags(["gemma2:27b"])
    with pytest.raises(local_llm.LocalLLMError) as exc:
        local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M")
    msg = str(exc.value)
    assert "ollama pull" in msg
    assert "gemma2:27b" in msg, "у причині мають бути наявні моделі"


def test_empty_tag_cache_does_not_block_call():
    """Ollama ще не опитували — не привід падати заздалегідь."""
    local_llm._tags_cache.update(ts=time.time(), models=[])
    assert local_llm.resolve_model("будь-що") == "будь-що"


# ============================================================
# Доступність не має брехати
# ============================================================

def _probe_with(monkeypatch, models):
    monkeypatch.setattr(local_llm, "_get",
                        lambda *a, **k: {"models": [{"name": m} for m in models]})


def test_availability_reports_the_substituted_tag(monkeypatch):
    """Саме цей рядок показує UI копілота. «OK (14b)», коли працює 7b, —
    неправда, через яку баг і не помічали."""
    _probe_with(monkeypatch, ["qwen2.5:7b-instruct"])
    ok, reason = local_llm.availability(force=True)
    assert ok
    assert "qwen2.5:7b-instruct" in reason
    assert "заміна" in reason


def test_availability_ok_without_substitution_stays_quiet(monkeypatch):
    _probe_with(monkeypatch, ["qwen2.5:14b-instruct-q5_K_M"])
    ok, reason = local_llm.availability(force=True)
    assert ok and "заміна" not in reason


def test_availability_false_when_family_absent(monkeypatch):
    _probe_with(monkeypatch, ["gemma2:27b"])
    ok, reason = local_llm.availability(force=True)
    assert not ok
    assert "ollama pull" in reason


def test_availability_and_generate_agree(monkeypatch):
    """Головний інваріант: якщо availability каже OK — generate не має падати
    на резолві моделі. Саме ця пара розʼїхалась у продакшні."""
    _probe_with(monkeypatch, ["qwen2.5:7b-instruct"])
    ok, _ = local_llm.availability(force=True)
    assert ok
    sent = {}
    monkeypatch.setattr(local_llm, "_post",
                        lambda path, payload, timeout: sent.update(payload) or {"response": "x"})
    local_llm.generate("привіт")
    assert sent["model"] == "qwen2.5:7b-instruct"


def test_generate_refuses_clearly_when_nothing_matches(monkeypatch):
    _probe_with(monkeypatch, ["gemma2:27b"])
    local_llm.availability(force=True)
    monkeypatch.setattr(local_llm, "_post",
                        lambda *a, **k: pytest.fail("не мало дійти до HTTP"))
    with pytest.raises(local_llm.LocalLLMError):
        local_llm.generate("привіт")
