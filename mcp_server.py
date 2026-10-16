"""Recall MCP server — the read-first bridge to Recall over MCP.

A separate process (modelled on telegram_listener.py). Gives Claude (Code / Desktop)
a read-first core of read-only tools (the exact count is live, from the FastMCP registry,
see the resource `recall://about`): archive search/reading, the entity graph, tasks,
statistics, ask-archive/research and status-only snapshots of the live subsystems
(recording/copilot/telegram/jobs). **Hybrid:** some tools read persisted
data straight from SQLite (`get_db_connection`/`app.services.research`, they work WITHOUT
a running app.py); the rest (search/RAG/statuses) are httpx proxies to the running
app.py (reusing ready endpoints, zero duplication, and keeping the heavy
e5/torch out of the stdio process) — those need a running app.py. The full split is
in the resource `recall://about` and docs/MCP_SETUP.md. **Strategic decision of the owner
(02.07.2026): MCP is read-first.** All create/update/delete and hardware control
(microphone recording, copilot, Telegram chats) or paid/long Claude calls
(enrich/polish/summarize/transcribe/ingest/backfill) are removed from MCP — the Recall
UI does that. New write tools are deliberately not added. Connection/env details —
docs/MCP_SETUP.md.

Transports:
  * stdio (default) — for Claude Desktop / Claude Code (the client spawns the process itself,
    no network/key). This is the one `claude mcp add` registers;
  * http            — Streamable HTTP on localhost, gated by a static key (Bearer).

Key: env RECALL_MCP_KEY (generate: python -c "import secrets;print(secrets.token_urlsafe(32))").
In http mode the server does NOT start without a key (protection against accidental exposure).

Run:
  .venv/Scripts/python.exe mcp_server.py                   # stdio (default)
  .venv/Scripts/python.exe mcp_server.py --transport http  # http://127.0.0.1:5060/mcp (needs RECALL_MCP_KEY)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import logging
import logging.handlers
# The stdio mcp_server is spawned by the client and lives briefly: we do NOT keep the shared
# whisper_ui.log open (otherwise we block the rollover in app.py → WinError 32 spam) and
# silence logging errors themselves. Must come BEFORE the app.* import (lazy setup_logger there).
os.environ.setdefault("RECALL_LOG_TO_FILE", "0")
logging.raiseExceptions = False

# T8.3 (Wave 4): this process had NO logger at all (only raiseExceptions=False
# above) — unattended crashes went unnoticed. We add a LIGHT (stdlib-only,
# without app.core.logger.setup_logger and without the heavy torch/e5 imports — search_archive
# still loads them lazily) file log into its OWN file mcp_server.log, separate
# from app.py (whisper_ui.log) and telegram_listener.py (telegram_listener.log) —
# same reason as there: a shared file across several processes → rollover conflict
# on Windows (WinError 32). CRITICAL: no handler on stdout — the MCP stdio transport
# uses stdout for JSON-RPC, any log there breaks the protocol. We write
# to the file only (stderr is not touched either, so as not to disturb client parsing).
# Under pytest this module is only imported (test_mcp_about.py, test_tool_count_docs.py
# and others — at module level, i.e. already during collection), and the handler is attached
# to the ROOT logger: any logging.error/exception from any other test in
# the same run (including the fake YouTube errors of test_youtube_pytubefix.py)
# would then go into the mcp_server.log of the owner's live server (that is how they ended up there).
# The settings registry already knows PYTEST_CURRENT_TEST as an external variable
# (app/core/settings.py IGNORED_ENV_NAMES), but pytest sets exactly this variable
# only for the test execution phase (setup/call/teardown), not for collection — at
# the time of this import it is still None. pytest puts "pytest" into sys.modules right
# at startup, before collection, so it is the reliable indicator here.
if "pytest" not in sys.modules:
    try:
        _MCP_LOG_PATH = Path(__file__).resolve().parent / "mcp_server.log"
        _mcp_file_handler = logging.handlers.RotatingFileHandler(
            _MCP_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True,
        )
        _mcp_file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [mcp_server pid=%(process)d] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logging.getLogger().addHandler(_mcp_file_handler)
        logging.getLogger().setLevel(logging.INFO)
        # Silence noisy third-party loggers (the same list as app.py) —
        # otherwise httpx proxy calls would bloat mcp_server.log per request.
        for _noisy in ("httpx", "httpcore", "urllib3", "anthropic", "huggingface_hub", "filelock"):
            logging.getLogger(_noisy).setLevel(logging.WARNING)
    except OSError:
        # Do not crash the stdio server if the log file is unavailable — the MCP protocol
        # matters more than diagnostics.
        pass

logger = logging.getLogger("mcp_server")

try:
    from dotenv import load_dotenv
    # The global (user-scope) connector starts from any cwd — load .env
    # from the script's own directory, not from the current directory.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


def _set_process_lang() -> None:
    """The MCP process speaks English: server texts rendered by `tr()` come out in `en`."""
    os.environ["RECALL_UI_LANG"] = "en"


# Under pytest this module is only imported; do not leak the language into other tests.
if "pytest" not in sys.modules:
    _set_process_lang()

from config import Config
from app.db.connection import get_db_connection
from app.services import record_meta
# `categories` — stdlib + app.db.connection only (note mcp-stdio-no-heavy-models),
# safe at module level: the single source of SQL fragments for the direction/tags/period filter
# for the direct-DB tools (C6, ADR-023).
from app.services import categories

import httpx
from fastmcp import FastMCP

# Heavy ML dependencies (retrieval → numpy/torch for e5 embeddings) are imported
# LAZILY inside search_archive so the stdio server starts instantly — otherwise
# the Claude Code health check hits a timeout while torch loads.

# --- Paths (resolved absolutely — the process starts from the repo root) ------
BASE_DIR: Path = Config.BASE_DIR
DB_PATH: str = str(BASE_DIR / Config.DATABASE)
# Folders holding the originals — only files from these may be read (anti-traversal).
_ALLOWED_DIRS = [
    (BASE_DIR / Config.DOCUMENTS_FOLDER).resolve(),
    (BASE_DIR / Config.UPLOAD_FOLDER).resolve(),
    (BASE_DIR / Config.YOUTUBE_FOLDER).resolve(),
    (BASE_DIR / getattr(Config, "TELEGRAM_MEDIA_DIR", "telegram_media")).resolve(),
]

_TEXT_DOC_TYPES = {"md", "txt", "csv"}


# Every proxied call asks the Recall server for English texts.
_LANG_HEADERS = {"X-Recall-Lang": "en"}

# --- HTTP proxy to the live Flask (app.py) -------------------------------------
# Hybrid architecture: reading persisted data and search go straight from SQLite (below);
# all WRITES and live/AI actions are proxied to the running app.py — reusing the ready validation,
# jobs, embeddings. Localhost-only, no CORS restrictions for server-to-server.
API_URL: str = os.environ.get("RECALL_API_URL", "http://127.0.0.1:5050").rstrip("/")
# Emergency switch for all writes (does not affect reads). Enabled by default.
_WRITE_DISABLED: bool = (
    os.environ.get("RECALL_MCP_READONLY", "").strip().lower() in ("1", "true", "yes", "on")
)


def _api(method: str, path: str, *, params: Optional[dict] = None,
         body: Optional[dict] = None, write: bool = False,
         timeout: float = 60.0) -> dict:
    """Call a Recall endpoint (localhost:5050). Reuses the ready logic instead of
    duplicating it. write=True → state change (blocked in read-only mode).
    Never raises — returns {"error": ...} if app.py is not running / on HTTP failure,
    so the tool degrades gracefully instead of crashing."""
    if write and _WRITE_DISABLED:
        return {"error": "MCP is in read-only mode (RECALL_MCP_READONLY is set). "
                         "Remove this environment variable to allow writes."}
    url = f"{API_URL}{path}"
    try:
        resp = httpx.request(method, url, params=params, json=body, timeout=timeout,
                             headers=_LANG_HEADERS)
    except httpx.ConnectError:
        return {"error": f"Recall (app.py) is not running at {API_URL}. "
                         f"Run '.venv/Scripts/python.exe app.py' and repeat the action."}
    except httpx.TimeoutException:
        return {"error": f"Timeout {timeout:g}s from {path} — the operation may have been too long "
                         f"(for long jobs use start+poll, not a blocking call)."}
    except httpx.HTTPError as e:
        return {"error": f"HTTP error to {path}: {e}"}
    return _handle_resp(resp)


def _handle_resp(resp: "httpx.Response") -> dict:
    """Parse a response: JSON or raw text; >=400 → pass the server error through."""
    try:
        data = resp.json()
    except ValueError:
        return {"status_code": resp.status_code, "text": resp.text[:4000]}
    if resp.status_code >= 400:
        if isinstance(data, dict):
            data.setdefault("error", f"HTTP {resp.status_code}")
            data["status_code"] = resp.status_code
            return data
        return {"error": f"HTTP {resp.status_code}", "body": data}
    return data


def _sse_collect(path: str, body: Optional[dict] = None,
                 timeout: float = 600.0) -> dict:
    """Consumes an SSE endpoint (event:/data: format) to the end and returns
    {'events':[...], 'done': <payload>|None, 'error': <msg>|None}. For long
    streams (research/summary), since MCP itself does not stream — we read it all on the proxy side."""
    url = f"{API_URL}{path}"
    events: list = []
    cur_event: Optional[str] = None
    data_lines: list = []
    try:
        with httpx.stream("POST", url, json=body, timeout=timeout,
                          headers=_LANG_HEADERS) as resp:
            if resp.status_code >= 400:
                resp.read()
                return {"error": f"HTTP {resp.status_code}", "events": []}
            for line in resp.iter_lines():
                if line == "":
                    if cur_event is not None or data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            payload = json.loads(raw) if raw else {}
                        except ValueError:
                            payload = {"raw": raw}
                        events.append({"event": cur_event, "data": payload})
                    cur_event, data_lines = None, []
                elif line.startswith("event:"):
                    cur_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
    except httpx.ConnectError:
        return {"error": f"Recall (app.py) is not running at {API_URL}.", "events": []}
    except httpx.HTTPError as e:
        return {"error": f"SSE error to {path}: {e}", "events": []}
    done = next((e["data"] for e in events if e["event"] == "done"), None)
    err = next((e["data"].get("error") for e in events
                if e["event"] == "error" and isinstance(e["data"], dict)), None)
    return {"events": events, "done": done, "error": err}


# --- Authorization -------------------------------------------------------------
def _build_auth():
    """StaticTokenVerifier for the owner's single key. None → no authorization (stdio)."""
    key = os.environ.get("RECALL_MCP_KEY")
    if not key:
        return None
    StaticTokenVerifier = None
    for path in ("fastmcp.server.auth.providers.static",
                 "fastmcp.server.auth.providers.jwt"):
        try:
            mod = __import__(path, fromlist=["StaticTokenVerifier"])
            StaticTokenVerifier = getattr(mod, "StaticTokenVerifier")
            break
        except (ImportError, AttributeError):
            continue
    if StaticTokenVerifier is None:
        raise RuntimeError("FastMCP StaticTokenVerifier is unavailable — upgrade fastmcp")
    return StaticTokenVerifier(
        tokens={key: {"client_id": "recall-owner", "scopes": ["recall"]}},
        required_scopes=["recall"],
    )


# --- DEBUG: optional tool-call log (enabled by env RECALL_MCP_DEBUG_LOG) -------
# 1/true → logs/mcp_calls.log; or an explicit path. OFF by default (zero overhead).
# A temporary MCP debugging tool — writes a line per tools/call + handshake.
import time as _time
from fastmcp.server.middleware.middleware import Middleware as _Middleware


def _debug_log_path() -> Optional[str]:
    raw = os.environ.get("RECALL_MCP_DEBUG_LOG", "").strip()
    if not raw:
        return None
    if raw.lower() in ("1", "true", "yes", "on"):
        return str(BASE_DIR / "logs" / "mcp_calls.log")
    return raw


class _CallLogMiddleware(_Middleware):
    """Logs every tool call: time, name, arguments (truncated), ok/exc, ms."""

    def __init__(self, path: str) -> None:
        self._path = path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except OSError:
            pass

    def _w(self, line: str) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    async def on_initialize(self, context, call_next):
        self._w(f"{_time.strftime('%H:%M:%S')} ── initialize (handshake)")
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        name = getattr(context.message, "name", "?")
        try:
            sargs = json.dumps(getattr(context.message, "arguments", None),
                               ensure_ascii=False)[:600]
        except Exception:
            sargs = str(getattr(context.message, "arguments", None))[:600]
        # START — written BEFORE the call so a hang is visible (START without an OK/EXC pair).
        self._w(f"{_time.strftime('%H:%M:%S')} →   START {name} args={sargs}")
        t0 = _time.perf_counter()
        try:
            res = await call_next(context)
            ms = (_time.perf_counter() - t0) * 1000
            self._w(f"{_time.strftime('%H:%M:%S')} OK  {name} {ms:.0f}ms")
            return res
        except Exception as e:
            ms = (_time.perf_counter() - t0) * 1000
            self._w(f"{_time.strftime('%H:%M:%S')} EXC {name} {ms:.0f}ms :: {type(e).__name__}: {e}")
            raise


_dbg_path = _debug_log_path()
_mw = [_CallLogMiddleware(_dbg_path)] if _dbg_path else None
mcp = FastMCP(name="Recall Archive", auth=_build_auth(), middleware=_mw)


# --- helpers -------------------------------------------------------------------
def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _where_clause(conditions: list, params: list, *fragments: tuple) -> tuple:
    """Joins simple conditions (`'col = ?'`, ...) with the fragments of
    `categories.filter_sql/tags_sql/date_sql` (each carries its own ` AND `,
    C2). When there are no simple conditions, the first fragment becomes `WHERE`, not
    a dangling `AND`. Returns (`' WHERE ...'` or `''`, parameters in order)."""
    sql = " WHERE " + " AND ".join(conditions) if conditions else ""
    out_params = list(params)
    for frag, p in fragments:
        if not frag:
            continue
        sql = sql + frag if sql else " WHERE " + frag[len(" AND "):]
        out_params += p
    return sql, out_params


def _tree_summary_line(tree: list) -> str:
    """`categories.list_tree` → `Агенція (N) → Маяк (n), Орбіта (n); Особисте (N)`
    for `about()` — the same shape as the tree in the UI, but as one line of text."""
    parts: list = []
    current: Optional[dict] = None
    kids: list = []

    def _flush() -> None:
        if current is None:
            return
        if kids:
            child_str = ", ".join(f"{c['name']} ({c['items']})" for c in kids)
            parts.append(f"{current['name']} ({current['items_total']}) → {child_str}")
        else:
            parts.append(f"{current['name']} ({current['items_total']})")

    for node in tree:
        if node["level"] == "direction":
            _flush()
            current = node
            kids = []
        else:
            kids.append(node)
    _flush()
    return "; ".join(parts)


def _tags_note(conn, tag_list: list) -> Optional[str]:
    """Explanation for an empty result when `tag_list` contains a name that is
    not in the `tags` dictionary — otherwise an unrecognized filter would be silently ignored
    (ADR-023 §C: `tags_sql` on an unknown name simply matches nothing)."""
    if not tag_list:
        return None
    keys = [categories.norm_name(n) for n in tag_list]
    ph = ",".join("?" * len(keys))
    existing = {r[0] for r in conn.execute(
        f"SELECT name_norm FROM tags WHERE name_norm IN ({ph})", keys)}
    missing = [n for n, k in zip(tag_list, keys) if k not in existing]
    if not missing:
        return None
    return f"tag not found: {', '.join(missing)}"


def _name_key(name: Optional[str]) -> str:
    """Folding key of an entity name: «PolarIs» and «Polaris» are the same.

    Computed in Python, not SQL: SQLite `LOWER` does not fold Cyrillic, so
    a comparison in the query would silently treat «Алгоритм» and «алгоритм» as different
    (the same rake as in the task owner filter).

    We do NOT strip the parenthesised tail, even though it seems natural («Промінвест (хаб)»
    → «Промінвест»). For people a parenthesis is a DISAMBIGUATOR, not noise: the graph holds
    «Юлія» (meetings), «Юлія (Willow)» and «Юлія (Ділова англійська)» — different
    women. Stripping merged them into one and added to the dossier "218
    meetings in total" — exactly the lie this whole mechanism exists against. The cost of refusing:
    «Промінвест (хаб)» stays separate from «Промінвест». That is as it should be — it is
    a guess, not a fact, and namesakes are shown here as fact.

    We take the apostrophe that is actually in the data: 9 names are written with U+02BC
    (ʼ) and none with U+2019 (’). We fold the dot — «Aegis.UA» and
    «Aegis UA» are the same.
    """
    s = (name or "").casefold().strip()
    return re.sub(r"[\s\-_.'`ʼ’«»\"]+", "", s)


def _same_name_rows(conn, entity_id: int, name: str) -> list[dict]:
    """Other graph rows with the same name — including a DIFFERENT type.

    Enrichment creates an entity separately in each type, so one project lives
    as `Polaris[project]` and `POLARIS[org]`: the dossier of one row shows
    part of the history and looks like all of it (in this pair — 114 and 78 links out of 190
    in the group). There are over a hundred such groups in the archive.

    We deliberately do NOT merge: merge moves spellings into aliases and has already once
    broken consumers (`memory/entity-merge-moves-names-to-aliases`). The tool must
    not rewrite the graph, but must not lie about it — so it just shows the rest
    of the group and the sum. Merge candidates come from `entity_dedup` (CLI), and it does not
    see this class by construction: it compares only within one type.
    """
    key = _name_key(name)
    if not key:
        return []
    rows = conn.execute(
        "SELECT id, type, canonical_name, mention_count, meeting_count FROM entities "
        "WHERE id <> ?", (int(entity_id),)).fetchall()
    return [_row(r) for r in rows if _name_key(r["canonical_name"]) == key]


def _safe_original_path(file_path: Optional[str]) -> Optional[Path]:
    """Resolve the path to an original with anti-traversal: only inside the allowed folders."""
    if not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        p = p.resolve()
    except OSError:
        return None
    for base in _ALLOWED_DIRS:
        try:
            p.relative_to(base)
            return p
        except ValueError:
            continue
    return None


# ============================== TOOLS =========================================
# The core of archive reading — direct-DB (get_db_connection/app.services.research),
# works even without a running app.py.
# ============================================================
# Owner's comments (Wave 5)
# ============================================================
#
# Read-only, like the rest of the tools: the decision "MCP → read-first" (02.07.2026) stays
# in force, and a write tool `add_comment` is deliberately NOT added — a comment is written
# from the Recall interface, and Claude only reads it.
#
# Direct SQL, not a proxy to app.py: the query is cheap (index on target), needs no heavy
# models, and the stdio server must not bring up torch (lesson
# mcp-stdio-no-heavy-models).

_COMMENT_COLS = ("id", "kind", "body", "pinned", "anchor_time", "author",
                 "source", "created_at", "target_type", "target_id", "target_key")


def _comments_for(conn, target_type: str, target_id) -> list[dict]:
    """Comments of one card. Empty on an old DB without migration v37 —
    a missing layer must not break reading a transcript."""
    col = "target_key" if target_type == "recording_session" else "target_id"
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_COMMENT_COLS)} FROM comments "
            f"WHERE target_type = ? AND {col} = ? AND deleted_at IS NULL "
            f"ORDER BY pinned DESC, COALESCE(anchor_time, 1e18), created_at",
            (target_type, target_id)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row(r) for r in rows]


@mcp.tool
def list_comments(target_type: Optional[str] = None, target_id: Optional[str] = None,
                  kind: Optional[str] = None, days: Optional[int] = None,
                  limit: int = 50) -> list:
    """The owner's comments — his own clarifications, corrections and emphases about
    archive records.

    This is the most precise layer of the archive: a transcript captures what was SAID (with
    recognition errors, slips, unfinished phrases), while a comment is what the owner deliberately
    FIXED as truth. So on a conflict trust the comment, not the transcript.

    Args:
        target_type: narrow to a card type — transcription | audio_download |
            action_item | entity | speaker | category | tg_thread |
            recording_session.
        target_id: a specific card (for recording_session — a `rec_...` string).
            Together with target_type.
        kind: correction (fixes what was said) | decision | note | context |
            question. `correction` is the most important slice: those are the places where the record
            misleads.
        days: only for the last N days.
        limit: how many to return (default 50).

    Returns: a list of comments with provenance — which card, of which type,
    when written, and `target_name` (the record's name), so you do not need another call.
    """
    sql = (f"SELECT {', '.join('c.' + col for col in _COMMENT_COLS)}, "
           f"t.source_name AS target_name "
           f"FROM comments c "
           f"LEFT JOIN transcriptions t ON c.target_type = 'transcription' "
           f"AND t.id = c.target_id "
           f"WHERE c.deleted_at IS NULL")
    params: list = []
    if target_type:
        sql += " AND c.target_type = ?"
        params.append(target_type)
        if target_id is not None:
            col = "c.target_key" if target_type == "recording_session" else "c.target_id"
            sql += f" AND {col} = ?"
            params.append(target_id)
    if kind:
        sql += " AND c.kind = ?"
        params.append(kind)
    if days:
        sql += " AND c.created_at >= datetime('now', ?)"
        params.append(f"-{int(days)} days")
    # id as a tie-breaker: created_at has one-second resolution, and without
    # it rows equal in time would come back in rowid order — i.e. reversed.
    sql += " ORDER BY c.created_at DESC, c.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with get_db_connection(DB_PATH) as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []       # old DB without migration v37
    return [_row(r) for r in rows]


@mcp.tool
def search_archive(query: str, top_k: int = 8,
                   category_id: Optional[int] = None, tags: Optional[str] = None,
                   since: Optional[str] = None, until: Optional[str] = None,
                   explain: bool = False) -> dict:
    """Hybrid (e5 vector + FTS5 BM25) search over the WHOLE archive of calls/documents/
    Telegram. Returns chunks with provenance (source, date, speaker, timecode/page,
    transcription_id, chunk_id) and relevance. category_id — narrow to a direction.
    This is the main tool: find here, then pull details via get_transcript.

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive.

    The results may contain chunks with `source_type='comment'` — these are the owner's comments
    about a record (clarifications/corrections/emphases), not lines from the conversation. They have
    HIGHER priority than the transcript: on a contradiction the comment is right, and
    `comment_kind='correction'` directly overrides the corresponding place in the record. The field
    `target_label` says what exactly the comment was written about. explain=True — add
    a relevance breakdown (bm25/dense/rrf/recency/rerank, source) to every chunk.

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    # Proxy to app.py (warm e5 is already in its memory) — do NOT load torch/e5 into the stdio
    # process of the MCP server: heavy native initialization in stdio corrupts JSON-RPC (writes to
    # stdout) and competes for the GPU with app.py (faster-whisper/Qwen) → a hang forever.
    # The same path as ask_archive/search_history → GET /api/memory/search.
    params: dict = {"q": query, "k": int(top_k)}
    if category_id is not None:
        params["category_id"] = int(category_id)
    if tags:
        params["tags"] = tags
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if explain:
        params["explain"] = True
    res = _api("GET", "/api/memory/search", params=params)
    # on-screen text (speaker='екран') goes through the private filter (mcp-visual-privacy)
    chunks, hidden = _privacy.filter_screen_chunks(DB_PATH, res.get("chunks") or [])
    out = {
        "query": res.get("query", query),
        "vector_available": res.get("vector_available"),
        "count": len(chunks),
        "chunks": chunks,
    }
    if hidden:
        out["private_hidden"] = hidden
    return out


def _email_blocks(conn, ids: list) -> dict:
    """`email` blocks for the given transcription ids (email rows only). Direct-DB read; an
    un-migrated database without the mail tables/columns yields {} instead of an error."""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            "SELECT t.id, t.mail_from, t.mail_to, t.mail_subject, t.mail_thread_key, "
            "t.mail_message_id, t.mail_folder, a.address AS account, l.display_name AS folder_name "
            "FROM transcriptions t "
            "LEFT JOIN mail_accounts a ON a.id = t.mail_account_id "
            "LEFT JOIN mail_labels l ON l.account_id = t.mail_account_id AND l.folder = t.mail_folder "
            f"WHERE t.source_type = 'email' AND t.id IN ({marks})", ids).fetchall()
        if not rows:
            return {}
        eids = [r["id"] for r in rows]
        att: dict = {}
        for r in conn.execute(
                "SELECT email_tid, document_tid, filename FROM mail_attachments "
                f"WHERE email_tid IN ({','.join('?' * len(eids))}) ORDER BY document_tid", eids):
            att.setdefault(r["email_tid"], []).append(
                {"document_id": r["document_tid"], "filename": r["filename"]})
    except sqlite3.Error as e:
        logger.debug("email block unavailable (mail tables missing?): %s", e)
        return {}
    return {r["id"]: {
        "from": r["mail_from"], "to": r["mail_to"], "subject": r["mail_subject"],
        "account": r["account"], "folder": r["folder_name"] or r["mail_folder"],
        "thread_key": r["mail_thread_key"], "message_id": r["mail_message_id"],
        "attachments": att.get(r["id"], []),
    } for r in rows}


def _email_thread(conn, transcription_id: int) -> Optional[dict]:
    """The whole email chain this email belongs to (same account + thread key), oldest
    first, as Telegram's `thread`. None for a lone email or without the mail columns."""
    try:
        row = conn.execute(
            "SELECT mail_account_id, mail_thread_key FROM transcriptions "
            "WHERE id = ? AND source_type = 'email'", (int(transcription_id),)).fetchone()
        if not row or not row["mail_thread_key"]:
            return None
        msgs = conn.execute(
            "SELECT id, meeting_date, mail_from, mail_to, mail_subject, mail_message_id, "
            "mail_in_reply_to, transcript_text FROM transcriptions "
            "WHERE source_type = 'email' AND mail_account_id IS ? AND mail_thread_key = ? "
            "AND deleted_at IS NULL ORDER BY meeting_date, id LIMIT 60",
            (row["mail_account_id"], row["mail_thread_key"])).fetchall()
    except sqlite3.Error as e:
        logger.debug("email thread unavailable (mail columns missing?): %s", e)
        return None
    if len(msgs) < 2:
        return None
    return {"thread_key": row["mail_thread_key"], "msg_count": len(msgs),
            "first_date": msgs[0]["meeting_date"], "last_date": msgs[-1]["meeting_date"],
            "messages": [_row(m) for m in msgs]}


def _attached_to(conn, document_id: int) -> list:
    """Ids of the emails a document is an attachment of ([] when none or no mail tables)."""
    try:
        return [r[0] for r in conn.execute(
            "SELECT email_tid FROM mail_attachments WHERE document_tid = ? ORDER BY email_tid",
            (int(document_id),))]
    except sqlite3.Error as e:
        logger.debug("attached_to unavailable (mail tables missing?): %s", e)
        return []


@mcp.tool
def get_transcript(transcription_id: int, include_segments: bool = False) -> dict:
    """Full transcript/text of a recording or document + metadata (source, language, date,
    direction, summary). include_segments=True — add a per-segment breakdown with
    timecodes/speakers (for audio).

    `display_name` — how to call the record to a person (`title` if the owner set it,
    otherwise `source_name`); `title`/`description` — raw fields, may be `null`.

    For Telegram `thread` is added — the whole conversation thread this message
    belongs to (Wave 4.5). A single line in a chat is often unreadable
    («Ок», «а скільки там?»), so without the thread the answer would have to be guessed.
    For an email `thread` is the whole email chain (same thread key), oldest first; each
    message's text has its quotes cut, so the chain is read message by message.

    `comments` — the owner's comments on this record, if any. These are NOT lines
    from the conversation but sentences written about it afterwards: clarifications, corrections,
    emphases. They have HIGHER priority than the transcript itself — if a comment
    contradicts the record text, the comment is right. The `correction` type directly
    overrides the corresponding place in the transcript."""
    with get_db_connection(DB_PATH) as conn:
        r = conn.execute(
            "SELECT id, created_at, source_type, source_name, title, description, "
            "language, model_used, "
            "category_id, meeting_date, doc_type, original_filename, page_count, "
            "summary_json, transcript_text, polished_text, segments, "
            # Telegram provenance: without (tg_chat_id, tg_message_id) a finding cannot be
            # opened in the thread, without tg_sender we cannot say WHO wrote it.
            "tg_chat_id, tg_chat_title, tg_sender, tg_message_id, tg_date, tg_link, "
            "tg_reply_to, tg_thread_id, tg_thread_src "
            "FROM transcriptions WHERE id = ?", (int(transcription_id),)).fetchone()
        if not r:
            return {"error": f"transcription {transcription_id} not found"}
        # Thread (Wave 4.5): a single TG message outside a conversation is often
        # unreadable («Ок», «а скільки там?»). We return the whole thread so that
        # context need not be guessed from neighbouring ids.
        thread = None
        if r["tg_thread_id"]:
            th = conn.execute("SELECT id, label, msg_count, first_date, last_date, status "
                              "FROM tg_threads WHERE id = ?", (r["tg_thread_id"],)).fetchone()
            msgs = conn.execute(
                "SELECT id, tg_message_id, tg_date, tg_sender, transcript_text "
                "FROM transcriptions WHERE tg_thread_id = ? AND deleted_at IS NULL "
                "ORDER BY tg_date LIMIT 60", (r["tg_thread_id"],)).fetchall()
            if th and len(msgs) > 1:
                thread = {**_row(th), "messages": [_row(m) for m in msgs]}
        comments = _comments_for(conn, "transcription", int(transcription_id))
        email_block = _email_blocks(conn, [r["id"]]).get(r["id"])
        if email_block and thread is None:
            thread = _email_thread(conn, r["id"])
        attached_to = _attached_to(conn, r["id"]) if r["source_type"] != "email" else []
    d = _row(r)
    d["display_name"] = record_meta.display_name(d)
    if email_block:
        d["email"] = email_block
    if attached_to:
        d["attached_to"] = attached_to
    if thread:
        d["thread"] = thread
    if comments:
        d["comments"] = comments
    if d.get("summary_json"):
        try:
            d["summary"] = json.loads(d.pop("summary_json"))
        except (ValueError, TypeError):
            d.pop("summary_json", None)
    segs = d.pop("segments", None)
    if include_segments and segs:
        try:
            d["segments"] = json.loads(segs)
        except (ValueError, TypeError):
            pass
    return d


@mcp.tool
def get_original_file(transcription_id: int, max_chars: int = 20000) -> dict:
    """Metadata of the original file of a recording/document + (for text ones: md/txt/csv) the content
    of the original from disk. For binary ones (pdf/docx/audio) returns path/type/size —
    the parsed text is already available via get_transcript."""
    with get_db_connection(DB_PATH) as conn:
        r = conn.execute(
            "SELECT id, source_type, doc_type, original_filename, file_path, byte_size "
            "FROM transcriptions WHERE id = ?", (int(transcription_id),)).fetchone()
    if not r:
        return {"error": f"transcription {transcription_id} not found"}
    d = _row(r)
    p = _safe_original_path(d.get("file_path"))
    d["original_available"] = bool(p and p.exists())
    if p and p.exists():
        d["resolved_path"] = str(p)
        if (d.get("doc_type") or "").lower() in _TEXT_DOC_TYPES:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                d["content"] = text[:int(max_chars)]
                d["content_truncated"] = len(text) > int(max_chars)
            except OSError as e:
                d["read_error"] = str(e)
    return d


@mcp.tool
def list_recent(limit: int = 20, source_type: Optional[str] = None,
                category_id: Optional[int] = None, tags: Optional[str] = None,
                since: Optional[str] = None, until: Optional[str] = None):
    """Latest archive records (id, date, source type, name, language, direction, text
    size). source_type: recording|youtube|document|telegram|email|file. For an overview of
    "what is fresh" before a targeted search.

    `display_name` — how to call the record to a person (`title` if the owner set it,
    otherwise `source_name`); `title`/`description` — raw fields, may be `null`.

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive. An invalid
    date returns `{"error": ...}` instead of an exception.

    The response envelope is always ONE shape: `{"items": [...], "note": str |
    None}`. `note` is an explanation for an empty/incomplete result (for example, a tag
    that is not in the dictionary: `"tag not found: …"`), `None` — when there is
    nothing to explain. Without this an agent indexing the list by a fixed path
    would break on the first unknown tag, when the shape was replaced by a bare
    list.

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    try:
        tag_list = categories.parse_tags(tags)
        since_p = categories.parse_date(since)
        until_p = categories.parse_date(until)
    except ValueError as e:
        return {"error": str(e)}

    sql = ("SELECT id, created_at, source_type, source_name, title, description, "
           "language, category_id, "
           "doc_type, meeting_date, tg_chat_id, tg_chat_title, tg_sender, tg_message_id, "
           "length(transcript_text) AS text_len FROM transcriptions")
    where, params = [], []
    if source_type:
        where.append("source_type = ?"); params.append(source_type)
    where_sql, params = _where_clause(
        where, params,
        categories.filter_sql("category_id", category_id),
        categories.tags_sql("id", tag_list),
        categories.date_sql("COALESCE(meeting_date, created_at)", since_p, until_p),
    )
    sql += where_sql
    sql += " ORDER BY id DESC LIMIT ?"; params.append(min(int(limit), 200))
    with get_db_connection(DB_PATH) as conn:
        rows = []
        for r in conn.execute(sql, params):
            d = _row(r)
            d["display_name"] = record_meta.display_name(d)
            rows.append(d)
        emails = _email_blocks(conn, [d["id"] for d in rows if d.get("source_type") == "email"])
        for d in rows:
            if d["id"] in emails:
                d["email"] = emails[d["id"]]
        note = _tags_note(conn, tag_list)
    return {"items": rows, "note": note}


@mcp.tool
def grep_archive(pattern: str, regex: bool = False, ignore_case: bool = True,
                 context: int = 1, limit: int = 20, source_type: Optional[str] = None,
                 transcription_id: Optional[int] = None, days: Optional[int] = None,
                 category_id: Optional[int] = None, tags: Optional[str] = None) -> dict:
    """Literal or regex search for an EXACT STRING over archive chunks, with ±context neighbouring
    chunks. Choose THIS tool, not `search_archive`, when you need to find EXACTLY
    THIS STRING: a record ID, an amount («1 200 000»), an @nick, a contract number, an exact name —
    anything that FTS5 tokenization and semantic embedding split into parts or
    hide behind a close-in-meaning but inexact match. `search_archive` is the
    opposite, when you need to find BY MEANING rather than by exact spelling.

    category_id — narrow the scan to a direction/project; tags — tag names separated by
    commas (OR), together with category_id — intersection (AND). Both filter in SQL
    BEFORE scanning; a tag that is not in the dictionary gives an empty result with the field
    `note` ("tag not found: …"), not a silent ignoring of the filter.

    The result order is by meeting date, NOT by relevance (there is no
    vector, no BM25, no reranker here); rows without a date come last. `truncated=True`
    has three different causes (`truncated_reason`), and the recipe differs:
    - `"limit"` — the whole archive was scanned, there are more matches than `limit`
      (`match_total` says exactly how many); raise `limit` or narrow with filters
      (`source_type`/`transcription_id`/`days`) to see the rest.
    - `"max_scan"` or `"deadline"` — the scan stopped EARLY, before reaching the
      end of the table; raising `limit` will do NOTHING, because matches beyond the scanned
      boundary were simply not scanned. `match_total` is then the count
      among what was scanned, not in the whole archive. The response carries
      `arbitrary_scan_caveat`: without `ORDER BY` in SQL the scanned prefix is
      ARBITRARY with respect to date, not guaranteed to be the newest records. The effective advice here
      is not `limit` (the tool has no `max_scan` parameter to widen the scan) —
      narrow `days`/`source_type`/`transcription_id` so that the scanned prefix
      covers a smaller, more precise slice of the archive.
    Limitation: the search goes over `chunks`, not the full transcription text —
    a string cut in half by a chunking seam will not be found
    (`chunk_boundary_caveat` in the response).
    Standalone: works without a running app.py (direct SQLite read).

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    from app.services.archive_grep import grep
    try:
        tag_list = categories.parse_tags(tags)
        res = grep(DB_PATH, pattern, regex=regex, ignore_case=ignore_case,
                   context=context, limit=limit, source_type=source_type,
                   transcription_id=transcription_id, days=days,
                   category_id=category_id, tags=tag_list)
    except ValueError as e:
        return {"error": str(e)}
    with get_db_connection(DB_PATH) as conn:
        note = _tags_note(conn, tag_list)
    if note:
        res["note"] = note
    _grep_hide_screen(res)
    return res


def _grep_hide_screen(res: dict) -> None:
    """On-screen text in grep — both in matches and in neighbouring context chunks (they have no
    speaker, so screen indexes are taken from the DB). The private filter, fail closed."""
    matches = res.get("matches") or []
    tids = sorted({m_.get("transcription_id") for m_ in matches if m_.get("transcription_id")})
    if not tids:
        return
    try:
        with get_db_connection(DB_PATH) as conn:
            screen = {(r[0], r[1]): r[2] for r in conn.execute(
                "SELECT transcription_id, chunk_index, text FROM chunks WHERE speaker = ? "
                f"AND transcription_id IN ({','.join('?' * len(tids))})",
                [_privacy.SCREEN_SPEAKER, *tids])}
        mode, words = _privacy.visual_mode(), _privacy.stop_words()
        private = _privacy.private_tids(DB_PATH, tids)
        # (record, index) of screen chunks that must not be given out
        blocked = {key for key, text in screen.items() if not _privacy.screen_chunk_allowed(
            {"speaker": _privacy.SCREEN_SPEAKER, "transcription_id": key[0], "text": text},
            private, words, mode)}
    except Exception:  # noqa: BLE001 — the check failed: do not give out screen text
        logger.warning("grep_archive: privacy not verified — on-screen text hidden",
                       exc_info=True)
        blocked = None
    kept = []
    for m_ in matches:
        tid = m_.get("transcription_id")
        if blocked is None:
            # we do not know which neighbours are screen ones — do not give context at all
            if m_.get("speaker") == _privacy.SCREEN_SPEAKER:
                continue
            m_["context_before"], m_["context_after"] = [], []
        else:
            if (tid, m_.get("chunk_index")) in blocked:
                continue
            for side in ("context_before", "context_after"):
                m_[side] = [c for c in (m_.get(side) or [])
                            if (tid, c.get("chunk_index")) not in blocked]
        kept.append(m_)
    if len(kept) != len(matches):
        res["matches"] = kept
        res["count"] = len(kept)
        res["private_hidden"] = len(matches) - len(kept)


@mcp.tool
def list_categories() -> list:
    """The tree of archive directions: direction → its projects (ADR-023 §A), right after
    every direction come its projects. Each element — `id, name, slug, color,
    icon, sort_order, parent_id, level` (`'direction'|'project'`), `items`
    (own live records) and `items_total` (together with the direction's projects) — for
    narrowing search via category_id (a direction's id expands in it to all
    its projects).

    `path` — the canonical node name ("Direction → Project" or "Direction"). The name is
    unique only among siblings, so projects of the same name in different directions
    are told apart by `path`: pass it in `project=` when the bare name is
    ambiguous."""
    with get_db_connection(DB_PATH) as conn:
        return categories.list_tree(conn)


@mcp.tool
def list_entities(type: Optional[str] = None, query: Optional[str] = None,
                  limit: int = 50) -> list:
    """Entities of the memory graph (people/projects/orgs/topics) with mention/
    meeting counts. type — person|project|org|topic; query — a substring of the name/alias.

    `same_name_ids` on a row means this name is registered in the graph once more — almost
    always under a different type («Polaris» as project and as org). The row's numbers then
    describe only its part of the history; the group total is given by `get_entity`
    (it counts by links, because the aggregate columns lag behind). The `type` filter
    SPLITS these rows: `type='org'` will return the «POLARIS» row with 32 meetings,
    while the whole group has 190."""
    sql = ("SELECT id, type, canonical_name, role, mention_count, meeting_count "
           "FROM entities")
    where, params = [], []
    if type:
        where.append("type = ?"); params.append(type)
    if query:
        where.append("(normalized_name LIKE ? OR id IN "
                     "(SELECT entity_id FROM entity_aliases WHERE normalized_alias LIKE ?))")
        like = f"%{query.lower()}%"; params += [like, like]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY mention_count DESC LIMIT ?"; params.append(min(int(limit), 200))
    with get_db_connection(DB_PATH) as conn:
        out = [_row(r) for r in conn.execute(sql, params)]
        # One pass over the graph (≈5k rows) instead of a query per row.
        by_key: dict[str, list[int]] = {}
        for r in conn.execute("SELECT id, canonical_name FROM entities"):
            k = _name_key(r["canonical_name"])
            if k:
                by_key.setdefault(k, []).append(r["id"])
    for d in out:
        twins = [i for i in by_key.get(_name_key(d.get("canonical_name")), [])
                 if i != d.get("id")]
        if twins:
            d["same_name_ids"] = twins
    return out


@mcp.tool
def get_entity(entity_id: int) -> dict:
    """Entity dossier: aliases, meetings (where it was mentioned, with salience) and linked
    tasks (action items)."""
    with get_db_connection(DB_PATH) as conn:
        e = conn.execute("SELECT * FROM entities WHERE id = ?", (int(entity_id),)).fetchone()
        if not e:
            return {"error": f"entity {entity_id} not found"}
        d = _row(e)
        d["aliases"] = [r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (int(entity_id),))]
        d["meetings"] = [_row(r) for r in conn.execute(
            "SELECT me.transcription_id, t.source_name, t.meeting_date, me.salience, "
            "me.role_in_meeting FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ? ORDER BY me.salience DESC LIMIT 50", (int(entity_id),))]
        d["action_items"] = [_row(r) for r in conn.execute(
            "SELECT id, task, due, status, transcription_id FROM action_items "
            "WHERE owner_entity_id = ? ORDER BY id DESC LIMIT 50", (int(entity_id),))]
        same = _same_name_rows(conn, int(entity_id), e["canonical_name"])
        if same:
            # The total is computed from links, not by adding up the `entities` columns.
            # Two reasons: rows share meetings (a sum would double them), and
            # the aggregates themselves go stale — in «POLARIS» the column says 32 with 78
            # real links, so adding up gave 134 where the truth is 190.
            ids = [int(entity_id)] + [r["id"] for r in same]
            ph = ",".join("?" * len(ids))
            # First fold by meeting, and only then sum. A plain
            # SUM would double: when one record is linked to both a project row and
            # an org row of the same name, the same mentions in the text would be counted
            # twice (on a project+org pair with the same name). We take MAX per
            # meeting: how many times the name was spoken there, not how many graph
            # rows look at it.
            agg = conn.execute(
                f"SELECT COUNT(*) AS meetings, COALESCE(SUM(m), 0) AS mentions FROM ("
                f"  SELECT transcription_id, MAX(mention_count) AS m "
                f"  FROM meeting_entities WHERE entity_id IN ({ph}) "
                f"  GROUP BY transcription_id)", ids).fetchone()
            d["same_name_entities"] = same
            d["totals_across_same_name"] = {
                "entities": len(ids),
                "meeting_count": agg["meetings"],
                "mention_count": agg["mentions"],
                "note": ("the same name is registered in the graph several times (most often under different "
                         "types); the numbers of this dossier concern ONLY this row, and here is the "
                         "group total computed from links (shared meetings are not "
                         "doubled)"),
            }
    return d


@mcp.tool
def list_action_items(window: str = "this_week", owner: Optional[str] = None,
                      category_id: Optional[int] = None, status: str = "open",
                      limit: int = 50, tags: Optional[str] = None,
                      since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Tasks/agreements from calls AND FROM CHATS by TIME WINDOW and owner.

    window: this_week | next_week | overdue | soon (14 days) | no_date | all.
    `this_week` means "still in time": counted from TODAY to the end of the week, not
    from Monday, so the windows do not overlap and the same thing is not read twice.
    owner — a substring of the owner's name («Юлія» will find «Юля» too, if they are merged in the graph).
    category_id — narrow to a direction. status: open | stale | done | cancelled | all
    (`stale` — tasks from old meetings with no future deadline: not deleted,
    but removed from digests). Duplicates of one task from different meetings are hidden.

    Returns both the raw deadline phrase (`due_raw`, as spoken) and the computed date
    (`due_date` + `due_precision`) — if a date looks odd, look at the raw phrase.
    The same with the owner: `owner` is the canonical name from the graph (merges «Юля»/«Юлія»/
    «Olena Koval»), `owner_said` is what he was called in the task itself. A discrepancy
    between these two fields means a crooked link in the graph, not two different tasks.

    A task spoken in Telegram (`source='tg_thread'`) carries an address:
    `chat` — in which chat, `said_by` — who said it, `chat_id`+`msg_id` — the exact
    address of the message. This is the answer to "where to write", not only "what to do".
    `link` (t.me) is not always present: in legacy Telegram groups a link to a
    message does not exist — the id pair itself serves as the address there.

    Returns `{items, coverage}`. `coverage` is the denominator under the same filters:
    date windows see only tasks with `due_date`, and a quarter of tasks have it. So
    "this week — 5" means "5 of the dated ones", not "there is almost nothing to do";
    `coverage.undated` says how many tasks will fall into no date window
    (they are in `window='no_date'`). Do not retell an empty window as "all quiet"
    without looking at the denominator.

    `coverage.window_matched` — how many tasks are really in THIS window, and
    `truncated` — whether `limit` cut the result. When `truncated=true`, the listed
    rows are not the whole window: raise `limit` or narrow the filter before drawing
    the conclusion "here is everything there is".

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive. An invalid
    date returns `{"error": ...}` instead of an exception; a tag that is not in the
    dictionary gives empty `items` and a `note` field ("tag not found: …") —
    not a silent ignoring of the filter.

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction").
    """
    from app.services import commitments
    try:
        tag_list = categories.parse_tags(tags)
        since_p = categories.parse_date(since)
        until_p = categories.parse_date(until)
    except ValueError as e:
        return {"error": str(e)}
    items = commitments.list_commitments(
        DB_PATH, window=window, owner=owner, category_id=category_id,
        status=status, limit=limit, tags=tag_list, since=since_p, until=until_p)
    matched = commitments.count_commitments(
        DB_PATH, window=window, owner=owner, category_id=category_id, status=status,
        tags=tag_list, since=since_p, until=until_p)
    coverage = commitments.commitments_coverage(
        DB_PATH, owner=owner, category_id=category_id, status=status,
        tags=tag_list, since=since_p, until=until_p)
    # How many there REALLY are in this window and whether the result was cut. Without this the denominator
    # next to a truncated list was harmful: "total 1700" beside 50 rows reads
    # as "here is the whole window, the other 1650 are elsewhere".
    coverage["window_matched"] = matched
    coverage["truncated"] = matched > len(items)
    result = {"items": items, "coverage": coverage}
    if tag_list:
        with get_db_connection(DB_PATH) as conn:
            note = _tags_note(conn, tag_list)
        if note:
            result["note"] = note
    return result


@mcp.tool
def list_dropped_commitments(days: int = 30, limit: int = 20, min_age_days: int = 14) -> list:
    """Promises that quietly died: a task was spoken, and AFTER that meeting the topic
    never came up again in any source (calls, Telegram, documents).

    days — how far back to take candidate tasks; min_age_days — a quarantine,
    so that yesterday's agreements are not counted "forgotten" just because nothing
    has happened after them yet. Answers "what did we lose focus on over the last month".

    Only promises from CONVERSATIONS are taken (call, meeting, Telegram, an uploaded
    audio file). Tasks from documents and YouTube videos do not get here: in a PDF
    to investors the "promises" are made by other companies, and they really do not surface in
    our conversations — because they never were ours.

    The task's topic is recognized by its two RAREST words, counted over the archive
    as of that meeting. The longest words do not work for this: in some tasks"alive" was proven only by a generic verb — "Continue work on the
    BUEF endowment" was considered surfaced because the word
    «стратегії» occurs 509 times in the archive.

    At most 3 tasks are taken from one meeting: a listened-to course is also
    a recording of a conversation, and its notes would otherwise take a sixth of the list.

    The list is NOT deterministic on a live archive, and that is as it should be: as soon as a
    message touching the task's topic arrives in the archive, the task stops being
    lost and disappears from the list."""
    from app.services import commitments
    return commitments.dropped_commitments(DB_PATH, days=days, limit=limit,
                                           min_age_days=min_age_days)


@mcp.tool
def list_stale_topics(days: int = 30, min_meetings: int = 3, limit: int = 15) -> list:
    """Topics/projects/organizations that were actively discussed (≥ min_meetings meetings)
    and then vanished from conversations for more than `days` days. The second half of the question
    "what fell out of focus" — at the level of directions, not individual tasks.

    Candidates come from the entity graph, but the silence is confirmed by TEXT: the graph
    covers only a quarter of the Telegram archive, so a topic written about daily in a chat has,
    for the graph, "gone silent" since the last call. `last_seen_source` says what
    the date rests on: `graph` — the name is absent from later texts, `text` — it is
    there (then the text date is taken). The exact form of the name: FTS does not catch cases,
    so the error goes toward "silent", not "alive"."""
    from app.services import commitments
    return commitments.stale_topics(DB_PATH, days=days, min_meetings=min_meetings, limit=limit)


@mcp.tool
def weekly_digest(owner: Optional[str] = None, days_back: int = 30, limit: int = 15) -> dict:
    """Monday digest in one call: what is on this week, what is overdue,
    what has no deadline, which promises got lost and which topics vanished from conversations.

    owner — narrow to a specific person ("what is on me" / «що на Юлії»).

    `coverage` in the response is the denominator for the first three buckets: they filter by
    `due_date`, which a quarter of tasks have. When retelling the digest, name it too, otherwise
    "five things for the week" sounds like the full picture instead of a quarter.

    `corrections` — the owner's correction comments for the period: places where a record
    turned out untrue and was fixed. This is the most expensive bucket of the digest: until such
    a correction is taken into account, every answer about that record stays wrong."""
    from app.services import commitments
    return commitments.weekly_digest(DB_PATH, owner=owner, days_back=days_back, limit=limit)


@mcp.tool
def list_open_questions(days: int = 90, limit: int = 20, window_h: float = 24.0,
                        chat_id: Optional[int] = None) -> dict:
    """Questions from Telegram addressed to the archive OWNER that he never answered.

    Answers "what is hanging over me in my chats". ONLY those questions are shown
    where the addressee is proven structurally: a private chat (nobody else is there) or an
    @mention of the owner's nick. A group question without an address does NOT get into the list —
    how many such were filtered out is visible in `skipped_group`. This is deliberate: there is no way to prove that
    a group "can we do 15:00?" is waiting for the owner specifically.

    `window_h` — how many hours of the owner's presence in THE SAME chat after the question
    count as an answer (a day by default). Presence is counted per chat, not
    per thread: threads are labelled by a model, and the answer regularly lands in a neighbouring one.
    `silence_h` in the response — after how many hours the owner appeared in the chat at all
    (null = never wrote there again).

    Not to be confused with `get_chat_context(chat_id).open_questions`: there are the questions that
    hang in a CHAT according to the local model (addressed to anyone), here — proven to
    the owner and without a model at all.

    Requires knowing who the owner is: taken from the Telegram listener (cached), or
    set by `TELEGRAM_SELF_NAME`/`TELEGRAM_SELF_USERNAME`. If unknown —
    returns an empty list and the reason in `reason`."""
    from app.services import tg_questions
    return tg_questions.open_questions(DB_PATH, days=days, limit=limit,
                                       window_h=window_h, chat_id=chat_id)


@mcp.tool
def list_pipe_runs(pipe: Optional[str] = None, limit: int = 20) -> dict:
    """History of scheduled pipes (K4): briefs that Recall itself sends to the Telegram bot.

    Pipes: `weekly-digest` (digest, Mon 9:00), `dropped-commitments` (forgotten promises,
    Mon 9:00), `open-questions` (unanswered questions, weekdays 9:00). Each row is
    one run: `trigger` (schedule | catchup — catching up on a missed one at start |
    manual), `status` (ok | skipped — empty | blocked — the private filter
    stopped it | error | running), `rendered_by` (claude | plain — a simple list,
    the reason in `error`), `cost_usd`, `chars`. Answers "did the digest arrive" and
    "why is the bot silent". The history does not store brief texts."""
    from app.services import pipes
    limit = max(1, min(int(limit), 200))
    try:
        return {"runs": pipes.list_runs(DB_PATH, pipe=pipe, limit=limit)}
    except sqlite3.OperationalError:
        return {"runs": [], "reason": "table pipe_runs does not exist yet — Recall has not been run since K4"}

@mcp.tool
def research_export(terms: str, category_id: Optional[int] = None,
                    tags: Optional[str] = None, since: Optional[str] = None,
                    until: Optional[str] = None) -> str:
    """Collect ALL mentions of a term/brand/entity from all sources into markdown (original
    quotes with provenance, no AI). terms — comma/space-separated terms.

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive. A tag that is
    not in the dictionary gives a line "tag not found: …" before the report — otherwise
    an empty report would read as "no mentions in the archive".

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    try:
        tag_list = categories.parse_tags(tags)
        since_p = categories.parse_date(since)
        until_p = categories.parse_date(until)
    except ValueError as e:
        return f"Filter error: {e}"
    try:
        from app.services import research
    except Exception as e:  # pragma: no cover
        return f"research module unavailable: {e}"
    collected = research.collect(DB_PATH, terms, category_id=category_id,
                                 tags=tag_list, since=since_p, until=until_p)
    try:
        out = research.render_originals_md(collected, "")
    except TypeError:
        out = json.dumps(collected, ensure_ascii=False, indent=2)
    with get_db_connection(DB_PATH) as conn:
        note = _tags_note(conn, tag_list)
    return f"{note}\n\n{out}" if note else out


# ====================== AI ON DEMAND (Claude, HTTP proxy to app.py) ===========

@mcp.tool
def ask_archive(question: str, k: int = 12, category_id: Optional[int] = None,
                project: Optional[str] = None, tags: Optional[str] = None,
                since: Optional[str] = None, until: Optional[str] = None,
                model: Optional[str] = None, explain: bool = False) -> dict:
    """RAG "Ask the archive": a question → a Claude answer ONLY from the archive + citations [n].

    k — how many chunks into the context (1–20; default 12 per the eval harness measurements:
    at k=8 some correct sources sit right beyond the cut —
    recall@8 67.9% vs recall@12 82.1%). Two levels of narrowing:
      * category_id — a direction (the coarse layer; the largest direction covers most of the archive,
        so by itself it hardly narrows anything);
      * project — a project or person name («Polaris», «Маяк», «Олена»):
        searches only records where this entity is mentioned. This is what saves from
        mixing directions. Several names — separated by commas. An unknown name does not
        zero out the search, it just does not narrow.

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive.

    For natural-language questions ("what did we decide on X?"); for a pinpoint search of
    fragments use search_archive. Changes nothing (works in read-only too).
    explain=True — add a relevance breakdown (bm25/dense/rrf/recency/rerank,
    source) to the cited chunks.

    A question with its answer settles in `ask_log` (channel="mcp") — raw material for the
    golden set; the owner rates 👍/👎 in the web chat, there is no tool for it.

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    pass in `project` the full path "Direction → Project" from the `path` field in
    `list_categories`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    body = {"question": question, "k": int(k), "category_id": category_id,
            "project": project, "model": model, "channel": "mcp"}
    if tags:
        body["tags"] = tags
    if since:
        body["since"] = since
    if until:
        body["until"] = until
    if explain:
        body["explain"] = True
    res = _api("POST", "/api/memory/ask", body=body, write=False, timeout=180.0)
    if isinstance(res, dict) and res.get("code") == "ambiguous":
        # 409 from /api/memory/ask: the name sits on several tree nodes.
        # We return a note listing the paths, not a silent answer from one of
        # them — the choice is made by whoever asked, and made explicitly.
        return {"error": res.get("message") or res.get("error"),
                "candidates": res.get("candidates", []),
                "hint": "Pass the full path 'Direction → Project' "
                        "(the path field in list_categories) in project=."}
    return res


@mcp.tool
def suggest_category(transcription_id: int) -> dict:
    """k-NN hint of a direction for a record (by neighbour embeddings). Changes
    nothing — only a suggestion (suggestion=None + reason on cold start).
    Applying a direction is an action in the Recall UI (MCP is read-only here)."""
    return _api("GET", f"/api/memory/transcriptions/{int(transcription_id)}/suggest-category")


@mcp.tool
def research_preview(q: Optional[str] = None, entity_id: Optional[int] = None,
                     category_id: Optional[int] = None, tags: Optional[str] = None,
                     since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Quick preview of mentions of a term/brand/entity across the whole archive (0 tokens,
    no AI). Give q (a substring) OR entity_id. For a full dump of quotes —
    research_export.

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive.

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    params: dict = {}
    if q is not None:
        params["q"] = q
    if entity_id is not None:
        params["entity_id"] = int(entity_id)
    if category_id is not None:
        params["category_id"] = int(category_id)
    if tags:
        params["tags"] = tags
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    return _api("GET", "/api/research/preview", params=params)


@mcp.tool
def research_summary(q: Optional[str] = None, entity_id: Optional[int] = None,
                     category_id: Optional[int] = None, tags: Optional[str] = None,
                     since: Optional[str] = None, until: Optional[str] = None,
                     model: Optional[str] = None) -> dict:
    """A consolidated structured report on a term/entity (Claude map-reduce over the
    extracted archive fragments). More expensive than research_preview (paid tokens),
    but gives ready markdown. Give q OR entity_id. Does not change state.

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive.

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    body: dict = {}
    if q is not None:
        body["q"] = q
    if entity_id is not None:
        body["entity_id"] = int(entity_id)
    if category_id is not None:
        body["category_id"] = int(category_id)
    if tags:
        body["tags"] = tags
    if since:
        body["since"] = since
    if until:
        body["until"] = until
    if model is not None:
        body["model"] = model
    res = _sse_collect("/api/research/summary", body, timeout=600.0)
    if res.get("error"):
        return {"error": res["error"]}
    done = res.get("done") or {}
    return {
        "markdown": done.get("markdown"),
        "model": done.get("model"),
        "stats": done.get("stats"),
        "cost": done.get("cost"),
        "tokens": {"input": done.get("input_tokens"), "output": done.get("output_tokens")},
    }


# ====================== RICHER READS, as in the UI (HTTP proxy to app.py) =====

@mcp.tool
def search_history(search: Optional[str] = None, source_type: Optional[str] = None,
                   language: Optional[str] = None, category_id: Optional[int] = None,
                   speaker_id: Optional[int] = None, tg_chat: Optional[str] = None,
                   tg_sender: Optional[str] = None, tags: Optional[str] = None,
                   since: Optional[str] = None, until: Optional[str] = None,
                   page: int = 1, per_page: int = 20) -> dict:
    """List/search of archive records with filters (FTS over the text via search) and
    pagination — a full search+facets like the UI of the Archive.

    Filters: source_type, language, category_id, speaker_id, and for Telegram —
    tg_chat (chat name, substring) and tg_sender (author, substring). This very pair
    gives a chronological reading of a chat: `source_type='telegram'` + `tg_chat='Маяк'`
    will return the correspondence of this chat in order, not a scatter of one-liners.

    tags — tag names separated by commas (OR); together with category_id — intersection (AND).
    since/until — ISO date on the meeting date, both bounds inclusive.

    Namesakes: the same name lives in different directions («Маркетинг» under two) —
    find the right node by the `path` field in `list_categories` and pass
    its `id` in `category_id`.

    ADR-023 §C — three filter parameters:
    `category_id` — id of a **direction or project** from `list_categories`; a
    direction automatically includes all its projects, a project only itself.
    This is the archive's hierarchy level.
    `project` — **not** a hierarchy project but a graph-entity name or free
    text (a person, a brand): narrows to records where that name is
    mentioned. To narrow to a project as a section of the archive, pass its id
    in `category_id`, not its name in `project`.
    `tags` — tag names comma-separated; several = any of them. Combined with
    `category_id` — an intersection ("tag AI only in this direction")."""
    params: dict = {"page": int(page), "per_page": int(per_page)}
    for key, val in (("search", search), ("source_type", source_type),
                     ("language", language), ("category_id", category_id),
                     ("speaker_id", speaker_id), ("tg_chat", tg_chat),
                     ("tg_sender", tg_sender), ("tags", tags),
                     ("since", since), ("until", until)):
        if val is not None:
            params[key] = val
    res = _api("GET", "/api/history", params=params)
    items = res.get("transcriptions") if isinstance(res, dict) else None
    mail_ids = [i["id"] for i in items or [] if i.get("source_type") == "email"]
    if mail_ids:
        with get_db_connection(DB_PATH) as conn:
            emails = _email_blocks(conn, mail_ids)
        for i in items:
            if i.get("id") in emails:
                i["email"] = emails[i["id"]]
    return res


@mcp.tool
def get_archive_stats() -> dict:
    """Archive aggregates: transcript counts by source, entity types,
    task statuses, etc. (as on the dashboard)."""
    return _api("GET", "/api/memory/stats")


@mcp.tool
def list_speakers(q: Optional[str] = None, limit: int = 50) -> dict:
    """Global speakers (+ autocomplete via q).

    These are VOICES from call diarization — not all people of the archive. The authors of chat
    messages (35 names over messages) are not here: `get_speaker_stats` gives them
    in `tg_senders`."""
    params: dict = {"limit": int(limit)}
    if q is not None:
        params["q"] = q
    return _api("GET", "/api/speakers", params=params)


@mcp.tool
def get_speaker_stats() -> dict:
    """Who spoke how much and who wrote how much — TWO DIFFERENT layers in one response.

    `speakers` — voices from diarization: records, seconds of speech, words, date
    range. Covers only calls and recordings (in the archive — some records).
    `tg_senders` — authors of chat messages: messages, in how many chats, first and
    last appearance. There are no seconds or words of speech there and cannot be.

    **The layers' units do not add up**: 40 thousand seconds and messages do not
    combine into "activity". To compare people — compare within a layer.

    A shared person is visible through `entity_id`, set in both lists: in
    diarization they are «Слава Верес», in the chats «Veres Viacheslav», and the same
    id means the GRAPH knows it. A dash means the graph does not know this spelling —
    do not draw one conclusion from two similar names on your own.

    `coverage` says how many records have diarization and how many senders
    are linked to the graph. Without it the list of voices read as the whole archive,
    though it describes 3% of it."""
    return _api("GET", "/api/speakers/stats")


@mcp.tool
def get_speaker_timeline(speaker_id: int, days: int = 30) -> dict:
    """A speaker's activity by day over the last N days.

    Counted from diarized segments, i.e. this is VOICE activity. Silence here
    does not mean the person was quiet: they may have written in chats all month —
    chat messages are not part of this timeline at all."""
    return _api("GET", f"/api/speakers/{int(speaker_id)}/timeline",
                params={"days": int(days)})


@mcp.tool
def list_bookmarks(transcription_id: int) -> dict:
    """Bookmarks of a specific transcript."""
    return _api("GET", f"/api/transcription/{int(transcription_id)}/bookmarks")


@mcp.tool
def list_saved_searches() -> dict:
    """Saved searches (name + saved filter object + counter)."""
    return _api("GET", "/api/saved-searches")


# ====================== LIVE / BACKGROUND TASKS (status-only) =================
# Polling the background task queue (transcribe/download/backfill/import/finalize)
# and status snapshots of the live subsystems: recording (Windows WASAPI mic/loopback), the call
# co-pilot (Qwen locally + Claude verification, 503 if COPILOT_ENABLED=False),
# the Telegram listener (Telethon, 503 if the process is offline). Control (start/stop/
# state change) is removed from MCP (read-first) — the Recall UI does those; here only
# viewing the current state.

@mcp.tool
def get_job_status(job_id: str) -> dict:
    """State of a background task from the queue (transcribe/download/backfill/import/finalize):
    queued|running|completed|failed|cancelled + metadata."""
    return _api("GET", f"/api/jobs/{job_id}")


# ---------------------------------------------------------------------------
# Visual timeline of a video recording (spec video-visual-timeline). All of it is a proxy to
# app.py: video decoding, OCR and frames live there, not in the stdio process.
# Secrets from the screen (passwords, tokens) are masked in the text as «[🔒 секрет]», painted over
# on frames; no tool returns raw values.
#
# The private filter (spec mcp-visual-privacy): everything visual goes from here to Claude, so the
# filter stands here, not in app.py — the Recall UI sees everything. No model, fail closed:
# a private direction (RECALL_PRIVATE_CATEGORIES) → refusal; a word from private.txt →
# segment/frame/hit removed (counter private_hidden); RECALL_MCP_VISUAL text|off.
# ---------------------------------------------------------------------------
from app.services import privacy as _privacy  # noqa: E402


def _visual_refusal(transcription_id: Optional[int], *, images: bool = False) -> Optional[dict]:
    mode = _privacy.visual_mode()
    if mode == 'off':
        return {"error_code": "MCP_VISUAL_OFF",
                "error": "Visual content via MCP is disabled (RECALL_MCP_VISUAL=off). Look in Recall."}
    if images and mode != 'images':
        return {"error_code": "MCP_VISUAL_TEXT_ONLY",
                "error": "Frames via MCP are disabled (RECALL_MCP_VISUAL=text) — text only: "
                         "get_visual_timeline / get_frames_for_text."}
    if transcription_id is None:
        return None
    try:
        private = _privacy.private_tids(DB_PATH, [transcription_id])
    except _privacy.PrivacyError:
        return {"error_code": "PRIVACY_CHECK_FAILED",
                "error": "Could not check whether the record is private — not giving out visual content."}
    if private:
        return {"error_code": "PRIVATE",
                "error": f"Record #{transcription_id} is in a private direction — its frames and on-screen "
                         f"text are not given out via MCP. Look in Recall."}
    return None


def _stop_words() -> Optional[list]:
    """Stop words; None — the file could not be read (then hide everything that was checked)."""
    try:
        return _privacy.stop_words()
    except _privacy.PrivacyError:
        logger.warning("private.txt could not be read — visual content with text is hidden", exc_info=True)
        return None


def _visual_text_hidden(item: dict, words: Optional[list], *keys: str) -> bool:
    if words is None:
        return True
    return any(_privacy.has_stop_word(item.get(k), words) for k in keys)


@mcp.tool
def video_analyze(transcription_id: int, method: Optional[str] = None,
                  step: Optional[float] = None, min_res: Optional[float] = None,
                  threshold: Optional[float] = None, settle: Optional[float] = None,
                  ignore_regions: Optional[list] = None, max_frames: Optional[int] = None,
                  ocr: bool = True, describe: bool = False,
                  vision_backend: Optional[str] = None, force: bool = False) -> dict:
    """Break a video recording into key frames ("slicing") and recognize the text on them.

    When to call: "break recording #N into frames", "what was on the screen", before
    get_visual_timeline / get_frame with from/to, if there is no timeline yet.

    Starts a BACKGROUND task and returns `job_id` at once — state and progress via
    `get_job_status(job_id)` (meta.progress: stage frames→ocr→describe→done, pct).
    If the recording has already been sliced with the same parameters — `{"ready": true}` with no task;
    if a task is already running — its `job_id` in `already_running`.

    How long: a 2-hour recording ≈ 2 min of slicing + ≈ 0.6 s OCR per frame (hundreds of frames
    → several minutes). Video exists only in Recall screen recordings; without it —
    `error_code: NO_VIDEO`.

    Parameters (all optional): method 'thumbs' (default, catches short changes
    A→B→A too) | 'bisect'; threshold — share of changed pixels (default 0.02);
    settle — a series of changes more often than once per N s = one frame (default 2); min_res —
    boundary precision, s (1); ignore_regions — [[x, y, w, h]] as fractions of the frame, e.g.
    webcam tiles [[0.8, 0, 0.2, 0.3]]; max_frames (1500).

    describe=true — also a description of every frame by a vision model. This is LONG (seconds per
    frame) and OFF by default. vision_backend: 'local' (default — the description STEP itself
    is done by a local model) or 'claude' — then the frames (with secrets painted over)
    GO TO ANTHROPIC for the description step and it costs money; only on explicit request.

    Remember: everything the timeline tools return (on-screen text, descriptions, frames
    from get_frame) lands in the context of whoever calls MCP — i.e. in Claude."""
    refusal = _visual_refusal(transcription_id)
    if refusal:
        return refusal
    body = {k: v for k, v in {
        'method': method, 'step': step, 'min_res': min_res, 'threshold': threshold,
        'settle': settle, 'ignore_regions': ignore_regions, 'max_frames': max_frames,
        'ocr': ocr, 'describe': describe, 'vision_backend': vision_backend, 'force': force,
    }.items() if v is not None}
    return _api("POST", f"/api/transcription/{transcription_id}/analyze-video", body=body)


@mcp.tool
def get_visual_timeline(transcription_id: int, from_sec: Optional[float] = None,
                        to_sec: Optional[float] = None, with_transcript: bool = True,
                        format: str = "json", limit: int = 30, offset: int = 0,
                        include_camera: bool = False, offset_sec: float = 0.0) -> dict:
    """Visual timeline of a recording: what was on the screen + what was said at that time.

    Each element is a segment [start, end] (s from the start of the recording) with one frame:
    frame_time, frame_type (screen_share / slide / camera_only / blank /
    transition), ocr_text (text from the screen), description (only if a description was already
    made — it is NOT generated here), duplicate_of (the same screen was already seen),
    transcript — phrases with speakers for the same interval.

    No pictures — only text and timecodes; look at a frame via get_frame. The timeline
    is large (hundreds of segments per 2 h) — take it in pages: limit/offset,
    `next_offset` in the response; or narrow the from_sec/to_sec window. format='markdown'
    gives the same as readable text (the `markdown` field).

    status: 'ready' | 'not_analyzed' (video_analyze first) | 'running' (with
    progress). include_camera=true shows frames with only webcams/empty ones too.
    offset_sec — a transcript shift relative to the video, if phrases "lag"."""
    refusal = _visual_refusal(transcription_id)
    if refusal:
        return refusal
    # always JSON: the filter works on segments, markdown is rendered from the already filtered
    params = {'with_transcript': int(with_transcript), 'format': 'json', 'limit': limit,
              'offset': offset, 'include_camera': int(include_camera),
              'offset_sec': offset_sec}
    if from_sec is not None:
        params['from'] = from_sec
    if to_sec is not None:
        params['to'] = to_sec
    res = _api("GET", f"/api/transcription/{transcription_id}/visual-timeline", params=params)
    if not isinstance(res, dict) or res.get('error'):
        return res
    words = _stop_words()
    items = res.get('items') or []
    kept = [i for i in items if not _visual_text_hidden(i, words, 'ocr_text', 'description')]
    res['items'] = kept
    res['private_hidden'] = len(items) - len(kept)
    res.pop('markdown', None)
    if format == 'markdown':
        from app.services.video_timeline import to_markdown
        res['markdown'] = to_markdown(res)
    return res


@mcp.tool
def get_frame(transcription_id: int, t: Optional[float] = None,
              from_sec: Optional[float] = None, to_sec: Optional[float] = None,
              limit: int = 6, size: str = "thumb"):
    """Look at the frames of a recording WITH EYES: returns an image + metadata.

    One frame — `t` (a second of the recording); works even without video_analyze (the frame
    is cut from the video on the fly). A batch — `from_sec`/`to_sec`: one frame per
    timeline segment in this window, no repeats, no more than `limit`
    (maximum 12) — for this the recording must be sliced (video_analyze).

    size: 'thumb' (≤960 px, cheaper for context) | 'full' (≤1920 px, small
    text). The frame description in the metadata — only if one was already made; this tool
    does not generate it. Boxes with passwords/tokens are painted black.

    Economy: get_visual_timeline first (text), pictures — only for key
    moments."""
    refusal = _visual_refusal(transcription_id, images=True)
    if refusal:
        return refusal
    params = {'limit': limit, 'size': size}
    if t is not None:
        params['t'] = t
    if from_sec is not None:
        params['from'] = from_sec
    if to_sec is not None:
        params['to'] = to_sec
    res = _api("GET", f"/api/transcription/{transcription_id}/frames", params=params,
               timeout=180.0)
    if not isinstance(res, dict) or res.get('error') or not res.get('frames'):
        return res
    from fastmcp.utilities.types import Image
    words = _stop_words()
    out: list = []
    meta = []
    hidden = 0
    for f in res['frames']:
        data = f.pop('image_b64', None)
        # frame_text — the text of this very frame; none (no OCR) — nothing to check with, we do not give it out
        frame_text = f.pop('frame_text', None)
        if frame_text is None or _visual_text_hidden(
                {**f, 'frame_text': frame_text}, words, 'frame_text', 'ocr_text', 'description'):
            hidden += 1
            continue
        meta.append(f)
        if data:
            out.append(Image(data=base64.b64decode(data), format='jpeg'))
    head = {'transcription_id': transcription_id, 'count': len(meta), 'frames': meta,
            'private_hidden': hidden}
    return [head] + out


@mcp.tool
def get_frames_for_text(transcription_id: int, query: Optional[str] = None,
                        segment_index: Optional[int] = None, window: float = 5.0,
                        offset_sec: float = 0.0) -> dict:
    """What was on the screen when a phrase was said: finds the phrase in the transcript
    (case-insensitive substring, up to 5 matches) or a segment by index and
    returns the timeline segments in a ±window s window — with ocr_text and frame_time.

    Example: "show what he showed when he said 'ось тут натискаємо'" →
    get_frames_for_text(N, query="тут натискаємо") → get_frame(N, t=frame_time).
    The recording must be sliced (video_analyze), otherwise frames are empty and analyzed=false."""
    refusal = _visual_refusal(transcription_id)
    if refusal:
        return refusal
    params = {'window': window, 'offset_sec': offset_sec}
    if query:
        params['query'] = query
    if segment_index is not None:
        params['segment_index'] = segment_index
    res = _api("GET", f"/api/transcription/{transcription_id}/frames-for-text", params=params)
    if not isinstance(res, dict) or res.get('error'):
        return res
    words = _stop_words()
    hidden = 0
    for m in res.get('matches') or []:
        frames = m.get('frames') or []
        kept = [f for f in frames if not _visual_text_hidden(f, words, 'ocr_text', 'description')]
        hidden += len(frames) - len(kept)
        m['frames'] = kept
    res['private_hidden'] = hidden
    return res


@mcp.tool
def search_visual(query: str, transcription_ids: Optional[list] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None,
                  limit: int = 20) -> dict:
    """Search over the text FROM THE SCREEN (OCR) and frame descriptions across the whole archive: "where on the
    screen was the form 'Реалізація товарів'", "when was the budget table shown".

    Returns the record, timecode, frame type, a text excerpt and frame_time for get_frame.
    A case-insensitive substring (Cyrillic handled correctly). Searches only in sliced
    records. The same frames are also visible in search_archive/ask_archive as a source
    «кадр ГГ:ХХ:СС» (speaker «екран»). date_from/date_to — YYYY-MM-DD."""
    params = {'q': query, 'limit': limit}
    if transcription_ids:
        params['transcription_ids'] = ','.join(str(int(x)) for x in transcription_ids)
    if date_from:
        params['date_from'] = date_from
    if date_to:
        params['date_to'] = date_to
    refusal = _visual_refusal(None)
    if refusal:
        return refusal
    res = _api("GET", "/api/video/search", params=params)
    if not isinstance(res, dict) or res.get('error'):
        return res
    hits = res.get('results') or []
    words = _stop_words()
    try:
        private = _privacy.private_tids(DB_PATH, [h.get('transcription_id') for h in hits])
        # the excerpt is only a piece of a frame: the stop word is searched in the FULL frame text from the DB
        ids = [int(h['keyframe_id']) for h in hits if h.get('keyframe_id') is not None]
        full: dict = {}
        if ids:
            with get_db_connection(DB_PATH) as conn:
                full = {r[0]: (r[1], r[2]) for r in conn.execute(
                    f"SELECT id, ocr_text, vision_text FROM video_keyframes "
                    f"WHERE id IN ({','.join('?' * len(ids))})", ids)}
    except Exception:  # noqa: BLE001 — the check failed: give out nothing
        logger.warning("search_visual: privacy not verified — results hidden",
                       exc_info=True)
        return {**res, 'count': 0, 'results': [], 'private_hidden': len(hits)}
    kept = []
    for h in hits:
        if h.get('transcription_id') in private or h.get('keyframe_id') not in full:
            continue
        ocr, vision = full[h['keyframe_id']]
        if _visual_text_hidden({'o': ocr, 'v': vision, 's': h.get('snippet')}, words,
                               'o', 'v', 's'):
            continue
        kept.append(h)
    res.update(results=kept, count=len(kept), private_hidden=len(hits) - len(kept))
    return res


_AMBIGUITY_FIELDS = ("session_id", "name", "auto_name", "elapsed_seconds", "streams",
                     "copilot_active")


def _live_sessions(active: dict) -> list:
    """List of active recordings from the `/api/recordings/active` response. The new shape
    (C5) carries `count` + `sessions`; the old one — only `session_id` → 1 recording if it
    exists, otherwise 0."""
    sessions = active.get("sessions")
    if "count" in active and isinstance(sessions, list):
        return sessions
    sid = active.get("session_id")
    return [{"session_id": sid}] if sid else []


def _ambiguity_payload(sessions: list) -> dict:
    """Response "there are several recordings — pass session_id" (ADR-026 D4)."""
    return {
        "success": False, "error_code": "AMBIGUOUS_SESSION",
        "sessions": [{k: (s.get(k) if isinstance(s, dict) else None) for k in _AMBIGUITY_FIELDS}
                     for s in sessions],
        "hint": "pass session_id",
    }


def _resolve_live_session(session_id: Optional[str]) -> tuple:
    """`(sid, None)` — the session was found (explicit or the only active one); otherwise
    `(None, payload)`: `payload` with `error_code == 'AMBIGUOUS_SESSION'` when there are
    several active recordings, or the raw `/api/recordings/active` response
    (zero recordings or an app.py failure — the tool builds its own "unavailable" response).
    Never guesses: with several recordings no call follows."""
    if session_id:
        return session_id, None
    active = _api("GET", "/api/recordings/active")
    if not isinstance(active, dict) or "error" in active:
        return None, active if isinstance(active, dict) else {"error": None}
    sessions = _live_sessions(active)
    if len(sessions) > 1:
        return None, _ambiguity_payload(sessions)
    if len(sessions) == 1:
        sid = active.get("session_id") or (sessions[0] or {}).get("session_id")
        if sid:
            return sid, None
    return None, active


@mcp.tool
def get_active_recording() -> dict:
    """The current active recording session (for re-attach) + what to latch onto in order to
    watch a call live (mcp-live-call-03): `copilot_session_id`
    (None if the copilot did not start for this recording), `copilot_active`
    (the copilot session is still 'active', not 'ended'), `live_transcript_available`
    (the same `available` field that `get_live_transcript` returns — we do not
    re-derive it here from `state.is_active`: the availability of the live preview
    is decided by the live worker, not by the recording session status, a finding of story 01).
    Always carries `count` and `sessions`; when there are several recordings, `session_id` = None
    and the session is not guessed — pick one from `sessions` and pass `session_id` on."""
    data = _api("GET", "/api/recordings/active")
    if not isinstance(data, dict) or "error" in data:
        return data
    sessions = _live_sessions(data)
    data["count"] = len(sessions)
    data["sessions"] = sessions
    if len(sessions) > 1:
        data["session_id"] = None
        return data
    if not data.get("active"):
        return data
    sid = data.get("session_id") or (sessions[0].get("session_id") if sessions else None)
    data["copilot_session_id"] = None
    data["copilot_active"] = False
    data["live_transcript_available"] = None
    if sid:
        row = None
        try:
            with get_db_connection(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT id, status FROM copilot_sessions WHERE recording_session_id = ? "
                    "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        except sqlite3.Error as e:
            # Degrade like the rest of the tool: a DB locked longer than busy_timeout is
            # no reason to break the response about an active recording that app.py
            # has already confirmed.
            logger.debug("[mcp] get_active_recording: copilot_sessions unavailable: %s", e)
        if row:
            data["copilot_session_id"] = row["id"]
            data["copilot_active"] = (row["status"] == "active")
        lt = get_live_transcript(session_id=sid)
        if isinstance(lt, dict) and "available" in lt:
            data["live_transcript_available"] = lt["available"]
    return data


# Ceiling of copilot events per tool call (separate from _LIVE_TRANSCRIPT_LIMIT=400
# in app/blueprints/recording.py — both exist per contract C4, plan.md, the values
# do NOT match: copilot events are heavier than a transcript segment).
_LIVE_COPILOT_LIMIT = 200

# Kinds of copilot_events — the comment of the kind column in app/db/migrations.py (v19).
# The source for validating kinds=... and for the full test (no kind is missed).
_COPILOT_EVENT_KINDS = frozenset({
    "topic_shift", "topic_return", "retrieval", "insight_local", "insight_verified",
    "escalation", "operator_action", "usage", "safety_sweep",
})


def _operator_saw(kind: str, payload: dict, event_id: int, verdict_by_ref: dict) -> Optional[bool]:
    """A derived (not raw) flag "the operator really saw this card".

    `payload.get("shown")` on its own lies in verified_only mode
    (app/services/copilot/config.py): there `insight_local` is persisted with
    `shown=False` BEFORE Claude's verdict, while the shown card goes only to SSE
    (`_publish("copilot_insight", ...)` in app/services/copilot/worker.py) —
    the event in the DB stays with `shown=False` although the operator saw it.

    The `ref_event_id`/verdict chain closes this: if for an `insight_local` there is a
    later `insight_verified` event with `payload.ref_event_id == id` and
    `verdict == "real"`, the card was shown after all (with the verified text).

    `insight_verified` itself comes in TWO shapes with the same kind:
    - an escalation verdict record (`ref_event_id` in the payload, WITHOUT `shown`) — it is
      metadata about another event, not a card by itself → None, not False;
    - a safety-sweep card (`_emit_insight` with kind_event="insight_verified") —
      carries its own `shown`, like insight_local.
    """
    if kind == "insight_local":
        if verdict_by_ref.get(event_id) == "real":
            return True
        shown = payload.get("shown")
        return bool(shown) if shown is not None else None
    if kind == "insight_verified" and "ref_event_id" in payload:
        return None
    return payload.get("shown")


@mcp.tool
def get_live_transcript(session_id: Optional[str] = None, since_seq: Optional[int] = None) -> dict:
    """Proxy to C1 (`GET /api/recording/<sid>/live-transcript`) — a snapshot of the
    live preview of segments of a recording in progress (RAM-only, vanishes after finalize —
    then use `get_transcript`). `session_id=None` → the active recording session.

    `since_seq` — **the only way to poll**: returns only segments with
    `seq > since_seq` (the number is shared by both tracks mic/system, grows in
    the order of the actual append — this guarantees that no track
    is lost). Take `next_seq` from the response VERBATIM and pass it as
    `since_seq` of the next call — do not compute the maximum yourself from the segments already received:
    an empty poll deliberately leaves `next_seq` unchanged so that
    truncated (`truncated`) segments do not vanish.

    There is deliberately no time filter (`since_sec`) here: the tracks are transcribed
    independently, and polling by time loses segments of the slower track.
    Needs a running app.py. Several active recordings and no `session_id` →
    `error_code: 'AMBIGUOUS_SESSION'` with a `sessions` list, the session is not guessed."""
    session_id, payload = _resolve_live_session(session_id)
    if not session_id:
        if payload.get("error_code") == "AMBIGUOUS_SESSION":
            return payload
        return {
            "success": True, "session_id": None, "available": False,
            "reason": payload.get("error") or "no active recording session",
            "is_active": False, "status": None, "elapsed_seconds": 0.0,
            "segments": [], "count": 0, "last_sec": None, "next_seq": None,
            "truncated": False,
        }
    params: dict = {}
    if since_seq is not None:
        params["since_seq"] = int(since_seq)
    return _api("GET", f"/api/recording/{session_id}/live-transcript", params=params or None)


@mcp.tool
def get_live_copilot(session_id: Optional[str] = None, since_event_id: Optional[int] = None,
                      kinds: Optional[str] = None) -> dict:
    """Direct-DB, the FULL stream of copilot events of a live call — including
    those muted by the attention budget and unverified `insight_local` (nothing
    is filtered here: the decision what to show stays with the agent, a non-goal
    of this story — not to narrow on the tool side).

    `session_id` — the id of the RECORDING session (`rec_...`, as in `get_live_transcript`), NOT
    `copilot_session_id`. `None` → the same active recording session as in
    `get_live_transcript` (via `/api/recordings/active` when app.py is alive —
    'active': false there means THERE IS NO active recording, not "look for something
    old in the DB"). If app.py is unavailable, the tool stays direct-DB: a fallback
    to `copilot_sessions` with status 'active' in the DB, only when there is exactly one — and
    then a hung session of a crashed process MAY turn up, which the field
    `session_resolution` in the response says. Several active recordings (or several
    'active' in the fallback) and no `session_id` → `error_code: 'AMBIGUOUS_SESSION'`
    with a `sessions` list, the session is not guessed.
    `since_event_id` — only events with `id >` the given one (incremental polling).
    `kinds` — an optional CSV filter (`"insight_local,escalation"`); empty —
    all kinds. An unknown kind is an explicit refusal with a `known_kinds` list, not a
    silent empty result.

    Each event carries an expanded `payload` + two top-level fields:
    - `shown` — the RAW value of `payload.shown` at the time of the DB write
      (`None` for kinds where showing a card does not apply: topic_shift,
      topic_return, retrieval, usage, operator_action, escalation, and for the
      `insight_verified` verdict record, see below). In verified_only mode
      (the default, `app/services/copilot/config.py`) this field LIES for
      `insight_local`: the event is persisted with `shown=False` BEFORE Claude's
      verdict, while the shown card goes only to SSE — the DB does not update at that moment.
    - `operator_saw` — a DERIVED, truthful indicator "the operator saw this",
      built from the `ref_event_id`/`verdict` chain: if for an
      `insight_local` an `insight_verified` later appeared with
      `ref_event_id == id` and `verdict == 'real'`, the card was shown
      (with the verified text) — `operator_saw=True` even if `shown`
      says `False`. The values of the two shapes of `insight_verified` differ: an
      escalation verdict record (has `ref_event_id`, no `shown`) is not a
      card by itself → `None`; a safety-sweep card (has `shown`, no `ref_event_id`)
      — like `insight_local`. The chain is computed within ONE call: if
      the verification came AFTER the cut/cursor of this page, the value
      stays as it was at the time of the call — best-effort, not a guarantee.

    Ceiling of 200 events per call (SQL `LIMIT`, no sorting out of the excess in Python),
    on truncation `truncated: true` — page on via `since_event_id` = the id
    of the last received event.

    Works WITHOUT a running app.py for an explicit `session_id`; for `session_id=None`
    an optional call to `/api/recordings/active` is made — its absence does not
    break the tool, it only disables the exact resolution in favour of the DB fallback."""
    session_resolution = None
    if not session_id:
        session_id, payload = _resolve_live_session(None)
        if not session_id:
            if payload.get("error_code") == "AMBIGUOUS_SESSION":
                return payload
            if "error" not in payload:
                return {
                    "success": True, "available": False,
                    "reason": "no active recording session",
                    "session_id": None, "copilot_session_id": None,
                    "events": [], "count": 0, "truncated": False,
                }
            session_resolution = (
                "app.py unavailable — the session was taken by the DB fallback (the only "
                "copilot_sessions with status 'active'), it may be left over from a "
                "crashed process")

    with get_db_connection(DB_PATH) as conn:
        if session_id:
            csess = conn.execute(
                "SELECT * FROM copilot_sessions WHERE recording_session_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        else:
            active_rows = conn.execute(
                "SELECT * FROM copilot_sessions WHERE status = 'active' "
                "ORDER BY id DESC").fetchall()
            rec_ids = list(dict.fromkeys(r["recording_session_id"] for r in active_rows))
            if len(rec_ids) > 1:
                return _ambiguity_payload(
                    [{"session_id": rid, "copilot_active": True} for rid in rec_ids])
            csess = active_rows[0] if active_rows else None
        if csess is None:
            return {
                "success": True, "available": False,
                "reason": (f"the copilot for session {session_id} was not started" if session_id
                           else "no active copilot session found"),
                "session_id": session_id, "copilot_session_id": None,
                "events": [], "count": 0, "truncated": False,
            }
        csess = _row(csess)
        cs_id = csess["id"]
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
        if kind_list:
            unknown = sorted(set(kind_list) - _COPILOT_EVENT_KINDS)
            if unknown:
                return {
                    "success": False,
                    "error": f"unknown kinds: {', '.join(unknown)}",
                    "known_kinds": sorted(_COPILOT_EVENT_KINDS),
                }
        query = ("SELECT id, ts_wall, ts_offset_sec, kind, topic_id, source, confidence, "
                 "payload_json, tokens_in, tokens_out, operator_action FROM copilot_events "
                 "WHERE copilot_session_id = ?")
        params: list = [cs_id]
        if since_event_id is not None:
            query += " AND id > ?"
            params.append(int(since_event_id))
        if kind_list:
            query += f" AND kind IN ({','.join('?' * len(kind_list))})"
            params.extend(kind_list)
        query += " ORDER BY id LIMIT ?"
        params.append(_LIVE_COPILOT_LIMIT + 1)
        rows = conn.execute(query, params).fetchall()

    truncated = len(rows) > _LIVE_COPILOT_LIMIT
    if truncated:
        rows = rows[:_LIVE_COPILOT_LIMIT]

    events = []
    verdict_by_ref: dict = {}
    for r in rows:
        e = _row(r)
        raw = e.pop("payload_json")
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            payload = {}
        e["payload"] = payload
        e["shown"] = payload.get("shown")
        events.append(e)
        if e["kind"] == "insight_verified" and "ref_event_id" in payload:
            verdict_by_ref[payload["ref_event_id"]] = payload.get("verdict")
    for e in events:
        e["operator_saw"] = _operator_saw(e["kind"], e["payload"], e["id"], verdict_by_ref)

    result = {
        "success": True, "available": True, "reason": None,
        "session_id": csess.get("recording_session_id"),
        "copilot_session_id": cs_id,
        "events": events, "count": len(events), "truncated": truncated,
    }
    if session_resolution:
        result["session_resolution"] = session_resolution
    return result


@mcp.tool
def ask_live(question: str, scope: str = "both", session_id: Optional[str] = None) -> dict:
    """Proxy to C2 (`POST /api/copilot/live-ask`) — the agent's question → the local
    model (Ollama, $0, no Claude) → an answer from the live transcript and/or
    the archive. Semantically a read: writes nothing to the DB, the recording session or
    the operator widget (A2, plan.md).

    `scope`: `'call'` — live transcript only | `'archive'` — archive only |
    `'both'` (default) — both. `session_id=None` → the active recording session
    (resolved on the app.py side). Ollama being unavailable is `available: false`
    + `reason` in the response, not an HTTP error. Needs a running app.py.
    Several active recordings and no `session_id` → `error_code:
    'AMBIGUOUS_SESSION'` with a `sessions` list, the question is not asked."""
    resolved, payload = _resolve_live_session(session_id)
    if payload is not None and payload.get("error_code") == "AMBIGUOUS_SESSION":
        return payload
    out = _api("POST", "/api/copilot/live-ask",
               body={"question": question, "session_id": resolved, "scope": scope})
    if isinstance(out, dict) and out.get("error_code") == "AMBIGUOUS_SESSION":
        return _ambiguity_payload(out.get("sessions") or [])
    return out


@mcp.tool
def copilot_availability() -> dict:
    """Whether the copilot is enabled + the status of the local LLM (Ollama)."""
    return _api("GET", "/api/copilot/availability")


@mcp.tool
def telegram_status() -> dict:
    """Whether the Telegram listener process is alive + the login status."""
    return _api("GET", "/api/telegram/status")


_MAIL_ACCOUNT_KEYS = ("id", "address", "configured", "last_poll_at", "last_error_code")
_MAIL_LABEL_KEYS = ("id", "folder", "display_name", "enabled", "system", "category_id", "enrich",
                    "last_sync_at", "synced_since", "last_uid", "last_error_code", "records")
_MAIL_POLLER_KEYS = ("running", "interval_min")


def _pick(src, keys) -> dict:
    src = src if isinstance(src, dict) else {}
    return {k: src[k] for k in keys if k in src}


@mcp.tool
def email_status() -> dict:
    """Mail ingest state: connected mailboxes (address, whether a credential is stored, last poll,
    last error code) and, per folder/label, whether it is enabled, how far it is synced
    (synced_since, last_sync_at), its error code and how many records it holds; plus whether the
    background poller runs. Take this FIRST when a search found no recent email: the folder may be
    disabled or the sync may have failed. Read-only; passwords are never part of the answer."""
    res = _api("GET", "/api/mail/status")
    if not isinstance(res, dict) or "error" in res:
        return res if isinstance(res, dict) else {"error": "unexpected response from /api/mail/status"}
    accounts = res.get("accounts")
    if isinstance(accounts, dict):  # the C2 status() envelope nested under the key
        accounts = accounts.get("accounts")
    out = []
    for a in accounts if isinstance(accounts, list) else []:
        item = _pick(a, _MAIL_ACCOUNT_KEYS)
        item["labels"] = [_pick(lb, _MAIL_LABEL_KEYS) for lb in (a.get("labels") or [])
                          if isinstance(lb, dict)] if isinstance(a, dict) else []
        out.append(item)
    return {"accounts": out, "poller": _pick(res.get("poller"), _MAIL_POLLER_KEYS)}


@mcp.tool
def telegram_coverage() -> dict:
    """How far the archive lags behind live Telegram — per monitored chat.

    Take this FIRST when a search in the Telegram part of the archive found nothing: the answer
    "it is not in the archive" is honest only when the chat is not lagging. Fields: when the chat
    last got into the archive (archived_last_date), when it was last written in
    (live_last_date), the lag in days (lag_days) and unread (unread_count).

    status: ok | behind (Telegram has newer than the archive) | never_ingested |
    migrated (chat_id changed after a move to a supergroup — the listener goes deaf) |
    not_listed (not among the dialogs: left/deleted/over the limit) | unknown
    (listener offline — the live half is unavailable, the archive half remains).

    behind_messages is counted only for supergroups (-100…): in the other chats
    message_id is from the account's global sequence. Gaps in ids may be
    stickers and service messages, which ingest deliberately does not take."""
    return _api("GET", "/api/telegram/coverage", timeout=90.0)


@mcp.tool
def get_chat_context(chat_id: int) -> dict:
    """Dossier of a Telegram chat: who is there, current topics, what hangs unanswered, what
    was agreed (Wave 4.5.2).

    Take this BEFORE asking the archive about a work chat: search returns separate
    lines, while here is the state of affairs as a whole. Especially for "what did I miss in X",
    "who is responsible for what" and "which questions are hanging".

    Fields:
      * participants — with HARD numbers (how much they wrote, when they appeared and
        last wrote): SQL counts them, the model does not rewrite them. `role` — a description
        from the local model, may be empty;
      * topics — open conversation THREADS (not the chat as a whole: in a work chat
        several projects run at once);
      * open_questions / decisions — with a thread_id they were taken from;
      * messages_seen / updated_at — as of when the dossier is. Updated by a
        threshold, so fresh messages may not be accounted for yet;
      * summary=None and model=None together mean the local model was
        unavailable and only the hard part exists — this is not an empty chat.

    Take chat_id from list_recent / search_history (the tg_chat_id field) or from
    telegram_coverage."""
    from app.services import tg_chat_context
    got = tg_chat_context.get_context(DB_PATH, int(chat_id))
    if not got:
        return {"error": f"no dossier for chat {chat_id} has been built yet",
                "hint": "python -m app.services.tg_chat_context refresh "
                        f"--chat {chat_id} --force"}
    # "Updated by a threshold" is true but unusable for a decision: the reader does not
    # know whether this is a lag of two messages or of two hundred. We compute the difference.
    if got.get("updated_at"):
        with get_db_connection(DB_PATH) as conn:
            got["messages_after_profile"] = conn.execute(
                "SELECT COUNT(*) FROM transcriptions WHERE tg_chat_id = ? "
                "AND created_at > ? AND deleted_at IS NULL",
                (int(chat_id), got["updated_at"])).fetchone()[0]
    return got


# ============================== RESOURCES =====================================
# The split into direct-DB/proxy is reference text for a human; it does NOT decide what is
# registered. The source of truth is the FastMCP registry (`_registered_tool_names`);
# a test catches a mismatch if these two buckets diverge from the registry.
_ABOUT_DIRECT_DB = (
    "get_transcript", "get_original_file", "list_recent", "list_categories",
    "list_entities", "get_entity", "list_action_items", "list_dropped_commitments",
    "list_stale_topics", "weekly_digest", "list_open_questions", "research_export",
    "get_chat_context", "list_comments", "grep_archive", "get_live_copilot",
    "list_pipe_runs",
)
_ABOUT_PROXY = (
    "search_archive", "ask_archive", "suggest_category", "research_preview",
    "research_summary", "search_history", "get_archive_stats", "list_speakers",
    "get_speaker_stats", "get_speaker_timeline", "list_bookmarks", "list_saved_searches",
    "get_job_status", "get_active_recording", "copilot_availability", "telegram_status",
    "email_status", "telegram_coverage", "get_live_transcript", "ask_live",
    "video_analyze", "get_visual_timeline", "get_frame", "get_frames_for_text",
    "search_visual",
)


def _registered_tool_names() -> list[str]:
    """Names of the tools really registered in FastMCP — not hardcoded, but the registry:
    a newly added tool changes here by itself, without editing `about()`."""
    tools = asyncio.run(mcp.list_tools())
    return sorted(t.name for t in tools)


@mcp.resource("recall://about")
def about() -> str:
    """System overview + live archive statistics (so Claude understands what is at hand)."""
    with get_db_connection(DB_PATH) as conn:
        n_tx = conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]
        n_ch = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        by_src = {r["source_type"]: r["c"] for r in conn.execute(
            "SELECT source_type, COUNT(*) c FROM transcriptions GROUP BY source_type")}
        tree = categories.list_tree(conn)
    n_tools = len(_registered_tool_names())
    return (
        f"# Recall — a local RAG archive of calls (MCP: read-first core, {n_tools} tools)\n\n"
        "A Flask app, data is local. Sources: call recordings (diarization), YouTube, "
        "documents (PDF/DOCX/…), Telegram, email. Search: e5 embeddings (1024-dim) + FTS5, "
        "RRF hybrid with recency/diversity.\n\n"
        f"## Statistics\n- transcripts: {n_tx}\n- chunks (vectorized): {n_ch}\n"
        f"- by source: {json.dumps(by_src, ensure_ascii=False)}\n"
        f"- directions (direction → its projects, ADR-023 §A): {_tree_summary_line(tree)}\n\n"
        "## How to use (everything below is read-only, changes no state)\n"
        "1. `search_archive(query)` — the main search (returns chunks with transcription_id);\n"
        "2. `get_transcript(id)`/`get_original_file(id)` — full text + summary/original;\n"
        "   `search_history(...)` — list/facets as in the UI; `list_recent`;\n"
        "3. `list_entities`/`get_entity` — the graph of people/projects/topics; `get_speaker_stats`/`get_speaker_timeline`;\n"
        "4. **Commitments:** `weekly_digest()` — the digest in one call («what is on me this week»); "
        "`list_action_items(window=this_week|next_week|overdue|no_date|all, owner=…)`; "
        "`list_dropped_commitments(days)` — promises with no trace; `list_stale_topics(days)` — topics "
        "that vanished from conversations; `list_open_questions(days)` — Telegram questions addressed personally to the owner "
        "that he did not answer. `research_preview`/`research_export`/`research_summary` — mentions of a term;\n"
        "5. `list_categories`/`list_speakers`/`list_bookmarks`/`list_saved_searches`/`get_archive_stats`;\n"
        "6. `ask_archive(question)` — a RAG answer with citations; `suggest_category` — a k-NN hint (not applied);\n"
        "7. **Live/background statuses:** `get_job_status`, `get_active_recording`, `copilot_availability`, "
        "`telegram_status`, `email_status` (mailboxes and per-folder sync state); `list_pipe_runs` — whether pipe briefs reached the bot; `list_comments` — the owner's own clarifications and corrections over the records;\n"
        "8. **Live call:** `get_live_transcript`/`ask_live` — a transcript preview and questions to it "
        "while the call goes on; `get_live_copilot` — the full stream of copilot events (including those muted by "
        "the budget and unverified), filtering is on the agent's side;\n"
        "9. **Recording video (what was on the screen):** `video_analyze(id)` — slicing into key frames + OCR "
        "as a background task (an exception to the rule below — at the owner's request); `get_visual_timeline` — "
        "frames + phrases by page; `get_frame` — the frame itself as a picture; `get_frames_for_text` — frames "
        "around a phrase; `search_visual` — search over on-screen text. Passwords and tokens from the screen are masked, "
        "MCP does not return raw values.\n\n"
        "## Autonomy (does it work without a running app.py?)\n"
        f"**Direct-DB, standalone ({len(_ABOUT_DIRECT_DB)}):** "
        + ", ".join(f"`{n}`" for n in _ABOUT_DIRECT_DB) +
        " — read SQLite directly, app.py is NOT needed.\n"
        f"**HTTP proxy to app.py ({len(_ABOUT_PROXY)}):** "
        + ", ".join(f"`{n}`" for n in _ABOUT_PROXY) +
        " — need a running `.venv/Scripts/python.exe app.py` "
        "(otherwise they return a clear error instead of crashing).\n\n"
        "**Strategic decision of the owner: MCP is read-first.** Creating/changing/deleting records, hardware control "
        "(microphone recording, copilot, Telegram chats) and paid/long Claude calls "
        "(enrich/polish/summarize/transcribe/ingest/backfill) are unavailable via MCP — the Recall UI does those.\n"
    )


@mcp.resource("recall://transcript/{transcription_id}")
def transcript_resource(transcription_id: str) -> str:
    """Full text of a transcript as a resource (to pull into a chat)."""
    d = get_transcript(int(transcription_id))  # reuse tool logic
    if "error" in d:
        return d["error"]
    head = f"# {d.get('display_name') or d.get('source_name')} (#{d.get('id')}, {d.get('source_type')}, {d.get('meeting_date') or d.get('created_at')})\n\n"
    return head + (d.get("transcript_text") or "")


@mcp.resource("recall://entity/{entity_id}")
def entity_resource(entity_id: str) -> str:
    """Entity dossier as a resource."""
    return json.dumps(get_entity(int(entity_id)), ensure_ascii=False, indent=2)


# ============================== MAIN ==========================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Recall MCP server")
    ap.add_argument("--transport", default="stdio", choices=["http", "stdio"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "5060")))
    args = ap.parse_args()

    if args.transport == "http" and not os.environ.get("RECALL_MCP_KEY"):
        sys.stderr.write(
            "REFUSED: http mode without RECALL_MCP_KEY = open access.\n"
            "Generate a key: python -c \"import secrets;print(secrets.token_urlsafe(32))\"\n"
            "and add to .env: RECALL_MCP_KEY=<key>. Or run --transport stdio.\n")
        sys.exit(2)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
