"""Re-transcribe a record with another Whisper model as a ``job_queue`` job (ADR-044).

``submit`` validates and queues; the job transcribes (and diarizes when it applies), then ends in
one transaction: write_back -> insert_version -> activate -> evict -> commit, and only after the
commit schedules the record re-embed. A failure anywhere leaves no version row and no pointer change.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from app import state
from app.db.connection import get_db_connection
from app.services import transcript_versions as tv
from app.services import vocabulary
from app.services.recording.state import recording_active

logger = logging.getLogger(__name__)

_inflight: dict = {}
_inflight_lock = threading.Lock()


class RetranscribeError(Exception):
    """A refusal or a job failure; ``code`` is an ``error_codes`` key, ``extra`` rides to the route."""

    def __init__(self, code: str, **extra):
        super().__init__(code)
        self.code = code
        self.error_code = code
        self.extra = extra


# error code -> (HTTP status, server text key); the route and the approval gate answer with these.
ERRORS = {
    'NO_MEDIA': (404, 'transcript.err.no_media'),
    'MODEL_NOT_INSTALLED': (400, 'transcript.err.model_not_installed'),
    'RECORDING_ACTIVE': (409, 'transcript.err.retranscribe_recording'),
    'ALREADY_RUNNING': (409, 'transcript.err.retranscribe_running'),
    'NOT_FOUND': (404, 'transcript.err.tx_not_found'),
    'UNAVAILABLE': (503, 'transcript.err.queue_unavailable'),
    'EVICTION_NOT_APPROVED': (409, 'transcript.err.eviction_not_approved'),
}


def error_body(code: str, **extra) -> tuple[int, dict]:
    """The refusal a route sends for ``code``: (status, JSON body)."""
    from app.core.i18n import tr
    status, key = ERRORS[code]
    return status, {'success': False, 'error': tr(key), 'error_code': code, **extra}


def running_job_id(tid: int) -> Optional[str]:
    with _inflight_lock:
        return _inflight.get(int(tid))


def _installed(model: str) -> bool:
    mgr = getattr(state, 'whisper_manager', None)
    if mgr is None:
        return False
    try:
        models = mgr.get_available_models()
    except Exception:
        logger.warning('model list unavailable for re-transcribe', exc_info=True)
        return False
    return any(m.get('name') == model and m.get('downloaded') for m in models)


def _record(db_path: str, tid: int):
    with get_db_connection(db_path) as conn:
        row = conn.execute('SELECT id, source_type, file_path, language, segments '
                           'FROM transcriptions WHERE id = ? AND deleted_at IS NULL',
                           (tid,)).fetchone()
    return dict(row) if row else None


def _has_media(path) -> bool:
    return bool(path) and os.path.isfile(path)


def precheck(tid: int, model, *, db_path: str) -> None:
    """Refuse a request ``submit`` would refuse, raising its ``RetranscribeError``; writes nothing."""
    tid = int(tid)
    if state.job_queue is None:
        raise RetranscribeError('UNAVAILABLE')
    rec = _record(db_path, tid)
    if rec is None:
        raise RetranscribeError('NOT_FOUND')
    if not _has_media(rec['file_path']):
        raise RetranscribeError('NO_MEDIA')
    if not model or not _installed(model):
        raise RetranscribeError('MODEL_NOT_INSTALLED')
    if recording_active():
        raise RetranscribeError('RECORDING_ACTIVE')
    live = running_job_id(tid)
    if live:
        raise RetranscribeError('ALREADY_RUNNING', job_id=live)


def would_evict(tid: int, *, db_path: str) -> bool:
    """True when a re-run of ``tid`` now would drop a kept version (ADR-044 retention)."""
    with get_db_connection(db_path) as conn:
        return tv.evict_candidate(conn, int(tid)) is not None


def submit(tid: int, *, model: str, language: Optional[str] = None,
           diarize: Optional[bool] = None, db_path: str, allow_evict: bool = True) -> dict:
    """Queue the re-run. ``allow_evict=False``: the job fails with ``EVICTION_NOT_APPROVED``
    (nothing written) if finishing it would drop a version."""
    tid = int(tid)
    precheck(tid, model, db_path=db_path)
    with _inflight_lock:
        live = _inflight.get(tid)
        if live:
            raise RetranscribeError('ALREADY_RUNNING', job_id=live)
        job = state.job_queue.submit(
            'retranscribe', _run, tid, model, language, diarize, db_path, bool(allow_evict),
            meta={'transcription_id': tid, 'model': model, 'stage': 'queued', 'progress': 0})
        _inflight[tid] = job.id
    return {'job_id': job.id, 'transcription_id': tid, 'model': model}


def _stage(job, stage: str, progress: int) -> None:
    job.meta['stage'] = stage
    job.meta['progress'] = progress


def _speaker_map(conn, labels, embeddings) -> list:
    """Raw labels -> speaker ids: 'self' is the owner, the rest by voice fingerprint."""
    from app.services.diarization_service import find_matching_speaker
    self_row = conn.execute('SELECT id FROM speakers WHERE is_self = 1 LIMIT 1').fetchone()
    saved = [(r[0], r[1]) for r in conn.execute(
        'SELECT id, embedding FROM speakers WHERE embedding IS NOT NULL AND is_self = 0')]
    out = []
    for label in labels:
        emb = embeddings.get(label)
        sid = None
        if label == 'self':
            sid = self_row[0] if self_row else None
        elif emb:
            match = find_matching_speaker(emb, saved)
            if match:
                sid = match[0]
        out.append({'raw_label': label, 'speaker_id': sid,
                    'embedding': [float(x) for x in emb] if emb else None})
    return out


def _run(job, tid: int, model: str, language, diarize, db_path: str,
         allow_evict: bool = True) -> dict:
    try:
        if recording_active():
            raise RetranscribeError('RECORDING_ACTIVE')
        rec = _record(db_path, tid)
        if rec is None:
            raise RetranscribeError('NOT_FOUND')
        if not _has_media(rec['file_path']):
            raise RetranscribeError('NO_MEDIA')
        filepath = rec['file_path']
        language = language or rec['language']
        source_type = rec['source_type']
        try:
            main_segments = json.loads(rec['segments']) if rec['segments'] else []
        except ValueError:
            main_segments = []
        speakers_before = any(isinstance(s, dict) and s.get('speaker') for s in main_segments)

        _stage(job, 'transcribing', 1)

        def _on_progress(info):
            try:
                pct = int(float((info or {}).get('progress', 0)) * 0.7)
            except (TypeError, ValueError, AttributeError):
                return
            job.meta['progress'] = max(1, min(70, pct))

        kind = source_type if source_type in ('youtube', 'recording') else 'file'
        t0 = time.time()
        result = state.whisper_manager.transcribe_with_progress(
            filepath, model, language, progress_callback=_on_progress,
            decode_options=vocabulary.decode_options_for(kind, db_path))
        if 'error' in result:
            raise RuntimeError(f"transcription failed: {result['error']}")
        processing_time = round(time.time() - t0, 2)
        segments = result['segments']

        labels, embeddings = [], {}
        if source_type == 'recording' or diarize is True or speakers_before:
            from app.services import diarization_service as ds
            if ds.DiarizationService.get_instance().is_available():
                _stage(job, 'diarizing', 72)
                segments, labels, _t, embeddings = ds.run_diarization_for_audio(
                    filepath, segments, source_type=source_type, return_embeddings=True)

        _stage(job, 'saving', 90)
        with get_db_connection(db_path) as conn:
            try:
                previous_main = tv.write_back(conn, tid)
                vid = tv.insert_version(
                    conn, tid, model_used=model, language=language,
                    processing_time=processing_time, transcript_text=result['text'],
                    segments=segments, speaker_map=_speaker_map(conn, labels, embeddings))
                tv.activate(conn, tid, vid)
                # The main version the owner had before this run is never the one dropped.
                evicted = tv.evict(conn, tid, protect=(previous_main,))
                if evicted and not allow_evict:
                    # Dropping a version needs the owner's approval; the rollback below
                    # leaves no version row and no pointer change.
                    raise RetranscribeError('EVICTION_NOT_APPROVED')
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        from app.services import reembed
        status = reembed.schedule_record_reembed(tid, db_path=db_path)
        _stage(job, 'done', 100)
        return {'version_id': vid, 'evicted_version_ids': evicted, 'model': model,
                'segment_count': len(segments), 'reembed': status}
    finally:
        with _inflight_lock:
            if _inflight.get(tid) == job.id:
                del _inflight[tid]
