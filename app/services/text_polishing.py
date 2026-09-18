"""Claude API сервис для постпроцессинга транскриптов (Phase 4.4).

Берёт «сырой» текст после Whisper и улучшает: расставляет пунктуацию,
исправляет опечатки, разбивает на абзацы. Сохраняет смысл и язык оригинала.

Architecture (per Anthropic's claude-api skill):
- Default model: app.services.models.get_default_model() (claude-opus-4-8,
  override via CLAUDE_MODEL env) — єдина точка правди, T6.2.
- Adaptive thinking: model сам решает когда и сколько думать.
- Effort: medium — баланс качества и стоимости для редактуры.
- Prompt caching на system prompt — система одинаковая для всех вызовов,
  cache_read стоит ~10% от полной цены input.
- Streaming для длинных транскриптов (избегает SDK HTTP timeout).
- Retry: транзиентні помилки (429/5xx/timeout) повторюються через
  app.services.claude_retry (T6.3); увесь виклик заново, без резюме стріму.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from app.services.claude_retry import call_with_retry
from app.services.models import get_default_model, supports_adaptive_thinking


logger = logging.getLogger(__name__)


# Lazy import — anthropic не критичная зависимость, без ключа модуль не падает.
_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        import anthropic  # lazy
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY не встановлено. Створіть .env у корені проекту "
                "з рядком ANTHROPIC_API_KEY=sk-ant-... та перезапустіть сервер."
            )
        _client = anthropic.Anthropic(api_key=api_key)
        return _client


def is_available() -> bool:
    """Чи налаштований API-ключ. Використовується в /api/health та UI."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _stream_with_retry(client, timeout: float, request_kwargs: dict, what: str = "polish"):
    """messages.stream(**request_kwargs) → get_final_message(), з retry на
    транзиентних помилках (429/5xx/timeout). Увесь виклик повторюється
    заново на кожній спробі (T6.3) — стрім НЕ резюмиться з середини."""
    def _do():
        with client.with_options(timeout=timeout).messages.stream(**request_kwargs) as stream:
            return stream.get_final_message()
    return call_with_retry(_do, what=what)


# System prompt — стабильный, кешируется.
# Phrasing нейтральный для трёх языков (UK/RU/EN), без жёстких "MUST".
_POLISH_SYSTEM_PROMPT = """Ты — редактор автоматических транскриптов аудиозаписей.

Твоя задача — взять «сырой» текст, который выдал speech-to-text движок (Whisper),
и привести его в читабельный вид:

1. Исправить орфографические и грамматические опечатки распознавания.
2. Расставить правильную пунктуацию: запятые, точки, тире, кавычки, вопросительные/восклицательные знаки.
3. Разбить сплошной поток на абзацы по смысловым переходам.
4. Заглавные буквы в начале предложений и в именах собственных.

Важно:
- Сохрани ВСЁ содержание. Не добавляй фактов, которых нет в оригинале. Не сокращай.
- Сохрани язык оригинала (украинский, русский, английский — что бы там ни было).
- Не меняй стиль речи. Если оратор говорит разговорно — оставь разговорным.
- Не вставляй заголовки, списки, markdown — только живой текст с правильной пунктуацией и абзацами.

Верни ТОЛЬКО улучшенный текст. Без преамбулы, без объяснений, без метакомментариев типа
"Вот улучшенный текст:" или "Я исправил...". Просто чистый результат."""


_POLISH_DIARIZED_SYSTEM_PROMPT = """Ты — редактор автоматических транскриптов аудиозаписей с разметкой говорящих.

Вход — диалоговый транскрипт, где каждая реплика начинается с имени говорящего и двоеточия:

    Андрій: текст репліки одного говорящего
    Юля: текст репліки другого говорящего

Задача — улучшить читабельность, СОХРАНИВ структуру говорящих:

1. Внутри каждой реплики:
   - Исправить орфографические и грамматические опечатки распознавания.
   - Расставить правильную пунктуацию: запятые, точки, тире, кавычки, ?, !.
   - Разбить длинные реплики на абзацы по смысловым переходам.
   - Заглавные буквы в начале предложений и в именах собственных.

2. Между репликами:
   - НЕ менять имена говорящих. Имя в каждой строке должно остаться буква-в-букву таким же.
   - НЕ объединять реплики разных говорящих.
   - НЕ разбивать одну реплику на несколько от того же имени.
   - НЕ менять порядок реплик.
   - НЕ добавлять реплик от говорящих, которых нет во входе.
   - Каждая реплика отделяется пустой строкой от следующей.

Важно:
- Сохрани ВСЁ содержание. Не сокращай, не добавляй фактов.
- Сохрани язык оригинала (украинский, русский, английский — что бы там ни было).
- Не меняй стиль речи. Если оратор говорит разговорно — оставь разговорным.
- Не вставляй заголовки, списки, markdown — только живой диалог в формате 'Имя: текст'.

Верни ТОЛЬКО улучшенный транскрипт в том же формате 'Имя: текст\\n\\nИмя: текст'.
Без преамбулы, без объяснений, без метакомментариев."""


def _format_diarized_input(
    segments: list[dict],
    speaker_map: dict[str, str],
) -> str:
    """Build 'Name: text\\n\\nName: text' string from segments + speaker_map.

    Group consecutive same-speaker segments into one paragraph for cleaner input.
    Uses raw_label as fallback if speaker_map doesn't have a name (e.g., 'Спікер 1').
    """
    import re as _re
    if not segments:
        return ""

    def _resolve(raw_label: str) -> str:
        if not raw_label:
            return '?'
        if raw_label in speaker_map and speaker_map[raw_label]:
            return speaker_map[raw_label]
        if raw_label == 'self':
            return 'Ви'
        if raw_label == 'SPEAKER_UNKNOWN':
            return '?'
        m = _re.match(r'^SPEAKER_(\d+)$', raw_label)
        if m:
            return f'Спікер {int(m.group(1)) + 1}'
        return raw_label

    lines: list[str] = []
    current_speaker: Optional[str] = None
    current_text: list[str] = []
    for seg in segments:
        speaker = _resolve(seg.get('speaker') or '')
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        if speaker == current_speaker:
            current_text.append(text)
        else:
            if current_speaker is not None:
                lines.append(f'{current_speaker}: {" ".join(current_text)}')
            current_speaker = speaker
            current_text = [text]
    if current_speaker is not None:
        lines.append(f'{current_speaker}: {" ".join(current_text)}')
    return '\n\n'.join(lines)


def polish_diarized_transcript(
    segments: list[dict],
    speaker_map: dict[str, str],
    model: Optional[str] = None,
    timeout: float = 300.0,
) -> dict:
    """Покращити транскрипт зі збереженням speaker-розмітки (Phase 10.7).

    Args:
        segments: список dict'ів з полями {start, end, text, speaker}.
            speaker — raw_label типу 'SPEAKER_00' / 'self' / 'SPEAKER_UNKNOWN'.
        speaker_map: {raw_label: display_name}. Якщо raw_label немає у мапі —
            використовується fallback ("Ви", "Спікер N", "?").
        model: override ANTHROPIC model id; default — models.get_default_model().
        timeout: HTTP timeout у секундах.

    Returns:
        Тот самий формат що polish_transcript: {polished_text, model,
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}.

    Raises:
        RuntimeError якщо ANTHROPIC_API_KEY відсутній.
        anthropic.APIError при помилці API.
    """
    formatted = _format_diarized_input(segments, speaker_map)
    if not formatted.strip():
        return {"polished_text": "", "model": "", "input_tokens": 0,
                "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}

    client = _get_client()
    model = model or get_default_model()

    user_message = (
        "Покращ цей багатоголосий транскрипт за правилами вище. "
        "Кожен рядок починається з імені говорящого і двокрапки. "
        "Транскрипт обгорнутий у теги <transcript>:\n\n"
        f"<transcript>\n{formatted}\n</transcript>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=64000,
        system=[{
            "type": "text",
            "text": _POLISH_DIARIZED_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )

    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "medium"}

    logger.info(
        "[polish-diar] Starting: model=%s, len(formatted)=%d, speakers=%d",
        model, len(formatted), len({s for s in (seg.get('speaker') for seg in segments) if s}),
    )

    result = _stream_with_retry(client, timeout, request_kwargs)

    polished = "".join(b.text for b in result.content if b.type == "text").strip()

    usage = result.usage
    info = {
        "polished_text": polished,
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    logger.info(
        "[polish-diar] Completed: in=%d, cache_read=%d, out=%d",
        info["input_tokens"], info["cache_read_tokens"], info["output_tokens"],
    )
    return info


def polish_transcript(
    text: str,
    model: Optional[str] = None,
    timeout: float = 300.0,
) -> dict:
    """Улучшить транскрипт через Claude API.

    Returns:
        {
            "polished_text": str,   # улучшенный текст
            "model": str,           # фактически использованная модель
            "input_tokens": int,    # uncached input tokens (full price)
            "output_tokens": int,
            "cache_read_tokens": int,
            "cache_creation_tokens": int,
        }

    Raises:
        RuntimeError при отсутствии ANTHROPIC_API_KEY.
        anthropic.APIError при ошибке API.
    """
    text = (text or "").strip()
    if not text:
        return {"polished_text": text, "model": "", "input_tokens": 0,
                "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}

    client = _get_client()
    model = model or get_default_model()

    user_message = (
        "Улучши следующий транскрипт по правилам выше. "
        "Транскрипт обёрнут в теги <transcript>:\n\n"
        f"<transcript>\n{text}\n</transcript>"
    )

    # Streaming: для длинных транскриптов max_tokens может быть высоким.
    # Используем messages.stream + .get_final_message() — единый таймаут на всё.
    request_kwargs = dict(
        model=model,
        max_tokens=64000,
        system=[{
            "type": "text",
            "text": _POLISH_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )

    # Adaptive thinking + effort: only on Opus 4.6 / 4.7 / Sonnet 4.6.
    # На других моделях параметры либо проигнорируются, либо упадут с 400 — пропускаем.
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "medium"}

    logger.info(f"[polish] Starting polish: model={model}, len(text)={len(text)}")

    result = _stream_with_retry(client, timeout, request_kwargs)

    # Текстовая часть результата
    polished = "".join(b.text for b in result.content if b.type == "text").strip()

    usage = result.usage
    info = {
        "polished_text": polished,
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    logger.info(
        f"[polish] Completed: in={info['input_tokens']}, "
        f"cache_read={info['cache_read_tokens']}, out={info['output_tokens']}"
    )
    return info


# ============================================================
# Phase 12.5: Summary + Action Items
# ============================================================

_SUMMARY_SYSTEM_PROMPT = """Ты — аналитик, который читает транскрипты встреч/обсуждений и выделяет ключевую информацию.

На входе — транскрипт (с разметкой говорящих или без). На выходе — структурированный JSON с тремя секциями:

1. **summary** — краткое резюме разговора (3-7 предложений). Что обсуждали, к чему пришли, общий контекст.
2. **key_points** — список ключевых тезисов / решений / важных утверждений. Каждый пункт — короткая фраза (одно предложение, без воды).
3. **action_items** — список задач / следующих шагов / договорённостей. Каждая запись — объект с полями:
   - "task": краткое описание задачи (одно предложение в инфинитиве: "Подготовить...", "Проверить...", "Связаться с...")
   - "owner": кто отвечает (имя из транскрипта, или null если не назначено)
   - "due": срок (если упоминается явно — например "до пятницы", "к концу месяца"; иначе null)

Важно:
- Не выдумывай информацию. Если action items нет — верни пустой список.
- Сохраняй язык оригинала (украинский/русский/английский) во всех текстовых полях.
- Имена людей бери ровно как в транскрипте (если в виде 'Имя: текст' — используй это имя).
- Не дублируй пункты между секциями — key_points это утверждения/наблюдения, action_items это задачи.

Верни ТОЛЬКО валидный JSON без markdown-обёртки и без преамбулы. Структура:

{
  "summary": "...",
  "key_points": ["...", "..."],
  "action_items": [
    {"task": "...", "owner": "...", "due": "..."},
    {"task": "...", "owner": null, "due": null}
  ]
}"""


def summarize_transcript(
    text: str,
    diarized_text: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 300.0,
) -> dict:
    """Згенерувати summary + key points + action items для транскрипту.

    Args:
        text: сирий транскрипт (fallback якщо немає diarized).
        diarized_text: текст у форматі 'Імʼя: репліка\\n\\nІмʼя: репліка' якщо є.
        model: model override.

    Returns:
        {
            "summary": str,
            "key_points": list[str],
            "action_items": list[{"task": str, "owner": str|None, "due": str|None}],
            "model": str,
            "input_tokens": int, "output_tokens": int,
            "cache_read_tokens": int, "cache_creation_tokens": int,
        }
    """
    import json as _json

    body = (diarized_text or text or "").strip()
    if not body:
        return {
            "summary": "", "key_points": [], "action_items": [],
            "model": "", "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }

    client = _get_client()
    model = model or get_default_model()

    user_message = (
        "Проанализируй следующий транскрипт и верни JSON по схеме выше. "
        "Транскрипт в тегах <transcript>:\n\n"
        f"<transcript>\n{body}\n</transcript>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=16000,
        system=[{
            "type": "text",
            "text": _SUMMARY_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "medium"}

    logger.info(f"[summarize] Starting: model={model}, len={len(body)}")

    result = _stream_with_retry(client, timeout, request_kwargs)

    raw = "".join(b.text for b in result.content if b.type == "text").strip()
    # Захист — model може повернути ```json ... ``` навіть з prompt-protection.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as e:
        logger.error(f"[summarize] JSON parse failed: {e}; raw={raw[:200]!r}")
        raise RuntimeError("Claude повернув некоректний JSON. Спробуйте ще раз.") from e

    usage = result.usage
    return {
        "summary": parsed.get("summary", ""),
        "key_points": parsed.get("key_points", []) or [],
        "action_items": parsed.get("action_items", []) or [],
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Phase 12.11: Translation
# ============================================================

_LANG_NAMES = {
    "uk": "українську",
    "en": "english",
    "ru": "русский",
    "pl": "polski",
    "de": "deutsch",
    "es": "español",
    "fr": "français",
}

_TRANSLATE_SYSTEM_PROMPT = """Ти — перекладач транскриптів аудіозаписів.

Задача — перекласти текст на цільову мову з якомога точнішим збереженням
змісту, інтонації та регістра мови.

Важливо:
- Якщо вхід має формат 'Імʼя: текст\\n\\nІмʼя: текст' (диалог) — збережи
  цю структуру у виводі. Імена власні (Андрій, Юлія, etc.) НЕ перекладай.
- Зберігай порядок реплік. Не зливай і не розбивай їх.
- Природний переклад на цільову мову — не калька. Локалізуй ідіоми.
- Збережи стиль (розмовний/офіційний). Не додавай абзаців яких немає.
- Не додавай преамбул, коментарів, пояснень. Тільки чистий переклад.

Результат — лише перекладений текст у тому ж форматі що й вхід."""


def translate_transcript(
    text: str,
    target_lang: str,
    diarized_text: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 300.0,
) -> dict:
    """Перекласти транскрипт на цільову мову через Claude.

    Args:
        text: сирий текст (fallback).
        target_lang: 'en' / 'uk' / 'ru' / etc.
        diarized_text: 'Імʼя: текст' формат якщо доступний.
        model: model override.

    Returns:
        {
            "translated_text": str, "target_lang": str, "model": str,
            "input_tokens": int, "output_tokens": int,
            "cache_read_tokens": int, "cache_creation_tokens": int,
        }
    """
    body = (diarized_text or text or "").strip()
    if not body:
        return {
            "translated_text": "", "target_lang": target_lang, "model": "",
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }

    lang_label = _LANG_NAMES.get(target_lang.lower(), target_lang)
    client = _get_client()
    model = model or get_default_model()

    user_message = (
        f"Переклади наступний транскрипт на {lang_label}. "
        f"Транскрипт у тегах <transcript>:\n\n"
        f"<transcript>\n{body}\n</transcript>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=64000,
        system=[{
            "type": "text",
            "text": _TRANSLATE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "medium"}

    logger.info(f"[translate] Starting: model={model}, target={target_lang}, len={len(body)}")

    result = _stream_with_retry(client, timeout, request_kwargs)

    translated = "".join(b.text for b in result.content if b.type == "text").strip()
    usage = result.usage
    return {
        "translated_text": translated,
        "target_lang": target_lang,
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Phase 12.19: Topic auto-tags
# ============================================================

_TOPICS_SYSTEM_PROMPT = """Ти — аналітик, що виділяє ключові теми з транскриптів.

Прочитай транскрипт і визнач 3-8 основних тем (topic tags), які він охоплює.
Кожна тема — коротке слово/фраза 1-3 слова українською мовою (або мовою
оригіналу, якщо інша).

Важливо:
- Теги — це загальні поняття/категорії, не конкретні факти. Приклади:
  "стратегія розвитку", "фінансова модель", "наймання", "дедлайни",
  "технічний борг", "маркетинг", "ціноутворення", "кадрові зміни".
- Не дублюй теми (e.g. "продукт" і "продуктовий розвиток" — це одна).
- Слова з великої літери як власні назви, інакше — нижній регістр.
- Якщо транскрипт короткий або без виразної теми — поверни порожній список.

Верни ТОЛЬКО валідний JSON-масив рядків. Без markdown-обгортки, без преамбули.
Приклад:
["стратегія Q2", "наймання", "технічний борг", "клієнтський фідбек"]"""


def extract_topics(
    text: str,
    diarized_text: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 180.0,
) -> dict:
    """Витягнути topic tags для transcript.

    Returns: {"topics": list[str], "model": str, ...token usage}
    """
    import json as _json

    body = (diarized_text or text or "").strip()
    if not body:
        return {
            "topics": [], "model": "",
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }

    client = _get_client()
    model = model or get_default_model()

    # Truncate щоб не передавати весь giant transcript — теми зазвичай видно з
    # перших 8000 символів + якщо коротший — повний. Це economiт tokens.
    sample = body if len(body) <= 8000 else (body[:6000] + "\n\n[…]\n\n" + body[-2000:])

    user_message = (
        "Виділи ключові теми (3-8) з наступного транскрипту:\n\n"
        f"<transcript>\n{sample}\n</transcript>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": _TOPICS_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "low"}  # короткий reply

    logger.info(f"[topics] Starting: model={model}, len={len(sample)}")
    result = _stream_with_retry(client, timeout, request_kwargs)

    raw = "".join(b.text for b in result.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        topics = _json.loads(raw)
        if not isinstance(topics, list):
            topics = []
        topics = [t for t in topics if isinstance(t, str) and t.strip()][:8]
    except _json.JSONDecodeError:
        logger.error(f"[topics] JSON parse failed; raw={raw[:200]!r}")
        topics = []

    usage = result.usage
    return {
        "topics": topics,
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Phase 12.25: Sentiment per speaker
# ============================================================

_SENTIMENT_SYSTEM_PROMPT = """Ти — аналітик настрою (sentiment analyst) транскриптів діалогів.

Вхід — діалоговий транскрипт у форматі 'Ім'я: текст\\n\\nІм'я: текст'.

Завдання — для КОЖНОГО учасника визначити:
1. **score** — числова оцінка тону від -1.0 (дуже негативний) до +1.0 (дуже
   позитивний); 0 = нейтральний.
2. **tone** — короткий ярлик українською: "позитивний", "нейтральний",
   "негативний", "ентузіазм", "критичний", "стурбований", "оптимістичний",
   "сухий", "напружений" тощо. Один-два слова.
3. **summary** — одне речення (макс 120 знаків) пояснення емоційного тонусу
   цього учасника у конкретно цьому діалозі.

Важливо:
- Не оцінюй sentiment загально — тільки в межах цього транскрипту.
- Якщо учасник майже нічого не сказав — score=0.0, tone="мало даних".
- Імена бери з вхідного формату дослівно.

Поверни ТОЛЬКО валідний JSON-обʼєкт, ключ — ім'я учасника:
{
  "Андрій": {"score": 0.4, "tone": "оптимістичний", "summary": "..."},
  "Юлія":   {"score": -0.2, "tone": "стурбований", "summary": "..."}
}"""


def analyze_sentiment(
    diarized_text: str,
    model: Optional[str] = None,
    timeout: float = 180.0,
) -> dict:
    """Sentiment analysis per speaker. Потребує diarized_text у форматі
    'Імʼя: текст\\n\\nІмʼя: текст'.
    """
    import json as _json

    if not diarized_text or not diarized_text.strip():
        return {
            "speakers": {}, "model": "",
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }

    client = _get_client()
    model = model or get_default_model()

    # Truncate щоб не передавати весь giant transcript
    sample = diarized_text if len(diarized_text) <= 12000 else (
        diarized_text[:9000] + "\n\n[…]\n\n" + diarized_text[-3000:])

    user_message = (
        "Проаналізуй sentiment кожного учасника:\n\n"
        f"<transcript>\n{sample}\n</transcript>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": _SENTIMENT_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "low"}

    logger.info(f"[sentiment] Starting: model={model}, len={len(sample)}")
    result = _stream_with_retry(client, timeout, request_kwargs)

    raw = "".join(b.text for b in result.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        speakers = _json.loads(raw)
        if not isinstance(speakers, dict):
            speakers = {}
    except _json.JSONDecodeError:
        logger.error(f"[sentiment] JSON parse failed; raw={raw[:200]!r}")
        speakers = {}

    usage = result.usage
    return {
        "speakers": speakers,
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Phase 13: Meeting card — комбінований екстрактор для авто-збагачення
# ============================================================
#
# ОДИН Claude-виклик повертає summary + key_points + action_items +
# people/projects/orgs + topics. Чому об'єднано, а не reuse
# summarize_transcript + extract_topics + окремий extract_entities:
# при авто-збагаченні КОЖНОГО мітингу домінантний term вартості — це input
# tokens транскрипту (унікальний на кожен виклик, НЕ кешується між мітингами).
# Три окремі виклики = 3× транскрипту = ~3× ціна. Один виклик = 1×.
# System prompt кешується (cache_control ephemeral) — стабільний для всіх.

_MEETING_CARD_SYSTEM_PROMPT = """Ти — аналітик, що читає транскрипт зустрічі/дзвінка і будує структуровану "картку мітингу" для архіву корпоративної памʼяті.

Вхід — транскрипт (з розміткою говорящих 'Імʼя: текст' або суцільний). На виході — ОДИН валідний JSON-обʼєкт з такими полями:

1. "summary" — резюме розмови (3-7 речень): про що говорили, до чого дійшли, контекст.
2. "key_points" — список ключових тез/рішень/важливих тверджень. Кожен — коротка фраза.
3. "action_items" — задачі/наступні кроки/домовленості. Кожна — обʼєкт:
   {"task": "опис в інфінітиві", "owner": "імʼя або null", "due": "термін як його сказали, або null", "due_date": "YYYY-MM-DD або null"}
   - "due" — ДОСЛІВНО як прозвучало («завтра», «до кінця тижня», «Q3»).
   - "due_date" — та сама дата в абсолютному вигляді, порахована від дати зустрічі
     (її дано у повідомленні користувача). Якщо термін неточний («найближчим часом»,
     «до наступної зустрічі») — став null, НЕ вигадуй дату.
4. "people" — люди, що БРАЛИ УЧАСТЬ або ЗГАДУВАЛИСЬ. Кожен — обʼєкт:
   {"name": "канонічне імʼя", "role": "посада/роль або null", "aliases": ["варіанти написання/звертання"]}
5. "projects" — проєкти/продукти/ініціативи. Кожен — обʼєкт:
   {"name": "канонічна назва", "aliases": ["варіанти написання"]}
6. "orgs" — організації/компанії/фонди. Формат як у projects.
7. "topics" — 3-8 загальних тем/категорій розмови (короткі фрази 1-3 слова).

КРИТИЧНО ВАЖЛИВО:
- Не вигадуй. Якщо чогось немає — порожній список. Не додавай фактів, яких немає у транскрипті.
- Зберігай мову оригіналу (українська/російська/англійська) у всіх текстових полях.
- "name" — це КАНОНІЧНА форма (найповніша/найофіційніша). Усі інші варіанти, скорочення, відмінки, транслітерації — в "aliases". Напр. name="Acmecorp", aliases=["Акме","Acmez","Акмекорп"].
- Розрізняй people / projects / orgs за змістом. Людина ≠ компанія ≠ проєкт.
- topics — це КАТЕГОРІЇ (e.g. "фінансовий аналіз", "наймання"), НЕ конкретні факти і НЕ назви проєктів.
- Не дублюй проєкт і як project, і як topic.

Поверни ТІЛЬКИ валідний JSON без markdown-обгортки і без преамбули. Схема:
{
  "summary": "...",
  "key_points": ["...", "..."],
  "action_items": [{"task": "...", "owner": "...", "due": null, "due_date": null}],
  "people": [{"name": "...", "role": null, "aliases": []}],
  "projects": [{"name": "...", "aliases": []}],
  "orgs": [{"name": "...", "aliases": []}],
  "topics": ["...", "..."]
}"""


def _strip_json_fence(raw: str) -> str:
    """Прибрати ```json ... ``` обгортку якщо модель її додала."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


# Стеля довжини входу extract_meeting_card (production-rag-wave-b-03). До цієї
# правки стелі не було взагалі — увесь транскрипт/документ ішов у промпт як є
# (на відміну від extract_topics/analyze_sentiment вище, де truncate вже був).
# Ризик — не JSON-збій (у нього окремий except), а помилка САМОГО API на
# запиті, що перевищує контекстне вікно моделі: `_enrich_card` ловить її
# широким except і лишає запис у "retry_needed" НАЗАВЖДИ (повторний backfill
# знову шле той самий задовгий текст і знову отримує ту саму помилку). Це
# нова експозиція саме для backfill-cards CLI — він бере і документи (Волна
# 16E), а не лише дзвінки, і документи (розпарсені xlsx/pdf) бувають на
# порядки довшими за стенограму дзвінка.
# Голова 3/4 + хвіст 1/4 (не 50/50): відкриття зустрічі/документа задає
# контекст (учасники/тема), а завершення — підсумки/рішення/дедлайни; середина
# найдовших записів губиться, це свідомий компроміс, а не побічний ефект.
_MAX_CARD_CHARS = 200_000


def _truncate_card_body(body: str) -> str:
    """Обрізати текст під стелю extract_meeting_card. `body` вже непорожній
    (перевірка порожнечі — у виклику раніше)."""
    if len(body) <= _MAX_CARD_CHARS:
        return body
    head_len = _MAX_CARD_CHARS * 3 // 4
    tail_len = _MAX_CARD_CHARS - head_len
    logger.warning(
        "[meeting-card] текст задовгий (%d символів > стеля %d) — обрізано "
        "голова+хвіст (%d/%d)", len(body), _MAX_CARD_CHARS, head_len, tail_len,
    )
    return body[:head_len] + "\n\n[…]\n\n" + body[-tail_len:]


def extract_meeting_card(
    text: str,
    diarized_text: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 300.0,
    effort: str = "medium",
    meeting_date: Optional[str] = None,
) -> dict:
    """Комбінований екстрактор картки мітингу (Phase 13) — один Claude-виклик.

    Args:
        text: сирий транскрипт (fallback).
        diarized_text: 'Імʼя: текст\\n\\nІмʼя: текст' формат якщо є (краще).
        model: override ANTHROPIC model id; default — models.get_default_model().
        effort: 'low'|'medium'|'high' — для масового backfill 'low' ×3 швидше/
            дешевше (структурований витяг JSON не потребує глибокого thinking).
        meeting_date: дата зустрічі 'YYYY-MM-DD' — точка відліку для дедлайнів
            (Трек 1). Без неї модель не може порахувати «завтра» в абсолютну
            дату, і `due_date` лишиться null (парсер добере на стороні БД).

    Returns:
        {
            "summary": str,
            "key_points": list[str],
            "action_items": list[{"task","owner","due"}],
            "people": list[{"name","role","aliases"}],
            "projects": list[{"name","aliases"}],
            "orgs": list[{"name","aliases"}],
            "topics": list[str],
            "model": str,
            "input_tokens","output_tokens","cache_read_tokens","cache_creation_tokens": int,
        }

    Raises:
        RuntimeError якщо ANTHROPIC_API_KEY відсутній або Claude повернув не-JSON.
    """
    import json as _json

    body = (diarized_text or text or "").strip()
    empty = {
        "summary": "", "key_points": [], "action_items": [],
        "people": [], "projects": [], "orgs": [], "topics": [],
        "model": "", "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
    }
    if not body:
        return empty
    body = _truncate_card_body(body)

    client = _get_client()
    model = model or get_default_model()

    date_line = (f"Дата цієї зустрічі: {meeting_date}. Усі відносні терміни "
                 f"(«завтра», «до кінця тижня») рахуй від неї.\n\n") if meeting_date else ""
    user_message = (
        "Побудуй картку мітингу (JSON за схемою вище) з наступного транскрипту. "
        f"{date_line}"
        "Транскрипт у тегах <transcript>:\n\n"
        f"<transcript>\n{body}\n</transcript>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=16000,
        system=[{
            "type": "text",
            "text": _MEETING_CARD_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": effort}

    logger.info(f"[meeting-card] Starting: model={model}, effort={effort}, len={len(body)}")

    result = _stream_with_retry(client, timeout, request_kwargs)

    raw = _strip_json_fence("".join(b.text for b in result.content if b.type == "text"))
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as e:
        logger.error(f"[meeting-card] JSON parse failed: {e}; raw={raw[:200]!r}")
        raise RuntimeError("Claude повернув некоректний JSON. Спробуйте ще раз.") from e

    def _list(key):
        v = parsed.get(key)
        return v if isinstance(v, list) else []

    usage = result.usage
    return {
        "summary": parsed.get("summary", "") or "",
        "key_points": _list("key_points"),
        "action_items": _list("action_items"),
        "people": _list("people"),
        "projects": _list("projects"),
        "orgs": _list("orgs"),
        "topics": [t for t in _list("topics") if isinstance(t, str) and t.strip()][:8],
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Волна 3: розбір коментаря власника
# ============================================================
#
# ЧОМУ ОКРЕМИЙ ЕКСТРАКТОР, А НЕ extract_meeting_card. Коментар — це два речення,
# а не транскрипт. Просити в моделі summary, key_points і 3-8 topics з двох
# речень означає платити за переказ того самого тексту і отримати «теми», які
# ловитимуть пів архіву. Тут потрібні рівно дві речі, яких у коментарі справді
# може не бути в самому записі: ЗАДАЧА і НАЗВАНІ ЛЮДИ/ПРОЄКТИ.
#
# Друга відмінність — статус тексту. Транскрипт це стенограма; коментар —
# твердження власника. Тому «власник» тут дефолтний виконавець, коли задача
# сформульована без імені («треба надіслати договір»), і про це сказано в
# промпті прямо: інакше модель ставить owner=null там, де відповідальний
# очевидний із того, ХТО це пише.

_COMMENT_ITEMS_SYSTEM_PROMPT = """Ти розбираєш КОМЕНТАР, який власник архіву написав про свій запис (дзвінок, документ, переписку). Це не стенограма — це його власне уточнення, виправлення чи акцент, написане вже після запису.

Поверни ОДИН валідний JSON-обʼєкт:

1. "action_items" — задачі/домовленості/наступні кроки, які В КОМЕНТАРІ реально поставлені. Кожна:
   {"task": "опис в інфінітиві", "owner": "імʼя або null", "due": "термін як його написано, або null", "due_date": "YYYY-MM-DD або null"}
   - "due" — ДОСЛІВНО як написано («завтра», «до кінця тижня», «у Q3»).
   - "due_date" — та сама дата абсолютно, порахована від дати коментаря (її дано нижче).
     Неточний термін («найближчим часом») → null. НЕ вигадуй дату.
   - "owner" — якщо задача написана без виконавця («треба надіслати договір»,
     «не забути передзвонити»), виконавець — САМ АВТОР коментаря: став "власник".
     Якщо названо іншу людину — став її імʼя.
2. "people" — люди, названі в коментарі: {"name": "канонічне імʼя", "role": "роль або null", "aliases": ["варіанти написання"]}
3. "projects" — проєкти/продукти: {"name": "канонічна назва", "aliases": [...]}
4. "orgs" — організації/компанії: формат як projects.

КРИТИЧНО ВАЖЛИВО:
- НЕ ВИГАДУЙ. Коментар часто взагалі не містить задач («насправді сума 12 тисяч»,
  «Іван більше не в проєкті») — тоді "action_items" це порожній список. Порожній
  список — нормальна і очікувана відповідь, а не невдача.
- Констатація ≠ задача. «Клієнт передумав» — це факт, а не доручення.
  «Треба переписати договір» — задача.
- Зберігай мову оригіналу в усіх текстових полях.
- "name" — найповніша форма; скорочення, відмінки, транслітерації — в "aliases".
- Розрізняй людину, компанію і проєкт за змістом.

Поверни ТІЛЬКИ валідний JSON без markdown-обгортки і без преамбули. Схема:
{"action_items": [], "people": [], "projects": [], "orgs": []}"""


def extract_comment_items(
    text: str,
    comment_date: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 120.0,
    effort: str = "low",
) -> dict:
    """Витягти задачі й названі сутності з коментаря — один Claude-виклик.

    effort='low' за замовчуванням: вхід — два речення, структурований витяг
    із них не потребує глибокого thinking, а розбір запускається кнопкою на
    кожен коментар окремо, тож ціна помітна саме тут.

    Returns {"action_items", "people", "projects", "orgs", "model", *_tokens}.
    Raises RuntimeError без ANTHROPIC_API_KEY або на некоректному JSON.
    """
    import json as _json

    body = (text or "").strip()
    empty = {"action_items": [], "people": [], "projects": [], "orgs": [],
             "model": "", "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0}
    if not body:
        return empty

    client = _get_client()
    model = model or get_default_model()

    date_line = (f"Дата коментаря: {comment_date}. Усі відносні терміни рахуй від неї.\n\n"
                 if comment_date else "")
    user_message = (
        "Розбери наступний коментар за схемою вище. "
        f"{date_line}"
        "Коментар у тегах <comment>:\n\n"
        f"<comment>\n{body}\n</comment>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=4000,
        system=[{
            "type": "text",
            "text": _COMMENT_ITEMS_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": effort}

    logger.info("[comment-items] Starting: model=%s, effort=%s, len=%d",
                model, effort, len(body))

    result = _stream_with_retry(client, timeout, request_kwargs, what="comment-items")

    raw = _strip_json_fence("".join(b.text for b in result.content if b.type == "text"))
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as e:
        logger.error("[comment-items] JSON parse failed: %s; raw=%r", e, raw[:200])
        raise RuntimeError("Claude повернув некоректний JSON. Спробуйте ще раз.") from e

    def _list(key):
        v = parsed.get(key)
        return v if isinstance(v, list) else []

    usage = result.usage
    return {
        "action_items": _list("action_items"),
        "people": _list("people"),
        "projects": _list("projects"),
        "orgs": _list("orgs"),
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Phase 16C: NL-опис аркушів таблиць (Excel/CSV)
# ============================================================
#
# Голий дамп ячейок погано ембедиться семантично. Цей опис («про що ці дані»)
# домішується у текст блоку аркуша → таблиця стає знаходимою через RAG. Один
# Claude-виклик на ВЕСЬ документ (усі аркуші разом), над ЗРАЗКОМ кожного аркуша
# (не повний обсяг) — дешево. Опційно: без ключа цей крок просто пропускається.

_TABLE_DESCRIBE_SYSTEM_PROMPT = """Ти — аналітик даних. Тобі дають кілька аркушів (sheets) з таблиць (Excel/CSV): назву, приблизний розмір і зразок рядків кожного.

Для КОЖНОГО аркуша напиши короткий опис (2-4 речення) природною мовою: що це за дані, які ключові колонки/показники, приблизний обсяг, призначення таблиці та помітні патерни. Опис потрібен, щоб таблицю можна було знайти СЕМАНТИЧНИМ пошуком — тому формулюй змістовно (про що ці дані й навіщо), а не просто перелічуй колонки.

Важливо:
- Не вигадуй значень. Описуй лише те, що видно з заголовків і зразка рядків.
- Мова опису — українська (або мова даних, якщо вони іншою мовою).
- Без markdown-обгортки. Поверни ТІЛЬКИ валідний JSON-масив рядків — по одному опису на аркуш, у ТОМУ Ж порядку, що й вхід (стільки елементів, скільки аркушів).

Приклад:
["Аркуш містить помісячний бюджет за 2026 рік: колонки Категорія та Q1–Q4, ~23 рядки витрат по відділах з підсумковим рядком; найбільша стаття — маркетинг.", "Довідник контактів партнерів: ПІБ, компанія, email, телефон; ~записів."]"""


def describe_sheets(
    sheets: list[dict],
    model: Optional[str] = None,
    timeout: float = 180.0,
) -> dict:
    """NL-опис кожного аркуша таблиці (Phase 16C) — один Claude-виклик.

    Args:
        sheets: [{"name": str, "rows": int|None, "cols": int|None, "preview": str}]
            preview — зразок markdown-таблиці (заголовок + перші рядки).
        model: override; default — models.get_default_model().

    Returns:
        {"descriptions": list[str] (вирівняні з sheets за порядком),
         "model", token usage}. Порожній список якщо аркушів немає.

    Raises:
        RuntimeError якщо ANTHROPIC_API_KEY відсутній (виклик гейтиться caller'ом).
    """
    import json as _json

    empty = {"descriptions": [], "model": "", "input_tokens": 0,
             "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    sheets = [s for s in (sheets or []) if s]
    if not sheets:
        return empty

    client = _get_client()
    model = model or get_default_model()

    parts = []
    for i, s in enumerate(sheets, 1):
        name = s.get("name") or f"Аркуш {i}"
        rows, cols = s.get("rows"), s.get("cols")
        shape = f" (~{rows} рядків × {cols} колонок)" if rows and cols else ""
        parts.append(f"=== Аркуш {i}: «{name}»{shape} ===\n{s.get('preview', '')}")
    user_message = (
        f"Опиши кожен з {len(sheets)} аркушів за правилами вище.\n\n"
        + "\n\n".join(parts)
    )

    request_kwargs = dict(
        model=model,
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": _TABLE_DESCRIBE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "low"}

    logger.info("[describe-sheets] Starting: model=%s, sheets=%d", model, len(sheets))
    result = _stream_with_retry(client, timeout, request_kwargs)

    raw = _strip_json_fence("".join(b.text for b in result.content if b.type == "text"))
    try:
        descs = _json.loads(raw)
        if not isinstance(descs, list):
            descs = []
        descs = [d if isinstance(d, str) else "" for d in descs]
    except _json.JSONDecodeError:
        logger.error("[describe-sheets] JSON parse failed; raw=%r", raw[:200])
        descs = []

    usage = result.usage
    return {
        "descriptions": descs,
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Волна 5.1: зобовʼязання з нитки переписки
# ============================================================
#
# Чому окремий екстрактор, а не extract_meeting_card. Картка мітингу будує
# ЩЕ Й граф сутностей (people/projects/orgs/topics), а для TG це вже зроблено
# інакше — звіркою з наявним графом (Волна 4.5.3, `tg_entities`). Пустити сюди
# картку означало б другий, конкуруючий механізм наповнення графа: рівно ті
# дублі «Акме/Acmecorp/акмекорп», яких волна 4.5.3 навмисно уникала.
# Тут потрібне вузьке: хто що кому пообіцяв і до якого числа.

_CHAT_TASKS_SYSTEM_PROMPT = """Ти читаєш нитку робочої переписки в месенджері і витягуєш з неї зобовʼязання.

Зобовʼязання — це конкретна дія, про яку домовились: людина взяла її на себе, доручила іншій, або сторони узгодили строк її виконання.

НЕ зобовʼязання: обмін думками, новини й пересилання матеріалів, констатація стану справ, наміри без адресата й дії («треба щось робити з маркетингом»), питання без відповіді, ввічливість.

Вхід — повідомлення нитки, кожне з номером, автором і датою. На виході — ОДИН валідний JSON-обʼєкт:

{
  "tasks": [
    {"task": "опис дії в інфінітиві",
     "owner": "імʼя того, ХТО МАЄ ЗРОБИТИ, як воно написане в чаті, або null",
     "due": "строк дослівно як його назвали, або null",
     "due_date": "YYYY-MM-DD або null",
     "msg": <номер повідомлення, в якому це прозвучало>}
  ]
}

Правила:
- Не вигадуй. Якщо в нитці немає жодної домовленості — поверни {"tasks": []}. Порожній список — нормальна і часта відповідь.
- "owner" — виконавець, а не той, хто просить. Якщо людина пише «зроблю» — власник вона сама; якщо «зроби, будь ласка» — власник адресат. Не видно кому — null.
- "due" — дослівна фраза строку з переписки. Немає — null, не підставляй свою.
- "due_date" — та сама дата абсолютно, порахована від дати ТОГО повідомлення, де строк прозвучав (дати дано біля кожного повідомлення). Строк неточний («найближчим часом», «як буде час») — null.
- "msg" — номер того повідомлення, де зобовʼязання прозвучало (не сусіднього).
- Мова опису — мова переписки.
- Одна домовленість — один запис. Не дроби її на кроки і не зливай різні в одну.

Поверни ТІЛЬКИ валідний JSON без markdown-обгортки і без преамбули."""


def extract_chat_tasks(
    messages: list[dict],
    chat_title: Optional[str] = None,
    thread_label: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 180.0,
    effort: str = "low",
) -> dict:
    """Витягти зобовʼязання з нитки переписки (Волна 5.1) — один Claude-виклик.

    Args:
        messages: [{"n": int, "sender": str, "date": "YYYY-MM-DD", "text": str}]
            у хронологічному порядку; ``n`` — номер, на який модель посилається
            в полі ``msg`` (звідти потім береться конкретне повідомлення архіву).
        chat_title / thread_label: контекст нитки для промпта.
        effort: 'low' — витяг структури, глибокий thinking тут не окупається.

    Returns:
        {"tasks": [{"task","owner","due","due_date","msg"}], "model", token usage}.

    Raises:
        RuntimeError якщо ANTHROPIC_API_KEY відсутній або Claude повернув не-JSON.
    """
    import json as _json

    empty = {"tasks": [], "model": "", "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0}
    messages = [m for m in (messages or []) if (m or {}).get("text")]
    if not messages:
        return empty

    client = _get_client()
    model = model or get_default_model()

    lines = []
    for m in messages:
        date = f" · {m['date']}" if m.get("date") else ""
        lines.append(f"[{m['n']}] {m.get('sender') or '?'}{date}: {m['text']}")
    head = "Нитка"
    if thread_label:
        head += f" «{thread_label}»"
    if chat_title:
        head += f" (чат: {chat_title})"

    user_message = (
        f"Витягни зобовʼязання з наступної нитки переписки. {head}.\n\n"
        "<thread>\n" + "\n".join(lines) + "\n</thread>"
    )

    request_kwargs = dict(
        model=model,
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": _CHAT_TASKS_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": effort}

    logger.info("[chat-tasks] Starting: model=%s, messages=%d", model, len(messages))
    result = _stream_with_retry(client, timeout, request_kwargs, what="chat-tasks")

    raw = _strip_json_fence("".join(b.text for b in result.content if b.type == "text"))
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as e:
        logger.error("[chat-tasks] JSON parse failed: %s; raw=%r", e, raw[:200])
        raise RuntimeError("Claude повернув некоректний JSON. Спробуйте ще раз.") from e

    tasks = parsed.get("tasks") if isinstance(parsed, dict) else None
    tasks = [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []

    usage = result.usage
    return {
        "tasks": tasks,
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
