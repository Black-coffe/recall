"""Archive scope tests (Track 2, app/services/scope.py + retrieval.entity_ids).

They verify two narrowing layers:
  1. direction by Telegram chat (deterministic history labelling + chat binding);
  2. slice by an entity-project through meeting_entities (many-to-many).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import scope


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "scope.db")
    init_database(path)
    with get_db_connection(path) as conn:
        # The migration itself seeds default directions - remove them so the test has
        # exactly the table it describes (otherwise ids conflict).
        conn.execute("DELETE FROM categories")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (1, 'Фонд Робота', 'фонд робота', 1)")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (2, 'NOVA', 'nova', 2)")
        # Chat #1 is monitored and wrongly sits in «Фонд», #2 is not in the table at all.
        conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                     "category_id) VALUES (-100, 'Nova Dance & Робота', 'group', 1, 1)")
        for i, (cid, chat) in enumerate([(-100, "Nova Dance & Робота"),
                                         (-100, "Nova Dance & Робота"),
                                         (-200, "Pegasus & Робота")], start=1):
            conn.execute(
                "INSERT INTO transcriptions (id, source_type, source_name, tg_chat_id, "
                "tg_chat_title, category_id, created_at) "
                "VALUES (?, 'telegram', ?, ?, ?, ?, '2026-05-14 10:00:00')",
                (i, f"[TG] {chat}", cid, chat, 1 if cid == -100 else None))
        conn.commit()
    return path


def _mapping(tmp_path: Path, rows: list[dict]) -> str:
    import json
    p = tmp_path / "map.json"
    p.write_text(json.dumps({"chats": rows}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_apply_chat_categories_creates_binds_and_moves(db: str, tmp_path: Path):
    path = _mapping(tmp_path, [
        {"chat_id": -100, "title": "Nova Dance & Робота", "category": "NOVA"},
        {"chat_id": -200, "title": "Pegasus & Робота", "category": "Pegasus"},
    ])
    res = scope.apply_chat_categories(db, mapping_path=path)

    assert res["categories_created"] == [
        {"path": "Pegasus", "namesake": [], "note": None}]
    assert res["chats_bound"] == 1        # -100 rebound from «Фонд» to NOVA
    assert res["chats_inserted"] == 1     # -200 was not monitored - a row was created
    assert res["records_recategorized"] == 3

    with get_db_connection(db) as conn:
        cats = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
        rows = {r["id"]: r["category_id"] for r in
                conn.execute("SELECT id, category_id FROM transcriptions")}
        chat200 = conn.execute(
            "SELECT enabled, category_id FROM tg_monitored_chats WHERE chat_id = -200"
        ).fetchone()
    assert rows[1] == rows[2] == cats["NOVA"]
    assert rows[3] == cats["Pegasus"]
    # Not monitored, but the direction is pinned - enabling will not return it to "no category".
    assert chat200["enabled"] == 0 and chat200["category_id"] == cats["Pegasus"]


def test_apply_chat_categories_idempotent_and_dry_run(db: str, tmp_path: Path):
    path = _mapping(tmp_path, [{"chat_id": -100, "title": "NOVA", "category": "NOVA"}])
    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert preview["records_recategorized"] == 2 and preview["chats_bound"] == 1
    with get_db_connection(db) as conn:  # dry-run writes nothing
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 1"
                            ).fetchone()["category_id"] == 1

    scope.apply_chat_categories(db, mapping_path=path)
    again = scope.apply_chat_categories(db, mapping_path=path)
    assert again["records_recategorized"] == 0 and again["chats_bound"] == 0


def test_load_mapping_rejects_incomplete_rows(tmp_path: Path):
    bad = _mapping(tmp_path, [{"title": "без chat_id", "category": "NOVA"}])
    with pytest.raises(ValueError):
        scope.load_mapping(bad)


# ============================================================
# Contract C7: parent + tags in the chat map (story 05)
# ============================================================

def test_apply_chat_categories_creates_project_under_parent_with_tags(
        db: str, tmp_path: Path, monkeypatch):
    """`{"parent": "Фонд Робота", "tags": ["AI"]}` - the project is created under
    the existing direction, tags lie with source='chat', and after a real pass
    the changed records go to re-embed (mock)."""
    calls = []
    monkeypatch.setattr(
        "app.services.reembed.schedule_records_reembed",
        lambda ids, **kw: calls.append((sorted(ids), kw)) or
        {"mode": "point", "count": len(ids), "stale": 0})

    path = _mapping(tmp_path, [
        {"chat_id": -300, "title": "Акмекорп чат", "category": "Акмекорп",
         "parent": "Фонд Робота", "tags": ["AI"]},
    ])
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, tg_chat_id, "
            "tg_chat_title, category_id, created_at) VALUES (10, 'telegram', "
            "'[TG] Акмекорп чат', -300, 'Акмекорп чат', NULL, '2026-05-14 10:00:00')")
        conn.commit()

    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert preview["records_recategorized"] >= 1
    assert calls == [], "--dry-run must write nothing"
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM categories WHERE name = 'Акмекорп'").fetchone()[0] == 0

    res = scope.apply_chat_categories(db, mapping_path=path)
    # The plan names the PATH, not the bare name: «Акмекорп» and «Фонд Робота → Акмекорп» are
    # two different nodes, and the preview must say which one the pass will create.
    assert res["categories_created"] == [
        {"path": "Фонд Робота → Акмекорп", "namesake": [], "note": None},
    ], "«Фонд Робота» already existed"
    assert res["tags_applied"] == 1

    with get_db_connection(db) as conn:
        parent_id = conn.execute(
            "SELECT id FROM categories WHERE name = 'Фонд Робота'").fetchone()["id"]
        proj = conn.execute(
            "SELECT id, parent_id FROM categories WHERE name = 'Акмекорп'").fetchone()
        assert proj["parent_id"] == parent_id
        rec = conn.execute(
            "SELECT category_id FROM transcriptions WHERE id = 10").fetchone()
        assert rec["category_id"] == proj["id"]
        tags = [r["name"] for r in conn.execute(
            "SELECT tg.name FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
            "WHERE rt.transcription_id = 10 AND rt.source = 'chat'")]
        assert tags == ["AI"]
    assert calls, "the real pass must have scheduled a re-embed"
    assert 10 in calls[0][0]
    assert calls[0][1]["reason"] == "apply-chats"

    # Idempotent: a repeated pass changes nothing and does not call re-embed.
    calls.clear()
    again = scope.apply_chat_categories(db, mapping_path=path)
    assert again["records_recategorized"] == 0 and again["tags_applied"] == 0
    assert calls == []


def test_apply_chat_categories_dropping_tag_keeps_manual_one(
        db: str, tmp_path: Path, monkeypatch):
    """Removing a tag from a chat in the map removes only `source='chat'` - a manual tag on
    a specific record stays (ADR §B, "one slot - one owner")."""
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed", lambda *a, **k: None)
    with get_db_connection(db) as conn:
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, tg_chat_id, "
            "category_id, created_at) VALUES (10, 'telegram', '[TG] x', -300, NULL, "
            "'2026-05-14 10:00:00')")
        conn.commit()

    path = _mapping(tmp_path, [{"chat_id": -300, "title": "x", "category": "NOVA",
                                "tags": ["AI", "інвестори"]}])
    scope.apply_chat_categories(db, mapping_path=path)
    with get_db_connection(db) as conn:
        from app.services import categories as cats
        cats.set_tags(conn, 10, add=["ручний"], source="manual")

    path2 = _mapping(tmp_path, [{"chat_id": -300, "title": "x", "category": "NOVA",
                                 "tags": ["AI"]}])         # «інвестори» removed from the chat
    scope.apply_chat_categories(db, mapping_path=path2)
    with get_db_connection(db) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT tg.name FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
            "WHERE rt.transcription_id = 10")}
    assert names == {"AI", "ручний"}, "the chat tag was removed, the manual one stayed"


def test_prior_correction_damps_giant_category():
    """The giant must not win merely by size - but the dwarf must not win by smallness either."""
    cands = [{"category_id": 1, "name": "Фонд", "score": 0.55},
             {"category_id": 2, "name": "NOVA", "score": 0.45}]
    ranked = scope._prior_correct(cands, {1: 2000, 2: 50})
    assert ranked[0]["category_id"] == 2, "the smaller category with almost the same score must win"

    # But with a clear lead the giant stays first - the correction is soft.
    ranked2 = scope._prior_correct(
        [{"category_id": 1, "name": "Фонд", "score": 0.95},
         {"category_id": 2, "name": "NOVA", "score": 0.05}], {1: 2000, 2: 50})
    assert ranked2[0]["category_id"] == 1


def test_label_uncategorized_claude_arbiter_sees_project_paths(tmp_path: Path, monkeypatch):
    """label_uncategorized --claude: the arbiter sees candidates as paths
    «Direction -> Project» (contract C7) and its answer is written via `set_category`."""
    path = str(tmp_path / "lc.db")
    init_database(path)
    with get_db_connection(path) as conn:
        conn.execute("DELETE FROM categories")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                     "VALUES (1, 'Фонд Робота', 'фонд робота', 1, NULL)")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                     "VALUES (2, 'Акмекорп', 'акмекорп', 2, 1)")
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, "
                     "category_id, embedded_at) VALUES (1, 'file', 'немарченко запис', NULL, "
                     "CURRENT_TIMESTAMP)")
        conn.commit()

    from app.services import categorize as categorize_mod
    monkeypatch.setattr(categorize_mod, "suggest_category", lambda db_path, tid, k=8: {
        "suggestion": {"category_id": 1, "name": "Фонд Робота", "confidence": 0.5},
        "candidates": [{"category_id": 1, "name": "Фонд Робота", "score": 0.5},
                       {"category_id": 2, "name": "Акмекорп", "score": 0.5}],
        "neighbors": 2})

    seen = {}

    def fake_ask(title, excerpt, cats, *a, **kw):
        seen["cats"] = dict(cats)
        return next(cid for cid, p in cats if p == "Фонд Робота → Акмекорп")

    monkeypatch.setattr(scope, "_ask_claude_category", fake_ask)

    res = scope.label_uncategorized(path, use_claude=True, min_confidence=0.6)
    assert seen["cats"].get(2) == "Фонд Робота → Акмекорп"
    assert res["by_claude"] == 1
    with get_db_connection(path) as conn:
        cat = conn.execute(
            "SELECT category_id FROM transcriptions WHERE id = 1").fetchone()["category_id"]
    assert cat == 2


def test_label_uncategorized_schedules_point_reembed(tmp_path: Path, monkeypatch):
    """Contract C7 (like `apply_chat_categories`): a real `label-rest` pass
    puts the labelled records into a targeted re-embed with exactly one call; `--dry-run`
    and a repeated pass (everything already labelled) - no call."""
    path = str(tmp_path / "lr.db")
    init_database(path)
    with get_db_connection(path) as conn:
        conn.execute("DELETE FROM categories")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                     "VALUES (1, 'Фонд Робота', 'фонд робота', 1, NULL)")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                     "VALUES (2, 'Акмекорп', 'акмекорп', 2, 1)")
        for tid, title in ((1, "Дзвінок з Акмекорп"), (2, "Нарада без назви"),
                           (3, "Ще один запис")):
            conn.execute(
                "INSERT INTO transcriptions (id, source_type, source_name, "
                "category_id, embedded_at) VALUES (?, 'file', ?, NULL, CURRENT_TIMESTAMP)",
                (tid, title))
        conn.commit()

    from app.services import categorize as categorize_mod
    monkeypatch.setattr(categorize_mod, "suggest_category", lambda db_path, tid, k=8: {
        "suggestion": {"category_id": 1, "name": "Фонд Робота", "confidence": 0.5},
        "candidates": [{"category_id": 1, "name": "Фонд Робота", "score": 0.5}],
        "neighbors": 1})
    monkeypatch.setattr(scope, "_ask_claude_category",
                        lambda title, excerpt, cats, *a, **kw: 2)

    calls = []
    monkeypatch.setattr(
        "app.services.reembed.schedule_records_reembed",
        lambda ids, **kw: calls.append((sorted(ids), kw)) or
        {"mode": "point", "count": len(ids), "stale": 0})

    preview = scope.label_uncategorized(path, use_claude=True, min_confidence=0.6, dry_run=True)
    assert preview["labeled"] == 3
    assert calls == [], "--dry-run must write nothing"
    with get_db_connection(path) as conn:
        assert all(r["category_id"] is None for r in conn.execute(
            "SELECT category_id FROM transcriptions"))

    res = scope.label_uncategorized(path, use_claude=True, min_confidence=0.6)
    assert res["labeled"] == 3
    assert len(calls) == 1, "exactly one re-embed call per pass"
    ids, kw = calls[0]
    assert ids == [1, 2, 3]
    assert kw["db_path"] == path
    assert kw["reason"] == "label-rest"

    # Repeated pass: everything is already labelled - re-embed is not called.
    calls.clear()
    again = scope.label_uncategorized(path, use_claude=True, min_confidence=0.6)
    assert again["labeled"] == 0
    assert calls == []


def test_title_matcher_ignores_generic_tokens(db: str):
    with get_db_connection(db) as conn:
        matchers = scope._title_matchers(conn)
    by_name = {name: rx for _, name, rx in matchers}
    assert "NOVA" in by_name
    assert by_name["NOVA"].search("NOVA я та Микола по маркетингу")
    assert not by_name["NOVA"].search("домовились про зустріч"), "must not catch a substring inside a word"
    # «Фонд Робота» consists only of stop words -> there is no matcher for it
    assert "Фонд Робота" not in by_name


def test_resolve_scope_matches_canonical_and_alias(db: str):
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (10, 'project', 'Acmecorp', 'acmecorp')")
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (10, 'Акмекорп', 'акмекорп')")
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (11, 'project', 'Ковальчука', 'ковальчука')")
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) VALUES (1, 10)")
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) VALUES (3, 11)")
        conn.commit()

    assert scope.resolve_scope(db, "acmecorp") == [10]
    assert scope.resolve_scope(db, "Акмекорп") == [10]          # via alias
    assert sorted(scope.resolve_scope(db, "Acmecorp, Ковальчука")) == [10, 11]
    assert scope.resolve_scope(db, "нема такого") == []

    assert scope.scope_transcription_ids(db, [10]) == [1]
    assert sorted(scope.scope_transcription_ids(db, [10, 11])) == [1, 3]


def test_scope_excludes_soft_deleted(db: str):
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (10, 'project', 'Acmecorp', 'acmecorp')")
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) VALUES (1, 10)")
        conn.execute("UPDATE transcriptions SET deleted_at = 1 WHERE id = 1")
        conn.commit()
    assert scope.scope_transcription_ids(db, [10]) == []


def test_retrieval_scope_sql_shape():
    """An empty scope adds no condition (otherwise the search would narrow to zero)."""
    from app.services import retrieval
    sql, params = retrieval._scope_sql(None)
    assert sql == "" and params == []
    sql, params = retrieval._scope_sql([7, 9])
    assert "ch.transcription_id IN" in sql and params == [7, 9]


def test_scope_filter_includes_records_without_graph_links(db: str):
    """The slice must not hide records that simply did not go through enrichment.

    meeting_entities exists only for enriched ones (~a noticeable share of the archive), so a "graph only" slice
    silently dropped the rest - exactly where the golden-set case with the document failed.
    """
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (10, 'project', 'Datalink', 'datalink')")
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (10, 'Сенсети', 'сенсети')")
        # id=1 has a link in the graph; id=2 has none, but its title contains the alias.
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) VALUES (1, 10)")
        conn.execute("UPDATE transcriptions SET source_name = 'Сенсети, базова інфраструктура' "
                     "WHERE id = 2")
        conn.commit()

    graph_only = scope.scope_transcription_ids(db, [10])
    full = scope.scope_filter_ids(db, "Datalink")
    assert graph_only == [1]
    assert set(full) >= {1, 2}, "the text layer must pick up the unenriched record"


def test_scope_filter_ids_direction_name_includes_projects(tmp_path: Path):
    """A name matching a DIRECTION takes the subtree (contract C2); a PROJECT - only itself."""
    path = str(tmp_path / "st.db")
    init_database(path)
    with get_db_connection(path) as conn:
        conn.execute("DELETE FROM categories")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                     "VALUES (1, 'Фонд Робота', 'фонд робота', 1, NULL)")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                     "VALUES (2, 'Акмекорп', 'акмекорп', 2, 1)")
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (1, 'file', 'запис напрямку', 1)")
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, category_id) "
                     "VALUES (2, 'file', 'запис проєкту', 2)")
        conn.commit()

    direction_scope = scope.scope_filter_ids(path, "Фонд Робота")
    project_scope = scope.scope_filter_ids(path, "Акмекорп")
    assert set(direction_scope) == {1, 2}
    assert set(project_scope) == {2}


# ============================================================
# Wave 4.5.1a: the slice expands to a whole thread
# ============================================================

def _tg_thread_fixture(path):
    """Thread: the project name sounds once, the decision is in a neighbouring reply."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO tg_threads (id, chat_id, label, status) "
                 "VALUES (1, -100, 'Бюджет', 'open')")
    rows = [(1, "по Лучанці треба інвестора великого", 1),
            (2, "а скільки там виходить?", 1),
            (3, "ок, беремо", 1),
            (4, "зовсім інша розмова", None)]
    for msg_id, text, thread in rows:
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "tg_chat_id, tg_message_id, tg_date, tg_thread_id) "
            "VALUES ('telegram', ?, ?, -100, ?, '2026-06-01T10:00:00+00:00', ?)",
            (f"[TG] {text[:20]}", text, msg_id, thread))
    conn.commit()
    conn.close()


def test_scope_expands_mention_to_whole_thread(tmp_path):
    """Live measurement: «Ковальчука» was mentioned 23 times inside fund chats, and exactly these
    23 one-liners were all that project= saw. The decision is in neighbouring
    replies where the name is gone."""
    path = str(tmp_path / "t.db")
    init_database(path)
    _tg_thread_fixture(path)
    got = set(scope._expand_to_threads(path, {1}))
    assert got == {1, 2, 3}, "the thread must come whole"


def test_scope_expansion_stops_at_thread_boundary(tmp_path):
    """Expanding to the CHAT would be a return to what the wave is curing."""
    path = str(tmp_path / "t.db")
    init_database(path)
    _tg_thread_fixture(path)
    assert 4 not in scope._expand_to_threads(path, {1})


def test_scope_expansion_noop_without_threads(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    _tg_thread_fixture(path)
    assert scope._expand_to_threads(path, {4}) == {4}


def test_scope_expansion_handles_empty(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    assert scope._expand_to_threads(path, set()) == set()


# ============================================================
# v47: same-named nodes in different directions (story 20)
# ============================================================

def _two_markets(db: str) -> dict:
    """«Фонд Робота → Маркетинг» and «Фріланс → Маркетинг» + one record in each.

    Record titles deliberately WITHOUT the word «Маркетинг»: otherwise the text layer of the slice
    would catch them itself and the test would prove not what it checks (the tree)."""
    from app.services import categories
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (3, 'Фріланс', 'фріланс', 3)")
        conn.commit()
        m_fund = categories.create_category(conn, "Маркетинг", parent_id=1)["id"]
        m_free = categories.create_category(conn, "Маркетинг", parent_id=3)["id"]
        ids = {}
        for key, cat in (("work", m_fund), ("free", m_free), ("bare_fund", 1)):
            cur = conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                "category_id, created_at) VALUES ('file', ?, 'Обговорили кошторис.', ?, "
                "'2026-05-14 10:00:00')", (f"Планерка {key}", cat))
            ids[key] = cur.lastrowid
        conn.commit()
    return {"m_fund": m_fund, "m_free": m_free, "rec": ids}


def test_scope_filter_ids_on_a_namesake_raises_instead_of_guessing(db: str):
    """A bare name sits on two nodes - the slice takes neither the first nor both:
    an exception with a list of paths goes up (`rag` does not swallow it), because a quiet
    merge would answer a different question than was asked."""
    from app.services import categories
    _two_markets(db)
    with pytest.raises(categories.CategoryError) as exc:
        scope.scope_filter_ids(db, "Маркетинг")
    assert exc.value.code == "ambiguous"
    assert exc.value.candidates == ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]


def test_rag_does_not_swallow_the_ambiguous_scope_error(db: str):
    """`rag._resolve_project` swallows "nothing found" (we search wider), but
    NOT ambiguity - otherwise a 409 would turn into a quiet answer over the whole
    archive."""
    from app.services import categories, rag
    _two_markets(db)
    with pytest.raises(categories.CategoryError):
        rag._resolve_project(db, "Маркетинг")


def test_scope_filter_ids_full_path_narrows_to_one_node(db: str):
    t = _two_markets(db)
    got = set(scope.scope_filter_ids(db, "Фонд Робота → Маркетинг"))
    assert got == {t["rec"]["work"]}


def test_scope_filter_ids_direction_still_expands_to_subtree(db: str):
    t = _two_markets(db)
    got = set(scope.scope_filter_ids(db, "Фонд Робота"))
    assert {t["rec"]["work"], t["rec"]["bare_fund"]} <= got
    assert t["rec"]["free"] not in got


def _mock_claude(monkeypatch, answer: str):
    """Mock of the anthropic client: the arbiter sees exactly this answer."""
    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Messages:
        @staticmethod
        def create(**kwargs):
            return type("R", (), {"content": [_Block(answer)]})()

    from app.services import text_polishing
    monkeypatch.setattr(text_polishing, "_get_client",
                        lambda: type("C", (), {"messages": _Messages})())


def _paths(db: str) -> list:
    with get_db_connection(db) as conn:
        return scope._category_paths(conn)


def test_claude_arbiter_maps_a_path_answer(db: str, monkeypatch):
    t = _two_markets(db)
    _mock_claude(monkeypatch, "Фріланс → Маркетинг")
    assert scope._ask_claude_category("запис", "текст", _paths(db)) == t["m_free"]


def test_claude_arbiter_refuses_an_ambiguous_bare_name(db: str, monkeypatch):
    """Two same-named cards - none. "The first match" here would mean assigning
    a record to a foreign direction silently; both paths go into the pass report."""
    _two_markets(db)
    _mock_claude(monkeypatch, "Маркетинг")
    stats = {}
    assert scope._ask_claude_category("запис", "текст", _paths(db), stats) is None
    assert stats["claude_ambiguous"] == 1
    assert stats["claude_ambiguous_paths"] == ["Фонд Робота → Маркетинг",
                                               "Фріланс → Маркетинг"]


def test_claude_arbiter_takes_a_unique_bare_name(db: str, monkeypatch):
    _two_markets(db)
    from app.services import categories
    with get_db_connection(db) as conn:
        bar = categories.create_category(conn, "Акмекорп", parent_id=1)["id"]
    _mock_claude(monkeypatch, "Акмекорп")
    assert scope._ask_claude_category("запис", "текст", _paths(db)) == bar


def test_title_matcher_skips_a_name_that_sits_on_two_nodes(db: str):
    _two_markets(db)
    with get_db_connection(db) as conn:
        matchers = scope._title_matchers(conn)
    by_name = {name: rx for _, name, rx in matchers}
    assert "Маркетинг" not in by_name, "an ambiguous name must not be a signal"
    assert "NOVA" in by_name


def test_chat_map_creates_own_project_instead_of_stealing_a_namesake(
        db: str, tmp_path: Path, monkeypatch):
    """A row with `parent` no longer reparents a foreign same-named node."""
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: {"mode": "point", "count": len(ids), "stale": 0})
    from app.services import categories
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (3, 'Фріланс', 'фріланс', 3)")
        conn.commit()
        m_fund = categories.create_category(conn, "Маркетинг", parent_id=1)["id"]

    path = _mapping(tmp_path, [{"chat_id": -400, "title": "Маркетинг чат",
                                "category": "Маркетинг", "parent": "Фріланс"}])
    scope.apply_chat_categories(db, mapping_path=path)

    with get_db_connection(db) as conn:
        rows = {r["id"]: r["parent_id"] for r in conn.execute(
            "SELECT id, parent_id FROM categories WHERE name_norm = 'маркетинг'")}
        bound = conn.execute(
            "SELECT category_id FROM tg_monitored_chats WHERE chat_id = -400"
        ).fetchone()["category_id"]
    assert rows[m_fund] == 1, "the foreign «Маркетинг» stayed under its own direction"
    assert len(rows) == 2
    new_id = next(i for i in rows if i != m_fund)
    assert rows[new_id] == 3 and bound == new_id


def test_chat_map_skips_an_ambiguous_row_without_parent(db: str, tmp_path: Path,
                                                        monkeypatch):
    """Without `parent` an ambiguous name is assigned to nobody - neither in
    dry-run nor in a real pass (otherwise the chat would lose its direction)."""
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: {"mode": "point", "count": len(ids), "stale": 0})
    _two_markets(db)
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                     "category_id) VALUES (-500, 'Маркетинг чат', 'group', 1, 1)")
        conn.commit()

    path = _mapping(tmp_path, [{"chat_id": -500, "title": "Маркетинг чат",
                                "category": "Маркетинг"}])
    both = ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]
    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert preview["ambiguous"] == [{"category": "Маркетинг", "candidates": both}]
    note = preview["per_chat"][0]["note"]
    assert "specify parent" in note and all(p in note for p in both)

    res = scope.apply_chat_categories(db, mapping_path=path)
    assert res["ambiguous"] == [{"category": "Маркетинг", "candidates": both}]
    assert res["chats_bound"] == 0
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT category_id FROM tg_monitored_chats WHERE chat_id = -500"
        ).fetchone()["category_id"] == 1, "the chat's direction must not have disappeared"


def test_chat_map_binds_two_rows_of_one_name_to_two_parents(
        db: str, tmp_path: Path, monkeypatch):
    """A two-row case of the same thing that the test above proves: a map row
    resolves by the PAIR `(parent, category)`, not by a bare name.

    A cache by bare name would merge both rows into one node and bind both chats
    to it - the second one's history would silently go to a foreign direction, and
    `--dry-run` would show one creation instead of two."""
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: {"mode": "point", "count": len(ids), "stale": 0})
    from app.services import categories
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (3, 'Фріланс', 'фріланс', 3)")
        for tid, chat in ((20, -401), (21, -402)):
            conn.execute(
                "INSERT INTO transcriptions (id, source_type, source_name, tg_chat_id, "
                "tg_chat_title, category_id, created_at) VALUES (?, 'telegram', "
                "'[TG] Маркетинг', ?, 'Маркетинг', NULL, '2026-05-14 10:00:00')",
                (tid, chat))
        conn.commit()

    path = _mapping(tmp_path, [
        {"chat_id": -401, "title": "Маркетинг Фонду", "category": "Маркетинг",
         "parent": "Фонд Робота"},
        {"chat_id": -402, "title": "Маркетинг Фрілансу", "category": "Маркетинг",
         "parent": "Фріланс"},
    ])

    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert preview["ambiguous"] == []
    assert [c["path"] for c in preview["categories_created"]] == [
        "Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]
    with get_db_connection(db) as conn:      # dry-run writes nothing
        assert conn.execute("SELECT COUNT(*) FROM categories WHERE name_norm = 'маркетинг'"
                            ).fetchone()[0] == 0

    res = scope.apply_chat_categories(db, mapping_path=path)
    assert res["ambiguous"] == []

    with get_db_connection(db) as conn:
        nodes = [r["id"] for r in conn.execute(
            "SELECT id FROM categories WHERE name_norm = 'маркетинг'")]
        assert len(nodes) == 2, "two parents - two nodes"
        assert {categories.node_path(conn, i) for i in nodes} == {
            "Фонд Робота → Маркетинг", "Фріланс → Маркетинг"}
        bound = {c: conn.execute(
            "SELECT category_id FROM tg_monitored_chats WHERE chat_id = ?",
            (c,)).fetchone()["category_id"] for c in (-401, -402)}
        assert categories.node_path(conn, bound[-401]) == "Фонд Робота → Маркетинг"
        assert categories.node_path(conn, bound[-402]) == "Фріланс → Маркетинг"
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 20"
                            ).fetchone()[0] == bound[-401]
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 21"
                            ).fetchone()[0] == bound[-402]

    again = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert again["categories_created"] == [], "everything is already created"


def test_chat_map_plan_names_the_namesake_it_is_about_to_fork(
        db: str, tmp_path: Path, monkeypatch):
    """The preview must name the node that will fork the taxonomy.

    Earlier `categories_created` was computed by checking the name against the WHOLE table
    "before", so a same-named child under another parent did not get into the plan at all:
    `--dry-run` printed `[]`, while the real pass created a second «Акмекорп».
    """
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: {"mode": "point", "count": len(ids), "stale": 0})
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (4, 'Акмекорп', 'акмекорп', 4)")
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, category_id, "
            "created_at) VALUES (20, 'file', 'Планерка', 4, '2026-05-14 10:00:00')")
        conn.execute(
            "INSERT INTO transcriptions (id, source_type, source_name, tg_chat_id, "
            "tg_chat_title, category_id, created_at) VALUES (21, 'telegram', "
            "'[TG] Акмекорп чат', -403, 'Акмекорп чат', NULL, '2026-05-14 10:00:00')")
        conn.commit()

    path = _mapping(tmp_path, [{"chat_id": -403, "title": "Акмекорп чат",
                                "category": "Акмекорп", "parent": "Фонд Робота"}])

    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert preview["categories_created"] == [
        {"path": "Фонд Робота → Акмекорп", "namesake": ["Акмекорп"],
         "note": "namesake exists: «Акмекорп»"}]
    assert preview["per_chat"][0]["note"] == "target: «Фонд Робота → Акмекорп»"

    scope.apply_chat_categories(db, mapping_path=path)
    with get_db_connection(db) as conn:
        new = conn.execute("SELECT id, parent_id FROM categories "
                           "WHERE name_norm = 'акмекорп' AND parent_id IS NOT NULL"
                           ).fetchone()
        assert new["parent_id"] == 1
        assert conn.execute("SELECT category_id FROM tg_monitored_chats WHERE chat_id = -403"
                            ).fetchone()["category_id"] == new["id"]
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 21"
                            ).fetchone()[0] == new["id"]
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 20"
                            ).fetchone()[0] == 4, "records of the old direction are not touched"


# ============================================================
# Story 26: dry-run = the same write path with a rollback
# ============================================================

def _snapshot(db: str) -> dict:
    """Everything the chat map writes: nodes, chat bindings, record directions, tags."""
    with get_db_connection(db) as conn:
        q = lambda sql: [tuple(r) for r in conn.execute(sql).fetchall()]  # noqa: E731
        return {
            "categories": q("SELECT id, name, name_norm, slug, parent_id FROM categories "
                            "ORDER BY id"),
            "chats": q("SELECT chat_id, title, enabled, category_id FROM tg_monitored_chats "
                       "ORDER BY chat_id"),
            "records": q("SELECT id, category_id FROM transcriptions ORDER BY id"),
            "record_tags": q("SELECT transcription_id, tag_id, source FROM record_tags "
                             "ORDER BY 1, 2"),
            "tags": q("SELECT id, name FROM tags ORDER BY id"),
            "chat_tags": q("SELECT chat_id, tag_id FROM tg_chat_tags ORDER BY 1, 2"),
        }


def _by_path(db: str) -> dict:
    """Pass result independent of ids: paths of nodes where chats and records went."""
    from app.services import categories
    with get_db_connection(db) as conn:
        path = lambda cid: categories.node_path(conn, cid) if cid else None  # noqa: E731
        return {
            "nodes": sorted(categories.node_path(conn, r[0])
                            for r in conn.execute("SELECT id FROM categories")),
            "chats": {r[0]: path(r[1]) for r in conn.execute(
                "SELECT chat_id, category_id FROM tg_monitored_chats")},
            "records": {r[0]: path(r[1]) for r in conn.execute(
                "SELECT id, category_id FROM transcriptions")},
        }


def _copy_db(src: str, dst: str) -> str:
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)
    return dst


def _no_reembed(stats: dict) -> dict:
    return {k: v for k, v in stats.items() if k != "reembed_scheduled"}


def _add_tg_records(db: str, rows: list[tuple[int, int]]) -> None:
    with get_db_connection(db) as conn:
        for tid, chat in rows:
            conn.execute(
                "INSERT INTO transcriptions (id, source_type, source_name, tg_chat_id, "
                "tg_chat_title, category_id, created_at) VALUES (?, 'telegram', "
                "'[TG] Маркетинг', ?, 'Маркетинг', NULL, '2026-05-14 10:00:00')",
                (tid, chat))
        conn.commit()


def test_chat_map_dry_run_is_the_real_pass_rolled_back_in_any_row_order(
        db: str, tmp_path: Path, monkeypatch):
    """A bare «Маркетинг» after/before `{Маркетинг, parent: Фонд Робота}`.

    Earlier dry-run re-derived the plan in a separate branch, while the real pass mutated
    the table as it went: the bare row created the direction «Маркетинг» first, the second one
    saw the fresh child of «Фонд». Now the resolve is deterministic (rows with
    parent first), and the preview is the same pass with `rollback()`."""
    calls = []
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: calls.append(sorted(ids)) or
                        {"mode": "point", "count": len(ids), "stale": 0})
    _add_tg_records(db, [(60, -601), (61, -602)])
    db_b = _copy_db(db, str(tmp_path / "scope_b.db"))
    row_fund = {"chat_id": -601, "title": "Маркетинг Фонду", "category": "Маркетинг",
                "parent": "Фонд Робота", "tags": ["Реклама"]}
    row_bare = {"chat_id": -602, "title": "Маркетинг голий", "category": "Маркетинг"}

    results = {}
    for label, path_db, rows in (("a", db, [row_fund, row_bare]),
                                 ("b", db_b, [row_bare, row_fund])):
        (tmp_path / label).mkdir()
        path = _mapping(tmp_path / label, rows)
        before = _snapshot(path_db)
        preview = scope.apply_chat_categories(path_db, mapping_path=path, dry_run=True)
        assert _snapshot(path_db) == before, f"({label}) dry-run left a trace in the DB"
        assert calls == [], "dry-run does not schedule re-embed"

        res = scope.apply_chat_categories(path_db, mapping_path=path)
        assert _no_reembed(preview) == _no_reembed(res), f"({label}) plan != fact"
        assert preview["reembed_scheduled"] == 0 and res["reembed_scheduled"] > 0
        assert res["ambiguous"] == []
        assert [c["path"] for c in res["categories_created"]] == [
            "Фонд Робота → Маркетинг"]
        notes = {r["title"]: r["note"] for r in res["per_chat"]}
        assert notes == {"Маркетинг Фонду": "target: «Фонд Робота → Маркетинг»",
                         "Маркетинг голий": "target: «Фонд Робота → Маркетинг»"}
        results[label] = _by_path(path_db)

        # The second real pass - zero changes.
        calls.clear()
        again = scope.apply_chat_categories(path_db, mapping_path=path)
        assert again["categories_created"] == []
        assert (again["chats_bound"], again["chats_inserted"],
                again["records_recategorized"], again["tags_applied"],
                again["reembed_scheduled"]) == (0, 0, 0, 0, 0)
        assert calls == []

    assert results["a"] == results["b"], "the result depends on the row order"
    assert results["a"]["chats"][-601] == results["a"]["chats"][-602] == \
        "Фонд Робота → Маркетинг"
    assert results["a"]["records"][60] == results["a"]["records"][61] == \
        "Фонд Робота → Маркетинг"


@pytest.mark.parametrize("parent_first", [True, False])
def test_chat_map_ambiguity_is_keyed_by_the_row_pair(db: str, tmp_path: Path,
                                                     monkeypatch, parent_first):
    """A row with `parent` that resolved cleanly by the pair is not rejected because of
    a bare namesake (earlier `ambiguous` was keyed by the bare name and rejected both)."""
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: {"mode": "point", "count": len(ids), "stale": 0})
    nodes = _two_markets(db)
    _add_tg_records(db, [(62, -603)])
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                     "category_id) VALUES (-604, 'Маркетинг голий', 'group', 1, 1)")
        conn.commit()
    row_free = {"chat_id": -603, "title": "Маркетинг Фрілансу", "category": "Маркетинг",
                "parent": "Фріланс"}
    row_bare = {"chat_id": -604, "title": "Маркетинг голий", "category": "Маркетинг"}
    path = _mapping(tmp_path, [row_free, row_bare] if parent_first else [row_bare, row_free])
    both = ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]

    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    res = scope.apply_chat_categories(db, mapping_path=path)
    assert _no_reembed(preview) == _no_reembed(res)
    assert res["ambiguous"] == [{"category": "Маркетинг", "candidates": both}]
    notes = {r["title"]: r for r in res["per_chat"]}
    assert notes["Маркетинг Фрілансу"]["note"] == "target: «Фріланс → Маркетинг»"
    assert notes["Маркетинг Фрілансу"]["records_to_move"] == 1
    assert notes["Маркетинг голий"]["note"].startswith("ambiguous, specify parent: ")
    assert notes["Маркетинг голий"]["records_to_move"] == 0
    assert res["categories_created"] == []

    with get_db_connection(db) as conn:
        bound = {r[0]: r[1] for r in conn.execute(
            "SELECT chat_id, category_id FROM tg_monitored_chats WHERE chat_id IN (-603, -604)")}
        assert bound[-603] == nodes["m_free"]
        assert bound[-604] == 1, "the ambiguous chat stayed where it was"
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 62"
                            ).fetchone()[0] == nodes["m_free"]


def test_chat_map_namesake_note_is_the_same_in_dry_run_and_real(
        db: str, tmp_path: Path, monkeypatch):
    """A second «Маркетинг» in the same pass is a namesake of the first. A separate plan branch
    computed `namesake` from the "before" table and printed `[]`; the real pass
    already saw the created first one."""
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: {"mode": "point", "count": len(ids), "stale": 0})
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (3, 'Фріланс', 'фріланс', 3)")
        conn.commit()
    path = _mapping(tmp_path, [
        {"chat_id": -605, "title": "Маркетинг Фонду", "category": "Маркетинг",
         "parent": "Фонд Робота"},
        {"chat_id": -606, "title": "Маркетинг Фрілансу", "category": "Маркетинг",
         "parent": "Фріланс"},
    ])
    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    res = scope.apply_chat_categories(db, mapping_path=path)
    expected = [
        {"path": "Фонд Робота → Маркетинг", "namesake": [], "note": None},
        {"path": "Фріланс → Маркетинг", "namesake": ["Фонд Робота → Маркетинг"],
         "note": "namesake exists: «Фонд Робота → Маркетинг»"}]
    assert preview["categories_created"] == expected
    assert res["categories_created"] == expected


def test_map_claude_answer_takes_id_only_from_a_whole_integer(db: str):
    """A digit inside a name is not an id: «Проєкт 2026» stays a name.

    Earlier the first digit anywhere in the answer was taken (a regex search over the text),
    so the very first node with a number in its name silently broke the arbiter."""
    from app.services import categories
    _two_markets(db)
    with get_db_connection(db) as conn:
        proj = categories.create_category(conn, "Проєкт 2026", parent_id=1)["id"]
        bar = categories.create_category(conn, "Акмекорп", parent_id=1)["id"]
    cats = _paths(db)

    assert scope._map_claude_answer("7", [(7, "Сьомий"), (8, "Восьмий")]) == (7, "id", [])
    assert scope._map_claude_answer(str(bar), cats) == (bar, "id", [])
    assert scope._map_claude_answer("Проєкт 2026", cats)[0] == proj
    assert scope._map_claude_answer("Фонд Робота → Акмекорп", cats) == (bar, "path", [])

    cid, reason, paths = scope._map_claude_answer("Маркетинг", cats)
    assert cid is None and reason == "ambiguous"
    assert paths == ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]
