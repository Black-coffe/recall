"""Meeting Memory — orchestrator for transcript enrichment (Phase 13).

Turns a raw transcript into a structured "meeting card" + fills the
cross-meeting entity graph (people / projects / organizations) and the action items table,
so that Whisper works as a RAG archive of corporate memory.

Flow (one Claude call per meeting — text_polishing.extract_meeting_card):
  1. Read the transcript + segments + speaker-map from the DB.
  2. Build the diarized text ('Name: utterance') if there is diarization.
  3. extract_meeting_card -> summary/key_points/action_items/people/projects/orgs/topics.
  4. In a transaction:
     - UPDATE transcriptions: summary_json + topics_json (same format as the existing
       endpoints, so the UI summary/topics keeps working), enriched_* markers.
     - upsert entities + aliases, link meeting_entities, recompute aggregates.
     - rewrite action_items (idempotent re-run).

Idempotency: enriched_at IS NOT NULL -> skip (if force=False). Changing
ENRICHMENT_VERSION lets a mass re-run be forced when prompts are updated.

Does NOT depend on the flask app context — takes db_path explicitly (testable, can be
called from a JobQueue worker or a backfill script).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime
from typing import Callable, Optional

from app.db.connection import get_db_connection
from app.repositories import transcriptions as tx_repo
from app.services import categories, commitments, embeddings, models, pricing, text_polishing


logger = logging.getLogger(__name__)

# Bump when the enrichment prompt/schema changes -> backfill(force_version=True) redoes old ones.
ENRICHMENT_VERSION = 1

_VALID_ENTITY_TYPES = ("person", "project", "org", "topic")

# Tag suggestions (story 06): the bounds are separate from `categories.parse_tags` (same
# strip+dedup, but the length and the exclusion of already confirmed tags are decisions of
# this path only, not a general tag rule).
_MAX_SUGGESTED_TAG_LEN = 40
_MAX_SUGGESTED_TAGS = 5


# ============================================================
# Normalization / deduplication
# ============================================================

def _normalize(name: str) -> str:
    """Canonical key for dedup: lowercase, strip punctuation at the edges,
    collapse whitespace. Keeps Cyrillic/Latin/digits."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" \t\n\r.,;:!?\"'`«»()[]{}-–—")
    return s


def _count_mentions(text: str, names: list[str]) -> int:
    """How many times an entity (canonical + aliases) is mentioned in the text.
    A rough case-insensitive substring count — for salience/weight."""
    if not text or not names:
        return 0
    low = text.lower()
    total = 0
    for n in names:
        n = (n or "").strip().lower()
        if len(n) < 2:
            continue
        total += low.count(n)
    return total


# ============================================================
# Entity graph
# ============================================================

def _find_entity(c, etype: str, normalized: str) -> Optional[int]:
    """Find an entity by normalized name (canonical or alias)."""
    if not normalized:
        return None
    row = c.execute(
        "SELECT id FROM entities WHERE type = ? AND normalized_name = ?",
        (etype, normalized),
    ).fetchone()
    if row:
        return row["id"]
    # via alias (an alias is globally unique; check that the type matches)
    row = c.execute(
        "SELECT e.id FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
        "WHERE a.normalized_alias = ? AND e.type = ?",
        (normalized, etype),
    ).fetchone()
    if row:
        return row["id"]
    return _find_merged_entity(c, etype, normalized)


def _find_merged_entity(c, etype: str, normalized: str) -> Optional[int]:
    """The entity into which a row of THIS type with this spelling was already merged.

    Without this a cross-type merge does not "stick": after merging org->project
    the spelling belongs to the project, the branch with the type check finds nothing, and
    the next enrich creates the org AGAIN — and with zero aliases, because all its
    `INSERT OR IGNORE` stumble on the global UNIQUE `normalized_alias`.
    The row comes back to life unreachable by alias search, and every new
    spelling breeds yet another one.

    We look at the merge trace itself (`metadata_json.merged_from`), not merely at an
    alias without a type check: otherwise the org «Ткачук» would silently become the person
    Ткачук — i.e. pairs the owner deliberately did NOT merge would collapse on their own.
    JSON is parsed in Python because this project does not rely on JSON1.

    A TYPE match is not enough: a row that once absorbed some org would otherwise
    appropriate any org whose spelling happens to sit among its
    aliases («BH» with an absorbed «Acmecorp Ltd»). So we also check the spelling
    — against the absorbed row's `aliases` or, for old records without it, against its
    canonical name.
    """
    rows = c.execute(
        "SELECT e.id, e.metadata_json FROM entity_aliases a "
        "JOIN entities e ON e.id = a.entity_id "
        "WHERE a.normalized_alias = ? AND e.metadata_json IS NOT NULL",
        (normalized,),
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(meta, dict):
            continue
        for gone in meta.get("merged_from") or []:
            if not isinstance(gone, dict) or gone.get("type") != etype:
                continue
            spellings = gone.get("aliases")
            if not spellings:                      # records from before the field existed
                spellings = [_normalize(gone.get("name") or "")]
            if normalized in spellings:
                return row["id"]
    return None


def _upsert_entity(
    c,
    etype: str,
    name: str,
    role: Optional[str] = None,
    aliases: Optional[list[str]] = None,
) -> Optional[int]:
    """Find or create an entity; add aliases; update the role if it was empty.

    Returns entity_id, or None if the name is empty/invalid.
    """
    if etype not in _VALID_ENTITY_TYPES:
        return None
    canonical = (name or "").strip()
    norm = _normalize(canonical)
    if not norm:
        return None

    aliases = aliases or []
    # Try to find by canonical and by each alias
    entity_id = _find_entity(c, etype, norm)
    if entity_id is None:
        for a in aliases:
            entity_id = _find_entity(c, etype, _normalize(a))
            if entity_id is not None:
                break

    now = datetime.now().isoformat(timespec="seconds")
    if entity_id is None:
        cur = c.execute(
            "INSERT INTO entities (type, canonical_name, normalized_name, role, "
            "first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (etype, canonical, norm, (role or None), now, now),
        )
        entity_id = cur.lastrowid
    else:
        # Fill in the role if there was none
        if role:
            existing = c.execute(
                "SELECT role FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if existing and not existing["role"]:
                c.execute(
                    "UPDATE entities SET role = ?, updated_at = ? WHERE id = ?",
                    (role, now, entity_id),
                )

    # Register aliases (canonical too, as an alias — for future lookups)
    for a in [canonical, *aliases]:
        na = _normalize(a)
        if not na:
            continue
        c.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized_alias) "
            "VALUES (?, ?, ?)",
            (entity_id, a.strip(), na),
        )

    # Link person -> speakers (voiceprint) if the name matches
    if etype == "person":
        sp = c.execute(
            "SELECT id FROM speakers WHERE name = ? COLLATE NOCASE LIMIT 1",
            (canonical,),
        ).fetchone()
        if sp:
            c.execute(
                "UPDATE entities SET speaker_id = ? WHERE id = ? AND speaker_id IS NULL",
                (sp["id"], entity_id),
            )

    return entity_id


def _recompute_entity_aggregates(c, entity_ids: set[int]) -> None:
    """Recompute mention_count / meeting_count from meeting_entities (idempotent)."""
    for eid in entity_ids:
        c.execute(
            "UPDATE entities SET "
            "mention_count = COALESCE((SELECT SUM(mention_count) FROM meeting_entities WHERE entity_id = ?), 0), "
            "meeting_count = COALESCE((SELECT COUNT(*) FROM meeting_entities WHERE entity_id = ?), 0), "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (eid, eid, eid),
        )


# ============================================================
# Diarized text for enrichment
# ============================================================

def _diarized_text_for(c, transcription_id: int, segments_json: Optional[str]) -> Optional[str]:
    """Assemble the 'Name: utterance' format from segments + speaker-map, if there is diarization."""
    if not segments_json:
        return None
    try:
        segments = json.loads(segments_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not segments or not any(s.get("speaker") for s in segments if isinstance(s, dict)):
        return None
    rows = c.execute(
        "SELECT m.raw_label, s.name FROM transcription_speaker_map m "
        "LEFT JOIN speakers s ON s.id = m.speaker_id WHERE m.transcription_id = ?",
        (transcription_id,),
    ).fetchall()
    speaker_map = {r["raw_label"]: r["name"] for r in rows if r["name"]}
    return text_polishing._format_diarized_input(segments, speaker_map)


# ============================================================
# Tag suggestions (story 06)
# ============================================================

def _filter_suggested_tags(raw_tags, existing_names: list[str]) -> list[str]:
    """`card["tags"]` (raw Claude output) -> a ready list for `suggested_tags_json`.

    strip + dedup by `categories.norm_name` (via `parse_tags`), drops
    overlong (>40 characters) ones and those already confirmed in this record's
    `record_tags`, truncates to 5. Does not write `record_tags` and does not create a tag in the dictionary —
    acceptance does both (story 07); here it is only a hint.
    """
    if not isinstance(raw_tags, list) or not raw_tags:
        return []
    existing_norm = {categories.norm_name(n) for n in existing_names}
    out: list[str] = []
    for name in categories.parse_tags(raw_tags):
        if len(name) > _MAX_SUGGESTED_TAG_LEN:
            continue
        if categories.norm_name(name) in existing_norm:
            continue
        out.append(name)
        if len(out) >= _MAX_SUGGESTED_TAGS:
            break
    return out


# ============================================================
# Writing the card
# ============================================================

def _persist_card(c, transcription_id: int, card: dict, transcript_text: str,
                  created_at: Optional[str], existing_meeting_date: Optional[str]) -> None:
    """Write the extract_meeting_card result to the DB (within the caller's transaction)."""
    now = datetime.now().isoformat(timespec="seconds")

    # --- summary_json in the format of the existing summary endpoint ---
    summary_payload = {
        "summary": card.get("summary", ""),
        "key_points": card.get("key_points", []),
        "action_items": card.get("action_items", []),
    }
    # --- topics_json in the format of the existing topics endpoint ---
    topics_payload = {"topics": card.get("topics", []), "model": card.get("model", "")}

    # meeting_date: keep the existing one, otherwise the created_at date
    meeting_date = existing_meeting_date
    if not meeting_date and created_at:
        meeting_date = str(created_at)[:10]

    c.execute(
        "UPDATE transcriptions SET summary_json = ?, summary_at = CURRENT_TIMESTAMP, "
        "summary_model = ?, topics_json = ?, enriched_at = CURRENT_TIMESTAMP, "
        "enriched_model = ?, enrichment_version = ?, meeting_date = ? WHERE id = ?",
        (
            json.dumps(summary_payload, ensure_ascii=False),
            card.get("model", ""),
            json.dumps(topics_payload, ensure_ascii=False),
            card.get("model", ""),
            ENRICHMENT_VERSION,
            meeting_date,
            transcription_id,
        ),
    )

    # --- tag suggestions: not record_tags, only suggested_tags_json (story 06) ---
    existing_tag_names = [
        t["name"] for t in categories.tags_for(c, [transcription_id]).get(transcription_id, [])
    ]
    suggested_tags = _filter_suggested_tags(card.get("tags"), existing_tag_names)
    c.execute(
        "UPDATE transcriptions SET suggested_tags_json = ? WHERE id = ?",
        (json.dumps(suggested_tags, ensure_ascii=False) if suggested_tags else None,
         transcription_id),
    )

    # --- entities: collect old links (for recomputing), erase, rewrite ---
    old_links = c.execute(
        "SELECT entity_id FROM meeting_entities WHERE transcription_id = ?",
        (transcription_id,),
    ).fetchall()
    affected = {r["entity_id"] for r in old_links}

    c.execute("DELETE FROM meeting_entities WHERE transcription_id = ?", (transcription_id,))
    c.execute("DELETE FROM action_items WHERE transcription_id = ?", (transcription_id,))

    # person / project / org → entities + meeting_entities
    person_index: dict[str, int] = {}  # normalized canonical/alias -> entity_id (for owner resolve)

    def _ingest(items, etype):
        for item in items:
            if isinstance(item, str):
                name, role, aliases = item, None, []
            elif isinstance(item, dict):
                name = item.get("name") or ""
                role = item.get("role")
                aliases = item.get("aliases") or []
            else:
                continue
            eid = _upsert_entity(c, etype, name, role=role, aliases=aliases)
            if eid is None:
                continue
            affected.add(eid)
            mentions = _count_mentions(transcript_text, [name, *aliases]) or 1
            c.execute(
                "INSERT OR REPLACE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, role_in_meeting) "
                "VALUES (?, ?, ?, ?)",
                (transcription_id, eid, mentions, role),
            )
            if etype == "person":
                person_index[_normalize(name)] = eid
                for a in aliases:
                    person_index[_normalize(a)] = eid

    _ingest(card.get("people", []), "person")
    _ingest(card.get("projects", []), "project")
    _ingest(card.get("orgs", []), "org")
    # topics as entities too (for unified entity pages / faceted search)
    _ingest([{"name": t} for t in card.get("topics", [])], "topic")

    # --- action_items -> normalized table, owner resolved to a person entity ---
    for ai in card.get("action_items", []):
        if not isinstance(ai, dict):
            continue
        task = (ai.get("task") or "").strip()
        if not task:
            continue
        owner_name = ai.get("owner")
        owner_eid = person_index.get(_normalize(owner_name)) if owner_name else None
        if owner_name and owner_eid is None:
            owner_eid = _find_entity(c, "person", _normalize(owner_name))
        # Track 1: the deadline is stored both as the raw phrase (due) and as a normalized date.
        # Priority is the model's due_date (it saw the conversation context), but
        # only if it is really an ISO date; otherwise the parser computes it from the
        # meeting date. This way the field does not depend on whether the model complied.
        raw_due = ai.get("due")
        due_date, due_prec = commitments.parse_due(raw_due, meeting_date)
        model_due = (ai.get("due_date") or "").strip() if isinstance(ai.get("due_date"), str) else ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", model_due):
            due_date, due_prec = model_due, (due_prec or commitments.P_DAY)
        c.execute(
            "INSERT INTO action_items (transcription_id, task, owner_name, "
            "owner_entity_id, due, due_date, due_precision, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (transcription_id, task, owner_name, owner_eid, raw_due, due_date, due_prec),
        )

    _recompute_entity_aggregates(c, affected)


# ============================================================
# Public API
# ============================================================

def is_available() -> bool:
    """Card enrichment is possible only with a configured Claude API key."""
    return text_polishing.is_available()


def any_available() -> bool:
    """Whether enrichment is worth running at all: Claude (card) OR local embeddings."""
    return text_polishing.is_available() or embeddings.is_available()


def enrich_transcription(
    db_path: str,
    transcription_id: int,
    *,
    model: Optional[str] = None,
    force: bool = False,
    effort: str = "medium",
) -> dict:
    """Full transcript enrichment = card (Claude) + embeddings (local).

    Two INDEPENDENT phases, each with its own idempotency:
    - card: entities/summary/action items via Claude (enriched_at marker).
    - embed: chunks + vectors locally (embedded_at marker) — works without an API key.

    effort: 'low' for mass backfill (3x faster/cheaper), 'medium' for
    live auto-transcription (quality).

    Returns {"transcription_id", "status", "card": {...}, "embed": {...}}.
    status: "done" if at least one phase did something, otherwise "skipped".
    """
    with get_db_connection(db_path) as conn:
        dup_row = tx_repo.get_by_id(conn, transcription_id, columns=("id", "duplicate_of"))
    if dup_row and dup_row["duplicate_of"] is not None:
        # Repair 3 (review #3): duplicates (dedup_audio.py) get neither a Claude
        # card nor chunks/embeddings — the same early exit and the same word
        # "skipped_duplicate" that ingest (transcription.py) already uses to mark
        # enrichment in the /api/transcribe response.
        skip = {"status": "skipped_duplicate", "transcription_id": transcription_id}
        return {"transcription_id": transcription_id, "status": "skipped_duplicate",
                "card": skip, "embed": skip}

    card_res = _enrich_card(db_path, transcription_id, model=model, force=force, effort=effort) \
        if text_polishing.is_available() else {"status": "unavailable"}

    embed_res = embeddings.chunk_and_embed_transcription(db_path, transcription_id, force=force) \
        if embeddings.is_available() else {"status": "unavailable"}

    if card_res.get("status") == "not_found" and embed_res.get("status") == "not_found":
        return {"transcription_id": transcription_id, "status": "not_found",
                "card": card_res, "embed": embed_res}
    did_work = card_res.get("status") == "enriched" or embed_res.get("status") == "embedded"
    return {
        "transcription_id": transcription_id,
        "status": "done" if did_work else "skipped",
        "card": card_res,
        "embed": embed_res,
    }


def _enrich_card(
    db_path: str,
    transcription_id: int,
    model: Optional[str] = None,
    force: bool = False,
    effort: str = "medium",
) -> dict:
    """Card phase: entities/summary/action items via Claude. Idempotent (enriched_at).

    Returns {"status": "enriched"|"skipped"|"not_found"|"empty", ...}.
    """
    # --- READ (short connection, before the network call) ---
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        row = tx_repo.get_by_id(
            conn, transcription_id,
            columns=("id", "transcript_text", "polished_text", "segments", "enriched_at",
                     "enrichment_version", "created_at", "meeting_date", "source_type"),
        )
        if not row:
            return {"status": "not_found", "transcription_id": transcription_id}

        # Phase 17: Telegram goes embed-only — a Claude card on a one-line chat
        # message («Ок», «Дякую») is useless, and over records it is also paid.
        # The decision applied ONLY on the ingest path (telegram.py:_submit_embed_only),
        # while backfill and the indexer button came in through the back door: there was
        # no source_type check here. force=True keeps a manual bypass, and Wave 5
        # will replace this with a selective filter (promise markers + length).
        if row["source_type"] == "telegram" and not force:
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "telegram_embed_only"}

        already = bool(row["enriched_at"]) and (row["enrichment_version"] == ENRICHMENT_VERSION)
        if already and not force:
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "already_enriched"}

        body_text = (row["polished_text"] or row["transcript_text"] or "")
        if not body_text.strip():
            return {"status": "empty", "transcription_id": transcription_id}

        diarized = _diarized_text_for(c, transcription_id, row["segments"])
        created_at = row["created_at"]
        existing_meeting_date = row["meeting_date"]
        tag_vocabulary = [t["name"] for t in categories.list_tags(conn)]

    # --- Claude call (outside the DB connection — a long network operation) ---
    # T6.3: there used to be NO try/except here — a failure of one transcript (network,
    # invalid JSON from the model, retries exhausted on transient errors)
    # propagated out of _enrich_card() and broke the WHOLE pair of phases in enrich_transcription()
    # (the embed phase, which does NOT depend on Claude, did not get to run either).
    # Soft degradation: we mark "retry_needed" instead of a hard failure —
    # the embed phase call and the rest of the backfill pass continue.
    try:
        card = text_polishing.extract_meeting_card(
            body_text, diarized_text=diarized, model=model, effort=effort,
            meeting_date=(existing_meeting_date or (str(created_at)[:10] if created_at else None)),
            tag_vocabulary=tag_vocabulary,
        )
    except Exception as e:
        logger.warning("[enrich] tx=%s card phase failed (retry_needed): %s", transcription_id, e)
        return {"status": "retry_needed", "transcription_id": transcription_id,
                "error": str(e)}

    # --- WRITE ---
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        _persist_card(c, transcription_id, card, body_text, created_at, existing_meeting_date)
        conn.commit()

    logger.info(
        "[enrich] tx=%s done: people=%d projects=%d orgs=%d topics=%d actions=%d "
        "(in=%d cache_read=%d out=%d)",
        transcription_id, len(card.get("people", [])), len(card.get("projects", [])),
        len(card.get("orgs", [])), len(card.get("topics", [])),
        len(card.get("action_items", [])), card.get("input_tokens", 0),
        card.get("cache_read_tokens", 0), card.get("output_tokens", 0),
    )
    return {
        "status": "enriched",
        "transcription_id": transcription_id,
        "model": card.get("model", ""),
        "counts": {
            "people": len(card.get("people", [])),
            "projects": len(card.get("projects", [])),
            "orgs": len(card.get("orgs", [])),
            "topics": len(card.get("topics", [])),
            "action_items": len(card.get("action_items", [])),
        },
        "input_tokens": card.get("input_tokens", 0),
        "output_tokens": card.get("output_tokens", 0),
        "cache_read_tokens": card.get("cache_read_tokens", 0),
    }


def list_unenriched_ids(db_path: str, limit: Optional[int] = None) -> list[int]:
    """IDs of transcripts that need work: card (Claude) OR embed (vectors) not yet
    done / out of date. Over-selection is safe — the phases skip separately."""
    with get_db_connection(db_path) as conn:
        # T4.6: do not burn Claude/embedding tokens on soft-deleted rows (the undo window
        # is not valid content; the grace-purge will remove such rows soon anyway).
        # Telegram gets here ONLY through the embed phase: a card does not apply to it
        # (see _enrich_card), so without this condition the indexer would count 3142
        # chat messages as "work" and show an honest user a
        # volume that does not exist. `IS NOT` is NULL-safe (source_type may be NULL).
        # T6.5: the embed phase goes stale not only on a MODEL change but also on a change
        # of chunking logic — and that is visible only through embedding_version. Without this
        # condition a bump of EMBED_VERSION would stay a declaration: chunk_and_embed
        # would recompute a record, but the work list would not name it, so nobody
        # would call it. The condition mirrors the idempotency check in
        # embeddings.chunk_and_embed_transcription (NULL = old chunking).
        sql = (
            "SELECT id FROM transcriptions WHERE deleted_at IS NULL "
            "AND duplicate_of IS NULL AND ("
            "(source_type IS NOT 'telegram' AND (enriched_at IS NULL OR "
            " enrichment_version IS NULL OR enrichment_version < ?)) "
            "OR embedded_at IS NULL OR embedding_model IS NULL OR embedding_model != ? "
            "OR embedding_version IS NULL OR embedding_version != ?) "
            "ORDER BY id"
        )
        params: list = [ENRICHMENT_VERSION, embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [r["id"] for r in conn.execute(sql, params).fetchall()]


def backfill(
    db_path: str,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    effort: str = "medium",
    progress_cb: Optional[Callable[[dict], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> dict:
    """Run enrichment over the whole history (like /indexer in meeting_archive).

    Idempotent — skips already enriched ones (if force=False). An error on one
    transcript does not break the whole pass (logged, counted in failed).
    """
    if force:
        with get_db_connection(db_path) as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM transcriptions WHERE deleted_at IS NULL "
                "AND duplicate_of IS NULL ORDER BY id"
            ).fetchall()]
        if limit:
            ids = ids[:limit]
    else:
        ids = list_unenriched_ids(db_path, limit=limit)

    total = len(ids)
    done = failed = skipped = 0
    logger.info("[backfill] start: %d transcripts (force=%s)", total, force)

    for i, tid in enumerate(ids):
        if cancel_cb and cancel_cb():
            logger.info("[backfill] cancelled at %d/%d", i, total)
            break
        try:
            res = enrich_transcription(db_path, tid, model=model, force=force, effort=effort)
            if res["status"] == "done":
                done += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            logger.error("[backfill] tx=%s failed: %s", tid, e, exc_info=True)
        if progress_cb:
            progress_cb({
                "processed": i + 1, "total": total,
                "done": done, "skipped": skipped, "failed": failed,
                "current_id": tid,
            })

    if done:
        optimize_chunk_index(db_path)

    result = {"total": total, "done": done, "skipped": skipped, "failed": failed}
    logger.info("[backfill] finished: %s", result)
    return result


def optimize_chunk_index(db_path: str) -> None:
    """Merge the FTS5 index segments of chunks after a mass rewrite.

    Every embedding does DELETE+INSERT of a record's chunks, and the triggers
    `chunks_ai/ad/au` write to `chunks_fts` — a thousand such transactions leave the
    index fragmented into segments. The cost was measured on the live archive after a
    re-embed of records (T6.5): `_fts_search` — **64 s** vs 9 s, i.e.
    search became unusable right after the pass that was meant to improve it.
    `optimize` itself costs ~0.4 s and reduces 7797 segments to 2287.
    An error here must not break backfill: the index stays working, just
    slow."""
    try:
        with get_db_connection(db_path) as conn:
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            conn.commit()
        logger.info("[backfill] chunks_fts optimize done")
    except Exception:
        logger.warning("[backfill] chunks_fts optimize failed — search will stay "
                       "slow until the next pass", exc_info=True)


# ============================================================
# CLI: backfill-cards (Wave B production-rag) — card phase only
# ============================================================
#
# `backfill()`/`list_unenriched_ids()` above stay as they are (the HTTP path through
# job_queue) — this CLI is offline and selects records more narrowly: card only (no
# embed phase), only those that lack one altogether (`summary_json IS NULL`),
# and with a manual model choice — the default `models.get_default_model()` (Opus 5)
# is expensive on large document archives.

#: --kind -> source_type. 'all' leaves only the base filter (not Telegram).
_KIND_SOURCE_TYPES = {
    "calls": ("file", "youtube", "recording"),
    "docs": ("document",),
}


def _select_backfill_card_rows(db_path: str, kind: str = "all",
                               limit: Optional[int] = None) -> list[dict]:
    """Records without a card: not Telegram (embed-only, T Wave 0), not soft-deleted,
    not duplicates (dedup_audio). `kind` narrows to a source_type."""
    sql = ("SELECT id, source_type, source_name, transcript_text, polished_text "
           "FROM transcriptions WHERE summary_json IS NULL "
           "AND source_type IS NOT 'telegram' AND deleted_at IS NULL "
           "AND duplicate_of IS NULL")
    params: list = []
    if kind != "all":
        types = _KIND_SOURCE_TYPES.get(kind)
        if not types:
            raise ValueError(f"unknown --kind: {kind!r}")
        sql += f" AND source_type IN ({','.join('?' * len(types))})"
        params.extend(types)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _dry_run_report(rows: list[dict], model: str) -> dict:
    """Estimate of volume/cost without any writes. tokens ~ characters/4 (rough,
    no real tokenizer); the cost is input only (output is unknown in advance)."""
    lengths = [
        (r["id"], len(r.get("polished_text") or r.get("transcript_text") or ""))
        for r in rows
    ]
    total_chars = sum(n for _, n in lengths)
    tokens_est = total_chars // 4
    cost_est = pricing.estimate_cost(model, tokens_est, 0)
    top10 = sorted(lengths, key=lambda t: -t[1])[:10]
    return {
        "dry_run": True, "count": len(rows), "model": model,
        "total_chars": total_chars, "tokens_est": tokens_est,
        "cost_est_usd_input_only": cost_est,
        "top10": [{"id": tid, "chars": n} for tid, n in top10],
    }


def backfill_cards(db_path: str, model: Optional[str] = None,
                   limit: Optional[int] = None, kind: str = "all") -> dict:
    """The real CLI pass: card phase only (`_enrich_card`), NOT embed.

    Idempotent through the selection itself (`summary_json IS NULL`) — a rerun
    with no new records processes 0. A Claude failure on one record does not break the pass:
    `_enrich_card` already catches it and returns "retry_needed" (the record stays in
    the selection for the next pass), here we catch only unexpected errors.
    """
    rows = _select_backfill_card_rows(db_path, kind=kind, limit=limit)
    total = len(rows)
    done = skipped = failed = 0
    logger.info("[backfill-cards] start: %d records (kind=%s, model=%s)",
                total, kind, model or models.get_default_model())
    for r in rows:
        try:
            res = _enrich_card(db_path, r["id"], model=model)
            if res.get("status") == "enriched":
                done += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            logger.error("[backfill-cards] tx=%s failed: %s", r["id"], e, exc_info=True)
    result = {"total": total, "done": done, "skipped": skipped, "failed": failed}
    logger.info("[backfill-cards] finished: %s", result)
    return result


def _print_dry_run_report(report: dict) -> None:
    print(f"Records without a card: {report['count']}")
    print(f"Total characters: {report['total_chars']}")
    print(f"Token estimate (characters/4): {report['tokens_est']}")
    print(f"Input-only cost estimate ({report['model']}): "
          f"${report['cost_est_usd_input_only']:.4f}")
    print("Top 10 longest:")
    for item in report["top10"]:
        print(f"  #{item['id']}: {item['chars']} characters")


def _cmd(args) -> int:
    if args.command != "backfill-cards":  # pragma: no cover
        return 2
    eff_model = args.model or models.get_default_model()
    if args.dry_run:
        rows = _select_backfill_card_rows(args.db, kind=args.kind, limit=args.limit)
        _print_dry_run_report(_dry_run_report(rows, eff_model))
        return 0
    result = backfill_cards(args.db, model=args.model, limit=args.limit, kind=args.kind)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(
        prog="enrichment",
        description="Offline backfill of cards (summary/entities/action items) "
                    "for calls and documents — WITHOUT the embed phase.")
    p.add_argument("--db", default=default_db)
    # --dry-run must work from anywhere on the line (as in dedup_audio.py):
    # the top-level flag goes to a separate dest, the final value is the OR of both.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Write nothing, only estimate volume/cost")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="Write nothing, only estimate volume/cost")
    common.add_argument("--model", default=None,
                        help="Override the Claude model (default — models.get_default_model())")
    common.add_argument("--limit", type=int, default=None)
    common.add_argument("--kind", choices=("calls", "docs", "all"), default="all",
                        help="calls=file/youtube/recording, docs=document, all=both")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "backfill-cards", parents=[common],
        help="Claude card for records without summary_json (not Telegram/duplicates/deleted)")

    args = p.parse_args(argv)
    args.dry_run = args.dry_run or args.dry_run_pre
    return _cmd(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
