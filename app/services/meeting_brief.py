"""Pre-meeting brief (wave4-client-calls): the people on an invite, their open tasks, the last calls.

``brief_for_event`` / ``brief_for_person`` read the archive: an attendee is matched to an EXISTING
graph person the way ``calendar_bind.link_attendees`` does it (``enrichment._find_entity`` on the
``enrichment._normalize``-d name, aliases included); an unmatched attendee stays by name with empty
lists. Nothing is created and no attendee email is kept.

The order is the pipes delivery chain (``docs/wiki/scheduled-pipes-delivery.md``): data ->
``filter_brief`` (``pipes_privacy.filter_data``: private directions and stop words, fail closed) ->
``render_card`` (``pipes_cards._Guard`` on every field, links on ``pipes_cards.link_base()``) ->
stop-word re-check -> ``deliver`` (the owner chat only).

``MeetingBriefTicker`` sends one card per event occurrence ``(event_id, start_utc)`` once its start
falls within ``MEETING_BRIEF_LEAD_MINUTES``; the outcome lands in ``meeting_brief_sent``. A failed
send is retried every tick until the event starts and is never marked sent.
"""
from __future__ import annotations

import html
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

from app.core import settings
from app.core.i18n import server_lang, tr, tr_n
from app.db.connection import get_db_connection
from app.services import calendar_ics, enrichment, pipes_cards, pipes_privacy, privacy
from app.services.commitments import _ITEM_SELECT
from app.services.privacy import PrivacyError

logger = logging.getLogger(__name__)

MAX_TASKS = 5
MAX_CALLS = 5
OPEN_STATUSES = ("open", "stale")
#: Transcriptions that are calls (a live recording, an uploaded file, an Audio Library row);
#: a Telegram message or a mail also links people, but it is not a call.
CALL_SOURCES = ("recording", "file", "library")
LEAD_MIN, LEAD_MAX = 1, 180

_TAG = re.compile(r"<[^>]+>")


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- data

def _match(conn, name: str) -> Optional[int]:
    key = enrichment._normalize(name or "")
    return enrichment._find_entity(conn, "person", key) if key else None


def _open_tasks(conn, entity_id: int) -> tuple[list[dict], int]:
    """Open tasks of a person (``open|stale``, not a duplicate, record not deleted): first 5 + the rest."""
    marks = ",".join("?" * len(OPEN_STATUSES))
    sql = ("SELECT q.*, t2.category_id AS category_id FROM ("
           + _ITEM_SELECT
           + f"WHERE ai.owner_entity_id = ? AND ai.status IN ({marks}) AND ai.dup_of IS NULL "
             "AND (ai.transcription_id IS NULL OR t.deleted_at IS NULL)"
           + ") q LEFT JOIN transcriptions t2 ON t2.id = q.transcription_id "
             "ORDER BY q.due_date IS NULL, q.due_date, q.meeting_date DESC, q.id DESC")
    rows = conn.execute(sql, (entity_id, *OPEN_STATUSES)).fetchall()
    tasks = [{"id": r["id"], "task": r["task"], "due_date": r["due_date"], "due": r["due_raw"],
              "meeting_date": r["meeting_date"], "transcription_id": r["transcription_id"],
              "category_id": r["category_id"], "source": r["source"]}
             for r in rows[:MAX_TASKS]]
    return tasks, max(0, len(rows) - MAX_TASKS)


def _last_calls(conn, entity_id: int) -> list[dict]:
    marks = ",".join("?" * len(CALL_SOURCES))
    rows = conn.execute(
        "SELECT t.id AS transcription_id, COALESCE(t.title, t.youtube_title, t.source_name) AS title, "
        "COALESCE(t.meeting_date, substr(t.created_at, 1, 10)) AS meeting_date, t.category_id "
        "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
        f"WHERE me.entity_id = ? AND t.deleted_at IS NULL AND t.source_type IN ({marks}) "
        "ORDER BY meeting_date DESC, t.id DESC LIMIT ?",
        (entity_id, *CALL_SOURCES, MAX_CALLS)).fetchall()
    return [dict(r) for r in rows]


def _person(conn, name: str, entity_id: Optional[int]) -> dict:
    entry = {"name": name, "entity_id": entity_id, "matched": entity_id is not None,
             "open_tasks": [], "open_more": 0, "last_calls": []}
    if entity_id is None:
        return entry
    row = conn.execute("SELECT canonical_name FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is not None and row[0]:
        entry["name"] = row[0]
    entry["open_tasks"], entry["open_more"] = _open_tasks(conn, entity_id)
    entry["last_calls"] = _last_calls(conn, entity_id)
    return entry


def _attendee_names(event: dict) -> list[str]:
    """Names of the invite only: an email-only attendee yields "" (the address is never kept)."""
    out = []
    for a in event.get("attendees") or []:
        name = a.get("name") if isinstance(a, dict) else a
        out.append(str(name or "").strip())
    return out


def brief_for_event(conn, event: dict) -> dict:
    """The brief of one calendar event (a row of ``calendar_ics.events_between``).
    ``conn`` must use ``sqlite3.Row`` (``get_db_connection`` does)."""
    names = _attendee_names(event)
    people, seen = [], set()
    for name in names:
        eid = _match(conn, name)
        if eid is not None:
            if eid in seen:
                continue
            seen.add(eid)
        people.append(_person(conn, name, eid))
    return {"event": {"id": event.get("id"), "title": event.get("title") or "",
                      "start": event.get("start"), "end": event.get("end"),
                      "attendees": [n for n in names if n]},
            "people": people, "hidden": 0, "generated_at": _iso_now()}


def brief_for_person(conn, name: str) -> dict:
    """The brief of one person by name (no calendar needed); unmatched -> ``matched: false``."""
    name = (name or "").strip()
    return {"event": None, "people": [_person(conn, name, _match(conn, name))],
            "hidden": 0, "generated_at": _iso_now()}


def get_event(db_path: str, event_id: int) -> Optional[dict]:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, start_utc AS start, end_utc AS end, all_day, title, attendees, status "
            "FROM calendar_events WHERE id = ?", (int(event_id),)).fetchone()
    return calendar_ics._event_row(row) if row is not None else None


# ---------------------------------------------------------------- privacy

def filter_brief(brief: dict, *, db_path: str) -> dict:
    """Drop private-direction and stop-word tasks and calls, count them in ``hidden``.
    An unreadable ``private.txt`` raises ``PrivacyError`` (nothing is given out)."""
    data = {}
    for i, p in enumerate(brief.get("people") or []):
        data[f"p{i}"] = {"tasks": list(p.get("open_tasks") or []),
                         "calls": list(p.get("last_calls") or [])}
    kept, hidden = pipes_privacy.filter_data(data, db_path=db_path)
    people = []
    for i, p in enumerate(brief.get("people") or []):
        q = dict(p)
        q["open_tasks"] = kept[f"p{i}"]["tasks"]
        q["last_calls"] = kept[f"p{i}"]["calls"]
        people.append(q)
    out = dict(brief)
    out["people"] = people
    out["hidden"] = int(brief.get("hidden") or 0) + hidden
    return out


# ------------------------------------------------------------------- card

def _parse_utc(iso: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _when(iso: Any) -> str:
    dt = _parse_utc(iso)
    return dt.astimezone().strftime("%d.%m %H:%M") if dt else ""


def render_card(brief: dict, *, db_path: Optional[str] = None, base_url: Optional[str] = None,
                lang: Optional[str] = None) -> tuple[str, list[str]]:
    """-> (Telegram HTML with ``b|i|a`` tags only, stop words met in fields). A non-empty list = do
    not send. ``PrivacyError`` from the stop list propagates. ``db_path`` is accepted for the
    shared render signature; the brief already carries everything the card shows."""
    lang = lang or server_lang()
    g = pipes_cards._Guard(privacy.stop_words())
    base = (base_url or pipes_cards.link_base()).rstrip("/")
    LEN, META = pipes_cards.TASK_LEN, pipes_cards.META_LEN
    paras = []
    ev = brief.get("event")
    if ev:
        paras.append(f"📅 <b>{g(ev.get('title') or tr('telegram.brief.noText', lang=lang), LEN)}</b>"
                     f" · {g(_when(ev.get('start')))}")
    for p in brief.get("people") or []:
        name = p.get("name") or ""
        if not p.get("matched"):
            if name:
                paras.append(f"👤 {g(name, META)}")
            continue
        label = g(name, META)
        head = (pipes_cards._href(f"{base}/entities/{int(p['entity_id'])}", label)
                if p.get("entity_id") is not None else label)
        lines = [f"👤 <b>{head}</b>"]
        for t in p.get("open_tasks") or []:
            text = g(t.get("task"), LEN) or tr("telegram.brief.noText", lang=lang)
            tid, iid = t.get("transcription_id"), t.get("id")
            item = (pipes_cards._href(f"{base}/transcript/{int(tid)}?task={int(iid)}", text)
                    if tid and iid else text)
            due = (pipes_cards._dm(t.get("due_date")) if t.get("due_date")
                   else (t.get("due") or ""))
            lines.append(f"📌 {item}" + (f" · {g(due, META)}" if due else ""))
        more = int(p.get("open_more") or 0)
        if more:
            owner = quote(pipes_privacy.mask(name), safe="")
            lines.append(pipes_cards._href(f"{base}/tasks?owner={owner}",
                                           tr("telegram.brief.more", lang=lang, n=more)))
        for c in p.get("last_calls") or []:
            title = g(c.get("title"), META) or tr("telegram.brief.recordWord", lang=lang)
            tid = c.get("transcription_id")
            item = pipes_cards._href(f"{base}/transcript/{int(tid)}", title) if tid else title
            lines.append(f"🎙 {item} · {g(pipes_cards._dm(c.get('meeting_date')))}")
        paras.append("\n".join(lines))
    hidden = int(brief.get("hidden") or 0)
    if hidden:
        paras.append(g(tr_n("telegram.brief.hidden", hidden, lang=lang)))
    return "\n\n".join(paras), sorted(g.leaked)


def _leaked_after_render(text: str) -> list[str]:
    """Stop words on the unescaped visible text of a rendered card (the wiki's re-check)."""
    return pipes_privacy.leaked_words(html.unescape(_TAG.sub(" ", text)))


# ----------------------------------------------------------------- ticker

def _lead_minutes(value: Optional[int]) -> int:
    lead = value if value is not None else settings.env_int("MEETING_BRIEF_LEAD_MINUTES")
    return max(LEAD_MIN, min(LEAD_MAX, int(lead)))


def _record(db_path: str, event_id: int, start_utc: str, status: str, *,
            error: Optional[str] = None, sent_at: Optional[str] = None) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO meeting_brief_sent (event_id, start_utc, status, attempts, last_error, sent_at) "
            "VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(event_id, start_utc) DO UPDATE SET "
            "status = excluded.status, attempts = attempts + 1, last_error = excluded.last_error, "
            "sent_at = excluded.sent_at",
            (event_id, start_utc, status, error, sent_at))
        conn.commit()


def _closed(db_path: str, event_id: int, start_utc: str) -> bool:
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT status FROM meeting_brief_sent WHERE event_id = ? AND start_utc = ?",
                           (event_id, start_utc)).fetchone()
    return row is not None and row["status"] in ("sent", "skipped")


class MeetingBriefTicker:
    """Daemon thread: every ``tick_sec`` sends the cards of events starting within the lead time."""

    def __init__(self, db_path: str, deliver: Callable[[str], Any], *, tick_sec: float = 60,
                 lead_minutes: Optional[int] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.db_path = db_path
        self.deliver = deliver
        self.tick_sec = tick_sec
        self.lead_minutes = lead_minutes
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="MeetingBriefTicker", daemon=True)
        self._thread.start()
        # The start line is how a release proves the ticker runs.
        logger.info("Meeting brief ticker started (lead %s min)", _lead_minutes(self.lead_minutes))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_once(self, now: Optional[datetime] = None) -> dict:
        """One tick -> ``{sent, failed, skipped}`` counted over this tick's events."""
        out = {"sent": 0, "failed": 0, "skipped": 0}
        if not settings.env_bool("MEETING_BRIEF_ENABLED"):
            return out
        now = (now or self.now_fn()).astimezone(timezone.utc)
        horizon = now + timedelta(minutes=_lead_minutes(self.lead_minutes))
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now_iso, horizon_iso = now.strftime(fmt), horizon.strftime(fmt)
        for ev in calendar_ics.events_between(self.db_path, now_iso, horizon_iso):
            start = str(ev.get("start") or "")
            # Started events are never tried again; all-day and cancelled ones get no card.
            if ev.get("all_day") or ev.get("status") == "cancelled" or not (now_iso <= start <= horizon_iso):
                continue
            if _closed(self.db_path, ev["id"], start):
                continue
            status = self._one(ev, start)
            out[status] += 1
        return out

    def _one(self, ev: dict, start: str) -> str:
        eid = ev["id"]
        try:
            if pipes_privacy.leaked_words(str(ev.get("title") or "")):
                _record(self.db_path, eid, start, "skipped", error="private")
                return "skipped"
            if not any(_attendee_names(ev)):
                _record(self.db_path, eid, start, "skipped", error="no_people")
                return "skipped"
            with get_db_connection(self.db_path) as conn:
                brief = brief_for_event(conn, ev)
            brief = filter_brief(brief, db_path=self.db_path)
            text, leaked = render_card(brief, db_path=self.db_path)
            if leaked or _leaked_after_render(text):
                # The word itself is never written down.
                _record(self.db_path, eid, start, "skipped", error="private")
                return "skipped"
        except PrivacyError:
            logger.warning("Meeting brief %s: privacy check failed, nothing sent", eid)
            _record(self.db_path, eid, start, "failed", error="PrivacyError")
            return "failed"
        except Exception as exc:  # noqa: BLE001 - a broken brief is a failed attempt, retried next tick
            logger.warning("Meeting brief %s: building failed: %s", eid, type(exc).__name__)
            _record(self.db_path, eid, start, "failed", error=type(exc).__name__)
            return "failed"
        try:
            self.deliver(text)
        except Exception as exc:  # noqa: BLE001 - retried every tick until the event starts
            logger.warning("Meeting brief %s: send failed: %s", eid, type(exc).__name__)
            _record(self.db_path, eid, start, "failed", error=f"{type(exc).__name__}: {exc}"[:300])
            return "failed"
        _record(self.db_path, eid, start, "sent", sent_at=_iso_now())
        logger.info("Meeting brief sent for event %s", eid)
        return "sent"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the thread
                logger.warning("Meeting brief tick failed: %s", type(exc).__name__)
            self._stop.wait(self.tick_sec)
