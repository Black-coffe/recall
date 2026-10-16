"""Tests of the TG graph re-measurement tooling (entity-graph-tg, story 04).

Offline, tmp DB via `init_database` (the same pattern as
`tests/test_tg_entities.py`), no models and no network. `evals/graph_links.py`
counts over a snapshot in read-only mode and writes nothing to the DB. The main points here:
(1) refusing to open a file that matches the live DB, without `--yes-live`,
(2) the exact-only/exact+morph slices and the delta are computed correctly, with no side
write to `meeting_entities`, (3) `--json-out` is stable for diff.
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
# Refusal to open the live DB
# ============================================================

def test_refuses_live_db_without_flag(db, monkeypatch, capsys):
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: db)
    rc = graph_links.main(["--db", db])
    assert rc == 2
    assert "Refused" in capsys.readouterr().err


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
    assert "not found" in capsys.readouterr().err


def test_refuses_live_db_via_real_config_path(db, monkeypatch, capsys):
    """Refusal through the real `config.Config`: the rest of the tests in this file
    replace `_live_db_path` directly, so the real path
    `Config.BASE_DIR / Config.DATABASE` would otherwise never be executed
    (story 06 repair)."""
    db_path = Path(db)
    monkeypatch.setattr("config.Config.BASE_DIR", db_path.parent)
    monkeypatch.setattr("config.Config.DATABASE", db_path.name)

    rc = graph_links.main(["--db", db])
    assert rc == 2
    assert "Refused" in capsys.readouterr().err


# ============================================================
# Read-only: nothing is written to the snapshot's meeting_entities
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
    assert _meeting_entities(db) == []  # the simulation persists nothing


def _journal_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    return mode


def test_snapshot_untouched_by_load_names_and_collisions(db, monkeypatch):
    """`tg_entities.load_names` and `entity_dedup.find_alias_cross_type_collisions`
    open their own connection with `PRAGMA journal_mode=WAL`; on a snapshot in
    `delete` mode this must not flip the file's journal_mode or leave
    `-wal`/`-shm` next to it (story 06 repair)."""
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
# Two slices: exact-only / exact+morph, delta
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
    """«Юлією» is a word form the exact matcher does not see; the morph branch
    matches it to «Юлія» by the word stem (story 03)."""
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
    """«документ» as a project alias is a junk match, and the morph branch must also
    reject it (the same written_as_proper_noun guard)."""
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
    """`_simulate` temporarily sets TG_ENTITIES_MORPH_ENABLED; it must not
    leak outward (neither into the environment variable nor into the module behaviour)."""
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
# Existing state of the graph (as recorded in the snapshot)
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
# --json-out is stable for diff
# ============================================================

def test_top_n_header_matches_printed_rows(db, monkeypatch, capsys):
    """The TOP-N header must match the number of actually printed
    rows: a slot in the slice with `delta == 0` is not counted (story 06 repair)."""
    monkeypatch.setattr(graph_links, "_live_db_path", lambda: None)
    _entity(db, "Адам")
    _entity(db, "Юлія")
    _msg(db, "сьогодні Адам підтвердив платіж")
    _msg(db, "домовились із Юлією на завтра")

    rc = graph_links.main(["--db", db, "--top-n", "10"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "TOP-1 by growth" in out
    assert "Юлія" in out
    assert "Адам:" not in out


def test_json_out_carries_full_diff_regardless_of_top_n(db, monkeypatch, tmp_path):
    """`--json-out` carries the full per-entity diff regardless of `--top-n`
    (story 06 repair)."""
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
    # generated_at is the only field allowed to differ between runs;
    # the rest of the structure must match byte for byte when diffing two snapshots of the same state.
    data.pop("generated_at")
    data2 = json.loads(text2)
    data2.pop("generated_at")
    assert data == data2
    assert list(json.loads(text1).keys()) == sorted(json.loads(text1).keys())
