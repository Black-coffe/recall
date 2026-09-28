"""Тести скоупу архіву (Трек 2, app/services/scope.py + retrieval.entity_ids).

Перевіряють два шари звуження:
  1. напрямок за Telegram-чатом (детермінована розмітка історії + прив'язка чату);
  2. зріз за сутністю-проєктом через meeting_entities (many-to-many).
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
        # Міграція сама сіє дефолтні напрямки — прибираємо їх, щоб тест мав
        # рівно ту таблицю, яку описує (інакше id конфліктують).
        conn.execute("DELETE FROM categories")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (1, 'Фонд Робота', 'фонд робота', 1)")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (2, 'NOVA', 'nova', 2)")
        # Чат №1 моніториться і помилково лежить у «Фонд», №2 — взагалі не в таблиці.
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
    assert res["chats_bound"] == 1        # -100 перевʼязаний з «Фонд» на NOVA
    assert res["chats_inserted"] == 1     # -200 не моніторився — рядок заведено
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
    # Не моніториться, але напрямок закріплено — вмикання не поверне у «без категорії».
    assert chat200["enabled"] == 0 and chat200["category_id"] == cats["Pegasus"]


def test_apply_chat_categories_idempotent_and_dry_run(db: str, tmp_path: Path):
    path = _mapping(tmp_path, [{"chat_id": -100, "title": "NOVA", "category": "NOVA"}])
    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert preview["records_recategorized"] == 2 and preview["chats_bound"] == 1
    with get_db_connection(db) as conn:  # dry-run нічого не пише
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
# Контракт C7: parent + tags у карті чатів (історія 05)
# ============================================================

def test_apply_chat_categories_creates_project_under_parent_with_tags(
        db: str, tmp_path: Path, monkeypatch):
    """`{"parent": "Фонд Робота", "tags": ["AI"]}` — проєкт створюється під
    наявним напрямком, теги лягають з source='chat', і після реального проходу
    змінені записи йдуть у re-embed (мок)."""
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
    assert calls == [], "--dry-run не має нічого писати"
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM categories WHERE name = 'Акмекорп'").fetchone()[0] == 0

    res = scope.apply_chat_categories(db, mapping_path=path)
    # План називає ШЛЯХ, а не голу назву: «Акмекорп» і «Фонд Робота → Акмекорп» —
    # два різні вузли, і превʼю мусить казати, який саме заведе прохід.
    assert res["categories_created"] == [
        {"path": "Фонд Робота → Акмекорп", "namesake": [], "note": None},
    ], "«Фонд Робота» уже існував"
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
    assert calls, "реальний прохід мав запланувати re-embed"
    assert 10 in calls[0][0]
    assert calls[0][1]["reason"] == "apply-chats"

    # Idempotent: повторний прохід нічого не міняє і re-embed не кличе.
    calls.clear()
    again = scope.apply_chat_categories(db, mapping_path=path)
    assert again["records_recategorized"] == 0 and again["tags_applied"] == 0
    assert calls == []


def test_apply_chat_categories_dropping_tag_keeps_manual_one(
        db: str, tmp_path: Path, monkeypatch):
    """Зняття тега з чату в карті знімає лише `source='chat'` — ручний тег на
    конкретному записі лишається (ADR §B, «один слот — один власник»)."""
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
                                 "tags": ["AI"]}])         # «інвестори» знято з чату
    scope.apply_chat_categories(db, mapping_path=path2)
    with get_db_connection(db) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT tg.name FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
            "WHERE rt.transcription_id = 10")}
    assert names == {"AI", "ручний"}, "чатовий тег знято, ручний лишився"


def test_prior_correction_damps_giant_category():
    """Гігант не має вигравати лише через розмір — але й карлик не має через малість."""
    cands = [{"category_id": 1, "name": "Фонд", "score": 0.55},
             {"category_id": 2, "name": "NOVA", "score": 0.45}]
    ranked = scope._prior_correct(cands, {1: 2000, 2: 50})
    assert ranked[0]["category_id"] == 2, "менша категорія з майже тим самим скором має вигравати"

    # Але при явній перевазі гігант лишається першим — коригування мʼяке.
    ranked2 = scope._prior_correct(
        [{"category_id": 1, "name": "Фонд", "score": 0.95},
         {"category_id": 2, "name": "NOVA", "score": 0.05}], {1: 2000, 2: 50})
    assert ranked2[0]["category_id"] == 1


def test_label_uncategorized_claude_arbiter_sees_project_paths(tmp_path: Path, monkeypatch):
    """label_uncategorized --claude: арбітр бачить кандидатів шляхами
    «Напрямок → Проєкт» (контракт C7) і його відповідь пише через `set_category`."""
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
    """Контракт C7 (як `apply_chat_categories`): реальний прохід `label-rest`
    ставить розмічені записи в точковий re-embed рівно одним викликом; `--dry-run`
    і повторний прохід (усе вже розмічено) — жодного виклику."""
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
    assert calls == [], "--dry-run не має нічого писати"
    with get_db_connection(path) as conn:
        assert all(r["category_id"] is None for r in conn.execute(
            "SELECT category_id FROM transcriptions"))

    res = scope.label_uncategorized(path, use_claude=True, min_confidence=0.6)
    assert res["labeled"] == 3
    assert len(calls) == 1, "рівно один виклик re-embed на прохід"
    ids, kw = calls[0]
    assert ids == [1, 2, 3]
    assert kw["db_path"] == path
    assert kw["reason"] == "label-rest"

    # Повторний прохід: усе вже розмічено — re-embed не кличеться.
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
    assert not by_name["NOVA"].search("домовились про зустріч"), "не має ловити підрядок у слові"
    # «Фонд Робота» складається лише зі стоп-слів → матчера для неї немає
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
    assert scope.resolve_scope(db, "Акмекорп") == [10]          # через аліас
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
    """Порожній скоуп не додає умови (інакше пошук звузився б до нуля)."""
    from app.services import retrieval
    sql, params = retrieval._scope_sql(None)
    assert sql == "" and params == []
    sql, params = retrieval._scope_sql([7, 9])
    assert "ch.transcription_id IN" in sql and params == [7, 9]


def test_scope_filter_includes_records_without_graph_links(db: str):
    """Зріз не має ховати записи, які просто не пройшли enrichment.

    meeting_entities є лише у збагачених (~помітна частина архіву), тож зріз «лише за графом»
    мовчки викидав решту — саме на цьому валився golden-set кейс із документом.
    """
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                     "VALUES (10, 'project', 'Datalink', 'datalink')")
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (10, 'Сенсети', 'сенсети')")
        # id=1 має лінк у графі; id=2 — жодного, але назва містить аліас.
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) VALUES (1, 10)")
        conn.execute("UPDATE transcriptions SET source_name = 'Сенсети, базова інфраструктура' "
                     "WHERE id = 2")
        conn.commit()

    graph_only = scope.scope_transcription_ids(db, [10])
    full = scope.scope_filter_ids(db, "Datalink")
    assert graph_only == [1]
    assert set(full) >= {1, 2}, "текстовий шар має підхопити незбагачений запис"


def test_scope_filter_ids_direction_name_includes_projects(tmp_path: Path):
    """Збіг назви з НАПРЯМКОМ бере піддерево (контракт C2); з ПРОЄКТОМ — лише його."""
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
# Волна 4.5.1a: зріз розширюється до цілої нитки
# ============================================================

def _tg_thread_fixture(path):
    """Нитка: назва проєкту звучить один раз, рішення — у сусідній репліці."""
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
    """Живий замір: Ковальчука згадана 23 рази всередині фонд-чатів, і саме ці
    23 однорядковики були всім, що бачив project=. Рішення — в сусідніх
    репліках, де назви вже немає."""
    path = str(tmp_path / "t.db")
    init_database(path)
    _tg_thread_fixture(path)
    got = set(scope._expand_to_threads(path, {1}))
    assert got == {1, 2, 3}, "нитка має приїхати цілком"


def test_scope_expansion_stops_at_thread_boundary(tmp_path):
    """Розширення до ЧАТУ було б поверненням до того, від чого волна лікує."""
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
# v47: однойменні вузли в різних напрямках (історія 20)
# ============================================================

def _two_markets(db: str) -> dict:
    """«Фонд Робота → Маркетинг» і «Фріланс → Маркетинг» + по запису в кожному.

    Назви записів навмисно БЕЗ слова «Маркетинг»: інакше текстовий шар зрізу
    зловив би їх сам і тест доводив би не те, що перевіряє (дерево)."""
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
    """Гола назва сидить на двох вузлах — зріз не бере ні перший, ні обидва:
    виняток зі списком шляхів іде нагору (`rag` його не ковтає), бо тихе
    обʼєднання відповіло б на інше питання, ніж спитали."""
    from app.services import categories
    _two_markets(db)
    with pytest.raises(categories.CategoryError) as exc:
        scope.scope_filter_ids(db, "Маркетинг")
    assert exc.value.code == "ambiguous"
    assert exc.value.candidates == ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]


def test_rag_does_not_swallow_the_ambiguous_scope_error(db: str):
    """`rag._resolve_project` гасить «нічого не знайшлось» (шукаємо ширше), але
    НЕ неоднозначність — інакше 409 перетворився б на тиху відповідь по всьому
    архіву."""
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
    """Мок анропік-клієнта: арбітр бачить рівно цю відповідь."""
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
    """Дві однойменні картки — жодної. «Перший збіг» тут означає приписати
    запис чужому напрямку мовчки; у звіт проходу їдуть обидва шляхи."""
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
    assert "Маркетинг" not in by_name, "неоднозначна назва не має бути сигналом"
    assert "NOVA" in by_name


def test_chat_map_creates_own_project_instead_of_stealing_a_namesake(
        db: str, tmp_path: Path, monkeypatch):
    """Рядок із `parent` більше не reparent-ить чужий однойменний вузол."""
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
    assert rows[m_fund] == 1, "чужий «Маркетинг» лишився під своїм напрямком"
    assert len(rows) == 2
    new_id = next(i for i in rows if i != m_fund)
    assert rows[new_id] == 3 and bound == new_id


def test_chat_map_skips_an_ambiguous_row_without_parent(db: str, tmp_path: Path,
                                                        monkeypatch):
    """Без `parent` неоднозначна назва не призначається нікому — ні в
    dry-run, ні в реальному проході (інакше чат втратив би напрямок)."""
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
    assert "вкажіть parent" in note and all(p in note for p in both)

    res = scope.apply_chat_categories(db, mapping_path=path)
    assert res["ambiguous"] == [{"category": "Маркетинг", "candidates": both}]
    assert res["chats_bound"] == 0
    with get_db_connection(db) as conn:
        assert conn.execute(
            "SELECT category_id FROM tg_monitored_chats WHERE chat_id = -500"
        ).fetchone()["category_id"] == 1, "напрямок чату не мав зникнути"


def test_chat_map_binds_two_rows_of_one_name_to_two_parents(
        db: str, tmp_path: Path, monkeypatch):
    """Дворядковий випадок того самого, що доводить тест вище: рядок карти
    резолвиться ПАРОЮ `(parent, category)`, не голою назвою.

    Кеш за голим імʼям звів би обидва рядки в один вузол і привʼязав до нього
    обидва чати — історія другого поїхала б у чужий напрямок мовчки, а
    `--dry-run` показав би одне створення замість двох."""
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
    with get_db_connection(db) as conn:      # dry-run нічого не пише
        assert conn.execute("SELECT COUNT(*) FROM categories WHERE name_norm = 'маркетинг'"
                            ).fetchone()[0] == 0

    res = scope.apply_chat_categories(db, mapping_path=path)
    assert res["ambiguous"] == []

    with get_db_connection(db) as conn:
        nodes = [r["id"] for r in conn.execute(
            "SELECT id FROM categories WHERE name_norm = 'маркетинг'")]
        assert len(nodes) == 2, "два батьки — два вузли"
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
    assert again["categories_created"] == [], "усе вже створено"


def test_chat_map_plan_names_the_namesake_it_is_about_to_fork(
        db: str, tmp_path: Path, monkeypatch):
    """Превʼю мусить назвати вузол, який роздвоїть таксономію.

    Раніше `categories_created` рахувалось звіркою назви з УСІЄЮ таблицею
    «до», тож дитина-тезка під іншим батьком у план не потрапляла зовсім:
    `--dry-run` друкував `[]`, а реальний прохід заводив другий «Акмекорп».
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
         "note": "тезка існує: «Акмекорп»"}]
    assert preview["per_chat"][0]["note"] == "ціль: «Фонд Робота → Акмекорп»"

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
                            ).fetchone()[0] == 4, "записи старого напрямку не чіпаємо"


# ============================================================
# Історія 26: dry-run = той самий писальний шлях із відкотом
# ============================================================

def _snapshot(db: str) -> dict:
    """Усе, що пише карта чатів: вузли, привʼязки чатів, напрямки записів, теги."""
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
    """Результат проходу, незалежний від id: шляхи вузлів, куди поїхали чати й записи."""
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
    """Голий «Маркетинг» після/до `{Маркетинг, parent: Фонд Робота}`.

    Раніше dry-run перевиводив план окремою гілкою, а реальний прохід мутував
    таблицю по ходу: голий рядок першим заводив напрямок «Маркетинг», другим —
    бачив свіжу дитину «Фонду». Тепер резолв детермінований (спершу рядки з
    parent), а превʼю — це той самий прохід із `rollback()`."""
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
        assert _snapshot(path_db) == before, f"({label}) dry-run лишив слід у БД"
        assert calls == [], "dry-run не планує re-embed"

        res = scope.apply_chat_categories(path_db, mapping_path=path)
        assert _no_reembed(preview) == _no_reembed(res), f"({label}) план ≠ факт"
        assert preview["reembed_scheduled"] == 0 and res["reembed_scheduled"] > 0
        assert res["ambiguous"] == []
        assert [c["path"] for c in res["categories_created"]] == [
            "Фонд Робота → Маркетинг"]
        notes = {r["title"]: r["note"] for r in res["per_chat"]}
        assert notes == {"Маркетинг Фонду": "ціль: «Фонд Робота → Маркетинг»",
                         "Маркетинг голий": "ціль: «Фонд Робота → Маркетинг»"}
        results[label] = _by_path(path_db)

        # Другий реальний прохід — нуль змін.
        calls.clear()
        again = scope.apply_chat_categories(path_db, mapping_path=path)
        assert again["categories_created"] == []
        assert (again["chats_bound"], again["chats_inserted"],
                again["records_recategorized"], again["tags_applied"],
                again["reembed_scheduled"]) == (0, 0, 0, 0, 0)
        assert calls == []

    assert results["a"] == results["b"], "результат залежить від порядку рядків"
    assert results["a"]["chats"][-601] == results["a"]["chats"][-602] == \
        "Фонд Робота → Маркетинг"
    assert results["a"]["records"][60] == results["a"]["records"][61] == \
        "Фонд Робота → Маркетинг"


@pytest.mark.parametrize("parent_first", [True, False])
def test_chat_map_ambiguity_is_keyed_by_the_row_pair(db: str, tmp_path: Path,
                                                     monkeypatch, parent_first):
    """Рядок із `parent`, що чисто розвʼязався парою, не відхиляється через
    голого тезку (раніше `ambiguous` ключувався голою назвою і відхиляв обидва)."""
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
    assert notes["Маркетинг Фрілансу"]["note"] == "ціль: «Фріланс → Маркетинг»"
    assert notes["Маркетинг Фрілансу"]["records_to_move"] == 1
    assert notes["Маркетинг голий"]["note"].startswith("неоднозначно, вкажіть parent: ")
    assert notes["Маркетинг голий"]["records_to_move"] == 0
    assert res["categories_created"] == []

    with get_db_connection(db) as conn:
        bound = {r[0]: r[1] for r in conn.execute(
            "SELECT chat_id, category_id FROM tg_monitored_chats WHERE chat_id IN (-603, -604)")}
        assert bound[-603] == nodes["m_free"]
        assert bound[-604] == 1, "неоднозначний чат лишився, де був"
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 62"
                            ).fetchone()[0] == nodes["m_free"]


def test_chat_map_namesake_note_is_the_same_in_dry_run_and_real(
        db: str, tmp_path: Path, monkeypatch):
    """Другий «Маркетинг» у тому ж проході — тезка першого. Окрема гілка плану
    рахувала `namesake` по таблиці «до» і друкувала `[]`; реальний прохід
    бачив уже створений перший."""
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
         "note": "тезка існує: «Фонд Робота → Маркетинг»"}]
    assert preview["categories_created"] == expected
    assert res["categories_created"] == expected


def test_map_claude_answer_takes_id_only_from_a_whole_integer(db: str):
    """Цифра всередині назви — не id: «Проєкт 2026» лишається назвою.

    Раніше бралась перша цифра будь-де у відповіді (пошук регексом по тексту),
    тож перший же вузол із числом у назві мовчки ламав арбітра."""
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
