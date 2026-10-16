# overview — architecture and processes

last-verified: 2026-10-16 (4.18.0, schema v52)

## Three processes

Recall is a **monolithic Flask server** + **2 separate child processes**:

1. **`app.py`** — the main Flask app (port 5050). All HTTP APIs, background tasks,
   recording, copilot, RAG. Global state lives in `app/state.py` singletons.
2. **`telegram_listener.py`** — a separate process (Telethon/MTProto). Started by
   `app.py` via `subprocess.Popen` (if `TELEGRAM_ENABLED` + a `telegram.session` exists).
   Control API on localhost:5051; posts messages back to Flask `/api/telegram/ingest`.
3. **`mcp_server.py`** — a separate MCP server: a **read-first** bridge to Recall for
   Claude Code/Desktop (42 read-only tools; the write surface was removed in Wave 1). The stdio connector (**default**) is spawned by the client itself
   (`claude mcp add`, user-scope) — no need to start it separately; http+key is optional.
   Hybrid: reads SQLite directly, while writes/live/AI actions are proxied to the running `app.py`.

Gmail ingest (4.18.0) is NOT a fourth process: `MailPoller` is a daemon thread inside `app.py` (`_maybe_start_mail`, IMAP read-only,
password in the OS keyring) — see `services-mail.md`.

All three read one SQLite DB (`whisper_history.db`) and the shared `app/services/*`
modules directly. MCP reads the DB without Flask, but for writes/live/AI actions
proxies HTTP to the running `app.py` (hybrid).

## Startup sequence (`app.py`)

1. `.env` → logging (RotatingFileHandler + console) → silencing noisy libraries.
2. Flask + CORS (localhost:5050 only) → creating folders (upload/transcripts/
   youtube/documents/telegram_media/recordings).
3. `whisper_manager` (backend auto-detect: faster > openai).
4. DB + migrations (`app/db/migrations.py`).
5. Background daemons: `system_monitor`, `model_updates` (staleness-gated), opt. `file_cleanup`.
6. `recording_service` (up to `RECORDING_MAX_ACTIVE` parallel sessions) + recovery of orphaned sessions (if `RECORDING_ENABLED`).
7. `live_transcribe_worker` (model='small', interval 8s) — if there is a recording_service.
8. `copilot_service` + `copilot_worker` — if `COPILOT_ENABLED` + an LLM is available.
9. `app.state.init(**singletons)` — injection of all singletons.
10. Registration of 16 blueprints (WITHOUT url_prefix — full paths in the routes).
11. Attaching rate limiters to specific view functions.
12. Spawn of telegram_listener (gated by `WERKZEUG_RUN_MAIN` so the reloader does not duplicate it); call watcher, pipes and
    mail poller start only under `__main__` (reloader child).
13. `app.run(threaded=True)` — each SSE connection in its own thread.

**Shutdown** (atexit): stop system_monitor → stop file_cleanup →
`executor.shutdown(wait=True)` → kill telegram_listener → flush logs.

## Cross-cutting mechanisms

- **SSE**: in-memory pub/sub `sse_broker`. Endpoint `/api/events/<process_id>`.
  `ThreadSafeProgressStore` and `ThreadSafeProcessLogs` auto-publish to the broker.
  Terminal events (`complete`/`error`) close the connection. **The frontend reads SSE via
  fetch+reader, NOT EventSource** (werkzeug keep-alive deadlock — see frontend.md).
- **Background tasks**: `job_queue` on top of `ThreadPoolExecutor(max_workers=2)`. State machine
  queued→running→completed/failed/cancelled. Cancel is cooperative (`job.is_cancelled()`).
- **Language**: code, comments and logs are English; user-facing server texts via `tr()` (uk/en catalog `app/core/i18n_catalog/`,
  incl. `svc_*` service modules); MCP runs with `RECALL_UI_LANG=en` + `X-Recall-Lang: en` (see root-processes.md); guard `tests/test_code_english.py`.
- **Logs**: `whisper_app.log` (10MB×5). anthropic/httpx/hf are silenced; torchaudio→CRITICAL.
- **Rate-limit**: flask_limiter memory-backed, per IP. 429 → JSON.
- **Security**: security headers on HTML only (CSP not on JSON/files, so as not to break fetch);
  path-traversal guard; bind defaults to `127.0.0.1` (localhost-only; exposing to the whole LAN is
  opt-in only via `RECALL_BIND_ALL=1`/`FLASK_HOST=0.0.0.0`, with a warning in the logs —
  there is no authentication yet, T1.2); shared secret for telegram IPC.

## DB (SQLite, `whisper_history.db`)

Key tables: `transcriptions` (central — all sources: file/youtube/recording/
document/telegram/email/copilot/meeting_archive; v52 adds `mail_*` columns, `source_type='email'`), `mail_accounts`/`mail_labels`/
`mail_attachments` (v52, no secret column), `audio_downloads` (Audio library), `segments`/
speaker-map, `chunks`+embeddings (RAG), `categories` (direction→project tree, `parent_id`,
v45)/`entities`/`meeting_entities`/`action_items`/`entity_aliases` (memory graph),
`tags`/`record_tags`/`tg_chat_tags` (v45), `embedding_epochs` (v46 — prefix format of each
`(model,version)` pair), `segment_bookmarks`, `saved_searches`, `tg_monitored_chats`,
`copilot_sessions`/`topics`/`events`, `ask_log` (v41 — log of UI+MCP questions), `tg_threads`
(v42 — provenance of the thread digest), `chunks.context_prefix` (v43), `transcriptions.title`/
`.description` (v44).
Access — the `get_db_connection()` context manager. Migrations — `app/db/migrations.py`
(current **v52**: v48-v51 — see `migrations.py` (v50 `pipe_runs`, v51 `action_item_events`); v52 (`:2130`) Gmail ingest — additive:
`mail_accounts`, `mail_labels`, `mail_attachments`, `transcriptions.mail_*` + unique partial index `idx_tx_mail_msg` (gmail-ingest, 4.18.0,
16.10.2026); v40 `transcriptions.duplicate_of` — audio/YouTube duplicates; v41 `ask_log` —
question/channel/scope/sources/tokens/cost/owner's rating (production-RAG Wave A,
16.09.2026); v42 `tg_threads.summary_source_ids_json`/`summary_at`/`summary_model`/
`summary_msgs` — provenance of the thread digest; v43 `chunks.context_prefix` + rebuild of
`chunks_fts` on `(context_prefix, text)` (production-RAG Wave B, 17.09.2026); v44
`transcriptions.title`/`.description` (NULL — fallback to `source_name`, `record_meta.
display_name()`) + rebuild of `transcriptions_fts` on both columns
(`editable-title-description`, 4.5.0, 21.09.2026); v45 `categories.parent_id` + `tags`/
`record_tags`/`tg_chat_tags` + `transcriptions.suggested_tags_json`; v46 `embedding_epochs`
(backfill of existing pairs with format 1); v47 — **the project's first non-additive migration**: rebuild of
`categories` under `PRAGMA foreign_keys=OFF` — drops the global `UNIQUE` on `name_norm`,
sets uniqueness AMONG SIBLINGS (`directions-projects-tags`, 4.6.0, 22-23.09.2026)).

## Windows specifics

- WASAPI recording via `pyaudiowpatch` (Linux/macOS → recording auto-disabled); per-application audio
  isolation — WASAPI process loopback via comtypes/pycaw (win32-only pins in requirements.txt).
- Forward slash in code; Pathlib translates. UTF-8 reconfigure of stdout in the telegram scripts.
- FFmpeg in PATH (missing → YouTube disabled, the rest works).
- `os.startfile()` for the QR PNG in telegram_login (graceful fallback on other OSes).
