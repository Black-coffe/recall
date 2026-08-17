"""Юніт-тести RAG «Запитай архів» (app/services/rag.py) — REMEDIATION_PLAN T7.5.

Офлайн: жодного реального виклику Anthropic API. Anthropic-клієнт і
retrieval.search підмінюються фейками. Покриває:
  - _fmt_time / _build_context (детермінований форматер контексту чанків)
  - _build_request_kwargs (побудова payload'у + adaptive thinking)
  - answer_question (нестрімова відповідь, мок _get_client/_stream_with_retry)
  - answer_question_stream (SSE-кадри, включно з retry на транзиентній
    помилці ДО першого delta і без retry ПІСЛЯ першого delta — T6.3)
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
    """Симулює транзиентну помилку (5xx) без реального anthropic SDK об'єкта."""
    status_code = 529


class _NonRetryable(Exception):
    status_code = 400


class ScriptedStreamCM:
    """Один виклик client.messages.stream(**kwargs) — контекст-менеджер."""

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
    assert ctx.count("\n\n") >= 1  # блоки розділені порожнім рядком


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
    # system — статичний промпт-шаблон, питання/контекст туди НЕ підмішуються
    # (інакше кожен запит мав би унікальний system і prompt-cache не спрацьовував би)
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
    assert client.messages.calls == 2, "мало відбутись 2 спроби (1 транзиентна помилка + успіх)"
    assert any(f.startswith("event: delta") and "ok" in f for f in frames)
    assert frames[-1].startswith("event: done")
    assert '"model": "claude-retry-ok"' in frames[-1]


def test_stream_does_not_retry_after_first_delta_already_yielded(monkeypatch):
    """T6.3: якщо хоч 1 delta вже пішов клієнту — повтор НЕБЕЗПЕЧНИЙ (дубль/пошкоджений вивід).
    Помилка одразу прокидається як event:error, БЕЗ повторної спроби навіть якщо транзиентна."""
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
    assert client.messages.calls == 1, "після виданого delta повтору НЕ мало бути"
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
    assert client.messages.calls == 1, "4xx (не 429) не ретраїться"
    assert frames[-1].startswith("event: error")


# ============================================================
# Волна 4: переписка — НЕ мітинг
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
    """Раніше сюди йшло «Мітинг «[TG] чат: сніпет»» — тобто переважна більшість корпусу модель
    переказувала як «на зустрічі ви вирішили», ще й не знаючи автора."""
    ctx = rag._build_context([_tg_chunk()])
    assert "Мітинг" not in ctx
    assert "Telegram" in ctx
    assert "Main Team Chat" in ctx
    assert "від: Адам" in ctx
    assert "2026-06-11" in ctx


def test_build_context_telegram_mentions_reply_thread():
    ctx = rag._build_context([_tg_chunk(tg_reply_to=4721)])
    assert "4721" in ctx, "нитка розмови має бути видима моделі"


def test_build_context_telegram_without_chat_title_falls_back():
    ctx = rag._build_context([_tg_chunk(tg_chat_title=None)])
    assert "Telegram" in ctx and "[TG] Main Team Chat" in ctx


def test_build_context_meeting_branch_untouched():
    """Звичайні мітинги мають лишитись рівно такими, як були."""
    ctx = rag._build_context([_sample_chunks()[0]])
    assert "Мітинг" in ctx


# ============================================================
# Волна 4.5: нитка розмови у промпті
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
    assert "09:00 до 11:00" in ctx, "відповідь має бути у промпті поруч із питанням"
    assert "Узгодження зустрічі" in ctx


def test_context_marks_the_found_message_inside_thread():
    """Без позначки модель не відрізнить знахідку від контексту і цитуватиме сусіда."""
    ctx = rag._build_context([_tg_chunk(thread=_thread())])
    hit_line = [ln for ln in ctx.splitlines() if "Давайте в четвер" in ln][0]
    other = [ln for ln in ctx.splitlines() if "09:00 до 11:00" in ln][0]
    assert hit_line.startswith("→")
    assert not other.startswith("→")


def test_context_shows_how_much_of_thread_is_included():
    ctx = rag._build_context([_tg_chunk(thread=_thread())])
    assert "2 з 7" in ctx


def test_context_without_thread_is_unchanged():
    """Записи без нитки мають рендеритись рівно як у Волні 4."""
    ctx = rag._build_context([_tg_chunk()])
    assert "домовились про оплату" in ctx
    assert "Нитка" not in ctx


def test_context_ignores_empty_thread():
    ctx = rag._build_context([_tg_chunk(thread={"messages": [], "label": "x"})])
    assert "домовились про оплату" in ctx
