"""Тести звʼязування Telegram із графом сутностей (Волна 4.5.3).

Без моделі і без мережі. Головне, що перевіряється, — правило «згадка має
виглядати власною назвою в самому тексті»: саме воно відділяє «Адам» від
«документ» і «Том» від «тому». Без нього граф стає гіршим, ніж був, бо
збагачення записало в аліаси звичайні слова.
"""
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import tg_entities


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
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


def _links(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute("SELECT * FROM meeting_entities")]
    conn.close()
    return out


# ============================================================
# Правило власної назви
# ============================================================

def test_capitalised_mention_is_linked(db):
    eid = _entity(db, "Адам")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("сьогодні Адам підтвердив платіж", names) == {eid}


def test_lowercase_mention_is_ignored(db):
    """«документ» як аліас проєкту ловив кожен рахунок в архіві."""
    _entity(db, "Стратегія розвитку області", etype="project", aliases=["документ"])
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("надішліть документ на пошту", names) == set()


def test_common_word_colliding_with_name_is_ignored(db):
    """«Тому» як аліас особи давав 144 збіги на слові «тому» (= через це)."""
    _entity(db, "Том", aliases=["Тому", "Тома"])
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("я тому і питаю про це", names) == set()


def test_sentence_start_does_not_prove_a_name(db):
    """Велика літера після крапки нічого не доводить."""
    _entity(db, "Стратегія розвитку області", etype="project", aliases=["документ"])
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("Все готово. Документ надіслано.", names) == set()
    assert tg_entities.find_mentions("Документ надіслано", names) == set()


def test_real_name_after_sentence_start_still_found(db):
    """Але справжнє імʼя не має губитись через те, що стоїть у середині."""
    eid = _entity(db, "Ковальчука", etype="project")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("Все готово. По Ковальчука домовились.", names) == {eid}


def test_multiword_name_is_matched(db):
    eid = _entity(db, "Київський авіаційний інститут", etype="org")
    names = tg_entities.load_names(db)
    got = tg_entities.find_mentions("зустріч у Київський авіаційний інститут завтра", names)
    assert got == {eid}


def test_short_names_are_skipped(db):
    """«Ок», «БХ» ловлять сміття і користі не дають."""
    _entity(db, "БХ", etype="org")
    assert "бх" not in tg_entities.load_names(db)


def test_topic_entities_are_not_linkable(db):
    """2153 сутності типу topic — вільний текст («планування», «бюджет»):
    вони зловлять половину архіву і нічого не означатимуть."""
    _entity(db, "Планування", etype="topic")
    assert tg_entities.load_names(db) == {}


def test_word_boundary_is_respected(db):
    _entity(db, "Make", etype="org")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("це Makefile проєкту", names) == set()


# ============================================================
# Звʼязок вішається на НИТКУ
# ============================================================

def test_link_marks_only_the_message_with_the_mention(db):
    """Спокуса була повісити сутність на всю нитку, але розширення до нитки вже
    робить scope._expand_to_threads. Дубль у графі дав би 43 482 рядки замість
    ~2 тисяч і зробив би meeting_count та list_stale_topics безглуздими."""
    eid = _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука треба інвестора", thread_id=1)
    _msg(db, "а скільки там виходить?", thread_id=1)
    _msg(db, "ок, беремо", thread_id=1)
    res = tg_entities.link_threads(db, dry_run=False)
    assert res["threads_with_mentions"] == 1
    assert res["messages_linked"] == 1, "лише повідомлення зі згадкою"
    assert {l["entity_id"] for l in _links(db)} == {eid}
    assert len(_links(db)) == 1


def test_other_threads_are_untouched(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука треба інвестора", thread_id=1)
    _msg(db, "зовсім інша розмова", thread_id=2)
    tg_entities.link_threads(db, dry_run=False)
    linked = {l["transcription_id"] for l in _links(db)}
    assert len(linked) == 1


def test_dry_run_writes_nothing(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    res = tg_entities.link_threads(db, dry_run=True)
    assert res["dry_run"] is True and res["messages_linked"] == 1
    assert _links(db) == []


def test_link_is_idempotent(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    tg_entities.link_threads(db, dry_run=False)
    tg_entities.link_threads(db, dry_run=False)
    assert len(_links(db)) == 1


def test_link_records_its_provenance(db):
    """Збіг рядків і витяг моделі — різні за надійністю речі."""
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    tg_entities.link_threads(db, dry_run=False)
    assert _links(db)[0]["source"] == tg_entities.SOURCE


def test_existing_claude_link_is_not_overwritten(db):
    """Звʼязок від збагачення надійніший — у нього є salience і роль."""
    eid = _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились", thread_id=1)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count, "
                 "salience, source) VALUES (?, ?, 7, 0.9, NULL)", (tid, eid))
    conn.commit()
    conn.close()

    tg_entities.link_threads(db, dry_run=False)
    row = [l for l in _links(db) if l["transcription_id"] == tid][0]
    assert row["source"] is None
    assert row["salience"] == 0.9
    assert row["mention_count"] == 7


def test_relink_drops_only_own_links(db):
    """Перезапис прибирає свої звʼязки, чужі лишає."""
    other = _entity(db, "Барселона", etype="org")
    tid = _msg(db, "тут нічого не згадано", thread_id=1)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, source) "
                 "VALUES (?, ?, NULL)", (tid, other))
    conn.commit()
    conn.close()
    tg_entities.link_threads(db, dry_run=False)
    assert len(_links(db)) == 1 and _links(db)[0]["source"] is None


def test_messages_without_thread_are_skipped(db):
    """Нитки ще не розкладені — звʼязувати нема по чому."""
    _entity(db, "Ковальчука", etype="project")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                 "tg_chat_id, tg_message_id, tg_date) VALUES ('telegram', 'x', "
                 "'по Ковальчука домовились', -100, 1, '2026-06-01T10:00:00+00:00')")
    conn.commit()
    conn.close()
    res = tg_entities.link_threads(db, dry_run=False)
    assert res["messages_total"] == 0
    assert _links(db) == []


def test_stats_report_coverage(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    tg_entities.link_threads(db, dry_run=False)
    got = tg_entities.stats(db)
    assert got["telegram_linked"] == 1
    assert got["links_by_source"].get(tg_entities.SOURCE) == 1
    assert got["top_entities"][0]["name"] == "Ковальчука"


# ============================================================
# Звʼязування на інжесті (інкрементальний шлях)
# ============================================================

@pytest.fixture(autouse=True)
def _clean_names_cache():
    """Кеш назв живе на рівні модуля, а кожен тест має свою тимчасову БД."""
    tg_entities.reset_names_cache()
    yield
    tg_entities.reset_names_cache()


def test_link_message_links_single_record(db):
    """Нове повідомлення потрапляє в граф без повного проходу по архіву."""
    eid = _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились")
    res = tg_entities.link_message(db, tid)
    assert res["status"] == "ok" and res["written"] == 1
    assert [(r["transcription_id"], r["entity_id"], r["source"]) for r in _links(db)] == \
        [(tid, eid, tg_entities.SOURCE)]


def test_link_message_is_idempotent(db):
    """Повтор (напр. переембединг) не множить звʼязки."""
    _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились")
    tg_entities.link_message(db, tid)
    tg_entities.link_message(db, tid)
    assert len(_links(db)) == 1


def test_link_message_forgets_mentions_removed_by_edit(db):
    """Правка тексту знімає згадку: підписка MessageEdited веде сюди ж."""
    _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились")
    tg_entities.link_message(db, tid)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET transcript_text = ? WHERE id = ?",
                 ("домовились по іншому обʼєкту", tid))
    conn.commit()
    conn.close()
    assert tg_entities.link_message(db, tid)["written"] == 0
    assert _links(db) == []


def test_link_message_keeps_foreign_links(db):
    """Звʼязок від Claude-збагачення надійніший — його не чіпаємо."""
    other = _entity(db, "Барселона", etype="org")
    tid = _msg(db, "тут нічого не згадано")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, source) "
                 "VALUES (?, ?, NULL)", (tid, other))
    conn.commit()
    conn.close()
    tg_entities.link_message(db, tid)
    assert len(_links(db)) == 1 and _links(db)[0]["source"] is None


def test_link_message_works_without_thread(db):
    """Згадку доводить текст, а не нитка: збій розкладання не лишає запис поза графом."""
    eid = _entity(db, "Ковальчука", etype="project")
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_message_id, tg_date) VALUES ('telegram', 'x', "
        "'по Ковальчука домовились', -100, 7, '2026-06-01T10:00:00+00:00')")
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    assert tg_entities.link_message(db, tid)["written"] == 1
    assert _links(db)[0]["entity_id"] == eid


def test_link_message_skips_non_telegram(db):
    """Дзвінки й документи має збагачувати Claude, а не пошук написань."""
    _entity(db, "Ковальчука", etype="project")
    conn = sqlite3.connect(db)
    cur = conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text) "
                       "VALUES ('recording', 'дзвінок', 'по Ковальчука домовились')")
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    assert tg_entities.link_message(db, tid)["status"] == "not_found"
    assert _links(db) == []


def test_names_cache_is_reused_and_resettable(db):
    """Кеш потрібен, бо інакше кожне повідомлення читає 6200 назв."""
    _entity(db, "Ковальчука", etype="project")
    first = tg_entities.names_for(db)
    _entity(db, "Барселона", etype="org")
    assert tg_entities.names_for(db) is first          # нова сутність ще не видна
    tg_entities.reset_names_cache()
    assert "барселона" in tg_entities.names_for(db)
