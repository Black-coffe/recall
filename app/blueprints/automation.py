"""Automation HTTP API (/api/automation/*): long-poll over the SSE broker event ring."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from app import state
from app.blueprints.telegram import _listener_call
from app.core import auth
from app.core.i18n import tr
from app.services import tg_bot
from app.services.pipes import STALE_RUNNING, cooldown_remaining, list_runs, load_pipes, run_pipe
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


_SECTIONS = ('calls', 'pipes', 'pollers', 'telegram')


def _age_sec(created_at, now_ts: float) -> int:
    """Whole seconds since an ISO timestamp from the watcher; 0 when it is absent."""
    if not created_at:
        return 0
    return max(0, int(now_ts - datetime.fromisoformat(created_at).timestamp()))


def _calls_section(now_ts: float) -> dict:
    watcher = current_app.extensions.get('call_watcher')
    if watcher is None:
        return {'enabled': False, 'apps': [], 'pending': []}
    pending = []
    for p in watcher.pending():
        # Explicit allowlist: prompt ids, mic endpoints and action URLs never leave the process.
        item = {'kind': p['kind'], 'exe': p['exe'], 'label': p['label'],
                'age_sec': _age_sec(p.get('created_at'), now_ts)}
        if p.get('session_id') is not None:
            item['session_id'] = p['session_id']
        pending.append(item)
    return {'enabled': True, 'apps': sorted(watcher.apps),
            'prompt_seconds': watcher.prompt_seconds,
            'autostop_minutes': watcher.autostop_sec / 60.0, 'pending': pending}


def _pipes_section(db_path: str, now: datetime) -> dict:
    out = []
    for pipe in load_pipes():
        last = list_runs(db_path, pipe=pipe.name, limit=1)
        last_run = None
        if last:
            r = last[0]
            last_run = {'id': r['id'], 'status': r['status'], 'trigger': r['trigger'], 'at': r['started_at']}
        out.append({'name': pipe.name, 'schedule': pipe.schedule.expr, 'tools': list(pipe.tools),
                    'budget_usd': pipe.budget_usd, 'skip_if_empty': pipe.skip_if_empty,
                    'last_run': last_run,
                    'cooldown_sec': cooldown_remaining(db_path, pipe.name, now=now)})
    return {'enabled': 'pipe_scheduler' in current_app.extensions, 'pipes': out}


def _pollers_section() -> dict:
    ext = current_app.extensions
    return {'scheduler': 'pipe_scheduler' in ext, 'brief_buttons': 'brief_buttons' in ext,
            'mail': 'mail_poller' in ext, 'calendar': 'calendar_poller' in ext}


def _telegram_section(db_path: str) -> dict:
    ok, _data = _listener_call('/status', timeout=5)
    with closing(sqlite3.connect(db_path)) as conn:
        monitored, enabled = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END), 0) "
            "FROM tg_monitored_chats").fetchone()
    return {'listener_online': bool(ok), 'monitored': monitored, 'enabled': enabled,
            'bot_configured': tg_bot.is_configured()}


@automation_bp.route('/api/automation/status', methods=['GET'])
def get_status():
    """Automation state in four sections; ``?sections=`` narrows them (default: all)."""
    raw = (request.args.get('sections') or '').strip()
    wanted = [s.strip() for s in raw.split(',') if s.strip()] if raw else list(_SECTIONS)
    if any(s not in _SECTIONS for s in wanted):
        return _err('INVALID_ARGUMENT', 'automation.err.invalidSection', 400)
    db_path = current_app.config['DATABASE']
    now = datetime.now()
    now_ts = time.time()
    builders = {
        'calls': lambda: _calls_section(now_ts),
        'pipes': lambda: _pipes_section(db_path, now),
        'pollers': _pollers_section,
        'telegram': lambda: _telegram_section(db_path),
    }
    return jsonify({'success': True,
                    'sections': {s: builders[s]() for s in dict.fromkeys(wanted)}})


def _fresh_running(db_path: str, name: str, now: datetime) -> bool:
    cutoff = now - STALE_RUNNING
    return any(r['status'] == 'running'
               and datetime.strptime(r['started_at'], "%Y-%m-%d %H:%M:%S") >= cutoff
               for r in list_runs(db_path, pipe=name, limit=5))


@automation_bp.route('/api/pipes/<name>/run', methods=['POST'])
def run_pipe_now(name: str):
    """Run a configured pipe to its fixed recipient as a ``pipe_run`` job, behind cooldown and lock."""
    db_path = current_app.config['DATABASE']
    pipe = next((p for p in load_pipes() if p.name == name), None)
    if pipe is None:
        return _err('NOT_FOUND', 'automation.err.pipeNotFound', 404)
    if 'pipe_scheduler' not in current_app.extensions:
        return _err('PIPES_DISABLED', 'automation.err.pipesDisabled', 409)
    if not tg_bot.is_configured():
        return _err('NOT_CONFIGURED', 'automation.err.notConfigured', 503)
    now = datetime.now()
    # A run in progress is ALREADY_RUNNING before COOLDOWN: a fresh `running` row also reads as cooldown.
    active = state.job_queue.find_active('pipe_run', pipe=name)
    if active is not None or _fresh_running(db_path, name, now):
        body = {'success': False, 'error': tr('automation.err.alreadyRunning'),
                'error_code': 'ALREADY_RUNNING'}
        if active is not None:
            body['job_id'] = str(active.id)
        return jsonify(body), 409
    left = cooldown_remaining(db_path, name, now=now)
    if left > 0:
        return jsonify({'success': False, 'error': tr('automation.err.cooldown'),
                        'error_code': 'COOLDOWN', 'retry_after_sec': left}), 429

    def _job(job, _pipe=pipe, _db=db_path):
        return run_pipe(_pipe, db_path=_db, deliver=tg_bot.send, buttons=True, trigger='mcp')

    job = state.job_queue.submit('pipe_run', _job, meta={
        'pipe': name, 'actor': auth.classify_request()[0], 'trigger': 'mcp'})
    return jsonify({'success': True, 'job_id': str(job.id), 'created': True,
                    'poll': 'get_job_status'}), 202


@automation_bp.route('/api/meeting-brief', methods=['GET'])
def get_meeting_brief():
    """Pre-meeting brief for ``event_id`` or ``person`` (exactly one), after the privacy filter."""
    from app.db.connection import get_db_connection
    from app.services import meeting_brief
    from app.services.privacy import PrivacyError

    raw_event = (request.args.get('event_id') or '').strip()
    person = (request.args.get('person') or '').strip()
    if bool(raw_event) == bool(person):
        return _err('INVALID_ARGUMENT', 'automation.err.meetingBriefArgs', 400)
    db_path = current_app.config['DATABASE']
    if raw_event:
        try:
            event_id = int(raw_event)
        except ValueError:
            return _err('INVALID_ARGUMENT', 'automation.err.meetingBriefArgs', 400)
        event = meeting_brief.get_event(db_path, event_id)
        if event is None:
            return jsonify({'success': False, 'error': tr('svc_tasks.err.eventNotFound', id=event_id),
                            'error_code': 'NOT_FOUND'}), 404
        with get_db_connection(db_path) as conn:
            brief = meeting_brief.brief_for_event(conn, event)
    else:
        with get_db_connection(db_path) as conn:
            brief = meeting_brief.brief_for_person(conn, person)
    try:
        brief = meeting_brief.filter_brief(brief, db_path=db_path)
    except PrivacyError:
        logger.warning("meeting brief: privacy check failed, nothing returned")
        return _err('UNAVAILABLE', 'telegram.brief.blockedReason.privateTxt', 503)
    return jsonify({'success': True, 'brief': brief})
