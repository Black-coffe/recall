"""Scheduled pipes (K4): the archive comes to you with a brief by itself.

A pipe is a file ``pipes/<name>.md``: frontmatter (schedule, read functions, channel,
budget) + a prompt body. The scheduler is a daemon thread in ``app.py`` (only under
``__main__`` and with ``PIPES_ENABLED``); a missed run is caught up once.

Deliberate limits:
- Data functions are the closed ``TOOLS`` registry (read-only). A pipe does not call
  arbitrary MCP tools: a key outside the registry → the pipe is not loaded.
- Cron is a custom 5-field parser (``*``, numbers, lists, ranges, step),
  without croniter/apscheduler. Time is the machine's local time.
- The message is assembled by code (``pipes_cards``: HTML cards by direction). Claude,
  within ``budget_usd`` (estimated BEFORE the call), writes only 1-2 "headline" lines; no
  key, budget or error → the same message without those lines.
- Every run is a row in ``pipe_runs`` (migration v50); a failure does not kill the thread.
- Three callers share ``run_pipe``: the scheduler, the CLI and the on-demand MCP route
  (trigger ``mcp``, a W action). A per-pipe lock makes a concurrent call return ``busy``
  without a run; the route's 10-minute pause is ``cooldown_remaining``. Every finished
  run publishes a ``pipe_run`` event (metadata only) on the ring channel ``pipes``.

CLI:
    python -m app.services.pipes list            # pipes, schedule, next run
    python -m app.services.pipes run <name>      # trial run, text to stdout
"""
from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.core.i18n import server_lang, tr
from app.services import brief_actions, pipes_cards, pipes_privacy, sse_broker

logger = logging.getLogger(__name__)

PIPES_DIR = Path(__file__).resolve().parents[2] / "pipes"
DEFAULT_MAX_TOKENS = 1500
#: The "headline" is 1-2 lines; the model needs no more tokens.
HEADLINE_MAX_TOKENS = 300


class PipeError(ValueError):
    """The pipe file is invalid — the pipe is not loaded."""


# ----------------------------------------------------------------- cron

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31),
           ("month", 1, 12), ("day of week", 0, 7))


def _parse_field(spec: str, name: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise PipeError(f"cron: field «{name}»: invalid step «{step_s}»")
            step = int(step_s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise PipeError(f"cron: field «{name}»: invalid range «{part}»")
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = b = int(part)
        else:
            raise PipeError(f"cron: field «{name}»: invalid value «{part}»")
        if a < lo or b > hi or a > b:
            raise PipeError(f"cron: field «{name}»: «{part}» is out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


@dataclass(frozen=True)
class Cron:
    """5-field cron. Day of week: 0 and 7 are Sunday, 1 is Monday."""
    expr: str
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset          # 0=Sunday … 6=Saturday
    dom_any: bool
    dow_any: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        parts = (expr or "").split()
        if len(parts) != 5:
            raise PipeError(f"cron: 5 fields required, got {len(parts)}: «{expr}»")
        sets = [_parse_field(p, *f) for p, f in zip(parts, _FIELDS)]
        dow = frozenset(d % 7 for d in sets[4])
        return cls(expr, frozenset(sets[0]), frozenset(sets[1]), frozenset(sets[2]),
                   frozenset(sets[3]), dow, parts[2] == "*", parts[4] == "*")

    def _day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        # The classic cron rule: both fields restricted → one is enough.
        if self.dom_any or self.dow_any:
            return dom and dow
        return dom or dow

    def prev(self, now: datetime) -> Optional[datetime]:
        """The last schedule moment ≤ now (within a year), otherwise None."""
        now = now.replace(second=0, microsecond=0)
        for back in range(0, 367):
            d = (now - timedelta(days=back)).date()
            if not self._day_matches(d):
                continue
            for h in sorted(self.hours, reverse=True):
                if back == 0 and h > now.hour:
                    continue
                for m in sorted(self.minutes, reverse=True):
                    t = datetime(d.year, d.month, d.day, h, m)
                    if t <= now:
                        return t
        return None

    def next(self, now: datetime) -> Optional[datetime]:
        """The first schedule moment > now (within a year), otherwise None."""
        now = now.replace(second=0, microsecond=0)
        for fwd in range(0, 367):
            d = (now + timedelta(days=fwd)).date()
            if not self._day_matches(d):
                continue
            for h in sorted(self.hours):
                if fwd == 0 and h < now.hour:
                    continue
                for m in sorted(self.minutes):
                    t = datetime(d.year, d.month, d.day, h, m)
                    if t > now:
                        return t
        return None


# ------------------------------------------------------- function registry

def _weekly_digest(db_path: str, now: datetime) -> dict:
    from app.services import commitments
    return commitments.weekly_digest(db_path, today=now.date())


def _dropped_commitments(db_path: str, now: datetime) -> dict:
    from app.services import commitments
    return {"dropped": commitments.dropped_commitments(db_path, today=now.date())}


def _open_questions(db_path: str, now: datetime) -> dict:
    from app.services import tg_questions
    return tg_questions.open_questions(db_path, today=now)


MCP_WINDOW_H = 24


def _utc(dt: datetime) -> datetime:
    """A naive local moment (the scheduler's clock) → naive UTC, for the UTC journals."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def mcp_actions_summary(db_path: str, *, now: datetime) -> dict:
    """What the MCP (any non-UI caller) did in the last 24 h, for the daily brief.

    ``groups``: audited calls (``mcp_actions``, ``actor != 'ui'``) by tool and action;
    ``refused``: those the gate did not let through (a ``decision`` other than ``approved``).
    ``items``: one per undoable event, only the three inverses the owner named: a task
    action (``action_item_events``), a chat the MCP disabled and a chat direction the MCP
    changed (``tg_chat_events``); an event is undoable only while it is the last one for
    its task / chat and kind (the undo functions refuse otherwise). Read-only."""
    since = now - timedelta(hours=MCP_WINDOW_H)
    since_utc = _utc(since)
    with _connect(db_path) as conn:
        if not _has_table(conn, "mcp_actions"):
            return {"reason": tr("telegram.brief.mcp.noAudit", lang=server_lang()),
                    "groups": [], "items": []}
        rows = conn.execute(
            "SELECT COALESCE(claimed_tool, endpoint, path) AS tool, "
            "COALESCE(action, method) AS action, COUNT(*) AS n, "
            "SUM(CASE WHEN decision IS NOT NULL AND decision != 'approved' THEN 1 ELSE 0 END) "
            "AS refused FROM mcp_actions WHERE actor != 'ui' AND at >= ? "
            "GROUP BY 1, 2 ORDER BY n DESC, tool, action",
            (since_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)).fetchall()
        groups = [{"tool": r["tool"], "action": r["action"], "count": r["n"]} for r in rows]
        items: list[dict] = []
        if _has_table(conn, "action_item_events"):
            for r in conn.execute(
                    "SELECT e.id, e.at, e.action, ai.task, t.category_id "
                    "FROM action_item_events e JOIN action_items ai ON ai.id = e.item_id "
                    "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
                    "WHERE e.source = 'mcp' AND e.action != 'undo' AND e.at >= ? "
                    "AND NOT EXISTS (SELECT 1 FROM action_item_events l "
                    "WHERE l.item_id = e.item_id AND l.id > e.id) ORDER BY e.id",
                    (since.isoformat(timespec="seconds"),)):
                items.append({"at": r["at"], "kind": "task", "text": r["task"] or "",
                              "action": r["action"], "category_id": r["category_id"],
                              "undo": {"kind": "task", "ref": r["id"]}})
        if _has_table(conn, "tg_chat_events"):
            for r in conn.execute(
                    "SELECT e.id, e.at, e.kind, m.title, m.category_id, "
                    "co.name AS old_name, cn.name AS new_name "
                    "FROM tg_chat_events e JOIN tg_monitored_chats m ON m.chat_id = e.chat_id "
                    "LEFT JOIN categories co ON co.id = CAST(NULLIF(e.old_value, '') AS INTEGER) "
                    "LEFT JOIN categories cn ON cn.id = CAST(NULLIF(e.new_value, '') AS INTEGER) "
                    "WHERE e.source = 'mcp' AND e.undone_at IS NULL AND e.at >= ? "
                    "AND (e.kind = 'category' OR (e.kind = 'enabled' AND e.new_value = '0' "
                    "AND COALESCE(e.old_value, '') != '')) "
                    "AND NOT EXISTS (SELECT 1 FROM tg_chat_events l WHERE l.chat_id = e.chat_id "
                    "AND l.kind = e.kind AND l.id > e.id) ORDER BY e.id",
                    (_ts(since_utc),)):
                item = {"at": r["at"], "kind": "chat_" + r["kind"], "text": r["title"] or "",
                        "category_id": r["category_id"],
                        "undo": {"kind": "chat", "ref": r["id"]}}
                if r["kind"] == "category":
                    item.update(old=r["old_name"] or "", new=r["new_name"] or "")
                items.append(item)
    items.sort(key=lambda it: str(it["at"]).replace("T", " "))
    return {"window_h": MCP_WINDOW_H, "total": sum(g["count"] for g in groups),
            "refused": sum(r["refused"] or 0 for r in rows), "groups": groups, "items": items}


def _mcp_actions(db_path: str, now: datetime) -> dict:
    return mcp_actions_summary(db_path, now=now)


#: Closed registry of read functions. A new function goes only here, read-only only.
TOOLS: dict[str, Callable[[str, datetime], dict]] = {
    "weekly_digest": _weekly_digest,
    "dropped_commitments": _dropped_commitments,
    "open_questions": _open_questions,
    "mcp_actions": _mcp_actions,
}


# ----------------------------------------------------------- pipe file

@dataclass
class Pipe:
    name: str
    schedule: Cron
    tools: list[str]
    channel: str
    budget_usd: float
    skip_if_empty: bool
    prompt: str
    path: Optional[Path] = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    extra: dict = field(default_factory=dict)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise PipeError("no frontmatter (the file must start with a --- line)")
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise PipeError(f"frontmatter: line without «:» — «{line.strip()}»")
        k, v = line.split(":", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        meta[k.strip()] = v
    raise PipeError("frontmatter not closed by a --- line")


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "так", "да")


def parse_pipe(text: str, name: str, path: Optional[Path] = None) -> Pipe:
    meta, body = _split_frontmatter(text)
    for key in ("schedule", "tools", "channel"):
        if not meta.get(key):
            raise PipeError(f"missing required field «{key}»")
    tools = [t.strip() for t in meta["tools"].strip("[]").split(",") if t.strip()]
    unknown = [t for t in tools if t not in TOOLS]
    if unknown:
        raise PipeError(f"functions outside the registry: {unknown} (allowed: {sorted(TOOLS)})")
    if not tools:
        raise PipeError("empty tools list")
    if not body:
        raise PipeError("empty prompt (file body)")
    try:
        budget = float(meta.get("budget_usd", "0") or 0)
        max_tokens = int(meta.get("max_tokens", DEFAULT_MAX_TOKENS))
    except ValueError as e:
        raise PipeError(f"budget_usd/max_tokens is not a number: {e}") from e
    known = {"schedule", "tools", "channel", "budget_usd", "skip_if_empty", "max_tokens"}
    return Pipe(name=name, schedule=Cron.parse(meta["schedule"]), tools=tools,
                channel=meta["channel"], budget_usd=budget,
                skip_if_empty=_bool(meta.get("skip_if_empty", "false")),
                prompt=body, path=path, max_tokens=max_tokens,
                extra={k: v for k, v in meta.items() if k not in known})


def load_pipes(pipes_dir: Optional[Path] = None) -> list[Pipe]:
    """All valid pipes from the directory. An invalid one is skipped with the reason in the log."""
    d = Path(pipes_dir or PIPES_DIR)
    out: list[Pipe] = []
    if not d.is_dir():
        logger.info("Pipes: directory %s does not exist — no pipes", d)
        return out
    for p in sorted(d.glob("*.md")):
        try:
            out.append(parse_pipe(p.read_text(encoding="utf-8"), p.stem, p))
        except (PipeError, OSError) as e:
            logger.warning("Pipe %s not loaded: %s", p.name, e)
    return out


# ------------------------------------------------------------- history

def _ts(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Commit and CLOSE: `with sqlite3.connect()` only commits, the connection hangs until GC."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def list_runs(db_path: str, *, pipe: Optional[str] = None, limit: int = 20) -> list[dict]:
    q = "SELECT * FROM pipe_runs"
    args: list[Any] = []
    if pipe:
        q += " WHERE pipe = ?"
        args.append(pipe)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


#: A run hanging in `running` longer than this — the process died in the middle of it.
STALE_RUNNING = timedelta(minutes=30)


def _next_attempt(db_path: str, pipe: str, due: datetime, now: datetime, *,
                  retry_minutes: int, retry_max: int) -> Optional[int]:
    """The attempt number to make for the moment ``due`` now, or None.

    A moment is closed if after it there is an ``ok``/``skipped``/``blocked`` or
    ``retry_max`` errors. ``error`` and a stuck ``running`` are NOT a run that
    happened: the week's brief must not be lost because of one failure at 9:00. A manual
    ``pipes run`` (to stdout, not to the bot) and an on-demand ``mcp`` run do not close
    the moment.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, started_at, finished_at FROM pipe_runs WHERE pipe = ? "
            "AND trigger NOT IN ('manual', 'mcp') AND (scheduled_for >= ? OR started_at >= ?)",
            (pipe, _ts(due), _ts(due))).fetchall()
    errors, last_error = 0, None
    for r in rows:
        if r["status"] in ("ok", "skipped", "blocked"):
            return None
        started = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
        if r["status"] == "running" and now - started < STALE_RUNNING:
            return None                       # running right now
        errors += 1
        t = (datetime.strptime(r["finished_at"], "%Y-%m-%d %H:%M:%S")
             if r["finished_at"] else started)
        last_error = max(last_error or t, t)
    if errors >= retry_max:
        return None
    if last_error and now - last_error < timedelta(minutes=retry_minutes):
        return None
    return errors + 1


#: The pause after a run before an on-demand (MCP) run of the same pipe is allowed.
COOLDOWN_MINUTES = 10


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


def cooldown_remaining(db_path: str, name: str, *, now: datetime,
                       minutes: int = COOLDOWN_MINUTES) -> int:
    """Seconds until an on-demand run of ``name`` is allowed; 0 when free.

    An ``ok``/``skipped``/``blocked`` run blocks for ``minutes`` after it finished; a
    ``running`` row blocks until it turns stale (``STALE_RUNNING``); ``error`` does not
    block. Only the on-demand route reads this: the scheduler and the CLI are not throttled.
    """
    window = timedelta(minutes=minutes)
    cutoff = now - max(window, STALE_RUNNING)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, started_at, finished_at FROM pipe_runs "
            "WHERE pipe = ? AND started_at >= ?", (name, _ts(cutoff))).fetchall()
    left = timedelta(0)
    for r in rows:
        started = _parse_ts(r["started_at"])
        if r["status"] == "running":
            until = started + STALE_RUNNING
        elif r["status"] in ("ok", "skipped", "blocked"):
            until = (_parse_ts(r["finished_at"]) or started) + window
        else:
            continue
        left = max(left, until - now)
    return int(left.total_seconds() + 0.999) if left > timedelta(0) else 0


_PIPE_LOCKS: dict[str, threading.Lock] = {}
_PIPE_LOCKS_GUARD = threading.Lock()


def _pipe_lock(name: str) -> threading.Lock:
    with _PIPE_LOCKS_GUARD:
        lock = _PIPE_LOCKS.get(name)
        if lock is None:
            lock = _PIPE_LOCKS[name] = threading.Lock()
        return lock


def _publish_run(res: dict) -> None:
    """``pipe_run`` event for ``wait_for``: run metadata only, never the text."""
    try:
        sse_broker.broker.publish("pipes", "pipe_run", {
            "pipe": res["pipe"], "run_id": res["id"], "status": res["status"],
            "trigger": res["trigger"], "rendered_by": res["rendered_by"],
            "cost_usd": res["cost_usd"]})
    except Exception as e:  # noqa: BLE001 - the run is already recorded; the event is a courtesy
        logger.warning("Pipe %s: pipe_run event not published (%s)", res["pipe"],
                       type(e).__name__)


# --------------------------------------------------------------- render

def is_empty(data: dict) -> bool:
    """Empty = in the data of all functions there is not a single non-empty list."""
    for result in data.values():
        if not isinstance(result, dict):
            continue
        for v in result.values():
            if isinstance(v, list) and v:
                return False
    return True


_TOOL_LABELS = ("weekly_digest", "dropped_commitments", "open_questions")


def _tool_label(tool: str) -> str:
    if tool == "mcp_actions":
        return tr("telegram.brief.mcp.tool", lang=server_lang())
    return tr(f"telegram.brief.tool.{tool}", lang=server_lang()) if tool in _TOOL_LABELS else tool


def not_collected(data: dict) -> list[str]:
    """"Not collected" lines for functions that returned `reason` (the data could not be collected).

    An empty list with `reason` is NOT "nothing there": e.g. `open_questions` without a
    known Telegram owner returns [] and an explanation. Silence here would not differ from
    "no questions", so the line is added by code, not by the model."""
    out = []
    lang = server_lang()
    for tool, res in data.items():
        if isinstance(res, dict) and res.get("reason"):
            reason = res["reason"]
            # The service writes the reason as a Ukrainian literal; a known reason (unknown
            # owner in open_questions) is translated by the catalog into the brief's language.
            if tool == "open_questions" and res.get("me") is None:
                reason = tr("telegram.brief.reason.noOwner", lang=lang)
            out.append(tr("telegram.brief.notCollected", lang=lang,
                          tool=_tool_label(tool), reason=reason))
    return out


_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            import anthropic  # lazy
            _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return _client


_SYSTEM = (
    "Ти пишеш 1–2 короткі рядки «головне» над брифінгом з особистого архіву дзвінків і "
    "чатів власника. Сам список задач уже складено кодом — не перелічуй його. Пиши "
    "українською, простим текстом: без Markdown, без зірочок, заголовків і списків. "
    "Використовуй ЛИШЕ факти з JSON-даних: не додавай висновків, оцінок і подій, яких "
    "там нема. Імена, дати й назви — дослівно."
)


def _model() -> str:
    from app.core import settings as _settings
    return _settings.env("PIPES_MODEL")


def render_claude(pipe: Pipe, data: dict) -> tuple[str, float]:
    """"Headline" from Claude (1-2 lines of plain text). → (lines, cost $).

    Raises _PlainFallback with a reason — then the message goes without the "headline"."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise _PlainFallback("no ANTHROPIC_API_KEY")
    from app.services.pricing import estimate_cost
    model = _model()
    payload = json.dumps(data, ensure_ascii=False, default=str)
    user = f"{pipe.prompt}\n\nДані (JSON):\n{payload}"
    # A rough estimate of input tokens with a margin: Cyrillic is ~2 characters per token.
    max_tokens = min(pipe.max_tokens, HEADLINE_MAX_TOKENS)
    est = estimate_cost(model, (len(user) + len(_SYSTEM)) // 2, max_tokens)
    if est > pipe.budget_usd:
        raise _PlainFallback(f"budget: estimate ${est:.4f} > ${pipe.budget_usd:.4f}")
    try:
        msg = _get_client().messages.create(
            model=model, max_tokens=max_tokens, system=_SYSTEM,
            messages=[{"role": "user", "content": user}])
    except Exception as e:
        logger.warning("Pipe %s: Claude did not answer (%s)", pipe.name, type(e).__name__,
                       exc_info=True)
        raise _PlainFallback(f"Claude error: {type(e).__name__}") from e
    text = pipes_cards.clean_headline("".join(getattr(b, "text", "") for b in msg.content))
    if not text:
        raise _PlainFallback("Claude returned an empty answer")
    usage = getattr(msg, "usage", None)
    cost = estimate_cost(model, getattr(usage, "input_tokens", 0) or 0,
                         getattr(usage, "output_tokens", 0) or 0,
                         getattr(usage, "cache_read_input_tokens", 0) or 0)
    return text, cost


class _PlainFallback(Exception):
    """"Headline" will not be there — cards go without it; the argument is the reason for a human."""


# ---------------------------------------------------------------- run

def run_pipe(pipe: Pipe, *, db_path: str, deliver: Callable[[str], None],
             now: Optional[datetime] = None, trigger: str = "manual",
             scheduled_for: Optional[datetime] = None,
             attempt: int = 1, max_attempts: int = 1, retry_minutes: int = 0,
             buttons: bool = False) -> dict:
    """One run of a pipe: data → render → deliver → a row in pipe_runs.

    Never raises: a failure of a data function or of delivery is written as status='error'.
    A failure BEFORE delivery → a short message to the same channel (only
    the pipe name and error class, no data). A failure of delivery itself — only history and
    log: sending a message about a channel failure through the same channel makes no sense.
    ``blocked`` (private filter): the brief does not go, but a message about that does —
    without data and without the stop word itself (silence would read as "there will be no brief at all").
    With retries (``max_attempts`` > 1) the message goes on the first error and on the
    last attempt, with silence in between.
    ``buttons`` (K5): tasks are numbered, and ``deliver`` also gets ``reply_markup``
    with buttons — so only for a delivery that accepts it (``tg_bot.send``).
    A run of the same pipe already in progress (any caller) → ``status: "busy"``
    with no ``pipe_runs`` row, no delivery and no event.
    """
    lock = _pipe_lock(pipe.name)
    if not lock.acquire(blocking=False):
        logger.info("Pipe %s (%s): busy, another run is in progress", pipe.name, trigger)
        return {"id": None, "pipe": pipe.name, "trigger": trigger, "status": "busy",
                "rendered_by": None, "cost_usd": 0.0, "error": "busy", "chars": 0}
    try:
        return _run_pipe_locked(pipe, db_path=db_path, deliver=deliver, now=now,
                                trigger=trigger, scheduled_for=scheduled_for,
                                attempt=attempt, max_attempts=max_attempts,
                                retry_minutes=retry_minutes, buttons=buttons)
    finally:
        lock.release()


def _run_pipe_locked(pipe: Pipe, *, db_path: str, deliver: Callable[[str], None],
                     now: Optional[datetime], trigger: str,
                     scheduled_for: Optional[datetime], attempt: int, max_attempts: int,
                     retry_minutes: int, buttons: bool) -> dict:
    now = now or datetime.now()
    t0 = datetime.now()                   # duration by the real clock
    with _connect(db_path) as conn:
        run_id = conn.execute(
            "INSERT INTO pipe_runs (pipe, trigger, scheduled_for, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (pipe.name, trigger, _ts(scheduled_for), _ts(now))).lastrowid
    res: dict[str, Any] = {"id": run_id, "pipe": pipe.name, "trigger": trigger,
                           "status": "error", "rendered_by": None, "cost_usd": 0.0,
                           "error": None, "chars": 0}
    delivering = False
    try:
        raw = {t: TOOLS[t](db_path, now) for t in pipe.tools}
        # Private data is dropped BEFORE render: neither Claude nor the cards see it.
        data, hidden = pipes_privacy.filter_data(raw, db_path=db_path)
        missing = not_collected(data)
        if pipe.skip_if_empty and is_empty(data) and not hidden and not missing:
            res["status"] = "skipped"
            res["error"] = "empty data (skip_if_empty)"
        else:
            headline = None
            try:
                headline, cost = render_claude(pipe, data)
                res.update(rendered_by="claude", cost_usd=cost)
            except _PlainFallback as fb:
                res.update(rendered_by="plain", error=str(fb))
            # Masks and stop words — on every field BEFORE escaping (inside render);
            # link base — pipes_cards.link_base() (RECALL_PUBLIC_URL → RECALL_API_URL).
            item_ids: Optional[list[int]] = [] if buttons else None
            undo_items: Optional[list[dict]] = [] if buttons else None
            text, leaked = pipes_cards.render(
                pipes_cards.title_for(pipe.tools), data, db_path=db_path,
                today=now.date(), headline=headline, missing=missing, hidden=hidden,
                buttons=item_ids, undo_items=undo_items)
            # Second line: a stop word must not survive message assembly either.
            leaked = leaked or pipes_privacy.leaked_words(html.unescape(text))
            if missing:
                note = "not collected: " + ", ".join(t for t in pipe.tools
                                                     if isinstance(data.get(t), dict)
                                                  and data[t].get("reason"))
                res["error"] = f"{res['error']}; {note}" if res["error"] else note
            if leaked:
                # We do not name the words even in history — it is read via MCP.
                res.update(status="blocked",
                           error=f"stop word in the finished text ({len(leaked)}) — not sent")
            else:
                delivering = True
                kb = (brief_actions.keyboard(item_ids or [], undo_items)
                      if item_ids or undo_items else None)
                if kb:
                    deliver(text, reply_markup=kb)
                else:
                    deliver(text)
                res.update(status="ok", chars=len(text))
    except pipes_privacy.PrivacyError as e:
        logger.warning("Pipe %s: privacy not verified (%s) — not sending", pipe.name, e)
        what = "private.txt unreadable" if str(e).startswith("private.txt") else "directions"
        res.update(status="blocked", error=f"privacy not verified ({what})")
    except Exception as e:
        logger.warning("Pipe %s: run broke (%s)", pipe.name, e, exc_info=True)
        res.update(status="error", error=f"{type(e).__name__}: {e}"[:500])
    if (res["status"] == "error" and not delivering
            and (attempt == 1 or attempt >= max_attempts)):
        _notify_failure(pipe, res, deliver, attempt=attempt, max_attempts=max_attempts,
                        retry_minutes=retry_minutes)
    elif res["status"] == "blocked":
        _notify_blocked(pipe, res, deliver)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE pipe_runs SET finished_at=?, status=?, rendered_by=?, cost_usd=?, "
            "error=?, chars=? WHERE id=?",
            (_ts(now + (datetime.now() - t0)), res["status"], res["rendered_by"], res["cost_usd"],
             res["error"], res["chars"], run_id))
    _publish_run(res)
    logger.info("Pipe %s (%s): %s, %s, %s chars", pipe.name, trigger, res["status"],
                res["rendered_by"] or "-", res["chars"])
    return res


#: Prefix of ``res["error"]`` (a history row, English: it is read through MCP) → reason key
#: in the catalog.
_BLOCKED_REASONS = {
    "stop word": "stopWord",
    "privacy not verified (private.txt": "privateTxt",
    "privacy not verified (directions": "dirsDb",
}


def _notify_blocked(pipe: Pipe, res: dict, deliver: Callable[[str], None]) -> None:
    """The filter stopped the brief: the owner must know it will not come. Without data and without the word itself."""
    err = res["error"] or ""
    lang = server_lang()
    key = next((v for k, v in _BLOCKED_REASONS.items() if err.startswith(k)), "filter")
    reason = tr("telegram.brief.blockedReason." + key, lang=lang)
    try:
        deliver(html.escape(tr("telegram.brief.blocked", lang=lang, name=pipe.name, reason=reason),
                            quote=False))
    except Exception as e:  # noqa: BLE001 — history already has blocked
        logger.warning("Pipe %s: block notification not sent (%s)",
                       pipe.name, type(e).__name__)


def _notify_failure(pipe: Pipe, res: dict, deliver: Callable[[str], None], *,
                    attempt: int = 1, max_attempts: int = 1, retry_minutes: int = 0) -> None:
    lang = server_lang()
    reason = (res["error"] or "").split(":", 1)[0]
    if max_attempts <= 1:
        tail = ""
    elif attempt < max_attempts:
        tail = tr("telegram.brief.failRetry", lang=lang, minutes=retry_minutes,
                  attempt=attempt, max=max_attempts)
    else:
        tail = tr("telegram.brief.failExhausted", lang=lang, max=max_attempts)
    try:
        deliver(html.escape(tr("telegram.brief.failed", lang=lang, name=pipe.name,
                               reason=reason, tail=tail), quote=False))
    except Exception as e:  # noqa: BLE001 — the channel is down too; history already has the failure
        logger.warning("Pipe %s: failure notification not sent (%s)",
                       pipe.name, type(e).__name__)


# ---------------------------------------------------------- scheduler

class PipeScheduler:
    """Daemon thread: every ``tick`` seconds it runs the pipes whose moment has come.

    The first pass (at start) is a catch-up: for each pipe the last schedule
    moment ≤ now, and if it is not closed — exactly one ``catchup`` run,
    however many moments were missed. A failure does not close the moment: a retry
    (``retry``) after ``PIPES_RETRY_MINUTES``, up to ``PIPES_RETRY_MAX`` attempts.
    """

    def __init__(self, pipes: list[Pipe], *, db_path: str,
                 deliver: Callable[[str], None], tick: float = 30.0,
                 clock: Callable[[], datetime] = datetime.now,
                 retry_minutes: Optional[int] = None, retry_max: Optional[int] = None,
                 buttons: bool = False):
        self.pipes = pipes
        self.db_path = db_path
        self.deliver = deliver
        self.buttons = buttons
        self.tick = tick
        self.clock = clock
        from app.core import settings as _settings
        self.retry_minutes = (retry_minutes if retry_minutes is not None
                              else _settings.env_int("PIPES_RETRY_MINUTES"))
        self.retry_max = max(1, retry_max if retry_max is not None
                             else _settings.env_int("PIPES_RETRY_MAX"))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def step(self, trigger: str = "schedule") -> list[dict]:
        now = self.clock()
        out = []
        for pipe in self.pipes:
            try:
                due = pipe.schedule.prev(now)
                if due is None:
                    continue
                attempt = _next_attempt(self.db_path, pipe.name, due, now,
                                        retry_minutes=self.retry_minutes,
                                        retry_max=self.retry_max)
                if attempt is None:
                    continue
                res = run_pipe(pipe, db_path=self.db_path, deliver=self.deliver,
                               now=now, trigger="retry" if attempt > 1 else trigger,
                               scheduled_for=due, attempt=attempt,
                               max_attempts=self.retry_max,
                               retry_minutes=self.retry_minutes,
                               buttons=self.buttons)
                if res["status"] == "busy":
                    continue                  # not a run: the moment stays open
                out.append(res)
            except Exception as e:
                logger.warning("Pipe scheduler: %s — %s", pipe.name, e, exc_info=True)
        return out

    def _loop(self):
        self.step("catchup")
        while not self._stop.wait(self.tick):
            self.step("schedule")

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="pipes-scheduler",
                                        daemon=True)
        self._thread.start()
        logger.info("Pipes: scheduler started (%s)",
                    ", ".join(f"{p.name} «{p.schedule.expr}»" for p in self.pipes) or "no pipes")

    def stop(self):
        self._stop.set()


def _cli(argv: list[str]) -> int:
    import argparse
    from config import Config
    try:
        from dotenv import load_dotenv
        from app.core.env_file import ENV_PATH
        load_dotenv(ENV_PATH)
    except ImportError:
        pass
    ap = argparse.ArgumentParser(prog="python -m app.services.pipes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("name")
    a = ap.parse_args(argv)
    pipes = load_pipes()
    if a.cmd == "list":
        now = datetime.now()
        for p in pipes:
            print(f"{p.name}: «{p.schedule.expr}» → next {p.schedule.next(now)}; "
                  f"tools={p.tools}; budget=${p.budget_usd}")
        return 0
    pipe = next((p for p in pipes if p.name == a.name), None)
    if pipe is None:
        print(f"no pipe «{a.name}»")
        return 1
    res = run_pipe(pipe, db_path=Config.DATABASE, deliver=print, trigger="manual")
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
