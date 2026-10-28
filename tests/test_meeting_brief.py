"""wave4-client-calls story 02: pre-meeting brief data, privacy filter, card, ticker, HTTP route."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from app.blueprints import automation as auto
from app.db.migrations import init_database
from app.services import enrichment
from app.services import meeting_brief as mb
from app.services.privacy import PrivacyError

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
PRIVATE_CAT = 2  # «Особисте» from the migrations, private by default
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "brief.db")
    init_database(path)
    monkeypatch.delenv("RECALL_PRIVATE_CATEGORIES", raising=False)
    monkeypatch.setenv("RECALL_PRIVATE_WORDS_FILE", str(tmp_path / "private.txt"))
    monkeypatch.setenv("MEETING_BRIEF_ENABLED", "1")
    monkeypatch.setenv("RECALL_API_URL", "http://127.0.0.1:5050")
    monkeypatch.delenv("RECALL_PUBLIC_URL", raising=False)
    c = _conn(path)
    c.execute("INSERT INTO calendar_sources (id, label, host) VALUES (1, 'Google', 'calendar.google.com')")
    pid = enrichment._upsert_entity(c, "person", "Іван Петренко", aliases=["Иван Петренко"])
    # Calls: 6 live ones (ids 1..6, newer id = newer date), one deleted, one Telegram message.
    for i in range(1, 7):
        c.execute("INSERT INTO transcriptions (id, source_type, source_name, title, meeting_date, category_id) "
                  "VALUES (?, 'recording', 'rec', ?, ?, 1)", (i, f"Дзвінок {i}", f"2026-10-0{i}"))
        c.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count) VALUES (?, ?, 1)",
                  (i, pid))
    c.execute("INSERT INTO transcriptions (id, source_type, source_name, title, meeting_date, deleted_at) "
              "VALUES (7, 'recording', 'rec', 'Видалений', '2026-10-20', '2026-10-21')")
    c.execute("INSERT INTO transcriptions (id, source_type, source_name, title, meeting_date) "
              "VALUES (8, 'telegram', 'tg', 'Повідомлення', '2026-10-22')")
    for tid in (7, 8):
        c.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count) VALUES (?, ?, 1)",
                  (tid, pid))
    # 7 open tasks (open|stale) + excluded ones.
    for i in range(1, 8):
        c.execute("INSERT INTO action_items (id, transcription_id, task, owner_entity_id, status, due_date) "
                  "VALUES (?, 1, ?, ?, ?, ?)",
                  (100 + i, f"Задача {i}", pid, "stale" if i == 7 else "open", f"2026-11-0{i}"))
    for iid, status, dup in ((120, "done", None), (121, "cancelled", None), (122, "proposed", None),
                             (123, "open", 101)):
        c.execute("INSERT INTO action_items (id, transcription_id, task, owner_entity_id, status, dup_of) "
                  "VALUES (?, 1, 'excluded', ?, ?, ?)", (iid, pid, status, dup))
    c.commit()
    c.close()
    return {"path": path, "pid": pid, "tmp": tmp_path}


def _words(env, *words):
    (env["tmp"] / "private.txt").write_text("\n".join(words), encoding="utf-8")


def _event(env, *, start, title="Зустріч з фондом", attendees=None, all_day=0, status="confirmed"):
    if attendees is None:
        attendees = [{"name": "Іван Петренко", "email": "ivan@example.com"}]
    c = _conn(env["path"])
    eid = c.execute(
        "INSERT INTO calendar_events (source_id, uid, occurrence_key, start_utc, end_utc, all_day, title, "
        "attendees, status) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"uid-{start}-{title}", start, start, (datetime.strptime(start, FMT) + timedelta(hours=1)).strftime(FMT),
         all_day, title, json.dumps(attendees, ensure_ascii=False), status)).lastrowid
    c.commit()
    c.close()
    return eid


def _row(env, eid):
    c = _conn(env["path"])
    r = c.execute("SELECT * FROM meeting_brief_sent WHERE event_id = ?", (eid,)).fetchone()
    c.close()
    return dict(r) if r else None


# ------------------------------------------------------------------- data

def test_brief_for_event_matches_people_tasks_and_calls(db):
    eid = _event(db, start=(NOW + timedelta(minutes=10)).strftime(FMT), attendees=[
        {"name": "Іван Петренко", "email": "ivan@example.com"},
        {"name": "Nobody Known", "email": "nobody@example.com"},
        {"name": "", "email": "only@example.com"}])
    ev = mb.get_event(db["path"], eid)
    c = _conn(db["path"])
    brief = mb.brief_for_event(c, ev)
    c.close()
    people = brief["people"]
    assert len(people) == 3
    first = people[0]
    assert first["matched"] is True and first["entity_id"] == db["pid"]
    assert [t["id"] for t in first["open_tasks"]] == [101, 102, 103, 104, 105]
    assert first["open_more"] == 2
    assert [c_["transcription_id"] for c_ in first["last_calls"]] == [6, 5, 4, 3, 2]
    for other in people[1:]:
        assert other["matched"] is False and other["entity_id"] is None
        assert other["open_tasks"] == [] and other["last_calls"] == [] and other["open_more"] == 0
    assert "example.com" not in json.dumps(brief)


def test_brief_for_person_finds_entity_through_alias(db):
    c = _conn(db["path"])
    brief = mb.brief_for_person(c, "иван петренко")
    miss = mb.brief_for_person(c, "Nobody Known")
    c.close()
    assert brief["event"] is None
    assert brief["people"][0]["entity_id"] == db["pid"] and brief["people"][0]["matched"]
    assert miss["people"][0]["matched"] is False


def test_matching_never_creates_an_entity(db):
    c = _conn(db["path"])
    before = c.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    mb.brief_for_person(c, "Brand New Person")
    after = c.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    c.close()
    assert before == after


# ---------------------------------------------------------------- privacy

def _person_brief(db):
    c = _conn(db["path"])
    brief = mb.brief_for_person(c, "Іван Петренко")
    c.close()
    return brief


def test_filter_hides_private_direction_and_stop_words(db):
    c = _conn(db["path"])
    c.execute("UPDATE transcriptions SET category_id = ? WHERE id = 6", (PRIVATE_CAT,))
    c.execute("UPDATE action_items SET transcription_id = 6 WHERE id = 101")
    c.commit()
    c.close()
    _words(db, "секрет")
    brief = _person_brief(db)
    brief["people"][0]["open_tasks"][1]["task"] = "Обговорити секрет"
    out = mb.filter_brief(brief, db_path=db["path"])
    p = out["people"][0]
    assert 101 not in [t["id"] for t in p["open_tasks"]]
    assert 102 not in [t["id"] for t in p["open_tasks"]]
    assert 6 not in [c_["transcription_id"] for c_ in p["last_calls"]]
    assert out["hidden"] == 3


def test_filter_unreadable_private_file_fails_closed(db):
    (db["tmp"] / "private.txt").write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(PrivacyError):
        mb.filter_brief(_person_brief(db), db_path=db["path"])


# ------------------------------------------------------------------- card

def test_card_tags_guard_and_links(db, monkeypatch):
    monkeypatch.setenv("RECALL_PUBLIC_URL", "https://recall.example.ts.net")
    brief = _person_brief(db)
    brief["people"][0]["open_tasks"][0]["task"] = "Pay to UA213223130000026007233566001"
    html_text, leaked = mb.render_card(brief, db_path=db["path"])
    assert leaked == []
    assert set(re.findall(r"</?([a-z]+)", html_text)) <= {"b", "i", "a"}
    assert "•••" in html_text and "UA2132" not in html_text
    assert "127.0.0.1" not in html_text
    hrefs = re.findall(r'href="([^"]+)"', html_text)
    assert hrefs and all(h.startswith("https://recall.example.ts.net/") for h in hrefs)
    assert "/tasks?owner=" in html_text


def test_card_reports_stop_word_in_a_field(db):
    _words(db, "петренко")
    _text, leaked = mb.render_card(_person_brief(db), db_path=db["path"])
    assert leaked == ["петренко"]


# ----------------------------------------------------------------- ticker

def _ticker(db, sent, fail=False):
    def deliver(text):
        if fail:
            raise RuntimeError("bot down")
        sent.append(text)
    return mb.MeetingBriefTicker(db["path"], deliver, lead_minutes=15, now_fn=lambda: NOW)


def test_ticker_sends_once_and_marks_sent(db):
    eid = _event(db, start=(NOW + timedelta(minutes=10)).strftime(FMT))
    _event(db, start=(NOW + timedelta(minutes=40)).strftime(FMT), title="Later")
    sent = []
    t = _ticker(db, sent)
    assert t.run_once(NOW) == {"sent": 1, "failed": 0, "skipped": 0}
    row = _row(db, eid)
    assert row["status"] == "sent" and row["sent_at"] and row["attempts"] == 1
    assert len(sent) == 1 and "Іван Петренко" in sent[0]
    assert t.run_once(NOW + timedelta(minutes=1)) == {"sent": 0, "failed": 0, "skipped": 0}
    assert len(sent) == 1


def test_ticker_failed_send_is_retried_until_start(db):
    eid = _event(db, start=(NOW + timedelta(minutes=10)).strftime(FMT))
    sent = []
    assert _ticker(db, sent, fail=True).run_once(NOW)["failed"] == 1
    row = _row(db, eid)
    assert row["status"] == "failed" and row["attempts"] == 1 and "bot down" in row["last_error"]
    assert row["sent_at"] is None
    assert _ticker(db, sent).run_once(NOW + timedelta(minutes=1))["sent"] == 1
    assert _row(db, eid)["attempts"] == 2


def test_ticker_no_attempt_after_start(db):
    eid = _event(db, start=(NOW + timedelta(minutes=10)).strftime(FMT))
    sent = []
    _ticker(db, sent, fail=True).run_once(NOW)
    assert _ticker(db, sent).run_once(NOW + timedelta(minutes=11)) == {"sent": 0, "failed": 0, "skipped": 0}
    assert _row(db, eid)["attempts"] == 1 and sent == []


def test_ticker_skips_no_names_and_private_title(db):
    a = _event(db, start=(NOW + timedelta(minutes=5)).strftime(FMT),
               attendees=[{"name": "", "email": "x@example.com"}])
    _words(db, "фонд")
    b = _event(db, start=(NOW + timedelta(minutes=6)).strftime(FMT), title="Фонд: рада")
    sent = []
    assert _ticker(db, sent).run_once(NOW)["skipped"] == 2
    assert _row(db, a)["status"] == "skipped" and _row(db, b)["status"] == "skipped"
    assert sent == []


def test_ticker_skips_all_day_and_cancelled(db):
    _event(db, start=(NOW + timedelta(minutes=5)).strftime(FMT), all_day=1)
    _event(db, start=(NOW + timedelta(minutes=6)).strftime(FMT), title="Cancelled", status="cancelled")
    sent = []
    assert _ticker(db, sent).run_once(NOW) == {"sent": 0, "failed": 0, "skipped": 0}


def test_ticker_unreadable_private_file_records_failed(db):
    eid = _event(db, start=(NOW + timedelta(minutes=10)).strftime(FMT))
    (db["tmp"] / "private.txt").write_bytes(b"\xff\xfe\xfa")
    sent = []
    assert _ticker(db, sent).run_once(NOW)["failed"] == 1
    assert _row(db, eid)["status"] == "failed" and sent == []


def test_ticker_leaked_word_blocks_send(db, monkeypatch):
    eid = _event(db, start=(NOW + timedelta(minutes=10)).strftime(FMT))
    _words(db, "петренко")
    sent = []
    assert _ticker(db, sent).run_once(NOW)["skipped"] == 1
    assert sent == [] and "петренко" not in (_row(db, eid)["last_error"] or "")


def test_ticker_disabled_does_nothing(db, monkeypatch):
    eid = _event(db, start=(NOW + timedelta(minutes=10)).strftime(FMT))
    monkeypatch.setenv("MEETING_BRIEF_ENABLED", "0")
    sent = []
    assert _ticker(db, sent).run_once(NOW) == {"sent": 0, "failed": 0, "skipped": 0}
    assert sent == [] and _row(db, eid) is None


# ------------------------------------------------------------------ route

@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.config["DATABASE"] = db["path"]
    app.register_blueprint(auto.automation_bp)
    c = app.test_client()
    c.env = db
    return c


def test_route_person_and_event(client):
    eid = _event(client.env, start=(NOW + timedelta(minutes=10)).strftime(FMT))
    r = client.get("/api/meeting-brief?person=иван петренко")
    assert r.status_code == 200 and r.get_json()["brief"]["people"][0]["matched"]
    r = client.get(f"/api/meeting-brief?event_id={eid}")
    body = r.get_json()
    assert r.status_code == 200 and body["brief"]["event"]["id"] == eid
    r = client.get("/api/meeting-brief?person=Nobody")
    assert r.status_code == 200 and r.get_json()["brief"]["people"][0]["matched"] is False


def test_route_filters_private(client):
    c = _conn(client.env["path"])
    c.execute("UPDATE transcriptions SET category_id = ? WHERE id = 6", (PRIVATE_CAT,))
    c.commit()
    c.close()
    body = client.get("/api/meeting-brief?person=Іван Петренко").get_json()
    assert body["brief"]["hidden"] >= 1
    assert 6 not in [x["transcription_id"] for x in body["brief"]["people"][0]["last_calls"]]


@pytest.mark.parametrize("query", ["", "?person=a&event_id=1", "?event_id=abc"])
def test_route_bad_arguments(client, query):
    r = client.get("/api/meeting-brief" + query)
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ARGUMENT"


def test_route_unknown_event(client):
    r = client.get("/api/meeting-brief?event_id=999")
    assert r.status_code == 404 and r.get_json()["error_code"] == "NOT_FOUND"


# -------------------------------------------------------------------- boot

def test_app_starts_ticker_only_when_enabled_and_configured():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _maybe_start_meeting_brief"):]
    body = body[:body.index("\n# ")]
    assert "MEETING_BRIEF_ENABLED" in body and "is_configured()" in body
    assert "app.extensions['meeting_brief_ticker']" in body
    assert "_maybe_start_meeting_brief()" in src
