"""Tests for app/services/recording/call_watch.py — «Записати?» / «Зупинити?» by the call (K2).

Pure state machine: clock, session enumerator, live recordings, stop call and notifier are fakes.
No COM, no toasts, no Flask.
"""

import json

import pytest

from app.services.recording import call_watch as cw

MIC = 'Мікрофон (FOX)'
LOUD, QUIET = 0.3, 0.0


class FakeNotifier:
    def __init__(self):
        self.shown = []     # (tag, title, body, actions)
        self.removed = []

    def show(self, tag, title, body, actions, *, launch_url=None, reminder=False):
        self.shown.append((tag, title, body, actions))
        return True

    def remove(self, tag):
        self.removed.append(tag)
        return True


class Rig:
    """Ticks the watcher one simulated second at a time."""

    def __init__(self, tmp_path=None, *, prompt_seconds=30, autostop_minutes=10, publisher=None,
                 reprompt_minutes=10, action='prompt', auto_record_fn=None):
        self.t = 0.0
        self.snap: dict | None = {}
        self.live: list[dict] = []
        self.stopped: list[str] = []
        self.stop_error: Exception | None = None
        self.notifier = FakeNotifier()
        self.log_path = str(tmp_path / 'call_watch.jsonl') if tmp_path else None
        self.w = cw.CallWatcher(
            apps=cw.parse_apps(cw.DEFAULT_APPS), prompt_seconds=prompt_seconds,
            autostop_minutes=autostop_minutes, base_url='http://127.0.0.1:5050',
            enumerate_calls=lambda: self.snap, live_sessions=lambda: self.live,
            stop_fn=self._stop, notifier=self.notifier,
            clock=lambda: self.t, wall=lambda: 1_760_000_000.0 + self.t, log_path=self.log_path,
            publisher=publisher, reprompt_minutes=reprompt_minutes, action=action,
            auto_record_fn=auto_record_fn)

    def _stop(self, sid):
        if self.stop_error:
            raise self.stop_error
        self.stopped.append(sid)

    def run(self, seconds, snap=None):
        if snap is not None:
            self.snap = snap
        for _ in range(int(seconds)):
            self.w.tick()
            self.t += 1.0

    def prompts(self, kind):
        return [s for s in self.notifier.shown if any(a[1].endswith('/' + ('record' if kind == 'record' else 'stop'))
                                                       for a in s[3])]

    def events(self):
        with open(self.log_path, encoding='utf-8') as fh:
            return [json.loads(line) for line in fh]


def app_snap(exe='zoom.exe', *, mic=LOUD, out=LOUD, mic_active=True, out_active=True):
    return {exe: {'mic': {'active': mic_active, 'peak': mic, 'endpoint': MIC if mic_active else None},
                  'out': {'active': out_active, 'peak': out, 'endpoint': 'Навушники'}}}


def recording(sid='rec1', source=None, *, paused=False, mic_peak=0.0, sys_peak=0.0):
    return {'sid': sid, 'system_source': source, 'is_paused': paused,
            'mic_peak': mic_peak, 'sys_peak': sys_peak}


# ---------------------------------------------------------------- «Записати?»

def test_call_with_both_sides_for_30s_prompts_exactly_once(tmp_path):
    r = Rig(tmp_path)
    r.run(29, app_snap())
    assert r.prompts('record') == []
    r.run(2)
    assert len(r.prompts('record')) == 1
    tag, title, body, actions = r.prompts('record')[0]
    assert 'Zoom' in title and 'Zoom' in body
    assert [a[0] for a in actions] == ['Записати', 'Інші джерела']
    assert actions[0][1] == f'http://127.0.0.1:5050/api/recording/autoprompt/{tag}/record'
    r.run(300)                                   # the same call goes on: no second prompt
    assert len(r.prompts('record')) == 1


def test_dictation_app_never_prompts():
    r = Rig()
    r.run(120, app_snap('voicedrop.exe'))
    r.run(120, app_snap('wispr flow.exe'))
    assert r.notifier.shown == []


def test_sound_without_microphone_never_prompts():
    r = Rig()
    r.run(600, app_snap('chrome.exe', mic=QUIET, mic_active=False))   # YouTube / music
    assert r.notifier.shown == []


def test_muted_microphone_is_not_a_call():
    r = Rig()
    r.run(120, app_snap(mic=QUIET))
    assert r.notifier.shown == []


def test_streak_must_be_unbroken():
    r = Rig()
    r.run(20, app_snap())
    r.run(1, {})
    r.run(20, app_snap())
    assert r.notifier.shown == []
    r.run(11)
    assert len(r.prompts('record')) == 1


def test_app_already_recorded_is_not_prompted(tmp_path):
    r = Rig(tmp_path)
    r.live = [recording(source={'mode': 'include', 'apps': ['zoom.exe']})]
    r.run(60, app_snap())
    assert r.notifier.shown == []
    assert 'prompt_skipped' in [e['event'] for e in r.events()]


def test_mic_only_recording_does_not_count_as_recording_the_call():
    r = Rig()
    r.live = [recording(source=None)]
    r.run(31, app_snap())
    assert len(r.prompts('record')) == 1


def test_enumeration_failure_holds_state():
    r = Rig()
    r.run(31, app_snap())
    r.run(120, None)                             # COM down: neither an end nor a new prompt
    assert r.notifier.removed == []
    r.run(5, app_snap())
    assert len(r.prompts('record')) == 1


# ---------------------------------------------------------------- end of call, «Зупинити?», autostop

def test_unanswered_prompt_is_withdrawn_when_call_ends():
    r = Rig()
    r.run(31, app_snap())
    tag = r.prompts('record')[0][0]
    r.run(int(cw.END_INACTIVE_SEC) + 1, {})
    assert tag in r.notifier.removed
    assert r.w.pending() == []


def _seconds_until_stop_prompt(r, after_snap):
    r.snap = after_snap
    for waited in range(1, 121):
        r.run(1)
        if r.prompts('stop'):
            return waited
    return None


def test_call_end_asks_to_stop_within_60s(tmp_path):
    r = Rig(tmp_path)
    r.live = [recording(source={'mode': 'include', 'apps': ['zoom.exe']})]
    r.run(120, app_snap())
    waited = _seconds_until_stop_prompt(r, {})
    assert waited is not None and waited <= 60
    assert len(r.prompts('stop')) == 1
    assert next(e for e in r.events() if e['event'] == 'call_end')['reason'] == 'inactive'


def test_call_end_by_silence_when_app_keeps_sessions_open(tmp_path):
    r = Rig(tmp_path)
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    waited = _seconds_until_stop_prompt(r, app_snap(mic=QUIET, out=QUIET))
    assert waited is not None and waited <= 60
    assert next(e for e in r.events() if e['event'] == 'call_end')['reason'] == 'silence'


def test_paused_recording_gets_no_stop_prompt():
    r = Rig()
    r.live = [recording(source={'mode': 'all'}, paused=True)]
    r.run(120, app_snap())
    r.run(60, {})
    assert r.prompts('stop') == []


def test_autostop_after_silence_minutes(tmp_path):
    r = Rig(tmp_path, autostop_minutes=10)
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    r.run(30, {})
    assert len(r.prompts('stop')) == 1
    r.run(9 * 60)
    assert r.stopped == []
    r.run(61)
    assert r.stopped == ['rec1']
    assert any('запис зупинено' in s[1] for s in r.notifier.shown)
    r.run(700)                                   # once only
    assert r.stopped == ['rec1']
    assert 'autostop' in [e['event'] for e in r.events()]


def test_sound_resets_the_silence_clock():
    r = Rig(autostop_minutes=10)
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    r.run(30, {})
    r.run(9 * 60)
    r.live = [recording(source={'mode': 'all'}, mic_peak=0.4)]
    r.run(1)
    r.live = [recording(source={'mode': 'all'})]
    r.run(9 * 60)
    assert r.stopped == []
    r.run(61)
    assert r.stopped == ['rec1']


def test_autostop_failure_is_logged_not_raised(tmp_path):
    r = Rig(tmp_path, autostop_minutes=1)
    r.stop_error = RuntimeError('boom')
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    r.run(30, {})
    r.run(70)
    assert 'autostop_failed' in [e['event'] for e in r.events()]


def test_recording_gone_cancels_the_watch():
    r = Rig(autostop_minutes=1)
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    r.run(30, {})
    tag = r.prompts('stop')[0][0]
    r.live = []
    r.run(100)
    assert r.stopped == [] and tag in r.notifier.removed


def test_new_call_in_same_recording_cancels_stop_prompt():
    r = Rig(autostop_minutes=1)
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    r.run(30, {})
    tag = r.prompts('stop')[0][0]
    r.run(31, app_snap())
    assert tag in r.notifier.removed
    r.run(100, app_snap())
    assert r.stopped == []


# ---------------------------------------------------------------- buttons

def test_consume_is_single_use_and_checks_action():
    r = Rig()
    r.run(31, app_snap())
    token = r.prompts('record')[0][0]
    assert r.w.consume(token, 'stop') is None          # wrong action for this prompt kind
    info = r.w.consume(token, 'record')
    assert info == {'kind': 'record', 'exe': 'zoom.exe', 'label': 'Zoom',
                    'mic_endpoint': MIC, 'session_id': None}
    assert r.w.consume(token, 'record') is None        # second click
    assert r.w.consume(token, 'sources') is None       # the other button of the same prompt
    assert r.w.consume('nope', 'record') is None
    assert token in r.notifier.removed


def test_keep_cancels_autostop_until_next_call_end():
    r = Rig(autostop_minutes=1)
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    r.run(30, {})
    token = r.prompts('stop')[0][0]
    assert r.w.consume(token, 'keep')['session_id'] == 'rec1'
    r.run(300)
    assert r.stopped == []
    r.run(120, app_snap())
    r.run(30, {})
    assert len(r.prompts('stop')) == 2


def test_pending_lists_open_prompts_and_dismiss_removes():
    r = Rig()
    r.run(31, app_snap())
    [p] = r.w.pending()
    assert p['kind'] == 'record' and p['label'] == 'Zoom' and set(p['actions']) == {'record', 'sources'}
    assert r.w.dismiss(p['id']) is True
    assert r.w.dismiss(p['id']) is False
    assert r.w.pending() == []
    r.run(300)                                   # dismissed = no repeat in this call
    assert len(r.prompts('record')) == 1


def test_log_has_no_pids(tmp_path):
    r = Rig(tmp_path)
    r.run(31, app_snap())
    events = r.events()
    assert [e['event'] for e in events] == ['call_start', 'prompt']
    assert all('pid' not in e for e in events)


@pytest.mark.parametrize('raw, expected', [
    ('Zoom, ms-teams.exe ,,', {'zoom.exe', 'ms-teams.exe'}),
    ('C:\\Apps\\Telegram.exe', {'telegram.exe'}),
    ('', set()),
])
def test_parse_apps(raw, expected):
    assert cw.parse_apps(raw) == frozenset(expected)


def test_captures():
    assert cw.captures('zoom.exe', {'mode': 'all'})
    assert cw.captures('zoom.exe', {'mode': 'include', 'apps': ['Zoom.exe']})
    assert not cw.captures('zoom.exe', {'mode': 'exclude', 'apps': ['zoom.exe']})
    assert not cw.captures('zoom.exe', None)


def test_call_prompt_texts_have_english_variant():
    from app.core.i18n import tr
    assert tr('svc_recording.call.prompt.title', lang='en', label='Zoom') == 'Recall · call in Zoom'
    assert tr('svc_recording.call.prompt.record', lang='en') == 'Record'


# ---------------------------------------------------------------- publisher (system:calls)

class Bus:
    def __init__(self):
        self.events = []

    def __call__(self, event, data):
        self.events.append((event, data))

    def names(self):
        return [e for e, _ in self.events]


def _assert_no_secrets(bus, tokens):
    for event, data in bus.events:
        assert not {'id', 'mic_endpoint', 'actions'} & set(data), (event, data)
        for value in data.values():
            text = str(value)
            assert 'http' not in text and '/api/' not in text, (event, data)
            assert all(t not in text for t in tokens), (event, data)


def test_publisher_sees_the_whole_call_without_tokens_or_urls(tmp_path):
    bus = Bus()
    r = Rig(tmp_path, autostop_minutes=1, publisher=bus)
    r.run(31, app_snap())
    tokens = [p['id'] for p in r.w.pending()]
    assert bus.events == [('call_start', {'exe': 'zoom.exe', 'label': 'Zoom'}),
                          ('prompt', {'kind': 'record', 'exe': 'zoom.exe', 'label': 'Zoom'})]
    r.run(60, {})                                # call ends with the prompt unanswered
    assert bus.events[2:] == [
        ('call_end', {'exe': 'zoom.exe', 'label': 'Zoom'}),
        ('prompt_withdrawn', {'kind': 'record', 'exe': 'zoom.exe', 'label': 'Zoom', 'reason': 'call_end'})]
    # second call, this time recorded: stop prompt, then autostop
    r.live = [recording(source={'mode': 'all'})]
    r.run(120, app_snap())
    r.run(30, {})
    tokens += [p['id'] for p in r.w.pending()]
    assert ('prompt', {'kind': 'stop', 'exe': 'zoom.exe', 'label': 'Zoom', 'session_id': 'rec1'}) in bus.events
    r.run(120)
    assert r.stopped == ['rec1']
    assert bus.events[-1] == ('autostop', {'exe': 'zoom.exe', 'label': 'Zoom', 'session_id': 'rec1'})
    _assert_no_secrets(bus, tokens)


def test_publisher_reports_consumed_dismissed_and_expired():
    bus = Bus()
    r = Rig(publisher=bus, reprompt_minutes=0)     # the 3 h jump must expire, not re-prompt
    r.run(31, app_snap())
    [p] = r.w.pending()
    assert r.w.dismiss(p['id'])
    assert bus.events[-1] == ('prompt_withdrawn', {'kind': 'record', 'exe': 'zoom.exe', 'label': 'Zoom',
                                                   'reason': 'dismissed'})
    r.run(31, app_snap('teams.exe'))
    [p] = r.w.pending()
    assert r.w.consume(p['id'], 'record')['exe'] == 'teams.exe'
    assert bus.events[-1][1]['reason'] == 'consumed'
    r.run(31, app_snap('slack.exe'))
    r.t += cw.PROMPT_MAX_AGE_SEC
    r.run(1)
    assert ('prompt_withdrawn', {'kind': 'record', 'exe': 'slack.exe', 'label': 'Slack',
                                 'reason': 'expired'}) in bus.events
    _assert_no_secrets(bus, [])


def test_publisher_that_raises_does_not_break_the_tick(caplog):
    def boom(event, data):
        raise RuntimeError('broker down')
    r = Rig(publisher=boom)
    r.run(31, app_snap())                        # tick() would raise out of the loop otherwise
    assert len(r.prompts('record')) == 1
    assert len(r.w.pending()) == 1
    assert 'publish of call_start failed' in caplog.text


def test_record_prompt_withdrawn_once_a_session_captures_the_app(tmp_path):
    bus = Bus()
    r = Rig(tmp_path, publisher=bus)
    snap = {**app_snap('teams.exe'), **app_snap('zoom.exe')}
    r.run(31, snap)
    by_exe = {p['exe']: p['id'] for p in r.w.pending()}
    assert set(by_exe) == {'teams.exe', 'zoom.exe'}
    r.live = [recording(source={'mode': 'include', 'apps': ['Teams.exe']})]
    r.run(1)
    assert [p['exe'] for p in r.w.pending()] == ['zoom.exe']
    assert by_exe['teams.exe'] in r.notifier.removed
    assert by_exe['zoom.exe'] not in r.notifier.removed
    withdrawn = [e for e in r.events() if e['event'] == 'prompt_withdrawn']
    assert withdrawn == [{**withdrawn[0], 'exe': 'teams.exe', 'reason': 'session_started'}]
    assert ('prompt_withdrawn', {'kind': 'record', 'exe': 'teams.exe', 'label': 'Teams',
                                 'reason': 'session_started'}) in bus.events
    r.run(60)                                    # the recorded call does not re-prompt
    assert [p['exe'] for p in r.w.pending()] == ['zoom.exe']


def test_without_publisher_nothing_changes(tmp_path):
    r = Rig(tmp_path)
    r.run(31, app_snap())
    assert [e['event'] for e in r.events()] == ['call_start', 'prompt']


# ---------------------------------------------------------------- re-prompt, log_result, auto-record

def test_defaults_without_telegram_and_equal_in_both_copies():
    from app.core import settings
    assert 'telegram.exe' not in cw.parse_apps(cw.DEFAULT_APPS)
    assert cw.parse_apps(settings.by_name()['RECORDING_AUTOPROMPT_APPS'].default) == cw.parse_apps(cw.DEFAULT_APPS)
    assert cw.app_label('telegram.exe') == 'Telegram'          # an owner .env list may still name it


def test_long_unrecorded_call_gets_exactly_one_reprompt(tmp_path):
    bus = Bus()
    r = Rig(tmp_path, publisher=bus, reprompt_minutes=10)
    r.run(31, app_snap())
    [first] = r.w.pending()
    r.run(10 * 60 - 2)
    assert [p['id'] for p in r.w.pending()] == [first['id']]
    r.run(2)
    [second] = r.w.pending()
    assert second['id'] != first['id'] and second['kind'] == 'record'
    assert first['id'] in r.notifier.removed
    assert len(r.prompts('record')) == 2
    ev = r.events()
    assert {'event': 'prompt_withdrawn', 'reason': 'reprompt', 'prompt': first['id']}.items() <= next(
        e for e in ev if e['event'] == 'prompt_withdrawn').items()
    assert [e['prompt_id'] for e in ev if e['event'] == 'reprompt'] == [second['id']]
    assert ('prompt_withdrawn', {'kind': 'record', 'exe': 'zoom.exe', 'label': 'Zoom',
                                 'reason': 'reprompt'}) in bus.events
    r.run(25 * 60)                               # a second interval: no third prompt
    assert len(r.prompts('record')) == 2


def test_no_reprompt_after_dismiss():
    r = Rig(reprompt_minutes=10)
    r.run(31, app_snap())
    assert r.w.dismiss(r.w.pending()[0]['id'])
    r.run(25 * 60)
    assert len(r.prompts('record')) == 1 and r.w.pending() == []


def test_no_reprompt_when_disabled():
    r = Rig(reprompt_minutes=0)
    r.run(31, app_snap())
    r.run(25 * 60)
    assert len(r.prompts('record')) == 1


def test_log_result_ok_marks_call_recorded_no_reprompt(tmp_path):
    r = Rig(tmp_path, reprompt_minutes=10)
    r.run(31, app_snap())
    token = r.w.pending()[0]['id']
    r.w.consume(token, 'record')
    r.live = [recording('rec9', source=None)]    # captures() cannot vouch: only the known sid can
    r.w.log_result(token, ok=True, session_id='rec9')
    r.run(25 * 60)
    assert len(r.prompts('record')) == 1
    started = [e for e in r.events() if e['event'] == 'record_started']
    assert started == [{**started[0], 'prompt_id': token, 'exe': 'zoom.exe', 'session_id': 'rec9',
                        'via': 'click'}]


def test_log_result_failure_leaves_call_unrecorded_and_reprompts(tmp_path):
    r = Rig(tmp_path, reprompt_minutes=10)
    r.run(31, app_snap())
    token = r.w.pending()[0]['id']
    r.w.consume(token, 'record')
    r.w.log_result(token, ok=False, reason='SESSION_LIMIT')
    failed = [e for e in r.events() if e['event'] == 'record_failed']
    assert failed == [{**failed[0], 'prompt_id': token, 'exe': 'zoom.exe', 'reason': 'SESSION_LIMIT',
                       'via': 'click'}]
    r.run(10 * 60)
    assert len(r.prompts('record')) == 2
    assert 'reprompt' in [e['event'] for e in r.events()]


def test_known_session_gets_the_stop_prompt_after_call_end():
    r = Rig(reprompt_minutes=0)
    r.run(31, app_snap())
    token = r.w.pending()[0]['id']
    r.w.consume(token, 'record')
    r.live = [recording('rec9', source=None)]
    r.w.log_result(token, ok=True, session_id='rec9')
    r.run(30, {})
    [p] = r.w.pending()
    assert p['kind'] == 'stop' and p['session_id'] == 'rec9'


class AutoRec:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, exe, call_id):
        self.calls.append((exe, call_id))
        return self.result


def test_auto_record_starts_and_shows_started_prompt(tmp_path):
    from app.core.i18n import tr
    bus = Bus()
    fn = AutoRec({'ok': True, 'session_id': 'rec7'})
    r = Rig(tmp_path, publisher=bus, action='record', auto_record_fn=fn)
    r.run(31, app_snap())
    r.live = [recording('rec7', source={'mode': 'all'})]
    r.run(25 * 60)
    assert len(fn.calls) == 1 and fn.calls[0][0] == 'zoom.exe' and fn.calls[0][1]
    assert r.prompts('record') == []
    [p] = r.w.pending()
    assert p['kind'] == 'started' and p['session_id'] == 'rec7' and set(p['actions']) == {'stop', 'keep'}
    [(tag, title, body, actions)] = r.notifier.shown
    assert tag == p['id'] and title == tr('svc_recording.call.started.title')
    assert [a[0] for a in actions] == [tr('svc_recording.call.started.stop'),
                                       tr('svc_recording.call.started.keep')]
    assert tr('svc_recording.call.started.title', lang='en') == 'Recall · recording started'
    assert [tr(f'svc_recording.call.started.{k}', lang='en') for k in ('stop', 'keep')] == ['Stop', 'Keep']
    started = [e for e in r.events() if e['event'] == 'record_started']
    assert started == [{**started[0], 'exe': 'zoom.exe', 'session_id': 'rec7', 'via': 'auto'}]
    assert ('prompt', {'kind': 'started', 'exe': 'zoom.exe', 'label': 'Zoom', 'session_id': 'rec7'}) in bus.events
    info = r.w.consume(p['id'], 'stop')          # the blueprint's existing stop path takes it from here
    assert info['kind'] == 'started' and info['session_id'] == 'rec7'
    assert r.w.consume(p['id'], 'stop') is None


def test_auto_record_failure_falls_back_to_prompt(tmp_path):
    fn = AutoRec({'ok': False, 'reason': 'SESSION_LIMIT'})
    r = Rig(tmp_path, action='record', auto_record_fn=fn)
    r.run(60, app_snap())
    assert len(fn.calls) == 1
    [p] = r.w.pending()
    assert p['kind'] == 'record' and len(r.prompts('record')) == 1
    failed = [e for e in r.events() if e['event'] == 'record_failed']
    assert failed == [{**failed[0], 'exe': 'zoom.exe', 'reason': 'SESSION_LIMIT', 'via': 'auto'}]


def test_default_prompt_action_never_calls_auto_record(tmp_path):
    fn = AutoRec({'ok': True, 'session_id': 'rec7'})
    r = Rig(tmp_path, auto_record_fn=fn)
    r.run(31, app_snap())
    assert fn.calls == []
    assert [e['event'] for e in r.events()] == ['call_start', 'prompt']
    assert r.w.pending()[0]['kind'] == 'record'
