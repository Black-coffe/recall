"""Тести сводок одиниць сенсу з провенансом (Волна B, v42).

Claude мокається — офлайн, без ANTHROPIC_API_KEY. Перевіряється: сама
міграція (ідемпотентна), гілка verbatim для коротких ниток, провенанс
(id повідомлень-джерел, модель, водяний знак), збій моделі не валить
прохід, покриття сводками трьох типів (дзвінки/документи/нитки),
``unit_summary_line`` для всіх трьох.
"""
import json
import sqlite3

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import summaries


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _seed_thread(path, *, chat_id=-100, title="Робочий чат", thread_id=1,
                 label="Договір", messages=()):
    """messages = [(sender, text)] — хронологічно, по одному на день."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR IGNORE INTO tg_threads (id, chat_id, label, status, "
                 "msg_count, last_date) VALUES (?, ?, ?, 'open', 0, "
                 "'2026-06-10T10:00:00+00:00')", (thread_id, chat_id, label))
    start = conn.execute("SELECT COALESCE(MAX(tg_message_id), 0) FROM transcriptions "
                         "WHERE tg_chat_id = ?", (chat_id,)).fetchone()[0] + 1
    ids = []
    for offset, (sender, text) in enumerate(messages):
        i = start + offset
        cur = conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "tg_chat_id, tg_chat_title, tg_message_id, tg_date, tg_sender, tg_link, "
            "tg_thread_id) VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"[TG] {text[:20]}", text, chat_id, title, i,
             f"2026-06-{min(i, 28):02d}T10:00:00+00:00", sender,
             f"https://t.me/c/1/{i}", thread_id))
        ids.append(cur.lastrowid)
    conn.execute("UPDATE tg_threads SET msg_count = (SELECT COUNT(*) FROM transcriptions "
                 "WHERE tg_thread_id = ?) WHERE id = ?", (thread_id, thread_id))
    conn.commit()
    conn.close()
    return ids


def _seed_record(path, *, source_type="file", summary_json=None,
                 deleted_at=None, duplicate_of=None) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "summary_json, deleted_at, duplicate_of) VALUES (?, ?, ?, ?, ?, ?)",
        (source_type, "запис", "текст запису",
         json.dumps(summary_json, ensure_ascii=False) if summary_json else None,
         deleted_at, duplicate_of))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def _thread_row(path, thread_id=1):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tg_threads WHERE id = ?", (thread_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _fake_summary(monkeypatch, text="Обговорили договір і домовились про строк.",
                  model="claude-test"):
    monkeypatch.setattr(summaries, "_call_summary", lambda payload, head, m: (text, model))


# ============================================================
# Міграція v42
# ============================================================

def test_migration_adds_summary_columns_idempotent(db):
    conn = sqlite3.connect(db)
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(tg_threads)")}
    conn.close()
    assert {"summary_source_ids_json", "summary_at", "summary_model",
           "summary_msgs"} <= cols_before

    init_database(db)  # повторний запуск — без падінь і без дублів колонок
    conn = sqlite3.connect(db)
    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(tg_threads)")}
    conn.close()
    assert cols_before == cols_after


# ============================================================
# Гілка verbatim
# ============================================================

def test_short_thread_is_verbatim_without_model_call(db):
    ids = _seed_thread(db, messages=[("Адам", "ок"), ("Юля", "дякую")])
    with get_db_connection(db) as conn:
        res = summaries.summarize_thread(conn, 1)
    assert res["status"] == "ok"
    assert res["model"] == "verbatim"

    row = _thread_row(db)
    assert row["summary"] == "ок дякую"
    assert row["summary_model"] == "verbatim"
    assert row["summary_at"] is not None
    assert json.loads(row["summary_source_ids_json"]) == ids
    assert row["summary_msgs"] == 2


def test_verbatim_dry_run_writes_nothing(db):
    _seed_thread(db, messages=[("Адам", "ок")])
    with get_db_connection(db) as conn:
        res = summaries.summarize_thread(conn, 1, dry_run=True)
    assert res["status"] == "dry_run" and res["model"] == "verbatim"
    assert _thread_row(db)["summary"] is None


# ============================================================
# Claude-гілка + провенанс
# ============================================================

def _long_messages():
    # > VERBATIM_CHAR_LIMIT (300) сумарно.
    return [("Адам", "Треба обговорити умови договору. " * 4),
            ("Юля", "Пропоную зустрітись у четвер і підписати. " * 4),
            ("Адам", "Домовились, чекаю проєкт документа.")]


def test_claude_summary_stores_source_ids_and_model(db, monkeypatch):
    ids = _seed_thread(db, messages=_long_messages())
    _fake_summary(monkeypatch, text="Домовились зустрітись у четвер і підписати договір.",
                  model="claude-test-5")
    with get_db_connection(db) as conn:
        res = summaries.summarize_thread(conn, 1)
    assert res["status"] == "ok" and res["model"] == "claude-test-5"

    row = _thread_row(db)
    assert row["summary"] == "Домовились зустрітись у четвер і підписати договір."
    assert row["summary_model"] == "claude-test-5"
    assert json.loads(row["summary_source_ids_json"]) == ids
    assert row["summary_msgs"] == 3


def test_summary_longer_than_limit_is_truncated(db, monkeypatch):
    _seed_thread(db, messages=_long_messages())
    _fake_summary(monkeypatch, text="д" * 900)
    with get_db_connection(db) as conn:
        summaries.summarize_thread(conn, 1)
    assert len(_thread_row(db)["summary"]) == summaries.SUMMARY_LINE_LIMIT


def test_dry_run_estimates_cost_without_writing(db, monkeypatch):
    _seed_thread(db, messages=_long_messages())

    def _boom(*a, **k):
        raise AssertionError("dry-run не має кликати Claude")
    monkeypatch.setattr(summaries, "_call_summary", _boom)

    with get_db_connection(db) as conn:
        res = summaries.summarize_thread(conn, 1, model="claude-test-5", dry_run=True)
    assert res["status"] == "dry_run"
    assert res["chars"] > 0
    assert res["cost_est_usd_input_only"] >= 0
    assert _thread_row(db)["summary"] is None


def test_claude_failure_leaves_thread_a_candidate(db, monkeypatch):
    _seed_thread(db, messages=_long_messages())

    def _boom(*a, **k):
        raise RuntimeError("Claude лежить")
    monkeypatch.setattr(summaries, "_call_summary", _boom)

    with get_db_connection(db) as conn:
        res = summaries.summarize_thread(conn, 1)
    assert res["status"] == "failed"
    assert _thread_row(db)["summary"] is None
    assert summaries.backfill_candidates(db) == [1]


# ============================================================
# Бекфіл
# ============================================================

def test_backfill_dry_run_writes_nothing(db, monkeypatch):
    _seed_thread(db, thread_id=1, messages=[("Адам", "ок")])
    _seed_thread(db, thread_id=2, messages=_long_messages())

    def _boom(*a, **k):
        raise AssertionError("dry-run не має кликати Claude")
    monkeypatch.setattr(summaries, "_call_summary", _boom)

    res = summaries.backfill(db, model="claude-test-5", dry_run=True)
    assert res["dry_run"] is True and res["candidates"] == 2
    assert _thread_row(db, 1)["summary"] is None
    assert _thread_row(db, 2)["summary"] is None


def test_backfill_rerun_is_idempotent(db, monkeypatch):
    _seed_thread(db, thread_id=1, messages=[("Адам", "ок")])
    _seed_thread(db, thread_id=2, messages=_long_messages())
    _fake_summary(monkeypatch)
    monkeypatch.setattr("app.services.text_polishing.is_available", lambda: True)

    first = summaries.backfill(db, dry_run=False)
    assert first["threads"] == 2 and first["failed"] == 0

    second = summaries.backfill(db, dry_run=False)
    assert second["candidates"] == 0 and second["threads"] == 0


def test_backfill_skips_without_api_key(db, monkeypatch):
    _seed_thread(db, messages=_long_messages())
    monkeypatch.setattr("app.services.text_polishing.is_available", lambda: False)
    res = summaries.backfill(db, dry_run=False)
    assert res["skipped"] is True and res["candidates"] == 1


# ============================================================
# stats() — покриття трьох типів
# ============================================================

def test_stats_covers_three_types_excluding_deleted_and_dups(db, monkeypatch):
    _seed_record(db, source_type="file", summary_json={"summary": "Зведення."})
    _seed_record(db, source_type="youtube")
    _seed_record(db, source_type="document", summary_json={"summary": "Опис."})
    # виключені з підрахунку:
    _seed_record(db, source_type="file", summary_json={"summary": "X"}, deleted_at=1700000000)
    _seed_record(db, source_type="file", summary_json={"summary": "X"}, duplicate_of=1)

    _seed_thread(db, thread_id=1, messages=[("Адам", "ок")])
    with get_db_connection(db) as conn:
        summaries.summarize_thread(conn, 1)  # verbatim
    _seed_thread(db, thread_id=2, messages=[("Юля", "привіт")])  # без сводки

    st = summaries.stats(db)
    assert st["calls"] == {"total": 2, "with_summary": 1, "pct": 50.0}
    assert st["documents"] == {"total": 1, "with_summary": 1, "pct": 100.0}
    assert st["threads"] == {"total": 2, "with_summary": 1, "pct": 50.0}


# ============================================================
# unit_summary_line — контракт C1
# ============================================================

def test_unit_summary_line_telegram_reads_thread_summary(db, monkeypatch):
    ids = _seed_thread(db, messages=[("Адам", "ок"), ("Юля", "дякую")])
    with get_db_connection(db) as conn:
        summaries.summarize_thread(conn, 1)  # verbatim → summary = "ок дякую"

    with get_db_connection(db) as conn:
        line = summaries.unit_summary_line(conn, ids[0])
    assert line == "ок дякую"


def test_unit_summary_line_takes_first_sentence_and_caps_length(db):
    _seed_thread(db, messages=[("Адам", "перше")])
    with get_db_connection(db) as conn:
        conn.execute("UPDATE tg_threads SET summary = ? WHERE id = 1",
                    ("Перше речення. Друге речення, яке не має потрапити.",))
        conn.commit()
    tid = _seed_record(db, source_type="telegram")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET tg_thread_id = 1 WHERE id = ?", (tid,))
    conn.commit()
    conn.close()

    with get_db_connection(db) as conn:
        line = summaries.unit_summary_line(conn, tid)
    assert line == "Перше речення."
    assert "\n" not in (line or "")
    assert len(line) <= summaries.UNIT_LINE_LIMIT


def test_unit_summary_line_call_reads_summary_json(db):
    tid = _seed_record(db, source_type="file",
                       summary_json={"summary": "Обговорили бюджет. Ухвалили рішення."})
    with get_db_connection(db) as conn:
        line = summaries.unit_summary_line(conn, tid)
    assert line == "Обговорили бюджет."


def test_unit_summary_line_document_reads_summary_json(db):
    tid = _seed_record(db, source_type="document",
                       summary_json={"summary": "Документ описує процес закупівлі."})
    with get_db_connection(db) as conn:
        line = summaries.unit_summary_line(conn, tid)
    assert line == "Документ описує процес закупівлі."


def test_unit_summary_line_none_without_summary(db):
    tid_call = _seed_record(db, source_type="file", summary_json=None)
    tid_tg = _seed_record(db, source_type="telegram")  # без tg_thread_id
    with get_db_connection(db) as conn:
        assert summaries.unit_summary_line(conn, tid_call) is None
        assert summaries.unit_summary_line(conn, tid_tg) is None
        assert summaries.unit_summary_line(conn, 999999) is None
