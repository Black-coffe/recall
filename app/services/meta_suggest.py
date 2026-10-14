"""Варіанти власної назви й опису запису від локальної моделі (спека recording-meta-suggest).

Нічого не пише в БД: лише пропонує, а зберігає звичайний PATCH `/api/history/<id>`.

Вхід моделі — `summary_json` запису (саммарі Claude від enrichment), а якщо його ще
нема — фрагменти транскрипту: початок, середина, кінець. Замір на #8677 (14.10.2026):
за саммарі варіанти предметні, за фрагментами — загальні («Обсуждение проекта…»),
тому `basis` повертається викликачу, і вікно чесно каже, з чого пропозиція.

Модель — `LOCAL_TITLE_MODEL` (MamayLM 12B: ~40 с, три варіанти справді різні; qwen2.5:7b
за 10 с давала три майже однакові описи, qwen3.5:9b — порожній JSON).

Лише stdlib + `local_llm` + `record_meta`: модуль не тягне torch.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core import settings
from app.services import local_llm, record_meta

logger = logging.getLogger(__name__)

MAX_VARIANTS = 3
EXCERPT_CHARS = 2500
KEY_POINTS_MAX = 10

_LANG_NAMES = {"uk": "українська", "ru": "русский", "en": "English"}

_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "descriptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titles", "descriptions"],
}

_QUOTES = "\"'«»„“”`"


class SuggestError(RuntimeError):
    """Контрольована причина, чому варіантів нема (текст — для людини)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _summary_parts(raw: Any) -> tuple[str, list[str]]:
    if not raw:
        return "", []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        logger.warning("meta_suggest: summary_json не JSON — беру фрагменти транскрипту")
        return "", []
    if not isinstance(data, dict):
        return "", []
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    points = [p for p in (data.get("key_points") or []) if isinstance(p, str) and p.strip()]
    return summary.strip(), points[:KEY_POINTS_MAX]


def _excerpts(text: str) -> str:
    if len(text) <= EXCERPT_CHARS * 3:
        return "Транскрипт:\n" + text
    mid = len(text) // 2
    return ("Фрагменти транскрипту:\n[початок] " + text[:EXCERPT_CHARS]
            + "\n[середина] " + text[mid:mid + EXCERPT_CHARS]
            + "\n[кінець] " + text[-EXCERPT_CHARS:])


def build_context(row) -> tuple[str, str, bool]:
    """(текст для моделі, basis, summary_ready) для рядка `transcriptions`."""
    summary, points = _summary_parts(row["summary_json"])
    if summary:
        ctx = "Саммарі запису:\n" + summary
        if points:
            ctx += "\nКлючові пункти:\n- " + "\n- ".join(points)
        return ctx, "summary", True
    text = (row["transcript_text"] or "").strip()
    return _excerpts(text), "excerpts", False


def _system_prompt(language: str | None) -> str:
    lang = _LANG_NAMES.get((language or "").lower())
    lang_rule = (f"Мова відповіді — {lang}." if lang
                 else "Відповідай мовою, якою написаний наданий текст.")
    return (
        "Ти придумуєш назву й опис запису дзвінка для особистого архіву. " + lang_rule + " "
        f"Дай {MAX_VARIANTS} різні варіанти назви (до 80 символів, конкретно: хто і про що, "
        f"без лапок і дат) і {MAX_VARIANTS} різні варіанти опису (1–3 речення: про що розмова "
        "і що вирішили). Лише JSON."
    )


def clean_variants(values: Any, limit: int) -> list[str]:
    """Прибрати порожні, дублі (без урахування регістру), лапки навколо; обрізати під ліміт."""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return out
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = " ".join(value.split()).strip(_QUOTES + " ").strip()
        if not cleaned:
            continue
        if len(cleaned) > limit:
            cleaned = cleaned[:limit - 1].rstrip() + "…"
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= MAX_VARIANTS:
            break
    return out


def suggest(conn, transcription_id: int) -> dict:
    """Варіанти назви й опису. Кидає :class:`SuggestError` (404/422/503)."""
    row = conn.execute(
        "SELECT id, transcript_text, summary_json, language FROM transcriptions "
        "WHERE id = ? AND deleted_at IS NULL",
        (transcription_id,),
    ).fetchone()
    if row is None:
        raise SuggestError("Транскрибування не знайдено", 404)
    if not (row["transcript_text"] or "").strip() and not row["summary_json"]:
        raise SuggestError("У запису ще нема тексту транскрипту", 422)

    context, basis, summary_ready = build_context(row)
    model = settings.env("LOCAL_TITLE_MODEL") or None
    timeout = settings.env_float("LOCAL_TITLE_TIMEOUT")
    # "0" — вивантажити модель одразу після відповіді: інакше 12B тримає VRAM
    # ще LOCAL_LLM_KEEPALIVE (30 хв), і дзвінок, що почнеться в цей час,
    # лишає копілот і live-транскрипцію без місця. Замір 14.10.2026: після
    # виклику з keep_alive="0" у /api/ps лишається лише модель копілота.
    keep_alive = settings.env("LOCAL_TITLE_KEEPALIVE")

    ok, reason = local_llm.availability()
    if not ok:
        raise SuggestError(f"Локальна модель недоступна: {reason}", 503)
    try:
        result = local_llm.generate_json(
            context, schema=_SCHEMA, system=_system_prompt(row["language"]),
            max_tokens=700, temperature=0.4, model=model, timeout=timeout,
            keep_alive=keep_alive,
        )
    except local_llm.LocalLLMError as e:
        logger.warning("meta_suggest: id=%s — локальна модель не відповіла: %s",
                       transcription_id, e)
        raise SuggestError(f"Локальна модель не відповіла: {e}", 503) from e

    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    titles = clean_variants(data.get("titles"), record_meta.TITLE_MAX)
    descriptions = clean_variants(data.get("descriptions"), record_meta.DESCRIPTION_MAX)
    if not titles and not descriptions:
        raise SuggestError("Модель повернула порожні варіанти — спробуйте ще раз", 503)

    raw = result.get("raw") if isinstance(result, dict) else None
    used_model = (raw or {}).get("model") or model or local_llm.LOCAL_LLM_MODEL
    logger.info("meta_suggest: id=%s basis=%s model=%s → %d назв, %d описів",
                transcription_id, basis, used_model, len(titles), len(descriptions))
    return {
        "titles": titles,
        "descriptions": descriptions,
        "basis": basis,
        "summary_ready": summary_ready,
        "model": used_model,
    }
