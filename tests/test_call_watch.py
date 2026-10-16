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

    def __init__(self, tmp_path=None, *, prompt_seconds=30, autostop_minutes=10):
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
            clock=lambda: self.t, wall=lambda: 1_760_000_000.0 + self.t, log_path=self.log_path)

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
