"""Catalog of the server-side interface texts: one module per area.

Every module (except private ``_*`` ones) exports ``MESSAGES: dict`` of the form
``{"<area>.<key>": {"uk": "...", "en": "..."}}``; a language value may be a
dict of plural forms (``uk``: one/few/many, ``en``: one/other).
New modules are picked up automatically (``pkgutil``), no edit of this file needed.
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
    """The single key dictionary; a key duplicated between modules raises ValueError."""
    result: dict = {}
    owner: dict = {}
    for name, messages in load_modules().items():
        for key, value in messages.items():
            if key in result:
                raise ValueError(
                    f"i18n: duplicate key {key!r} in modules {owner[key]!r} and {name!r}")
            result[key] = value
            owner[key] = name
    return result
