"""k6-calendar-ics story 03: `list_calendar_events` and `calendar_event` in `get_transcript`.

Offline: a temporary SQLite via `init_database`, an in-memory keyring (conftest), no network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import keyring
import pytest

import mcp_server as m
from app.db.migrations import init_database
from app.services import calendar_ics

SECRET = "https://calendar.google.com/calendar/ical/x/private-zz5519/basic.ics"


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "mcp_cal.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    words = tmp_path / "private.txt"
    words.write_text("таємниця\n", encoding="utf-8")
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(words))
    monkeypatch.setenv("RECALL_PRIVATE_CATEGORIES", "Особисте")
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO calendar_sources (id, label, host, last_sync_at, events_seen, events_with_attendees) "
                 "VALUES (1, 'Google', 'calendar.google.com', '2026-10-26T12:00:00Z', 4, 1)")
    conn.execute("INSERT INTO calendar_sources (id, label, host, events_seen, events_with_attendees) "
                 "VALUES (2, 'Outlook Datalink', 'outlook.office365.com', 1, 0)")
    private_cat = conn.execute("INSERT INTO categories (name) VALUES ('Особисте')").lastrowid

    def ev(eid, src, start, end, title, attendees=(), status="confirmed"):
        conn.execute("INSERT INTO calendar_events (id, source_id, uid, occurrence_key, start_utc, end_utc, "
                     "title, attendees, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (eid, src, f"u{eid}", start, start, end, title,
                      json.dumps(list(attendees), ensure_ascii=False), status))

    ev(1, 1, "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z", "Дзвінок з фондом",
       [{"name": "Олена Коваль", "email": "olena@example.com"}, {"name": "", "email": "noname@example.com"}])
    ev(2, 2, "2026-10-27T15:00:00Z", "2026-10-27T16:00:00Z", "Teams sync")
    ev(3, 1, "2026-10-28T09:00:00Z", "2026-10-28T10:00:00Z", "Лікар")
    ev(4, 1, "2026-10-28T11:00:00Z", "2026-10-28T12:00:00Z", "Моя таємниця")
    ev(5, 1, "2026-10-28T13:00:00Z", "2026-10-28T14:00:00Z", "Скасовано", status="cancelled")
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, title, calendar_event_id) "
                 "VALUES (101, 'recording', 'r', 'Дзвінок з фондом', 1)")
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id, calendar_event_id) "
                 "VALUES (102, 'recording', 'r', ?, 3)", (private_cat,))
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name) VALUES (103, 'recording', 'r')")
    conn.commit()
    conn.close()
    keyring.set_password(calendar_ics.KEYRING_SERVICE, "Google", SECRET)
    return path


def test_window_lists_events_with_bound_record_and_hides_private_ones(db, monkeypatch):
    def no_keyring(*_a, **_k):
        raise AssertionError("the MCP process must not read the feed address")
    monkeypatch.setattr(calendar_ics.keyring, "get_password", no_keyring)
    res = m.list_calendar_events("2026-10-27", "2026-10-29")
    assert [e["title"] for e in res["events"]] == ["Дзвінок з фондом", "Teams sync"]
    call = res["events"][0]
    assert call["transcription_id"] == 101 and call["source"] == "Google"
    assert call["attendees"] == ["Олена Коваль", "noname@example.com"]
    assert res["events"][1]["transcription_id"] is None
    assert res["hidden"] == 2                       # private-direction record + stop word in the title
    outlook = next(s for s in res["sources"] if s["label"] == "Outlook Datalink")
    assert outlook["events_with_attendees"] == 0
    assert "private-zz5519" not in json.dumps(res)


def test_bad_window_is_an_argument_error(db):
    assert m.list_calendar_events("вчора")["error_code"] == "INVALID_ARGUMENT"
    assert m.list_calendar_events("2026-10-28", "2026-10-27")["error_code"] == "INVALID_ARGUMENT"


def test_get_transcript_carries_the_bound_event_only_when_bound(db):
    bound = m.get_transcript(101)
    assert bound["calendar_event"]["title"] == "Дзвінок з фондом"
    assert bound["calendar_event"]["source"] == "Google"
    assert bound["calendar_event"]["attendees"] == ["Олена Коваль", "noname@example.com"]
    assert "calendar_event" not in m.get_transcript(103)


def test_tool_is_read_only_in_the_registry():
    assert m._TOOL_CLASS["list_calendar_events"]["class"] == "R"
