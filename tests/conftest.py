"""Pytest fixtures загального користування."""
import os
import sys

# Додаємо корень проекту в sys.path щоб імпорти працювали
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(autouse=True)
def _ui_lang_auto(monkeypatch):
    """Мова інтерфейсу в тестах — auto (→ українська), незалежно від .env власника."""
    monkeypatch.setenv("RECALL_UI_LANG", "auto")
