"""One-time connect links for secrets typed on a local page (in memory only).

A link lives 600 s and dies on success (``consume``), on the third failed attempt or at
expiry. ``peek`` never consumes.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Optional

KINDS = ("mail", "anthropic_key")
TTL_SECONDS = 600
MAX_FAILURES = 3

_clock = time.time  # tests patch this
_lock = threading.Lock()
_links: dict[str, dict] = {}  # token -> link


def _purge(now: float) -> None:
    for tok in [t for t, l in _links.items() if l["expires_at"] <= now]:
        del _links[tok]


def issue(kind: str, *, subject: Optional[str] = None, requested_by: str = "unknown") -> dict:
    if kind not in KINDS:
        raise ValueError("unknown connect link kind")
    now = _clock()
    with _lock:
        _purge(now)
        for tok in [t for t, l in _links.items() if l["kind"] == kind and l["subject"] == subject]:
            del _links[tok]
        token = secrets.token_urlsafe(32)
        link = {"token": token, "kind": kind, "subject": subject, "requested_by": requested_by,
                "created_at": now, "expires_at": now + TTL_SECONDS, "expires_in": TTL_SECONDS,
                "failures": 0}
        _links[token] = link
        return dict(link)


def _live(kind: str, token: str, now: float) -> Optional[dict]:
    link = _links.get(token)
    if link is None or link["kind"] != kind:
        return None
    if link["expires_at"] <= now:
        del _links[token]
        return None
    return link


def peek(kind: str, token: str) -> Optional[dict]:
    with _lock:
        link = _live(kind, token, _clock())
        return dict(link) if link else None


def consume(kind: str, token: str) -> Optional[dict]:
    with _lock:
        link = _live(kind, token, _clock())
        if link is None:
            return None
        del _links[token]
        return dict(link)


def record_failure(kind: str, token: str) -> int:
    """Count a failed attempt; returns the attempts left (0 = the link is dead)."""
    with _lock:
        link = _live(kind, token, _clock())
        if link is None:
            return 0
        link["failures"] += 1
        left = MAX_FAILURES - link["failures"]
        if left <= 0:
            del _links[token]
            return 0
        return left


def url_for(kind: str, token: str, port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/connect/{kind}/{token}"


def port_from_host(host: str, default: int = 5050) -> int:
    """Port of a ``Host`` header value (the app is reached directly on loopback)."""
    tail = host.rsplit(":", 1)[-1] if ":" in host else ""
    return int(tail) if tail.isdigit() else default
