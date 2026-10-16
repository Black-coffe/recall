"""T7.4 (REMEDIATION_PLAN Wave 4): a single error handler for UNhandled
exceptions.

Problem (audit): ~124 `except Exception` across the codebase. Some catch
specific exceptions and return a controlled `jsonify(...)` 4xx/5xx — that is NOT
this module's territory, those places stay as they are. But for whatever is
NOT caught by any try/except in the blueprints, the default Flask behaviour is
a generic "500 Internal Server Error" response with no traceback in the logs and no
correlation with X-Request-Id (T8.2). This module closes that gap.

Registered as an app-wide ``@app.errorhandler(Exception)``:
  - Catches any unhandled exception from ANY view/blueprint.
  - ``werkzeug.exceptions.HTTPException`` (404, 405, 429, 413, ...)
    we pass through unchanged — Flask has already either called a more specific handler
    (e.g. ``@app.errorhandler(429)`` in app.py), or built a standard
    response; intercepting them here and replacing with 500 is a regression, not a feature.
  - Otherwise: full traceback to the log (``logger.exception`` — request_id
    is picked up automatically via ``_RequestIdLogFilter`` from app.py, the same
    filter sits on the root logger), the client gets a generic message
    WITHOUT ``str(exc)`` (no internal details: paths, SQL, stack) plus the
    request_id itself to correlate the response with the log entry.

The "no bare except without log" rule (see CLAUDE.md): any
``except Exception`` without at least a debug log — either add a log with an explanation
(best-effort/intentional swallowing), or remove the try/except and let the exception
reach this handler.
"""
from __future__ import annotations

import logging

from flask import Flask, g, has_request_context, jsonify, request
from werkzeug.exceptions import HTTPException

from app.core.i18n import tr

logger = logging.getLogger(__name__)

GENERIC_MESSAGE = tr("core.error.internal", lang="uk")  # uk text for compatibility; the response is localized by tr()


def _current_request_id() -> str:
    if has_request_context():
        return getattr(g, "request_id", "-")
    return "-"


def register(app: Flask) -> None:
    """Registers the single catch-all error handler for the whole application."""

    @app.errorhandler(Exception)
    def _handle_unexpected_exception(exc: Exception):
        if isinstance(exc, HTTPException):
            # Already handled correctly by Flask/werkzeug (or by a more specific
            # @app.errorhandler(<code>)) — do not replace with 500.
            return exc

        rid = _current_request_id()
        method = request.method if has_request_context() else "?"
        path = request.path if has_request_context() else "?"
        logger.exception("Unhandled exception: %s %s", method, path)
        return jsonify({
            "success": False,
            "error": tr("core.error.internal"),
            "request_id": rid,
        }), 500
