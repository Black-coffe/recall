"""T2.6 (REMEDIATION_PLAN Wave 2): a static lint gate against direct
write SQL in telegram_listener.py.

Architectural invariant: telegram_listener.py is a SEPARATE process (Telethon),
and must write to SQLite ONLY through the HTTP proxy to app.py (/api/telegram/ingest,
/api/telegram/chats/toggle) — not directly. app.py stays the only writer,
fewer chances of concurrent `database is locked` even with busy_timeout
(Wave 1, T2.3). Earlier this invariant was violated: `cmd_toggle` ran
`conn.execute("INSERT INTO tg_monitored_chats ...")` directly (T2.6 fixes
this — see telegram_listener.py::cmd_toggle, now a POST to app.py).

This test is not a unit test of a specific function but a STATIC GATE: it parses the AST of
telegram_listener.py and fails if a NEW `.execute(...)` appears anywhere
with an SQL string starting with INSERT/UPDATE/DELETE. Reads (SELECT) and
PRAGMA stay allowed — they do not write to the DB.

If the test failed after your change:
  1. Most likely — move the write to the HTTP proxy to app.py (as done
     for ingest and chats/toggle), and do not write to the DB directly from this process.
  2. If a direct write is really justified (e.g. a documented legitimate
     second «writer» role) — update ALLOWLIST below with a justifying comment
     and record the decision in an ADR (docs/adr/) + the project's memory/ (Option
     B from T2.6 explicitly documents dual-writer + retry/busy_timeout).
"""
from __future__ import annotations

import ast
from pathlib import Path

LISTENER_PATH = Path(__file__).resolve().parent.parent / "telegram_listener.py"

# Empty on purpose: telegram_listener.py currently MUST NOT have any
# direct write SQL. Every write (chat_id, chat, tg_monitored_chats) goes
# through HTTP to app.py. If a legitimate exception is ever needed — add
# the line number here with a comment why, and do not just widen it silently.
ALLOWLIST_LINES: set[int] = set()

_WRITE_KEYWORDS = ("insert", "update", "delete", "replace")


def _sql_literal(node: ast.AST) -> str | None:
    """Extract a constant SQL string from the first argument of .execute(...), if
    possible (a Constant string or an f-string with a static prefix)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string: we take only the constant pieces — enough to see
        # the SQL keyword at the start (it is always static in this file).
        parts = [v.value for v in node.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts) if parts else None
    return None


def _find_direct_writes(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source, filename=str(LISTENER_PATH))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        is_execute_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executemany", "executescript")
        )
        if not is_execute_call or not node.args:
            continue
        sql = _sql_literal(node.args[0])
        if not sql:
            continue
        normalized = sql.strip().lstrip("(").strip().lower()
        if normalized.startswith(_WRITE_KEYWORDS) and node.lineno not in ALLOWLIST_LINES:
            hits.append((node.lineno, sql.strip().splitlines()[0][:100]))
    return hits


def test_telegram_listener_has_no_direct_write_sql():
    source = LISTENER_PATH.read_text(encoding="utf-8")
    hits = _find_direct_writes(source)
    assert not hits, (
        "telegram_listener.py contains direct write SQL (.execute on "
        "INSERT/UPDATE/DELETE/REPLACE) outside the allowlist — violates the invariant "
        "«only app.py writes to the DB» (T2.6, REMEDIATION_PLAN Wave 2). "
        "Move the write to the HTTP proxy to app.py (app/blueprints/telegram.py) "
        "or deliberately add a line to ALLOWLIST_LINES with an explanation.\n"
        + "\n".join(f"  line {ln}: {snippet}" for ln, snippet in hits)
    )


def test_guard_detects_direct_write_when_present():
    """Sanity check of the gate itself: make sure the detector really catches
    write SQL and does not always return an empty list (otherwise the test above is
    a trap with a false GREEN)."""
    fake_source = (
        "import sqlite3\n"
        "def f(conn):\n"
        "    conn.execute(\"INSERT INTO tg_monitored_chats (chat_id) VALUES (?)\", (1,))\n"
    )
    tree_hits = _find_direct_writes(fake_source)
    assert len(tree_hits) == 1
    assert "INSERT INTO tg_monitored_chats" in tree_hits[0][1]
