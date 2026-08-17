"""Тести питань без відповіді (Волна 5.3).

Перевіряється те, що замір спіймав на живих даних і що коштувало б довіри до
всього списку: query-рядок посилання не є питанням, відповідь у СУСІДНІЙ нитці
того ж чату закриває питання, а групове питання без звернення не видається за
адресоване власнику.
"""
import sqlite3
from datetime import datetime

import pytest

from app.db.migrations import init_database
from app.services import tg_questions as tq


ME = "Я Власник"
CHAT = -5162514111          # legacy-група: tg_link там не існує
PRIVATE = 250264900         # лічка


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "q.db")
    init_database(path)
    # Особу задаємо явно — тест не має ходити ні до слухача, ні до кешу на диску.
    monkeypatch.setenv("TELEGRAM_SELF_NAME", ME)
    monkeypatch.setenv("TELEGRAM_SELF_USERNAME", "vlasnyk")
    return path


def _msg(path, *, sender, text, date, chat_id=CHAT, title="Робоча група",
         msg_id=None, thread_id=1, reply_to=None, link=None):
    conn = sqlite3.connect(path)
    mid = msg_id if msg_id is not None else conn.execute(
        "SELECT COALESCE(MAX(tg_message_id), 1000) + 1 FROM transcriptions").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_chat_title, tg_message_id, tg_date, tg_sender, tg_link, "
        "tg_thread_id, tg_reply_to) VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"[TG] {text[:20]}", text, chat_id, title, mid, date, sender, link,
         thread_id, reply_to))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


NOW = datetime(2026, 6, 10, 12, 0, 0)


def _questions(db, **kw):
    return tq.open_questions(db, today=NOW, **kw)["questions"]


# --- що взагалі є питанням ------------------------------------------------

def test_url_query_string_is_not_a_question():
    """`?igsh=` у посиланні дало 77 хибних кандидатів з 574 на живому архіві."""
    assert tq.is_question("а коли зустріч?")
    assert not tq.is_question("https://www.instagram.com/p/DbtP/?igsh=MTd6MWll")
    assert not tq.is_question("дивись https://opendatabot.ua/c/46078764?from=search")
    # Посилання поруч зі справжнім питанням питання не скасовує.
    assert tq.is_question("глянь https://x.com/a?b=1 що думаєш?")


# --- кому адресовано ------------------------------------------------------

def test_group_question_without_address_is_not_claimed(db):
    """233 з 294 кандидатів — групові без звернення. Мовчимо про них навмисно."""
    _msg(db, sender="Колега", text="а можемо на 15:00?", date="2026-06-01T10:00:00+00:00")
    res = tq.open_questions(db, today=NOW)
    assert res["questions"] == []
    assert res["skipped_group"] == 1          # але кажемо, скільки сховали


def test_mention_of_my_username_is_addressed_to_me(db):
    _msg(db, sender="Колега", text="@vlasnyk підкажи, коли зустріч?",
         date="2026-06-01T10:00:00+00:00")
    got = _questions(db)
    assert [q["addressed"] for q in got] == ["mention"]
    assert got[0]["silence_h"] is None        # більше не писав у чаті


def test_mention_of_someone_else_is_not_mine(db):
    _msg(db, sender="Колега", text="@jbondarenko тобі вдасться долучитись?",
         date="2026-06-01T10:00:00+00:00")
    assert _questions(db) == []


def test_private_chat_question_is_always_mine(db):
    """У чаті 1:1 більше нікого — звернення не потрібне."""
    _msg(db, sender="Співрозмовник", text="зміг створити Ноушн?", chat_id=PRIVATE,
         title="Співрозмовник", thread_id=7, date="2026-06-01T10:00:00+00:00")
    assert [q["addressed"] for q in _questions(db)] == ["private"]


# --- що вважати відповіддю ------------------------------------------------

def test_reply_in_neighbour_thread_closes_the_question(db):
    """Головна знахідка заміру: у 4 випадках з 10 відповідь лягла в СУСІДНЮ нитку.

    Нитки розмічені моделлю, тож присутність рахуємо по чату. Інакше список
    називає забутим те, на що людина відповіла через 12 хвилин.
    """
    _msg(db, sender="Колега", text="@vlasnyk коли зможемо?", thread_id=1,
         date="2026-06-01T10:00:00+00:00")
    _msg(db, sender=ME, text="давай о 15:00", thread_id=2,   # інша нитка того ж чату
         date="2026-06-01T10:12:00+00:00")
    assert _questions(db) == []


def test_presence_after_the_window_still_counts_as_unanswered(db):
    _msg(db, sender="Колега", text="@vlasnyk коли зможемо?",
         date="2026-06-01T10:00:00+00:00")
    _msg(db, sender=ME, text="про інше", date="2026-06-05T10:00:00+00:00")   # +96 год
    got = _questions(db)
    assert len(got) == 1
    assert got[0]["silence_h"] == 96.0
    # А з добовим вікном у 5 діб те саме питання вважається побаченим.
    assert _questions(db, window_h=200) == []


def test_direct_reply_closes_question_even_outside_window(db):
    """`tg_reply_to` — частина записів, але де він є, він сильніший за присутність."""
    qid = 5001
    _msg(db, sender="Колега", text="@vlasnyk коли зможемо?", msg_id=qid,
         date="2026-06-01T10:00:00+00:00")
    _msg(db, sender=ME, text="ось відповідь", reply_to=qid, thread_id=9,
         date="2026-06-09T10:00:00+00:00")      # через 8 діб, поза вікном
    assert _questions(db) == []


def test_my_own_question_is_not_mine_to_answer(db):
    _msg(db, sender=ME, text="@vlasnyk сам себе питаю?", date="2026-06-01T10:00:00+00:00")
    assert _questions(db) == []


def test_window_and_address_pair_are_returned(db):
    """Адреса — пара id: у legacy-групі t.me-посилання не існує (урок 5.2)."""
    _msg(db, sender="Колега", text="@vlasnyk підкажи?", msg_id=515741,
         date="2026-06-01T10:00:00+00:00")
    q = _questions(db)[0]
    assert (q["chat_id"], q["msg_id"]) == (CHAT, 515741)
    assert q["link"] is None
    assert q["asked_by"] == "Колега"


def test_days_window_cuts_old_questions(db):
    _msg(db, sender="Колега", text="@vlasnyk давнє питання?",
         date="2026-01-01T10:00:00+00:00")
    assert _questions(db, days=30) == []
    assert len(_questions(db, days=365)) == 1


# --- хто такий «я» --------------------------------------------------------

def test_self_from_env_wins(db):
    me = tq.resolve_self(db)
    assert (me["name"], me["username"], me["source"]) == (ME, "vlasnyk", "env")


def test_self_derived_from_private_chat(db, monkeypatch, tmp_path):
    """Запасний шлях: у лічці власник — той, хто не є назвою чату."""
    monkeypatch.delenv("TELEGRAM_SELF_NAME", raising=False)
    monkeypatch.delenv("TELEGRAM_SELF_USERNAME", raising=False)
    monkeypatch.setattr(tq, "_SELF_CACHE", tmp_path / "nope.json")
    monkeypatch.setattr(tq, "_listener_status", lambda *a, **k: None)
    _msg(db, sender="Співрозмовник", text="привіт", chat_id=PRIVATE,
         title="Співрозмовник", date="2026-06-01T10:00:00+00:00")
    _msg(db, sender=ME, text="привіт-привіт", chat_id=PRIVATE,
         title="Співрозмовник", date="2026-06-01T10:05:00+00:00")
    me = tq.resolve_self(db)
    assert (me["name"], me["source"]) == (ME, "private_chats")


def test_unknown_owner_returns_reason_not_empty_list(db, monkeypatch, tmp_path):
    """Порожній список без пояснення читався б як «нічого не висить»."""
    monkeypatch.delenv("TELEGRAM_SELF_NAME", raising=False)
    monkeypatch.setattr(tq, "_SELF_CACHE", tmp_path / "nope.json")
    monkeypatch.setattr(tq, "_listener_status", lambda *a, **k: None)
    res = tq.open_questions(db, today=NOW)
    assert res["questions"] == [] and res["me"] is None
    assert "власник" in res["reason"]


def test_stale_cache_without_username_is_refreshed(db, monkeypatch, tmp_path):
    """Кеш від старого слухача (без ніка) не має назавжди вимикати детект по @."""
    cache = tmp_path / "tg_self.local.json"
    cache.write_text('{"id": 1, "name": "Хтось", "username": null}', encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_SELF_NAME", raising=False)
    monkeypatch.setattr(tq, "_SELF_CACHE", cache)
    monkeypatch.setattr(tq, "_listener_status",
                        lambda *a, **k: {"id": 2, "name": ME, "username": "vlasnyk"})
    me = tq.resolve_self(db)
    assert (me["username"], me["source"]) == ("vlasnyk", "listener")
