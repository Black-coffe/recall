"""Local LLM (Ollama) клієнт — диспетчер живого ко-пілота (Phase 19, Крок 0).

Тонка обгортка над Ollama HTTP API (за замовч. http://localhost:11434). Ollama —
ОКРЕМИЙ системний сервіс (як telegram_listener — окремий процес): ми лише
говоримо з ним по HTTP, а `ollama serve` піднімає користувач/ОС. Модель тягнеться
один раз вручну: ``ollama pull qwen2.5:14b-instruct-q5_K_M``.

Усе опціональне й деградує. Нема Ollama, не запущений сервіс або не завантажена
модель → :func:`is_available` повертає False (з людською причиною), і ко-пілот
м'яко вимикається — запис / STT / RAG працюють як раніше.

БЕЗ нових залежностей: HTTP через стандартний ``urllib``. Anthropic/Telethon
тягнуть httpx, але ми навмисно не плодимо залежність — простий JSON-POST stdlib
вистачає і не чіпає крихку цепочку пінів torch/transformers/hf_hub.

Публічний API:
- :func:`availability` / :func:`is_available` — пінг ``/api/tags`` + чи є модель.
- :func:`list_models` — назви завантажених моделей.
- :func:`generate` — один не-стрімовий виклик ``/api/generate``; повертає сирий
  Ollama-респонс (``response`` + лічильники токенів/тривалостей для бенчу).
- :func:`generate_json` — те саме, але з примусовим JSON-виводом (Ollama
  structured outputs: ``format='json'`` або JSON-схема), парсингом і ретраєм.
- :func:`warmup` — холостий виклик, щоб модель сіла у VRAM і лишилась
  (``keep_alive``).

Константи читаються з ``os.environ`` напряму (як ``embeddings.EMBED_MODEL``) —
щоб сервіс лишався standalone-тестованим (bench-скрипт без Flask-контексту).
Ті самі значення продубльовані у ``config.py`` для discoverability/UI.
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

logger = logging.getLogger(__name__)

# Крок 9 (GPU-планування): серіалізуємо локальні LLM-виклики — щоб кілька запитів
# до Ollama не били по GPU одночасно й не голодували whisper. Одна черга на всі
# генерації процесу (single active recording → конкуренція рідка, але страхуємось).
_GEN_LOCK = threading.Lock()


# --- Конфіг (дзеркало config.py; джерело істини у рантаймі — env, .env вже завантажено) ---
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434").rstrip("/")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct-q5_K_M")
LOCAL_LLM_KEEPALIVE = os.environ.get("LOCAL_LLM_KEEPALIVE", "30m")
LOCAL_LLM_NUM_CTX = int(os.environ.get("LOCAL_LLM_NUM_CTX", "8192"))
LOCAL_LLM_TIMEOUT = float(os.environ.get("LOCAL_LLM_TIMEOUT", "60"))


class LocalLLMError(RuntimeError):
    """Помилка виклику локального LLM (мережа / HTTP / невалідний JSON)."""


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
# Доступність
# ============================================================

_avail_cache: dict = {"ts": 0.0, "ok": False, "reason": "не перевірено"}
_AVAIL_TTL = 10.0  # короткий кеш — Ollama може стартувати/падати під час сесії

# Список наявних тегів. Кешується з тим самим TTL, щоб resolve_model() не
# ходив у /api/tags на кожну генерацію.
_tags_cache: dict = {"ts": 0.0, "models": []}


def _model_present(model: str, available: list[str]) -> bool:
    """Чи є потрібна модель серед завантажених. Точний збіг або збіг базової
    назви до ':' (користувач міг витягти трохи інший квант-тег тієї ж моделі)."""
    return _match_model(model, available) is not None


def _match_model(model: str, available: list[str]) -> Optional[str]:
    """Яким РЕАЛЬНО наявним тегом обслужити запит на ``model``.

    Повертає точний збіг, інакше перший тег тієї ж родини (до ':'), інакше None.

    Чому не просто bool: підстановка родини задумана навмисно (інший квант-тег
    тієї ж моделі — це та сама модель), але доти, доки про неї знала лише
    перевірка доступності, вона була пасткою. На живій машині
    LOCAL_LLM_MODEL='qwen2.5:14b-instruct-q5_K_M' не встановлена, а стоять
    'qwen2.5:7b-instruct' і 'qwen2.5:32b-instruct' — availability() бачила
    родину і рапортувала OK, після чого generate() йшов точним іменем і ловив
    HTTP 404. Тобто копілот повідомляв «локальна модель є» і падав у рантаймі:
    рівно та деградація, яка бреше замість того, щоб чесно вимкнутись."""
    if model in available:
        return model
    base = model.split(":")[0]
    # sorted, а не порядок Ollama: інакше при кількох тегах родини вибір
    # мовчки змінювався б між запусками разом із порядком видачі /api/tags.
    for name in sorted(n for n in available if n):
        if name.split(":")[0] == base:
            return name
    return None


def _probe() -> tuple[bool, str]:
    try:
        data = _get("/api/tags", timeout=5.0)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return False, (f"Ollama недоступний на {LOCAL_LLM_URL} ({reason}). "
                       f"Запустіть `ollama serve` (зазвичай стартує сам після інсталяції).")
    except Exception as e:  # pragma: no cover — захист від несподіванок
        return False, f"Ollama помилка: {e}"

    models = [m.get("name", "") for m in (data.get("models") or [])]
    _tags_cache.update(ts=time.time(), models=models)
    if not models:
        return False, ("Ollama працює, але моделей нема. Виконайте: "
                       f"ollama pull {LOCAL_LLM_MODEL}")
    effective = _match_model(LOCAL_LLM_MODEL, models)
    if effective:
        if effective != LOCAL_LLM_MODEL:
            # Не просто попередження: цим тегом реально підуть виклики, тож він
            # має бути видимий у reason (його показує UI копілота), інакше
            # «OK (14b)» означало б не ту модель, що працює.
            logger.warning("[local_llm] тег '%s' не знайдено — беру '%s' з тієї ж "
                           "родини (доступні: %s)",
                           LOCAL_LLM_MODEL, effective, ", ".join(models))
            return True, f"OK ({effective} — заміна для {LOCAL_LLM_MODEL})"
        return True, f"OK ({LOCAL_LLM_MODEL})"
    return False, (f"Модель '{LOCAL_LLM_MODEL}' не знайдена. Доступні: "
                   f"{', '.join(models)}. Виконайте: ollama pull {LOCAL_LLM_MODEL}")


def availability(force: bool = False) -> tuple[bool, str]:
    """(ok, reason) з коротким кешем. reason — людська причина для UI/логів."""
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
    """Назва тега, яким РЕАЛЬНО піде виклик (з підстановкою родини).

    Викидає LocalLLMError із дієвою причиною, якщо нічого не підходить — це
    краще за HTTP 404 із надр Ollama, по якому не видно, що саме не так.
    Порожній кеш тегів (Ollama не опитували) — не привід падати: віддаємо як є,
    хай помилку зʼясує сам виклик."""
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
        raise LocalLLMError(
            f"Модель '{want}' не знайдена в Ollama. Доступні: "
            f"{', '.join(available)}. Виконайте: ollama pull {want}")
    return matched


# ============================================================
# Генерація
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
    """Один не-стрімовий виклик Ollama ``/api/generate``.

    Повертає сирий респонс. Корисні поля для бенчу/обліку:
    ``response`` (текст), ``eval_count`` (вихідні токени),
    ``eval_duration`` / ``prompt_eval_duration`` / ``total_duration`` /
    ``load_duration`` (наносекунди), ``prompt_eval_count`` (вхідні токени).

    ``format`` — ``'json'`` або JSON-схема (dict) для structured outputs.
    ``images`` — список base64-рядків (БЕЗ data-URI префікса) для мультимодальних
    моделей (llava / qwen2.5vl / …); ``model`` тоді має бути VL-моделлю.
    """
    payload: dict = {
        # Не model or LOCAL_LLM_MODEL: підстановка родини має діяти і тут, бо
        # інакше availability() рапортує OK, а генерація ловить 404.
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
        with _GEN_LOCK:  # серіалізація GPU-важких викликів (Крок 9)
            return _post("/api/generate", payload, timeout or LOCAL_LLM_TIMEOUT)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        raise LocalLLMError(f"Ollama HTTP {e.code}: {body[:300]}") from e
    except urllib.error.URLError as e:
        raise LocalLLMError(f"Ollama недоступний: {getattr(e, 'reason', e)}") from e


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
) -> dict:
    """Виклик з примусовим JSON-виводом.

    ``format`` = передана ``schema`` (Ollama structured outputs) або ``'json'``.
    Парсить ``response`` як JSON; ретрай ``retries`` разів на невалідний вивід.

    Повертає ``{'data': <parsed dict>, 'raw': <сирий Ollama-респонс>}``, щоб
    caller мав і структуру, і лічильники токенів/тривалостей.

    Кидає :class:`LocalLLMError` якщо модель так і не дала валідний JSON.
    """
    fmt: Any = schema if schema is not None else "json"
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        resp = generate(
            prompt, system=system, format=fmt, max_tokens=max_tokens,
            temperature=temperature, num_ctx=num_ctx, model=model, timeout=timeout,
        )
        text = (resp.get("response") or "").strip()
        try:
            return {"data": json.loads(text), "raw": resp}
        except (ValueError, TypeError) as e:
            last_err = e
            logger.warning("[local_llm] невалідний JSON (спроба %d/%d): %r",
                           attempt + 1, retries + 1, text[:200])
    raise LocalLLMError(
        f"Модель не повернула валідний JSON після {retries + 1} спроб: {last_err}"
    )


def warmup(timeout: float = 180.0) -> bool:
    """Холостий виклик, щоб модель завантажилась у VRAM і лишилась там
    (``keep_alive``). Перше завантаження 14B може зайняти десятки секунд —
    звідси великий timeout. Повертає True якщо прогрів вдався."""
    ok, reason = availability(force=True)
    if not ok:
        logger.info("[local_llm] warmup пропущено: %s", reason)
        return False
    try:
        generate("ping", max_tokens=1, timeout=timeout)
        # Логуємо ЩО прогріли, а не що просили: при підстановці родини у VRAM
        # лежить інший тег, і рядок «прогріта 14b» був би неправдою.
        logger.info("[local_llm] модель %s прогріта (keep_alive=%s)",
                    resolve_model(), LOCAL_LLM_KEEPALIVE)
        return True
    except LocalLLMError as e:
        logger.warning("[local_llm] warmup помилка: %s", e)
        return False
