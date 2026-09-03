"""Смок-тести `evals/gate.py` + `evals/snapshot.py` + `evals/golden_io.py`
(eval-gate, історія 01) — синтетична БД (`app.db.migrations.init_database`),
`app.services.retrieval.search` підмінений фейком: жодного реального
e5/GPU виклику, жодного `ANTHROPIC_API_KEY`, жодного звернення до мережі.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from evals import gate, golden_io
from evals import snapshot as ev_snapshot

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# фікстури / хелпери
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


def _item(item_id, tid, name, *, status="labeled", slice_="calls"):
    return {
        "id": item_id, "question": item_id, "slice": slice_, "status": status,
        "expected_transcription_ids": [tid],
        "expected_source_name_contains": [name],
    }


def _good_search(target_by_query):
    """Ціль (якщо задана) завжди перша у пулі — recall@k=1.0 на будь-якому k."""
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
    """Ціль ніколи не потрапляє у видачу — recall@k=0.0 всюди."""
    chunks = [{"transcription_id": 9000 + i, "source_name": "filler"} for i in range(top_k)]
    return {"query": query, "chunks": chunks, "vector_available": True}


def _real_snapshot(src, dst):
    """Побудувати справжній `VACUUM INTO`-знімок (`journal_mode=delete`), той
    самий шлях, що продакшн `evals/snapshot.py::make_snapshot`.

    Знахідка 4 (review-round-2): фікстура `db` після `init_database` вже в
    `journal_mode=wal` — WAL-конект на вже-WAL файл не чіпає жоден байт, тож
    байтові твердження (хеш/mtime/відсутність `-wal`) вакуумні на самій
    фікстурі: чотири з пʼяти тверджень D16-тесту проходили і на дефектному
    коді. На `delete`-цілі перший же `get_db_connection` (яка ставить
    `PRAGMA journal_mode=WAL`) фізично міняє байти заголовка — твердження
    стають несучими. Передумову перевіряємо тут-таки, щоб доказ не зогнив
    мовчки назад у вакуумний, якщо дефолти `init_database`/`make_snapshot`
    колись зміняться.
    """
    ev_snapshot.make_snapshot(src, dst)
    conn = sqlite3.connect(dst)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "delete", (
        f"знімок не в journal_mode=delete (маємо {mode!r}) — доказ знову вакуумний"
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
    """D8/знахідка 10: повторний прогін на незміненому джерелі не створює
    другий 315-МБ знімок — той самий шлях перевикористовується."""
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
    """D8: якщо джерело змінилось, старий знімок з тим самим basename прибирається."""
    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")
    src = str(tmp_path / "src.db")
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    first = ev_snapshot.auto_snapshot(src)
    assert os.path.exists(first)

    # Мутуємо джерело і форсуємо інший таймстемп у назві знімка, інакше
    # другий знімок у ту саму секунду перезаписав би перший файл на диску,
    # а mtime файлу міг не встигнути тикнути в межах роздільної здатності ФС.
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
    assert not os.path.exists(first)  # старий прибраний


def test_prune_other_snapshots_keeps_protected_path(tmp_path, monkeypatch):
    """D20/знахідка 5: знімок, названий ЖИВОЮ базовою лінією, не видаляється
    прунером, доки лінію не перезаписано — навіть якщо він не `keep` цього
    прогону. На синтетичних файлах, без реального SQLite."""
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
    """Знахідка 14: прунер прибирає `.db`, а `-wal`/`-shm`/`.meta.json`
    лишає сиротами — рівно ті файли, що зараз лежать у `evals/snapshots/`
    від знімка `130113` без жодного `.db` поруч."""
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
    """Знахідка 14 (сироти від попереднього багу, БЕЗ `.db` поруч) — прунер
    їх теж прибирає, а не лишає накопичуватись назавжди."""
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
    with pytest.raises(golden_io.GoldenSetError, match="рядку 2"):
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
    assert items[0]["slice"] == "calls"  # без conn -> дефолт


def test_read_golden_legacy_items_key_form(tmp_path):
    path = tmp_path / "legacy2.json"
    path.write_text(json.dumps({"items": [{"id": "x", "question": "q"}]}), encoding="utf-8")
    items = golden_io.read_golden(str(path))
    assert len(items) == 1
    assert items[0]["source"] == "legacy"


def test_read_golden_legacy_infers_slice_from_source_type(db, tmp_path):
    path = tmp_path / "legacy3.json"
    path.write_text(json.dumps([
        {"id": "x", "question": "q", "expected_transcription_ids": [1]},  # id=1 -> telegram у фікстурі
    ]), encoding="utf-8")
    conn = sqlite3.connect(db)
    items = golden_io.read_golden(str(path), conn=conn)
    conn.close()
    assert items[0]["slice"] == "tg"


def test_golden_io_legacy_slice_lookup_broken_conn_logs_and_defaults(db, tmp_path, capsys):
    """Знахідка 18: побитий/невідповідний conn не має тихо ставати slice="calls"
    без сліду в логу (CLAUDE.md: no bare except without a log)."""
    path = tmp_path / "legacy4.json"
    path.write_text(json.dumps([
        {"id": "x", "question": "q", "expected_transcription_ids": [1]},
    ]), encoding="utf-8")

    class _BrokenConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table: transcriptions")

    items = golden_io.read_golden(str(path), conn=_BrokenConn())
    assert items[0]["slice"] == "calls"
    assert "не вдалось визначити source_type" in capsys.readouterr().err


# ============================================================
# golden_io.py — read_jsonl (D13, знахідка 21): спільний зчитувач для всіх eval-CLI
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
    with pytest.raises(golden_io.GoldenSetError, match="рядку 2"):
        golden_io.read_jsonl(str(path))


def test_golden_io_read_jsonl_non_object_line_raises(tmp_path):
    path = tmp_path / "arr.jsonl"
    path.write_text('[1, 2, 3]\n', encoding="utf-8")
    with pytest.raises(golden_io.GoldenSetError):
        golden_io.read_jsonl(str(path))


# ============================================================
# gate.py — CLI/parsing хелпери
# ============================================================

def test_parse_k_list():
    assert gate._parse_k_list("8,12") == [8, 12]


def test_parse_min_recall():
    assert gate._parse_min_recall("8=0.6,12=0.75") == {8: 0.6, 12: 0.75}


# ============================================================
# gate.py — D15.3: виведення --min-recall із калібрувальної лінії
# ============================================================

def test_derive_min_recall_threshold_matches_rule_on_synthetic_line():
    """Правило, не переписані числа (D15.3): synthetic recall_at_k/n ->
    floor_0.05(recall_at_k - 2/n), обчислене незалежно від _derive_min_recall_threshold."""
    recall_at_k, n = 0.70, 20  # 0.70 - 2/20 = 0.60 -> точно на межі сітки
    assert gate._derive_min_recall_threshold(recall_at_k, n) == pytest.approx(0.60)

    recall_at_k, n = 0.32142857142857145, 15  # заміряна лінія 03.09 (recall@8, plan.md D15)
    expected = 0.05 * int((recall_at_k - 2 / n) / 0.05 + 1e-9)
    assert gate._derive_min_recall_threshold(recall_at_k, n) == pytest.approx(expected)
    assert gate._derive_min_recall_threshold(recall_at_k, n) == pytest.approx(0.15)


def test_derive_min_recall_threshold_floor_not_round():
    """floor_0.05, не round_0.05 — 0.145 лежить у комірці [0.10, 0.15), round дав
    би 0.15, floor мусить дати 0.10."""
    assert gate._derive_min_recall_threshold(0.165, 100) == pytest.approx(0.10)  # 0.165-0.02=0.145 -> 0.10


def test_floor_grid_handles_float_boundary_without_epsilon_bug():
    """0.30/0.05 = 5.999999999999999 у float — без epsilon floor скидає межове
    значення в сусідню комірку нижче (0.25 замість 0.30)."""
    assert gate._floor_grid(0.30) == pytest.approx(0.30)


# ============================================================
# gate.py — наскрізний смок (Tracer): pass / поріг / sabotage / жива БД
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

    # Голова пулу (стара top-k) і хвіст тасуються НЕЗАЛЕЖНО, тоді хвіст стає
    # перед головою (D1) — ціль з позиції 0 (у голові) гарантовано опиняється
    # за межами нових перших k, на будь-якому k.
    rc_bad = gate.main(["--golden", golden, "--db", db, "--k", "2,3",
                         "--min-recall", "2=0.9,3=0.9", "--sabotage", "shuffle"])
    assert rc_bad == 1


def test_sabotage_pool_guarantees_displacement_at_default_k():
    """D1/знахідка 1: пряме доведення властивості на рівні `_sabotage_pool` —
    жодна з перших k позицій ПУЛУ не лишається в перших k ПІСЛЯ саботажу, на
    дефолтних пулах гейта (k*4 для k=8,12), для ОБОХ режимів. Стара
    `random.Random(0).shuffle` над усім пулом цієї гарантії не давала саме
    на цих k (знахідка 1) — доведено тестом на k=2,3, спростовано на k=8,12."""
    for k in (8, 12):
        pool = [{"transcription_id": i} for i in range(k * 4)]
        old_head = {c["transcription_id"] for c in pool[:k]}
        for mode in ("reverse", "shuffle"):
            out = gate._sabotage_pool(pool, mode, k)
            new_head = {c["transcription_id"] for c in out[:k]}
            assert not (old_head & new_head), f"mode={mode} k={k}"


def test_gate_sabotage_shuffle_flips_pass_to_fail_at_default_k(db, tmp_path, monkeypatch):
    """D1/знахідка 1 наскрізно, на ДЕФОЛТНИХ --k 8,12 (не 2,3) — саме той
    сценарій, де стара `shuffle` мовчала (`rc=0`) замість `rc=1`. 15 пунктів,
    ціль кожного завжди перша у пулі — найтиповіший вигляд розміченого набору."""
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
    """D4/знахідка 5: без саботажу гейт міряє те, що продакшн реально
    віддає на цьому k (`top_k=k`), не зріз k із запиту `k*4` — `_cap_comments`
    рахує стелю коментарів від `top_k`, тож більший запит спотворює замір."""
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
    """Саботаж лишається вправі просити більший пул — інструмент перевірки
    гейта, не замір продакшн-видачі (Non-goals)."""
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
    """Урок `eval-verdict-flips-with-k`: вердикт не приймається на одному k.
    Ціль на позиції 9 (0-індексовано) — поза top-8, всередині top-12."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        chunks = [{"transcription_id": 9000 + i, "source_name": "filler"} for i in range(9)]
        chunks.append({"transcription_id": 1, "source_name": "TG чат"})
        chunks += [{"transcription_id": 9100 + i, "source_name": "filler"} for i in range(40)]
        return {"query": query, "chunks": chunks[:top_k], "vector_available": True}

    monkeypatch.setattr("app.services.retrieval.search", _search)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "8,12",
                     "--min-recall", "8=0.9,12=0.9"])
    assert rc == 1  # k=12 проходить (recall=1.0), k=8 провалюється (recall=0.0) -> сукупно 1


def test_gate_live_db_without_yes_live_exits_2(db, tmp_path, monkeypatch):
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr(gate, "is_live_db", lambda p: p == db)
    rc = gate.main(["--golden", golden, "--db", db, "--no-snapshot"])
    assert rc == 2


# ============================================================
# gate.py — знімок живої БД за замовчуванням (C5)
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
    """D20/знахідка 5: прунер не має права видалити знімок, на який показує
    ЖИВА лінія (`--baseline`), навіть коли джерело змінилось і `auto_snapshot`
    чесно будує НОВИЙ знімок з тим самим basename. Раніше саме це вбивало
    калібрувальну лінію S1 назавжди."""
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

    # Джерело змінюється, і форсуємо інший таймстемп у назві знімка (той
    # самий трюк, що test_auto_snapshot_prunes_old_on_source_change) —
    # auto_snapshot тепер будує НОВИЙ знімок замість першого.
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
    # Інший провенанс (новий знімок) -> exit 2 — очікувано (D17/знахідка 7).
    # Головне тут: старий артефакт НЕ зникає, лінію ще можна перевірити.
    assert rc2 == 2
    assert os.path.exists(first_snapshot), "прунер видалив знімок, названий живою лінією"


def test_gate_auto_snapshot_protects_conventional_baseline_without_flags(
        db, tmp_path, monkeypatch):
    """Ремонтний раунд 1 (Findings): захист прунера раніше бачив ЛИШЕ прапорці
    `--baseline`/`--write-baseline` ЦЬОГО виклику (`args.baseline`/
    `args.write_baseline`, обидва `default=None`). Прогін без жодного з них —
    саботажні прогони, ручні заміри, будь-який `python -m evals.gate --db ...`
    — про `evals/baseline.local.json` узагалі не питав, і прунер вільно знищував
    знімок живої лінії. Саме так S1 і померла. Тут лінія лежить на конвенційному
    шляху заздалегідь, а прогін під тестом НЕ передає ні --baseline, ні
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
    # Бутстрап лінії на конвенційному шляху (--write-baseline тут лише готує
    # фікстуру — сам прогін під тестом нижче про нього не знає).
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0
    first_snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))["db_snapshot"]["path"]
    assert os.path.exists(first_snapshot)

    # Джерело змінюється, і форсуємо інший таймстемп у назві знімка (той
    # самий трюк, що test_auto_snapshot_prunes_old_on_source_change) —
    # auto_snapshot тепер будує НОВИЙ знімок замість першого.
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

    # Прогін БЕЗ --baseline і БЕЗ --write-baseline.
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0"])
    assert rc2 == 0
    assert os.path.exists(first_snapshot), (
        "прунер видалив знімок конвенційної лінії, хоч жоден із "
        "--baseline/--write-baseline не передавався цим прогоном")


# ============================================================
# gate.py — вимір НЕ тримає WAL-rw проти файлу, названого db_snapshot (D6/ADR-003)
# ============================================================

def test_gate_measurement_does_not_wal_the_snapshot(db, tmp_path, monkeypatch):
    # Знахідка 4 (review-round-2): ціль — справжній знімок (journal_mode=delete),
    # не фікстура `db` (уже journal_mode=wal після init_database), інакше
    # твердження нижче вакуумні. Див. `_real_snapshot`.
    snap = _real_snapshot(db, str(tmp_path / "snap.db"))
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    seen_paths = []

    def _search(db_path, query, top_k=8, category_id=None, **kwargs):
        seen_paths.append(db_path)
        with get_db_connection(db_path) as conn:  # той самий шлях, що продакшн retrieval.search
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
    assert seen_paths and seen_paths[0] != snap  # scratch-копія, не сам знімок


def test_scratch_copy_carries_wal_sidecar(tmp_path):
    """Знахідка 13: `_scratch_copy` копіює лише `.db` — якщо джерело несе
    `-wal` з непорожнім наповненням, вимір іде проти стану СТАРІШОГО за
    committed. Копія мусить представляти те саме зафіксоване наповнення,
    що й файл, чию ідентичність гейт звітує як `db_snapshot`."""
    src = tmp_path / "snap.db"
    src.write_bytes(b"main-content")
    (tmp_path / "snap.db-wal").write_bytes(b"wal-content")
    (tmp_path / "snap.db-shm").write_bytes(b"shm-content")

    with gate._scratch_copy(str(src)) as copy_path:
        assert Path(copy_path + "-wal").read_bytes() == b"wal-content"
        assert Path(copy_path + "-shm").read_bytes() == b"shm-content"
    # прибирається все, включно з сайдкарами, після виходу з контексту
    assert not os.path.exists(copy_path)
    assert not os.path.exists(copy_path + "-wal")
    assert not os.path.exists(copy_path + "-shm")


def test_gate_no_snapshot_yes_live_skips_scratch_copy(db, tmp_path, monkeypatch):
    """`--no-snapshot --yes-live`, наведені на СПРАВДІ ЖИВУ БД, лишається
    єдиним документованим обходом (docstring модуля, D16) — вимір іде проти
    буквального `--db`, не копії."""
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
    """D16: та сама пара прапорців, наведена на ЦІЛЬ, ЩО НЕ Є живою БД (тут —
    знімок/довільний файл, `is_live_db` за замовчуванням каже False), НЕ сміє
    вимикати `_scratch_copy` — інакше `retrieval.search` ставить постійний
    WAL на файл, чию ідентичність гейт же й штампує як `db_snapshot`, і
    будь-яка лінія, знята до цього, назавжди читається як «інший провенанс»."""
    # Знахідка 4 (review-round-2): ціль — справжній знімок (journal_mode=delete),
    # не фікстура `db` (уже journal_mode=wal після init_database), інакше
    # твердження нижче вакуумні. Див. `_real_snapshot`.
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
    assert seen_paths and seen_paths[0] != snap  # scratch-копія, не сам файл

    # Знахідка 15: --no-snapshot нічого не пропустив мовчки — гейт сказав про
    # це в рантаймі, не лише в довідці.
    err = capsys.readouterr().err
    assert "--no-snapshot" in err and "захисна копія" in err


# ============================================================
# gate.py — unlabeled/negative/по зрізах
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
    assert calls == ["b"]  # "a" (unlabeled) взагалі не пошуковується


def test_gate_all_unlabeled_exits_2_never_pass(db, tmp_path, monkeypatch, capsys):
    """D2/знахідка 2: набір без жодного `status="labeled"` пункту (саме те,
    що видає build_golden без розмітки) — помилка даних, НЕ PASS. Раніше
    друкувало `[gate] PASS k=8,12` і виходило з 0, навіть під -q."""
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
    assert data["aggregate"]["2"]["n"] == 1  # лише "b" (labeled) у агрегації
    ids_in_report = [r["id"] for r in data["per_item"]["2"]]
    assert "neg" in ids_in_report and "b" in ids_in_report


# ============================================================
# gate.py — базова лінія: запис + per-item diff + регресія пункту
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

    # "a" перестає знаходитись -> item hit->miss -> регресія відносно лінії
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"b": (2, "Дзвінок")}))
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path)])
    assert rc2 == 1


def test_gate_fails_on_item_regression_alone_when_mean_drop_allowed(db, tmp_path, monkeypatch):
    """Той самий сценарій, що вище, але з --max-drop 1.0 (падіння mean більше
    НЕ провалює гейт) — провал лишається лише через item_regressions (default
    --max-item-regressions 0), ізольовано від причини mean_drop."""
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
    """Recall падає (0.5->0.0), але bool hit лишається False до і після
    (regression рахує лише True->False) — провал лише через mean_drop."""
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
    assert baseline["items"]["a"]["2"]["hit"] is False  # 0.5 < 0.999 -> не hit

    monkeypatch.setattr("app.services.retrieval.search", _partial([]))  # recall=0.0
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.0",
                      "--baseline", str(baseline_path), "--max-item-regressions", "5"])
    assert rc2 == 1  # False->False (без регресії пункту), провал лише через mean_drop


# ============================================================
# gate.py — провенанс базової лінії (D5, знахідки 6, 13)
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
    """D12/знахідка 15: регресований прогін не стає мовчки новою істиною."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    monkeypatch.setattr("app.services.retrieval.search", _bad_search)
    baseline_path = tmp_path / "baseline.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                     "--write-baseline", str(baseline_path)])
    assert rc == 1
    assert not baseline_path.exists()


def test_gate_baseline_vector_available_mismatch_exits_2(db, tmp_path, monkeypatch):
    """Знахідка 6: e5 недоступна в цьому прогоні — не має тихо читатись як
    ранжувальна регресія відносно лінії, знятої з доступною e5."""
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
    """Знахідка 13: лінія, знята за іншим знімком, не видає тихий вердикт."""
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
    """Знахідка 6: лінія знята над іншим набором (інші id) не сміє тихо
    вважатись порівнянною лише тому, що знімок/vector_available/rerank
    збіглись — інакше mean_drop рахує різницю проти чужих пунктів."""
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
    """Стара лінія (до цього ремонту) без db_snapshot/vector_available/rerank
    у новому форматі — провенанс невідомий, не порівнюваний мовчки."""
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
# gate.py — гейт не сміє PASS, не перевіривши нічого (D2, review-round-2,
# знахідка 1: двоє дверей)
# ============================================================

def test_gate_k_without_threshold_or_baseline_entry_exits_2_not_pass(db, tmp_path, monkeypatch):
    """Двері (а): лінія знята на k=2, прогін на k=3 — --min-recall не містить
    запису для 3, а лінія теж не містить (записана лише на k=2). Жодна
    перевірка для k=3 не існує, тому навіть тотальний обвал (recall 0.0) не
    сміє видати PASS/0 — вердикту немає (2). ДО правки цей тест падає:
    гейт друкує PASS і повертає 0."""
    items = [_item("a", 1, "TG чат")]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    baseline_path = tmp_path / "baseline.json"
    rc1 = gate.main(["--golden", golden, "--db", db, "--k", "2", "--min-recall", "2=0.9",
                      "--write-baseline", str(baseline_path)])
    assert rc1 == 0

    monkeypatch.setattr("app.services.retrieval.search", _bad_search)  # тотальний обвал
    rc2 = gate.main(["--golden", golden, "--db", db, "--k", "3", "--min-recall", "2=0.9",
                      "--baseline", str(baseline_path)])
    assert rc2 == 2


def test_gate_name_only_items_check_source_name_hit_rate_not_silent_pass(db, tmp_path, monkeypatch):
    """Двері (б), досяжні на дефолтних --k 8,12: пункт розмічений лише
    `expected_source_name_contains` (README радить це як стійкішу до дрейфу
    id альтернативу) лишає recall_at_k.mean = None — абсолютний поріг мусить
    звірятись проти source_name_hit_rate, інакше тотальний промах мовчки
    минається. ДО правки цей тест падає: гейт друкує PASS k=8,12 і 0,
    попри те що очікуване джерело жодного разу не зʼявилось у видачі."""
    items = [{"id": "a", "question": "a", "slice": "calls", "status": "labeled",
              "expected_transcription_ids": [], "expected_source_name_contains": ["TG чат"]}]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _bad_search)  # ціль ніколи не в топі
    rc = gate.main(["--golden", golden, "--db", db])  # дефолтні --k 8,12 і --min-recall
    assert rc == 1  # реальний вердикт якості (FAIL), не мовчазний PASS


def test_gate_name_only_items_pass_when_source_name_found(db, tmp_path, monkeypatch):
    """Той самий набір, що вище, але ціль реально знаходиться — перевірка
    source_name_hit_rate тепер РЕАЛЬНО фіксує успіх, не просто ніколи не
    провалює."""
    items = [{"id": "a", "question": "a", "slice": "calls", "status": "labeled",
              "expected_transcription_ids": [], "expected_source_name_contains": ["TG чат"]}]
    golden = _golden_jsonl(tmp_path, items)
    monkeypatch.setattr("app.services.retrieval.search", _good_search({"a": (1, "TG чат")}))
    rc = gate.main(["--golden", golden, "--db", db])
    assert rc == 0


def test_default_min_recall_does_not_exceed_rule_applied_to_calibration_line():
    """Ремонтний раунд 1 (D15.4): корпус дрейфує в обидва боки (recall@12
    0.5357→0.6071 за сім годин фонового інджесту 03.09), тож рівність
    `_DEFAULT_MIN_RECALL` числам ОДНІЄЇ знятої лінії — вакуумна умова, яка
    протухає при наступному знятті. Інваріант, що лишається правдою при
    дрейфі: шипований дефолт — найнижче з виведених по всіх лініях, отже
    НЕ ПЕРЕВИЩУЄ порогу, виведеного з ЧИННОЇ лінії. Числа "виміряної лінії"
    читаються з `evals/baseline.local.json` на диску (не вшиті вручну) —
    файл gitignored, тож на машині без знятої лінії тест пропускається з
    явною причиною, а не мовчки проходить вакуумно."""
    baseline_path = _PROJECT_ROOT / "evals" / "baseline.local.json"
    if not baseline_path.exists():
        pytest.skip(f"{baseline_path} відсутній (gitignored) — калібрувальну лінію "
                    "не знято на цій машині, немає чинних чисел для звірки")
    line = json.loads(baseline_path.read_text(encoding="utf-8"))
    aggregate = line["aggregate"]
    default = gate._parse_min_recall(gate._DEFAULT_MIN_RECALL)
    assert default, "_DEFAULT_MIN_RECALL має задавати хоч один k"
    checked = 0
    for k_str, stats in aggregate.items():
        k = int(k_str)
        if k not in default:
            continue
        derived_ceiling = gate._derive_min_recall_threshold(stats["recall_at_k"], stats["n"])
        assert default[k] <= derived_ceiling + 1e-9, (
            f"k={k}: дефолт {default[k]} перевищує верхню межу {derived_ceiling}, "
            f"виведену з чинної лінії ({baseline_path}, n={stats['n']}) — "
            "дефолт треба знизити (D15.4: він детектор обвалу, не стеля)"
        )
        checked += 1
    assert checked == len(default), (
        "чинна лінія не покриває всі k з _DEFAULT_MIN_RECALL — перевірку зроблено не для всіх")


# ============================================================
# gate.py — збій інфраструктури під час заміру ≠ вердикт якості (D3, знахідка 4)
# ============================================================

def test_gate_search_exception_exits_2_not_1(db, tmp_path, monkeypatch, capsys):
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])

    def _boom(db_path, query, top_k=8, category_id=None, **kwargs):
        raise RuntimeError("модель не піднялась")

    monkeypatch.setattr("app.services.retrieval.search", _boom)
    rc = gate.main(["--golden", golden, "--db", db, "--k", "8"])
    assert rc == 2
    assert "модель не піднялась" in capsys.readouterr().err


# ============================================================
# gate.py — вивід
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
# gate.py — помилки використання (exit 2)
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
    assert "рядку 2" in capsys.readouterr().err


def test_gate_json_out_requires_local_convention_exits_2(db, tmp_path):
    """D10/знахідка 19: --json-out пише реальні питання й transcription_id —
    єдина нова поверхня, яка мусить впасти під конвенцію приватності *.local.*."""
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
    """Знахідка 3 (review-round-2): `gate_run.local.json` у корені репозиторію
    відповідає конвенції ІМЕНІ, але git його ВІДСТЕЖИТЬ — нема кореневого
    правила `*.local.json` у .gitignore. Має впасти під exit 2 до будь-якого
    запису на диск."""
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
    """`.gitignore` покриває лише `evals/*.local.json` НАПРЯМУ (без `**`) —
    вкладений каталог на кшталт `evals/runs/` під це правило не підпадає.
    Раніше filename-евристика приймала такий шлях; git його відстежить."""
    golden = _golden_jsonl(tmp_path, [_item("a", 1, "TG чат")])
    path = gate._PROJECT_ROOT / "evals" / "runs" / "x.local.json"
    rc = gate.main(["--golden", golden, "--db", db, "--k", "2",
                     "--json-out", str(path)])
    assert rc == 2
    assert not path.exists()


def test_gate_json_out_real_evals_dir_path_still_accepted(db, tmp_path, monkeypatch):
    """Контроль: шлях, що ДІЙСНО підпадає під `evals/*.local.json`
    у справжньому репозиторії, git не рахує "гейт зламав легітимний випадок"."""
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
    """Знахідка 15: довідка `--no-snapshot` не сміє стверджувати, що прапорець
    завжди означає "напряму на --db" — на не-живій цілі захисна копія
    (_scratch_copy) все одно застосовується (D16)."""
    with pytest.raises(SystemExit):
        gate.main(["--help"])
    help_text = capsys.readouterr().out
    assert "--no-snapshot" in help_text
    assert "захисна копія" in help_text or "_scratch_copy" in help_text


# ============================================================
# gate.py — наскрізний прогін на РЕАЛЬНОМУ знімку і e5 (C9: маркер `eval`,
# скіп з причиною, якщо нема golden-set/архіву/моделі)
# ============================================================

@pytest.mark.eval
def test_gate_real_snapshot_passes_with_local_golden_set(tmp_path, monkeypatch):
    """Canary, не строгий CI-гейт (знахідка 14): архів дрейфує (нові записи,
    переінджест), тож PASS тут — сигнал "перевір вручну, якщо почервоніло",
    а не залізний контракт коду. Знімок пишеться у tmp_path, НЕ в
    evals/snapshots/ реального репо — інакше кожен прогін лишає 315-МБ
    артефакт у робочому дереві (finding 14)."""
    golden_path = _PROJECT_ROOT / "evals" / "golden_set.local.json"
    db_path = _PROJECT_ROOT / "whisper_history.db"
    if not golden_path.exists():
        pytest.skip(f"нема {golden_path} — локальний golden-set не розмічено")
    if not db_path.exists():
        pytest.skip(f"нема {db_path} — немає локального архіву")
    from app.services import embeddings
    if not embeddings.is_available():
        pytest.skip(f"e5 недоступна: {embeddings.unavailability_reason()}")

    monkeypatch.setattr(ev_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")

    rc = gate.main(["--golden", str(golden_path), "--db", str(db_path), "--k", "8,12", "-q"])
    assert rc == 0
