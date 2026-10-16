"""Offline test of co-pilot Step 3: the local dispatcher LLM + integration into the worker.

Without Ollama / microphone / DB — all external dependencies (LLM, RAG search, persist
service, live segments) are stubs. Checks the Step 3 contracts:
  A. Dispatcher.triage/analyze/summarize: parsing, normalization, filtering out hallucinations
     (an insight without a REAL evidence_chunk_id is dropped).
  B. Worker._tick → dispatch on a cadence: triage → RAG search → analyze → SSE
     copilot_insight + persist insight_local; the topic's RAG cache is saved.
  C. Return to a topic → reuse of the cached RAG (no new search is made).
  D. Dedup: a chunk already shown to the operator is not sent to the LLM again.

Run:  .venv/Scripts/python.exe test_copilot_dispatcher.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np

from app.services.copilot.dispatcher import Dispatcher
from app.services.copilot.topics import TopicTracker
from app.services.copilot.worker import CopilotWorker, _WS


_fail = 0


def check(cond, msg):
    global _fail
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fail += 1


# --------------------------------------------------------------------- fakes

class FakeLLM:
    """A stub local_llm: branches on the system prompt."""
    def __init__(self):
        self.calls = []

    def generate_json(self, prompt, *, schema=None, system=None, max_tokens=512):
        self.calls.append(system or "")
        raw = {"prompt_eval_count": 42, "eval_count": 17}
        if system and system.startswith("Ти — локальний диспетчер"):
            data = {
                "topic_label": "Бюджет проєкту X",
                "needs_retrieval": True,
                "retrieval_query": "бюджет проєкту X сума Q2",
                "observations": [
                    {"kind": "question", "text": "Уточнити суму бюджету", "confidence": 0.6},
                    {"kind": "garbage", "text": "ігнорувати", "confidence": 0.9},  # invalid kind
                    {"kind": "fact", "text": "", "confidence": 0.5},               # empty text
                ],
            }
        elif system and system.startswith("Ти — аналітик"):
            data = {"insights": [
                {"kind": "contradiction",
                 "text": "В архіві бюджет 40k, зараз звучить 60k",
                 "evidence_chunk_ids": [101, 999],   # 999 — made up, must be filtered out
                 "confidence": 0.74},
                {"kind": "fact", "text": "Без доказів", "evidence_chunk_ids": [],
                 "confidence": 0.8},                 # empty evidence → drop
            ]}
        else:  # summary
            data = {"summary": "Обговорили бюджет проєкту X."}
        return {"data": data, "raw": raw}


def fake_search_factory():
    state = {"calls": 0, "queries": [], "scope_tids": []}

    # Track 2 (25.07.2026): the worker passes scope_tids (a slice by project) to
    # search_fn — the fake's signature must accept it, otherwise a TypeError
    # is swallowed by the worker as a "RAG search failure" and sections B/F fail in a cascade.
    def fake_search(db_path, query, top_k=8, category_id=None, scope_tids=None):
        state["calls"] += 1
        state["queries"].append(query)
        state["scope_tids"].append(scope_tids)
        return {"query": query, "vector_available": True, "chunks": [
            {"chunk_id": 101, "transcription_id": 5, "source_name": "Дзвінок 12.05",
             "meeting_date": "2026-05-12", "start_time": 30.0, "text": "бюджет 40k"},
            {"chunk_id": 202, "transcription_id": 6, "source_name": "Документ",
             "meeting_date": "2026-04-01", "start_time": None, "text": "інше"},
        ]}
    return fake_search, state


class FakeBroker:
    def __init__(self):
        self.events = []  # (channel, event, data)

    def publish(self, channel, event, data):
        self.events.append((channel, event, data))


class FakeService:
    def __init__(self, budget=None):
        self.events = []
        self.retrieval_cache = {}
        self._eid = 0
        self.budget = budget          # None = no limit
        self.cost = 0.0
        self.tokens_in = 0
        self.tokens_out = 0

    def log_event(self, sid, kind, **kw):
        self._eid += 1
        self.events.append((kind, kw))
        return self._eid

    def add_usage(self, sid, *, tokens_in=0, tokens_out=0, cache_read=0, cost=0.0):
        self.cost += cost
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        cap = self.budget is not None and self.budget > 0
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "cache_read": cache_read, "cost_estimate": round(self.cost, 6),
                "budget_usd": self.budget, "exhausted": bool(cap and self.cost >= self.budget)}

    def upsert_topic(self, *a, **k):
        return 1

    def update_topic_retrieval(self, sid, topic_index, chunks):
        self.retrieval_cache[(sid, topic_index)] = chunks

    def get_topic_retrieval(self, sid, topic_index):
        return self.retrieval_cache.get((sid, topic_index))

    def get_category_name(self, category_id):
        return "Фонд" if category_id else None


class FakeLive:
    """get_preview returns a queue of segment batches (one per call)."""
    def __init__(self, batches):
        self._batches = list(batches)
        self._acc = []

    def get_preview(self, sid):
        if self._batches:
            self._acc = self._batches.pop(0)
        return self._acc


class FakeEscalator:
    def __init__(self, sonnet_verdict="real", delay=0.0):
        self.verify_calls = 0
        self.sweep_calls = 0
        self.votes_seen = []
        self.models_seen = []
        self._sonnet_verdict = sonnet_verdict
        self._delay = delay

    def verify(self, *, insight, evidence_chunks, profile="", model=None, votes=1,
               on_vote=None, can_continue=None):
        import time as _t
        self.verify_calls += 1
        self.votes_seen.append(votes)
        self.models_seen.append(model)
        verdict = "real" if (model and "opus" in model) else self._sonnet_verdict
        per = {"verdict": verdict, "confidence": 0.92,
               "text": "Перевірено: в архіві бюджет 40k, зараз 60k — розбіжність.",
               "model": model or "claude-sonnet-4-6", "tokens_in": 1200,
               "tokens_out": 80, "cache_read": 3000, "cost": 0.005}
        # We simulate a multi-vote with per-vote accounting + an early stop on the budget.
        n = 0
        for i in range(max(1, int(votes or 1))):
            if i > 0 and can_continue is not None and not can_continue():
                break
            if self._delay:
                _t.sleep(self._delay)
            n += 1
            if on_vote is not None:
                on_vote(dict(per))
        agg = dict(per)
        agg.update(votes=n, tokens_in=per["tokens_in"] * n, tokens_out=per["tokens_out"] * n,
                   cache_read=per["cache_read"] * n, cost=per["cost"] * n)
        return agg

    def sweep(self, *, window, chunks, profile="", model=None):
        self.sweep_calls += 1
        # we use the LAST chunk (hardly shown by the analysis insight) → it passes dedup
        ids = [chunks[-1]["chunk_id"]] if chunks else []
        ins = [{"kind": "fact", "text": "Sweep: важлива деталь з архіву",
                "evidence_chunk_ids": ids, "confidence": 0.8}] if ids else []
        return {"insights": ins, "model": model, "tokens_in": 2000,
                "tokens_out": 150, "cache_read": 3000, "cost": 0.01}


class _Resp:
    def __init__(self, inp):
        class _B:
            type = "tool_use"
        b = _B()
        b.input = inp
        self.content = [b]
        self.model = "claude-sonnet-4-6"

        class _U:
            input_tokens = 100
            output_tokens = 20
            cache_read_input_tokens = 0
        self.usage = _U()


class FakeAnthropic:
    """A minimal stub anthropic client for the real Escalator (tool-use)."""
    def __init__(self, verdicts):
        self._q = [{"verdict": v, "confidence": 0.8, "text": "перевірка"} for v in verdicts]
        self.calls = 0

    def with_options(self, **kw):
        outer = self

        class _M:
            def create(self, **kwargs):
                outer.calls += 1
                return _Resp(outer._q.pop(0))

        class _O:
            messages = _M()
        return _O()


def _embed(text):
    # a deterministic non-zero vector; stability is enough for the topic test
    h = sum(ord(c) for c in text[:50]) or 1
    rng = np.array([(h * (i + 1)) % 97 for i in range(8)], dtype=np.float32)
    return rng


def _ws(cfg):
    return _WS(copilot_session_id=1, recording_session_id="rec_test",
              config=cfg, tracker=TopicTracker(_embed))


# ------------------------------------------------------------------- A. Dispatcher

def test_dispatcher():
    print("\nA. Dispatcher (triage / analyze / summarize)")
    llm = FakeLLM()
    search, _ = fake_search_factory()
    d = Dispatcher(db_path=":memory:", llm=llm, search_fn=search)

    tri = d.triage(window="...розмова про бюджет...", summary="", profile="Напрямок: Фонд")
    check(tri is not None, "triage returned a result")
    check(tri["needs_retrieval"] is True and tri["retrieval_query"], "triage: needs_retrieval+query")
    check(len(tri["observations"]) == 1 and tri["observations"][0]["kind"] == "question",
          "triage: invalid kind + empty text filtered out (1 left)")
    check(tri["tokens_in"] == 42 and tri["tokens_out"] == 17, "triage: token accounting")

    chunks = [{"chunk_id": 101, "source_name": "Дзвінок", "meeting_date": "2026-05-12",
               "text": "бюджет 40k", "transcription_id": 5, "start_time": 30.0}]
    an = d.analyze(window="зараз 60k", chunks=chunks, profile="")
    check(an is not None, "analyze returned a result")
    ins = an["insights"]
    check(len(ins) == 1 and ins[0]["kind"] == "contradiction",
          "analyze: 1 insight left (the one without evidence dropped)")
    check(ins[0]["evidence_chunk_ids"] == [101],
          "analyze: the made-up chunk_id 999 filtered out, the real 101 remained")

    check(d.analyze(window="x", chunks=[], profile="")["insights"] == [],
          "analyze without chunks → empty, without an LLM call")

    s = d.summarize(prev_summary="", window="репліки")
    check(isinstance(s, str) and s, "summarize returned text")


# ------------------------------------------------------------------- B. Worker dispatch

def _make_worker(llm, search):
    disp = Dispatcher(db_path=":memory:", llm=llm, search_fn=search)
    broker, svc = FakeBroker(), FakeService()
    live = FakeLive([[{"start": 0.0, "end": 5.0, "stream": "mic",
                       "text": "Обговорюємо бюджет проєкту X, зараз називають 60 тисяч."}]])
    w = CopilotWorker(broker=broker, live_transcribe_worker=live, recording_service=None,
                      copilot_service=svc, dispatcher=disp, llm=llm,
                      min_window_chars=10, default_cadence_sec=0, summary_every=999)
    return w, broker, svc


def test_worker_dispatch():
    print("\nB. Worker: _tick → dispatch (triage→RAG→analyze→SSE+persist)")
    llm = FakeLLM()
    search, sstate = fake_search_factory()
    w, broker, svc = _make_worker(llm, search)

    ws = _ws({"cadence_sec": 0, "top_k": 5, "category_id": 7, "escalate_threshold": 0.65})
    # tick 1: collects a segment → creates a topic → dispatch
    w._tick(ws)

    check(ws.tracker.current is not None, "topic created (topic tracker)")
    check(ws.tracker.topics[ws.tracker.current]["label"] == "Бюджет проєкту X",
          "topic label updated from the LLM triage")
    check(sstate["calls"] == 1, "RAG search called exactly 1×")
    pub_insights = [d for (_c, ev, d) in broker.events if ev == "copilot_insight"]
    check(len(pub_insights) == 1, "1 copilot_insight published")
    if pub_insights:
        ins = pub_insights[0]
        check(ins["source"] == "local" and ins["kind"] == "contradiction",
              "insight: source=local, kind=contradiction")
        check(ins["evidence"] and ins["evidence"][0]["chunk_id"] == 101,
              "the insight carries a real evidence chunk_id=101 with provenance")
    kinds = [k for (k, _kw) in svc.events]
    check("retrieval" in kinds and "insight_local" in kinds,
          "persist: retrieval + insight_local events recorded")
    check((1, ws.tracker.current) in svc.retrieval_cache, "the topic's RAG cache saved into the service")
    check(101 in ws.shown_chunks, "the shown chunk 101 is in the dedup set")
    return w, broker, svc, sstate, llm


# ------------------------------------------------------------------- C. Topic return reuse

def test_topic_return_reuse():
    print("\nC. Return to a topic → cache reuse (no new search)")
    llm = FakeLLM()
    search, sstate = fake_search_factory()
    disp = Dispatcher(db_path=":memory:", llm=llm, search_fn=search)
    broker, svc = FakeBroker(), FakeService()
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=disp, llm=llm, default_cadence_sec=0)

    ws = _ws({"cadence_sec": 0, "top_k": 5, "category_id": None, "escalate_threshold": 0.65})
    ws.transcript = "повернулись до теми бюджету"
    # we simulate the state after returning to topic 0 with a cached RAG
    ws.tracker.topics = [{"index": 0, "centroid": _embed("x"), "count": 1,
                          "label": "Бюджет", "first_ts": 0, "last_ts": 0}]
    ws.tracker.current = 0
    ws.return_to = 0
    ws.topic_chunks[0] = [{"chunk_id": 101, "transcription_id": 5, "source_name": "Дзвінок",
                           "meeting_date": "2026-05-12", "start_time": 30.0, "text": "бюджет 40k"}]

    w._maybe_dispatch(ws)
    check(sstate["calls"] == 0, "a new RAG search was NOT made (cache reused)")
    pub = [d for (_c, ev, d) in broker.events if ev == "copilot_insight"]
    check(len(pub) == 1 and pub[0]["evidence"][0]["chunk_id"] == 101,
          "an insight from the topic's cached context")


# ------------------------------------------------------------------- D. Dedup

def test_dedup():
    print("\nD. Dedup: an already shown chunk is not sent to the LLM again")
    llm = FakeLLM()
    search, sstate = fake_search_factory()
    disp = Dispatcher(db_path=":memory:", llm=llm, search_fn=search)
    broker, svc = FakeBroker(), FakeService()
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=disp, llm=llm, default_cadence_sec=0)
    ws = _ws({"cadence_sec": 0, "top_k": 5, "category_id": None, "escalate_threshold": 0.65})
    ws.transcript = "знову бюджет"
    ws.tracker.topics = [{"index": 0, "centroid": _embed("x"), "count": 1,
                          "label": "Бюджет", "first_ts": 0, "last_ts": 0}]
    ws.tracker.current = 0
    ws.shown_chunks = {101: True, 202: True}  # both already shown (ordered-dict as a set)

    n_llm_before = len(llm.calls)
    w._maybe_dispatch(ws)
    analysis_calls = [s for s in llm.calls[n_llm_before:] if s.startswith("Ти — аналітик")]
    check(len(analysis_calls) == 0, "analyze NOT called (all chunks already shown)")
    # triage gave only an observation kind=question without proof → muted as noise (filter)
    pub = [d for (_c, ev, d) in broker.events if ev == "copilot_insight"]
    check(len(pub) == 0,
          "fallback: question without proof muted (anti-noise _filter_unanchored)")


def test_topic_tracker_llm_driven():
    print("\nC2. TopicTracker — LLM-driven decision (continue/shift/return)")
    tr = TopicTracker(_embed)  # embed_fn is ignored
    e0 = tr.apply(status="continue", label="Фонд", return_to=None, ts=1)
    check(e0["action"] == "shift" and tr.current == 0, "first topic → shift (whatever the LLM said)")
    e1 = tr.apply(status="continue", label="Фонд (деталі)", return_to=None, ts=2)
    check(e1["action"] == "continue" and e1["relabeled"]
          and tr.topics[0]["label"] == "Фонд (деталі)", "continue with a new label → relabel")
    e1b = tr.apply(status="continue", label="Фонд (деталі)", return_to=None, ts=3)
    check(e1b["action"] == "continue" and not e1b["relabeled"], "the same label → no relabel")
    e2 = tr.apply(status="shift", label="AI Box", return_to=None, ts=4)
    check(e2["action"] == "shift" and tr.current == 1 and len(tr.topics) == 2, "shift → new topic")
    e3 = tr.apply(status="return", label="Фонд", return_to=0, ts=5)
    check(e3["action"] == "return" and tr.current == 0 and e3["returned_from"] == 1,
          "return to an existing index → return")
    e4 = tr.apply(status="return", label="x", return_to=99, ts=6)
    check(e4["action"] == "continue" and tr.current == 0, "return to a non-existent index → continue")
    e5 = tr.apply(status="shift", label="", return_to=None, ts=7)
    check(e5["action"] == "continue", "shift with an empty label → degrades to continue")


def test_unanchored_filter():
    print("\nD3. Anti-noise: _filter_unanchored gates insights without proofs")
    w = CopilotWorker(broker=FakeBroker(), live_transcribe_worker=FakeLive([]),
                      recording_service=None, copilot_service=FakeService(),
                      dispatcher=None, db_path=":memory:")
    ws = _ws({"min_unanchored_conf": 0.80})
    mix = [
        {"kind": "question", "text": "q", "confidence": 0.95},        # no proof → drop (kind)
        {"kind": "clarification", "text": "c", "confidence": 0.99},   # no proof → drop (kind)
        {"kind": "fact", "text": "f-low", "confidence": 0.5},         # no proof, low → drop
        {"kind": "fact", "text": "f-hi", "confidence": 0.85},         # no proof, hi → KEEP
        {"kind": "contradiction", "text": "ctr", "confidence": 0.80}, # no proof, =threshold → KEEP
        {"kind": "question", "text": "q-ev", "confidence": 0.1,
         "evidence_chunk_ids": [101]},                                # with proof → KEEP (as is)
    ]
    out = w._filter_unanchored(ws, mix)
    texts = [o["text"] for o in out]
    check(texts == ["f-hi", "ctr", "q-ev"], f"only the signal ones remained: {texts}")
    # a stricter mode (light=0.90) cuts even f-hi(0.85) and ctr(0.80)
    ws2 = _ws({"min_unanchored_conf": 0.90})
    out2 = [o["text"] for o in w._filter_unanchored(ws2, mix)]
    check(out2 == ["q-ev"], f"the strict threshold leaves only the anchored one: {out2}")


# ------------------------------------------------------------------- D2. shown_chunks bounded (fix #3)

def test_shown_chunks_bounded():
    print("\nD2. shown_chunks is bounded by a FIFO window (fix #3)")
    w = CopilotWorker(broker=FakeBroker(), live_transcribe_worker=FakeLive([]),
                      recording_service=None, copilot_service=FakeService(),
                      dispatcher=None, db_path=":memory:", shown_cap=5)
    ws = _ws({})
    for cid in range(20):
        w._mark_shown(ws, cid)
    check(len(ws.shown_chunks) == 5, "size does not exceed the cap (5)")
    check(0 not in ws.shown_chunks and 19 in ws.shown_chunks,
          "the oldest evicted, the newest remained (FIFO)")


# ------------------------------------------------------------------- F. Escalation + safety-sweep

def test_escalation_and_sweep():
    print("\nF. Cascade: escalation to Claude + safety-sweep (fake escalator)")
    llm = FakeLLM()
    search, sstate = fake_search_factory()
    disp = Dispatcher(db_path=":memory:", llm=llm, search_fn=search)
    esc = FakeEscalator()
    broker, svc = FakeBroker(), FakeService(budget=10.0)
    live = FakeLive([[{"start": 0.0, "end": 5.0, "stream": "mic",
                       "text": "Обговорюємо бюджет проєкту X, зараз називають 60 тисяч."}]])
    w = CopilotWorker(broker=broker, live_transcribe_worker=live, recording_service=None,
                      copilot_service=svc, dispatcher=disp, escalator=esc, llm=llm,
                      min_window_chars=10, default_cadence_sec=0, summary_every=999)
    ws = _ws({"cadence_sec": 0, "top_k": 5, "category_id": 7, "escalate_threshold": 0.65,
              "api_enabled": True, "model_api": "claude-sonnet-4-6", "budget_usd": 10.0,
              "safety_sweep_sec": 0.0001})
    w._tick(ws)

    check(esc.verify_calls == 1, "Claude verify called 1× (conf 0.74 ≥ threshold 0.65, evidence present)")
    upd = [d for (_c, ev, d) in broker.events if ev == "copilot_insight_update"]
    check(len(upd) == 1 and upd[0]["source"] == "api" and upd[0]["verdict"] == "real",
          "copilot_insight_update: the card is upgraded to 'verified' (real)")
    check(esc.sweep_calls == 1, "safety-sweep called (a Sonnet pass over the window)")
    sweep_cards = [d for (_c, ev, d) in broker.events
                   if ev == "copilot_insight" and d.get("source") == "api"]
    check(len(sweep_cards) == 1 and sweep_cards[0]["text"].startswith("Sweep"),
          "the sweep card is published as source=api (verified)")
    kinds = [k for (k, _kw) in svc.events]
    check("insight_verified" in kinds, "persist: insight_verified recorded")
    usage = [d for (_c, ev, d) in broker.events if ev == "copilot_usage"]
    check(len(usage) >= 2 and usage[-1]["state"] == "hybrid",
          "copilot_usage published (state=hybrid, budget not exhausted)")


# ------------------------------------------------------------------- G. Budget hard-stop

def test_budget_hardstop():
    print("\nG. Budget hard stop → degradation to local-only")
    esc = FakeEscalator()
    broker, svc = FakeBroker(), FakeService(budget=0.004)  # < the cost of one verify (0.005)
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=None, escalator=esc,
                      db_path=":memory:", default_cadence_sec=0)
    ws = _ws({"api_enabled": True, "model_api": "claude-sonnet-4-6", "escalate_threshold": 0.5})
    ins = {"kind": "contradiction", "text": "40k vs 60k",
           "evidence_chunk_ids": [101], "confidence": 0.9}
    meta = {101: {"chunk_id": 101, "transcription_id": 5, "source_name": "Дзвінок",
                  "meeting_date": "2026-05-12", "start_time": 30.0, "text": "бюджет 40k"}}

    v1 = w._escalate_insight(ws, 1, ins, meta, "")
    check(v1 is not None and esc.verify_calls == 1, "the first escalation went through")
    check(ws.budget_exhausted is True, "after the overspend the budget is marked exhausted")
    last_usage = [d for (_c, ev, d) in broker.events if ev == "copilot_usage"][-1]
    check(last_usage["state"] == "budget_exhausted", "copilot_usage: state=budget_exhausted")

    v2 = w._escalate_insight(ws, 2, ins, meta, "")
    check(v2 is None and esc.verify_calls == 1, "the second escalation did NOT go to the API (hard stop)")


# ------------------------------------------------------------------- G2. Multi-vote budget early-stop (fix #1)

def test_multivote_budget_earlystop():
    print("\nG2. The multi-vote stops on the budget inside (fix #1)")
    esc = FakeEscalator()
    # budget for ~1 vote (0.005); hard mode asks for 3 — not all should go
    broker, svc = FakeBroker(), FakeService(budget=0.004)
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=None, escalator=esc,
                      db_path=":memory:", default_cadence_sec=0)
    ws = _ws({"api_enabled": True, "model_api": "claude-sonnet-4-6", "escalate_threshold": 0.4,
              "verify_votes": 3, "budget_usd": 0.004})
    ins = {"kind": "contradiction", "text": "x", "evidence_chunk_ids": [101], "confidence": 0.9}
    meta = {101: {"chunk_id": 101, "text": "y", "source_name": "s", "transcription_id": 1}}
    v = w._escalate_insight(ws, 1, ins, meta, "")
    check(v is not None and v["votes"] == 1,
          "only 1 vote of 3 was performed — early stop on the budget")
    check(abs(svc.cost - 0.005) < 1e-9, "charged for only 1 vote ($0.005), not for 3")
    check(ws.budget_exhausted is True, "the budget is marked exhausted")


# ------------------------------------------------------------------- G3. Escalation serialized under lock (fix #2)

def test_escalation_serialized():
    print("\nG3. Concurrent escalations are serialized by a lock — no double spending (fix #2)")
    import threading
    esc = FakeEscalator(delay=0.05)
    broker, svc = FakeBroker(), FakeService(budget=0.004)  # budget for 1 call
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=None, escalator=esc,
                      db_path=":memory:", default_cadence_sec=0)
    ws = _ws({"api_enabled": True, "model_api": "claude-sonnet-4-6", "escalate_threshold": 0.4,
              "verify_votes": 1, "budget_usd": 0.004})
    meta = {101: {"chunk_id": 101, "text": "y", "source_name": "s", "transcription_id": 1}}
    ins = {"kind": "contradiction", "text": "x", "evidence_chunk_ids": [101], "confidence": 0.9}
    ws.insights_by_event[1] = {"ins": ins, "chunks": meta, "topic_index": 0}
    with w._lock:
        w._sessions["rec_g3"] = ws

    # two parallel manual escalations of the same card (HTTP threads)
    threads = [threading.Thread(target=lambda: w.escalate_now("rec_g3", 1)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(esc.verify_calls == 1, "only 1 Claude call (the second saw the exhausted budget under the lock)")
    check(abs(svc.cost - 0.005) < 1e-9, "charged for exactly 1 call — no double spending")


# ------------------------------------------------------------------- H. API disabled

def test_api_disabled():
    print("\nH. 'Local only' (api_enabled=False) → 0 Claude calls")
    esc = FakeEscalator()
    broker, svc = FakeBroker(), FakeService()
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=None, escalator=esc,
                      db_path=":memory:", default_cadence_sec=0)
    ws = _ws({"api_enabled": False, "model_api": None, "escalate_threshold": 0.4})
    ins = {"kind": "contradiction", "text": "x", "evidence_chunk_ids": [101], "confidence": 0.99}
    meta = {101: {"chunk_id": 101, "text": "y"}}
    out = w._escalate_insight(ws, 1, ins, meta, "")
    check(out is None and esc.verify_calls == 0, "escalation blocked, the API was not called")
    check(w._api_active(ws) is False, "_api_active=False when api_enabled=False")


# ------------------------------------------------------------------- K. Multi-vote verify

def test_multivote_verify():
    print("\nK. Multi-vote verification (real Escalator + fake client)")
    from app.services.copilot.escalate import Escalator
    client = FakeAnthropic(["real", "refuted", "real"])  # 2:1 → real
    esc = Escalator(client_factory=lambda: client)
    ins = {"kind": "contradiction", "text": "40k vs 60k", "evidence_chunk_ids": [101]}
    chunks = [{"chunk_id": 101, "source_name": "Дзвінок", "meeting_date": "2026-05-12",
               "text": "бюджет 40k"}]
    v = esc.verify(insight=ins, evidence_chunks=chunks, votes=3)
    check(client.calls == 3, "3 independent votes made")
    check(v["verdict"] == "real" and v["votes"] == 3, "majority verdict = real (2 of 3)")
    check(v["tokens_in"] == 300 and v["tokens_out"] == 60, "token accounting is summed across votes")

    client2 = FakeAnthropic(["refuted", "real", "refuted"])  # 2:1 → refuted
    esc2 = Escalator(client_factory=lambda: client2)
    v2 = esc2.verify(insight=ins, evidence_chunks=chunks, votes=3)
    check(v2["verdict"] == "refuted", "majority verdict = refuted (2 of 3)")


# ------------------------------------------------------------------- L. Hard mode → 3 votes

def test_hard_mode_votes():
    print("\nL. Hard mode → verify_votes=3 (multi-lens)")
    esc = FakeEscalator()
    broker, svc = FakeBroker(), FakeService(budget=10.0)
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=None, escalator=esc,
                      db_path=":memory:", default_cadence_sec=0)
    ws = _ws({"api_enabled": True, "model_api": "claude-sonnet-4-6", "escalate_threshold": 0.45,
              "verify_votes": 3, "budget_usd": 10.0})
    ins = {"kind": "contradiction", "text": "x", "evidence_chunk_ids": [101], "confidence": 0.9}
    meta = {101: {"chunk_id": 101, "text": "y", "source_name": "s", "transcription_id": 1}}
    w._escalate_insight(ws, 1, ins, meta, "")
    check(esc.votes_seen == [3], "the worker passed votes=3 to verify (from the mode matrix)")


# ------------------------------------------------------------------- M. Opus-on-gnarly

def test_opus_on_gnarly():
    print("\nM. 'Opus on gnarly': uncertain from Sonnet → Opus re-arbitration")
    esc = FakeEscalator(sonnet_verdict="uncertain")
    broker, svc = FakeBroker(), FakeService(budget=10.0)
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=None, escalator=esc,
                      db_path=":memory:", default_cadence_sec=0)
    ws = _ws({"api_enabled": True, "model_api": "claude-sonnet-4-6",
              "model_api_gnarly": "claude-opus-4-8", "escalate_threshold": 0.45,
              "verify_votes": 1, "budget_usd": 10.0})
    ins = {"kind": "contradiction", "text": "x", "evidence_chunk_ids": [101], "confidence": 0.9}
    meta = {101: {"chunk_id": 101, "text": "y", "source_name": "s", "transcription_id": 1}}
    v = w._escalate_insight(ws, 1, ins, meta, "")
    check(esc.verify_calls == 2, "2 calls: Sonnet (uncertain) → Opus arbiter")
    check(any("opus" in (m or "") for m in esc.models_seen), "arbitration done by the Opus model")
    check(v["verdict"] == "real", "final verdict from Opus = real")


# ------------------------------------------------------------------- N. Cost-governor warn

def test_cost_governor_warn():
    print("\nN. Cost-governor: soft warn before the hard stop")
    esc = FakeEscalator()
    # budget $0.006: warn at ≥ $0.0048, hard stop at ≥ $0.006. One verify = $0.005.
    broker, svc = FakeBroker(), FakeService(budget=0.006)
    w = CopilotWorker(broker=broker, live_transcribe_worker=FakeLive([]), recording_service=None,
                      copilot_service=svc, dispatcher=None, escalator=esc,
                      db_path=":memory:", default_cadence_sec=0)
    ws = _ws({"api_enabled": True, "model_api": "claude-sonnet-4-6", "escalate_threshold": 0.4,
              "verify_votes": 1, "budget_usd": 0.006, "budget_warn_ratio": 0.8})
    ins = {"kind": "contradiction", "text": "x", "evidence_chunk_ids": [101], "confidence": 0.9}
    meta = {101: {"chunk_id": 101, "text": "y", "source_name": "s", "transcription_id": 1}}
    w._escalate_insight(ws, 1, ins, meta, "")  # cost 0.005 → 83% of the budget, not yet exhausted
    s1 = [d for (_c, ev, d) in broker.events if ev == "copilot_usage"][-1]["state"]
    check(s1 == "warn", "after 1 call ($0.005 = 83%) — a soft warn (not yet a hard stop)")
    check(ws.budget_exhausted is False, "warn does NOT stop the API (the API is still active)")
    w._escalate_insight(ws, 2, ins, meta, "")  # cost 0.010 ≥ limit → exhausted
    s2 = [d for (_c, ev, d) in broker.events if ev == "copilot_usage"][-1]["state"]
    check(s2 == "budget_exhausted", "after the 2nd ($0.010) — hard stop")


# ------------------------------------------------------------------- O. On-the-fly settings

def test_onthefly_settings():
    print("\nO. Changing mode/importance on the fly (service + worker)")
    import os
    import tempfile
    from app.db.migrations import init_database
    from app.services.copilot.service import CopilotService

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_database(path)
        svc = CopilotService(db_path=path)
        sid = svc.start(settings={"mode": "light", "importance": "low"},
                        recording_session_id="rec_o")["copilot_session_id"]
        cfg0 = svc.get_state(sid)["config"]
        check(cfg0["api_enabled"] is False and cfg0["mode"] == "light",
              "start: light/low → API disabled")

        cfg1 = svc.update_settings(sid, mode="hard")
        check(cfg1["mode"] == "hard" and cfg1["verify_votes"] == 3 and cfg1["api_enabled"] is False,
              "mode change only: hard (votes=3), api stayed disabled (low)")

        from app.services import models as _models
        cfg2 = svc.update_settings(sid, importance="high")
        check(cfg2["api_enabled"] is True
              and cfg2["model_api_gnarly"] == _models.COPILOT_GNARLY_MODEL_DEFAULT,
              "importance change to high: API on, Opus arbiter")
        check(svc.get_state(sid)["config"]["mode"] == "hard", "persist: mode saved in the DB")

        # worker.update_config replaces the live config
        w = CopilotWorker(broker=FakeBroker(), live_transcribe_worker=FakeLive([]),
                          recording_service=None, copilot_service=svc, db_path=path)
        ws = _ws(cfg0)
        with w._lock:
            w._sessions["rec_o"] = ws
        ws.budget_exhausted = True
        ok = w.update_config("rec_o", cfg2)
        check(ok and ws.config["importance"] == "high" and ws.budget_exhausted is False,
              "worker.update_config: the new config applied, the exhaustion flag reset")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ------------------------------------------------------------------- E. Operator actions (real DB)

def test_operator_actions():
    print("\nE. CopilotService.log_action (a real temporary DB + migrations)")
    import os
    import tempfile
    from app.db.migrations import init_database
    from app.services.copilot.service import CopilotService

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_database(path)
        svc = CopilotService(db_path=path)
        res = svc.start(settings={"mode": "medium", "importance": "medium"},
                        recording_session_id="rec_e")
        sid = res["copilot_session_id"]
        check(isinstance(sid, int), "copilot session created in a real DB")

        eid = svc.log_event(sid, "insight_local", source="local", confidence=0.7,
                            payload={"kind": "contradiction", "text": "x"})
        aid = svc.log_action(sid, "pin", ref_event_id=eid, ts_offset_sec=12.5)
        check(isinstance(aid, int) and aid != eid, "log_action returned a new event id")

        import sqlite3
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM copilot_events WHERE id = ?", (aid,)).fetchone()
        conn.close()
        check(row["kind"] == "operator_action" and row["operator_action"] == "pin",
              "event: kind=operator_action, operator_action=pin")
        check(row["source"] == "operator" and abs(row["ts_offset_sec"] - 12.5) < 1e-6,
              "event: source=operator, ts_offset_sec saved")
        import json as _json
        payload = _json.loads(row["payload_json"])
        check(payload.get("ref_event_id") == eid, "payload links to the insight (ref_event_id)")

        # add_usage + cost_estimate (Step 5)
        from app.services.copilot.escalate import cost_estimate
        c = cost_estimate("claude-sonnet-4-6", 1000, 500, 2000)
        check(abs(c - 0.0111) < 1e-6, "cost_estimate: sonnet 1k→500 (+2k cache) = $0.0111")
        # rec_e stays active in the DB; without a checker the service conservatively considers it
        # live (C6) — here none of the recordings is running, so we say so explicitly.
        svc2 = CopilotService(db_path=path, is_recording_active=lambda s: False)
        r2 = svc2.start(settings={"importance": "medium", "budget_usd": 0.01},
                        recording_session_id="rec_u")
        s2 = r2["copilot_session_id"]
        t1 = svc2.add_usage(s2, tokens_in=1000, tokens_out=100, cache_read=0, cost=0.006)
        check(t1["tokens_in"] == 1000 and not t1["exhausted"], "add_usage: accumulation, budget ok")
        t2 = svc2.add_usage(s2, tokens_in=1000, tokens_out=100, cost=0.006)
        check(t2["exhausted"] is True and t2["tokens_in"] == 2000,
              "add_usage: accumulated $0.012 ≥ limit $0.01 → exhausted")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ------------------------------------------------ F. Feedback-by-confidence (T6.8)

def test_feedback_by_confidence_bucket():
    print("\nF. CopilotService.get_feedback_by_confidence_bucket (real DB)")
    import os
    import tempfile
    from app.db.migrations import init_database
    from app.services.copilot.service import CopilotService

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_database(path)
        svc = CopilotService(db_path=path)
        sid = svc.start(settings={"mode": "medium", "importance": "medium"},
                        recording_session_id="rec_f")["copilot_session_id"]

        # An empty DB (no events) — does not fail, returns zeros.
        empty = svc.get_feedback_by_confidence_bucket()
        check(empty["buckets"] == [] and empty["unlinked"] == 0,
              "get_feedback_by_confidence_bucket: empty DB → buckets=[]")

        # Two insights in different confidence buckets: 0.68 (low trust, disliked
        # by the operator) and 0.92 (high trust, liked).
        low_id = svc.log_event(sid, "insight_local", source="local", confidence=0.68,
                               payload={"kind": "contradiction", "text": "низька"})
        high_id = svc.log_event(sid, "insight_verified", source="api", confidence=0.92,
                                payload={"kind": "fact", "text": "висока"})
        svc.log_action(sid, "thumbs_down", ref_event_id=low_id)
        svc.log_action(sid, "thumbs_down", ref_event_id=low_id)
        svc.log_action(sid, "thumbs_up", ref_event_id=high_id)
        # An action without a resolved ref_event_id (an orphan insight) — must go to unlinked.
        svc.log_action(sid, "dismiss", ref_event_id=999999)

        stats = svc.get_feedback_by_confidence_bucket()
        check(stats["unlinked"] == 1, "operator_action without a valid ref → unlinked=1")
        by_range = {b["range"]: b for b in stats["buckets"]}
        check("0.6-0.7" in by_range, "bucket 0.6-0.7 present")
        check(by_range["0.6-0.7"]["thumbs_down"] == 2 and by_range["0.6-0.7"]["total"] == 2,
              "bucket 0.6-0.7: 2 thumbs_down (a signal — perhaps the threshold is too low)")
        check(by_range["0.6-0.7"]["thumbs_down_ratio"] == 1.0,
              "thumbs_down_ratio=1.0 with 0 thumbs_up in the bucket")
        check("0.9-1.0" in by_range and by_range["0.9-1.0"]["thumbs_up"] == 1,
              "bucket 0.9-1.0: 1 thumbs_up")

        # Defaults are NOT touched by this call (the mere fact of aggregation bumps nothing).
        from app.services.copilot import config as copilot_config
        check(copilot_config.MODE_PARAMS["medium"]["escalate_threshold"] == 0.65,
              "aggregation does NOT change the escalate_threshold default")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_timeline():
    print("\nP. Session timeline: persist+link+get_timeline+by_transcription+list (real DB)")
    import os
    import tempfile
    from app.db.migrations import init_database
    from app.services.copilot.service import CopilotService

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_database(path)
        svc = CopilotService(db_path=path)
        sid = svc.start(settings={"mode": "medium", "importance": "medium"},
                        recording_session_id="rec_p")["copilot_session_id"]
        # events scattered by timecodes — the timeline must sort them
        svc.log_event(sid, "insight_verified", ts_offset_sec=32, source="api", confidence=0.9,
                      payload={"kind": "contradiction", "text": "перевірено"})
        svc.log_event(sid, "topic_shift", ts_offset_sec=5, source="local",
                      payload={"label": "Бюджет"})
        svc.log_event(sid, "retrieval", ts_offset_sec=28, source="local",
                      payload={"query": "бюджет", "n": 3})
        svc.log_event(sid, "insight_local", ts_offset_sec=30, source="local", confidence=0.7,
                      payload={"kind": "contradiction", "text": "40k vs 60k",
                               "evidence": [{"chunk_id": 1, "transcription_id": 5,
                                             "source_name": "Дзвінок"}]})
        svc.log_action(sid, "pin", ref_event_id=1, ts_offset_sec=31)
        svc.link_transcription(sid, 99)

        tl = svc.get_timeline(sid)
        check(tl is not None, "get_timeline returned data")
        offs = [e["ts_offset_sec"] for e in tl["events"]]
        check(offs == sorted(offs) and offs[0] == 5,
              "events sorted by timecode (5,28,30,31,32)")
        kinds = [e["kind"] for e in tl["events"]]
        check("insight_local" in kinds and "operator_action" in kinds,
              "all event types in the track (insight + operator action)")
        loc = next(e for e in tl["events"] if e["kind"] == "insight_local")
        check(loc["payload"]["evidence"][0]["chunk_id"] == 1, "the insight payload parsed (evidence)")
        check(tl["aggregates"].get("insight_verified") == 1, "aggregates count the types")

        bt = svc.get_by_transcription(99)
        check(bt and bt["id"] == sid, "get_by_transcription finds the session by transcript")

        sessions = svc.list_sessions()
        row = next((s for s in sessions if s["id"] == sid), None)
        check(row is not None and row["insights"] == 2,
              "list_sessions: a session with an insight counter (local+verified=2)")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_export():
    print("\nQ. Session export (build_markdown/json/notes_digest + collect_notes)")
    from app.services.copilot import export as ex

    timeline = {
        "session": {"id": 1, "mode": "hard", "importance": "high",
                    "cost_estimate": 0.12, "transcription_id": 99},
        "topics": [{"topic_index": 0, "label": "Бюджет", "first_ts": 5, "last_ts": 40}],
        "events": [
            {"id": 10, "kind": "insight_local", "ts_offset_sec": 30, "source": "local",
             "confidence": 0.7, "payload": {"kind": "contradiction", "text": "40k vs 60k",
                "evidence": [{"chunk_id": 1, "transcription_id": 5,
                              "source_name": "Дзвінок", "meeting_date": "2026-05-12"}]}},
            {"id": 11, "kind": "insight_verified", "ts_offset_sec": 31, "source": "api",
             "confidence": 0.92, "payload": {"ref_event_id": 10, "verdict": "real",
                "text": "Підтверджено: бюджет 40k, не 60k"}},
            {"id": 12, "kind": "insight_verified", "ts_offset_sec": 50, "source": "api",
             "confidence": 0.8, "payload": {"kind": "fact", "text": "Sweep-факт з архіву",
                "evidence": []}},
            {"id": 9, "kind": "topic_shift", "ts_offset_sec": 5, "source": "local",
             "payload": {"label": "Бюджет"}},
        ],
    }
    transcript = {"source_name": "Дзвінок з клієнтом", "segments": [
        {"start": 4, "speaker": "self", "text": "Привіт"},
        {"start": 29, "speaker": "other", "text": "Бюджет 60k"}]}

    notes = ex.collect_notes(timeline["events"])
    check(len(notes) == 2, "collect_notes: 2 notes (local+verified merged, sweep separately)")
    c = next(n for n in notes if n["kind"] == "contradiction")
    check(c["source"] == "api" and c["verdict"] == "real", "insight merged with verification (api/real)")
    check(c["text"] == "Підтверджено: бюджет 40k, не 60k", "the text taken from the verified version")
    check(c["evidence"][0]["chunk_id"] == 1, "evidence carried over from the local insight")

    md = ex.build_markdown(timeline, transcript)
    check("ПРОТИРІЧЧЯ" in md and "перевірено" in md, "md: a note with kind+badge")
    check("[0:04]" in md and "Ви:" in md, "md: dialogue with timecodes and speaker")
    check("Дзвінок (2026-05-12)" in md, "md: link to the archive source")
    check("## Теми" in md and "Бюджет" in md, "md: topics section")

    j = ex.build_json(timeline, transcript)
    check(len(j["dialog"]) == 2 and len(j["notes"]) == 2, "json: dialogue(2) + notes(2)")
    check(j["session"]["id"] == 1 and j["transcript_name"] == "Дзвінок з клієнтом", "json: session meta")

    dig = ex.notes_digest(timeline, transcript)
    check("Протиріччя:" in dig and "Факти:" in dig, "digest: grouping by kind")
    check("[перевірено]" in dig and "джерела: Дзвінок" in dig, "digest: verification mark + sources")
    check(len(dig) > 20, "digest sufficient for re-ingest")


if __name__ == "__main__":
    test_dispatcher()
    test_worker_dispatch()
    test_topic_return_reuse()
    test_topic_tracker_llm_driven()
    test_dedup()
    test_unanchored_filter()
    test_shown_chunks_bounded()
    test_escalation_and_sweep()
    test_budget_hardstop()
    test_multivote_budget_earlystop()
    test_escalation_serialized()
    test_api_disabled()
    test_multivote_verify()
    test_hard_mode_votes()
    test_opus_on_gnarly()
    test_cost_governor_warn()
    test_onthefly_settings()
    test_timeline()
    test_export()
    test_operator_actions()
    test_feedback_by_confidence_bucket()
    print(f"\n{'='*50}")
    if _fail:
        print(f"FAILED: {_fail} checks failed")
        sys.exit(1)
    print("ALL CHECKS PASSED ✓")
