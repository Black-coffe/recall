"""Серверний шар перекладу: мова запиту, `tr`/`tr_n` над пакетом `i18n_catalog`.

Мова: заголовок ``X-Recall-Lang: en|uk`` → вона; нема/сміття → ``server_lang()``
(явне ``RECALL_UI_LANG`` uk/en, ``auto`` → uk). ``RECALL_UI_LANG`` читається
з ``os.environ`` на кожен виклик, тож зміна через Налаштування діє без рестарту.
"""
from __future__ import annotations

import logging
import os

from flask import has_request_context, request

from app.core import i18n_catalog

logger = logging.getLogger(__name__)

LANGS = ("uk", "en")
SETTINGS = ("auto", "uk", "en")
HEADER = "X-Recall-Lang"

_catalog: dict | None = None


def _get_catalog() -> dict:
    global _catalog
    if _catalog is None:
        _catalog = i18n_catalog.merged()
    return _catalog


def ui_lang_setting() -> str:
    v = (os.environ.get("RECALL_UI_LANG") or "auto").strip().lower()
    return v if v in SETTINGS else "auto"


def server_lang() -> str:
    return "en" if ui_lang_setting() == "en" else "uk"


def request_lang() -> str:
    if has_request_context():
        h = (request.headers.get(HEADER) or "").strip().lower()
        if h in LANGS:
            return h
    return server_lang()


def tr(key: str, lang: str | None = None, **vars) -> str:
    entry = _get_catalog().get(key)
    if entry is None:
        logger.warning("i18n: невідомий ключ %r", key)
        return key
    lang = lang if lang in LANGS else request_lang()
    text = entry.get(lang) or entry.get("uk")
    if isinstance(text, dict):
        logger.warning("i18n: ключ %r — плюраль, викликайте tr_n", key)
        return key
    return text.format_map(vars) if vars else text


def _plural_form(lang: str, n: int) -> str:
    if lang == "en":
        return "one" if n == 1 else "other"
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def tr_n(key: str, n: int, lang: str | None = None, **vars) -> str:
    entry = _get_catalog().get(key)
    if entry is None:
        logger.warning("i18n: невідомий ключ %r", key)
        return key
    lang = lang if lang in LANGS else request_lang()
    forms = entry.get(lang) or entry.get("uk")
    if not isinstance(forms, dict):
        logger.warning("i18n: ключ %r — не плюраль", key)
        return key
    form = _plural_form(lang, n)
    text = forms.get(form) or forms.get("other") or forms.get("many") or next(iter(forms.values()))
    return text.format_map({**vars, "n": n})


_SSE_FIELDS = ("error", "label", "text")


def _uk_pattern(key: str):
    """Регекс за uk-шаблоном ключа: ``{var}`` → іменована група."""
    import re
    uk = _get_catalog()[key]["uk"]
    parts = re.split(r"\{(\w+)\}", uk)
    rx = "".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>.+?)"
                 for i, p in enumerate(parts))
    return re.compile(rx + r"\Z")


def localize_sse(frames, lang: str, keys):
    """Перекласти тексти статусів/помилок у SSE-кадрах сервісу на мову запиту.

    Сервіси (research/rag) стрімлять українські рядки поза request-контекстом;
    blueprint, що знає мову, пропускає кадри крізь цей фільтр. Поле
    ``error``/``label``/``text`` кадру, яке дослівно збігається з uk-текстом
    одного з ``keys``, замінюється на текст мови ``lang``; решта — як є.
    """
    import json
    if lang not in LANGS or lang == "uk":
        yield from frames
        return
    patterns = [(k, _uk_pattern(k)) for k in keys]
    for frame in frames:
        head, sep, rest = frame.partition("\ndata: ")
        if not sep or not head.startswith("event: "):
            yield frame
            continue
        body = rest.rstrip("\n")
        try:
            data = json.loads(body)
        except ValueError:
            yield frame
            continue
        changed = False
        if isinstance(data, dict):
            for field in _SSE_FIELDS:
                val = data.get(field)
                if not isinstance(val, str):
                    continue
                for key, rx in patterns:
                    m = rx.match(val)
                    if m:
                        data[field] = tr(key, lang=lang, **m.groupdict())
                        changed = True
                        break
        if not changed:
            yield frame
            continue
        yield f"{head}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
