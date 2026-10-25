"""
app/services/recording/call_watch.py  —  «Записати?» / «Зупинити?» by the call itself (K2).

A daemon thread ticks once a second over every audio session (``app_audio.list_call_sessions``):

- **A call** is an app from the allow-list (``RECORDING_AUTOPROMPT_APPS``) whose microphone AND
  output sessions are both Active for ``RECORDING_AUTOPROMPT_SECONDS`` without a break, with each
  side above the speech peak on at least ``MIN_VOICED_TICKS`` ticks of that streak. Dictation apps
  are not on the list; YouTube and music hold no microphone — neither can become a call.
- **One prompt per call.** «Записати?» is shown once, unless a live recording already captures the
  app. «Записати» starts mic + this app's sound only; nothing starts without a click.
- **Call end** — the app's sessions stopped being Active for ``END_INACTIVE_SEC``, or both sides
  went silent for ``END_SILENCE_SEC`` — withdraws an unanswered prompt and asks «Зупинити?» for
  every live, unpaused recording that captures the app.
- **Autostop.** After «Зупинити?», ``RECORDING_AUTOSTOP_SILENCE_MIN`` minutes of silence on both
  tracks of that recording stop it through the same stop path as the UI button; the recording is
  finalized whole.

Each prompt id doubles as a single-use token for its buttons (``consume``). Toasts and the stop
call run outside the lock. Every decision is appended to a JSONL log (no PIDs, no speech) for the
K2 measurement.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from app.core.i18n import tr

logger = logging.getLogger(__name__)

DEFAULT_APPS = ('zoom.exe,ms-teams.exe,teams.exe,telegram.exe,viber.exe,whatsapp.root.exe,'
                'whatsapp.exe,chrome.exe,msedge.exe,firefox.exe,discord.exe,slack.exe,signal.exe')

#: Session peak (0..1) that counts as sound. K2 spike: an open microphone in a quiet room peaks
#: at 0.005-0.007, so 0.02 sits above room noise; history 03 tunes it on live calls.
SPEECH_PEAK = 0.02
#: Each side must be heard on this many ticks of the streak before it counts as a call.
MIN_VOICED_TICKS = 3
#: A confirmed call ends when its sessions are not both Active for this long…
END_INACTIVE_SEC = 20.0
#: …or when both sides have been silent for this long (apps that keep sessions open).
END_SILENCE_SEC = 40.0
#: Hard cap on a prompt's life; normally it dies with its call or its recording.
PROMPT_MAX_AGE_SEC = 3 * 3600.0
TICK_SEC = 1.0
_LOG_MAX_BYTES = 5 * 1024 * 1024

_LABELS = {
    'zoom.exe': 'Zoom', 'ms-teams.exe': 'Teams', 'teams.exe': 'Teams', 'telegram.exe': 'Telegram',
    'viber.exe': 'Viber', 'whatsapp.root.exe': 'WhatsApp', 'whatsapp.exe': 'WhatsApp',
    'chrome.exe': 'Chrome', 'msedge.exe': 'Edge', 'firefox.exe': 'Firefox',
    'discord.exe': 'Discord', 'slack.exe': 'Slack', 'signal.exe': 'Signal',
}

RECORD_ACTIONS = ('record', 'sources')
STOP_ACTIONS = ('stop', 'keep')


def parse_apps(raw: str) -> frozenset[str]:
    """``'Zoom, ms-teams.exe'`` → ``{'zoom.exe', 'ms-teams.exe'}`` (lowercase exe basenames)."""
    out = set()
    for part in (raw or '').split(','):
        name = os.path.basename(part.strip()).lower()
        if not name:
            continue
        out.add(name if name.endswith('.exe') else name + '.exe')
    return frozenset(out)


def app_label(exe: str) -> str:
    return _LABELS.get(exe, os.path.splitext(exe)[0])


def captures(exe: str, system_source: dict | None) -> bool:
    """Does a recording with this system source capture ``exe``? No system stream → no."""
    if not system_source:
        return False
    mode = system_source.get('mode', 'all')
    apps = {str(a).lower() for a in (system_source.get('apps') or [])}
    return mode == 'all' or (mode == 'include' and exe in apps) or (mode == 'exclude' and exe not in apps)


@dataclass
class _Call:
    exe: str
    since: float
    last_live: float
    last_voice: float
    mic_voiced: int = 0
    out_voiced: int = 0
    confirmed: bool = False
    prompt_id: str | None = None
    mic_endpoint: str | None = None


@dataclass
class _Prompt:
    id: str
    kind: str              # 'record' | 'stop'
    exe: str
    created: float
    created_wall: float
    mic_endpoint: str | None = None
    session_id: str | None = None


@dataclass
class _Watch:
    sid: str
    exe: str
    prompt_id: str
    since_silence: float | None = None


class ToastNotifier:
    """Production notifier: Windows toasts through ``toast.py``."""

    def __init__(self, icon_path: str | None = None):
        from app.services.recording import toast
        self._toast = toast
        toast.ensure_registered('Recall', icon_path)

    def show(self, tag: str, title: str, body: str, actions: list[tuple[str, str]], *,
             launch_url: str | None = None, reminder: bool = False) -> bool:
        xml = self._toast.build_xml(title, body, actions, launch_url=launch_url, reminder=reminder)
        return self._toast.show(xml, tag)

    def remove(self, tag: str) -> bool:
        return self._toast.remove(tag)


class CallWatcher:
    """See the module docstring. Every collaborator is injected, so ``tick()`` is testable."""

    def __init__(self, *, apps: frozenset[str], prompt_seconds: float, autostop_minutes: float,
                 base_url: str, enumerate_calls, live_sessions, stop_fn, notifier,
                 clock=time.monotonic, wall=time.time, log_path: str | None = None,
                 publisher=None):
        self.apps = frozenset(apps)
        self.prompt_seconds = float(prompt_seconds)
        self.autostop_sec = float(autostop_minutes) * 60.0
        self.base_url = base_url.rstrip('/')
        self._enumerate = enumerate_calls
        self._live_sessions = live_sessions
        self._stop_fn = stop_fn
        self._notifier = notifier
        self._clock = clock
        self._wall = wall
        self._log_path = log_path
        self._publisher = publisher
        self._lock = threading.Lock()
        self._calls: dict[str, _Call] = {}
        self._prompts: dict[str, _Prompt] = {}
        self._watches: dict[str, _Watch] = {}
        self._enum_failing = False
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # ------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='call-watch', daemon=True)
        self._thread.start()
        logger.info('call watcher started: %d apps, prompt after %ss, autostop after %s min',
                    len(self.apps), self.prompt_seconds, self.autostop_sec / 60)

    def stop(self) -> None:
        self._stop_evt.set()

    def _run(self) -> None:
        while not self._stop_evt.wait(TICK_SEC):
            try:
                self.tick()
            except Exception:
                logger.exception('call watcher tick failed')

    # ------------------------------------------------------------ tick

    def tick(self) -> None:
        snap = self._enumerate()
        if snap is None:
            if not self._enum_failing:
                logger.warning('call watcher: audio-session enumeration failing, holding state')
            self._enum_failing = True
            return
        if self._enum_failing:
            logger.info('call watcher: audio-session enumeration recovered')
            self._enum_failing = False
        live = list(self._live_sessions() or [])
        now = self._clock()
        effects: list = []
        with self._lock:
            self._tick_calls(now, snap, live, effects)
            self._withdraw_recorded(live, effects)
            self._tick_autostop(now, live, effects)
            self._expire(now, effects)
        self._run_effects(effects)

    def _tick_calls(self, now, snap, live, effects) -> None:
        for exe in self.apps:
            app = snap.get(exe)
            both = bool(app and app['mic']['active'] and app['out']['active'])
            call = self._calls.get(exe)
            if both:
                if call is None:
                    call = self._calls[exe] = _Call(exe, since=now, last_live=now, last_voice=now)
                call.last_live = now
                mic_v = app['mic']['peak'] > SPEECH_PEAK
                out_v = app['out']['peak'] > SPEECH_PEAK
                call.mic_voiced += mic_v
                call.out_voiced += out_v
                if mic_v or out_v:
                    call.last_voice = now
                call.mic_endpoint = app['mic']['endpoint'] or call.mic_endpoint
            if call is None:
                continue
            if not call.confirmed:
                if not both:
                    del self._calls[exe]           # the streak broke before it became a call
                    continue
                if (now - call.since >= self.prompt_seconds
                        and call.mic_voiced >= MIN_VOICED_TICKS
                        and call.out_voiced >= MIN_VOICED_TICKS):
                    self._confirm(now, call, live, effects)
                continue
            if now - call.last_live >= END_INACTIVE_SEC or now - call.last_voice >= END_SILENCE_SEC:
                self._end_call(now, call, live, effects)

    def _confirm(self, now, call: _Call, live, effects) -> None:
        call.confirmed = True
        self._log('call_start', exe=call.exe, after_sec=round(now - call.since, 1))
        self._emit(effects, 'call_start', exe=call.exe)
        for w in list(self._watches.values()):        # the call resumed in a recording we asked about
            if w.exe == call.exe:
                self._drop_watch(w.sid, 'call_resumed', effects)
        recorded = [s['sid'] for s in live if captures(call.exe, s.get('system_source'))]
        if recorded:
            self._log('prompt_skipped', exe=call.exe, reason='recorded')
            return
        p = self._new_prompt(now, 'record', call.exe, mic_endpoint=call.mic_endpoint)
        call.prompt_id = p.id
        label = app_label(call.exe)
        effects.append(lambda p=p, label=label: self._notifier.show(
            p.id, tr('svc_recording.call.prompt.title', label=label),
            tr('svc_recording.call.prompt.body', label=label),
            [(tr('svc_recording.call.prompt.record'), self._url(p.id, 'record')),
             (tr('svc_recording.call.prompt.sources'), self._url(p.id, 'sources'))],
            launch_url=self._url(p.id, 'sources'), reminder=True))
        self._log('prompt', exe=call.exe, prompt=p.id)
        self._emit(effects, 'prompt', kind='record', exe=call.exe)

    def _end_call(self, now, call: _Call, live, effects) -> None:
        del self._calls[call.exe]
        reason = 'inactive' if now - call.last_live >= END_INACTIVE_SEC else 'silence'
        self._log('call_end', exe=call.exe, reason=reason, duration_sec=round(now - call.since, 1))
        self._emit(effects, 'call_end', exe=call.exe)
        if call.prompt_id and call.prompt_id in self._prompts:
            self._prompts.pop(call.prompt_id)
            effects.append(lambda pid=call.prompt_id: self._notifier.remove(pid))
            self._log('prompt_withdrawn', exe=call.exe, prompt=call.prompt_id)
            self._emit(effects, 'prompt_withdrawn', kind='record', exe=call.exe, reason='call_end')
        for s in live:
            sid = s['sid']
            if s.get('is_paused') or sid in self._watches or not captures(call.exe, s.get('system_source')):
                continue
            p = self._new_prompt(now, 'stop', call.exe, session_id=sid)
            self._watches[sid] = _Watch(sid, call.exe, p.id)
            label = app_label(call.exe)
            effects.append(lambda p=p, label=label: self._notifier.show(
                p.id, tr('svc_recording.call.ended.title'),
                tr('svc_recording.call.ended.body', label=label,
                   minutes=f'{self.autostop_sec / 60:g}'),
                [(tr('svc_recording.call.ended.stop'), self._url(p.id, 'stop')),
                 (tr('svc_recording.call.ended.keep'), self._url(p.id, 'keep'))],
                reminder=True))
            self._log('stop_prompt', exe=call.exe, prompt=p.id)
            self._emit(effects, 'prompt', kind='stop', exe=call.exe, session_id=sid)

    def _tick_autostop(self, now, live, effects) -> None:
        by_sid = {s['sid']: s for s in live}
        for sid, w in list(self._watches.items()):
            s = by_sid.get(sid)
            if s is None:
                self._drop_watch(sid, 'recording_gone', effects)
                continue
            if s.get('is_paused'):
                w.since_silence = None
                continue
            loud = max(float(s.get('mic_peak') or 0.0), float(s.get('sys_peak') or 0.0)) > SPEECH_PEAK
            if loud:
                w.since_silence = None
            elif w.since_silence is None:
                w.since_silence = now
            elif now - w.since_silence >= self.autostop_sec:
                self._watches.pop(sid)
                self._prompts.pop(w.prompt_id, None)
                effects.append(lambda w=w: self._autostop(w))

    def _autostop(self, w: _Watch) -> None:
        self._notifier.remove(w.prompt_id)
        try:
            self._stop_fn(w.sid)
        except Exception as exc:
            logger.warning('autostop of %s failed: %s', w.sid, exc, exc_info=True)
            self._log('autostop_failed', exe=w.exe, error=type(exc).__name__)
            return
        self._log('autostop', exe=w.exe, silence_min=round(self.autostop_sec / 60, 1))
        self._publish('autostop', {'exe': w.exe, 'label': app_label(w.exe), 'session_id': w.sid})
        self._notifier.show(f'{w.prompt_id}-done', tr('svc_recording.call.autostopped.title'),
                            tr('svc_recording.call.autostopped.body',
                               minutes=f'{self.autostop_sec / 60:g}'),
                            [], launch_url=f'{self.base_url}/record')

    def _expire(self, now, effects) -> None:
        for pid, p in list(self._prompts.items()):
            if now - p.created >= PROMPT_MAX_AGE_SEC:
                self._prompts.pop(pid)
                if p.kind == 'stop':
                    self._watches.pop(p.session_id, None)
                effects.append(lambda pid=pid: self._notifier.remove(pid))
                self._log('prompt_expired', exe=p.exe, prompt=pid)
                self._emit(effects, 'prompt_withdrawn', kind=p.kind, exe=p.exe, reason='expired')

    def _withdraw_recorded(self, live, effects) -> None:
        """A pending «record» prompt whose app a live recording already captures is withdrawn."""
        for pid, p in list(self._prompts.items()):
            if p.kind != 'record':
                continue
            if not any(captures(p.exe, s.get('system_source')) for s in live):
                continue
            self._prompts.pop(pid)
            effects.append(lambda pid=pid: self._notifier.remove(pid))
            self._log('prompt_withdrawn', exe=p.exe, prompt=pid, reason='session_started')
            self._emit(effects, 'prompt_withdrawn', kind='record', exe=p.exe, reason='session_started')

    # ------------------------------------------------------------ actions (HTTP thread)

    def pending(self) -> list[dict]:
        with self._lock:
            prompts = sorted(self._prompts.values(), key=lambda p: p.created)
            out = []
            for p in prompts:
                acts = RECORD_ACTIONS if p.kind == 'record' else STOP_ACTIONS
                out.append({
                    'id': p.id, 'kind': p.kind, 'exe': p.exe, 'label': app_label(p.exe),
                    'session_id': p.session_id, 'mic_endpoint': p.mic_endpoint,
                    'created_at': datetime.fromtimestamp(p.created_wall, timezone.utc).isoformat(),
                    'actions': {a: self._url(p.id, a) for a in acts},
                })
            return out

    def consume(self, token: str, action: str) -> dict | None:
        """Single use: the first valid click wins, every later or wrong one gets ``None``."""
        effects: list = []
        with self._lock:
            p = self._prompts.get(token)
            allowed = RECORD_ACTIONS if p and p.kind == 'record' else STOP_ACTIONS
            if p is None or action not in allowed:
                return None
            if self._clock() - p.created >= PROMPT_MAX_AGE_SEC:
                return None
            self._prompts.pop(token)
            if p.kind == 'stop':
                self._watches.pop(p.session_id, None)
            effects.append(lambda: self._notifier.remove(token))
            self._log('click', exe=p.exe, prompt=token, action=action)
            self._emit(effects, 'prompt_withdrawn', kind=p.kind, exe=p.exe, reason='consumed')
            info = {'kind': p.kind, 'exe': p.exe, 'label': app_label(p.exe),
                    'mic_endpoint': p.mic_endpoint, 'session_id': p.session_id}
        self._run_effects(effects)
        return info

    def dismiss(self, prompt_id: str) -> bool:
        """«Не потрібно» from the in-tab banner: the prompt goes, the call does not re-prompt."""
        effects: list = []
        with self._lock:
            p = self._prompts.get(prompt_id)
            if p is None or p.kind != 'record':
                return False
            self._prompts.pop(prompt_id)
            effects.append(lambda: self._notifier.remove(prompt_id))
            self._log('dismiss', exe=p.exe, prompt=prompt_id)
            self._emit(effects, 'prompt_withdrawn', kind=p.kind, exe=p.exe, reason='dismissed')
        self._run_effects(effects)
        return True

    # ------------------------------------------------------------ helpers

    def _new_prompt(self, now, kind, exe, *, mic_endpoint=None, session_id=None) -> _Prompt:
        p = _Prompt(secrets.token_urlsafe(16), kind, exe, now, self._wall(),
                    mic_endpoint=mic_endpoint, session_id=session_id)
        self._prompts[p.id] = p
        return p

    def _drop_watch(self, sid: str, reason: str, effects) -> None:
        w = self._watches.pop(sid, None)
        if w is None:
            return
        self._prompts.pop(w.prompt_id, None)
        effects.append(lambda pid=w.prompt_id: self._notifier.remove(pid))
        self._log('stop_cancelled', exe=w.exe, reason=reason)

    def _url(self, token: str, action: str) -> str:
        return f'{self.base_url}/api/recording/autoprompt/{quote(token)}/{action}'

    def _emit(self, effects, event: str, *, exe: str, **fields) -> None:
        """Queue a publish as a side effect (runs outside the lock). Payload: no token, no URL."""
        if self._publisher is None:
            return
        data = {**fields, 'exe': exe, 'label': app_label(exe)}
        effects.append(lambda: self._publish(event, data))

    def _publish(self, event: str, data: dict) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher(event, data)
        except Exception:
            logger.warning('call watcher publish of %s failed', event, exc_info=True)

    @staticmethod
    def _run_effects(effects) -> None:
        for fn in effects:
            try:
                fn()
            except Exception:
                logger.warning('call watcher side effect failed', exc_info=True)

    def _log(self, event: str, **fields) -> None:
        if not self._log_path:
            return
        row = {'ts': datetime.fromtimestamp(self._wall(), timezone.utc).isoformat(), 'event': event, **fields}
        try:
            if os.path.exists(self._log_path) and os.path.getsize(self._log_path) > _LOG_MAX_BYTES:
                os.replace(self._log_path, self._log_path + '.1')
            with open(self._log_path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        except OSError as exc:
            logger.debug('call watcher log write failed: %s', exc)
