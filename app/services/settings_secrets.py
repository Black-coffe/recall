"""Anthropic API key persistence shared by the Settings route and the one-time connect page.

The key is written to ``.env`` (only that name) and to ``os.environ``. It is never logged
and never returned.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from flask import current_app

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r'^sk-ant-[A-Za-z0-9_\-]{8,300}$')

ERR_EMPTY = 'core.settings.api_key_empty'
ERR_FORMAT = 'core.settings.api_key_format'
ERR_ENV_WRITE = 'core.settings.env_write_failed'


def anthropic_key_configured() -> bool:
    return bool((os.environ.get('ANTHROPIC_API_KEY') or '').strip())


def save_anthropic_key(key: str) -> dict:
    """Validate (format only), write to .env and apply at once. Returns ``{'configured': True}``.

    Raises ``ValueError(<i18n error key>)`` on rejected input or a failed .env write.
    """
    key = (key or '').strip()
    if not key:
        raise ValueError(ERR_EMPTY)
    if not _KEY_RE.match(key):
        raise ValueError(ERR_FORMAT)
    try:
        from app.core import env_file
        env_file.set_env_var('ANTHROPIC_API_KEY', key, path=Path(current_app.root_path) / '.env')
    except Exception as e:
        logger.error("Failed to save ANTHROPIC_API_KEY to .env: %s", type(e).__name__)
        raise ValueError(ERR_ENV_WRITE) from None
    # Drop a memoized client so the next call builds one with the fresh key.
    from app.services import text_polishing
    text_polishing._client = None
    logger.info("ANTHROPIC_API_KEY updated (length=%d, the key is not logged)", len(key))
    return {'configured': True}
