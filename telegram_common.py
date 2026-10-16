"""Shared module for the Flask blueprint and the Telegram listener (Phase 17C).

Shared secret for the two-way localhost link:
  • Flask → listener control-API (/dialogs, /status, /backfill)
  • listener → Flask /api/telegram/ingest

A localhost-IP check can be spoofed and does not protect against other LOCAL processes,
so both ends require the X-Telegram-Token header. The token is shared:
generated once and persisted in telegram_control.token (gitignored). Both
processes read the same file, so the token matches automatically, with no manual steps.
It can be overridden via the TELEGRAM_CONTROL_TOKEN env var.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

CONTROL_TOKEN_HEADER = "X-Telegram-Token"
_TOKEN_FILE = Path(__file__).parent / "telegram_control.token"


def control_token() -> str:
    """Shared Flask<->listener secret. env override -> file -> generate atomically."""
    env = os.environ.get("TELEGRAM_CONTROL_TOKEN")
    if env and env.strip():
        return env.strip()

    token = secrets.token_hex(32)
    try:
        # O_CREAT|O_EXCL is atomic: only the first process creates it, the rest read.
        fd = os.open(_TOKEN_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
        return token
    except FileExistsError:
        return _TOKEN_FILE.read_text(encoding="ascii").strip()
    except OSError:
        # Could not write (permissions/FS): the token lives only in this process's memory.
        # A second process would generate its own and the link would fail; that is a
        # degradation, not a crash. The caller logs it.
        return token
