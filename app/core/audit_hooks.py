"""Audit hooks: one ``mcp_actions`` row per mutating ``/api/*`` or ``/connect/*`` request.

No body, no query string and no header value other than the two claimed labels is stored.
A ``/connect/<kind>/<token>`` path is stored as ``/connect/<kind>/*``: the token never lands
in the table. The listener's ``X-Telegram-Token`` skips the audit only on non-destructive
routes; a request to a gated (``_D_ROUTES``) endpoint is always written.
A failing write is logged and never changes the response.
"""
from __future__ import annotations

import hmac
import logging
import re
import time

from flask import Flask, current_app, g, request

from app.core import auth
from app.core.approval_gate import _D_ROUTES
from app.repositories import mcp_actions

logger = logging.getLogger(__name__)

# Flask endpoint names too frequent to audit. Starts empty on purpose.
_AUDIT_SKIP: set[str] = set()

_MAX_BODY_BYTES = 64 * 1024
_NON_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def _label(value: str | None) -> str | None:
    if not value:
        return None
    return _NON_PRINTABLE.sub("", value)[:64] or None


def _is_listener() -> bool:
    """True only when ``X-Telegram-Token`` matches the listener's control token.

    A forged or empty header is audited like any other request; a failure to read the
    token counts as "not the listener" (fail closed: the row is written).
    """
    sent = request.headers.get("X-Telegram-Token")
    if not sent:
        return False
    try:
        from telegram_common import control_token
        expected = control_token()
    except Exception:
        logger.debug("audit: control token unavailable; auditing the request", exc_info=True)
        return False
    return bool(expected) and hmac.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))


def _should_audit() -> bool:
    if request.method == "GET" or request.endpoint in _AUDIT_SKIP:
        return False
    if request.path.startswith("/connect/"):
        return True
    if not request.path.startswith("/api/"):
        return False
    return request.endpoint in _D_ROUTES or not _is_listener()


def _audit_path() -> str:
    """The request path, with the one-time token of a connect link redacted."""
    if not request.path.startswith("/connect/"):
        return request.path
    from app.services.connect_links import KINDS
    kind = (request.view_args or {}).get("kind")
    return f"/connect/{kind}/*" if kind in KINDS else "/connect/*"


def _error_code(response) -> str | None:
    try:
        if response.is_streamed or response.direct_passthrough:
            return None
        if response.mimetype != "application/json":
            return None
        length = response.content_length
        if length is not None and length > _MAX_BODY_BYTES:
            return None
        body = response.get_json(silent=True)
        if isinstance(body, dict):
            code = body.get("error_code")
            if isinstance(code, str):
                return code[:64]
    except Exception:
        logger.debug("audit: could not read error_code", exc_info=True)
    return None


def _write(status: int | None, error_code: str | None) -> None:
    g.recall_audited = True
    try:
        t0 = getattr(g, "recall_t0", None)
        duration = int((time.perf_counter() - t0) * 1000) if t0 is not None else 0
        extra = getattr(g, "recall_audit", None) or {}
        row = {
            "request_id": getattr(g, "request_id", None),
            "actor": auth.classify_request()[0],
            "claimed_actor": _label(request.headers.get("X-Recall-Actor")),
            "claimed_tool": _label(request.headers.get("X-Recall-Tool")),
            "method": request.method,
            "path": _audit_path(),
            "endpoint": request.endpoint,
            "action": extra.get("action"),
            "object_ref": extra.get("object_ref"),
            "summary": extra.get("summary"),
            "decision": extra.get("decision"),
            "http_status": status,
            "error_code": error_code,
            "duration_ms": max(0, duration),
        }
        mcp_actions.insert(current_app.config["DATABASE"], row)
    except Exception as e:
        logger.warning("audit: could not write mcp_actions row: %s", e)


def register(app: Flask) -> None:
    @app.before_request
    def _audit_start():  # noqa: ANN202
        g.recall_t0 = time.perf_counter()

    @app.after_request
    def _audit_after(response):  # noqa: ANN202
        if _should_audit() and not getattr(g, "recall_audited", False):
            code = _error_code(response)
            if code is None and response.status_code == 500:
                code = "INTERNAL_ERROR"
            _write(response.status_code, code)
        return response

    @app.teardown_request
    def _audit_teardown(exc):  # noqa: ANN202
        if exc is not None and not getattr(g, "recall_audited", False) and _should_audit():
            _write(500, "INTERNAL_ERROR")
