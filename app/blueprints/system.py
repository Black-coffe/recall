"""System & meta endpoints (Phase 5.5).

- /                          (index template)
- /api/health
- /api/models
- /api/download_model
- /api/system_info
- /api/system_stats
- /api/polish/availability
- /api/metrics
"""
import io
import json
import logging
import os
import pathlib
import platform
import re
import sqlite3
import subprocess
import zipfile
from datetime import datetime, timezone

import torch
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

from app import state
from app.core.i18n import tr, ui_lang_setting
from app.services import text_polishing
from app.services.models import get_default_model
from app.utils.proc import NO_WINDOW


logger = logging.getLogger(__name__)
system_bp = Blueprint('system', __name__)

# Shared with /api/health — one version for the whole blueprint (T8.1).
APP_VERSION = "4.17.0"


@system_bp.route('/', defaults={'_path': ''}, methods=['GET'])
@system_bp.route('/<path:_path>', methods=['GET'])
def app_shell(_path):
    """New client shell. Served on any non-API path,
    so the client router can handle deep links on a cold start
    (e.g. /transcript/2125-..., /entities/5).

    More specific rules (Flask static, concrete /api/... routes, /favicon.ico,
    /sw.js) have higher priority by specificity and are matched first.
    The guard on 'api/' is a safety net: an unknown /api/... must return
    a JSON 404, not the HTML shell (otherwise fetch consumers break silently)."""
    if _path.startswith('api/'):
        abort(404)
    if current_app.config.get('HEADLESS'):
        return jsonify(error="headless", hint="API-only profile"), 404
    return render_template('shell.html', ui_lang=ui_lang_setting())


@system_bp.route('/favicon.ico')
def favicon():
    """Favicon from the static/ folder — avoids a 404 on a default GET."""
    from flask import send_from_directory
    return send_from_directory(
        os.path.join(current_app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/x-icon',
    )


@system_bp.route('/sw.js')
def service_worker():
    """Service worker — must be served from the root so the scope is '/'.
    If served from /static/sw.js, the scope is limited to '/static/*'.
    Alternative: the Service-Worker-Allowed: / header, but a direct route is cleaner.
    """
    if current_app.config.get('HEADLESS'):
        return jsonify(error="headless", hint="API-only profile"), 404
    from flask import send_from_directory
    resp = send_from_directory(
        os.path.join(current_app.root_path, 'static'),
        'sw.js',
        mimetype='application/javascript',
    )
    # Do not cache the SW itself — so that SW code updates arrive quickly.
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@system_bp.route('/api/health', methods=['GET'])
def health_check():
    """System state."""
    health = {
        "status": "ok",
        "version": APP_VERSION,
        "ffmpeg_available": state.ffmpeg_available,
        "gpu_available": torch.cuda.is_available(),
        "database": "ok",
    }
    if _check_database_ready(current_app.config.get('DATABASE')) != "ok":
        health["database"] = "error"
        health["status"] = "degraded"
    if not state.ffmpeg_available:
        health["status"] = "degraded"
    return jsonify(health)


def _check_database_ready(db_path):
    """Робота check without side-effect creation of the DB file.

    Unlike the old path via `get_db_connection` (which auto-creates an
    empty file on `sqlite3.connect` by default), here we open the
    EXISTING file in `mode=rw` — otherwise the 'error' state would be unreachable
    on the first request against an auto-created empty DB (report S3, finding 7).

    A DB is considered usable only if it has a `schema_versions` table and
    `MAX(version) >= 1` (report S3, finding 7 — an empty file without a schema used to
    give 'ok' as well). `/api/health` calls the same helper (Round 3, story 03).
    Returns 'ok'/'error'; the reason goes to the log only (T7.4), it does not leak to the client.
    """
    if not db_path or db_path == ':memory:':
        logger.warning("/api/ready: DATABASE config is missing or ':memory:'")
        return "error"
    try:
        uri = pathlib.Path(db_path).absolute().as_uri() + '?mode=rw'
        conn = sqlite3.connect(uri, uri=True)
    except Exception as e:
        # T7.4: log with the reason, the client gets only a generic status (no str(exc)).
        logger.warning(f"/api/ready: DB check failed ({db_path}): {e}")
        return "error"
    try:
        row = conn.execute('SELECT MAX(version) FROM schema_versions').fetchone()
        version = row[0] if row and row[0] is not None else 0
        if version < 1:
            logger.warning(f"/api/ready: DB without a schema ({db_path})")
            return "error"
        return "ok"
    except Exception as e:
        logger.warning(f"/api/ready: DB schema check failed ({db_path}): {e}")
        return "error"
    finally:
        conn.close()


@system_bp.route('/api/ready', methods=['GET'])
def ready_check():
    """Робота to serve traffic (T03 config-registry-profiles).

    Unlike /api/health (a static "ok, alive"), this checks
    the actual робота of components by the `state.робота` state (written
    in app.py by startup phases) + a live SELECT 1 on every call — that piece
    overrides the checks.database field, the other fields come from the state snapshot.

    `migrations` is deliberately not part of the contract (робота-contract-03):
    this key is never in the 'error' state — an exception from `app/db/migrations.py`
    kills boot before робота gets to read the result.
    """
    робота = getattr(state, 'робота', None)
    if робота is None:
        return jsonify({
            "ready": False,
            "profile": current_app.config.get('PROFILE', 'desktop'),
            "version": APP_VERSION,
            "checks": {},
            "error": "ready-state not initialised",
        }), 503

    checks = dict(робота)
    checks["database"] = _check_database_ready(current_app.config.get('DATABASE'))

    job_queue = checks.get("job_queue") or {}
    whisper = checks.get("whisper") or {}
    ready = (
        checks.get("database") == "ok"
        and bool(job_queue.get("bound"))
        and whisper.get("preload") in ("ready", "disabled")
        and bool(checks.get("boot_finished"))
    )
    return jsonify({
        "ready": ready,
        "profile": current_app.config.get('PROFILE', 'desktop'),
        "version": APP_VERSION,
        "checks": checks,
    }), (200 if ready else 503)


@system_bp.route('/api/models', methods=['GET'])
def get_models():
    """List of Whisper models."""
    return jsonify(state.whisper_manager.get_available_models())


@system_bp.route('/api/download_model', methods=['POST'])
def download_model():
    """Download a model."""
    data = request.json
    model_name = data.get('model_name')
    if not model_name:
        return jsonify({"success": False, "error": tr("core.system.model_missing")}), 400
    if state.whisper_manager.download_model(model_name):
        return jsonify({"success": True, "message": tr("core.system.model_downloaded", model=model_name)})
    return jsonify({"success": False, "error": tr("core.system.model_download_failed")}), 500


@system_bp.route('/api/models/update-status', methods=['GET'])
def models_update_status():
    """Cached faster-whisper update status (no network). Filled in
    by a background startup check (once per ~2 weeks). The header shows a line if there is
    a newer version (= possible new models)."""
    from app.services import model_updates
    path = current_app.config.get('MODEL_UPDATE_STATE', 'model_update_state.json')
    return jsonify(model_updates.read_status(path))


@system_bp.route('/api/models/check-updates', methods=['POST'])
def models_check_updates():
    """Force a PyPI check now (the "check" button in Settings)."""
    from app.services import model_updates
    path = current_app.config.get('MODEL_UPDATE_STATE', 'model_update_state.json')
    return jsonify(model_updates.check_now(path))


@system_bp.route('/api/system_info', methods=['GET'])
def system_info():
    """System information."""
    return jsonify(state.whisper_manager.get_system_info())


# ---- T8.1 (Wave 4): diagnostic package ------------------------------
# The simple variant from REMEDIATION_PLAN (no Sentry): one zip with the latest
# lines of the file logs of three processes (app.py/telegram_listener.py/
# mcp_server.py — see app/core/logger.py, telegram_listener.py:43-70,
# mcp_server.py:41-74) + system_info + version/commit, for manual sending
# with a complaint. NEVER includes transcripts/DB/.env — only a short
# log tail, passed through the redaction below.
DIAGNOSTIC_LOG_TAIL_LINES = 500

# arcname in the zip -> path relative to the project root (current_app.root_path).
# whisper_ui.log is configurable (config.LOG_FILE), the other two are fixed
# names of separate processes (deliberately different files, to avoid WinError 32
# on simultaneous rollover from several processes — see the comments there).
_DIAGNOSTIC_LOG_SOURCES = (
    ("whisper_ui.log", None),  # None -> read from current_app.config['LOG_FILE']
    ("telegram_listener.log", "telegram_listener.log"),
    ("mcp_server.log", "mcp_server.log"),
)

# Values that look like keys/tokens/hashes — masked before packing.
# 1) "bare" Anthropic keys (sk-ant-...), even when they occur outside a
#    name=value pair (e.g. in an SDK error text).
_RE_BARE_ANTHROPIC_KEY = re.compile(r'sk-ant-[A-Za-z0-9_\-]{10,}')
# 2) NAME=value / NAME: value pairs where NAME looks like a secret:
#    *_KEY, *_TOKEN, *_HASH, *_SECRET, *_PASSWORD, as well as api_id/api_hash
#    (TELEGRAM_API_ID, TELEGRAM_API_HASH, ANTHROPIC_API_KEY, SECRET_KEY, …).
_RE_SECRET_ASSIGNMENT = re.compile(
    r'(?i)\b([A-Za-z][\w.]*(?:api[_-]?key|api[_-]?hash|api[_-]?id|secret|token|'
    r'password|passwd|_key|_hash)[\w.]*)\s*([:=])\s*(\S+)'
)


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
    except Exception:
        pass
    return None


@system_bp.route('/api/system/diagnostic-package', methods=['GET'])
def diagnostic_package():
    """Zip for manual sending with a complaint: log tails (redacted) +
    system_info + version/commit + manifest. A button in Settings (T8.1)."""
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

    buf.seek(0)
    filename = f"recall-diagnostic-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    resp = Response(buf.getvalue(), mimetype='application/zip')
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@system_bp.route('/api/system_stats', methods=['GET'])
def get_system_stats():
    """Snapshot CPU/GPU/RAM (Phase 2: non-blocking, reads from SystemMonitor)."""
    try:
        return jsonify(state.system_monitor.get_snapshot())
    except Exception as e:
        logger.error(f"Error getting statistics: {e}")
        return jsonify({"success": False, "error": tr("core.system.stats_failed")}), 500


@system_bp.route('/api/polish/availability', methods=['GET'])
def polish_availability():
    """Claude polish availability + list of models (Phase 4.4+)."""
    return jsonify({
        "available": text_polishing.is_available(),
        "default_model": get_default_model(),
        "models": [
            {
                "id": "claude-opus-4-8",
                "label": "Opus 4.8",
                "tier": "premium",
                "speed": tr('settings.polish.slow'),
                "cost_hint": tr('settings.polish.cost_opus'),
                "note": tr('settings.polish.note_opus'),
            },
            {
                "id": "claude-sonnet-4-6",
                "label": "Sonnet 4.6",
                "tier": "balanced",
                "speed": tr('settings.polish.medium'),
                "cost_hint": tr('settings.polish.cost_sonnet'),
                "note": tr('settings.polish.note_sonnet'),
            },
            {
                "id": "claude-haiku-4-5",
                "label": "Haiku 4.5",
                "tier": "fast",
                "speed": tr('settings.polish.fast'),
                "cost_hint": tr('settings.polish.cost_haiku'),
                "note": tr('settings.polish.note_haiku'),
            },
        ],
    })


@system_bp.route('/api/metrics', methods=['GET'])
def get_metrics():
    """Prometheus-compatible metrics endpoint (Phase 6.3)."""
    active_jobs = len(state.job_queue.list(active_only=True))
    snap = state.system_monitor.get_snapshot()
    gauges = {
        "whisper_active_jobs": active_jobs,
        "whisper_gpu_memory_gb": snap.get("gpu_memory_used_gb", 0) or 0,
        "whisper_cpu_percent": snap.get("cpu_percent", 0) or 0,
    }
    body = state.metrics.render(gauges=gauges)
    return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")
