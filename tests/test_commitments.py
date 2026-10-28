"""Tests of commitments (Track 1, app/services/commitments.py).

The parser cases are taken from REAL `action_items.due` values of the live archive
(426 distinct phrasings; here — the bulk plus edge cases), so that the parser
is checked against what the model actually writes, not against the author's assumptions.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import commitments as cm


# Anchor — Thursday 14.05.2026 (so that «четвер» != today and the shift is visible).
ANCHOR = "2026-05-14"


@pytest.mark.parametrize("raw,expected_date,expected_prec", [
    # --- relative days ---
    ("сьогодні", "2026-05-14", cm.P_DAY),
    ("сегодня", "2026-05-14", cm.P_DAY),
    ("завтра", "2026-05-15", cm.P_DAY),
    ("післязавтра", "2026-05-16", cm.P_DAY),
    ("сьогодні-завтра", "2026-05-15", cm.P_DAY),        # range -> the later bound
    ("сьогодні ввечері", "2026-05-14", cm.P_DAY),
    ("завтра до 11:30", "2026-05-15", cm.P_DAY),
    ("сьогодні, 14:00", "2026-05-14", cm.P_DAY),
    # --- weekdays (anchor is Thursday) ---
    ("четвер", "2026-05-21", cm.P_DAY),                 # STRICTLY after the anchor
    ("п'ятниця", "2026-05-15", cm.P_DAY),
    ("до понеділка", "2026-05-18", cm.P_DAY),
    ("до четверга", "2026-05-21", cm.P_DAY),
    ("наступний вівторок", "2026-05-19", cm.P_DAY),
    ("наступна п'ятниця", "2026-05-22", cm.P_DAY),      # not tomorrow but a week later
    ("понеділок-вівторок", "2026-05-19", cm.P_DAY),
    ("вихідні", "2026-05-16", cm.P_DAY),
    # --- weeks ---
    ("цього тижня", "2026-05-15", cm.P_WEEK),           # Friday of the current one
    ("кінець тижня", "2026-05-15", cm.P_WEEK),
    ("до кінця тижня", "2026-05-15", cm.P_WEEK),
    ("наступний тиждень", "2026-05-22", cm.P_WEEK),
    ("наступного тижня", "2026-05-22", cm.P_WEEK),
    ("кінець наступного тижня", "2026-05-22", cm.P_WEEK),
    ("через тиждень", "2026-05-21", cm.P_WEEK),
    ("протягом тижня", "2026-05-21", cm.P_WEEK),
    ("через 2 тижні", "2026-05-28", cm.P_WEEK),
    ("протягом 1-2 днів", "2026-05-16", cm.P_DAY),
    # --- months / quarters ---
    ("кінець місяця", "2026-05-31", cm.P_MONTH),
    ("цього місяця", "2026-05-31", cm.P_MONTH),
    ("червень", "2026-06-30", cm.P_MONTH),
    ("січень 2026", "2026-01-31", cm.P_MONTH),          # an explicit year is respected, even in the past
    ("січень", "2027-01-31", cm.P_MONTH),               # no year and already past -> next
    ("5 січня", "2027-01-05", cm.P_DAY),
    ("21 квітня 2026", "2026-04-21", cm.P_DAY),
    ("1 квітня 2026", "2026-04-01", cm.P_DAY),
    ("до середини січня 2026", "2026-01-15", cm.P_WEEK),
    ("Q3 2026", "2026-09-30", cm.P_QUARTER),
    ("Q1 2026", "2026-03-31", cm.P_QUARTER),
    ("Q1", "2027-03-31", cm.P_QUARTER),                 # quarter in the past -> next year
    # --- absolute ---
    ("2025-03-28", "2025-03-28", cm.P_DAY),
    ("27.03.2026", "2026-03-27", cm.P_DAY),
    ("07.04.2026 (вівторок)", "2026-04-07", cm.P_DAY),
    ("2025-04", "2025-04-30", cm.P_MONTH),
    # --- classes without an exact date ---
    ("якнайшвидше", "2026-05-17", cm.P_SOON),
    ("найближчим часом", "2026-05-17", cm.P_SOON),
    ("найближчі дні", "2026-05-17", cm.P_SOON),
    ("терміново", "2026-05-17", cm.P_SOON),
    ("якомога швидше", "2026-05-17", cm.P_SOON),
    ("кілька днів", "2026-05-17", cm.P_SOON),
    ("до наступної зустрічі", None, cm.P_NEXT_MEETING),
    # --- a deadline tied to an event: there is no date by design ---
    ("після дзвінка з Acmecorp", None, cm.P_EVENT),
    ("після свят", None, cm.P_EVENT),
    ("після завтрашньої стратсесії", None, cm.P_EVENT),   # not «завтра»!
    ("до зустрічі з Толіком", None, cm.P_EVENT),
    # --- day-level/abbreviated forms ---
    ("наступний день", "2026-05-15", cm.P_DAY),
    ("до кінця наступного дня", "2026-05-15", cm.P_DAY),
    ("ранок наступного дня", "2026-05-15", cm.P_DAY),
    ("до кінця дня", "2026-05-14", cm.P_DAY),
    ("пн-вт", "2026-05-19", cm.P_DAY),
    ("з 15-го числа", "2026-05-15", cm.P_DAY),            # the nearest 15th after 14.05
    ("наступна зустріч", None, cm.P_NEXT_MEETING),
    ("до наступного дзвінка", None, cm.P_NEXT_MEETING),
    ("щомісяця", None, cm.P_RECURRING),
])
def test_parse_due_real_values(raw, expected_date, expected_prec):
    assert cm.parse_due(raw, ANCHOR) == (expected_date, expected_prec)


@pytest.mark.parametrize("raw", ["", None, "коли буде готово", "як домовились", "???"])
def test_parse_due_unrecognized(raw):
    """Unrecognized stays unrecognized — better None than an invented date."""
    assert cm.parse_due(raw, ANCHOR) == (None, None)


def test_parse_due_anchor_shifts_relative():
    """«Завтра» is counted from the MEETING date, not from today."""
    assert cm.parse_due("завтра", "2026-01-31")[0] == "2026-02-01"
    assert cm.parse_due("завтра", "2024-02-28")[0] == "2024-02-29"   # leap year


def test_parse_due_absolute_without_anchor():
    """Without an anchor absolute dates are still parsed."""
    assert cm.parse_due("2026-03-01", None) == ("2026-03-01", cm.P_DAY)


# ============================================================
# Offline passes on a temporary DB
# ============================================================

def _seed(db: str, rows: list[dict], *, meeting_date: str = "2026-05-14") -> None:
    """rows: [{task, due, owner_name, owner_entity_id?, meeting_date?}]"""
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, created_at) "
            "VALUES (1, 'recording', 'Тестова зустріч', ?, '2026-05-14 10:00:00')",
            (meeting_date,))
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, created_at) "
            "VALUES (2, 'recording', 'Стара зустріч', '2026-01-10', '2026-01-10 10:00:00')")
        for n, r in enumerate(rows):
            conn.execute(
                "INSERT INTO action_items (transcription_id, task, owner_name, "
                "owner_entity_id, due, status) VALUES (?, ?, ?, ?, ?, ?)",
                (r.get("tid", 1), r["task"], r.get("owner_name"), r.get("owner_entity_id"),
                 r.get("due"), r.get("status", "open")))
            # The text of the meeting the task came from is ALWAYS in the index in production —
            # otherwise the task itself would have nowhere to come from. Without this row the tests
            # would check a state that never occurs, and `dropped_commitments` would have
            # nothing to measure word rarity by (`_rare_terms` requires df > 0).
            conn.execute(
                "INSERT INTO chunks (transcription_id, chunk_index, text) VALUES (?, ?, ?)",
                (r.get("tid", 1), n, r.get("chunk_text", f"обговорили: {r['task']}")))
        conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "commitments.db")
    init_database(path)
    return path


def test_migration_v29_columns_exist(db: str):
    with get_db_connection(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(action_items)")}
    assert {"due_date", "due_precision", "stale_at", "dup_of"} <= cols


def test_backfill_due_is_idempotent(db: str):
    _seed(db, [
        {"task": "Надіслати договір", "due": "завтра"},
        {"task": "Перевірити модель", "due": "Q3 2026"},
        {"task": "Незрозуміле", "due": "коли буде час"},
        {"task": "Без терміну", "due": None},
    ])
    first = cm.backfill_due(db)
    assert first["parsed"] == 2 and first["unparsed"] == 1 and first["updated"] == 2

    with get_db_connection(db) as conn:
        rows = {r["task"]: (r["due_date"], r["due_precision"]) for r in
                conn.execute("SELECT task, due_date, due_precision FROM action_items")}
    assert rows["Надіслати договір"] == ("2026-05-15", cm.P_DAY)
    assert rows["Перевірити модель"] == ("2026-09-30", cm.P_QUARTER)
    assert rows["Незрозуміле"] == (None, None)

    second = cm.backfill_due(db)
    assert second["updated"] == 0, "a repeated pass must not rewrite anything"


def test_backfill_dry_run_writes_nothing(db: str):
    _seed(db, [{"task": "Надіслати договір", "due": "завтра"}])
    res = cm.backfill_due(db, dry_run=True)
    assert res["updated"] == 1 and res["samples"]
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT due_date FROM action_items").fetchone()["due_date"] is None


def test_link_owners_matches_alias(db: str):
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (10, 'person', 'Юлія', 'юлія')")
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (10, 'Юля', 'юля')")
        conn.commit()
    _seed(db, [
        {"task": "Скинути файл", "owner_name": "Юля"},
        {"task": "Підготувати звіт", "owner_name": "Юлія"},
        {"task": "Нікому", "owner_name": "Невідомий"},
    ])
    res = cm.link_owners(db)
    assert res["linked"] == 2
    with get_db_connection(db) as conn:
        linked = conn.execute(
            "SELECT COUNT(*) AS n FROM action_items WHERE owner_entity_id = 10").fetchone()["n"]
    assert linked == 2


def test_dedup_marks_repeats_keeps_latest(db: str):
    _seed(db, [
        {"task": "Підготувати фінансову модель Ковальчуки", "owner_name": "Адам", "tid": 2},
        {"task": "Підготувати фінансову модель Ковальчуки повністю", "owner_name": "Адам", "tid": 1},
        {"task": "Зовсім інша задача про маркетинг", "owner_name": "Адам"},
    ])
    res = cm.dedup(db, threshold=0.6)
    assert res["groups"] == 1 and res["marked"] == 1
    with get_db_connection(db) as conn:
        dup = conn.execute("SELECT id, task, dup_of FROM action_items "
                           "WHERE dup_of IS NOT NULL").fetchone()
        keep = conn.execute("SELECT id, transcription_id FROM action_items "
                            "WHERE id = ?", (dup["dup_of"],)).fetchone()
    assert keep["transcription_id"] == 1, "the task from the fresher meeting stays canonical"


def test_mark_stale_skips_future_deadline(db: str):
    _seed(db, [
        {"task": "Стара забута", "due": "завтра", "tid": 2},
        {"task": "Стара, але з дедлайном у майбутньому", "due": "Q4 2027", "tid": 2},
        {"task": "Свіжа", "due": "завтра", "tid": 1},
    ])
    cm.backfill_due(db)
    # We fix today explicitly: otherwise the test would depend on the run date (the meeting
    # 2026-05-14 becomes "old" on its own over time).
    res = cm.mark_stale(db, days=30, today=date(2026, 5, 20))
    assert res["marked"] == 1
    with get_db_connection(db) as conn:
        stale = [r["task"] for r in conn.execute(
            "SELECT task FROM action_items WHERE status = 'stale'")]
    assert stale == ["Стара забута"]


def test_list_commitments_windows(db: str):
    """Windows are counted from the passed 'today' — the test does not depend on the run date."""
    _seed(db, [
        {"task": "Цього тижня", "due": "2026-05-14", "owner_name": "Андрій"},
        {"task": "Наступного тижня", "due": "2026-05-19", "owner_name": "Юлія"},
        {"task": "Протерміноване", "due": "2026-05-01", "owner_name": "Андрій"},
        {"task": "Без дати", "due": "коли завгодно", "owner_name": "Андрій"},
    ])
    cm.backfill_due(db)
    today = date(2026, 5, 14)   # Thursday

    def tasks(**kw):
        return [r["task"] for r in cm.list_commitments(db, today=today, **kw)]

    assert tasks(window="this_week") == ["Цього тижня"]
    assert tasks(window="next_week") == ["Наступного тижня"]
    assert tasks(window="overdue") == ["Протерміноване"]
    assert tasks(window="no_date") == ["Без дати"]
    assert tasks(window="all", owner="Юлія") == ["Наступного тижня"]


def test_overdue_shows_freshest_first(db: str):
    """A fresh overdue item on top: last year's is archaeology, often also a bad extraction."""
    _seed(db, [
        {"task": "Древня", "due": "2024-02-15"},
        {"task": "Вчорашня", "due": "2026-05-13"},
    ])
    cm.backfill_due(db)
    got = [r["task"] for r in cm.list_commitments(db, window="overdue", today=date(2026, 5, 14))]
    assert got == ["Вчорашня", "Древня"]


def test_list_commitments_hides_dups_and_deleted(db: str):
    _seed(db, [
        {"task": "Оригінал задачі про модель", "due": "2026-05-14"},
        {"task": "Оригінал задачі про модель копія", "due": "2026-05-14"},
    ])
    cm.backfill_due(db)
    cm.dedup(db, threshold=0.6)
    today = date(2026, 5, 14)
    assert len(cm.list_commitments(db, window="this_week", today=today)) == 1
    assert len(cm.list_commitments(db, window="this_week", include_dups=True, today=today)) == 2

    with get_db_connection(db) as conn:
        conn.execute("UPDATE transcriptions SET deleted_at = 1 WHERE id = 1")
        conn.commit()
    assert cm.list_commitments(db, window="this_week", today=today) == []


def test_dropped_commitments_respects_quarantine(db: str):
    """A fresh agreement is not "lost" — nothing has simply come after it yet."""
    _seed(db, [{"task": "Свіжа домовленість про сервер", "tid": 1}])   # meeting 2026-05-14
    today = date(2026, 5, 20)
    assert cm.dropped_commitments(db, days=60, min_age_days=14, today=today) == []
    got = cm.dropped_commitments(db, days=60, min_age_days=1, today=today)
    assert [r["task"] for r in got] == ["Свіжа домовленість про сервер"]


def test_list_commitments_gives_address_without_tme_link(db: str):
    """The address of a chat task is the pair (chat_id, msg_id), not just a link.

    In legacy Telegram groups a message link DOES NOT EXIST (`_chat_link`
    returns None for a chat_id without the -100 prefix) — that is some recordsin the live
    archive, working chats among them. If the digest has only `link`, a third of the
    archive has no answer to "where to write".
    """
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, "
            "created_at, tg_chat_id, tg_chat_title, tg_sender, tg_message_id, tg_link) "
            "VALUES (3, 'telegram', '[TG] Скину договір…', '2026-05-14', "
            "'2026-05-14 10:00:00', -5162514111, 'Робоча група', 'Микола', 514141, NULL)")
        conn.execute(
            "INSERT INTO action_items (transcription_id, task, owner_name, due, status, source) "
            "VALUES (3, 'Скинути договір', 'Микола', '2026-05-14', 'open', 'tg_thread')")
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) "
                     "VALUES (3, 0, 'Обіцяю скинути договір найближчим часом')")
        conn.commit()
    cm.backfill_due(db)

    got = cm.list_commitments(db, window="this_week", today=date(2026, 5, 14))
    assert len(got) == 1
    r = got[0]
    assert r["source"] == "tg_thread"
    assert (r["chat"], r["said_by"]) == ("Робоча група", "Микола")
    assert r["link"] is None                       # in a legacy group there is none...
    assert (r["chat_id"], r["msg_id"]) == (-5162514111, 514141)   # ...but the address is there

    dropped = cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 5, 20))
    assert [(d["chat_id"], d["msg_id"]) for d in dropped] == [(-5162514111, 514141)]


def test_list_commitments_call_has_no_chat_address(db: str):
    """For a call the address fields are empty — there is no address, and that is honest."""
    _seed(db, [{"task": "Щось зробити", "due": "2026-05-14"}])
    cm.backfill_due(db)
    r = cm.list_commitments(db, window="this_week", today=date(2026, 5, 14))[0]
    assert (r["source"], r["chat"], r["chat_id"], r["msg_id"]) == (None, None, None, None)


def test_weekly_digest_shape(db: str):
    _seed(db, [{"task": "Щось зробити", "due": "2026-05-14"}])
    cm.backfill_due(db)
    dig = cm.weekly_digest(db, today=date(2026, 5, 14))
    assert set(dig) == {"generated_for", "this_week", "overdue", "no_date",
                        "stuck", "dropped", "stale_topics", "coverage", "corrections",
                        "proposals"}
    assert dig["this_week"][0]["due_raw"] == "2026-05-14"


def test_digest_carries_denominator_for_dated_windows(db: str):
    """Date windows see only dated tasks — the digest must say how many there are.

    Without a denominator "this week — one" reads as the full picture, although on the
    live archive only a share of the tasks have a date: 408 of 1680. It is the same flaw as in
    `stale_topics` before the text check — the claim is true about its own layer and false
    about the archive.
    """
    _seed(db, [
        {"task": "З датою", "due": "2026-05-14"},
        {"task": "Без дати", "due": None},
        {"task": "Теж без дати", "due": ""},
    ])
    cm.backfill_due(db)
    cov = cm.weekly_digest(db, today=date(2026, 5, 14))["coverage"]

    assert cov["total"] == 3
    assert cov["dated"] == 1
    assert cov["undated"] == 2
    assert cov["undated_share"] == round(2 / 3, 3)
    assert "no_date" in cov["note"]


def test_coverage_counts_under_the_same_owner_filter(db: str):
    """The denominator is counted under the same filters, otherwise it speaks about other tasks."""
    _seed(db, [
        {"task": "Моя з датою", "due": "2026-05-14", "owner_name": "Андрій"},
        {"task": "Моя без дати", "due": None, "owner_name": "Андрій"},
        {"task": "Чужа без дати", "due": None, "owner_name": "Юлія"},
    ])
    cm.backfill_due(db)

    mine = cm.commitments_coverage(db, owner="Андрій")
    assert (mine["total"], mine["dated"], mine["undated"]) == (2, 1, 1)

    everyone = cm.commitments_coverage(db)
    assert everyone["total"] == 3


def test_coverage_counts_subtree_not_just_own_category(db: str):
    """`category_id=<direction>` counts tasks of the direction AND its projects through
    `categories.filter_sql` — otherwise `commitments_coverage` counted only the
    direction's own rows (`t.category_id = ?`), while `list_commitments`/
    `count_commitments` already expanded the tree: MCP `list_action_items(
    category_id=«Фонд»)` showed the Acmecorp project's tasks in `items`, while
    `coverage.total` was silent about them (round 2 review, item 2: on a scratch DB
    `count_commitments=1`, `commitments_coverage.total=0` on the same
    selection)."""
    with get_db_connection(db) as conn:
        fond = conn.execute(
            "SELECT id FROM categories WHERE name = 'Фонд Робота'").fetchone()[0]
        bar = conn.execute(
            "INSERT INTO categories (name, name_norm, parent_id) VALUES (?, ?, ?)",
            ("Акмекорп", "акмекорп", fond)).lastrowid
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, category_id, "
            "meeting_date, created_at) VALUES (3, 'recording', 'Акмекорп синк', ?, "
            "'2026-05-14', '2026-05-14 10:00:00')", (bar,))
        conn.execute(
            "INSERT INTO action_items (transcription_id, task, status) "
            "VALUES (3, 'Надіслати терміни раунду', 'open')")
        conn.commit()

    matched = cm.count_commitments(db, window="all", category_id=fond)
    assert matched == 1

    cov_fond = cm.commitments_coverage(db, category_id=fond)
    assert cov_fond["total"] == matched == 1

    cov_bar = cm.commitments_coverage(db, category_id=bar)
    assert cov_bar["total"] == 1

    cov_none = cm.commitments_coverage(db, category_id="none")
    assert cov_none["total"] == 0


def test_coverage_denominator_matches_tag_filter(db: str):
    """The denominator counts THE SAME set as `items`/`window_matched` (repair of
    item 1 of round 3): before this `commitments_coverage` ignored `tags`, so
    `list_action_items(tags="AI")` returned a filtered `items` next to the
    archive-wide `coverage.total`/`note` («3 of 3 tasks have no date» with one
    task having the tag) — the same lie as in the memo
    `derived-claims-need-second-source`."""
    _seed(db, [
        {"task": "З тегом ШІ", "tid": 1, "due": None},
        {"task": "Без тега перший", "tid": 2, "due": None},
        {"task": "Без тега другий", "tid": 2, "due": None},
    ])
    with get_db_connection(db) as conn:
        tag_id = conn.execute(
            "INSERT INTO tags (name, name_norm) VALUES (?, ?)", ("ШІ", "ші")).lastrowid
        conn.execute("INSERT INTO record_tags (transcription_id, tag_id) VALUES (1, ?)", (tag_id,))
        conn.commit()

    cov = cm.commitments_coverage(db, tags=["ШІ"])
    assert cov["total"] == 1
    assert cov["total"] == cm.count_commitments(db, window="all", tags=["ШІ"])
    assert "3 із 3" not in cov["note"]


def test_coverage_denominator_matches_since_until(db: str):
    """`since`/`until` narrow the denominator the same way as `count_commitments` with
    the same bounds."""
    _seed(db, [
        {"task": "Свіжа", "tid": 1, "due": None},
        {"task": "Стара перша", "tid": 2, "due": None},
        {"task": "Стара друга", "tid": 2, "due": None},
    ], meeting_date="2026-05-14")

    cov = cm.commitments_coverage(db, since="2026-05-01", until="2026-05-31")
    assert cov["total"] == 1
    assert cov["total"] == cm.count_commitments(
        db, window="all", since="2026-05-01", until="2026-05-31")


def test_coverage_tags_and_category_intersect(db: str):
    """`tags` together with `category_id` — an intersection, as in `list_commitments`."""
    _seed(db, [
        {"task": "А з тегом", "tid": 1, "due": None},
        {"task": "Б з тегом", "tid": 2, "due": None},
    ])
    with get_db_connection(db) as conn:
        cat_a = conn.execute(
            "INSERT INTO categories (name, name_norm) VALUES (?, ?)",
            ("Напрямок А", "напрямок а")).lastrowid
        conn.execute("UPDATE transcriptions SET category_id = ? WHERE id = 1", (cat_a,))
        tag_id = conn.execute(
            "INSERT INTO tags (name, name_norm) VALUES (?, ?)", ("ШІ", "ші")).lastrowid
        conn.execute("INSERT INTO record_tags (transcription_id, tag_id) VALUES (1, ?)", (tag_id,))
        conn.execute("INSERT INTO record_tags (transcription_id, tag_id) VALUES (2, ?)", (tag_id,))
        conn.commit()

    cov = cm.commitments_coverage(db, tags=["ШІ"], category_id=cat_a)
    assert cov["total"] == 1
    assert cov["total"] == cm.count_commitments(db, window="all", tags=["ШІ"], category_id=cat_a)


# ============================================================
# Truthfulness of the digest (run-in on the live archive 08.08.2026)
# ============================================================

def test_this_week_and_overdue_do_not_overlap(db: str):
    """A task cannot be both "this week" and "overdue" at once.

    The window was counted from Monday, so in a digest read on a day other than Monday the
    start of the week went into BOTH buckets: on live data 6 duplicates out of 15 rows.
    """
    _seed(db, [
        {"task": "Вівторок (вже минув)", "due": "2026-05-12"},
        {"task": "Сьогодні", "due": "2026-05-14"},
        {"task": "Субота (ще встигаємо)", "due": "2026-05-16"},
    ])
    cm.backfill_due(db)
    today = date(2026, 5, 14)                       # Thursday
    week = {r["task"] for r in cm.list_commitments(db, window="this_week", today=today)}
    over = {r["task"] for r in cm.list_commitments(db, window="overdue", today=today)}

    assert week == {"Сьогодні", "Субота (ще встигаємо)"}
    assert over == {"Вівторок (вже минув)"}
    assert not (week & over)


def test_dropped_ignores_third_party_documents(db: str):
    """"What WE dropped" does not take promises from other people's documents and videos.

    In an investor PDF the portfolio companies make the "promises". They really do not
    surface in our conversations — because they were never ours: on the live digest
    7 of 10 "lost" came from one such file.
    """
    _seed(db, [{"task": "Наша домовленість про сервер", "tid": 1}])
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, created_at) "
            "VALUES (3, 'document', 'Fund for investors Q2-2026.pdf', '2026-05-14', "
            "'2026-05-14 10:00:00')")
        conn.execute(
            "INSERT INTO action_items (transcription_id, task, owner_name, due, status) "
            "VALUES (3, 'Complete ISO 27001 certification audit', 'Northwind Vision', "
            "'Q3 2026', 'open')")
        # The text of the document itself is in the index (as in production): without it there is nothing to
        # measure word rarity by.
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) "
                     "VALUES (3, 0, 'Roadmap: complete ISO 27001 certification audit')")
        conn.commit()

    today = date(2026, 5, 20)
    ours = [r["task"] for r in cm.dropped_commitments(db, days=60, min_age_days=1, today=today)]
    assert ours == ["Наша домовленість про сервер"]

    everything = [r["task"] for r in cm.dropped_commitments(
        db, days=60, min_age_days=1, sources=["all"], today=today)]
    assert "Complete ISO 27001 certification audit" in everything


def _topic(db: str, name: str, *, tids: list[int], eid: int = 900) -> None:
    """A topic in the graph: an entity + links to meetings."""
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (?, 'project', ?, ?)", (eid, name, name.lower()))
        for tid in tids:
            conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) "
                         "VALUES (?, ?)", (tid, eid))
        conn.commit()


def test_stale_topics_verifies_silence_in_text(db: str):
    """The graph is not proof of silence: it covers 26% of Telegram records.

    A topic written about in a chat daily is "silent" for the graph from the moment it was last
    mentioned on a call. On the live archive some topicslied this way — «Робота
    silent for 36 days» with 80 mentions in the text.
    """
    _seed(db, [{"task": "Неважливо", "due": None}])
    with get_db_connection(db) as conn:
        # a fresh record the graph does not know (a typical Telegram without enrichment)
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, created_at) "
            "VALUES (3, 'telegram', '[TG] чат', '2026-05-18', '2026-05-18 10:00:00')")
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) "
                     "VALUES (3, 0, 'Резиденція — переносимо зустріч на понеділок')")
        conn.commit()
    # both topics in the graph were last seen at the old meeting (tid=2, 2026-01-10)
    _topic(db, "Резиденція", tids=[2], eid=900)
    _topic(db, "Мовчазна тема", tids=[2], eid=901)

    today = date(2026, 5, 20)
    got = {r["canonical_name"]: r for r in
           cm.stale_topics(db, days=30, min_meetings=1, today=today)}

    assert "Резиденція" not in got, "the topic sounded three days ago — it is not silent"
    assert got["Мовчазна тема"]["last_seen_source"] == "graph"

    trusting = {r["canonical_name"] for r in
                cm.stale_topics(db, days=30, min_meetings=1, today=today, verify_text=False)}
    assert "Резиденція" in trusting, "the old (trusting) behaviour stays available"


def test_link_owners_does_not_guess_by_first_name(db: str):
    """The by-name fallback takes only a single-word entity and only without namesakes.

    Enrichment gave the «Andrij Kovalenko» entity from a YouTube lecture the alias «Andrei» —
    and 43 tasks of the archive owner went under his name, because the fallback took the first
    token and any alias.
    """
    _seed(db, [
        {"task": "Задача власника", "owner_name": "Andrii Melnyk"},
        {"task": "Задача Юлії", "owner_name": "Юлія Бондаренка"},
        {"task": "Задача тезки", "owner_name": "Андрій Ткаченко"},
    ])
    with get_db_connection(db) as conn:
        conn.executemany(
            "INSERT INTO entities (id, type, canonical_name, normalized_name) VALUES (?, ?, ?, ?)",
            [(10, "person", "Andrij Kovalenko", "andrej kovalenko"),
             (11, "person", "Юлія", "юлія"),
             (12, "person", "Андрій Ткаченко", "андрій ткаченко")])
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (10, 'Andrei', 'andrei')")
        conn.commit()

    cm.link_owners(db)
    with get_db_connection(db) as conn:
        got = {r["task"]: r["owner_entity_id"] for r in
               conn.execute("SELECT task, owner_entity_id FROM action_items")}

    assert got["Задача власника"] is None, "a foreign alias must not catch the full name"
    assert got["Задача Юлії"] == 11
    assert got["Задача тезки"] == 12, "a full name match stays exact"


def test_owner_filter_sees_graph_aliases(db: str):
    """After entities are merged, all spellings of a person live as graph aliases.

    The archive owner sounds like «Андрій», «Мельник», «@johndoe», «Andrii» —
    and all their tasks are bound to one entity with the canonical «Андрій». While the
    filter looked only at the canonical and raw name, the question «що на Мельнику»
    gave zero with live data.
    """
    _seed(db, [])
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (1, 'person', 'Андрій', 'андрій')")
        conn.executemany(
            "INSERT INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (1, ?, ?)",
            [("Мельник", "мельник"), ("@johndoe", "@johndoe")])
        conn.execute(
            "INSERT INTO action_items (transcription_id, task, owner_name, owner_entity_id, "
            "due, status) VALUES (1, 'Задача власника', 'Ви', 1, '2026-05-14', 'open')")
        conn.commit()
    cm.backfill_due(db)

    def found(owner):
        return [r["task"] for r in cm.list_commitments(db, window="all", owner=owner)]

    assert found("Андрій") == ["Задача власника"]
    assert found("Мельник") == ["Задача власника"]
    assert found("johndoe") == ["Задача власника"]
    assert found("Юлія") == []


def test_list_commitments_shows_both_owner_names(db: str):
    """Next to the canonical name goes how the owner was named in the task."""
    _seed(db, [])
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (11, 'person', 'Юлія', 'юлія')")
        conn.execute(
            "INSERT INTO action_items (transcription_id, task, owner_name, owner_entity_id, "
            "due, status) VALUES (1, 'Щось зробити', 'Julia Bondarenko', 11, '2026-05-14', 'open')")
        conn.commit()
    cm.backfill_due(db)

    r = cm.list_commitments(db, window="this_week", today=date(2026, 5, 14))[0]
    assert (r["owner"], r["owner_said"]) == ("Юлія", "Julia Bondarenko")


# ============================================================
# relink_owners — re-point to a more precise person when one has appeared
# ============================================================

def test_relink_moves_a_task_to_the_person_whose_full_name_it_says(db: str):
    """`Dmytro Lebid` settled on the «Dmytro» row through the first-token fallback.

    While Lebid was not in the graph that was the best available guess. As soon as his row
    appeared the task must move, otherwise the digest shows the wrong person for years.
    """
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (10, 'person', 'Dmytro', 'dmytro')")
        conn.commit()
    _seed(db, [{"task": "Порахувати модель", "owner_name": "Dmytro Lebid"}])
    cm.link_owners(db)          # Lebid is not in the graph yet -> first-token fallback -> #10
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT owner_entity_id AS e FROM action_items").fetchone()["e"] == 10
        # Lebid's row appears later — via split or by hand.
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (11, 'person', 'Дмитро Лебідь', 'дмитро река')")
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (11, 'Dmytro Lebid', 'dmytro lebid')")
        conn.commit()

    res = cm.relink_owners(db, dry_run=False)

    assert res["relinked"] == 1
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT owner_entity_id AS e FROM action_items").fetchone()["e"] == 11
    assert cm.relink_owners(db, dry_run=False)["relinked"] == 0, "the second pass moves nothing"


def test_relink_does_not_repeat_the_first_token_guess(db: str):
    """Only the FULL spelling counts here. Otherwise re-linking would drive namesakes in circles."""
    with get_db_connection(db) as conn:
        conn.executemany(
            "INSERT INTO entities (id, type, canonical_name, normalized_name) VALUES (?, ?, ?, ?)",
            [(10, "person", "Дмитро", "дмитро"), (11, "person", "Дмитро Шевченко", "дмитро шевченко")])
        conn.commit()
    _seed(db, [{"task": "Уточнити структуру", "owner_name": "Дмитро Марченко"}])
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_items SET owner_entity_id = 10")
        conn.commit()

    res = cm.relink_owners(db, dry_run=False)

    assert res["relinked"] == 0
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT owner_entity_id AS e FROM action_items").fetchone()["e"] == 10


def test_relink_is_a_dry_run_until_asked(db: str):
    """The command moves tasks that are ALREADY linked — so an estimate only by default."""
    with get_db_connection(db) as conn:
        conn.executemany(
            "INSERT INTO entities (id, type, canonical_name, normalized_name) VALUES (?, ?, ?, ?)",
            [(10, "person", "Слава", "слава"), (11, "person", "Вячеслав", "вячеслав")])
        conn.commit()
    _seed(db, [{"task": "Зібрати фінмодель", "owner_name": "Слава"}])
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_items SET owner_entity_id = 11")
        conn.commit()

    res = cm.relink_owners(db)

    assert res["relinked"] == 1 and res["moves"]
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT owner_entity_id AS e FROM action_items").fetchone()["e"] == 11


def test_relink_dry_run_says_so_in_the_payload(db: str):
    """The estimate and the write must sound different, otherwise the operator applies nothing."""
    with get_db_connection(db) as conn:
        conn.executemany(
            "INSERT INTO entities (id, type, canonical_name, normalized_name) VALUES (?, ?, ?, ?)",
            [(10, "person", "Слава", "слава"), (11, "person", "Вячеслав", "вячеслав")])
        conn.commit()
    _seed(db, [{"task": "Зібрати фінмодель", "owner_name": "Слава"}])
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_items SET owner_entity_id = 11")
        conn.commit()

    assert cm.relink_owners(db)["dry_run"] is True
    assert cm.relink_owners(db, dry_run=False)["dry_run"] is False


def test_relink_cli_takes_dry_run_after_the_subcommand(db: str):
    """`--dry-run` after the subcommand is accepted by all its neighbours — here too, and it beats --apply."""
    with get_db_connection(db) as conn:
        conn.executemany(
            "INSERT INTO entities (id, type, canonical_name, normalized_name) VALUES (?, ?, ?, ?)",
            [(10, "person", "Слава", "слава"), (11, "person", "Вячеслав", "вячеслав")])
        conn.commit()
    _seed(db, [{"task": "Зібрати фінмодель", "owner_name": "Слава"}])
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_items SET owner_entity_id = 11")
        conn.commit()

    assert cm.main(["--db", db, "relink", "--apply", "--dry-run"]) == 0
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT owner_entity_id AS e FROM action_items").fetchone()["e"] == 11


# ============================================================
# Term selection: rarity instead of length
# ============================================================

def _later_chunk(db: str, text: str, *, tid: int = 5, meeting_date: str = "2026-06-20") -> None:
    """A later record with text — what "the topic resurfaced" is proven by."""
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO transcriptions (id, source_type, source_name, "
            "meeting_date, created_at) VALUES (?, 'recording', 'Пізніша зустріч', ?, ?)",
            (tid, meeting_date, f"{meeting_date} 10:00:00"))
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) VALUES (?, ?, ?)",
                     (tid, 0, text))
        conn.commit()


def test_rare_terms_prefer_the_name_over_the_long_verb(db: str):
    """The longest word of a task is a generic verb; a short name recognizes it."""
    _seed(db, [{"task": "Продовжувати роботу по ендавменту BUEF та стратегії"}])
    _later_chunk(db, "обговорювали стратегії розвитку і стратегії виходу знову і знову")
    with get_db_connection(db) as conn:
        got = cm._rare_terms(conn, "Продовжувати роботу по ендавменту BUEF та стратегії")
    assert "стратегії" not in got, "a frequent word cannot recognize a task"
    assert {"ендавменту", "buef"} & set(got)


def test_rare_terms_skip_words_absent_from_the_corpus(db: str):
    """Zero hits on a word that is not in the archive prove nothing."""
    _later_chunk(db, "говорили про ресепцію готелю")
    with get_db_connection(db) as conn:
        got = cm._rare_terms(conn, "Полагодити ресепцію та зламаний деліріумтременс")
    assert "деліріумтременс" not in got
    assert "ресепцію" in got


def test_dropped_sees_a_promise_that_only_a_common_word_kept_alive(db: str):
    """Regression: some taskscounted as alive through a generic word.

    «Продовжувати роботу по ендавменту BUEF» was declared resurfaced because «стратегії»
    occurs 509 times in the archive — even though BUEF was not mentioned
    a single time afterwards.
    """
    _seed(db, [{"task": "Продовжувати роботу по ендавменту BUEF та стратегії фонду"}])
    _later_chunk(db, "ми ще довго обговорювали стратегії і знову стратегії розвитку")

    got = cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 6, 25))

    assert [r["task"] for r in got] == ["Продовжувати роботу по ендавменту BUEF та стратегії фонду"]
    assert "ендавменту" in got[0]["terms"] or "buef" in got[0]["terms"]


def test_dropped_stays_silent_when_the_subject_really_came_back(db: str):
    """If the task's subject really did resurface — it is not dropped."""
    _seed(db, [{"task": "Продовжувати роботу по ендавменту BUEF та стратегії фонду"}])
    _later_chunk(db, "повернулись до ендавменту BUEF, домовились про наступний крок")

    assert cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 6, 25)) == []


def test_one_meeting_cannot_fill_the_dropped_list(db: str):
    """A course the owner listened to is also a `recording`, and its notes took 7 rows of 44.

    The source filter catches other people's PDFs but not a long session: it
    pushes other meetings out simply by being long.
    """
    _seed(db, [{"task": f"Крок уроку номер {n} про мікропроєкти", "tid": 1} for n in
               ("перший", "другий", "третій", "четвертий", "пʼятий")])

    got = cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 5, 20))

    assert len(got) == 3, "one meeting cannot fill the whole list"
    assert {r["meeting"] for r in got} == {"Тестова зустріч"}


# ============================================================
# Dropping promises whose topic went quiet
# ============================================================

def test_sweep_marks_only_the_silent_promise(db: str):
    """We drop what is no longer talked about; what resurfaced stays."""
    _seed(db, [
        {"task": "Продовжити роботу по ендавменту BUEF", "tid": 1},
        {"task": "Полагодити лінковку в Notion", "tid": 1},
    ], meeting_date="2026-05-14")
    _later_chunk(db, "повернулись до ендавменту BUEF і домовились про крок",
                 meeting_date="2026-06-01")

    res = cm.sweep_dropped(db, days=180, min_age_days=7, dry_run=False,
                           today=date(2026, 6, 20))

    assert (res["marked"], res["alive"]) == (1, 1)
    with get_db_connection(db) as conn:
        rows = {r["task"]: (r["status"], r["stale_reason"]) for r in
                conn.execute("SELECT task, status, stale_reason FROM action_items")}
    assert rows["Полагодити лінковку в Notion"] == ("stale", cm.REASON_NO_TRACE)
    assert rows["Продовжити роботу по ендавменту BUEF"] == ("open", None)


def test_sweep_dry_run_writes_nothing(db: str):
    _seed(db, [{"task": "Полагодити лінковку в Notion"}])
    res = cm.sweep_dropped(db, days=180, min_age_days=7, today=date(2026, 6, 20))
    assert res["marked"] == 1 and res["dry_run"] is True
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT status FROM action_items").fetchone()["status"] == "open"


def test_sweep_never_touches_a_future_deadline(db: str):
    """A Q3 promise has not "gone quiet" — its time simply has not come yet."""
    _seed(db, [{"task": "Полагодити лінковку в Notion", "due": "Q3 2026"}])
    cm.backfill_due(db)
    res = cm.sweep_dropped(db, days=180, min_age_days=7, dry_run=False,
                           today=date(2026, 6, 20))
    assert res["marked"] == 0
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT status FROM action_items").fetchone()["status"] == "open"


def test_sweep_respects_the_quarantine(db: str):
    """Yesterday's agreement is not "without a trace", nothing has come after it yet."""
    _seed(db, [{"task": "Полагодити лінковку в Notion"}], meeting_date="2026-06-18")
    assert cm.sweep_dropped(db, days=180, min_age_days=30,
                            today=date(2026, 6, 20))["marked"] == 0


def test_sweep_leaves_what_it_cannot_judge(db: str):
    """A task whose words are not in the index stays open and is counted separately."""
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, "
                     "meeting_date, created_at) VALUES (9, 'recording', 'Зустріч', "
                     "'2026-05-14', '2026-05-14 10:00:00')")
        conn.execute("INSERT INTO action_items (transcription_id, task, status) "
                     "VALUES (9, 'Зробити щось незрозуміле', 'open')")
        conn.commit()

    res = cm.sweep_dropped(db, days=180, min_age_days=7, dry_run=False,
                           today=date(2026, 6, 20))

    assert (res["marked"], res["unjudged"]) == (0, 1)
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT status FROM action_items").fetchone()["status"] == "open"


def test_sweep_is_idempotent(db: str):
    _seed(db, [{"task": "Полагодити лінковку в Notion"}])
    first = cm.sweep_dropped(db, days=180, min_age_days=7, dry_run=False,
                             today=date(2026, 6, 20))
    second = cm.sweep_dropped(db, days=180, min_age_days=7, dry_run=False,
                              today=date(2026, 6, 20))
    assert (first["marked"], second["marked"]) == (1, 0)


def test_stale_by_age_keeps_its_own_reason(db: str):
    """The two drop reasons must not be confused: it must be possible to roll back separately."""
    _seed(db, [{"task": "Дуже стара задача"}], meeting_date="2026-01-10")
    cm.mark_stale(db, days=90, today=date(2026, 6, 20))
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT stale_reason FROM action_items").fetchone()["stale_reason"] == cm.REASON_AGE


def test_short_name_survives_a_long_task(db: str):
    """Regression: selecting the "12 longest words" threw out short proper names.

    That is exactly what choosing by rarity was done for: in a task with a long
    wording «buef» (4 characters) did not survive to the rarity count, and the topic
    was again "proven" by generic words.
    """
    task = ("Продовжувати системну підготовку та узгодження документації щодо "
            "фінансування напрямку BUEF разом із партнерськими організаціями")
    _seed(db, [{"task": task}])
    with get_db_connection(db) as conn:
        got = cm._rare_terms(conn, task, before="2026-05-14")
    assert "buef" in got


def test_rare_terms_are_reproducible(db: str):
    """The choice must not depend on the set iteration order between runs."""
    task = "Узгодити з Notion структуру та лінковку розділів BUEF і Archicad"
    _seed(db, [{"task": task}])
    with get_db_connection(db) as conn:
        runs = {tuple(cm._rare_terms(conn, task, before="2026-05-14")) for _ in range(5)}
    assert len(runs) == 1


# --- K5 k5-task-buttons: status change with a journal and undo -------------------

def _item(db: str, task: str = "Надіслати Андрієві договір", **kw) -> int:
    _seed(db, [{"task": task, **kw}])
    with get_db_connection(db) as conn:
        return conn.execute("SELECT id FROM action_items WHERE task = ?", (task,)).fetchone()["id"]


def _state(db: str, aid: int):
    with get_db_connection(db) as conn:
        return conn.execute("SELECT status, due, due_date, due_precision FROM action_items "
                            "WHERE id = ?", (aid,)).fetchone()


def _events(db: str) -> list:
    with get_db_connection(db) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM action_item_events ORDER BY id")]


@pytest.mark.parametrize("action", ["done", "cancelled", "open"])
def test_apply_action_changes_status_and_logs_event(db: str, action: str):
    aid = _item(db, status="stale" if action == "open" else "open")
    res = cm.apply_action(db, aid, action, source="bot", actor="tg:42 @андрій")
    assert res["status"] == action and _state(db, aid)["status"] == action
    [ev] = _events(db)
    assert ev["id"] == res["event_id"] and ev["item_id"] == aid
    assert ev["source"] == "bot" and ev["actor"] == "tg:42 @андрій" and ev["at"]
    assert ev["new_status"] == action and ev["old_status"] in ("open", "stale")


def test_postpone_sets_due_date_keeps_raw_phrase_and_reopens_stale(db: str):
    aid = _item(db, due="колись потім", status="stale")
    res = cm.apply_action(db, aid, "postpone:7", source="bot", today=date(2026, 10, 15))
    st = _state(db, aid)
    assert res["due_date"] == "2026-10-22" == st["due_date"]
    assert st["status"] == "open" and st["due_precision"] == cm.P_DAY
    assert st["due"] == "колись потім"


def test_undo_restores_status_and_due_and_is_logged(db: str):
    aid = _item(db, due="завтра")
    with get_db_connection(db) as conn:
        conn.execute("UPDATE action_items SET due_date='2026-05-15', due_precision='day' "
                     "WHERE id=?", (aid,))
        conn.commit()
    ev = cm.apply_action(db, aid, "postpone:30", source="bot", today=date(2026, 10, 15))
    res = cm.undo_action(db, ev["event_id"], source="bot", actor="tg:42")
    st = _state(db, aid)
    assert (st["status"], st["due_date"], st["due_precision"]) == ("open", "2026-05-15", "day")
    assert res["status"] == "open" and _events(db)[-1]["undo_of"] == ev["event_id"]


def test_undo_only_the_last_action(db: str):
    aid = _item(db)
    first = cm.apply_action(db, aid, "done", source="bot")
    cm.apply_action(db, aid, "open", source="web")
    with pytest.raises(ValueError):
        cm.undo_action(db, first["event_id"], source="bot")
    assert _state(db, aid)["status"] == "open"


def test_undo_of_an_undo_is_refused(db: str):
    aid = _item(db)
    ev = cm.apply_action(db, aid, "done", source="bot")
    undo = cm.undo_action(db, ev["event_id"], source="bot")
    with pytest.raises(ValueError):
        cm.undo_action(db, undo["event_id"], source="bot")


def test_unknown_item_or_action_changes_nothing(db: str):
    aid = _item(db)
    with pytest.raises(LookupError):
        cm.apply_action(db, aid + 999, "done", source="bot")
    with pytest.raises(ValueError):
        cm.apply_action(db, aid, "видалити", source="bot")
    with pytest.raises(LookupError):
        cm.undo_action(db, 12345, source="bot")
    assert _state(db, aid)["status"] == "open" and _events(db) == []


def test_web_patch_goes_through_the_journal(db: str):
    from flask import Flask
    from app.blueprints.memory import memory_bp
    app = Flask(__name__)
    app.register_blueprint(memory_bp)
    app.config.update(TESTING=True, DATABASE=db)
    client = app.test_client()
    from app.core import auth
    client.environ_base["HTTP_" + auth.UI_TOKEN_HEADER.upper().replace("-", "_")] = auth.ui_token()
    aid = _item(db)
    assert client.patch(f"/api/memory/action-items/{aid}", json={"status": "done"}).status_code == 200
    assert client.patch(f"/api/memory/action-items/{aid}", json={"status": "зроблено"}).status_code == 400
    assert client.patch("/api/memory/action-items/99999", json={"status": "done"}).status_code == 404
    [ev] = _events(db)
    assert ev["source"] == "web" and ev["new_status"] == "done"
    assert _state(db, aid)["status"] == "done"


def test_migration_v51_is_idempotent(db: str):
    init_database(db)
    with get_db_connection(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(action_item_events)")}
    assert {"item_id", "at", "source", "actor", "old_status", "new_status", "undo_of"} <= cols


def test_task_action_errors_come_from_the_catalog(db: str):
    from app.core.i18n import tr
    aid = _item(db)
    with pytest.raises(ValueError) as exc:
        cm.apply_action(db, aid, "bogus", source="bot")
    assert str(exc.value) == tr("svc_tasks.err.unknownAction", lang="uk", action="bogus")
    assert tr("svc_tasks.err.unknownAction", lang="en", action="bogus") == "unknown action: bogus"
