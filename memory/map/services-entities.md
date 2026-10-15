# services — entity graph: dedup, TG links

last-verified: 2026-10-15

Part of `app/services/*`; hub — [services.md](services.md). Wave 4 (T6.7 + stories 01-04).

- **entity_dedup.py** — offline CLI (`python -m app.services.entity_dedup <cmd>`); deliberately not wired into MCP (write actions).
  Read-only reconnaissance: `find_merge_candidates()` (embedding similarity of canonical names WITHIN a type),
  `find_cross_type_twins()`/`twins` (exact namesakes across types), `find_alias_cross_type_collisions()`/`twins --with-aliases`
  (3+ participants → `ambiguous: true`, not offered for merging), `find_junk_aliases`/`junk-aliases` (spellings the corpus
  mostly writes in lowercase), `find_morph_blockers()`/`blockers` (`generic_canonical` — a problematic spelling of the canonical name ITSELF;
  `case_form` — an inflected form from the TG text), `inspect_entity()`/`inspect` (evidence of "who exactly" in a single graph row).
  Write (dry-run by default, `--apply`/`--yes`): `merge_entities(keep_id, merge_id, allow_cross_type=, allow_person=)`
  (`:295`), `split_entity(source_id, move_aliases, target_id|new_name|drop, dry_run=)`, `add_aliases()`/`alias --add`,
  `rename_entity()`/`rename` (the old name stays as an alias, `metadata_json["renamed_from"]`; a `UNIQUE(type, normalized_name)`
  collision → refusal "merge needed").
  *A spelling is counted in Python (`_fold_name`), not by SQLite LOWER. One mention cannot be credited to two entities — the LONGEST
  spelling wins (`_winning_occurrences`). Person + non-person in a merge is forbidden even with `--allow-cross-type`, only
  `--allow-person` lifts it; a person row that disappears must not have tasks or a `speaker_id`.*
- **tg_entities.py** (4.5.3) — TG in the graph WITHOUT a model. `load_names()` (canonical+aliases of person/project/org, `MIN_NAME_LEN=4`,
  ≤6 words), `find_mentions()` (exact + morphology behind a flag), `find_exact_mentions()` (`:254`; NEVER morphology), `link_message`,
  `link_threads()`, `stats()`; the `names_for()` cache (`TG_ENTITIES_NAMES_TTL`=300 s) remembers the state of the morphology flag. CLI
  `link --dry-run|--chat`, `stats`.
  - Provenance `meeting_entities.source`: `SOURCE="thread_match"` (exact), `SOURCE_MORPH="thread_morph"`; NULL = inherited
    Claude links (more reliable, not overwritten).
  - Evidence FROM THE PLACE OF USE: a mention counts only if it is written with a capital letter and not at the start of a sentence
    (`written_as_proper_noun`); an ALL-CAPS header is filtered out (`is_shouting`). The link is per MESSAGE, not per thread
    (expansion to the thread is done by `scope._expand_to_threads`).
  - Morphology: `TG_ENTITIES_MORPH_ENABLED` (default `"0"` — OFF; read by the function `_morph_enabled()` without restart); a custom
    stemmer `_stem()`/`_CASE_SUFFIXES` (no pymorphy/snowball/nltk), only for SINGLE-WORD names. A multi-word exact match
    claims ALL its word positions (`exact_word_positions`) so that one mention does not go to two entities. Enable only
    after working through `blockers`.
- **comments.py** — `link_entities` (`:770`) calls `tg_entities.find_exact_mentions`: the comments layer deliberately does NOT pick up the
  morphology flag of TG ingest.
- **evals/graph_links.py** — read-only measurement of the morphology branch on a DB SNAPSHOT (`mode=ro`; refuses the live one without `--yes-live`,
  guard `is_live_db()`): exact-only vs exact+morph, diff per entity + the number of DISTINCT messages/chats. CLI
  `python -m evals.graph_links --db <path> [--json-out] [--top-n]`.
