"""Automation HTTP API (/api/automation/*): long-poll over the SSE broker event ring."""
from __future__ import annotations

import logging
import threading
import time

from flask import Blueprint, jsonify, request

from app import state
from app.core.i18n import tr
from app.services.recording.service import SessionNotFoundError
from app.services.sse_broker import RING_PREFIXES, broker

logger = logging.getLogger(__name__)

automation_bp = Blueprint('automation', __name__)

MAX_TIMEOUT = 25.0
MAX_WAITERS = 4
_RECORDING_DEFAULT_EVENTS = {'status', 'live_segment', 'copilot_insight', 'source_changed', 'error'}
_PROVENANCE = {'live_segment': 'transcript', 'copilot_insight': 'copilot'}

_waiters = 0
_waiters_lock = threading.Lock()


def _err(code: str, key: str, status: int):
    return jsonify({'success': False, 'error': tr(key), 'error_code': code}), status


def _with_provenance(events: list) -> list:
    for e in events:
        e['provenance_kind'] = _PROVENANCE.get(e['event'], 'system')
    return events


@automation_bp.route('/api/automation/events', methods=['GET'])
def get_events():
    """Wait for ring events of a channel after a cursor (``after``), up to ``timeout`` seconds."""
    global _waiters
    channel = (request.args.get('channel') or '').strip()
    if not channel.startswith(RING_PREFIXES):
        return _err('INVALID_ARGUMENT', 'automation.err.invalidChannel', 400)
    after = request.args.get('after') or None
    raw_events = request.args.get('events')
    events = {e.strip() for e in raw_events.split(',') if e.strip()} if raw_events else None
    if events is None and channel.startswith('recording:'):
        events = set(_RECORDING_DEFAULT_EVENTS)
    try:
        timeout = float(request.args.get('timeout', MAX_TIMEOUT))
    except ValueError:
        timeout = MAX_TIMEOUT
    timeout = max(0.0, min(timeout, MAX_TIMEOUT))

    is_recording = channel.startswith('recording:')
    if is_recording:
        svc = state.recording_service
        if svc is None:
            return _err('NOT_FOUND', 'automation.err.notFound', 404)
        try:
            svc.get_state(channel[len('recording:'):])
        except SessionNotFoundError:
            return _err('NOT_FOUND', 'automation.err.notFound', 404)

    with _waiters_lock:
        if _waiters >= MAX_WAITERS:
            return _err('TOO_MANY_WAITERS', 'automation.err.tooManyWaiters', 429)
        _waiters += 1
    q = None
    try:
        q = broker.subscribe(channel, events=events)
        if after is None and is_recording:
            nxt = broker.cursor(channel)
            snapshot = state.recording_service.get_state(channel[len('recording:'):])
            return jsonify({'already': True, 'state': snapshot, 'next': nxt})

        deadline = time.monotonic() + timeout
        while True:
            res = broker.read_since(channel, after, events)
            if res['events']:
                res['events'] = _with_provenance(res['events'])
                res['timed_out'] = False
                return jsonify(res)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                res['timed_out'] = True
                return jsonify(res)
            try:
                q.get(timeout=remaining)
            except Exception:  # queue.Empty: re-read once more, the deadline check ends the loop
                logger.debug("event wait woke without an event", exc_info=False)
    finally:
        if q is not None:
            broker.unsubscribe(channel, q)
        with _waiters_lock:
            _waiters -= 1
