"""Тести NL-опису аркушів таблиць (Phase 16C).

Лёгкі — text_polishing імпортується без anthropic (клієнт lazy). Перевіряємо
graceful-гілку (порожній вхід → без виклику API). Happy-path потребує ключа
і перевіряється вручну.
"""
from app.services import text_polishing as tp


def test_describe_sheets_empty_returns_empty():
    res = tp.describe_sheets([])
    assert res["descriptions"] == []
    assert res["input_tokens"] == 0


def test_describe_sheets_none_returns_empty():
    res = tp.describe_sheets(None)
    assert res["descriptions"] == []
