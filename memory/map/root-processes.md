# root-processes — telegram_*, mcp_server.py

last-verified: 2026-10-16
Adjacent slice: `root.md` (app.py, config, state, whisper).

## telegram_listener.py
**Purpose:** a standalone process. Telethon MTProto, listens to monitored chats, pulls media, POSTs to Flask `/api/telegram/ingest`;
a control API (localhost, port `TELEGRAM_CONTROL_PORT`, default 5051 :129) for Flask.
**Entry points:** `run_listener(cfg)` :867; `_start_control_api` :782 — `GET /status` :810, `GET /dialogs` :812,
`POST /repair` :821, `POST /backfill` :840 (token `X-Telegram-Token`, :793). `_ingest_message` :362, `_catchup` :667
(watermarks `_watermarks` :617, state in a file), `_backfill_task` :493, `_ctl_repair` :546.
`/dialogs` each row carries `_identity_fields(ent)` :449: `first_name/last_name/phone/bot/deleted` (None for groups/channels).
Reliable delivery: `_post_ingest_reliable` :282 + dead-letter (`_deadletter_append` :271, `_replay_deadletter` :312).
CLI (`main` :1065): `list | enable <id> | disable <id> | status`.
**Gotchas:** a separate process with inherited stdout/stderr. Asyncio in one thread — control handlers go through
`run_coroutine_threadsafe`. Dedup by (chat_id,message_id) on the Flask side. A Telethon session cannot be opened by two clients —
Flask goes through HTTP only.

## telegram_login.py
**Purpose:** a one-time interactive QR login (+2FA, `_complete_2fa` :42), creates `telegram.session`. `run` :52, `main` :111.
**Gotchas:** the QR PNG contains a token → `os.remove` :95 after login/timeout. The 2FA prompt needs a real terminal.
`os.startfile()` :73 — Windows only.

## telegram_common.py
**Purpose:** a shared secret for localhost Flask↔listener. `control_token()` :23, `CONTROL_TOKEN_HEADER="X-Telegram-Token"` :19.
**Gotchas:** the token file is `O_CREAT|O_EXCL` (race-free), mode 0o600 (:32). Localhost IPC only.

## mcp_server.py
**Purpose:** a separate MCP server, a read-first bridge to Recall for Claude Code/Desktop. No create/update/delete
(owner's decision 02.07.2026; write tools removed).
**41 read-only tools** (17 direct-DB + 24 proxy; checked against the tuples). The number in `about()` comes from the FastMCP registry
(`_registered_tool_names()` :2228).
**Hybrid:** 17 tools read SQLite directly without app.py (`_ABOUT_DIRECT_DB` :2210; among them `grep_archive`, `list_pipe_runs`);
24 are httpx proxies to app.py (`_ABOUT_PROXY` :2217) via `_api()` :155, `RECALL_API_URL` (default `http://127.0.0.1:5050`).
**Transports:** stdio (default :2308; `claude mcp add ... -- ... --transport stdio` drops trailing arguments, hence the default) or
`--transport http` (127.0.0.1:5060, Bearer `RECALL_MCP_KEY`; without a key — startup refused :2313).
**Resources:** `recall://about`, `recall://transcript/{id}`, `recall://entity/{id}`.
**Language (english-code, 4.17.0):** the whole MCP surface is English (tool descriptions, responses, logs). `_set_process_lang()` :103
sets `RECALL_UI_LANG=en` at import (skipped under pytest, :109), so `tr()` server texts come out `en`; every `_api` call sends
`_LANG_HEADERS = {"X-Recall-Lang": "en"}` :142. Archive content (transcripts, summaries) stays in its source language.
**Env:** `RECALL_API_URL`, `RECALL_MCP_READONLY` (:150; blocks writes in `_api`, effectively no effect), `RECALL_MCP_KEY`,
`RECALL_MCP_DEBUG_LOG=1` (call log in `logs/mcp_calls.log`), `RECALL_MCP_VISUAL` (`images` default|`text`|`off`),
`RECALL_PRIVATE_CATEGORIES`, `RECALL_PRIVATE_WORDS_FILE`. `.env` is loaded from the script's directory.
**Tool groups:**
- Search: `search_archive` :527 (removes chunks with `speaker='екран'`), `grep_archive` :749 (literal/regex over `chunks` without
  embeddings; `_grep_hide_screen` :816), `ask_archive` (sends `channel="mcp"` to `/api/memory/ask` → `ask_log`; `project=` a bare
  name or a full path; 409 `ambiguous` → a note with `candidates`).
- Taxonomy: `list_categories` = the `categories.list_tree` tree (a node has `path`). `tags`/`since`/`until` in
  search/list_recent/list_action_items/ask/research_*/search_history; direct-DB SQL only via
  `app.services.categories.filter_sql/tags_sql/date_sql`.
- Commitments: `weekly_digest`, `list_action_items`, `list_dropped_commitments`, `list_stale_topics` (`app.services.commitments`).
- `display_name` next to `source_name` in `list_recent`/`get_transcript`/`search_archive`/`ask_archive`/the transcript resource;
  `get_transcript`/`list_recent` add `title`/`description`.
- Pipes: `list_pipe_runs(pipe=None, limit=20)` :1153 (limit 1..200) → `{"runs": pipes.list_runs(...)}`; without the table
  `pipe_runs` → `{"runs": [], "reason": ...}` :1168. Brief texts are not stored.
- Live: `_resolve_live_session` :1819; `get_live_transcript`/`get_live_copilot`/`ask_live` without `session_id` when there is >1 recording →
  `error_code:'AMBIGUOUS_SESSION'`; `get_active_recording` carries `count`+`sessions`.
- Video (proxy, :1564-1741): `video_analyze` :1565 → job (`get_job_status`), `get_visual_timeline`, `get_frame` :1657
  (base64, up to 12 frames), `get_frames_for_text`, `search_visual` :1742. Skill `skills/recall-video/SKILL.md`.
- Privacy (`mcp-visual-privacy`): all 5 video tools go through `_visual_refusal` :1526 → `error_code`
  `MCP_VISUAL_OFF|MCP_VISUAL_TEXT_ONLY|PRIVACY_CHECK_FAILED|PRIVATE`; `_stop_words()` :1549 (None = the file could not be read →
  hide everything with text), `_visual_text_hidden` :1558. `get_frame` hides a frame without `frame_text` or with a stop word;
  `search_visual` checks the full OCR/description from the DB. Logic — `services.md` → privacy.py.
**Gotchas:** ML imports (retrieval→torch) are lazy — otherwise the Claude Code stdio health check runs into a timeout. Proxy tools
without app.py → `{"error": ...}`. `_safe_original_path()` :414 — anti-traversal against allowed-dirs.
