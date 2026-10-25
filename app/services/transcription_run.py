"""The transcription pipeline behind `POST /api/transcribe` and `POST /api/transcribe/async`.

``resolve`` turns a source (an uploaded file, an Audio Library row, a finalized recording, a
YouTube download or link, a local path inside `RECALL_IMPORT_ROOTS`) into
``(filepath, source_name, meta)``; ``run`` transcribes, diarizes, saves (dedup, speaker map),
counts metrics, queues enrichment and links a recording - the body the sync route ran inline.
The sync route calls both in the request thread; ``submit`` queues the same work as a
``transcription`` job (ADR-044 pattern: ``meta.stage``/``progress``, a typed error with
``.error_code``, dedupe on the source).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from werkzeug.utils import secure_filename

from app import state
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.services import dedup_audio, import_roots, vocabulary
from app.services.recording.state import recording_active
from app.utils.audio import extract_audio_from_video as _extract_audio_raw
from app.utils.files import allowed_file, is_video_file

logger = logging.getLogger(__name__)

ASYNC_SOURCES = ("library", "recording", "youtube", "path")

_inflight: dict = {}
_inflight_lock = threading.Lock()


class TranscribeError(Exception):
    """A refusal or a failure; ``code`` is an ``error_codes`` key, ``extra`` rides to the route.

    ``status`` is what the async route answers; ``sync_status`` what the sync route answered
    before the extraction (when different). ``payload`` replaces the sync body as a whole
    (Whisper's own error dict). ``sync_code`` puts ``error_code`` and ``extra`` into the sync
    body too (only the duplicate-library 409 carried them)."""

    def __init__(self, code: str, status: int, message: str, *, sync_status: Optional[int] = None,
                 payload: Optional[dict] = None, sync_code: bool = False, **extra):
        super().__init__(code)
        self.code = code
        self.error_code = code
        self.status = status
        self.message = message
        self.sync_status = sync_status or status
        self.payload = payload
        self.sync_code = sync_code
        self.extra = extra

    def sync_body(self) -> dict:
        if self.payload is not None:
            return self.payload
        body = {"success": False, "error": self.message}
        if self.sync_code:
            body.update({"error_code": self.code, **self.extra})
        return body

    def async_body(self) -> dict:
        return {"success": False, "error": self.message, "error_code": self.code, **self.extra}


@dataclass
class TranscribeParams:
    source: str  # file | library | recording | youtube | path
    source_ref: object = None  # uploaded path | audio_download_id | session id | url | path
    model: str = "large-v3"
    language: str = "uk"
    diarize: Optional[bool] = None
    category_id: Optional[int] = None
    title: Optional[str] = None  # already normalized by the caller
    description: Optional[str] = None
    recording_session_id: Optional[str] = None  # as the form sends it today
    actor: str = "ui"
    db_path: str = ""
    upload_folder: Optional[str] = None
    non_ui: bool = False
    download_id: Optional[str] = None  # sync youtube: a finished /api/youtube/download
    original_name: Optional[str] = None  # sync file: the uploaded file's own name
    max_bytes: Optional[int] = None  # path: MAX_CONTENT_LENGTH


def empty_source_meta(source_type: str) -> dict:
    return {
        'youtube_info': {},
        'download_id': None,
        'library_audio_id': None,
        'library_recording_sid': None,
        'source_type': source_type,
        # editable-title-description-02: a copy from the Audio Library (only 'library'
        # fills it), the caller's title/description takes priority.
        'title': None,
        'description': None,
    }


def _log(pid, stage, message, progress=None, status="processing"):
    if pid and state.add_log is not None:
        state.add_log(pid, stage, message, progress, status)


# ============================================================ resolve

def _extract_if_video(filepath: str, original_name: str, upload_dir: str, timestamp: str) -> str:
    """A video becomes its extracted audio; the video file (an upload or a copy) is removed."""
    if not is_video_file(original_name):
        return filepath
    logger.info(f"Video file detected: {original_name}")
    audio_filepath = os.path.join(upload_dir, f"{timestamp}_extracted_audio.mp3")
    if not _extract_audio_raw(filepath, audio_filepath, add_log=None):
        if os.path.exists(filepath):
            os.remove(filepath)
        raise TranscribeError('INTERNAL', 500, tr('transcript.err.audio_extract_failed'))
    if os.path.exists(filepath):
        os.remove(filepath)
        logger.info(f"Video file removed: {filepath}")
    logger.info(f"Using the extracted audio: {audio_filepath}")
    return audio_filepath


def _resolve_file(p: TranscribeParams):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = _extract_if_video(str(p.source_ref), p.original_name or '', p.upload_folder, timestamp)
    return filepath, p.original_name, empty_source_meta('file')


def _resolve_youtube_download(p: TranscribeParams):
    download_id = p.download_id
    if not download_id or download_id not in state.download_progress:
        raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.download_id_invalid'))
    progress = state.download_progress.get(download_id)
    if progress['status'] != 'completed':
        raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.download_unfinished'))
    youtube_info = progress['info']
    _log(download_id, "transcribe", tr("svc_media.tx.log.start"), 55, "processing")
    meta = empty_source_meta('youtube')
    meta['youtube_info'] = youtube_info
    meta['download_id'] = download_id
    return progress['file_path'], youtube_info['title'], meta


def _resolve_youtube_url(p: TranscribeParams):
    """Download the link's audio in this thread (the job's), then read it like a sync download."""
    from app.blueprints import youtube as youtube_bp_mod
    download_id = str(uuid.uuid4())[:20]
    state.download_progress.set(download_id, {"status": "starting", "percent": 0})
    youtube_bp_mod._download_youtube_core(str(p.source_ref), download_id, False, 'best', None, None)
    progress = state.download_progress.get(download_id) or {}
    if progress.get('status') != 'completed' or not progress.get('file_path'):
        raise TranscribeError('JOB_FAILED', 500, tr('transcript.err.youtube_download_failed'))
    return _resolve_youtube_download(TranscribeParams(source='youtube', download_id=download_id))


def _resolve_recording(p: TranscribeParams):
    sid = p.source_ref
    if not sid:
        raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.rec_session_required'))
    if state.recording_service is None:
        raise TranscribeError('RECORDING_DISABLED', 503, tr('transcript.err.rec_disabled'))
    try:
        manifest = state.recording_service.store.read(sid)
    except Exception:
        logger.debug("recording manifest read failed for sid=%s", sid, exc_info=True)
        raise TranscribeError('NOT_FOUND', 404, tr('transcript.err.rec_session_not_found'))
    if manifest.get('status') != 'finalized':
        raise TranscribeError('NOT_FINALIZED', 409, tr('transcript.err.rec_not_finalized'),
                              sync_status=400, session_id=sid)
    final_mp3 = manifest.get('final_mp3_path')
    if not final_mp3 or not os.path.isfile(final_mp3):
        raise TranscribeError('NO_MEDIA', 400, tr('transcript.err.rec_mp3_missing'))
    source_name = manifest.get('name') or manifest.get('auto_name') or sid
    return final_mp3, source_name, empty_source_meta('recording')


def _resolve_library(p: TranscribeParams):
    audio_id = p.source_ref
    if audio_id is None or audio_id == '':
        raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.audio_dl_required'))
    try:
        audio_id_int = int(audio_id)
    except (TypeError, ValueError):
        raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.audio_dl_not_number'))
    with get_db_connection(p.db_path) as conn:
        row = conn.execute(
            'SELECT * FROM audio_downloads WHERE id = ?', (audio_id_int,)
        ).fetchone()
    if not row:
        raise TranscribeError('NOT_FOUND', 404, tr('transcript.err.audio_lib_not_found'))
    filepath = row['file_path']
    if not filepath or not os.path.isfile(filepath):
        raise TranscribeError('NO_MEDIA', 400, tr('transcript.err.file_missing', path=filepath))
    keys = row.keys()
    # Inherit the original source_type so the transcript in History is grouped correctly.
    inherited = (row['source_type'] if 'source_type' in keys else None) or 'youtube'
    meta = empty_source_meta(inherited)
    meta['library_audio_id'] = audio_id_int
    meta['title'] = row['title'] if 'title' in keys else None
    meta['description'] = row['description'] if 'description' in keys else None
    # Phase 21: for a recording keep the sid -> link the copilot session.
    if inherited == 'recording':
        meta['library_recording_sid'] = row['recording_session_id'] if 'recording_session_id' in keys else None
    if inherited == 'youtube':
        meta['youtube_info'] = {
            'video_id': row['youtube_id'],
            'title': row['title'],
            'author': row['author'],
            'duration': row['duration'],
            'thumbnail': row['thumbnail_url'],
            'original_url': row['youtube_url'],
        }
    return filepath, row['title'], meta


def _resolve_path(p: TranscribeParams):
    """A local file inside an import root, copied into the upload folder (the original stays)."""
    try:
        real = import_roots.resolve_allowed(p.source_ref)
    except import_roots.PathNotAllowed:
        raise TranscribeError('PATH_NOT_ALLOWED', 403, tr('transcript.err.path_not_allowed'))
    original_name = os.path.basename(real)
    if not allowed_file(original_name):
        raise TranscribeError('UNSUPPORTED_EXTENSION', 400, tr('transcript.err.file_format_unsupported'))
    if not os.path.isfile(real):
        raise TranscribeError('NOT_FOUND', 404, tr('transcript.err.path_not_found'))
    if p.max_bytes and os.path.getsize(real) > p.max_bytes:
        raise TranscribeError('FILE_TOO_LARGE', 413, tr('transcript.err.file_too_large'))
    ext = original_name.rsplit('.', 1)[1].lower()
    name = secure_filename(original_name) or "audio_file"
    if not name.endswith(f'.{ext}'):
        name = f"{name}.{ext}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(p.upload_folder, exist_ok=True)
    target = os.path.join(p.upload_folder, f"{timestamp}_{uuid.uuid4().hex[:6]}_{name}")
    shutil.copyfile(real, target)
    logger.info("Import path copied into uploads: %s", target)
    filepath = _extract_if_video(target, original_name, p.upload_folder, timestamp)
    return filepath, original_name, empty_source_meta('file')


def resolve(params: TranscribeParams) -> tuple[str, str, dict]:
    """The source as ``(filepath, source_name, meta)``; a refusal raises ``TranscribeError``.

    ``meta['source_type']`` may differ from ``params.source`` (a library row inherits its own);
    everything downstream follows ``meta``."""
    if params.source == 'file':
        return _resolve_file(params)
    if params.source == 'youtube':
        if params.download_id is not None or not params.source_ref:
            return _resolve_youtube_download(params)
        return _resolve_youtube_url(params)
    if params.source == 'recording':
        return _resolve_recording(params)
    if params.source == 'library':
        return _resolve_library(params)
    if params.source == 'path':
        return _resolve_path(params)
    raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.source_type_invalid'))


# ============================================================ run

class _Cancelled(Exception):
    pass


def _stage(job, stage: str, progress: Optional[int] = None) -> None:
    if job is None:
        return
    job.meta['stage'] = stage
    if progress is not None:
        job.meta['progress'] = progress
    if job.is_cancelled():
        raise _Cancelled()


def run(params: TranscribeParams, resolved: Optional[tuple] = None, *, job=None,
        progress: Optional[Callable] = None) -> Optional[dict]:
    """Transcribe the resolved source and save it; the dict the sync route serialises.

    With ``job``: ``meta.stage``/``progress``/``transcription_id`` move, a cancel is checked
    between stages (then ``None`` is returned), logs go under ``job.id``."""
    filepath, source_name, meta = resolved if resolved is not None else resolve(params)
    model_name = params.model
    language = params.language
    db_path = params.db_path
    log_id = job.id if job is not None else None

    record_title = params.title if params.title is not None else meta['title']
    record_description = params.description if params.description is not None else meta['description']
    youtube_info = meta['youtube_info']
    download_id = meta['download_id']
    library_audio_id: Optional[int] = meta['library_audio_id']
    library_recording_sid: Optional[str] = meta['library_recording_sid']
    source_type = meta['source_type']

    registry = state.active_library_transcriptions
    if library_audio_id is not None and registry is not None:
        existing = registry.get(library_audio_id)
        if job is None and existing is not None:
            # A repeated POST for the same row while the first still runs: 409, not a second
            # full whisper+diarization pass on the same file.
            raise TranscribeError('ALREADY_RUNNING', 409, tr('transcript.err.already_running'),
                                  sync_code=True, active=existing)
        if job is None or existing is None or existing.get('job_id') != job.id:
            entry = {'audio_download_id': library_audio_id, 'started_at': time.time(),
                     'stage': 'transcribing', 'progress': 0}
            if job is not None:
                entry['job_id'] = job.id
            registry[library_audio_id] = entry
        else:
            existing['stage'] = 'transcribing'

    try:
        _stage(job, 'transcribing', 5)
        _log(log_id, "transcribe", tr("svc_media.tx.log.start"), 5, "processing")
        start_time = time.time()

        def transcription_progress_callback(progress_info):
            if not isinstance(progress_info, dict):
                return
            progress_value = progress_info.get('progress', 0)
            if source_type == 'youtube' and download_id:
                message = progress_info.get('message', tr("svc_media.tx.log.processing"))
                overall_progress = 55 + (progress_value * 0.4)
                segment = progress_info.get('segment')
                if segment:
                    try:
                        state.sse_broker.publish(download_id, "segment", segment)
                    except Exception:
                        logger.debug(
                            "SSE segment publish failed for %s (best-effort, does not block transcribe)",
                            download_id, exc_info=True,
                        )
                state.add_log(download_id, "transcribe", message, overall_progress, "processing")
            if library_audio_id is not None and registry is not None:
                entry = registry.get(library_audio_id)
                if entry is not None:
                    entry['progress'] = progress_value
            if job is not None:
                try:
                    job.meta['progress'] = max(5, min(70, 5 + int(float(progress_value) * 65)))
                except (TypeError, ValueError):
                    logger.debug("unreadable whisper progress %r", progress_value)
            if progress is not None:
                progress(progress_info)

        # K1: vocabulary and language - on files and recordings; YouTube gets {}.
        vocab_kind = source_type if source_type in ('youtube', 'recording') else 'file'
        result = state.whisper_manager.transcribe_with_progress(
            filepath, model_name, language,
            progress_callback=transcription_progress_callback,
            decode_options=vocabulary.decode_options_for(vocab_kind, db_path),
        )
        processing_time = time.time() - start_time

        if "error" in result:
            if source_type == 'youtube' and download_id:
                state.add_log(download_id, "transcribe", tr("svc_media.tx.log.failed", error=result['error']), -1, "error")
            _log(log_id, "transcribe", tr("svc_media.tx.log.failed", error=result['error']), -1, "error")
            raise TranscribeError('JOB_FAILED', 500, tr('transcript.err.transcribe_failed'), payload=result)

        if source_type == 'youtube' and download_id:
            state.add_log(download_id, "transcribe", tr("svc_media.tx.log.done"), 95, "processing")

        result["processing_time"] = round(processing_time, 2)
        result["model_used"] = model_name
        result["source_name"] = source_name
        result["source_type"] = source_type
        if source_type == 'youtube':
            result["youtube_info"] = youtube_info

        # Phase 10.3: speaker diarization (optional, always for a recording). A diarization
        # error does NOT break transcribe - the segments are saved without speaker fields.
        should_diarize = source_type == 'recording' or bool(params.diarize)
        if should_diarize and library_audio_id is not None and registry is not None:
            entry = registry.get(library_audio_id)
            if entry is not None:
                entry['stage'] = 'diarizing'
                entry['progress'] = 1.0
        unique_speaker_labels: list[str] = []
        diar_embeddings: dict[str, list[float]] = {}
        if should_diarize:
            _stage(job, 'diarizing', 72)
            try:
                from app.services.diarization_service import (
                    DiarizationService, run_diarization_for_audio,
                )
                svc = DiarizationService.get_instance()
                if not svc.is_available():
                    logger.info(
                        "Diarization disabled: %s - skipping",
                        svc.unavailability_reason(),
                    )
                else:
                    rec_manifest = None
                    if source_type == 'recording':
                        sid = params.recording_session_id
                        if sid and state.recording_service is not None:
                            try:
                                rec_manifest = state.recording_service.store.read(sid)
                            except Exception:
                                logger.debug(
                                    "recording manifest read failed for diarization sid=%s "
                                    "(continuing without manifest)", sid, exc_info=True,
                                )
                                rec_manifest = None
                    diar_t0 = time.time()
                    enriched_segs, unique_speaker_labels, diar_time, diar_embeddings = (
                        run_diarization_for_audio(
                            filepath,
                            result['segments'],
                            source_type=source_type,
                            recording_manifest=rec_manifest,
                            return_embeddings=True,  # Phase 10.6 voice fingerprinting
                        )
                    )
                    result['segments'] = enriched_segs
                    result['diarization_time'] = round(diar_time, 2)
                    result['speakers_detected'] = unique_speaker_labels
                    logger.info(
                        "Diarization: found %d speakers in %.1fs (wall=%.1fs, embeddings=%d)",
                        len(unique_speaker_labels), diar_time,
                        time.time() - diar_t0, len(diar_embeddings),
                    )
            except Exception as e:
                logger.warning(
                    "Diarization failed (continuing without it): %s", e, exc_info=True,
                )

        _stage(job, 'saving', 90)
        category_id = params.category_id

        with get_db_connection(db_path) as conn:
            c = conn.cursor()
            # Audio dedup (Wave A): the same text uploaded a second time stays a record, but
            # is marked duplicate_of -> the original's id and is NOT indexed.
            text_hash = dedup_audio.hash_for(source_type, result['text'])
            original = dedup_audio.find_original(conn, text_hash)
            duplicate_of = original['id'] if original else None
            if duplicate_of:
                logger.info("Audio duplicate: the text matches #%s «%s» - the record "
                            "is kept but not indexed", duplicate_of,
                            original.get('source_name'))
            c.execute('''INSERT INTO transcriptions
                         (source_type, source_name, source_url, youtube_id, youtube_title,
                          youtube_author, youtube_duration, youtube_thumbnail, file_path,
                          transcript_text, language, model_used, processing_time, segments,
                          category_id, content_hash, duplicate_of, title, description)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                source_type, source_name,
                youtube_info.get('original_url') if source_type == 'youtube' else None,
                youtube_info.get('video_id') if source_type == 'youtube' else None,
                youtube_info.get('title') if source_type == 'youtube' else None,
                youtube_info.get('author') if source_type == 'youtube' else None,
                youtube_info.get('duration') if source_type == 'youtube' else None,
                youtube_info.get('thumbnail') if source_type == 'youtube' else None,
                filepath, result['text'], result['language'], model_name,
                processing_time, json.dumps(result['segments']),
                category_id, text_hash, duplicate_of, record_title, record_description,
            ))
            transcription_id = c.lastrowid
            result['transcription_id'] = transcription_id
            if duplicate_of:
                result['duplicate_of'] = duplicate_of
            # An agent's own title/description is marked as such (as PATCH /api/history does).
            if params.non_ui and (params.title is not None or params.description is not None):
                c.execute("UPDATE transcriptions SET meta_source = 'mcp' WHERE id = ?",
                          (transcription_id,))

            # Phase 10.3 + 10.6: speaker map; 'self' -> the seeded is_self speaker, other
            # labels auto-matched by voice fingerprint, else NULL.
            if unique_speaker_labels:
                from app.services.diarization_service import (
                    embedding_to_blob, find_matching_speaker,
                )
                self_id_row = c.execute(
                    "SELECT id FROM speakers WHERE is_self = 1 LIMIT 1"
                ).fetchone()
                self_speaker_id = self_id_row[0] if self_id_row else None
                saved_speakers = c.execute(
                    "SELECT id, embedding FROM speakers WHERE embedding IS NOT NULL AND is_self = 0"
                ).fetchall()
                saved_list = [(r[0], r[1]) for r in saved_speakers]

                auto_matched = 0
                for label in unique_speaker_labels:
                    embedding = diar_embeddings.get(label)
                    embedding_blob = embedding_to_blob(embedding) if embedding else None
                    if label == 'self':
                        sid_for_label = self_speaker_id
                    else:
                        sid_for_label = None
                        if embedding:
                            match = find_matching_speaker(embedding, saved_list)
                            if match:
                                sid_for_label, sim = match
                                auto_matched += 1
                                logger.info(
                                    "Auto-match: %s → speaker_id=%d (cosine=%.3f)",
                                    label, sid_for_label, sim,
                                )
                    c.execute(
                        'INSERT OR IGNORE INTO transcription_speaker_map '
                        '(transcription_id, raw_label, speaker_id, embedding) '
                        'VALUES (?, ?, ?, ?)',
                        (transcription_id, label, sid_for_label, embedding_blob),
                    )
                if auto_matched:
                    logger.info(
                        "Voice fingerprinting: %d/%d speakers auto-identified",
                        auto_matched, len(unique_speaker_labels),
                    )
            conn.commit()
            # The full speaker payload, so the UI renders chips without another GET.
            if unique_speaker_labels:
                speaker_rows = c.execute('''
                    SELECT m.raw_label, m.speaker_id, s.name, s.color, s.is_self
                    FROM transcription_speaker_map m
                    LEFT JOIN speakers s ON s.id = m.speaker_id
                    WHERE m.transcription_id = ?
                ''', (transcription_id,)).fetchall()
                result['speakers'] = [
                    {
                        'raw_label': r['raw_label'],
                        'speaker_id': r['speaker_id'],
                        'name': r['name'],
                        'color': r['color'],
                        'is_self': bool(r['is_self']) if r['is_self'] is not None else False,
                    }
                    for r in speaker_rows
                ]
        if job is not None:
            job.meta['transcription_id'] = transcription_id

        state.metrics.inc("whisper_transcriptions_total", model=model_name, source=source_type)
        state.metrics.observe_duration("whisper_transcription_duration_seconds", processing_time, model=model_name)
        if unique_speaker_labels:
            state.metrics.inc(
                "whisper_diarizations_total", source=source_type,
            )

        # Phase 13: auto-enrichment as a background job. A duplicate is neither enriched nor
        # chunked: the original's chunks already exist.
        if job is not None:
            job.meta['stage'] = 'enriching'
            job.meta['progress'] = 95
        try:
            from app.services import enrichment
            if duplicate_of:
                result['enrichment'] = {"status": "skipped_duplicate"}
            elif enrichment.any_available() and state.job_queue is not None:
                _enrich_ch = f"enrich_{transcription_id}"

                def _enrich_job(job, _tid=transcription_id, _db=db_path, _ch=_enrich_ch):
                    try:
                        state.sse_broker.publish(_ch, "enrich", {"status": "running", "transcription_id": _tid})
                    except Exception:
                        logger.debug("SSE enrich 'running' publish failed for %s (best-effort)", _ch, exc_info=True)
                    res = enrichment.enrich_transcription(_db, _tid)
                    try:
                        state.sse_broker.publish(_ch, "complete", {"status": "completed", **res})
                    except Exception:
                        logger.debug("SSE enrich 'complete' publish failed for %s (best-effort)", _ch, exc_info=True)
                    return res

                state.job_queue.submit(
                    "enrichment", _enrich_job,
                    meta={"transcription_id": transcription_id},
                )
                result['enrichment'] = {"status": "queued"}
        except Exception as e:
            logger.warning("Enrichment job not queued (continuing): %s", e)

        # Phase 19 (Step 7): link the recording's copilot session to the created transcript.
        if source_type == 'recording':
            _rec_sid = params.recording_session_id or library_recording_sid
            if _rec_sid and state.copilot_service is not None:
                try:
                    _cs = state.copilot_service.get_by_recording(_rec_sid)
                    if _cs:
                        state.copilot_service.link_transcription(_cs['id'], transcription_id)
                except Exception as e:
                    logger.warning("copilot link to transcription failed: %s", e)

            # Wave 4: comments written DURING the call hung on the recording session; they
            # move to the transcript (anchor_time unchanged: same offset from the start).
            if _rec_sid:
                try:
                    from app.services import comments as comments_svc
                    moved = comments_svc.retarget(
                        db_path,
                        'recording_session', _rec_sid,
                        'transcription', transcription_id)
                    if moved:
                        result['comments_moved'] = moved
                        logger.info("[comments] session %s → tx=%s: re-attached %d",
                                    _rec_sid, transcription_id, moved)
                except Exception as e:
                    # Comments stay on the session and are not lost; a repeated run fixes it.
                    logger.warning("comments retarget failed: %s", e)

        # Phase 12.11: the uploaded file stays (the transcript view's audio player needs it).
        if source_type == 'youtube' and download_id:
            state.add_log(download_id, "complete", tr("svc_media.tx.log.complete"), 100, "completed")
        _log(log_id, "complete", tr("svc_media.tx.log.complete"), 100, "completed")
        if job is not None:
            job.meta['stage'] = 'done'
            job.meta['progress'] = 100
        return result

    except _Cancelled:
        logger.info("[transcription] job %s cancelled at stage %s", log_id, job.meta.get('stage'))
        return None
    except TranscribeError:
        raise
    except Exception as e:
        logger.error(f"Transcription error: {e}", exc_info=True)
        if source_type == 'youtube' and download_id:
            state.add_log(download_id, "transcribe", tr("svc_media.tx.log.failed", error=e), -1, "error")
        _log(log_id, "transcribe", tr("svc_media.tx.log.failed", error=e), -1, "error")
        # On error the uploaded file (or the import copy) is deleted - no transcription for it.
        if source_type == 'file' and filepath and os.path.exists(filepath):
            os.remove(filepath)
        raise TranscribeError('JOB_FAILED', 500, tr('transcript.err.transcribe_failed')) from e
    finally:
        if library_audio_id is not None and registry is not None:
            entry = registry.get(library_audio_id)
            if job is None or (entry is not None and entry.get('job_id') == job.id):
                registry.pop(library_audio_id, None)


# ============================================================ submit (async)

def _installed(model: str) -> bool:
    mgr = getattr(state, 'whisper_manager', None)
    if mgr is None:
        return False
    try:
        models = mgr.get_available_models()
    except Exception:
        logger.warning('model list unavailable for async transcription', exc_info=True)
        return False
    return any(m.get('name') == model and m.get('downloaded') for m in models)


def _ref_key(params: TranscribeParams) -> tuple:
    ref = params.source_ref
    if params.source == 'library':
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            pass
    return (params.source, str(ref))


def _active_job_id(key) -> Optional[str]:
    """The live job for ``key``; an entry whose job never ran (cancelled in the queue) is dropped."""
    jid = _inflight.get(key)
    if jid is None:
        return None
    job = state.job_queue.get(jid) if state.job_queue is not None else None
    if job is None or job.state not in ('queued', 'running'):
        _inflight.pop(key, None)
        return None
    return jid


def _recording_refusal() -> TranscribeError:
    return TranscribeError('RECORDING_ACTIVE', 409, tr('transcript.err.transcribe_recording'))


def submit(params: TranscribeParams) -> dict:
    """Queue the transcription; ``{"job_id", "created"}``. A refusal raises ``TranscribeError``.

    Library, recording and path sources are resolved here (a path is copied into the upload
    folder before the job starts); a YouTube link is downloaded inside the job."""
    if params.source not in ASYNC_SOURCES:
        raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.async_source_invalid'))
    if params.non_ui and recording_active():
        raise _recording_refusal()
    if state.job_queue is None:
        raise TranscribeError('UNAVAILABLE', 503, tr('transcript.err.queue_unavailable'))
    if not params.model or not _installed(params.model):
        raise TranscribeError('MODEL_NOT_INSTALLED', 400, tr('transcript.err.model_not_installed'))

    key = _ref_key(params)
    with _inflight_lock:
        live = _active_job_id(key)
        if live:
            return {"job_id": live, "created": False}
        if params.source == 'youtube':
            from app.utils.youtube_id import extract_youtube_id
            if not params.source_ref or not extract_youtube_id(str(params.source_ref)):
                raise TranscribeError('INVALID_ARGUMENT', 400, tr('transcript.err.youtube_url_invalid'))
            resolved = None
        else:
            resolved = resolve(params)
        meta_src = resolved[2] if resolved is not None else None
        aid = meta_src['library_audio_id'] if meta_src else None
        registry = state.active_library_transcriptions
        if aid is not None and registry is not None and registry.get(aid) is not None:
            raise TranscribeError('ALREADY_RUNNING', 409, tr('transcript.err.already_running'),
                                  active=registry.get(aid))

        from flask import current_app
        flask_app = current_app._get_current_object()
        job_id = str(uuid.uuid4())[:20]
        if aid is not None and registry is not None:
            registry[aid] = {'audio_download_id': aid, 'started_at': time.time(),
                             'stage': 'queued', 'progress': 0, 'job_id': job_id}
        _inflight[key] = job_id
        meta = {'source': params.source, 'source_ref': key[1], 'actor': params.actor,
                'model': params.model, 'stage': 'queued', 'progress': 0}
        if aid is not None:
            meta['audio_download_id'] = aid
        try:
            state.job_queue.submit('transcription', _job, params, resolved, key, flask_app,
                                   job_id=job_id, meta=meta)
        except Exception:
            _inflight.pop(key, None)
            if aid is not None and registry is not None:
                registry.pop(aid, None)
            raise
    return {"job_id": job_id, "created": True}


def _job(job, params: TranscribeParams, resolved, key, flask_app) -> Optional[dict]:
    ran = False
    try:
        with flask_app.app_context():
            if params.non_ui and recording_active():
                raise _recording_refusal()
            if job.is_cancelled():
                return None
            if resolved is None:
                job.meta['stage'] = 'downloading'
                job.meta['progress'] = 1
                _log(job.id, "download", tr("svc_media.tx.log.start"), 1, "processing")
                resolved = resolve(params)
                if params.non_ui and recording_active():
                    raise _recording_refusal()
            ran = True
            result = run(params, resolved, job=job)
            if result is None:
                return None
            return {
                'transcription_id': result.get('transcription_id'),
                'source_type': result.get('source_type'),
                'source_name': result.get('source_name'),
                'language': result.get('language'),
                'model_used': result.get('model_used'),
                'processing_time': result.get('processing_time'),
                'duplicate_of': result.get('duplicate_of'),
            }
    finally:
        with _inflight_lock:
            if _inflight.get(key) == job.id:
                del _inflight[key]
        registry = state.active_library_transcriptions
        aid = resolved[2]['library_audio_id'] if resolved is not None else None
        if aid is not None and registry is not None:
            entry = registry.get(aid)
            if entry is not None and entry.get('job_id') == job.id:
                registry.pop(aid, None)
        # A path copy the job never transcribed is not left behind in the upload folder.
        if not ran and params.source == 'path' and resolved is not None:
            try:
                if os.path.exists(resolved[0]):
                    os.remove(resolved[0])
            except OSError:
                logger.debug("import copy cleanup failed for %s", resolved[0], exc_info=True)
