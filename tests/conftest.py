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
