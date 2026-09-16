"""Лог питань до архіву (`ask_log`, міграція v41) — історія 04 Хвилі A.

Офлайн: Anthropic-клієнт і `retrieval.search` підмінені фейками, БД —
тимчасова. Покриває запис рядка з обох шляхів (sync і stream), канал
`ui`/`mcp`, маршрут оцінки, вартість (невідома модель → NULL) і мінінг
`--from-ask-log` у `evals/build_golden.py`.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.db.migrations import init_database
from app.services import enrichment, rag
from app.services import models as models_mod
from evals import build_golden


# ============================================================
# Фейки Claude (той самий контракт, що у tests/test_rag.py)
# ============================================================

class FakeUsage:
    def __init__(self, input_tokens=1000, output_tokens=200, cache_read_input_tokens=500):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _Block:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class FakeFinal:
    def __init__(self, text="Відповідь з архіву [1]", model=models_mod.HAIKU_4_5, usage=None):
        self.content = [_Block("text", text)]
        self.model = model
        self.usage = usage or FakeUsage()


class _StreamCM:
    def __init__(self, final):
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(["Відповідь ", "з архіву [1]"])

    def get_final_message(self):
        return self._final


class _FakeClient:
    def __init__(self, final):
        self._final = final

    def with_options(self, **kw):
        return self

    @property
    def messages(self):
        return self

    def stream(self, **kwargs):
        return _StreamCM(self._final)


CHUNKS = [{"transcription_id": 7, "source_name": "Дзвінок про фонд",
           "source_type": "file", "text": "Домовились про 12k",
           "meeting_date": "2026-09-01", "speaker": "Андрій"}]


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "ask_log.db")
    init_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
                 "VALUES (7, 'file', 'Дзвінок про фонд', 'Домовились про 12k')")
    conn.execute("INSERT INTO transcriptions (id, source_type, source_name, transcript_text) "
                 "VALUES (8, 'telegram', 'Чат фонду', 'повідомлення')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def fake_claude(monkeypatch):
    """`retrieval` + Anthropic-клієнт офлайн; повертає фінальне повідомлення."""
    final = FakeFinal()
    monkeypatch.setattr(rag.retrieval, "search",
                        lambda db_path, q, **kw: {"chunks": list(CHUNKS), "vector_available": True})
    monkeypatch.setattr(rag.retrieval, "attach_thread_context", lambda db_path, chunks: chunks)
    monkeypatch.setattr(rag.retrieval, "attach_comments", lambda db_path, chunks: [])
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: _FakeClient(final))
    monkeypatch.setattr(rag.text_polishing, "_stream_with_retry",
                        lambda client, timeout, kwargs, what=None: final)
    return final


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM ask_log ORDER BY id").fetchall()
    finally:
        conn.close()


# ============================================================
# Запис рядка
# ============================================================

def test_answer_question_writes_one_row_with_all_fields(db, fake_claude):
    res = rag.answer_question(db, "Скільки домовились по фонду?", top_k=5,
                              category_id=3, project="Фонд")
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert res["ask_id"] == row["id"]
    assert row["channel"] == "ui"
    assert row["question"] == "Скільки домовились по фонду?"
    assert json.loads(row["scope_json"]) == {"category_id": 3, "project": "Фонд"}
    assert row["k"] == 5
    assert row["model"] == models_mod.HAIKU_4_5
    assert json.loads(row["source_ids_json"]) == [7]
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (1000, 200, 500)
    assert row["answer"].startswith("Відповідь")
    assert row["rating"] is None and row["note"] is None and row["rated_at"] is None


def test_stream_writes_row_and_done_carries_ask_id(db, fake_claude):
    frames = list(rag.answer_question_stream(db, "Що по фонду?", top_k=4, channel="mcp"))
    done = [f for f in frames if f.startswith("event: done")]
    assert len(done) == 1
    payload = json.loads(done[0].split("data: ", 1)[1].strip())
    rows = _rows(db)
    assert len(rows) == 1
    assert payload["ask_id"] == rows[0]["id"]
    assert rows[0]["channel"] == "mcp"
    assert rows[0]["k"] == 4
    assert rows[0]["answer"] == "Відповідь з архіву [1]"


def test_claude_failure_writes_no_row(db, monkeypatch):
    monkeypatch.setattr(rag.retrieval, "search",
                        lambda db_path, q, **kw: {"chunks": list(CHUNKS), "vector_available": True})
    monkeypatch.setattr(rag.retrieval, "attach_thread_context", lambda db_path, chunks: chunks)
    monkeypatch.setattr(rag.retrieval, "attach_comments", lambda db_path, chunks: [])

    class _Boom(Exception):
        status_code = 400

    class _FailClient(_FakeClient):
        def stream(self, **kwargs):
            raise _Boom("нема звʼязку")

    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: _FailClient(None))
    frames = list(rag.answer_question_stream(db, "питання"))
    assert any(f.startswith("event: error") for f in frames)
    assert _rows(db) == []


def test_empty_search_result_writes_no_row(db, monkeypatch):
    monkeypatch.setattr(rag.retrieval, "search",
                        lambda db_path, q, **kw: {"chunks": [], "vector_available": True})
    monkeypatch.setattr(rag.retrieval, "attach_thread_context", lambda db_path, chunks: chunks)
    res = rag.answer_question(db, "питання без відповіді")
    assert res["found"] == 0
    assert _rows(db) == []


def test_missing_table_does_not_break_the_answer(fake_claude, tmp_path):
    """Лог не має права зламати відповідь, за яку вже заплачено."""
    bare = str(tmp_path / "no_migrations.db")
    sqlite3.connect(bare).close()
    res = rag.answer_question(bare, "питання")
    assert res["answer"].startswith("Відповідь")
    assert res["ask_id"] is None


# ============================================================
# Вартість
# ============================================================

def test_cost_uses_pricing_table_and_charges_cache_at_tenth(db, fake_claude):
    rag.answer_question(db, "питання")
    # Haiku 4.5: $1/$5 за MTok, кеш-читання 0.1× input
    expected = (1000 * 1.0 + 500 * 0.1 + 200 * 5.0) / 1_000_000
    assert _rows(db)[0]["cost_usd"] == pytest.approx(expected)


def test_cost_is_null_for_unknown_model(db, fake_claude):
    fake_claude.model = "claude-невідома-модель"
    rag.answer_question(db, "питання")
    assert _rows(db)[0]["cost_usd"] is None


# ============================================================
# HTTP: channel + rate
# ============================================================

@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(enrichment, "is_available", lambda: True)
    app = Flask(__name__)
    app.config['DATABASE'] = db
    app.register_blueprint(memory_bp)
    return app.test_client()


def test_ask_defaults_to_ui_channel(client, db, fake_claude):
    r = client.post('/api/memory/ask', json={"question": "Що по фонду?"})
    assert r.status_code == 200
    assert r.get_json()["ask_id"] == _rows(db)[0]["id"]
    assert _rows(db)[0]["channel"] == "ui"


def test_ask_accepts_mcp_channel(client, db, fake_claude):
    client.post('/api/memory/ask', json={"question": "Що по фонду?", "channel": "mcp"})
    assert _rows(db)[0]["channel"] == "mcp"


@pytest.mark.parametrize("url", ['/api/memory/ask', '/api/memory/ask/stream'])
def test_unknown_channel_is_400(client, db, fake_claude, url):
    r = client.post(url, json={"question": "Що по фонду?", "channel": "telegram"})
    assert r.status_code == 400
    assert _rows(db) == []


def test_rate_updates_row(client, db, fake_claude):
    ask_id = client.post('/api/memory/ask', json={"question": "Що по фонду?"}).get_json()["ask_id"]
    r = client.post(f'/api/memory/ask/{ask_id}/rate', json={"rating": -1, "note": "не те джерело"})
    assert r.status_code == 200 and r.get_json()["ask_id"] == ask_id
    row = _rows(db)[0]
    assert row["rating"] == -1 and row["note"] == "не те джерело" and row["rated_at"]


def test_rate_can_be_changed(client, db, fake_claude):
    ask_id = client.post('/api/memory/ask', json={"question": "Що по фонду?"}).get_json()["ask_id"]
    client.post(f'/api/memory/ask/{ask_id}/rate', json={"rating": -1})
    client.post(f'/api/memory/ask/{ask_id}/rate', json={"rating": 1})
    assert _rows(db)[0]["rating"] == 1


def test_rate_unknown_id_is_404(client):
    assert client.post('/api/memory/ask/999/rate', json={"rating": 1}).status_code == 404


@pytest.mark.parametrize("rating", [0, 2, "добре", None])
def test_rate_out_of_range_is_400(client, db, fake_claude, rating):
    ask_id = client.post('/api/memory/ask', json={"question": "Що по фонду?"}).get_json()["ask_id"]
    r = client.post(f'/api/memory/ask/{ask_id}/rate', json={"rating": rating})
    assert r.status_code == 400
    assert _rows(db)[0]["rating"] is None


# ============================================================
# build_golden --from-ask-log
# ============================================================

def _seed_ask_log(db_path, items):
    conn = sqlite3.connect(db_path)
    for question, rating, source_ids, note in items:
        conn.execute("INSERT INTO ask_log (channel, question, scope_json, k, model, "
                     "source_ids_json, answer, rating, note) VALUES ('ui',?,?,8,'m',?,'відповідь',?,?)",
                     (question, json.dumps({"category_id": 3, "project": None}),
                      json.dumps(source_ids), rating, note))
    conn.commit()
    conn.close()


def test_mine_ask_log_puts_thumbs_down_first_and_dedupes(db):
    _seed_ask_log(db, [
        ("питання А", 1, [7], None),
        ("питання Б", -1, [8], "мимо"),
        ("  ПИТАННЯ А  ", None, [7], None),   # дубль нормалізовано
        ("питання В", None, [], None),
    ])
    mined = build_golden.mine_ask_log(db)
    assert [m["question"] for m in mined] == ["питання Б", "питання А", "питання В"]
    assert mined[0]["source"] == "ask_log"
    assert mined[0]["category_id"] == 3
    assert mined[0]["source_ids"] == [8]
    assert "мимо" in mined[0]["notes"]


def test_build_golden_from_ask_log_adds_unlabeled_items_without_mcp_duplicates(db, tmp_path, capsys):
    _seed_ask_log(db, [("той самий текст питання", None, [7], None),
                       ("нове питання з UI", -1, [8], None)])
    log = tmp_path / "mcp_calls.log"
    # той самий текст іншим регістром — дедуп нормалізований, як у mine_queries
    log.write_text('10:00:00 →   START ask_archive args={"question": "Той Самий Текст Питання"}\n',
                   encoding="utf-8")
    out = tmp_path / "golden.jsonl"

    rc = build_golden.main(["--db", db, "--mcp-log", str(log), "--out", str(out),
                            "--from-ask-log", "--stats"])
    assert rc == 0
    items = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    questions = [it["question"] for it in items]
    # питання з обох джерел лишилось одне — і це версія з mcp-логу (він перший)
    same = [it for it in items if it["question"].lower() == "той самий текст питання"]
    assert len(same) == 1 and same[0]["source"] == "mcp_log"
    assert "нове питання з UI" in questions
    ask_items = [it for it in items if it["source"] == "ask_log"]
    assert len(ask_items) == 1
    it = ask_items[0]
    assert it["status"] == "unlabeled" and it["expected_transcription_ids"] == []
    assert [c["transcription_id"] for c in it["candidates"]] == [8]
    assert it["slice"] == "tg"          # джерело — telegram-запис #8
    assert len({i["id"] for i in items}) == len(items)
