"""Тести зобовʼязань (Трек 1, app/services/commitments.py).

Кейси парсера взяті з РЕАЛЬНИХ значень `action_items.due` живого архіву
(426 різних формулювань; тут — головна маса + межові випадки), щоб парсер
перевірявся проти того, що модель насправді пише, а не проти уявлень автора.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import commitments as cm


# Якір — четвер 14.05.2026 (щоб «четвер» ≠ сьогодні і було видно перенос).
ANCHOR = "2026-05-14"


@pytest.mark.parametrize("raw,expected_date,expected_prec", [
    # --- відносні дні ---
    ("сьогодні", "2026-05-14", cm.P_DAY),
    ("сегодня", "2026-05-14", cm.P_DAY),
    ("завтра", "2026-05-15", cm.P_DAY),
    ("післязавтра", "2026-05-16", cm.P_DAY),
    ("сьогодні-завтра", "2026-05-15", cm.P_DAY),        # діапазон → пізніша межа
    ("сьогодні ввечері", "2026-05-14", cm.P_DAY),
    ("завтра до 11:30", "2026-05-15", cm.P_DAY),
    ("сьогодні, 14:00", "2026-05-14", cm.P_DAY),
    # --- дні тижня (якір — четвер) ---
    ("четвер", "2026-05-21", cm.P_DAY),                 # строго ПІСЛЯ якоря
    ("п'ятниця", "2026-05-15", cm.P_DAY),
    ("до понеділка", "2026-05-18", cm.P_DAY),
    ("до четверга", "2026-05-21", cm.P_DAY),
    ("наступний вівторок", "2026-05-19", cm.P_DAY),
    ("наступна п'ятниця", "2026-05-22", cm.P_DAY),      # не завтра, а через тиждень
    ("понеділок-вівторок", "2026-05-19", cm.P_DAY),
    ("вихідні", "2026-05-16", cm.P_DAY),
    # --- тижні ---
    ("цього тижня", "2026-05-15", cm.P_WEEK),           # пʼятниця поточного
    ("кінець тижня", "2026-05-15", cm.P_WEEK),
    ("до кінця тижня", "2026-05-15", cm.P_WEEK),
    ("наступний тиждень", "2026-05-22", cm.P_WEEK),
    ("наступного тижня", "2026-05-22", cm.P_WEEK),
    ("кінець наступного тижня", "2026-05-22", cm.P_WEEK),
    ("через тиждень", "2026-05-21", cm.P_WEEK),
    ("протягом тижня", "2026-05-21", cm.P_WEEK),
    ("через 2 тижні", "2026-05-28", cm.P_WEEK),
    ("протягом 1-2 днів", "2026-05-16", cm.P_DAY),
    # --- місяці / квартали ---
    ("кінець місяця", "2026-05-31", cm.P_MONTH),
    ("цього місяця", "2026-05-31", cm.P_MONTH),
    ("червень", "2026-06-30", cm.P_MONTH),
    ("січень 2026", "2026-01-31", cm.P_MONTH),          # явний рік поважаємо, навіть у минулому
    ("січень", "2027-01-31", cm.P_MONTH),               # без року і вже минув → наступний
    ("5 січня", "2027-01-05", cm.P_DAY),
    ("21 квітня 2026", "2026-04-21", cm.P_DAY),
    ("1 квітня 2026", "2026-04-01", cm.P_DAY),
    ("до середини січня 2026", "2026-01-15", cm.P_WEEK),
    ("Q3 2026", "2026-09-30", cm.P_QUARTER),
    ("Q1 2026", "2026-03-31", cm.P_QUARTER),
    ("Q1", "2027-03-31", cm.P_QUARTER),                 # квартал у минулому → наступний рік
    # --- абсолютні ---
    ("2025-03-28", "2025-03-28", cm.P_DAY),
    ("27.03.2026", "2026-03-27", cm.P_DAY),
    ("07.04.2026 (вівторок)", "2026-04-07", cm.P_DAY),
    ("2025-04", "2025-04-30", cm.P_MONTH),
    # --- класи без точної дати ---
    ("якнайшвидше", "2026-05-17", cm.P_SOON),
    ("найближчим часом", "2026-05-17", cm.P_SOON),
    ("найближчі дні", "2026-05-17", cm.P_SOON),
    ("терміново", "2026-05-17", cm.P_SOON),
    ("якомога швидше", "2026-05-17", cm.P_SOON),
    ("кілька днів", "2026-05-17", cm.P_SOON),
    ("до наступної зустрічі", None, cm.P_NEXT_MEETING),
    # --- термін, привʼязаний до події: дати немає принципово ---
    ("після дзвінка з Acmecorp", None, cm.P_EVENT),
    ("після свят", None, cm.P_EVENT),
    ("після завтрашньої стратсесії", None, cm.P_EVENT),   # не «завтра»!
    ("до зустрічі з Толіком", None, cm.P_EVENT),
    # --- добові/скорочені форми ---
    ("наступний день", "2026-05-15", cm.P_DAY),
    ("до кінця наступного дня", "2026-05-15", cm.P_DAY),
    ("ранок наступного дня", "2026-05-15", cm.P_DAY),
    ("до кінця дня", "2026-05-14", cm.P_DAY),
    ("пн-вт", "2026-05-19", cm.P_DAY),
    ("з 15-го числа", "2026-05-15", cm.P_DAY),            # найближче 15-те після 14.05
    ("наступна зустріч", None, cm.P_NEXT_MEETING),
    ("до наступного дзвінка", None, cm.P_NEXT_MEETING),
    ("щомісяця", None, cm.P_RECURRING),
])
def test_parse_due_real_values(raw, expected_date, expected_prec):
    assert cm.parse_due(raw, ANCHOR) == (expected_date, expected_prec)


@pytest.mark.parametrize("raw", ["", None, "коли буде готово", "як домовились", "???"])
def test_parse_due_unrecognized(raw):
    """Нерозпізнане лишається нерозпізнаним — краще None, ніж вигадана дата."""
    assert cm.parse_due(raw, ANCHOR) == (None, None)


def test_parse_due_anchor_shifts_relative():
    """«Завтра» рахується від дати ЗУСТРІЧІ, а не від сьогодні."""
    assert cm.parse_due("завтра", "2026-01-31")[0] == "2026-02-01"
    assert cm.parse_due("завтра", "2024-02-28")[0] == "2024-02-29"   # високосний


def test_parse_due_absolute_without_anchor():
    """Без якоря абсолютні дати все одно розбираються."""
    assert cm.parse_due("2026-03-01", None) == ("2026-03-01", cm.P_DAY)


# ============================================================
# Offline-проходи на тимчасовій БД
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
            # Текст зустрічі, з якої задача взялась, у бою ЗАВЖДИ в індексі —
            # інакше нема звідки взятись і самій задачі. Без цього рядка тести
            # перевіряли б стан, якого не буває, а `dropped_commitments` не мав
            # би за чим міряти рідкість слів (`_rare_terms` вимагає df > 0).
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
    assert second["updated"] == 0, "повторний прохід не має нічого переписувати"


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
    assert keep["transcription_id"] == 1, "канонічною лишається задача зі свіжішої зустрічі"


def test_mark_stale_skips_future_deadline(db: str):
    _seed(db, [
        {"task": "Стара забута", "due": "завтра", "tid": 2},
        {"task": "Стара, але з дедлайном у майбутньому", "due": "Q4 2027", "tid": 2},
        {"task": "Свіжа", "due": "завтра", "tid": 1},
    ])
    cm.backfill_due(db)
    # today фіксуємо явно: інакше тест залежав би від дати прогону (зустріч
    # 2026-05-14 з часом сама стає «старою»).
    res = cm.mark_stale(db, days=30, today=date(2026, 5, 20))
    assert res["marked"] == 1
    with get_db_connection(db) as conn:
        stale = [r["task"] for r in conn.execute(
            "SELECT task FROM action_items WHERE status = 'stale'")]
    assert stale == ["Стара забута"]


def test_list_commitments_windows(db: str):
    """Вікна рахуються від переданого 'сьогодні' — тест не залежить від дати прогону."""
    _seed(db, [
        {"task": "Цього тижня", "due": "2026-05-14", "owner_name": "Андрій"},
        {"task": "Наступного тижня", "due": "2026-05-19", "owner_name": "Юлія"},
        {"task": "Протерміноване", "due": "2026-05-01", "owner_name": "Андрій"},
        {"task": "Без дати", "due": "коли завгодно", "owner_name": "Андрій"},
    ])
    cm.backfill_due(db)
    today = date(2026, 5, 14)   # четвер

    def tasks(**kw):
        return [r["task"] for r in cm.list_commitments(db, today=today, **kw)]

    assert tasks(window="this_week") == ["Цього тижня"]
    assert tasks(window="next_week") == ["Наступного тижня"]
    assert tasks(window="overdue") == ["Протерміноване"]
    assert tasks(window="no_date") == ["Без дати"]
    assert tasks(window="all", owner="Юлія") == ["Наступного тижня"]


def test_overdue_shows_freshest_first(db: str):
    """Свіжа прострочка вгорі: торішня — археологія, часто ще й кривий витяг."""
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
    """Свіжа домовленість не «загублена» — після неї просто ще нічого не було."""
    _seed(db, [{"task": "Свіжа домовленість про сервер", "tid": 1}])   # зустріч 2026-05-14
    today = date(2026, 5, 20)
    assert cm.dropped_commitments(db, days=60, min_age_days=14, today=today) == []
    got = cm.dropped_commitments(db, days=60, min_age_days=1, today=today)
    assert [r["task"] for r in got] == ["Свіжа домовленість про сервер"]


def test_list_commitments_gives_address_without_tme_link(db: str):
    """Адреса задачі з переписки — пара (chat_id, msg_id), а не лише посилання.

    У legacy-групах Telegram посилання на повідомлення НЕ ІСНУЄ (`_chat_link`
    повертає None для chat_id без префікса -100) — це 1453 записи з 3959 живого
    архіву, серед них робочі чати. Якщо звід має тільки `link`, на третині
    архіву відповіді «куди написати» немає.
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
    assert r["link"] is None                       # у legacy-групі його немає…
    assert (r["chat_id"], r["msg_id"]) == (-5162514111, 514141)   # …а адреса є

    dropped = cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 5, 20))
    assert [(d["chat_id"], d["msg_id"]) for d in dropped] == [(-5162514111, 514141)]


def test_list_commitments_call_has_no_chat_address(db: str):
    """Для дзвінка поля адреси порожні — там адреси немає, і це чесно."""
    _seed(db, [{"task": "Щось зробити", "due": "2026-05-14"}])
    cm.backfill_due(db)
    r = cm.list_commitments(db, window="this_week", today=date(2026, 5, 14))[0]
    assert (r["source"], r["chat"], r["chat_id"], r["msg_id"]) == (None, None, None, None)


def test_weekly_digest_shape(db: str):
    _seed(db, [{"task": "Щось зробити", "due": "2026-05-14"}])
    cm.backfill_due(db)
    dig = cm.weekly_digest(db, today=date(2026, 5, 14))
    assert set(dig) == {"generated_for", "this_week", "overdue", "no_date",
                        "dropped", "stale_topics", "coverage", "corrections"}
    assert dig["this_week"][0]["due_raw"] == "2026-05-14"


def test_digest_carries_denominator_for_dated_windows(db: str):
    """Вікна за датою бачать лише датовані задачі — звід має казати, скільки їх.

    Без знаменника «цього тижня — одна» читається як повна картина, хоча на
    живому архіві дату має частина задач: 408 із 1680. Це та сама вада, що й у
    `stale_topics` до перевірки текстом — твердження вірне про свій шар і хибне
    про архів.
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
    """Знаменник рахується під тими ж фільтрами, інакше він про чужі задачі."""
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


# ============================================================
# Правдивість зводу (обкатка на живому архіві 08.08.2026)
# ============================================================

def test_this_week_and_overdue_do_not_overlap(db: str):
    """Задача не може бути одночасно «цього тижня» і «протермінована».

    Вікно рахувалось від понеділка, тож у зводі, прочитаному не в понеділок,
    початок тижня їхав у ОБА відра: на живих даних 6 дублів із 15 рядків.
    """
    _seed(db, [
        {"task": "Вівторок (вже минув)", "due": "2026-05-12"},
        {"task": "Сьогодні", "due": "2026-05-14"},
        {"task": "Субота (ще встигаємо)", "due": "2026-05-16"},
    ])
    cm.backfill_due(db)
    today = date(2026, 5, 14)                       # четвер
    week = {r["task"] for r in cm.list_commitments(db, window="this_week", today=today)}
    over = {r["task"] for r in cm.list_commitments(db, window="overdue", today=today)}

    assert week == {"Сьогодні", "Субота (ще встигаємо)"}
    assert over == {"Вівторок (вже минув)"}
    assert not (week & over)


def test_dropped_ignores_third_party_documents(db: str):
    """«Що МИ упустили» не бере обіцянки з чужих документів і роликів.

    У PDF інвесторам «обіцянки» дають портфельні компанії. Вони справді не
    спливають у наших розмовах — бо ніколи й не були нашими: на живому зводі
    7 із 10 «загублених» приїхали з одного такого файлу.
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
        # Текст самого документа — в індексі (як у бою): без нього немає за чим
        # міряти рідкість слів.
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
    """Тема в графі: сутність + звʼязки зі зустрічами."""
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (?, 'project', ?, ?)", (eid, name, name.lower()))
        for tid in tids:
            conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) "
                         "VALUES (?, ?)", (tid, eid))
        conn.commit()


def test_stale_topics_verifies_silence_in_text(db: str):
    """Граф не є доказом мовчання: він покриває 26% Telegram-записів.

    Тема, яку щодня пишуть у чаті, для графа «замовкла» тоді, коли її востаннє
    згадали на дзвінку. На живому архіві так брехали частина тем— «Робота
    мовчить 36 днів» при 80 згадках у тексті.
    """
    _seed(db, [{"task": "Неважливо", "due": None}])
    with get_db_connection(db) as conn:
        # свіжий запис, якого граф не знає (типовий Telegram без збагачення)
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, created_at) "
            "VALUES (3, 'telegram', '[TG] чат', '2026-05-18', '2026-05-18 10:00:00')")
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) "
                     "VALUES (3, 0, 'Резиденція — переносимо зустріч на понеділок')")
        conn.commit()
    # обидві теми в графі востаннє бачені на старій зустрічі (tid=2, 2026-01-10)
    _topic(db, "Резиденція", tids=[2], eid=900)
    _topic(db, "Мовчазна тема", tids=[2], eid=901)

    today = date(2026, 5, 20)
    got = {r["canonical_name"]: r for r in
           cm.stale_topics(db, days=30, min_meetings=1, today=today)}

    assert "Резиденція" not in got, "тема звучала три дні тому — вона не мовчить"
    assert got["Мовчазна тема"]["last_seen_source"] == "graph"

    trusting = {r["canonical_name"] for r in
                cm.stale_topics(db, days=30, min_meetings=1, today=today, verify_text=False)}
    assert "Резиденція" in trusting, "стара (довірлива) поведінка лишається доступною"


def test_link_owners_does_not_guess_by_first_name(db: str):
    """Фолбек по імені бере тільки однослівну сутність і тільки без тезок.

    Сутності «Andrij Kovalenko» з ютуб-лекції збагачення видало аліас «Andrei» —
    і 43 задачі власника архіву поїхали під його імʼям, бо фолбек брав перший
    токен і будь-який аліас.
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

    assert got["Задача власника"] is None, "чужий аліас не має ловити повне імʼя"
    assert got["Задача Юлії"] == 11
    assert got["Задача тезки"] == 12, "повний збіг імені лишається точним"


def test_owner_filter_sees_graph_aliases(db: str):
    """Після злиття сутностей усі написання людини живуть аліасами графа.

    Власник архіву звучить як «Андрій», «Мельник», «@johndoe», «Andrii» —
    і всі його задачі привʼязані до однієї сутності з канонічним «Андрій». Поки
    фільтр дивився лише на канонічне і сире імʼя, питання «що на Мельнику»
    давало нуль при живих даних.
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
    """Поруч із канонічним імʼям їде те, як власника назвали в задачі."""
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
# relink_owners — перевісити на точнішу людину, коли вона зʼявилась
# ============================================================

def test_relink_moves_a_task_to_the_person_whose_full_name_it_says(db: str):
    """`Dmytro Lebid` осідав на рядку «Dmytro» через фолбек по першому токену.

    Поки Лебедя не було в графі, це був найкращий доступний здогад. Щойно рядок
    зʼявився — задача мусить переїхати, інакше свод роками показує не ту людину.
    """
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (10, 'person', 'Dmytro', 'dmytro')")
        conn.commit()
    _seed(db, [{"task": "Порахувати модель", "owner_name": "Dmytro Lebid"}])
    cm.link_owners(db)          # Лебедя в графі ще нема → фолбек по першому токену → #10
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT owner_entity_id AS e FROM action_items").fetchone()["e"] == 10
        # Рядок Лебедя зʼявляється пізніше — split'ом або руками.
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
    assert cm.relink_owners(db, dry_run=False)["relinked"] == 0, "другий прохід — без рухів"


def test_relink_does_not_repeat_the_first_token_guess(db: str):
    """Тут ходить лише ПОВНЕ написання. Інакше перевішування ганяло б тезок по колу."""
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
    """Команда рухає ВЖЕ привʼязані задачі — тому за замовчуванням лише прикидка."""
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
    """Прикидка й запис мусять звучати по-різному, інакше оператор не застосує нічого."""
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
    """`--dry-run` після підкоманди приймають усі сусіди — тут теж, і він переважає --apply."""
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
# Вибір термінів: рідкість замість довжини
# ============================================================

def _later_chunk(db: str, text: str, *, tid: int = 5, meeting_date: str = "2026-06-20") -> None:
    """Пізніший запис із текстом — те, чим доводиться «тема спливала»."""
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO transcriptions (id, source_type, source_name, "
            "meeting_date, created_at) VALUES (?, 'recording', 'Пізніша зустріч', ?, ?)",
            (tid, meeting_date, f"{meeting_date} 10:00:00"))
        conn.execute("INSERT INTO chunks (transcription_id, chunk_index, text) VALUES (?, ?, ?)",
                     (tid, 0, text))
        conn.commit()


def test_rare_terms_prefer_the_name_over_the_long_verb(db: str):
    """Найдовше слово задачі — загальне дієслово; впізнає її коротка назва."""
    _seed(db, [{"task": "Продовжувати роботу по ендавменту BUEF та стратегії"}])
    _later_chunk(db, "обговорювали стратегії розвитку і стратегії виходу знову і знову")
    with get_db_connection(db) as conn:
        got = cm._rare_terms(conn, "Продовжувати роботу по ендавменту BUEF та стратегії")
    assert "стратегії" not in got, "часте слово не може впізнавати задачу"
    assert {"ендавменту", "buef"} & set(got)


def test_rare_terms_skip_words_absent_from_the_corpus(db: str):
    """Нуль влучань по слову, якого в архіві нема, нічого не доводить."""
    _later_chunk(db, "говорили про ресепцію готелю")
    with get_db_connection(db) as conn:
        got = cm._rare_terms(conn, "Полагодити ресепцію та зламаний деліріумтременс")
    assert "деліріумтременс" not in got
    assert "ресепцію" in got


def test_dropped_sees_a_promise_that_only_a_common_word_kept_alive(db: str):
    """Регресія: частина задачвважались живими через загальне слово.

    «Продовжувати роботу по ендавменту BUEF» оголошувалась спливлою, бо в архіві
    509 разів трапляється «стратегії» — при тому, що про BUEF більше не сказали
    жодного разу.
    """
    _seed(db, [{"task": "Продовжувати роботу по ендавменту BUEF та стратегії фонду"}])
    _later_chunk(db, "ми ще довго обговорювали стратегії і знову стратегії розвитку")

    got = cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 6, 25))

    assert [r["task"] for r in got] == ["Продовжувати роботу по ендавменту BUEF та стратегії фонду"]
    assert "ендавменту" in got[0]["terms"] or "buef" in got[0]["terms"]


def test_dropped_stays_silent_when_the_subject_really_came_back(db: str):
    """Якщо предмет задачі справді спливав — це не упущене."""
    _seed(db, [{"task": "Продовжувати роботу по ендавменту BUEF та стратегії фонду"}])
    _later_chunk(db, "повернулись до ендавменту BUEF, домовились про наступний крок")

    assert cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 6, 25)) == []


def test_one_meeting_cannot_fill_the_dropped_list(db: str):
    """Прослуханий курс — теж `recording`, і його конспект займав 7 рядків із 44.

    Фільтр за джерелом ловить чужі PDF, але не ловить довгу сесію: вона
    витісняє інші зустрічі просто тим, що довга.
    """
    _seed(db, [{"task": f"Крок уроку номер {n} про мікропроєкти", "tid": 1} for n in
               ("перший", "другий", "третій", "четвертий", "пʼятий")])

    got = cm.dropped_commitments(db, days=60, min_age_days=1, today=date(2026, 5, 20))

    assert len(got) == 3, "одна зустріч не може заповнити весь список"
    assert {r["meeting"] for r in got} == {"Тестова зустріч"}


# ============================================================
# Зняття обіцянок, чия тема замовкла
# ============================================================

def test_sweep_marks_only_the_silent_promise(db: str):
    """Знімаємо те, про що більше не говорили; те, що спливало, лишається."""
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
    """Обіцянка на Q3 не «замовкла» — її час просто ще не настав."""
    _seed(db, [{"task": "Полагодити лінковку в Notion", "due": "Q3 2026"}])
    cm.backfill_due(db)
    res = cm.sweep_dropped(db, days=180, min_age_days=7, dry_run=False,
                           today=date(2026, 6, 20))
    assert res["marked"] == 0
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT status FROM action_items").fetchone()["status"] == "open"


def test_sweep_respects_the_quarantine(db: str):
    """Учорашня домовленість не «без сліду», після неї ще нічого не було."""
    _seed(db, [{"task": "Полагодити лінковку в Notion"}], meeting_date="2026-06-18")
    assert cm.sweep_dropped(db, days=180, min_age_days=30,
                            today=date(2026, 6, 20))["marked"] == 0


def test_sweep_leaves_what_it_cannot_judge(db: str):
    """Задача, чиїх слів немає в індексі, лишається відкритою і рахується окремо."""
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
    """Дві причини зняття не можна плутати: відкотити треба вміти окремо."""
    _seed(db, [{"task": "Дуже стара задача"}], meeting_date="2026-01-10")
    cm.mark_stale(db, days=90, today=date(2026, 6, 20))
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT stale_reason FROM action_items").fetchone()["stale_reason"] == cm.REASON_AGE


def test_short_name_survives_a_long_task(db: str):
    """Регресія: відбір «12 найдовших слів» викидав короткі власні назви.

    Тобто саме ті, заради яких вибір за рідкістю і робився: у задачі з довгим
    формулюванням «buef» (4 символи) не доживав до підрахунку рідкості, і тему
    знову доводили загальні слова.
    """
    task = ("Продовжувати системну підготовку та узгодження документації щодо "
            "фінансування напрямку BUEF разом із партнерськими організаціями")
    _seed(db, [{"task": task}])
    with get_db_connection(db) as conn:
        got = cm._rare_terms(conn, task, before="2026-05-14")
    assert "buef" in got


def test_rare_terms_are_reproducible(db: str):
    """Вибір не має залежати від порядку обходу множини між запусками."""
    task = "Узгодити з Notion структуру та лінковку розділів BUEF і Archicad"
    _seed(db, [{"task": task}])
    with get_db_connection(db) as conn:
        runs = {tuple(cm._rare_terms(conn, task, before="2026-05-14")) for _ in range(5)}
    assert len(runs) == 1
