"""What every task writer needs to turn an extracted item into a proposal (ADR-056).

The extraction calls (`text_polishing`: meeting card, summary, comment analysis,
`extract_chat_tasks`) ask the model for three extra fields per action item:
`for_whom`, `confidence`, `importance`. The model may omit them or return junk, so
the writers never read them raw: `normalize_candidate` fills the defaults and clamps,
`filter_candidates` drops what is not proposed (third-party tasks, low confidence),
and `text_key` / `existing_keys` let a re-run skip a task the owner already decided on.

Matching is EXACT on purpose (normalised text hash within one record): no
embeddings (lesson `e5-cosine-cannot-thread-chat`) and no fuzzy matching here.
Whether exact is enough across re-runs is measured by `evals/task_rerun_measure.py`.

Only stdlib + `app.db.connection`; the owner-name lookup imports its helpers lazily.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from typing import Any, Iterable, Optional

from app.core import settings

logger = logging.getLogger(__name__)

FOR_WHOM = ("owner", "promised_to_owner", "third_party", "unknown")
DEFAULT_FOR_WHOM = "unknown"
DEFAULT_IMPORTANCE = 2
_DEFAULT_MIN_CONFIDENCE = 0.6

# Apostrophe-like characters are removed (not replaced by a space), so the two Ukrainian
# spellings of one word give one key; U+02BC is a letter modifier, not punctuation, in Unicode.
_APOSTROPHES = {"'", "’", "ʼ", "`", "´"}
_WS_RE = re.compile(r"\s+")


def text_key(text: Optional[str]) -> str:
    """sha1 hex of the normalised task text: NFC, casefold, punctuation stripped,
    whitespace collapsed. Two texts that differ only in those get the same key."""
    s = unicodedata.normalize("NFC", text or "").casefold()
    out = []
    for ch in s:
        if ch in _APOSTROPHES:
            continue
        out.append(" " if unicodedata.category(ch).startswith("P") else ch)
    s = _WS_RE.sub(" ", "".join(out)).strip()
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _confidence(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return min(1.0, max(0.0, f))


def _importance(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return DEFAULT_IMPORTANCE
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_IMPORTANCE
    return min(3, max(1, n))


def _for_whom(value: Any) -> str:
    if not isinstance(value, str):
        return DEFAULT_FOR_WHOM
    v = value.strip().lower()
    return v if v in FOR_WHOM else DEFAULT_FOR_WHOM


def normalize_candidate(item: dict) -> dict:
    """A copy of the extracted item with the three proposal fields made safe.

    Missing -> `for_whom='unknown'`, `confidence=None`, `importance=2`; `importance`
    clamped to 1..3, `confidence` to 0..1, an unknown `for_whom` -> `unknown`. A missing
    field is model non-compliance, not low confidence: it stays proposable."""
    out = dict(item or {})
    out["for_whom"] = _for_whom(out.get("for_whom"))
    out["confidence"] = _confidence(out.get("confidence"))
    out["importance"] = _importance(out.get("importance"))
    return out


def filter_candidates(items: Iterable[dict], *, min_confidence: float) -> tuple[list[dict], dict]:
    """Normalise each item and drop what is never proposed.

    Returns (kept, {"third_party": n, "low_confidence": n}). `confidence is None`
    is kept; a third-party item is counted as third-party even if also unsure."""
    kept: list[dict] = []
    dropped = {"third_party": 0, "low_confidence": 0}
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = normalize_candidate(raw)
        if item["for_whom"] == "third_party":
            dropped["third_party"] += 1
        elif item["confidence"] is not None and item["confidence"] < min_confidence:
            dropped["low_confidence"] += 1
        else:
            kept.append(item)
    return kept, dropped


def existing_keys(conn, transcription_id: int) -> set[str]:
    """Keys of every task already in the record, in any status (tombstones included).

    A row without a stored `text_key` (written before v59) gets its key computed
    from `task`, so a re-run never re-proposes a legacy task either."""
    rows = conn.execute(
        "SELECT text_key, task FROM action_items WHERE transcription_id = ?",
        (transcription_id,)).fetchall()
    return {(r[0] or text_key(r[1])) for r in rows}


def owner_names(db_path: str) -> list[str]:
    """The archive owner's names for the extraction prompt: canonical + aliases.

    The owner comes from `tg_questions.resolve_self()` (env override, listener, cache,
    private chats) and is matched to a graph entity by `enrichment._normalize`, the
    same key the dashboard owner filter uses. A resolved owner without a graph entity
    gives the resolved name and handle; nothing resolvable gives []."""
    from app.services import enrichment, tg_questions
    from app.db.connection import get_db_connection

    try:
        me = tg_questions.resolve_self(db_path)
    except Exception as e:      # owner names are a prompt hint; extraction works without them
        logger.debug("[task_proposals] owner not resolved: %s", e)
        return []
    if not me or not (me.get("name") or me.get("username")):
        logger.debug("[task_proposals] owner not resolved: no self identity")
        return []

    name = (me.get("name") or "").strip()
    handle = (me.get("username") or "").strip().lstrip("@")
    raw = [n for n in (name, handle, f"@{handle}" if handle else "") if n]
    keys = sorted({enrichment._normalize(n) for n in raw} - {""})

    found: list[str] = []
    try:
        with get_db_connection(db_path) as conn:
            marks = ",".join("?" * len(keys))
            row = conn.execute(
                f"SELECT e.id, e.canonical_name FROM entities e WHERE e.type = 'person' AND "
                f"(e.normalized_name IN ({marks}) OR EXISTS (SELECT 1 FROM entity_aliases ea "
                f"WHERE ea.entity_id = e.id AND ea.normalized_alias IN ({marks}))) "
                f"ORDER BY e.meeting_count DESC, e.id LIMIT 1",
                keys + keys).fetchone()
            if row:
                found.append(row[1])
                found += [a[0] for a in conn.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
                    (row[0],)).fetchall()]
    except Exception as e:      # a missing graph table is a fresh DB, not an error
        logger.debug("[task_proposals] owner entity lookup failed: %s", e)

    names = found or [name, f"@{handle}" if handle else ""]
    out: list[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def min_confidence() -> float:
    """`TASK_PROPOSAL_MIN_CONFIDENCE` clamped to 0..1; an unreadable value -> 0.6."""
    try:
        v = settings.env_float("TASK_PROPOSAL_MIN_CONFIDENCE")
    except ValueError:
        logger.warning("[task_proposals] TASK_PROPOSAL_MIN_CONFIDENCE is not a number; using %s",
                       _DEFAULT_MIN_CONFIDENCE)
        return _DEFAULT_MIN_CONFIDENCE
    if math.isnan(v):
        return _DEFAULT_MIN_CONFIDENCE
    return min(1.0, max(0.0, v))
