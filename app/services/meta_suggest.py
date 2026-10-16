"""Variants of an own title and description of a record from the local model (spec recording-meta-suggest).

Writes nothing to the DB: only suggests, and the usual PATCH `/api/history/<id>` saves.

The model input is the record's `summary_json` (Claude's summary from enrichment), and if it is not
there yet, transcript fragments: beginning, middle, end. A measurement on #8677 (14.10.2026):
with a summary the variants are specific, with fragments they are generic ("Обсуждение проекта…"),
so `basis` is returned to the caller, and the window honestly says what the suggestion is based on.

The model is `LOCAL_TITLE_MODEL` (MamayLM 12B: ~40 s, three variants really differ; qwen2.5:7b
in 10 s gave three nearly identical descriptions, qwen3.5:9b an empty JSON).

Only stdlib + `local_llm` + `record_meta`: the module does not pull torch.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core import settings
from app.core.i18n import tr
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
    """A controlled reason why there are no variants (the text is for a person)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _summary_parts(raw: Any) -> tuple[str, list[str]]:
    if not raw:
        return "", []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        logger.warning("meta_suggest: summary_json is not JSON; using transcript fragments")
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
    """(text for the model, basis, summary_ready) for a `transcriptions` row."""
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
    """Remove empty ones, duplicates (case-insensitive), surrounding quotes; trim to the limit."""
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
    """Title and description variants. Raises :class:`SuggestError` (404/422/503)."""
    row = conn.execute(
        "SELECT id, transcript_text, summary_json, language FROM transcriptions "
        "WHERE id = ? AND deleted_at IS NULL",
        (transcription_id,),
    ).fetchone()
    if row is None:
        raise SuggestError(tr("svc_embed.suggest.not_found"), 404)
    if not (row["transcript_text"] or "").strip() and not row["summary_json"]:
        raise SuggestError(tr("svc_embed.suggest.no_text"), 422)

    context, basis, summary_ready = build_context(row)
    model = settings.env("LOCAL_TITLE_MODEL") or None
    timeout = settings.env_float("LOCAL_TITLE_TIMEOUT")
    # "0" unloads the model right after the answer: otherwise the 12B holds VRAM
    # for another LOCAL_LLM_KEEPALIVE (30 min), and a call that starts in that time
    # leaves the copilot and live transcription without room. Measurement 14.10.2026: after
    # a call with keep_alive="0" only the copilot model is left in /api/ps.
    keep_alive = settings.env("LOCAL_TITLE_KEEPALIVE")

    ok, reason = local_llm.availability()
    if not ok:
        raise SuggestError(tr("svc_embed.suggest.model_unavailable", reason=reason), 503)
    try:
        result = local_llm.generate_json(
            context, schema=_SCHEMA, system=_system_prompt(row["language"]),
            max_tokens=700, temperature=0.4, model=model, timeout=timeout,
            keep_alive=keep_alive,
        )
    except local_llm.LocalLLMError as e:
        logger.warning("meta_suggest: id=%s - local model did not respond: %s",
                       transcription_id, e)
        raise SuggestError(tr("svc_embed.suggest.model_failed", error=e), 503) from e

    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    titles = clean_variants(data.get("titles"), record_meta.TITLE_MAX)
    descriptions = clean_variants(data.get("descriptions"), record_meta.DESCRIPTION_MAX)
    if not titles and not descriptions:
        raise SuggestError(tr("svc_embed.suggest.empty"), 503)

    raw = result.get("raw") if isinstance(result, dict) else None
    used_model = (raw or {}).get("model") or model or local_llm.LOCAL_LLM_MODEL
    logger.info("meta_suggest: id=%s basis=%s model=%s → %d titles, %d descriptions",
                transcription_id, basis, used_model, len(titles), len(descriptions))
    return {
        "titles": titles,
        "descriptions": descriptions,
        "basis": basis,
        "summary_ready": summary_ready,
        "model": used_model,
    }
