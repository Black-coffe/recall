"""Фільтри напрямок/теги/період на `/api/history`, `/api/audio/downloads`,
`/api/research/*` (спека `directions-projects-tags`, історія 02).

Той самий підхід, що й `test_taxonomy.py`: tmp-БД (`init_database`) + Flask
`test_client` навколо блупринтів напряму — бойовий `whisper_history.db` не
чіпається. Фікстури кириличні (`plan-dictated-sql-ignored-own-memory`).
"""
from __future__ import annotations

import inspect
import json

import pytest
from flask import Flask

from app.blueprints.audio_library import audio_bp
from app.blueprints.memory import memory_bp
from app.blueprints.research import research_bp
from app.blueprints.transcription import transcription_bp
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories as cats
from app.services import research as research_svc


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "taxonomy_http.db")
    init_database(path)
    return path


@pytest.fixture
def conn(db):
    with get_db_connection(db) as c:
        yield c


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.register_blueprint(audio_bp)
    app.register_blueprint(research_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db
    return app.test_client()


@pytest.fixture
def memory_client(db):
    """`memory_bp` окремо: `merge_category`/`sort_order`/`ask` (історія 23)."""
    app = Flask(__name__)
    app.register_blueprint(memory_bp)
    app.config["TESTING"] = True
    app.config["DATABASE"] = db
    return app.test_client()


def _tree(conn):
    """«Фонд Робота» (наявний серед засівів v15) → «Акмекорп» + сусідній
    напрямок «Інше»."""
    row = conn.execute("SELECT id FROM categories WHERE COALESCE(name_norm, name) = ?",
                       ("фонд робота",)).fetchone()
    fund = cats.get_category(conn, row[0]) if row else cats.create_category(conn, "Фонд Робота")
    bar = cats.create_category(conn, "Акмекорп", parent_id=fund["id"])
    other = cats.create_category(conn, "Інше")
    return fund, bar, other


def _add_tx(conn, *, category_id=None, name="Планерка", text="Обговорили Барселону",
           meeting_date=None, created_at=None, file_path=None):
    cols = ["source_type", "source_name", "transcript_text", "category_id"]
    vals = ["file", name, text, category_id]
    if meeting_date is not None:
        cols.append("meeting_date"); vals.append(meeting_date)
    if created_at is not None:
        cols.append("created_at"); vals.append(created_at)
    if file_path is not None:
        cols.append("file_path"); vals.append(file_path)
    ph = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO transcriptions ({','.join(cols)}) VALUES ({ph})", vals)
    conn.commit()
    return cur.lastrowid


def _add_audio(conn, *, yid, title, file_path, category_id=None, youtube_tags=None):
    conn.execute(
        "INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, "
        "source_type, category_id, tags) VALUES (?, ?, ?, ?, 'youtube', ?, ?)",
        (f"https://yt/{yid}", yid, title, file_path, category_id,
         json.dumps(youtube_tags) if youtube_tags is not None else "[]"))
    conn.commit()


# ============================================================
# /api/history — category_id
# ============================================================

def test_history_category_id_expands_direction_to_its_projects(client, conn):
    fund, bar, other = _tree(conn)
    t_fund = _add_tx(conn, category_id=fund["id"], name="У фонді")
    t_bar = _add_tx(conn, category_id=bar["id"], name="У Акмекорп")
    t_other = _add_tx(conn, category_id=other["id"], name="Інше")
    t_none = _add_tx(conn, category_id=None, name="Без напрямку")

    ids = {t["id"] for t in client.get(f"/api/history?category_id={fund['id']}")
                                 .get_json()["transcriptions"]}
    assert ids == {t_fund, t_bar}

    ids = {t["id"] for t in client.get(f"/api/history?category_id={bar['id']}")
                                 .get_json()["transcriptions"]}
    assert ids == {t_bar}

    ids = {t["id"] for t in client.get("/api/history?category_id=none")
                                 .get_json()["transcriptions"]}
    assert ids == {t_none}
    assert t_other not in ids


# ============================================================
# /api/history — tags (OR), category+tags (AND), unknown tag, period, bad date
# ============================================================

def test_history_tags_and_period_filters(client, conn):
    fund, bar, other = _tree(conn)
    t_ai = _add_tx(conn, category_id=bar["id"], name="AI", meeting_date="2026-09-03")
    t_inv = _add_tx(conn, category_id=fund["id"], name="Інвестори", meeting_date="2026-09-10")
    t_bare = _add_tx(conn, category_id=bar["id"], name="Без тегів", meeting_date="2026-09-05")
    # Обидві межі включно: рівно since / рівно until (останній — з часом у
    # межах дня, щоб довести `date(...)` а не рядкове порівняння).
    t_left = _add_tx(conn, category_id=bar["id"], name="Рівно з", meeting_date="2026-09-01")
    t_right = _add_tx(conn, category_id=bar["id"], name="Рівно по",
                      meeting_date="2026-09-07 23:40:00")
    cats.set_tags(conn, t_ai, add=["AI"])
    cats.set_tags(conn, t_inv, add=["інвестори"])

    ids = {t["id"] for t in client.get("/api/history?tags=AI,інвестори")
                                 .get_json()["transcriptions"]}
    assert ids == {t_ai, t_inv}

    ids = {t["id"] for t in client.get(f"/api/history?tags=AI&category_id={bar['id']}")
                                 .get_json()["transcriptions"]}
    assert ids == {t_ai}

    data = client.get("/api/history?tags=неіснуючий").get_json()
    assert data["transcriptions"] == []
    assert data["total"] == 0

    ids = {t["id"] for t in client.get("/api/history?since=2026-09-01&until=2026-09-07")
                                 .get_json()["transcriptions"]}
    assert ids == {t_ai, t_bare, t_left, t_right}

    r = client.get("/api/history?since=вчора")
    assert r.status_code == 400
    assert r.get_json()["success"] is False


# ============================================================
# /api/history + /api/history/<id> — tags/suggested_tags на кожному елементі
# ============================================================

def test_history_items_and_detail_carry_tags_and_suggested_tags(client, conn):
    tid = _add_tx(conn, name="З тегами")
    cats.set_tags(conn, tid, add=["AI", "інвестори"])
    cats.set_suggested_tags(conn, tid, ["чернетка"])
    tid_empty = _add_tx(conn, name="Без тегів")

    items = {t["id"]: t for t in client.get("/api/history").get_json()["transcriptions"]}
    assert {tg["name"] for tg in items[tid]["tags"]} == {"AI", "інвестори"}
    assert all({"id", "name", "color"} <= tg.keys() for tg in items[tid]["tags"])
    assert items[tid]["suggested_tags"] == ["чернетка"]
    assert items[tid_empty]["tags"] == []
    assert items[tid_empty]["suggested_tags"] == []

    detail = client.get(f"/api/history/{tid}").get_json()
    assert {tg["name"] for tg in detail["tags"]} == {"AI", "інвестори"}
    assert detail["suggested_tags"] == ["чернетка"]

    detail_empty = client.get(f"/api/history/{tid_empty}").get_json()
    assert detail_empty["tags"] == []
    assert detail_empty["suggested_tags"] == []


# ============================================================
# /api/audio/downloads — ті самі три осі через останній транскрипт
# ============================================================

def test_audio_downloads_axes_via_last_transcript(client, conn):
    fund, bar, other = _tree(conn)
    _add_audio(conn, yid="a1", title="A1", file_path="/x/a1.mp3",
              youtube_tags=["gaming"])
    t1 = _add_tx(conn, category_id=bar["id"], name="Транскрипт A1",
                file_path="/x/a1.mp3", meeting_date="2026-09-03")
    cats.set_tags(conn, t1, add=["AI"])

    _add_audio(conn, yid="a2", title="A2 без транскрипту", file_path="/x/a2.mp3",
              category_id=fund["id"])

    data = client.get(f"/api/audio/downloads?category_id={fund['id']}").get_json()
    titles = {d["title"] for d in data["downloads"]}
    # A1 — напрямок через останній транскрипт (Акмекорп, проєкт фонду);
    # A2 — власна category_id (fallback без транскрипту).
    assert titles == {"A1", "A2 без транскрипту"}

    row_a1 = next(d for d in data["downloads"] if d["title"] == "A1")
    assert {t["name"] for t in row_a1["tags"]} == {"AI"}
    assert row_a1["youtube_tags"] == ["gaming"]

    row_a2 = next(d for d in data["downloads"] if d["title"] == "A2 без транскрипту")
    assert row_a2["tags"] == []
    assert row_a2["youtube_tags"] == []

    # Тег живе лише на транскрипті — без нього рядок випадає з тег-фільтра.
    titles = {d["title"] for d in client.get("/api/audio/downloads?tags=AI")
                                        .get_json()["downloads"]}
    assert titles == {"A1"}

    # Період так само — тільки через транскрипт.
    titles = {d["title"] for d in
              client.get("/api/audio/downloads?since=2026-09-01&until=2026-09-07")
                    .get_json()["downloads"]}
    assert titles == {"A1"}

    r = client.get("/api/audio/downloads?since=вчора")
    assert r.status_code == 400
    assert r.get_json()["success"] is False


# ============================================================
# /api/research/* — напрямок розкривається на проєкти + tags/since/until
# ============================================================

def test_research_preview_sees_mentions_in_direction_projects(client, conn):
    fund, bar, other = _tree(conn)
    t_bar = _add_tx(conn, category_id=bar["id"], name="Нарада Акмекорп",
                    text="Обговорили Horeca напрямок")
    t_other = _add_tx(conn, category_id=other["id"], name="Стороннє",
                      text="Теж Horeca, але не той напрямок")

    data = client.get(f"/api/research/preview?q=Horeca&category_id={fund['id']}").get_json()
    assert data["success"] is True
    ids = {r["id"] for r in data["sample"]}
    assert ids == {t_bar}


def test_research_preview_filters_by_tags(client, conn):
    fund, bar, other = _tree(conn)
    t1 = _add_tx(conn, category_id=bar["id"], name="Раз", text="Про Horeca поговорили")
    t2 = _add_tx(conn, category_id=bar["id"], name="Два", text="Про Horeca теж")
    cats.set_tags(conn, t1, add=["AI"])

    data = client.get("/api/research/preview?q=Horeca&tags=AI").get_json()
    ids = {r["id"] for r in data["sample"]}
    assert ids == {t1}


def test_research_endpoints_pass_tags_since_until_to_service(client, monkeypatch):
    captured = {}

    def fake_collect(db_path, q, category_id=None, display=None, *,
                     tags=None, since=None, until=None):
        captured["collect"] = dict(category_id=category_id, tags=tags, since=since, until=until)
        return {"terms": [q], "display": display, "records": [],
                "stats": {"records": 0, "mentions": 0, "by_source": {}}}

    monkeypatch.setattr(research_svc, "collect", fake_collect)

    r = client.get(
        "/api/research/preview?q=Horeca&tags=AI,інвестори&since=2026-09-01&until=2026-09-07")
    assert r.status_code == 200
    assert captured["collect"]["tags"] == ["AI", "інвестори"]
    assert captured["collect"]["since"] == "2026-09-01"
    assert captured["collect"]["until"] == "2026-09-07"

    captured.clear()
    r = client.post("/api/research/originals", json={
        "q": "Horeca", "tags": "AI", "since": "2026-09-01", "until": "2026-09-07"})
    assert r.status_code == 200
    assert captured["collect"]["tags"] == ["AI"]
    assert captured["collect"]["since"] == "2026-09-01"
    assert captured["collect"]["until"] == "2026-09-07"

    def fake_summarize_stream(db_path, q, category_id=None, model=None, display=None, *,
                              tags=None, since=None, until=None):
        captured["summarize"] = dict(category_id=category_id, tags=tags, since=since, until=until)
        yield "event: start\ndata: {}\n\n"

    monkeypatch.setattr(research_svc, "summarize_stream", fake_summarize_stream)
    r = client.post("/api/research/summary", json={
        "q": "Horeca", "tags": "AI", "since": "2026-09-01", "until": "2026-09-07"})
    assert r.status_code == 200
    r.get_data()  # спожити генератор
    assert captured["summarize"]["tags"] == ["AI"]
    assert captured["summarize"]["since"] == "2026-09-01"
    assert captured["summarize"]["until"] == "2026-09-07"


def test_research_preview_rejects_bad_date(client):
    r = client.get("/api/research/preview?q=Horeca&since=вчора")
    assert r.status_code == 400
    assert r.get_json()["success"] is False


def test_research_service_has_no_private_category_parser():
    """research.py більше не тримає власну копію парсера — лише `categories.parse_filter`."""
    src = inspect.getsource(research_svc)
    assert "_parse_category" not in src
    assert "from app.services.categories import" in src
    assert "parse_filter" in src


# ============================================================
# v47: помилка кривої дати — українська на всіх спискових поверхнях (історія 20)
# ============================================================

@pytest.mark.parametrize("url", [
    "/api/history?since=not-a-date",
    "/api/audio/downloads?until=not-a-date",
    "/api/research/preview?q=кошторис&since=not-a-date",
])
def test_bad_date_error_is_ukrainian_everywhere(client, url):
    """Один розбір (`categories.parse_date`) — один текст: «Невалідна дата …
    очікується YYYY-MM-DD». Сире `Invalid isoformat string` не доходить до
    клієнта в жодній із трьох поверхонь."""
    r = client.get(url)
    assert r.status_code == 400
    error = r.get_json()["error"]
    assert "YYYY-MM-DD" in error and "not-a-date" in error
    assert "isoformat" not in error.lower()


# ============================================================
# Мажор 2 р4/р5: `merge_category` `target_id`, `sort_order` — той самий
# розбір (`_parse_int_or_none`), що вже стоїть на `category_id`/`parent_id`.
# ============================================================

def test_merge_category_rejects_garbled_target_id(memory_client, conn):
    fund, bar, other = _tree(conn)
    t = _add_tx(conn, category_id=other["id"], name="Інше")

    for bad in ("abc", "²"):
        r = memory_client.post(f"/api/memory/categories/{other['id']}/merge",
                               json={"target_id": bad})
        assert r.status_code == 400
        assert r.get_json()["success"] is False

    row = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?", (t,)).fetchone()
    assert row["category_id"] == other["id"]
    assert conn.execute("SELECT 1 FROM categories WHERE id = ?", (other["id"],)).fetchone()


def test_merge_category_valid_target_id_still_moves_records(memory_client, conn):
    fund, bar, other = _tree(conn)
    t = _add_tx(conn, category_id=other["id"], name="Інше")

    r = memory_client.post(f"/api/memory/categories/{other['id']}/merge",
                           json={"target_id": bar["id"]})
    assert r.status_code == 200
    assert r.get_json()["moved"] == 1
    row = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?", (t,)).fetchone()
    assert row["category_id"] == bar["id"]


def test_merge_category_null_or_missing_target_id_unassigns_as_before(memory_client, conn):
    fund, bar, other = _tree(conn)
    t = _add_tx(conn, category_id=other["id"], name="Інше")

    r = memory_client.post(f"/api/memory/categories/{other['id']}/merge", json={})
    assert r.status_code == 200
    row = conn.execute("SELECT category_id FROM transcriptions WHERE id = ?", (t,)).fetchone()
    assert row["category_id"] is None


def test_update_category_sort_order_garbled_is_400(memory_client, conn):
    fund, bar, other = _tree(conn)
    r = memory_client.patch(f"/api/memory/categories/{other['id']}", json={"sort_order": "²"})
    assert r.status_code == 400
    assert r.get_json()["success"] is False


def test_update_category_sort_order_valid_still_works(memory_client, conn):
    fund, bar, other = _tree(conn)
    r = memory_client.patch(f"/api/memory/categories/{other['id']}", json={"sort_order": 5})
    assert r.status_code == 200
    row = conn.execute("SELECT sort_order FROM categories WHERE id = ?",
                       (other["id"],)).fetchone()
    assert row["sort_order"] == 5


def test_update_category_sort_order_negative_accepted(memory_client, conn):
    """28 (мінор р0 п.5 / р1 п.4): `-5` знову приймається — звуження до
    невідʼємних (26) відкочено, JS-відправника з відʼємним значенням немає,
    звуження не було на записі."""
    fund, bar, other = _tree(conn)
    r = memory_client.patch(f"/api/memory/categories/{other['id']}", json={"sort_order": -5})
    assert r.status_code == 200
    row = conn.execute("SELECT sort_order FROM categories WHERE id = ?",
                       (other["id"],)).fetchone()
    assert row["sort_order"] == -5


def test_update_category_sort_order_non_integer_is_400(memory_client, conn):
    fund, bar, other = _tree(conn)
    for bad in ("abc", 1.5):
        r = memory_client.patch(f"/api/memory/categories/{other['id']}", json={"sort_order": bad})
        assert r.status_code == 400
        error = r.get_json()["error"]
        assert "sort_order" in error
        assert "невідʼємне" not in error.lower()


# ============================================================
# Мінор 11 р0: `project` у `/api/memory/ask` — нерядковий вхід → 400 ДО rag.
# ============================================================

def test_ask_rejects_non_string_project_before_calling_rag(memory_client, monkeypatch):
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    called = []
    monkeypatch.setattr("app.services.rag.answer_question",
                        lambda *a, **k: called.append(1))

    for bad in (5, {"a": 1}):
        r = memory_client.post("/api/memory/ask",
                               json={"question": "питання?", "project": bad})
        assert r.status_code == 400
        assert r.get_json()["success"] is False
    assert called == []


def test_ask_accepts_string_project_as_before(memory_client, conn, monkeypatch):
    fund, bar, other = _tree(conn)
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    captured = {}

    def fake_answer(db_path, q, top_k=8, **kwargs):
        captured.update(kwargs)
        return {"answer": "", "sources": [], "found": 0, "model": ""}

    monkeypatch.setattr("app.services.rag.answer_question", fake_answer)
    r = memory_client.post("/api/memory/ask",
                           json={"question": "питання?", "project": "Фонд Робота"})
    assert r.status_code == 200
    assert captured["project"] == "Фонд Робота"
