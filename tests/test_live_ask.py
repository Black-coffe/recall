"""«Question → local model» (Story 02, mcp-live-call), offline.

Two layers: `app/services/live_ask.py` (the LLM/search are injected — without Ollama/torch)
and the HTTP contract `POST /api/copilot/live-ask` (a light Flask app with a single
blueprint, `live_ask.ask_local` is mocked — without a real DB/torch/Ollama).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

from app.blueprints.copilot import copilot_bp
from app.services import live_ask


CHUNK = {
    "chunk_id": 5, "transcription_id": 7, "source_name": "Дзвінок з клієнтом",
    "meeting_date": "2026-05-14", "text": "домовились про дедлайн у пʼятницю " * 20,
}


class _FakeLLM:
    def __init__(self, ok=True, reason="OK (fake)", answer="Відповідь моделі",
                model="qwen-fake", raise_on_generate=None):
        self.ok = ok
        self.reason = reason
        self.answer = answer
        self.model = model
        self.raise_on_generate = raise_on_generate
        self.prompts: list[str] = []

    def availability(self):
        return self.ok, self.reason

    def generate(self, prompt, **kw):
        self.prompts.append(prompt)
        self.generate_kwargs = kw
        if self.raise_on_generate:
            raise self.raise_on_generate
        return {"response": self.answer, "model": self.model}


def _search_fn(chunks, raise_exc=None):
    calls = []

    def fn(db_path, query, top_k=6, rerank=False):
        calls.append((db_path, query, top_k, rerank))
        if raise_exc:
            raise raise_exc
        return {"query": query, "chunks": chunks, "vector_available": True}

    fn.calls = calls
    return fn


# ------------------------------------------------------------- ask_local


def test_scope_call_ignores_archive():
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="живий текст дзвінка",
                             scope="call", llm=llm, search_fn=search)
    assert out["available"] is True
    assert out["answer"] == "Відповідь моделі"
    assert out["sources"] == []
    assert out["used_transcript_chars"] > 0
    assert not search.calls  # C2/acceptance: scope='call' does not touch the archive


def test_scope_archive_ignores_transcript():
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="живий текст дзвінка",
                             scope="archive", llm=llm, search_fn=search)
    assert out["available"] is True
    assert out["used_transcript_chars"] == 0
    assert len(out["sources"]) == 1
    assert out["sources"][0] == {
        "chunk_id": 5, "transcription_id": 7, "title": "Дзвінок з клієнтом",
        "date": "2026-05-14", "snippet": CHUNK["text"][:400].rstrip() + "…",
    }
    assert search.calls  # scope='archive' MUST touch the archive


def test_scope_both_uses_transcript_and_archive():
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="живий текст дзвінка",
                             scope="both", llm=llm, search_fn=search)
    assert out["available"] is True
    assert out["used_transcript_chars"] > 0
    assert len(out["sources"]) == 1
    assert search.calls
    # both sources really got into the model prompt
    prompt = llm.prompts[0]
    assert "живий текст дзвінка" in prompt
    assert "Дзвінок з клієнтом" in prompt


def test_ollama_unavailable_returns_200_shaped_result_not_raises():
    search = _search_fn([CHUNK])
    llm = _FakeLLM(ok=False, reason="Ollama недоступний на http://localhost:11434")
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="живий текст",
                             scope="both", llm=llm, search_fn=search)
    assert out["available"] is False
    assert out["reason"] == "Ollama недоступний на http://localhost:11434"
    assert out["answer"] == ""
    assert not llm.prompts  # generate() was not called


def test_generate_failure_degrades_not_raises():
    search = _search_fn([CHUNK])
    llm = _FakeLLM(raise_on_generate=RuntimeError("connection reset"))
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="живий текст",
                             scope="both", llm=llm, search_fn=search)
    assert out["available"] is False
    assert "connection reset" in out["reason"]


def test_no_active_session_scope_call_gives_understandable_reason():
    """session_id=null (no active session) → transcript_text=None → a clear
    reason, not an exception/an empty answer without an explanation."""
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text=None,
                             scope="call", llm=llm, search_fn=search)
    assert out["available"] is False
    assert out["reason"]
    assert not search.calls
    assert not llm.prompts


def test_scope_archive_no_hits_gives_reason_not_exception():
    search = _search_fn([])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "щось незрозуміле", transcript_text=None,
                             scope="archive", llm=llm, search_fn=search)
    assert out["available"] is False
    assert out["reason"]
    assert out["sources"] == []


def test_archive_search_exception_degrades():
    search = _search_fn([], raise_exc=RuntimeError("no embeddings"))
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text=None,
                             scope="archive", llm=llm, search_fn=search)
    assert out["available"] is False


def test_unknown_scope_falls_back_to_both():
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="текст",
                             scope="nonsense", llm=llm, search_fn=search)
    assert out["scope"] == "both"
    assert search.calls


def test_generation_bounded_below_dispatcher_tick_budget():
    """A10 guardrail: the generation lock is shared with the copilot ticks — the budget
    of ask_local() must be deliberately shorter than the accepted tick call of the
    dispatcher (max_tokens=1024, dispatcher.py)."""
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="текст",
                       scope="both", llm=llm, search_fn=search)
    assert llm.generate_kwargs["max_tokens"] == live_ask._MAX_ANSWER_TOKENS
    assert llm.generate_kwargs["max_tokens"] < 1024
    assert llm.generate_kwargs["max_tokens"] <= 1024 / 2


def test_top_k_clamped_to_upper_bound():
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text=None,
                       scope="archive", top_k=9999, llm=llm, search_fn=search)
    assert search.calls[0][2] == live_ask._MAX_TOP_K


def test_top_k_infinite_does_not_raise():
    """`int(float('inf'))` raises OverflowError without this fallback — a direct
    call of ask_local() (e.g. from MCP) must not fail even if the HTTP layer
    let an infinite value through."""
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text=None,
                             scope="archive", top_k=float("inf"), llm=llm, search_fn=search)
    assert out["available"] is True
    assert search.calls[0][2] <= live_ask._MAX_TOP_K


def test_recording_active_never_calls_llm():
    """C1/acceptance of Story 08: during an active recording `llm` gets
    no call at all (neither availability() nor generate()) — the generation lock is
    not taken even for a moment."""
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="живий текст дзвінка",
                             scope="both", llm=llm, search_fn=search, recording_active=True)
    assert out["mode"] == "raw"
    assert out["available"] is True
    assert out["answer"] == ""
    assert out["model"] == ""
    assert out["transcript_text"] == "живий текст дзвінка"
    assert len(out["sources"]) == 1
    assert not llm.prompts  # generate() was not called


def test_recording_active_llm_availability_not_called():
    """We prove separately that even `availability()` is not called — not only
    generate()."""
    search = _search_fn([CHUNK])

    class _SpyLLM(_FakeLLM):
        def availability(self):
            raise AssertionError("availability() must not be called in raw mode")

    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="живий текст",
                             scope="both", llm=_SpyLLM(), search_fn=search, recording_active=True)
    assert out["mode"] == "raw"


def test_recording_active_still_runs_retrieval():
    """Retrieval (e5) keeps working in raw mode — only the generation
    is cut."""
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text=None,
                             scope="archive", llm=llm, search_fn=search, recording_active=True)
    assert out["mode"] == "raw"
    assert search.calls
    assert len(out["sources"]) == 1


def test_recording_active_empty_material_gives_reason():
    search = _search_fn([])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text=None,
                             scope="archive", llm=llm, search_fn=search, recording_active=True)
    assert out["mode"] == "raw"
    assert out["available"] is False
    assert out["reason"]


def test_recording_inactive_keeps_generated_mode():
    """By default (recording_active=False, the default) the behaviour has not
    changed — the full answer of the local model, mode="generated"."""
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    out = live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text="текст",
                             scope="both", llm=llm, search_fn=search)
    assert out["mode"] == "generated"
    assert out["answer"] == "Відповідь моделі"
    assert llm.prompts


def test_rerank_matches_env_flag(monkeypatch):
    """The divergence from ask_archive (rag.py::_RERANK_ENABLED) is removed — the same
    env var controls rerank here too."""
    search = _search_fn([CHUNK])
    llm = _FakeLLM()
    monkeypatch.setattr(live_ask, "_RERANK_ENABLED", True)
    live_ask.ask_local("db.sqlite", "що вирішили?", transcript_text=None,
                       scope="archive", llm=llm, search_fn=search)
    assert search.calls[0][3] is True


# ------------------------------------------------------------- HTTP contract


@pytest.fixture
def app_client(monkeypatch):
    app = Flask(__name__)
    app.config['DATABASE'] = 'db.sqlite'
    app.register_blueprint(copilot_bp)
    return app.test_client()


def _patch_ask_local(monkeypatch, captured: dict):
    def fake(db_path, question, *, transcript_text=None, scope='both', top_k=6,
             recording_active=False, **_):
        captured['db_path'] = db_path
        captured['question'] = question
        captured['transcript_text'] = transcript_text
        captured['scope'] = scope
        captured['top_k'] = top_k
        captured['recording_active'] = recording_active
        return {"answer": "ок", "model": "qwen-fake", "scope": scope,
                "used_transcript_chars": len(transcript_text or ""),
                "sources": [], "available": True, "reason": None,
                "mode": "raw" if recording_active else "generated"}
    monkeypatch.setattr(live_ask, "ask_local", fake)


def test_empty_question_is_400(app_client):
    r = app_client.post('/api/copilot/live-ask', json={"question": "  "})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "EMPTY_QUESTION"


def test_endpoint_resolves_active_session_when_none_given(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)
    monkeypatch.setattr("app.state.recording_service",
                        SimpleNamespace(active_session_id="sid-42"))
    monkeypatch.setattr("app.state.live_transcribe_worker", None)  # the worker is off

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "що там?", "scope": "call"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert body["session_id"] == "sid-42"
    assert captured["transcript_text"] is None  # no worker → None, not an error


def test_endpoint_no_active_session_scope_call_not_500(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)
    monkeypatch.setattr("app.state.recording_service", SimpleNamespace(active_session_id=None))
    monkeypatch.setattr("app.state.live_transcribe_worker", None)

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "що там?", "scope": "call", "session_id": None})
    assert r.status_code == 200
    body = r.get_json()
    assert body["session_id"] is None
    assert captured["transcript_text"] is None


def test_endpoint_builds_transcript_from_live_worker(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)

    class _Worker:
        def is_active(self, session_id):
            return session_id == "sid-1"

        def get_preview(self, session_id):
            return [{"start": 1.0, "text": "перше", "speaker": "other"},
                    {"start": 0.0, "text": "нульове", "speaker": "self"}]

    monkeypatch.setattr("app.state.recording_service", SimpleNamespace(active_session_id="sid-1"))
    monkeypatch.setattr("app.state.live_transcribe_worker", _Worker())

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "що там?", "scope": "call", "session_id": "sid-1"})
    assert r.status_code == 200
    assert "перше" in captured["transcript_text"]
    assert "нульове" in captured["transcript_text"]
    # C3/acceptance: the transcript distinguishes the operator from the other party, rather than
    # simply concatenating the segment text without attribution.
    assert "[Співрозмовник] перше" in captured["transcript_text"]
    assert "[Оператор] нульове" in captured["transcript_text"]


def test_endpoint_scope_archive_never_touches_live_worker(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)

    class _ExplodingWorker:
        def is_active(self, session_id):
            raise AssertionError("scope='archive' must not touch the live worker")

    monkeypatch.setattr("app.state.recording_service", SimpleNamespace(active_session_id="sid-1"))
    monkeypatch.setattr("app.state.live_transcribe_worker", _ExplodingWorker())

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "що в архіві?", "scope": "archive"})
    assert r.status_code == 200
    assert captured["transcript_text"] is None


def test_endpoint_defaults_top_k_and_scope(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)
    monkeypatch.setattr("app.state.recording_service", None)
    monkeypatch.setattr("app.state.live_transcribe_worker", None)

    r = app_client.post('/api/copilot/live-ask', json={"question": "питання"})
    assert r.status_code == 200
    assert captured["scope"] == "both"
    assert captured["top_k"] == 6


def test_endpoint_top_k_infinite_is_400_not_500(monkeypatch, app_client):
    """Flask/JSON accepts ``Infinity``; `int(float('inf'))` raises OverflowError —
    without validation the request fell into a 500."""
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "питання", "top_k": float("inf")})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "INVALID_TOP_K"


def test_endpoint_top_k_non_numeric_is_400(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "питання", "top_k": "багато"})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "INVALID_TOP_K"


def test_endpoint_recording_active_derived_from_state(monkeypatch, app_client):
    """recording_active is passed to ask_local ONLY from the server state
    (`state.recording_service.active_session_id`), not from the request body."""
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)
    monkeypatch.setattr("app.state.recording_service", SimpleNamespace(active_session_id="sid-1"))
    monkeypatch.setattr("app.state.live_transcribe_worker", None)

    r = app_client.post('/api/copilot/live-ask', json={"question": "що там?", "scope": "archive"})
    assert r.status_code == 200
    assert captured["recording_active"] is True
    assert r.get_json()["mode"] == "raw"


def test_endpoint_recording_inactive_derived_from_state(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)
    monkeypatch.setattr("app.state.recording_service", SimpleNamespace(active_session_id=None))
    monkeypatch.setattr("app.state.live_transcribe_worker", None)

    r = app_client.post('/api/copilot/live-ask', json={"question": "що там?", "scope": "archive"})
    assert r.status_code == 200
    assert captured["recording_active"] is False
    assert r.get_json()["mode"] == "generated"


def test_endpoint_ignores_client_supplied_recording_active_param(monkeypatch, app_client):
    """The agent has no way to force the generated mode during a recording
    even by slipping its own field into the request body — the server ignores it."""
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)
    monkeypatch.setattr("app.state.recording_service", SimpleNamespace(active_session_id="sid-1"))
    monkeypatch.setattr("app.state.live_transcribe_worker", None)

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "що там?", "scope": "archive", "recording_active": False})
    assert r.status_code == 200
    assert captured["recording_active"] is True  # the body is ignored, taken from the server state


def test_endpoint_top_k_clamped_to_upper_bound(monkeypatch, app_client):
    captured: dict = {}
    _patch_ask_local(monkeypatch, captured)
    monkeypatch.setattr("app.state.recording_service", None)
    monkeypatch.setattr("app.state.live_transcribe_worker", None)

    r = app_client.post('/api/copilot/live-ask',
                        json={"question": "питання", "top_k": 9999})
    assert r.status_code == 200
    assert captured["top_k"] == 20
