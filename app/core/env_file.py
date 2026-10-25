"""The only writer of the repo-root ``.env``.

Every write goes through one process lock, so two threads writing different keys
never lose an update. Stdlib and python-dotenv only; imports nothing from ``config``.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

#: ``<repo root>/.env`` (this file lives in ``app/core``).
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_lock = threading.Lock()


def _target(path) -> Path:
    return Path(path) if path is not None else ENV_PATH


def set_env_var(key: str, value: str, *, path=None) -> None:
    """Write ``key`` to the file (quoted) and apply it to ``os.environ``."""
    from dotenv import set_key
    target = _target(path)
    with _lock:
        target.touch(exist_ok=True)  # set_key requires an existing file
        set_key(str(target), key, value, quote_mode="always", encoding="utf-8")
        os.environ[key] = value


def unset_env_var(key: str, *, path=None) -> bool:
    """Remove the line from the file and drop the key from ``os.environ``.

    Returns True when a line was removed from the file."""
    from dotenv import unset_key
    target = _target(path)
    removed = False
    with _lock:
        if target.exists():
            result: Optional[bool] = unset_key(str(target), key, encoding="utf-8")[0]
            removed = bool(result)
        os.environ.pop(key, None)
    return removed
