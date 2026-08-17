"""Фільтр власника на дашборді задач (/api/memory/action-items?owner=…).

Дашборд шукає ТОЧНИМ збігом, бо значення приходить із чипа фасета. Але одне й
те саме імʼя живе в трьох місцях — канонічне в графі, сире в задачі та аліаси —
і після злиття сутностей (`entity_dedup merge`) саме в аліаси переїжджають усі
інші написання людини. Власник архіву жив трьома сутностями; після їх злиття в
одну посилання `/tasks?owner=Мельник` давало порожньо при живих даних.
"""
import sqlite3

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.db.migrations import init_database


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "tasks.db")
    init_database(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, created_at) "
        "VALUES (1, 'recording', 'Зустріч', '2026-05-14', '2026-05-14 10:00:00')")
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, normalized_name) "
        "VALUES (1, 'person', 'Андрій', 'андрій')")
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, normalized_name) "
        "VALUES (2, 'person', 'Юлія', 'юлія')")
    conn.executemany(
        "INSERT INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (?, ?, ?)",
        [(1, "Мельник", "мельник"),
         (1, "@johndoe", "@johndoe"),
         (2, "Julia", "julia")])
    conn.executemany(
        "INSERT INTO action_items (transcription_id, task, owner_name, owner_entity_id, status) "
        "VALUES (1, ?, ?, ?, 'open')",
        [("Задача власника", "Ви", 1),
         ("Друга задача власника", "Andrii Melnyk", 1),
         ("Задача Юлії", "Julia Bondarenko", 2),
         ("Задача без сутності", "Павло", None)])
    conn.commit()
    conn.close()

    app = Flask(__name__)
    app.config["DATABASE"] = db
    app.register_blueprint(memory_bp)
    return app.test_client()


def _tasks(client, owner):
    r = client.get("/api/memory/action-items?owner=%s" % owner)
    assert r.status_code == 200
    return {a["task"] for a in r.get_json()["action_items"]}


OWNER_TASKS = {"Задача власника", "Друга задача власника"}


def test_canonical_name_finds_owner_tasks(client):
    """Чип фасета — канонічне імʼя сутності."""
    assert _tasks(client, "Андрій") == OWNER_TASKS


def test_alias_finds_the_same_tasks(client):
    """Після злиття сутностей решта написань живе аліасами — і має працювати."""
    assert _tasks(client, "Мельник") == OWNER_TASKS
    assert _tasks(client, "@johndoe") == OWNER_TASKS


def test_alias_lookup_ignores_case_in_cyrillic(client):
    """Ключ рахується `enrichment._normalize`, а не SQL LOWER() (той не згортає кирилицю)."""
    assert _tasks(client, "мельник") == OWNER_TASKS
    assert _tasks(client, "  Мельник  ") == OWNER_TASKS


def test_unlinked_task_matches_by_its_own_name(client):
    """Задача без сутності живе під чипом свого сирого імені — і ним же шукається."""
    assert _tasks(client, "Павло") == {"Задача без сутності"}


def test_raw_name_of_linked_task_is_not_a_separate_filter(client):
    """Сире імʼя звʼязаної задачі НЕ фільтр: інакше список був би довшим за чип.

    «Andrii Melnyk» — те, як задачу назвали в тексті, але живе вона під
    чипом «Андрій». Якби сире імʼя теж фільтрувало, під чип потрапляли б задачі
    з чужих чипів (задача, що звучить як «Юлія», але звʼязана з сутністю «Юля»),
    і число на чипі перестало б дорівнювати довжині списку. Для вільного тексту
    є MCP-тулза `list_action_items`.
    """
    assert _tasks(client, "Andrii Melnyk") == set()


def test_filter_stays_exact_and_does_not_leak(client):
    """Точний збіг: чужі задачі не підмішуються, підрядок не спрацьовує."""
    assert _tasks(client, "Юлія") == {"Задача Юлії"}
    assert _tasks(client, "Андр") == set()          # підрядок — не збіг
    assert _tasks(client, "Julia") == {"Задача Юлії"}   # аліас сутності Юлії


def test_owner_facet_counts_unchanged_by_filter(client):
    """Фасет власників рахується без самого себе — чипи показують повні суми."""
    r = client.get("/api/memory/action-items?owner=Мельник")
    owners = {o["owner"]: o["n"] for o in r.get_json()["owners"]}
    assert owners == {"Андрій": 2, "Юлія": 1, "Павло": 1}


@pytest.mark.parametrize("owner", ["Андрій", "Мельник", "Юлія", "Павло"])
def test_chip_number_equals_list_length(client, owner):
    """Контракт дашборда: натиснув чип із числом N — побачив рівно N рядків."""
    data = client.get("/api/memory/action-items?owner=%s" % owner).get_json()
    chip = {o["owner"]: o["n"] for o in data["owners"]}
    canonical = {"Мельник": "Андрій"}.get(owner, owner)
    assert len(data["action_items"]) == chip[canonical]
