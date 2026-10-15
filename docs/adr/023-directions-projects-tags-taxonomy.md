# ADR-023: A two-layer taxonomy (direction → project) and tags — one resolver, one table, point re-embed through the embeddings version

- Status: proposed
- Date: 2026-09-22
- Spec: docs/specs/directions-projects-tags

## Context

The owner's decisions from the grill (brief.md `## Answers`) are already fixed and are not revisited here:
a project is the second level of the same `categories` table through `parent_id`; tags are manual +
a Claude hint during enrichment + inheritance from a TG chat; the phrase is parsed by the agent through
MCP; a change of direction/project/tags → point re-embed, the chunk prefix gets
"direction → project → tags" for all record types.

This ADR settles **how exactly**. The forces at play (checked against the code on 22.09.2026):

- The filter by `category_id` lives in ~15 places without a shared helper: `retrieval.py:227-232,
  281-287` (twice — vector and FTS), `research.py:136-140`, `commitments.py:927-929`,
  `audio_library.py:160-162` (through the COALESCE subquery `cat_subq`), `/api/history`,
  `/api/memory/entities`, `/api/memory/action-items`, `/api/telegram/chats`, and in
  `mcp_server.py` — direct SQL in `list_recent` (`:603-604`), `list_categories` (`:663`),
  `search_history`. The HTTP branch and the direct-DB MCP tools do not share code. The parser `_parse_category`
  is copied twice (`memory.py:72`, `research.py:96`).
- `retrieval.search` accepts one `category_id`; `scope_tids` is the "second layer", which
  deliberately knows nothing about the graph (`retrieval.py:196-199`).
- Name normalization: `memory.py:_norm_cat` (casefold + whitespace collapse) and
  `scope.py:_norm_name` (NFKC + casefold) — two different keys for one column `name_norm`.
- The re-embed hook (`record_meta.after_meta_update` → `reembed.schedule_record_reembed`)
  is called only from the PATCH of title/description. A change of `category_id` — single (`memory.py:245`),
  bulk (`:277/:280`), merge (`:59-66`), `scope.apply_chat_categories` (`:172-176`, one
  UPDATE over thousands of rows), `scope.label_uncategorized` (`:340`) — does not update the vectors.
  `_null_category_refs`/`_reassign_category_refs` do not touch `audio_downloads.category_id`.
- The chunk prefix (`embeddings.build_context_prefix:630-641`) puts the direction name **for calls
  only**; TG gets the thread, a document gets the page. `_load_prefix_meta:668-678` is
  one JOIN on `categories`, no tags.
- A mass re-embed crumbles FTS5 (`chunks_fts optimize` at the end — already in `reembed.run`);
  ADR-022 — a point re-embed is deferred for the duration of a live recording.
- `ask_archive(project=…)` and `scope.scope_filter_ids` already mean "a graph entity /
  free text", the graph has entities of type `project`. The new hierarchy level is also called
  "project".
- The golden set (`evals/golden_io.py:60-72`) knows only `category_id`; the gate (`gate.py:228-237`)
  passes it to `retrieval.search` as is.
- Live numbers: 29 categories, "Фонд Робота" records (≈more than half of the archive), the rest is a tail.

## Options

### A. Expanding "direction = itself + its projects"

1. A recursive CTE / `IN (SELECT …)` by hand in every place — zero new modules, but
   15 copies of one subquery in two processes that do not share code; they will drift apart on the first
   edit (the same trap as `lower()` in a Cyrillic grep).
2. **One SQL-only module `app/services/categories.py` with a fragment function that every
   filter place calls** — the same 15 places are edited once (replacing `col = ?` with a call),
   afterwards the semantics live in one function; imported from `mcp_server.py` too (no torch).
3. A denormalized `root_category_id` on 4 tables — a fast filter, but every reparent/
   merge/move must synchronize the column in 4 tables; expensive to undo, and there is no gain in
   speed on SQLite with 6 thousand records.

### B. Tag schema

1. A tag as a row in `entities` of type `topic` — the table already exists, but it has 2663 noisy topics, there is no
   manual control of the vocabulary, and `meeting_entities` exists only for enriched (~16 %) records.
2. **Separate tables `tags` + `record_tags` + `tg_chat_tags`; attachment only to
   `transcriptions`; Claude's hints are a separate column `suggested_tags_json`, only the
   confirmed ones go into `record_tags`** — a controlled vocabulary, filters without conditions on status.
3. A JSON array of tags in the column `transcriptions.tags_json` — the fewest tables, but the filter
   "tag AI" becomes `LIKE`/`json_each` in 15 places, and renaming a tag is a pass over all rows.

### C. Name collision

1. A new parameter `project_id` next to the existing `project` — two parameters for one word, the agent
   will confuse them guaranteed.
2. **`category_id` accepts the id of any node (direction or project), there is no new parameter;
   `project` stays a graph entity/free text** — no existing call breaks.
3. Rename `project` → `entity` with a backward alias — cleaner, but it is an edit of the contract of
   9 tools for the sake of a word, and `ask_log`/the golden set already contain `project`.

### D. Re-embed and prefix

1. A point hook on every record on every UPDATE — on the move of 4051 TG records that is 4051
   queue submissions and 4051 DELETE+INSERT on FTS without `optimize`.
2. **A hook only for the "current pair" of embeddings + a batch task for mass changes + the change of
   the prefix format itself goes as a new epoch `EMBED_VERSION` (the number is in `.env`, strictly above the
   live pair) through the existing `reembed.run`** — the mass
   rebuild is done by the mechanism that already knows optimize at the end and snapshot→gate→live.
3. A special flag "migration, hooks off" — an extra state that someone will forget to clear.

## Decision

### A. One resolver — `app/services/categories.py`

A new module **without torch/anthropic at import level** (memory note `mcp-stdio-no-heavy-models`),
into which the existing `_norm_cat`/`_norm_name`, `_parse_category`,
`_null_category_refs`/`_reassign_category_refs` move:

```python
def parse_filter(raw) -> int | str | None            # '5'→5, 'none'→'none', ''/'all'/None→None
def filter_sql(column: str, value) -> tuple[str, list]
    # None  → ("", [])
    # 'none'→ (f" AND {column} IS NULL", [])
    # int   → (f" AND {column} IN (SELECT id FROM categories WHERE id = ? OR parent_id = ?)", [v, v])
def norm_name(name: str) -> str                      # NFKC + strip + whitespace collapse + casefold
def subtree_ids(conn, category_id: int) -> list[int]  # [self, *children] — for UI counters and tests
```

The tree has **exactly two levels**: `parent_id` may point only at a node with `parent_id IS
NULL` (checked in PATCH/POST of a category, 400 otherwise). Therefore `id = ? OR parent_id = ?` is the
full expansion without a CTE; for a project (a leaf) the same fragment returns only itself. `column`
is any expression, including `cat_subq` from `audio_library.py`.

All 15 filter places (the list is in Context) replace their `= ? / IS NULL` branch with
`filter_sql(...)`. `retrieval.search` **keeps the signature** `category_id` — the semantics
"node + its projects" is implemented inside `_vector_search`/`_fts_search` by the same
helper, so the HTTP proxy, `gate.py`, `copilot/dispatcher` and the MCP proxy are not edited.
`scope.scope_filter_ids:438-441` (a name match with a category) looks the node up by `name_norm` and then
also calls `filter_sql`. Both old normalizers become an import of `categories.norm_name`
(the only difference — NFKC + whitespace collapse together; for the existing 29 names the key does not change,
we do not recompute `name_norm`).

Rejected: (1) — 15 copies in two processes; (3) — synchronizing 4 tables on every
reparent, expensive to undo.

### B. Tags

Migration **v45** (additive, `PRAGMA table_info` before every step):

```sql
ALTER TABLE categories ADD COLUMN parent_id INTEGER;            -- NULL = direction
CREATE INDEX idx_categories_parent ON categories(parent_id);
CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                   name_norm TEXT NOT NULL UNIQUE, color TEXT,
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE record_tags (transcription_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,
                          source TEXT NOT NULL DEFAULT 'manual',   -- manual | claude | chat
                          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                          PRIMARY KEY (transcription_id, tag_id));
CREATE INDEX idx_record_tags_tag ON record_tags(tag_id);
CREATE TABLE tg_chat_tags (chat_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,
                           PRIMARY KEY (chat_id, tag_id));
ALTER TABLE transcriptions ADD COLUMN suggested_tags_json TEXT;   -- Claude's hints, not yet accepted
```

- **Attachment — `transcriptions` only.** Audio in the Media library shows the tags of its latest
  transcript through the same COALESCE path as the direction (`audio_library.py:132-136`);
  an untranscribed recording has no tags. A copilot session has no tags.
- **Hints live separately from the truth.** During enrichment (`enrichment._persist_card`) Claude
  receives the existing tag vocabulary (as `_ask_claude_category` receives the list of directions) and
  returns ≤5 names → `suggested_tags_json`. Accepting with one click moves them into
  `record_tags` with `source='claude'`; the filters read **only** `record_tags`, so no
  filter place knows about the status of a hint.
- **`source` is needed for unbinding.** Removing a tag from a chat deletes only the rows
  `source='chat'` of that chat; manual ones stay (the same "one slot — one
  owner" rule as in `entity-graph-provenance`). Binding a tag to a chat is retroactive, like
  `apply_chat_categories`; a new TG ingest copies the chat's `tg_chat_tags` into `record_tags`
  next to inheriting `category_id`.
- Several tags in a filter — **OR** (any of them); the intersection with `category_id` — AND. This is "combine
  by tags" from the brief; an intersection of tags was not asked for.
- **There is a colour, no description/icon.** Colour — because the chip `.rc-chip` + `.rc-cat__dot` is already drawn that way;
  description — a fourth free field that nobody would read.
- **Tags are orthogonal to the tree.** Deleting/merging a direction does not touch `record_tags` at all.
  Deleting a tag cascades in app code (`record_tags`, `tg_chat_tags`); merging tags — `INSERT OR
  IGNORE` + DELETE, as with categories.

Rejected: (1) `entities.topic` — a noisy vocabulary and 16 % coverage; (3) a JSON column —
`LIKE` in 15 places.

### C. Naming

- **`category_id` — the id of any node** (direction or project) in HTTP and MCP. There is no new
  parameter `project_id`. `list_categories` (HTTP and MCP) returns the tree: `id, name,
  slug, color, icon, parent_id, level ('direction'|'project'), items` (own) and
  `items_total` (subtree); the MCP version stops losing color/icon.
- **We do not rename `project`** and do not change its semantics: a graph entity ∪ a text mention ∪
  a same-named tree node (with projects). Since a project name is now also a node, `project="NOVA"`
  honestly captures the subtree as well, which is a documented part of the union.
- New parameters wherever `category_id` already exists (`search_archive`, `list_recent`,
  `list_action_items`, `ask_archive`, `research_*`, `search_history`, `grep_archive` — this one
  also gets `category_id`): `tags: Optional[str]` (names comma-separated, like `project`), `since`/`until`
  (ISO date, by `meeting_date`). Parsing "for a week" is done by the agent, Recall does not parse dates.
- **The docstring text for every tool with both parameters (verbatim).** The docstrings in
  `mcp_server.py` are Ukrainian (the agent relays them to the owner) and `tests/test_mcp_taxonomy.py`
  reads this block as the source of truth, so it is a quoted interface string, not prose, and stays
  as the code has it:

  <!-- verbatim:start — Текст для docstring (quoted from mcp_server.py, kept in the code's language) -->
  > `category_id` — id **напрямку або проєкту** з `list_categories`; напрямок автоматично
  > включає всі свої проєкти, проєкт — лише себе. Це рівень ієрархії архіву.
  > `project` — **не** проєкт ієрархії, а назва сутності графа або вільний текст («Маяк»,
  > «Олена», бренд): звужує до записів, де ця назва згадана. Щоб звузити до проєкту як розділу
  > архіву — передай його id у `category_id`, не назву у `project`.
  > `tags` — назви тегів через кому; кілька = будь-який із них. Поєднання з `category_id` —
  > перетин («тег AI лише в цьому напрямку»).
  <!-- verbatim:end -->

  In English, for the reader: `category_id` is the id of a direction or a project from `list_categories`
  (a direction includes all its projects, a project only itself — the archive's hierarchy level);
  `project` is not a hierarchy project but a graph-entity name or free text that narrows to records
  mentioning it (to narrow to a project as a section, pass its id in `category_id`); `tags` are tag names
  comma-separated, several = any of them, and combined with `category_id` it is an intersection.

Rejected: (1) `project_id` — confusion is guaranteed; (3) renaming `project` — an edit of
9 contracts and `ask_log` for the sake of a word.

### D. Re-embed and prefix

- **The prefix format changes for all types → this is a new embeddings epoch.** The tail of line 1
  becomes `Напрямок → Проєкт` (or the direction alone) **for all types**, then the type-specific part
  (`нитка: …` / `стор. N`), then `теги: a, b, c` (≤5 tags, ≤60 characters — a ceiling, like
  `_PREFIX_DESCRIPTION_MAX`). `_load_prefix_meta` — a self-JOIN of `categories parent`
  + `GROUP_CONCAT` over `record_tags`. The mass rebuild of the archive is done by the **existing**
  `reembed.run` (snapshot on the old epoch → snapshot on the new → `evals.gate` → `evals.compare`
  → live with `--yes-live`), which already calls `optimize_chunk_index` once at the end.
  We write nothing new for the mass path.
- **The epoch number is an act of configuration, not a code edit.** It is read from the DB and set in `.env`:
  `SELECT embedding_model, embedding_version, COUNT(*) FROM transcriptions GROUP BY 1, 2`
  — as of 22.09.2026 the live one holds `(intfloat/multilingual-e5-large, 4)` on live data (+7 on the old 2), so **live 4 → 5 in `.env`** (step 1 of the runbook below). The number is
  taken **strictly above** any pair already in the DB. **ADR-016 is respected: the code default does not
  change** — `embeddings.EMBED_VERSION` and the registry `app/core/settings.py` stay at `2`,
  the live one went through the epochs above only via a `.env` line. **ADR-017: the pin separates formats only
  with a new number** — under a number that already exists in the DB, the new prefix would land in the old epoch,
  `reembed.run --dry-run` would show 0 stale, and the point hooks would quietly mix two formats in one
  sample of `retrieval.search`.
- **The point hook filters by pair.** `reembed.schedule_records_reembed(ids, *, db_path,
  reason)` — a new batch variant on top of `schedule_record_reembed`: it skips ids whose pair
  `(embedding_model, embedding_version)` ≠ current (`run` will pick those up anyway); ≤20 ids → one
  by one through the existing point path (with the ADR-022 deferral); >20 → one task
  `reembed_batch`, which iterates the ids, removes each from `_pending_ids` at its start, and calls
  `optimize_chunk_index` once at the end; deferral during a recording — the same
  `_recording_active()` + timer.
- **The only taxonomy writers.** `categories.set_category(conn, ids, cid) -> list[int]`,
  `categories.set_tags(conn, tid, add, remove, source) -> bool`, `categories.reparent(...)`,
  `apply_chat_tags(...)` — return the ids of records whose prefix changed; every HTTP/CLI call
  after `commit` passes them to `schedule_records_reembed`. `memory.py:245/277/280`, merge,
  `scope.apply_chat_categories:172-176`, `scope.label_uncategorized:340` are moved to these
  functions. Reparenting/renaming a node changes the prefix of the whole subtree — that too is
  `schedule_records_reembed(subtree records)`; on the day of the layout all of them are no-ops (the pair
  is old), and the archive is rebuilt by one `run`.
- **A format guard, not a manual order of steps.** The code declares `embeddings.PREFIX_FORMAT`
  (a fact of the code, not a setting), and the DB remembers the format of EVERY epoch —
  `embedding_epochs(model, version, prefix_format)`, migration **v46**, the backfill stamps everything
  that existed before it with format 1. An epoch number that coincides with an older format = **a refusal
  to write** (ERROR in the log, the record stays without chunks and `run` will pick it up; `reembed.run`
  refuses to start, including `--dry-run`), not a quiet mixing of two formats in
  one sample of `retrieval.search` (ADR-017). The default `EMBED_VERSION` does not
  move — ADR-016 stands.
- The order of the migration day: `EMBED_VERSION=5` in `.env` → **`python -m app.services.reembed
  epoch-check` → exit 0** (`--db` defaults to the live one, mode `mode=ro`; the CLI reads the
  PROCESS ENVIRONMENT, not `.env`, so in the same session first `$env:EMBED_VERSION="5"`) →
  restart (v45+v46 roll out) → the owner lays out the 29 names in Settings →
  `scope apply-chats --dry-run` → apply → `label-rest --dry-run` →
  apply → `reembed.run` (snapshot → gate → live). A forgotten `.env` is no longer silent: the very first
  new transcript will give an ERROR and stay without chunks, and `epoch-check` — exit 1
  `format_mismatch`. The window of degradation of the vector branch to FTS is
  the duration of `run` (≈2–3 h on 6 thousand records at 45 rec/min); acceptable for a personal tool,
  done in one evening.

Rejected: (1) — 4051 queue submissions and FTS without optimize; (3) — a flag that will be forgotten.

### E. Migration and safety

- v45 — DDL only, **no data transfer**; the layout is done by the owner by hand through
  `PATCH /api/memory/categories/<id>` (new field `parent_id`) and through `chat_categories.local.json`,
  where a chat row gets an optional `"parent": "Фонд Робота"` — `_ensure_categories` creates
  the missing node under it. A node name is unique **across the whole table** (the existing UNIQUE
  `name_norm`): the project "Маркетинг" cannot exist in two directions — a deliberate simplification.

  > **Replaced 2026-09-22 (v47, wave 6).** The simplification did not survive the very first layout of the live
  > archive: "Маркетинг" is needed both under "Фонд Робота" and under "Фріланс". Uniqueness is now
  > **among siblings** — directions among themselves, projects of one parent among themselves; a direction and another's
  > project may coincide. Migration v47 rebuilds `categories` (the inline `UNIQUE COLLATE
  > NOCASE` from v15 cannot be dropped by ALTER), puts `idx_categories_parent_name` UNIQUE on
  > `(COALESCE(parent_id, 0), name_norm)` and leaves `slug UNIQUE`; `PRAGMA foreign_keys=OFF` for
  > the duration of the rebuild is mandatory — with FKs on, `DROP TABLE categories` acts like
  > DELETE and silently nulls `tg_monitored_chats.category_id` (`ON DELETE SET NULL`, v18).
  > A duplicate is caught by a Python check among siblings BEFORE the write (`categories.
  > _assert_free_sibling_name`), so the 409 names the direction in which it is taken and carries
  > `conflict {id, path}`; the index stays the last line of defence, not the main control.
  >
  > **The canonical textual name of a node is the full path "Direction → Project"** (separator
  > `categories.PATH_SEP`, the same `→` as in the chunk context prefix). It is carried by the
  > field `path` of every `list_tree` item — HTTP `GET /api/memory/categories` and MCP
  > `list_categories`. A bare name remains an allowed input, but it is resolved by ONE
  > resolver — `categories.resolve_node(conn, text)`: one match → it, zero → `None`,
  > several → `CategoryError('ambiguous')` with `.candidates` (a list of paths).
  >
  > Ambiguity is never silent: HTTP returns 409 `{"error":"ambiguous","candidates":[…]}`
  > (`/api/memory/categories`, `/api/memory/ask`, `/api/memory/ask/stream` — the last one
  > resolves `project` BEFORE the generator, because a 409 cannot be returned from a stream), MCP
  > `ask_archive` — a note with the same paths, the chat map (`_ensure_categories`) and the arbiter
  > (`_ask_claude_category`) skip the row and print the paths in the report, `_title_matchers`
  > does not make a signal out of such a name. Nobody takes "the first match", merges the subtrees
  > of namesakes or returns empty.
  >
  > A chat-map row with `parent` no longer reparents another's same-named node — it creates its
  > own under the specified direction. And conversely: **a reparent puts the whole subtree into a point
  > re-embed just like a rename** — the path is in the chunk prefix, so after a
  > move the vectors of namesakes diverge precisely by it.
  >
  > **Added 2026-09-22 (wave 7, story 22)** — four refinements, each for a finding
  > of the round 5 council:
  >
  > 1. **Resolution precedence.** `resolve_node` first looks for the EXACT canonical path, and only
  >    then the bare name across all nodes. The canonical path of a direction is its bare name, so
  >    a namesake direction ("Маркетинг" next to another's "Фріланс → Маркетинг") is reachable by its own
  >    name, not `ambiguous` forever. Invariant: `resolve_node(node["path"]) == node["id"]`
  >    for EVERY item of `list_tree`, and every row of `.candidates` resolves back to its own
  >    node — a list that returns itself into `ambiguous` is a dead end.
  > 2. **`→` in a node name is forbidden** (`CategoryError('bad_name')` BEFORE the write, 400).
  >    A node "Фонд → Клієнти" breaks the path from both sides: it does not resolve by its own name,
  >    and the canonical path of its child becomes three-part (`split_path` counts exactly two parts).
  > 3. **The chat map resolves a row by the PAIR `(parent, category)`**, not a bare name: two rows
  >    "Маркетинг" under different directions are two nodes and two bindings. `--dry-run` lists
  >    ALL the nodes that the real pass will create (the plan is computed from the same resolution, not by
  >    checking names against the whole "before" table), each with a path and a note "namesake exists: …".
  > 4. **The v47 FK guard is defensive.** Measured: the migration is run by a raw `sqlite3.connect` in
  >    `init_database`, where `PRAGMA foreign_keys` = 0; `foreign_keys=ON` is turned on by another connection
  >    (`app/db/connection.py:40`). Therefore `PRAGMA foreign_keys=OFF` in v47 carries nothing on the
  >    current path and is kept in case of a call on an FK-ON connection; the trap itself is proven
  >    by a test on a hand-built FK-ON connection. Read the word "mandatory" above as
  >    "mandatory for any connection where FKs are enabled".
  >
  > **Added 2026-09-22 (wave 8, story 26)** — per the round 6 council:
  >
  > 1. **The chat map `--dry-run` is the same write path with a rollback**, not a separately
  >    derived plan. Three rounds in a row a separate branch diverged from the real pass
  >    (the `namesake` note, the order of rows). The writers `create_category`, `set_category`,
  >    `apply_chat_tags`, `ensure_tag` have a key `commit=False`; `apply_chat_categories`
  >    runs them on one connection and ends with `commit()` or `rollback()` (re-embed — only
  >    in the real one). `categories_created` is a log of the pass's actual creations. Rejected:
  >    a proxy connection with a no-op `commit` (hidden magic) and a `VACUUM INTO` copy of the DB for every
  >    preview. Resolution is deterministic: first the rows with `parent`, then the bare ones — against the tree that already
  >    contains these creations. `ambiguous` is keyed by the row's pair: a row with `parent`
  >    is never ambiguous.
  > 2. **The `→` rule is unconditional** — `update_category` checks it on a reparent without
  >    a rename too (an ordinary reparent stays allowed).
  > 3. **Two inputs — two rules, deliberately.** *Addressing* — a person or a tool passes a scope
  >    (`resolve_node`, `scope_filter_ids`, the chat map): the exact canonical path first, so
  >    a bare direction name = this direction; this is "one match", not "a direction always wins".
  >    *Inference from free text* — `_title_matchers`, a mention in a title that the author did not
  >    address: names with namesakes are skipped entirely.
- `_null_category_refs`/`_reassign_category_refs` move into `categories.py` and
  **close the hole** with `audio_downloads.category_id` (the same loop over tables).
- **Deleting/merging a node with children is a refusal (409)** until the projects are moved or
  deleted separately. Rejected: a cascade (unexpected disappearance of projects) and a quiet lift of children to
  the root (hides a mistake).
- **We do not aggregate the k-NN priors.** `categorize.suggest_category` votes by the neighbour's `category_id`
  as is — a node may be a direction or a project, and it is the project that the owner wants to see in a hint;
  `_prior_correct` computes shares per node as now — after records move into projects the "Фонд"
  will shrink, and the skew of the giant, which this coefficient suppresses, will shrink with it.
  Only two inputs change: `_title_matchers` already reads all nodes (projects get in
  automatically), and the arbiter `_ask_claude_category` receives the list as paths
  "Direction → Project", to choose a project and not only the root.
- The MCP `_prime_system_prompt` and the frontend's `catOptions()` receive the same tree from
  `list_categories` (optgroup direction → projects indented); `research.js` switches to
  `catOptions` instead of assembling by hand.

### F. The yardstick

The golden set gets two optional fields: `tags: list[str]` and `expected_leak: 0` (a constant,
explained below). `gate._search` passes `tags=` to `retrieval.search`. For every
item with `category_id` or `tags` the gate additionally computes **`filter_leak`** — the number of chunks in
the results whose record does not lie in the subtree of `category_id` or has none of the `tags`; any item
with `filter_leak > 0` is a FAIL regardless of recall. This proves that the filter does not let the foreign through,
without any manual labelling; `recall_at_k` on the same items proves that it does not
hide its own. The minimum set in `golden_set.local.json`: ≥3 items where the expected record
lies in a **project** and `category_id` is its **direction** (proof of expansion); ≥3 items with
`tags`; ≥2 with `tags` + `category_id`. We do not extend `slice`; we do not touch `compare.py`.

## Consequences

Easier: one module holds the filter semantics for HTTP and MCP; `retrieval.search`, the gate,
the copilot and the proxy tools do not change signatures; the mass rebuild of vectors is the already proven
path of Wave B. Harder: 15 mechanical edits in two processes (routine, but must be complete —
a missed place will look like "the project vanished from the filter"); the degradation window to FTS for the duration of
`run`; a node name unique across the whole table; the "two levels" requirement is checked in app code, not by
the schema. Accepted debt: `grep_archive` and `archive_grep` still do not filter `duplicate_of`
(a known hole from the wiki) — adding `category_id`/`tags` there, we do not touch duplicates.

## Invariants created

- **The category tree is exactly two levels deep.** `categories.parent_id` may only point at
  a row whose own `parent_id IS NULL`; the API rejects anything else (400). A node name is
  unique across the whole table (`name_norm`), not per parent.

  > **Replaced 2026-09-22 (v47, wave 6).** A node name is unique **among its siblings**
  > (`idx_categories_parent_name` UNIQUE on `(COALESCE(parent_id, 0), name_norm)`), not across
  > the table; `slug` stays globally unique. The two-level rule itself is unchanged. The
  > canonical textual name of a node is its full path "Direction → Project" (`path` in
  > `list_tree`); every reader that used to resolve a node by its bare name goes through
  > `categories.resolve_node`, which raises `ambiguous` with the list of paths instead of
  > picking "the first match", merging namesakes or answering empty.
- **Every category filter goes through `app.services.categories.filter_sql`.** A direction
  id means the direction plus all its projects; a project id means the project alone. No
  reader (HTTP, MCP direct-DB, retrieval, research, commitments, copilot) writes its own
  `category_id = ?`; `parse_filter` and `norm_name` are likewise single-sourced there. The
  module imports no torch/anthropic at module level so `mcp_server.py` can import it.
- **`category_id` is the only hierarchy parameter; `project` is never the hierarchy.**
  `project` in `ask_archive`/`scope_filter_ids` stays "graph entity or free text" and is not
  reused, renamed, or shadowed by a `project_id`.
- **Tags attach to `transcriptions` only; `record_tags` holds confirmed tags only.** Claude
  suggestions live in `transcriptions.suggested_tags_json` until accepted. `record_tags.source`
  records who set the tag (`manual|claude|chat`); unbinding a tag from a chat removes only
  rows with `source='chat'` for that chat. Deleting or merging a category never touches
  `record_tags`.
- **Every writer of `transcriptions.category_id` or `record_tags` returns the changed ids and
  schedules `reembed.schedule_records_reembed` after commit.** No raw
  `UPDATE transcriptions SET category_id` outside `app.services.categories`.
- **Point re-embed only touches records on the current `(embedding_model, embedding_version)`
  pair.** Stale records belong to `reembed.run`, which re-embeds them with current meta anyway;
  a batch of more than 20 ids runs as one job and calls `optimize_chunk_index` once at the end.
- **The context prefix carries `Напрямок → Проєкт` for every source type, then the
  type-specific tail, then `теги: …` (≤5 tags, ≤60 chars).** A change to the prefix format is
  a new `EMBED_VERSION`, rebuilt through `reembed.run` (snapshot → gate → live), never through
  per-record hooks.
- **Deleting or merging a category that still has projects is refused (409).** Reassign or
  delete the projects first; `audio_downloads.category_id` is nulled/moved together with the
  other three tables.
- **A filtered eval item with `filter_leak > 0` fails regardless of recall.** Any golden item
  carrying `category_id` or `tags` proves both that the filter hides nothing of its own
  (recall) and lets nothing foreign through (leak).

## Revisit when

- A third level is asked for (a project needs sub-projects) — `id = ? OR parent_id = ?`
  stops being a full expansion and the two-level check becomes a recursive CTE.
- The same project name is needed under two directions — the table-wide `name_norm` UNIQUE
  must become `(parent_id, name_norm)`.

  > **Came to pass 2026-09-22 (v47, wave 6).** It was done exactly this way:
  > `idx_categories_parent_name` UNIQUE on `(COALESCE(parent_id, 0), name_norm)`
  > (`COALESCE`, because NULLs do not conflict in a UNIQUE index — otherwise directions would remain
  > without uniqueness among themselves). The next revisit of this item is when a third
  > level is needed (the first item of the list).
- Tag filtering needs AND semantics ("AI and `інвестори`") or tag counts exceed a few hundred —
  the OR-only `tags` parameter and the flat `tags` table without hierarchy get reopened.
- `reembed.run` over the archive takes longer than an evening (archive ≫ 6k records) — the
  degradation window during the `EMBED_VERSION` bump needs a shadow-column strategy instead.
