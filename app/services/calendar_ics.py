"""Read-only calendar sync from secret ICS links (K6).

Each source is a secret ICS address (Google or Outlook "publish calendar"). The address is a
bearer secret: it is typed only on the one-time ``/connect/calendar/<token>`` page, stored in
the OS keyring (service ``KEYRING_SERVICE``, username = normalised label) and never logged,
never rendered back, never accepted from MCP. The DB keeps the label, the host, the last sync
and its counters.

A sync fetches the feed, expands recurrences for a window with ``recurring-ical-events``
(EXDATE drops an occurrence, a RECURRENCE-ID override replaces it: read in the installed
``series/rrule.py``), and upserts one ``calendar_events`` row per occurrence, keyed by
(source, UID, original occurrence start) so a moved occurrence updates its row. The library
does not filter ``STATUS:CANCELLED``; that is done here, and an occurrence that vanished from
the window is marked cancelled, never deleted, so a recording bound to it keeps its link.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import closing
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

import keyring
import keyring.errors

from app.core import settings

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "Recall Calendar"
ALLOWED_HOSTS = frozenset({
    "calendar.google.com",
    "outlook.office365.com",
    "outlook.office.com",
    "outlook.live.com",
})
FETCH_TIMEOUT_SEC = 30
MAX_FEED_BYTES = 20 * 1024 * 1024
LABEL_MAX = 60
DEFAULT_WINDOW_DAYS = 30

# CalendarError codes
BAD_URL = "CALENDAR_BAD_URL"
HOST_NOT_ALLOWED = "CALENDAR_HOST_NOT_ALLOWED"
FETCH_FAILED = "CALENDAR_FETCH_FAILED"
PARSE_FAILED = "CALENDAR_PARSE_FAILED"


class CalendarError(Exception):
    """A feed could not be used; ``code`` is safe to log, the message never holds the URL."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def normalize_label(label: str) -> str:
    return " ".join((label or "").split())[:LABEL_MAX]


def _keyring_user(label: str) -> str:
    # The exact label, as UNIQUE in calendar_sources is: «Google» and «google» are two sources
    # and must not share one keyring entry (a disconnect would delete the other's address).
    return normalize_label(label)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# URL and fetch
# ============================================================

def validate_url(url: str) -> str:
    """Return the host of an acceptable feed address; raise CalendarError otherwise."""
    raw = (url or "").strip()
    if raw.lower().startswith("webcal://"):
        raw = "https://" + raw[len("webcal://"):]
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise CalendarError(BAD_URL) from None
    if parts.scheme != "https" or not parts.hostname:
        raise CalendarError(BAD_URL)
    host = parts.hostname.lower()
    if host not in ALLOWED_HOSTS:
        raise CalendarError(HOST_NOT_ALLOWED)
    return host


def _https_url(url: str) -> str:
    raw = url.strip()
    if raw.lower().startswith("webcal://"):
        return "https://" + raw[len("webcal://"):]
    return raw


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another allowed https host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_feed(url: str) -> bytes:
    """Download a feed. Errors carry only a code: an HTTPError's text would hold the URL."""
    validate_url(url)
    opener = urllib.request.build_opener(_AllowlistRedirect)
    req = urllib.request.Request(_https_url(url), headers={"User-Agent": "Recall-calendar/1"})
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            data = resp.read(MAX_FEED_BYTES + 1)
    except CalendarError:
        raise
    except urllib.error.HTTPError as exc:
        logger.warning("Calendar feed fetch failed: HTTP %s", exc.code)
        raise CalendarError(FETCH_FAILED) from None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Calendar feed fetch failed: %s", type(exc).__name__)
        raise CalendarError(FETCH_FAILED) from None
    if len(data) > MAX_FEED_BYTES:
        logger.warning("Calendar feed is larger than %s bytes", MAX_FEED_BYTES)
        raise CalendarError(FETCH_FAILED)
    return data


# ============================================================
# Parse and expand
# ============================================================

def _as_datetime(value) -> tuple[datetime, bool]:
    """(aware datetime, all_day). A date is midnight UTC; a floating time is local time."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.astimezone()
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, dtime(0, 0), tzinfo=timezone.utc), True
    raise TypeError("not a date")


def _addresses(prop) -> list:
    if prop is None:
        return []
    return list(prop) if isinstance(prop, list) else [prop]


def _person(addr) -> dict:
    email = str(addr or "")
    if email.lower().startswith("mailto:"):
        email = email[len("mailto:"):]
    params = getattr(addr, "params", {}) or {}
    name = str(params.get("CN") or "").strip().strip('"')
    return {"name": name, "email": email.strip()}


def parse_occurrences(data: bytes, start: datetime, end: datetime) -> list[dict]:
    """Every VEVENT occurrence that overlaps [start, end), cancelled ones included."""
    import icalendar
    import recurring_ical_events

    try:
        cal = icalendar.Calendar.from_ical(data)
        components = list(recurring_ical_events.of(cal, skip_bad_series=True).between(start, end))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Calendar feed parse failed: %s", type(exc).__name__)
        raise CalendarError(PARSE_FAILED) from None

    out = []
    for ev in components:
        try:
            s, all_day = _as_datetime(ev.get("DTSTART").dt)
            if ev.get("DTEND") is not None:
                e, _ = _as_datetime(ev.get("DTEND").dt)
            elif ev.get("DURATION") is not None:
                e = s + ev.get("DURATION").dt
            else:
                e = s + (timedelta(days=1) if all_day else timedelta(0))
            rid = ev.get("RECURRENCE-ID")
            key_dt = _as_datetime(rid.dt)[0] if rid is not None else s
        except (AttributeError, TypeError, ValueError) as exc:
            logger.debug("Skipping a calendar component without usable times: %s", type(exc).__name__)
            continue
        uid = str(ev.get("UID") or "").strip()
        if not uid:
            continue
        status = str(ev.get("STATUS") or "").strip().upper()
        out.append({
            "uid": uid,
            "occurrence_key": _utc_iso(key_dt),
            "start": _utc_iso(s),
            "end": _utc_iso(e),
            "all_day": all_day,
            "title": str(ev.get("SUMMARY") or "").strip(),
            "organizer": _person(ev.get("ORGANIZER"))["email"] if ev.get("ORGANIZER") else "",
            "attendees": [_person(a) for a in _addresses(ev.get("ATTENDEE"))],
            "status": "cancelled" if status == "CANCELLED" else
                      ("tentative" if status == "TENTATIVE" else "confirmed"),
        })
    return out


# ============================================================
# Sources
# ============================================================

def _source_row(row, check_keyring: bool = True) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    if check_keyring:
        d["configured"] = keyring.get_password(KEYRING_SERVICE, _keyring_user(d["label"])) is not None
    return d


def list_sources(db_path: str, *, check_keyring: bool = True) -> list[dict]:
    """``check_keyring=False`` for a caller that must not touch the secret (the MCP process)."""
    with _db(db_path) as conn:
        rows = conn.execute("SELECT * FROM calendar_sources ORDER BY id").fetchall()
        counts = dict(conn.execute(
            "SELECT source_id, COUNT(*) FROM calendar_events WHERE status != 'cancelled' "
            "GROUP BY source_id").fetchall())
    out = []
    for r in rows:
        d = _source_row(r, check_keyring)
        d["events"] = counts.get(d["id"], 0)
        out.append(d)
    return out


def get_source(db_path: str, source_id: int) -> Optional[dict]:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM calendar_sources WHERE id = ?", (source_id,)).fetchone()
    return _source_row(row) if row else None


def has_source(db_path: str, label: str) -> bool:
    with _db(db_path) as conn:
        return conn.execute("SELECT 1 FROM calendar_sources WHERE label = ?",
                            (normalize_label(label),)).fetchone() is not None


def connect_source(db_path: str, label: str, url: str, *,
                   fetch: Optional[Callable[[str], bytes]] = None) -> dict:
    """Trial fetch and parse first, keyring and DB second, then the first sync.

    An existing label gets the new address. Raises ValueError for an empty label and
    CalendarError for an unusable address or feed (nothing is stored then)."""
    name = normalize_label(label)
    if not name:
        raise ValueError("empty label")
    host = validate_url(url)
    now = _now()
    data = (fetch or fetch_feed)(url)
    parse_occurrences(data, now - timedelta(days=DEFAULT_WINDOW_DAYS),
                      now + timedelta(days=DEFAULT_WINDOW_DAYS))

    user = _keyring_user(name)
    previous = keyring.get_password(KEYRING_SERVICE, user)
    keyring.set_password(KEYRING_SERVICE, user, url.strip())
    try:
        with _db(db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO calendar_sources (label, host, created_at) "
                         "VALUES (?, ?, ?)", (name, host, _utc_iso(now)))
            conn.execute("UPDATE calendar_sources SET host = ?, enabled = 1, last_error = NULL "
                         "WHERE label = ?", (host, name))
            source_id = conn.execute("SELECT id FROM calendar_sources WHERE label = ?",
                                     (name,)).fetchone()["id"]
            conn.commit()
    except Exception:
        # Put the keyring back to what it held before this call.
        try:
            if previous is None:
                keyring.delete_password(KEYRING_SERVICE, user)
            else:
                keyring.set_password(KEYRING_SERVICE, user, previous)
        except keyring.errors.PasswordDeleteError:
            logger.debug("keyring entry already gone during connect_source rollback")
        raise
    result = sync_source(db_path, source_id, fetch=lambda _u: data, now=now)
    return {"source_id": source_id, "label": name, **result}


def disconnect_source(db_path: str, source_id: int) -> bool:
    """Remove the source, its events and its keyring entry; recordings drop their binding."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT label FROM calendar_sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return False
        ids = "SELECT id FROM calendar_events WHERE source_id = ?"
        for table in ("audio_downloads", "transcriptions"):
            conn.execute(f"UPDATE {table} SET calendar_event_id = NULL "
                         f"WHERE calendar_event_id IN ({ids})", (source_id,))
        conn.execute("DELETE FROM calendar_events WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM calendar_sources WHERE id = ?", (source_id,))
        conn.commit()
    try:
        keyring.delete_password(KEYRING_SERVICE, _keyring_user(row["label"]))
    except keyring.errors.PasswordDeleteError:
        logger.debug("no keyring entry to delete for calendar source id=%s", source_id)
    return True


# ============================================================
# Sync
# ============================================================

def sync_source(db_path: str, source_id: int, *, window_days: Optional[int] = DEFAULT_WINDOW_DAYS,
                fetch: Optional[Callable[[str], bytes]] = None, now: Optional[datetime] = None) -> dict:
    """Pull one source into ``calendar_events``. ``window_days=None`` takes the whole feed.

    A failure is recorded in ``last_error`` (a code) and re-raised as CalendarError."""
    now = now or _now()
    source = get_source(db_path, source_id)
    if source is None:
        raise KeyError(source_id)
    url = keyring.get_password(KEYRING_SERVICE, _keyring_user(source["label"]))
    if window_days is None:
        start, end = datetime(1970, 1, 1, tzinfo=timezone.utc), now + timedelta(days=366)
    else:
        start, end = now - timedelta(days=window_days), now + timedelta(days=window_days)
    try:
        if not url:
            raise CalendarError(FETCH_FAILED)
        occurrences = parse_occurrences((fetch or fetch_feed)(url), start, end)
    except CalendarError as exc:
        with _db(db_path) as conn:
            conn.execute("UPDATE calendar_sources SET last_error = ?, last_sync_at = ? WHERE id = ?",
                         (exc.code, _utc_iso(now), source_id))
            conn.commit()
        raise

    stamp = _utc_iso(now)
    seen = set()
    with _db(db_path) as conn:
        for o in occurrences:
            seen.add((o["uid"], o["occurrence_key"]))
            conn.execute(
                "INSERT INTO calendar_events (source_id, uid, occurrence_key, start_utc, end_utc, "
                "all_day, title, organizer, attendees, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_id, uid, occurrence_key) DO UPDATE SET "
                "start_utc = excluded.start_utc, end_utc = excluded.end_utc, "
                "all_day = excluded.all_day, title = excluded.title, organizer = excluded.organizer, "
                "attendees = excluded.attendees, status = excluded.status, "
                "updated_at = excluded.updated_at",
                (source_id, o["uid"], o["occurrence_key"], o["start"], o["end"], int(o["all_day"]),
                 o["title"], o["organizer"], json.dumps(o["attendees"], ensure_ascii=False),
                 o["status"], stamp))
        vanished = 0
        for row in conn.execute(
                "SELECT id, uid, occurrence_key FROM calendar_events WHERE source_id = ? "
                "AND status != 'cancelled' AND start_utc < ? AND end_utc > ?",
                (source_id, _utc_iso(end), _utc_iso(start))).fetchall():
            if (row["uid"], row["occurrence_key"]) not in seen:
                conn.execute("UPDATE calendar_events SET status = 'cancelled', updated_at = ? "
                             "WHERE id = ?", (stamp, row["id"]))
                vanished += 1
        live = [o for o in occurrences if o["status"] != "cancelled"]
        with_attendees = sum(1 for o in live if o["attendees"])
        conn.execute("UPDATE calendar_sources SET last_sync_at = ?, last_error = NULL, "
                     "events_seen = ?, events_with_attendees = ? WHERE id = ?",
                     (stamp, len(live), with_attendees, source_id))
        conn.commit()
    return {"events": len(live), "with_attendees": with_attendees,
            "cancelled": len(occurrences) - len(live) + vanished}


def sync_all(db_path: str, **kw) -> dict:
    """Sync every enabled source; one failing source does not stop the others."""
    out = {}
    for s in list_sources(db_path):
        if not s["enabled"]:
            continue
        try:
            out[s["id"]] = sync_source(db_path, s["id"], **kw)
        except CalendarError as exc:
            out[s["id"]] = {"error": exc.code}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[calendar] sync of source %s failed: %s", s["id"], type(exc).__name__)
            out[s["id"]] = {"error": "INTERNAL"}
    return out


def _event_row(row) -> dict:
    d = dict(row)
    d["all_day"] = bool(d["all_day"])
    d["attendees"] = json.loads(d["attendees"] or "[]")
    return d


def events_between(db_path: str, start_utc: str, end_utc: str, *,
                   include_cancelled: bool = False) -> list[dict]:
    """Events overlapping [start_utc, end_utc) (ISO-8601 UTC), oldest first."""
    sql = ("SELECT e.id, e.source_id, s.label AS source_label, e.uid, e.start_utc AS start, "
           "e.end_utc AS end, e.all_day, e.title, e.organizer, e.attendees, e.status "
           "FROM calendar_events e JOIN calendar_sources s ON s.id = e.source_id "
           "WHERE e.start_utc < ? AND e.end_utc > ?")
    if not include_cancelled:
        sql += " AND e.status != 'cancelled'"
    with _db(db_path) as conn:
        rows = conn.execute(sql + " ORDER BY e.start_utc, e.id", (end_utc, start_utc)).fetchall()
    return [_event_row(r) for r in rows]


# ============================================================
# Poller
# ============================================================

class CalendarPoller:
    """Daemon thread: syncs every CALENDAR_SYNC_MINUTES, wakes on ``trigger()``."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._requests: list = []
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="CalendarPoller", daemon=True)
        self._thread.start()
        # The start line is how a release proves the poller runs (docs/defects/shipped-but-not-restarted.md).
        logger.info("Calendar poller started (every %s min)",
                    max(1, settings.env_int("CALENDAR_SYNC_MINUTES")))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def trigger(self, source_id: Optional[int] = None) -> None:
        with self._lock:
            self._requests.append(source_id)
        self._wake.set()

    def run_once(self) -> None:
        """One loop iteration: queued requests, otherwise the periodic sync."""
        with self._lock:
            reqs, self._requests = self._requests, []
        try:
            if reqs:
                for source_id in dict.fromkeys(reqs):
                    if source_id is None:
                        sync_all(self.db_path)
                    else:
                        try:
                            sync_source(self.db_path, source_id)
                        except (CalendarError, KeyError):
                            logger.debug("calendar sync of source %s refused", source_id)
            elif settings.env_bool("CALENDAR_SYNC_ENABLED"):
                sync_all(self.db_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[calendar] sync iteration failed: %s", type(exc).__name__)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            interval = max(1, settings.env_int("CALENDAR_SYNC_MINUTES")) * 60
            self._wake.wait(interval)
            self._wake.clear()


# ============================================================
# CLI
# ============================================================

# Only events a recording can bind to: an all-day series (a birthday since 1970) would otherwise
# set the start of the calendar's history decades back.
EARLIEST_SQL = ("SELECT MIN(start_utc) FROM calendar_events "
                "WHERE status != 'cancelled' AND all_day = 0")


def earliest_event(db_path: str, source_id: Optional[int] = None) -> Optional[str]:
    """Start of the earliest stored bindable event (ISO UTC), of one source or all, or None."""
    sql, args = EARLIEST_SQL, ()
    if source_id is not None:
        sql, args = sql + " AND source_id = ?", (source_id,)
    with _db(db_path) as conn:
        row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def main(argv=None) -> int:
    """``sync [--full] [--source ID]``: one sync now. ``--full`` takes the whole feed, once,
    before ``calendar_bind backfill`` (the periodic sync keeps only the +-30-day window)."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m app.services.calendar_ics")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="sync the connected calendars now")
    s.add_argument("--full", action="store_true", help="the whole feed, not the +-30-day window")
    s.add_argument("--source", type=int, default=None, help="one source id")
    from config import Config
    s.add_argument("--db", default=str(Config.BASE_DIR / Config.DATABASE))
    args = ap.parse_args(argv)
    window = {"window_days": None} if args.full else {}
    if args.source is None:
        out = sync_all(args.db, **window)
    else:
        try:
            out = {args.source: sync_source(args.db, args.source, **window)}
        except CalendarError as exc:
            out = {args.source: {"error": exc.code}}
        except KeyError:
            out = {args.source: {"error": "NOT_FOUND"}}
    report = {"full": args.full, "sources": out,
              "calendar_earliest": earliest_event(args.db, args.source)}
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if all("error" not in v for v in out.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
