"""System & meta endpoints (Phase 5.5).

- /                          (index template)
- /api/health
- /api/models
- /api/download_model
- /api/models/download-job
- /api/system/diagnostic-package (+ /file)
- /api/system_info
- /api/system_stats
- /api/polish/availability
- /api/metrics
"""
import logging
import os
import pathlib
import sqlite3
from datetime import datetime

import torch
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

from app import state
from app.core import auth
from app.core.i18n import tr, ui_lang_setting
from app.services import text_polishing
from app.services.models import get_default_model


logger = logging.getLogger(__name__)
system_bp = Blueprint('system', __name__)

# Shared with /api/health — one version for the whole blueprint (T8.1).
APP_VERSION = "4.30.0"


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
    if auth.is_direct_loopback() and not auth.host_ok():
        return jsonify(success=False, error="host rejected", code="HOST_REJECTED"), 403
    token = auth.ui_token() if auth.is_local_trusted() else ""
    return render_template('shell.html', ui_lang=ui_lang_setting(), ui_token=token)


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


def _model_name_from_body():
    data = request.get_json(silent=True)
    name = data.get('model_name') if isinstance(data, dict) else None
    return name if isinstance(name, str) and name.strip() else None


@system_bp.route('/api/download_model', methods=['POST'])
def download_model():
    """Download a model: submits (or joins) the lock-free model_download job and waits for it,
    so loaded models keep serving; the answer keeps the synchronous shape."""
    from app.services import model_download
    model_name = _model_name_from_body()
    if not model_name:
        return jsonify({"success": False, "error": tr("core.system.model_missing")}), 400
    try:
        sub = model_download.submit(model_name, actor=auth.classify_request()[0])
    except model_download.ModelDownloadError as e:
        return jsonify({"success": False, "error": e.message, "error_code": e.code}), 400
    job = state.job_queue.get(sub["job_id"])
    if job is not None and job._future is not None:
        try:
            job._future.result()
        except Exception as e:  # the job already logged it; the answer below says failed
            logger.debug(f"Model download job {job.id} failed: {e}")
    if job is not None and job.state == "completed":
        return jsonify({"success": True, "message": tr("core.system.model_downloaded", model=model_name),
                        "job_id": job.id})
    return jsonify({"success": False, "error": tr("core.system.model_download_failed"),
                    "job_id": sub["job_id"]}), 500


@system_bp.route('/api/models/download-job', methods=['POST'])
def models_download_job():
    """Queue a Whisper model download in the background: 202 {success, job_id, created}."""
    from app.services import model_download
    model_name = _model_name_from_body()
    if not model_name:
        return jsonify({"success": False, "error": tr("core.system.model_missing"),
                        "error_code": "INVALID_ARGUMENT"}), 400
    try:
        sub = model_download.submit(model_name, actor=auth.classify_request()[0])
    except model_download.ModelDownloadError as e:
        return jsonify({"success": False, "error": e.message, "error_code": e.code}), 400
    return jsonify({"success": True, **sub}), 202


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
# Built by app/services/diagnostics.py; the constant stays importable from here.
from app.services.diagnostics import DIAGNOSTIC_LOG_TAIL_LINES  # noqa: E402,F401


@system_bp.route('/api/system/diagnostic-package', methods=['GET'])
def diagnostic_package():
    """Zip for manual sending with a complaint: log tails (redacted) +
    system_info + version/commit + manifest. A button in Settings (T8.1)."""
    from app.services import diagnostics
    filename = f"recall-diagnostic-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    resp = Response(diagnostics.build_package(), mimetype='application/zip')
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@system_bp.route('/api/system/diagnostic-package/file', methods=['POST'])
def diagnostic_package_file():
    """Write the package to logs/diagnostics/<ts>.zip (newest five kept); answers its path and size."""
    from app.services import diagnostics
    try:
        written = diagnostics.write_package()
    except OSError as e:
        logger.warning(f"Diagnostic package write failed: {e}")
        return jsonify({"success": False, "error": tr("core.system.diagnostic_write_failed"),
                        "error_code": "INTERNAL_ERROR"}), 500
    return jsonify({"success": True, **written})


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
