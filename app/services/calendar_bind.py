"""Bind recordings to calendar events (K6, story 02).

A recording [start, end] is bound to the event with the largest overlap against the event's
window widened by ``TOLERANCE`` on both sides. Cancelled and all-day events never bind, and
the overlap must cover at least ``MIN_SHARE`` of the recording, so a short hallway call next
to a long meeting stays unbound.

Title order (owner's decision): the owner's name, then the event title, then Claude's
``suggest-meta``. The event title is applied once, where the library row is written
(``recording/library.register_recording``), so the server autosave and the browser tab read
the same title; the autosave skips ``suggest-meta`` for a bound row.

Attendees are linked only to people who already exist (normalised name, alias or a merged-away
spelling: ``enrichment._find_entity``). No entity is ever created and an email is never
stored as an alias, so the calendar cannot add a duplicate person. Links carry
``meeting_entities.source = 'calendar'`` and never replace a link another source made.

CLI: ``python -m app.services.calendar_bind backfill [--dry-run]`` binds past recordings and
links their attendees without touching any title; ``--dry-run`` is the same write path
rolled back.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.services import enrichment

logger = logging.getLogger(__name__)

SOURCE = "calendar"
TOLERANCE = timedelta(minutes=10)
MIN_SHARE = 1 / 3


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def best_event(conn, start: datetime, end: datetime) -> Optional[sqlite3.Row]:
    """The event with the largest overlap, or None if no event covers MIN_SHARE of [start, end]."""
    length = (end - start).total_seconds()
    if length <= 0:
        return None
    rows = conn.execute(
        "SELECT * FROM calendar_events WHERE status != 'cancelled' AND all_day = 0 "
        "AND start_utc < ? AND end_utc > ?",
        (_iso(end + TOLERANCE), _iso(start - TOLERANCE))).fetchall()
    best, best_overlap = None, 0.0
    for r in rows:
        lo = max(start, _parse(r["start_utc"]) - TOLERANCE)
        hi = min(end, _parse(r["end_utc"]) + TOLERANCE)
        overlap = (hi - lo).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = r, overlap
    if best is None or best_overlap < MIN_SHARE * length:
        return None
    return best


def recording_window(started_at, duration_sec) -> Optional[tuple[datetime, datetime]]:
    try:
        start = _parse(started_at)
        dur = float(duration_sec or 0)
    except (TypeError, ValueError):
        return None
    if dur <= 0:
        return None
    return start, start + timedelta(seconds=dur)


def bind_download(conn, download_id: int, start: datetime, end: datetime) -> Optional[sqlite3.Row]:
    """Set ``audio_downloads.calendar_event_id`` from the best event; returns the event or None.

    An existing binding is kept (the first decision wins; a disconnect clears it)."""
    row = conn.execute("SELECT calendar_event_id FROM audio_downloads WHERE id = ?",
                       (download_id,)).fetchone()
    if row is None:
        return None
    if row["calendar_event_id"] is not None:
        return conn.execute("SELECT * FROM calendar_events WHERE id = ?",
                            (row["calendar_event_id"],)).fetchone()
    ev = best_event(conn, start, end)
    if ev is not None:
        conn.execute("UPDATE audio_downloads SET calendar_event_id = ? WHERE id = ?",
                     (ev["id"], download_id))
    return ev


def link_attendees(conn, transcription_id: int, event_id: int) -> dict:
    """Link the event's attendees to existing people; never creates one."""
    ev = conn.execute("SELECT attendees, organizer FROM calendar_events WHERE id = ?",
                      (event_id,)).fetchone()
    if ev is None:
        return {"linked": 0, "unmatched": 0}
    people = json.loads(ev["attendees"] or "[]")
    linked, unmatched = 0, 0
    for p in people:
        name = (p.get("name") or "").strip()
        entity_id = enrichment._find_entity(conn, "person", enrichment._normalize(name)) if name else None
        if entity_id is None:
            unmatched += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO meeting_entities (transcription_id, entity_id, mention_count, source) "
            "VALUES (?, ?, 1, ?)", (transcription_id, entity_id, SOURCE))
        linked += cur.rowcount
    return {"linked": linked, "unmatched": unmatched}


def apply_to_transcription(conn, transcription_id: int, download_id: Optional[int]) -> Optional[int]:
    """Copy the library row's binding onto a new transcript and link attendees."""
    if download_id is None:
        return None
    row = conn.execute("SELECT calendar_event_id FROM audio_downloads WHERE id = ?",
                       (download_id,)).fetchone()
    event_id = row["calendar_event_id"] if row is not None else None
    if event_id is None:
        return None
    conn.execute("UPDATE transcriptions SET calendar_event_id = ? WHERE id = ?",
                 (event_id, transcription_id))
    link_attendees(conn, transcription_id, event_id)
    return event_id


# ============================================================
# Backfill
# ============================================================

def _person_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'person'").fetchone()[0]


def _manifest_start(sessions_dir: Optional[str], sid: Optional[str]):
    """``started_at`` from the session's manifest, or None (no folder, no file, unreadable)."""
    if not sessions_dir or not sid:
        return None
    try:
        with open(Path(sessions_dir) / sid / "manifest.json", encoding="utf-8") as fh:
            return _parse(json.load(fh)["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def backfill(db_path: str, *, commit: bool = True, sessions_dir: Optional[str] = None) -> dict:
    """Bind every recording of the library that has no binding yet; titles are never touched.

    The start of a past recording is the session manifest's ``started_at`` when the manifest is
    still on disk; otherwise ``created_at - duration``, which drifts by however long the owner
    took to save (measured 26.10.2026: 38 of 289 saved more than 10 min after the stop)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with closing(conn):
        persons_before = _person_count(conn)
        per_source: dict = {}
        stats = {"recordings": 0, "bound": 0, "already_bound": 0, "unbound": 0,
                 "attendees_linked": 0, "attendees_unmatched": 0,
                 "start_from_manifest": 0, "start_from_saved_at": 0, "before_calendar": 0}
        from app.services.calendar_ics import EARLIEST_SQL
        earliest_row = conn.execute(EARLIEST_SQL).fetchone()
        earliest = _parse(earliest_row[0]) if earliest_row and earliest_row[0] else None
        rows = conn.execute(
            "SELECT id, created_at, recording_duration_sec, duration, file_path, calendar_event_id, "
            "recording_session_id "
            "FROM audio_downloads WHERE source_type = 'recording' ORDER BY id").fetchall()
        for r in rows:
            stats["recordings"] += 1
            if r["calendar_event_id"] is not None:
                stats["already_bound"] += 1
                continue
            dur = float(r["recording_duration_sec"] or r["duration"] or 0)
            start = _manifest_start(sessions_dir, r["recording_session_id"])
            if start is not None:
                stats["start_from_manifest"] += 1
            else:
                try:
                    start = _parse(r["created_at"]) - timedelta(seconds=dur)
                except (TypeError, ValueError):
                    stats["unbound"] += 1
                    continue
                stats["start_from_saved_at"] += 1
            ev = bind_download(conn, r["id"], start, start + timedelta(seconds=dur)) if dur else None
            if ev is None:
                stats["unbound"] += 1
                # No stored event can exist for a recording older than the calendar's history.
                if earliest is None or start + timedelta(seconds=dur) < earliest - TOLERANCE:
                    stats["before_calendar"] += 1
                continue
            stats["bound"] += 1
            src = per_source.setdefault(ev["source_id"], {"bound": 0, "attendees_linked": 0})
            src["bound"] += 1
            for t in conn.execute("SELECT id FROM transcriptions WHERE file_path = ? "
                                  "AND calendar_event_id IS NULL", (r["file_path"],)).fetchall():
                conn.execute("UPDATE transcriptions SET calendar_event_id = ? WHERE id = ?",
                             (ev["id"], t["id"]))
                res = link_attendees(conn, t["id"], ev["id"])
                stats["attendees_linked"] += res["linked"]
                stats["attendees_unmatched"] += res["unmatched"]
                src["attendees_linked"] += res["linked"]
        stats["calendar_earliest"] = _iso(earliest) if earliest else None
        stats["coverable"] = stats["recordings"] - stats["before_calendar"]
        stats["persons_before"] = persons_before
        stats["persons_after"] = _person_count(conn)
        labels = dict(conn.execute("SELECT id, label FROM calendar_sources").fetchall())
        stats["per_source"] = {labels.get(k, str(k)): v for k, v in per_source.items()}
        if commit:
            conn.commit()
        else:
            conn.rollback()
    stats["dry_run"] = not commit
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.services.calendar_bind")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="bind past recordings to calendar events (titles untouched)")
    b.add_argument("--dry-run", action="store_true")
    from config import Config
    b.add_argument("--db", default=str(Config.BASE_DIR / Config.DATABASE))
    b.add_argument("--sessions", default=str(Config.RECORDING_DIR),
                   help="recording sessions folder (manifest.json carries the exact start)")
    args = ap.parse_args(argv)
    stats = backfill(args.db, commit=not args.dry_run, sessions_dir=args.sessions)
    sys.stdout.write(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
