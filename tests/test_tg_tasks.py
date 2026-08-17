"""Тести зобовʼязань з переписки (Волна 5.1).

Обидві моделі мокаються — офлайн, без Ollama і без Claude. Перевіряється те,
що на живих даних коштує дорого: задача чіпляється до КОНКРЕТНОГО повідомлення
(інакше 5.2 «куди написати» не існує), повторний прогін не плодить дублів і не
чіпає чужі рядки, водяний знак стримує локальні виклики, а збій будь-якої з
моделей не валить прохід і не псує дані.
"""
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import tg_tasks


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _seed(path, *, chat_id=-100, title="Робочий чат", thread_id=1,
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


def _fake_triage(monkeypatch, has_commitment, evidence="бо так"):
    from app.services import local_llm
    monkeypatch.setattr(local_llm, "generate_json", lambda *a, **k: {
        "data": {"has_commitment": has_commitment, "evidence": evidence}})
    monkeypatch.setattr(local_llm, "availability", lambda *a, **k: (True, "OK"))


def _fake_claude(monkeypatch, tasks):
    from app.services import text_polishing
    monkeypatch.setattr(text_polishing, "is_available", lambda: True)
    monkeypatch.setattr(text_polishing, "extract_chat_tasks", lambda *a, **k: {
        "tasks": tasks, "model": "claude-test", "input_tokens": 10,
        "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0})


def _items(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM action_items ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# Триаж
# ============================================================

def test_triage_writes_verdict_and_watermark(db, monkeypatch):
    _seed(db, messages=[("Адам", "надішлю договір завтра")])
    _fake_triage(monkeypatch, True)
    res = tg_tasks.triage_thread(db, 1)
    assert res["verdict"] == tg_tasks.VERDICT_YES

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT triage, triage_msgs, triage_model FROM tg_threads "
                       "WHERE id = 1").fetchone()
    conn.close()
    assert row[0] == tg_tasks.VERDICT_YES
    assert row[1] == 1, "водяний знак = скільки повідомлень бачили"
    assert row[2]


def test_triaged_thread_is_not_a_candidate_again(db, monkeypatch):
    """Нитка, що не змінилась, не витрачає модель повторно."""
    _seed(db, messages=[("Адам", "ок")])
    _fake_triage(monkeypatch, False)
    tg_tasks.triage_thread(db, 1)
    assert tg_tasks.triage_candidates(db) == []


def test_grown_thread_is_retriaged(db, monkeypatch):
    """Домовленість могла прозвучати вже ПІСЛЯ вердикту."""
    _seed(db, messages=[("Адам", "привіт")])
    _fake_triage(monkeypatch, False)
    tg_tasks.triage_thread(db, 1)
    _seed(db, messages=[("Юля", "зроблю до пʼятниці")])
    assert tg_tasks.triage_candidates(db) == [1]


def test_triage_failure_leaves_thread_a_candidate(db, monkeypatch):
    """Збій моделі — не вердикт «зобовʼязань немає»."""
    from app.services import local_llm

    _seed(db, messages=[("Адам", "надішлю")])

    def _boom(*a, **k):
        raise local_llm.LocalLLMError("нема Ollama")

    monkeypatch.setattr(local_llm, "generate_json", _boom)
    res = tg_tasks.triage_thread(db, 1)
    assert res["status"] == "failed"
    assert tg_tasks.triage_candidates(db) == [1]


def test_triage_all_degrades_without_ollama(db, monkeypatch):
    from app.services import local_llm

    _seed(db, messages=[("Адам", "надішлю")])
    monkeypatch.setattr(local_llm, "availability", lambda *a, **k: (False, "нема Ollama"))
    res = tg_tasks.triage_all(db, dry_run=False)
    assert res["skipped"] is True and res["candidates"] == 1


def test_triage_text_previews_documents(db):
    """Документ на 88 тис. символів не має витіснити розмову з промпта."""
    _seed(db, messages=[("Адам", "д" * 5000), ("Юля", "ок")])
    from app.db.connection import get_db_connection
    with get_db_connection(db) as conn:
        text = tg_tasks._triage_text(tg_tasks.thread_messages(conn, 1))
    assert "Юля: ок" in text, "останнє повідомлення видно"
    assert len(text) < 5000


# ============================================================
# Витяг
# ============================================================

def test_task_is_attached_to_the_message_that_voiced_it(db, monkeypatch):
    """Без цього немає 5.2: у задачі має бути видно, куди саме написати."""
    ids = _seed(db, messages=[("Адам", "хто зробить кошторис?"),
                              ("Юля", "я зроблю до пʼятниці"),
                              ("Адам", "дякую")])
    _fake_triage(monkeypatch, True)
    tg_tasks.triage_thread(db, 1)
    _fake_claude(monkeypatch, [{"task": "Зробити кошторис", "owner": "Юля",
                                "due": "до пʼятниці", "due_date": None, "msg": 2}])
    tg_tasks.extract_thread(db, 1)

    items = _items(db)
    assert len(items) == 1
    assert items[0]["transcription_id"] == ids[1], "повідомлення Юлі, а не перше в нитці"
    assert items[0]["owner_name"] == "Юля"
    assert items[0]["source"] == tg_tasks.SOURCE


def test_due_is_expanded_from_the_message_date(db, monkeypatch):
    """Строк рахується від дати РЕПЛІКИ, а не від сьогодні (Трек 1)."""
    _seed(db, messages=[("Юля", "зроблю завтра")])
    _fake_claude(monkeypatch, [{"task": "Зробити", "owner": "Юля", "due": "завтра",
                                "due_date": None, "msg": 1}])
    tg_tasks.extract_thread(db, 1)
    item = _items(db)[0]
    assert item["due"] == "завтра"
    assert item["due_date"] == "2026-06-02", "наступний день після дати повідомлення"


def test_model_iso_date_wins_over_parser(db, monkeypatch):
    _seed(db, messages=[("Юля", "зроблю на тому тижні")])
    _fake_claude(monkeypatch, [{"task": "Зробити", "owner": None,
                                "due": "на тому тижні", "due_date": "2026-06-08",
                                "msg": 1}])
    tg_tasks.extract_thread(db, 1)
    assert _items(db)[0]["due_date"] == "2026-06-08"


def test_unknown_msg_number_falls_back_to_last_message(db, monkeypatch):
    ids = _seed(db, messages=[("Адам", "перше"), ("Юля", "друге")])
    _fake_claude(monkeypatch, [{"task": "Щось зробити", "owner": None, "due": None,
                                "msg": 99}])
    tg_tasks.extract_thread(db, 1)
    assert _items(db)[0]["transcription_id"] == ids[-1]


def test_placeholder_owner_becomes_null(db, monkeypatch):
    """Рядок «null» як імʼя виконавця — це не виконавець (урок Волни 4.5.2)."""
    _seed(db, messages=[("Юля", "зроблю")])
    _fake_claude(monkeypatch, [{"task": "Зробити", "owner": "null", "due": None, "msg": 1}])
    tg_tasks.extract_thread(db, 1)
    assert _items(db)[0]["owner_name"] is None


def test_empty_task_text_is_dropped(db, monkeypatch):
    _seed(db, messages=[("Юля", "ок")])
    _fake_claude(monkeypatch, [{"task": "   ", "owner": "Юля", "msg": 1},
                               {"task": "Реальна задача", "owner": None, "msg": 1}])
    tg_tasks.extract_thread(db, 1)
    assert [i["task"] for i in _items(db)] == ["Реальна задача"]


def test_rerun_replaces_own_rows_and_spares_foreign(db, monkeypatch):
    """Повторний прогін не плодить дублів і не стирає задачі з картки дзвінка."""
    ids = _seed(db, messages=[("Юля", "зроблю")])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO action_items (transcription_id, task, status) "
                 "VALUES (?, 'Чужа задача з картки', 'open')", (ids[0],))
    conn.commit()
    conn.close()

    _fake_claude(monkeypatch, [{"task": "Зробити", "owner": "Юля", "msg": 1}])
    tg_tasks.extract_thread(db, 1)
    tg_tasks.extract_thread(db, 1, model=None)

    tasks = sorted(i["task"] for i in _items(db))
    assert tasks == ["Зробити", "Чужа задача з картки"]


def test_extract_failure_leaves_thread_a_candidate(db, monkeypatch):
    from app.services import text_polishing

    _seed(db, messages=[("Юля", "зроблю")])
    _fake_triage(monkeypatch, True)
    tg_tasks.triage_thread(db, 1)

    def _boom(*a, **k):
        raise RuntimeError("Claude лежить")

    monkeypatch.setattr(text_polishing, "is_available", lambda: True)
    monkeypatch.setattr(text_polishing, "extract_chat_tasks", _boom)
    res = tg_tasks.extract_thread(db, 1)
    assert res["status"] == "retry_needed"
    assert tg_tasks.extract_candidates(db) == [1], "нитка чекає наступного проходу"
    assert _items(db) == []


def test_only_triaged_threads_are_extracted(db, monkeypatch):
    """Гард Волни 0 знімається вибірково, а не цілком."""
    _seed(db, messages=[("Адам", "новина дня")], thread_id=1, label="Новини")
    _seed(db, messages=[("Юля", "зроблю")], thread_id=2, label="Кошторис")
    _fake_triage(monkeypatch, False)
    tg_tasks.triage_thread(db, 1)
    _fake_triage(monkeypatch, True)
    tg_tasks.triage_thread(db, 2)
    assert tg_tasks.extract_candidates(db) == [2]


def test_grown_thread_is_extracted_again(db, monkeypatch):
    """Нові повідомлення — це нові домовленості (або скасування старих)."""
    _seed(db, messages=[("Юля", "зроблю")])
    _fake_triage(monkeypatch, True)
    tg_tasks.triage_thread(db, 1)
    _fake_claude(monkeypatch, [{"task": "Зробити", "owner": "Юля", "msg": 1}])
    tg_tasks.extract_thread(db, 1)
    assert tg_tasks.extract_candidates(db) == []

    _seed(db, messages=[("Юля", "уже не треба, скасовуємо")])
    tg_tasks.triage_thread(db, 1)
    assert tg_tasks.extract_candidates(db) == [1]


def test_unchanged_thread_is_not_extracted_twice(db, monkeypatch):
    _seed(db, messages=[("Юля", "зроблю")])
    _fake_triage(monkeypatch, True)
    tg_tasks.triage_thread(db, 1)
    _fake_claude(monkeypatch, [{"task": "Зробити", "owner": "Юля", "msg": 1}])
    tg_tasks.extract_thread(db, 1)
    tg_tasks.triage_thread(db, 1)          # повторний триаж без нових повідомлень
    assert tg_tasks.extract_candidates(db) == []


def test_extract_all_degrades_without_api_key(db, monkeypatch):
    from app.services import text_polishing

    _seed(db, messages=[("Юля", "зроблю")])
    _fake_triage(monkeypatch, True)
    tg_tasks.triage_thread(db, 1)
    monkeypatch.setattr(text_polishing, "is_available", lambda: False)
    res = tg_tasks.extract_all(db, dry_run=False)
    assert res["skipped"] is True and res["candidates"] == 1


def test_stats_counts_pipeline(db, monkeypatch):
    _seed(db, messages=[("Юля", "зроблю до пʼятниці")])
    _fake_triage(monkeypatch, True)
    tg_tasks.triage_thread(db, 1)
    _fake_claude(monkeypatch, [{"task": "Зробити", "owner": "Юля",
                                "due": "до пʼятниці", "msg": 1}])
    tg_tasks.extract_thread(db, 1)

    st = tg_tasks.stats(db)
    assert st["threads"] == 1 and st["triaged"] == 1 and st["commitment"] == 1
    assert st["extracted"] == 1 and st["stale_triage"] == 0
    assert st["tasks"]["total"] == 1 and st["tasks"]["with_owner"] == 1
