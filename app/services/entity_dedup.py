"""Entity dedup — offline embedding-based fuzzy-merge (Wave 4, T6.7).

**The problem.** `enrichment._normalize`/`_find_entity`/`_upsert_entity` deduplicate
entities (`entities`) only by an exact normalized string (lowercase + trimmed
punctuation) + explicit aliases that Claude gave in a single call. So «Олена Петренко»
in one call and «Олена» / «Петренко» in another — without explicit
aliasing — stay SEPARATE entities: the graph (the main RAG feature — "who
promised what") gets cluttered with duplicates, and mention_count/meeting_count
are smeared across several ids instead of accumulating on one.

**The solution here is a periodic OFFLINE pass (NOT inline in enrich, so as not to slow
the main enrichment path):**

1. `find_merge_candidates()` — for each `type` separately (person/project/org/
   topic are NOT mixed) computes the embedding of canonical names (reuses
   `app.services.embeddings.embed_texts` — the same e5 embedder as the transcript
   chunks; NO new model is loaded), computes pairwise
   cosine similarity (vectors from `embed_texts` are already L2-normalized → cosine =
   dot product) and returns pairs above the threshold. **Only reads the DB, changes
   nothing.**
2. `merge_entities(keep_id, merge_id)` — an explicit merge of ONE specific pair.
   Called only after confirmation (a human reviewed the candidates).
   NOT automatic: no function here decides "merge" on its own, it only performs
   an already confirmed merge. The same principle as the soft speaker merge T4.6
   (Wave 2) — a false automerge risks merging different people.

**Idempotence.** `merge_entities` physically deletes `merge_id` from `entities`
(FK `ON DELETE CASCADE` cleans up the remaining `entity_aliases`/`meeting_entities`
of that entity). A repeated `find_merge_candidates` pass simply no longer
sees the already merged entity as a candidate — it is deleted. A repeated
`merge_entities` with the same (already nonexistent) `merge_id` is a no-op
(`{"status": "not_found"}`), it breaks nothing.

**Call point — the CLI, separate from app.py/enrich (NOT inline):**

    .venv/Scripts/python.exe -m app.services.entity_dedup list
    .venv/Scripts/python.exe -m app.services.entity_dedup list --type person --threshold 0.9
    .venv/Scripts/python.exe -m app.services.entity_dedup list --json
    .venv/Scripts/python.exe -m app.services.entity_dedup merge 12 47        # asks y/n
    .venv/Scripts/python.exe -m app.services.entity_dedup merge 12 47 --yes  # non-interactive

**Deliberately NOT wired as an MCP tool in this wave** (a note, not an oversight):
(a) the owner set MCP to read-first — new WRITE tools are not added, and
`merge_entities` is a write action; (b) `mcp_server.py` on principle does not import
torch/embeddings in its process (memory: mcp-stdio-no-heavy-models) —
a correct read tool for finding candidates would require either breaking that
rule or a new `app.py`/blueprint proxy endpoint (the territory of another
worker of this wave, T7.4). The CLI stays the main call point for T6.7;
an MCP/HTTP wrapper can be added as a separate card later without changing this module.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from typing import Optional

from app.db.connection import get_db_connection
from app.services import embeddings
from app.services.enrichment import (
    _VALID_ENTITY_TYPES,
    _normalize,
    _recompute_entity_aggregates,
)

logger = logging.getLogger(__name__)

# Cosine-similarity threshold of the e5 embeddings of canonical names for a merge candidate.
# An empirical starting point (typical e5 sentence-similarity thresholds are 0.85-0.90 for
# "the same referent, different wording"); deliberately conservative (higher means
# fewer false positives), because a merge is confirmed by a human, not automatically.
DEFAULT_THRESHOLD = 0.86


# ============================================================
# 1. Candidate search (read-only)
# ============================================================

_NAME_PUNCT = re.compile(r"[\s\-_.'`ʼ’«»\"]+")

# Type "strength" order when choosing keep. It decides visibility, not importance:
# `scope.resolve_scope` takes project|org|person, so a row that became `topic`
# drops out of the project slice no matter how many links it has.
_TYPE_RANK = {"person": 0, "project": 1, "org": 2, "topic": 3}


def _fold_name(name: Optional[str]) -> str:
    """Spelling fold key. Computed in Python: SQLite LOWER does not touch Cyrillic."""
    return _NAME_PUNCT.sub("", (name or "").casefold().strip())


def find_cross_type_twins(db_path: str, *, include_person: bool = False) -> list[dict]:
    """Exact namesakes scattered across DIFFERENT types — no model and no threshold.

    `find_merge_candidates` compares only within a type, so it does not see the main class
    of duplicates: enrichment creates one thing separately in each type
    («Acmecorp» as project and as org). Here the whole spelling matches, so there is
    no guessing and no embeddings are needed.

    `keep` is chosen FIRST by type, and only then by number of links. Links still
    move to keep, so the choice only affects the type — and the
    type decides visibility: `scope.resolve_scope` takes `project|org|person` and does not
    see `topic`. When keep was chosen by links, seven projects in the live
    archive ended up as topic and vanished from `ask_archive(project=…)` and the copilot.

    "Person + non-person" pairs are not taken by the normal pass (`merge` requires a
    separate `--allow-person` for them after an eyeball check), but they do not hide the rest of the group either:
    from a group {person «Ткачук», org «Ткачук», project «Ткачук»} the pair
    org+project remains, which can and should be merged.
    """
    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT id, type, canonical_name FROM entities").fetchall()
        links = {r["id"]: r["n"] for r in conn.execute(
            "SELECT me.entity_id id, COUNT(DISTINCT me.transcription_id) n "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.deleted_at IS NULL GROUP BY me.entity_id")}

    groups: dict[str, list] = {}
    for r in rows:
        k = _fold_name(r["canonical_name"])
        if k:
            groups.setdefault(k, []).append(r)

    out: list[dict] = []
    for members in groups.values():
        if not include_person and any(r["type"] == "person" for r in members):
            # We drop precisely the person rows, not the whole group: otherwise «Ткачук»
            # would hide the org+project pair that can be merged.
            members = [r for r in members if r["type"] != "person"]
        types = {r["type"] for r in members}
        if len(members) < 2 or len(types) < 2:
            continue
        ordered = sorted(members, key=lambda r: (_TYPE_RANK.get(r["type"], 9),
                                                 -links.get(r["id"], 0)))
        out.append({
            "keep": {"id": ordered[0]["id"], "type": ordered[0]["type"],
                     "name": ordered[0]["canonical_name"],
                     "links": links.get(ordered[0]["id"], 0)},
            "merge": [{"id": r["id"], "type": r["type"], "name": r["canonical_name"],
                       "links": links.get(r["id"], 0)} for r in ordered[1:]],
        })
    out.sort(key=lambda g: -g["keep"]["links"])
    return out


def find_alias_cross_type_collisions(db_path: str, *, include_person: bool = False) -> list[dict]:
    """An alias of one entity whose spelling matches the canonical name
    of an entity of ANOTHER type — a class of duplicates that `find_cross_type_twins` does not see,
    because it reads only `canonical_name`. Example: «WYNN Дубай» is an alias of org
    «Wynn Casino Las Vegas» (#670) and at the same time the canonical name of project #2301.

    A spelling that folds (`_fold_name`, the same key as in `twins` —
    SQLite `LOWER` does not fold Cyrillic) in three or more DIFFERENT entities
    is returned separately with `"ambiguous": true` and is NOT offered for merging:
    one spelling cannot be credited to two (a direct prohibition from memory —
    do not confuse «Русланою» with «Руслан»/«Руслана»).

    Read-only, writes nothing to the DB. The merge itself is the existing `merge_entities(...,
    allow_cross_type=True)`, here only detection + `evidence`.
    """
    with get_db_connection(db_path) as conn:
        entities = conn.execute("SELECT id, type, canonical_name FROM entities").fetchall()
        aliases = conn.execute("SELECT entity_id, alias FROM entity_aliases").fetchall()
        links = {r["id"]: r["n"] for r in conn.execute(
            "SELECT me.entity_id id, COUNT(DISTINCT me.transcription_id) n "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.deleted_at IS NULL GROUP BY me.entity_id")}

    entities_by_id = {r["id"]: r for r in entities}

    canon_by_key: dict[str, list] = {}
    for r in entities:
        k = _fold_name(r["canonical_name"])
        if k:
            canon_by_key.setdefault(k, []).append(r["id"])

    alias_by_key: dict[str, list] = {}
    for a in aliases:
        k = _fold_name(a["alias"])
        if k:
            alias_by_key.setdefault(k, []).append((a["entity_id"], a["alias"]))

    def _rec(entity_id: int) -> dict:
        r = entities_by_id[entity_id]
        return {"id": entity_id, "type": r["type"], "name": r["canonical_name"],
                "links": links.get(entity_id, 0)}

    out: list[dict] = []
    for k, alist in alias_by_key.items():
        canon_ids = canon_by_key.get(k, [])
        if not canon_ids:
            continue  # no canonical collision — not our class (an alias on itself)
        involved = set(canon_ids) | {eid for eid, _alias in alist}
        if not include_person:
            involved = {eid for eid in involved if entities_by_id[eid]["type"] != "person"}
        if len(involved) < 2:
            continue
        types = {entities_by_id[eid]["type"] for eid in involved}
        if len(types) < 2:
            continue  # all participants are of the same type — not a cross-type class
        ev_owner, ev_alias = next(((eid, al) for eid, al in alist if eid in involved), alist[0])
        group: dict = {"key": k, "evidence": {"alias_of": ev_owner, "alias": ev_alias}}
        if len(involved) > 2:
            group["ambiguous"] = True
            group["members"] = [_rec(eid) for eid in sorted(involved)]
            out.append(group)
            continue
        ordered = sorted((_rec(eid) for eid in involved),
                          key=lambda r: (_TYPE_RANK.get(r["type"], 9), -r["links"]))
        group["keep"] = ordered[0]
        group["merge"] = ordered[1:]
        out.append(group)

    out.sort(key=lambda g: -g["keep"]["links"] if "keep" in g else 0)
    return out


def find_merge_candidates(
    db_path: str,
    etype: Optional[str] = None,
    threshold: float = DEFAULT_THRESHOLD,
    limit: Optional[int] = None,
) -> list[dict]:
    """Merge candidates by embedding similarity of canonical names, WITHIN ONE
    `type` (person with person, project with project, ...). Writes nothing to the DB.

    Returns: a list of {"type","id1","name1","id2","name2","similarity"},
    sorted by similarity desc (the most likely duplicates first).
    """
    if not embeddings.is_available():
        raise RuntimeError(
            f"embeddings unavailable: {embeddings.unavailability_reason()}"
        )

    types = [etype] if etype else list(_VALID_ENTITY_TYPES)
    candidates: list[dict] = []

    with get_db_connection(db_path) as conn:
        for t in types:
            rows = conn.execute(
                "SELECT id, canonical_name FROM entities WHERE type = ? ORDER BY id",
                (t,),
            ).fetchall()
            if len(rows) < 2:
                continue
            ids = [r["id"] for r in rows]
            names = [r["canonical_name"] for r in rows]
            vecs = embeddings.embed_texts(names)  # [N, dim], L2-normalized
            if vecs.shape[0] != len(ids):
                logger.warning("[entity_dedup] embed_texts returned %d vectors for %d names (type=%s) — skipping",
                               vecs.shape[0], len(ids), t)
                continue
            sims = vecs @ vecs.T  # cosine, because normalized
            n = len(ids)
            for i in range(n):
                for j in range(i + 1, n):
                    sim = float(sims[i, j])
                    if sim >= threshold:
                        candidates.append({
                            "type": t,
                            "id1": ids[i], "name1": names[i],
                            "id2": ids[j], "name2": names[j],
                            "similarity": round(sim, 4),
                        })

    candidates.sort(key=lambda c: c["similarity"], reverse=True)
    if limit:
        candidates = candidates[:limit]
    return candidates


# ============================================================
# 2. Merge (explicit, confirmed)
# ============================================================

def _first_token_person_twin(c, gone, *, exclude_id: int):
    """A single-word person namesake who would receive future tasks of the vanished name.

    Mirrors the fallback of `commitments.link_owners`: «Юля Гончар» without its own row
    is looked up as «юля». Here it is the same, but in advance — to see the harm BEFORE the merge,
    not a month later in the digest.
    """
    first = (gone["normalized_name"] or "").split(" ")[0]
    if not first or first == gone["normalized_name"]:
        return None                      # a single-word name is not caught by the fallback anyway
    return c.execute(
        "SELECT id, type, canonical_name FROM entities "
        "WHERE type = 'person' AND normalized_name = ? AND id <> ?",
        (first, exclude_id)).fetchone()


def merge_entities(db_path: str, keep_id: int, merge_id: int,
                   allow_cross_type: bool = False,
                   allow_person: bool = False) -> dict:
    """An explicit merge of ONE confirmed pair: `merge_id` disappears, `keep_id`
    inherits aliases, meeting links (mention_count is SUMMED when
    they intersect on the same meeting, salience — max), action_items, and
    role/description/speaker_id if keep did not have them yet. The aggregates
    (`mention_count`/`meeting_count`) on `keep_id` are recomputed from scratch
    via `enrichment._recompute_entity_aggregates` (idempotent).

    `allow_cross_type` lifts the ban on different types. The ban is right for
    embedding candidates (there name similarity says nothing about the nature of a thing), but
    blind to the main class of duplicates in this archive: enrichment creates ONE thing
    separately in each type, so «Acmecorp» lives as a project and as an org. There are
    133 such groups out of 152, and this merge takes none of them without the flag. The type stays from
    `keep_id` — so keep must be chosen by number of links, not by id.

    In a cross-type merge mention_count on a SHARED meeting is taken as
    MAX, not the sum: it is the same name in the same text, seen by two
    graph rows, so adding would count the same mentions twice. Within one
    type the behavior is unchanged — there a sum stays a sum.

    `allow_person` lifts the ban on "person + non-person" — and only after
    the pair was looked at with eyes. The ban exists because most such pairs are
    DIFFERENT things under one spelling (the biblical Eve and the firm «Єва»), but a minority
    really are one: an agency named after its owner's surname («Ткачук»), a crew
    registered both as a person and as an organization. The flag does not switch off the protection, but
    moves it to verification conditions: the person row that DISAPPEARS must not
    have either tasks or a bound speaker — those are what the harm came through
    (`link_owners`, `speakers._entity_index`). Spellings that enter the person index
    for the first time are returned in `person_index_added`, so the decision is
    visible in the log, not only in its consequences.

    Returns {"status": "merged"|"not_found"|"error", ...}.
    """
    if keep_id == merge_id:
        return {"status": "error", "error": "keep_id == merge_id"}

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        keep = c.execute("SELECT * FROM entities WHERE id = ?", (keep_id,)).fetchone()
        merge = c.execute("SELECT * FROM entities WHERE id = ?", (merge_id,)).fetchone()
        if not keep or not merge:
            return {"status": "not_found", "keep_id": keep_id, "merge_id": merge_id}
        cross_type = keep["type"] != merge["type"]
        if cross_type and not allow_cross_type:
            return {"status": "error",
                    "error": f"type mismatch: keep={keep['type']} merge={merge['type']}"}
        # A person and a non-person are NEVER merged, even with the flag. «Ткачук»
        # is both a person and an agency; «Вербич» is both a person and a brand. Besides, both
        # directions spoil the person indexes: a project name, becoming an alias of a person,
        # binds a speaker to a non-matching entity (`speakers._entity_index`),
        # and a person's name, moving into project, vanishes from the `link_owners` index —
        # and that person's tasks are left without an owner.
        report_person_index = False
        if cross_type and "person" in (keep["type"], merge["type"]):
            if not allow_person:
                return {"status": "error",
                        "error": (f"person merge refused: keep={keep['type']} "
                                  f"merge={merge['type']} — merging a person with a non-person "
                                  f"is done only manually, after an eyeball check")}
            if merge["type"] == "person":
                # The person row disappears: we check exactly what used to break.
                owned = c.execute(
                    "SELECT COUNT(*) FROM action_items WHERE owner_entity_id = ?",
                    (merge_id,)).fetchone()[0]
                if owned or merge["speaker_id"] is not None:
                    return {"status": "error",
                            "error": (f"person merge refused: #{merge_id} has "
                                      f"{owned} tasks and speaker_id="
                                      f"{merge['speaker_id']} — they would lose "
                                      f"their person. First re-hang them onto keep")}
                # A row that is empty now does not mean there will be no harm LATER:
                # the name leaves the `link_owners` person index, and the next task
                # «Вербич Петренко» will go through the first-token fallback — just to the
                # single-word namesake. This is the same defect through which 43 tasks of the
                # owner went out under someone else's name, and `relink` does not cure it
                # (its index is person-only too). So with a live namesake — refuse.
                twin = _first_token_person_twin(c, merge, exclude_id=keep_id)
                if twin:
                    return {"status": "error",
                            "error": (f"person merge refused: the name '{merge['canonical_name']}' "
                                      f"will leave the person index, and future tasks with it will be "
                                      f"given by the first-token fallback to the namesake "
                                      f"[{twin['type']}] #{twin['id']} '{twin['canonical_name']}'. "
                                      f"First remove the namesake or rename")}
            else:
                # The person row stays: other people's spellings ENTER the person index.
                # We count AFTER the transfer — see below: some spellings may
                # stay with a third entity (normalized_alias is UNIQUE globally),
                # and a report on "entered" would be untrue.
                report_person_index = True

        now = datetime.now().isoformat(timespec="seconds")

        # We collect the absorbed row's spellings BEFORE moving the aliases — after it
        # they already belong to keep. They are needed for the merge "stickiness"
        # (`enrichment._find_merged_entity`), which checks exactly the spellings: without them
        # a match of the TYPE alone was enough, and a foreign entity of the same type was
        # silently absorbed on the next enrich.
        gone_aliases = sorted({merge["normalized_name"]} | {
            r["normalized_alias"] for r in c.execute(
                "SELECT normalized_alias FROM entity_aliases WHERE entity_id = ?",
                (merge_id,))})

        # --- aliases: all existing aliases of merge_id -> aliases of keep_id, + canonical_name
        # of merge_id also becomes an alias of keep_id. UPDATE OR IGNORE (not DELETE+INSERT):
        # normalized_alias is UNIQUE globally — if keep_id already has the same alias
        # (another entity_id), that particular row stays on merge_id and will be
        # removed below via CASCADE when the entities row of merge_id is deleted —
        # without losing information, because keep already has this alias.
        c.execute(
            "UPDATE OR IGNORE entity_aliases SET entity_id = ? WHERE entity_id = ?",
            (keep_id, merge_id),
        )
        c.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized_alias) "
            "VALUES (?, ?, ?)",
            (keep_id, merge["canonical_name"], merge["normalized_name"]),
        )

        person_index_added: list[str] = []
        if report_person_index:
            # Only what REALLY landed in keep: in a race for a globally unique
            # spelling the absorbed row may have left empty-handed.
            person_index_added = [
                r["alias"] for r in c.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_id = ? "
                    "AND normalized_alias IN "
                    f"({', '.join('?' * len(gone_aliases))})",
                    (keep_id, *gone_aliases))]

        # --- meeting_entities: move, summing on conflict at the same meeting ---
        # We move `source` together with the row: `tg_entities` cleans up ITS links
        # with the query `DELETE … WHERE source = ?`, and a row that lost its origin
        # survives a rebuild as a ghost, and `tg_entities.stats()` credits it
        # to Claude. In an archive where 88% is Telegram, this is not a small thing.
        me_rows = c.execute(
            "SELECT transcription_id, mention_count, salience, role_in_meeting, source "
            "FROM meeting_entities WHERE entity_id = ?",
            (merge_id,),
        ).fetchall()
        # MAX instead of a sum — when the spelling is ONE and the same, just registered twice
        # («Acmecorp»[project] and «AcmeCorp»[org]): in the text these are the same
        # words, seen by two graph rows. What decides is the name match, not
        # the fact of different types: «Acmecorp» and «Acmecorp Ltd» are different rows that
        # Claude counted separately, so there a sum stays a sum. Otherwise mention_count
        # would be understated, and it drags ranking and "from N mentions" filters along.
        # The same fold key as in `find_cross_type_twins`: `normalized_name`
        # keeps inner spaces and punctuation, so «Acme Corp» and «AcmeCorp»
        # were shown by the finder as one group, but here were considered different — and mentions
        # were summed where they should have been taken as MAX.
        same_name = _fold_name(keep["canonical_name"]) == _fold_name(merge["canonical_name"])
        on_conflict = ("mention_count = MAX(mention_count, excluded.mention_count), "
                       if same_name else
                       "mention_count = mention_count + excluded.mention_count, ")
        for r in me_rows:
            c.execute(
                "INSERT INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, salience, role_in_meeting, source) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(transcription_id, entity_id) DO UPDATE SET "
                + on_conflict +
                # We do NOT touch the keep row's origin. `source IS NULL` here means
                # not "unknown" but "a link from Claude", and `tg_entities` deliberately
                # does not touch such as more reliable. Overwriting with 'telegram' would put
                # the row under `DELETE … WHERE source='telegram'`, and a rebuild
                # would recreate it bare: without salience, role and accumulated mentions.
                "salience = MAX(COALESCE(salience, 0), COALESCE(excluded.salience, 0))",
                (r["transcription_id"], keep_id, r["mention_count"], r["salience"],
                 r["role_in_meeting"], r["source"]),
            )

        # --- action_items: reassign the owner ---
        c.execute(
            "UPDATE action_items SET owner_entity_id = ? WHERE owner_entity_id = ?",
            (keep_id, merge_id),
        )

        # --- fill keep with merge's fields if empty ---
        if not keep["role"] and merge["role"]:
            c.execute("UPDATE entities SET role = ? WHERE id = ?", (merge["role"], keep_id))
        if not keep["description"] and merge["description"]:
            c.execute("UPDATE entities SET description = ? WHERE id = ?",
                      (merge["description"], keep_id))
        if not keep["speaker_id"] and merge["speaker_id"]:
            c.execute("UPDATE entities SET speaker_id = ? WHERE id = ?",
                      (merge["speaker_id"], keep_id))

        # --- audit trail in metadata_json (best-effort) ---
        try:
            meta = json.loads(keep["metadata_json"]) if keep["metadata_json"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        merged_from = meta.get("merged_from") or []
        merged_from.append({"id": merge_id, "name": merge["canonical_name"],
                            "type": merge["type"], "aliases": gone_aliases, "at": now})
        meta["merged_from"] = merged_from
        c.execute(
            "UPDATE entities SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), now, keep_id),
        )

        # --- delete merge_id: CASCADE cleans up the remaining aliases/meeting_entities ---
        c.execute("DELETE FROM entities WHERE id = ?", (merge_id,))

        _recompute_entity_aggregates(c, {keep_id})
        conn.commit()

    logger.info("[entity_dedup] merged id=%s ('%s') -> id=%s ('%s')",
                merge_id, merge["canonical_name"], keep_id, keep["canonical_name"])
    out = {
        "status": "merged",
        "keep_id": keep_id, "merge_id": merge_id,
        "kept_name": keep["canonical_name"], "merged_name": merge["canonical_name"],
    }
    if person_index_added:
        out["person_index_added"] = person_index_added
        logger.info("[entity_dedup] spellings entered the person index: %s",
                    ", ".join(person_index_added))
    return out


# ============================================================
# 3. Split — the inverse trouble: different people sit in one row
# ============================================================

# Minimum spelling length to count it as evidence in a text. A short one
# («Юр», «Юл») occurs as a substring in half the archive and cannot be evidence.
# The price of the threshold: when the entity's whole name is shorter than it («Юра»), its side of
# the evidence in the text is empty — so a link will move if only the detached spelling is proven.
# That is the intended behavior: we move only what is proven.
_EVIDENCE_MIN_LEN = 4


def _pattern(spelling: str) -> "re.Pattern":
    """A spelling from the START of a word, any ending allowed.

    A bare substring lied: the alias «Слав» was found in 160 texts, because it sits
    inside «Слава», «Славік» and «Вʼячеслав». The boundary is only at the front, not on both
    sides, because Ukrainian cases are suffixes: «Гринчука», «Вересу»,
    «Настею» must be credited to the spelling «Гринчук»/«Верес»/«Настя».
    """
    return re.compile(r"(?<!\w)" + re.escape(spelling.lower()))

# Separators of a compound owner («Юлія Бондаренка, Дмитро Лебідь», «Юля / Діма»):
# such a row points to two people, and it is evidence for neither side.
_OWNER_SPLIT = re.compile(r"[,/;+&]| та | і | and ", re.IGNORECASE)


def _alias_rows(c, entity_id: int) -> list:
    return c.execute(
        "SELECT id, alias, normalized_alias FROM entity_aliases WHERE entity_id = ? "
        "ORDER BY LENGTH(alias) DESC", (entity_id,)).fetchall()


def _evidence_pairs(spellings: list[str]) -> list[tuple[str, "re.Pattern"]]:
    """Spellings long enough to be evidence — from long to short."""
    return [(s, _pattern(s))
            for s in sorted(set(spellings), key=len, reverse=True)
            if len(s) >= _EVIDENCE_MIN_LEN]


def _winning_occurrences(text_low: str, pairs: list[tuple[str, "re.Pattern"]]) -> list[str]:
    """Who owns each mention in a text: at a position the LONGEST spelling wins.

    One mention cannot be credited to two. «Слава Гринчук показав макет» is
    evidence of «Гринчук», not of the presence of «Слава»: the short name here is only part of
    the full one. And vice versa, «Юлія Бондаренка підтвердила» is evidence of «Бондаренка», not of
    any «Юлія». So both sides compete for the same text position, and
    the longer spelling takes it together with the shorter ones inside it.

    Erasing one side before searching for the other (the first approach) broke
    exactly here: the detached «Юлія» erased «Юлія Бондаренка» with itself, the evidence for
    the one who stays vanished — and someone else's meeting moved.
    """
    hits = sorted((m.start(), -len(s), s)
                  for s, pat in pairs for m in pat.finditer(text_low))
    won: list[str] = []
    end = -1
    for start, neg_len, spelling in hits:
        if start < end:          # covered by a longer match — the same mention
            continue
        end = start - neg_len
        won.append(spelling)
    return won


def inspect_entity(db_path: str, entity_id: int) -> dict:
    """Evidence of "who exactly" inside one graph row. Changes nothing.

    Shows for each alias: how many tasks are named by exactly this spelling
    (`action_items.owner_name` — the raw name from the transcript, it matches the
    alias in ~96% of rows) and in how many linked texts this spelling is seen.
    This is the material for the `split_entity` decision: the row «Слава» holds both
    `Слава Гринчук`, and `Вʼячеслав Верес`, and a foreign `Stepan` — but they can be separated
    only where the spelling named itself.
    """
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        ent = c.execute(
            "SELECT id, type, canonical_name, role, description, mention_count, "
            "meeting_count FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not ent:
            return {"status": "not_found", "entity_id": entity_id}
        aliases = _alias_rows(c, entity_id)
        tasks = c.execute(
            "SELECT ai.id, ai.owner_name, ai.status FROM action_items ai "
            "JOIN transcriptions t ON t.id = ai.transcription_id "
            "WHERE ai.owner_entity_id = ? AND t.deleted_at IS NULL", (entity_id,)).fetchall()
        links = c.execute(
            "SELECT me.transcription_id tid, COALESCE(t.transcript_text, '') txt "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ? AND t.deleted_at IS NULL", (entity_id,)).fetchall()

    # The canonical name is a spelling too, and there may be NO row in `entity_aliases` for it:
    # `_upsert_entity` creates it via INSERT OR IGNORE against the
    # global UNIQUE, so when another entity has already taken the spelling (the same
    # trouble this module exists for), the alias was not created. Without it the row's own
    # tasks would look like "owner outside the aliases", while `split_entity`
    # still counts the canonical name among those who stay — the numbers would diverge.
    spellings = [r["alias"] for r in aliases]
    if _fold_name(ent["canonical_name"]) not in {_fold_name(a) for a in spellings}:
        spellings.insert(0, ent["canonical_name"])
    by_alias: dict[str, dict] = {a: {"tasks": 0, "texts": 0} for a in spellings}
    fold_to_alias: dict[str, str] = {}
    for a in spellings:                       # the longer spelling wins the fold key
        fold_to_alias.setdefault(_fold_name(a), a)

    ambiguous_owners = 0
    unmatched_owners: dict[str, int] = {}
    for t in tasks:
        owner = (t["owner_name"] or "").strip()
        hit = fold_to_alias.get(_fold_name(owner))
        if hit:
            by_alias[hit]["tasks"] += 1
        elif _OWNER_SPLIT.search(owner):
            ambiguous_owners += 1
        else:
            unmatched_owners[owner] = unmatched_owners.get(owner, 0) + 1

    text_only_short = 0
    pairs = _evidence_pairs(spellings)
    for r in links:
        # The same attribution as in `split_entity`: «Слава Гринчук» is the row of
        # «Гринчук», not evidence of both. Otherwise `inspect` would promise texts to a spelling
        # that split will not hand over, and the numbers of the two tools would diverge.
        seen = set(_winning_occurrences(r["txt"].lower(), pairs))
        for s in seen:
            by_alias[s]["texts"] += 1
        if not seen:
            text_only_short += 1

    return {
        "status": "ok",
        "entity": {"id": ent["id"], "type": ent["type"], "name": ent["canonical_name"],
                   "role": ent["role"], "mention_count": ent["mention_count"],
                   "meeting_count": ent["meeting_count"]},
        "tasks_total": len(tasks),
        "links_total": len(links),
        # Links where no long spelling is visible — they rest on a short
        # name and have no evidence. Split does not move them, and this is visible in advance.
        "links_short_name_only": text_only_short,
        "tasks_ambiguous_owner": ambiguous_owners,
        "tasks_unmatched_owner": sorted(unmatched_owners.items(), key=lambda kv: -kv[1]),
        "aliases": [{"alias": a, "tasks": by_alias[a]["tasks"],
                     "texts": by_alias[a]["texts"],
                     "evidence": len(a) >= _EVIDENCE_MIN_LEN}
                    for a in spellings],
    }


def split_entity(db_path: str, source_id: int, move_aliases: list[str], *,
                 target_id: Optional[int] = None, new_name: Optional[str] = None,
                 drop: bool = False, dry_run: bool = True) -> dict:
    """Detach spellings from an entity — together with the tasks and links that
    provably belong to them. The inverse of `merge_entities`.

    Needed where enrichment glued DIFFERENT people into one row: id 147
    «Дмитро» holds both `Дмитро Лебідь` and `М.Верес` — two living people, and 40 tasks hang on
    the row. Merge does not help here, and without detaching the defect is not
    cosmetic but active: `entity_aliases.normalized_alias` is UNIQUE GLOBALLY,
    so a foreign spelling among the aliases intercepts a foreign person for the future too —
    `enrichment._find_entity` will give «Слава» to the person «Stepan», because `Stepan`
    lies in the aliases of «Слава».

    The destination is one of three:
      * `target_id` — an existing entity of THE SAME type (`Stepan` → id 459 «Степан»);
      * `new_name` — a new entity of the source's type (`Юра Литвин` separately from `Юра`);
      * `drop=True` — the spelling is trash, not a person (`JSON` at «Джейсон»,
        `EULA` at «Юлія»): the alias disappears, tasks with such an owner are left without an
        owner (NULL — more honest than a wrong owner), proven links
        are deleted.

    **Evidence, not a guess.** A task moves only if `owner_name` is exactly
    the detached spelling. A link moves only if the detached spelling is visible in the text
    and NONE of those that stay is visible; if both sides are visible — the link is COPIED
    (there really are two in the text), not moved;
    if nothing is visible (a mention by the short name) — it stays with the source. That is why
    `dry_run` is the default: first we look at the numbers.

    Returns {"status": "ok"|"error"|"not_found", ...} with counts.
    """
    if not move_aliases:
        return {"status": "error", "error": "move_aliases is empty"}
    modes = [target_id is not None, bool(new_name), drop]
    if sum(1 for m in modes if m) != 1:
        return {"status": "error",
                "error": "EXACTLY one destination is required: target_id | new_name | drop"}
    if target_id is not None and target_id == source_id:
        return {"status": "error", "error": "target_id == source_id"}

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        src = c.execute("SELECT * FROM entities WHERE id = ?", (source_id,)).fetchone()
        if not src:
            return {"status": "not_found", "source_id": source_id}
        tgt = None
        if target_id is not None:
            tgt = c.execute("SELECT * FROM entities WHERE id = ?", (target_id,)).fetchone()
            if not tgt:
                return {"status": "not_found", "target_id": target_id}
            # We do not change the type even with a flag: a person's alias, moving into
            # project, vanishes from the person indexes (`speakers._entity_index`,
            # `commitments.link_owners`) — the same defect as in merge.
            if tgt["type"] != src["type"]:
                return {"status": "error",
                        "error": f"type mismatch: source={src['type']} target={tgt['type']}"}

        rows = _alias_rows(c, source_id)
        want = {_fold_name(a) for a in move_aliases if _fold_name(a)}
        moving = [r for r in rows if _fold_name(r["alias"]) in want]
        staying = [r for r in rows if _fold_name(r["alias"]) not in want]
        found = {_fold_name(r["alias"]) for r in moving}
        missing = sorted(a for a in move_aliases if _fold_name(a) not in found)
        if not moving:
            return {"status": "error",
                    "error": f"none of the spellings is among the aliases of #{source_id}: {missing}"}
        # The canonical name stays with the source: it holds `normalized_name`, by which
        # `_find_entity` finds the row, and UNIQUE(type, normalized_name). Taking
        # it away is a rename of the entity, a different operation.
        if _fold_name(src["canonical_name"]) in want:
            return {"status": "error",
                    "error": (f"'{src['canonical_name']}' is the canonical name of #{source_id}; "
                              "it cannot be detached (that is a rename, not a split)")}
        # An empty `staying` is NOT a rename: the canonical name always stays
        # (the check above), and there may be no row in `entity_aliases` for it —
        # `_upsert_entity` creates it via INSERT OR IGNORE against the global
        # UNIQUE and loses the race to another entity. Exactly such a row — «Дмитро» with
        # the single alias `М.Верес` — is what split exists for; a guard on
        # `not staying` would refuse precisely this case and no other.

        moving_spellings = [r["alias"] for r in moving]
        staying_spellings = [r["alias"] for r in staying] + [src["canonical_name"]]

        # We check the destination BEFORE the estimate, not right before the write: otherwise
        # dry-run promises a split that `--apply` then rejects ("already exists #N"),
        # while the estimate is the only surface on which the operator decides.
        canonical = norm = None
        if new_name:
            canonical = new_name.strip()
            norm = _normalize(canonical)
            if not norm:
                return {"status": "error", "error": "new_name is empty after normalization"}
            clash = c.execute(
                "SELECT id FROM entities WHERE type = ? AND normalized_name = ?",
                (src["type"], norm)).fetchone()
            if clash:
                return {"status": "error",
                        "error": (f"[{src['type']}] '{canonical}' already exists (#{clash['id']}) "
                                  f"— use target_id={clash['id']}")}

        # Softly deleted meetings are taken on a par with live ones — just like
        # `merge_entities`. `POST /api/transcription/<id>/restore` brings them back to
        # life, and if split bypassed them, after a restore the task would stay with
        # the old owner, and the link would lack the spelling that held it.
        # In the estimate numbers they are visible separately: these meetings are not in the UI right now.
        # --- tasks: evidence is the raw name from the transcript ---
        moving_folds = {_fold_name(r["alias"]) for r in moving}
        task_rows = c.execute(
            "SELECT ai.id, ai.owner_name, t.deleted_at FROM action_items ai "
            "JOIN transcriptions t ON t.id = ai.transcription_id "
            "WHERE ai.owner_entity_id = ?", (source_id,)).fetchall()
        moving_tasks = [r for r in task_rows
                        if _fold_name(r["owner_name"] or "") in moving_folds]
        task_ids = [r["id"] for r in moving_tasks]

        # --- links: evidence is the spelling in the meeting text ---
        link_rows = c.execute(
            "SELECT me.transcription_id tid, me.mention_count, me.salience, "
            "me.role_in_meeting, me.source, t.deleted_at, "
            "COALESCE(t.transcript_text, '') txt "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ?", (source_id,)).fetchall()
        # The pairs are shared by both sides: they compete for the same text
        # positions, and each mention is taken by exactly one (see `_winning_occurrences`).
        evidence_pairs = _evidence_pairs(moving_spellings + staying_spellings)
        moving_evidence = {s for s in moving_spellings if len(s) >= _EVIDENCE_MIN_LEN}
        to_move, to_copy = [], []
        mine_mentions: dict[int, int] = {}
        for r in link_rows:
            won = _winning_occurrences(r["txt"].lower(), evidence_pairs)
            mine = [s for s in won if s in moving_evidence]
            theirs = [s for s in won if s not in moving_evidence]
            if mine and not theirs:
                to_move.append(r)
            elif mine and theirs:
                to_copy.append(r)
                mine_mentions[r["tid"]] = len(mine)

        # In `drop` mode no copies are made — the shared text simply stays
        # with the source. Counting it "copied" would show links_left=0
        # where the links have not gone anywhere; the estimate is the only decision surface.
        copied = [] if drop else to_copy
        deleted_links = sum(1 for r in to_move + copied if r["deleted_at"])
        deleted_tasks = sum(1 for r in moving_tasks if r["deleted_at"])
        report = {
            "status": "ok",
            "dry_run": dry_run,
            "source": {"id": source_id, "type": src["type"], "name": src["canonical_name"]},
            "target": ({"id": tgt["id"], "name": tgt["canonical_name"]} if tgt else
                       ({"new_name": new_name, "type": src["type"]} if new_name else
                        {"drop": True})),
            "aliases_moved": moving_spellings,
            "aliases_not_found": missing,
            "tasks_moved": len(task_ids),
            "links_moved": len(to_move),
            "links_copied": len(copied),
            "links_left": len(link_rows) - len(to_move) - len(copied),
            # How much of the counted comes from meetings in the trash: they are not in the UI now,
            # but after a restore they must already be under the right owner.
            "from_deleted_meetings": {"tasks": deleted_tasks, "links": deleted_links},
            # In drop the shared text stays with the source and is part of links_left.
            "links_shared_kept": len(to_copy) if drop else 0,
        }
        if dry_run:
            return report

        now = datetime.now().isoformat(timespec="seconds")

        # --- destination: create a new entity if asked ---
        if new_name:
            cur = c.execute(
                "INSERT INTO entities (type, canonical_name, normalized_name, "
                "first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (src["type"], canonical, norm, now, now))
            target_id = cur.lastrowid
            report["target"] = {"id": target_id, "name": canonical, "created": True}

        alias_ids = [r["id"] for r in moving]
        marks = ",".join("?" for _ in alias_ids)
        if drop:
            c.execute(f"DELETE FROM entity_aliases WHERE id IN ({marks})", alias_ids)
        else:
            # A plain UPDATE without OR IGNORE: `normalized_alias` is UNIQUE GLOBALLY, so
            # two rows with one spelling in different entities cannot exist — there is no conflict
            # here. If the index were ever dropped, IntegrityError would break the whole
            # transaction, and that is better than an alias silently stuck with the source.
            c.execute(f"UPDATE entity_aliases SET entity_id = ? WHERE id IN ({marks})",
                      [target_id, *alias_ids])

        # --- tasks ---
        if task_ids:
            tmarks = ",".join("?" for _ in task_ids)
            # NULL, not source: without an owner a task is visible among the "ownerless", while with a
            # wrong owner it lies in the digest.
            c.execute(f"UPDATE action_items SET owner_entity_id = ? WHERE id IN ({tmarks})",
                      [None if drop else target_id, *task_ids])

        # --- links ---
        moved_tids = [r["tid"] for r in to_move]
        if drop:
            if moved_tids:
                lmarks = ",".join("?" for _ in moved_tids)
                c.execute(f"DELETE FROM meeting_entities WHERE entity_id = ? "
                          f"AND transcription_id IN ({lmarks})", [source_id, *moved_tids])
        else:
            # A moved link carries its numbers as is — the text speaks only of the detached
            # person, so the row was hers from the start. The copy gets exactly as many
            # mentions as ITS spellings won: in a shared text the two graph rows look at
            # different words, and taking the other's sum would mean attributing the source's
            # mentions to the destination.
            for r, mentions in (
                [(r, r["mention_count"]) for r in to_move]
                + [(r, mine_mentions[r["tid"]]) for r in to_copy]
            ):
                c.execute(
                    "INSERT INTO meeting_entities (transcription_id, entity_id, "
                    "mention_count, salience, role_in_meeting, source) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(transcription_id, entity_id) DO UPDATE SET "
                    # A sum, not MAX: the destination may already have a row for this meeting,
                    # earned by its OWN name, and here come the mentions of other words
                    # — the same rule as in `merge_entities` for different spellings
                    # (MAX there only when the spelling is one and the same).
                    "mention_count = mention_count + excluded.mention_count, "
                    "salience = MAX(COALESCE(salience, 0), COALESCE(excluded.salience, 0))",
                    (r["tid"], target_id, mentions, r["salience"], r["role_in_meeting"],
                     r["source"]))
            # Shared text: from the source we take exactly what moved. Otherwise
            # the mentions are counted twice — at the source and at the destination. We do not go below 1:
            # the row stays alive (it is still mentioned by its own
            # spelling), and `mention_count` from enrichment is overstated even without
            # us — it is a raw substring count, where «Слава Гринчук» gives +3.
            for r in to_copy:
                c.execute("UPDATE meeting_entities SET mention_count = MAX(1, ? - ?) "
                          "WHERE entity_id = ? AND transcription_id = ?",
                          (r["mention_count"], mine_mentions[r["tid"]], source_id, r["tid"]))
            if moved_tids:
                lmarks = ",".join("?" for _ in moved_tids)
                c.execute(f"DELETE FROM meeting_entities WHERE entity_id = ? "
                          f"AND transcription_id IN ({lmarks})", [source_id, *moved_tids])

        # --- the merge trail must not return the detached back ---
        # `enrichment._find_merged_entity` returns the source by a spelling from
        # `merged_from[].aliases`. If we left it there, the next enrich
        # would again bind the detached name to the source — split would "not stick"
        # just as merge once did not.
        try:
            meta = json.loads(src["metadata_json"]) if src["metadata_json"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        gone_norms = {r["normalized_alias"] for r in moving}
        kept_history = []
        for gone in meta.get("merged_from") or []:
            if not isinstance(gone, dict):
                continue
            spellings = [s for s in (gone.get("aliases") or []) if s not in gone_norms]
            if gone.get("aliases") is not None and not spellings:
                continue          # no spellings left from the record — remove it entirely
            if gone.get("aliases") is not None:
                gone = {**gone, "aliases": spellings}
            elif _normalize(gone.get("name") or "") in gone_norms:
                continue          # an old record without a list: it was itself this spelling
            kept_history.append(gone)
        if kept_history:
            meta["merged_from"] = kept_history
        else:
            meta.pop("merged_from", None)
        meta.setdefault("split_off", []).append({
            "aliases": moving_spellings,
            "to_id": None if drop else target_id,
            "to_name": None if drop else report["target"].get("name") or new_name,
            "dropped": bool(drop),
            "tasks": len(task_ids), "links_moved": len(to_move),
            "links_copied": len(to_copy), "at": now,
        })
        c.execute("UPDATE entities SET metadata_json = ?, updated_at = ? WHERE id = ?",
                  (json.dumps(meta, ensure_ascii=False), now, source_id))

        touched = {source_id}
        if not drop:
            tmeta = {}
            if tgt and tgt["metadata_json"]:
                try:
                    loaded = json.loads(tgt["metadata_json"])
                    tmeta = loaded if isinstance(loaded, dict) else {}
                except (json.JSONDecodeError, TypeError):
                    tmeta = {}
            tmeta.setdefault("split_from", []).append({
                "id": source_id, "name": src["canonical_name"],
                "aliases": moving_spellings, "at": now,
            })
            c.execute("UPDATE entities SET metadata_json = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(tmeta, ensure_ascii=False), now, target_id))
            touched.add(target_id)

        _recompute_entity_aggregates(c, touched)
        conn.commit()

    logger.info("[entity_dedup] split #%s '%s': %s -> %s (tasks %d, links %d+%d)",
                source_id, src["canonical_name"], moving_spellings,
                "DROP" if drop else f"#{target_id}", len(task_ids), len(to_move), len(to_copy))
    return report


# ============================================================
# 4. Alias — assign a spelling by hand
# ============================================================

def add_aliases(db_path: str, entity_id: int, aliases: list[str], *,
                dry_run: bool = True) -> dict:
    """Add to an entity a spelling under which ANOTHER layer of the archive knows it.

    The gap this exists for: an alias could be created only by enrichment (Claude) and
    `merge` as a side effect. So «Dmytro Lebid» from the `tg_sender` field and
    «Дмитро Лебідь» from the graph stay two different people for everyone who joins the layers
    (`speakers._entity_index`, `commitments.link_owners`), and they could be merged
    only by merging whole entities — and the sender entity does not exist at all.

    Why not merge: in a chat the match goes by ONE token after transliteration
    («Лебідь» = «Lebid»), and that is a weak sign — the form of a name can differ, a surname
    can be shared by different people. An alias leaves the spelling in place and absorbs nothing.

    We do not silently take someone else's spelling. We look at BOTH places where a spelling may
    already live: `entity_aliases` (UNIQUE globally) and `entities.normalized_name` —
    because `enrichment._find_entity` looks first by canonical name, and `split
    --to-name` creates a row with no alias row at all. An alias added on top of
    someone else's canonical name would be dead weight, and `relink` after it
    would also re-hang tasks onto that foreign entity.
    """
    if not aliases:
        return {"status": "error", "error": "aliases is empty"}
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        ent = c.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not ent:
            return {"status": "not_found", "entity_id": entity_id}

        added, already, taken, skipped = [], [], [], []
        for raw in aliases:
            spelling = (raw or "").strip()
            na = _normalize(spelling)
            if not na:
                skipped.append(raw)
                continue
            owner = c.execute(
                "SELECT a.entity_id AS id, e.canonical_name, e.type FROM entity_aliases a "
                "JOIN entities e ON e.id = a.entity_id WHERE a.normalized_alias = ?",
                (na,)).fetchone()
            if owner is None:
                owner = c.execute(
                    "SELECT id, canonical_name, type FROM entities WHERE normalized_name = ?",
                    (na,)).fetchone()
            if owner and owner["id"] == entity_id:
                already.append(spelling)
                continue
            if owner:
                taken.append({"alias": spelling, "entity_id": owner["id"],
                              "name": owner["canonical_name"], "type": owner["type"]})
                continue
            added.append(spelling)
            if not dry_run:
                c.execute(
                    "INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                    "VALUES (?, ?, ?)", (entity_id, spelling, na))
        if not dry_run and added:
            c.execute("UPDATE entities SET updated_at = ? WHERE id = ?",
                      (datetime.now().isoformat(timespec="seconds"), entity_id))
            conn.commit()

    if not dry_run and added:
        logger.info("[entity_dedup] alias +%s -> #%s '%s'",
                    added, entity_id, ent["canonical_name"])
    return {"status": "ok", "dry_run": dry_run,
            "entity": {"id": entity_id, "type": ent["type"],
                       "name": ent["canonical_name"]},
            "added": added, "already": already, "taken": taken, "skipped": skipped}


# ============================================================
# Spellings that are actually common words
# ============================================================

def find_junk_aliases(db_path: str, *, min_lower: int = 5,
                      max_caps_share: float = 0.2) -> list[dict]:
    """Aliases that the corpus writes in LOWERCASE — i.e. common words, not names.

    Where the junk comes from: enrichment puts into the graph everything Claude called
    an entity, together with spelling variants. That is how the aliases
    «документ», «договір», «проєкт», «інвестор» ended up in the graph. Each of them is a trap for
    any layer that checks text against the graph: `tg_entities` has to hold the
    rule "a proper name is written with a capital" precisely because of them, and the
    project slice catches «Проєкту» in someone else's sentence and attributes it to Nova Dance Center.

    We take the evidence in the same place as `tg_entities` — in usage. A proper name is written
    with a capital letter everywhere, not only at the start of a sentence; a common word —
    with a lowercase one. So across the whole archive we count two values: how many times the spelling
    occurred with a capital in the MIDDLE of a sentence and how many with a lowercase.
    If lowercase prevails — it is a word, not a name.

    We import the proper-name rule from `tg_entities` rather than rewrite it: two
    definitions of the same thing would diverge at the first edit, and the audit would start
    showing something other than what real links stand on.

    Changes nothing. To remove what is found — `split <id> --alias «…» --drop`.
    """
    from app.services import tg_entities

    with get_db_connection(db_path) as conn:
        # The canonical name must be judged too, and not via `entity_aliases`: its own
        # alias row is created via INSERT OR IGNORE against the global
        # UNIQUE, so when another entity already took the spelling, there is no row.
        # In the live DB there are 58 such out of 2692 — among them exactly the form the command
        # looks for («Zero», «Notion», «Форест»). And `tg_entities` sets links
        # by the canonical name too (`load_names` reads both sources).
        ph = ",".join("?" * len(tg_entities.LINKABLE_TYPES))
        rows = conn.execute(
            "SELECT a.alias, a.entity_id, e.type, e.canonical_name, e.normalized_name "
            "FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
            f"WHERE e.type IN ({ph}) AND a.alias NOT LIKE '% %' "
            "UNION "
            "SELECT e.canonical_name AS alias, e.id AS entity_id, e.type, "
            "       e.canonical_name, e.normalized_name "
            "FROM entities e "
            f"WHERE e.type IN ({ph}) AND e.canonical_name NOT LIKE '% %'",
            (*tg_entities.LINKABLE_TYPES, *tg_entities.LINKABLE_TYPES)).fetchall()
        # How many links the entity holds specifically by the text check: this is
        # the price of a mistake if the spelling turns out to be junk.
        link_rows = conn.execute(
            "SELECT entity_id, COUNT(*) n FROM meeting_entities WHERE source = ? "
            "GROUP BY entity_id", (tg_entities.SOURCE,)).fetchall()
        texts = conn.execute(
            "SELECT transcript_text FROM transcriptions "
            "WHERE deleted_at IS NULL AND transcript_text IS NOT NULL").fetchall()

    watched: dict[str, list] = {}
    handles: set[str] = set()
    for r in rows:
        key = tg_entities._key(r["alias"])
        if len(key) < tg_entities.MIN_NAME_LEN:
            continue
        # A Telegram handle is a proper name by convention, and is always written in
        # lowercase: the capital-letter rule condemns it, although «@jbondarenko» is by no means
        # a common word. Handles cannot be judged by this method at all.
        # We look not only at an alias with «@»: the same spelling also arrives as
        # a canonical name, already without the at-sign, and then the guard did not fire.
        if r["alias"].lstrip().startswith("@"):
            handles.add(key)
        watched.setdefault(key, []).append(r)
    with get_db_connection(db_path) as conn:
        for row in conn.execute("SELECT DISTINCT tg_sender FROM transcriptions "
                                "WHERE tg_sender IS NOT NULL"):
            handles.add(tg_entities._key(row["tg_sender"]))
    for key in handles:
        watched.pop(key, None)
    if not watched:
        return []

    caps: dict[str, int] = {}
    lower: dict[str, int] = {}
    for row in texts:
        text = row["transcript_text"]
        for m in tg_entities._WORD.finditer(text):
            word = m.group(0)
            key = word.casefold()
            if key not in watched:
                continue
            if word[:1].islower():
                lower[key] = lower.get(key, 0) + 1
            # We count the evidence by THE SAME rule by which links are set —
            # otherwise the audit advises removing a spelling that works right now:
            # «ACMECORP» in an ordinary sentence is a name for `find_mentions`, yet
            # the audit did not see it at all (a separate ALL-CAPS guard) and reported
            # caps=0 with six live links.
            elif tg_entities.written_as_proper_noun(text, word, m.start()):
                caps[key] = caps.get(key, 0) + 1

    links = {r["entity_id"]: r["n"] for r in link_rows}
    out = []
    for key, entries in watched.items():
        lo, up = lower.get(key, 0), caps.get(key, 0)
        if lo < min_lower or (up + lo) == 0:
            continue
        share = up / (up + lo)
        if share >= max_caps_share:
            continue
        for r in entries:
            out.append({
                "alias": r["alias"], "entity_id": r["entity_id"], "type": r["type"],
                "entity": r["canonical_name"],
                # The canonical name also lies in the aliases: removing it as an alias is not enough —
                # the row stays in the graph under the same word.
                "is_canonical": tg_entities._key(r["canonical_name"]) == key,
                "lower": lo, "caps_mid_sentence": up, "caps_share": round(share, 3),
                "entity_links": links.get(r["entity_id"], 0),
            })
    out.sort(key=lambda d: (-d["lower"], d["alias"]))
    return out


# ============================================================
# 5. Morphology blockers — read-only report + rename (S2, blocked_by S1)
# ============================================================

# Spellings that the owner explicitly asked not to propose for changes without
# a manual check (Goal/Non-goals of this story): «Місто» is a real cafe name,
# «BehindBars» and «Акмеет» were already checked earlier. The same fold key
# as everywhere in the module.
_DISPUTED_NAMES = {_fold_name(n) for n in ("Місто", "BehindBars", "Акмеет")}


def _link_stats(conn) -> dict[int, tuple[int, int]]:
    """entity_id -> (total links, of them `tg_entities.SOURCE`/thread_match).

    The same link count as in the rest of the module (`COUNT(DISTINCT
    transcription_id)`, softly deleted meetings are not counted), plus the second
    column — how many of them came from an exact substring match of the text, not
    from Claude at enrichment (`source IS NULL`).
    """
    from app.services import tg_entities

    rows = conn.execute(
        "SELECT me.entity_id id, "
        "COUNT(DISTINCT me.transcription_id) total, "
        "COUNT(DISTINCT CASE WHEN me.source = ? THEN me.transcription_id END) tm "
        "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
        "WHERE t.deleted_at IS NULL GROUP BY me.entity_id",
        (tg_entities.SOURCE,),
    ).fetchall()
    return {r["id"]: (r["total"], r["tm"]) for r in rows}


def find_morph_blockers(db_path: str, *, min_lower: int = 5,
                        max_caps_share: float = 0.2) -> list[dict]:
    """Entities that will poison the morphological matcher (S3), because the problematic
    spelling stands in `canonical_name`, not in an alias — an alias (`split ... --drop`)
    does not remove such a case, a rename (`rename`) is needed, or a
    merge with an existing spelling (`merge`). Changes nothing.

    Two reasons (`reason`), both composed of the module's existing primitives, not
    a new heuristic (Non-goals of this story):

    * `generic_canonical` — the same rule by which `find_junk_aliases` already
      judges a spelling by case in the corpus ("lowercase — not a proper name"),
      taken only for rows where this spelling is also the CANONICAL name of the entity
      (`is_canonical`): «Фонд», «Сенс», «Make» — an alias could simply be
      removed, a canonical name cannot.
    * `case_form` — a single-word name from which the suffix folder of the morph matcher
      (`tg_entities._stem`, the same as in S3) strips a case suffix, and
      most of its links came from an exact substring match of the text
      (`thread_match`): the row LITERALLY saw this word form in the chat and
      created the entity under it, rather than receiving a meaningful name from Claude —
      the example from the brief: project «Україні» #3785, 29 of 30 links from TG.

    Disputed spellings from the story's list are marked `disputed`, not
    dropped — the decision about them stays with a human, as said in
    Non-goals.
    """
    from app.services import tg_entities

    with get_db_connection(db_path) as conn:
        link_stats = _link_stats(conn)
        ph = ",".join("?" * len(tg_entities.LINKABLE_TYPES))
        rows = conn.execute(
            f"SELECT id, type, canonical_name FROM entities WHERE type IN ({ph})",
            tg_entities.LINKABLE_TYPES).fetchall()

    out: list[dict] = []
    seen_ids: set[int] = set()

    for r in find_junk_aliases(db_path, min_lower=min_lower, max_caps_share=max_caps_share):
        if not r["is_canonical"] or r["entity_id"] in seen_ids:
            continue
        seen_ids.add(r["entity_id"])
        total, tm = link_stats.get(r["entity_id"], (0, 0))
        out.append({
            "entity_id": r["entity_id"], "type": r["type"], "name": r["entity"],
            "reason": "generic_canonical",
            "links_total": total, "links_thread_match": tm,
            "disputed": _fold_name(r["entity"]) in _DISPUTED_NAMES,
        })

    for r in rows:
        if r["id"] in seen_ids:
            continue
        name = r["canonical_name"] or ""
        if " " in name.strip():
            continue                          # the morph folder does not touch phrases
        key = tg_entities._key(name)
        if len(key) < tg_entities.MIN_NAME_LEN:
            continue
        stem = tg_entities._stem(key)
        if stem == key:
            continue                          # no case suffix — not our class
        total, tm = link_stats.get(r["id"], (0, 0))
        if total == 0 or tm / total < 0.5:
            continue                          # mostly not from the exact TG text
        seen_ids.add(r["id"])
        out.append({
            "entity_id": r["id"], "type": r["type"], "name": name,
            "reason": "case_form",
            "links_total": total, "links_thread_match": tm,
            "disputed": _fold_name(name) in _DISPUTED_NAMES,
        })

    out.sort(key=lambda d: (-d["links_total"], d["entity_id"]))
    return out


def rename_entity(db_path: str, entity_id: int, new_name: str, *,
                  dry_run: bool = True) -> dict:
    """Rename an entity's canonical name — the destination for `blockers`, where
    the problematic spelling lies in `canonical_name`. An alias is not the destination here
    (the spelling is not foreign but its own and the only one), `split` neither (there is no foreign spelling
    to detach).

    The old name stays with the entity as an alias (INSERT OR IGNORE — the global
    UNIQUE leaves it in place if it is already taken by someone else's row): the same
    principle of "stickiness" as in merge/split, otherwise the next enrichment,
    seeing the old word form again, would create a new entity under it.

    Idempotent: a repeated call with the same `new_name` writes nothing
    (`changed=False`) — `normalized_name`/`canonical_name` are already such.

    `normalized_name` is computed in Python (`enrichment._normalize`), the same
    way as on write — not by a SQLite function (`LOWER` does not fold Cyrillic).

    Returns {"status": "ok", "dry_run":, "changed":, "entity": {...}} or
    {"status": "error", "error": ..., "rival": {...}} on a collision of
    `UNIQUE(type, normalized_name)`, or {"status": "not_found", ...}.
    """
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        ent = c.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not ent:
            return {"status": "not_found", "entity_id": entity_id}

        canonical = (new_name or "").strip()
        norm = _normalize(canonical)
        if not norm:
            return {"status": "error", "error": "new_name is empty after normalization"}

        if norm == ent["normalized_name"] and canonical == ent["canonical_name"]:
            return {"status": "ok", "dry_run": dry_run, "changed": False,
                    "entity": {"id": entity_id, "type": ent["type"],
                               "old_name": ent["canonical_name"], "new_name": canonical}}

        rival = c.execute(
            "SELECT id, canonical_name FROM entities WHERE type = ? AND normalized_name = ? "
            "AND id <> ?", (ent["type"], norm, entity_id)).fetchone()
        if rival:
            return {"status": "error",
                    "error": (f"[{ent['type']}] '{canonical}' already exists "
                              f"(#{rival['id']} '{rival['canonical_name']}') — "
                              f"a merge is needed here, not a rename"),
                    "rival": {"id": rival["id"], "name": rival["canonical_name"]}}

        report = {"status": "ok", "dry_run": dry_run, "changed": True,
                  "entity": {"id": entity_id, "type": ent["type"],
                             "old_name": ent["canonical_name"], "new_name": canonical}}
        if dry_run:
            return report

        now = datetime.now().isoformat(timespec="seconds")
        old_name, old_norm = ent["canonical_name"], ent["normalized_name"]
        c.execute(
            "UPDATE entities SET canonical_name = ?, normalized_name = ?, updated_at = ? "
            "WHERE id = ?", (canonical, norm, now, entity_id))
        c.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized_alias) "
            "VALUES (?, ?, ?)", (entity_id, old_name, old_norm))

        try:
            meta = json.loads(ent["metadata_json"]) if ent["metadata_json"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta.setdefault("renamed_from", []).append(
            {"old_name": old_name, "new_name": canonical, "at": now})
        c.execute("UPDATE entities SET metadata_json = ? WHERE id = ?",
                  (json.dumps(meta, ensure_ascii=False), entity_id))
        conn.commit()

    logger.info("[entity_dedup] rename #%s '%s' -> '%s'", entity_id, old_name, canonical)
    return report


# ============================================================
# CLI
# ============================================================

def _cmd_junk_aliases(args: argparse.Namespace) -> int:
    found = find_junk_aliases(args.db, min_lower=args.min_lower,
                              max_caps_share=args.max_caps_share)
    if args.json:
        print(json.dumps(found[:args.limit], ensure_ascii=False, indent=2))
        return 0
    if not found:
        print("No spellings that look like common words were found.")
        return 0
    shown = found[:args.limit]
    print(f"Found {len(found)} spellings that the corpus writes mostly in lowercase "
          f"(shown {len(shown)}):\n")
    for r in shown:
        mark = " ← CANONICAL NAME" if r["is_canonical"] else ""
        print(f"  «{r['alias']}» → [{r['type']}] #{r['entity_id']} '{r['entity']}'{mark}")
        print(f"      lowercase {r['lower']}, capitalized mid-sentence "
              f"{r['caps_mid_sentence']} ({r['caps_share']:.0%}); "
              f"links on the entity: {r['entity_links']}")
    print("\nTo remove a spelling: python -m app.services.entity_dedup split <entity_id> "
          "--alias «SPELLING» --drop --apply")
    print("A canonical name is not removed as an alias — it needs a merge or "
          "a rename of the entity.")
    return 0


def _cmd_twins(args: argparse.Namespace) -> int:
    groups = find_cross_type_twins(args.db, include_person=args.include_person)
    with_aliases = getattr(args, "with_aliases", False)
    alias_groups = (
        find_alias_cross_type_collisions(args.db, include_person=args.include_person)
        if with_aliases else None
    )
    if args.json:
        payload = groups if not with_aliases else {"twins": groups, "with_aliases": alias_groups}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not groups:
        print("No exact namesakes in different types were found.")
    else:
        print(f"Found {len(groups)} groups "
              f"(rows to disappear: {sum(len(g['merge']) for g in groups)}):\n")
        for g in groups:
            k = g["keep"]
            print(f"  keep [{k['type']}] #{k['id']} '{k['name']}' ({k['links']} links)")
            for m in g["merge"]:
                print(f"     ← [{m['type']}] #{m['id']} '{m['name']}' ({m['links']} links)")
        print("\nTo merge a pair: python -m app.services.entity_dedup merge "
              "<keep_id> <merge_id> --allow-cross-type")
    if with_aliases:
        mergeable = [g for g in alias_groups if not g.get("ambiguous")]
        ambiguous = [g for g in alias_groups if g.get("ambiguous")]
        print(f"\nAlias = canonical name of another type: {len(mergeable)} groups "
              f"({len(ambiguous)} ambiguous, skipped):\n")
        for g in mergeable:
            k, ev = g["keep"], g["evidence"]
            print(f"  keep [{k['type']}] #{k['id']} '{k['name']}' ({k['links']} links)"
                  f" — alias «{ev['alias']}» of entity #{ev['alias_of']}")
            for m in g["merge"]:
                print(f"     ← [{m['type']}] #{m['id']} '{m['name']}' ({m['links']} links)")
        for g in ambiguous:
            names = ", ".join(f"[{m['type']}] #{m['id']} '{m['name']}'" for m in g["members"])
            print(f"  ⚠ ambiguous: spelling «{g['key']}» — {names}")
        if mergeable:
            print("\nTo merge a pair: python -m app.services.entity_dedup merge "
                  "<keep_id> <merge_id> --allow-cross-type")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    candidates = find_merge_candidates(
        args.db, etype=args.type, threshold=args.threshold, limit=args.limit,
    )
    if args.json:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return 0
    if not candidates:
        print(f"No merge candidates found (threshold={args.threshold}).")
        return 0
    print(f"Found {len(candidates)} candidates (threshold={args.threshold}):\n")
    for cnd in candidates:
        print(f"  [{cnd['type']}] #{cnd['id1']} '{cnd['name1']}'  <->  "
              f"#{cnd['id2']} '{cnd['name2']}'   sim={cnd['similarity']}")
    print("\nTo merge a pair: "
          "python -m app.services.entity_dedup merge <keep_id> <merge_id>")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    from app.db.connection import get_db_connection as _gdc
    with _gdc(args.db) as conn:
        keep = conn.execute("SELECT type, canonical_name FROM entities WHERE id = ?",
                            (args.keep_id,)).fetchone()
        merge = conn.execute("SELECT type, canonical_name FROM entities WHERE id = ?",
                             (args.merge_id,)).fetchone()
    if not keep or not merge:
        print(f"Not found: keep_id={args.keep_id} ({'present' if keep else 'MISSING'}), "
              f"merge_id={args.merge_id} ({'present' if merge else 'MISSING'})")
        return 1
    print(f"Will be merged: [{merge['type']}] #{args.merge_id} '{merge['canonical_name']}' "
          f"-> [{keep['type']}] #{args.keep_id} '{keep['canonical_name']}'")
    if keep["type"] != merge["type"]:
        if not args.allow_cross_type:
            print("  Different types — --allow-cross-type is required. Nothing done.")
            return 1
        if "person" in (keep["type"], merge["type"]) and not args.allow_person:
            print("  A person and a non-person: merge is forbidden (see merge_entities).")
            print("  If the pair has already been checked by eye and is really one thing — "
                  "add --allow-person.")
            return 1
        print(f"  WARNING: different types, the resulting type will be '{keep['type']}'")
    if not args.yes:
        answer = input("Confirm merge? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "так", "т"):
            print("Cancelled.")
            return 1
    result = merge_entities(args.db, args.keep_id, args.merge_id,
                            allow_cross_type=args.allow_cross_type,
                            allow_person=args.allow_person)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "merged" else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    info = inspect_entity(args.db, args.entity_id)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info.get("status") == "ok" else 1
    if info.get("status") != "ok":
        print(f"Entity #{args.entity_id} not found.")
        return 1
    e = info["entity"]
    print(f"[{e['type']}] #{e['id']} '{e['name']}'  role={e['role'] or '—'}")
    print(f"  mentions {e['mention_count']}, meetings {e['meeting_count']}, "
          f"tasks {info['tasks_total']}, links {info['links_total']} "
          f"(of them without a long spelling in the text: {info['links_short_name_only']})")
    if info["tasks_ambiguous_owner"]:
        print(f"  tasks with a compound owner («X and Y»): "
              f"{info['tasks_ambiguous_owner']} — not evidence")
    if info["tasks_unmatched_owner"]:
        print(f"  owners outside the aliases: {info['tasks_unmatched_owner'][:8]}")
    print("\n  alias                          tasks   texts")
    for a in info["aliases"]:
        mark = " " if a["evidence"] else "·"   # · = short, we do not search it in texts
        print(f"  {mark}{a['alias'][:30]:<30} {a['tasks']:>5}  {a['texts']:>7}")
    print("\nDetach: python -m app.services.entity_dedup split "
          f"{e['id']} --alias '<spelling>' (--to-id N | --to-name '...' | --drop) --apply")
    return 0


def _cmd_alias(args: argparse.Namespace) -> int:
    res = add_aliases(args.db, args.entity_id, args.add, dry_run=not args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("status") != "ok":
        return 1
    if res.get("taken"):
        for t in res["taken"]:
            print(f"  TAKEN: '{t['alias']}' already belongs to [{t['type']}] #{t['entity_id']} "
                  f"'{t['name']}' — not touching. If it is the same person, a merge is needed here")
    if res.get("dry_run"):
        print("\nThis was a DRY RUN (nothing changed). Add --apply to execute.")
    if not res.get("added") and res.get("taken"):
        # Nothing assigned, and the reason is someone else's spellings. For a script this is a refusal,
        # not "done": merge behaves exactly the same when a pair is rejected.
        return 1
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    res = split_entity(args.db, args.source_id, args.alias, target_id=args.to_id,
                       new_name=args.to_name, drop=args.drop, dry_run=not args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("status") != "ok":
        return 1
    if res.get("dry_run"):
        print("\nThis was a DRY RUN (nothing changed). Add --apply to execute.")
    return 0


def _cmd_blockers(args: argparse.Namespace) -> int:
    found = find_morph_blockers(args.db)
    if args.limit:
        found = found[:args.limit]
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    if not found:
        print("No morphology blockers found.")
        return 0
    print(f"Found {len(found)} morphology blockers:\n")
    for r in found:
        mark = " ⚠ DISPUTED — do not propose without an eyeball check" if r["disputed"] else ""
        print(f"  [{r['type']}] #{r['entity_id']} '{r['name']}' — {r['reason']}{mark}")
        print(f"      links in total {r['links_total']}, "
              f"of them thread_match {r['links_thread_match']}")
    print("\nRename: python -m app.services.entity_dedup rename "
          "<entity_id> '<new name>' --apply")
    print("Or merge with an existing spelling: python -m app.services.entity_dedup merge "
          "<keep_id> <merge_id>")
    return 0


def _cmd_rename(args: argparse.Namespace) -> int:
    res = rename_entity(args.db, args.entity_id, args.new_name, dry_run=not args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("status") != "ok":
        return 1
    if res.get("dry_run"):
        print("\nThis was a DRY RUN (nothing changed). Add --apply to execute.")
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    parser = argparse.ArgumentParser(
        prog="entity_dedup",
        description="Offline embedding-based fuzzy dedup of memory graph entities (T6.7).",
    )
    parser.add_argument("--db", default=default_db, help="Path to the SQLite DB")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Show merge candidates (read-only)")
    p_list.add_argument("--type", choices=list(_VALID_ENTITY_TYPES), default=None)
    p_list.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_list.set_defaults(func=_cmd_list)

    p_twins = sub.add_parser("twins", help="Exact namesakes in different types (no model)")
    p_twins.add_argument("--include-person", action="store_true",
                         help="Also show «person + non-person» pairs (merge takes them "
                              "only with --allow-person, after an eyeball check)")
    p_twins.add_argument("--with-aliases", action="store_true",
                         help="Add «alias = canonical name of another type» collisions "
                              "(find_alias_cross_type_collisions); without the flag the output "
                              "does not change")
    p_twins.add_argument("--json", action="store_true", help="Output as JSON")
    p_twins.set_defaults(func=_cmd_twins)

    p_merge = sub.add_parser("merge", help="Merge a pair of entities (confirmable)")
    p_merge.add_argument("keep_id", type=int, help="ID of the entity that stays")
    p_merge.add_argument("merge_id", type=int, help="ID of the entity that disappears")
    p_merge.add_argument("--yes", action="store_true", help="Without interactive confirmation")
    p_merge.add_argument("--allow-cross-type", action="store_true",
                         help="Allow merging different types (project+org etc.); "
                              "the resulting type comes from keep_id")
    p_merge.add_argument("--allow-person", action="store_true",
                         help="Allow a «person + non-person» pair — only when it "
                              "has been checked by eye and is really one thing (an agency named "
                              "after its owner's surname). The person row that disappears still "
                              "must not have tasks or a speaker")
    p_merge.set_defaults(func=_cmd_merge)

    p_alias = sub.add_parser("alias", help="Add a spelling to an entity (e.g. from tg_sender)")
    p_alias.add_argument("entity_id", type=int)
    p_alias.add_argument("--add", action="append", default=[], required=True,
                         metavar="SPELLING", help="A spelling; may be given several times")
    p_alias.add_argument("--apply", action="store_true",
                         help="Without it — a dry run (nothing changes)")
    p_alias.set_defaults(func=_cmd_alias)

    p_junk = sub.add_parser(
        "junk-aliases", help="Spellings that the corpus writes in lowercase (read-only)")
    p_junk.add_argument("--min-lower", type=int, default=5,
                        help="How many times a word must occur in lowercase to be judged")
    p_junk.add_argument("--max-caps-share", type=float, default=0.2,
                        help="Share of capitalized spellings from which it is a name after all "
                             "(exactly at the boundary — already a name)")
    p_junk.add_argument("--limit", type=int, default=40)
    p_junk.add_argument("--json", action="store_true", help="Output as JSON")
    p_junk.set_defaults(func=_cmd_junk_aliases)

    p_insp = sub.add_parser("inspect", help="Evidence of «who exactly» inside an entity (read-only)")
    p_insp.add_argument("entity_id", type=int)
    p_insp.add_argument("--json", action="store_true", help="Output as JSON")
    p_insp.set_defaults(func=_cmd_inspect)

    p_split = sub.add_parser(
        "split", help="Detach a spelling (+ proven tasks and links) from an entity")
    p_split.add_argument("source_id", type=int, help="ID of the entity to detach from")
    p_split.add_argument("--alias", action="append", default=[], required=True,
                         metavar="SPELLING", help="Alias to detach (may be given several times)")
    dest = p_split.add_mutually_exclusive_group(required=True)
    dest.add_argument("--to-id", type=int, default=None,
                      help="Attach to an existing entity of the same type")
    dest.add_argument("--to-name", default=None, help="Create a new entity with this name")
    dest.add_argument("--drop", action="store_true",
                      help="The spelling is trash (JSON, EULA): delete the alias, "
                           "leave the tasks without an owner, delete the proven links")
    p_split.add_argument("--apply", action="store_true",
                         help="Execute (without the flag — a dry run only)")
    p_split.set_defaults(func=_cmd_split)

    p_block = sub.add_parser(
        "blockers", help="Entities that will poison the morphological matcher (read-only)")
    p_block.add_argument("--limit", type=int, default=None)
    p_block.add_argument("--json", action="store_true", help="Output as JSON")
    p_block.set_defaults(func=_cmd_blockers)

    p_rename = sub.add_parser("rename", help="Rename an entity's canonical name")
    p_rename.add_argument("entity_id", type=int)
    p_rename.add_argument("new_name")
    p_rename.add_argument("--apply", action="store_true",
                          help="Without it — a dry run (nothing changes)")
    p_rename.set_defaults(func=_cmd_rename)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
