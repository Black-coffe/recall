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
                     "VALUES (1, 'Робота', 'фонд робота', 1)")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order) "
                     "VALUES (2, 'NOVA', 'nova', 2)")
        # Чат №1 моніториться і помилково лежить у «Фонд», №2 — взагалі не в таблиці.
        conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled, "
                     "category_id) VALUES (-100, 'Nova Dance & Робота', 'group', 1, 1)")
        for i, (cid, chat) in enumerate([(-100, "Nova Dance & Робота"),
                                         (-100, "Nova Dance & Робота"),
                                         (-200, "Unicorn & Робота")], start=1):
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
        {"chat_id": -200, "title": "Unicorn & Робота", "category": "Unicorn"},
    ])
    res = scope.apply_chat_categories(db, mapping_path=path)

    assert res["categories_created"] == ["Unicorn"]
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
    assert rows[3] == cats["Unicorn"]
    # Не моніториться, але напрямок закріплено — вмикання не поверне у «без категорії».
    assert chat200["enabled"] == 0 and chat200["category_id"] == cats["Unicorn"]


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


def test_title_matcher_ignores_generic_tokens(db: str):
    with get_db_connection(db) as conn:
        matchers = scope._title_matchers(conn)
    by_name = {name: rx for _, name, rx in matchers}
    assert "NOVA" in by_name
    assert by_name["NOVA"].search("NOVA я та Микола по маркетингу")
    assert not by_name["NOVA"].search("домовились про зустріч"), "не має ловити підрядок у слові"
    # «Робота» складається лише зі стоп-слів → матчера для неї немає
    assert "Робота" not in by_name


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
