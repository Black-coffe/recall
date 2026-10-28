"""Claude API service for post-processing transcripts (Phase 4.4).

Takes the "raw" text after Whisper and improves it: adds punctuation,
fixes typos, splits into paragraphs. Preserves the meaning and language of the original.

Architecture (per Anthropic's claude-api skill):
- Default model: app.services.models.get_default_model() (claude-opus-4-8,
  override via CLAUDE_MODEL env) — the single source of truth, T6.2.
- Adaptive thinking: the model decides itself when and how much to think.
- Effort: medium — a balance of quality and cost for editing.
- Prompt caching on the system prompt — the system is the same for all calls,
  cache_read costs ~10% of the full input price.
- Streaming for long transcripts (avoids the SDK HTTP timeout).
- Retry: transient errors (429/5xx/timeout) are repeated via
  app.services.claude_retry (T6.3); the whole call anew, without resuming the stream.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Sequence

from app.core.i18n import tr
from app.services.claude_retry import call_with_retry
from app.services.models import get_default_model, supports_adaptive_thinking


logger = logging.getLogger(__name__)


# Lazy import — anthropic is not a critical dependency, without a key the module does not fail.
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
            raise RuntimeError(tr("svc_llm.polish.no_key"))
        _client = anthropic.Anthropic(api_key=api_key)
        return _client


def is_available() -> bool:
    """Whether the API key is configured. Used in /api/health and the UI."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _stream_with_retry(client, timeout: float, request_kwargs: dict, what: str = "polish"):
    """messages.stream(**request_kwargs) → get_final_message(), with retry on
    transient errors (429/5xx/timeout). The whole call is repeated
    anew on every attempt (T6.3) — the stream is NOT resumed from the middle."""
    def _do():
        with client.with_options(timeout=timeout).messages.stream(**request_kwargs) as stream:
            return stream.get_final_message()
    return call_with_retry(_do, what=what)


# System prompt — stable, cached.
# Phrasing neutral for three languages (UK/RU/EN), without rigid "MUST".
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
    """Improve the transcript while keeping the speaker markup (Phase 10.7).

    Args:
        segments: list of dicts with the fields {start, end, text, speaker}.
            speaker — a raw_label like 'SPEAKER_00' / 'self' / 'SPEAKER_UNKNOWN'.
        speaker_map: {raw_label: display_name}. If raw_label is not in the map —
            the fallback is used ("Ви", "Спікер N", "?").
        model: override ANTHROPIC model id; default — models.get_default_model().
        timeout: HTTP timeout in seconds.

    Returns:
        The same format as polish_transcript: {polished_text, model,
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}.

    Raises:
        RuntimeError if ANTHROPIC_API_KEY is missing.
        anthropic.APIError on an API error.
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
    """Improve the transcript via the Claude API.

    Returns:
        {
            "polished_text": str,   # the improved text
            "model": str,           # the model actually used
            "input_tokens": int,    # uncached input tokens (full price)
            "output_tokens": int,
            "cache_read_tokens": int,
            "cache_creation_tokens": int,
        }

    Raises:
        RuntimeError when ANTHROPIC_API_KEY is absent.
        anthropic.APIError on an API error.
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

    # Streaming: for long transcripts max_tokens can be high.
    # We use messages.stream + .get_final_message() — a single timeout for everything.
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
    # On other models the parameters are either ignored or fail with a 400 — skipping.
    if supports_adaptive_thinking(model):
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "medium"}

    logger.info(f"[polish] Starting polish: model={model}, len(text)={len(text)}")

    result = _stream_with_retry(client, timeout, request_kwargs)

    # Text part of the result
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


def _owner_line(owner_names, lang: str = "uk") -> str:
    """A user-message line naming the archive owner, so the model can set `for_whom`.

    It goes into the user message, not the cached system prompt (the names are dynamic).
    Empty when no names are given: the model then relies on the first person."""
    names = [n.strip() for n in (owner_names or ()) if isinstance(n, str) and n.strip()]
    if not names:
        return ""
    if lang == "ru":
        return 'Владелец архива (для поля "for_whom"): ' + ", ".join(names) + ".\n\n"
    return 'Власник архіву (для поля "for_whom"): ' + ", ".join(names) + ".\n\n"


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
   - "for_whom": для кого задача относительно ВЛАДЕЛЬЦА архива (его имена, если известны, даны в сообщении пользователя):
     "owner" — делает сам владелец; "promised_to_owner" — кто-то обещал это владельцу или сделать для него;
     "third_party" — договорённость между другими людьми, владелец не исполнитель и не получатель
   - "confidence": число от 0 до 1 — насколько ты уверен, что это реальная договорённость, а не мысль вслух
   - "importance": 1 | 2 | 3 — 3 важно/срочно, 2 обычная задача, 1 мелочь

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
    {"task": "...", "owner": "...", "due": "...", "for_whom": "owner", "confidence": 0.9, "importance": 2},
    {"task": "...", "owner": null, "due": null, "for_whom": "third_party", "confidence": 0.6, "importance": 1}
  ]
}"""


def summarize_transcript(
    text: str,
    diarized_text: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 300.0,
    owner_names: Sequence[str] = (),
) -> dict:
    """Generate summary + key points + action items for a transcript.

    Args:
        text: raw transcript (fallback if there is no diarized).
        diarized_text: text in the format 'Імʼя: репліка\\n\\nІмʼя: репліка' if available.
        model: model override.
        owner_names: the archive owner's names (`task_proposals.owner_names`) for `for_whom`.

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
        f"{_owner_line(owner_names, 'ru')}"
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
    # Protection — the model may return ```json ... ``` even with prompt-protection.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as e:
        logger.error(f"[summarize] JSON parse failed: {e}; raw={raw[:200]!r}")
        raise RuntimeError(tr("svc_llm.polish.bad_json")) from e

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
    """Translate the transcript into the target language via Claude.

    Args:
        text: raw text (fallback).
        target_lang: 'en' / 'uk' / 'ru' / etc.
        diarized_text: 'Імʼя: текст' format if available.
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
    """Extract topic tags for a transcript.

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

    # Truncate so as not to pass the whole giant transcript — topics are usually visible from
    # the first 8000 characters + if shorter — the full one. This saves tokens.
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
        request_kwargs["output_config"] = {"effort": "low"}  # short reply

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
    """Sentiment analysis per speaker. Requires diarized_text in the format
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

    # Truncate so as not to pass the whole giant transcript
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
# Phase 13: Meeting card — a combined extractor for auto-enrichment
# ============================================================
#
# ONE Claude call returns summary + key_points + action_items +
# people/projects/orgs + topics. Why combined, rather than reusing
# summarize_transcript + extract_topics + a separate extract_entities:
# when auto-enriching EVERY meeting the dominant cost term is the input
# tokens of the transcript (unique per call, NOT cached between meetings).
# Three separate calls = 3× the transcript = ~3× the price. One call = 1×.
# The system prompt is cached (cache_control ephemeral) — stable for all.

_MEETING_CARD_SYSTEM_PROMPT = """Ти — аналітик, що читає транскрипт зустрічі/дзвінка і будує структуровану "картку мітингу" для архіву корпоративної памʼяті.

Вхід — транскрипт (з розміткою говорящих 'Імʼя: текст' або суцільний). На виході — ОДИН валідний JSON-обʼєкт з такими полями:

1. "summary" — резюме розмови (3-7 речень): про що говорили, до чого дійшли, контекст.
2. "key_points" — список ключових тез/рішень/важливих тверджень. Кожен — коротка фраза.
3. "action_items" — задачі/наступні кроки/домовленості. Кожна — обʼєкт:
   {"task": "опис в інфінітиві", "owner": "імʼя або null", "due": "термін як його сказали, або null", "due_date": "YYYY-MM-DD або null", "for_whom": "owner"|"promised_to_owner"|"third_party", "confidence": 0..1, "importance": 1|2|3}
   - "due" — ДОСЛІВНО як прозвучало («завтра», «до кінця тижня», «Q3»).
   - "due_date" — та сама дата в абсолютному вигляді, порахована від дати зустрічі
     (її дано у повідомленні користувача). Якщо термін неточний («найближчим часом»,
     «до наступної зустрічі») — став null, НЕ вигадуй дату.
   - "for_whom" — для кого задача відносно ВЛАСНИКА архіву (його імена, якщо відомі, дано в повідомленні
     користувача): "owner" — робить сам власник; "promised_to_owner" — хтось пообіцяв це власнику або зробити
     для нього; "third_party" — домовленість між іншими людьми, власник не виконавець і не отримувач.
   - "confidence" — число від 0 до 1: наскільки ти впевнений, що це реальна домовленість, а не думка вголос.
   - "importance" — 1 | 2 | 3: 3 важливо/терміново, 2 звичайна задача, 1 дрібниця.
4. "people" — люди, що БРАЛИ УЧАСТЬ або ЗГАДУВАЛИСЬ. Кожен — обʼєкт:
   {"name": "канонічне імʼя", "role": "посада/роль або null", "aliases": ["варіанти написання/звертання"]}
5. "projects" — проєкти/продукти/ініціативи. Кожен — обʼєкт:
   {"name": "канонічна назва", "aliases": ["варіанти написання"]}
6. "orgs" — організації/компанії/фонди. Формат як у projects.
7. "topics" — 3-8 загальних тем/категорій розмови (короткі фрази 1-3 слова).
8. "tags" — до 5 коротких тегів (1-3 слова) для обʼєднання записів поперек напрямків і
   проєктів (напр. "AI"). Словник наявних тегів, якщо є, дано в повідомленні користувача —
   бери назви звідти, якщо підходять; нову назву додавай лише якщо жодна наявна не пасує.
   Якщо словник порожній або не дано — запропонуй власні короткі назви.

КРИТИЧНО ВАЖЛИВО:
- Не вигадуй. Якщо чогось немає — порожній список. Не додавай фактів, яких немає у транскрипті.
- Зберігай мову оригіналу (українська/російська/англійська) у всіх текстових полях.
- "name" — це КАНОНІЧНА форма (найповніша/найофіційніша). Усі інші варіанти, скорочення, відмінки, транслітерації — в "aliases". Напр. name="Polaris", aliases=["Поларіс","Polarys","Поляріс"].
- Розрізняй people / projects / orgs за змістом. Людина ≠ компанія ≠ проєкт.
- topics — це КАТЕГОРІЇ (e.g. "фінансовий аналіз", "наймання"), НЕ конкретні факти і НЕ назви проєктів.
- Не дублюй проєкт і як project, і як topic.

Поверни ТІЛЬКИ валідний JSON без markdown-обгортки і без преамбули. Схема:
{
  "summary": "...",
  "key_points": ["...", "..."],
  "action_items": [{"task": "...", "owner": "...", "due": null, "due_date": null, "for_whom": "owner", "confidence": 0.9, "importance": 2}],
  "people": [{"name": "...", "role": null, "aliases": []}],
  "projects": [{"name": "...", "aliases": []}],
  "orgs": [{"name": "...", "aliases": []}],
  "topics": ["...", "..."],
  "tags": ["...", "..."]
}"""


def _strip_json_fence(raw: str) -> str:
    """Strip the ```json ... ``` wrapper if the model added it."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


# Input length ceiling of extract_meeting_card (production-rag-wave-b-03). Before this
# fix there was no ceiling at all — the whole transcript/document went into the prompt as is
# (unlike extract_topics/analyze_sentiment above, where truncate already existed).
# The risk is not a JSON failure (it has a separate except), but an error of the API ITSELF on
# a request that exceeds the model's context window: `_enrich_card` catches it with
# a broad except and leaves the record in "retry_needed" FOREVER (a repeated backfill
# sends the same overlong text again and gets the same error again). This is
# a new exposure specifically for the backfill-cards CLI — it takes documents too (Wave
# 16E), not only calls, and documents (parsed xlsx/pdf) can be
# orders of magnitude longer than a call transcript.
# Head 3/4 + tail 1/4 (not 50/50): the opening of a meeting/document sets the
# context (participants/topic), while the ending holds the outcomes/decisions/deadlines; the middle
# of the longest records is lost, this is a deliberate compromise, not a side effect.
_MAX_CARD_CHARS = 200_000


def _truncate_card_body(body: str) -> str:
    """Truncate the text to the extract_meeting_card ceiling. `body` is already non-empty
    (the emptiness check is earlier, in the caller)."""
    if len(body) <= _MAX_CARD_CHARS:
        return body
    head_len = _MAX_CARD_CHARS * 3 // 4
    tail_len = _MAX_CARD_CHARS - head_len
    logger.warning(
        "[meeting-card] text too long (%d chars > ceiling %d) — truncated "
        "head+tail (%d/%d)", len(body), _MAX_CARD_CHARS, head_len, tail_len,
    )
    return body[:head_len] + "\n\n[…]\n\n" + body[-tail_len:]


def extract_meeting_card(
    text: str,
    diarized_text: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 300.0,
    effort: str = "medium",
    meeting_date: Optional[str] = None,
    tag_vocabulary: Optional[list[str]] = None,
    owner_names: Sequence[str] = (),
) -> dict:
    """Combined meeting card extractor (Phase 13) — one Claude call.

    Args:
        text: raw transcript (fallback).
        diarized_text: 'Імʼя: текст\\n\\nІмʼя: текст' format if available (better).
        model: override ANTHROPIC model id; default — models.get_default_model().
        effort: 'low'|'medium'|'high' — for a mass backfill 'low' is ×3 faster/
            cheaper (structured JSON extraction does not need deep thinking).
        meeting_date: meeting date 'YYYY-MM-DD' — the reference point for deadlines
            (Track 1). Without it the model cannot compute "завтра" into an absolute
            date, and `due_date` stays null (the parser finishes it on the DB side).
        tag_vocabulary: existing tag names (`categories.list_tags`, only
            "name") — the model takes tags from here and proposes a new name only if
            none fits (story 06). The dynamic vocabulary goes into the
            user message, not the system prompt, so as not to break the
            prompt caching of the static system prompt.
        owner_names: the archive owner's names (`task_proposals.owner_names`) so the
            model can tell the owner's tasks from third-party ones (`for_whom`); also
            in the user message, for the same caching reason.

    Returns:
        {
            "summary": str,
            "key_points": list[str],
            "action_items": list[{"task","owner","due"}],
            "people": list[{"name","role","aliases"}],
            "projects": list[{"name","aliases"}],
            "orgs": list[{"name","aliases"}],
            "topics": list[str],
            "tags": list[str],
            "model": str,
            "input_tokens","output_tokens","cache_read_tokens","cache_creation_tokens": int,
        }

    Raises:
        RuntimeError if ANTHROPIC_API_KEY is missing or Claude returned non-JSON.
    """
    import json as _json

    body = (diarized_text or text or "").strip()
    empty = {
        "summary": "", "key_points": [], "action_items": [],
        "people": [], "projects": [], "orgs": [], "topics": [], "tags": [],
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
    vocab_line = (
        "Наявний словник тегів (до 5 у полі \"tags\" — обирай звідси, якщо підходить; "
        "нову назву додавай лише якщо жодна не пасує): "
        + ", ".join(tag_vocabulary) + "\n\n"
    ) if tag_vocabulary else ""
    user_message = (
        "Побудуй картку мітингу (JSON за схемою вище) з наступного транскрипту. "
        f"{date_line}{vocab_line}{_owner_line(owner_names)}"
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
        raise RuntimeError(tr("svc_llm.polish.bad_json")) from e

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
        "tags": [t for t in _list("tags") if isinstance(t, str) and t.strip()],
        "model": result.model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================
# Wave 3: parsing the owner's comment
# ============================================================
#
# WHY A SEPARATE EXTRACTOR, NOT extract_meeting_card. A comment is two sentences,
# not a transcript. Asking the model for a summary, key_points and 3-8 topics out of two
# sentences means paying for a retelling of the same text and getting "topics" that
# would catch half the archive. Exactly two things are needed here, which a comment really
# may not have in the record itself: a TASK and NAMED PEOPLE/PROJECTS.
#
# The second difference is the status of the text. A transcript is a verbatim record; a comment is
# the owner's statement. So the "owner" here is the default assignee when a task
# is phrased without a name ("треба надіслати договір"), and the prompt
# says so directly: otherwise the model puts owner=null where the responsible
# person is obvious from WHO is writing.

_COMMENT_ITEMS_SYSTEM_PROMPT = """Ти розбираєш КОМЕНТАР, який власник архіву написав про свій запис (дзвінок, документ, переписку). Це не стенограма — це його власне уточнення, виправлення чи акцент, написане вже після запису.

Поверни ОДИН валідний JSON-обʼєкт:

1. "action_items" — задачі/домовленості/наступні кроки, які В КОМЕНТАРІ реально поставлені. Кожна:
   {"task": "опис в інфінітиві", "owner": "імʼя або null", "due": "термін як його написано, або null", "due_date": "YYYY-MM-DD або null", "for_whom": "owner"|"promised_to_owner"|"third_party", "confidence": 0..1, "importance": 1|2|3}
   - "due" — ДОСЛІВНО як написано («завтра», «до кінця тижня», «у Q3»).
   - "due_date" — та сама дата абсолютно, порахована від дати коментаря (її дано нижче).
     Неточний термін («найближчим часом») → null. НЕ вигадуй дату.
   - "owner" — якщо задача написана без виконавця («треба надіслати договір»,
     «не забути передзвонити»), виконавець — САМ АВТОР коментаря: став "власник".
     Якщо названо іншу людину — став її імʼя.
   - "for_whom" — для кого задача відносно ВЛАСНИКА архіву, тобто автора коментаря (його імена, якщо
     відомі, дано в повідомленні користувача): "owner" — робить сам власник; "promised_to_owner" — хтось пообіцяв це власнику або зробити
     для нього; "third_party" — домовленість між іншими людьми, власник не виконавець і не отримувач.
   - "confidence" — число від 0 до 1: наскільки ти впевнений, що це реальна домовленість, а не думка вголос.
   - "importance" — 1 | 2 | 3: 3 важливо/терміново, 2 звичайна задача, 1 дрібниця.
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
    owner_names: Sequence[str] = (),
) -> dict:
    """Extract tasks and named entities from a comment — one Claude call.

    effort='low' by default: the input is two sentences, a structured extraction
    from them does not need deep thinking, while parsing is triggered by a button on
    each comment separately, so the cost is noticeable exactly here.

    Returns {"action_items", "people", "projects", "orgs", "model", *_tokens}.
    Raises RuntimeError without ANTHROPIC_API_KEY or on invalid JSON.
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
        f"{date_line}{_owner_line(owner_names)}"
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
        raise RuntimeError(tr("svc_llm.polish.bad_json")) from e

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
# Phase 16C: NL description of table sheets (Excel/CSV)
# ============================================================
#
# A bare dump of cells embeds badly in meaning. This description ("what this data is about")
# is mixed into the text of the sheet block → the table becomes findable via RAG. One
# Claude call for the WHOLE document (all sheets together), over a SAMPLE of each sheet
# (not the full volume) — cheap. Optional: without a key this step is simply skipped.

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
    """NL description of each table sheet (Phase 16C) — one Claude call.

    Args:
        sheets: [{"name": str, "rows": int|None, "cols": int|None, "preview": str}]
            preview — a sample markdown table (header + first rows).
        model: override; default — models.get_default_model().

    Returns:
        {"descriptions": list[str] (aligned with sheets by order),
         "model", token usage}. An empty list if there are no sheets.

    Raises:
        RuntimeError if ANTHROPIC_API_KEY is missing (the call is gated by the caller).
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
# Wave 5.1: commitments from a correspondence thread
# ============================================================
#
# Why a separate extractor, not extract_meeting_card. The meeting card also builds
# the entity graph (people/projects/orgs/topics), while for TG that has already been done
# differently — by reconciling with the existing graph (Wave 4.5.3, `tg_entities`). Letting the card
# in here would mean a second, competing mechanism of graph population: exactly those
# duplicates "Поларіс/Polaris/поларис" that wave 4.5.3 deliberately avoided.
# What is needed here is narrow: who promised what to whom and by what date.

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
     "msg": <номер повідомлення, в якому це прозвучало>,
     "for_whom": "owner" | "promised_to_owner" | "third_party",
     "confidence": <число від 0 до 1>,
     "importance": 1 | 2 | 3}
  ]
}

Правила:
- Не вигадуй. Якщо в нитці немає жодної домовленості — поверни {"tasks": []}. Порожній список — нормальна і часта відповідь.
- "owner" — виконавець, а не той, хто просить. Якщо людина пише «зроблю» — власник вона сама; якщо «зроби, будь ласка» — власник адресат. Не видно кому — null.
- "due" — дослівна фраза строку з переписки. Немає — null, не підставляй свою.
- "due_date" — та сама дата абсолютно, порахована від дати ТОГО повідомлення, де строк прозвучав (дати дано біля кожного повідомлення). Строк неточний («найближчим часом», «як буде час») — null.
- "msg" — номер того повідомлення, де зобовʼязання прозвучало (не сусіднього).
- "for_whom" — для кого задача відносно ВЛАСНИКА архіву (його імена, якщо відомі, дано в повідомленні
  користувача): "owner" — робить сам власник; "promised_to_owner" — хтось пообіцяв це власнику або зробити
  для нього; "third_party" — домовленість між іншими людьми, власник не виконавець і не отримувач.
- "confidence" — число від 0 до 1: наскільки ти впевнений, що це реальна домовленість, а не думка вголос.
- "importance" — 1 | 2 | 3: 3 важливо/терміново, 2 звичайна задача, 1 дрібниця.
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
    owner_names: Sequence[str] = (),
) -> dict:
    """Extract commitments from a correspondence thread (Wave 5.1) — one Claude call.

    Args:
        messages: [{"n": int, "sender": str, "date": "YYYY-MM-DD", "text": str}]
            in chronological order; ``n`` is the number the model refers to
            in the ``msg`` field (the specific archive message is then taken from there).
        chat_title / thread_label: thread context for the prompt.
        effort: 'low' — structure extraction, deep thinking does not pay off here.

    Returns:
        {"tasks": [{"task","owner","due","due_date","msg"}], "model", token usage}.

    Raises:
        RuntimeError if ANTHROPIC_API_KEY is missing or Claude returned non-JSON.
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
        f"{_owner_line(owner_names)}"
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
        raise RuntimeError(tr("svc_llm.polish.bad_json")) from e

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
