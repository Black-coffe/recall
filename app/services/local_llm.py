"""Local LLM (Ollama) client — the live co-pilot dispatcher (Phase 19, Step 0).

A thin wrapper over the Ollama HTTP API (by default http://localhost:11434). Ollama is a
SEPARATE system service (like telegram_listener, a separate process): we only
talk to it over HTTP, while `ollama serve` is started by the user/OS. The model is pulled
once by hand: ``ollama pull qwen2.5:14b-instruct-q5_K_M``.

Everything is optional and degrades. No Ollama, the service is not running or the model is not
loaded → :func:`is_available` returns False (with a human reason), and the co-pilot
switches off softly — recording / STT / RAG work as before.

NO new dependencies: HTTP through the standard ``urllib``. Anthropic/Telethon
pull in httpx, but we deliberately do not breed dependencies — a simple stdlib JSON POST
is enough and does not touch the fragile chain of torch/transformers/hf_hub pins.

Public API:
- :func:`availability` / :func:`is_available` — ping ``/api/tags`` + whether the model is there.
- :func:`list_models` — names of the loaded models.
- :func:`generate` — one non-streaming ``/api/generate`` call; returns the raw
  Ollama response (``response`` + token/duration counters for the bench).
- :func:`generate_json` — the same, but with forced JSON output (Ollama
  structured outputs: ``format='json'`` or a JSON schema), parsing and retry.
- :func:`warmup` — a dry call so the model settles in VRAM and stays
  (``keep_alive``).

Constants are read from ``os.environ`` directly (like ``embeddings.EMBED_MODEL``) —
so the service stays standalone-testable (a bench script without a Flask context).
The same values are duplicated in ``config.py`` for discoverability/UI.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from app.core.i18n import tr

logger = logging.getLogger(__name__)

# Step 9 (GPU planning): we serialize local LLM calls — so that several requests
# to Ollama do not hit the GPU at once and starve whisper. One queue for all
# generations of the process (single active recording → contention is rare, but we hedge).
_GEN_LOCK = threading.Lock()


# --- Config (mirror of config.py; the source of truth at runtime is env, .env is already loaded) ---
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434").rstrip("/")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct-q5_K_M")
LOCAL_LLM_KEEPALIVE = os.environ.get("LOCAL_LLM_KEEPALIVE", "30m")
LOCAL_LLM_NUM_CTX = int(os.environ.get("LOCAL_LLM_NUM_CTX", "8192"))
LOCAL_LLM_TIMEOUT = float(os.environ.get("LOCAL_LLM_TIMEOUT", "60"))


class LocalLLMError(RuntimeError):
    """Local LLM call error (network / HTTP / invalid JSON)."""


# ============================================================
# HTTP (stdlib urllib)
# ============================================================

def _get(path: str, timeout: float) -> dict:
    req = urllib.request.Request(f"{LOCAL_LLM_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LOCAL_LLM_URL}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ============================================================
# Availability
# ============================================================

_avail_cache: dict = {"ts": 0.0, "ok": False, "reason": "not checked"}
_AVAIL_TTL = 10.0  # short cache — Ollama may start/die during a session

# List of available tags. Cached with the same TTL so that resolve_model() does not
# hit /api/tags on every generation.
_tags_cache: dict = {"ts": 0.0, "models": []}


def _model_present(model: str, available: list[str]) -> bool:
    """Whether the needed model is among the loaded ones. Exact match or a match of the base
    name before ':' (the user may have pulled a slightly different quant tag of the same model)."""
    return _match_model(model, available) is not None


def _match_model(model: str, available: list[str]) -> Optional[str]:
    """Which ACTUALLY available tag to serve the request for ``model`` with.

    Returns an exact match, otherwise the first tag of the same family (before ':'), otherwise None.

    Why not just a bool: family substitution is intended on purpose (a different quant tag
    of the same model is the same model), but as long as only the availability
    check knew about it, it was a trap. On the live machine
    LOCAL_LLM_MODEL='qwen2.5:14b-instruct-q5_K_M' is not installed, while
    'qwen2.5:7b-instruct' and 'qwen2.5:32b-instruct' are — availability() saw
    the family and reported OK, after which generate() went by the exact name and got
    HTTP 404. I.e. the copilot said "the local model is there" and crashed at runtime:
    exactly the degradation that lies instead of honestly switching off."""
    if model in available:
        return model
    base = model.split(":")[0]
    # sorted rather than Ollama order: otherwise with several family tags the choice
    # would silently change between runs together with the /api/tags order.
    for name in sorted(n for n in available if n):
        if name.split(":")[0] == base:
            return name
    return None


def _probe() -> tuple[bool, str]:
    try:
        data = _get("/api/tags", timeout=5.0)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return False, tr("svc_llm.local.unavailable", url=LOCAL_LLM_URL, reason=reason)
    except Exception as e:  # pragma: no cover — protection against surprises
        return False, tr("svc_llm.local.error", error=e)

    models = [m.get("name", "") for m in (data.get("models") or [])]
    _tags_cache.update(ts=time.time(), models=models)
    if not models:
        return False, tr("svc_llm.local.no_models", model=LOCAL_LLM_MODEL)
    effective = _match_model(LOCAL_LLM_MODEL, models)
    if effective:
        if effective != LOCAL_LLM_MODEL:
            # Not just a warning: calls will actually go with this tag, so it
            # must be visible in reason (the copilot UI shows it), otherwise
            # "OK (14b)" would mean not the model that is running.
            logger.warning("[local_llm] tag '%s' not found — taking '%s' from the same "
                           "family (available: %s)",
                           LOCAL_LLM_MODEL, effective, ", ".join(models))
            return True, tr("svc_llm.local.ok_substitute", effective=effective, model=LOCAL_LLM_MODEL)
        return True, tr("svc_llm.local.ok", model=LOCAL_LLM_MODEL)
    return False, tr("svc_llm.local.model_missing", model=LOCAL_LLM_MODEL,
                     available=", ".join(models))


def availability(force: bool = False) -> tuple[bool, str]:
    """(ok, reason) with a short cache. reason is a human reason for the UI/logs."""
    now = time.time()
    if not force and (now - _avail_cache["ts"]) < _AVAIL_TTL:
        return _avail_cache["ok"], _avail_cache["reason"]
    ok, reason = _probe()
    _avail_cache.update(ts=now, ok=ok, reason=reason)
    return ok, reason


def is_available(force: bool = False) -> bool:
    return availability(force=force)[0]


def unavailability_reason() -> str:
    return _avail_cache["reason"]


def list_models() -> list[str]:
    try:
        data = _get("/api/tags", timeout=5.0)
    except Exception:
        return []
    return [m.get("name", "") for m in (data.get("models") or []) if m.get("name")]


def resolve_model(model: Optional[str] = None) -> str:
    """Name of the tag the call will ACTUALLY go with (with family substitution).

    Raises LocalLLMError with an actionable reason if nothing fits — this is
    better than an HTTP 404 from the depths of Ollama, from which it is not visible what exactly is wrong.
    An empty tag cache (Ollama was not polled) is no reason to fail: we return as is,
    let the call itself find out the error."""
    want = model or LOCAL_LLM_MODEL
    if (time.time() - _tags_cache["ts"]) >= _AVAIL_TTL:
        models = list_models()
        if models:
            _tags_cache.update(ts=time.time(), models=models)
    available = _tags_cache["models"]
    if not available:
        return want
    matched = _match_model(want, available)
    if matched is None:
        raise LocalLLMError(tr("svc_llm.local.model_not_in_ollama", model=want,
                               available=", ".join(available)))
    return matched


# ============================================================
# Generation
# ============================================================

def generate(
    prompt: str,
    *,
    system: Optional[str] = None,
    format: Any = None,
    images: Optional[list[str]] = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    num_ctx: Optional[int] = None,
    keep_alive: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict:
    """One non-streaming Ollama ``/api/generate`` call.

    Returns the raw response. Useful fields for the bench/accounting:
    ``response`` (text), ``eval_count`` (output tokens),
    ``eval_duration`` / ``prompt_eval_duration`` / ``total_duration`` /
    ``load_duration`` (nanoseconds), ``prompt_eval_count`` (input tokens).

    ``format`` — ``'json'`` or a JSON schema (dict) for structured outputs.
    ``images`` — a list of base64 strings (WITHOUT the data-URI prefix) for multimodal
    models (llava / qwen2.5vl / …); ``model`` must then be a VL model.
    """
    payload: dict = {
        # Not model or LOCAL_LLM_MODEL: family substitution must work here too, because
        # otherwise availability() reports OK while generation gets a 404.
        "model": resolve_model(model),
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive or LOCAL_LLM_KEEPALIVE,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": num_ctx or LOCAL_LLM_NUM_CTX,
        },
    }
    if system:
        payload["system"] = system
    if format is not None:
        payload["format"] = format
    if images:
        payload["images"] = images

    try:
        with _GEN_LOCK:  # serialization of GPU-heavy calls (Step 9)
            return _post("/api/generate", payload, timeout or LOCAL_LLM_TIMEOUT)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        raise LocalLLMError(f"Ollama HTTP {e.code}: {body[:300]}") from e
    except urllib.error.URLError as e:
        raise LocalLLMError(tr("svc_llm.local.unreachable", reason=getattr(e, 'reason', e))) from e


def generate_json(
    prompt: str,
    *,
    schema: Optional[dict] = None,
    system: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    num_ctx: Optional[int] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    retries: int = 1,
    keep_alive: Optional[str] = None,
) -> dict:
    """A call with forced JSON output.

    ``format`` = the passed ``schema`` (Ollama structured outputs) or ``'json'``.
    Parses ``response`` as JSON; retries ``retries`` times on invalid output.

    Returns ``{'data': <parsed dict>, 'raw': <raw Ollama response>}``, so that the
    caller has both the structure and the token/duration counters.

    Raises :class:`LocalLLMError` if the model never gave valid JSON.
    """
    fmt: Any = schema if schema is not None else "json"
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        resp = generate(
            prompt, system=system, format=fmt, max_tokens=max_tokens,
            temperature=temperature, num_ctx=num_ctx, model=model, timeout=timeout,
            keep_alive=keep_alive,
        )
        text = (resp.get("response") or "").strip()
        try:
            return {"data": json.loads(text), "raw": resp}
        except (ValueError, TypeError) as e:
            last_err = e
            logger.warning("[local_llm] invalid JSON (attempt %d/%d): %r",
                           attempt + 1, retries + 1, text[:200])
    raise LocalLLMError(
        tr("svc_llm.local.bad_json", attempts=retries + 1, error=last_err)
    )


def warmup(timeout: float = 180.0) -> bool:
    """A dry call so the model loads into VRAM and stays there
    (``keep_alive``). The first load of a 14B can take tens of seconds —
    hence the large timeout. Returns True if the warmup succeeded."""
    ok, reason = availability(force=True)
    if not ok:
        logger.info("[local_llm] warmup skipped: %s", reason)
        return False
    try:
        generate("ping", max_tokens=1, timeout=timeout)
        # We log WHAT was warmed up, not what was asked for: with family substitution another tag
        # sits in VRAM, and a line "14b warmed up" would be untrue.
        logger.info("[local_llm] model %s warmed up (keep_alive=%s)",
                    resolve_model(), LOCAL_LLM_KEEPALIVE)
        return True
    except LocalLLMError as e:
        logger.warning("[local_llm] warmup failure: %s", e)
        return False
