"""Model/library update check.

New Whisper models become available to Recall when the `faster-whisper` library
is upgraded (its `_MODELS` list grows). So "are there new models?" maps to "is a
newer faster-whisper published on PyPI?".

On startup the app spawns ONE background daemon that, only if the last check was
more than CHECK_INTERVAL_DAYS ago (or never), asks PyPI for the latest version
and records the result. No cron — a plain staleness-gated check per launch. The
UI reads the cached status via /api/models/update-status and shows a header
notice when an update is available. Everything is fail-silent (offline → skip).
"""
import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CHECK_INTERVAL_DAYS = 14
PKG = "faster-whisper"
PYPI_URL = f"https://pypi.org/pypi/{PKG}/json"


def _installed_version():
    try:
        import faster_whisper
        return getattr(faster_whisper, "__version__", None)
    except Exception:
        return None


def _pypi_latest(timeout=8.0):
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "Recall/model-update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("info") or {}).get("version")


def _newer(latest, installed):
    if not (latest and installed):
        return False
    try:
        from packaging.version import Version
        return Version(str(latest)) > Version(str(installed))
    except Exception:
        def t(v):
            return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in str(v).split("."))
        return t(latest) > t(installed)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("model_updates: write failed: %s", e)


def read_status(path):
    """Cached status (no network)."""
    st = _read(path)
    installed = _installed_version()
    latest = st.get("latest_version")
    return {
        "package": PKG,
        "installed_version": installed,
        "latest_version": latest,
        "update_available": _newer(latest, installed),
        "upgrade_command": f"pip install -U {PKG}",
        "last_checked": st.get("last_checked"),
        "checked_ts": st.get("checked_ts"),
    }


def check_now(path):
    """Query PyPI, persist, return fresh status. Fail-silent on network errors."""
    installed = _installed_version()
    latest = None
    try:
        latest = _pypi_latest()
    except Exception as e:
        logger.info("model_updates: PyPI check skipped (%s)", e)
    st = _read(path)
    st["checked_ts"] = time.time()
    st["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st["installed_version"] = installed
    if latest:
        st["latest_version"] = latest
    _write(path, st)
    return read_status(path)


def maybe_check(path, max_age_days=CHECK_INTERVAL_DAYS):
    """Run check_now only if the last check is older than max_age_days (or never)."""
    st = _read(path)
    last = st.get("checked_ts")
    fresh = last is not None and (time.time() - float(last)) <= max_age_days * 86400
    if fresh:
        logger.debug("model_updates: fresh (last %s) — skip", st.get("last_checked"))
        return read_status(path)
    logger.info("model_updates: stale/never — checking PyPI for newer %s…", PKG)
    return check_now(path)


def start_background_check(path, max_age_days=CHECK_INTERVAL_DAYS, delay=20.0):
    """Spawn a daemon thread that runs maybe_check after `delay`s (lets app boot)."""
    def _run():
        time.sleep(delay)
        try:
            res = maybe_check(path, max_age_days)
            if res.get("update_available"):
                logger.info("model_updates: update available %s -> %s",
                            res.get("installed_version"), res.get("latest_version"))
        except Exception as e:
            logger.warning("model_updates: background check failed: %s", e)
    t = threading.Thread(target=_run, name="model-update-check", daemon=True)
    t.start()
    return t
