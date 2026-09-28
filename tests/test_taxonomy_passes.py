"""Офлайн-проходи таксономії наскрізь (спека `directions-projects-tags`, історія 05).

Тут перевіряється не окрема функція, а ЛАНЦЮГ, який у житті розтягнутий у часі
і саме тому рвався мовчки: карта чатів розкладає історію по проєктах і тегах →
нове повідомлення того ж чату успадковує і те, і те → зріз за назвою напрямку
бачить усе піддерево. Юніти кожної ланки живуть у `test_scope.py`
(`apply_chat_categories`, `scope_filter_ids`) і `test_telegram.py`
(`PUT …/tags`, інжест); дублювати їх тут нема сенсу.

Усе — на тимчасовій БД: жодного дотику до бойового архіву (Non-goals історії).

Запуск:
    .venv/Scripts/python.exe -m pytest -q tests/test_taxonomy_passes.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import Flask

from app.blueprints import telegram as tg
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import scope

CHAT_ID = -300


@pytest.fixture()
def app(tmp_path: Path, monkeypatch):
    """Мінімальний Flask із TG-блюпринтом на tmp-БД: напрямок «Фонд Робота»,
    моніторений чат без напрямку і дві його історичні репліки."""
    monkeypatch.setenv("TELEGRAM_CONTROL_TOKEN", "testtoken")
    db = str(tmp_path / "tax.db")
    init_database(db)
    with get_db_connection(db) as conn:
        conn.execute("DELETE FROM categories")
        conn.execute("INSERT INTO categories (id, name, name_norm, sort_order, parent_id) "
                     "VALUES (1, 'Фонд Робота', 'фонд робота', 1, NULL)")
        conn.execute("INSERT INTO tg_monitored_chats (chat_id, title, chat_type, enabled) "
                     "VALUES (?, 'Акмекорп чат', 'group', 1)", (CHAT_ID,))
        for mid in (1, 2):
            conn.execute(
                "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                "tg_chat_id, tg_chat_title, tg_message_id, category_id, created_at) VALUES "
                "('telegram', '[TG] Акмекорп чат', 'обговорення інвестицій', ?, "
                "'Акмекорп чат', ?, NULL, '2026-05-14 10:00:00')", (CHAT_ID, mid))
        conn.commit()

    flask_app = Flask(__name__)
    media = tmp_path / "media"
    media.mkdir()
    flask_app.config.update(DATABASE=db, TELEGRAM_CONTROL_HOST="127.0.0.1",
                            TELEGRAM_CONTROL_PORT=5599, TELEGRAM_MEDIA_DIR=str(media))
    flask_app.register_blueprint(tg.telegram_bp)
    return flask_app


def _mapping(tmp_path: Path, rows: list[dict]) -> str:
    p = tmp_path / "chat_map.json"
    p.write_text(json.dumps({"chats": rows}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_chat_map_pass_then_new_message_then_scope(app, tmp_path: Path, monkeypatch):
    """Карта → історія → нове повідомлення → зріз.

    Ланка, яку це ловить: успадкування напрямку і успадкування тегів — різні
    механізми (`tg_monitored_chats.category_id` проти `tg_chat_tags`), і поки
    їх не перевірили одним проходом, «теги працюють як напрямки» лишалося
    заявою.
    """
    db = app.config["DATABASE"]
    scheduled: list[tuple] = []
    monkeypatch.setattr("app.services.reembed.schedule_records_reembed",
                        lambda ids, **kw: scheduled.append((sorted(ids), kw.get("reason"))) or
                        {"mode": "point", "count": len(ids), "stale": 0})
    monkeypatch.setattr(tg, "_schedule_reembed", lambda ids, reason: {"mode": "none"})
    monkeypatch.setattr(tg, "_submit_embed_only", lambda *a, **k: "skipped")

    path = _mapping(tmp_path, [{"chat_id": CHAT_ID, "title": "Акмекорп чат",
                                "category": "Акмекорп", "parent": "Фонд Робота",
                                "tags": ["AI", "інвестори"]}])

    preview = scope.apply_chat_categories(db, mapping_path=path, dry_run=True)
    assert preview["records_recategorized"] == 2
    assert scheduled == [], "--dry-run не планує re-embed"
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 1

    res = scope.apply_chat_categories(db, mapping_path=path)
    assert res["categories_created"] == [
        {"path": "Фонд Робота → Акмекорп", "namesake": [], "note": None}]
    assert scheduled and scheduled[0][1] == "apply-chats"

    with get_db_connection(db) as conn:
        project_id = conn.execute(
            "SELECT id FROM categories WHERE name_norm = 'акмекорп'").fetchone()["id"]
        assert set(scheduled[0][0]) == {r[0] for r in conn.execute(
            "SELECT id FROM transcriptions")}

    # Нове повідомлення того ж чату: напрямок і теги — з чату, без жодного проходу.
    fresh = app.test_client().post(
        "/api/telegram/ingest",
        json={"kind": "text", "text": "свіжа репліка", "chat_id": CHAT_ID,
              "message_id": 77, "chat_title": "Акмекорп чат"},
        headers={"X-Telegram-Token": "testtoken"}).get_json()
    tid = fresh["transcription_id"]

    with get_db_connection(db) as conn:
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = ?",
                            (tid,)).fetchone()[0] == project_id
        inherited = {r[0] for r in conn.execute(
            "SELECT tg.name FROM record_tags rt JOIN tags tg ON tg.id = rt.tag_id "
            "WHERE rt.transcription_id = ? AND rt.source = 'chat'", (tid,))}
    assert inherited == {"AI", "інвестори"}

    # Зріз за назвою НАПРЯМКУ бачить записи проєкту (піддерево, контракт C2).
    by_direction = set(scope.scope_filter_ids(db, "Фонд Робота"))
    by_project = set(scope.scope_filter_ids(db, "Акмекорп"))
    assert tid in by_direction and by_direction == by_project


def test_label_uncategorized_dry_run_writes_nothing(app, monkeypatch):
    """Залишок (дзвінки/документи) розкладає той самий прохід — але `--dry-run`
    лише показує план: підтвердження дає людина, а не скрипт. Реальний прохід
    ставить змінені записи в точковий re-embed (контракт C7, як `apply_chat_categories`)."""
    db = app.config["DATABASE"]
    with get_db_connection(db) as conn:
        conn.execute("INSERT INTO transcriptions (id, source_type, source_name, "
                     "transcript_text, category_id, embedded_at) VALUES (99, 'recording', "
                     "'Дзвінок із Акмекорп', 'про інвестиції', NULL, CURRENT_TIMESTAMP)")
        conn.commit()

    from app.services import categorize as categorize_mod
    monkeypatch.setattr(categorize_mod, "suggest_category", lambda db_path, tid, k=8: {
        "suggestion": {"category_id": 1, "name": "Фонд Робота", "confidence": 0.9},
        "candidates": [{"category_id": 1, "name": "Фонд Робота", "score": 0.9}],
        "neighbors": 3})

    scheduled: list[tuple] = []
    monkeypatch.setattr(
        "app.services.reembed.schedule_records_reembed",
        lambda ids, **kw: scheduled.append((sorted(ids), kw)) or
        {"mode": "point", "count": len(ids), "stale": 0})

    stats = scope.label_uncategorized(db, dry_run=True)
    assert stats["labeled"] == 1 and stats["samples"]
    assert scheduled == [], "--dry-run не планує re-embed"
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 99"
                            ).fetchone()[0] is None, "--dry-run не пише"

    scope.label_uncategorized(db)
    with get_db_connection(db) as conn:
        assert conn.execute("SELECT category_id FROM transcriptions WHERE id = 99"
                            ).fetchone()[0] == 1
    assert scheduled == [([99], {"db_path": db, "reason": "label-rest"})]

    # Повторний прохід: усе вже розмічено — re-embed не кличеться.
    scheduled.clear()
    scope.label_uncategorized(db)
    assert scheduled == []
