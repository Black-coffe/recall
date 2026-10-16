"""Tests of Ollama model resolution (app/services/local_llm.py).

The cause was a real failure: the machine has a LOCAL_LLM_MODEL that Ollama does not
have, yet `availability()` reported OK — family substitution worked only in the
check, while `generate()` went by the exact name and got HTTP 404. The copilot
said "the local model is there" and crashed at runtime. There is no network here: we stub
the tag cache.
"""
import time

import pytest

from app.services import local_llm


#: The model the expectations below are written against. Pinned explicitly, because
#: `local_llm.LOCAL_LLM_MODEL` is read from `os.environ` AT IMPORT, while `.env`
#: gets into the environment when the first module that does so loads it
#: (e.g. `mcp_server`). So the result of these tests depended on which other
#: file had been run earlier in the same session — and on the contents of `.env` on
#: a particular machine. The pin removes both dependencies.
_PINNED_MODEL = "qwen2.5:14b-instruct-q5_K_M"


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    monkeypatch.setattr(local_llm, "LOCAL_LLM_MODEL", _PINNED_MODEL)
    local_llm._tags_cache.update(ts=0.0, models=[])
    local_llm._avail_cache.update(ts=0.0, ok=False, reason="not checked")
    yield
    local_llm._tags_cache.update(ts=0.0, models=[])
    local_llm._avail_cache.update(ts=0.0, ok=False, reason="not checked")


def _tags(models):
    local_llm._tags_cache.update(ts=time.time(), models=models)


# ============================================================
# Resolution
# ============================================================

def test_exact_tag_wins():
    _tags(["qwen2.5:7b-instruct", "qwen2.5:32b-instruct"])
    assert local_llm.resolve_model("qwen2.5:7b-instruct") == "qwen2.5:7b-instruct"


def test_family_substitution_when_exact_tag_missing():
    """A different quant tag of the same model is the same model; the substitution is intended."""
    _tags(["qwen2.5:7b-instruct"])
    assert local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M") == "qwen2.5:7b-instruct"


def test_substitution_is_deterministic():
    """The choice must not depend on the order of the /api/tags output."""
    _tags(["qwen2.5:7b-instruct", "qwen2.5:32b-instruct"])
    first = local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M")
    _tags(["qwen2.5:32b-instruct", "qwen2.5:7b-instruct"])
    assert local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M") == first


def test_foreign_family_is_not_substituted():
    """A family is not "any model". gemma instead of qwen is not a substitute."""
    _tags(["gemma2:27b"])
    with pytest.raises(local_llm.LocalLLMError):
        local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M")


def test_missing_model_raises_actionable_error():
    """Instead of an HTTP 404 from the depths of Ollama — a reason one can act on."""
    _tags(["gemma2:27b"])
    with pytest.raises(local_llm.LocalLLMError) as exc:
        local_llm.resolve_model("qwen2.5:14b-instruct-q5_K_M")
    msg = str(exc.value)
    assert "ollama pull" in msg
    assert "gemma2:27b" in msg, "the reason must list the available models"


def test_empty_tag_cache_does_not_block_call():
    """Ollama has not been polled yet — no reason to fail in advance."""
    local_llm._tags_cache.update(ts=time.time(), models=[])
    assert local_llm.resolve_model("будь-що") == "будь-що"


# ============================================================
# Availability must not lie
# ============================================================

def _probe_with(monkeypatch, models):
    monkeypatch.setattr(local_llm, "_get",
                        lambda *a, **k: {"models": [{"name": m} for m in models]})


def test_availability_reports_the_substituted_tag(monkeypatch):
    """This is the very line the copilot UI shows. "OK (14b)" when 7b is running is
    the untruth because of which the bug went unnoticed."""
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
    """The main invariant: if availability says OK, generate must not fail
    on model resolution. This very pair diverged in production."""
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
                        lambda *a, **k: pytest.fail("must not have reached HTTP"))
    with pytest.raises(local_llm.LocalLLMError):
        local_llm.generate("привіт")


def test_availability_reasons_are_translated(monkeypatch):
    """The reason the UI shows comes from the svc_llm catalog and follows the language."""
    monkeypatch.setenv("RECALL_UI_LANG", "en")

    def _down(*a, **k):
        raise local_llm.urllib.error.URLError("refused")

    monkeypatch.setattr(local_llm, "_get", _down)
    ok, reason = local_llm.availability(force=True)
    assert not ok
    assert reason.startswith("Ollama is unavailable at")
    monkeypatch.setenv("RECALL_UI_LANG", "uk")
    ok, reason = local_llm.availability(force=True)
    assert reason.startswith("Ollama недоступний на")
