"""`explain` наскрізь: HTTP → retrieval.search → rag → sources (Історія 05).

Перевіряємо не саму форму `why` (це контракт Історії 02, `retrieval.py` не
наш файл) — перевіряємо, що прапорець доїжджає до `retrieval.search` з
блупринта і що `why`, який `retrieval.search` поклав у чанк, не губиться на
шляху до `sources` відповіді `ask` (order_citables / attach_thread_context).
"""
import sqlite3

import numpy as np
import pytest
from flask import Flask

from app.blueprints.memory import memory_bp
from app.db.migrations import init_database
from app.services import embeddings, retrieval as retrieval_real


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "explain.db")
    init_database(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO transcriptions (id, source_type, source_name, meeting_date, created_at) "
        "VALUES (1, 'recording', 'Зустріч', '2026-05-14', '2026-05-14 10:00:00')")
    # Закріплений коментар до цього ж запису — без нього `attach_comments`
    # (rag.answer_question) завжди повертає порожній список і гілка
    # "приєднаний коментар без `why`", заради якої історія 08, ніколи не
    # виконується (знахідка рев'ю story 05).
    conn.execute(
        "INSERT INTO comments (target_type, target_id, body, kind, pinned) "
        "VALUES ('transcription', 1, 'Насправді домовились на іншу дату', 'correction', 1)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(db):
    app = Flask(__name__)
    app.config["DATABASE"] = db
    app.register_blueprint(memory_bp)
    return app.test_client()


def _fake_chunk(explain: bool, rerank: bool = False) -> dict:
    """Мінімальна форма чанка з `why`, як її віддає `retrieval.search`
    (Історія 02): компактний `why` завжди, `stages` лише при explain=True,
    `rr` лише коли rerank реально скорував кандидата (Історія 13, Minor 4)."""
    why = {"src": "transcript", "rrf": 0.5, "rec": 0.0, "by": ["fts"], "top": "rrf"}
    if rerank:
        why["rr"] = 0.9
    if explain:
        why["stages"] = {"fts": {"pos": 0, "bm25": 1.2}}
        why["weights"] = {"recency": 0.1, "comment": 0.1}
        why["final_raw"] = 0.5
        why["search_capped"] = {"comment_share": False, "diversity": False}
        why["rewrites"] = []
    return {
        "chunk_id": 1, "transcription_id": 1, "source_name": "Зустріч",
        "source_type": "transcript", "meeting_date": "2026-05-14",
        "speaker": None, "start_time": None, "end_time": None,
        "text": "фрагмент тексту", "score": 0.5, "matched_by": ["fts"],
        "why": why,
    }


def _patch_search(monkeypatch, captured: dict):
    def fake_search(db_path, query, top_k=8, explain=False, **kwargs):
        captured["explain"] = explain
        return {"query": query, "chunks": [_fake_chunk(explain)], "vector_available": False}
    monkeypatch.setattr("app.services.retrieval.search", fake_search)


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return a / n if n else a


def test_local_fake_why_keys_match_real_retrieval_search(monkeypatch, tmp_path):
    """Дрейф тест-фейка (памʼятка `graceful-degradation-hides-test-fake-drift`):
    `_fake_chunk` вище — ручний фейк `retrieval.search`, і він розійшовся зі
    своїм виробником (`why["capped"]` замість перейменованого історією 09
    `why["search_capped"]`), поки ЦЕЙ тест не порівнював набори ключів. Тут
    звіряємо набір ключів `why` фейка з набором ключів справжнього виклику
    `retrieval.search`, щоб наступне перейменування/додавання ключа в
    продакшн-коді ловилось автоматично, а не оком."""
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_query", lambda q: _unit([1.0, 0.0, 0.0, 0.0]))
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))

    path = str(tmp_path / "real.db")
    init_database(path)
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, meeting_date) "
        "VALUES ('file', 'Дзвінок', 'x', '2026-08-01')")
    tid = cur.lastrowid
    blob = _unit([1.0, 0.0, 0.0, 0.0]).tobytes()
    # Два чанки — `_apply_rerank` пропускає пул при len(ranked_ids) <= 1, тож
    # для форми з `rerank=True` (Minor 4) потрібен хоча б один сусід, інакше
    # `why["rr"]` ніколи не зʼявиться і паритет з фейком перевіряється не на
    # тій формі, яку заявляє тест.
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text, embedding) VALUES (?, 0, 0, 10, 'Ви', 'бюджет проєкту', ?)",
        (tid, blob))
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text, embedding) VALUES (?, 1, 10, 20, 'Ви', 'інший бюджет', ?)",
        (tid, blob))
    conn.commit()
    conn.close()

    for explain in (False, True):
        real = retrieval_real.search(path, "бюджет проєкту", top_k=5, explain=explain)
        real_keys = set(real["chunks"][0]["why"].keys())
        fake_keys = set(_fake_chunk(explain)["why"].keys())
        assert real_keys == fake_keys, (explain, real_keys, fake_keys)

    # Форма з reranker'ом (несе `rr`) — до цього паритет доводився лише на
    # rerank=False, де `rr` відсутній з обох боків і розбіжність не могла
    # проявитись (Історія 13, Minor 4).
    def _fake_rerank(query, candidates, text_key="text"):
        return [dict(c, rerank_score=0.9 - i * 0.1) for i, c in enumerate(candidates)]
    monkeypatch.setattr("app.services.retrieval.reranker.rerank", _fake_rerank)

    for explain in (False, True):
        real = retrieval_real.search(path, "бюджет проєкту", top_k=5, explain=explain,
                                     rerank=True)
        real_keys = set(real["chunks"][0]["why"].keys())
        assert "rr" in real_keys, "rerank=True мусив додати `rr` до `why`"
        fake_keys = set(_fake_chunk(explain, rerank=True)["why"].keys())
        assert real_keys == fake_keys, (explain, real_keys, fake_keys)


# ---------------------------------------------------------------------------
# GET /api/memory/search
# ---------------------------------------------------------------------------

def test_search_default_no_stages(client, monkeypatch):
    captured = {}
    _patch_search(monkeypatch, captured)
    r = client.get("/api/memory/search?q=щось")
    assert r.status_code == 200
    why = r.get_json()["chunks"][0]["why"]
    assert "stages" not in why
    assert captured["explain"] is False


def test_search_explain_1_has_stages(client, monkeypatch):
    captured = {}
    _patch_search(monkeypatch, captured)
    r = client.get("/api/memory/search?q=щось&explain=1")
    assert r.status_code == 200
    why = r.get_json()["chunks"][0]["why"]
    assert "stages" in why
    assert captured["explain"] is True


@pytest.mark.parametrize("val", ["", "0", "false", "False"])
def test_search_explain_unknown_value_is_false(client, monkeypatch, val):
    captured = {}
    _patch_search(monkeypatch, captured)
    r = client.get("/api/memory/search?q=щось&explain=%s" % val)
    assert r.status_code == 200
    assert captured["explain"] is False
    assert "stages" not in r.get_json()["chunks"][0]["why"]


# ---------------------------------------------------------------------------
# POST /api/memory/ask — `why` мусить дожити до `sources` крізь
# order_citables/attach_thread_context (rag.answer_question), не тільки
# крізь блупринт.
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0


class _ClaudeResult:
    def __init__(self):
        self.content = [_Block("відповідь [1]")]
        self.usage = _Usage()
        self.model = "claude-test"


def _patch_claude(monkeypatch):
    monkeypatch.setattr("app.services.text_polishing._get_client", lambda: object())
    monkeypatch.setattr("app.services.text_polishing._stream_with_retry",
                        lambda *a, **kw: _ClaudeResult())


def test_ask_explain_true_sources_have_stages(client, monkeypatch):
    captured = {}
    _patch_search(monkeypatch, captured)
    _patch_claude(monkeypatch)
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    r = client.post("/api/memory/ask", json={"question": "питання?", "explain": True})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    sources = data["sources"]
    # Приєднаний `correction`-коментар (fixture `db`) стає sources[0]
    # (order_citables ставить коментарі першими) — саме та позиція, де
    # `why` губився до ремонту.
    assert len(sources) >= 2
    found = [s for s in sources if s.get("source_type") != "comment" or not s.get("attached")]
    assert found and "stages" in found[0]["why"]
    assert captured["explain"] is True


def test_ask_default_sources_no_stages(client, monkeypatch):
    captured = {}
    _patch_search(monkeypatch, captured)
    _patch_claude(monkeypatch)
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    r = client.post("/api/memory/ask", json={"question": "питання?"})
    assert r.status_code == 200
    data = r.get_json()
    sources = data["sources"]
    found = [s for s in sources if s.get("source_type") != "comment" or not s.get("attached")]
    assert found and "stages" not in found[0]["why"]
    assert captured["explain"] is False


def test_ask_every_source_has_why_including_attached_comment(client, monkeypatch):
    """Знахідка рев'ю #1: приєднаний коментар (не крізь `retrieval.search`)
    стоїть sources[0] і раніше не мав `why` взагалі — перевіряємо УСІ
    елементи, а не лише перший знайдений. Історія 12 (D2): маркер мусить
    нести ТОЙ САМИЙ обовʼязковий набір ключів C2, що й знайдений результат
    (`src`, `rrf`, `rec`, `by`, `top`), інакше `why["rrf"]` ловить `KeyError`
    на цьому ж елементі рівнем глибше."""
    captured = {}
    _patch_search(monkeypatch, captured)
    _patch_claude(monkeypatch)
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    r = client.post("/api/memory/ask", json={"question": "питання?"})
    assert r.status_code == 200
    sources = r.get_json()["sources"]
    assert len(sources) >= 2
    required = {"src", "rrf", "rec", "by", "top"}
    for s in sources:
        assert "why" in s
        assert set(s["why"]) >= required
    attached = [s for s in sources if s.get("attached")]
    assert attached, "фікстура мусить давати хоча б один приєднаний коментар"
    assert attached[0]["why"].get("src") == "comment"
    assert attached[0]["why"]["rrf"] == 0.0
    assert attached[0]["why"]["rec"] == 0.0
    assert attached[0]["why"]["top"] == "attached"


@pytest.mark.parametrize("val", ["", 0, False, "false"])
def test_ask_unknown_explain_is_false(client, monkeypatch, val):
    captured = {}
    _patch_search(monkeypatch, captured)
    _patch_claude(monkeypatch)
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    r = client.post("/api/memory/ask", json={"question": "питання?", "explain": val})
    assert r.status_code == 200
    assert captured["explain"] is False


# ---------------------------------------------------------------------------
# POST /api/memory/ask/stream — знахідка рев'ю #3: прапорець там прокидається
# коректно, але жоден тест цього не доводив (контракт називав три ендпоінти,
# тест покривав два).
# ---------------------------------------------------------------------------

def _sse_events(raw: str) -> list[tuple[str, dict]]:
    import json as _json
    events = []
    for frame in raw.split("\n\n"):
        if not frame.strip():
            continue
        event, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = _json.loads(line[len("data: "):])
        if event:
            events.append((event, data))
    return events


def test_ask_stream_explain_true_passes_through_and_sources_have_why(client, monkeypatch):
    captured = {}
    _patch_search(monkeypatch, captured)
    _patch_claude(monkeypatch)
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    r = client.post("/api/memory/ask/stream", json={"question": "питання?", "explain": True})
    assert r.status_code == 200
    events = _sse_events(r.get_data(as_text=True))
    assert captured["explain"] is True
    sources_events = [d for ev, d in events if ev == "sources"]
    assert sources_events
    sources = sources_events[0]["sources"]
    assert len(sources) >= 2
    for s in sources:
        assert "why" in s
    found = [s for s in sources if not s.get("attached")]
    attached = [s for s in sources if s.get("attached")]
    assert found and "stages" in found[0]["why"]
    assert attached and attached[0]["why"].get("src") == "comment"


def test_ask_stream_default_explain_false(client, monkeypatch):
    captured = {}
    _patch_search(monkeypatch, captured)
    _patch_claude(monkeypatch)
    monkeypatch.setattr("app.blueprints.memory.enrichment.is_available", lambda: True)
    r = client.post("/api/memory/ask/stream", json={"question": "питання?"})
    assert r.status_code == 200
    events = _sse_events(r.get_data(as_text=True))
    assert captured["explain"] is False
    sources = [d for ev, d in events if ev == "sources"][0]["sources"]
    found = [s for s in sources if not s.get("attached")]
    assert found and "stages" not in found[0]["why"]
