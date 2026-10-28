"""k6-calendar-ics story 02: bind a recording to its event, title order, attendee links, backfill."""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import calendar_bind as cb
from app.services import enrichment
from app.services.recording.library import register_recording

SID = "rec_cal0001"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "bind.db")
    init_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO calendar_sources (id, label, host) VALUES (1, 'Google', 'calendar.google.com')")
    conn.commit()
    conn.close()
    return path


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _event(db, uid, start, end, title="Дзвінок з фондом", status="confirmed", all_day=0, attendees=()):
    c = _conn(db)
    eid = c.execute(
        "INSERT INTO calendar_events (source_id, uid, occurrence_key, start_utc, end_utc, all_day, "
        "title, attendees, status) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, start, start, end, all_day, title, json.dumps(list(attendees), ensure_ascii=False),
         status)).lastrowid
    c.commit()
    c.close()
    return eid


def _person(db, name, aliases=()):
    c = _conn(db)
    eid = enrichment._upsert_entity(c, "person", name, aliases=list(aliases))
    c.commit()
    c.close()
    return eid


def _persons(db):
    c = _conn(db)
    n = c.execute("SELECT COUNT(*) FROM entities WHERE type = 'person'").fetchone()[0]
    c.close()
    return n


def _manifest(tmp_path, start="2026-10-27T13:02:00+00:00", duration=3300.0, name=None):
    mp3 = tmp_path / "final.mp3"
    mp3.write_bytes(b"ID3")
    return {"session_id": SID, "started_at": start, "total_duration_sec": duration,
            "final_mp3_path": str(mp3), "name": name, "auto_name": "Запис 2026-10-27 15:02"}


# ------------------------------------------------------------------ choosing the event

def test_larger_overlap_wins_and_weak_or_unusable_events_bind_nothing(db):
    _event(db, "short", "2026-10-27T12:30:00Z", "2026-10-27T13:15:00Z", title="Попередня")
    long_id = _event(db, "long", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z")
    c = _conn(db)
    start, end = cb._parse("2026-10-27T13:02:00Z"), cb._parse("2026-10-27T13:57:00Z")
    assert cb.best_event(c, start, end)["id"] == long_id
    # a 2-minute brush with an event is below a third of a 55-minute recording
    assert cb.best_event(c, cb._parse("2026-10-27T14:08:00Z"), cb._parse("2026-10-27T15:00:00Z")) is None
    c.close()


@pytest.mark.parametrize("kw", [{"status": "cancelled"}, {"all_day": 1}])
def test_cancelled_and_all_day_events_never_bind(db, kw):
    _event(db, "x", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z", **kw)
    c = _conn(db)
    assert cb.best_event(c, cb._parse("2026-10-27T13:02:00Z"), cb._parse("2026-10-27T13:57:00Z")) is None
    c.close()


# ------------------------------------------------------------------ title order on save

def test_unnamed_recording_takes_the_event_title(db, tmp_path):
    eid = _event(db, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z")
    reg = register_recording(db, SID, _manifest(tmp_path))
    assert reg["name"] == "Дзвінок з фондом"
    c = _conn(db)
    row = c.execute("SELECT title, calendar_event_id FROM audio_downloads WHERE id = ?",
                    (reg["download_id"],)).fetchone()
    c.close()
    assert (row["title"], row["calendar_event_id"]) == ("Дзвінок з фондом", eid)


@pytest.mark.parametrize("explicit, manifest_name", [("Мій дзвінок", None), (None, "Назва з екрана")])
def test_owner_name_is_never_replaced_by_the_event(db, tmp_path, explicit, manifest_name):
    eid = _event(db, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z")
    reg = register_recording(db, SID, _manifest(tmp_path, name=manifest_name), name=explicit)
    assert reg["name"] == (explicit or manifest_name)
    c = _conn(db)
    row = c.execute("SELECT title, calendar_event_id FROM audio_downloads").fetchone()
    c.close()
    assert row["title"] == (explicit or manifest_name) and row["calendar_event_id"] == eid


def test_second_save_with_a_name_after_autobinding_keeps_the_owner_name(db, tmp_path):
    _event(db, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z")
    register_recording(db, SID, _manifest(tmp_path))
    reg = register_recording(db, SID, _manifest(tmp_path), name="Мій дзвінок")
    assert reg["name"] == "Мій дзвінок"


def test_repeated_save_without_a_name_reports_the_event_title(db, tmp_path):
    _event(db, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z")
    register_recording(db, SID, _manifest(tmp_path))
    assert register_recording(db, SID, _manifest(tmp_path))["name"] == "Дзвінок з фондом"


def test_no_event_keeps_the_automatic_name(db, tmp_path):
    reg = register_recording(db, SID, _manifest(tmp_path))
    assert reg["name"] == "Запис 2026-10-27 15:02"


# ------------------------------------------------------------------ attendees

def test_attendees_link_existing_people_only(db):
    olena = _person(db, "Олена Коваль", aliases=["Olena Koval"])
    kept = _person(db, "Петро Шевчук")
    gone = _person(db, "Petro Shevchuk")
    from app.services import entity_dedup
    entity_dedup.merge_entities(db, kept, gone, allow_person=True)   # «Petro Shevchuk» merged away
    before = _persons(db)
    eid = _event(db, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z", attendees=[
        {"name": "Olena Koval", "email": "olena@example.com"},
        {"name": "Petro Shevchuk", "email": "petro@example.com"},
        {"name": "Andriy Unknown", "email": "a@example.com"},
        {"name": "", "email": "noname@example.com"}])
    c = _conn(db)
    tid = c.execute("INSERT INTO transcriptions (source_type, source_name) VALUES ('recording', 'r')").lastrowid
    res = cb.link_attendees(c, tid, eid)
    c.commit()
    links = {r["entity_id"]: r["source"] for r in c.execute(
        "SELECT entity_id, source FROM meeting_entities WHERE transcription_id = ?", (tid,))}
    aliases = [r[0] for r in c.execute("SELECT alias FROM entity_aliases")]
    c.close()
    assert links == {olena: "calendar", kept: "calendar"}
    assert res == {"linked": 2, "unmatched": 2}
    assert _persons(db) == before                              # nobody created
    assert not any("@" in a for a in aliases)                  # emails never become aliases


def test_transcription_copies_the_binding_and_a_re_enrich_keeps_calendar_links(db, tmp_path):
    olena = _person(db, "Олена Коваль")
    _event(db, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z",
           attendees=[{"name": "Олена Коваль", "email": "o@example.com"}])
    reg = register_recording(db, SID, _manifest(tmp_path))
    c = _conn(db)
    tid = c.execute("INSERT INTO transcriptions (source_type, source_name, file_path) "
                    "VALUES ('recording', 'r', ?)", (reg["file_path"],)).lastrowid
    event_id = cb.apply_to_transcription(c, tid, reg["download_id"])
    enrichment._persist_card(c, tid, {"summary": "s"}, "текст", None, None)
    c.commit()
    row = c.execute("SELECT calendar_event_id FROM transcriptions WHERE id = ?", (tid,)).fetchone()
    links = c.execute("SELECT entity_id, source FROM meeting_entities WHERE transcription_id = ?",
                      (tid,)).fetchall()
    c.close()
    assert row["calendar_event_id"] == event_id is not None
    assert [(r["entity_id"], r["source"]) for r in links] == [(olena, "calendar")]


# ------------------------------------------------------------------ backfill

def _past_recording(db, created_at, duration, title, sid=None):
    c = _conn(db)
    did = c.execute(
        "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type, "
        "recording_duration_sec, created_at, recording_session_id) "
        "VALUES ('recording://x', ?, ?, ?, 'recording', ?, ?, ?)",
        (f"recording_{title}", title, f"E:/rec/{title}.mp3", duration, created_at, sid)).lastrowid
    c.execute("INSERT INTO transcriptions (source_type, source_name, file_path, title) "
              "VALUES ('recording', 'r', ?, ?)", (f"E:/rec/{title}.mp3", title))
    c.commit()
    c.close()
    return did


def test_backfill_dry_run_matches_the_real_run_and_titles_stay(db):
    _person(db, "Олена Коваль")
    _event(db, "call", "2026-10-20T13:00:00Z", "2026-10-20T14:00:00Z",
           attendees=[{"name": "Олена Коваль", "email": "o@example.com"}, {"name": "Хтось", "email": ""}])
    _past_recording(db, "2026-10-20 13:58:00", 3300, "Зустріч Claude-назва")
    _past_recording(db, "2026-10-21 09:00:00", 600, "Без події")
    dry = cb.backfill(db, commit=False)
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) FROM audio_downloads WHERE calendar_event_id IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM meeting_entities").fetchone()[0] == 0
    c.close()
    real = cb.backfill(db)
    for k in ("recordings", "bound", "unbound", "attendees_linked", "attendees_unmatched",
              "persons_before", "persons_after", "per_source"):
        assert dry[k] == real[k], k
    assert real["bound"] == 1 and real["unbound"] == 1
    assert real["persons_before"] == real["persons_after"]
    assert real["per_source"] == {"Google": {"bound": 1, "attendees_linked": 1}}
    c = _conn(db)
    titles = sorted(r[0] for r in c.execute("SELECT title FROM transcriptions"))
    c.close()
    assert titles == ["Без події", "Зустріч Claude-назва"]
    assert cb.backfill(db)["already_bound"] == 1               # idempotent


def test_backfill_takes_the_exact_start_from_the_session_manifest(db, tmp_path):
    # Saved two hours after the stop: created_at - duration lands on the next meeting.
    _event(db, "first", "2026-10-20T09:00:00Z", "2026-10-20T10:00:00Z", title="Ранкова")
    _event(db, "second", "2026-10-20T11:00:00Z", "2026-10-20T12:00:00Z", title="Обідня")
    sessions = tmp_path / "sessions"
    (sessions / "rec_late").mkdir(parents=True)
    (sessions / "rec_late" / "manifest.json").write_text(
        json.dumps({"started_at": "2026-10-20T09:02:00+00:00"}), encoding="utf-8")
    _past_recording(db, "2026-10-20 11:55:00", 3300, "Пізно збережено", sid="rec_late")
    res = cb.backfill(db, sessions_dir=str(sessions))
    assert res["start_from_manifest"] == 1 and res["bound"] == 1
    c = _conn(db)
    title = c.execute("SELECT e.title FROM audio_downloads a JOIN calendar_events e "
                      "ON e.id = a.calendar_event_id").fetchone()[0]
    c.close()
    assert title == "Ранкова"


def test_re_enrich_naming_the_attendee_keeps_the_calendar_provenance(db):
    olena = _person(db, "Олена Коваль")
    eid = _event(db, "call", "2026-10-27T13:00:00Z", "2026-10-27T14:00:00Z",
                 attendees=[{"name": "Олена Коваль", "email": "o@example.com"}])
    c = _conn(db)
    tid = c.execute("INSERT INTO transcriptions (source_type, source_name) VALUES ('recording', 'r')").lastrowid
    cb.link_attendees(c, tid, eid)
    card = {"summary": "s", "people": [{"name": "Олена Коваль"}]}
    enrichment._persist_card(c, tid, card, "Олена Коваль сказала", None, None)
    enrichment._persist_card(c, tid, {"summary": "s"}, "текст", None, None)
    c.commit()
    rows = c.execute("SELECT entity_id, source FROM meeting_entities WHERE transcription_id = ?", (tid,)).fetchall()
    c.close()
    assert [(r["entity_id"], r["source"]) for r in rows] == [(olena, "calendar")]


def test_backfill_counts_recordings_older_than_the_calendar_apart(db):
    # k6-calendar-full-sync: the feed starts in October; a June recording is not a miss.
    _event(db, "oct", "2026-10-20T13:00:00Z", "2026-10-20T14:00:00Z")
    _event(db, "bday", "1970-03-01T00:00:00Z", "1970-03-02T00:00:00Z", title="День народження", all_day=1)
    _past_recording(db, "2026-10-20 13:58:00", 3300, "Жовтень")
    _past_recording(db, "2026-06-02 10:00:00", 1800, "Червень")
    _past_recording(db, "2026-10-22 10:00:00", 1800, "Без події в жовтні")
    res = cb.backfill(db, commit=False)
    assert res["calendar_earliest"] == "2026-10-20T13:00:00Z"
    assert (res["bound"], res["unbound"], res["before_calendar"]) == (1, 2, 1)
    assert res["coverable"] == 2
