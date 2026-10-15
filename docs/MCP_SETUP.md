# Recall MCP Server

A local MCP server (`mcp_server.py`) — a **read-first bridge** to Recall for
Claude Code / Claude Desktop. A separate process that reuses the `app.services.*` services and
the existing HTTP endpoints — no logic duplication. **29 read-only tools**
(no create/update/delete — a deliberate owner decision, 02.07.2026; write tools
were physically removed and new ones are not added).

## Architecture (hybrid) — what works without `app.py` and what does not

There is no single rule "all READs are independent" — it depends on the specific tool:
8 read SQLite directly (autonomous), the other 16 are HTTP proxies to a running
`app.py` (including the flagship `search_archive`).

**Direct-DB, autonomous — work WITHOUT a running `app.py` (8):**
`get_transcript`, `get_original_file`, `list_recent`, `list_categories`,
`list_entities`, `get_entity`, `list_action_items`, `weekly_digest`,
`list_dropped_commitments`, `list_stale_topics`, `research_export`.
These tools open SQLite directly via `get_db_connection`/`app.services.research`
and make no HTTP calls.

**HTTP proxy, need a running `app.py` (16):**
`search_archive`, `ask_archive`, `suggest_category`, `research_preview`,
`research_summary`, `search_history`, `get_archive_stats`, `list_speakers`,
`get_speaker_stats`, `get_speaker_timeline`, `list_bookmarks`, `list_saved_searches`,
`get_job_status`, `get_active_recording`, `copilot_availability`, `telegram_status`.
All of them call `_api()`/`_sse_collect()` → `http://127.0.0.1:5050`. The reason is
deliberate: `search_archive`/`ask_archive` do not pull e5/torch into the stdio process (the warm
model already lives in `app.py` memory), while `get_job_status`/`get_active_recording`/
`copilot_availability`/`telegram_status` read the state of live subsystems, which
simply is not in SQLite.

If `app.py` is not running, the proxy tools return a clear error
(`{"error": "Recall (app.py) не запущений…"}`) instead of crashing; direct-DB tools
work as usual.

> **Read-only.** MCP performs no create/update/delete, neither directly nor
> through the proxy — all state changes are made by the Recall UI.

## Tool catalog (27)

- **Archive core (direct-DB, 7):** `get_transcript`, `get_original_file`,
  `list_recent`, `list_categories`, `list_entities`, `get_entity`, `research_export`.
- **Commitments (direct-DB, 4 — Track 1):** `weekly_digest(owner)` — the Monday digest
  in one call; `list_action_items(window=this_week|next_week|overdue|soon|no_date|all,
  owner, category_id, status)`; `list_dropped_commitments(days)` — promises after which the
  topic never came up again; `list_stale_topics(days)` — directions that vanished from conversations.
  Tasks return both the raw deadline phrase (`due_raw`) and the computed date (`due_date` +
  `due_precision`) — see `app/services/commitments.py`.
- **On-demand AI (proxy, 4):** `ask_archive` (RAG answer with citations),
  `suggest_category` (k-NN hint), `research_preview`, `research_summary`.
- **Richer reads, as in the UI (proxy, 7):** `search_archive` (hybrid e5+FTS5),
  `search_history`, `get_archive_stats`, `list_speakers`, `get_speaker_stats`,
  `get_speaker_timeline`, `list_bookmarks`, `list_saved_searches`.
- **Live / background statuses (proxy, 4):** `get_job_status`, `get_active_recording`,
  `copilot_availability`, `telegram_status`.

**Resources:** `recall://about` (overview + live statistics + tool map +
autonomous/proxy list), `recall://transcript/{id}`, `recall://entity/{id}`.

**Long operations:** MCP is request/response without streaming. Poll the state of a background task
via `get_job_status(job_id)` (the job_id is issued by the Recall UI — there is no start tool in
MCP). SSE reports (`research_summary`) are read to the end on the proxy side and returned
complete.

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
| `RECALL_MCP_READONLY` | (off) | belt-and-braces: all write tools are already physically removed from the code, so this flag currently has no effect — it can be left for the future |
| `RECALL_MCP_KEY` | — | Bearer key, **only** for http mode |
| `RECALL_MCP_DEBUG_LOG` | `1` (on since 24.07.2026) | log of every tool call to `logs/mcp_calls.log`: name, arguments, ms, ok/exc. Before Track 1 it was off — 488 calls went nowhere, and any tuning of the results was blind |

`.env` is loaded from the script's own directory (not the cwd) — so the user-scope connector
works correctly when launched from any project.

## Security
- Listens on localhost only; http without a valid `Bearer` → 401.
- DB reads go through `get_db_connection` (WAL, in parallel with Flask).
- Reading original files (`get_original_file`) is restricted to a whitelist of folders
  (`documents/`, `uploads/`, `youtube_downloads/`, `telegram_media/`) — anti-traversal.
- There are no writes through MCP — the whole toolset is read-only.
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
- Proxy tools (16 of 27, see the table above) need a running `app.py`
  (most conveniently via the **Recall** icon on the desktop: a terminal with logs + the server
  on :5050). Direct-DB tools (11) work without it too.

## Roadmap
- **Web / claude.ai:** OAuth 2.1+PKCE (FastMCP `OAuthProxy`) + Cloudflare Tunnel for a
  public HTTPS host + autostart of the http server from `app.py`.
