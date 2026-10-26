"""Tests for app/services/recording/stop_control.py: background stop, registry, duration timer.

No sleeps for minutes: the timer takes an injected fake factory, and stops run on real daemon
threads that the tests wait for with a short poll.
"""

import logging
import threading
import time

import pytest

from app.services.recording import stop_control as sc
from app.services.recording.service import SessionNotFoundError


@pytest.fixture(autouse=True)
def _clean():
    sc.reset()
    yield
    sc.reset()


def _wait_idle(sid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while sc.is_stopping(sid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not sc.is_stopping(sid)


class FakeTimer:
    instances = []

    def __init__(self, delay, fn, args=(), kwargs=None):
        self.delay, self.fn = delay, fn
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def fake_timers():
    FakeTimer.instances = []
    return FakeTimer


def test_request_stop_registers_and_second_call_is_already():
    release = threading.Event()
    calls = []

    def stop_fn(sid, name=None):
        calls.append((sid, name))
        release.wait(5)

    assert sc.request_stop('s1', 'n', stop_fn=stop_fn) == 'started'
    assert sc.is_stopping('s1')
    assert sc.request_stop('s1', 'n', stop_fn=stop_fn) == 'already'
    release.set()
    _wait_idle('s1')
    assert calls == [('s1', 'n')]


def test_stop_thread_is_daemon_named_and_calls_fn_once():
    seen = {}
    done = threading.Event()

    def stop_fn(sid, name=None):
        seen['thread'] = threading.current_thread()
        seen['args'] = (sid, name)
        done.set()

    sc.request_stop('s2', None, stop_fn=stop_fn)
    assert done.wait(5)
    _wait_idle('s2')
    assert seen['thread'].daemon is True
    assert seen['thread'].name == 'rec-stop-s2'
    assert seen['args'] == ('s2', None)


def test_registry_popped_and_timer_cancelled_when_fn_raises(monkeypatch, fake_timers):
    hook_errors = []
    monkeypatch.setattr(threading, 'excepthook', lambda args: hook_errors.append(args.exc_value))
    sc.arm_timer('s3', 10, stop_fn=lambda *a, **k: None, is_live=lambda s: True,
                 timer_factory=fake_timers)
    timer = fake_timers.instances[0]

    def stop_fn(sid, name=None):
        raise RuntimeError('boom')

    sc.request_stop('s3', None, stop_fn=stop_fn)
    _wait_idle('s3')
    assert [str(e) for e in hook_errors] == ['boom']
    assert timer.cancelled is True
    assert sc.request_stop('s3', None, stop_fn=lambda *a, **k: None) == 'started'


def test_session_not_found_is_logged_at_debug_only(caplog):
    def stop_fn(sid, name=None):
        raise SessionNotFoundError(sid)

    with caplog.at_level(logging.DEBUG, logger=sc.logger.name):
        sc.request_stop('s4', None, stop_fn=stop_fn)
        _wait_idle('s4')
    records = [r for r in caplog.records if r.name == sc.logger.name]
    assert any(r.levelno == logging.DEBUG and 's4' in r.getMessage() for r in records)


def test_timer_fires_stop_only_when_still_live(fake_timers):
    stops = []
    live = {'v': True}
    sc.arm_timer('s5', 180, stop_fn=lambda sid, name=None: stops.append((sid, name)),
                 is_live=lambda s: live['v'], timer_factory=fake_timers)
    t = fake_timers.instances[0]
    assert t.delay == 180 * 60 and t.daemon and t.started

    live['v'] = False
    t.fn()
    _wait_idle('s5')
    assert stops == []

    sc.arm_timer('s5', 1, stop_fn=lambda sid, name=None: stops.append((sid, name)),
                 is_live=lambda s: True, timer_factory=fake_timers)
    fake_timers.instances[-1].fn()
    _wait_idle('s5')
    assert stops == [('s5', None)]


def test_rearm_replaces_previous_timer(fake_timers):
    for minutes in (5, 7):
        sc.arm_timer('s6', minutes, stop_fn=lambda *a, **k: None, is_live=lambda s: True,
                     timer_factory=fake_timers)
    first, second = fake_timers.instances
    assert first.cancelled is True
    assert second.cancelled is False


def test_cancel_timer_disarms_and_is_idempotent(fake_timers):
    sc.arm_timer('s7', 3, stop_fn=lambda *a, **k: None, is_live=lambda s: True,
                 timer_factory=fake_timers)
    t = fake_timers.instances[0]
    sc.cancel_timer('s7')
    sc.cancel_timer('s7')
    sc.cancel_timer('never-armed')
    assert t.cancelled is True


def test_reset_cancels_all_timers_and_clears_registry(fake_timers):
    sc.arm_timer('a', 1, stop_fn=lambda *a, **k: None, is_live=lambda s: True,
                 timer_factory=fake_timers)
    sc.arm_timer('b', 1, stop_fn=lambda *a, **k: None, is_live=lambda s: True,
                 timer_factory=fake_timers)
    sc._stopping['c'] = time.monotonic()
    sc.reset()
    assert all(t.cancelled for t in fake_timers.instances)
    assert not sc.is_stopping('c')
