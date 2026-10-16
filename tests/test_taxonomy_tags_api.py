"""`/api/memory/*`: the category_id/tags/since/until axes on search/ask/ask-stream/
entities/action-items + tag dictionary CRUD + record tags + suggestions (spec
`directions-projects-tags`, story 07, contract C4 "Tags" / C5).

The same approach as `test_taxonomy_http.py`: a tmp DB (`init_database`) +
Flask `test_client` around `memory_bp` directly - the live `whisper_history.db`
is not touched. Fixtures are Cyrillic (`plan-dictated-sql-ignored-own-memory`).
"""
from __future__ import annotations

import json

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories as cats


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "taxonomy_tags_api.db")
    init_database(path)
    return path


@pytest.fixture
def conn(db):
    with get_db_connection(db) as c:
        yield c


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.register_blueprint(memory_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db
    return app.test_client()


@pytest.fixture(autouse=True)
def _reembed_spy(monkeypatch):
    """Catches every re-embed call without the real scheduler (that one pulls in
    embeddings/torch - memory map `mcp-stdio-no-heavy-models`)."""
    calls = []
    monkeypatch.setattr(
        "app.services.reembed.schedule_records_reembed",
        lambda ids, **kw: calls.append((sorted(ids), kw.get("reason"))) or
        {"mode": "point", "count": len(ids), "stale": 0})
    return calls


def _tree(conn):
    """«Фонд Робота» is already among the v15 seeds - find-or-create, as in
    `test_taxonomy_http.py._tree`."""
    row = conn.execute("SELECT id FROM categories WHERE COALESCE(name_norm, name) = ?",
                       ("фонд робота",)).fetchone()
    fund = cats.get_category(conn, row[0]) if row else cats.create_category(conn, "Фонд Робота")
    bar = cats.create_category(conn, "Акмекорп", parent_id=fund["id"])
    other = cats.create_category(conn, "Інше")
    return fund, bar, other


def _add_tx(conn, *, category_id=None, name="Планерка", text="Обговорили Барселону",
           meeting_date=None):
    cols = ["source_type", "source_name", "transcript_text", "category_id"]
    vals = ["file", name, text, category_id]
    if meeting_date is not None:
        cols.append("meeting_date"); vals.append(meeting_date)
    ph = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO transcriptions ({','.join(cols)}) VALUES ({ph})", vals)
    conn.commit()
    return cur.lastrowid


def _add_entity(conn, *, name, tids, etype="person"):
    cur = conn.execute(
        "INSERT INTO entities (type, canonical_name, normalized_name, "
        "mention_count, meeting_count) VALUES (?, ?, ?, 1, ?)",
        (etype, name, name.casefold(), len(tids)))
    eid = cur.lastrowid
    for tid in tids:
        conn.execute(
            "INSERT INTO meeting_entities (transcription_id, entity_id, mention_count) "
            "VALUES (?, ?, 1)", (tid, eid))
    conn.commit()
    return eid


def _add_action(conn, *, tid, task, owner_name=None, status="open"):
    cur = conn.execute(
        "INSERT INTO action_items (transcription_id, task, owner_name, status) "
        "VALUES (?, ?, ?, ?)", (tid, task, owner_name, status))
    conn.commit()
    return cur.lastrowid


# ============================================================
# search / ask / ask/stream - the category_id/tags/since/until axes (C5)
# ============================================================

def test_search_passes_axes_to_retrieval(client, monkeypatch):
    captured = {}

    def fake_search(db_path, q, top_k=8, **kwargs):
        captured.update(kwargs)
        return {"chunks": [], "query": q}

    monkeypatch.setattr("app.services.retrieval.search", fake_search)
    r = client.get("/api/memory/search?q=щось&category_id=5&tags=AI"
                   "&since=2026-09-01&until=2026-09-07")
    assert r.status_code == 200
    assert captured["category_id"] == 5
    assert captured["tags"] == ["AI"]
    assert captured["since"] == "2026-09-01"
    assert captured["until"] == "2026-09-07"


def test_search_bad_since_is_400_without_calling_retrieval(client, monkeypatch):
    called = []
    monkeypatch.setattr("app.services.retrieval.search",
                        lambda *a, **k: called.append(1))
    r = client.get("/api/memory/search?q=щось&since=вчора")
    assert r.status_code == 400
    assert r.get_json()["success"] is False
    assert called == []


def test_ask_passes_axes_to_rag(client, monkeypatch):
    captured = {}

    def fake_answer(db_path, q, top_k=8, **kwargs):
        captured.update(kwargs)
        return {"answer": "", "sources": [], "found": 0, "model": ""}

    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    monkeypatch.setattr("app.services.rag.answer_question", fake_answer)
    r = client.post("/api/memory/ask", json={
        "question": "питання?", "category_id": 5, "tags": "AI",
        "since": "2026-09-01", "until": "2026-09-07"})
    assert r.status_code == 200
    assert captured["category_id"] == 5
    assert captured["tags"] == ["AI"]
    assert captured["since"] == "2026-09-01"
    assert captured["until"] == "2026-09-07"


def test_ask_bad_since_is_400_without_calling_rag(client, monkeypatch):
    called = []
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    monkeypatch.setattr("app.services.rag.answer_question",
                        lambda *a, **k: called.append(1))
    r = client.post("/api/memory/ask", json={"question": "питання?", "since": "вчора"})
    assert r.status_code == 400
    assert called == []


def test_ask_stream_passes_axes_to_rag(client, monkeypatch):
    captured = {}

    def fake_stream(db_path, q, top_k=8, **kwargs):
        captured.update(kwargs)
        yield "event: done\ndata: {}\n\n"

    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    monkeypatch.setattr("app.services.rag.answer_question_stream", fake_stream)
    r = client.post("/api/memory/ask/stream", json={
        "question": "питання?", "tags": "AI,інвестори"})
    r.get_data()
    assert captured["tags"] == ["AI", "інвестори"]


def test_ask_stream_bad_until_is_400_without_calling_rag(client, monkeypatch):
    called = []
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    monkeypatch.setattr("app.services.rag.answer_question_stream",
                        lambda *a, **k: called.append(1))
    r = client.post("/api/memory/ask/stream", json={"question": "питання?", "until": "завтра"})
    assert r.status_code == 400
    assert called == []


# ============================================================
# /api/memory/entities - category_id (with expansion) / tags
# ============================================================

def test_entities_category_id_counts_mentions_in_project_records(client, conn):
    fund, bar, other = _tree(conn)
    t_bar = _add_tx(conn, category_id=bar["id"], name="У Акмекорп")
    t_other = _add_tx(conn, category_id=other["id"], name="Інше")
    e_bar = _add_entity(conn, name="Андрій", tids=[t_bar])
    e_other = _add_entity(conn, name="Марія", tids=[t_other])

    r = client.get(f"/api/memory/entities?category_id={fund['id']}&min_meetings=1")
    assert r.status_code == 200
    ids = {e["id"] for e in r.get_json()["entities"]}
    assert ids == {e_bar}
    assert e_other not in ids


def test_entities_tags_filters_to_tagged_records_and_unknown_tag_is_empty(client, conn):
    t_ai = _add_tx(conn, name="AI-запис")
    t_bare = _add_tx(conn, name="Без тегів")
    cats.set_tags(conn, t_ai, add=["AI"])
    e_ai = _add_entity(conn, name="Андрій", tids=[t_ai])
    e_bare = _add_entity(conn, name="Марія", tids=[t_bare])

    r = client.get("/api/memory/entities?tags=AI&min_meetings=1")
    assert r.status_code == 200
    ids = {e["id"] for e in r.get_json()["entities"]}
    assert ids == {e_ai}
    assert e_bare not in ids

    r = client.get("/api/memory/entities?tags=невідомий&min_meetings=1")
    assert r.status_code == 200
    assert r.get_json()["entities"] == []


def test_entities_bad_category_id_is_400(client):
    r = client.get("/api/memory/entities?category_id=сміття")
    assert r.status_code == 400
    assert r.get_json()["success"] is False


# ============================================================
# /api/memory/action-items - the same axes (category_id, tags)
# ============================================================

def test_action_items_category_id_expands_direction_to_projects(client, conn):
    fund, bar, other = _tree(conn)
    t_bar = _add_tx(conn, category_id=bar["id"], name="У Акмекорп")
    t_other = _add_tx(conn, category_id=other["id"], name="Інше")
    a_bar = _add_action(conn, tid=t_bar, task="Завдання А")
    a_other = _add_action(conn, tid=t_other, task="Завдання Б")

    r = client.get(f"/api/memory/action-items?category_id={fund['id']}")
    assert r.status_code == 200
    ids = {a["id"] for a in r.get_json()["action_items"]}
    assert ids == {a_bar}
    assert a_other not in ids


def test_action_items_tags_filters_and_unknown_tag_is_empty(client, conn):
    t_ai = _add_tx(conn, name="AI-запис")
    t_bare = _add_tx(conn, name="Без тегів")
    cats.set_tags(conn, t_ai, add=["AI"])
    a_ai = _add_action(conn, tid=t_ai, task="Завдання А")
    a_bare = _add_action(conn, tid=t_bare, task="Завдання Б")

    r = client.get("/api/memory/action-items?tags=AI")
    assert r.status_code == 200
    ids = {a["id"] for a in r.get_json()["action_items"]}
    assert ids == {a_ai}
    assert a_bare not in ids

    r = client.get("/api/memory/action-items?tags=невідомий")
    assert r.status_code == 200
    assert r.get_json()["action_items"] == []


# ============================================================
# Tag dictionary: GET/POST/PATCH/DELETE/merge
# ============================================================

def test_create_tag_then_repeat_post_is_idempotent_by_norm(client):
    r1 = client.post("/api/memory/tags", json={"name": "AI"})
    assert r1.status_code == 201
    tag1 = r1.get_json()["tag"]

    r2 = client.post("/api/memory/tags", json={"name": " ai "})
    assert r2.status_code == 200
    tag2 = r2.get_json()["tag"]
    assert tag2["id"] == tag1["id"]


def test_create_tag_empty_or_too_long_is_400(client):
    r = client.post("/api/memory/tags", json={"name": "  "})
    assert r.status_code == 400
    r = client.post("/api/memory/tags", json={"name": "а" * 41})
    assert r.status_code == 400


def test_list_tags(client):
    client.post("/api/memory/tags", json={"name": "AI"})
    client.post("/api/memory/tags", json={"name": "інвестори"})
    r = client.get("/api/memory/tags")
    assert r.status_code == 200
    names = {t["name"] for t in r.get_json()["tags"]}
    assert names == {"AI", "інвестори"}


def test_patch_tag_rename_collision_is_409(client):
    a = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    client.post("/api/memory/tags", json={"name": "інвестори"})
    r = client.patch(f"/api/memory/tags/{a['id']}", json={"name": "Інвестори"})
    assert r.status_code == 409


def test_patch_tag_rename_ok(client):
    a = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    r = client.patch(f"/api/memory/tags/{a['id']}", json={"name": "ШІ"})
    assert r.status_code == 200
    assert r.get_json()["tag"]["name"] == "ШІ"


def test_patch_tag_rename_reembeds_live_records_only_color_change_does_not(client, conn, _reembed_spy):
    a = _add_tx(conn, name="Раз")
    b = _add_tx(conn, name="Два")
    c = _add_tx(conn, name="Три (видалений)")
    conn.execute("UPDATE transcriptions SET deleted_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00", c))
    conn.commit()
    tag = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    for tid in (a, b, c):
        client.put(f"/api/memory/{tid}/tags", json={"add": ["AI"]})
    _reembed_spy.clear()

    r = client.patch(f"/api/memory/tags/{tag['id']}", json={"name": "ШІ"})
    assert r.status_code == 200
    assert r.get_json()["reembed"]["count"] == 2
    assert sorted(_reembed_spy[-1][0]) == sorted([a, b])
    assert _reembed_spy[-1][1] == "tag_renamed"

    _reembed_spy.clear()
    r2 = client.patch(f"/api/memory/tags/{tag['id']}", json={"color": "#112233"})
    assert r2.status_code == 200
    assert _reembed_spy == []


def test_patch_tag_rename_same_exact_name_does_not_reembed(client, conn, _reembed_spy):
    """Minor 3: `name_changed` compares the exact stored string, not
    `name_norm` - «AI» -> «AI» changes nothing in the prefix."""
    a = _add_tx(conn, name="Раз")
    tag = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    client.put(f"/api/memory/{a}/tags", json={"add": ["AI"]})
    _reembed_spy.clear()

    r = client.patch(f"/api/memory/tags/{tag['id']}", json={"name": "AI"})
    assert r.status_code == 200
    assert r.get_json()["reembed"]["count"] == 0
    assert _reembed_spy == []


def test_set_record_tags_add_rejects_empty_name_before_write(client, conn):
    """`""` by itself: `parse_tags` quietly drops empty items (fine
    for filters), so without a separate guard of the raw list the request returned
    200 and wrote nothing - now a 400 before the first INSERT (Minor 4)."""
    tid = _add_tx(conn, name="Запис")

    r = client.put(f"/api/memory/{tid}/tags", json={"add": [""]})
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM record_tags WHERE transcription_id = ?",
                        (tid,)).fetchone()[0] == 0

    r2 = client.put(f"/api/memory/{tid}/tags", json={"add": ["AI", ""]})
    assert r2.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE name_norm = 'ai'").fetchone()[0] == 0


def test_set_record_tags_add_rejects_too_long_name_before_write(client, conn):
    tid = _add_tx(conn, name="Запис")
    too_long = "а" * 41

    r = client.put(f"/api/memory/{tid}/tags", json={"add": ["AI", too_long]})
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE name_norm = 'ai'").fetchone()[0] == 0


def test_resolve_suggestions_accept_rejects_empty_name_before_write(client, conn):
    tid = _add_tx(conn, name="Запис")
    cats.set_suggested_tags(conn, tid, ["AI"])

    r = client.post(f"/api/memory/{tid}/tags/suggestions", json={"accept": [""]})
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM record_tags WHERE transcription_id = ?",
                        (tid,)).fetchone()[0] == 0


def test_resolve_suggestions_accept_unknown_name_is_400_dismiss_unknown_is_noop(
        client, conn, _reembed_spy):
    tid = _add_tx(conn, name="Запис")
    cats.set_suggested_tags(conn, tid, ["AI"])
    _reembed_spy.clear()

    bad = client.post(f"/api/memory/{tid}/tags/suggestions", json={"accept": ["вигаданий"]})
    assert bad.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM record_tags WHERE transcription_id = ?",
                        (tid,)).fetchone()[0] == 0
    assert _reembed_spy == []

    ok = client.post(f"/api/memory/{tid}/tags/suggestions", json={"accept": ["ai"]})
    assert ok.status_code == 200
    row = conn.execute(
        "SELECT rt.source FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
        "WHERE rt.transcription_id = ? AND tg.name_norm = 'ai'", (tid,)).fetchone()
    assert row[0] == "claude"

    noop = client.post(f"/api/memory/{tid}/tags/suggestions", json={"dismiss": ["чужий"]})
    assert noop.status_code == 200


def test_delete_tag_unbinds_record_tags_and_schedules_reembed(client, conn, _reembed_spy):
    tid = _add_tx(conn, name="AI-запис")
    tag = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    client.put(f"/api/memory/{tid}/tags", json={"add": ["AI"]})
    _reembed_spy.clear()

    r = client.delete(f"/api/memory/tags/{tag['id']}")
    assert r.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM record_tags WHERE tag_id = ?",
                        (tag["id"],)).fetchone()[0] == 0
    assert any(tid in ids for ids, _reason in _reembed_spy)


def test_delete_unknown_tag_is_404(client):
    r = client.delete("/api/memory/tags/99999")
    assert r.status_code == 404


# ============================================================
# `color` - a literal #RGB/#RGBA/#RRGGBB/#RRGGBBAA, otherwise 400 (minor 12 r1,
# round 5 review): without a check any text landed in
# `style="--tag-color:..."`.
# ============================================================

def test_create_tag_rejects_arbitrary_css_as_color(client):
    r = client.post("/api/memory/tags",
                    json={"name": "ШІ", "color": "red;background:url(x)"})
    assert r.status_code == 400
    assert r.get_json()["success"] is False
    assert client.get("/api/memory/tags").get_json()["tags"] == []


def test_create_tag_accepts_hex_color(client):
    r = client.post("/api/memory/tags", json={"name": "ШІ", "color": "#1A2B3C"})
    assert r.status_code == 201
    assert r.get_json()["tag"]["color"] == "#1A2B3C"


def test_patch_tag_rejects_bad_color_leaves_it_unchanged(client):
    tag = client.post("/api/memory/tags",
                      json={"name": "AI", "color": "#112233"}).get_json()["tag"]
    r = client.patch(f"/api/memory/tags/{tag['id']}", json={"color": "url(x)"})
    assert r.status_code == 400
    got = next(t for t in client.get("/api/memory/tags").get_json()["tags"]
              if t["id"] == tag["id"])
    assert got["color"] == "#112233"


def test_patch_tag_null_color_clears_it(client):
    tag = client.post("/api/memory/tags",
                      json={"name": "AI", "color": "#112233"}).get_json()["tag"]
    r = client.patch(f"/api/memory/tags/{tag['id']}", json={"color": None})
    assert r.status_code == 200
    got = next(t for t in client.get("/api/memory/tags").get_json()["tags"]
              if t["id"] == tag["id"])
    assert got["color"] is None


# ============================================================
# POST /api/memory/tags/merge - a bad keep_id/merge_id value -> 400
# (minor 6 r4).
# ============================================================

def test_merge_tags_rejects_garbled_id(client):
    keep = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    r = client.post("/api/memory/tags/merge",
                    json={"keep_id": keep["id"], "merge_id": "²"})
    assert r.status_code == 400
    assert r.get_json()["success"] is False


def test_merge_tags_rejects_bool_id(client):
    keep = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    r = client.post("/api/memory/tags/merge",
                    json={"keep_id": True, "merge_id": keep["id"]})
    assert r.status_code == 400
    assert r.get_json()["success"] is False


def test_merge_tags_moves_records_under_keep_id(client, conn):
    tid_a = _add_tx(conn, name="Запис А")
    tid_b = _add_tx(conn, name="Запис Б")
    keep = client.post("/api/memory/tags", json={"name": "AI"}).get_json()["tag"]
    merge = client.post("/api/memory/tags", json={"name": "Штучний інтелект"}).get_json()["tag"]
    cats.set_tags(conn, tid_a, add=["AI"])
    cats.set_tags(conn, tid_b, add=["Штучний інтелект"])

    r = client.post("/api/memory/tags/merge",
                    json={"keep_id": keep["id"], "merge_id": merge["id"]})
    assert r.status_code == 200
    rows = conn.execute("SELECT transcription_id FROM record_tags WHERE tag_id = ?",
                        (keep["id"],)).fetchall()
    assert {row[0] for row in rows} == {tid_a, tid_b}
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE id = ?",
                        (merge["id"],)).fetchone()[0] == 0


# ============================================================
# PUT /api/memory/<id>/tags
# ============================================================

def test_set_record_tags_add_and_remove(client, conn, _reembed_spy):
    tid = _add_tx(conn, name="Запис")
    cats.set_tags(conn, tid, add=["інвестори"])
    _reembed_spy.clear()

    r = client.put(f"/api/memory/{tid}/tags",
                   json={"add": ["AI", "новий"], "remove": ["інвестори"]})
    assert r.status_code == 200
    data = r.get_json()
    names = [t["name"] for t in data["tags"]]
    assert set(names) == {"AI", "новий"}
    assert names == sorted(names, key=cats.norm_name)
    assert any(tid in ids for ids, _reason in _reembed_spy)
    # The tag "новий" really appeared in the dictionary.
    assert any(t["name"] == "новий" for t in
              client.get("/api/memory/tags").get_json()["tags"])


def test_set_record_tags_requires_add_or_remove(client, conn):
    tid = _add_tx(conn, name="Запис")
    r = client.put(f"/api/memory/{tid}/tags", json={})
    assert r.status_code == 400


def test_set_record_tags_unknown_record_is_404(client):
    r = client.put("/api/memory/99999/tags", json={"add": ["AI"]})
    assert r.status_code == 404


# ============================================================
# POST /api/memory/<id>/tags/suggestions
# ============================================================

def test_resolve_suggestions_accepts_and_dismisses(client, conn, _reembed_spy):
    tid = _add_tx(conn, name="Запис")
    cats.set_suggested_tags(conn, tid, ["AI", "x"])
    _reembed_spy.clear()

    r = client.post(f"/api/memory/{tid}/tags/suggestions",
                    json={"accept": ["AI"], "dismiss": ["x"]})
    assert r.status_code == 200
    data = r.get_json()
    assert data["suggested_tags"] == []
    assert [t["name"] for t in data["tags"]] == ["AI"]
    row = conn.execute(
        "SELECT rt.source FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
        "WHERE rt.transcription_id = ? AND tg.name_norm = 'ai'", (tid,)).fetchone()
    assert row[0] == "claude"
    assert any(tid in ids for ids, _reason in _reembed_spy)


def test_resolve_suggestions_requires_accept_or_dismiss(client, conn):
    tid = _add_tx(conn, name="Запис")
    cats.set_suggested_tags(conn, tid, ["AI"])
    r = client.post(f"/api/memory/{tid}/tags/suggestions", json={"accept": [], "dismiss": []})
    assert r.status_code == 400
