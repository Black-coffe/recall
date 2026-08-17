"""Video understanding — keyframe OCR → RAG ingestion (Phase 23, Story B-A).

Pipeline (all $0, no vision API):
  1. resolve_recording_video(db_path, transcription_id)
       Single source of truth for the recording's screen-video path.
       Looks up transcription.file_path → audio_downloads (has_video=1) →
       recording_video_tracks (start_offset_sec).
       Returns dict or None — never raises.

  2. extract_scene_keyframes(video_path, out_dir, ...)
       Run ffmpeg scene-detection (-vf "select='gt(scene,{thr})',showinfo") +
       parse pts_time from showinfo stderr.
       Floor fallback: if scene gives fewer frames than ceil(duration/30),
       run a second pass at fps=1/30 (long static videos still get coverage).
       Dedupes near-identical timestamps; caps to max_frames.
       Returns [(ts_offset_sec, image_path), ...] — empty list on any error.

  3. analyze_video(db_path, transcription_id, force=False, job=None)
       Orchestrates everything:
         - Guard: skip if video_analysis_at already set (unless force).
         - Force cleanup: DELETE prior video_keyframes + chunks WHERE speaker='екран'.
         - OCR each frame via document_parser._ocr_image_obj (Tesseract, $0).
         - INSERT into video_keyframes.
         - If OCR text has enough signal: embed_texts([text]) → INSERT into chunks
           (speaker='екран', section='екран MM:SS').  chunks_fts trigger auto-fires.
         - UPDATE transcriptions.video_analysis_at / video_keyframes_count.
       Cancellable: checks job.is_cancelled() between frames.
       Best-effort: one bad frame never aborts the whole job.

Config (in Config class, config.py):
  RECORDING_VIDEO_SCENE_THRESHOLD     float  default 0.4
  RECORDING_VIDEO_ANALYSIS_MAX_FRAMES int    default 120
"""
from __future__ import annotations

import datetime
import glob
import logging
import math
import os
import re
import subprocess
from typing import Optional

import numpy as np

from app.db.connection import get_db_connection
from app.utils.proc import NO_WINDOW

logger = logging.getLogger(__name__)

# Minimum number of non-whitespace chars in OCR output to treat it as signal.
_MIN_OCR_SIGNAL = 8
# Minimum non-whitespace chars in a vision description to treat it as signal.
_MIN_VISION_SIGNAL = 12

# Timestamps within this many seconds of each other are considered duplicates.
_TS_DEDUP_GAP = 0.5


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

            # start_offset_sec from the matching track row
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
# 2. extract_scene_keyframes
# ---------------------------------------------------------------------------

def _ffmpeg_bin(ffmpeg: Optional[str] = None) -> str:
    """Resolve ffmpeg binary path (never raises)."""
    if ffmpeg:
        return ffmpeg
    try:
        from app.services.recording.video_probe import ffmpeg_path
        return ffmpeg_path()
    except Exception:
        logger.debug("ffmpeg_path() resolution failed, falling back to default path", exc_info=True)
        return r'C:\ffmpeg\bin\ffmpeg.exe'


def _probe_duration(video_path: str, ff: str) -> float:
    """Return video duration in seconds via ffprobe, or 0.0 on failure."""
    ffprobe = os.path.join(os.path.dirname(ff), 'ffprobe.exe')
    if not os.path.isfile(ffprobe):
        ffprobe = 'ffprobe'
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'error',
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1',
             video_path],
            capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW,
        )
        return float(r.stdout.strip())
    except Exception:
        logger.debug("ffprobe duration probe failed for %s", video_path, exc_info=True)
        return 0.0


def _parse_pts_times(stderr: str) -> list[float]:
    """Extract ordered pts_time floats from showinfo filter stderr.

    showinfo emits one line per frame:
        [Parsed_showinfo_1 @ 0x...] n:  0 pts: ... pts_time:1.234 ...
    """
    return [float(m) for m in re.findall(r'pts_time:([\d.]+)', stderr)]


def extract_scene_keyframes(
    video_path: str,
    out_dir: str,
    scene_threshold: float = 0.4,
    max_frames: int = 120,
    ffmpeg: Optional[str] = None,
) -> list[tuple[float, str]]:
    """Extract scene-change keyframes from *video_path* into *out_dir*.

    Uses ffmpeg scene detection filter.  If the resulting frame count is
    below ``ceil(duration_sec / 30)`` (very static video), a second pass
    samples one frame every 30 s to guarantee coverage.

    Args:
        video_path:       Source .mp4 / .mkv.
        out_dir:          Directory for JPEG output.  Created if absent.
        scene_threshold:  Scene-change detection sensitivity (0–1).
        max_frames:       Hard cap; excess frames are evenly sub-sampled.
        ffmpeg:           ffmpeg binary path (auto-resolved if None).

    Returns:
        Sorted [(ts_offset_sec, image_path)] pairs.  Empty list on failure.
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
        ff = _ffmpeg_bin(ffmpeg)
        duration = _probe_duration(video_path, ff)

        # ---- Pass 1: scene detection ----------------------------------------
        p1_pat = os.path.join(out_dir, 'sc_%05d.jpg')
        cmd_scene = [
            ff, '-hide_banner', '-i', video_path,
            '-vf', f"select='gt(scene,{scene_threshold})',showinfo",
            '-vsync', 'vfr', '-q:v', '3',
            p1_pat,
        ]
        logger.debug("[video_analysis] scene cmd: %s", ' '.join(cmd_scene))
        r1 = subprocess.run(cmd_scene, capture_output=True, text=True, timeout=600,
                            creationflags=NO_WINDOW)
        pts1 = _parse_pts_times(r1.stderr)
        sc_files = sorted(glob.glob(os.path.join(out_dir, 'sc_*.jpg')))
        pairs: list[tuple[float, str]] = list(zip(pts1, sc_files))

        # ---- Pass 2: static-video floor -------------------------------------
        floor_count = math.ceil(duration / 30) if duration > 0 else 1
        if len(pairs) < floor_count:
            logger.debug(
                "[video_analysis] %d scene frames for %.0f s video (floor=%d) "
                "— running fps=1/30 pass",
                len(pairs), duration, floor_count,
            )
            p2_pat = os.path.join(out_dir, 'fp_%05d.jpg')
            cmd_fps = [
                ff, '-hide_banner', '-i', video_path,
                '-vf', 'fps=1/30,showinfo',
                '-vsync', 'vfr', '-q:v', '3',
                p2_pat,
            ]
            r2 = subprocess.run(cmd_fps, capture_output=True, text=True, timeout=600,
                                creationflags=NO_WINDOW)
            pts2 = _parse_pts_times(r2.stderr)
            fp_files = sorted(glob.glob(os.path.join(out_dir, 'fp_*.jpg')))
            pairs.extend(zip(pts2, fp_files))

        # ---- Sort, dedup near-identical timestamps, cap ---------------------
        pairs.sort(key=lambda x: x[0])
        deduped: list[tuple[float, str]] = []
        last_ts = -_TS_DEDUP_GAP - 1
        for ts, fpath in pairs:
            if ts - last_ts >= _TS_DEDUP_GAP:
                deduped.append((ts, fpath))
                last_ts = ts

        if len(deduped) > max_frames:
            step = len(deduped) / max_frames
            deduped = [deduped[int(i * step)] for i in range(max_frames)]

        logger.info(
            "[video_analysis] %s → %d keyframes (thr=%.2f, max=%d)",
            os.path.basename(video_path), len(deduped), scene_threshold, max_frames,
        )
        return deduped

    except Exception:
        logger.exception("[video_analysis] extract_scene_keyframes failed")
        return []


# ---------------------------------------------------------------------------
# 3. analyze_video — main orchestrator
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


def analyze_video(
    db_path: str,
    transcription_id: int,
    force: bool = False,
    job=None,
    vision_backend: Optional[str] = None,
) -> dict:
    """Extract keyframes, OCR + vision-describe them, ingest into the RAG.

    Each keyframe gets two text layers, both optional and degrading gracefully:
      * OCR (Tesseract, $0) — literal on-screen text.
      * Vision description (Phase 23B) — "what is shown" from a VL model
        (local Qwen2.5-VL via Ollama by default, or Claude vision). See
        :mod:`app.services.video_vision`.
    A chunk is embedded into the RAG when EITHER layer has signal, so text-less
    but visual frames (photos, diagrams) also become searchable.

    Args:
        db_path:           Path to SQLite database.
        transcription_id:  Target transcription.
        force:             Re-analyse even if video_analysis_at is already set.
        job:               Optional job object supporting .is_cancelled().
        vision_backend:    Per-run override of VIDEO_VISION_BACKEND
                           ('local'|'claude'|'off'); None → config default.

    Returns dict:
        has_video    bool
        skipped      str  — reason (only present when returning early)
        keyframes    int  — rows in video_keyframes
        chunks_added int  — new rows in chunks

    Never raises — all errors are caught and logged.
    """
    def _cancelled() -> bool:
        try:
            return job is not None and job.is_cancelled()
        except Exception:
            logger.debug("job.is_cancelled() check failed, assuming not cancelled", exc_info=True)
            return False

    # ---- 1. Locate video ---------------------------------------------------
    info = resolve_recording_video(db_path, transcription_id)
    if info is None:
        return {'has_video': False, 'skipped': 'no video'}

    primary_video_path = info['primary_video_path']
    session_dir = info['session_dir']

    # ---- 2. Idempotency guard ----------------------------------------------
    try:
        with get_db_connection(db_path) as conn:
            tx = conn.execute(
                "SELECT video_analysis_at, video_keyframes_count "
                "FROM transcriptions WHERE id = ?",
                (transcription_id,),
            ).fetchone()
            if tx and tx['video_analysis_at'] and not force:
                return {
                    'has_video': True,
                    'skipped': 'already analyzed',
                    'keyframes': tx['video_keyframes_count'] or 0,
                }
    except Exception:
        logger.exception("[video_analysis] idempotency check failed")
        return {'has_video': False, 'skipped': 'db error'}

    # ---- 3. Clean prior data (force re-run) --------------------------------
    if force:
        try:
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "DELETE FROM video_keyframes WHERE transcription_id = ?",
                    (transcription_id,),
                )
                conn.execute(
                    "DELETE FROM chunks WHERE transcription_id = ? AND speaker = 'екран'",
                    (transcription_id,),
                )
                conn.commit()
        except Exception:
            logger.exception("[video_analysis] cleanup prior data failed (continuing)")

    if _cancelled():
        return {'has_video': True, 'skipped': 'cancelled'}

    # ---- 4. Read config values ---------------------------------------------
    cfg = _load_cfg()
    scene_thr = float(getattr(cfg, 'RECORDING_VIDEO_SCENE_THRESHOLD', 0.4)) if cfg else 0.4
    max_frames = int(getattr(cfg, 'RECORDING_VIDEO_ANALYSIS_MAX_FRAMES', 120)) if cfg else 120
    vision_max = int(getattr(cfg, 'VIDEO_VISION_MAX_FRAMES', 120)) if cfg else 120

    # ---- 5. Extract keyframes ----------------------------------------------
    kf_dir = os.path.join(session_dir, 'keyframes')
    frames = extract_scene_keyframes(
        primary_video_path, kf_dir,
        scene_threshold=scene_thr,
        max_frames=max_frames,
    )

    # ---- 6. OCR availability -----------------------------------------------
    from app.services import document_parser as _dp
    ocr_ok = _dp.ocr_available()
    if not ocr_ok:
        logger.warning(
            "[video_analysis] Tesseract unavailable — OCR text will be empty; "
            "chunks will not be added but keyframe rows will still be stored"
        )

    from app.services import embeddings as _emb
    emb_ok = _emb.is_available()

    # -- Vision (Phase 23B): "what is shown on screen" → RAG alongside OCR --
    from app.services import video_vision as _vv
    vision_model = _vv.active_model(vision_backend)
    vision_ok, vision_reason = _vv.availability(vision_backend)
    if vision_ok:
        logger.info("[video_analysis] vision backend ready: %s", vision_reason)
    else:
        logger.info("[video_analysis] vision off/unavailable (continuing OCR-only): %s",
                    vision_reason)

    # ---- 7. Process frames -------------------------------------------------
    keyframes_stored = 0
    chunks_added = 0
    vision_calls = 0
    vision_capped = False

    for ts, frame_path in frames:
        if _cancelled():
            logger.info("[video_analysis] job cancelled after %d frames", keyframes_stored)
            break

        # -- OCR --
        ocr_text = ''
        if ocr_ok:
            try:
                from PIL import Image
                with Image.open(frame_path) as img:
                    ocr_text = _dp._ocr_image_obj(img)
            except Exception:
                logger.debug(
                    "[video_analysis] OCR failed for %s", frame_path, exc_info=True
                )

        # -- Vision description (best-effort; '' if backend off/unavailable) --
        vision_text = ''
        if vision_ok:
            if vision_calls < vision_max:
                vision_text = _vv.describe_frame(frame_path, backend=vision_backend)
                vision_calls += 1
            elif not vision_capped:
                vision_capped = True
                logger.info(
                    "[video_analysis] vision cap reached (%d frames); remaining "
                    "frames are OCR-only (raise VIDEO_VISION_MAX_FRAMES)", vision_max,
                )

        # -- Store keyframe row --
        vis_clean = (vision_text or '').strip()
        try:
            with get_db_connection(db_path) as conn:
                conn.execute(
                    """INSERT INTO video_keyframes
                         (transcription_id, ts_offset_sec, image_path, ocr_text,
                          vision_text, vision_model, vision_at, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'scene')""",
                    (transcription_id, ts, frame_path, ocr_text or None,
                     vis_clean or None,
                     vision_model if vis_clean else None,
                     datetime.datetime.now().isoformat(' ', 'seconds') if vis_clean else None),
                )
                conn.commit()
            keyframes_stored += 1
        except Exception:
            logger.debug(
                "[video_analysis] keyframe row insert failed ts=%.2f", ts, exc_info=True
            )
            continue  # skip chunk too — frame not persisted

        # -- Embed + chunk: gate on OCR signal OR vision signal --
        clean_ocr = ocr_text.strip()
        has_ocr = len(re.sub(r'\s', '', clean_ocr)) >= _MIN_OCR_SIGNAL
        has_vis = len(re.sub(r'\s', '', vis_clean)) >= _MIN_VISION_SIGNAL
        if not (has_ocr or has_vis):
            continue

        # Combine layers so a single chunk carries both literal text and meaning.
        chunk_parts = []
        if has_ocr:
            chunk_parts.append(clean_ocr)
        if has_vis:
            chunk_parts.append(f'[опис кадру] {vis_clean}')
        chunk_text = '\n'.join(chunk_parts)

        try:
            embedding_blob: Optional[bytes] = None
            if emb_ok:
                vec = _emb.embed_texts([chunk_text])     # shape (1, 1024)
                embedding_blob = vec[0].astype(np.float32).tobytes()

            mm = int(ts // 60)
            ss = int(ts % 60)
            section = f'екран {mm:02d}:{ss:02d}'

            with get_db_connection(db_path) as conn:
                chunk_idx = _next_chunk_index(conn, transcription_id)
                conn.execute(
                    """INSERT OR IGNORE INTO chunks
                         (transcription_id, chunk_index, start_time, end_time,
                          speaker, text, embedding, section)
                       VALUES (?, ?, ?, ?, 'екран', ?, ?, ?)""",
                    (
                        transcription_id, chunk_idx,
                        ts, ts + 0.1,
                        chunk_text, embedding_blob,
                        section,
                    ),
                )
                conn.commit()
            chunks_added += 1

        except Exception:
            logger.debug(
                "[video_analysis] chunk insert failed ts=%.2f", ts, exc_info=True
            )

    # ---- 8. Update transcription marker ------------------------------------
    try:
        with get_db_connection(db_path) as conn:
            conn.execute(
                """UPDATE transcriptions
                      SET video_analysis_at = CURRENT_TIMESTAMP,
                          video_keyframes_count = ?
                    WHERE id = ?""",
                (keyframes_stored, transcription_id),
            )
            conn.commit()
    except Exception:
        logger.exception("[video_analysis] failed to update transcriptions marker")

    logger.info(
        "[video_analysis] tx=%d done — %d keyframes stored, %d chunks added, "
        "%d vision descriptions",
        transcription_id, keyframes_stored, chunks_added, vision_calls,
    )
    return {
        'has_video': True,
        'keyframes': keyframes_stored,
        'chunks_added': chunks_added,
        'vision_descriptions': vision_calls,
    }
