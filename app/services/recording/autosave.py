"""Autosave of a stopped recording (spec recording-autosave).

A recording stopped from anywhere but its own Stop button (the Windows notification, MCP, the K2
autostop, another tab) used to end up in the library untranscribed, untitled and unfiled. Now
finalize schedules a deadline (``RECORDING_AUTOSAVE_SEC`` after it). The save screen shows a
countdown to it; editing holds it, ``/save`` and ``/discard`` cancel it. When it fires and nobody
took the recording, the server runs the same HTTP write path the save screen runs:

1. ``POST /api/transcribe`` source_type=library (model/language from settings);
2. a title from ``POST /api/history/<id>/suggest-meta`` -> ``PATCH /api/history/<id>``, only when
   the user gave no name (the local model is not called during a recording: the endpoint answers
   409 RECORDING_ACTIVE and we wait);
3. the direction from ``GET .../suggest-category`` -> ``PATCH .../category``, only at
   confidence >= ``RECORDING_AUTOSAVE_MIN_CONFIDENCE``; otherwise it stays NULL and the Audio
   library asks the user.

The state lives in the manifest (``manifest['autosave']``) so a restart reschedules what was
pending. Loopback HTTP instead of calling the views: the same write path as the UI (lesson
``dry-run-must-be-same-write-path``), with its duplicate guard and enrichment.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Optional

from app.core import auth
from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

HOLD_SEC = 600            # one hold from the save screen postpones the deadline by 10 min
WAIT_STEP_SEC = 30        # polling step while waiting for enrichment/embeddings/an idle recorder
WAIT_ENRICH_SEC = 900     # how long to wait for the summary and the embeddings
WAIT_IDLE_SEC = 4 * 3600  # how long to wait for a running recording to end before titling
WAIT_TRANSCRIPT_SEC = 2 * 3600  # after a tab save: how long to wait for the tab's transcript

PENDING_STATES = ('pending', 'held')
OPEN_STATES = PENDING_STATES + ('running',)


class AutosaveScheduler:
    """One timer per stopped recording; the truth is ``manifest['autosave']``."""

    def __init__(self, store, delay_sec: int, run_fn: Callable[[str], str], *,
                 hold_sec: int = HOLD_SEC, clock: Callable[[], float] = time.time,
                 timer_factory: Callable[..., Any] = threading.Timer,
                 after_save_fn: Optional[Callable[[str], str]] = None):
        self._store = store
        self.delay_sec = max(0, int(delay_sec))
        self._run_fn = run_fn
        self._after_save_fn = after_save_fn
        self._hold_sec = hold_sec
        self._clock = clock
        self._timer_factory = timer_factory
        self._timers: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.delay_sec > 0

    # ------------------------------------------------------------ state
    def info(self, sid: str) -> dict:
        """``{at, status, reason}`` for the UI; ``status='off'`` when the server autosave is disabled."""
        try:
            mf = self._store.read(sid)
        except Exception:
            logger.debug("autosave info: manifest of %s unreadable", sid, exc_info=True)
            return {'at': None, 'status': None}
        data = mf.get('autosave') or {}
        if not data and not self.enabled:
            return {'at': None, 'status': 'off'}
        return {'at': data.get('at'), 'status': data.get('status'), 'reason': data.get('reason')}

    def _write(self, sid: str, **fields) -> dict:
        def _m(mf: dict) -> None:
            cur = dict(mf.get('autosave') or {})
            cur.update(fields)
            mf['autosave'] = cur
        self._store.modify(sid, _m)
        return self.info(sid)

    # ------------------------------------------------------------ control
    def schedule(self, sid: str, at: Optional[float] = None) -> dict:
        if not self.enabled:
            return {'at': None, 'status': 'off'}
        cur = self.info(sid)
        if cur.get('status') == 'cancelled':
            return cur   # /save got here first (it waits for the same finalize)
        at = float(at if at is not None else self._clock() + self.delay_sec)
        status = 'pending'
        if cur.get('status') == 'held':
            # The save screen was edited before finalize got here: keep that hold.
            at, status = max(at, float(cur.get('at') or 0)), 'held'
        info = self._write(sid, status=status, at=at)
        self._arm(sid, at)
        return info

    def hold(self, sid: str, fields: Optional[dict] = None) -> dict:
        """The user is editing the save screen: move the deadline out, never closer, and keep
        what they typed (``name``/``description``/``category_id``) so an autosave after a closed
        tab still carries it. Works before finalize scheduled anything (status None): the
        deadline is then written but armed only by ``schedule``."""
        cur = self.info(sid)
        if cur.get('status') not in PENDING_STATES + (None,):
            return cur
        at = max(float(cur.get('at') or 0), self._clock() + self._hold_sec)
        kept = {k: v for k, v in (fields or {}).items() if k in ('name', 'description', 'category_id')}
        if kept:
            def _m(mf: dict) -> None:
                a = dict(mf.get('autosave') or {})
                merged = {**(a.get('fields') or {}), **kept}
                a['fields'] = {k: v for k, v in merged.items() if v not in (None, '')}
                mf['autosave'] = a
            self._store.modify(sid, _m)
        info = self._write(sid, status='held', at=at)
        if cur.get('status') is not None:
            self._arm(sid, at)
        return info

    def cancel(self, sid: str, reason: str) -> dict:
        """``/save`` or ``/discard`` took the recording. A running job is left alone."""
        with self._lock:
            t = self._timers.pop(sid, None)
        if t is not None:
            t.cancel()
        cur = self.info(sid)
        if cur.get('status') in ('running', 'done', 'failed'):
            return cur
        try:
            return self._write(sid, status='cancelled', reason=reason)
        except Exception:
            logger.debug("autosave cancel: manifest of %s gone", sid, exc_info=True)
            return {'at': None, 'status': 'cancelled'}

    def after_save(self, sid: str) -> bool:
        """The tab saved the recording (and transcribes it itself): still file the direction.
        Runs ``after_save_fn`` once in the background; not persisted, a restart leaves it to the banner."""
        if not self.enabled or self._after_save_fn is None:
            return False
        t = self._timer_factory(0, self._run_after_save, args=(sid,))
        t.daemon = True
        t.start()
        return True

    def _run_after_save(self, sid: str) -> None:
        try:
            outcome = self._after_save_fn(sid)
        except Exception as e:
            logger.exception("autosave after save %s crashed: %s", sid, e)
            outcome = 'failed'
        try:
            self._write(sid, direction=outcome)
        except Exception:
            logger.debug("autosave after save: manifest of %s gone", sid, exc_info=True)
        logger.info("autosave after save %s: %s", sid, outcome)

    def reschedule_pending(self) -> list[str]:
        """Boot: re-arm what a restart dropped (only recordings that had a deadline)."""
        if not self.enabled:
            return []
        out = []
        for mf in self._store.list_all():
            sid = mf.get('session_id')
            data = mf.get('autosave') or {}
            if not sid or data.get('status') not in OPEN_STATES:
                continue
            at = max(float(data.get('at') or 0), self._clock() + self.delay_sec)
            self._write(sid, status='pending', at=at)
            self._arm(sid, at)
            out.append(sid)
        return out

    # ------------------------------------------------------------ timer
    def _arm(self, sid: str, at: float) -> None:
        delay = max(0.0, at - self._clock())
        t = self._timer_factory(delay, self._fire, args=(sid,))
        t.daemon = True
        with self._lock:
            old = self._timers.pop(sid, None)
            self._timers[sid] = t
        if old is not None:
            old.cancel()
        t.start()

    def _fire(self, sid: str) -> None:
        with self._lock:
            self._timers.pop(sid, None)
        cur = self.info(sid)
        if cur.get('status') not in PENDING_STATES:
            return
        if float(cur.get('at') or 0) > self._clock() + 1:
            self._arm(sid, float(cur['at']))   # a hold moved the deadline after we were armed
            return
        self._write(sid, status='running')
        try:
            outcome = self._run_fn(sid)
        except Exception as e:
            logger.exception("autosave %s crashed: %s", sid, e)
            self._write(sid, status='failed')
            return
        self._write(sid, status='done', outcome=outcome)
        logger.info("autosave %s: %s", sid, outcome)


# ------------------------------------------------------------------ the job

def http_json(method: str, url: str, *, data: Optional[dict] = None, form: Optional[dict] = None,
              timeout: float = 60) -> tuple[int, dict]:
    """A loopback call; returns (status, json body). Never raises on an HTTP error status.

    Carries the per-boot UI token: this is Recall's own server acting for the owner, not an agent,
    so its writes are not refused or stamped as MCP ones."""
    headers = {auth.UI_TOKEN_HEADER: auth.ui_token()}
    body = None
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    elif form is not None:
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in form.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
        parts.append(f'--{boundary}--\r\n')
        body = ''.join(parts).encode('utf-8')
        headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode('utf-8') or '{}')
        except ValueError:
            payload = {}
        return e.code, payload


class AutosaveJob:
    """What the timer runs. Every dependency is injectable for the offline test."""

    def __init__(self, *, base_url: str, db_path: str, store, model: str, language: str,
                 min_confidence: float, is_recording: Callable[[], bool],
                 is_transcribing: Callable[[int], bool] = lambda _id: False,
                 http: Callable[..., tuple[int, dict]] = http_json,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.base = base_url.rstrip('/')
        self.db_path = db_path
        self.store = store
        self.model = model
        self.language = language
        self.min_confidence = float(min_confidence)
        self.is_recording = is_recording
        self.is_transcribing = is_transcribing
        self.http = http
        self.sleep = sleep
        self.clock = clock

    def __call__(self, sid: str) -> str:
        row = self._library_row(sid)
        if row is None:
            return 'no_library_row'
        if self.is_transcribing(row['id']) or self._has_transcript(row['file_path']):
            return 'skipped_already_transcribed'

        manifest = self.store.read(sid)
        typed = (manifest.get('autosave') or {}).get('fields') or {}
        name = (typed.get('name') or manifest.get('name') or '').strip()
        form = {'source_type': 'library', 'audio_download_id': str(row['id']),
                'model': self.model, 'language': self.language}
        # What the user typed on the save screen before the tab went away wins over any guess.
        if name:
            form['title'] = name
        if (typed.get('description') or '').strip():
            form['description'] = typed['description'].strip()
        if typed.get('category_id'):
            form['category_id'] = str(typed['category_id'])
        status, res = self.http('POST', f'{self.base}/api/transcribe', timeout=4 * 3600, form=form)
        tid = res.get('transcription_id') if status == 200 else None
        if not tid:
            logger.warning("autosave %s: transcribe returned %s %s", sid, status, res.get('error_code'))
            return f'transcribe_failed_{status}'

        titled = self._title(tid) if not name else 'user_name'
        filed = self._direction(tid)
        return f'transcribed_{tid}_{titled}_{filed}'

    def file_saved(self, sid: str) -> str:
        """Direction only, for a recording the tab saved: wait for the tab's transcript, then the
        same direction step as the autosave. Never transcribes: no transcript means the tab chose
        not to."""
        row = self._library_row(sid)
        if row is None:
            return 'no_library_row'
        tid = None

        def _found() -> bool:
            nonlocal tid
            tid = self._transcript_id(row['file_path'])
            return tid is not None

        if not self._wait(_found, WAIT_TRANSCRIPT_SEC):
            return 'no_transcript'
        return f'saved_{tid}_{self._direction(tid)}'

    # -------------------------------------------------------------- pieces
    def _library_row(self, sid: str):
        with get_db_connection(self.db_path) as conn:
            return conn.execute(
                'SELECT id, file_path FROM audio_downloads '
                'WHERE recording_session_id = ? OR youtube_id = ? LIMIT 1',
                (sid, f'recording_{sid}'),
            ).fetchone()

    def _has_transcript(self, file_path: Optional[str]) -> bool:
        return self._transcript_id(file_path) is not None

    def _transcript_id(self, file_path: Optional[str]) -> Optional[int]:
        if not file_path:
            return None
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                'SELECT id FROM transcriptions WHERE file_path = ? AND deleted_at IS NULL '
                'ORDER BY id DESC LIMIT 1',
                (file_path,),
            ).fetchone()
        return row['id'] if row is not None else None

    def _row(self, tid: int):
        with get_db_connection(self.db_path) as conn:
            return conn.execute(
                'SELECT title, category_id, summary_json FROM transcriptions WHERE id = ?', (tid,),
            ).fetchone()

    def _field(self, tid: int, name: str):
        row = self._row(tid)
        return row[name] if row is not None else None

    def _wait(self, ready: Callable[[], bool], limit: float) -> bool:
        deadline = self.clock() + limit
        while True:
            if ready():
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(WAIT_STEP_SEC)

    def _title(self, tid: int) -> str:
        initial = self._field(tid, 'title')   # may be the library's date placeholder
        # A summary makes the variants specific (meta_suggest docstring); fragments still work.
        self._wait(lambda: bool(self._field(tid, 'summary_json')), WAIT_ENRICH_SEC)
        if not self._wait(lambda: not self.is_recording(), WAIT_IDLE_SEC):
            return 'title_skipped_recording'
        for _ in range(3):
            status, res = self.http('POST', f'{self.base}/api/history/{tid}/suggest-meta', timeout=300)
            if status == 409 and res.get('error_code') == 'RECORDING_ACTIVE':
                if not self._wait(lambda: not self.is_recording(), WAIT_IDLE_SEC):
                    return 'title_skipped_recording'
                continue
            titles = (res.get('titles') or []) if status == 200 else []
            if not titles:
                return f'title_none_{status}'
            if self._field(tid, 'title') != initial:
                return 'title_set_meanwhile'   # the user named it while we waited
            st, _ = self.http('PATCH', f'{self.base}/api/history/{tid}', data={'title': titles[0]})
            return 'titled' if st == 200 else f'title_patch_{st}'
        return 'title_gave_up'

    def _direction(self, tid: int) -> str:
        if self._field(tid, 'category_id') is not None:
            return 'direction_given'
        res: dict = {}

        def _embedded() -> bool:
            nonlocal res
            _st, res = self.http('GET', f'{self.base}/api/memory/transcriptions/{tid}/suggest-category')
            return res.get('reason') != 'not_embedded'

        self._wait(_embedded, WAIT_ENRICH_SEC)
        sug = res.get('suggestion') or {}
        conf = float(sug.get('confidence') or 0)
        if not sug.get('category_id') or conf < self.min_confidence:
            return 'direction_ask_user'
        if self._field(tid, 'category_id') is not None:
            return 'direction_set_meanwhile'
        st, _ = self.http('PATCH', f'{self.base}/api/memory/transcriptions/{tid}/category',
                          data={'category_id': sug['category_id']})
        return 'direction_set' if st == 200 else f'direction_patch_{st}'
