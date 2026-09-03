"""Поглиблені тести evals/tasks_eval.py (eval-gate-03, ремонт eval-gate-06) —
due_accuracy/owner_accuracy на реалістичному наборі (кирилиця в різному
регістрі, аліас власника, due_raw=None), пороги --gate, порожній labeled,
дедуп build_tasks_golden --merge, і ремонтні критерії раунду 1 рев'ю:
неcтверджена правда не рахується (знахідка 9), стейл-рядки виключаються
(знахідка 11), нечитний JSONL дає exit 2 в ОБОХ CLI (знахідка 3/D3), дублі
id не згортаються мовчки (знахідка 16), `parse_due(meeting_date=None)` не
підставляє сьогодні мовчки (знахідка 17), first-name fallback лінкера
віддзеркалений (знахідка 25), `source='comment'` виключений постійно (D14).

Синтетична SQLite (мінімальні таблиці transcriptions/action_items/entities/
entity_aliases), commitments.parse_due — реальний виклик (чиста функція,
не торкається БД/мережі). Смок-тести цих же CLI вже є в test_build_golden.py
(eval-gate-02) — тут глибша перевірка саме математики accuracy.

`evals.golden_io.read_jsonl` — спільний читач історії 05 (D13); якщо він ще
не приземлився на диск, увесь цей файл падає на імпорті (ImportError) — це
очікувано під час паралельної хвилі, не помилка цього файлу.
"""
import json
import os
import sqlite3

import pytest

from evals import build_tasks_golden, tasks_eval
from evals.graph_links import open_readonly


# ============================================================
# Фікстури
# ============================================================

def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transcriptions (id INTEGER PRIMARY KEY, source_type TEXT, "
                 "meeting_date TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE action_items (id INTEGER PRIMARY KEY, transcription_id INTEGER, "
                 "task TEXT, owner_name TEXT, owner_entity_id INTEGER, due TEXT, source TEXT)")
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, canonical_name TEXT)")
    conn.execute("CREATE TABLE entity_aliases (id INTEGER PRIMARY KEY, entity_id INTEGER, alias TEXT)")
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "snap.db")
    _make_db(path)
    return path


def _tasks_golden_jsonl(tmp_path, items, name="tasks.local.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                     encoding="utf-8")
    return str(path)


# ============================================================
# 6 пунктів із відомою правдою: кирилиця в різному регістрі, аліас
# власника, due_raw=None, а також свідомі промахи due/owner.
# ============================================================

def _seed_six_items_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO transcriptions (id, source_type, meeting_date, created_at) "
                 "VALUES (100, 'file', '2026-06-01', '2026-06-01T10:00:00')")
    conn.execute("INSERT INTO entities (id, canonical_name) VALUES (1, 'Андрій')")
    conn.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (1, 'Andrew')")
    rows = [
        # (id, owner_entity_id, due, task)
        (10, 1, "2026-06-05", "задача A"),
        (11, 1, "2026-06-05", "задача B"),
        (12, 1, None, "задача C"),
        (13, None, "2026-06-05", "задача D"),
        (14, 1, "2026-06-05", "задача E"),
        (15, 1, "2026-06-05", "задача F (unlabeled)"),
    ]
    for aid, owner_eid, due, task in rows:
        conn.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                     "owner_entity_id, due, source) VALUES (?, 100, ?, NULL, ?, ?, NULL)",
                     (aid, task, owner_eid, due))
    conn.commit()
    conn.close()


def _six_items():
    """due_accuracy = 3/5 (A,C,E ok; B,D wrong); owner_accuracy = 4/5
    (A,B,C,D ok; E wrong) — рахунок нижче в асертах, не тут. `task` у кожному
    пункті збігається зі значенням у фікстурі БД — жоден не стейл."""
    return [
        {  # A: кирилиця точний регістр, дедлайн збігається
            "id": "task-A", "action_item_id": 10, "transcription_id": 100, "task": "задача A",
            "owner_name_raw": "Андрій", "due_raw": "2026-06-05", "meeting_date": "2026-06-01",
            "source": None, "truth": {"owner": "Андрій", "due_date": "2026-06-05", "due_precision": "day"},
            "status": "labeled",
        },
        {  # B: кирилиця ІНШИЙ регістр (owner ok), дедлайн НЕ збігається (owner ok, due fail)
            "id": "task-B", "action_item_id": 11, "transcription_id": 100, "task": "задача B",
            "owner_name_raw": "Андрій", "due_raw": "2026-06-05", "meeting_date": "2026-06-01",
            "source": None, "truth": {"owner": "АНДРІЙ", "due_date": "2026-01-01", "due_precision": "day"},
            "status": "labeled",
        },
        {  # C: due_raw=None (обидва None -> ok), власник через латинський аліас у ІНШОМУ регістрі
            "id": "task-C", "action_item_id": 12, "transcription_id": 100, "task": "задача C",
            "owner_name_raw": None, "due_raw": None, "meeting_date": "2026-06-01",
            "source": None, "truth": {"owner": "ANDREW", "due_date": None, "due_precision": None},
            "status": "labeled",
        },
        {  # D: власник не звʼязаний (owner_entity_id=NULL) і truth теж None -> owner ok;
           # дедлайн НЕ збігається -> due fail; truth не дефолтний (due_date заповнено) -> стверджено
            "id": "task-D", "action_item_id": 13, "transcription_id": 100, "task": "задача D",
            "owner_name_raw": None, "due_raw": "2026-06-05", "meeting_date": "2026-06-01",
            "source": None, "truth": {"owner": None, "due_date": "2099-01-01", "due_precision": "day"},
            "status": "labeled",
        },
        {  # E: дедлайн збігається, власник НЕ збігається (truth — стороння людина)
            "id": "task-E", "action_item_id": 14, "transcription_id": 100, "task": "задача E",
            "owner_name_raw": "Андрій", "due_raw": "2026-06-05", "meeting_date": "2026-06-01",
            "source": None, "truth": {"owner": "Хтось Інший", "due_date": "2026-06-05", "due_precision": "day"},
            "status": "labeled",
        },
        {  # F: unlabeled -> не входить у accuracy
            "id": "task-F", "action_item_id": 15, "transcription_id": 100, "task": "задача F (unlabeled)",
            "owner_name_raw": "Андрій", "due_raw": "2026-06-05", "meeting_date": "2026-06-01",
            "source": None, "truth": {"owner": None, "due_date": None, "due_precision": None},
            "status": "unlabeled",
        },
    ]


def test_due_and_owner_accuracy_across_six_items(db):
    _seed_six_items_db(db)
    items = _six_items()
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate(items, conn)
    finally:
        conn.close()

    assert result["n_total"] == 6
    assert result["n_labeled"] == 5  # task-F (unlabeled) виключена
    assert result["n_scored"] == 5   # усі 5 стверджені і свіжі
    assert result["due_accuracy"] == 0.6   # A,C,E ok; B,D fail -> 3/5
    assert result["owner_accuracy"] == 0.8  # A,B,C,D ok; E fail -> 4/5
    miss_ids = {m["id"] for m in result["misses"]}
    assert miss_ids == {"task-B", "task-D", "task-E"}


def test_gate_threshold_boundary_pass_at_exact_and_fail_just_above(tmp_path, db):
    """--min-*-acc рівно на порахованому значенні -> PASS (>=); на волосок
    вище -> FAIL. Порахований набір: due=0.6, owner=0.8."""
    _seed_six_items_db(db)
    golden = _tasks_golden_jsonl(tmp_path, _six_items())

    rc_pass = tasks_eval.main(["--golden", golden, "--db", db, "--gate",
                                "--min-due-acc", "0.6", "--min-owner-acc", "0.8"])
    assert rc_pass == 0

    rc_fail = tasks_eval.main(["--golden", golden, "--db", db, "--gate",
                                "--min-due-acc", "0.61", "--min-owner-acc", "0.8"])
    assert rc_fail == 1


def test_tasks_eval_empty_labeled_exits_2(tmp_path, db):
    _seed_six_items_db(db)
    only_unlabeled = [it for it in _six_items() if it["id"] == "task-F"]
    golden = _tasks_golden_jsonl(tmp_path, only_unlabeled)
    rc = tasks_eval.main(["--golden", golden, "--db", db])
    assert rc == 2


# ============================================================
# Знахідка 9 / D7: неcтверджена правда не рахується за влучання
# ============================================================

def test_unfilled_truth_after_status_flip_is_not_a_hit(db):
    """Пункт, чий `truth` лишився дефолтним блоком будівника, а `status`
    просто перемкнули на `labeled` — не зараховується. `None == None` не є
    влучанням."""
    _seed_six_items_db(db)
    item = {
        "id": "task-flip", "action_item_id": 10, "transcription_id": 100, "task": "задача A",
        "owner_name_raw": "Андрій", "due_raw": "2026-06-05", "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": None, "due_date": None, "due_precision": None},
        "status": "labeled",  # перемкнули статус, truth не чіпали
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn)
    finally:
        conn.close()

    assert result["n_labeled"] == 1
    assert result["n_scored"] == 0
    assert result["n_unasserted"] == 1
    assert result["due_accuracy"] is None
    assert result["owner_accuracy"] is None


def test_unfilled_truth_file_does_not_pass_gate(tmp_path, db):
    """Той самий сценарій через CLI: файл, якому лише перемкнули `status`,
    не проходить `--gate` (і не видає тиху високу точність)."""
    _seed_six_items_db(db)
    items = [{
        "id": f"task-flip-{i}", "action_item_id": aid, "transcription_id": 100, "task": task,
        "owner_name_raw": "Андрій", "due_raw": "2026-06-05", "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": None, "due_date": None, "due_precision": None},
        "status": "labeled",
    } for i, (aid, task) in enumerate([(10, "задача A"), (11, "задача B"), (14, "задача E")])]
    golden = _tasks_golden_jsonl(tmp_path, items)

    rc = tasks_eval.main(["--golden", golden, "--db", db, "--gate"])
    assert rc == 2  # нема на чому рахувати, не 0 (PASS) і не тиха 100%


# ============================================================
# Знахідка 11 / D7: стейл-пункти (реінджест перестворив action_items)
# ============================================================

def test_stale_row_missing_is_excluded_not_scored(db):
    _seed_six_items_db(db)
    item = {
        "id": "task-gone", "action_item_id": 999, "transcription_id": 100, "task": "задача, якої нема",
        "owner_name_raw": None, "due_raw": None, "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": "Андрій", "due_date": None, "due_precision": None},
        "status": "labeled",
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn)
    finally:
        conn.close()

    assert result["n_scored"] == 0
    assert result["n_stale"] == 1
    assert result["stale"][0]["reason"] == "row-missing"
    assert result["due_accuracy"] is None
    assert result["owner_accuracy"] is None


def test_stale_task_text_changed_is_excluded_not_scored(db):
    """`action_item_id` існує, але його `task` більше не той, під який
    писалась правда — реінджест перестворив рядок (знахідка 11)."""
    _seed_six_items_db(db)
    item = {
        "id": "task-reingest", "action_item_id": 10, "transcription_id": 100,
        "task": "ЗОВСІМ ІНША задача (текст після реінджесту не збігається)",
        "owner_name_raw": None, "due_raw": None, "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": "Андрій", "due_date": None, "due_precision": None},
        "status": "labeled",
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn)
    finally:
        conn.close()

    assert result["n_scored"] == 0
    assert result["n_stale"] == 1
    assert result["stale"][0]["reason"] == "task-text-changed"


def test_stale_snapshot_mismatch_is_excluded_not_scored(db):
    """`db_snapshot` пункту не збігається з тим, проти якого рахує
    `tasks_eval` (D7) — виключається, навіть якщо рядок і текст задачі досі
    збігаються."""
    _seed_six_items_db(db)
    item = {
        "id": "task-old-snap", "action_item_id": 10, "transcription_id": 100, "task": "задача A",
        "owner_name_raw": None, "due_raw": None, "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": "Андрій", "due_date": None, "due_precision": None},
        "status": "labeled",
        "db_snapshot": {"path": "інший-знімок.db", "size": 999999},
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn, db_path=db)
    finally:
        conn.close()

    assert result["n_scored"] == 0
    assert result["n_stale"] == 1
    assert result["stale"][0]["reason"] == "snapshot-mismatch"


def test_matching_snapshot_is_scored_normally(db):
    """Той самий знімок (`db_snapshot`, записаний `build_tasks_golden`,
    збігається з поточним `--db`) — не виключається."""
    _seed_six_items_db(db)
    snap = {"path": os.path.basename(db), "size": os.path.getsize(db)}
    item = {
        "id": "task-same-snap", "action_item_id": 10, "transcription_id": 100, "task": "задача A",
        "owner_name_raw": None, "due_raw": None, "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": "Андрій", "due_date": None, "due_precision": None},
        "status": "labeled",
        "db_snapshot": snap,
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn, db_path=db)
    finally:
        conn.close()

    assert result["n_scored"] == 1
    assert result["n_stale"] == 0


# ============================================================
# Знахідка 17: parse_due(meeting_date=None) не підставляє сьогодні мовчки
# ============================================================

def test_due_raw_without_anchor_date_is_excluded_from_due_accuracy(db):
    """`meeting_date=None` з непорожньою `due_raw` — не кличемо `parse_due`
    (він би мовчки підставив `date.today()`); due не рахується, owner —
    рахується (незалежний від якоря)."""
    _seed_six_items_db(db)
    item = {
        "id": "task-no-anchor", "action_item_id": 10, "transcription_id": 100, "task": "задача A",
        "owner_name_raw": "Андрій", "due_raw": "завтра", "meeting_date": None,
        "source": None, "truth": {"owner": "Андрій", "due_date": "2026-06-06", "due_precision": "day"},
        "status": "labeled",
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn)
    finally:
        conn.close()

    assert result["n_scored"] == 1
    assert result["n_due_no_anchor"] == 1
    assert result["n_due_scored"] == 0
    assert result["due_accuracy"] is None
    assert result["n_owner_scored"] == 1
    assert result["owner_accuracy"] == 1.0


def test_due_raw_empty_without_anchor_date_is_still_scored(db):
    """`due_raw` порожня — `parse_due` повертає `(None, None)` РАНІШЕ, ніж
    торкається якоря (навіть `None`), тож викликати безпечно; рахується
    нормально, якщо `truth.due_date` теж `None`."""
    _seed_six_items_db(db)
    item = {
        "id": "task-empty-due-no-anchor", "action_item_id": 10, "transcription_id": 100, "task": "задача A",
        "owner_name_raw": "Андрій", "due_raw": None, "meeting_date": None,
        "source": None, "truth": {"owner": "Андрій", "due_date": None, "due_precision": None},
        "status": "labeled",
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn)
    finally:
        conn.close()

    assert result["n_due_no_anchor"] == 0
    assert result["n_due_scored"] == 1
    assert result["due_accuracy"] == 1.0


# ============================================================
# Знахідка 25: first-name fallback лінкера (`link_owners`) віддзеркалений
# ============================================================

def test_owner_match_mirrors_linker_first_name_fallback(db):
    """`link_owners` лінкує «Юля Гончар» на однослівну сутність «Юля» (перший
    токен). Якщо труна `truth.owner` записана повним імʼям («Юля Гончар»),
    а зʼязана сутність — однослівна «Юля», це має рахуватись влучанням, не
    промахом (продакшн вважає цю лінку правильною)."""
    conn_setup = sqlite3.connect(db)
    conn_setup.execute("INSERT INTO transcriptions (id, source_type, meeting_date, created_at) "
                        "VALUES (200, 'file', '2026-06-01', '2026-06-01T10:00:00')")
    conn_setup.execute("INSERT INTO entities (id, canonical_name) VALUES (2, 'Юля')")
    conn_setup.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                        "owner_entity_id, due, source) VALUES (20, 200, 'задача Юлі', NULL, 2, NULL, NULL)")
    conn_setup.commit()
    conn_setup.close()

    item = {
        "id": "task-julia", "action_item_id": 20, "transcription_id": 200, "task": "задача Юлі",
        "owner_name_raw": "Юля Гончар", "due_raw": None, "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": "Юля Гончар", "due_date": None, "due_precision": None},
        "status": "labeled",
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn)
    finally:
        conn.close()

    assert result["owner_accuracy"] == 1.0
    assert result["misses"] == []


def test_owner_match_first_name_fallback_direction_only(db):
    """Фолбек діє лише в напрямку «truth багатослівне → сутність однослівна»
    — не навпаки і не між двома різними однослівними іменами (без цього
    перевірка стала б занадто поблажливою)."""
    conn_setup = sqlite3.connect(db)
    conn_setup.execute("INSERT INTO transcriptions (id, source_type, meeting_date, created_at) "
                        "VALUES (200, 'file', '2026-06-01', '2026-06-01T10:00:00')")
    conn_setup.execute("INSERT INTO entities (id, canonical_name) VALUES (3, 'Петро Іванов')")
    conn_setup.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                        "owner_entity_id, due, source) VALUES (21, 200, 'задача Петра', NULL, 3, NULL, NULL)")
    conn_setup.commit()
    conn_setup.close()

    item = {
        "id": "task-petro", "action_item_id": 21, "transcription_id": 200, "task": "задача Петра",
        "owner_name_raw": "Петро", "due_raw": None, "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": "Петро", "due_date": None, "due_precision": None},
        "status": "labeled",
    }
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate([item], conn)
    finally:
        conn.close()

    # truth однослівне ("петро"), сутність багатослівна ("петро іванов") —
    # НЕ той напрямок фолбеку лінкера, промах лишається промахом.
    assert result["owner_accuracy"] == 0.0


# ============================================================
# Знахідка 3 / D3: нечитний JSONL -> exit 2 у ОБОХ CLI, не exit 1/трейсбек
# ============================================================

def test_tasks_eval_malformed_jsonl_exits_2_not_1(tmp_path, db):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "task-1", "status": "labeled"}\nNOT JSON\n', encoding="utf-8")
    rc = tasks_eval.main(["--golden", str(bad), "--db", db])
    assert rc == 2


def test_tasks_eval_missing_golden_file_exits_2(db):
    rc = tasks_eval.main(["--golden", "definitely-does-not-exist.jsonl", "--db", db])
    assert rc == 2


def test_build_tasks_golden_malformed_out_with_merge_exits_2(tmp_path, db):
    bad_out = tmp_path / "bad_out.jsonl"
    bad_out.write_text('{"id": "task-0001"}\nNOT JSON\n', encoding="utf-8")
    rc = build_tasks_golden.main(["--db", db, "--out", str(bad_out), "--merge"])
    assert rc == 2


# ============================================================
# Знахідка 16: дублі id не згортаються мовчки
# ============================================================

def test_tasks_eval_duplicate_ids_exit_2(tmp_path, db):
    _seed_six_items_db(db)
    items = _six_items()[:2]
    items[1]["id"] = items[0]["id"]  # штучний дубль
    golden = _tasks_golden_jsonl(tmp_path, items)
    rc = tasks_eval.main(["--golden", golden, "--db", db])
    assert rc == 2


def test_next_index_skips_gap_after_manual_deletion():
    """Ручне видалення `task-0002` з файлу не змушує наступний `--merge`
    видати `task-0003` повторно — `len(existing)+1` (стара логіка) саме так
    і зробив би (знахідка 16)."""
    existing = [{"id": "task-0001"}, {"id": "task-0003"}]
    assert build_tasks_golden._next_index(existing) == 4


def test_next_index_empty_existing_starts_at_one():
    assert build_tasks_golden._next_index([]) == 1


# ============================================================
# build_tasks_golden --merge — без дублів id/action_item_id, з провенансом
# ============================================================

def test_build_tasks_golden_merge_twice_yields_no_duplicate_ids(tmp_path, db):
    _seed_six_items_db(db)
    out = tmp_path / "tasks_merge.local.jsonl"

    rc1 = build_tasks_golden.main(["--db", db, "--out", str(out), "--n", "6", "--merge"])
    assert rc1 == 0
    items1 = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

    rc2 = build_tasks_golden.main(["--db", db, "--out", str(out), "--n", "6", "--merge"])
    assert rc2 == 0
    items2 = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

    ids2 = [it["action_item_id"] for it in items2]
    assert len(ids2) == len(set(ids2))
    ids_field2 = [it["id"] for it in items2]
    assert len(ids_field2) == len(set(ids_field2))
    assert len(items2) == len(items1)  # той самий детермінований набір, нового нема


def test_build_tasks_golden_writes_db_snapshot_provenance(tmp_path, db):
    _seed_six_items_db(db)
    out = tmp_path / "tasks_snap.local.jsonl"
    rc = build_tasks_golden.main(["--db", db, "--out", str(out), "--n", "6"])
    assert rc == 0
    items = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert items
    for it in items:
        assert it.get("db_snapshot", {}).get("size") == os.path.getsize(db)


# ============================================================
# D14: source='comment' виключено з вибірки постійно
# ============================================================

def test_sample_action_items_excludes_comment_source(db):
    conn_setup = sqlite3.connect(db)
    conn_setup.execute("INSERT INTO transcriptions (id, source_type, meeting_date, created_at) "
                        "VALUES (300, 'file', '2026-06-01', '2026-06-01T10:00:00')")
    conn_setup.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                        "owner_entity_id, due, source) VALUES (30, 300, 'коментар власника', NULL, NULL, NULL, 'comment')")
    conn_setup.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                        "owner_entity_id, due, source) VALUES (31, 300, 'звичайна задача', NULL, NULL, NULL, NULL)")
    conn_setup.commit()
    conn_setup.close()

    conn = open_readonly(db)
    try:
        picked = build_tasks_golden.sample_action_items(conn, 10)
    finally:
        conn.close()

    picked_ids = {row["id"] for row in picked}
    assert 30 not in picked_ids
    assert 31 in picked_ids
