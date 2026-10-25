"""Diagnostic package (T8.1): one zip with redacted log tails, system_info and version/commit.

The simple variant from REMEDIATION_PLAN (no Sentry): the latest lines of the file logs of
three processes (app.py/telegram_listener.py/mcp_server.py - see app/core/logger.py,
telegram_listener.py:43-70, mcp_server.py:41-74) + system_info + version/commit, for manual
sending with a complaint. NEVER includes transcripts/DB/.env - only a short log tail, passed
through the redaction below.

``build_package()`` returns the zip bytes (``GET /api/system/diagnostic-package`` streams them);
``write_package()`` stores one under ``logs/diagnostics/`` and keeps the newest five.
Both read paths through ``current_app`` (root path, ``LOG_FILE``), so they run in an app context.
"""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from typing import Optional, Tuple

from flask import current_app

from app import state
from app.utils.proc import NO_WINDOW


logger = logging.getLogger(__name__)

DIAGNOSTIC_LOG_TAIL_LINES = 500

# arcname in the zip -> path relative to the project root (current_app.root_path).
# whisper_ui.log is configurable (config.LOG_FILE), the other two are fixed
# names of separate processes (deliberately different files, to avoid WinError 32
# on simultaneous rollover from several processes - see the comments there).
_DIAGNOSTIC_LOG_SOURCES = (
    ("whisper_ui.log", None),  # None -> read from current_app.config['LOG_FILE']
    ("telegram_listener.log", "telegram_listener.log"),
    ("mcp_server.log", "mcp_server.log"),
)

# Values that look like keys/tokens/hashes - masked before packing.
# 1) "bare" Anthropic keys (sk-ant-...), even when they occur outside a
#    name=value pair (e.g. in an SDK error text).
_RE_BARE_ANTHROPIC_KEY = re.compile(r'sk-ant-[A-Za-z0-9_\-]{10,}')
# 2) NAME=value / NAME: value pairs where NAME looks like a secret:
#    *_KEY, *_TOKEN, *_HASH, *_SECRET, *_PASSWORD, as well as api_id/api_hash
#    (TELEGRAM_API_ID, TELEGRAM_API_HASH, ANTHROPIC_API_KEY, SECRET_KEY, ...).
_RE_SECRET_ASSIGNMENT = re.compile(
    r'(?i)\b([A-Za-z][\w.]*(?:api[_-]?key|api[_-]?hash|api[_-]?id|secret|token|'
    r'password|passwd|_key|_hash)[\w.]*)\s*([:=])\s*(\S+)'
)

DEFAULT_DEST_DIR = os.path.join("logs", "diagnostics")
DEFAULT_KEEP = 5


def _redact_secrets(line: str) -> str:
    """Masks values that look like keys/tokens/hashes in a single log line."""
    line = _RE_BARE_ANTHROPIC_KEY.sub('sk-ant-***REDACTED***', line)
    line = _RE_SECRET_ASSIGNMENT.sub(r'\1\2***REDACTED***', line)
    return line


def _tail_redacted(path: str, max_lines: int) -> str:
    """The last max_lines lines of a file, each passed through redaction."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    tail = lines[-max_lines:]
    return ''.join(_redact_secrets(l) for l in tail)


def _git_commit_short():
    """Short git commit, if cheap to get (repo exists, git in PATH). None otherwise."""
    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=current_app.root_path, capture_output=True, text=True, timeout=2,
            creationflags=NO_WINDOW,
        )
        if res.returncode == 0:
            return res.stdout.strip() or None
    except Exception as e:  # best-effort: the package is useful without a commit
        logger.debug("diagnostics: git commit lookup failed: %s", e)
    return None


def _build() -> Tuple[bytes, dict]:
    """(zip bytes, manifest)."""
    from app.blueprints.system import APP_VERSION
    buf = io.BytesIO()
    included_logs = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for arcname, relpath in _DIAGNOSTIC_LOG_SOURCES:
            relpath = relpath or current_app.config.get('LOG_FILE', 'whisper_ui.log')
            path = relpath if os.path.isabs(relpath) else os.path.join(current_app.root_path, relpath)
            if not os.path.isfile(path):
                continue
            try:
                content = _tail_redacted(path, DIAGNOSTIC_LOG_TAIL_LINES)
            except OSError as e:
                logger.warning(f"Failed to read log {path} for the diagnostic package: {e}")
                continue
            zf.writestr(f"logs/{arcname}", content)
            included_logs.append(arcname)

        try:
            sys_info = state.whisper_manager.get_system_info() if state.whisper_manager else {}
        except Exception as e:
            sys_info = {"error": str(e)}
        zf.writestr("system_info.json", json.dumps(sys_info, ensure_ascii=False, indent=2, default=str))

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "app_version": APP_VERSION,
            "git_commit": _git_commit_short(),
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "included_logs": included_logs,
            "log_tail_lines": DIAGNOSTIC_LOG_TAIL_LINES,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue(), manifest


def build_package() -> bytes:
    """The diagnostic zip as bytes."""
    return _build()[0]


def write_package(dest_dir: str = DEFAULT_DEST_DIR, keep: int = DEFAULT_KEEP,
                  now: Optional[datetime] = None) -> dict:
    """Write the zip to ``<dest_dir>/<YYYYMMDD-HHMMSS>.zip`` (relative to the project root) and
    remove the oldest zips beyond ``keep``. Returns ``{path, size, included_logs, app_version,
    git_commit}``."""
    if not os.path.isabs(dest_dir):
        dest_dir = os.path.join(current_app.root_path, dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    data, manifest = _build()
    stamp = (now or datetime.now()).strftime('%Y%m%d-%H%M%S')
    path = os.path.join(dest_dir, f"{stamp}.zip")
    n = 1
    while os.path.exists(path):  # two packages in one second keep both
        path = os.path.join(dest_dir, f"{stamp}-{n}.zip")
        n += 1
    with open(path, 'wb') as f:
        f.write(data)
    _prune(dest_dir, keep)
    return {
        "path": os.path.abspath(path),
        "size": len(data),
        "included_logs": manifest["included_logs"],
        "app_version": manifest["app_version"],
        "git_commit": manifest["git_commit"],
    }


def _prune(dest_dir: str, keep: int) -> None:
    """Remove the oldest ``*.zip`` in dest_dir beyond ``keep`` (by mtime, then name)."""
    zips = []
    for name in os.listdir(dest_dir):
        full = os.path.join(dest_dir, name)
        if name.lower().endswith('.zip') and os.path.isfile(full):
            zips.append((os.stat(full).st_mtime_ns, name, full))
    zips.sort()
    for _, _, full in zips[:max(0, len(zips) - max(1, keep))]:
        try:
            os.remove(full)
        except OSError as e:
            logger.warning("diagnostics: could not prune %s: %s", full, e)
