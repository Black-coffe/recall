# root-processes — telegram_*, mcp_server.py

last-verified: 2026-10-25 (live-video-frames 4.30.0: tool count + 2 tools); 2026-10-21 (mcp-pipeline 4.27.0: counts/registry block only); 2026-10-20 (mcp-archive-org 4.26.0: tool registry/counts/new tools re-read; mcp_server.py line numbers below :500 shifted by up to ~900, trust function names, `grep` them); 2026-10-19 (4.24.0)

**4.30.0 (live-video-frames): 82 tools = 51 R + 31 W/D.** New R (class `ocr`, `_TOOL_CLASS` :559-560, in the read profile :4239): `get_live_video(session_id)`, `get_live_frames(session_id, t | start,end,mode,step,min_res,limit<=12,cursor,track,size)`
(`mcp_server.py:3156,3180`; proxies `GET /api/recording/<sid>/live-video|live-frames`, timeout 180 s). `_live_visual_refusal` refuses only `RECALL_MCP_VISUAL=off` (`MCP_VISUAL_OFF`); a running session has no direction, so only the stop-words apply
(frame with a stop word or no OCR text -> dropped, `private_hidden`); `images` -> text head + Image parts, `text` -> text only. Not live -> `available:false` + hint to `get_frame`. Page with `next_t` -> `cursor`.
**4.27.0 (mcp-pipeline): 80 tools = 49 R + 31 W/D** (counted from the registry guard: `_ABOUT_DIRECT_DB` :4099 18 direct-DB + `_ABOUT_PROXY` :4106 rest; the 4.26.0 counts below are superseded).
New R: `get_youtube_info`, `list_audio_library`. New W (`_TOOL_CLASS.update` :636): `audio_download`, `audio_item`, `transcribe`, `document_ingest`, `job_cancel`, `transcript_ai`, `models`, `system_diagnostics`.
New D: `document_import_folder`, `document_reparse`. New rate families: `paid` (20/day: `document_ingest`, `transcript_ai`), `models` (2/day, both actions share it), `diagnostics` (1 per 10 min); `transcribe` is `transcripts` + `paid`.
`models` dispatches `download` (-> `/api/models/download-job`) and `check_updates` (:4040). `get_job_status` gained `wait_seconds`, `include_log`, `tail`, list filters.
**4.26.0 (mcp-archive-org): 68 tools = 47 R + 11 W + 10 D** (18 direct-DB + 50 proxy, `_ABOUT_DIRECT_DB`/`_ABOUT_PROXY` :3734-3754). New R `transcript_suggest_meta` (`POST /api/history/<id>/suggest-meta`, 409 `RECORDING_ACTIVE`).
New W (`_entry(cls, family, args_allow, cap=)` :609 + `_TOOL_CLASS.update` :617; call helpers `_w_call` :3268, `_bad_action`, `_missing`, `_int_ids` (<=`_AGENT_BULK_MAX`=5 else `BULK_LIMIT`), `_batch`, `_duplicate_as_existing` :3408), all action-dispatch, `INVALID_ACTION` for a foreign action:
`task` (done|cancelled|open|postpone(1|7|30)|undo; family `tasks` cap 30; `NOT_MCP_EVENT`/`UNDO_NOT_LAST`), `bookmark` (create|update|delete; hard delete returns `deleted_snapshot`; note<=2000),
`comment` (create|update|delete|restore; family `comments` 20; agent rows `source=mcp`, no boost until owner confirms; `OWNER_AUTHORED` on owner rows), `taxonomy` (create_category|update_category|create_tag|update_tag|assign_category|set_tags; existing tags only; duplicate create -> `{ok,created:false,existing_id}`),
`speaker` (create|rename|map_transcript (existing ids only)|unmerge; `SPEAKER_ID_REQUIRED`, `IS_SELF`), `transcript` (edit_meta -> `meta_source=mcp`, `JOB_ACTIVE`; restore).
New D via `_gated(..., timeout=60)` (default timeout 90): `taxonomy_delete`, `taxonomy_merge`, `speaker_merge`, `speaker_delete`, `transcript_delete` (soft), `enrich_backfill` (limit 1..200, paid, open-world).
`retranscribe` is still W but the route is conditionally gated (dialog when the run would drop a version, `services-infra.md`). `list_comments` returns `source`/`confirmed_at`; `list_recent`/`search_history` take `deleted=true` (only soft-deleted). Counts text below this block (55 = 46+5+4, "3 write tools") is superseded.
Adjacent slice: `root.md` (app.py, config, state, whisper).

## telegram_listener.py
**Purpose:** a standalone process. Telethon MTProto, listens to monitored chats, pulls media, POSTs to Flask `/api/telegram/ingest`;
a control API (localhost, port `TELEGRAM_CONTROL_PORT`, default 5051 :129) for Flask. `.env` is loaded by explicit path next to the script (:102).
**Entry points:** `run_listener(cfg)` :872; `_start_control_api` :787 — `GET /status` :815, `GET /dialogs` :817,
`POST /repair` :826, `POST /backfill` :845 (token `X-Telegram-Token`, :798). `_ingest_message` :362, `_catchup` :672
(state in a file), `_backfill_task` :498, `_ctl_repair` :551.
`/dialogs` each row carries `_identity_fields(ent)` :454: `first_name/last_name/phone/bot/deleted` (None for groups/channels).
Reliable delivery: `_post_ingest_reliable` :282 + dead-letter (`_deadletter_append` :271, `_replay_deadletter` :312).
CLI (`main` :1070): `list | enable <id> | disable <id> | status`.
**Gotchas:** a separate process with inherited stdout/stderr. Asyncio in one thread — control handlers go through
`run_coroutine_threadsafe`. Dedup by (chat_id,message_id) on the Flask side. A Telethon session cannot be opened by two clients —
Flask goes through HTTP only.

## telegram_login.py / telegram_common.py
`telegram_login.py`: one-time interactive QR login (+2FA :43) -> `telegram.session`; the QR PNG is removed (:95); Windows only `os.startfile()`.
`telegram_common.py`: `control_token()` :23 (file `O_CREAT|O_EXCL`, 0o600), header `X-Telegram-Token` :19 (also read by `audit_hooks`).

## mcp_server.py
**Purpose:** a separate MCP server, a bridge to Recall for Claude Code/Desktop. Direction is full control of the UI function tool by
tool (ADR-042, supersedes read-first); today 5 WRITE tools (`video_analyze`, `mailbox`, `settings`, `retranscribe`, `activate_transcript_version`) and 4 DESTRUCTIVE tools (`mailbox_backfill`,
`mailbox_label_enrich`, `mailbox_disconnect`, `settings_remove_anthropic_key`). `mailbox`/`settings` cannot carry a
secret (they only ask app.py for a one-time link). The module docstring and `about()` state the profile and counts truthfully.
**55 tools = 46 R + 5 W + 4 D** (18 direct-DB + 37 proxy; checked against `_ABOUT_DIRECT_DB` :3239 / `_ABOUT_PROXY` :3246). `about()` :3267
takes the count and the write list (W and D) from the FastMCP registry (`_registered_tool_names()` :3259), plus `profile`; its header
says "N read-only + M write" with D counted as write.
**Hybrid:** 18 tools read SQLite directly without app.py (among them `grep_archive`, `list_pipe_runs`, `list_mcp_actions`); the rest are httpx proxies
to app.py via `_api()` :231, `RECALL_API_URL` (default `http://127.0.0.1:5050`).
**Transports:** stdio (default; `claude mcp add ... -- ... --transport stdio` drops trailing arguments, hence the default, :3016) or
`--transport http` (127.0.0.1:5060, Bearer `RECALL_MCP_KEY`; without a key — startup refused, `__main__` at the file end).
**Registry (4.20.0):** `_TOOL_CLASS` :517 — one entry per tool `{class R|W|D, provenance_kind, open_world, limits (None iff R),
args_allow}`; `PROVENANCE_KINDS` :508; `_derive(entry)` :586 -> `ToolAnnotations` + `_meta`
(`recall/class`, `recall/provenance_kind`; D adds `anthropic/requiresUserInteraction`). `@_tool` :607 raises `KeyError` for a function
with no entry and skips registration when `_registers(name)` :598 is false. **Profiles** (decided at import): `RECALL_MCP_PROFILE`
`full` (default) | `read` (reads + `_READ_PROFILE_EXTRA={"video_analyze"}` :583; unknown value -> `read` + stderr warning, `_read_profile`
:166); `RECALL_MCP_READONLY=1` (`_WRITE_DISABLED` :159) registers NO W/D tool, and `_api(write=True)` still returns `MCP_READONLY`.
**Write path:** `_api(..., write=True)` sends `X-Recall-Actor: mcp` + `X-Recall-Tool` (from `_CURRENT_TOOL`); app.py stores both as
claimed labels in `mcp_actions` (`services-infra.md`). **W ceiling (4.22.0):** before the request `_w_ceiling(limits)` :215 over
`_W_WINDOWS` :212 (per family, in-process deque, `limits` of the tool's `_TOOL_CLASS` entry) -> `RATE_CEILING` 429 envelope with
`retry_after_sec`; a response <400 appends to the window (:266). A loop guard, not shared between MCP processes. The comment at :505
("nothing enforces them yet") is stale for W limits. Middleware order (outermost first, :496): `_SecretGuardMiddleware`
:445 (`secret_shape.find_secret_args(strict=class != "R")`: the bare 16-letter rule only on W/D or unregistered tools; refuses with
`SECRET_IN_ARGUMENT`, names only), `_ErrorEnvelopeMiddleware` :466 (unhandled exception -> `INTERNAL_ERROR`, pydantic -> `INVALID_ARGUMENT`;
sent as `isError` `_EnvelopeResult`), optional `_CallLogMiddleware` :388 (its `OK` line ends with the response size from
`_result_size` :377, e.g. `48.2k chars`; START/OK/EXC lines).
**Error envelope:** `_err()` :182 -> `{ok:False, error_code, error, hint?, status_code?, ...extra}`; `_handle_resp` :271 maps a >=400 reply
(`error_codes.code_for_status`; a non-JSON error body is NOT passed through, only `HTTP <status>`); `_as_envelope` :194 keeps server keys.
A gated D route answers `APPROVAL_DENIED|APPROVAL_UNAVAILABLE|APPROVAL_PENDING|RECORDING_ACTIVE` through the same mapping.
Connection errors: `APP_UNREACHABLE`, `TIMEOUT`. Code table — `app/core/error_codes.py` (`services-infra.md`).
**Resources:** `recall://about` :3021, `recall://transcript/{id}` :3099 (full RAW text, no budget: `get_transcript(max_tokens=0, prefer_raw=True)`),
`recall://entity/{id}`.
**Token budget (4.21.0):** chars/3 estimate (`_CHARS_PER_TOKEN`, no tokenizer: a model load hangs stdio), `DEFAULT_MAX_TOKENS=40000` :629,
`max_tokens<=0` = no limit (`_budget_chars` :634); `_BUDGET_RESERVE=600` :630 chars kept for the `budget` block; `_page_end` :648 snaps a
page back <=500 chars to a line/sentence end. Every `budget` has `budget_tokens, approx:true, approx_tokens_used, truncated`; a cut response
also has top-level `truncated:true`.
- `get_transcript(..., max_tokens, offset, segment_offset, prefer_raw)` -> `_fit_transcript`: metadata/summary/email/comments always, then ONE
  text (`text_source` `polished|raw`), then `thread.messages`, then segments. Paging: `budget.next_offset` -> `offset`, `budget.segments_next`
  -> `segment_offset`; `text_*`, `*_omitted` fields report the cut.
- `search_archive(..., max_tokens)` -> `_fit_chunks`: chunks in rank order; overflow becomes text-free `locators` (`_LOCATOR_KEYS`), the
  rest only counted (`budget.shown, as_locators, dropped`). Open a locator via `get_transcript`.
**Language:** MCP surface is English: `_set_process_lang()` :108 (skipped under pytest); `_api` sends `_LANG_HEADERS` :149.
**Env:** `RECALL_API_URL`, `RECALL_MCP_PROFILE`, `RECALL_MCP_READONLY` (both in `app/core/settings.py:652-656`), `RECALL_MCP_KEY`,
`RECALL_MCP_DEBUG_LOG=1` (`logs/mcp_calls.log`), `RECALL_MCP_VISUAL` (`images` default|`text`|`off`), `RECALL_PRIVATE_CATEGORIES`,
`RECALL_PRIVATE_WORDS_FILE`. `.env` is loaded from the script's directory.
**Tool groups:**
- Search: `search_archive` (removes chunks with `speaker='екран'`), `grep_archive` :1299 (literal/regex over `chunks`; `_grep_hide_screen`
  :1366), `ask_archive` :1844 (`channel="mcp"` -> `ask_log`; `project=` bare name or full path; 409 `ambiguous` -> `candidates`).
- Taxonomy: direct-DB `tags`/`since`/`until` SQL only via `app.services.categories.filter_sql/tags_sql/date_sql`.
- `wait_for(channel, events?, after?, timeout=25)` :2146 (R, `system`): long poll `GET /api/automation/events` (`blueprints.md` §
  automation.py), client timeout `timeout+10`; pass the returned `next` back verbatim; result `{events[], next, timed_out, gap, reset}`.
- Re-transcribe (4.25.0, ADR-044, all proxies; class entry `list_whisper_models` :583): `list_whisper_models` :2297 (R, `GET /api/models` filtered to
  `downloaded`), `retranscribe(transcription_id, model, language?, diarize?)` :2313 (W, `POST /api/history/<id>/retranscribe`; returns
  `job_id`, `ALREADY_RUNNING` -> `{already_running, job_id}`, other refusals pass through; poll with `get_job_status`/`wait_for`),
  `transcript_versions(transcription_id, version_id?)` :2342 (R), `activate_transcript_version` :2358 (W). `_READ_PROFILE_EXTRA` unchanged.
- `video_analyze` :2222 is a W tool (`provenance_kind=metadata`, `open_world`, declared `paid`, family `transcripts` 10/600 s); it sets
  `_CURRENT_TOOL`. Mail/settings WRITE: `mailbox(action, ...)` :2865 (`connect` -> `POST /api/mail/connect-link`; `refresh_labels`;
  `label_set`; `sync` months<=12; `INVALID_ACTION` for a foreign action; no `enrich`, no password argument; family `intake` 10/600 s),
  `settings(action, key, value)` :2933: `set` (`_SETTINGS_KEYS` :2929: `ui_lang` auto|uk|en -> `POST /api/settings/ui-lang`;
  `copilot_enabled` bool -> `POST /api/settings/copilot/toggle`; value type must match exactly), `test_anthropic_key` (one paid call,
  so its entry is `open_world` + `paid`, family `system`) and `connect_anthropic_key`. The user types the secret on `/connect/...`
  (`blueprints.md` § connect.py). Caller limits in `mail.py`.
- D tools (4.24.0), all through `_gated()` :2975 (sets `_CURRENT_TOOL`, `_api(..., timeout=90.0)`, adds `_GATE_NOTE` hint to
  `APPROVAL_*|RECORDING_ACTIVE|RATE_CEILING` envelopes): `mailbox_backfill(months 13..120, account_id?)` :2986 -> `POST /api/mail/backfill`;
  `mailbox_label_enrich(label_id, enrich)` :3005 (true -> gated `POST /api/mail/labels/<id>/enrich`; false -> ungated `PATCH enrich:false`);
  `mailbox_disconnect(account_id)` :3020 -> `DELETE /api/mail/accounts/<id>/credentials`; `settings_remove_anthropic_key()` :3032 ->
  `DELETE /api/settings/anthropic-key`. No MCP tool for the hard mail delete (`mailbox_delete` is UI-only). The class/limits table is the
  `_TOOL_CLASS` entries at :575-595.
- `system_status(section="summary")` :3062 (R, `system`): `summary|health|ready|hardware|stats|polish|models|settings` over `_STATUS_ROUTES`
  :3041 (GET proxies; `/api/ready` 503 is returned as status, `_status_get` :3053); never returns the Anthropic key, no paid call.
- Email (4.18.0): `email_status` :2831 proxies `GET /api/mail/status`, whitelisted keys only. For `source_type='email'`
  `get_transcript`/`list_recent`/`search_history` add `email{...}` (`_email_blocks` :982; direct DB), `get_transcript` carries `thread`
  (`_email_thread` :1017) and `attached_to` (`_attached_to` :1042). Logic — `services-mail.md`.
- Pipes: `list_pipe_runs(pipe=None, limit=20)` :1703 -> `{"runs": ...}`; without the table `{"runs": [], "reason": ...}`.
- Audit (4.22.0): `list_mcp_actions(since, actor, decision, action, path_prefix, status_min, limit=50, before_id)` :1721 (R, `system`, direct
  DB over `mcp_actions`, newest first, limit 1..200, `next_before_id` pages back; `claimed` labels are `trusted:false`). The same filter
  also lives in `repositories/mcp_actions.query` (the tool does not call it). Wiki — `approval-gate.md`.
- Live: `_resolve_live_session` :2480; `get_live_transcript`/`get_live_copilot`/`ask_live` without `session_id` and >1 recording ->
  `error_code:'AMBIGUOUS_SESSION'`; `get_active_recording` carries `count`+`sessions`.
- Video (proxy, :2222-2450): `video_analyze` -> job, `get_visual_timeline`, `get_frame` :2319, `get_frames_for_text`, `search_visual` :2404.
  All 5 go through `_visual_refusal` :2183 (`MCP_VISUAL_OFF|MCP_VISUAL_TEXT_ONLY|PRIVACY_CHECK_FAILED|PRIVATE`; `_stop_words` :2206,
  None = hide all text). Logic — `services.md` -> privacy.py; skill `skills/recall-video/SKILL.md`.
**Gotchas:** ML imports (retrieval->torch) are lazy — stdio health check times out otherwise; `error_codes` and `secret_shape` are stdlib-only
for the same reason. Proxy tools without app.py -> `APP_UNREACHABLE` envelope. `_safe_original_path()` :767 — anti-traversal.
