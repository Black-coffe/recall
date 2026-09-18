"""Смок-тести трьох CLI eval-gate-02: build_golden / build_tasks_golden / tasks_eval.

Усе офлайн — синтетичний лог MCP, тимчасова SQLite (мінімальні таблиці:
transcriptions/action_items/entities/entity_aliases), `app.services.retrieval.search`
підмінено. Жодних реальних запитів чи назв архіву тут немає (Non-goals історії).
"""
import json
import sqlite3

import pytest

from evals import build_golden, build_tasks_golden, tasks_eval


# ============================================================
# Фікстури
# ============================================================

def _make_db(path: str) -> None:
    """Мінімальні таблиці, яких вистачає трьом CLI (не повна `init_database` —
    вони й так торкаються лише чотирьох таблиць)."""
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


def _fake_search(chunks_by_query: dict):
    def _search(db_path, query, top_k=8):
        return {"chunks": chunks_by_query.get(query, [])}
    return _search


# ============================================================
# build_golden — мінінг логу (C8)
# ============================================================

def test_mine_queries_parses_dedupes_and_survives_truncation(tmp_path):
    log = tmp_path / "mcp_calls.log"
    truncated_question = '"Дуже довге питання про архів без закритої лапки бо лог обрізає рядок'
    log.write_text(
        "10:00:00 ── initialize (handshake)\n"
        '10:00:01 →   START search_archive args={"query": "бюджет на Q2", "top_k": 8}\n'
        # дубль нормалізовано (регістр/пробіли) — не має влізти вдруге
        '10:00:02 →   START search_archive args={"query": "  Бюджет НА q2  ", "top_k": 5}\n'
        '10:00:03 →   START ask_archive args={"question": "хто відповідає за онбординг", "k": 12}\n'
        # обрізаний JSON (600-символьний ліміт mcp_server.py) — без закритої лапки/дужки
        f'10:00:04 →   START ask_archive args={{"question": {truncated_question}\n'
        '10:00:05 OK  search_archive 12ms\n'
        '10:00:06 →   START get_transcript args={"transcription_id": 5}\n',
        encoding="utf-8",
    )
    out = build_golden.mine_queries(str(log))
    questions = [q["question"] for q in out]
    assert questions[0] == "бюджет на Q2"
    assert "хто відповідає за онбординг" in questions
    # третій рядок (дубль) не додався
    assert len(questions) == 3
    # обрізаний рядок все одно дав придатний (нехай і обрізаний) текст питання
    assert questions[2].startswith("Дуже довге питання")
    assert out[0]["tool"] == "search_archive"
    assert out[2]["tool"] == "ask_archive"


def test_slice_majority_vote():
    tg_chunks = [{"source_type": "telegram"}] * 3 + [{"source_type": "file"}] * 2
    docs_chunks = [{"source_type": "document"}] * 3 + [{"source_type": "youtube"}] * 2
    calls_chunks = [{"source_type": "file"}, {"source_type": "recording"}]
    assert build_golden._slice_for_chunks(tg_chunks) == "tg"
    assert build_golden._slice_for_chunks(docs_chunks) == "docs"
    assert build_golden._slice_for_chunks(calls_chunks) == "calls"
    assert build_golden._slice_for_chunks([]) == "calls"


def test_slice_tie_resolved_by_first_seen_order():
    """Нічия за кількістю (по 1) — перемагає той зріз, що зʼявився першим
    у видачі (той самий принцип, що в
    test_build_golden_writes_unlabeled_items_with_candidates нижче)."""
    tie_calls_first = [{"source_type": "file"}, {"source_type": "telegram"}]
    assert build_golden._slice_for_chunks(tie_calls_first) == "calls"
    tie_tg_first = [{"source_type": "telegram"}, {"source_type": "file"}]
    assert build_golden._slice_for_chunks(tie_tg_first) == "tg"


def test_slice_comment_provenance_does_not_vote_toward_calls():
    """Коментар (`source_type="comment"`, `app/services/retrieval.py`) не додає
    голос за `calls` — інакше питання, відповідь на яке взято здебільшого з
    коментарів у TG-нитці, хибно осідає в `calls` замість `tg` (finding 22)."""
    tg_with_comments = [{"source_type": "telegram"}] + [{"source_type": "comment"}] * 3
    assert build_golden._slice_for_chunks(tg_with_comments) == "tg"
    # немає жодного не-коментарного кандидата — голосувати нема за що, дефолт "calls"
    # (той самий дефолт, що для порожнього списку, а не голос коментаря)
    assert build_golden._slice_for_chunks([{"source_type": "comment"}] * 3) == "calls"


def test_scratch_copy_removes_temp_file_when_copyfile_raises(db, monkeypatch):
    created = []
    real_mkstemp = build_golden.tempfile.mkstemp

    def _tracking_mkstemp(*a, **kw):
        fd, path = real_mkstemp(*a, **kw)
        created.append(path)
        return fd, path

    def _raising_copyfile(*a, **kw):
        raise OSError("диск повний")

    monkeypatch.setattr(build_golden.tempfile, "mkstemp", _tracking_mkstemp)
    monkeypatch.setattr(build_golden.shutil, "copyfile", _raising_copyfile)

    with pytest.raises(OSError):
        with build_golden._scratch_copy(db):
            pass  # copyfile кидає до yield — сюди не дійде

    assert created, "mkstemp мав бути викликаний"
    assert not build_golden.os.path.exists(created[0]), "заглушка від mkstemp не прибралася"


# ============================================================
# build_golden — CLI end-to-end
# ============================================================

def test_build_golden_writes_unlabeled_items_with_candidates(tmp_path, db, monkeypatch):
    log = tmp_path / "mcp_calls.log"
    log.write_text(
        '11:00:00 →   START search_archive args={"query": "статус проєкту Х", "top_k": 8}\n',
        encoding="utf-8",
    )
    out = tmp_path / "golden.local.jsonl"
    fake_chunks = [
        {"transcription_id": 1, "source_name": "Дзвінок 1", "source_type": "file"},
        {"transcription_id": 2, "source_name": "TG чат", "source_type": "telegram"},
    ]

    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"статус проєкту Х": fake_chunks}))

    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out)])
    assert rc == 0

    items = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "unlabeled"
    assert item["question"] == "статус проєкту Х"
    assert item["expected_transcription_ids"] == []
    assert item["candidates"] == [
        {"transcription_id": 1, "source_name": "Дзвінок 1", "source_type": "file"},
        {"transcription_id": 2, "source_name": "TG чат", "source_type": "telegram"},
    ]
    assert item["slice"] == "calls"  # 1 file vs 1 telegram — file зверху, нічия рахує file


def test_build_golden_merge_preserves_labeled_and_skips_known(tmp_path, db, monkeypatch):
    out = tmp_path / "golden.local.jsonl"
    existing = [
        {"id": "mined-0001", "question": "стара розмічена", "slice": "calls", "category_id": None,
         "expected_transcription_ids": [9], "expected_source_name_contains": [], "expected_facts": [],
         "notes": "", "source": "mcp_log", "status": "labeled", "candidates": []},
    ]
    out.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in existing) + "\n", encoding="utf-8")

    log = tmp_path / "mcp_calls.log"
    log.write_text(
        # дубль вже розміченого питання — не повинен додатись вдруге
        '12:00:00 →   START search_archive args={"query": "стара розмічена", "top_k": 8}\n'
        '12:00:01 →   START search_archive args={"query": "нове питання", "top_k": 8}\n',
        encoding="utf-8",
    )

    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"нове питання": []}))

    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out), "--merge"])
    assert rc == 0

    items = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(items) == 2
    assert items[0] == existing[0]  # байт-у-байт за змістом
    assert items[1]["question"] == "нове питання"
    assert items[1]["status"] == "unlabeled"


def test_build_golden_refuses_live_db_without_yes_live(tmp_path, db, monkeypatch):
    monkeypatch.setattr(build_golden, "is_live_db", lambda path: True)
    rc = build_golden.main(["--db", db, "--out", str(tmp_path / "x.local.jsonl")])
    assert rc == 2


# ============================================================
# build_golden — безпека перезапису ручної розмітки (D9, finding 12)
# ============================================================

def _labeled_item(id_="mined-0001", question="розмічене питання"):
    return {"id": id_, "question": question, "slice": "calls", "category_id": None,
            "expected_transcription_ids": [7], "expected_source_name_contains": [],
            "expected_facts": [], "notes": "", "source": "mcp_log", "status": "labeled",
            "candidates": []}


def test_build_golden_refuses_overwrite_without_merge_when_labeled_present(tmp_path, db, monkeypatch, capsys):
    out = tmp_path / "golden.local.jsonl"
    original = json.dumps(_labeled_item(), ensure_ascii=False) + "\n"
    out.write_text(original, encoding="utf-8")

    log = tmp_path / "mcp_calls.log"
    log.write_text('14:00:00 →   START search_archive args={"query": "нове", "top_k": 8}\n', encoding="utf-8")
    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"нове": []}))

    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "--merge" in err
    assert "--yes-overwrite-labeled" in err
    assert out.read_text(encoding="utf-8") == original  # ручна розмітка не зачеплена


def test_build_golden_yes_overwrite_labeled_forces_overwrite(tmp_path, db, monkeypatch):
    out = tmp_path / "golden.local.jsonl"
    out.write_text(json.dumps(_labeled_item(), ensure_ascii=False) + "\n", encoding="utf-8")

    log = tmp_path / "mcp_calls.log"
    log.write_text('14:00:01 →   START search_archive args={"query": "нове після форсу", "top_k": 8}\n',
                    encoding="utf-8")
    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"нове після форсу": []}))

    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out), "--yes-overwrite-labeled"])
    assert rc == 0

    items = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # свідомий прапорець дійсно перезаписує — старе розмічене питання зникає
    assert len(items) == 1
    assert items[0]["question"] == "нове після форсу"


def test_build_golden_merge_invalid_existing_out_exits_2_not_traceback(tmp_path, db):
    out = tmp_path / "golden.local.jsonl"
    out.write_text("{не валідний json\n", encoding="utf-8")
    rc = build_golden.main(["--db", db, "--out", str(out), "--merge"])
    assert rc == 2


# ============================================================
# build_golden — унікальність id (D13/finding 16)
# ============================================================

def test_build_golden_next_id_skips_gap_after_manual_deletion(tmp_path, db, monkeypatch):
    """`mined-0002` видалено вручну — новий пункт не повинен ані перевикористати
    його номер, ані зіткнутися з `mined-0003`, що лишився."""
    out = tmp_path / "golden.local.jsonl"
    existing = [_labeled_item("mined-0001", "перше"), _labeled_item("mined-0003", "третє")]
    out.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in existing) + "\n", encoding="utf-8")

    log = tmp_path / "mcp_calls.log"
    log.write_text('15:00:00 →   START search_archive args={"query": "нове після дірки", "top_k": 8}\n',
                    encoding="utf-8")
    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"нове після дірки": []}))

    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out), "--merge"])
    assert rc == 0

    ids = [json.loads(line)["id"] for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(ids) == len(set(ids))  # без дублів
    assert ids[-1] == "mined-0004"  # не mined-0002 (дірка) і не mined-0003 (колізія)


def test_build_golden_refuses_to_write_when_existing_ids_collide(tmp_path, db, capsys):
    out = tmp_path / "golden.local.jsonl"
    dupe_a = _labeled_item("mined-0001", "перше")
    dupe_b = _labeled_item("mined-0001", "друге")  # той самий id — зіпсований файл
    original = "\n".join(json.dumps(it, ensure_ascii=False) for it in (dupe_a, dupe_b)) + "\n"
    out.write_text(original, encoding="utf-8")

    # неіснуючий лог — mine_queries віддає [] (без цього main() тягне дефолтний
    # logs/mcp_calls.log і б'ється об retrieval.search на тестовій БД без chunks)
    rc = build_golden.main(["--mcp-log", str(tmp_path / "no_such.log"), "--db", db,
                             "--out", str(out), "--merge"])
    assert rc == 2
    assert out.read_text(encoding="utf-8") == original  # дублі не згорнулися мовчки в новий файл


# ============================================================
# build_golden — --stats (дефіцит до цілі SLICE_TARGET на зріз)
# ============================================================

def test_print_stats_computes_deficit_per_slice(capsys):
    items = (
        [{"slice": "calls", "status": "labeled"} for _ in range(3)]
        + [{"slice": "tg", "status": "unlabeled"}]
        + [{"slice": "docs", "status": "negative"}]
    )
    build_golden.print_stats(items)
    rows = {line.split()[0]: line.split() for line in capsys.readouterr().out.splitlines()
            if line.split() and line.split()[0] in build_golden.SLICES}
    # calls: 3 labeled, ціль=SLICE_TARGET -> дефіцит=SLICE_TARGET-3
    assert rows["calls"][-1] == str(build_golden.SLICE_TARGET - 3)
    # tg/docs: 0 labeled (unlabeled/negative не рахуються) -> дефіцит=SLICE_TARGET
    assert rows["tg"][-1] == str(build_golden.SLICE_TARGET)
    assert rows["docs"][-1] == str(build_golden.SLICE_TARGET)


def test_build_golden_cli_stats_flag_prints_deficit_table(tmp_path, db, monkeypatch, capsys):
    log = tmp_path / "mcp_calls.log"
    log.write_text('11:00:00 →   START search_archive args={"query": "тест", "top_k": 8}\n',
                    encoding="utf-8")
    out = tmp_path / "golden.local.jsonl"

    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"тест": []}))

    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out), "--stats"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "дефіцит" in captured
    assert "calls" in captured


def test_print_stats_treats_missing_status_as_labeled_legacy_default(capsys):
    """Legacy-пункт без явного "status" (старий golden_set.local.json) — це вже
    розмічений вручну запис (golden_io._read_legacy за умовчанням "labeled"),
    а не щойно намінений unlabeled."""
    items = [{"slice": "calls", "question": "стара розмічена", "expected_facts": ["x"]}]
    build_golden.print_stats(items)
    captured = capsys.readouterr().out
    assert "labeled 1 / target" in captured
    rows = {line.split()[0]: line.split() for line in captured.splitlines()
            if line.split() and line.split()[0] in build_golden.SLICES}
    assert rows["calls"][1] == "1"  # колонка "labeled"


def test_print_stats_prints_total_labeled_over_target(capsys):
    items = [{"slice": "calls", "status": "labeled"} for _ in range(5)] + [
        {"slice": "tg", "status": "unlabeled"},
    ]
    build_golden.print_stats(items)
    captured = capsys.readouterr().out
    assert f"labeled 5 / target {build_golden.TOTAL_TARGET}" in captured


# ============================================================
# build_golden — мінінг: впорядкування за частотою (production-rag-wave-b-01)
# ============================================================

def test_mine_queries_orders_repeated_questions_first_with_frequency_in_notes(tmp_path):
    log = tmp_path / "mcp_calls.log"
    log.write_text(
        # "рідкісне питання" зустрічається один раз, а "часте питання" — тричі,
        # але перша поява "часте питання" йде ПІСЛЯ рідкісного в самому лозі —
        # частота, а не порядок появи, вирішує підсумкове впорядкування.
        '10:00:00 →   START search_archive args={"query": "рідкісне питання", "top_k": 8}\n'
        '10:00:01 →   START search_archive args={"query": "часте питання", "top_k": 8}\n'
        '10:00:02 →   START search_archive args={"query": "Часте ПИТАННЯ", "top_k": 8}\n'
        '10:00:03 →   START search_archive args={"query": "часте питання", "top_k": 8}\n',
        encoding="utf-8",
    )
    out = build_golden.mine_queries(str(log))
    questions = [q["question"] for q in out]
    assert questions == ["часте питання", "рідкісне питання"]
    assert "3" in out[0]["notes"]
    assert out[1].get("notes", "") == ""  # частота 1 — нотатку не додаємо


# ============================================================
# build_golden — legacy `{"description":..., "items":[...]}` (golden_set.local.json)
# ============================================================

def _legacy_item():
    return {
        "id": "kyiv-app-stack-01",
        "question": "Який технологічний стек обрали?",
        "category_id": None,
        "expected_transcription_ids": [109],
        "expected_source_name_contains": ["Києва"],
        "expected_facts": ["Flutter, .NET, PostgreSQL"],
        "notes": "Технічна зустріч.",
    }


def test_build_golden_merges_into_legacy_json_preserves_old_items_verbatim(tmp_path, db, monkeypatch):
    out = tmp_path / "golden_set.local.json"
    old_item = _legacy_item()
    payload = {"description": "Локальний набір, НЕ комітити.", "items": [old_item]}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log = tmp_path / "mcp_calls.log"
    log.write_text('13:00:00 →   START search_archive args={"query": "нове мінене питання", "top_k": 8}\n',
                    encoding="utf-8")
    fake_chunks = [{"transcription_id": 3, "source_name": "TG нитка", "source_type": "telegram"}]
    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"нове мінене питання": fake_chunks}))

    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out), "--merge"])
    assert rc == 0

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["description"] == "Локальний набір, НЕ комітити."
    assert len(data["items"]) == 2
    assert data["items"][0] == old_item  # 15 наявних пунктів — незмінні
    new_item = data["items"][1]
    assert new_item["question"] == "нове мінене питання"
    assert new_item["status"] == "unlabeled"
    assert new_item["source"] == "mcp_log"
    assert new_item["slice"] == "tg"
    assert new_item["candidates"] == fake_chunks
    assert new_item["expected_transcription_ids"] == []  # НЕ розмічено (Non-goals)


def test_build_golden_second_run_same_snapshot_adds_zero_new_items(tmp_path, db, monkeypatch, capsys):
    out = tmp_path / "golden_set.local.json"
    payload = {"description": "d", "items": [_legacy_item()]}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log = tmp_path / "mcp_calls.log"
    log.write_text('13:00:00 →   START search_archive args={"query": "стабільне питання", "top_k": 8}\n',
                    encoding="utf-8")
    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"стабільне питання": []}))

    rc1 = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out), "--merge"])
    assert rc1 == 0
    after_first = json.loads(out.read_text(encoding="utf-8"))
    assert len(after_first["items"]) == 2

    capsys.readouterr()
    rc2 = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out), "--merge"])
    assert rc2 == 0
    assert "нових=0" in capsys.readouterr().out
    after_second = json.loads(out.read_text(encoding="utf-8"))
    assert after_second == after_first  # той самий знімок — 0 нових пунктів


# ============================================================
# build_golden — --limit ріже лише лог, не ask_log (production-rag-wave-b-01)
# ============================================================

def _add_ask_log(db_path: str, rows: list[tuple]) -> None:
    """`ask_log` (міграція v41) — лише колонки, які читає `mine_ask_log`."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ask_log (id INTEGER PRIMARY KEY, question TEXT, scope_json TEXT, "
                 "source_ids_json TEXT, rating INTEGER, note TEXT)")
    conn.executemany("INSERT INTO ask_log (question, scope_json, source_ids_json, rating, note) "
                     "VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_build_golden_limit_cuts_log_questions_but_keeps_ask_log(tmp_path, db, monkeypatch, capsys):
    """Спільний ліміт з'їдали сотні питань логу, і прогін «з обох джерел» мовчки
    давав одне — ask_log (десяток рядків з оцінкою власника) ліміт не ріже."""
    _add_ask_log(db, [("питання власника з оцінкою", "{}", "[]", -1, "")])
    log = tmp_path / "mcp_calls.log"
    log.write_text(
        '10:00:00 →   START search_archive args={"query": "часте питання логу", "top_k": 8}\n'
        '10:00:01 →   START search_archive args={"query": "часте питання логу", "top_k": 8}\n'
        '10:00:02 →   START search_archive args={"query": "рідке питання логу", "top_k": 8}\n',
        encoding="utf-8",
    )
    from app.services import retrieval
    monkeypatch.setattr(retrieval, "search", _fake_search({"часте питання логу": []}))

    out = tmp_path / "golden.jsonl"
    rc = build_golden.main(["--mcp-log", str(log), "--db", db, "--out", str(out),
                             "--from-ask-log", "--limit", "1"])
    assert rc == 0
    items = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    questions = [it["question"] for it in items]
    assert questions == ["часте питання логу", "питання власника з оцінкою"]
    assert "рідке питання логу" not in questions  # зрізане лімітом
    assert "пропущено_лімітом=1" in capsys.readouterr().out


# ============================================================
# build_tasks_golden — стратифікована вибірка (C7)
# ============================================================

def _seed_action_items(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO transcriptions (id, source_type, meeting_date, created_at) "
                 "VALUES (100, 'telegram', '2026-06-01', '2026-06-01T10:00:00')")
    rows = [
        # (id, source, due, owner_name)
        (1, None, "2026-06-05", "Андрій"),
        (2, None, "2026-06-06", "Андрій"),
        (3, None, None, "Юля"),
        (4, None, None, "Юля"),
        (5, "tg_thread", "2026-06-07", "Олена"),
        (6, "tg_thread", "2026-06-08", "Олена"),
        (7, "tg_thread", None, "Павло"),
        (8, "tg_thread", None, "Павло"),
        # коментарний провенанс — НЕ входить у C7 (лише null|tg_thread)
        (9, "comment", "2026-06-09", "Хтось"),
    ]
    for aid, source, due, owner in rows:
        conn.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                     "owner_entity_id, due, source) VALUES (?, 100, ?, ?, NULL, ?, ?)",
                     (aid, f"задача {aid}", owner, due, source))
    conn.commit()
    conn.close()


def test_sample_action_items_excludes_comment_source_and_covers_strata(db):
    _seed_action_items(db)
    from evals.graph_links import open_readonly
    conn = open_readonly(db)
    try:
        picked = build_tasks_golden.sample_action_items(conn, n=8)
    finally:
        conn.close()

    ids = {r["id"] for r in picked}
    assert 9 not in ids  # comment-джерело виключено
    assert len(picked) == 8  # рівно по 2 на кожну з 4 страт

    seen_keys = {(r["source"], bool(r["due"] and str(r["due"]).strip())) for r in picked}
    assert seen_keys == {(None, True), (None, False), ("tg_thread", True), ("tg_thread", False)}


def test_build_tasks_golden_merge_does_not_duplicate_action_item_id(tmp_path, db):
    _seed_action_items(db)
    out = tmp_path / "tasks.local.jsonl"

    rc1 = build_tasks_golden.main(["--db", db, "--out", str(out), "--n", "8", "--merge"])
    assert rc1 == 0
    items1 = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(items1) == 8

    rc2 = build_tasks_golden.main(["--db", db, "--out", str(out), "--n", "8", "--merge"])
    assert rc2 == 0
    items2 = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    ids2 = [it["action_item_id"] for it in items2]
    assert len(ids2) == len(set(ids2))  # без дублів
    assert len(items2) == 8  # той самий детермінований набір, нового нема


def test_build_tasks_golden_refuses_live_db_without_yes_live(tmp_path, db, monkeypatch):
    monkeypatch.setattr(build_tasks_golden, "is_live_db", lambda path: True)
    rc = build_tasks_golden.main(["--db", db, "--out", str(tmp_path / "x.local.jsonl")])
    assert rc == 2


# ============================================================
# tasks_eval — due/owner accuracy + gate (C7/C3)
# ============================================================

def _seed_owner_graph(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO transcriptions (id, source_type, meeting_date, created_at) "
                 "VALUES (100, 'telegram', '2026-06-01', '2026-06-01T10:00:00')")
    conn.execute("INSERT INTO entities (id, canonical_name) VALUES (1, 'Андрій')")
    conn.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (1, 'Andrew')")
    # #10 — правильно звʼязана задача (owner_entity_id=1), #11 — нічого не звʼязано
    conn.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                 "owner_entity_id, due, source) VALUES (10, 100, 'зробити щось', 'Андрій', 1, "
                 "'2026-06-02', NULL)")
    conn.execute("INSERT INTO action_items (id, transcription_id, task, owner_name, "
                 "owner_entity_id, due, source) VALUES (11, 100, 'інша задача', NULL, NULL, "
                 "'2026-06-03', NULL)")
    conn.commit()
    conn.close()


def _tasks_golden_jsonl(tmp_path, items):
    path = tmp_path / "tasks.local.jsonl"
    path.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n", encoding="utf-8")
    return str(path)


def test_evaluate_due_and_owner_accuracy_via_alias(db):
    _seed_owner_graph(db)
    items = [
        {"id": "task-0001", "action_item_id": 10, "transcription_id": 100, "task": "зробити щось",
         "owner_name_raw": "Андрій", "due_raw": "2026-06-02", "meeting_date": "2026-06-01",
         "source": None, "truth": {"owner": "Andrew", "due_date": "2026-06-02", "due_precision": "day"},
         "status": "labeled"},
        {"id": "task-0002", "action_item_id": 11, "transcription_id": 100, "task": "інша задача",
         "owner_name_raw": None, "due_raw": "2026-06-03", "meeting_date": "2026-06-01",
         "source": None, "truth": {"owner": None, "due_date": "2026-01-01", "due_precision": "day"},
         "status": "labeled"},
        {"id": "task-0003", "action_item_id": 999, "transcription_id": 100, "task": "ще не розмічена",
         "owner_name_raw": None, "due_raw": None, "meeting_date": "2026-06-01",
         "source": None, "truth": {"owner": None, "due_date": None, "due_precision": None},
         "status": "unlabeled"},
    ]
    from evals.graph_links import open_readonly
    conn = open_readonly(db)
    try:
        result = tasks_eval.evaluate(items, conn)
    finally:
        conn.close()

    assert result["n_total"] == 3
    assert result["n_labeled"] == 2  # unlabeled не рахується
    # task-0001: власник збігся по аліасу "Andrew", дедлайн збігся → обидва ok
    # task-0002: власник збігся (обидва None), дедлайн НЕ збігся (правда 2026-01-01 != 2026-06-03)
    assert result["due_accuracy"] == 0.5
    assert result["owner_accuracy"] == 1.0
    assert len(result["misses"]) == 1
    assert result["misses"][0]["id"] == "task-0002"


def test_tasks_eval_gate_pass_and_fail_exit_codes(tmp_path, db):
    _seed_owner_graph(db)
    passing_item = {
        "id": "task-0001", "action_item_id": 10, "transcription_id": 100, "task": "зробити щось",
        "owner_name_raw": "Андрій", "due_raw": "2026-06-02", "meeting_date": "2026-06-01",
        "source": None, "truth": {"owner": "Андрій", "due_date": "2026-06-02", "due_precision": "day"},
        "status": "labeled",
    }
    golden_pass = _tasks_golden_jsonl(tmp_path, [passing_item])
    rc_pass = tasks_eval.main(["--golden", golden_pass, "--db", db, "--gate",
                               "--min-due-acc", "0.9", "--min-owner-acc", "0.8"])
    assert rc_pass == 0

    failing_item = dict(passing_item, truth={"owner": "Хтось Інший", "due_date": "2099-01-01",
                                             "due_precision": "day"})
    golden_fail = _tasks_golden_jsonl(tmp_path, [failing_item])
    rc_fail = tasks_eval.main(["--golden", golden_fail, "--db", db, "--gate",
                               "--min-due-acc", "0.9", "--min-owner-acc", "0.8"])
    assert rc_fail == 1


def test_tasks_eval_empty_labeled_exits_2(tmp_path, db):
    _seed_owner_graph(db)
    golden = _tasks_golden_jsonl(tmp_path, [
        {"id": "task-0001", "action_item_id": 10, "transcription_id": 100, "task": "х",
         "owner_name_raw": None, "due_raw": None, "meeting_date": "2026-06-01", "source": None,
         "truth": {"owner": None, "due_date": None, "due_precision": None}, "status": "unlabeled"},
    ])
    rc = tasks_eval.main(["--golden", golden, "--db", db])
    assert rc == 2


def test_tasks_eval_refuses_live_db_without_yes_live(tmp_path, db, monkeypatch):
    golden = _tasks_golden_jsonl(tmp_path, [])
    monkeypatch.setattr(tasks_eval, "is_live_db", lambda path: True)
    rc = tasks_eval.main(["--golden", golden, "--db", db])
    assert rc == 2
