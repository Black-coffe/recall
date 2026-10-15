---
domain: taxonomy
tags: [categories, tags, taxonomy, mcp, scope, telegram]
related: [map/services.md, map/blueprints.md, map/root.md, rag-context-integrity, entity-graph-provenance, adr/023-directions-projects-tags-taxonomy.md]
last-verified: 2026-09-23
---

# Taxonomy tree (direction → project) and tags: one resolver, canonical path, no silent merge

Introduced by spec `directions-projects-tags` (4.6.0, shipped 22-23.09.2026,
`docs/adr/023-directions-projects-tags-taxonomy.md`). `categories` is a tree of
exactly two levels (`parent_id`: `NULL` = direction, otherwise a project inside
one). `app.services.categories` is the *only* module allowed to build the SQL
fragment for it — see `memory/map/services.md` for the full function list
(`filter_sql`, `tags_sql`, `date_sql`, `resolve_node`, `list_tree`, the tag
CRUD family). This note states the invariants a model must not violate when
touching any of the ~15 call sites (HTTP, MCP direct-DB, retrieval, research,
commitments, scope, copilot).

**A node's canonical text identity is its full path, not its bare name.**
`list_tree` returns `path` ("Direction → Project" for a project, "Direction" for a
direction) for every node, and `resolve_node(conn, path) == id` holds for
every node returned by `list_tree` — this is the invariant a test in story 20
pins. Node names are unique only **among siblings** (`idx_categories_parent_name`
on `(COALESCE(parent_id,0), name_norm)`, migration v47 — the *first
non-additive migration* in this project, table rebuild under `PRAGMA
foreign_keys=OFF`), not across the whole table: "Marketing" under two
different directions can and does coexist. A node name containing the path
separator (`categories.PATH_SEP = "→"`) is rejected as `bad_name` before write,
which is what keeps every `path` string parseable back into the node it names.

**A bare (non-path) name is a valid lookup, but it is never resolved silently
when it is ambiguous.** `resolve_node(conn, text)` tries the text as a full
canonical path first (unique by construction, so a match there is unambiguous
by definition); only if that fails does it try the text as a bare name across
the whole tree: zero matches → `None`, exactly one → that node, two or more →
raises `CategoryError('ambiguous')` carrying `.candidates = [paths]`. There is
no "first match wins" branch anywhere in this pipeline. Every caller either
lets the exception propagate to its own "ambiguous, please disambiguate"
surface, or is a defect: HTTP returns 409 `{"error":"ambiguous","candidates":
[...]}`, MCP `ask_archive(project=...)` returns a note with `candidates`
instead of raising, `scope.py`'s Telegram chat-map arbiter logs the row as
skipped with the candidate list, and `copilot/worker.py._scope_entity_ids`
(history 25/27) shows the operator a one-time-per-session `scope_notice` card
and searches unscoped rather than silently returning nothing or picking one.
The one thing every one of these call sites is forbidden from doing is
swallowing `CategoryError('ambiguous')` into a plain empty result — that turns
"the archive has two namesake projects" into "nothing found here", which is
worse than the ambiguity itself.

**`--dry-run` on a write pass is not a second, hand-maintained code path.**
`scope.apply_chat_categories(..., dry_run=False)` (history 22/26) runs the
*exact same* creation/tagging code as the real pass, on the same connection —
every `categories.*` writer it calls takes `commit: bool = True` and gets
called with `commit=False` — and ends the transaction with `conn.rollback()`
instead of `conn.commit()` when `dry_run` is set. There is no separate
"preview" branch that computes what *would* happen from a different query;
divergence between preview and real runs was exactly the bug this rewrite
(history 26, prior council round 6 ESCALATE) closed. Any future writer that
adds a "preview mode" by re-deriving state instead of running the real path
with `commit=False` reintroduces that bug class.

**Renaming or reparenting a node invalidates every chunk's embedded prefix
under it, not just the node's own records.** `update_category` returns
`(node, ids)` where `ids` is the full subtree's living record ids whenever
`name` or `parent_id` changes — reparenting one path segment changes the
`"Direction → Project"` string baked into every descendant's context prefix
(see `rag-context-integrity.md`), so it is treated exactly like a rename, not
like a metadata-only update. Every `categories`/`tags` writer that mutates
what a record's prefix would render — including chat-level tag replacement
(`apply_chat_tags`) and tag rename (`update_tag`, only when `name` actually
changed) — returns the record ids it touched so the caller can hand them to
`reembed.schedule_records_reembed` (`rag-context-integrity.md`); nothing in
this module schedules re-embedding itself.

See `memory/map/services.md` (Taxonomy section) for the current function
inventory, `docs/wiki/rag-context-integrity.md` for what the path/tags do
inside the embedded prefix and the epoch-format guard that stops a
half-migrated prefix format from writing, and
`docs/wiki/entity-graph-provenance.md` for the sibling shape of "one span, one
owner, never silently repointed" in the entity graph.
