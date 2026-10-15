"""Каталог серверних текстів інтерфейсу: по модулю на область.

Кожен модуль (крім приватних ``_*``) експортує ``MESSAGES: dict`` виду
``{"<area>.<key>": {"uk": "...", "en": "..."}}``; значення мови може бути
словником форм плюралю (``uk``: one/few/many, ``en``: one/other).
Нові модулі підхоплюються автоматично (``pkgutil``), без правки цього файлу.
"""
from __future__ import annotations

import importlib
import pkgutil


def load_modules() -> dict:
    """{ім'я модуля: MESSAGES} для всіх модулів пакета."""
    out = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        out[info.name] = getattr(mod, "MESSAGES", {})
    return out


def merged() -> dict:
    """Єдиний словник ключів; дубль ключа між модулями — ValueError."""
    result: dict = {}
    owner: dict = {}
    for name, messages in load_modules().items():
        for key, value in messages.items():
            if key in result:
                raise ValueError(
                    f"i18n: дубль ключа {key!r} у модулях {owner[key]!r} і {name!r}")
            result[key] = value
            owner[key] = name
    return result
