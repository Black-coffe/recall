"""Smoke tests of `evals/gate.py` + `evals/snapshot.py` + `evals/golden_io.py`
(eval-gate, story 01): a synthetic DB (`app.db.migrations.init_database`),
`app.services.retrieval.search` replaced by a fake: no real
e5/GPU call, no `ANTHROPIC_API_KEY`, no network access.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories
from evals import gate, golden_io
from evals import snapshot as ev_snapshot

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# fixtures / helpers
# ============================================================

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "archive.db")
    init_database(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
        "VALUES (1, 'telegram', 'TG чат', 'x')")
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
        "VALUES (2, 'file', 'Дзвінок', 'x')")
    conn.commit()
    conn.close()
    return path


def _golden_jsonl(tmp_path, items, name="golden.jsonl"):
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return str(path)


def _item(item_id, tid, name, *, status="labeled", slice_="calls",
          category_id=None, tags=None):
    return {
        "id": item_id, "question": item_id, "slice": slice_, "status": status,
        "expected_transcription_ids": [tid],
        "expected_source_name_contains": [name],
        "category_id": category_id,
        "tags": tags or [],
    }


@pytest.fixture
def db_taxonomy(db):
    """`db` (ids 1/2, no direction/tags) + a direction->project tree and the tag `AI`
    (C8/filter_leak): #3 is inside the project «Акмекорп» of the direction «Фонд
    Робота», with the tag AI; #4 is outside the tree and has no tags (a foreign record the filter
    must not let into the results)."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
        "VALUES (3, 'file', 'Акмекорп дзвінок', 'x')")
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
        "VALUES (4, 'file', 'Чужий дзвінок', 'x')")
    conn.commit()
    # «Фонд Робота» is one of the default directions seeded by the migration itself
    # (`app/db/migrations.py`, v?? «Фонд/Особисте/AI/…»); it cannot be created a second
    # time (`_assert_free_name` would fail on duplicates), so we take the ready one.
    fund_id = conn.execute(
        "SELECT id FROM categories WHERE name = 'Фонд Робота'").fetchone()[0]
    fund = {"id": fund_id}
    project = categories.create_category(conn, "Акмекорп", parent_id=fund["id"])
    categories.set_category(conn, [3], project["id"])
    categories.set_tags(conn, 3, add=["AI"])
    conn.close()
    return {"db": db, "fund_id": fund["id"], "project_id": project["id"]}


def _leak_search(target, *, leaked=None):
    """The target is always first (recall@k=1.0); `leaked` is an optional extra chunk
    of a record the filter must not let through (C8/filter_leak). WITHOUT filler
    chunks (unlike `_good_search`): filler ids (9000+) do not exist in
    `transcriptions`, and for items with `category_id`/`tags` that would count as
    a leak (a record without a category/tags is in no filter)."""
    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        chunks = [{"transcription_id": target[0], "source_name": target[1]}]
        if leaked is not None:
            chunks.append({"transcription_id": leaked[0], "source_name": leaked[1]})
        return {"query": query, "chunks": chunks[:top_k], "vector_available": True}
    return _search


def _good_search(target_by_query):
    """The target (if set) is always first in the pool: recall@k=1.0 at any k."""
    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        target = target_by_query.get(query)
        chunks = []
        if target is not None:
            chunks.append({"transcription_id": target[0], "source_name": target[1]})
        chunks += [{"transcription_id": 9000 + i, "source_name": "filler"}
                   for i in range(max(top_k - len(chunks), 0))]
        return {"query": query, "chunks": chunks[:top_k], "vector_available": True}
    return _search


def _bad_search(db_path, query, top_k=8, category_id=None, **kwargs):
    """The target never makes it into the results: recall@k=0.0 everywhere."""
    chunks = [{"transcription_id": 9000 + i, "source_name": "filler"} for i in range(top_k)]
    return {"query": query, "chunks": chunks, "vector_available": True}


def _real_snapshot(src, dst):
    """Build a real `VACUUM INTO` snapshot (`journal_mode=delete`), the same
    path as production `evals/snapshot.py::make_snapshot`.

    Finding 4 (review-round-2): the `db` fixture after `init_database` is already in
    `journal_mode=wal`; a WAL connect to an already-WAL file touches no byte, so
    byte assertions (hash/mtime/absence of `-wal`) are vacuous on the
    fixture itself: four of the five assertions of the D16 test passed on defective
    code too. On a `delete` target the very first `get_db_connection` (which sets
    `PRAGMA journal_mode=WAL`) physically changes header bytes, so the assertions
    become load-bearing. We check the precondition right here, so the proof does not rot
    silently back into a vacuous one if the `init_database`/`make_snapshot` defaults
    ever change.
    """
    ev_snapshot.make_snapshot(src, dst)
    conn = sqlite3.connect(dst)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "delete", (
        f"the snapshot is not in journal_mode=delete (got {mode!r}): the proof is vacuous again"
    )
    return dst


# ============================================================
# snapshot.py
# ============================================================

def test_make_snapshot_leaves_src_untouched_and_copies_data(tmp_path):
    src = str(tmp_path / "src.db")
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    src_hash_before = hashlib.sha256(open(src, "rb").read()).hexdigest()
    src_mtime_before = __import__("os").path.getmtime(src)

    dst = str(tmp_path / "dst.db")
    result = ev_snapshot.make_snapshot(src, dst)
    assert result == dst

    src_hash_after = hashlib.sha256(open(src, "rb").read()).hexdigest()
    assert src_hash_after == src_hash_before
    assert __import__("os").path.getmtime(src) == src_mtime_before

    dconn = sqlite3.connect(dst)
    assert dconn.execute("SELECT v FROM t").fetchall() == [("hello",)]
    dconn.close()


def test_default_snapshot_path_format():
    p = ev_snapshot.default_snapshot_path("C:/x/whisper_history.db")
    assert "snapshots" in p.replace("\\", "/")
    assert "whisper_history-" in p
    assert p.endswith(".db")


def test_auto_snapshot_reuses_when_source_unchanged(tmp_path, monkeypatch):
    """D8/finding 10: a repeated run on an unchanged source does not create
    a second 315 MB snapshot; the same path is reused."""
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")
    src = str(tmp_path / "src.db")
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    first = ev_snapshot.auto_snapshot(src)
    second = ev_snapshot.auto_snapshot(src)
    assert first == second
    assert len(list((tmp_path / "snapshots").glob("*.db"))) == 1


def test_auto_snapshot_prunes_old_on_source_change(tmp_path, monkeypatch):
    """D8: if the source changed, the old snapshot with the same basename is removed."""
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")
    src = str(tmp_path / "src.db")
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    first = ev_snapshot.auto_snapshot(src)
    assert os.path.exists(first)

    # We mutate the source and force a different timestamp in the snapshot name, otherwise
    # a second snapshot within the same second would overwrite the first file on disk,
    # and the file mtime might not tick within the FS resolution.
    conn = sqlite3.connect(src)
    conn.execute("INSERT INTO t DEFAULT VALUES")
    conn.commit()
    conn.close()
    os.utime(src, (os.path.getmtime(src) + 5, os.path.getmtime(src) + 5))
    monkeypatch.setattr(
        ev_snapshot, "default_snapshot_path",
        lambda s: str((tmp_path / "snapshots") / (Path(s).stem + "-99999999-999999.db")))

    second = ev_snapshot.auto_snapshot(src)
    assert second != first
    assert os.path.exists(second)
    assert not os.path.exists(first)  # the old one was removed


def test_prune_other_snapshots_keeps_protected_path(tmp_path, monkeypatch):
    """D20/finding 5: a snapshot named by a LIVE baseline is not deleted
    by the pruner until the baseline is overwritten, even if it is not this run `keep`.
    On synthetic files, without real SQLite."""
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path)
    protected = tmp_path / "whisper_history-11111111-111111.db"
    protected.write_text("old")
    (tmp_path / "whisper_history-11111111-111111.db.meta.json").write_text("{}")
    new = tmp_path / "whisper_history-22222222-222222.db"
    new.write_text("new")

    ev_snapshot._prune_other_snapshots(
        "whisper_history", str(new), protect=[str(protected)])

    assert protected.exists()
    assert new.exists()


def test_prune_other_snapshots_removes_sidecars_of_pruned_snapshot(tmp_path, monkeypatch):
    """Finding 14: the pruner removes `.db` but leaves `-wal`/`-shm`/`.meta.json`
    as orphans: exactly the files now lying in `evals/snapshots/`
    from the `130113` snapshot with no `.db` next to them."""
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path)
    old = tmp_path / "whisper_history-11111111-111111.db"
    old.write_text("old")
    old_wal = tmp_path / "whisper_history-11111111-111111.db-wal"
    old_shm = tmp_path / "whisper_history-11111111-111111.db-shm"
    old_meta = tmp_path / "whisper_history-11111111-111111.db.meta.json"
    old_wal.write_text("wal")
    old_shm.write_text("shm")
    old_meta.write_text("{}")
    new = tmp_path / "whisper_history-22222222-222222.db"
    new.write_text("new")

    ev_snapshot._prune_other_snapshots("whisper_history", str(new))

    assert not old.exists()
    assert not old_wal.exists()
    assert not old_shm.exists()
    assert not old_meta.exists()


def test_prune_other_snapshots_sweeps_preexisting_orphan_sidecars(tmp_path, monkeypatch):
    """Finding 14 (orphans from the previous bug, WITHOUT a `.db` next to them): the pruner
    removes them too, rather than leaving them to accumulate forever."""
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path)
    orphan_wal = tmp_path / "whisper_history-11111111-111111.db-wal"
    orphan_shm = tmp_path / "whisper_history-11111111-111111.db-shm"
    orphan_wal.write_text("wal")
    orphan_shm.write_text("shm")
    new = tmp_path / "whisper_history-22222222-222222.db"
    new.write_text("new")

    ev_snapshot._prune_other_snapshots("whisper_history", str(new))

    assert not orphan_wal.exists()
    assert not orphan_shm.exists()


# ============================================================
# golden_io.py
# ============================================================

def test_read_golden_jsonl_valid(tmp_path):
    path = _golden_jsonl(tmp_path, [_item("a", 1, "X")])
    items = golden_io.read_golden(path)
    assert items[0]["id"] == "a"
    assert items[0]["status"] == "labeled"
    assert items[0]["slice"] == "calls"


def test_read_golden_jsonl_invalid_json_reports_line_number(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a", "question": "q"}\nnot json\n', encoding="utf-8")
    with pytest.raises(golden_io.GoldenSetError, match="line 2"):
        golden_io.read_golden(str(path))


def test_read_golden_jsonl_invalid_status(tmp_path):
    path = tmp_path / "badstatus.jsonl"
    path.write_text(json.dumps({"id": "a", "question": "q", "status": "bogus"}) + "\n",
                     encoding="utf-8")
    with pytest.raises(golden_io.GoldenSetError):
        golden_io.read_golden(str(path))


def test_read_golden_legacy_list_form(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps([
        {"id": "x", "question": "q", "expected_transcription_ids": [101]},
    ]), encoding="utf-8")
    items = golden_io.read_golden(str(path))
    assert items[0]["source"] == "legacy"
    assert items[0]["status"] == "labeled"
    assert items[0]["slice"] == "calls"  # without conn -> default


def test_read_golden_legacy_items_key_form(tmp_path):
    path = tmp_path / "legacy2.json"
    path.write_text(json.dumps({"items": [{"id": "x", "question": "q"}]}), encoding="utf-8")
    items = golden_io.read_golden(str(path))
    assert len(items) == 1
    assert items[0]["source"] == "legacy"


def test_read_golden_legacy_infers_slice_from_source_type(db, tmp_path):
    path = tmp_path / "legacy3.json"
    path.write_text(json.dumps([
        {"id": "x", "question": "q", "expected_transcription_ids": [1]},  # id=1 -> telegram in the fixture
    ]), encoding="utf-8")
    conn = sqlite3.connect(db)
    items = golden_io.read_golden(str(path), conn=conn)
    conn.close()
    assert items[0]["slice"] == "tg"


def test_golden_io_legacy_slice_lookup_broken_conn_logs_and_defaults(db, tmp_path, capsys):
    """Finding 18: a broken/mismatched conn must not quietly become slice="calls"
    without a trace in the log (CLAUDE.md: no bare except without a log)."""
    path = tmp_path / "legacy4.json"
    path.write_text(json.dumps([
        {"id": "x", "question": "q", "expected_transcription_ids": [1]},
    ]), encoding="utf-8")

    class _BrokenConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table: transcriptions")

    items = golden_io.read_golden(str(path), conn=_BrokenConn())
    assert items[0]["slice"] == "calls"
    assert "could not determine source_type" in capsys.readouterr().err


# ============================================================
# golden_io.py: read_jsonl (D13, finding 21): the shared reader for all eval CLIs
# ============================================================

def test_golden_io_read_jsonl_returns_raw_dicts(tmp_path):
    path = tmp_path / "raw.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    items = golden_io.read_jsonl(str(path))
    assert items == [{"a": 1}, {"b": 2}]


def test_golden_io_read_jsonl_skips_blank_lines(tmp_path):
    path = tmp_path / "raw2.jsonl"
    path.write_text('{"a": 1}\n\n   \n{"b": 2}\n', encoding="utf-8")
    assert golden_io.read_jsonl(str(path)) == [{"a": 1}, {"b": 2}]


def test_golden_io_read_jsonl_missing_file_raises(tmp_path):
    with pytest.raises(golden_io.GoldenSetError):
        golden_io.read_jsonl(str(tmp_path / "nope.jsonl"))


def test_golden_io_read_jsonl_invalid_json_line_reports_line_number(tmp_path):
    path = tmp_path / "bad2.jsonl"
    path.write_text('{"a":1}\nnot json\n', encoding="utf-8")
    with pytest.raises(golden_io.GoldenSetError, match="line 2"):
        golden_io.read_jsonl(str(path))


def test_golden_io_read_jsonl_non_object_line_raises(tmp_path):
    path = tmp_path / "arr.jsonl"
    path.write_text('[1, 2, 3]\n', encoding="utf-8")
    with pytest.raises(golden_io.GoldenSetError):
        golden_io.read_jsonl(str(path))


# ============================================================
# gate.py: CLI/parsing helpers
# ============================================================

def test_parse_k_list():
    assert gate._parse_k_list("8,12") == [8, 12]


def test_parse_min_recall():
    assert gate._parse_min_recall("8=0.6,12=0.75") == {8: 0.6, 12: 0.75}


# ============================================================
# gate.py: D15.3: deriving --min-recall from the calibration baseline
# ============================================================

def test_derive_min_recall_threshold_matches_rule_on_synthetic_line():
    """The rule, not re-typed numbers (D15.3): synthetic recall_at_k/n ->
    floor_0.05(recall_at_k - 2/n), computed independently of _derive_min_recall_threshold."""
    recall_at_k, n = 0.70, 20  # 0.70 - 2/20 = 0.60 -> exactly on the grid boundary
    assert gate._derive_min_recall_threshold(recall_at_k, n) == pytest.approx(0.60)

    recall_at_k, n = 0.32142857142857145, 15  # the baseline measured on 03.09 (recall@8, plan.md D15)
    expected = 0.05 * int((recall_at_k - 2 / n) / 0.05 + 1e-9)
    assert gate._derive_min_recall_threshold(recall_at_k, n) == pytest.approx(expected)
    assert gate._derive_min_recall_threshold(recall_at_k, n) == pytest.approx(0.15)


def test_derive_min_recall_threshold_floor_not_round():
    """floor_0.05, not round_0.05: 0.145 lies in the cell [0.10, 0.15), round would give
    0.15, floor must give 0.10."""
    assert gate._derive_min_recall_threshold(0.165, 100) == pytest.approx(0.10)  # 0.165-0.02=0.145 -> 0.10


def test_floor_grid_handles_float_boundary_without_epsilon_bug():
    """0.30/0.05 = 5.999999999999999 in float: without an epsilon floor drops the boundary
    value into the neighbouring cell below (0.25 instead of 0.30)."""
    assert gate._floor_grid(0.30) == pytest.approx(0.30)


# ============================================================
# gate.py: end-to-end smoke (Tracer): pass / threshold / sabotage / live DB
# ============================================================

def test_gate_pass(db, tmp_path, monkeypatch):
    items = [_item("a", 1, "TG чат", slice_="tg"), _item("b", 2, "Дзвінок")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат"), "b": (2, "Дзвінок")}))
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2,3",
                     "--min-recall", "2=0.9,3=0.9"])
    assert rc == 0


def test_gate_fails_below_threshold(db, tmp_path, monkeypatch):
    items = [_item("a", 1, "TG чат"), _item("b", 2, "Дзвінок")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _bad_search)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2,3",
                     "--min-recall", "2=0.5,3=0.5"])
    assert rc == 1


def test_gate_sabotage_reverse_flips_pass_to_fail(db, tmp_path, monkeypatch):
    items = [_item("a", 1, "TG чат"), _item("b", 2, "Дзвінок")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат"), "b": (2, "Дзвінок")}))

    rc_ok = gate.main(["--golden", golden, "--db", db, "--k", "2,3",
                        "--min-recall", "2=0.9,3=0.9"])
    assert rc_ok == 0

    rc_bad = gate.main(["--golden", golden, "--db", db, "--k", "2,3",
                         "--min-recall", "2=0.9,3=0.9", "--sabotage", "reverse"])
    assert rc_bad == 1


def test_gate_sabotage_shuffle_flips_pass_to_fail(db, tmp_path, monkeypatch):
    items = [_item("a", 1, "TG чат"), _item("b", 2, "Дзвінок")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат"), "b": (2, "Дзвінок")}))

    rc_ok = gate.main(["--golden", golden, "--db", db, "--k", "2,3",
                        "--min-recall", "2=0.9,3=0.9"])
    assert rc_ok == 0

    # The pool head (the old top-k) and the tail are shuffled INDEPENDENTLY, then the tail goes
    # before the head (D1): the target from position 0 (in the head) is guaranteed to end up
    # outside the new first k, at any k.
    rc_bad = gate.main(["--golden", golden, "--db", db, "--k", "2,3",
                         "--min-recall", "2=0.9,3=0.9", "--sabotage", "shuffle"])
    assert rc_bad == 1


def test_sabotage_pool_guarantees_displacement_at_default_k():
    """D1/finding 1: direct proof of the property at the `_sabotage_pool` level:
    none of the first k positions of the POOL stays in the first k AFTER sabotage, on
    the gate default pools (k*4 for k=8,12), for BOTH modes. The old
    `random.Random(0).shuffle` over the whole pool did not give this guarantee exactly
    at these k (finding 1): proved by a test at k=2,3, refuted at k=8,12."""
    for k in (8, 12):
        pool = [{"transcription_id": i} for i in range(k * 4)]
        old_head = {c["transcription_id"] for c in pool[:k]}
        for mode in ("reverse", "shuffle"):
            out = gate._sabotage_pool(pool, mode, k)
            new_head = {c["transcription_id"] for c in out[:k]}
            assert not (old_head & new_head), f"mode={mode} k={k}"


def test_gate_sabotage_shuffle_flips_pass_to_fail_at_default_k(db, tmp_path, monkeypatch):
    """D1/finding 1 end to end, on the DEFAULT --k 8,12 (not 2,3): exactly the
    scenario where the old `shuffle` stayed silent (`rc=0`) instead of `rc=1`. 15 items,
    each target is always first in the pool: the most typical shape of a labeled set."""
    items = [_item(f"q{i}", i, f"S{i}") for i in range(1, 16)]
    targets = {it["id"]: (i, f"S{i}") for i, it in enumerate(items, start=1)}
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _good_search(targets))

    rc_ok = gate.main(["--golden", golden, "--db", db, "--min-recall", "8=0.9,12=0.9"])
    assert rc_ok == 0

    rc_bad = gate.main(["--golden", golden, "--db", db, "--min-recall", "8=0.9,12=0.9",
                         "--sabotage", "shuffle"])
    assert rc_bad == 1


def test_gate_sabotage_reverse_flips_pass_to_fail_at_default_k(db, tmp_path, monkeypatch):
    items = [_item(f"q{i}", i, f"S{i}") for i in range(1, 16)]
    targets = {it["id"]: (i, f"S{i}") for i, it in enumerate(items, start=1)}
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _good_search(targets))

    rc_bad = gate.main(["--golden", golden, "--db", db, "--min-recall", "8=0.9,12=0.9",
                         "--sabotage", "reverse"])
    assert rc_bad == 1


def test_gate_no_sabotage_requests_top_k_equal_k_not_pool(db, tmp_path, monkeypatch):
    """D4/finding 5: without sabotage the gate measures what production really
    returns at this k (`top_k=k`), not a slice of k from a `k*4` request: `_cap_comments`
    computes the comments ceiling from `top_k`, so a larger request distorts the measurement."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    seen_top_k = []

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        seen_top_k.append(top_k)
        return {"query": query,
                 "chunks": [{"transcription_id": 1, "source_name": "TG чат"}] * top_k,
                 "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "8,12", "--min-recall", "8=0.0,12=0.0"])
    assert rc == 0
    assert seen_top_k == [8, 12]


def test_gate_sabotage_still_requests_larger_pool(db, tmp_path, monkeypatch):
    """Sabotage stays free to ask for a larger pool: it is a tool for checking
    the gate, not a measurement of production results (Non-goals)."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    seen_top_k = []

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        seen_top_k.append(top_k)
        return {"query": query,
                 "chunks": [{"transcription_id": 1, "source_name": "TG чат"}] * top_k,
                 "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    gate.main(["--golden", golden, "--db", db, "--k", "8", "--min-recall", "8=0.0",
               "--sabotage", "reverse"])
    assert seen_top_k == [32]


def test_gate_verdict_requires_both_k_fail_at_8_pass_at_12(db, tmp_path, monkeypatch):
    """Lesson `eval-verdict-flips-with-k`: the verdict is not accepted at one k.
    The target is at position 9 (0-indexed): outside top-8, inside top-12."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        chunks = [{"transcription_id": 9000 + i, "source_name": "filler"} for i in range(9)]
        chunks.append({"transcription_id": 1, "source_name": "TG чат"})
        chunks += [{"transcription_id": 9100 + i, "source_name": "filler"} for i in range(40)]
        return {"query": query, "chunks": chunks[:top_k], "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "8,12",
                     "--min-recall", "8=0.9,12=0.9"])
    assert rc == 1  # k=12 passes (recall=1.0), k=8 fails (recall=0.0) -> overall 1


def test_gate_live_db_without_yes_live_exits_2(db, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr(gate, "is_live_db", lambda p: p == db)
    rc = gate.main(["--golden", golden, "--db", db, "--no-snapshot"])
    assert rc == 2


# ============================================================
# gate.py: snapshot of the live DB by default (C5)
# ============================================================

def test_gate_auto_snapshots_live_db_by_default(db, tmp_path, monkeypatch):
    snap_dir = tmp_path / "snapshots"
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(gate, "is_live_db", lambda p: p == db)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат")}))

    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9"])

    assert rc == 0
    assert snap_dir.exists()
    assert list(snap_dir.glob("*.db"))


def test_gate_auto_snapshot_protects_snapshot_named_by_live_baseline(db, tmp_path, monkeypatch):
    """D20/finding 5: the pruner has no right to delete the snapshot a
    LIVE baseline (`--baseline`) points to, even when the source changed and `auto_snapshot`
    honestly builds a NEW snapshot with the same basename. This is exactly what used to kill
    the S1 calibration baseline forever."""
    snap_dir = tmp_path / "snapshots"
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(gate, "is_live_db", lambda p: p == db)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат")}))
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])

    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0
    first_snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))["db_snapshot"]["path"]
    assert os.path.exists(first_snapshot)

    # The source changes, and we force a different timestamp in the snapshot name (the
    # same trick as test_auto_snapshot_prunes_old_on_source_change):
    # auto_snapshot now builds a NEW snapshot instead of the first.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
        "VALUES (3, 'file', 'Третій', 'y')")
    conn.commit()
    conn.close()
    os.utime(db, (os.path.getmtime(db) + 5, os.path.getmtime(db) + 5))
    monkeypatch.setattr(
        ev_snapshot, "default_snapshot_path",
        lambda s: str(snap_dir / (Path(s).stem + "-99999999-999999.db")))

    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path)])
    # A different provenance (a new snapshot) -> exit 2 is expected (D17/finding 7).
    # What matters here: the old artifact does NOT disappear, the baseline can still be checked.
    assert rc2 == 2
    assert os.path.exists(first_snapshot), "the pruner deleted the snapshot named by a live baseline"


def test_gate_auto_snapshot_protects_conventional_baseline_without_flags(
        db, tmp_path, monkeypatch):
    """Repair round 1 (Findings): the pruner protection used to see ONLY the flags
    `--baseline`/`--write-baseline` of THIS call (`args.baseline`/
    `args.write_baseline`, both `default=None`). A run with neither of them
    (sabotage runs, manual measurements, any `python -m evals.gate --db ...`)
    never asked about `evals/baseline.local.json` at all, and the pruner freely destroyed
    the live baseline snapshot. This is exactly how S1 died. Here the baseline lies at the conventional
    path beforehand, and the run under test passes neither --baseline nor
    --write-baseline."""
    snap_dir = tmp_path / "snapshots"
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(gate, "is_live_db", lambda p: p == db)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат")}))
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])

    monkeypatch.chdir(tmp_path)
    baseline_path = tmp_path / "evals" / "baseline.local.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    # Bootstrap of the baseline at the conventional path (--write-baseline only prepares
    # the fixture here; the run under test below knows nothing about it).
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0
    first_snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))["db_snapshot"]["path"]
    assert os.path.exists(first_snapshot)

    # The source changes, and we force a different timestamp in the snapshot name (the
    # same trick as test_auto_snapshot_prunes_old_on_source_change):
    # auto_snapshot now builds a NEW snapshot instead of the first.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
        "VALUES (3, 'file', 'Третій', 'y')")
    conn.commit()
    conn.close()
    os.utime(db, (os.path.getmtime(db) + 5, os.path.getmtime(db) + 5))
    monkeypatch.setattr(
        ev_snapshot, "default_snapshot_path",
        lambda s: str(snap_dir / (Path(s).stem + "-99999999-999999.db")))

    # A run WITHOUT --baseline and WITHOUT --write-baseline.
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0"])
    assert rc2 == 0
    assert os.path.exists(first_snapshot), (
        "the pruner deleted the conventional baseline snapshot, although neither "
        "--baseline/--write-baseline was passed by this run")


# ============================================================
# gate.py: the measurement does NOT hold a WAL-rw connection against the file named db_snapshot (D6/ADR-003)
# ============================================================

def test_gate_measurement_does_not_wal_the_snapshot(db, tmp_path, monkeypatch):
    # Finding 4 (review-round-2): the target is a real snapshot (journal_mode=delete),
    # not the `db` fixture (already journal_mode=wal after init_database), otherwise
    # the assertions below are vacuous. See `_real_snapshot`.
    snap = _real_snapshot(db, str(tmp_path / "snap.db"))
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    seen_paths = []

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        seen_paths.append(db_path)
        with get_db_connection(db_path) as conn:  # the same path as production retrieval.search
            conn.execute("SELECT 1").fetchone()
        return {"query": query, "chunks": [{"transcription_id": 1, "source_name": "TG чат"}],
                "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    hash_before = hashlib.sha256(open(snap, "rb").read()).hexdigest()
    mtime_before = os.path.getmtime(snap)

    rc = gate.main(["--golden", golden, "--db", snap, "--k", "2", "--min-recall", "2=0.0"])
    assert rc == 0

    assert hashlib.sha256(open(snap, "rb").read()).hexdigest() == hash_before
    assert os.path.getmtime(snap) == mtime_before
    assert not os.path.exists(snap + "-wal")
    assert not os.path.exists(snap + "-shm")
    assert seen_paths and seen_paths[0] != snap  # a scratch copy, not the snapshot itself


def test_scratch_copy_carries_wal_sidecar(tmp_path):
    """Finding 13: `_scratch_copy` copies only `.db`; if the source carries
    a `-wal` with non-empty content, the measurement runs against a state OLDER than
    committed. The copy must represent the same committed content
    as the file whose identity the gate reports as `db_snapshot`."""
    src = tmp_path / "snap.db"
    src.write_bytes(b"main-content")
    (tmp_path / "snap.db-wal").write_bytes(b"wal-content")
    (tmp_path / "snap.db-shm").write_bytes(b"shm-content")

    with gate._scratch_copy(str(src)) as copy_path:
        assert Path(copy_path + "-wal").read_bytes() == b"wal-content"
        assert Path(copy_path + "-shm").read_bytes() == b"shm-content"
    # everything is removed, including sidecars, after leaving the context
    assert not os.path.exists(copy_path)
    assert not os.path.exists(copy_path + "-wal")
    assert not os.path.exists(copy_path + "-shm")


def test_gate_no_snapshot_yes_live_skips_scratch_copy(db, tmp_path, monkeypatch):
    """`--no-snapshot --yes-live`, pointed at the REAL LIVE DB, remains
    the only documented bypass (module docstring, D16): the measurement runs against
    the literal `--db`, not a copy."""
    monkeypatch.setattr(gate, "is_live_db", lambda p: p == db)
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    seen_paths = []

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        seen_paths.append(db_path)
        return {"query": query, "chunks": [{"transcription_id": 1, "source_name": "TG чат"}],
                "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                     "--no-snapshot", "--yes-live"])
    assert rc == 0
    assert seen_paths == [db]


def test_gate_no_snapshot_yes_live_on_non_live_target_still_scratch_copies(db, tmp_path, monkeypatch, capsys):
    """D16: the same pair of flags, pointed at a TARGET THAT IS NOT the live DB (here a
    snapshot/arbitrary file, `is_live_db` says False by default), MUST NOT
    disable `_scratch_copy`; otherwise `retrieval.search` sets a permanent
    WAL on the file whose identity the gate itself stamps as `db_snapshot`, and
    any baseline taken before that is forever read as a "different provenance"."""
    # Finding 4 (review-round-2): the target is a real snapshot (journal_mode=delete),
    # not the `db` fixture (already journal_mode=wal after init_database), otherwise
    # the assertions below are vacuous. See `_real_snapshot`.
    snap = _real_snapshot(db, str(tmp_path / "snap.db"))
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    seen_paths = []

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        seen_paths.append(db_path)
        with get_db_connection(db_path) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"query": query, "chunks": [{"transcription_id": 1, "source_name": "TG чат"}],
                "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    hash_before = hashlib.sha256(open(snap, "rb").read()).hexdigest()
    mtime_before = os.path.getmtime(snap)

    rc = gate.main(["--golden", golden, "--db", snap, "--k", "2", "--min-recall", "2=0.0",
                     "--no-snapshot", "--yes-live"])
    assert rc == 0

    assert hashlib.sha256(open(snap, "rb").read()).hexdigest() == hash_before
    assert os.path.getmtime(snap) == mtime_before
    assert not os.path.exists(snap + "-wal")
    assert not os.path.exists(snap + "-shm")
    assert seen_paths and seen_paths[0] != snap  # a scratch copy, not the file itself

    # Finding 15: --no-snapshot did not skip anything silently: the gate said so
    # at runtime, not only in the help.
    err = capsys.readouterr().err
    assert "--no-snapshot" in err and "protective copy" in err


# ============================================================
# gate.py: unlabeled/negative/per slice
# ============================================================

def test_gate_skips_unlabeled_items(db, tmp_path, monkeypatch):
    items = [_item("a", 1, "TG чат", status="unlabeled"), _item("b", 2, "Дзвінок")]
    golden = _golden_jsonl(tmp_path, items)
    calls = []

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        calls.append(query)
        return {"query": query,
                 "chunks": [{"transcription_id": 2, "source_name": "Дзвінок"}] * top_k,
                 "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9"])
    assert rc == 0
    assert calls == ["b"]  # "a" (unlabeled) is not searched at all


def test_gate_all_unlabeled_exits_2_never_pass(db, tmp_path, monkeypatch, capsys):
    """D2/finding 2: a set without a single `status="labeled"` item (exactly what
    build_golden yields without labelling) is a data error, NOT a PASS. It used to
    print `[gate] PASS k=8,12` and exit 0, even under -q."""
    items = [_item("a", 1, "TG чат", status="unlabeled")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _bad_search)

    rc = gate.main(["--golden", golden, "--db", db, "-q"])
    assert rc == 2
    out, err = capsys.readouterr()
    assert "PASS" not in out
    assert "labeled" in err


def test_gate_negative_status_excluded_from_aggregate_but_in_report(db, tmp_path, monkeypatch):
    items = [
        {"id": "neg", "question": "neg", "slice": "calls", "status": "negative",
         "expected_transcription_ids": [], "expected_source_name_contains": []},
        _item("b", 2, "Дзвінок"),
    ]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"b": (2, "Дзвінок")}))
    json_out = tmp_path / "out.local.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                     "--json-out", str(json_out)])
    assert rc == 0

    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["aggregate"]["2"]["n"] == 1  # only "b" (labeled) in the aggregation
    ids_in_report = [r["id"] for r in data["per_item"]["2"]]
    assert "neg" in ids_in_report and "b" in ids_in_report


# ============================================================
# gate.py: baseline: write + per-item diff + item regression
# ============================================================

def test_gate_write_baseline_then_detect_regression(db, tmp_path, monkeypatch):
    items = [_item("a", 1, "TG чат"), _item("b", 2, "Дзвінок")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат"), "b": (2, "Дзвінок")}))

    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["k"] == [2]
    assert baseline["items"]["a"]["2"]["hit"] is True

    # "a" stops being found -> item hit->miss -> regression against the baseline
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"b": (2, "Дзвінок")}))
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path)])
    assert rc2 == 1


def test_gate_fails_on_item_regression_alone_when_mean_drop_allowed(db, tmp_path, monkeypatch):
    """The same scenario as above, but with --max-drop 1.0 (a mean drop larger
    does NOT fail the gate): the failure remains only via item_regressions (default
    --max-item-regressions 0), isolated from the mean_drop cause."""
    items = [_item("a", 1, "TG чат"), _item("b", 2, "Дзвінок")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search",
                         _good_search({"a": (1, "TG чат"), "b": (2, "Дзвінок")}))

    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0

    monkeypatch.setattr("app.services.retrieval.search", _good_search({"b": (2, "Дзвінок")}))
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path), "--max-drop", "1.0"])
    assert rc2 == 1


def test_gate_fails_on_mean_drop_without_item_regression(db, tmp_path, monkeypatch):
    """Recall drops (0.5->0.0), but the bool hit stays False before and after
    (regression counts only True->False): a failure only via mean_drop."""
    item = {"id": "a", "question": "a", "slice": "calls", "status": "labeled",
            "expected_transcription_ids": [1, 2], "expected_source_name_contains": []}
    golden = _golden_jsonl(tmp_path, [item])

    def _partial(target_ids):
        def _search(db_path, query, top_k=8, category_id=None, **kwargs):
            chunks = [{"transcription_id": tid, "source_name": "x"} for tid in target_ids]
            chunks += [{"transcription_id": 9000 + i, "source_name": "filler"}
                       for i in range(max(top_k - len(chunks), 0))]
            return {"query": query, "chunks": chunks[:top_k], "vector_available": True}
        return _search

    monkeypatch.setattr("app.services.retrieval.search", _partial([1]))  # recall=0.5
    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["items"]["a"]["2"]["hit"] is False  # 0.5 < 0.999 -> not a hit

    monkeypatch.setattr("app.services.retrieval.search", _partial([]))  # recall=0.0
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path), "--max-item-regressions", "5"])
    assert rc2 == 1  # False->False (no item regression), failure only via mean_drop


# ============================================================
# gate.py: baseline provenance (D5, findings 6, 13)
# ============================================================

def test_gate_write_baseline_records_provenance(db, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    baseline_path = tmp_path / "baseline.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                     "--write-baseline", str(baseline_path)])
    assert rc == 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["vector_available"] is True
    assert baseline["rerank"] is False
    assert baseline["db_snapshot"]["path"] == os.path.abspath(db)
    assert baseline["db_snapshot"]["size"] == os.path.getsize(db)


def test_gate_write_baseline_skipped_on_fail(db, tmp_path, monkeypatch):
    """D12/finding 15: a regressed run does not silently become the new truth."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _bad_search)
    baseline_path = tmp_path / "baseline.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                     "--write-baseline", str(baseline_path)])
    assert rc == 1
    assert not baseline_path.exists()


def test_gate_baseline_vector_available_mismatch_exits_2(db, tmp_path, monkeypatch):
    """Finding 6: e5 is unavailable in this run; it must not quietly read as
    a ranking regression against a baseline taken with e5 available."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])

    def _search_with_avail(avail):
        def _search(db_path, query, top_k=8, category_id=None, **kwargs):
            return {"query": query, "chunks": [{"transcription_id": 1, "source_name": "TG чат"}],
                    "vector_available": avail}
        return _search

    monkeypatch.setattr("app.services.retrieval.search", _search_with_avail(True))
    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0

    monkeypatch.setattr("app.services.retrieval.search", _search_with_avail(False))
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path)])
    assert rc2 == 2


def test_gate_baseline_snapshot_identity_mismatch_exits_2(db, tmp_path, monkeypatch):
    """Finding 13: a baseline taken over a different snapshot gives no quiet verdict."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({
        "created_at": "x", "k": [2],
        "db_snapshot": {"path": "C:/nope/other.db", "size": 1, "mtime": 1.0},
        "vector_available": True, "rerank": False,
        "items": {}, "aggregate": {}, "by_slice": {},
    }), encoding="utf-8")
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                     "--baseline", str(baseline_path)])
    assert rc == 2


def test_gate_baseline_different_golden_set_exits_2(db, tmp_path, monkeypatch):
    """Finding 6: a baseline taken over a different set (other ids) must not quietly
    be considered comparable merely because the snapshot/vector_available/rerank
    matched; otherwise mean_drop computes a difference against foreign items."""
    items1 = [_item("a", 1, "TG чат")]
    golden1 = _golden_jsonl(tmp_path, items1, name="g1.jsonl")
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden1, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0

    items2 = [_item("z", 2, "Дзвінок")]
    golden2 = _golden_jsonl(tmp_path, items2, name="g2.jsonl")
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"z": (2, "Дзвінок")}))
    rc2 = gate.main(["--golden", golden2, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path)])
    assert rc2 == 2


def test_gate_baseline_missing_provenance_exits_2(db, tmp_path, monkeypatch):
    """An old baseline (before this repair) without db_snapshot/vector_available/rerank
    in the new format: the provenance is unknown, not silently comparable."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({
        "created_at": "x", "db_snapshot": db, "k": [2],
        "items": {}, "aggregate": {}, "by_slice": {},
    }), encoding="utf-8")
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                     "--baseline", str(baseline_path)])
    assert rc == 2


# ============================================================
# gate.py: the gate must not PASS having checked nothing (D2, review-round-2,
# finding 1: two doors)
# ============================================================

def test_gate_k_without_threshold_or_baseline_entry_exits_2_not_pass(db, tmp_path, monkeypatch):
    """Door (a): the baseline taken at k=2, the run at k=3: --min-recall has no
    entry for 3, and the baseline has none either (written only at k=2). No
    check for k=3 exists, so even a total collapse (recall 0.0) must not
    give PASS/0: there is no verdict (2). BEFORE the fix this test fails:
    the gate prints PASS and returns 0."""
    items = [_item("a", 1, "TG чат")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0

    monkeypatch.setattr("app.services.retrieval.search", _bad_search)  # total collapse
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "3", "--min-recall", "2=0.9",
                      "--baseline", str(baseline_path)])
    assert rc2 == 2


def test_gate_name_only_items_check_source_name_hit_rate_not_silent_pass(db, tmp_path, monkeypatch):
    """Door (b), reachable at the default --k 8,12: an item labelled only with
    `expected_source_name_contains` (the README recommends this as an alternative more robust
    to id drift) leaves recall_at_k.mean = None; the absolute threshold must
    be checked against source_name_hit_rate, otherwise a total miss silently
    passes. BEFORE the fix this test fails: the gate prints PASS k=8,12 and 0,
    although the expected source never once appeared in the results."""
    items = [{"id": "a", "question": "a", "slice": "calls", "status": "labeled",
              "expected_transcription_ids": [], "expected_source_name_contains": ["TG чат"]}]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _bad_search)  # the target is never in the top
    rc = gate.main(["--golden", golden, "--db", db])  # default --k 8,12 and --min-recall
    assert rc == 1  # a real quality verdict (FAIL), not a silent PASS


def test_gate_name_only_items_pass_when_source_name_found(db, tmp_path, monkeypatch):
    """The same set as above, but the target really is found: the check of
    source_name_hit_rate now REALLY records success, rather than merely never
    failing."""
    items = [{"id": "a", "question": "a", "slice": "calls", "status": "labeled",
              "expected_transcription_ids": [], "expected_source_name_contains": ["TG чат"]}]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    rc = gate.main(["--golden", golden, "--db", db])
    assert rc == 0


def test_default_min_recall_does_not_exceed_rule_applied_to_calibration_line():
    """Repair round 1 (D15.4): the corpus drifts in both directions (recall@12
    0.5357->0.6071 over seven hours of background ingest on 03.09), so equality of
    `_DEFAULT_MIN_RECALL` to the numbers of ONE taken baseline is a vacuous condition that
    goes stale at the next measurement. An invariant that stays true under
    drift: the shipped default is the lowest of those derived across all baselines, hence
    it does NOT EXCEED the threshold derived from the CURRENT baseline. The "measured baseline" numbers
    are read from `evals/baseline.local.json` on disk (not hard-coded) -
    the file is gitignored, so on a machine without a taken baseline the test is skipped with
    an explicit reason, rather than silently passing vacuously."""
    baseline_path = _PROJECT_ROOT / "evals" / "baseline.local.json"
    if not baseline_path.exists():
        pytest.skip(f"{baseline_path} is missing (gitignored): the calibration baseline "
                    "was not taken on this machine, there are no current numbers to check against")
    line = json.loads(baseline_path.read_text(encoding="utf-8"))
    aggregate = line["aggregate"]
    default = gate._parse_min_recall(gate._DEFAULT_MIN_RECALL)
    assert default, "_DEFAULT_MIN_RECALL must set at least one k"
    checked = 0
    for k_str, stats in aggregate.items():
        k = int(k_str)
        if k not in default:
            continue
        derived_ceiling = gate._derive_min_recall_threshold(stats["recall_at_k"], stats["n"])
        assert default[k] <= derived_ceiling + 1e-9, (
            f"k={k}: default {default[k]} exceeds the upper bound {derived_ceiling}, "
            f"derived from the current baseline ({baseline_path}, n={stats['n']}) - "
            "the default must be lowered (D15.4: it is a collapse detector, not a ceiling)"
        )
        checked += 1
    assert checked == len(default), (
        "the current baseline does not cover all k from _DEFAULT_MIN_RECALL: the check was not done for all")


# ============================================================
# gate.py: an infrastructure failure during measurement != a quality verdict (D3, finding 4)
# ============================================================

def test_gate_search_exception_exits_2_not_1(db, tmp_path, monkeypatch, capsys):
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])

    def _boom(db_path, query, top_k=8, category_id=None, **kwargs):
        raise RuntimeError("model did not come up")

    monkeypatch.setattr("app.services.retrieval.search", _boom)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "8"])
    assert rc == 2
    assert "model did not come up" in capsys.readouterr().err


# ============================================================
# gate.py: output
# ============================================================

def test_gate_quiet_prints_single_line(db, tmp_path, monkeypatch, capsys):
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9", "-q"])
    assert rc == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 1
    assert "PASS" in lines[0]


# ============================================================
# gate.py: usage errors (exit 2)
# ============================================================

def test_gate_missing_golden_file_exits_2(db, tmp_path):
    rc = gate.main(["--golden", str(tmp_path / "nope.jsonl"), "--db", db])
    assert rc == 2


def test_gate_missing_db_exits_2(tmp_path):
    rc = gate.main(["--golden", str(tmp_path / "g.jsonl"), "--db", str(tmp_path / "nope.db")])
    assert rc == 2


def test_gate_invalid_golden_line_exits_2_with_line_number(db, tmp_path, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a", "question": "q"}\nnot json\n', encoding="utf-8")
    rc = gate.main(["--golden", str(path), "--db", db])
    assert rc == 2
    assert "line 2" in capsys.readouterr().err


def test_gate_json_out_requires_local_convention_exits_2(db, tmp_path):
    """D10/finding 19: --json-out writes real questions and transcription_id;
    the only new surface that must fall under the *.local.* privacy convention."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2",
                     "--json-out", str(tmp_path / "gate_run.json")])
    assert rc == 2


def test_gate_json_out_local_convention_accepted(db, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    out_path = tmp_path / "gate_run.local.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                     "--json-out", str(out_path)])
    assert rc == 0
    assert out_path.exists()


def test_gate_json_out_repo_root_path_rejected(db, tmp_path):
    """Finding 3 (review-round-2): `gate_run.local.json` in the repository root
    matches the NAME convention, but git TRACKS it: there is no root
    `*.local.json` rule in .gitignore. It must fail with exit 2 before any
    write to disk."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    path = gate._PROJECT_ROOT / "gate_run.local.json"
    assert not path.exists()
    try:
        rc = gate.main(["--golden", golden, "--db", db, "--k", "2",
                         "--json-out", str(path)])
        assert rc == 2
        assert not path.exists()
    finally:
        if path.exists():
            path.unlink()


def test_gate_json_out_nested_evals_subdir_rejected(db, tmp_path):
    """`.gitignore` covers only `evals/*.local.json` DIRECTLY (without `**`);
    a nested directory such as `evals/runs/` does not fall under this rule.
    Previously the filename heuristic accepted such a path; git will track it."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    path = gate._PROJECT_ROOT / "evals" / "runs" / "x.local.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2",
                     "--json-out", str(path)])
    assert rc == 2
    assert not path.exists()


def test_gate_json_out_real_evals_dir_path_still_accepted(db, tmp_path, monkeypatch):
    """Control: a path that REALLY falls under `evals/*.local.json`
    in the real repository, git does not count as "the gate broke a legitimate case"."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    path = gate._PROJECT_ROOT / "evals" / "gate_run_test.local.json"
    try:
        rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                         "--json-out", str(path)])
        assert rc == 0
        assert path.exists()
    finally:
        if path.exists():
            path.unlink()


def test_gate_no_snapshot_help_does_not_overclaim_after_d16(capsys):
    """Finding 15: the `--no-snapshot` help must not claim that the flag
    always means "directly on --db": on a non-live target the protective copy
    (_scratch_copy) is still applied (D16)."""
    with pytest.raises(SystemExit):
        gate.main(["--help"])
    help_text = capsys.readouterr().out
    assert "--no-snapshot" in help_text
    assert "protective copy" in help_text or "_scratch_copy" in help_text


# ============================================================
# gate.py: end-to-end run on a REAL snapshot and e5 (C9: the `eval` marker,
# skip with a reason if there is no golden-set/archive/model)
# ============================================================

@pytest.mark.eval
def test_gate_real_snapshot_passes_with_local_golden_set(tmp_path, monkeypatch):
    """Canary, not a strict CI gate (finding 14): the archive drifts (new records,
    re-ingest), so PASS here is a signal "check by hand if it turns red",
    not an iron contract of the code. The snapshot is written to tmp_path, NOT to
    evals/snapshots/ of the real repo; otherwise every run leaves a 315 MB
    artifact in the working tree (finding 14)."""
    golden_path = _PROJECT_ROOT / "evals" / "golden_set.local.json"
    db_path = _PROJECT_ROOT / "whisper_history.db"
    if not golden_path.exists():
        pytest.skip(f"no {golden_path}: the local golden-set is not labeled")
    if not db_path.exists():
        pytest.skip(f"no {db_path}: there is no local archive")
    from app.services import embeddings
    if not embeddings.is_available():
        pytest.skip(f"e5 unavailable: {embeddings.unavailability_reason()}")

    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")

    rc = gate.main(["--golden", str(golden_path), "--db", str(db_path), "--k", "8,12", "-q"])
    assert rc == 0


# ============================================================
# gate.py: the --rerank flag (18.09.2026, a finding of the Wave B enabling
# stage: the gate measured a configuration that does not exist in production)
# ============================================================

def test_gate_rerank_flag_reaches_retrieval_search(db, tmp_path, monkeypatch):
    """Without the flag `retrieval.search` gets rerank=False, with it True.
    This exact argument distinguishes the measurement from production: the RAG chat enables the reranker at
    RECALL_RERANK_ENABLED=1, while the gate before this fix held False as a constant."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    seen = []
    inner = _good_search({"a": (1, "TG чат")})

    def _spy(db_path, query, top_k=8, category_id=None, **kwargs):
        seen.append(kwargs.get("rerank"))
        return inner(db_path, query, top_k=top_k, category_id=category_id, **kwargs)

    monkeypatch.setattr("app.services.retrieval.search", _spy)

    assert gate.main(["--golden", golden, "--db", db, "--k", "2",
                      "--min-recall", "2=0.9", "-q"]) == 0
    assert seen == [False]

    seen.clear()
    assert gate.main(["--golden", golden, "--db", db, "--k", "2",
                      "--min-recall", "2=0.9", "-q", "--rerank"]) == 0
    assert seen == [True]


def test_gate_rerank_recorded_in_provenance_and_verdict_line(db, tmp_path, monkeypatch, capsys):
    """The run signs the configuration twice: in the verdict line (the only line under
    `-q`) and in `--json-out`. Otherwise two files merged via `evals.compare`
    carry no mention that one was taken with the reranker and the other without."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    out_path = tmp_path / "run.local.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                    "-q", "--rerank", "--json-out", str(out_path)])
    assert rc == 0
    line = [l for l in capsys.readouterr().out.splitlines() if l.strip()][0]
    assert "rerank=on" in line
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["provenance"]["rerank"] is True


def test_normalize_tags_stripped_and_empties_dropped():
    item = golden_io._normalize(
        {"id": "a", "question": "q", "tags": [" AI ", "", "  "]}, source="manual",
        default_status="labeled")
    assert item["tags"] == ["AI"]


def test_normalize_without_tags_defaults_empty_list():
    item = golden_io._normalize({"id": "a", "question": "q"}, source="manual",
                                 default_status="labeled")
    assert item["tags"] == []


def test_normalize_expected_leak_nonzero_raises():
    with pytest.raises(golden_io.GoldenSetError, match="expected_leak"):
        golden_io._normalize({"id": "a", "question": "q", "expected_leak": 1},
                              source="manual", default_status="labeled")


def test_normalize_expected_leak_zero_ok():
    item = golden_io._normalize({"id": "a", "question": "q", "expected_leak": 0},
                                 source="manual", default_status="labeled")
    assert item["id"] == "a"  # did not crash


# ============================================================
# C8: filter_leak
# ============================================================

def test_gate_search_receives_category_id_and_tags(db_taxonomy, tmp_path, monkeypatch):
    seen = {}

    def _spy(db_path, query, top_k=8, category_id=None, **kwargs):
        seen["category_id"] = category_id
        seen["tags"] = kwargs.get("tags")
        return {"query": query, "chunks": [{"transcription_id": 3, "source_name": "Акмекорп дзвінок"}],
                "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _spy)
    golden = _golden_jsonl(tmp_path, [
        _item("a", 3, "Акмекорп", category_id=db_taxonomy["fund_id"], tags=["AI"])])
    gate.main(["--golden", golden, "--db", db_taxonomy["db"], "--k", "2",
               "--min-recall", "2=0.5", "-q"])
    assert seen["category_id"] == db_taxonomy["fund_id"]
    assert seen["tags"] == ["AI"]


def test_gate_filter_leak_zero_on_honest_category_search(db_taxonomy, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [
        _item("a", 3, "Акмекорп дзвінок", category_id=db_taxonomy["fund_id"])])
    monkeypatch.setattr("app.services.retrieval.search",
                         _leak_search((3, "Акмекорп дзвінок")))
    rc = gate.main(["--golden", golden, "--db", db_taxonomy["db"], "--k", "2",
                     "--min-recall", "2=0.9"])
    assert rc == 0


def test_gate_filter_leak_category_axis_fails_despite_full_recall(db_taxonomy, tmp_path, monkeypatch,
                                                                    capsys):
    golden = _golden_jsonl(tmp_path, [
        _item("a", 3, "Акмекорп дзвінок", category_id=db_taxonomy["fund_id"])])
    # #4 is not in the subtree of «Фонд Робота»: a foreign record the filter must not let through.
    monkeypatch.setattr("app.services.retrieval.search",
                         _leak_search((3, "Акмекорп дзвінок"), leaked=(4, "Чужий дзвінок")))
    rc = gate.main(["--golden", golden, "--db", db_taxonomy["db"], "--k", "2",
                     "--min-recall", "2=0.9"])
    assert rc == 1
    assert "filter_leak" in capsys.readouterr().out


def test_gate_filter_leak_tags_axis_fails_on_untagged_chunk(db_taxonomy, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [
        _item("b", 3, "Акмекорп дзвінок", tags=["AI"])])
    # #2 (from the base `db`) does not have the tag AI.
    monkeypatch.setattr("app.services.retrieval.search",
                         _leak_search((3, "Акмекорп дзвінок"), leaked=(2, "Дзвінок")))
    rc = gate.main(["--golden", golden, "--db", db_taxonomy["db"], "--k", "2",
                     "--min-recall", "2=0.9"])
    assert rc == 1


def test_gate_filter_leak_none_without_filters(db_taxonomy, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [_item("a", 3, "Акмекорп дзвінок")])
    monkeypatch.setattr("app.services.retrieval.search",
                         _leak_search((3, "Акмекорп дзвінок"), leaked=(4, "Чужий дзвінок")))
    out_path = tmp_path / "out.local.json"
    rc = gate.main(["--golden", golden, "--db", db_taxonomy["db"], "--k", "2",
                     "--min-recall", "2=0.5", "--json-out", str(out_path)])
    assert rc == 0  # without category_id/tags filter_leak is not counted, there is no leak
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["per_item"]["2"][0]["filter_leak"] is None


def test_gate_json_out_reports_filter_leak_per_item_and_aggregate(db_taxonomy, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [
        _item("a", 3, "Акмекорп дзвінок", category_id=db_taxonomy["fund_id"])])
    monkeypatch.setattr("app.services.retrieval.search",
                         _leak_search((3, "Акмекорп дзвінок"), leaked=(4, "Чужий дзвінок")))
    out_path = tmp_path / "out.local.json"
    gate.main(["--golden", golden, "--db", db_taxonomy["db"], "--k", "2",
               "--min-recall", "2=0.9", "--json-out", str(out_path)])
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["per_item"]["2"][0]["filter_leak"] == 1
    assert payload["aggregate"]["2"]["filter_leak"] == 1


def test_gate_baseline_without_rerank_not_compared_to_rerank_run(db, tmp_path, monkeypatch, capsys):
    """A baseline taken WITHOUT the reranker must not silently judge a run WITH it:
    the provenance does not match, so the per-item diff and the baseline verdict
    are switched off (the same protection that already stands on the DB snapshot)."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    base = tmp_path / "base.local.json"
    assert gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                      "-q", "--write-baseline", str(base)]) == 0
    capsys.readouterr()
    gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
               "--rerank", "--baseline", str(base)])
    assert "does not match" in capsys.readouterr().err
