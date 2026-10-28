"""Meeting participants picked by the owner (wave2-recording-calls, story 02).

Three questions answered without HTTP:

- ``suggest`` - who is probably in the meeting now: attendees of calendar events overlapping
  now +- ``calendar_bind.TOLERANCE``, matched to EXISTING persons through
  ``enrichment._find_entity`` (name, alias or a merged-away spelling);
- ``search`` - which persons match a typed fragment (canonical name or alias, ``casefold``
  in Python: SQL ``lower()`` does not fold Cyrillic);
- ``link_to_transcription`` - link person ids to a transcript in ``meeting_entities`` with
  ``source='owner'``; a re-enrich keeps these rows as it keeps ``calendar``.

No entity is ever created, no email is stored, nothing calls Claude or the embeddings.
The recording manifest carries the picked ids as ``participants: [int]``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.services import calendar_bind, enrichment

logger = logging.getLogger(__name__)

SOURCE = "owner"


def _person_ids(values) -> list[int]:
    """Distinct positive ints in input order; anything else (bool, text, None) is dropped."""
    out: list[int] = []
    for v in values or []:
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            continue
        if v not in out:
            out.append(v)
    return out


def session_participants(manifest: Optional[dict]) -> list[int]:
    """The person ids a recording manifest carries (``participants``), or ``[]``."""
    if not isinstance(manifest, dict):
        return []
    raw = manifest.get("participants")
    return _person_ids(raw) if isinstance(raw, list) else []


def suggest(conn, now: Optional[datetime] = None) -> list[dict]:
    """Existing persons among the attendees of events overlapping ``now`` +- TOLERANCE."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows = conn.execute(
        "SELECT e.attendees FROM calendar_events e JOIN calendar_sources s ON s.id = e.source_id "
        "WHERE e.status != 'cancelled' AND e.all_day = 0 AND e.start_utc < ? AND e.end_utc > ? "
        "ORDER BY e.start_utc",
        (calendar_bind._iso(now + calendar_bind.TOLERANCE),
         calendar_bind._iso(now - calendar_bind.TOLERANCE))).fetchall()
    seen: list[int] = []
    for r in rows:
        try:
            people = json.loads(r[0] or "[]")
        except (TypeError, ValueError):
            logger.debug("calendar event attendees unreadable, skipped", exc_info=True)
            continue
        for p in people if isinstance(people, list) else []:
            name = (p.get("name") or "").strip() if isinstance(p, dict) else ""
            if not name:
                continue
            eid = enrichment._find_entity(conn, "person", enrichment._normalize(name))
            if eid is not None and eid not in seen:
                seen.append(eid)
    names = _names(conn, seen)
    return [{"id": i, "name": names[i], "source": "calendar"} for i in seen if i in names]


def _names(conn, ids: Iterable[int]) -> dict[int, str]:
    ids = list(ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT id, canonical_name FROM entities WHERE type = 'person' AND id IN ({marks})",
        ids).fetchall()}


def search(conn, q: str, limit: int = 10) -> list[dict]:
    """Persons whose canonical name or alias contains ``q`` (casefold); prefix matches first."""
    needle = (q or "").strip().casefold()
    if not needle or limit <= 0:
        return []
    spellings: dict[int, list[str]] = {}
    names: dict[int, str] = {}
    for r in conn.execute("SELECT id, canonical_name FROM entities WHERE type = 'person'"):
        names[r[0]] = r[1] or ""
        spellings.setdefault(r[0], []).append(r[1] or "")
    for r in conn.execute(
            "SELECT a.entity_id, a.alias FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
            "WHERE e.type = 'person'"):
        spellings.setdefault(r[0], []).append(r[1] or "")
    ranked = []
    for eid, forms in spellings.items():
        best = None
        for form in forms:
            low = form.casefold()
            pos = low.find(needle)
            if pos < 0:
                continue
            rank = 0 if pos == 0 else (1 if low[pos - 1] in " -'" else 2)
            best = rank if best is None else min(best, rank)
        if best is not None:
            ranked.append((best, names[eid].casefold(), eid))
    ranked.sort()
    return [{"id": eid, "name": names[eid]} for _, _, eid in ranked[:limit]]


def link_to_transcription(conn, transcription_id: int, entity_ids) -> int:
    """Link existing persons to a transcript (``source='owner'``); returns rows written.

    Unknown and non-person ids are skipped; a repeat or a link another source already made is
    left as it is (``INSERT OR IGNORE``)."""
    ids = _person_ids(entity_ids)
    known = _names(conn, ids)
    linked = 0
    for eid in ids:
        if eid not in known:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO meeting_entities (transcription_id, entity_id, mention_count, source) "
            "VALUES (?, ?, 1, ?)", (transcription_id, eid, SOURCE))
        linked += cur.rowcount
    return linked
