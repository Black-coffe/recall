# blueprints-memory — memory / research / telegram (`app/blueprints/`)

Part of the HTTP layer; overview — `blueprints.md`, media — `blueprints-media.md`. No `url_prefix`. Logic — `services.md`.

last-verified: 2026-10-15 (english-i18n 4.16.0: errors/statuses — `tr()` with keys `knowledge.*`/`telegram.*`; SSE
`/ask/stream` and `/research/summary` go through `localize_sse` (`memory.py:858`, `research.py:141`), see `blueprints.md`
§ i18n; line numbers below are from before 4.16.0, check against the code)

Gated (4.22.0, `approval_gate._D_ROUTES`, runs before the view): `memory.delete_category`, `merge_category`, `delete_tag_route`,
`merge_tags_route`, `start_backfill` (enrichment backfill), `telegram.tg_backfill`. `telegram.tg_deleted` (listener stream) is exempt. See `services-infra.md`.

last-verified: 2026-10-20 (mcp-archive-org 4.26.0, memory.py re-read)
4.26.0 agent rules in memory.py (`_is_ui` :45): category create stamps `categories.created_source='mcp'`, agent name <=60 (`AGENT_CATEGORY_NAME_MAX`); `bulk-category` for an agent: `ids` only, <=5 (`BULK_LIMIT`), private-direction exit -> 403 `PRIVATE_CATEGORY_CHANGE`/503 `PRIVACY_CHECK_FAILED`; `PUT /<tid>/tags` for an agent adds existing tags only (`source='mcp'`); action-items PATCH/`POST /action-items/<id>/postpone {days 1|7|30}` and `POST /action-events/<id>/undo` (agent: only `source='mcp'` events, `NOT_MCP_EVENT`/`UNDO_NOT_LAST`; agent repeats dedupe via `commitments.apply_action(dedupe=True)` -> `created:false`). The line below "Postponing a deadline from the web is not exposed" is stale.

## memory.py (~1260) — `/api/memory/*` (memory graph, RAG, categories, tags)
Depends on: enrichment, retrieval, rag, embeddings, categories, commitments.
- Categories: `GET /categories` (:198, `categories.list_tree`, a node carries `path`), `POST` (:269), `PATCH/DELETE
  /<cid>` (:296/336), `POST /<cid>/merge` (:354). `PATCH /transcriptions/<tid>/category` (:380), `GET
  .../suggest-category` (:398, k-NN), `POST .../bulk-category` (:411), `POST .../<tid>/enrich` (:628).
- Rules (4.6.0, ADR-023; direction→project tree, `parent_id`, v45): 409 `duplicate` (a duplicate among siblings,
  `conflict{id,path}`), 409 `has_projects` (deleting/merging a direction), `bad_name` on `→`; PATCH with a new `parent_id`
  or `name` → re-embed of the subtree; the response carries `reembed{mode,count,stale}` (`categories.schedule_records_reembed`).
- Tags: `GET/POST /tags` (:488/495), `PATCH/DELETE /tags/<id>` (:517/544), `POST /tags/merge` (:556), `PUT /<tid>/tags`
  (:574, `{add,remove}`), `POST /<tid>/tags/suggestions` (:598, `{accept,dismiss}` only from `suggested_tags_json`, otherwise 400).
- `POST /backfill` (:650) + `/backfill/status` (:712, SSE channel "backfill"), `POST /import` (:718).
- Search/RAG: `GET /search` (:741, hybrid), `POST /ask` (:763), `POST /ask/stream` (:809, SSE), `POST
  /ask/<ask_id>/rate` (:859, `{rating:1|-1,note?}` → `rag.rate_ask` → `ask_log`).
  `ask*` accept `channel` (`ui` default | `mcp`) — every successful question is logged (`rag._log_ask`);
  `search`/`ask*` accept `explain` (`?explain=1` or JSON, `_parse_explain()`; `why` survives in `sources`).
  `search`/`ask*`/`entities`/`action-items` accept `category_id` (expands the subtree), `tags` (comma, OR; with
  `category_id` — AND), `since`/`until` (`YYYY-MM-DD`, invalid → 400 from `categories.parse_date`).
  `ask*` `project=` is resolved by `categories.resolve_node` BEFORE rag: several matches → 409
  `{error:"ambiguous",candidates}`, rag is not called.
- `GET /entities` (:883), `/entities/<eid>` (:964), `GET /action-items` (:1039), `GET /stats` (:1241).
- `PATCH /action-items/<aid>` (:1220, K5 4.15.0) `{status: open|done|cancelled}`: anything else → 400 `tr('knowledge.memory.err.status_bad')`, missing
  → 404 (`LookupError`), 200 `{success:true}`; writes `commitments.apply_action(..., source='web', actor='web')` —
  the same path as the brief buttons, with an event in `action_item_events`. Postponing a deadline from the web is not exposed.
- Gotchas: entity `min_meetings=2` if there is no `q`; a category name is unique casefold AMONG SIBLINGS (v47); action-items
  period by `meeting_date`. The `owner` filter is an EXACT match on the canonical name OR graph aliases (the alias key is
  `enrichment._normalize`, not SQL `LOWER()`, which does not fold Cyrillic); the raw `owner_name` does not filter. MCP
  (`commitments.list_commitments`) is deliberately wider: a substring over canonical/raw/aliases.

## research.py (~130) — `/api/research/*`
Brand research (Phase 18), logic in `services/research.py`. `GET /preview` (:63, 0 tokens), `POST /originals`
(:87, markdown), `POST /summary` (:110, SSE map-reduce Haiku). All three accept `category_id`/`tags`/`since`/`until`
via `_parse_axes()` → `categories.parse_filter/parse_tags/parse_date`.

## telegram.py (~1130) — `/api/telegram/*`
Shared-secret auth; the media path is confined to `TELEGRAM_MEDIA_DIR`; dedup `(chat_id,message_id)`; backfill — embeddings without Claude.
- `POST /ingest` (:366, localhost+token): media — a placeholder row synchronously, the job does an `UPDATE` (`_finalize_media`),
  otherwise a message "in flight" is invisible to dedup/watermark. Whisper with `decode_options_for('telegram', db_path)`
  (:573-576). `meeting_date` from `tg_date` (`_meeting_date`, local day; v30 fixed old records).
- `GET /status` (:628), `/dialogs` (:637; listener rows carry `first_name/last_name/phone/bot/deleted`), `GET /chats`
  (:647, filter `category_id`, a row carries `tags`), `POST /chats` (:675), `POST /chats/toggle` (:767).
- `PUT /chats/<int(signed=True):chat_id>/tags` (:723) `{tags:[names]}` — a full replacement (`categories.apply_chat_tags`),
  retroactively to messages with `source='chat'`; unlinking removes only `source='chat'`.
- `GET /coverage` (:854): per monitored chat `archived_last_date/live_last_date/lag_days/unread_count/migrated_to` +
  `status` ok|behind|never_ingested|migrated|not_listed|unknown; the cursor comes from the listener's `iter_dialogs` (no second Telethon session);
  listener offline → 200 `live:false` (not 503, unlike `/dialogs`). `behind_messages` only for supergroups `-100…` (in the others
  `message_id` comes from the account's global sequence).
- `POST /edited` (:939), `/deleted` (:980), `/repair` (:1035; targeted repair of gaps by id via
  `get_messages(ids=…)`, `None` = deleted by the author; `-100…` only, for the others `/backfill`), `POST /backfill` (:1093, by dates).
