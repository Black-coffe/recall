"""Тести «тихішого» копілота (Трек 3): бюджет карток + верифікація до показу.

Привід (заміри на 80 реальних сесіях): 5743 локальних інсайти ≈ 72 картки за
дзвінок, з них Claude перевірив 5.9%, а оператор не відреагував ЖОДНОГО разу
(operator_action = NULL у всіх 9706 подіях при робочих кнопках 👍/👎). Тобто
проблема не в браку сигналів, а в їх кількості й недоведеності.

Тут перевіряємо саме контракт показу, без мережі й GPU: воркер зі підставними
диспетчером/ескалатором/сервісом.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

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
    """Без API (лише локально) копілот НЕ мовчить — інакше деградація зла."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    cfg = cp_config.resolve_settings({"importance": "low"})   # api вимкнено
    assert cfg["verified_only"] is False
    ws = _ws(cfg)

    w._process_insight(ws, 0, _insight(), CHUNKS, "профіль")
    assert len(broker.cards()) == 1
    assert ws.cards_shown == 1


def test_card_budget_stops_after_limit():
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    cfg = cp_config.resolve_settings({"importance": "low", "mode": "light"})
    cfg["min_card_gap_sec"] = 0        # тут перевіряємо саме бюджет
    ws = _ws(cfg)

    for i in range(6):
        w._process_insight(ws, 0, _insight(text=f"інсайт {i}"), CHUNKS, "профіль")

    assert cfg["max_cards"] == 3
    assert len(broker.cards()) == 3, "понад бюджет карток не показуємо"
    assert ws.cards_suppressed == 3
    # Усі шість лишились в історії сесії — притишене не зникає, а йде в пост-бриф.
    assert len([e for e in svc.logged if e["kind"] == "insight_local"]) == 6
    # Оператору один раз сказали, що стало тихо (щоб не читалось як «зламався»).
    quiet = [s for s in broker.statuses() if s.get("reason") == "card_budget"]
    assert len(quiet) == 1


def test_min_gap_spreads_cards_over_call():
    """Пауза між картками окремо від бюджету: інакше все вилітає в перші хвилини."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    cfg = cp_config.resolve_settings({"importance": "low"})
    cfg["min_card_gap_sec"] = 999
    ws = _ws(cfg)

    w._process_insight(ws, 0, _insight("перший"), CHUNKS, "профіль")
    w._process_insight(ws, 0, _insight("другий"), CHUNKS, "профіль")
    assert len(broker.cards()) == 1

    ws.last_card_ts = time.monotonic() - 1000     # пауза минула
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
    assert cards[0]["text"] == "Claude: підтверджено", "показуємо вердикт, а не чернетку локалки"
    assert cards[0]["evidence"][0]["source_name"] == "Дзвінок"


@pytest.mark.parametrize("verdict", ["refuted", "uncertain"])
def test_verified_only_hides_unconfirmed(verdict):
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    w._escalator = _Escalator(verdict=verdict)
    ws = _api_ws()

    w._process_insight(ws, 0, _insight(), CHUNKS, "профіль")

    assert broker.cards() == [], f"вердикт {verdict} не показуємо оператору"
    assert ws.cards_suppressed == 1
    # але подія збережена — пост-колл розбір її побачить
    assert any(e["kind"] == "insight_local" for e in svc.logged)


def test_verified_only_skips_insight_below_escalation_bar():
    """Низька впевненість локалки не витрачає виклик Claude і не стає карткою."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    esc = _Escalator()
    w._escalator = esc
    ws = _api_ws()

    w._process_insight(ws, 0, _insight(conf=0.10), CHUNKS, "профіль")

    assert broker.cards() == [] and esc.calls == 0


def test_verified_only_degrades_when_api_dies_midcall():
    """Бюджет вичерпано посеред дзвінка → повертаємось до локальних карток."""
    broker, svc = _Broker(), _Svc()
    w = _worker(broker, svc)
    w._escalator = _Escalator()
    ws = _api_ws()
    ws.budget_exhausted = True          # API більше недоступний

    w._process_insight(ws, 0, _insight(), CHUNKS, "профіль")

    assert len(broker.cards()) == 1, "мовчання гірше за неперевірену, але анкоровану картку"
    assert broker.cards()[0]["source"] == "local"


def test_gate_reports_reason():
    w = _worker(_Broker(), _Svc())
    ws = _ws({"max_cards": 1, "min_card_gap_sec": 60})
    assert w._card_gate(ws) is None
    w._count_card(ws)
    assert w._card_gate(ws) == "budget"
    ws.config["max_cards"] = 5
    assert w._card_gate(ws) == "too_soon"
