"""k6-calendar-ics story 01: secret ICS sources, sync, connect page. No network, in-memory keyring."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import keyring
import pytest
from flask import Flask

from app.blueprints.connect import connect_bp
from app.blueprints.settings_api import settings_bp
from app.db.migrations import init_database
from app.services import calendar_ics as ci
from app.services import connect_links as cl

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
GOOGLE_URL = "https://calendar.google.com/calendar/ical/owner%40example.com/private-zz9917/basic.ics"
OUTLOOK_URL = "https://outlook.office365.com/owa/calendar/abc/def-zz4471/calendar.ics"

# Weekly Monday stand-up in Kyiv time: 19.10 dropped by EXDATE, 26.10 moved to 11:00 and renamed.
# Plus an all-day event, a cancelled one-off, and a call with attendees.
GOOGLE_FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VTIMEZONE
TZID:Europe/Kyiv
BEGIN:STANDARD
DTSTART:19701025T040000
TZOFFSETFROM:+0300
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0300
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:standup-1@google.com
DTSTART;TZID=Europe/Kyiv:20261005T100000
DTEND;TZID=Europe/Kyiv:20261005T103000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6
EXDATE;TZID=Europe/Kyiv:20261019T100000
SUMMARY:Стендап
END:VEVENT
BEGIN:VEVENT
UID:standup-1@google.com
RECURRENCE-ID;TZID=Europe/Kyiv:20261026T100000
DTSTART;TZID=Europe/Kyiv:20261026T110000
DTEND;TZID=Europe/Kyiv:20261026T113000
SUMMARY:Стендап (перенесено)
END:VEVENT
BEGIN:VEVENT
UID:holiday-1@google.com
DTSTART;VALUE=DATE:20261028
DTEND;VALUE=DATE:20261029
SUMMARY:Вихідний
END:VEVENT
BEGIN:VEVENT
UID:cancelled-1@google.com
DTSTART:20261027T090000Z
DTEND:20261027T100000Z
STATUS:CANCELLED
SUMMARY:Скасована зустріч
END:VEVENT
BEGIN:VEVENT
UID:call-1@google.com
DTSTART:20261027T130000Z
DTEND:20261027T140000Z
SUMMARY:Дзвінок з фондом
ORGANIZER;CN=Owner:mailto:owner@example.com
ATTENDEE;CN=Олена Коваль;PARTSTAT=ACCEPTED:mailto:olena@example.com
ATTENDEE:mailto:noname@example.com
END:VEVENT
END:VCALENDAR
""".encode("utf-8")

OUTLOOK_FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:Microsoft Exchange Server 2010
BEGIN:VEVENT
UID:teams-1
DTSTART:20261026T150000Z
DTEND:20261026T160000Z
SUMMARY:Teams sync
END:VEVENT
END:VCALENDAR
""".encode("utf-8")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """A test that forgets to pass a fake feed fails here instead of calling Google."""
    def refuse(_url):
        raise AssertionError("network fetch in a test")
    monkeypatch.setattr(ci, "fetch_feed", refuse)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "cal.db")
    init_database(path)
    return path


def _feeds(mapping):
    return lambda url: mapping[url]


def _events(db, source_id=None):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM calendar_events"
    rows = conn.execute(sql + (" WHERE source_id = ?" if source_id else "") + " ORDER BY start_utc",
                        (source_id,) if source_id else ()).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _connect(db, label, url, feed, monkeypatch):
    monkeypatch.setattr(ci, "_now", lambda: NOW)
    return ci.connect_source(db, label, url, fetch=lambda _u: feed)


# ------------------------------------------------------------------ sync

def test_sync_expands_recurrence_exdate_override_allday_and_cancelled(db, monkeypatch):
    res = _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    rows = _events(db)
    standups = [r for r in rows if r["uid"] == "standup-1@google.com"]
    # 05.10, 12.10, (19.10 excluded), 26.10 moved, 02.11, 09.11 - all inside +-30 days
    assert [r["start_utc"] for r in standups] == [
        "2026-10-05T07:00:00Z", "2026-10-12T07:00:00Z", "2026-10-26T09:00:00Z",
        "2026-11-02T08:00:00Z", "2026-11-09T08:00:00Z"]
    moved = next(r for r in standups if r["start_utc"] == "2026-10-26T09:00:00Z")
    assert moved["title"] == "Стендап (перенесено)"
    assert moved["occurrence_key"] == "2026-10-26T08:00:00Z"      # keyed by the original start
    holiday = next(r for r in rows if r["uid"] == "holiday-1@google.com")
    assert holiday["all_day"] == 1 and holiday["start_utc"] == "2026-10-28T00:00:00Z"
    cancelled = next(r for r in rows if r["uid"] == "cancelled-1@google.com")
    assert cancelled["status"] == "cancelled"
    call = next(r for r in rows if r["uid"] == "call-1@google.com")
    assert json.loads(call["attendees"]) == [
        {"name": "Олена Коваль", "email": "olena@example.com"}, {"name": "", "email": "noname@example.com"}]
    assert call["organizer"] == "owner@example.com"
    assert res["events"] == 7 and res["with_attendees"] == 1


def test_second_sync_is_idempotent_and_vanished_occurrence_is_cancelled(db, monkeypatch):
    res = _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    sid = res["source_id"]
    before = _events(db)
    ci.sync_source(db, sid, fetch=lambda _u: GOOGLE_FEED, now=NOW)
    assert _events(db) == [dict(r, updated_at=r["updated_at"]) for r in before]
    without_call = GOOGLE_FEED.replace(GOOGLE_FEED[GOOGLE_FEED.index(b"BEGIN:VEVENT\nUID:call-1"):
                                                   GOOGLE_FEED.index(b"END:VCALENDAR")], b"")
    out = ci.sync_source(db, sid, fetch=lambda _u: without_call, now=NOW)
    call = next(r for r in _events(db) if r["uid"] == "call-1@google.com")
    assert call["status"] == "cancelled"
    assert out["with_attendees"] == 0
    assert len(_events(db)) == len(before)                        # nothing is deleted


def test_two_sources_keep_their_own_events_and_attendee_counts(db, monkeypatch):
    g = _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    o = _connect(db, "Outlook Datalink", OUTLOOK_URL, OUTLOOK_FEED, monkeypatch)
    assert {r["uid"] for r in _events(db, o["source_id"])} == {"teams-1"}
    sources = {s["label"]: s for s in ci.list_sources(db)}
    assert sources["Outlook Datalink"]["events_seen"] == 1
    assert sources["Outlook Datalink"]["events_with_attendees"] == 0   # a feed without ATTENDEE is visible
    assert sources["Google"]["events_with_attendees"] == 1
    assert all(s["configured"] for s in sources.values())
    window = ci.events_between(db, "2026-10-26T00:00:00Z", "2026-10-27T00:00:00Z")
    assert [(e["source_label"], e["title"]) for e in window] == [
        ("Google", "Стендап (перенесено)"), ("Outlook Datalink", "Teams sync")]


def test_failing_source_records_its_error_and_others_still_sync(db, monkeypatch):
    g = _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    o = _connect(db, "Outlook", OUTLOOK_URL, OUTLOOK_FEED, monkeypatch)

    def fetch(url):
        if url == GOOGLE_URL:
            raise ci.CalendarError(ci.FETCH_FAILED)
        return OUTLOOK_FEED

    out = ci.sync_all(db, fetch=fetch, now=NOW)
    assert out[g["source_id"]] == {"error": ci.FETCH_FAILED}
    assert out[o["source_id"]]["events"] == 1
    assert ci.get_source(db, g["source_id"])["last_error"] == ci.FETCH_FAILED


def test_poller_does_not_sync_when_disabled(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "sync_all", lambda *a, **k: calls.append(1))
    monkeypatch.setenv("CALENDAR_SYNC_ENABLED", "0")
    ci.CalendarPoller(db).run_once()
    assert calls == []
    monkeypatch.setenv("CALENDAR_SYNC_ENABLED", "1")
    ci.CalendarPoller(db).run_once()
    assert calls == [1]


# ------------------------------------------------------------------ secret handling

@pytest.mark.parametrize("url, code", [
    ("http://calendar.google.com/calendar/ical/x/basic.ics", ci.BAD_URL),
    ("https://evil.example.com/calendar.ics", ci.HOST_NOT_ALLOWED),
    ("not a url", ci.BAD_URL),
])
def test_unacceptable_addresses_are_refused_before_any_fetch(db, url, code):
    def fetch(_u):
        raise AssertionError("must not fetch")
    with pytest.raises(ci.CalendarError) as exc:
        ci.connect_source(db, "X", url, fetch=fetch)
    assert exc.value.code == code
    assert ci.list_sources(db) == []


def test_webcal_scheme_is_accepted_as_https():
    assert ci.validate_url("webcal://calendar.google.com/calendar/ical/x/basic.ics") == "calendar.google.com"


def test_unparsable_feed_stores_nothing(db):
    with pytest.raises(ci.CalendarError) as exc:
        ci.connect_source(db, "Google", GOOGLE_URL, fetch=lambda _u: b"<html>login</html>")
    assert exc.value.code == ci.PARSE_FAILED
    assert ci.list_sources(db) == []
    assert keyring.get_password(ci.KEYRING_SERVICE, "Google") is None


def test_address_lives_only_in_the_keyring(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    assert keyring.get_password(ci.KEYRING_SERVICE, "Google") == GOOGLE_URL
    assert b"private-zz9917" not in Path(db).read_bytes()
    assert "private-zz9917" not in caplog.text
    assert "private-zz9917" not in json.dumps(ci.list_sources(db))


def test_disconnect_drops_keyring_events_and_bindings(db, monkeypatch):
    res = _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    o = _connect(db, "Outlook", OUTLOOK_URL, OUTLOOK_FEED, monkeypatch)
    ev = _events(db, res["source_id"])[0]["id"]
    conn = sqlite3.connect(db)
    tid = conn.execute("INSERT INTO transcriptions (source_type, source_name, calendar_event_id) "
                       "VALUES ('file', 'a', ?)", (ev,)).lastrowid
    conn.commit()
    conn.close()
    assert ci.disconnect_source(db, res["source_id"]) is True
    assert keyring.get_password(ci.KEYRING_SERVICE, "Google") is None
    assert _events(db, res["source_id"]) == []
    assert len(_events(db, o["source_id"])) == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT calendar_event_id FROM transcriptions WHERE id = ?", (tid,)).fetchone()[0] is None
    conn.close()
    assert ci.disconnect_source(db, res["source_id"]) is False


# ------------------------------------------------------------------ connect page and routes

@pytest.fixture
def client(db, tmp_path, monkeypatch):
    cl._links.clear()
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    a = Flask(__name__, root_path=str(tmp_path),
              template_folder=str(Path(__file__).resolve().parent.parent / "templates"))
    a.register_blueprint(settings_bp)
    a.register_blueprint(connect_bp)
    a.config.update(TESTING=True, DATABASE=db)
    return a.test_client()


def _link(client, label="Google"):
    r = client.post("/api/settings/calendars/connect-link", json={"label": label},
                    base_url="http://127.0.0.1:5050")
    assert r.status_code == 200
    return r.get_json()["connect_url"].split("5050", 1)[1]


def test_connect_page_refuses_foreign_host_then_accepts_google(client, db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    path = _link(client)
    page = client.get(path, base_url="http://127.0.0.1:5050")
    assert page.status_code == 200 and b"Google" in page.data
    bad = client.post(path, data={"secret": "https://evil.example.com/x.ics"}, base_url="http://127.0.0.1:5050")
    assert bad.status_code == 400 and b"Google Calendar and Outlook" in bad.data
    monkeypatch.setattr(ci, "fetch_feed", lambda url: GOOGLE_FEED)
    monkeypatch.setattr(ci, "_now", lambda: NOW)
    ok = client.post(path, data={"secret": GOOGLE_URL}, base_url="http://127.0.0.1:5050")
    assert ok.status_code == 200 and b"with attendees: 1" in ok.data
    assert b"private-zz9917" not in ok.data and "private-zz9917" not in caplog.text
    listed = client.get("/api/settings/calendars").get_json()
    assert [s["label"] for s in listed["sources"]] == ["Google"]
    assert "private-zz9917" not in json.dumps(listed)


def test_connect_link_needs_a_label(client):
    r = client.post("/api/settings/calendars/connect-link", json={"label": "  "})
    assert r.status_code == 400 and r.get_json()["error_code"] == "BAD_REQUEST"


def test_sync_now_without_poller_and_unknown_source(client, db, monkeypatch):
    res = _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    r = client.post(f"/api/settings/calendars/{res['source_id']}/sync")
    assert r.status_code == 503 and r.get_json()["error_code"] == "CALENDAR_POLLER_OFF"
    assert client.post("/api/settings/calendars/999/sync").status_code == 404


def test_labels_differing_in_case_keep_separate_addresses(db, monkeypatch):
    a = _connect(db, "Google", GOOGLE_URL, GOOGLE_FEED, monkeypatch)
    other = GOOGLE_URL.replace("private-zz9917", "private-zz0002")
    _connect(db, "google", other, GOOGLE_FEED, monkeypatch)
    assert ci.disconnect_source(db, a["source_id"]) is True
    assert keyring.get_password(ci.KEYRING_SERVICE, "google") == other
    assert keyring.get_password(ci.KEYRING_SERVICE, "Google") is None


# ------------------------------------------------------------------ CLI (k6-calendar-full-sync)

def test_cli_full_sync_takes_the_whole_feed_for_every_source(db, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ci, "sync_all", lambda path, **kw: calls.append(kw) or {1: {"events": 3}})
    assert ci.main(["sync", "--full", "--db", db]) == 0
    assert calls == [{"window_days": None}]
    out = json.loads(capsys.readouterr().out)
    assert out["full"] is True and out["sources"] == {"1": {"events": 3}}


def test_cli_one_source_without_full_keeps_the_window(db, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ci, "sync_source", lambda path, sid, **kw: calls.append((sid, kw)) or {"events": 1})
    assert ci.main(["sync", "--source", "7", "--db", db]) == 0
    assert calls == [(7, {})]
    monkeypatch.setattr(ci, "sync_source", lambda *a, **k: (_ for _ in ()).throw(KeyError(9)))
    assert ci.main(["sync", "--source", "9", "--db", db]) == 1
