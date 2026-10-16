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
