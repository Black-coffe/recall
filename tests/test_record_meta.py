"""Власна назва й опис запису: міграція v44 + `record_meta` + `PATCH /api/history/<id>`.

Спека `editable-title-description`, історія 01 (трасер). Зріз перевіряється
наскрізь: міграція на тимчасовій БД → сервіс → HTTP через `test_client`
окремого Flask-застосунку з `transcription_bp`. Бойовий `whisper_history.db`
не відкривається; фікстури — кириличні, бо саме на кирилиці цей проєкт уже
ловив баги порівняння (`plan-dictated-sql-ignored-own-memory`).
"""
import sqlite3

import pytest
from flask import Flask

from app.blueprints.transcription import transcription_bp
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import record_meta, reembed

#: справжній планувальник, знятий ДО автоюз-мока — фікстура `real_scheduler`
#: повертає саме його.
_REAL_SCHEDULE = reembed.schedule_record_reembed


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "meta.db")
    init_database(path)
    return path


@pytest.fixture(autouse=True)
def no_real_reembed(monkeypatch):
    """Жоден тест цього файлу не ставить справжню задачу в `job_queue`.

    Під повним набором сусідній `test_endpoints_smoke` привʼязує живий
    `JobQueue` до `app.state` і не відвʼязує, тож PATCH на тимчасовій БД без
    цього мока переембеджував би чанки бойових записів із тими самими id.
    Хто хоче справжній планувальник — бере фікстуру `real_scheduler`.
    """
    from app.services import reembed

    calls: list[tuple] = []
    monkeypatch.setattr(reembed, "schedule_record_reembed",
                        lambda tid, **kw: calls.append((tid, kw)) or "queued")
    return calls


@pytest.fixture
def real_scheduler(monkeypatch, no_real_reembed):
    """Повернути справжній `schedule_record_reembed` (для тесту шляху БД)."""
    from app.services import reembed

    monkeypatch.setattr(reembed, "schedule_record_reembed", _REAL_SCHEDULE)
    return reembed


@pytest.fixture
def client(db, tmp_path):
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db
    exports = tmp_path / "transcripts"
    exports.mkdir(exist_ok=True)
    app.config["TRANSCRIPTS_FOLDER"] = str(exports)
    return app.test_client()


def _add(db, source_name="Дзвінок із Барʼєрами.mp3", text="Обговорили бюджет Барселони",
         title=None, description=None):
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "title, description) VALUES ('file', ?, ?, ?, ?)",
        (source_name, text, title, description),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _row(db, tid):
    with get_db_connection(db) as conn:
        return conn.execute("SELECT * FROM transcriptions WHERE id = ?", (tid,)).fetchone()


def _fts_ids(db, query):
    conn = sqlite3.connect(db)
    ids = [r[0] for r in conn.execute(
        "SELECT rowid FROM transcriptions_fts WHERE transcriptions_fts MATCH ?",
        (query,)).fetchall()]
    conn.close()
    return ids


# ============================================================
# Міграція v44
# ============================================================

def test_migration_is_idempotent(db):
    init_database(db)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT COUNT(*) FROM schema_versions WHERE version = 44").fetchone()[0]
    conn.close()
    assert rows == 1


def test_columns_exist_and_default_null(db):
    tid = _add(db)
    row = _row(db, tid)
    assert row["title"] is None
    assert row["description"] is None


def test_fts_finds_term_only_in_title(db):
    tid = _add(db, text="Про щось геть інше", title="Нарада щодо Гуцульщини")
    assert _fts_ids(db, "Гуцульщини") == [tid]


def test_fts_finds_term_only_in_description(db):
    tid = _add(db, text="Про щось геть інше", description="Домовились про передоплату")
    assert _fts_ids(db, "передоплату") == [tid]


def test_fts_still_finds_old_rows_by_source_name(db):
    tid = _add(db, source_name="Зустріч із Андрієм.mp3")
    assert _row(db, tid)["title"] is None
    assert _fts_ids(db, "Андрієм") == [tid]


def test_upgrade_of_populated_db_keeps_index(tmp_path):
    """Апгрейд БД із наявними рядками: rebuild не губить старий індекс."""
    path = str(tmp_path / "old.db")
    init_database(path)
    # Відкочуємо FTS до стану «до v44» (без title/description) і знімаємо
    # версію, щоб повторна init_database пройшла шлях апгрейду, а не чистого
    # створення. Колонки в transcriptions лишаються — їх PRAGMA-гард пропустить.
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM schema_versions WHERE version >= 44")
    for trig in ("transcriptions_ai", "transcriptions_ad", "transcriptions_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    conn.execute("DROP TABLE IF EXISTS transcriptions_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE transcriptions_fts USING fts5(
            transcript_text, source_name, youtube_title,
            content='transcriptions', content_rowid='id',
            tokenize='unicode61 remove_diacritics 1')
    """)
    conn.commit()
    conn.close()
    tid = _add(path, source_name="Розмова з Оксаною.mp3", text="про склад і логістику")
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO transcriptions_fts(transcriptions_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    assert "title" not in {r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(transcriptions_fts)")}

    init_database(path)

    assert "title" in {r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(transcriptions_fts)")}
    assert _fts_ids(path, "Оксаною") == [tid]
    assert _fts_ids(path, "логістику") == [tid]
    assert _row(path, tid)["title"] is None

    # Тригери після перебудови живі: новий запис індексується за назвою.
    fresh = _add(path, text="інше", title="Нарада щодо Гуцульщини")
    assert _fts_ids(path, "Гуцульщини") == [fresh]


# ============================================================
# Сервіс record_meta
# ============================================================

def test_normalize_title_strips():
    assert record_meta.normalize_title("  Зустріч із Барʼєрами  ") == "Зустріч із Барʼєрами"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_normalize_title_empty_is_none(value):
    assert record_meta.normalize_title(value) is None


def test_normalize_title_too_long():
    with pytest.raises(ValueError):
        record_meta.normalize_title("я" * (record_meta.TITLE_MAX + 1))


def test_normalize_title_at_limit_ok():
    value = "я" * record_meta.TITLE_MAX
    assert record_meta.normalize_title(value) == value


def test_normalize_description_keeps_newlines_and_caps():
    assert record_meta.normalize_description(" рядок 1\nрядок 2 ") == "рядок 1\nрядок 2"
    with pytest.raises(ValueError):
        record_meta.normalize_description("о" * (record_meta.DESCRIPTION_MAX + 1))


def test_display_name_prefers_title():
    assert record_meta.display_name(
        {"id": 7, "title": "Нарада з Іваном", "source_name": "call_7.mp3"}
    ) == "Нарада з Іваном"


def test_display_name_falls_back_to_source_name():
    assert record_meta.display_name(
        {"id": 7, "title": "   ", "source_name": "Зустріч.mp3"}) == "Зустріч.mp3"


def test_display_name_falls_back_to_id():
    assert record_meta.display_name({"id": 7, "title": None, "source_name": None}) == "Запис #7"


def test_update_meta_touches_only_given_field(db):
    tid = _add(db, title="Стара назва", description="Старий опис")
    with get_db_connection(db) as conn:
        result = record_meta.update_meta(conn, tid, title="Нова назва")
    assert result["title"] == "Нова назва"
    assert result["description"] == "Старий опис"
    assert result["changed"] is True
    assert _row(db, tid)["description"] == "Старий опис"


def test_update_meta_not_changed_on_same_values(db):
    tid = _add(db, title="Нарада з Іваном")
    with get_db_connection(db) as conn:
        result = record_meta.update_meta(conn, tid, title="  Нарада з Іваном  ")
    assert result["changed"] is False
    assert result["title"] == "Нарада з Іваном"


def test_update_meta_missing_and_deleted(db):
    tid = _add(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET deleted_at = 1 WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    with get_db_connection(db) as conn:
        assert record_meta.update_meta(conn, tid, title="Хай там що") is None
        assert record_meta.update_meta(conn, 99999, title="Хай там що") is None


def test_after_meta_update_delegates_to_scheduler(no_real_reembed, db):
    """`changed=True` → планувальник кличеться з тим самим шляхом БД."""
    assert record_meta.after_meta_update(1, changed=True, db_path=db) is None
    assert no_real_reembed == [(1, {"db_path": db})]


def test_after_meta_update_skips_when_nothing_changed(no_real_reembed, db):
    assert record_meta.after_meta_update(1, changed=False, db_path=db) is None
    assert no_real_reembed == []


# ============================================================
# PATCH /api/history/<id>
# ============================================================

def test_patch_sets_title_and_keeps_source_name(client, db):
    tid = _add(db, source_name="call_2026.mp3")
    resp = client.patch(f"/api/history/{tid}", json={"title": "Нарада з Іваном"})
    assert resp.status_code == 200
    record = resp.get_json()["record"]
    assert record["display_name"] == "Нарада з Іваном"
    assert record["source_name"] == "call_2026.mp3"
    assert _row(db, tid)["source_name"] == "call_2026.mp3"


def test_patch_reembeds_request_db_not_live_one(client, db, real_scheduler, monkeypatch):
    """Задача в черзі несе шлях БД ЗАПИТУ (знахідка 2 раунду 2)."""
    from app import state
    from app.services import embeddings

    submitted = []

    class _Queue:
        def submit(self, kind, fn, *args, meta=None, **kwargs):
            submitted.append({"kind": kind, "args": args, "meta": meta or {}})
            return object()

    monkeypatch.setattr(state, "job_queue", _Queue(), raising=False)
    monkeypatch.setattr(state, "recording_service", None, raising=False)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    real_scheduler._pending_ids.clear()

    tid = _add(db, source_name="call_2026.mp3")
    assert client.patch(f"/api/history/{tid}",
                        json={"title": "Нарада з Іваном"}).status_code == 200

    assert [c["kind"] for c in submitted] == ["reembed_record"]
    assert submitted[0]["args"] == (tid, db)
    assert submitted[0]["meta"]["db_path"] == db
    real_scheduler._pending_ids.clear()


@pytest.mark.parametrize("value", [123, [], {}, True])
def test_patch_non_string_title_is_400(client, db, value):
    tid = _add(db, title="Нарада з Іваном")
    resp = client.patch(f"/api/history/{tid}", json={"title": value})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False and body["error"]
    assert _row(db, tid)["title"] == "Нарада з Іваном"


def test_patch_null_title_still_clears(client, db):
    tid = _add(db, source_name="Зустріч.mp3", title="Нарада з Іваном")
    record = client.patch(f"/api/history/{tid}",
                          json={"title": None}).get_json()["record"]
    assert record["title"] is None
    assert record["display_name"] == "Зустріч.mp3"


def test_patch_empty_title_clears_it(client, db):
    tid = _add(db, source_name="Зустріч.mp3", title="Нарада з Іваном")
    record = client.patch(f"/api/history/{tid}", json={"title": ""}).get_json()["record"]
    assert record["title"] is None
    assert record["display_name"] == "Зустріч.mp3"


def test_patch_description_keeps_newline(client, db):
    tid = _add(db)
    record = client.patch(
        f"/api/history/{tid}", json={"description": "рядок 1\nрядок 2"}).get_json()["record"]
    assert record["description"] == "рядок 1\nрядок 2"
    assert _row(db, tid)["description"] == "рядок 1\nрядок 2"


def test_patch_empty_payload_is_400(client, db):
    tid = _add(db)
    resp = client.patch(f"/api/history/{tid}", json={})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_patch_non_json_is_400(client, db):
    tid = _add(db)
    resp = client.patch(f"/api/history/{tid}", data="назва", content_type="text/plain")
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_patch_too_long_title_is_400(client, db):
    tid = _add(db)
    resp = client.patch(f"/api/history/{tid}", json={"title": "я" * 201})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert "200" in body["error"]
    assert _row(db, tid)["title"] is None


def test_patch_missing_record_is_404(client):
    resp = client.patch("/api/history/424242", json={"title": "Нарада"})
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


def test_patch_errors_leak_no_paths_or_stack(client, db):
    tid = _add(db)
    for resp in (client.patch(f"/api/history/{tid}", json={}),
                 client.patch(f"/api/history/{tid}", json={"title": "я" * 201}),
                 client.patch("/api/history/424242", json={"title": "Нарада"})):
        error = resp.get_json()["error"]
        assert "Traceback" not in error
        assert "\\" not in error and "/" not in error
        assert ".py" not in error


# ============================================================
# Експорт без жодної назви
# ============================================================

def test_export_without_any_name_has_no_none(client):
    """Payload без `title`/`source_name`/`id` → нейтральний фолбек, не «Запис #None»."""
    resp = client.post("/api/export/md", json={"text": "Обговорили кошторис"})
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    filename = resp.headers.get("Content-Disposition", "")
    assert "None" not in body and "Запис #" not in body
    assert "None" not in filename
    assert "transcript" in filename


def test_export_still_prefers_title(client):
    resp = client.post("/api/export/md",
                       json={"text": "Обговорили кошторис", "title": "Нарада з Іваном"})
    assert resp.status_code == 200
    assert "Нарада з Іваном" in resp.data.decode("utf-8")


# ============================================================
# GET-видача
# ============================================================

def test_history_list_exposes_meta(client, db):
    with_title = _add(db, source_name="call_a.mp3", title="Нарада з Іваном",
                      description="Про бюджет")
    without_title = _add(db, source_name="call_b.mp3")
    items = {i["id"]: i for i in client.get("/api/history").get_json()["transcriptions"]}

    assert items[with_title]["title"] == "Нарада з Іваном"
    assert items[with_title]["description"] == "Про бюджет"
    assert items[with_title]["display_name"] == "Нарада з Іваном"
    assert items[without_title]["title"] is None
    assert items[without_title]["display_name"] == "call_b.mp3"


def test_history_detail_exposes_meta(client, db):
    tid = _add(db, title="Нарада з Іваном", description="Про бюджет")
    body = client.get(f"/api/history/{tid}").get_json()
    assert body["title"] == "Нарада з Іваном"
    assert body["description"] == "Про бюджет"
    assert body["display_name"] == "Нарада з Іваном"

    plain = _add(db, source_name="call_b.mp3")
    body = client.get(f"/api/history/{plain}").get_json()
    assert body["title"] is None
    assert body["description"] is None
    assert body["display_name"] == "call_b.mp3"


def test_search_finds_record_by_title_only_word(client, db):
    tid = _add(db, text="Про щось геть інше", title="Нарада щодо Гуцульщини")
    _add(db, text="Сторонній запис")
    found = client.get("/api/history?search=Гуцульщини").get_json()["transcriptions"]
    assert [i["id"] for i in found] == [tid]


def test_search_finds_record_by_description_only_word(client, db):
    tid = _add(db, text="Про щось геть інше", description="Домовились про передоплату")
    _add(db, text="Сторонній запис")
    found = client.get("/api/history?search=передоплату").get_json()["transcriptions"]
    assert [i["id"] for i in found] == [tid]


# ============================================================
# Інваріант: чужий UPDATE source_name не чіпає назву
# ============================================================

def test_source_name_repersist_keeps_title(client, db):
    """Імітація TG re-persist: UPDATE source_name не змінює title/display_name."""
    tid = _add(db, source_name="Старе превʼю", title="Нарада з Іваном")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET source_name = ? WHERE id = ?",
                 ("Нове превʼю повідомлення", tid))
    conn.commit()
    conn.close()

    body = client.get(f"/api/history/{tid}").get_json()
    assert body["title"] == "Нарада з Іваном"
    assert body["display_name"] == "Нарада з Іваном"
    assert body["source_name"] == "Нове превʼю повідомлення"
