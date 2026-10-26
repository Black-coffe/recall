"""Shared pytest fixtures."""
import os
import sys

# Add the project root to sys.path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(autouse=True)
def _ui_lang_auto(monkeypatch):
    """Interface language in tests — auto (→ Ukrainian), regardless of the owner's .env."""
    monkeypatch.setenv("RECALL_UI_LANG", "auto")


@pytest.fixture(autouse=True)
def _memory_keyring():
    """In-memory keyring: no test touches the real OS credential store."""
    import keyring
    import keyring.backend
    import keyring.errors

    class MemoryKeyring(keyring.backend.KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self._data = {}

        def get_password(self, service, username):
            return self._data.get((service, username))

        def set_password(self, service, username, password):
            self._data[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self._data:
                raise keyring.errors.PasswordDeleteError("not found")
            del self._data[(service, username)]

    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


class _DialogRecorder:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, title, text):
        self.calls.append((title, text))
        return self.answer


@pytest.fixture(autouse=True)
def dialog_stub(monkeypatch):
    """No test shows a window: the approval dialog answers No and records its calls."""
    from app.services import approvals
    stub = _DialogRecorder(approvals.IDNO)
    monkeypatch.setattr(approvals, "_show_dialog", stub)
    return stub


@pytest.fixture
def approve_dialogs(monkeypatch):
    """Opt-in: the approval dialog answers Yes (requested after dialog_stub, so it wins)."""
    from app.services import approvals
    stub = _DialogRecorder(approvals.IDYES)
    monkeypatch.setattr(approvals, "_show_dialog", stub)
    return stub


class _NoticeRecorder:
    """Stands in for start_notice.notify_start: answers ``shown`` and records each call's kwargs."""

    def __init__(self):
        self.shown = True
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.shown


@pytest.fixture(autouse=True)
def notice_stub(monkeypatch, request):
    """No test posts a real toast: an agent-class start sees the notice as shown. After each test
    every duration timer is cancelled and the stopping registry is cleared. tests/test_start_notice.py
    keeps the real notify_start (it stubs the posting layer itself), so the gate is tested, not the stub."""
    from app.services.recording import start_notice, stop_control
    stub = _NoticeRecorder()
    if request.module.__name__.rpartition(".")[2] != "test_start_notice":
        monkeypatch.setattr(start_notice, "notify_start", stub)
    yield stub
    stop_control.reset()


@pytest.fixture
def notice_down(notice_stub):
    """Opt-in: the start toast cannot be shown (no visible desktop)."""
    notice_stub.shown = False
    return notice_stub
