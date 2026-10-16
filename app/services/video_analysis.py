"""Video understanding — adaptive keyframes → OCR → timeline and RAG.

Phase 23 started the pipeline (scene-detect, frames on disk); spec video-visual-timeline
(13.10.2026) replaced frame selection with :mod:`app.services.video_frames` and removed
image storage:

  1. resolve_recording_video(db_path, transcription_id)
       The single source of truth about a recording's video (audio_downloads.has_video=1 →
       recording_video_tracks.start_offset_sec). None if there is no video.

  2. analyze_video(db_path, transcription_id, ...)
       * segment selection (``video_frames.select_keyframes``, method thumbs|bisect);
       * the representative frame of each segment is cut from the video INTO MEMORY and goes to OCR
         (Tesseract with word boxes) via a thread pool — nothing is written to disk;
       * frame type, repeat by content, word boxes → ``video_keyframes``;
       * OCR (and a description, only with ``describe=True``) → chunks ``speaker='екран'``,
         section «кадр ГГ:ХХ:СС», so search_archive/ask_archive see the frames;
       * algorithm version + parameters → ``transcriptions.video_analysis_params``:
         the same parameters without ``force`` → a ready result without decoding.
       Progress is ``job.meta['progress']``; it is cancelled between frames.

Frame time in the timeline = time in the video + the track's ``start_offset_sec``.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Sequence

import numpy as np

from app.db.connection import get_db_connection
from app.services import video_frames as vf

logger = logging.getLogger(__name__)

# Minimum number of non-whitespace chars in OCR output to treat it as signal.
_MIN_OCR_SIGNAL = 8
# Minimum non-whitespace chars in a vision description to treat it as signal.
_MIN_VISION_SIGNAL = 12
# How many frames are OCR-ed in parallel: tesseract is a separate process per frame, and
# during video OCR each one is single-threaded (OMP_THREAD_LIMIT=1). Measured 13.10.2026 on
# 28 CPU threads, 2560 px frame, tessdata_fast: 8 workers — 0.77 s/frame, 16 — 0.59.
_OCR_WORKERS = max(2, min(16, (os.cpu_count() or 4) // 2 + 2))
# Frame width for OCR: small screen font at 2560 px is readable, wider
# gives no more text, only more time.
_OCR_MAX_WIDTH = 2560


# ---------------------------------------------------------------------------
# 1. resolve_recording_video — single source of truth for video location
# ---------------------------------------------------------------------------

def resolve_recording_video(db_path: str, transcription_id: int) -> Optional[dict]:
    """Return video info dict for *transcription_id*, or None if no video.

    Lookup chain:
      transcriptions.file_path
        → audio_downloads WHERE file_path=? AND has_video=1
          → recording_video_tracks (primary track, start_offset_sec)

    Returns dict with keys:
        recording_session_id  str
        primary_video_path    str  (absolute path verified to exist on disk)
        session_dir           str  (dirname of primary_video_path)
        start_offset_sec      float  (default 0.0)

    Returns None on any error, missing data, or missing file.
    Never raises.
    """
    try:
        with get_db_connection(db_path) as conn:
            tx = conn.execute(
                "SELECT file_path FROM transcriptions WHERE id = ?",
                (transcription_id,),
            ).fetchone()
            if not tx or not tx['file_path']:
                return None

            ad = conn.execute(
                """SELECT recording_session_id, primary_video_path
                     FROM audio_downloads
                    WHERE file_path = ? AND has_video = 1
                    LIMIT 1""",
                (tx['file_path'],),
            ).fetchone()
            if not ad or not ad['primary_video_path']:
                return None

            pvp: str = ad['primary_video_path']
            if not os.path.isfile(pvp):
                logger.warning("[video_analysis] video file not found: %s", pvp)
                return None

            sid: str = ad['recording_session_id'] or ''

            start_offset_sec = 0.0
            if sid:
                track = conn.execute(
                    """SELECT start_offset_sec
                         FROM recording_video_tracks
                        WHERE recording_session_id = ?
                          AND file_path = ?
                        ORDER BY id ASC LIMIT 1""",
                    (sid, pvp),
                ).fetchone()
                if track and track['start_offset_sec'] is not None:
                    start_offset_sec = float(track['start_offset_sec'])

            return {
                'recording_session_id': sid,
                'primary_video_path': pvp,
                'session_dir': os.path.dirname(pvp),
                'start_offset_sec': start_offset_sec,
            }
    except Exception:
        logger.exception("[video_analysis] resolve_recording_video failed silently")
        return None


# ---------------------------------------------------------------------------
# 2. analyze_video
# ---------------------------------------------------------------------------

def _next_chunk_index(conn, transcription_id: int) -> int:
    """Return the next available chunk_index for this transcription."""
    row = conn.execute(
        "SELECT MAX(chunk_index) FROM chunks WHERE transcription_id = ?",
        (transcription_id,),
    ).fetchone()
    return (int(row[0]) + 1) if (row and row[0] is not None) else 0


def _load_cfg():
    """Load Config object without hard import cycle (never raises)."""
    try:
        import config as _cm
        return getattr(_cm, 'current_config', None) or _cm.Config
    except Exception:
        logger.debug("config load failed, falling back to hardcoded defaults", exc_info=True)
        return None


def ocr_words(img) -> tuple[str, list]:
    """OCR of one PIL frame with word boxes.

    → (text by lines, [[x, y, w, h, word], ...] in pixels of ``img``).
    The boxes are needed to paint over secrets on the fly."""
    import pytesseract
    from app.services import document_parser as _dp
    try:
        data = pytesseract.image_to_data(img, lang=_dp._OCR_LANG,
                                         output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        logger.debug("OCR with languages %s failed — default language", _dp._OCR_LANG, exc_info=True)
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words: list = []
    lines: dict = {}
    for i, word in enumerate(data.get('text') or []):
        word = (word or '').strip()
        if not word:
            continue
        try:
            if float(data['conf'][i]) < 0:
                continue
        except (TypeError, ValueError):
            logger.debug("OCR conf is not a number: %r", data['conf'][i])
        words.append([int(data['left'][i]), int(data['top'][i]),
                      int(data['width'][i]), int(data['height'][i]), word])
        key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        lines.setdefault(key, []).append(word)
    text = '\n'.join(' '.join(ws) for ws in lines.values())
    return text, words


class _VideoOcrEnv:
    """During video OCR: single-threaded tesseract and fast models (if installed).

    ``tessdata_fast`` at 2560 px reads on-screen Cyrillic the same as the standard
    models, ~30 % faster (measurement in docs/video-timeline.md). Documents stay
    on the standard ones — speed is not the main thing there."""
    _KEYS = ('OMP_THREAD_LIMIT', 'TESSDATA_PREFIX')

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        os.environ['OMP_THREAD_LIMIT'] = '1'
        fast = os.environ.get('OCR_TESSDATA_FAST_DIR') or os.path.join(
            os.environ.get('LOCALAPPDATA', ''), 'Recall', 'tessdata_fast')
        if os.path.isdir(fast):
            os.environ['TESSDATA_PREFIX'] = fast
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _params(*, method, step, min_res, threshold, settle, ignore_regions, max_frames,
            ocr, describe, vision_backend) -> dict:
    return {
        'algo': vf.ALGO_VERSION, 'method': method, 'step': step, 'min_res': min_res,
        'threshold': threshold, 'settle': settle,
        'ignore_regions': [[float(v) for v in r] for r in (ignore_regions or [])],
        'max_frames': max_frames, 'ocr': bool(ocr), 'describe': bool(describe),
        'vision_backend': (vision_backend or 'default') if describe else None,
    }


def _set_progress(job, stage: str, done: float, total: float) -> None:
    if job is None:
        return
    try:
        pct = round(100.0 * done / total, 1) if total else 0.0
        job.meta['progress'] = {'stage': stage, 'done': int(done), 'total': int(total),
                                'pct': pct}
    except Exception:
        logger.debug("job progress update failed", exc_info=True)


def _describe(video: str, t: float, backend: Optional[str], ffmpeg: Optional[str],
              boxes: Optional[list] = None, src_w: int = 0, src_h: int = 0) -> str:
    """Describe a frame with a vision model. The frame lives in a temporary file only for the duration of the call,
    and secret boxes on it are already painted over — for both the local model and Claude."""
    from app.services import screen_secrets as _ss
    from app.services import video_vision as _vv
    img = _ss.redact(vf.grab_frame(video, t, max_width=1920, ffmpeg=ffmpeg),
                     boxes or [], src_w, src_h)
    fd, path = tempfile.mkstemp(suffix='.jpg', prefix='recall_frame_')
    os.close(fd)
    try:
        img.save(path, 'JPEG', quality=88)
        return (_vv.describe_frame(path, backend=backend) or '').strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            logger.warning("temporary frame not deleted: %s", path, exc_info=True)


def analyze_video(
    db_path: str,
    transcription_id: int,
    force: bool = False,
    job=None,
    vision_backend: Optional[str] = None,
    *,
    method: Optional[str] = None,
    step: Optional[float] = None,
    min_res: Optional[float] = None,
    threshold: Optional[float] = None,
    settle: Optional[float] = None,
    ignore_regions: Optional[Sequence[Sequence[float]]] = None,
    max_frames: Optional[int] = None,
    ocr: bool = True,
    describe: bool = False,
    ffmpeg: Optional[str] = None,
) -> dict:
    """Select keyframes, OCR them (optionally describe), write the timeline and chunks.

    ``describe`` — a frame description by a vision model (off by default: slow and/or costs):
    ``vision_backend`` None/'local' — a local Ollama model, 'claude' — Anthropic.

    Returns a dict: has_video, skipped (only on early exit), error_code
    (NO_VIDEO), keyframes, chunks_added, vision_descriptions, capped, method,
    params. Never raises.
    """
    def _cancelled() -> bool:
        try:
            return job is not None and job.is_cancelled()
        except Exception:
            logger.debug("job.is_cancelled() check failed, assuming not cancelled", exc_info=True)
            return False

    info = resolve_recording_video(db_path, transcription_id)
    if info is None:
        return {'has_video': False, 'skipped': 'no video', 'error_code': 'NO_VIDEO'}
    video = info['primary_video_path']
    offset = float(info.get('start_offset_sec') or 0.0)

    cfg = _load_cfg()
    cfg_max = vf.DEFAULT_MAX_FRAMES
    if cfg is not None:
        cfg_max = int(getattr(cfg, 'RECORDING_VIDEO_ANALYSIS_MAX_FRAMES', cfg_max) or cfg_max)
    params = _params(
        method=method or vf.DEFAULT_METHOD, step=float(step or vf.DEFAULT_STEP),
        min_res=float(min_res or vf.DEFAULT_MIN_RES),
        threshold=float(threshold if threshold is not None else vf.DEFAULT_THRESHOLD),
        settle=float(settle if settle is not None else vf.DEFAULT_SETTLE),
        ignore_regions=ignore_regions, max_frames=int(max_frames or cfg_max),
        ocr=ocr, describe=describe, vision_backend=vision_backend)

    # ---- ready result with the same parameters -------------------------
    try:
        with get_db_connection(db_path) as conn:
            tx = conn.execute(
                "SELECT video_analysis_at, video_keyframes_count, video_analysis_params "
                "FROM transcriptions WHERE id = ?", (transcription_id,)).fetchone()
    except Exception:
        logger.exception("[video_analysis] reading the analysis state failed")
        return {'has_video': True, 'skipped': 'db error'}
    if tx and tx['video_analysis_at'] and not force:
        try:
            prev = json.loads(tx['video_analysis_params'] or 'null')
        except ValueError:
            logger.debug("video_analysis_params is not JSON — recompute", exc_info=True)
            prev = None
        if prev == params:
            return {'has_video': True, 'skipped': 'already analyzed',
                    'keyframes': tx['video_keyframes_count'] or 0, 'params': params}

    # ---- segment selection ----------------------------------------------------
    _set_progress(job, 'frames', 0, 1)
    try:
        sel = vf.select_keyframes(
            video, method=params['method'], step=params['step'], min_res=params['min_res'],
            threshold=params['threshold'], ignore_regions=params['ignore_regions'],
            max_frames=params['max_frames'], settle=params['settle'],
            cache_dir=os.path.join(info['session_dir'], 'frames_cache'), ffmpeg=ffmpeg,
            progress=lambda f: _set_progress(job, 'frames', f * 1000, 1000))
    except vf.NoVideoError as e:
        logger.info("[video_analysis] tx=%d: video unreadable: %s", transcription_id, e)
        return {'has_video': False, 'skipped': str(e), 'error_code': 'NO_VIDEO'}
    except Exception:
        logger.exception("[video_analysis] frame selection failed")
        return {'has_video': True, 'skipped': 'frame selection failed'}
    segs = sel['segments']
    if _cancelled():
        return {'has_video': True, 'skipped': 'cancelled'}

    # ---- OCR (parallel, frames in memory only) ------------------------------
    from app.services import document_parser as _dp
    ocr_ok = bool(ocr) and _dp.ocr_available()
    if ocr and not ocr_ok:
        logger.warning("[video_analysis] Tesseract unavailable — frames without OCR")

    def _ocr_one(seg):
        if _cancelled() or not ocr_ok:
            return '', []
        img = vf.grab_frame(video, seg.frame_time, max_width=_OCR_MAX_WIDTH, ffmpeg=ffmpeg)
        return ocr_words(img)

    ocr_results: list = [('', [])] * len(segs)
    if segs and ocr_ok:
        with _VideoOcrEnv(), ThreadPoolExecutor(max_workers=_OCR_WORKERS) as pool:
            futures = [pool.submit(_ocr_one, s) for s in segs]
            for i, fut in enumerate(futures):
                try:
                    ocr_results[i] = fut.result()
                except Exception:
                    logger.debug("[video_analysis] OCR of frame %.1f s failed",
                                 segs[i].frame_time, exc_info=True)
                _set_progress(job, 'ocr', i + 1, len(segs))
    if _cancelled():
        return {'has_video': True, 'skipped': 'cancelled'}

    # ---- on-screen secrets: masked BEFORE any write or description ----------
    from app.services import screen_secrets as _ss
    width, height = int(sel.get('width') or 0), int(sel.get('height') or 0)
    ocr_w = min(width, _OCR_MAX_WIDTH) if width else 0
    ocr_h = int(height * min(1.0, _OCR_MAX_WIDTH / width)) if width else height
    frame_secrets: list = []
    for i, (text, words) in enumerate(ocr_results):
        found = _ss.scan_all(text, words)
        frame_secrets.append((found, words))
        ocr_results[i] = (_ss.mask_text(text, found) or '', _ss.mask_words(words, found))
    n_secrets = sum(len(f) for f, _ in frame_secrets)
    if n_secrets:
        logger.info("[video_analysis] tx=%d: %d on-screen secrets masked",
                    transcription_id, n_secrets)

    # ---- description (only describe=True) ------------------------------------------
    dups = vf.mark_duplicates(segs)
    vision_texts: list[str] = [''] * len(segs)
    vision_model = None
    if describe:
        from app.services import video_vision as _vv
        vision_ok, why = _vv.availability(vision_backend)
        vision_model = _vv.active_model(vision_backend)
        if not vision_ok:
            logger.info("[video_analysis] description unavailable: %s", why)
            # into the parameters goes what was really done: a repeated call with describe
            # will try again instead of returning "ready" without descriptions
            params = dict(params, describe=False, vision_backend=None)
        else:
            for i, s in enumerate(segs):
                if _cancelled():
                    break
                if dups[i] is None:          # a repeat takes the first one's description — not described twice
                    try:
                        found, raw_words = frame_secrets[i]
                        boxes = [raw_words[x.word_index][:4] for x in found
                                 if x.word_index is not None]
                        vis = _describe(video, s.frame_time, vision_backend, ffmpeg,
                                        boxes, ocr_w, ocr_h)
                        vision_texts[i] = _ss.mask_text(vis, found + _ss.scan_text(vis)) or ''
                    except Exception:
                        logger.debug("description of frame %.1f s failed", s.frame_time, exc_info=True)
                _set_progress(job, 'describe', i + 1, len(segs))

    # ---- RAG chunks and their vectors — BEFORE the write transaction --------------------
    # Vectors take long to compute; in an open transaction that would hold the SQLite
    # write lock for the whole pass, and other writers would fail on BUSY_TIMEOUT.
    now = datetime.datetime.now().isoformat(' ', 'seconds')
    try:
        from app.services import embeddings as _emb
        emb_ok = _emb.is_available()
    except Exception:
        logger.debug("embeddings unavailable — chunks without vectors", exc_info=True)
        _emb, emb_ok = None, False
    pending_chunks: list = []                 # (t, end, text, vector)
    for i, s in enumerate(segs):
        if dups[i] is not None:               # a repeat is not duplicated in RAG
            continue
        text = (ocr_results[i][0] or '').strip()
        vis = vision_texts[i]
        has_ocr = len(re.sub(r'\s', '', text)) >= _MIN_OCR_SIGNAL
        has_vis = len(re.sub(r'\s', '', vis)) >= _MIN_VISION_SIGNAL
        if not (has_ocr or has_vis):
            continue
        parts = ([text] if has_ocr else []) + ([f'[опис кадру] {vis}'] if has_vis else [])
        chunk_text = '\n'.join(parts)
        blob = None
        if emb_ok:
            try:
                blob = _emb.embed_texts([chunk_text])[0].astype(np.float32).tobytes()
            except Exception:
                logger.debug("frame embedding failed", exc_info=True)
        pending_chunks.append((s.start + offset, s.end + offset, chunk_text, blob))

    # ---- write ---------------------------------------------------------------
    chunks_added = 0
    try:
        with get_db_connection(db_path) as conn:
            legacy_files = [r['image_path'] for r in conn.execute(
                "SELECT image_path FROM video_keyframes "
                "WHERE transcription_id = ? AND image_path IS NOT NULL", (transcription_id,))]
            conn.execute("DELETE FROM video_keyframes WHERE transcription_id = ?",
                         (transcription_id,))
            conn.execute("DELETE FROM chunks WHERE transcription_id = ? AND speaker = 'екран'",
                         (transcription_id,))
            row_ids: list[int] = []
            for i, s in enumerate(segs):
                text, words = ocr_results[i]
                lines = [ln for ln in text.split('\n') if ln.strip()]
                mean_h = (sum(w[3] for w in words) / len(words)) if words else 0.0
                ftype = vf.classify(s, ocr_chars=len(re.sub(r'\s', '', text)),
                                    ocr_lines=len(lines), mean_word_h=mean_h, frame_h=ocr_h,
                                    min_res=params['min_res'], ocr_ran=ocr_ok)
                vis = vision_texts[i]
                cur = conn.execute(
                    """INSERT INTO video_keyframes
                         (transcription_id, ts_offset_sec, end_sec, video_time_sec, image_path,
                          ocr_text, ocr_boxes_json, scene_score, change_score, frame_type,
                          vision_text, vision_model, vision_at, source)
                       VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (transcription_id, s.start + offset, s.end + offset, s.frame_time,
                     text or None,
                     json.dumps({'w': ocr_w, 'h': ocr_h,
                                 'words': words}, ensure_ascii=False) if words else None,
                     s.change_score, s.change_score, ftype,
                     vis or None, vision_model if vis else None, now if vis else None,
                     params['method']))
                row_ids.append(cur.lastrowid)
                found, raw_words = frame_secrets[i]
                if found:
                    _ss.store(conn, cur.lastrowid, found, raw_words)
            for i, d in enumerate(dups):
                if d is not None:
                    conn.execute("UPDATE video_keyframes SET duplicate_of = ? WHERE id = ?",
                                 (row_ids[d], row_ids[i]))

            for t, end_t, chunk_text, blob in pending_chunks:
                section = f'кадр {int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}'
                conn.execute(
                    """INSERT OR IGNORE INTO chunks
                         (transcription_id, chunk_index, start_time, end_time,
                          speaker, text, embedding, section)
                       VALUES (?, ?, ?, ?, 'екран', ?, ?, ?)""",
                    (transcription_id, _next_chunk_index(conn, transcription_id),
                     t, end_t, chunk_text, blob, section))
                chunks_added += 1
            conn.execute(
                """UPDATE transcriptions SET video_analysis_at = CURRENT_TIMESTAMP,
                          video_keyframes_count = ?, video_analysis_params = ? WHERE id = ?""",
                (len(segs), json.dumps(params, ensure_ascii=False), transcription_id))
            conn.commit()
    except Exception:
        logger.exception("[video_analysis] writing the timeline failed")
        return {'has_video': True, 'skipped': 'db write failed'}

    # frames of the old pipeline (Phase 23 stored jpg) — images are no longer stored
    for path in legacy_files:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            logger.warning("old frame not deleted: %s", path, exc_info=True)

    n_desc = sum(1 for v in vision_texts if v)
    logger.info("[video_analysis] tx=%d: %d frames (%s), %d chunks, %d descriptions%s",
                transcription_id, len(segs), params['method'], chunks_added, n_desc,
                ' (capped)' if sel['capped'] else '')
    _set_progress(job, 'done', 1, 1)
    return {'has_video': True, 'keyframes': len(segs), 'chunks_added': chunks_added,
            'vision_descriptions': n_desc, 'secrets_masked': n_secrets, 'capped': sel['capped'],
            'method': params['method'], 'params': params}
