"""Шар коментарів, Волна 6: стрічка для сторінки «Коментарі».

Головне, що тут перевіряється, — не список, а ЗНАМЕННИК і ФАСЕТИ. Без них
«5 виправлень» читається як повна картина, хоча це 5 із 200 — та сама пастка,
через яку у зводі свого часу зʼявився `coverage`.
"""
import sqlite3

import pytest
from flask import Flask

from app.blueprints.comments import comments_bp
from app.db.migrations import init_database
from app.services import comments


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.config['DATABASE'] = db
    app.register_blueprint(comments_bp)
    return app.test_client(), db


def _add_tx(path, name="Дзвінок"):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text) "
        "VALUES ('file', ?, 'текст')", (name,))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


def _seed(db, tid):
    comments.create(db, "transcription", tid, "насправді сума 12k", kind="correction")
    comments.create(db, "transcription", tid, "домовились на понеділок", kind="decision")
    comments.create(db, "transcription", tid, "передзвонити", kind="note")
    comments.create(db, "recording_session", "rec_x", "клієнт передумав",
                    kind="correction", source="live", anchor_time=65.0)


def test_feed_returns_rows_total_and_facets(db):
    tid = _add_tx(db)
    _seed(db, tid)
    d = comments.list_recent(db)
    assert d["total"] == 4
    assert len(d["comments"]) == 4
    assert d["by_kind"] == {"correction": 2, "decision": 1, "note": 1}


def test_facets_ignore_the_kind_filter(db):
    """Обравши «виправлення», користувач має далі бачити, скільки решти —
    інакше нема куди перемикатись."""
    tid = _add_tx(db)
    _seed(db, tid)
    d = comments.list_recent(db, kind="correction")
    assert d["total"] == 2                      # знаменник ЗА фільтром
    assert d["by_kind"]["note"] == 1            # фасети — без нього


def test_facets_do_respect_other_filters(db):
    tid = _add_tx(db)
    _seed(db, tid)
    d = comments.list_recent(db, target_type="recording_session")
    assert d["by_kind"] == {"correction": 1}


def test_feed_carries_target_name(db):
    tid = _add_tx(db, "Розмова з підрядником")
    comments.create(db, "transcription", tid, "уточнення")
    assert comments.list_recent(db)["comments"][0]["target_name"] == "Розмова з підрядником"


def test_feed_names_media_card_and_falls_back(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, "
                 "file_path) VALUES ('u', 'y', 'Аудіозапис', 'f.mp3')")
    aid = conn.execute("SELECT id FROM audio_downloads").fetchone()[0]
    conn.commit(); conn.close()
    comments.create(db, "audio_download", aid, "тут підрядник")
    comments.create(db, "recording_session", "rec_z", "жива", source="live")
    names = {c["body"]: c["target_name"] for c in comments.list_recent(db)["comments"]}
    assert names["тут підрядник"] == "Аудіозапис"
    # Тип без власного JOIN показується як «тип #ключ», а не порожнім рядком.
    assert names["жива"] == "recording_session #rec_z"


def test_feed_search_matches_body(db):
    tid = _add_tx(db)
    _seed(db, tid)
    d = comments.list_recent(db, search="понеділок")
    assert [c["body"] for c in d["comments"]] == ["домовились на понеділок"]
    assert d["total"] == 1


def test_feed_paginates(db):
    tid = _add_tx(db)
    for i in range(7):
        comments.create(db, "transcription", tid, f"коментар {i}")
    first = comments.list_recent(db, limit=3, offset=0)
    second = comments.list_recent(db, limit=3, offset=3)
    assert first["total"] == second["total"] == 7
    assert len(first["comments"]) == len(second["comments"]) == 3
    assert not ({c["id"] for c in first["comments"]}
                & {c["id"] for c in second["comments"]})


def test_feed_orders_newest_first(db):
    tid = _add_tx(db)
    a = comments.create(db, "transcription", tid, "перший")
    b = comments.create(db, "transcription", tid, "другий")
    ids = [c["id"] for c in comments.list_recent(db)["comments"]]
    assert ids[0] == b["id"] and ids[-1] == a["id"]


def test_feed_hides_deleted(db):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "тимчасове")
    comments.delete(db, c["id"])
    d = comments.list_recent(db)
    assert d["total"] == 0 and d["by_kind"] == {}


# ------------------------------------------------------------------- HTTP

def test_recent_endpoint_shape(client):
    c, db = client
    tid = _add_tx(db)
    _seed(db, tid)
    d = c.get('/api/comments/recent').get_json()
    assert set(d) >= {"comments", "total", "by_kind", "limit", "offset"}
    assert d["total"] == 4


def test_recent_endpoint_filters(client):
    c, db = client
    tid = _add_tx(db)
    _seed(db, tid)
    assert c.get('/api/comments/recent?kind=correction').get_json()["total"] == 2
    assert c.get('/api/comments/recent?search=понеділок').get_json()["total"] == 1
    assert c.get('/api/comments/recent?target_type=recording_session'
                 ).get_json()["total"] == 1
    assert c.get('/api/comments/recent?limit=2&offset=2').get_json()["offset"] == 2


def test_recent_endpoint_limit_is_bounded(client):
    """Стеля потрібна: сторінка гортає, а не вивалює архів одним запитом."""
    c, _ = client
    assert c.get('/api/comments/recent?limit=99999').get_json()["limit"] == 500
