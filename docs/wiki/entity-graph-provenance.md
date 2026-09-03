---
domain: entity-graph
tags: [entities, telegram, dedup, provenance]
related: [map/services.md, adr/001-token-exclusivity-exact-vs-morph-match.md, adr/002-comments-link-entities-exact-match-only.md]
last-verified: 2026-08-21
---

# Entity graph: one mention, one owner; provenance is never silently substituted

**One textual mention can never be attributed to two entities.** Wherever two
candidate spellings compete for the same position in a text, the LONGEST
spelling wins the whole span — not just its own characters. Enforced by:
- `app.services.entity_dedup._winning_occurrences` (used by `inspect_entity`
  and `split_entity` to decide which alias a task/link belongs to).
- `app.services.tg_entities._mentions_core`, guard 1: an exact multi-word
  match closes ALL of its word positions (`exact_word_positions`), not just
  the first word — otherwise a word inside a multi-word name stays open for
  the morphological branch and can be credited to a different entity.

**Provenance values on `meeting_entities.source` are not interchangeable and
must never be silently repointed:**
- `NULL` — inherited link from Claude enrichment (most reliable; dedup/rebuild
  code deliberately never touches it).
- `"thread_match"` (`tg_entities.SOURCE`) — exact spelling/alias match in a
  Telegram message.
- `"thread_morph"` (`tg_entities.SOURCE_MORPH`) — matched by stripped case
  suffix only (weaker evidence). Kept as a *separate* constant specifically so
  code that reads `source == "thread_match"` (`entity_dedup.py`, `comments.py`)
  does not start seeing morphological guesses as exact matches.

**The morphological matcher is off by default and must stay opt-in.**
`TG_ENTITIES_MORPH_ENABLED` (env, default `"0"`) gates it; merging or changing
this module must never flip the effective behavior of a running system by
itself. `comments.link_entities` uses `tg_entities.find_exact_mentions()`,
which ignores the flag entirely — the comment layer's contract only ever
covers exact matches, and turning the flag on for TG ingestion must not
silently change what comments does.

**Cross-type duplicates are a distinct class from same-type near-duplicates.**
`entity_dedup.find_merge_candidates` (embedding similarity) only compares
within one `type`. Two other detectors exist because most real duplicates in
this archive are the SAME name enriched separately as `project`/`org`/`person`:
`find_cross_type_twins` (exact canonical-name folding across types) and
`find_alias_cross_type_collisions` (an alias of one entity collides with the
canonical name of another). A folded spelling owned by 3+ distinct entities is
`ambiguous` and is never proposed for merge — one spelling cannot be assigned
to two owners without evidence.

**Person + non-person merges require a second, explicit flag
(`--allow-person`) even when cross-type merging is already allowed**, and are
refused outright if the disappearing person row still owns tasks or a
`speaker_id` — that data has no fallback owner once the person row is gone.

See `memory/map/services.md` (Граф сутностей section) for the current function
inventory and file locations, and `docs/adr/001-token-exclusivity-exact-vs-morph-match.md` /
`docs/adr/002-comments-link-entities-exact-match-only.md` for the decisions behind the
two provenance rules above.
