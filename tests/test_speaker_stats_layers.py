"""Статистика спікерів описує два шари, а не один (правдивість, 12.08.2026).

`speakers` — це ГОЛОСИ з діаризації: на живому архіві частина записів.
Решта — переписка, де автор відомий точно (`tg_sender`, 35 людей на повідомлень), але в цій таблиці його немає взагалі. Поки відповідь про це
мовчала, «статистика по кожному спікеру» описувала дрібна частка архіву і читалась як увесь.

Одна людина живе в обох шарах під різними написаннями («Слава Верес» у
діаризації, «Veres Viacheslav» у переписці). Зводимо їх ГРАФОМ (canonical-імена
та аліаси), а не схожістю рядків: у сутності «Юлія» серед аліасів справді є
«Julia Bondarenko», і це знання, а не здогад.

Ізольований Flask із самим speakers_bp + tmp БД — той самий патерн, що в
tests/test_speakers_api.py.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from app.blueprints.speakers import speakers_bp
from app.db.migrations import init_database


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "stats.db")
    init_database(path)
    return path


@pytest.fixture
def client(db_path: str):
    app = Flask(__name__)
    app.config["DATABASE"] = db_path
    app.register_blueprint(speakers_bp)
    return app.test_client()


def _tg(db: str, sender: str, chat_id: int, msg_id: int, date: str = "2026-06-01") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_message_id, tg_sender, meeting_date) "
        "VALUES ('telegram', ?, 'текст', ?, ?, ?, ?)",
        (f"[TG] {sender}", chat_id, msg_id, sender, date))
    conn.commit()
    conn.close()


def _entity(db: str, canonical: str, aliases: tuple = (), etype: str = "person") -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO entities (type, canonical_name, normalized_name) VALUES (?, ?, ?)",
        (etype, canonical, canonical.casefold()))
    eid = cur.lastrowid
    for a in aliases:
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (?, ?, ?)", (eid, a, a.casefold()))
    conn.commit()
    conn.close()
    return eid


def test_stats_report_the_correspondence_layer(client, db_path):
    """Автори переписки видні окремим списком зі своїми одиницями."""
    _tg(db_path, "Dmytro Lebid", chat_id=-100, msg_id=1)
    _tg(db_path, "Dmytro Lebid", chat_id=-200, msg_id=2, date="2026-07-01")
    _tg(db_path, "Julia Bondarenko", chat_id=-100, msg_id=3)

    d = client.get("/api/speakers/stats").get_json()

    senders = {s["name"]: s for s in d["tg_senders"]}
    assert senders["Dmytro Lebid"]["messages"] == 2
    assert senders["Dmytro Lebid"]["chats"] == 2
    assert senders["Dmytro Lebid"]["first_seen"] == "2026-06-01"
    assert senders["Dmytro Lebid"]["last_seen"] == "2026-07-01"
    assert d["total_tg_senders"] == 2


def test_coverage_says_how_little_diarization_covers(client, db_path):
    """Знаменник: скільки записів має діаризацію, а скільки їх усього."""
    for i in range(3):
        _tg(db_path, "Dmytro Lebid", chat_id=-100, msg_id=i + 1)

    cov = client.get("/api/speakers/stats").get_json()["coverage"]

    assert cov["diarized_transcripts"] == 0        # жодного дзвінка не заведено
    assert cov["total_transcripts"] == 3
    assert cov["telegram_messages"] == 3
    assert "не додають" in cov["note"]


def test_graph_links_the_same_person_across_layers(client, db_path):
    """«Julia Bondarenko» у переписці і «Юлія» в діаризації — одна сутність графа."""
    eid = _entity(db_path, "Юлія", aliases=("Julia Bondarenko",))
    _tg(db_path, "Julia Bondarenko", chat_id=-100, msg_id=1)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO speakers (name) VALUES ('Юлія')")
    conn.commit()
    conn.close()

    d = client.get("/api/speakers/stats").get_json()

    sender = next(s for s in d["tg_senders"] if s["name"] == "Julia Bondarenko")
    voice = next(s for s in d["speakers"] if s["name"] == "Юлія")
    assert sender["entity_id"] == eid
    assert voice["entity_id"] == eid               # спільний id = це та сама людина
    assert d["coverage"]["tg_senders_linked_to_graph"] == 1


def test_unknown_writing_gets_no_invented_link(client, db_path):
    """Чого граф не знає — лишається прочерком, а не здогадом за схожістю."""
    _entity(db_path, "Слава", aliases=("Слава Верес",))
    _tg(db_path, "Veres Viacheslav", chat_id=-100, msg_id=1)

    d = client.get("/api/speakers/stats").get_json()

    assert d["tg_senders"][0]["entity_id"] is None
    assert d["coverage"]["tg_senders_linked_to_graph"] == 0


def test_ambiguous_writing_is_left_unlinked(client, db_path):
    """Однакове написання у двох сутностей — привʼязка неможлива, і це не помилка.

    Схема забороняє дублікат у межах типу (UNIQUE на `type`+`normalized_name`),
    але НЕ поперек типів — і саме там неоднозначність реальна: на живому архіві
    202 написання ведуть до двох і більше сутностей («acmecorp» → 14 і 538).
    Приписати повідомлення одній із них навмання означало б вигадати звʼязок.
    """
    _entity(db_path, "Acmecorp", etype="project")
    _entity(db_path, "Acmecorp", etype="org")
    _tg(db_path, "Acmecorp", chat_id=-100, msg_id=1)

    d = client.get("/api/speakers/stats").get_json()

    assert d["tg_senders"][0]["entity_id"] is None


def test_deleted_messages_do_not_count(client, db_path):
    """Видалене в Telegram не роздуває лічильник автора."""
    _tg(db_path, "Dmytro Lebid", chat_id=-100, msg_id=1)
    _tg(db_path, "Dmytro Lebid", chat_id=-100, msg_id=2)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE transcriptions SET deleted_at = '2026-07-01' WHERE tg_message_id = 2")
    conn.commit()
    conn.close()

    d = client.get("/api/speakers/stats").get_json()

    assert d["tg_senders"][0]["messages"] == 1
    assert d["coverage"]["total_transcripts"] == 1


def test_existing_shape_is_kept_for_the_ui(client, db_path):
    """Старі ключі лишаються: сторінка «Спікери» читає саме їх."""
    d = client.get("/api/speakers/stats").get_json()

    assert {"speakers", "total_speakers", "total_seconds"} <= set(d)
    assert isinstance(d["speakers"], list)


def test_soft_deleted_recording_leaves_the_numerator_too(client, db_path):
    """Мʼяке видалення прибирає запис з ОБОХ боків дробу, а не лише зі знаменника.

    `transcription_speaker_map` переживає видалення запису (рядки чистить лише
    масовий прохід), тож чисельник тримав видалене, поки знаменник його вже не
    рахував — і покриття діаризації виглядало більшим, ніж воно є.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
                 "VALUES (1, 'recording', 'call.mp3', 'текст')")
    conn.execute("INSERT INTO speakers (name) VALUES ('Юлія')")
    conn.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) "
                 "VALUES (1, 'SPEAKER_00', (SELECT id FROM speakers WHERE name='Юлія'))")
    conn.commit()

    cov = client.get("/api/speakers/stats").get_json()["coverage"]
    assert (cov["diarized_transcripts"], cov["total_transcripts"]) == (1, 1)

    conn.execute("UPDATE transcriptions SET deleted_at = '2026-07-01' WHERE id = 1")
    conn.commit()
    conn.close()

    d = client.get("/api/speakers/stats").get_json()
    cov = d["coverage"]
    assert (cov["diarized_transcripts"], cov["total_transcripts"]) == (0, 0)
    voice = next(s for s in d["speakers"] if s["name"] == "Юлія")
    assert voice["transcripts_count"] == 0          # звʼязка в мапі лишилась, запис — ні


def test_link_only_through_person_entities(client, db_path):
    """Канал, чиє імʼя збігається з назвою проєкту, не стає «тією самою людиною»."""
    _entity(db_path, "Acmecorp", etype="project")
    _tg(db_path, "Acmecorp", chat_id=-100, msg_id=1)

    d = client.get("/api/speakers/stats").get_json()

    assert d["tg_senders"][0]["entity_id"] is None
    assert d["coverage"]["tg_senders_linked_to_graph"] == 0


def test_both_layers_date_by_the_event_not_by_ingest(client, db_path):
    """`first_seen`/`last_seen` в обох списках означають дату ПОДІЇ.

    У голосів вони бралися з `created_at` (коли запис потрапив в архів), у
    переписки — з дати повідомлення. Однаково названі сусідні поля з різним
    сенсом штовхають до порівняння, від якого сам же опис і застерігає.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, transcript_text, "
                 "meeting_date, created_at) VALUES (1, 'recording', 'call.mp3', 'текст', "
                 "'2026-03-05', '2026-08-01 10:00:00')")
    conn.execute("INSERT INTO speakers (name) VALUES ('Юлія')")
    conn.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) "
                 "VALUES (1, 'SPEAKER_00', (SELECT id FROM speakers WHERE name='Юлія'))")
    conn.commit()
    conn.close()

    voice = next(s for s in client.get("/api/speakers/stats").get_json()["speakers"]
                 if s["name"] == "Юлія")

    assert voice["first_seen"] == "2026-03-05"      # дата розмови, не дата заливки
    assert voice["last_seen"] == "2026-03-05"
