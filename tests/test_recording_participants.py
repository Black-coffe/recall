"""wave2-recording-calls story 02: participants suggested from the calendar, searched, linked as 'owner'."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from app.db.migrations import init_database
from app.services import calendar_bind as cb
from app.services import enrichment
from app.services.recording import participants as pp

NOW = datetime(2026, 10, 27, 13, 30, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "participants.db")
    init_database(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("INSERT INTO calendar_sources (id, label, host) VALUES (1, 'Google', 'calendar.google.com')")
    yield c
    c.close()


def _person(c, name, aliases=(), etype="person"):
    return enrichment._upsert_entity(c, etype, name, aliases=list(aliases))


def _event(c, uid, start, end, attendees, status="confirmed", all_day=0):
    return c.execute(
        "INSERT INTO calendar_events (source_id, uid, occurrence_key, start_utc, end_utc, all_day, "
        "title, attendees, status) VALUES (1, ?, ?, ?, ?, ?, 'Дзвінок', ?, ?)",
        (uid, start, start, end, all_day, json.dumps(list(attendees), ensure_ascii=False),
         status)).lastrowid


def _persons(c):
    return c.execute("SELECT COUNT(*) FROM entities WHERE type = 'person'").fetchone()[0]


def _tid(c):
    return c.execute("INSERT INTO transcriptions (source_type, source_name) "
                     "VALUES ('recording', 'r')").lastrowid


# ------------------------------------------------------------------ suggest

def test_suggest_matches_name_alias_and_merged_spelling_without_creating_anyone(conn, tmp_path):
    olena = _person(conn, "Олена Коваль", aliases=["Olena Koval"])
    kept = _person(conn, "Петро Шевчук")
    gone = _person(conn, "Petro Shevchuk")
    andriy = _person(conn, "Андрій")
    conn.commit()
    from app.services import entity_dedup
    entity_dedup.merge_entities(str(tmp_path / "participants.db"), kept, gone, allow_person=True)
    # Starts 9 min after now: inside the tolerance.
    _event(conn, "soon", "2026-10-27T13:39:00Z", "2026-10-27T14:30:00Z", [
        {"name": "Olena Koval", "email": "olena@example.com"},
        {"name": "Petro Shevchuk", "email": "petro@example.com"},
        {"name": "Nobody Known", "email": "n@example.com"},
        {"name": "", "email": "noname@example.com"}])
    _event(conn, "now", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z",
           [{"name": "андрій", "email": "a@example.com"}])
    before = _persons(conn)
    got = pp.suggest(conn, NOW)
    assert sorted((p["id"], p["source"]) for p in got) == sorted(
        [(olena, "calendar"), (kept, "calendar"), (andriy, "calendar")])
    assert {p["name"] for p in got} == {"Олена Коваль", "Петро Шевчук", "Андрій"}
    assert _persons(conn) == before
    aliases = [r[0] for r in conn.execute("SELECT alias FROM entity_aliases")]
    assert not any("@" in a for a in aliases)


@pytest.mark.parametrize("kw", [
    {"status": "cancelled"},
    {"all_day": 1},
    {"start": "2026-10-27T13:41:00Z", "end": "2026-10-27T14:30:00Z"},   # starts beyond +10 min
    {"start": "2026-10-27T12:00:00Z", "end": "2026-10-27T13:19:00Z"},   # ended before -10 min
], ids=["cancelled", "all_day", "too_late", "too_early"])
def test_suggest_ignores_unusable_events(conn, kw):
    _person(conn, "Олена Коваль")
    _event(conn, "e", kw.get("start", "2026-10-27T13:00:00Z"), kw.get("end", "2026-10-27T14:00:00Z"),
           [{"name": "Олена Коваль"}], status=kw.get("status", "confirmed"),
           all_day=kw.get("all_day", 0))
    assert pp.suggest(conn, NOW) == []


def test_suggest_without_a_calendar_source_is_empty(conn):
    _person(conn, "Олена Коваль")
    _event(conn, "e", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z", [{"name": "Олена Коваль"}])
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM calendar_sources")
    assert pp.suggest(conn, NOW) == []


def test_suggest_with_no_events_is_empty(conn):
    _person(conn, "Олена Коваль")
    assert pp.suggest(conn, NOW) == []


# ------------------------------------------------------------------ search

def test_search_is_cyrillic_case_insensitive_and_bounded(conn):
    andriy = _person(conn, "Андрій Коваль")
    _person(conn, "Андріана")
    _person(conn, "Сергій Андрієнко")
    _person(conn, "Андрій", etype="project")
    got = pp.search(conn, "андр")
    assert [p["name"] for p in got] == ["Андріана", "Андрій Коваль", "Сергій Андрієнко"]
    assert got[1] == {"id": andriy, "name": "Андрій Коваль"}
    assert len(pp.search(conn, "АНДР", limit=2)) == 2


def test_search_matches_an_alias_and_never_returns_non_persons(conn):
    olena = _person(conn, "Олена Коваль", aliases=["Olena Koval"])
    _person(conn, "Koval Group", etype="org")
    assert pp.search(conn, "KOVAL") == [{"id": olena, "name": "Олена Коваль"}]
    assert pp.search(conn, "") == []


# ------------------------------------------------------------------ link

def test_link_writes_owner_rows_skips_unknown_and_non_persons_and_is_idempotent(conn):
    olena = _person(conn, "Олена Коваль")
    project = _person(conn, "Барселона", etype="project")
    tid = _tid(conn)
    assert pp.link_to_transcription(conn, tid, [olena, project, 99999, "x", True, olena]) == 1
    assert pp.link_to_transcription(conn, tid, [olena]) == 0
    rows = conn.execute("SELECT entity_id, source FROM meeting_entities WHERE transcription_id = ?",
                        (tid,)).fetchall()
    assert [(r["entity_id"], r["source"]) for r in rows] == [(olena, "owner")]


def test_re_enrich_keeps_owner_and_calendar_links(conn):
    olena = _person(conn, "Олена Коваль")
    petro = _person(conn, "Петро Шевчук")
    eid = _event(conn, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z",
                 [{"name": "Олена Коваль"}])
    tid = _tid(conn)
    cb.link_attendees(conn, tid, eid)
    pp.link_to_transcription(conn, tid, [petro])
    card = {"summary": "s", "people": [{"name": "Петро Шевчук"}]}
    enrichment._persist_card(conn, tid, card, "Петро Шевчук сказав", None, None)
    enrichment._persist_card(conn, tid, {"summary": "s"}, "текст", None, None)
    rows = conn.execute("SELECT entity_id, source FROM meeting_entities WHERE transcription_id = ?",
                        (tid,)).fetchall()
    assert sorted((r["entity_id"], r["source"]) for r in rows) == sorted(
        [(olena, "calendar"), (petro, "owner")])


def test_session_participants_reads_only_positive_ints():
    assert pp.session_participants({"participants": [3, "4", 3, -1, None, True, 7]}) == [3, 7]
    assert pp.session_participants({}) == []
    assert pp.session_participants(None) == []
