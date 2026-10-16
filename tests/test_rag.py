"""Unit tests for the "Ask the archive" RAG (app/services/rag.py) -- REMEDIATION_PLAN T7.5.

Offline: no real Anthropic API call. The Anthropic client and
retrieval.search are replaced by fakes. Covers:
  - _fmt_time / _build_context (the deterministic chunk context formatter)
  - _build_request_kwargs (building the payload + adaptive thinking)
  - answer_question (non-streaming answer, mock of _get_client/_stream_with_retry)
  - answer_question_stream (SSE frames, including a retry on a transient
    error BEFORE the first delta and no retry AFTER the first delta -- T6.3)
"""
from __future__ import annotations

import pytest

from app.services import rag
from app.services import models as models_mod


# ============================================================
# Fakes
# ============================================================

class FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=5, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _Block:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class FakeFinalMessage:
    def __init__(self, text_blocks, model="claude-test", usage=None, extra_blocks=None):
        self.content = [_Block("text", t) for t in text_blocks]
        if extra_blocks:
            self.content = extra_blocks + self.content
        self.model = model
        self.usage = usage or FakeUsage()


class _Transient(Exception):
    """Simulates a transient error (5xx) without a real anthropic SDK object."""
    status_code = 529


class _NonRetryable(Exception):
    status_code = 400


class ScriptedStreamCM:
    """One call of client.messages.stream(**kwargs) -- a context manager."""

    def __init__(self, spec):
        self.spec = spec

    def __enter__(self):
        if self.spec.get("error_at") == "enter":
            raise self.spec["error"]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def text_stream(self):
        chunks = self.spec.get("chunks", [])
        error_after = self.spec.get("error_after_chunk")

        def gen():
            for i, c in enumerate(chunks):
                yield c
                if error_after is not None and i == error_after:
                    raise self.spec["error"]
        return gen()

    def get_final_message(self):
        return self.spec["final"]


class FakeMessagesAPI:
    def __init__(self, specs):
        self._specs = list(specs)
        self.calls = 0

    def stream(self, **kwargs):
        spec = self._specs[min(self.calls, len(self._specs) - 1)]
        self.calls += 1
        self.last_kwargs = kwargs
        return ScriptedStreamCM(spec)


class FakeClient:
    """client.with_options(timeout=...).messages.stream(**kwargs)."""

    def __init__(self, specs):
        self.messages = FakeMessagesAPI(specs)

    def with_options(self, **kw):
        return self


def _no_sleep(monkeypatch):
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)


def _sample_chunks():
    return [
        {
            "source_type": "meeting", "source_name": "Дзвінок з клієнтом",
            "meeting_date": "2026-05-12", "speaker": "Андрій", "start_time": 65,
            "text": "Обговорили бюджет проєкту.",
        },
        {
            "source_type": "document", "source_name": "Кошторис.pdf",
            "doc_type": "pdf", "page": 3, "section": "Витрати",
            "text": "Загальний бюджет — 40000 грн.",
        },
    ]


# ============================================================
# _fmt_time
# ============================================================

def test_fmt_time_none_returns_empty():
    assert rag._fmt_time(None) == ""


def test_fmt_time_formats_mmss():
    assert rag._fmt_time(65) == "01:05"
    assert rag._fmt_time(0) == "00:00"
    assert rag._fmt_time(3661) == "61:01"


# ============================================================
# _build_context
# ============================================================

def test_build_context_meeting_chunk_full_header():
    ctx = rag._build_context([_sample_chunks()[0]])
    assert ctx.startswith("[1] Мітинг «Дзвінок з клієнтом» (2026-05-12), спікер: Андрій, ~01:05")
    assert "Обговорили бюджет проєкту." in ctx


def test_build_context_document_chunk_pdf_uses_page_label():
    ctx = rag._build_context([_sample_chunks()[1]])
    assert "Документ «Кошторис.pdf»" in ctx
    assert "стор. 3 «Витрати»" in ctx


def test_build_context_document_chunk_pptx_uses_slide_label():
    ch = {"source_type": "document", "source_name": "Презентація.pptx",
          "doc_type": "pptx", "page": 5, "text": "Слайд про план."}
    ctx = rag._build_context([ch])
    assert "слайд 5" in ctx


@pytest.mark.parametrize("doc_type", ["xlsx", "csv"])
def test_build_context_document_chunk_spreadsheet_uses_sheet_label(doc_type):
    ch = {"source_type": "document", "source_name": f"Таблиця.{doc_type}",
          "doc_type": doc_type, "page": 1, "text": "Дані."}
    ctx = rag._build_context([ch])
    assert "лист 1" in ctx


def test_build_context_minimal_chunk_no_optional_fields():
    ch = {"source_type": "meeting", "source_name": "Мітинг X", "text": "текст"}
    ctx = rag._build_context([ch])
    assert ctx == "[1] Мітинг «Мітинг X»\nтекст"


def test_build_context_multiple_chunks_numbered_and_joined():
    ctx = rag._build_context(_sample_chunks())
    assert "[1] Мітинг" in ctx
    assert "[2] Документ" in ctx
    assert ctx.count("\n\n") >= 1  # blocks are separated by an empty line


# ============================================================
# order_citables -- chronology (story 03)
# ============================================================

def test_order_citables_sorts_rest_by_date_ascending_regardless_of_retrieval_order():
    """Two versions of the same agreement with different dates: the context shows them in
    chronology (early first, late second), REGARDLESS of the order in which
    retrieval returned them -- here the later one deliberately comes first in chunks."""
    later = {"source_type": "meeting", "source_name": "Дзвінок 2",
              "meeting_date": "2026-06-10", "text": "Бюджет — 50000 грн (нова версія)."}
    earlier = {"source_type": "meeting", "source_name": "Дзвінок 1",
               "meeting_date": "2026-05-12", "text": "Бюджет — 40000 грн (стара версія)."}
    sources = rag.order_citables([later, earlier])
    assert [s["source_name"] for s in sources] == ["Дзвінок 1", "Дзвінок 2"]

    ctx = rag._build_context([later, earlier])
    assert ctx.index("Дзвінок 1") < ctx.index("Дзвінок 2")


def test_order_citables_owner_comment_before_both_dated_versions():
    later = {"source_type": "meeting", "source_name": "Дзвінок 2",
              "meeting_date": "2026-06-10", "text": "нова версія"}
    earlier = {"source_type": "meeting", "source_name": "Дзвінок 1",
               "meeting_date": "2026-05-12", "text": "стара версія"}
    attached = [{"kind": "correction", "target_label": "Дзвінок 1",
                 "date": "2026-06-15", "text": "виправлення власника"}]
    sources = rag.order_citables([later, earlier], attached)
    assert sources[0]["source_type"] == "comment"
    assert [s.get("source_name") for s in sources[1:]] == ["Дзвінок 1", "Дзвінок 2"]


def test_order_citables_chunk_without_date_goes_last():
    dated = {"source_type": "meeting", "source_name": "З датою",
              "meeting_date": "2026-05-12", "text": "x"}
    undated = {"source_type": "document", "source_name": "Без дати", "text": "y"}
    sources = rag.order_citables([undated, dated])
    assert [s["source_name"] for s in sources] == ["З датою", "Без дати"]


def test_order_citables_same_date_keeps_retrieval_position():
    a = {"source_type": "meeting", "source_name": "A", "meeting_date": "2026-05-12", "text": "a"}
    b = {"source_type": "meeting", "source_name": "B", "meeting_date": "2026-05-12", "text": "b"}
    assert [s["source_name"] for s in rag.order_citables([a, b])] == ["A", "B"]
    assert [s["source_name"] for s in rag.order_citables([b, a])] == ["B", "A"]


def test_system_prompt_states_later_version_wins():
    assert "ПІЗНІША версія" in rag._RAG_SYSTEM_PROMPT
    assert "«було" in rag._RAG_SYSTEM_PROMPT
    assert "ХРОНОЛОГІЧНОМУ" in rag._RAG_SYSTEM_PROMPT


# ============================================================
# order_citables -- chunks of one record by chunk_index (story 09)
# ============================================================

def test_order_citables_chunks_of_one_record_sorted_by_chunk_index_regardless_of_retrieval():
    """Retrieval [A#7, B#1, A#2] (A and B have different dates, A is earlier) -> [A#2, A#7, B#1];
    the same result for [B#1, A#7, A#2]."""
    a7 = {"source_type": "meeting", "source_name": "A#7", "transcription_id": 1,
          "meeting_date": "2026-05-12", "chunk_index": 7, "text": "a7"}
    a2 = {"source_type": "meeting", "source_name": "A#2", "transcription_id": 1,
          "meeting_date": "2026-05-12", "chunk_index": 2, "text": "a2"}
    b1 = {"source_type": "meeting", "source_name": "B#1", "transcription_id": 2,
          "meeting_date": "2026-06-10", "chunk_index": 1, "text": "b1"}
    expected = ["A#2", "A#7", "B#1"]
    assert [s["source_name"] for s in rag.order_citables([a7, b1, a2])] == expected
    assert [s["source_name"] for s in rag.order_citables([b1, a7, a2])] == expected


def test_order_citables_same_date_records_dont_interleave():
    """Two records with the same date: between them -- the retrieval order (first appearance),
    inside each -- chunk_index, without interleaving."""
    a3 = {"source_type": "meeting", "source_name": "A#3", "transcription_id": 1,
          "meeting_date": "2026-05-12", "chunk_index": 3, "text": "a3"}
    a1 = {"source_type": "meeting", "source_name": "A#1", "transcription_id": 1,
          "meeting_date": "2026-05-12", "chunk_index": 1, "text": "a1"}
    b4 = {"source_type": "meeting", "source_name": "B#4", "transcription_id": 2,
          "meeting_date": "2026-05-12", "chunk_index": 4, "text": "b4"}
    b0 = {"source_type": "meeting", "source_name": "B#0", "transcription_id": 2,
          "meeting_date": "2026-05-12", "chunk_index": 0, "text": "b0"}
    # A appeared first in retrieval (a3) -> A entirely before B, inside by chunk_index
    sources = rag.order_citables([a3, b4, a1, b0])
    assert [s["source_name"] for s in sources] == ["A#1", "A#3", "B#0", "B#4"]


def test_order_citables_undated_record_chunks_sorted_by_chunk_index_at_end():
    r5 = {"source_type": "document", "source_name": "R#5", "transcription_id": 9,
          "chunk_index": 5, "text": "r5"}
    r3 = {"source_type": "document", "source_name": "R#3", "transcription_id": 9,
          "chunk_index": 3, "text": "r3"}
    dated = {"source_type": "meeting", "source_name": "Dated", "transcription_id": 1,
              "meeting_date": "2026-05-12", "chunk_index": 1, "text": "d"}
    sources = rag.order_citables([r5, dated, r3])
    assert [s["source_name"] for s in sources] == ["Dated", "R#3", "R#5"]


def test_order_citables_chunk_without_chunk_index_does_not_crash():
    a = {"source_type": "meeting", "source_name": "A", "transcription_id": 1,
         "meeting_date": "2026-05-12", "text": "a-no-index"}
    b = {"source_type": "meeting", "source_name": "B", "transcription_id": 1,
         "meeting_date": "2026-05-12", "chunk_index": 2, "text": "b"}
    attached = [{"kind": "correction", "target_label": "A", "date": "2026-06-15",
                 "text": "виправлення"}]
    sources = rag.order_citables([a, b], attached)
    assert sources[0]["source_type"] == "comment"
    assert {s.get("source_name") for s in sources[1:]} == {"A", "B"}


# ============================================================
# _build_request_kwargs
# ============================================================

def test_build_request_kwargs_default_model_used_and_adaptive_thinking_added(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    kwargs, model = rag._build_request_kwargs("Яке рішення прийняли?", _sample_chunks(), None)
    assert model == models_mod.get_default_model()
    assert kwargs["model"] == model
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "medium"}


def test_build_request_kwargs_explicit_non_adaptive_model_no_thinking():
    kwargs, model = rag._build_request_kwargs("q", _sample_chunks(), models_mod.HAIKU_4_5)
    assert model == models_mod.HAIKU_4_5
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


def test_build_request_kwargs_system_prompt_has_cache_control():
    kwargs, _ = rag._build_request_kwargs("q", _sample_chunks(), models_mod.HAIKU_4_5)
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # system -- a static prompt template, the question/context are NOT mixed into it
    # (otherwise every request would have a unique system and the prompt cache would not work)
    assert "q" not in kwargs["system"][0]["text"].split()
    assert kwargs["system"][0]["text"] == rag._RAG_SYSTEM_PROMPT


def test_build_request_kwargs_user_message_contains_question_and_context():
    kwargs, _ = rag._build_request_kwargs("Хто відповідає за бюджет?", _sample_chunks(), models_mod.HAIKU_4_5)
    user_msg = kwargs["messages"][0]["content"]
    assert "Хто відповідає за бюджет?" in user_msg
    assert "[1] Мітинг" in user_msg
    assert kwargs["max_tokens"] == 4096


# ============================================================
# answer_question (non-stream)
# ============================================================

def test_answer_question_empty_question_skips_search(monkeypatch):
    called = []
    monkeypatch.setattr(rag.retrieval, "search", lambda *a, **k: called.append(1))
    res = rag.answer_question(":memory:", "   ")
    assert res == {"answer": "", "sources": [], "found": 0, "model": ""}
    assert not called


def test_answer_question_passes_rewrite_flag_from_env_constant(monkeypatch):
    """production-rag-wave-b-07: rag.py passes `rewrite=_QUERY_REWRITE_ENABLED`
    (the same pattern as rerank) -- env=1 -> True, by default -> False."""
    captured = {}

    def fake_search(db_path, q, top_k=8, category_id=None, **kw):
        captured["rewrite"] = kw.get("rewrite")
        return {"chunks": [], "vector_available": True}

    monkeypatch.setattr(rag.retrieval, "search", fake_search)

    rag.answer_question(":memory:", "питання")
    assert captured["rewrite"] is rag._QUERY_REWRITE_ENABLED is False

    monkeypatch.setattr(rag, "_QUERY_REWRITE_ENABLED", True)
    rag.answer_question(":memory:", "питання")
    assert captured["rewrite"] is True


def test_answer_question_no_chunks_returns_fallback(monkeypatch):
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": [], "vector_available": True},
    )
    res = rag.answer_question(":memory:", "питання без відповіді")
    assert res["found"] == 0
    assert res["sources"] == []
    assert "не знайдено" in res["answer"]
    assert res["vector_available"] is True


def test_answer_question_success_extracts_text_and_usage(monkeypatch):
    chunks = _sample_chunks()
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": chunks, "vector_available": True},
    )
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: object())
    final = FakeFinalMessage(["Відповідь ", "по бюджету."], model="claude-x",
                              usage=FakeUsage(100, 20, 30))

    captured = {}

    def fake_stream_with_retry(client, timeout, request_kwargs, what="polish"):
        captured["kwargs"] = request_kwargs
        captured["what"] = what
        return final

    monkeypatch.setattr(rag.text_polishing, "_stream_with_retry", fake_stream_with_retry)

    res = rag.answer_question(":memory:", "Яке рішення?", model=models_mod.HAIKU_4_5)

    assert res["answer"] == "Відповідь по бюджету."
    assert res["found"] == 2
    assert res["model"] == "claude-x"
    assert res["input_tokens"] == 100
    assert res["output_tokens"] == 20
    assert res["cache_read_tokens"] == 30
    assert captured["what"] == "rag-ask"
    assert captured["kwargs"]["model"] == models_mod.HAIKU_4_5


@pytest.mark.parametrize("channel,seen", [("mcp", False), ("ui", True)])
def test_answer_question_mcp_hides_private_screen_text_before_claude(monkeypatch, channel, seen):
    """mcp-visual-privacy-02: text from the screen of a private record does not reach Claude from MCP;
    the Recall interface (ui) is not affected by the filter."""
    from app.services import privacy
    screen = {"transcription_id": 77, "chunk_id": 900, "chunk_index": 40, "speaker": "екран",
              "section": "кадр 00:12:30", "text": "Позовна заява Петренко", "source_type": "recording",
              "source_name": "Нарада", "meeting_date": "2026-10-01", "start_time": 750.0}
    chunks = _sample_chunks() + [screen]
    monkeypatch.setattr(rag.retrieval, "search",
                        lambda db_path, q, top_k=8, category_id=None, **kw:
                        {"chunks": [dict(c) for c in chunks], "vector_available": True})
    monkeypatch.setattr(privacy, "private_tids", lambda db, tids: {77})
    monkeypatch.setattr(privacy, "stop_words", lambda: [])
    monkeypatch.setenv("RECALL_MCP_VISUAL", "images")
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: object())
    captured = {}

    def fake(client, timeout, kwargs, what="polish"):
        captured["kwargs"] = kwargs
        return FakeFinalMessage(["ок"])
    monkeypatch.setattr(rag.text_polishing, "_stream_with_retry", fake)
    res = rag.answer_question(":memory:", "що з позовом?", channel=channel)
    assert ("Позовна заява" in str(captured["kwargs"])) is seen
    assert any(s.get("speaker") == "екран" for s in res["sources"]) is seen


def test_answer_question_mcp_privacy_failure_hides_all_screen_text(monkeypatch):
    from app.services import privacy
    screen = {"transcription_id": 5, "chunk_id": 1, "chunk_index": 9, "speaker": "екран",
              "text": "Бюджет на екрані", "source_type": "recording", "source_name": "Нарада"}
    monkeypatch.setattr(rag.retrieval, "search",
                        lambda db_path, q, top_k=8, category_id=None, **kw:
                        {"chunks": _sample_chunks() + [dict(screen)], "vector_available": True})

    def boom(db, tids):
        raise privacy.PrivacyError("БД недоступна")
    monkeypatch.setattr(privacy, "private_tids", boom)
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: object())
    captured = {}
    monkeypatch.setattr(rag.text_polishing, "_stream_with_retry",
                        lambda c, t, kwargs, what="polish": captured.update(k=kwargs)
                        or FakeFinalMessage(["ок"]))
    rag.answer_question(":memory:", "бюджет?", channel="mcp")
    assert "Бюджет на екрані" not in str(captured["k"])


def test_answer_question_ignores_non_text_blocks(monkeypatch):
    chunks = _sample_chunks()
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": chunks, "vector_available": False},
    )
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: object())
    final = FakeFinalMessage(["видима частина"], extra_blocks=[_Block("thinking", "приховані роздуми")])
    monkeypatch.setattr(rag.text_polishing, "_stream_with_retry",
                        lambda client, timeout, kwargs, what="polish": final)

    res = rag.answer_question(":memory:", "питання")
    assert res["answer"] == "видима частина"
    assert "роздуми" not in res["answer"]


# ============================================================
# answer_question_stream
# ============================================================

def _collect(gen):
    return list(gen)


def test_stream_empty_question_yields_error_event():
    frames = _collect(rag.answer_question_stream(":memory:", "  "))
    assert len(frames) == 1
    assert frames[0].startswith("event: error")


def test_stream_sources_event_emitted_before_no_chunks_fallback(monkeypatch):
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": [], "vector_available": False},
    )
    frames = _collect(rag.answer_question_stream(":memory:", "питання"))
    assert frames[0].startswith("event: sources")
    assert '"found": 0' in frames[0]
    assert any(f.startswith("event: delta") and "не знайдено" in f for f in frames)
    assert frames[-1].startswith("event: done")


def test_stream_passes_rewrite_flag_from_env_constant(monkeypatch):
    captured = {}

    def fake_search(db_path, q, top_k=8, category_id=None, **kw):
        captured["rewrite"] = kw.get("rewrite")
        return {"chunks": [], "vector_available": False}

    monkeypatch.setattr(rag.retrieval, "search", fake_search)
    monkeypatch.setattr(rag, "_QUERY_REWRITE_ENABLED", True)
    _collect(rag.answer_question_stream(":memory:", "питання"))
    assert captured["rewrite"] is True


# ============================================================
# production-rag-wave-b-08, Major 2: `RAG_QUERY_REWRITE` is read through
# `settings.env_bool` in BOTH readers (rag.py and retrieval.search) -- neither
# parses the flag with its own set of truthy values (ADR-008).
# ============================================================

@pytest.fixture
def _reload_rag_after():
    """`_QUERY_REWRITE_ENABLED` is computed ONCE at import -- the test below changes
    env via `importlib.reload`. Declared BEFORE `monkeypatch` in the test
    signature (LIFO: the monkeypatch teardown -- which rolls env back -- goes FIRST, the teardown
    of this fixture goes LAST), so the reload here sees the already restored
    (disabled) env and returns the module to the default state for the rest of the session."""
    yield
    import importlib
    importlib.reload(rag)


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
])
def test_rag_query_rewrite_env_matches_retrieval_search_default(
        _reload_rag_after, monkeypatch, tmp_path, val, expected):
    """Major 2: the value with which `search` receives `rewrite=` matches the
    default of `retrieval.search(rewrite=None)` under the same env -- both
    readers go through `settings.env_bool`, neither parses with a literal."""
    import importlib
    import sqlite3

    from app.db.migrations import init_database
    from app.services import query_rewrite

    monkeypatch.setenv("RAG_QUERY_REWRITE", val)
    importlib.reload(rag)
    assert rag._QUERY_REWRITE_ENABLED is expected

    db_path = str(tmp_path / "t.db")
    init_database(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "meeting_date) VALUES ('file', 'Мітинг', 'бюджет проєкту', '2026-01-01')")
    tid = cur.lastrowid
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text) VALUES (?, 0, 0, 10, 'Ви', 'бюджет проєкту')", (tid,))
    conn.commit()
    conn.close()

    called = []
    monkeypatch.setattr(query_rewrite, "rewrite_query",
                        lambda q, **kw: (called.append(q), [])[1])

    # Independent measurement: the default of retrieval.search(rewrite=None) under the same env.
    rag.retrieval.search(db_path, "бюджет проєкту", rewrite=None)
    assert bool(called) is expected

    captured = {}

    def fake_search(db_path_, q, top_k=8, category_id=None, **kw):
        captured["rewrite"] = kw.get("rewrite")
        return {"chunks": [], "vector_available": False}
    monkeypatch.setattr(rag.retrieval, "search", fake_search)
    rag.answer_question(db_path, "питання")
    assert captured["rewrite"] is expected


def test_stream_yields_deltas_then_done(monkeypatch):
    chunks = _sample_chunks()
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": chunks, "vector_available": True},
    )
    final = FakeFinalMessage([], model="claude-stream", usage=FakeUsage(50, 10, 5))
    client = FakeClient([{"chunks": ["Час", "тина ", "відповіді"], "final": final}])
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: client)

    frames = _collect(rag.answer_question_stream(":memory:", "питання", model=models_mod.HAIKU_4_5))
    assert frames[0].startswith("event: sources")
    deltas = [f for f in frames if f.startswith("event: delta")]
    assert len(deltas) == 3
    assert "Час" in deltas[0]
    done = frames[-1]
    assert done.startswith("event: done")
    assert '"model": "claude-stream"' in done
    assert '"input_tokens": 50' in done


def test_stream_retries_transient_error_before_first_delta(monkeypatch):
    _no_sleep(monkeypatch)
    chunks = _sample_chunks()
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": chunks, "vector_available": True},
    )
    final = FakeFinalMessage([], model="claude-retry-ok", usage=FakeUsage(1, 1, 0))
    client = FakeClient([
        {"error_at": "enter", "error": _Transient("overloaded")},
        {"chunks": ["ok"], "final": final},
    ])
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: client)

    frames = _collect(rag.answer_question_stream(":memory:", "питання"))
    assert client.messages.calls == 2, "2 attempts must have happened (1 transient error + success)"
    assert any(f.startswith("event: delta") and "ok" in f for f in frames)
    assert frames[-1].startswith("event: done")
    assert '"model": "claude-retry-ok"' in frames[-1]


def test_stream_does_not_retry_after_first_delta_already_yielded(monkeypatch):
    """T6.3: if even 1 delta has already gone to the client -- a retry is UNSAFE (duplicate/corrupted output).
    The error is propagated at once as event:error, WITHOUT a retry even if transient."""
    _no_sleep(monkeypatch)
    chunks = _sample_chunks()
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": chunks, "vector_available": True},
    )
    client = FakeClient([
        {"chunks": ["часткова"], "error_after_chunk": 0, "error": _Transient("mid-stream drop"),
         "final": None},
    ])
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: client)

    frames = _collect(rag.answer_question_stream(":memory:", "питання"))
    assert client.messages.calls == 1, "there must have been no retry after the delta was emitted"
    assert any(f.startswith("event: delta") and "часткова" in f for f in frames)
    assert frames[-1].startswith("event: error")


def test_stream_non_retryable_error_propagates_immediately(monkeypatch):
    _no_sleep(monkeypatch)
    chunks = _sample_chunks()
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {"chunks": chunks, "vector_available": True},
    )
    client = FakeClient([{"error_at": "enter", "error": _NonRetryable("bad request")}])
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: client)

    frames = _collect(rag.answer_question_stream(":memory:", "питання"))
    assert client.messages.calls == 1, "4xx (not 429) is not retried"
    assert frames[-1].startswith("event: error")


# ============================================================
# Wave 4: correspondence is NOT a meeting
# ============================================================

def _tg_chunk(**over):
    ch = {
        "source_type": "telegram",
        "source_name": "[TG] Main Team Chat: домовились",
        "tg_chat_title": "Main Team Chat",
        "meeting_date": "2026-06-11",
        "speaker": "Адам",
        "text": "домовились про оплату в пʼятницю",
        "start_time": None, "page": None, "section": None, "doc_type": None,
    }
    ch.update(over)
    return ch


def test_build_context_telegram_is_not_labelled_meeting():
    """Earlier this took «Мітинг «[TG] чат: сніпет»» -- i.e. the model retold the vast majority of the corpus
    as "at the meeting you decided", and without even knowing the author."""
    ctx = rag._build_context([_tg_chunk()])
    assert "Мітинг" not in ctx
    assert "Telegram" in ctx
    assert "Main Team Chat" in ctx
    assert "від: Адам" in ctx
    assert "2026-06-11" in ctx


def test_build_context_telegram_mentions_reply_thread():
    ctx = rag._build_context([_tg_chunk(tg_reply_to=4721)])
    assert "4721" in ctx, "the conversation thread must be visible to the model"


def test_build_context_telegram_without_chat_title_falls_back():
    ctx = rag._build_context([_tg_chunk(tg_chat_title=None)])
    assert "Telegram" in ctx and "[TG] Main Team Chat" in ctx


def test_build_context_meeting_branch_untouched():
    """Ordinary meetings must stay exactly as they were."""
    ctx = rag._build_context([_sample_chunks()[0]])
    assert "Мітинг" in ctx


# ============================================================
# Wave 4.5: conversation thread in the prompt
# ============================================================

def _thread(**over):
    t = {
        "thread_id": 12,
        "label": "Узгодження зустрічі",
        "total_messages": 7,
        "messages": [
            {"tg_message_id": 1, "date": "2026-06-09T13:51:00+00:00",
             "sender": "Юля", "text": "Давайте в четвер о 10:00?", "is_hit": True},
            {"tg_message_id": 2, "date": "2026-06-09T13:44:00+00:00",
             "sender": "Настя", "text": "чт – з 09:00 до 11:00", "is_hit": False},
        ],
    }
    t.update(over)
    return t


def test_context_renders_thread_so_answer_travels_with_question():
    ctx = rag._build_context([_tg_chunk(thread=_thread())])
    assert "09:00 до 11:00" in ctx, "the answer must be in the prompt next to the question"
    assert "Узгодження зустрічі" in ctx


def test_context_marks_the_found_message_inside_thread():
    """Without a mark the model will not tell the finding from the context and will cite the neighbour."""
    ctx = rag._build_context([_tg_chunk(thread=_thread())])
    hit_line = [ln for ln in ctx.splitlines() if "Давайте в четвер" in ln][0]
    other = [ln for ln in ctx.splitlines() if "09:00 до 11:00" in ln][0]
    assert hit_line.startswith("→")
    assert not other.startswith("→")


def test_context_shows_how_much_of_thread_is_included():
    ctx = rag._build_context([_tg_chunk(thread=_thread())])
    assert "2 з 7" in ctx


def test_context_without_thread_is_unchanged():
    """Records without a thread must render exactly as in Wave 4."""
    ctx = rag._build_context([_tg_chunk()])
    assert "домовились про оплату" in ctx
    assert "Нитка" not in ctx


def test_context_ignores_empty_thread():
    ctx = rag._build_context([_tg_chunk(thread={"messages": [], "label": "x"})])
    assert "домовились про оплату" in ctx


# ============================================================
# editable-title-description-03: a record is called by its own title
# ============================================================

def test_build_context_names_record_by_display_name():
    ch = {
        "source_type": "meeting", "source_name": "rec_2026-09-17_final.mp3",
        "title": "Планерка Фонду", "display_name": "Планерка Фонду",
        "meeting_date": "2026-09-17", "text": "Обговорили кошторис.",
    }
    ctx = rag._build_context([ch])
    assert "«Планерка Фонду»" in ctx
    assert "rec_2026-09-17_final.mp3" not in ctx


def test_build_context_falls_back_to_source_name_without_title():
    ch = {"source_type": "meeting", "source_name": "Дзвінок з клієнтом",
          "display_name": None, "text": "Обговорили бюджет."}
    assert "«Дзвінок з клієнтом»" in rag._build_context([ch])


def test_sources_carry_display_name(monkeypatch):
    """`sources[]` are the same chunks from retrieval, so the new keys reach
    the client through order_citables without a separate change."""
    chunks = [{
        "source_type": "meeting", "source_name": "rec_1.mp3",
        "title": "Планерка Фонду", "display_name": "Планерка Фонду",
        "transcription_id": 5, "meeting_date": "2026-09-17",
        "text": "Обговорили кошторис.",
    }]
    ordered = rag.order_citables(chunks, [])
    assert [s["display_name"] for s in ordered] == ["Планерка Фонду"]


# ============================================================
# directions-projects-tags-03: new scope axes reach retrieval
# ============================================================

def test_answer_question_passes_tags_since_until_to_search(monkeypatch):
    """rag changes nothing: the axes go into `retrieval.search` as is."""
    captured = {}

    def fake_search(db_path, q, top_k=8, category_id=None, **kw):
        captured.update(kw)
        captured["category_id"] = category_id
        return {"chunks": [], "vector_available": True}

    monkeypatch.setattr(rag.retrieval, "search", fake_search)
    rag.answer_question(":memory:", "питання", category_id=7,
                        tags=["AI", "інвестори"], since="2026-09-01",
                        until="2026-09-30")
    assert captured["category_id"] == 7
    assert captured["tags"] == ["AI", "інвестори"]
    assert captured["since"] == "2026-09-01"
    assert captured["until"] == "2026-09-30"


def test_answer_question_without_new_axes_sends_none(monkeypatch):
    """An old call stays an old call -- no silent narrowing."""
    captured = {}

    def fake_search(db_path, q, top_k=8, category_id=None, **kw):
        captured.update(kw)
        return {"chunks": [], "vector_available": True}

    monkeypatch.setattr(rag.retrieval, "search", fake_search)
    rag.answer_question(":memory:", "питання")
    assert captured["tags"] is None
    assert captured["since"] is None and captured["until"] is None


def test_answer_question_stream_passes_tags_since_until(monkeypatch):
    captured = {}

    def fake_search(db_path, q, top_k=8, category_id=None, **kw):
        captured.update(kw)
        return {"chunks": [], "vector_available": True}

    monkeypatch.setattr(rag.retrieval, "search", fake_search)
    list(rag.answer_question_stream(":memory:", "питання", tags=["AI"],
                                    since="2026-09-01", until="2026-09-30"))
    assert captured["tags"] == ["AI"]
    assert captured["since"] == "2026-09-01"
    assert captured["until"] == "2026-09-30"


def test_new_axes_do_not_change_sources_or_why(monkeypatch):
    """`sources`/`why` -- contract S2: the new axes do not touch them."""
    chunks = _sample_chunks()
    monkeypatch.setattr(
        rag.retrieval, "search",
        lambda db_path, q, top_k=8, category_id=None, **kw: {
            "chunks": chunks, "vector_available": True},
    )
    monkeypatch.setattr(rag.text_polishing, "_get_client", lambda: object())
    monkeypatch.setattr(
        rag.text_polishing, "_stream_with_retry",
        lambda client, timeout, request_kwargs, what="polish": FakeFinalMessage(
            ["Відповідь."], model="claude-x", usage=FakeUsage(100, 20, 30)))

    plain = rag.answer_question(":memory:", "питання")
    tagged = rag.answer_question(":memory:", "питання", tags=["AI"],
                                 since="2026-09-01")
    assert tagged["sources"] == plain["sources"]
    assert tagged["found"] == plain["found"]
