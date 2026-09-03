"""Тести перемірної оснастки графа TG (entity-graph-tg, історія 04).

Офлайн, tmp-БД через `init_database` (той самий патерн, що
`tests/test_tg_entities.py`) — без моделей і без мережі. `evals/graph_links.py`
рахує над знімком у режимі read-only і нічого в БД не пише — головне тут:
(1) відмова відкривати файл, що збігається з бойовою БД, без `--yes-live`,
(2) exact-only/exact+morph зрізи і дельта рахуються коректно, без побічного
запису в `meeting_entities`, (3) `--json-out` стабільний для diff.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.db.migrations import init_database
from evals import graph_links


# ============================================================
# Fixtures / helpers
# ============================================================

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "snapshot.db")
    init_database(path)
    return path


def _entity(path, name, etype="person", aliases=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO entities (type, canonical_name, normalized_name) VALUES (?, ?, ?)",
        (etype, name, name.casefold()))
    eid = cur.lastrowid
    for a in aliases:
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (?, ?, ?)", (eid, a, a.casefold()))
    conn.commit()
    conn.close()
    return eid


def _msg(path, text, *, thread_id=1, chat_id=-100, msg_id=None):
    conn = sqlite3.connect(path)
    if msg_id is None:
        msg_id = (conn.execute("SELECT COALESCE(MAX(tg_message_id),0) FROM transcriptions "
                               "WHERE tg_chat_id=?", (chat_id,)).fetchone()[0] or 0) + 1
    conn.execute("INSERT OR IGNORE INTO tg_threads (id, chat_id, status) VALUES (?, ?, 'open')",
                 (thread_id, chat_id))
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, tg_chat_id, "
        "tg_message_id, tg_date, tg_thread_id) VALUES ('telegram', ?, ?, ?, ?, "
        "'2026-06-01T10:00:00+00:00', ?)",
        (f"[TG] {text[:20]}", text, chat_id, msg_id, thread_id))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _meeting_entities(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute("SELECT * FROM meeting_entities")]
    conn.close()
    return out


# ============================================================
# Відмова відкривати бойову БД
# ============================================================

def test_refuses_live_db_without_flag(db, monkeypatch, capsys):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: db)
    rc = graph_links.main(["--db", db])
    assert rc == 2
    assert "Відмова" in capsys.readouterr().err


def test_yes_live_overrides_refusal(db, monkeypatch):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: db)
    rc = graph_links.main(["--db", db, "--yes-live"])
    assert rc == 0


def test_non_live_db_runs_without_flag(db, monkeypatch):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: db + ".other")
    rc = graph_links.main(["--db", db])
    assert rc == 0


def test_missing_db_file_returns_error(tmp_path, monkeypatch, capsys):
    missing = str(tmp_path / "nope.db")
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    rc = graph_links.main(["--db", missing])
    assert rc == 2
    assert "не знайдено" in capsys.readouterr().err


def test_refuses_live_db_via_real_config_path(db, monkeypatch, capsys):
    """Відмова через справжній `config.Config` — решта тестів цього файлу
    підміняють `_live_db_path` напряму, тож реальний шлях
    `Config.BASE_DIR / Config.DATABASE` інакше не виконується жодного разу
    (ремонт історії 06)."""
    db_path = Path(db)
    monkeypatch.setattr("config.Config.BASE_DIR", db_path.parent)
    monkeypatch.setattr("config.Config.DATABASE", db_path.name)

    rc = graph_links.main(["--db", db])
    assert rc == 2
    assert "Відмова" in capsys.readouterr().err


# ============================================================
# Read-only: нічого не пишеться в meeting_entities знімка
# ============================================================

def test_does_not_write_meeting_entities(db, monkeypatch):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    _entity(db, "Адам")
    _msg(db, "сьогодні Адам підтвердив платіж")
    assert _meeting_entities(db) == []

    conn = graph_links.open_readonly(db)
    try:
        report = graph_links.build_report(conn, db)
    finally:
        conn.close()

    assert report["simulation"]["exact_only"]["total_links"] == 1
    assert _meeting_entities(db) == []  # симуляція нічого не персистить


def _journal_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    return mode


def test_snapshot_untouched_by_load_names_and_collisions(db, monkeypatch):
    """`tg_entities.load_names` і `entity_dedup.find_alias_cross_type_collisions`
    відкривають власне зʼєднання з `PRAGMA journal_mode=WAL` — на знімку в
    режимі `delete` це не повинно перекинути journal_mode файлу чи лишити
    поруч `-wal`/`-shm` (ремонт історії 06)."""
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    _entity(db, "Адам")
    _msg(db, "сьогодні Адам підтвердив платіж")

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    assert _journal_mode(db) == "delete"
    before = Path(db).read_bytes()

    rc = graph_links.main(["--db", db])
    assert rc == 0

    assert _journal_mode(db) == "delete"
    assert not Path(db + "-wal").exists()
    assert not Path(db + "-shm").exists()
    assert Path(db).read_bytes() == before


def test_readonly_connection_rejects_write(db, monkeypatch):
    conn = graph_links.open_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO entities (type, canonical_name, normalized_name) "
                         "VALUES ('person', 'x', 'x')")
    finally:
        conn.close()


# ============================================================
# Два зрізи: exact-only / exact+morph, дельта
# ============================================================

def test_exact_only_sees_capitalised_mention(db, monkeypatch):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    _entity(db, "Адам")
    _msg(db, "сьогодні Адам підтвердив платіж")

    conn = graph_links.open_readonly(db)
    try:
        report = graph_links.build_report(conn, db)
    finally:
        conn.close()

    assert report["simulation"]["exact_only"]["total_links"] == 1
    assert report["simulation"]["exact_plus_morph"]["total_links"] == 1
    assert report["simulation"]["delta"]["links"] == 0


def test_morph_slice_adds_links_exact_misses(db, monkeypatch):
    """«Юлією» — словоформа, яку точний матчер не бачить; морфо-гілка
    зіставляє її з «Юлія» за основою слова (історія 03)."""
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    eid = _entity(db, "Юлія")
    _msg(db, "домовились із Юлією на завтра")

    conn = graph_links.open_readonly(db)
    try:
        report = graph_links.build_report(conn, db)
    finally:
        conn.close()

    assert report["simulation"]["exact_only"]["total_links"] == 0
    assert report["simulation"]["exact_plus_morph"]["total_links"] == 1
    assert report["simulation"]["delta"]["links"] == 1

    top = report["diff"]
    assert top[0]["entity_id"] == eid
    assert top[0]["delta"] == 1
    assert top[0]["exact_links"] == 0
    assert top[0]["morph_links"] == 1
    assert top[0]["distinct_records"] == 1
    assert top[0]["distinct_chats"] == 1


def test_lowercase_mention_ignored_in_both_slices(db, monkeypatch):
    """«документ» як аліас проєкту — сміттєвий збіг, і морфо-гілка теж мусить
    його відкидати (той самий гард written_as_proper_noun)."""
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    _entity(db, "Стратегія розвитку області", etype="project", aliases=["документ"])
    _msg(db, "надішліть документ на пошту")

    conn = graph_links.open_readonly(db)
    try:
        report = graph_links.build_report(conn, db)
    finally:
        conn.close()

    assert report["simulation"]["exact_only"]["total_links"] == 0
    assert report["simulation"]["exact_plus_morph"]["total_links"] == 0


def test_env_var_restored_after_simulation(db, monkeypatch):
    """`_simulate` тимчасово виставляє TG_ENTITIES_MORPH_ENABLED — не повинно
    протікати назовні (ані в змінну оточення, ані в поведінку модуля)."""
    import os
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    monkeypatch.delenv("TG_ENTITIES_MORPH_ENABLED", raising=False)
    _entity(db, "Адам")
    _msg(db, "сьогодні Адам підтвердив платіж")

    conn = graph_links.open_readonly(db)
    try:
        graph_links.build_report(conn, db)
    finally:
        conn.close()

    assert "TG_ENTITIES_MORPH_ENABLED" not in os.environ


# ============================================================
# Існуючий стан графа (як записано в знімку)
# ============================================================

def test_existing_graph_state_groups_by_source(db, monkeypatch):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    eid1 = _entity(db, "Адам")
    eid2 = _entity(db, "Акме", etype="project")
    tid = _msg(db, "щось про Адама тут не при чому")

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count, source) "
                 "VALUES (?, ?, 1, NULL)", (tid, eid1))
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count, source) "
                 "VALUES (?, ?, 1, 'thread_match')", (tid, eid2))
    conn.commit()
    conn.close()

    ro = graph_links.open_readonly(db)
    try:
        state = graph_links.existing_graph_state(ro)
    finally:
        ro.close()

    assert state["links_by_source"]["NULL"] == 1
    assert state["links_by_source"]["thread_match"] == 1
    assert state["entities_with_any_link"] == 2


# ============================================================
# --json-out стабільний для diff
# ============================================================

def test_top_n_header_matches_printed_rows(db, monkeypatch, capsys):
    """Заголовок TOP-N має збігатись з кількістю фактично надрукованих
    рядків: слот у слайсі з `delta == 0` не рахується (ремонт історії 06)."""
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    _entity(db, "Адам")
    _entity(db, "Юлія")
    _msg(db, "сьогодні Адам підтвердив платіж")
    _msg(db, "домовились із Юлією на завтра")

    rc = graph_links.main(["--db", db, "--top-n", "10"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "TOP-1 за приростом" in out
    assert "Юлія" in out
    assert "Адам:" not in out


def test_json_out_carries_full_diff_regardless_of_top_n(db, monkeypatch, tmp_path):
    """`--json-out` несе повний per-entity diff незалежно від `--top-n`
    (ремонт історії 06)."""
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    names = ["Адам", "Борис", "Віктор", "Дмитро", "Юлія"]
    for name in names:
        _entity(db, name)
    for name in names:
        _msg(db, f"сьогодні {name} підтвердив платіж")

    out = tmp_path / "run.json"
    rc = graph_links.main(["--db", db, "--json-out", str(out), "--top-n", "1"])
    assert rc == 0

    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["diff"]) == len(names)


def test_json_out_is_stable_and_sorted(db, monkeypatch, tmp_path):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    _entity(db, "Адам")
    _entity(db, "Юлія")
    _msg(db, "сьогодні Адам підтвердив платіж")
    _msg(db, "домовились із Юлією на завтра")

    out1 = tmp_path / "run1.json"
    out2 = tmp_path / "run2.json"
    assert graph_links.main(["--db", db, "--json-out", str(out1)]) == 0
    assert graph_links.main(["--db", db, "--json-out", str(out2)]) == 0

    text1 = out1.read_text(encoding="utf-8")
    text2 = out2.read_text(encoding="utf-8")
    assert text1 == text2 or json.loads(text1)["generated_at"] != json.loads(text2)["generated_at"]

    data = json.loads(text1)
    # generated_at — єдине поле, якому дозволено відрізнятись між прогонами;
    # решта структури має збігатись побайтово при diff двох знімків того самого стану.
    data.pop("generated_at")
    data2 = json.loads(text2)
    data2.pop("generated_at")
    assert data == data2
    assert list(json.loads(text1).keys()) == sorted(json.loads(text1).keys())
