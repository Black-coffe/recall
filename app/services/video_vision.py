"""Vision-опис кадрів відео (Phase 23B) — «що показано на екрані» → у RAG.

Бекенд-абстракція над двома реалізаціями (вибір через ``VIDEO_VISION_BACKEND``):

  * ``local``  — локальна VL-модель через Ollama (за замовч. ``qwen2.5vl:7b``),
                 $0/офлайн. Перевикористовує :mod:`app.services.local_llm`
                 (той самий ``_GEN_LOCK``, що серіалізує GPU поряд з whisper).
  * ``claude`` — Anthropic vision API (за замовч. ``claude-haiku-4-5`` —
                 найдешевша vision-модель), платно/точніше. Перевикористовує
                 ``text_polishing._get_client()`` (lazy, ``ANTHROPIC_API_KEY``).
  * ``off``    — вимкнено.

Усе опціональне й деградує (як OCR у Phase 16/23A): нема моделі / ключа / Ollama
→ :func:`is_available` = False (з людською причиною), :func:`describe_frame`
повертає ``''``, а кадри + OCR + плеєр працюють як раніше.

БЕЗ нових залежностей: base64 — stdlib; Ollama — через ``local_llm`` (urllib);
anthropic — lazy-import (вже опц. залежність проекту).

Константи читаються з ``os.environ`` напряму (як ``local_llm`` / ``embeddings``),
щоб модуль лишався standalone-тестованим без Flask-контексту. Ті самі значення
продубльовані у ``config.py`` для discoverability/UI.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# --- Конфіг (дзеркало config.py; джерело істини у рантаймі — env) ---
VISION_BACKEND = os.environ.get("VIDEO_VISION_BACKEND", "local").strip().lower()
VISION_MODEL_LOCAL = os.environ.get("VIDEO_VISION_MODEL_LOCAL", "qwen2.5vl:7b")
VISION_MODEL_CLAUDE = os.environ.get("VIDEO_VISION_MODEL_CLAUDE", "claude-haiku-4-5")
VISION_TIMEOUT = float(os.environ.get("VIDEO_VISION_TIMEOUT", "90"))

# Коротка стеля на вихід — опис кадру має бути 1–3 речення, не есе.
_MAX_TOKENS = 220

# UA-інструкція: опиши ЩО на екрані, без вигаданого тексту.
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
    """Назва моделі активного бекенда (для запису у video_keyframes.vision_model)."""
    b = _backend(backend)
    if b == "claude":
        return VISION_MODEL_CLAUDE
    if b == "local":
        return VISION_MODEL_LOCAL
    return None


# ============================================================
# Доступність
# ============================================================

def availability(backend: Optional[str] = None) -> tuple[bool, str]:
    """(ok, reason). reason — людська причина для UI/логів. Ніколи не кидає."""
    b = _backend(backend)
    if b == "off":
        return False, "вимкнено (VIDEO_VISION_BACKEND=off)"

    if b == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "немає ANTHROPIC_API_KEY (Claude vision вимкнено)"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic SDK не встановлено (pip install anthropic)"
        return True, f"OK (Claude vision: {VISION_MODEL_CLAUDE})"

    # local
    try:
        from app.services import local_llm
    except Exception:
        return False, "local_llm недоступний"
    ok, reason = local_llm.availability()
    if not ok:
        return False, reason  # Ollama не запущений / недоступний
    models = local_llm.list_models()
    base = VISION_MODEL_LOCAL.split(":")[0]
    if VISION_MODEL_LOCAL in models or base in {m.split(":")[0] for m in models}:
        return True, f"OK (локальна VL: {VISION_MODEL_LOCAL})"
    return False, (f"VL-модель '{VISION_MODEL_LOCAL}' не знайдена в Ollama. "
                   f"Виконайте: ollama pull {VISION_MODEL_LOCAL}")


def is_available(backend: Optional[str] = None) -> bool:
    return availability(backend)[0]


# ============================================================
# Опис кадру
# ============================================================

def _read_image_b64(image_path: str) -> tuple[str, str]:
    """(base64-рядок без data-URI, media_type) для зображення на диску."""
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
    )
    return _clean(resp.get("response") or "")


def _describe_claude(image_path: str, *, timeout: Optional[float]) -> str:
    b64, media_type = _read_image_b64(image_path)
    from app.services import text_polishing
    client = text_polishing._get_client()  # lazy; raises if no ANTHROPIC_API_KEY
    # Haiku 4.5 — без thinking/effort/temperature: мінімальний vision-виклик.
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
    """Повертає короткий опис «що показано на екрані», або ``''`` за будь-якої
    помилки / якщо бекенд вимкнено. Best-effort — НІКОЛИ не кидає (один поганий
    кадр не має валити весь розбір відео)."""
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
