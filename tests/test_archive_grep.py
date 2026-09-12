"""Тести буквального/regex-пошуку по чанках (grep-explainability, історія 01).

БД — власна тимчасова SQLite, побудована через `init_database()` (та сама
схема `chunks`/`chunks_fts`/`transcriptions`, що й у бойовому архіві), НІКОЛИ
не `whisper_history.db`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import archive_grep


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "grep_test.db")
    init_database(path)
    return path


def _add_transcription(conn, *, source_type="meeting", source_name="test", meeting_date=None,
                        tg_chat_title=None, tg_link=None):
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, meeting_date, "
        "tg_chat_title, tg_link) VALUES (?, ?, ?, ?, ?)",
        (source_type, source_name, meeting_date, tg_chat_title, tg_link),
    )
    return cur.lastrowid


def _add_chunk(conn, transcription_id, chunk_index, text, speaker=None, start_time=None):
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, text, speaker, start_time) "
        "VALUES (?, ?, ?, ?, ?)",
        (transcription_id, chunk_index, text, speaker, start_time),
    )


def test_literal_finds_what_fts5_cannot(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        _add_chunk(conn, tid, 0, "Оплата рахунку ID-4471 підтверджена вчора.")
        conn.commit()

    result = archive_grep.grep(db_path, "ID-4471")
    assert result["count"] == 1
    assert result["matches"][0]["chunk_index"] == 0
    assert "ID-4471" in result["matches"][0]["excerpt"]

    # `-` у нетокенізованому запиті FTS5 парситься як NOT-оператор колонки —
    # рівно те розсипання, про яке каже контракт: наївний MATCH ламається,
    # там де grep() коректно знаходить підрядок.
    import sqlite3 as _sqlite3
    with get_db_connection(db_path) as conn:
        with pytest.raises(_sqlite3.OperationalError):
            conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", ("ID-4471",)
            ).fetchall()


def test_regex_finds_split_number_and_rejects_bad_pattern(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        _add_chunk(conn, tid, 0, "Сума договору 380 123 456 грн без ПДВ.")
        conn.commit()

    result = archive_grep.grep(db_path, r"\d{3}\s?\d{3}\s?\d{3}", regex=True)
    assert result["count"] == 1
    assert result["matches"][0]["match_count"] == 1

    with pytest.raises(ValueError):
        archive_grep.grep(db_path, r"[unterminated(", regex=True)


def test_context_clamped_at_transcription_boundaries(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        for i in range(4):
            _add_chunk(conn, tid, i, f"чанк {i} з міткою TARGET" if i == 1 else f"чанк {i}")
        conn.commit()

    result = archive_grep.grep(db_path, "TARGET", context=2)
    match = result["matches"][0]
    assert match["chunk_index"] == 1
    # ліворуч є лише чанк 0 (межа транскрипції) — не 2, хоч context=2
    assert [c["chunk_index"] for c in match["context_before"]] == [0]
    assert [c["chunk_index"] for c in match["context_after"]] == [2, 3]


def test_filters_source_type_transcription_id_days(db_path):
    today = datetime.now().strftime("%Y-%m-%d")
    old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    with get_db_connection(db_path) as conn:
        tid_tg = _add_transcription(
            conn, source_type="telegram", meeting_date=today,
            tg_chat_title="Робочий чат", tg_link="https://t.me/x/1",
        )
        _add_chunk(conn, tid_tg, 0, "MARKER у Telegram-повідомленні")

        tid_meeting = _add_transcription(conn, source_type="meeting", meeting_date=today)
        _add_chunk(conn, tid_meeting, 0, "MARKER на зустрічі")

        tid_old = _add_transcription(conn, source_type="meeting", meeting_date=old_date)
        _add_chunk(conn, tid_old, 0, "MARKER у старій зустрічі")
        conn.commit()

    only_tg = archive_grep.grep(db_path, "MARKER", source_type="telegram")
    assert only_tg["count"] == 1
    assert only_tg["matches"][0]["transcription_id"] == tid_tg
    assert only_tg["matches"][0]["tg_chat_title"] == "Робочий чат"

    only_meeting_tid = archive_grep.grep(db_path, "MARKER", transcription_id=tid_meeting)
    assert only_meeting_tid["count"] == 1
    assert only_meeting_tid["matches"][0]["transcription_id"] == tid_meeting

    recent_only = archive_grep.grep(db_path, "MARKER", days=30)
    found_ids = {m["transcription_id"] for m in recent_only["matches"]}
    assert tid_old not in found_ids
    assert tid_tg in found_ids and tid_meeting in found_ids


def test_limit_truncates_and_marks_truncated(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        for i in range(5):
            _add_chunk(conn, tid, i, f"чанк {i} з MARKER")
        conn.commit()

    result = archive_grep.grep(db_path, "MARKER", limit=2)
    assert len(result["matches"]) == 2
    assert result["truncated"] is True


def test_empty_result_is_not_an_exception(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        _add_chunk(conn, tid, 0, "нічого спільного тут немає")
        conn.commit()

    result = archive_grep.grep(db_path, "НІКОЛИНЕЗУСТРІНЕТЬСЯ-999")
    assert result == {
        "pattern": "НІКОЛИНЕЗУСТРІНЕТЬСЯ-999",
        "regex": False,
        "count": 0,
        "match_total": 0,
        "scanned": 1,
        "truncated": False,
        "truncated_reason": None,
        "matches": [],
        "chunk_boundary_caveat": archive_grep._CHUNK_BOUNDARY_CAVEAT,
        "arbitrary_scan_caveat": None,
    }


def test_cyrillic_case_insensitive_literal_search(db_path):
    # знахідка 1 (критична): SQLite lower() згортає лише ASCII — цей тест
    # ловить саме те, чого не було в оригінальних ASCII-тестах.
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        _add_chunk(conn, tid, 0, "Вчора дзвонив Андрій щодо оплати.")
        _add_chunk(conn, tid, 1, "Їжа була готова, Європа далеко, історія довга.")
        conn.commit()

    lower_needle = archive_grep.grep(db_path, "андрій")
    assert lower_needle["count"] == 1
    assert "Андрій" in lower_needle["matches"][0]["excerpt"]

    upper_needle = archive_grep.grep(db_path, "АНДРІЙ")
    assert upper_needle["count"] == 1

    for needle in ("ї", "є", "і"):
        result = archive_grep.grep(db_path, needle)
        assert result["count"] >= 1, f"needle {needle!r} має знайти чанк 1"


def test_literal_and_regex_agree_under_ignore_case(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        _add_chunk(conn, tid, 0, "Андрій підтвердив бюджет.")
        tid2 = _add_transcription(conn, meeting_date="2026-08-02")
        _add_chunk(conn, tid2, 0, "андрій запізнюється.")
        tid3 = _add_transcription(conn, meeting_date="2026-08-03")
        _add_chunk(conn, tid3, 0, "тут його нема.")
        conn.commit()

    import re as _re

    literal = archive_grep.grep(db_path, "андрій", ignore_case=True)
    regex_ = archive_grep.grep(db_path, _re.escape("андрій"), regex=True, ignore_case=True)

    literal_ids = {m["transcription_id"] for m in literal["matches"]}
    regex_ids = {m["transcription_id"] for m in regex_["matches"]}
    assert literal_ids == regex_ids == {tid, tid2}


def test_empty_or_blank_pattern_raises(db_path):
    with pytest.raises(ValueError):
        archive_grep.grep(db_path, "")
    with pytest.raises(ValueError):
        archive_grep.grep(db_path, "   ")


def test_excerpt_window_uses_actual_match_not_pattern_length(db_path):
    # знахідка 6: вікно уривка мало рахуватись по len(pattern) (сирому, до
    # casefold), а не по фактичному збігу — тест ловить розбіжність довжини.
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        text = "х" * 90 + "Андрій" + "y" * 90
        _add_chunk(conn, tid, 0, text)
        conn.commit()

    result = archive_grep.grep(db_path, "андрій")
    excerpt = result["matches"][0]["excerpt"]
    assert "Андрій" in excerpt
    # уривок обрізаний навколо фактичного 6-символьного збігу з обох боків
    assert excerpt.startswith("…")
    assert excerpt.endswith("…")


def test_null_meeting_date_sorts_last_without_crashing(db_path):
    # знахідка 1 (Plan deltas D2): C1 оголошує meeting_date: str | None, Python
    # не порівнює None зі str — рядок без дати (і без meeting_date, і без
    # created_at, що теоретично неможливо, але COALESCE тут не рятує) не
    # повинен ронять сортування, а має йти останнім.
    with get_db_connection(db_path) as conn:
        tid_null = _add_transcription(conn, meeting_date=None)
        conn.execute("UPDATE transcriptions SET created_at = NULL WHERE id = ?", (tid_null,))
        _add_chunk(conn, tid_null, 0, "чанк без дати з MARKER")

        tid_new = _add_transcription(conn, meeting_date="2026-08-02")
        _add_chunk(conn, tid_new, 0, "чанк новіший з MARKER")

        tid_old = _add_transcription(conn, meeting_date="2026-08-01")
        _add_chunk(conn, tid_old, 0, "чанк старіший з MARKER")
        conn.commit()

    result = archive_grep.grep(db_path, "MARKER", limit=20)
    assert result["count"] == 3
    ids_in_order = [m["transcription_id"] for m in result["matches"]]
    assert ids_in_order == [tid_new, tid_old, tid_null]
    assert result["matches"][-1]["meeting_date"] is None


def test_arbitrary_scan_caveat_present_only_on_early_stop(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        for i in range(5):
            _add_chunk(conn, tid, i, f"чанк {i} з MARKER")
        conn.commit()

    # зупинка лише на `limit` (весь скан завершено) — довільності немає.
    limited = archive_grep.grep(db_path, "MARKER", limit=2)
    assert limited["truncated_reason"] == "limit"
    assert limited["arbitrary_scan_caveat"] is None

    # зупинка на max_scan — скан достроковий, підмножина довільна щодо дати.
    scan_capped = archive_grep.grep(db_path, "MARKER", max_scan=2, limit=20)
    assert scan_capped["truncated_reason"] == "max_scan"
    assert scan_capped["arbitrary_scan_caveat"]


def test_max_scan_bounds_match_function_calls(db_path, monkeypatch):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        for i in range(50):
            _add_chunk(conn, tid, i, f"чанк {i} з MARKER")
        conn.commit()

    calls = {"n": 0}
    original = archive_grep._match_row

    def _counting_match_row(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(archive_grep, "_match_row", _counting_match_row)

    result = archive_grep.grep(db_path, "MARKER", max_scan=5, limit=20)
    assert result["truncated"] is True
    assert result["truncated_reason"] == "max_scan"
    assert result["scanned"] <= 5
    assert calls["n"] <= 5


def test_catastrophic_regex_stops_at_deadline_not_hang(db_path, monkeypatch):
    # знахідка 3: patern з катастрофічним бектрекінгом не має права повісити
    # виклик — дедлайн перевіряється між рядками. Дуже маленький дедлайн +
    # кілька рядків, кожен з яких окремо помітно повільний (але не
    # нескінченний), доводять, що скан зупиняється достроково.
    monkeypatch.setattr(archive_grep, "_DEFAULT_DEADLINE_SECONDS", 0.3)

    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        slow_text = "a" * 22 + "X"  # (a+)+$ на цьому рядку помітно повільний
        for i in range(30):
            _add_chunk(conn, tid, i, slow_text)
        conn.commit()

    import concurrent.futures

    # НЕ `with ThreadPoolExecutor(...) as pool:` — його __exit__ кличе
    # shutdown(wait=True) і чекає на потік, що застряг у бектрекінгу, тобто
    # регресія все одно зависає, лише за TimeoutError замість __exit__.
    # shutdown(wait=False) не блокує тест, навіть якщо regex-потік ще живий.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(archive_grep.grep, db_path, r"(a+)+$", regex=True, max_scan=1000)
        # якщо дедлайн-перевірка між рядками регресує, це впаде по таймауту
        # замість того, щоб зависнути назавжди.
        result = future.result(timeout=15)
    finally:
        pool.shutdown(wait=False)

    assert result["truncated"] is True
    assert result["truncated_reason"] == "deadline"
    # зупинились задовго до того, як переглянули всі 30 рядків
    assert result["scanned"] < 30


def test_max_scan_truncates_before_limit(db_path):
    with get_db_connection(db_path) as conn:
        tid = _add_transcription(conn, meeting_date="2026-08-01")
        for i in range(50):
            _add_chunk(conn, tid, i, f"чанк {i} з MARKER")
        conn.commit()

    result = archive_grep.grep(db_path, "MARKER", max_scan=5, limit=20)
    assert result["truncated"] is True
    assert result["scanned"] <= 5
