"""`explain` end to end: HTTP -> retrieval.search -> rag -> sources (Story 05).

We check not the shape of `why` itself (that is the Story 02 contract, `retrieval.py` is not
our file) -- we check that the flag reaches `retrieval.search` from the
blueprint and that the `why` that `retrieval.search` put into a chunk is not lost on
the way to the `sources` of the `ask` answer (order_citables / attach_thread_context).
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
    # A pinned comment on this same record -- without it `attach_comments`
    # (rag.answer_question) always returns an empty list and the branch
    # "attached comment without `why`", for which story 08 was made, is never
    # executed (review finding of story 05).
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
    """A minimal chunk shape with `why`, as `retrieval.search` returns it
    (Story 02): a compact `why` always, `stages` only with explain=True,
    `rr` only when rerank really scored the candidate (Story 13, Minor 4)."""
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
    """Test fake drift (memo `graceful-degradation-hides-test-fake-drift`):
    `_fake_chunk` above is a hand-made fake of `retrieval.search`, and it diverged from
    its producer (`why["capped"]` instead of `why["search_capped"]` renamed by story 09)
    while THIS test did not compare the key sets. Here
    we compare the key set of the fake's `why` with the key set of the real call
    of `retrieval.search`, so that the next rename/addition of a key in
    production code is caught automatically, not by eye."""
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
    # Two chunks -- `_apply_rerank` skips the pool at len(ranked_ids) <= 1, so
    # for the `rerank=True` shape (Minor 4) at least one neighbour is needed, otherwise
    # `why["rr"]` never appears and the parity with the fake is checked not on
    # the shape the test claims.
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

    # The shape with the reranker (carries `rr`) -- until now parity was proven only on
    # rerank=False, where `rr` is absent on both sides and a divergence could not
    # show up (Story 13, Minor 4).
    def _fake_rerank(query, candidates, text_key="text"):
        return [dict(c, rerank_score=0.9 - i * 0.1) for i, c in enumerate(candidates)]
    monkeypatch.setattr("app.services.retrieval.reranker.rerank", _fake_rerank)

    for explain in (False, True):
        real = retrieval_real.search(path, "бюджет проєкту", top_k=5, explain=explain,
                                     rerank=True)
        real_keys = set(real["chunks"][0]["why"].keys())
        assert "rr" in real_keys, "rerank=True must have added `rr` to `why`"
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
# POST /api/memory/ask -- `why` must survive to `sources` through
# order_citables/attach_thread_context (rag.answer_question), not only
# through the blueprint.
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
    # The attached `correction` comment (fixture `db`) becomes sources[0]
    # (order_citables puts comments first) -- exactly the position where
    # `why` was lost before the repair.
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
    """Review finding #1: the attached comment (not through `retrieval.search`)
    stands at sources[0] and used to have no `why` at all -- we check ALL
    elements, not only the first found. Story 12 (D2): the marker must
    carry THE SAME required set of C2 keys as a found result
    (`src`, `rrf`, `rec`, `by`, `top`), otherwise `why["rrf"]` catches a `KeyError`
    on this same element one level deeper."""
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
    assert attached, "the fixture must give at least one attached comment"
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
# POST /api/memory/ask/stream -- review finding #3: the flag is passed through there
# correctly, but no test proved it (the contract named three endpoints,
# the test covered two).
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
