"""Tests for app/services/recording/app_audio.py — sounding apps (contract C3). No COM needed."""

import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from app.services.recording import app_audio

OWN = 4242
ROOT = Path(__file__).resolve().parents[1]


def _sessions():
    return [
        (100, 'active', 'Speakers', 'Zoom.exe'),
        (101, 'inactive', 'Headset', 'zoom.exe'),
        (200, 'active', 'Speakers', 'chrome.exe'),
        (0, 'active', 'Speakers', 'system.exe'),
        (OWN, 'active', 'Speakers', 'python.exe'),
        (300, 'expired', 'Speakers', 'dead.exe'),
    ]


def _apps(live_specs=None, sessions=_sessions):
    res = app_audio.list_sounding_apps(live_specs, enumerate_sessions=sessions, own_pid=OWN)
    assert res['available'] is True and res['reason'] is None
    return res['apps']


def test_dedupe_skip_and_order():
    apps = _apps()
    assert [a['exe'] for a in apps] == ['chrome.exe', 'zoom.exe']
    zoom = apps[1]
    assert zoom['pids'] == [100, 101]
    assert zoom['state'] == 'active'
    assert zoom['endpoints'] == ['Speakers', 'Headset']
    assert zoom['label'] == 'zoom'
    assert apps[0]['label'] == 'chrome' and apps[0]['pids'] == [200]


def test_order_active_first_then_label():
    def sessions():
        return [(1, 'inactive', 'S', 'aaa.exe'), (2, 'active', 'S', 'zzz.exe'), (3, 'active', 'S', 'bbb.exe')]
    assert [a['label'] for a in _apps(sessions=sessions)] == ['bbb', 'zzz', 'aaa']


def test_recorded_by_all_and_exclude():
    apps = {a['exe']: a for a in _apps({'A': {'mode': 'all', 'apps': []},
                                        'B': {'mode': 'exclude', 'apps': ['zoom.exe']}})}
    assert apps['zoom.exe']['recorded_by'] == ['A']
    assert apps['chrome.exe']['recorded_by'] == ['A', 'B']


def test_recorded_by_include():
    apps = {a['exe']: a for a in _apps({'A': {'mode': 'include', 'apps': ['zoom.exe']}})}
    assert apps['zoom.exe']['recorded_by'] == ['A']
    assert apps['chrome.exe']['recorded_by'] == []


def test_no_live_specs_means_recorded_by_empty():
    assert all(a['recorded_by'] == [] for a in _apps())


def test_enumerator_failure_is_unavailable():
    def boom():
        raise OSError('COM failure 0x80004005')
    res = app_audio.list_sounding_apps(enumerate_sessions=boom, own_pid=OWN)
    assert res['available'] is False
    assert isinstance(res['reason'], str) and 'COM failure' in res['reason']
    assert res['apps'] == []


def test_vanished_pid_is_skipped(monkeypatch):
    class FakeProc:
        def __init__(self, pid):
            if pid == 500:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def name(self):
            return {501: 'Telegram.exe'}[self.pid]

    monkeypatch.setattr(app_audio.psutil, 'Process', FakeProc)

    def sessions():
        return [(500, 'active', 'S', None), (501, 'active', 'S', None), (200, 'active', 'S', 'chrome.exe')]
    apps = _apps(sessions=sessions)
    assert [a['exe'] for a in apps] == ['chrome.exe', 'telegram.exe']
    assert apps[1]['pids'] == [501]


def test_default_path_without_pycaw(monkeypatch):
    for name in list(sys.modules):
        if name == 'pycaw' or name.startswith('pycaw.'):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, 'pycaw', None)  # import → ModuleNotFoundError
    monkeypatch.setitem(sys.modules, 'pycaw.api', None)
    monkeypatch.setitem(sys.modules, 'pycaw.api.audiopolicy', None)
    res = app_audio.list_sounding_apps()
    assert res['available'] is False
    assert 'pycaw' in res['reason']
    assert res['apps'] == []


def _fresh(code):
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_package_import_is_lazy():
    loaded = _fresh(
        "import sys, app.services.recording.app_audio\n"
        "print(','.join(m for m in ('pycaw', 'comtypes', 'torch') if m in sys.modules))")
    assert loaded == ''


def test_module_itself_imports_no_audio_stack():
    # The package __init__ already pulls pyaudiowpatch via recorder.py; the module on its own must not.
    loaded = _fresh(
        "import sys, importlib.util\n"
        "spec = importlib.util.spec_from_file_location('app_audio_iso', 'app/services/recording/app_audio.py')\n"
        "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
        "print(','.join(m for m in ('pycaw', 'comtypes', 'pyaudiowpatch', 'torch') if m in sys.modules))")
    assert loaded == ''


# ---------------------------------------------------------------- session roots (story 11)

# Two zoom.exe trees: 10 (idle, lower PID) with child 11; 20 (sounding) with child 21.
_TREE = {11: [(10, 'Zoom.exe'), (1, 'explorer.exe')], 10: [(1, 'explorer.exe')],
         21: [(20, 'zoom.exe'), (1, 'explorer.exe')], 20: [(1, 'explorer.exe')],
         30: [(1, 'explorer.exe')]}


def _parents(pid):
    if pid not in _TREE:
        raise psutil.NoSuchProcess(pid)
    return _TREE[pid]


def _roots(sessions, fallback=None):
    return app_audio.session_roots('zoom.exe', enumerate_sessions=lambda: sessions, own_pid=OWN,
                                   process_parents=_parents, fallback=fallback)


def test_session_pids_filters_by_exe_and_prefers_active():
    sessions = [(11, 'inactive', 'S', 'zoom.exe'), (11, 'active', 'H', 'zoom.exe'),
                (21, 'inactive', 'S', 'Zoom.exe'), (30, 'active', 'S', 'chrome.exe'),
                (OWN, 'active', 'S', 'zoom.exe'), (22, 'expired', 'S', 'zoom.exe')]
    got = app_audio.session_pids('Zoom.exe', enumerate_sessions=lambda: sessions, own_pid=OWN)
    assert sorted(got) == [(11, 'active'), (21, 'inactive')]


def test_session_pids_never_raises():
    def boom():
        raise OSError('COM failure')
    assert app_audio.session_pids('zoom.exe', enumerate_sessions=boom, own_pid=OWN) == []


def test_second_tree_sounding_comes_first():
    sessions = [(11, 'inactive', 'S', 'zoom.exe'), (21, 'active', 'S', 'zoom.exe')]
    assert _roots(sessions) == [20, 10]


def test_child_session_collapses_to_root():
    assert _roots([(21, 'active', 'S', 'zoom.exe'), (20, 'inactive', 'S', 'zoom.exe')]) == [20]


def test_no_active_session_is_deterministic():
    sessions = [(21, 'inactive', 'S', 'zoom.exe'), (11, 'inactive', 'S', 'zoom.exe')]
    assert _roots(sessions) == [10, 20]
    assert _roots(list(reversed(sessions))) == [10, 20]


def test_most_active_sessions_wins():
    sessions = [(10, 'active', 'S', 'zoom.exe'), (21, 'active', 'S', 'zoom.exe'),
                (20, 'active', 'H', 'zoom.exe')]
    assert _roots(sessions) == [20, 10]


def test_vanished_session_pid_skipped_and_no_session_falls_back():
    assert _roots([(99, 'active', 'S', 'zoom.exe')]) == []
    assert _roots([(99, 'active', 'S', 'zoom.exe')], fallback=lambda exe: [7, 8]) == [7, 8]
    assert _roots([], fallback=lambda exe: [7]) == [7]


# ---------------------------------------------------------------- list_call_sessions (K2)

def _call_sessions():
    return [
        ('mic', 100, 'active', 'Мікрофон (FOX)', 'Zoom.exe', 0.05),
        ('mic', 101, 'active', 'Мікрофон (USB)', 'zoom.exe', 0.20),
        ('out', 100, 'active', 'Навушники', 'Zoom.exe', 0.30),
        ('mic', 200, 'inactive', 'Мікрофон (USB)', 'ms-teams.exe', 0.0),
        ('out', 300, 'active', 'Навушники', 'chrome.exe', 0.40),
        ('mic', OWN, 'active', 'Мікрофон (USB)', 'python.exe', 0.9),
        ('mic', 0, 'active', 'Мікрофон (USB)', 'system', 0.9),
        ('out', 400, 'expired', 'Навушники', 'dead.exe', 0.9),
    ]


def test_call_sessions_per_exe_and_flow():
    res = app_audio.list_call_sessions(enumerate_sessions=_call_sessions, own_pid=OWN)
    assert set(res) == {'zoom.exe', 'ms-teams.exe', 'chrome.exe'}
    zoom = res['zoom.exe']
    assert zoom['mic'] == {'active': True, 'peak': 0.20, 'endpoint': 'Мікрофон (USB)'}   # loudest wins
    assert zoom['out'] == {'active': True, 'peak': 0.30, 'endpoint': 'Навушники'}
    assert res['ms-teams.exe']['mic']['active'] is False                               # open, no call
    assert res['chrome.exe']['mic'] == {'active': False, 'peak': 0.0, 'endpoint': None}  # sound only


def test_call_sessions_enumeration_failure_is_none_not_empty():
    def boom():
        raise OSError('COM failure')
    assert app_audio.list_call_sessions(enumerate_sessions=boom, own_pid=OWN) is None
