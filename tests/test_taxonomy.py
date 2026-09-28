"""Двошарова таксономія: дерево напрямків + теги (`app/services/categories.py`).

Спека `directions-projects-tags`, історія 01 (трасер). Зріз перевіряється
наскрізь: міграція v45 на тимчасовій БД → сервіс (фільтр/дерево/письменники/
теги) → HTTP через `test_client` окремого Flask-застосунку з `memory_bp`.
Бойовий `whisper_history.db` не відкривається, `schedule_records_reembed`
всюди замокано.

Фікстури кириличні — саме на кирилиці цей проєкт уже ловив баги порівняння
(памʼятка `plan-dictated-sql-ignored-own-memory`): дерево тут «Фонд Робота»
→ «Акмекорп»/«DOM», а не Foo → Bar.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories as cats
from app.utils.proc import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "taxonomy.db")
    init_database(path)
    return path


@pytest.fixture
def conn(db):
    with get_db_connection(db) as c:
        yield c


@pytest.fixture(autouse=True)
def no_real_reembed(monkeypatch):
    """Жоден тест цього файлу не ставить справжню задачу в чергу (сусідній
    `test_endpoints_smoke` привʼязує живий `JobQueue` до `app.state`)."""
    from app.services import reembed

    calls: list[dict] = []
    monkeypatch.setattr(
        reembed, "schedule_records_reembed",
        lambda ids, *, db_path, reason: calls.append(
            {"ids": list(ids), "db_path": db_path, "reason": reason})
        or {"mode": "point", "count": len(list(ids)), "stale": 0})
    return calls


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.register_blueprint(memory_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db
    return app.test_client()


def _tree(conn):
    """Кириличне дерево: «Фонд Робота» → «Акмекорп», «DOM» + сусідній напрямок.

    «Фонд Робота» є серед засівів v15, тож беремо наявний вузол — саме так
    дерево й зʼявиться в бойовій БД (розкладка наявних 29 імен, не створення
    з нуля)."""
    row = conn.execute("SELECT id FROM categories WHERE COALESCE(name_norm, name) = ?",
                       ("фонд робота",)).fetchone()
    fund = (cats.get_category(conn, row[0]) if row
            else cats.create_category(conn, "Фонд Робота"))
    bar = cats.create_category(conn, "Акмекорп", parent_id=fund["id"])
    dom = cats.create_category(conn, "DOM", parent_id=fund["id"])
    other = cats.create_category(conn, "Юридичне")
    return fund, bar, dom, other


def _add_tx(conn, category_id=None, name="Планерка", source_type="file",
            tg_chat_id=None, deleted_at=None):
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "category_id, tg_chat_id, deleted_at) VALUES (?, ?, ?, ?, ?, ?)",
        (source_type, name, "Обговорили кошторис Барселони", category_id,
         tg_chat_id, deleted_at))
    conn.commit()
    return cur.lastrowid


def _select(conn, value, column="category_id"):
    sql, params = cats.filter_sql(column, value)
    return [r[0] for r in conn.execute(
        f"SELECT id FROM transcriptions WHERE 1=1{sql} ORDER BY id", params).fetchall()]


# ============================================================
# Ключ нормалізації (схему v45 перевіряє tests/test_migrations.py)
# ============================================================

def test_norm_name_matches_legacy_key_of_existing_names(conn):
    """`name_norm` наявних напрямків НЕ перераховується — новий нормалізатор
    мусить давати той самий ключ, що старий `memory._norm_cat`."""
    def legacy(name):
        return " ".join((name or "").strip().split()).casefold()

    seeded = conn.execute("SELECT name, name_norm FROM categories").fetchall()
    assert seeded, "засіви напрямків мають бути в схемі"
    for name, name_norm in seeded:
        assert cats.norm_name(name) == name_norm == legacy(name)
    for name in ("  Фонд   Робота  ", "AI / професія", "Vector Рівне",
                 "Резиденція Промінвест"):
        assert cats.norm_name(name) == legacy(name)


# ============================================================
# Фільтр і розбір
# ============================================================

def test_parse_filter_reads_all_none_and_ids():
    assert cats.parse_filter(None) is None
    assert cats.parse_filter("") is None
    assert cats.parse_filter("all") is None
    assert cats.parse_filter("none") == "none"
    assert cats.parse_filter("7") == 7
    assert cats.parse_filter(7) == 7
    with pytest.raises(ValueError):
        cats.parse_filter("Фонд")


def test_parse_filter_gives_its_own_text_on_a_unicode_pseudo_digit():
    """«²» проходить `str.isdigit()`, але `int()` на ньому падає сирим
    «invalid literal for int()» — внутрішнім текстом у полі, яке читають
    людина в тості й агент у відповіді тулза."""
    for bad in ("²", "½"):
        with pytest.raises(ValueError) as exc:
            cats.parse_filter(bad)
        text = str(exc.value)
        assert "очікую id" in text, text
        assert "invalid literal" not in text, text


def test_filter_sql_expands_direction_to_its_projects(conn):
    fund, bar, dom, other = _tree(conn)
    t_fund = _add_tx(conn, fund["id"])
    t_bar = _add_tx(conn, bar["id"])
    t_dom = _add_tx(conn, dom["id"])
    t_other = _add_tx(conn, other["id"])
    t_none = _add_tx(conn, None)

    assert _select(conn, fund["id"]) == sorted([t_fund, t_bar, t_dom])
    assert _select(conn, bar["id"]) == [t_bar]
    assert _select(conn, "none") == [t_none]
    assert _select(conn, None) == sorted([t_fund, t_bar, t_dom, t_other, t_none])


def test_filter_sql_fragment_starts_with_and_and_carries_params():
    sql, params = cats.filter_sql("t.category_id", 5)
    assert sql.startswith(" AND ") and params == [5, 5]
    assert cats.filter_sql("t.category_id", None) == ("", [])
    assert cats.filter_sql("t.category_id", "none") == (" AND t.category_id IS NULL", [])


def test_subtree_ids_is_self_then_children(conn):
    fund, bar, dom, other = _tree(conn)
    assert cats.subtree_ids(conn, fund["id"]) == [fund["id"], bar["id"], dom["id"]]
    assert cats.subtree_ids(conn, bar["id"]) == [bar["id"]]
    assert cats.subtree_ids(conn, 99999) == []


def test_list_tree_orders_projects_under_their_direction_and_sums_items(conn):
    fund, bar, dom, other = _tree(conn)
    _add_tx(conn, fund["id"])
    _add_tx(conn, bar["id"])
    _add_tx(conn, bar["id"])
    _add_tx(conn, dom["id"])
    _add_tx(conn, dom["id"], deleted_at=1700000000)      # soft-deleted не рахуємо

    tree = cats.list_tree(conn)
    by_id = {n["id"]: n for n in tree}
    order = [n["id"] for n in tree]
    assert order.index(bar["id"]) == order.index(fund["id"]) + 1
    assert order.index(dom["id"]) == order.index(fund["id"]) + 2
    assert by_id[fund["id"]]["level"] == "direction"
    assert by_id[bar["id"]]["level"] == "project"
    assert by_id[bar["id"]]["parent_id"] == fund["id"]
    assert by_id[fund["id"]]["items"] == 1
    assert by_id[fund["id"]]["items_total"] == 1 + 2 + 1
    assert by_id[bar["id"]]["items_total"] == by_id[bar["id"]]["items"] == 2
    assert by_id[other["id"]]["items_total"] == 0


def test_parse_tags_and_tags_sql(conn):
    assert cats.parse_tags("AI, інвестори") == ["AI", "інвестори"]
    assert cats.parse_tags(" , ") == []
    assert cats.parse_tags(["Гроші", "гроші"]) == ["Гроші"]

    tid = _add_tx(conn)
    other = _add_tx(conn)
    cats.set_tags(conn, tid, add=["інвестори"])

    sql, params = cats.tags_sql("t.id", ["інвестори"])
    rows = [r[0] for r in conn.execute(
        f"SELECT id FROM transcriptions t WHERE 1=1{sql}", params).fetchall()]
    assert rows == [tid] and other not in rows
    # невідома назва звужує до порожнечі, а не ігнорується
    sql, params = cats.tags_sql("t.id", ["немаєтакого"])
    assert conn.execute(f"SELECT COUNT(*) FROM transcriptions t WHERE 1=1{sql}",
                        params).fetchone()[0] == 0
    assert cats.tags_sql("t.id", []) == ("", [])


def test_date_sql_is_inclusive_and_rejects_garbage():
    sql, params = cats.date_sql("t.created_at", "2026-09-01", "2026-09-30")
    assert sql == " AND date(t.created_at) >= ? AND date(t.created_at) <= ?"
    assert params == ["2026-09-01", "2026-09-30"]
    assert cats.date_sql("t.created_at") == ("", [])
    with pytest.raises(ValueError):
        cats.date_sql("t.created_at", since="за тиждень")


# ============================================================
# Письменники дерева
# ============================================================

def test_create_rejects_project_as_parent_and_casefold_duplicate(conn):
    fund, bar, dom, other = _tree(conn)
    with pytest.raises(cats.CategoryError) as exc:
        cats.create_category(conn, "Підпроєкт", parent_id=bar["id"])
    assert exc.value.code == "two_levels"

    with pytest.raises(cats.CategoryError) as exc:
        cats.create_category(conn, "фонд  робота")
    assert exc.value.code == "duplicate"


def test_update_category_tree_rules(conn):
    fund, bar, dom, other = _tree(conn)

    with pytest.raises(cats.CategoryError) as exc:
        cats.update_category(conn, fund["id"], parent_id=fund["id"])
    assert exc.value.code == "self_parent"

    with pytest.raises(cats.CategoryError) as exc:
        cats.update_category(conn, other["id"], parent_id=bar["id"])
    assert exc.value.code == "two_levels"

    # напрямок із проєктами не може сам стати проєктом
    with pytest.raises(cats.CategoryError) as exc:
        cats.update_category(conn, fund["id"], parent_id=other["id"])
    assert exc.value.code == "two_levels"

    # проєкт → напрямок
    node, _ = cats.update_category(conn, dom["id"], parent_id=None)
    assert node["parent_id"] is None and node["level"] == "direction"


def test_rename_and_reparent_return_whole_subtree_ids(conn):
    fund, bar, dom, other = _tree(conn)
    t_fund = _add_tx(conn, fund["id"])
    t_bar = _add_tx(conn, bar["id"])
    _add_tx(conn, other["id"])

    _, ids = cats.update_category(conn, fund["id"], name="Фонд Робота 2.0")
    assert ids == sorted([t_fund, t_bar])

    # колір сам по собі префікса не змінює
    _, ids = cats.update_category(conn, fund["id"], color="#123456")
    assert ids == []

    _, ids = cats.update_category(conn, bar["id"], parent_id=other["id"])
    assert ids == [t_bar]


def test_update_rejects_casefold_duplicate_name(conn):
    fund, bar, dom, other = _tree(conn)
    with pytest.raises(cats.CategoryError) as exc:
        cats.update_category(conn, other["id"], name="фонд  робота")
    assert exc.value.code == "duplicate"


def test_delete_and_merge_refuse_node_with_projects(conn):
    fund, bar, dom, other = _tree(conn)
    for call in (lambda: cats.delete_category(conn, fund["id"]),
                 lambda: cats.merge_categories(conn, other["id"], fund["id"])):
        with pytest.raises(cats.CategoryError) as exc:
            call()
        assert exc.value.code == "has_projects"
        assert {p["name"] for p in exc.value.projects} == {"Акмекорп", "DOM"}


def test_delete_nulls_refs_in_all_four_tables(conn):
    fund, bar, dom, other = _tree(conn)
    tid = _add_tx(conn, fund["id"])
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, "
                 "category_id) VALUES ('u', 'vid1', 'Запис', 'f.mp3', ?)", (fund["id"],))
    conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, category_id) "
                 "VALUES (777, 'Чат фонду', ?)", (fund["id"],))
    conn.execute("INSERT INTO copilot_sessions (recording_session_id, category_id) "
                 "VALUES ('sess-1', ?)", (fund["id"],))
    conn.commit()

    # спершу переносимо проєкти — інакше 409 (has_projects)
    cats.update_category(conn, bar["id"], parent_id=other["id"])
    cats.update_category(conn, dom["id"], parent_id=other["id"])
    cats.delete_category(conn, fund["id"])

    assert conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                        (tid,)).fetchone()[0] is None
    for table in ("audio_downloads", "tg_monitored_chats", "copilot_sessions"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE category_id IS NOT NULL"
                            ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transcriptions WHERE id = ?",
                        (tid,)).fetchone()[0] == 1


def test_merge_moves_refs_in_all_four_tables(conn):
    fund, bar, dom, other = _tree(conn)
    tid = _add_tx(conn, bar["id"])
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, "
                 "category_id) VALUES ('u', 'vid1', 'Запис', 'f.mp3', ?)", (bar["id"],))
    conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, category_id) "
                 "VALUES (777, 'Чат', ?)", (bar["id"],))
    conn.execute("INSERT INTO copilot_sessions (recording_session_id, category_id) "
                 "VALUES ('sess-1', ?)", (bar["id"],))
    conn.commit()

    moved = cats.merge_categories(conn, other["id"], bar["id"])
    assert moved == [tid]
    assert conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                        (tid,)).fetchone()[0] == other["id"]
    for table in ("audio_downloads", "tg_monitored_chats", "copilot_sessions"):
        assert conn.execute(f"SELECT category_id FROM {table}").fetchone()[0] == other["id"]


def test_set_category_returns_only_really_changed_ids(conn):
    fund, bar, dom, other = _tree(conn)
    a = _add_tx(conn, fund["id"])
    b = _add_tx(conn, None)
    assert cats.set_category(conn, [a, b], fund["id"]) == [b]
    assert cats.set_category(conn, [a, b], fund["id"]) == []
    assert cats.set_category(conn, [a], None) == [a]
    assert cats.set_category(conn, [], fund["id"]) == []


# ============================================================
# Теги
# ============================================================

def test_set_tags_creates_missing_tags_and_reports_change(conn):
    tid = _add_tx(conn)
    assert cats.set_tags(conn, tid, add=["інвестори", "гроші"]) is True
    assert cats.set_tags(conn, tid, add=["інвестори"]) is False          # без змін
    assert [t["name"] for t in cats.tags_for(conn, [tid])[tid]] == ["гроші", "інвестори"]
    assert cats.set_tags(conn, tid, remove=["Гроші"]) is True            # регістр не важить
    assert [t["name"] for t in cats.tags_for(conn, [tid])[tid]] == ["інвестори"]
    assert cats.set_tags(conn, tid, remove=["немаєтакого"]) is False


def test_ensure_and_list_tags_are_idempotent_by_norm(conn):
    first = cats.ensure_tag(conn, "Інвестори", color="#ff0000")
    again = cats.ensure_tag(conn, "  інвестори  ")
    assert again["id"] == first["id"] and again["name"] == "Інвестори"
    tid = _add_tx(conn)
    cats.set_tags(conn, tid, add=["Інвестори"])
    assert cats.list_tags(conn) == [{"id": first["id"], "name": "Інвестори",
                                     "color": "#ff0000", "items": 1}]


def test_apply_chat_tags_backfills_history_and_unbinds_only_chat_source(conn):
    chat_id = 777
    a = _add_tx(conn, source_type="telegram", tg_chat_id=chat_id)
    b = _add_tx(conn, source_type="telegram", tg_chat_id=chat_id)
    outside = _add_tx(conn, source_type="telegram", tg_chat_id=888)
    deleted = _add_tx(conn, source_type="telegram", tg_chat_id=chat_id,
                      deleted_at="2026-01-01T00:00:00")
    cats.set_tags(conn, a, add=["інвестори"], source="manual")   # ручний — недоторканний

    changed = cats.apply_chat_tags(conn, chat_id, ["інвестори", "Фонд"])
    assert changed == [a, b]                       # a отримав «Фонд», b — обидва
    assert deleted not in changed                  # видалений запис — не живий
    assert cats.tags_for(conn, [deleted])[deleted] == []
    assert cats.tags_for(conn, [outside])[outside] == []

    changed = cats.apply_chat_tags(conn, chat_id, [])
    assert changed == [a, b]                       # обидва втратили «Фонд»
    # …але ручний «інвестори» на `a` відвʼязування чату НЕ знімає
    assert [t["name"] for t in cats.tags_for(conn, [a])[a]] == ["інвестори"]
    assert conn.execute("SELECT source FROM record_tags WHERE transcription_id = ?",
                        (a,)).fetchone()[0] == "manual"
    assert cats.tags_for(conn, [b])[b] == []
    assert cats.chat_tags(conn, chat_id) == []


def test_inherit_chat_tags_copies_chat_set_to_new_record(conn):
    chat_id = 777
    cats.apply_chat_tags(conn, chat_id, ["Фонд"])
    fresh = _add_tx(conn, source_type="telegram", tg_chat_id=chat_id)
    cats.inherit_chat_tags(conn, fresh, chat_id)
    assert [t["name"] for t in cats.tags_for(conn, [fresh])[fresh]] == ["Фонд"]
    assert conn.execute("SELECT source FROM record_tags WHERE transcription_id = ?",
                        (fresh,)).fetchone()[0] == "chat"


def test_resolve_suggestions_accepts_into_record_tags_and_drops_from_json(conn):
    tid = _add_tx(conn)
    cats.set_suggested_tags(conn, tid, ["інвестори", "кошторис", "сміття"])
    assert cats.suggested_tags(conn, tid) == ["інвестори", "кошторис", "сміття"]

    assert cats.resolve_suggestions(conn, tid, accept=["інвестори"],
                                    dismiss=["сміття"]) is True
    assert [t["name"] for t in cats.tags_for(conn, [tid])[tid]] == ["інвестори"]
    assert conn.execute("SELECT source FROM record_tags WHERE transcription_id = ?",
                        (tid,)).fetchone()[0] == "claude"
    assert cats.suggested_tags(conn, tid) == ["кошторис"]
    assert cats.resolve_suggestions(conn, tid) is False


def test_delete_and_merge_tags_return_touched_records(conn):
    a, b, c = _add_tx(conn), _add_tx(conn), _add_tx(conn)
    cats.set_tags(conn, a, add=["інвестори"])
    cats.set_tags(conn, b, add=["Інвестор"])
    cats.set_tags(conn, c, add=["інвестори", "Інвестор"])   # мав обидва тег
    keep = cats.ensure_tag(conn, "інвестори")
    dup = cats.ensure_tag(conn, "Інвестор")

    # `c` теж повертається: його набір тегів звузився з двох імен до одного.
    assert cats.merge_tags(conn, keep["id"], dup["id"]) == [b, c]
    assert [t["name"] for t in cats.tags_for(conn, [b])[b]] == ["інвестори"]
    assert [t["name"] for t in cats.tags_for(conn, [c])[c]] == ["інвестори"]

    assert cats.delete_tag(conn, keep["id"]) == [a, b, c]
    assert cats.list_tags(conn) == []


def test_update_tag_returns_tuple_and_reembeds_only_live_records_on_rename(conn):
    live1, live2 = _add_tx(conn), _add_tx(conn)
    deleted = _add_tx(conn, deleted_at="2026-01-01T00:00:00")
    tag = cats.ensure_tag(conn, "AI")
    for tid in (live1, live2, deleted):
        cats.set_tags(conn, tid, add=["AI"])

    renamed, ids = cats.update_tag(conn, tag["id"], name="ШІ")
    assert renamed["name"] == "ШІ"
    assert sorted(ids) == sorted([live1, live2])

    unchanged, ids2 = cats.update_tag(conn, tag["id"], color="#112233")
    assert ids2 == []


def test_update_tag_same_exact_name_does_not_reembed_but_recase_does(conn):
    tid = _add_tx(conn)
    tag = cats.ensure_tag(conn, "ai")
    cats.set_tags(conn, tid, add=["ai"])

    same, ids = cats.update_tag(conn, tag["id"], name="ai")
    assert same["name"] == "ai"
    assert ids == []

    recased, ids2 = cats.update_tag(conn, tag["id"], name="AI")
    assert recased["name"] == "AI"
    assert ids2 == [tid]


def test_update_tag_rejects_bad_name_via_shared_guard(conn):
    tag = cats.ensure_tag(conn, "AI")
    with pytest.raises(cats.CategoryError) as exc:
        cats.update_tag(conn, tag["id"], name="а" * 41)
    assert exc.value.code == "bad_name"


# ============================================================
# HTTP (контракт C4, частина «Категорії»)
# ============================================================

def test_http_create_project_and_reject_project_as_parent(client, db):
    fund = client.post("/api/memory/categories", json={"name": "Фонд Робота 2"})
    fund_id = fund.get_json()["id"]
    proj = client.post("/api/memory/categories",
                       json={"name": "Акмекорп", "parent_id": fund_id})
    assert proj.status_code == 200
    assert proj.get_json()["level"] == "project"
    assert proj.get_json()["parent_id"] == fund_id

    deep = client.post("/api/memory/categories",
                       json={"name": "Підпроєкт", "parent_id": proj.get_json()["id"]})
    assert deep.status_code == 400
    assert deep.get_json()["code"] == "two_levels"

    # неіснуючий батько — теж криве тіло запиту, а не 404 вузла з URL
    orphan = client.post("/api/memory/categories",
                         json={"name": "Сирота", "parent_id": 99999})
    assert orphan.status_code == 400 and orphan.get_json()["code"] == "bad_parent"


def test_http_get_returns_tree_with_level_and_totals(client, db):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
        _add_tx(conn, bar["id"])

    data = client.get("/api/memory/categories").get_json()
    by_id = {c["id"]: c for c in data["categories"]}
    order = [c["id"] for c in data["categories"]]
    assert order.index(bar["id"]) == order.index(fund["id"]) + 1
    assert by_id[bar["id"]]["level"] == "project"
    assert by_id[fund["id"]]["items_total"] == 1
    assert by_id[bar["id"]]["count"] == 1          # сумісність із чинним SPA


def test_http_patch_parent_id_reports_reembed(client, db, no_real_reembed):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
        tid = _add_tx(conn, bar["id"])

    r = client.patch(f"/api/memory/categories/{bar['id']}",
                     json={"parent_id": other["id"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["category"]["parent_id"] == other["id"]
    assert body["reembed"]["count"] == 1
    assert no_real_reembed[-1]["ids"] == [tid]
    assert no_real_reembed[-1]["db_path"] == db

    up = client.patch(f"/api/memory/categories/{bar['id']}", json={"parent_id": None})
    assert up.status_code == 200 and up.get_json()["category"]["level"] == "direction"


def test_http_patch_rejects_self_parent_and_missing_node(client, db):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
    same = client.patch(f"/api/memory/categories/{fund['id']}",
                        json={"parent_id": fund["id"]})
    assert same.status_code == 400 and same.get_json()["code"] == "self_parent"
    missing = client.patch("/api/memory/categories/99999", json={"name": "Нове"})
    assert missing.status_code == 404 and missing.get_json()["code"] == "not_found"


def test_http_create_and_patch_reject_same_bad_parent_id_with_same_text(client, db):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
    # "²" — унікодна псевдоцифра: `str.isdigit()` каже True, `int()` падає
    # `ValueError` — раніше це був 500, а не 400 (Мінор 5).
    for bad in ("abc", -1, "²", True):
        post = client.post("/api/memory/categories",
                           json={"name": "X", "parent_id": bad})
        assert post.status_code == 400
        patch = client.patch(f"/api/memory/categories/{other['id']}",
                             json={"parent_id": bad})
        assert patch.status_code == 400
        assert post.get_json()["error"] == patch.get_json()["error"]

    null_post = client.post("/api/memory/categories", json={"name": "Y", "parent_id": None})
    assert null_post.status_code == 200 and null_post.get_json()["level"] == "direction"


def test_http_patch_category_id_and_bulk_category_reject_unicode_pseudo_digits(client, db):
    """`"²"`/`"½"` проходять `str.isdigit()` частково (лише перше), але
    `int()` — ні в обох: ловимо 400 замість 500/тихого скидання (Мінор 5/6)."""
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
        tid = _add_tx(conn, bar["id"])

    r = client.patch(f"/api/memory/transcriptions/{tid}/category",
                     json={"category_id": "²"})
    assert r.status_code == 400
    with get_db_connection(db) as conn:
        cid = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                           (tid,)).fetchone()[0]
    assert cid == bar["id"]      # без змін

    b = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": [tid], "category_id": "½"})
    assert b.status_code == 400
    with get_db_connection(db) as conn:
        cid2 = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                            (tid,)).fetchone()[0]
    assert cid2 == bar["id"]     # bulk-category не обнулила напрямок мовчки


def test_http_bulk_category_rejects_string_ids_instead_of_list(client, db):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
        tid = _add_tx(conn, bar["id"])

    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": "123", "category_id": fund["id"]})
    assert r.status_code == 400
    with get_db_connection(db) as conn:
        cid = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                           (tid,)).fetchone()[0]
    assert cid == bar["id"]      # рядок не розсипався на цифри [1, 2, 3]


def test_http_bulk_category_null_still_clears_direction(client, db, no_real_reembed):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
        tid = _add_tx(conn, bar["id"])

    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": [tid], "category_id": None})
    assert r.status_code == 200
    assert r.get_json()["updated"] == 1
    with get_db_connection(db) as conn:
        cid = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                           (tid,)).fetchone()[0]
    assert cid is None


def test_http_bulk_category_rejects_non_integer_ids_before_touching_db(client, db):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
        tid = _add_tx(conn, None)

    r = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": ["a"], "category_id": fund["id"]})
    assert r.status_code == 400
    with get_db_connection(db) as conn:
        cid = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                           (tid,)).fetchone()[0]
    assert cid is None


def test_http_delete_and_merge_return_409_with_projects(client, db):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)

    r = client.delete(f"/api/memory/categories/{fund['id']}")
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "has_projects"
    assert {p["name"] for p in body["projects"]} == {"Акмекорп", "DOM"}

    m = client.post(f"/api/memory/categories/{fund['id']}/merge",
                    json={"target_id": other["id"], "delete_source": True})
    assert m.status_code == 409 and m.get_json()["error"] == "has_projects"


def test_http_writers_schedule_reembed_with_config_db_path(client, db, no_real_reembed):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
        one = _add_tx(conn, None)
        two = _add_tx(conn, None)
        three = _add_tx(conn, other["id"])

    r = client.patch(f"/api/memory/transcriptions/{one}/category",
                     json={"category_id": bar["id"]})
    assert r.status_code == 200
    assert no_real_reembed[-1] == {"ids": [one], "db_path": db,
                                   "reason": "category_changed"}
    assert r.get_json()["reembed"]["count"] == 1

    # повторне призначення того самого напрямку re-embed не ставить
    client.patch(f"/api/memory/transcriptions/{one}/category",
                 json={"category_id": bar["id"]})
    assert no_real_reembed[-1]["ids"] == [one]

    b = client.post("/api/memory/transcriptions/bulk-category",
                    json={"ids": [one, two], "category_id": dom["id"]})
    assert b.get_json()["updated"] == 2
    assert no_real_reembed[-1] == {"ids": [one, two], "db_path": db,
                                   "reason": "category_bulk"}

    mg = client.post(f"/api/memory/categories/{other['id']}/merge",
                     json={"target_id": dom["id"], "delete_source": True})
    assert mg.status_code == 200 and mg.get_json()["moved"] == 1
    assert no_real_reembed[-1] == {"ids": [three], "db_path": db,
                                   "reason": "category_merged"}


def test_http_transcription_category_404_on_unknown_record(client, db):
    with get_db_connection(db) as conn:
        fund, bar, dom, other = _tree(conn)
    r = client.patch("/api/memory/transcriptions/99999/category",
                     json={"category_id": fund["id"]})
    assert r.status_code == 404


# ============================================================
# Межа модуля
# ============================================================

def test_importing_categories_does_not_pull_torch():
    """`categories` імпортує stdio-MCP; torch на рівні модуля вішає сервер
    назавжди (памʼятка `mcp-stdio-no-heavy-models`)."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, app.services.categories; "
         "sys.exit(1 if 'torch' in sys.modules else 0)"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
        creationflags=NO_WINDOW, timeout=60,
    )
    assert proc.returncode == 0, (
        f"app.services.categories затягнув torch у sys.modules підпроцесу "
        f"(stdout={proc.stdout!r} stderr={proc.stderr!r})")


def test_suggested_tags_json_survives_roundtrip_with_cyrillic(conn):
    """JSON підказок має лишатись читабельним (ensure_ascii=False) — його
    дивляться очима в БД під час розбору проблем."""
    tid = _add_tx(conn)
    cats.set_suggested_tags(conn, tid, ["інвестори", "кошторис"])
    raw = conn.execute("SELECT suggested_tags_json FROM transcriptions WHERE id = ?",
                       (tid,)).fetchone()[0]
    assert "інвестори" in raw and json.loads(raw) == ["інвестори", "кошторис"]


# ============================================================
# v47: однойменні вузли в різних напрямках (історія 20)
# ============================================================

def _pair_of_markets(conn):
    """«Маркетинг» під «Фонд Робота» і «Маркетинг» під «Фріланс».

    Обидва напрямки засіяні міграцією v15 — саме так виглядає жива БД."""
    fund = cats.get_category(conn, conn.execute(
        "SELECT id FROM categories WHERE name_norm = 'фонд робота'").fetchone()[0])
    free = cats.get_category(conn, conn.execute(
        "SELECT id FROM categories WHERE name_norm = 'фріланс'").fetchone()[0])
    m_fund = cats.create_category(conn, "Маркетинг", parent_id=fund["id"])
    m_free = cats.create_category(conn, "Маркетинг", parent_id=free["id"])
    return fund, free, m_fund, m_free


def test_same_project_name_lives_under_two_directions(conn):
    """Головне, заради чого v47: два «Маркетинги» в різних напрямках
    співіснують і мають різні непорожні `slug`."""
    fund, free, m_fund, m_free = _pair_of_markets(conn)
    assert m_fund["id"] != m_free["id"]
    assert m_fund["parent_id"] == fund["id"] and m_free["parent_id"] == free["id"]
    assert m_fund["slug"] and m_free["slug"] and m_fund["slug"] != m_free["slug"]


def test_duplicate_among_siblings_names_the_direction(conn):
    """Третій «Маркетинг» у тому ж напрямку — `duplicate`, і текст каже, ДЕ
    зайнято (інакше 409 не відрізнити від 409 сусіднього напрямку)."""
    fund, free, m_fund, _ = _pair_of_markets(conn)
    with pytest.raises(cats.CategoryError) as exc:
        cats.create_category(conn, "МАРКЕТИНГ", parent_id=fund["id"])
    assert exc.value.code == "duplicate"
    assert "Фонд Робота" in str(exc.value)
    assert exc.value.conflict == {"id": m_fund["id"], "path": "Фонд Робота → Маркетинг"}


def test_duplicate_direction_still_says_direction(conn):
    _pair_of_markets(conn)
    with pytest.raises(cats.CategoryError) as exc:
        cats.create_category(conn, "фонд  робота")
    assert exc.value.code == "duplicate"
    assert "уже існує" in str(exc.value)
    assert exc.value.conflict["path"] == "Фонд Робота"


def test_direction_may_share_name_with_a_foreign_project(conn):
    """Напрямок «Маркетинг» поруч із проєктом «Маркетинг» чужого напрямку —
    дозволено: брати — це лише напрямки між собою."""
    _pair_of_markets(conn)
    node = cats.create_category(conn, "Маркетинг")
    assert node["parent_id"] is None and node["level"] == "direction"
    assert node["slug"]


def test_reparent_into_taken_name_and_rename_to_sibling_are_duplicates(conn):
    fund, free, m_fund, m_free = _pair_of_markets(conn)
    bar = cats.create_category(conn, "Акмекорп", parent_id=free["id"])

    # переїзд «Маркетинга» з Фрілансу під Фонд, де такий проєкт уже є
    with pytest.raises(cats.CategoryError) as exc:
        cats.update_category(conn, m_free["id"], parent_id=fund["id"])
    assert exc.value.code == "duplicate" and "Фонд Робота" in str(exc.value)
    assert exc.value.conflict["id"] == m_fund["id"]

    # перейменування на назву брата
    with pytest.raises(cats.CategoryError) as exc:
        cats.update_category(conn, bar["id"], name="маркетинг")
    assert exc.value.code == "duplicate" and exc.value.conflict["id"] == m_free["id"]

    # а переїзд під напрямок, де такої назви немає, — дозволений
    moved, _ = cats.update_category(conn, bar["id"], parent_id=fund["id"])
    assert moved["parent_id"] == fund["id"]


def test_duplicate_is_caught_in_python_before_the_index(conn):
    """Перевірка серед братів спрацьовує ДО запису: клієнт бачить
    `CategoryError`, а не `IntegrityError` від `idx_categories_parent_name`,
    і зайвого рядка в таблиці не лишається."""
    fund, _, _, _ = _pair_of_markets(conn)
    before = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    with pytest.raises(cats.CategoryError):
        cats.create_category(conn, "  МАРКЕТИНГ  ", parent_id=fund["id"])
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == before


def test_resolve_node_takes_a_full_path_and_a_unique_bare_name(conn):
    fund, free, m_fund, m_free = _pair_of_markets(conn)
    bar = cats.create_category(conn, "Акмекорп", parent_id=fund["id"])
    assert cats.resolve_node(conn, "Фріланс → Маркетинг") == m_free["id"]
    assert cats.resolve_node(conn, "фонд робота→МАРКЕТИНГ") == m_fund["id"]
    assert cats.resolve_node(conn, "Акмекорп") == bar["id"]
    assert cats.resolve_node(conn, "Фонд Робота") == fund["id"]
    assert cats.resolve_node(conn, "Немає такого") is None
    assert cats.resolve_node(conn, "Фонд Робота → Немає") is None
    assert cats.resolve_node(conn, "") is None


def test_resolve_node_on_a_namesake_raises_ambiguous_with_paths(conn):
    """Ніколи «перший», ніколи тихе обʼєднання, ніколи порожньо: кілька
    збігів — це перелік шляхів, який клієнт може показати і передати назад."""
    _pair_of_markets(conn)
    with pytest.raises(cats.CategoryError) as exc:
        cats.resolve_node(conn, "МАРКЕТИНГ")
    assert exc.value.code == "ambiguous"
    assert exc.value.candidates == ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]
    assert "Фріланс → Маркетинг" in str(exc.value)


def test_resolve_node_prefers_the_exact_canonical_path_of_a_direction(conn):
    """Старшинство (хвиля 7): точний канонічний шлях — раніше за голу назву.

    Канонічний шлях напрямку — це його гола назва, тож «Маркетинг» при
    напрямку «Маркетинг» поруч із чужим проєктом «Фріланс → Маркетинг» дає
    напрямок. Без цього правила напрямок-тезка недосяжний текстом назавжди:
    його власний `path` повертався б тим самим `ambiguous`."""
    fund, free, m_fund, m_free = _pair_of_markets(conn)
    direction = cats.create_category(conn, "Маркетинг")
    assert cats.resolve_node(conn, "Маркетинг") == direction["id"]
    assert cats.resolve_node(conn, "  МАРКЕТИНГ ") == direction["id"]
    assert cats.resolve_node(conn, "Фріланс → Маркетинг") == m_free["id"]
    assert cats.resolve_node(conn, "Фонд Робота → Маркетинг") == m_fund["id"]
    assert cats.resolve_node(conn, "А → Б → В") is None, "третього рівня немає"


def test_every_ambiguous_candidate_resolves_back_to_its_own_node(conn):
    """Перелік, який сам себе повертає в `ambiguous`, — глухий кут: клієнту
    нема чого передати назад. Кожен кандидат мусить розвʼязуватись."""
    fund, free, m_fund, m_free = _pair_of_markets(conn)
    with pytest.raises(cats.CategoryError) as exc:
        cats.resolve_node(conn, "Маркетинг")
    assert exc.value.code == "ambiguous" and len(exc.value.candidates) == 2
    assert {cats.resolve_node(conn, c) for c in exc.value.candidates} ==         {m_fund["id"], m_free["id"]}


def test_every_path_from_list_tree_resolves_back_to_its_node(conn):
    """Круговорот канонічної назви на фікстурі З ТЕЗКАМИ: `path` кожного вузла
    дерева повертається саме в цей вузол."""
    fund, free, m_fund, m_free = _pair_of_markets(conn)
    cats.create_category(conn, "Маркетинг")                       # напрямок-тезка
    cats.create_category(conn, "Акмекорп", parent_id=free["id"])
    tree = cats.list_tree(conn)
    assert len(tree) > 3
    for node in tree:
        assert cats.resolve_node(conn, node["path"]) == node["id"], node["path"]


def test_a_node_name_may_not_contain_the_path_separator(conn):
    """`→` у назві ламає шлях з обох боків (сам вузол не резолвиться, а шлях
    його дитини стає тричастинним) — `bad_name` ДО запису, таблиця без змін."""
    fund, _, _, _ = _pair_of_markets(conn)
    before = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    with pytest.raises(cats.CategoryError) as exc:
        cats.create_category(conn, "Фонд → Клієнти")
    assert exc.value.code == "bad_name"
    with pytest.raises(cats.CategoryError) as child_exc:
        cats.create_category(conn, "Фонд → Клієнти", parent_id=fund["id"])
    assert child_exc.value.code == "bad_name"
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == before

    node = cats.create_category(conn, "Клієнти")
    with pytest.raises(cats.CategoryError) as upd:
        cats.update_category(conn, node["id"], name="А → Б")
    assert upd.value.code == "bad_name"
    assert conn.execute("SELECT name FROM categories WHERE id = ?",
                        (node["id"],)).fetchone()[0] == "Клієнти"


def test_path_separator_rule_is_unconditional_but_plain_reparent_stays_allowed(conn):
    """Правило `→` безумовне (ADR-023 §E п. 2): reparent без перейменування теж
    його перевіряє, а звичайний вузол переїжджає, як і раніше."""
    fund, free, _, _ = _pair_of_markets(conn)
    node = cats.create_category(conn, "Акмекорп", parent_id=fund["id"])
    moved, _ = cats.update_category(conn, node["id"], parent_id=free["id"])
    assert moved["parent_id"] == free["id"]

    # Вузол, якого через API не створити (легасі-рядок напряму в таблиці).
    cur = conn.execute("INSERT INTO categories (name, name_norm, sort_order, parent_id) "
                       "VALUES ('Старе → Імʼя', 'старе → імʼя', 99, ?)", (fund["id"],))
    conn.commit()
    with pytest.raises(cats.CategoryError) as exc:
        cats.update_category(conn, cur.lastrowid, parent_id=free["id"])
    assert exc.value.code == "bad_name"


def test_chat_map_writers_leave_nothing_behind_with_commit_false(conn):
    """Письменники на шляху карти чатів з `commit=False` не комітять неявно:
    `rollback()` викликача прибирає вузол, напрямок записів і теги чату
    (на цьому стоїть `--dry-run` як той самий прохід із відкотом)."""
    fund = cats.get_category(conn, conn.execute(
        "SELECT id FROM categories WHERE name_norm = 'фонд робота'").fetchone()[0])
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, tg_chat_id, created_at) "
        "VALUES ('telegram', '[TG] Акмекорп', -700, '2026-05-14 10:00:00')")
    tid = cur.lastrowid
    conn.commit()
    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    before = {t: count(t) for t in ("categories", "tags", "record_tags", "tg_chat_tags")}

    node = cats.create_category(conn, "Акмекорп", parent_id=fund["id"], commit=False)
    assert cats.set_category(conn, [tid], node["id"], commit=False) == [tid]
    assert cats.apply_chat_tags(conn, -700, ["Реклама"], commit=False) == [tid]
    conn.rollback()

    assert {t: count(t) for t in before} == before
    assert conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                        (tid,)).fetchone()[0] is None


def test_list_tree_carries_the_canonical_path_of_every_node(conn):
    fund, free, m_fund, m_free = _pair_of_markets(conn)
    paths = {n["id"]: n["path"] for n in cats.list_tree(conn)}
    assert all(paths.values()), "жоден вузол не лишається без path"
    assert paths[fund["id"]] == "Фонд Робота"
    assert paths[m_fund["id"]] == "Фонд Робота → Маркетинг"
    assert paths[m_free["id"]] == "Фріланс → Маркетинг"
    assert cats.get_category(conn, m_free["id"])["path"] == "Фріланс → Маркетинг"


def test_node_path_of_direction_and_project(conn):
    fund, _, m_fund, _ = _pair_of_markets(conn)
    assert cats.node_path(conn, fund["id"]) == "Фонд Робота"
    assert cats.node_path(conn, m_fund["id"]) == "Фонд Робота → Маркетинг"


def test_parse_date_error_is_ukrainian_and_mentions_the_format():
    """Сире `Invalid isoformat string` бачили і людина в тості, і агент у полі
    `error` MCP-тулза — обидва однаково безпорадно."""
    with pytest.raises(ValueError) as exc:
        cats.parse_date("not-a-date")
    text = str(exc.value)
    assert "YYYY-MM-DD" in text and "not-a-date" in text
    assert "isoformat" not in text.lower()
    with pytest.raises(ValueError):
        cats.date_sql("t.created_at", until="31.12.2026")
    assert cats.parse_date("2026-09-22") == "2026-09-22"


# --- HTTP ---------------------------------------------------------------

def test_http_create_duplicate_project_is_409_with_message_and_conflict(client, db):
    with get_db_connection(db) as conn:
        fund, free, m_fund, _ = _pair_of_markets(conn)
    r = client.post("/api/memory/categories",
                    json={"name": "Маркетинг", "parent_id": fund["id"]})
    assert r.status_code == 409
    body = r.get_json()
    assert body["code"] == "duplicate"
    assert "Фонд Робота" in body["message"]
    assert body["conflict"] == {"id": m_fund["id"], "path": "Фонд Робота → Маркетинг"}


def test_http_patch_parent_into_collision_is_409(client, db):
    with get_db_connection(db) as conn:
        fund, free, m_fund, m_free = _pair_of_markets(conn)
    r = client.patch(f"/api/memory/categories/{m_free['id']}",
                     json={"parent_id": fund["id"]})
    assert r.status_code == 409
    body = r.get_json()
    assert body["code"] == "duplicate" and "Фонд Робота" in body["message"]
    assert body["conflict"]["id"] == m_fund["id"]
    with get_db_connection(db) as conn:
        assert cats.get_category(conn, m_free["id"])["parent_id"] == free["id"]


def test_http_create_same_name_under_another_direction_is_201(client, db):
    with get_db_connection(db) as conn:
        fund = cats.get_category(conn, conn.execute(
            "SELECT id FROM categories WHERE name_norm = 'фонд робота'").fetchone()[0])
        free = cats.get_category(conn, conn.execute(
            "SELECT id FROM categories WHERE name_norm = 'фріланс'").fetchone()[0])
    first = client.post("/api/memory/categories",
                        json={"name": "Маркетинг", "parent_id": fund["id"]})
    second = client.post("/api/memory/categories",
                         json={"name": "Маркетинг", "parent_id": free["id"]})
    assert first.status_code == 200 and second.status_code == 200
    assert first.get_json()["id"] != second.get_json()["id"]


@pytest.mark.parametrize("url", [
    "/api/memory/search?q=кошторис&since=not-a-date",
    "/api/memory/action-items?until=not-a-date",
    "/api/memory/entities?since=not-a-date",
])
def test_http_memory_axes_reject_bad_date_in_ukrainian(client, url):
    r = client.get(url)
    assert r.status_code == 400
    error = r.get_json()["error"]
    assert "YYYY-MM-DD" in error and "isoformat" not in error.lower()


def test_http_list_categories_items_carry_path(client, db):
    with get_db_connection(db) as conn:
        _pair_of_markets(conn)
    items = client.get("/api/memory/categories").get_json()["categories"]
    paths = {i["name"]: i["path"] for i in items}
    assert all(i.get("path") for i in items)
    assert "Фонд Робота → Маркетинг" in {i["path"] for i in items}
    assert paths["Фріланс"] == "Фріланс"


# --- reparent → re-embed ------------------------------------------------

def _record_in(conn, cid: int, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "category_id, created_at) VALUES ('file', ?, 'Обговорили кошторис.', ?, "
        "'2026-05-14 10:00:00')", (name, cid))
    conn.commit()
    return cur.lastrowid


def test_http_reparent_alone_schedules_reembed_of_the_whole_subtree(
        client, db, no_real_reembed):
    """Переїзд проєкту без перейменування — така сама підстава для точкового
    re-embed, як rename: шлях «Напрямок → Проєкт» стоїть у контекстному
    префіксі чанка, тож після reparent вектори тезок розходяться саме по ньому."""
    from app.services import embeddings
    with get_db_connection(db) as conn:
        fund = cats.get_category(conn, conn.execute(
            "SELECT id FROM categories WHERE name_norm = 'фонд робота'").fetchone()[0])
        free = cats.get_category(conn, conn.execute(
            "SELECT id FROM categories WHERE name_norm = 'фріланс'").fetchone()[0])
        market = cats.create_category(conn, "Маркетинг", parent_id=fund["id"])
        recs = [_record_in(conn, market["id"], f"Планерка {i}") for i in range(3)]

    r = client.patch(f"/api/memory/categories/{market['id']}",
                     json={"parent_id": free["id"]})
    assert r.status_code == 200
    assert r.get_json()["reembed"]["count"] == len(recs)
    assert [c["ids"] for c in no_real_reembed] == [recs]
    assert no_real_reembed[0]["reason"] == "category_tree_changed"

    with get_db_connection(db) as conn:
        meta = embeddings._load_prefix_meta(conn, recs[0])
    prefix = embeddings.build_context_prefix(meta, {})
    assert "Фріланс → Маркетинг" in prefix
    assert "Фонд Робота → Маркетинг" not in prefix


# --- неоднозначний `project` на вході питання ---------------------------

@pytest.fixture
def ask_client(client, monkeypatch):
    """`/api/memory/ask` з замоканими ключем і самим RAG: тест про те, ЩО
    доходить до `rag`, а не про відповідь моделі."""
    from app.services import enrichment, rag
    monkeypatch.setattr(enrichment, "is_available", lambda: True)
    calls: list[dict] = []
    monkeypatch.setattr(rag, "answer_question",
                        lambda db_path, q, **kw: calls.append(kw) or {"answer": "ок"})
    client.rag_calls = calls
    return client


def test_http_ask_refuses_an_ambiguous_project_before_calling_rag(ask_client, db):
    with get_db_connection(db) as conn:
        _pair_of_markets(conn)
    r = ask_client.post("/api/memory/ask",
                        json={"question": "що по кошторису?", "project": "Маркетинг"})
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "ambiguous"
    assert body["candidates"] == ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]
    assert ask_client.rag_calls == [], "rag не мав бути викликаний"


def test_http_ask_accepts_a_full_path_project(ask_client, db):
    with get_db_connection(db) as conn:
        _, _, _, m_free = _pair_of_markets(conn)
        rec = _record_in(conn, m_free["id"], "Планерка Фрілансу")
    r = ask_client.post("/api/memory/ask",
                        json={"question": "що по кошторису?",
                              "project": "Фріланс → Маркетинг"})
    assert r.status_code == 200
    assert ask_client.rag_calls[0]["project"] == "Фріланс → Маркетинг"

    from app.services import scope
    assert scope.scope_filter_ids(db, "Фріланс → Маркетинг") == [rec]
