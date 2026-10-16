"""Tests for the live chat profile (Wave 4.5.2).

The local model is mocked — offline, without Ollama. The main thing checked here:
hard numbers come from SQL and are NOT given to the model to rewrite, invented
thread references are discarded, and the update threshold really restrains calls.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.db.migrations import init_database
from app.services import tg_chat_context as ctx


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _seed(path, *, chat_id=-100, title="Робочий чат", messages=None, threads=None):
    """threads=None — create a default thread; threads=[] — none at all
    (an empty list is NOT the same as «not passed»)."""
    if threads is None:
        threads = [(1, "Бюджет", "open")]
    conn = sqlite3.connect(path)
    for tid, label, status in threads:
        conn.execute("INSERT INTO tg_threads (id, chat_id, label, status, msg_count, "
                     "last_date) VALUES (?, ?, ?, ?, 0, '2026-06-01T10:00:00+00:00')",
                     (tid, chat_id, label, status))
    # We continue the numbering rather than start from 1: _seed can top up
    # messages in an already seeded chat, and (chat_id, message_id) are unique.
    start = conn.execute("SELECT COALESCE(MAX(tg_message_id), 0) FROM transcriptions "
                         "WHERE tg_chat_id = ?", (chat_id,)).fetchone()[0] + 1
    for offset, (sender, text, thread) in enumerate(messages or []):
        i = start + offset
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "tg_chat_id, tg_chat_title, tg_message_id, tg_date, tg_sender, tg_thread_id) "
            "VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"[TG] {text[:20]}", text, chat_id, title, i,
             f"2026-06-{min(i, 28):02d}T10:00:00+00:00", sender, thread))
    for row in conn.execute("SELECT id FROM tg_threads WHERE chat_id = ?",
                            (chat_id,)).fetchall():
        conn.execute("UPDATE tg_threads SET msg_count = (SELECT COUNT(*) FROM "
                     "transcriptions WHERE tg_thread_id = ?) WHERE id = ?",
                     (row[0], row[0]))
    conn.commit()
    conn.close()


def _fake_model(monkeypatch, payload):
    from app.services import local_llm
    monkeypatch.setattr(local_llm, "generate_json", lambda *a, **k: {"data": payload})


def _model_down(monkeypatch):
    from app.services import local_llm

    def _boom(*a, **k):
        raise local_llm.LocalLLMError("нема Ollama")

    monkeypatch.setattr(local_llm, "generate_json", _boom)


# ============================================================
# Hard part
# ============================================================

def test_participants_counted_by_sql(db):
    _seed(db, messages=[("Адам", "перше", 1), ("Адам", "друге", 1), ("Юля", "третє", 1)])
    people = ctx.participants(db, -100)
    assert [(p["name"], p["messages"]) for p in people] == [("Адам", 2), ("Юля", 1)]
    assert people[0]["first_seen"] < people[0]["last_seen"]


def test_topics_come_from_threads_not_from_model(db, monkeypatch):
    _seed(db, messages=[("Адам", "про бюджет", 1)])
    _fake_model(monkeypatch, {"summary": "звід", "roles": []})
    ctx.refresh_chat(db, -100)
    got = ctx.get_context(db, -100)
    assert [t["label"] for t in got["topics"]] == ["Бюджет"]


def test_closed_threads_are_not_current_topics(db):
    _seed(db, messages=[("Адам", "стара тема", 1)],
          threads=[(1, "Закрита", "closed")])
    assert ctx.open_threads(db, -100) == []


def test_model_cannot_rewrite_hard_numbers(db, monkeypatch):
    """An invented number in a living person's profile is worse than its absence."""
    _seed(db, messages=[("Адам", "перше", 1), ("Адам", "друге", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "roles": [{"name": "Адам", "role": "веде облік"}]})
    ctx.refresh_chat(db, -100)
    adam = ctx.get_context(db, -100)["participants"][0]
    assert adam["messages"] == 2, "the number stays from SQL"
    assert adam["role"] == "веде облік", "only the description comes from the model"


# ============================================================
# Hygiene of the model's response
# ============================================================

def test_invented_thread_ids_are_dropped(db, monkeypatch):
    """A reference to a thread that was not shown leads nowhere."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {
        "summary": "звід",
        "open_questions": [{"question": "коли зустріч?", "thread_id": 999}],
    })
    ctx.refresh_chat(db, -100)
    q = ctx.get_context(db, -100)["open_questions"][0]
    assert "thread_id" not in q
    assert q["question"] == "коли зустріч?"


def test_valid_thread_id_is_kept(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "decisions": [{"decision": "беремо", "thread_id": 1}]})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["decisions"][0]["thread_id"] == 1


def test_empty_strings_are_dropped(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "open_questions": [{"question": "   "}, {"question": "справжнє"}]})
    ctx.refresh_chat(db, -100)
    qs = ctx.get_context(db, -100)["open_questions"]
    assert [q["question"] for q in qs] == ["справжнє"]


def test_degradation_keeps_hard_part(db, monkeypatch):
    """No model — the profile still exists, but the text fields are empty, and that is visible."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _model_down(monkeypatch)
    res = ctx.refresh_chat(db, -100)
    assert res["degraded"] is True
    got = ctx.get_context(db, -100)
    assert got["summary"] is None
    assert got["model"] is None
    assert len(got["participants"]) == 1, "the hard part stays"
    assert len(got["topics"]) == 1


# ============================================================
# Threshold
# ============================================================

def test_first_time_needs_refresh(db):
    _seed(db, messages=[("Адам", "текст", 1)])
    assert ctx.needs_refresh(db, -100) is True


def test_no_refresh_right_after_update(db, monkeypatch):
    """The profile is NOT rebuilt on every message — these are exactly the calls
    that Wave 0 put a guard against."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    assert ctx.needs_refresh(db, -100) is False


def test_refresh_after_enough_new_messages(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    _seed(db, messages=[("Юля", f"нове {i}", 1) for i in range(ctx.MIN_NEW_MESSAGES)],
          threads=[])
    assert ctx.needs_refresh(db, -100) is True


def test_refresh_after_max_age(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    later = datetime.now() + timedelta(hours=ctx.MAX_AGE_HOURS + 1)
    assert ctx.needs_refresh(db, -100, now=later) is True


def test_refresh_all_dry_run_writes_nothing(db):
    _seed(db, messages=[("Адам", "текст", 1)])
    res = ctx.refresh_all(db, dry_run=True)
    assert res["due"] == 1
    assert ctx.get_context(db, -100) is None


def test_refresh_is_upsert_not_duplicate(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "перший"})
    ctx.refresh_chat(db, -100)
    _fake_model(monkeypatch, {"summary": "другий"})
    ctx.refresh_chat(db, -100)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM tg_chat_context").fetchone()[0] == 1
    conn.close()
    assert ctx.get_context(db, -100)["summary"] == "другий"


def test_get_context_missing_chat(db):
    assert ctx.get_context(db, -999) is None


def test_failed_refresh_does_not_erase_existing_profile(db, monkeypatch):
    """Caught on a live run: one 32B timeout turned a normal dossier
    into an empty one, because the upsert wrote None over the assembled one."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "зібраний звід",
                              "roles": [{"name": "Адам", "role": "веде облік"}],
                              "open_questions": [{"question": "коли?"}],
                              "decisions": [{"decision": "беремо"}]})
    ctx.refresh_chat(db, -100)

    _model_down(monkeypatch)
    res = ctx.refresh_chat(db, -100)
    assert res["degraded"] is True

    got = ctx.get_context(db, -100)
    assert got["summary"] == "зібраний звід", "the digest must survive a model refusal"
    assert got["participants"][0]["role"] == "веде облік"
    assert [q["question"] for q in got["open_questions"]] == ["коли?"]
    assert [d["decision"] for d in got["decisions"]] == ["беремо"]
    assert got["model"] is not None, "the text exists, just not fresh"


def test_failed_refresh_still_updates_hard_part(db, monkeypatch):
    """The hard part does not depend on the model — we update it even on a refusal."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    _seed(db, messages=[("Юля", "нове", 1)], threads=[])
    _model_down(monkeypatch)
    ctx.refresh_chat(db, -100)
    got = ctx.get_context(db, -100)
    assert {p["name"] for p in got["participants"]} == {"Адам", "Юля"}
    assert got["messages_seen"] == 2


def test_first_refresh_failure_leaves_empty_text(db, monkeypatch):
    """There is no previous dossier — nothing to keep, degraded is honestly empty."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _model_down(monkeypatch)
    ctx.refresh_chat(db, -100)
    got = ctx.get_context(db, -100)
    assert got["summary"] is None and got["model"] is None
    assert len(got["participants"]) == 1


def test_model_column_records_default_model(db, monkeypatch):
    """NULL in model must mean exactly one thing: there is no text. Earlier it was written
    on success too, if the model was taken by default."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["model"]


@pytest.mark.parametrize("junk", ["null", "None", "n/a", "—", "невідомо", "  "])
def test_placeholder_roles_are_treated_as_empty(db, monkeypatch, junk):
    """Caught on a live run: 18 participants got the role string "null" —
    a non-emptiness check is not enough, in the dossier it looked like a real role."""
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "roles": [{"name": "Адам", "role": junk}]})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["participants"][0]["role"] is None


def test_real_role_survives_cleaning(db, monkeypatch):
    _seed(db, messages=[("Адам", "текст", 1)])
    _fake_model(monkeypatch, {"summary": "звід",
                              "roles": [{"name": "Адам", "role": "узгоджує бюджет"}]})
    ctx.refresh_chat(db, -100)
    assert ctx.get_context(db, -100)["participants"][0]["role"] == "узгоджує бюджет"


# ============================================================
# Freshness of the dossier in the MCP tool (truthfulness, 12.08.2026)
# ============================================================

def test_tool_says_how_many_messages_missed_the_profile(db, monkeypatch):
    """«Updated by threshold» is a truth you can do nothing with.

    The dossier returned `updated_at` but did not say how many messages arrived
    AFTER it: a lag of two and of two hundred looked the same. The tool
    computes the difference as a number so the reader sees how far the card can be trusted.
    """
    import mcp_server as m

    monkeypatch.setattr(m, "DB_PATH", db)
    _seed(db, messages=[("Адам", "перше", 1)])
    _fake_model(monkeypatch, {"summary": "звід"})
    ctx.refresh_chat(db, -100)

    assert m.get_chat_context(-100)["messages_after_profile"] == 0

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_message_id, tg_date, tg_sender, created_at) "
        "VALUES ('telegram', '[TG] пізніше', 'пізніше', -100, 9001, "
        "'2026-06-30T10:00:00+00:00', 'Адам', '2099-01-01 00:00:00')")
    conn.commit()
    conn.close()

    assert m.get_chat_context(-100)["messages_after_profile"] == 1


def test_tool_keeps_hint_when_profile_absent(db, monkeypatch):
    """No dossier — an error with a hint, not an empty dict with a zero."""
    import mcp_server as m

    monkeypatch.setattr(m, "DB_PATH", db)
    got = m.get_chat_context(-777)

    assert "error" in got and "refresh" in got["hint"]
    assert "messages_after_profile" not in got
