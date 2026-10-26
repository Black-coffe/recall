# root-processes — telegram_*, mcp_server.py

last-verified: 2026-10-26 (4.33.0, mcp-triggers). `mcp_server.py` line numbers re-derived at 4953 lines; the telegram_* sections were not re-checked this pass.

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
**Purpose:** a separate MCP process (stdio by default), Claude Code/Desktop's bridge to Recall. Full control of the UI function under
the R/W/D model (ADR-042, Accepted 2026-10-26; ADR-051 recording exceptions; ADR-052 cap 100). `mailbox`/`settings` never carry a secret
(they ask app.py for a one-time connect link).
**Count: 95 tools = 56 R + 39 write (23 W + 16 D).** 18 read SQLite directly (`_ABOUT_DIRECT_DB` :4750), 77 proxy app.py through `_api()` :232
(`RECALL_API_URL`, default `http://127.0.0.1:5050`); a proxy without app.py answers `APP_UNREACHABLE`. `about()` :4787 and
`recall://about` take the count and write list from the FastMCP registry (`_registered_tool_names()` :4779). The number is pinned once,
against PRODUCT/CLAUDE/CLAUDE.vulyk/MCP_SETUP, in `tests/test_tool_count_docs.py`; feature tests no longer hardcode it.
**Write families** (`_TOOL_CLASS` limits family; D in brackets): recording `recording`, `copilot` [`recording_stop`, `recording_cancel_own`];
transcripts `transcribe`, `retranscribe`, `transcript`, `activate_transcript_version`, `audio_item`, `bookmark`, `video_analyze`
[`transcript_delete`, `enrich_backfill`]; intake `audio_download`, `mailbox`, `telegram` [`document_import_folder`, `document_reparse`,
`mailbox_backfill`, `mailbox_label_enrich`, `mailbox_disconnect`, `telegram_enable_chat`, `telegram_backfill`]; paid `document_ingest`,
`pipes`, `transcript_ai`; taxonomy `taxonomy` [`taxonomy_merge`, `taxonomy_delete`]; speakers `speaker` [`speaker_merge`, `speaker_delete`];
tasks `task`; comments `comment`; system `settings`, `job_cancel` [`settings_remove_anthropic_key`]; models `models`; diagnostics
`system_diagnostics`. W action tools dispatch on `action` (`INVALID_ACTION` otherwise) via `_w_call` :3567; bulk ids <= 5 (`BULK_LIMIT`).
**Registry:** `_TOOL_CLASS` :522, one entry per tool `{class R|W|D, provenance_kind, open_world, limits (None iff R), args_allow}`;
`PROVENANCE_KINDS` :513; `_derive` :695 -> `ToolAnnotations` + `_meta` (`recall/class`, `recall/provenance_kind`; D adds
`anthropic/requiresUserInteraction`). `@_tool` :716 raises `KeyError` for a function with no entry and registers only when `_registers(name)`
:707. **Profiles** (at import): `RECALL_MCP_PROFILE` `full` (default) | `read` (R + `_READ_PROFILE_EXTRA={"video_analyze"}` :692; unknown
value -> `read`, `_read_profile` :167); `RECALL_MCP_READONLY=1` (`_WRITE_DISABLED` :160) registers no W/D tool.
**Write path:** `_api(..., write=True)` sends `X-Recall-Actor: mcp` + `X-Recall-Tool`, stored by app.py as claimed labels in `mcp_actions`
(`services-infra.md`). W ceiling `_w_ceiling` :216 over `_W_WINDOWS` :213 (per family, in-process deque) -> 429 `RATE_CEILING` with
`retry_after_sec`: a loop guard, not shared between MCP processes. D tools go through `_gated` :3486 (sets `_CURRENT_TOOL`, longer
timeout, adds the dialog hint to `APPROVAL_DENIED|APPROVAL_UNAVAILABLE|APPROVAL_PENDING|RECORDING_ACTIVE|RATE_CEILING`); the dialog itself
is app.py's (`approval_gate`, 45 s timeout = denial). Jobs: `_with_job` :2377 adds `job` for `wait_seconds` 0..25.
**Middleware** (FastMCP instance :503, outermost first): `_SecretGuardMiddleware` :450 (`secret_shape.find_secret_args(strict=class != "R")`
-> `SECRET_IN_ARGUMENT`, names only), `_ErrorEnvelopeMiddleware` :471 (exception -> `INTERNAL_ERROR`, pydantic -> `INVALID_ARGUMENT`),
optional `_CallLogMiddleware` :393 (`RECALL_MCP_DEBUG_LOG=1` -> `logs/mcp_calls.log`, START/OK/EXC with response size).
**Error envelope:** `_err()` :183 -> `{ok:False, error_code, error, hint?, status_code?}`; `_handle_resp` :276 maps >=400 via
`error_codes.code_for_status` (a non-JSON body is not passed through); `_as_envelope` :195 keeps server keys. Table: `app/core/error_codes.py`.
**Prompt (4.33.0):** `post_digest(pipe="weekly-digest")` :4437, `mcp.prompt` only `if _registers("pipes")` (full profile). Not a tool, runs
nothing: returns text telling the model to call `pipes(run)` then poll `get_job_status`; unknown name -> the configured list. Names from
`app.services.pipes.load_pipes()` (lazy). Claude Code: `/mcp__recall__post_digest`. Guard `tests/test_mcp_prompts.py`.
**Resources:** `recall://about`, `recall://transcript/{id}` :4914 (full raw text, no budget), `recall://entity/{id}`.
**Token budget:** chars/3 estimate (no tokenizer: a model load hangs stdio), `DEFAULT_MAX_TOKENS=40000` :738, `max_tokens<=0` = no limit.
`get_transcript` :1176 -> `_fit_transcript` :1276 (metadata always, one text `polished|raw`, then thread, then segments; page with
`budget.next_offset`/`segments_next`); `search_archive` :1002 -> `_fit_chunks` :1073 (overflow -> text-free `locators`).
**Notable tools:** `grep_archive` :1453 (literal/regex over `chunks`, screen chunks hidden); `ask_archive` :2027 (PAID, `channel="mcp"` ->
`ask_log`, `project=`); `wait_for` :2415 (long poll `GET /api/automation/events`; channels `recording:<sid>`, `doc_import`, `backfill`,
`pipes`, `system:<name>`; pass `next` back verbatim). Live tools resolve the session with `_resolve_live_session` :2839
(`AMBIGUOUS_SESSION` when >1); recording tools use `_live_or_given` :4481; `recording(start)` always raises the owner's toast first
(`NOTIFY_UNAVAILABLE` -> nothing starts) and is clamped (default 180 min, max 240); an owner session answers `OWNER_SESSION`.
Visual tools go through `_visual_refusal` :2452 (`RECALL_MCP_VISUAL` images|text|off, private directions, stop words; `services.md` -> privacy).
**Client side (4.33.0):** skills `skills/recall` (reads), `recall-record`, `recall-record-stop` (owner-typed), `recall-transcribe`,
`recall-digest`, `recall-video`, copied into `~/.claude/skills/`; registrations `recall`/`recall-ro`/`recall-scheduled` and hook recipes in
`docs/MCP_SETUP.md` and `docs/recipes/hooks.md`.
**Env:** `RECALL_API_URL`, `RECALL_MCP_PROFILE`, `RECALL_MCP_READONLY` (`app/core/settings.py`), `RECALL_MCP_KEY` (http transport only),
`RECALL_MCP_DEBUG_LOG`, `RECALL_MCP_VISUAL`, `RECALL_PRIVATE_CATEGORIES`, `RECALL_PRIVATE_WORDS_FILE`; `.env` from the script's directory.
**Language:** the MCP surface is English (`_set_process_lang()` :109, skipped under pytest).
**Gotchas:** ML imports are lazy (a torch/e5 load times out the stdio health check); `error_codes` and `secret_shape` are stdlib-only for the
same reason. `--transport http` = 127.0.0.1:5060 with Bearer `RECALL_MCP_KEY` (refused without a key). `_safe_original_path()` :876 is the
anti-traversal check for `get_original_file`.
