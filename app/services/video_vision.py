"""Vision description of video frames (Phase 23B) — "what is shown on screen" → into RAG.

A backend abstraction over two implementations (chosen via ``VIDEO_VISION_BACKEND``):

  * ``local``  — a local VL model via Ollama (by default ``qwen2.5vl:7b``),
                 $0/offline. Reuses :mod:`app.services.local_llm`
                 (the same ``_GEN_LOCK`` that serializes the GPU next to whisper).
  * ``claude`` — the Anthropic vision API (by default ``claude-haiku-4-5`` —
                 the cheapest vision model), paid/more accurate. Reuses
                 ``text_polishing._get_client()`` (lazy, ``ANTHROPIC_API_KEY``).
  * ``off``    — disabled.

Everything is optional and degrades (like OCR in Phase 16/23A): no model / key / Ollama
→ :func:`is_available` = False (with a human reason), :func:`describe_frame`
returns ``''``, and frames + OCR + the player work as before.

NO new dependencies: base64 is stdlib; Ollama — via ``local_llm`` (urllib);
anthropic — lazy import (already an optional dependency of the project).

Constants are read from ``os.environ`` directly (like ``local_llm`` / ``embeddings``),
so the module stays standalone-testable without a Flask context. The same values are
duplicated in ``config.py`` for discoverability/UI.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from app.core.i18n import tr

logger = logging.getLogger(__name__)

# --- Config (mirror of config.py; the runtime source of truth is env) ---
VISION_BACKEND = os.environ.get("VIDEO_VISION_BACKEND", "local").strip().lower()
VISION_MODEL_LOCAL = os.environ.get("VIDEO_VISION_MODEL_LOCAL", "qwen2.5vl:7b")
VISION_MODEL_CLAUDE = os.environ.get("VIDEO_VISION_MODEL_CLAUDE", "claude-haiku-4-5")
VISION_TIMEOUT = float(os.environ.get("VIDEO_VISION_TIMEOUT", "90"))
# How long Ollama keeps the VL model in VRAM after a frame. local_llm's default is 30 min, and another
# ~5 GB next to the copilot left the call without room. 60 s, not 0: frames come in a batch, and
# with 0 the model would reload on each one (measured 14.10.2026: 5.5-7.6 s/frame vs 2.3-2.5).
VISION_KEEPALIVE = os.environ.get("VIDEO_VISION_KEEPALIVE", "60s")

# A short cap on output — a frame description should be 1-3 sentences, not an essay.
_MAX_TOKENS = 220

# UA instruction: describe WHAT is on screen, with no invented text.
_PROMPT = (
    "Ти аналізуєш один кадр із запису екрана. Стисло (1–3 речення) опиши, ЩО "
    "показано на екрані: тип контенту (код / діаграма / таблиця / слайд / "
    "вебсторінка / термінал / відеодзвінок / редактор / робочий стіл тощо) і про "
    "що він — назви програм, заголовки, ключові видимі елементи. НЕ вигадуй "
    "текст, якого не видно. Без преамбул. Відповідай українською."
)


def _backend(override: Optional[str] = None) -> str:
    b = (override or VISION_BACKEND or "local").strip().lower()
    return b if b in ("local", "claude", "off") else "local"


def active_model(backend: Optional[str] = None) -> Optional[str]:
    """Name of the active backend's model (for writing into video_keyframes.vision_model)."""
    b = _backend(backend)
    if b == "claude":
        return VISION_MODEL_CLAUDE
    if b == "local":
        return VISION_MODEL_LOCAL
    return None


# ============================================================
# Availability
# ============================================================

def availability(backend: Optional[str] = None) -> tuple[bool, str]:
    """(ok, reason). reason is a human reason for the UI/logs. Never raises."""
    b = _backend(backend)
    if b == "off":
        return False, tr("svc_pipes.vision.off")

    if b == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, tr("svc_pipes.vision.noKey")
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, tr("svc_pipes.vision.noSdk")
        return True, f"OK (Claude vision: {VISION_MODEL_CLAUDE})"

    # local
    try:
        from app.services import local_llm
    except Exception:
        return False, tr("svc_pipes.vision.localLlmUnavailable")
    ok, reason = local_llm.availability()
    if not ok:
        return False, reason  # Ollama is not running / unavailable
    models = local_llm.list_models()
    base = VISION_MODEL_LOCAL.split(":")[0]
    if VISION_MODEL_LOCAL in models or base in {m.split(":")[0] for m in models}:
        return True, tr("svc_pipes.vision.okLocal", model=VISION_MODEL_LOCAL)
    return False, (tr("svc_pipes.vision.modelMissing",
                   model=VISION_MODEL_LOCAL))


def is_available(backend: Optional[str] = None) -> bool:
    return availability(backend)[0]


# ============================================================
# Frame description
# ============================================================

def _read_image_b64(image_path: str) -> tuple[str, str]:
    """(base64 string without data-URI, media_type) for an image on disk."""
    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("ascii")
    ext = os.path.splitext(image_path)[1].lower()
    media = "image/png" if ext == ".png" else "image/jpeg"
    return b64, media


def _clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _describe_local(image_path: str, *, timeout: Optional[float]) -> str:
    from app.services import local_llm
    b64, _ = _read_image_b64(image_path)
    resp = local_llm.generate(
        _PROMPT,
        images=[b64],
        model=VISION_MODEL_LOCAL,
        max_tokens=_MAX_TOKENS,
        temperature=0.0,
        timeout=timeout or VISION_TIMEOUT,
        keep_alive=VISION_KEEPALIVE,
    )
    return _clean(resp.get("response") or "")


def _describe_claude(image_path: str, *, timeout: Optional[float]) -> str:
    b64, media_type = _read_image_b64(image_path)
    from app.services import text_polishing
    client = text_polishing._get_client()  # lazy; raises if no ANTHROPIC_API_KEY
    # Haiku 4.5 — without thinking/effort/temperature: a minimal vision call.
    msg = client.with_options(timeout=timeout or VISION_TIMEOUT).messages.create(
        model=VISION_MODEL_CLAUDE,
        max_tokens=_MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": b64,
                }},
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    return _clean(" ".join(parts))


def describe_frame(
    image_path: str,
    *,
    backend: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Returns a short description of "what is shown on screen", or ``''`` on any
    error / if the backend is disabled. Best-effort — NEVER raises (one bad
    frame must not break the whole video analysis)."""
    b = _backend(backend)
    if b == "off":
        return ""
    if not image_path or not os.path.isfile(image_path):
        return ""
    try:
        if b == "claude":
            return _describe_claude(image_path, timeout=timeout)
        return _describe_local(image_path, timeout=timeout)
    except Exception:
        logger.debug("[video_vision] describe_frame failed (backend=%s) for %s",
                     b, image_path, exc_info=True)
        return ""
