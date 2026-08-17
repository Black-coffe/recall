"""SSE events + Process logs + Job queue endpoints (Phase 5.9).

- GET  /api/process/logs/<process_id>          (polling fallback)
- GET  /api/events/<process_id>                (SSE realtime)
- GET  /api/jobs                                (list)
- GET  /api/jobs/<job_id>                       (one)
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
    """Логи процесу (polling fallback для старого фронта)."""
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
    """SSE-канал на процес. Події: progress, log, segment, complete, error."""
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
    """Список задач. ?kind=youtube_download&active_only=true"""
    kind = request.args.get('kind')
    active_only = request.args.get('active_only', 'false').lower() == 'true'
    jobs = state.job_queue.list(kind=kind, active_only=active_only)
    return jsonify({"jobs": [j.to_dict() for j in jobs]})


@events_bp.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    job = state.job_queue.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    return jsonify(job.to_dict())


@events_bp.route('/api/jobs/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    ok = state.job_queue.cancel(job_id)
    if not ok:
        return jsonify({"success": False, "error": "Job cannot be cancelled (not found or already finished)"}), 400
    return jsonify({"success": True, "message": "Cancellation requested"})
