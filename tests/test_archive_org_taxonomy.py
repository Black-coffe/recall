"""Agent (non-UI) taxonomy writes over the memory blueprint: name caps, `created_source`,
private-direction refusals, bulk ceiling, `source='mcp'` tag links. UI paths unchanged."""
from __future__ import annotations

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.core import auth
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories as cats
from app.services import privacy

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


@pytest.fixture(autouse=True)
def no_real_reembed(monkeypatch):
    from app.services import reembed
    monkeypatch.setattr(reembed, "schedule_records_reembed",
                        lambda ids, *, db_path, reason: {"mode": "point", "count": 0, "stale": 0})
    monkeypatch.setenv("RECALL_PRIVATE_CATEGORIES", "Особисте")


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "taxonomy.db")
    init_database(path)
    return path


def _node(conn, name):
    row = conn.execute("SELECT id FROM categories WHERE name_norm = ?",
                       (cats.norm_name(name),)).fetchone()
    return cats.get_category(conn, row[0]) if row else cats.create_category(conn, name)


@pytest.fixture
def world(db):
    """«Особисте» (private) and «Робота» (public), a tag «бюджет», one record in each direction."""
    with get_db_connection(db) as conn:
        priv = _node(conn, "Особисте")     # among the v15 seeds, so taken as it is
        work = _node(conn, "Робота")
        cats.ensure_tag(conn, "бюджет")
        ids = {}
        for key, cid in (("priv", priv["id"]), ("work", work["id"]), ("none", None)):
            ids[key] = conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                "category_id) VALUES ('file', 'Дзвінок', 'кошторис', ?)", (cid,)).lastrowid
        conn.commit()
    return {"priv": priv["id"], "work": work["id"], "tx": ids}


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.register_blueprint(memory_bp)
    app.config.update(TESTING=True, DATABASE=db)
    return app.test_client()


def _cat(db, cid):
    with get_db_connection(db) as conn:
        return dict(conn.execute("SELECT name, created_source FROM categories WHERE id = ?",
                                 (cid,)).fetchone())


def _tx_cat(db, tid):
    with get_db_connection(db) as conn:
        return conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                            (tid,)).fetchone()[0]


def _links(db, tid):
    with get_db_connection(db) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT tg.name, rt.source FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
            "WHERE rt.transcription_id = ? ORDER BY tg.name_norm", (tid,))]


# ---------------------------------------------------------------- create / rename

def test_agent_create_marks_created_source_mcp(client, db):
    r = client.post("/api/memory/categories", json={"name": "Інвестори"})
    assert r.status_code == 200 and _cat(db, r.get_json()["id"])["created_source"] == "mcp"


def test_ui_create_keeps_created_source_null(client, db):
    r = client.post("/api/memory/categories", json={"name": "Інвестори"}, headers=UI)
    assert r.status_code == 200 and _cat(db, r.get_json()["id"])["created_source"] is None


def test_agent_category_name_cap_60_after_clean_name(client, db, world):
    long = "Д" * 61
    r = client.post("/api/memory/categories", json={"name": long})
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ARGUMENT"
    r = client.patch(f"/api/memory/categories/{world['work']}", json={"name": long})
    assert r.status_code == 400 and _cat(db, world["work"])["name"] == "Робота"
    # spaces collapse first: 60 letters padded with blanks is within the cap
    ok = client.post("/api/memory/categories", json={"name": "  " + "Д" * 60 + "   "})
    assert ok.status_code == 200
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM categories WHERE name = ?",
                            (long,)).fetchone()[0] == 0


def test_ui_category_name_beyond_60_is_still_accepted(client, db):
    r = client.post("/api/memory/categories", json={"name": "Д" * 61}, headers=UI)
    assert r.status_code == 200


def test_agent_patch_of_private_category_is_refused_whole(client, db, world):
    r = client.patch(f"/api/memory/categories/{world['priv']}",
                     json={"name": "Особисте 2", "color": "#111"})
    assert r.status_code == 403 and r.get_json()["error_code"] == "PRIVATE_CATEGORY_CHANGE"
    assert _cat(db, world["priv"])["name"] == "Особисте"
    ok = client.patch(f"/api/memory/categories/{world['work']}", json={"name": "Робота 2"})
    assert ok.status_code == 200 and _cat(db, world["work"])["name"] == "Робота 2"


def test_ui_patch_of_private_category_still_works(client, db, world):
    r = client.patch(f"/api/memory/categories/{world['priv']}", json={"color": "#111"},
                     headers=UI)
    assert r.status_code == 200


def test_agent_privacy_check_error_is_503(client, db, world, monkeypatch):
    def _boom(*a, **k):
        raise privacy.PrivacyError("db locked")
    monkeypatch.setattr(privacy, "category_change_exposes", _boom)
    r = client.patch(f"/api/memory/categories/{world['work']}", json={"name": "Робота 2"})
    assert r.status_code == 503 and r.get_json()["error_code"] == "PRIVACY_CHECK_FAILED"
    assert _cat(db, world["work"])["name"] == "Робота"


# ---------------------------------------------------------------- record moves

def test_agent_move_out_of_private_direction_is_refused(client, db, world):
    tid = world["tx"]["priv"]
    for target in (world["work"], None):
        r = client.patch(f"/api/memory/transcriptions/{tid}/category",
                         json={"category_id": target})
        assert r.status_code == 403 and r.get_json()["error_code"] == "PRIVATE_CATEGORY_CHANGE"
    assert _tx_cat(db, tid) == world["priv"]


def test_agent_move_into_private_direction_is_allowed(client, db, world):
    tid = world["tx"]["work"]
    r = client.patch(f"/api/memory/transcriptions/{tid}/category",
                     json={"category_id": world["priv"]})
    assert r.status_code == 200 and _tx_cat(db, tid) == world["priv"]


def test_ui_move_out_of_private_direction_still_works(client, db, world):
    tid = world["tx"]["priv"]
    r = client.patch(f"/api/memory/transcriptions/{tid}/category",
                     json={"category_id": world["work"]}, headers=UI)
    assert r.status_code == 200 and _tx_cat(db, tid) == world["work"]


def test_agent_move_privacy_check_error_is_503(client, db, world, monkeypatch):
    def _boom(*a, **k):
        raise privacy.PrivacyError("db locked")
    monkeypatch.setattr(privacy, "category_change_exposes", _boom)
    tid = world["tx"]["work"]
    r = client.patch(f"/api/memory/transcriptions/{tid}/category", json={"category_id": None})
    assert r.status_code == 503 and _tx_cat(db, tid) == world["work"]


def test_agent_bulk_refuses_whole_request_when_one_record_is_private(client, db, world):
    ids = [world["tx"]["work"], world["tx"]["priv"]]
    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": ids, "category_id": None})
    assert r.status_code == 403 and r.get_json()["error_code"] == "PRIVATE_CATEGORY_CHANGE"
    assert _tx_cat(db, world["tx"]["work"]) == world["work"]
    assert _tx_cat(db, world["tx"]["priv"]) == world["priv"]


def test_agent_bulk_of_public_records_works_with_mcp_rules(client, db, world):
    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": [world["tx"]["work"], world["tx"]["none"]],
                          "category_id": world["priv"]})
    assert r.status_code == 200 and r.get_json()["updated"] == 2


def test_agent_bulk_limit_source_type_and_more_than_five_ids(client, db, world):
    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"source_type": "file", "category_id": world["work"]})
    assert r.status_code == 400 and r.get_json()["error_code"] == "BULK_LIMIT"
    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": [1, 2, 3, 4, 5, 6], "category_id": world["work"]})
    assert r.status_code == 400 and r.get_json()["error_code"] == "BULK_LIMIT"
    assert _tx_cat(db, world["tx"]["none"]) is None


def test_ui_bulk_by_source_type_still_works(client, db, world):
    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"source_type": "file", "category_id": world["work"]}, headers=UI)
    assert r.status_code == 200 and _tx_cat(db, world["tx"]["none"]) == world["work"]


# ---------------------------------------------------------------- tags

def test_tag_sources_contain_mcp():
    assert "mcp" in cats.TAG_SOURCES


def test_agent_tag_link_carries_source_mcp(client, db, world):
    tid = world["tx"]["work"]
    r = client.put(f"/api/memory/{tid}/tags", json={"add": ["Бюджет"]})
    assert r.status_code == 200 and [t["name"] for t in r.get_json()["tags"]] == ["бюджет"]
    assert _links(db, tid) == [{"name": "бюджет", "source": "mcp"}]


def test_agent_unknown_tag_name_is_400_and_creates_nothing(client, db, world):
    tid = world["tx"]["work"]
    r = client.put(f"/api/memory/{tid}/tags", json={"add": ["бюджет", "ризики"]})
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_ARGUMENT"
    assert _links(db, tid) == []
    with get_db_connection(db) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM tags ORDER BY name")] == ["бюджет"]


def test_agent_may_remove_any_tag_link(client, db, world):
    tid = world["tx"]["work"]
    with get_db_connection(db) as conn:
        cats.set_tags(conn, tid, add=["бюджет"], source="manual")
    r = client.put(f"/api/memory/{tid}/tags", json={"remove": ["бюджет"]})
    assert r.status_code == 200 and _links(db, tid) == []


def test_ui_tag_add_still_creates_missing_tags_with_manual_source(client, db, world):
    tid = world["tx"]["work"]
    r = client.put(f"/api/memory/{tid}/tags", json={"add": ["ризики"]}, headers=UI)
    assert r.status_code == 200
    assert _links(db, tid) == [{"name": "ризики", "source": "manual"}]
