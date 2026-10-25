import sys
import threading
import time

import pytest

from app.services import approvals
from app.services.approvals import IDNO, IDYES, DialogUnavailable

TEXT = "Видалити запис №5?"


def test_yes_is_approved(monkeypatch):
    monkeypatch.setattr(approvals, "_show_dialog", lambda t, x: IDYES)
    d = approvals.ask(TEXT)
    assert d.status == "approved"


def test_no_is_denied(monkeypatch):
    monkeypatch.setattr(approvals, "_show_dialog", lambda t, x: IDNO)
    d = approvals.ask(TEXT)
    assert (d.status, d.reason) == ("denied", "no")


def test_unavailable(monkeypatch):
    def boom(t, x):
        raise DialogUnavailable("no_desktop")
    monkeypatch.setattr(approvals, "_show_dialog", boom)
    d = approvals.ask(TEXT)
    assert (d.status, d.reason) == ("unavailable", "no_desktop")


def test_timeout_then_late_yes_is_discarded(monkeypatch):
    gate = threading.Event()

    def slow(t, x):
        gate.wait(5)
        return IDYES
    monkeypatch.setattr(approvals, "_show_dialog", slow)
    t0 = time.monotonic()
    d = approvals.ask(TEXT, timeout_s=0.2)
    assert time.monotonic() - t0 < 1
    assert (d.status, d.reason) == ("denied", "timeout")
    gate.set()
    time.sleep(0.1)
    assert d.status == "denied"
    assert approvals.busy() is False


def test_second_caller_gets_pending(monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def blocking(t, x):
        calls.append(t)
        entered.set()
        gate.wait(5)
        return IDYES
    monkeypatch.setattr(approvals, "_show_dialog", blocking)
    out = {}
    th = threading.Thread(target=lambda: out.update(first=approvals.ask(TEXT, timeout_s=3)))
    th.start()
    assert entered.wait(2)
    assert approvals.busy() is True
    second = approvals.ask(TEXT, timeout_s=0.2)
    assert (second.status, second.reason) == ("pending", "busy")
    gate.set()
    th.join(3)
    assert out["first"].status == "approved"
    assert len(calls) == 1


def test_title_nonce_and_text_passthrough(monkeypatch):
    seen = []
    monkeypatch.setattr(approvals, "_show_dialog", lambda t, x: seen.append((t, x)) or IDNO)
    approvals.ask(TEXT)
    approvals.ask(TEXT)
    (t1, x1), (t2, x2) = seen
    assert t1.startswith("Recall") and t2.startswith("Recall")
    assert len(t1.rsplit(" ", 1)[-1]) == 4
    assert t1 != t2
    assert x1 == x2 == TEXT


def test_real_dialog_not_windows(monkeypatch):
    monkeypatch.undo()  # drop the autouse stub to reach the real function
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(approvals, "_user32", lambda: pytest.fail("touched ctypes"))
    with pytest.raises(DialogUnavailable) as e:
        approvals._show_dialog("t", TEXT)
    assert e.value.reason == "not_windows"


def test_real_dialog_invisible_station(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(sys, "platform", "win32")

    class Fake:
        def GetProcessWindowStation(self):
            return 1

        def GetUserObjectInformationW(self, *a):
            return 1  # success, flags stay zero: not visible

        def MessageBoxW(self, *a):
            pytest.fail("MessageBoxW must not be called")

    monkeypatch.setattr(approvals, "_user32", lambda: Fake())
    with pytest.raises(DialogUnavailable) as e:
        approvals._show_dialog("t", TEXT)
    assert e.value.reason == "no_desktop"


def test_autouse_stub_denies_without_ctypes(monkeypatch, dialog_stub):
    monkeypatch.setattr(approvals, "_user32", lambda: pytest.fail("touched ctypes"))
    d = approvals.ask(TEXT)
    assert d.status == "denied"
    assert len(dialog_stub.calls) == 1


def test_approve_dialogs_wins(approve_dialogs):
    assert approvals.ask(TEXT).status == "approved"
    assert len(approve_dialogs.calls) == 1
