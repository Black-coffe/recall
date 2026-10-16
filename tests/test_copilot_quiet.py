"""Tests of the "quieter" copilot (Track 3): card budget + verification before display.

The cause (measurements on 80 real sessions): 5743 local insights ≈ 72 cards per
call, of which Claude verified 5.9%, and the operator reacted NOT ONCE
(operator_action = NULL in all 9706 events with working 👍/👎 buttons). So
the problem is not a lack of signals but their number and lack of proof.

Here we check exactly the display contract, without network and GPU: a worker with stub
dispatcher/escalator/service.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.services.categories import CategoryError
from app.services.copilot import config as cp_config
from app.services.copilot.worker import CopilotWorker, _WS


class _Broker:
    def __init__(self):
        self.events = []

    def publish(self, channel, event, data):
        self.events.append((event, data))

    def cards(self):
        return [d for e, d in self.events if e == "copilot_insight"]

    def statuses(self):
        return [d for e, d in self.events if e == "copilot_status"]


class _Svc:
    def __init__(self):
        self.logged = []
        self._id = 0

    def log_event(self, session_id, kind, **kw):
        self._id += 1
        self.logged.append({"id": self._id, "kind": kind, **kw})
        return self._id


def _worker(broker, svc) -> CopilotWorker:
    return CopilotWorker(broker=broker, live_transcribe_worker=None,
                         recording_service=None, copilot_service=svc,
                         db_path=None, embed_fn=lambda t: None)


def _ws(config: dict) -> _WS:
    ws = _WS(copilot_session_id=1, recording_session_id="sid", config=config,
             tracker=SimpleNamespace(current=0, topic_list=lambda: []))
    ws.last_offset = 10.0
    return ws


def _insight(text="щось важливе", conf=0.9, evidence=("c1",)):
    return {"kind": "contradiction", "text": text, "confidence": conf,
            "evidence_chunk_ids": list(evidence)}


CHUNKS = {"c1": {"chunk_id": "c1", "transcription_id": 7, "source_name": "Дзвінок",
                 "meeting_date": "2026-05-14", "start_time": 12.0}}


def test_local_mode_shows_anchored_insight():
    """Without the API (local only) the copilot does NOT go silent — otherwise the degradation is evil."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    cfg = cp_config.resolve_settings({"importance": "low"})   # api disabled
    assert cfg["verified_only"] is False
    ws = _ws(cfg)

    w._process_insight(ws, 0, _insight(), CHUNKS, "профіль")
    assert len(broker.cards()) == 1
    assert ws.cards_shown == 1


def test_card_budget_stops_after_limit():
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    cfg = cp_config.resolve_settings({"importance": "low", "mode": "light"})
    cfg["min_card_gap_sec"] = 0        # here we test exactly the budget
    ws = _ws(cfg)

    for i in range(6):
        w._process_insight(ws, 0, _insight(text=f"інсайт {i}"), CHUNKS, "профіль")

    assert cfg["max_cards"] == 3
    assert len(broker.cards()) == 3, "we do not show cards beyond the budget"
    assert ws.cards_suppressed == 3
    # All six stayed in the session history — the muted does not vanish, it goes to the post-brief.
    assert len([e for e in svc.logged if e["kind"] == "insight_local"]) == 6
    # The operator was told once that it got quiet (so it is not read as "it broke").
    quiet = [s for s in broker.statuses() if s.get("reason") == "card_budget"]
    assert len(quiet) == 1


def test_min_gap_spreads_cards_over_call():
    """The pause between cards is separate from the budget: otherwise everything flies out in the first minutes."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    cfg = cp_config.resolve_settings({"importance": "low"})
    cfg["min_card_gap_sec"] = 999
    ws = _ws(cfg)

    w._process_insight(ws, 0, _insight("перший"), CHUNKS, "профіль")
    w._process_insight(ws, 0, _insight("другий"), CHUNKS, "профіль")
    assert len(broker.cards()) == 1

    ws.last_card_ts = time.monotonic() - 1000     # the pause has passed
    w._process_insight(ws, 0, _insight("третій"), CHUNKS, "профіль")
    assert len(broker.cards()) == 2


class _Escalator:
    def __init__(self, verdict="real", text="перевірений текст"):
        self.calls = 0
        self._verdict = verdict
        self._text = text

    def verify(self, **kw):
        self.calls += 1
        return {"verdict": self._verdict, "confidence": 0.91, "text": self._text,
                "tokens_in": 10, "tokens_out": 5, "model": "test-model"}


def _api_ws(**over):
    cfg = cp_config.resolve_settings({"importance": "medium"})
    cfg.update({"model_api": "test-model", "min_card_gap_sec": 0})
    cfg.update(over)
    ws = _ws(cfg)
    return ws


def test_verified_only_shows_card_after_real_verdict():
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    w._escalator = _Escalator(verdict="real", text="Claude: підтверджено")
    ws = _api_ws()
    assert ws.config["verified_only"] is True

    w._process_insight(ws, 0, _insight(), CHUNKS, "профіль")

    cards = broker.cards()
    assert len(cards) == 1
    assert cards[0]["source"] == "api" and cards[0]["verdict"] == "real"
    assert cards[0]["text"] == "Claude: підтверджено", "we show the verdict, not the local draft"
    assert cards[0]["evidence"][0]["source_name"] == "Дзвінок"


@pytest.mark.parametrize("verdict", ["refuted", "uncertain"])
def test_verified_only_hides_unconfirmed(verdict):
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    w._escalator = _Escalator(verdict=verdict)
    ws = _api_ws()

    w._process_insight(ws, 0, _insight(), CHUNKS, "профіль")

    assert broker.cards() == [], f"we do not show the {verdict} verdict to the operator"
    assert ws.cards_suppressed == 1
    # but the event is saved — the post-call review will see it
    assert any(e["kind"] == "insight_local" for e in svc.logged)


def test_verified_only_skips_insight_below_escalation_bar():
    """Low confidence of the local model does not spend a Claude call and does not become a card."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    esc = _Escalator()
    w._escalator = esc
    ws = _api_ws()

    w._process_insight(ws, 0, _insight(conf=0.10), CHUNKS, "профіль")

    assert broker.cards() == [] and esc.calls == 0


def test_verified_only_degrades_when_api_dies_midcall():
    """The budget is exhausted mid-call → we return to local cards."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    w._escalator = _Escalator()
    ws = _api_ws()
    ws.budget_exhausted = True          # the API is no longer available

    w._process_insight(ws, 0, _insight(), CHUNKS, "профіль")

    assert len(broker.cards()) == 1, "silence is worse than an unverified but anchored card"
    assert broker.cards()[0]["source"] == "local"


def test_gate_reports_reason():
    w = _worker(_Broker(), _Svc())
    ws = _ws({"max_cards": 1, "min_card_gap_sec": 60})
    assert w._card_gate(ws) is None
    w._count_card(ws)
    assert w._card_gate(ws) == "budget"
    ws.config["max_cards"] = 5
    assert w._card_gate(ws) == "too_soon"


# ---------------------------------------------------------------------------
# Story 25: a namesake scope ("Маркетинг" in two directions) — visible to the operator,
# we do not silently widen the search to the whole archive for the whole session.
# ---------------------------------------------------------------------------

_AMBIGUOUS_PATHS = ["Фонд Робота → Маркетинг", "Фріланс → Маркетинг"]


def test_scope_ambiguous_notifies_operator_with_both_paths(monkeypatch, caplog):
    """Round 6 major 1: the channel is the existing insight path (log_event + copilot_insight
    with shown=true), NOT a separate copilot_status; outside the card budget."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    ws = _ws({"scope_projects": ["Маркетинг"]})

    def _raise(db_path, names):
        raise CategoryError("ambiguous", candidates=list(_AMBIGUOUS_PATHS))

    monkeypatch.setattr("app.services.scope.scope_filter_ids", _raise)

    with caplog.at_level("WARNING"):
        result = w._scope_entity_ids(ws, "dummy.db")

    assert result is None, "namesake — search WITHOUT narrowing, as with not-found"
    assert any(all(p in rec.message for p in _AMBIGUOUS_PATHS) for rec in caplog.records)

    # No copilot_status with state:quiet for this case.
    assert broker.statuses() == []

    cards = broker.cards()
    assert len(cards) == 1
    notice = cards[0]
    assert notice["kind"] == "scope_notice"
    assert notice["candidates"] == _AMBIGUOUS_PATHS
    assert "кількох напрямках" in notice["text"]

    # It does not take the card budget/gate (it did not pass through _card_gate/_count_card).
    assert ws.cards_shown == 0

    # It is persisted into the session history (get_state()/MCP get_live_copilot/timeline
    # see it via log_event, not only SSE).
    assert len(svc.logged) == 1
    logged = svc.logged[0]
    assert logged["kind"] == "insight_local"
    assert logged["payload"]["kind"] == "scope_notice"
    assert logged["payload"]["shown"] is True
    assert logged["payload"]["candidates"] == _AMBIGUOUS_PATHS


def test_scope_ambiguous_notice_not_repeated_when_cache_does_not_save_you(monkeypatch):
    """Minor 7 r0: the flag alone prevents a duplicate, proven with a cleared cache
    ws._scope_ids — otherwise the test does not prove that exactly the flag saves."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    ws = _ws({"scope_projects": ["Маркетинг"]})

    def _raise(db_path, names):
        raise CategoryError("ambiguous", candidates=list(_AMBIGUOUS_PATHS))

    monkeypatch.setattr("app.services.scope.scope_filter_ids", _raise)

    w._scope_entity_ids(ws, "dummy.db")
    assert len(broker.cards()) == 1

    # The scope cache is cleared — without the flag the second tick would again reach
    # scope_filter_ids and publish a second card.
    ws._scope_ids = None
    w._scope_entity_ids(ws, "dummy.db")

    assert len(broker.cards()) == 1, "the flag, not the cache, prevents the duplicate"


def test_scope_not_found_only_warns_no_notice(monkeypatch):
    """A scope that is not in the archive (not a namesake) — as before: WARNING without a message."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    ws = _ws({"scope_projects": ["Невідомий Проєкт"]})

    monkeypatch.setattr("app.services.scope.scope_filter_ids", lambda db_path, names: [])

    result = w._scope_entity_ids(ws, "dummy.db")

    assert result is None
    assert broker.statuses() == []
    assert broker.cards() == [], "not-found does not report namesakes"


def test_scope_other_exception_behaves_as_before(monkeypatch):
    """Another exception from scope_filter_ids — as before: no message to the operator."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    ws = _ws({"scope_projects": ["Маркетинг"]})

    def _raise(db_path, names):
        raise RuntimeError("ouch")

    monkeypatch.setattr("app.services.scope.scope_filter_ids", _raise)

    result = w._scope_entity_ids(ws, "dummy.db")

    assert result is None
    assert broker.statuses() == []
    assert broker.cards() == []


def test_scope_notice_excluded_from_rag_reingest_digest():
    """Re-ingest into RAG (notes_digest) takes only the fixed groups of insights
    (contradiction/fact/question/clarification) — scope_notice does not
    get there, even if in the session events it lies next to them."""
    from app.services.copilot import export as cp_export

    events = [
        {"id": 1, "kind": "insight_local", "ts_offset_sec": 5.0, "source": "local",
         "payload": {"kind": "scope_notice", "text": "Скоуп «Маркетинг» є в кількох напрямках",
                     "candidates": _AMBIGUOUS_PATHS, "evidence": [], "shown": True}},
        {"id": 2, "kind": "insight_local", "ts_offset_sec": 6.0, "source": "local",
         "payload": {"kind": "fact", "text": "Справжній факт з архіву", "evidence": []}},
    ]
    digest = cp_export.notes_digest({"session": {"id": 1}, "events": events, "topics": []})

    assert "Скоуп «Маркетинг»" not in digest
    assert "Справжній факт з архіву" in digest
