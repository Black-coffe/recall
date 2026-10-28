"""«What did I miss» live summary (wave4-client-calls-03): windows, one Sonnet call through the
retry helper, per-session spend cap, agent refusal, never the local LLM.

The route runs on a light Flask app with the copilot blueprint; `app.state` is substituted,
so no app.py and no live DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

from app.blueprints.copilot import copilot_bp
from app.core import auth
from app.services import live_missed, local_llm
from app.services.copilot import escalate
from app.services.recording.session_store import SessionStoreError

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}
MCP = {'X-Recall-Actor': 'mcp'}

SEGS = [
    {'start': 0.0, 'end': 10.0, 'speaker': 'other', 'text': 'Добрий день, почнемо.'},
    {'start': 10.0, 'end': 20.0, 'speaker': 'self', 'text': 'Так, я слухаю.'},
    {'start': 200.0, 'end': 210.0, 'speaker': 'other', 'text': 'Вирішили запуск у понеділок.'},
    {'start': 400.0, 'end': 420.0, 'speaker': 'other', 'text': 'Ви надішлете договір до пʼятниці?'},
]

ANSWER = "- Decided: launch on Monday.\n- Promised: nothing yet.\n- Asked of you: send the contract by Friday."


class _Resp:
    def __init__(self, text=ANSWER, tin=1000, tout=60):
        self.content = [SimpleNamespace(type='text', text=text)]
        self.usage = SimpleNamespace(input_tokens=tin, output_tokens=tout)


class _FakeRetry:
    def __init__(self, resp=None, exc=None):
        self.calls, self.kwargs = 0, []
        self.resp, self.exc = resp or _Resp(), exc

    def __call__(self, fn, **kw):
        self.calls += 1
        self.kwargs.append(fn.keywords)
        if self.exc:
            raise self.exc
        return self.resp


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setenv('LIVE_MISSED_BUDGET_USD', '0.10')
    monkeypatch.setattr(live_missed, '_spent', {})

    def _boom(*a, **kw):
        raise AssertionError('local_llm.generate must never be called')
    monkeypatch.setattr(local_llm, 'generate', _boom)


@pytest.fixture
def fake(monkeypatch):
    f = _FakeRetry()
    monkeypatch.setattr(live_missed, 'call_with_retry', f)
    return f


# ---- select_window ----

def test_since_self_takes_segments_after_last_self():
    w = live_missed.select_window(SEGS, 'since_self', None)
    assert [s['start'] for s in w['segments']] == [200.0, 400.0]
    assert w['since'] == 20.0
    assert w['truncated'] is False


def test_since_self_without_self_takes_all_with_null_since():
    segs = [s for s in SEGS if s['speaker'] != 'self']
    w = live_missed.select_window(segs, 'since_self', None)
    assert len(w['segments']) == 3
    assert w['since'] is None


@pytest.mark.parametrize('window,expected', [
    ('2m', [400.0]), ('5m', [200.0, 400.0]), ('10m', [0.0, 10.0, 200.0, 400.0]),
])
def test_minute_windows_are_relative_to_last_segment(window, expected):
    w = live_missed.select_window(SEGS, window, None)
    assert [s['start'] for s in w['segments']] == expected


def test_whole_trims_to_max_chars_and_flags(monkeypatch):
    monkeypatch.setattr(live_missed, 'MAX_CHARS', 60)
    w = live_missed.select_window(SEGS, 'whole', None)
    assert w['truncated'] is True
    assert w['segments'][-1]['start'] == 400.0
    assert len(w['segments']) < len(SEGS)
    assert len(live_missed.window_text(w['segments'])) <= 60


def test_whole_short_keeps_everything():
    w = live_missed.select_window(SEGS, 'whole', None)
    assert len(w['segments']) == 4 and w['truncated'] is False


def test_unknown_window_raises():
    with pytest.raises(ValueError):
        live_missed.select_window(SEGS, '1h', None)


# ---- summarize ----

def test_summarize_one_sonnet_call(fake):
    res = live_missed.summarize('s1', 'since_self', segments=SEGS, now=None)
    assert 3 <= len(res['lines']) <= 5 and all(line.strip() for line in res['lines'])
    assert res['text']
    assert fake.calls == 1
    kw = fake.kwargs[0]
    assert kw['model'] == escalate.DEFAULT_MODEL
    prompt = kw['messages'][0]['content']
    assert 'Вирішили запуск у понеділок.' in prompt
    assert 'Так, я слухаю.' not in prompt   # before the window
    assert res['cost_usd'] > 0
    assert live_missed.spent('s1') == pytest.approx(res['cost_usd'])


def test_spent_grows_and_budget_refuses_before_call(fake, monkeypatch):
    first = live_missed.summarize('s1', 'since_self', segments=SEGS)
    monkeypatch.setenv('LIVE_MISSED_BUDGET_USD', str(first['cost_usd'] + 0.0001))
    with pytest.raises(live_missed.BudgetExceeded) as ei:
        live_missed.summarize('s1', 'since_self', segments=SEGS)
    assert fake.calls == 1
    assert ei.value.spent_usd == pytest.approx(first['cost_usd'])
    assert live_missed.spent('s1') == pytest.approx(first['cost_usd'])


def test_zero_budget_refuses_first_call(fake, monkeypatch):
    monkeypatch.setenv('LIVE_MISSED_BUDGET_USD', '0')
    with pytest.raises(live_missed.BudgetExceeded):
        live_missed.summarize('s1', 'whole', segments=SEGS)
    assert fake.calls == 0


def test_failed_call_releases_reservation(monkeypatch):
    monkeypatch.setattr(live_missed, 'call_with_retry', _FakeRetry(exc=RuntimeError('boom')))
    with pytest.raises(live_missed.Unavailable):
        live_missed.summarize('s1', 'whole', segments=SEGS)
    assert live_missed.spent('s1') == 0


# ---- route ----

MANIFESTS = {'rec-ui': {'started_by': 'ui'}, 'rec-mcp': {'started_by': 'mcp'},
             'rec-2': {'started_by': 'ui'}}


class _Store:
    def read(self, sid):
        if sid not in MANIFESTS:
            raise SessionStoreError(f'no manifest {sid}')
        return MANIFESTS[sid]


class _Live:
    def __init__(self, active):
        self.active = set(active)

    def is_active(self, sid):
        return sid in self.active

    def get_preview(self, sid):
        return list(SEGS)


def _client(monkeypatch, live=('rec-ui',)):
    monkeypatch.setattr('app.state.recording_service',
                        SimpleNamespace(store=_Store(), active_session_ids=lambda: list(live)))
    monkeypatch.setattr('app.state.live_transcribe_worker', _Live(live))
    app = Flask(__name__)
    app.config['DATABASE'] = ':memory:'
    app.register_blueprint(copilot_bp)
    return app.test_client()


def test_route_200_contract(monkeypatch, fake):
    r = _client(monkeypatch).post('/api/copilot/live-missed', json={}, headers=UI)
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {'success', 'text', 'lines', 'window', 'segments_used', 'since',
                         'truncated', 'cost_usd', 'spent_usd', 'budget_usd'}
    assert body['success'] is True and body['window'] == 'since_self'
    assert body['segments_used'] == 2 and body['since'] == 20.0
    assert body['budget_usd'] == 0.10


def test_route_bad_window_400(monkeypatch, fake):
    r = _client(monkeypatch).post('/api/copilot/live-missed', json={'window': '1h'}, headers=UI)
    assert r.status_code == 400 and r.get_json()['error_code'] == 'INVALID_ARGUMENT'


@pytest.mark.parametrize('sid', ['nope', 'rec-2'])
def test_route_unknown_or_not_live_404(monkeypatch, fake, sid):
    r = _client(monkeypatch).post('/api/copilot/live-missed', json={'session_id': sid}, headers=UI)
    assert r.status_code == 404 and r.get_json()['error_code'] == 'NOT_FOUND'
    assert fake.calls == 0


def test_route_two_live_no_session_409(monkeypatch, fake):
    r = _client(monkeypatch, live=('rec-ui', 'rec-2')).post(
        '/api/copilot/live-missed', json={}, headers=UI)
    assert r.status_code == 409 and r.get_json()['error_code'] == 'AMBIGUOUS_SESSION'


@pytest.mark.parametrize('headers', [UI, MCP], ids=['ui', 'mcp'])
def test_route_agent_session_403_for_every_caller(monkeypatch, fake, headers):
    r = _client(monkeypatch, live=('rec-mcp',)).post(
        '/api/copilot/live-missed', json={'session_id': 'rec-mcp'}, headers=headers)
    assert r.status_code == 403 and r.get_json()['error_code'] == 'AGENT_SESSION'
    assert fake.calls == 0


def test_route_over_budget_429(monkeypatch, fake):
    monkeypatch.setenv('LIVE_MISSED_BUDGET_USD', '0')
    r = _client(monkeypatch).post('/api/copilot/live-missed', json={}, headers=UI)
    assert r.status_code == 429
    body = r.get_json()
    assert body['error_code'] == 'LIVE_BUDGET_EXHAUSTED'
    assert body['spent_usd'] == 0 and body['budget_usd'] == 0
    assert fake.calls == 0


def test_route_no_key_503(monkeypatch, fake):
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    r = _client(monkeypatch).post('/api/copilot/live-missed', json={}, headers=UI)
    assert r.status_code == 503 and r.get_json()['error_code'] == 'UNAVAILABLE'
    assert fake.calls == 0
