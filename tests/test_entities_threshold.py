"""Test of entity graph de-noising (Phase 15A): the meeting_count threshold.

/api/memory/entities hides one-off entities by default (meeting_count<2 — noise from
per-meeting LLM extraction). The min_meetings=1 toggle shows all; an explicit search (q)
shows the rare ones too.
"""
import sqlite3

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.db.migrations import init_database


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "e.db")
    init_database(db)
    conn = sqlite3.connect(db)

    def add(etype, name, mc):
        conn.execute(
            "INSERT INTO entities (type, canonical_name, normalized_name, "
            "mention_count, meeting_count) VALUES (?, ?, ?, ?, ?)",
            (etype, name, name.lower(), mc, mc),
        )

    add("person", "Андрій", 10)      # significant
    add("person", "Разовий", 1)      # noise
    add("topic", "Бюджет", 3)        # significant
    add("topic", "Випадкова Тема", 1)  # noise
    conn.commit()
    conn.close()

    app = Flask(__name__)
    app.config["DATABASE"] = db
    app.register_blueprint(memory_bp)
    return app.test_client()


def _names(resp):
    return {e["canonical_name"] for e in resp.get_json()["entities"]}


def test_default_hides_singletons(client):
    r = client.get("/api/memory/entities")
    assert r.status_code == 200
    data = r.get_json()
    assert data["min_meetings"] == 2
    assert _names(r) == {"Андрій", "Бюджет"}
    assert data["total"] == 2  # total is counted with the threshold too


def test_min_meetings_1_shows_all(client):
    r = client.get("/api/memory/entities?min_meetings=1")
    assert _names(r) == {"Андрій", "Разовий", "Бюджет", "Випадкова Тема"}


def test_search_reveals_rare(client):
    # an explicit search by name shows the rare entity too (q -> min=1)
    r = client.get("/api/memory/entities?q=разов")
    assert "Разовий" in _names(r)


def test_type_filter_with_threshold(client):
    r = client.get("/api/memory/entities?type=topic")
    assert _names(r) == {"Бюджет"}  # «Випадкова Тема» (1 meeting) is hidden
