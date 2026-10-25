"""SSE events + Process logs + Job queue endpoints (Phase 5.9).

- GET  /api/process/logs/<process_id>          (polling fallback)
- GET  /api/events/<process_id>                (SSE realtime)
- GET  /api/jobs                                (list)
- GET  /api/jobs/<job_id>                       (one; ?wait=<0..25>&include_log=1&tail=<1..200>)
- POST /api/jobs/<job_id>/cancel                (cancel)
"""
import logging
import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

from app import state


logger = logging.getLogger(__name__)
events_bp = Blueprint('events', __name__)


@events_bp.route('/api/process/logs/<process_id>')
def get_process_logs_api(process_id):
    """Process logs (polling fallback for the old frontend)."""
    logs = state.process_logs.get(process_id)
    for i, log in enumerate(logs):
        if log.get('elapsed') is not None and i > 0:
            start_time = log['elapsed']
            elapsed = time.time() - start_time
            log['elapsed_seconds'] = round(elapsed, 1)
            log['elapsed_formatted'] = f"{int(elapsed // 60)}:{int(elapsed % 60):02d}"
    return jsonify({
        "process_id": process_id,
        "logs": logs,
        "total_entries": len(logs),
    })


@events_bp.route('/api/events/<process_id>')
def stream_events(process_id):
    """Per-process SSE channel. Events: progress, log, segment, complete, error."""
    history = []
    progress = state.download_progress.get(process_id)
    if progress:
        history.append({"event": "progress", "data": progress, "ts": time.time()})
    for log in state.process_logs.get(process_id):
        history.append({"event": "log", "data": log, "ts": time.time()})

    response = Response(
        stream_with_context(state.sse_broker.stream_events(process_id, history=history)),
        mimetype='text/event-stream',
        direct_passthrough=True,
    )
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response


@events_bp.route('/api/jobs', methods=['GET'])
def list_jobs():
    """Task list. ?kind=youtube_download&active_only=true"""
    kind = request.args.get('kind')
    active_only = request.args.get('active_only', 'false').lower() in ('true', '1')
    jobs = state.job_queue.list(kind=kind, active_only=active_only)
    return jsonify({"jobs": [j.to_dict() for j in jobs]})


_WAIT_MAX_S = 25.0
_WAIT_STEP_S = 0.25
_TAIL_DEFAULT = 50
_TAIL_MAX = 200


def _arg_number(name, default, lo, hi, cast=float):
    """A query number clamped to lo..hi; the default when absent or not a number."""
    try:
        value = cast(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


@events_bp.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    """One job. ``wait`` polls until a terminal state or the timeout; ``include_log`` attaches
    the last ``tail`` entries of the job's process log (``[]`` when the kind never logged)."""
    from app.services.job_queue import FINISHED_STATES
    job = state.job_queue.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    wait = _arg_number('wait', 0.0, 0.0, _WAIT_MAX_S)
    deadline = time.monotonic() + wait
    while job.state not in FINISHED_STATES and time.monotonic() < deadline:
        time.sleep(min(_WAIT_STEP_S, max(0.0, deadline - time.monotonic())))
    body = job.to_dict()
    if request.args.get('include_log', '').lower() in ('1', 'true'):
        tail = _arg_number('tail', _TAIL_DEFAULT, 1, _TAIL_MAX, cast=int)
        logs = state.process_logs.get(job_id) if state.process_logs is not None else None
        body["log"] = list(logs or [])[-tail:]
    return jsonify(body)


@events_bp.route('/api/jobs/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    ok = state.job_queue.cancel(job_id)
    if not ok:
        body = {"success": False, "error": "Job cannot be cancelled (not found or already finished)"}
        if state.job_queue.get(job_id) is not None:
            body["cancel_ignored"] = True  # the job exists but already finished
        return jsonify(body), 400
    return jsonify({"success": True, "message": "Cancellation requested"})
