"""Owner filter on the task dashboard (/api/memory/action-items?owner=...).

The dashboard searches by EXACT match, because the value comes from a facet chip. But one and the
same name lives in three places - canonical in the graph, raw in the task and aliases -
and after an entity merge (`entity_dedup merge`) all other spellings of a person move
into the aliases. The archive owner lived as three entities; after merging them into
one, the link `/tasks?owner=Мельник` gave empty with live data.
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
    """A facet chip is the entity's canonical name."""
    assert _tasks(client, "Андрій") == OWNER_TASKS


def test_alias_finds_the_same_tasks(client):
    """After an entity merge the remaining spellings live as aliases - and must work."""
    assert _tasks(client, "Мельник") == OWNER_TASKS
    assert _tasks(client, "@johndoe") == OWNER_TASKS


def test_alias_lookup_ignores_case_in_cyrillic(client):
    """The key is computed by `enrichment._normalize`, not by SQL LOWER() (which does not fold Cyrillic)."""
    assert _tasks(client, "мельник") == OWNER_TASKS
    assert _tasks(client, "  Мельник  ") == OWNER_TASKS


def test_unlinked_task_matches_by_its_own_name(client):
    """A task without an entity lives under the chip of its raw name - and is searched by it."""
    assert _tasks(client, "Павло") == {"Задача без сутності"}


def test_raw_name_of_linked_task_is_not_a_separate_filter(client):
    """The raw name of a linked task is NOT a filter: otherwise the list would be longer than the chip.

    «Andrii Melnyk» is how the task was named in the text, but it lives under
    the chip «Андрій». If the raw name filtered too, tasks of foreign chips
    would land under the chip (a task that sounds like «Юлія» but is linked to the entity «Юля»),
    and the number on the chip would stop equalling the list length. For free text
    there is the MCP tool `list_action_items`.
    """
    assert _tasks(client, "Andrii Melnyk") == set()


def test_filter_stays_exact_and_does_not_leak(client):
    """Exact match: foreign tasks are not mixed in, a substring does not fire."""
    assert _tasks(client, "Юлія") == {"Задача Юлії"}
    assert _tasks(client, "Андр") == set()          # a substring is not a match
    assert _tasks(client, "Julia") == {"Задача Юлії"}   # an alias of the entity «Юлія»


def test_owner_facet_counts_unchanged_by_filter(client):
    """The owner facet is counted without itself - chips show full totals."""
    r = client.get("/api/memory/action-items?owner=Мельник")
    owners = {o["owner"]: o["n"] for o in r.get_json()["owners"]}
    assert owners == {"Андрій": 2, "Юлія": 1, "Павло": 1}


@pytest.mark.parametrize("owner", ["Андрій", "Мельник", "Юлія", "Павло"])
def test_chip_number_equals_list_length(client, owner):
    """Dashboard contract: clicked a chip with number N - saw exactly N rows."""
    data = client.get("/api/memory/action-items?owner=%s" % owner).get_json()
    chip = {o["owner"]: o["n"] for o in data["owners"]}
    canonical = {"Мельник": "Андрій"}.get(owner, owner)
    assert len(data["action_items"]) == chip[canonical]
