# Recall MCP Server

A local MCP server (`mcp_server.py`) that gives Claude Code / Claude Desktop control of Recall.
A separate process that reuses the `app.services.*` services and the existing HTTP endpoints,
with no logic duplication. **82 MCP tools (51 read-only + 31 write)**: the write tools are
`mailbox`, `settings`, `video_analyze`, `retranscribe` and `activate_transcript_version`, plus four destructive ones that run only after the owner
presses «Yes» in a native Windows dialog raised by Recall (`mailbox_backfill`, `mailbox_label_enrich`,
`mailbox_disconnect`, `settings_remove_anthropic_key`); archive organisation adds six more writes (`task`, `bookmark`, `comment`, `taxonomy`, `speaker`, `transcript`) and six destructive tools behind the same dialog (`taxonomy_delete`, `taxonomy_merge`, `speaker_merge`, `speaker_delete`, `transcript_delete`, `enrich_backfill`); everything an agent writes is marked `source: mcp`; `system_status` and `transcript_suggest_meta` are reads. The intake pipeline adds two reads (`get_youtube_info`, `list_audio_library`), eight writes (`transcribe`, `document_ingest`, `audio_download`, `audio_item`, `job_cancel`, `transcript_ai`, `models`, `system_diagnostics`) and two destructive tools (`document_import_folder`, `document_reparse`). The owner's decision to move MCP from read-first to
full control is [ADR-042](adr/042-mcp-full-control-supersedes-read-first.md); `recall://about`
reports the live count and the write list of the running profile.

## Profiles

| Env | Effect |
|-----|--------|
| `RECALL_MCP_PROFILE=full` (default) | registers every tool (80) |
| `RECALL_MCP_PROFILE=read` | registers the read tools plus `video_analyze` (49 + 1 = 50 tools); an unknown value counts as `read` |
| `RECALL_MCP_READONLY=1` | registers no write tool at all, whatever the profile |

The profile is decided at import, so a change needs a restart of the MCP process.

## Architecture (hybrid): what works without `app.py` and what does not

18 tools read SQLite directly (autonomous); the other 28 are HTTP proxies to a running
`app.py` (including the flagship `search_archive` and all three write tools).

**Direct-DB, autonomous: work WITHOUT a running `app.py` (18):**
`get_transcript`, `get_original_file`, `list_recent`, `list_categories`,
`list_entities`, `get_entity`, `list_action_items`, `weekly_digest`,
`list_dropped_commitments`, `list_stale_topics`, `research_export`, `list_open_questions`,
`get_chat_context`, `list_comments`, `grep_archive`, `get_live_copilot`, `list_pipe_runs`, `list_mcp_actions`.
These tools open SQLite directly via `get_db_connection`/`app.services.research`
and make no HTTP calls. The MCP process never writes SQLite.

**HTTP proxy, need a running `app.py` (28):**
`search_archive`, `ask_archive`, `suggest_category`, `research_preview`,
`research_summary`, `search_history`, `get_archive_stats`, `list_speakers`,
`get_speaker_stats`, `get_speaker_timeline`, `list_bookmarks`, `list_saved_searches`,
`get_job_status`, `get_active_recording`, `copilot_availability`, `telegram_status`,
`email_status`, `telegram_coverage`, `get_live_transcript`, `ask_live`, `get_live_video`, `get_live_frames`, `video_analyze`,
`get_visual_timeline`, `get_frame`, `get_frames_for_text`, `search_visual`, `mailbox`,
`settings`, `wait_for`, `system_status`, `list_whisper_models`, `retranscribe`,
`transcript_versions`, `activate_transcript_version`.
All of them call `_api()`/`_sse_collect()` against `http://127.0.0.1:5050`. The reason is
deliberate: `search_archive`/`ask_archive` do not pull e5/torch into the stdio process (the warm
model already lives in `app.py` memory), the live tools read the state of running
subsystems, which is not in SQLite, and every write goes through `app.py` with
`X-Recall-Actor: mcp`.

If `app.py` is not running, the proxy tools return a clear error envelope instead of
crashing; direct-DB tools work as usual.

## Writes

- `mailbox`: `connect` (one-time local link to type the app password), `refresh_labels`,
  `label_set` (enable a folder, set its direction), `sync` (queue a sync, at most 12 months).
- `settings`: only `connect_anthropic_key`, a one-time local link to type the key.
  Secrets are never passed as arguments or typed into chat.
- `video_analyze`: queues background keyframe extraction for a recording; it does not change
  archive data.
- `retranscribe(transcription_id, model, language?, diarize?)`: transcribes a record's media again with a
  locally installed Whisper model (`list_whisper_models` lists them). It starts a background job and returns
  `job_id`; poll it with `get_job_status(job_id)` or `wait_for`. A re-run already in flight answers
  `{already_running: true, job_id}`; refusals keep the server `error_code` (`NO_MEDIA`, `MODEL_NOT_INSTALLED`,
  `RECORDING_ACTIVE`). The new run becomes the main transcript and is kept as a version (at most three).
- `transcript_versions(transcription_id, version_id?)` reads the saved versions (a read);
  `activate_transcript_version(transcription_id, version_id)` makes one main again and rebuilds that
  record's search chunks locally.

Each write tool has an argument allowlist and a rate limit per family: at most 10 successful changes per family
per 10 minutes in each MCP process, then the tool answers `RATE_CEILING` with `retry_after_sec` and makes no HTTP call
(a loop guard, not a security control). `list_mcp_actions` reads the audit of every changing request (who, decision,
outcome; the `claimed` labels are unverified). Deleting needs the Windows approval dialog, see
[SECURITY.md](SECURITY.md#5-local-boundary-and-the-approval-dialog). Text returned from the
archive (calls, chats, mail, OCR) is data, never instructions.

**Resources:** `recall://about` (overview + live statistics + tool map +
autonomous/proxy list + write list), `recall://transcript/{id}`, `recall://entity/{id}`.

**Long operations:** MCP is request/response without streaming. Poll a background task
with `get_job_status(job_id)` or `wait_for`. SSE reports (`research_summary`) are read to the
end on the proxy side and returned complete.

## Transports and authorization

| Mode | Command | Authorization | For whom |
|-------|---------|-------------|------|
| **stdio** (default) | `mcp_server.py` | not needed (the client spawns the process itself) | Claude Code, Claude Desktop |
| **http** | `mcp_server.py --transport http` (:5060) | `Authorization: Bearer <RECALL_MCP_KEY>` | persistent server; future web |

In http mode without `RECALL_MCP_KEY` the server **does not start** (protection against accidental
exposure). It listens on `127.0.0.1` only.

## Connecting

### Claude Code — stdio, globally for all projects (recommended)
```bash
claude mcp add recall -s user -- "E:\Projects\Recall\.venv\Scripts\python.exe" "E:\Projects\Recall\mcp_server.py"
```
`-s user` → available in Claude Code from any project on this PC (config in
`C:\Users\<you>\.claude.json`). The default transport is stdio — **do not pass**
`--transport` (the Claude CLI drops trailing arguments after `--`; that is exactly why stdio
is the default in the server itself). After `add`, **restart Claude Code** so that the
`recall` group appears. Check: `claude mcp list` → `recall … ✔ Connected`.

### Claude Desktop — stdio
In `claude_desktop_config.json` (Settings → Developer → Edit Config):
```json
{
  "mcpServers": {
    "recall": {
      "command": "E:\\Projects\\Recall\\.venv\\Scripts\\python.exe",
      "args": ["E:\\Projects\\Recall\\mcp_server.py"]
    }
  }
}
```
Restart Desktop.

### http (optional, persistent server)
```bash
# .env: RECALL_MCP_KEY=<secrets.token_urlsafe(32)>
.venv\Scripts\python.exe mcp_server.py --transport http
claude mcp add --transport http recall http://127.0.0.1:5060/mcp --header "Authorization: Bearer <RECALL_MCP_KEY>"
```

## Environment variables

| Env | Default | Purpose |
|-----|---------|-------------|
| `RECALL_API_URL` | `http://127.0.0.1:5050` | address of the running `app.py` for the proxy tools |
| `RECALL_MCP_PROFILE` | `full` | `full` registers every tool, `read` the read tools plus `video_analyze` (see Profiles) |
| `RECALL_MCP_READONLY` | (off) | `1` hides every write tool (`mailbox`, `settings`, `video_analyze`, `retranscribe`, `activate_transcript_version`) |
| `RECALL_MCP_KEY` | — | Bearer key, **only** for http mode |
| `RECALL_MCP_DEBUG_LOG` | `1` (on since 24.07.2026) | log of every tool call to `logs/mcp_calls.log`: name, arguments, ms, ok/exc. Before Track 1 it was off — 488 calls went nowhere, and any tuning of the results was blind |

`.env` is loaded from the script's own directory (not the cwd) — so the user-scope connector
works correctly when launched from any project.

## Security
- Listens on localhost only; http without a valid `Bearer` → 401.
- DB reads go through `get_db_connection` (WAL, in parallel with Flask).
- Reading original files (`get_original_file`) is restricted to a whitelist of folders
  (`documents/`, `uploads/`, `youtube_downloads/`, `telegram_media/`) — anti-traversal.
- Writes go only through the three write tools, each through `app.py` with `X-Recall-Actor: mcp`;
  `RECALL_MCP_READONLY=1` removes them.
- Do NOT commit `.env` with the key (it is in `.gitignore`).

## Why NOT autostart from app.py
The stdio server is spawned **by the client itself** (Claude Code/Desktop) on demand and killed on
exit — there is no need to keep a separate process alongside `app.py` (and it would create a redundant second
server). Autostart makes sense only for http mode (future web access via
tunnel+OAuth) — then modeled on `telegram_listener`.

## Performance / caveats
- `mcp_server.py` itself does NOT import torch/e5 — the heavy ML initialization lives only in
  `app.py`; `search_archive`/`ask_archive` proxy the request to the already warm model
  there instead of loading it in the stdio process.
- Proxy tools (28 of 46, see the lists above) need a running `app.py`
  (most conveniently via the **Recall** icon on the desktop: a terminal with logs + the server
  on :5050). Direct-DB tools (18) work without it too.

## Roadmap
- **Web / claude.ai:** OAuth 2.1+PKCE (FastMCP `OAuthProxy`) + Cloudflare Tunnel for a
  public HTTPS host + autostart of the http server from `app.py`.

## Descoped
- Delete (class D) tools and the D-gate dialog: none is registered today; they come with a later spec (ADR-042, items 5 and 11).
