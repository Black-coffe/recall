# services — archive taxonomy (direction→project + tags)

last-verified: 2026-10-15

Part of `app/services/*`; hub — [services.md](services.md). Tier 4 `directions-projects-tags`, 4.6.0.

- **categories.py** — the single SQL module for the `categories` tree (exactly 2 levels via `parent_id`: `NULL` = direction, otherwise
  project) and tags (`tags`/`record_tags`/`tg_chat_tags`). Only stdlib + `app.db.connection` (no torch/anthropic/flask —
  safe from the stdio-MCP).
  - Filters: `parse_filter(raw)`/`filter_sql(column, value)` (a direction id expands into its subtree; `'none'`/`None`/int),
    `parse_tags`/`tags_sql` (OR over `name_norm`), `parse_date`/`date_sql` (`since`/`until` inclusive; `ValueError` with a Ukrainian
    text — the single source of the error for HTTP and MCP).
  - Names: `norm_name` (NFKC+strip+casefold) — the single key to `name_norm`; `name_keys(name, name_norm)` (also called by `scope.py`);
    `split_path`/`PATH_SEP="→"` — the single parser of "Direction → Project".
  - `resolve_node(conn, text)` (`:371`): first the exact canonical path, then the bare name (one match → it, several →
    `CategoryError('ambiguous')` with `.candidates=[paths]`, zero → `None`); never "first match".
  - `list_tree(conn)` — every node carries `path`; invariant `resolve_node(path) == id` (the same separator as in
    `build_context_prefix`).
  - `CategoryError` (`ValueError`) codes: `not_found|two_levels|self_parent|has_projects|duplicate|bad_parent|bad_name|
    not_suggested|ambiguous`.
  - Writers (`create_category`/`update_category`/`delete_category`/`merge_categories`/`set_category`/`ensure_tag`/
    `update_tag`/`delete_tag`/`merge_tags`/`set_tags`/`apply_chat_tags`) return the **ids of live records whose data actually
    changed** — this goes into `reembed.schedule_records_reembed`. Some have `commit: bool = True`; `commit=False` writes into the
    caller's transaction (dry-run = the same pass + `conn.rollback()`).
  - Suggested tags: `suggested_tags`/`set_suggested_tags`/`resolve_suggestions`; `inherit_chat_tags`.
  - *A node name is unique AMONG SIBLINGS (`idx_categories_parent_name` on `(COALESCE(parent_id,0), name_norm)`, migration
    v47 — a table rebuild under `PRAGMA foreign_keys=OFF`); "Маркетинг" under different directions coexist. A name
    with `→` → `bad_name` before writing. `reparent` and `rename` put the whole subtree into re-embed (the path in the prefix changes).
    Only `tags.name_norm` is globally unique.*
- Consumers: `research._parse_axes` (blueprints), `commitments._scope_clauses`, `scope.py`, `embeddings._load_prefix_meta`,
  `reembed.schedule_records_reembed`. Chat scope and auto-labelling — [services-enrichment.md](services-enrichment.md).
- `record_meta.py` (record title/description) — [services-media.md](services-media.md).
