"""Шар коментарів, Волна 2.5: пошук по Бібліотеці + математика заміру.

Дірка, яку закриває перша половина: власник пише «Барселона» уточненням до
дзвінка, де це слово не звучало. Семантичний пошук коментар знаходить, а
Бібліотека — там, де запис і треба відкрити — каже «нічого не знайдено».

Друга половина — детермінована математика `evals/comments_eval.py`: диф між
видачею з коментарями і без. Без мережі, GPU і БД (як `evals/metrics.py`).
"""
import sqlite3

import numpy as np
import pytest
from flask import Flask

from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database
from app.services import comments, embeddings
from evals import comments_eval


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    return a / (np.linalg.norm(a) or 1.0)


@pytest.fixture(autouse=True)
def mock_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "hist.db")
    init_database(db_path)
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config['TESTING'] = True
    app.config['DATABASE'] = db_path
    return app.test_client(), db_path


def _add_tx(db_path, name="Дзвінок", text="говорили про дах і фасад"):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "language) VALUES ('file', ?, ?, 'uk')", (name, text))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


def _ids(resp):
    return [t["id"] for t in resp.get_json()["transcriptions"]]


# --------------------------------------------------- пошук по Бібліотеці

def test_search_finds_record_by_its_comment(client):
    c, db = client
    tid = _add_tx(db)
    cm = comments.create(db, "transcription", tid, "це по обʼєкту Барселона")
    comments.index_comment(db, cm["id"])
    assert _ids(c.get('/api/history?search=Барселона')) == [tid]


def test_search_still_finds_by_transcript(client):
    """Регресія: додавання гілки коментарів не має зламати основний шлях."""
    c, db = client
    tid = _add_tx(db)
    assert _ids(c.get('/api/history?search=фасад')) == [tid]


def test_search_misses_stay_misses(client):
    c, db = client
    _add_tx(db)
    assert _ids(c.get('/api/history?search=вертоліт')) == []


def test_search_uses_same_tokenizer_as_transcripts(client):
    """Через FTS коментарів, а не LIKE: «бюджету» мусить знаходити «бюджет»."""
    c, db = client
    tid = _add_tx(db)
    cm = comments.create(db, "transcription", tid, "тут про бюджет")
    comments.index_comment(db, cm["id"])
    assert _ids(c.get('/api/history?search=бюджет')) == [tid]


def test_deleted_comment_stops_matching(client):
    c, db = client
    tid = _add_tx(db)
    cm = comments.create(db, "transcription", tid, "унікальнеслово")
    comments.index_comment(db, cm["id"])
    assert _ids(c.get('/api/history?search=унікальнеслово')) == [tid]
    comments.delete(db, cm["id"])
    assert _ids(c.get('/api/history?search=унікальнеслово')) == []


def test_comment_on_other_record_does_not_leak(client):
    c, db = client
    t1 = _add_tx(db, "перший")
    _add_tx(db, "другий")
    cm = comments.create(db, "transcription", t1, "рідкіснеслово")
    comments.index_comment(db, cm["id"])
    assert _ids(c.get('/api/history?search=рідкіснеслово')) == [t1]


def test_search_survives_db_without_comment_tables(client):
    """Стара БД без v37: пошук мусить працювати як раніше, а не падати."""
    c, db = client
    tid = _add_tx(db)
    conn = sqlite3.connect(db)
    conn.executescript("DROP TABLE comment_chunks_fts; DROP TABLE comment_chunks; "
                       "DROP TABLE comments;")
    conn.commit(); conn.close()
    assert _ids(c.get('/api/history?search=фасад')) == [tid]


def test_search_does_not_duplicate_when_both_match(client):
    """Збіг і в транскрипті, і в коментарі — це ОДИН запис, не два."""
    c, db = client
    tid = _add_tx(db)
    cm = comments.create(db, "transcription", tid, "теж про фасад")
    comments.index_comment(db, cm["id"])
    assert _ids(c.get('/api/history?search=фасад')) == [tid]


# ----------------------------------------------------- математика заміру

def _ch(cid, tid=1):
    return {"chunk_id": cid, "transcription_id": tid, "source_type": "file"}


def _cm(cid):
    return {"chunk_id": -cid, "comment_id": cid, "source_type": "comment"}


def test_key_separates_comment_and_chunk_spaces():
    """Простори id перетинаються — диф не має рахувати чанк №5 і коментар №5
    за той самий елемент."""
    assert comments_eval._key(_ch(5)) != comments_eval._key(_cm(5))


def test_aggregate_counts_drift_and_slots():
    rows = [
        {"id": "a", "k": 8, "identical": False, "drift": 2, "comment_slots": 2,
         "max_rank_shift": 3, "top1_changed": True},
        {"id": "b", "k": 8, "identical": True, "drift": 0, "comment_slots": 0,
         "max_rank_shift": 0, "top1_changed": False},
        {"id": "a", "k": 12, "identical": True, "drift": 0, "comment_slots": 0,
         "max_rank_shift": 0, "top1_changed": False},
    ]
    at8 = comments_eval._aggregate(rows, 8)
    assert at8 == {"k": 8, "items": 2, "identical": 1, "drift_total": 2,
                   "drift_mean": 1.0, "comment_slots_total": 2,
                   "top1_changed": 1, "max_rank_shift": 3}
    assert comments_eval._aggregate(rows, 12)["drift_total"] == 0


def test_aggregate_survives_empty_k():
    """Ділення на кількість без захисту дало б ZeroDivisionError рівно тоді,
    коли замір і так нічого не знайшов."""
    assert comments_eval._aggregate([], 8)["items"] == 0


def test_two_k_values_are_mandatory():
    """Один k дає впевнену відповідь, яка не витримує перевірки другим
    (memory/eval-verdict-flips-with-k)."""
    assert comments_eval.K_VALUES == (8, 12)


def test_load_questions_accepts_both_shapes(tmp_path):
    import json
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"items": [{"id": "x", "question": "Q1"}]}), encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text(json.dumps([{"question": "Q2"}]), encoding="utf-8")
    assert comments_eval._load_questions(str(a))[0]["id"] == "x"
    assert comments_eval._load_questions(str(b))[0]["id"] == "q0"


def test_seed_refuses_to_write_to_the_live_db():
    """--seed пише в БД. Промах повз копію зіпсував би бойовий архів
    синтетичними коментарями, які потім довелось би виловлювати руками."""
    rc = comments_eval.main(["--golden", "evals/golden_set.example.json",
                             "--db", "whisper_history.db", "--seed", "1"])
    assert rc == 2
