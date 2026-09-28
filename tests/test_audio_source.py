"""AudioSource boundary + per-app backend (story parallel-recordings-app-audio-02).

All offline: a fake ``client_factory`` stands in for the native process-loopback client,
a fake resolver for psutil, a fake clock for wall time. No audio is played or captured.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from app.services.recording.audio_source import (
    MAX_INCLUDE_CLIENTS,
    AppAudioSource,
    MixedSource,
    ProcessLoopbackSource,
    SystemSpec,
    UnsupportedSourceSpecError,
    resolve_root_pids,
)
from app.services.recording.recorder import WasapiRecorder

ROOT = Path(__file__).resolve().parents[1]
RATE, CH = 48000, 2
BPS = RATE * CH * 2  # bytes per second
OWN = 999


class FakeClient:
    def __init__(self, pid, mode, rate, channels):
        self.pid, self.mode, self.rate, self.channels = pid, mode, rate, channels
        self.on_frame = self.on_error = None
        self.started = self.stopped = False

    def start(self, on_frame, on_error):
        self.on_frame, self.on_error = on_frame, on_error
        self.started = True

    def stop(self):
        self.stopped = True

    def push(self, samples):
        self.on_frame(np.asarray(samples, dtype='<i2').tobytes(), 0.0)


class Factory:
    def __init__(self):
        self.clients: list[FakeClient] = []

    def __call__(self, pid, mode, rate, channels):
        c = FakeClient(pid, mode, rate, channels)
        self.clients.append(c)
        return c

    def live(self):
        return [c for c in self.clients if c.started and not c.stopped]


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _app(spec, pids_by_exe, **kw):
    factory = Factory()
    clock = Clock()
    state = {'pids': dict(pids_by_exe)}
    src = AppAudioSource(
        spec, RATE, CH, client_factory=factory,
        pid_resolver=lambda exe: list(state['pids'].get(exe, [])),
        watch_interval=0, own_pid=OWN, clock=clock, **kw,
    )
    return src, factory, clock, state


# ---------------------------------------------------------------- SystemSpec

def test_from_dict_normalises_apps():
    s = SystemSpec.from_dict({'mode': 'include',
                              'apps': [r'C:\Program Files\Zoom\Zoom.EXE', 'chrome.exe', 'ZOOM.exe']})
    assert s.apps == ('zoom.exe', 'chrome.exe')
    assert s.to_dict() == {'mode': 'include', 'apps': ['zoom.exe', 'chrome.exe'],
                           'device_index': None}


@pytest.mark.parametrize('d', [
    {'mode': 'exclude', 'apps': []},
    {'mode': 'exclude', 'apps': ['a.exe', 'b.exe']},
    {'mode': 'include', 'apps': []},
    {'mode': 'include', 'apps': ['a.exe', 'b.exe', 'c.exe', 'd.exe', 'e.exe']},
    {'mode': 'all', 'apps': ['a.exe']},
    {'mode': 'everything'},
])
def test_invalid_specs_raise(d):
    with pytest.raises(UnsupportedSourceSpecError) as ei:
        SystemSpec.from_dict(d)
    assert ei.value.code == 'UNSUPPORTED_SOURCE_SPEC'
    with pytest.raises(UnsupportedSourceSpecError):
        SystemSpec(d['mode'], tuple(d.get('apps', ()))).validate()


def test_valid_specs():
    SystemSpec.from_dict({'mode': 'all'}).validate()
    SystemSpec.from_dict({'mode': 'exclude', 'apps': ['zoom.exe']}).validate()
    SystemSpec.from_dict({'mode': 'include', 'apps': ['a', 'b', 'c', 'd']}).validate()


# ---------------------------------------------------------------- include / mix

def test_include_two_root_pids_opens_two_clients():
    src, f, _, _ = _app(SystemSpec('include', ('a.exe',)), {'a.exe': [10, 11]})
    src.open(); src.start()
    assert sorted(c.pid for c in f.live()) == [10, 11]
    assert {c.mode for c in f.live()} == {'include'}
    assert all((c.rate, c.channels) == (RATE, CH) for c in f.clients)
    src.close()


def test_include_cap_counts_resolved_pids():
    src, _, _, _ = _app(SystemSpec('include', ('a.exe',)),
                        {'a.exe': list(range(1, MAX_INCLUDE_CLIENTS + 2))})
    with pytest.raises(UnsupportedSourceSpecError):
        src.open()


def test_saturating_add_clamps():
    f = Factory()
    parts = [ProcessLoopbackSource(p, 'include', RATE, CH, client_factory=f) for p in (1, 2)]
    mix = MixedSource(parts, RATE, CH)
    mix.start()
    f.clients[0].push([30000, -30000, 100, -5])
    f.clients[1].push([30000, -30000, 200, 5])
    (frame,) = mix.drain()
    assert np.frombuffer(frame.data, dtype='<i2').tolist() == [32767, -32768, 300, 0]
    assert frame.frame_count == 2


def test_lagging_part_is_zero_filled_and_mix_flows():
    f = Factory()
    parts = [ProcessLoopbackSource(p, 'include', RATE, CH, client_factory=f) for p in (1, 2)]
    mix = MixedSource(parts, RATE, CH)
    mix.start()
    f.clients[0].push([7] * (RATE * CH // 10))       # 100 ms, other silent
    assert mix.drain() == []                          # under the 500 ms lag limit: wait
    f.clients[0].push([7] * (RATE * CH // 2))         # leader now 600 ms ahead
    frames = mix.drain()
    out = b''.join(fr.data for fr in frames)
    assert len(out) == int(BPS * 0.6)
    assert set(np.frombuffer(out, dtype='<i2').tolist()) == {7}
    assert mix.dropped_frames == 0


def test_dropped_frames_drop_oldest():
    f = Factory()
    part = ProcessLoopbackSource(1, 'include', RATE, CH, client_factory=f, queue_max=2)
    part.start()
    for v in (1, 2, 3):
        f.clients[0].push([v, v])
    assert part.dropped_frames == 1
    assert [np.frombuffer(fr.data, dtype='<i2')[0] for fr in part.drain()] == [2, 3]


# ---------------------------------------------------------------- exclude / watcher

def test_exclude_absent_app_uses_own_pid_then_swaps():
    src, f, _, state = _app(SystemSpec('exclude', ('zoom.exe',)), {})
    src.open(); src.start()
    assert [(c.pid, c.mode) for c in f.live()] == [(OWN, 'exclude')]
    assert src.refresh_pids() is None and src.drain_events() == []

    state['pids']['zoom.exe'] = [4242]
    src.refresh_pids()
    assert [(c.pid, c.mode) for c in f.live()] == [(4242, 'exclude')]
    events = src.drain_events()
    assert len(events) == 1
    ev = events[0]
    assert ev['kind'] == 'pid_change' and ev['exe'] == 'zoom.exe'
    assert ev['old_pids'] == [] and ev['new_pids'] == [4242]
    assert isinstance(ev['gap_ms'], float) and ev['gap_ms'] >= 0
    assert src.drain_events() == []
    src.close()


def test_watcher_thread_detects_change():
    import threading
    factory = Factory()
    pids = {'zoom.exe': []}
    seen = threading.Event()

    def resolver(exe):
        if pids[exe]:
            seen.set()
        return list(pids[exe])

    src = AppAudioSource(SystemSpec('exclude', ('zoom.exe',)), RATE, CH,
                         client_factory=factory, pid_resolver=resolver,
                         watch_interval=0.01, own_pid=OWN)
    src.start()
    pids['zoom.exe'] = [77]
    assert seen.wait(2.0)
    for _ in range(200):
        if src.drain_events():
            break
        threading.Event().wait(0.01)
    else:
        pytest.fail('watcher pushed no pid_change')
    src.close()


# ---------------------------------------------------------------- wall clock

def test_include_absent_app_yields_wall_clock_silence():
    src, f, clock, _ = _app(SystemSpec('include', ('zoom.exe',)), {})
    src.open(); src.start()
    assert f.clients == []
    clock.t += 1.0
    out = b''.join(fr.data for fr in src.drain())
    assert len(out) == BPS and out == bytes(BPS)
    clock.t += 0.5
    assert len(b''.join(fr.data for fr in src.drain())) == BPS // 2
    assert src.dropped_frames == 0
    src.close()


def test_stream_behind_wall_clock_is_padded():
    src, f, clock, _ = _app(SystemSpec('include', ('a.exe',)), {'a.exe': [5]})
    src.open(); src.start()
    clock.t += 1.0
    f.clients[0].push([1] * (RATE * CH * 9 // 10))    # delivered 0.9 s of 1.0 s
    out = b''.join(fr.data for fr in src.drain())
    assert len(out) == BPS
    src.close()


# ---------------------------------------------------------------- errors / lifecycle

def test_client_error_surfaces_and_double_close_is_noop():
    src, f, _, _ = _app(SystemSpec('include', ('a.exe',)), {'a.exe': [5]})
    src.open(); src.start()
    assert src.get_callback_error() is None
    f.clients[0].on_error(OSError('device gone'))
    assert isinstance(src.get_callback_error(), OSError)
    src.close()
    src.close()
    assert f.clients[0].stopped


def test_all_mode_is_exclude_own_pid():
    src, f, _, _ = _app(SystemSpec('all'), {})
    src.start()
    assert [(c.pid, c.mode) for c in f.live()] == [(OWN, 'exclude')]
    src.close()


def test_wasapi_recorder_drain_events():
    assert hasattr(WasapiRecorder, 'drain_events')
    rec = WasapiRecorder.__new__(WasapiRecorder)  # no device: the method is stateless
    assert rec.drain_events() == []


def test_resolve_root_pids_skips_children_and_own(monkeypatch):
    import psutil

    class P:
        def __init__(self, pid, name, ppid):
            self.info = {'pid': pid, 'name': name, 'ppid': ppid}

    procs = [P(1, 'Zoom.exe', 500), P(2, 'zoom.exe', 1), P(3, 'zoom.exe', 600),
             P(4, 'chrome.exe', 1), P(OWN, 'zoom.exe', 700)]
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs=None: iter(procs))
    assert resolve_root_pids('ZOOM.EXE', own_pid=OWN) == [1, 3]


# ---------------------------------------------------------------- lazy native imports

def test_import_pulls_no_native_backend():
    code = ("import sys, app.services.recording.audio_source; "
            "print(','.join(m for m in ('proc_tap', 'comtypes', 'pycaw') if m in sys.modules))")
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ''


@pytest.mark.parametrize('mod', ['audio_source.py', 'process_loopback.py'])
def test_no_module_level_native_imports(mod):
    tree = ast.parse((ROOT / 'app' / 'services' / 'recording' / mod).read_text(encoding='utf-8'))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split('.')[0])
    assert not names & {'proc_tap', 'pyaudiowpatch', 'comtypes', 'pycaw', 'psutil'}


# ---------------------------------------------------------------- sounding tree (story 11)

def test_exclude_targets_sounding_tree_and_swaps_on_change():
    from app.services.recording.app_audio import session_roots
    tree = {11: [(10, 'zoom.exe')], 21: [(20, 'zoom.exe')], 10: [], 20: []}
    sessions = [(11, 'inactive', 'S', 'zoom.exe'), (21, 'active', 'S', 'zoom.exe')]
    factory = Factory()
    src = AppAudioSource(
        SystemSpec('exclude', ('zoom.exe',)), RATE, CH, client_factory=factory,
        pid_resolver=lambda exe: session_roots(exe, enumerate_sessions=lambda: list(sessions),
                                               own_pid=OWN, process_parents=tree.__getitem__),
        watch_interval=0, own_pid=OWN, clock=Clock())
    src.open(); src.start()
    assert [(c.pid, c.mode) for c in factory.live()] == [(20, 'exclude')]

    sessions[:] = [(11, 'active', 'S', 'zoom.exe'), (21, 'inactive', 'S', 'zoom.exe')]
    ev = src.refresh_pids()
    assert ev['kind'] == 'pid_change' and ev['new_pids'][0] == 10
    assert [c.pid for c in factory.live()] == [10]
    src.close()


def test_exclude_multi_tree_warning_once_per_change(caplog):
    src, f, _, state = _app(SystemSpec('exclude', ('zoom.exe',)), {'zoom.exe': [20, 10]})
    with caplog.at_level('WARNING', logger='app.services.recording.audio_source'):
        src.open(); src.start()
        for _ in range(5):
            src.refresh_pids()
        assert [c.pid for c in f.live()] == [20]
        assert sum('process trees' in r.message for r in caplog.records) == 1
        state['pids']['zoom.exe'] = [10, 20]
        src.refresh_pids(); src.refresh_pids()
        assert [c.pid for c in f.live()] == [10]
        assert sum('process trees' in r.message for r in caplog.records) == 2
    src.close()


def test_include_keeps_resolver_order_without_spurious_swap():
    src, f, _, state = _app(SystemSpec('include', ('chrome.exe',)), {'chrome.exe': [30, 5]})
    src.open(); src.start()
    assert [c.pid for c in f.live()] == [30, 5]
    state['pids']['chrome.exe'] = [5, 30]
    assert src.refresh_pids() is None
    src.close()
