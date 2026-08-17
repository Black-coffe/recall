"""T7.4 (REMEDIATION_PLAN Волна 4): єдиний error-handler для НЕоброблених
виключень.

Проблема (аудит): ~124 `except Exception` по кодовій базі. Частина ловить
конкретні винятки і повертає контрольований `jsonify(...)` 4xx/5xx — це НЕ
територія цього модуля, ті місця лишаються як є. Але для того, що взагалі
НЕ спіймане жодним try/except у blueprint'ах, дефолтна поведінка Flask —
генерична "500 Internal Server Error" відповідь без traceback у логах і без
кореляції з X-Request-Id (T8.2). Це і закриває цей модуль.

Реєструється як app-wide ``@app.errorhandler(Exception)``:
  - Ловить будь-яке необроблене виключення з БУДЬ-якого view/blueprint.
  - ``werkzeug.exceptions.HTTPException`` (404, 405, 429, 413, ...)
    пропускаємо без змін — Flask вже або викликав конкретніший handler
    (напр. ``@app.errorhandler(429)`` у app.py), або сформував стандартну
    відповідь; перехоплювати їх тут і підміняти на 500 — регресія, не фіча.
  - Інакше: повний traceback у лог (``logger.exception`` — request_id
    підхоплюється автоматично через ``_RequestIdLogFilter`` з app.py, той
    самий фільтр стоїть на root-логері), клієнту — узагальнене повідомлення
    БЕЗ ``str(exc)`` (жодних internal деталей: шляхи, SQL, стек) + сам
    request_id для кореляції відповіді з лог-записом.

Правило «no bare except without log» (див. CLAUDE.md): будь-який
``except Exception`` без хоча б debug-логу — або додай лог з поясненням
(best-effort/навмисне поглинання), або прибери try/except і дай виключенню
долетіти сюди.
"""
from __future__ import annotations

import logging

from flask import Flask, g, has_request_context, jsonify, request
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)

GENERIC_MESSAGE = "Внутрішня помилка сервера. Спробуйте пізніше."


def _current_request_id() -> str:
    if has_request_context():
        return getattr(g, "request_id", "-")
    return "-"


def register(app: Flask) -> None:
    """Реєструє єдиний catch-all error-handler на весь застосунок."""

    @app.errorhandler(Exception)
    def _handle_unexpected_exception(exc: Exception):
        if isinstance(exc, HTTPException):
            # Уже коректно оброблено Flask/werkzeug (або конкретнішим
            # @app.errorhandler(<code>)) — не підміняємо на 500.
            return exc

        rid = _current_request_id()
        method = request.method if has_request_context() else "?"
        path = request.path if has_request_context() else "?"
        logger.exception("Unhandled exception: %s %s", method, path)
        return jsonify({
            "success": False,
            "error": GENERIC_MESSAGE,
            "request_id": rid,
        }), 500
