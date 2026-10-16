"""T1.2 (REMEDIATION_PLAN Wave 1): authentication gate on all /api/*.

Problem: 0 identity checks on ~90 endpoints (recording/deletion/export/
expensive Claude calls). Only /api/telegram/ingest was protected by a secret so far
(shared-secret, telegram_common.py) and MCP --transport http (RECALL_MCP_KEY).
Combined with a network bind (Wave 0: default 127.0.0.1, but the opt-in
RECALL_BIND_ALL exists) — an attack vector for password-less LAN access.

Decision: ONE centralized `before_request` gate (not a decorator on every
endpoint — there are ~90, easy to forget a new one). The key pattern is copied from the existing
Telegram shared-secret (telegram_common.control_token) — we do not pull in
flask-login/JWT for a single-user application.

Decision logic (in order of checking):
  1. OPTIONS (CORS preflight) and paths outside the allowlist are exempt — always crossed.
  2. RECALL_LOCAL_TRUSTED (default "1"/on) AND a request from localhost
     (request.remote_addr ∈ {127.0.0.1, ::1}) → let through WITHOUT a key.
     This preserves the current UX: the app works on its own machine without a password.
  3. Otherwise a valid key is needed: RECALL_API_KEY must be set in env,
     and the client must send the same key in the X-Recall-Api-Key header
     (or Authorization: Bearer <key>). The comparison is constant-time
     (hmac.compare_digest), so the comparison time does not leak.
  4. If RECALL_API_KEY is NOT set and access does not fall under local-trusted
     (not localhost, or RECALL_LOCAL_TRUSTED=0) → fail-closed: 401 for
     ANY key / its absence. Without a configured key, external
     access is denied by default.

Allowlist (no auth, the gate is not applied at all):
  - everything that does NOT start with /api/  (SPA shell '/', /<path:_path>, /static/*,
    /favicon.ico, /sw.js — they are not /api/* anyway, so the rule "gate only on
    /api/*" covers them automatically)
  - /api/health (the health check stays open for monitoring)
  - OPTIONS requests (CORS preflight, otherwise we break CORS from app.py ~231)

Proxied access (K5-04, owner 15.10.2026: "After Tailscale access is turned on, the vps-prod
server will be able to open all of Recall without a password until you yourself close its access."):
`tailscale serve` delivers a tailnet request as 127.0.0.1, so local-trusted would let in the WHOLE
tailnet. A localhost request carrying X-Forwarded-For or Tailscale-User-Login is proxied:
local-trusted does not apply to it, we let in only IPs from RECALL_PROXY_ALLOWED_IPS (the first address of
X-Forwarded-For; serve overwrites it, the client cannot forge it) or a valid key. Devices are
told apart by IP, not by Tailscale-User-Login: it is the same for all untagged nodes of the owner.

NOT gated here: /api/telegram/ingest has its OWN shared-secret mechanism
(X-Telegram-Token, telegram_common.control_token) — this gate is added
ON TOP of it, and does not replace it. In the default mode (RECALL_LOCAL_TRUSTED=1)
listener::ingest() comes from localhost → local-trusted lets it through, behaviour does not
change. If the operator turns off RECALL_LOCAL_TRUSTED (RECALL_LOCAL_TRUSTED=0)
WITHOUT updating telegram_listener.py so that it also sends X-Recall-Api-Key —
ingest will start getting 401 from THIS gate (telegram_listener.py is deliberately
outside the scope of this task — not touched). Documented as a known adjacent
problem in the T1.2 report, not fixed here.
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import Flask, jsonify, request

from app.core import settings

logger = logging.getLogger(__name__)

# Header modelled on telegram_common.CONTROL_TOKEN_HEADER ("X-Telegram-Token").
API_KEY_HEADER = "X-Recall-Api-Key"

_LOCALHOST_ADDRS = {"127.0.0.1", "::1"}

# /api/* paths that stay open without a key (the health check for
# monitoring — must not require authorization, otherwise external
# health checks/uptime monitors would also need authorizing). /api/ready
# (config-registry-profiles S3, does not exist yet at the time of S2) — the same class:
# no auth, like /api/health.
_EXEMPT_API_PATHS = {"/api/health", "/api/ready"}


def _local_trusted_enabled() -> bool:
    """RECALL_LOCAL_TRUSTED; the profile-dependent default lives in the registry
    (`app/core/settings.py::Setting.default_headless`, config-registry-fix-01):
    desktop — "1" (as it was, the app works on its own machine without a password);
    headless — "0" (fail-closed — headless typically listens wider than one machine,
    localhost is no longer assumed to be trusted). An explicit value of the environment
    variable always takes priority over the profile default (settings.env)."""
    return settings.env_bool("RECALL_LOCAL_TRUSTED")


def _configured_api_key() -> str | None:
    val = os.environ.get("RECALL_API_KEY", "").strip()
    return val or None


def _is_localhost(remote_addr: str | None) -> bool:
    return (remote_addr or "") in _LOCALHOST_ADDRS


_PROXY_HEADERS = ("X-Forwarded-For", "Tailscale-User-Login")


def _proxied_client() -> str | None:
    """None — the request is direct; otherwise the client IP behind a proxy ("" — the proxy did not name an IP)."""
    if not any(request.headers.get(h) for h in _PROXY_HEADERS):
        return None
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    # serve does Header.Set — exactly one address. A chain means another proxy or
    # a substituted value: the IP is unknown, from here on only the key.
    return "" if "," in xff else xff


def _proxy_allowed_ips() -> set[str]:
    raw = settings.env("RECALL_PROXY_ALLOWED_IPS") or ""
    return {ip.strip() for ip in raw.replace(";", ",").split(",") if ip.strip()}


def _extract_presented_key() -> str | None:
    """X-Recall-Api-Key takes priority; Authorization: Bearer <key> — a convenient fallback
    (e.g. for clients that already have the standard Bearer plumbing)."""
    header_key = request.headers.get(API_KEY_HEADER)
    if header_key:
        return header_key.strip()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[len("bearer "):].strip()
    return None


def _is_exempt(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    if not path.startswith("/api/"):
        # SPA shell ('/', deep-links), /static/*, /favicon.ico, /sw.js — all
        # of this is outside /api/* and was meant to stay accessible without a key.
        return True
    if path in _EXEMPT_API_PATHS:
        return True
    return False


def _is_authorized() -> bool:
    if _local_trusted_enabled() and _is_localhost(request.remote_addr):
        client = _proxied_client()
        if client is None:
            return True
        if client and client in _proxy_allowed_ips():
            return True
        # Proxied and not in the list — from here on only with a key.

    configured = _configured_api_key()
    if not configured:
        # Fail-closed: without a configured key, external (non-local-trusted)
        # access is denied by default.
        return False

    presented = _extract_presented_key()
    if not presented:
        return False
    return hmac.compare_digest(presented, configured)


def register(app: Flask) -> None:
    """Registers the single before_request gate for the whole application."""

    if settings.profile() == "headless" and not _configured_api_key():
        logger.warning(
            "Auth gate: profile=headless without RECALL_API_KEY — non-localhost "
            "access will be rejected (fail-closed) until a key is set or "
            "RECALL_LOCAL_TRUSTED=1 is explicitly enabled."
        )

    @app.before_request
    def _recall_auth_gate():  # noqa: ANN202 — Flask hook, no return annotation
        if _is_exempt(request.path, request.method):
            return None
        if _is_authorized():
            return None
        logger.warning(
            "Auth gate: 401 %s %s (remote=%s)",
            request.method, request.path, request.remote_addr,
        )
        return jsonify({"success": False, "error": "unauthorized"}), 401
