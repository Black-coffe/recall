# ADR-055: Calendar attendee links never create entities and survive enrichment

- Status: proposed
- Date: 2026-10-26
- Spec: docs/specs/k6-calendar-ics (shipped 4.34.0)

## Context
Calendar attendees are a second source of `meeting_entities` links beside the Claude enrichment card. The
enrichment card rewrites a record's links each time it runs. If it did that blindly, calendar links would disappear
a minute after transcription.

Assumption (verbatim, `docs/specs/k6-calendar-ics/plan.md` `## Assumptions`, "Attendees"):

> a name is matched through the existing finder used by `_upsert_entity` (`app/services/enrichment.py:151`: normalized name, aliases, `merged_from`) for type `person`; a match is linked in `meeting_entities` with a new `source='calendar'`; no match leaves the attendee only in the event. Latin spellings with no alias stay unlinked: the report counts them, nothing guesses. Attendee emails are stored only in `calendar_events.attendees` (local), never as entity aliases, never in embeddings or RAG.

Plan delta (verbatim, same plan, `## Plan deltas`, excerpt):

> `enrichment.py:342` deletes every `meeting_entities` row of a record on enrich, which would erase calendar links a minute after transcription; it now keeps `source='calendar'` rows.

Repair round 1 (F2) found a second path: `INSERT OR REPLACE INTO meeting_entities` overwrote a kept calendar row
when the card also named the attendee. It became an upsert on (transcription_id, entity_id) that updates only
mention_count/role.

This builds on `docs/wiki/entity-graph-provenance.md` (one mention/one owner, provenance values never interchangeable).
The new part is that enrichment must preserve another writer's provenance.

## Options
None recorded. The delta states the chosen shape only.

## Decision
Attendees link only to existing `person` entities through the enrichment finder (normalized name, aliases,
`merged_from`). Links are written with `source='calendar'` via `INSERT OR IGNORE`. Enrichment keeps `source='calendar'`
rows when it rewrites a record's links, and never overwrites their `source`.

## Consequences
Not recorded beyond the above. Unmatched attendees, including Latin spellings with no alias, stay unlinked and are
counted.

## Invariants created
- Calendar data never creates an entity and never adds an attendee email as an alias.
- Attendee emails stay in `calendar_events.attendees` only, never in embeddings or RAG.
- Any code that deletes or rewrites `meeting_entities` rows for a record keeps rows with `source='calendar'` and
  keeps their `source` value. Updates touch only mention_count/role.
- No phonetic, transliteration or LLM matching for attendees.

## Revisit when
Not recorded.
