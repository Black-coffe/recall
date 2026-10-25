"""Adaptive selection of keyframes of a video recording (spec video-visual-timeline, story 01).

The task: a minimal set of frames that covers every change of picture to within
1 s. Two methods (both measured, numbers in docs/video-timeline.md):

* ``thumbs`` (default) — ONE decoding pass at 1 fps into 160×90 grayscale thumbnails
  through an ffmpeg rawvideo pipe. Each thumbnail is compared with the ANCHOR of the current
  segment (the segment's first thumbnail), not only with the previous one — this catches both
  a sharp change, and slow drift (scrolling), and A→B→A shorter than the step.
* ``bisect`` — anchor frames every ``step`` s via seek (``-ss`` before ``-i``),
  an interval with different ends is halved down to ``min_res``. Cheaper on a very
  static video, but misses A→B→A shorter than ``step`` (the interval's ends
  coincide) — this is a documented limit of the method.

Comparison ``diff_score(a, b, mask)``: the share of mask pixels that changed by more than
``PIX_DELTA`` (out of 255). Mask = ``ignore_regions`` (fractions of the frame) minus an auto-mask
of pixels that flicker constantly (tray clock, webcam tiles, cursor).

Frame images are NOT stored: ``grab_frame`` cuts a frame from the video on demand.
The module knows nothing about the DB — ``video_analysis`` does that.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, asdict
from io import BytesIO
from typing import Callable, Optional, Sequence

import numpy as np

from app.utils.proc import NO_WINDOW

logger = logging.getLogger(__name__)

ALGO_VERSION = 1

THUMB_W, THUMB_H = 160, 90
PIX_DELTA = 12                 # |Δ| of brightness from which a pixel counts as changed
DEFAULT_THRESHOLD = 0.02       # share of changed mask pixels (measured on 6 recordings, docs/video-timeline.md)
DEFAULT_STEP = 10.0
DEFAULT_MIN_RES = 1.0
DEFAULT_MAX_FRAMES = 1500
DEFAULT_METHOD = 'thumbs'
DEFAULT_SETTLE = 2.0           # a series of changes more often than every 2 s (typing, scrolling) — one segment
FLICKER_SHARE = 0.4            # a pixel changed in >40 % of pairs is noise, not content
REP_OFFSET = 0.7               # segment representative: +0.7 s from the start
BLANK_STD = 4.0                # thumbnail std below this — blank frame


class NoVideoError(Exception):
    """The file has no video track (or is not readable as video)."""


@dataclass
class Segment:
    start: float
    end: float
    frame_time: float
    change_score: float          # score of the change that OPENED the segment (0 for the first)
    std: float = 0.0             # contrast of the representative's thumbnail (for the blank type)
    dhash: str = ''              # 64-bit dHash of the representative (hex) — for repeats

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def _ffmpeg(ffmpeg: Optional[str] = None) -> str:
    if ffmpeg:
        return ffmpeg
    try:
        from app.services.recording.video_probe import ffmpeg_path
        return ffmpeg_path()
    except Exception:
        logger.debug("ffmpeg_path() unavailable, taking ffmpeg from PATH", exc_info=True)
        return 'ffmpeg'


def _ffprobe(ff: str) -> str:
    cand = os.path.join(os.path.dirname(ff), 'ffprobe.exe')
    return cand if os.path.dirname(ff) and os.path.isfile(cand) else 'ffprobe'


def probe(video_path: str, ffmpeg: Optional[str] = None) -> dict:
    """Duration, size and fps of the first video track. No video → NoVideoError."""
    if not os.path.isfile(video_path):
        raise NoVideoError(f'file not found: {video_path}')
    ff = _ffmpeg(ffmpeg)
    try:
        r = subprocess.run(
            [_ffprobe(ff), '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,avg_frame_rate:format=duration',
             '-of', 'json', video_path],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW,
        )
        data = json.loads(r.stdout or '{}')
    except Exception as e:
        raise NoVideoError(f'ffprobe could not read the file: {e}') from e
    streams = data.get('streams') or []
    if not streams:
        raise NoVideoError('the file has no video track')
    s = streams[0]
    try:
        num, den = (s.get('avg_frame_rate') or '0/1').split('/')
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        logger.debug("avg_frame_rate not parsed: %r", s.get('avg_frame_rate'), exc_info=True)
        fps = 0.0
    duration = float((data.get('format') or {}).get('duration') or 0.0)
    if duration <= 0:
        raise NoVideoError('video duration is unknown or zero')
    return {'duration': duration, 'width': int(s.get('width') or 0),
            'height': int(s.get('height') or 0), 'fps': fps}


def _run_raw(cmd: list, timeout: float) -> bytes:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError((r.stderr or b'')[-300:].decode('utf-8', 'replace'))
    return r.stdout


def grab_small(video_path: str, t: float, *, w: int = THUMB_W, h: int = THUMB_H,
               ffmpeg: Optional[str] = None) -> Optional[np.ndarray]:
    """One frame at moment ``t`` as a grayscale h×w array (fast seek). None — there is no frame."""
    ff = _ffmpeg(ffmpeg)
    raw = _run_raw([ff, '-v', 'error', '-ss', f'{max(t, 0):.3f}', '-i', video_path,
                    '-frames:v', '1', '-vf', f'scale={w}:{h}:flags=area,format=gray',
                    '-f', 'rawvideo', '-'], timeout=60)
    if len(raw) < w * h:
        return None
    return np.frombuffer(raw[:w * h], dtype=np.uint8).reshape(h, w)


def grab_frame(video_path: str, t: float, *, max_width: Optional[int] = None,
               ffmpeg: Optional[str] = None):
    """Frame at moment ``t`` as PIL.Image (RGB). Nothing is written to disk."""
    from PIL import Image
    ff = _ffmpeg(ffmpeg)
    vf = f"scale='min({int(max_width)},iw)':-2" if max_width else 'null'
    raw = _run_raw([ff, '-v', 'error', '-ss', f'{max(t, 0):.3f}', '-i', video_path,
                    '-frames:v', '1', '-vf', vf, '-f', 'image2pipe', '-vcodec', 'png', '-'],
                   timeout=60)
    if not raw:
        raise NoVideoError(f'frame at {t:.1f} s could not be decoded')
    img = Image.open(BytesIO(raw))
    img.load()
    return img.convert('RGB')


def thumbs_pass(video_path: str, duration: float, *, fps: float = 1.0,
                ffmpeg: Optional[str] = None,
                progress: Optional[Callable[[float], None]] = None,
                start: Optional[float] = None,
                window: Optional[float] = None) -> np.ndarray:
    """One decoding pass: thumbnails (N, THUMB_H, THUMB_W) uint8, thumbnail i = moment i/fps.

    ``start``/``window`` bound the pass to ``[start, start + window]`` of the video (live frames
    read a growing file in slices); thumbnail i is then the moment ``start + i/fps``."""
    ff = _ffmpeg(ffmpeg)
    frame_bytes = THUMB_W * THUMB_H
    bound: list = []
    if start is not None:
        bound += ['-ss', f'{max(start, 0):.3f}']
    if window is not None:
        bound += ['-t', f'{max(window, 0):.3f}']
    cmd = [ff, '-v', 'error', *bound, '-i', video_path,
           '-vf', f'fps={fps},scale={THUMB_W}:{THUMB_H}:flags=area,format=gray',
           '-f', 'rawvideo', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    chunks: list[bytes] = []
    expected = max(1, int(duration * fps))
    got = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes * 64)
            if not buf:
                break
            chunks.append(buf)
            got += len(buf) // frame_bytes
            if progress:
                progress(min(1.0, got / expected))
    finally:
        proc.wait(timeout=60)
    data = b''.join(chunks)
    n = len(data) // frame_bytes
    if n == 0:
        raise NoVideoError('ffmpeg returned no frames')
    return np.frombuffer(data[:n * frame_bytes], dtype=np.uint8).reshape(n, THUMB_H, THUMB_W)


# ---------------------------------------------------------------------------
# comparison and masks
# ---------------------------------------------------------------------------

def region_mask(ignore_regions: Optional[Sequence[Sequence[float]]],
                h: int = THUMB_H, w: int = THUMB_W) -> np.ndarray:
    """True = the pixel takes part in the comparison. Regions are fractions of the frame [x, y, w, h]."""
    mask = np.ones((h, w), dtype=bool)
    for reg in ignore_regions or ():
        x, y, rw, rh = (float(v) for v in reg)
        x0, y0 = int(round(x * w)), int(round(y * h))
        x1, y1 = int(round((x + rw) * w)), int(round((y + rh) * h))
        mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = False
    return mask


def flicker_mask(thumbs: np.ndarray, share: float = FLICKER_SHARE) -> np.ndarray:
    """False for pixels that change in more than ``share`` of neighbouring pairs (noise, not content)."""
    if len(thumbs) < 3:
        return np.ones(thumbs.shape[1:], dtype=bool)
    changed = np.zeros(thumbs.shape[1:], dtype=np.int32)
    for i in range(1, len(thumbs)):
        changed += np.abs(thumbs[i].astype(np.int16) - thumbs[i - 1]) > PIX_DELTA
    return changed < share * (len(thumbs) - 1)


def diff_score(a: np.ndarray, b: np.ndarray, mask: Optional[np.ndarray] = None) -> float:
    """Share of mask pixels changed by more than PIX_DELTA."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIX_DELTA
    if mask is not None:
        n = int(mask.sum())
        return float((d & mask).sum()) / n if n else 0.0
    return float(d.mean())


def same(a: np.ndarray, b: np.ndarray, mask: Optional[np.ndarray] = None,
         threshold: float = DEFAULT_THRESHOLD) -> bool:
    return diff_score(a, b, mask) <= threshold


def dhash(img: np.ndarray) -> str:
    """64-bit dHash (9×8) — for repeats by content between non-adjacent segments."""
    from PIL import Image
    small = np.asarray(Image.fromarray(img).resize((9, 8), Image.BILINEAR), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return f'{int("".join("1" if b else "0" for b in bits), 2):016x}'


def hamming(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count('1')


def sharpness(img: np.ndarray) -> float:
    """Variance of the discrete Laplacian — larger = sharper."""
    f = img.astype(np.float32)
    lap = (f[1:-1, :-2] + f[1:-1, 2:] + f[:-2, 1:-1] + f[2:, 1:-1] - 4 * f[1:-1, 1:-1])
    return float(lap.var())


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def _settle(bounds: list[tuple[float, float]], settle: float) -> list[tuple[float, float, float]]:
    """A series of changes coming more often than every ``settle`` s (typing, scrolling)
    — one segment. → [(start, score, moment, when the series settled)]."""
    out: list[tuple[float, float, float]] = []
    for t, score in bounds:
        if out and settle > 0 and score > 0 and t - out[-1][2] < settle:
            start, sc, _ = out[-1]
            out[-1] = (start, max(sc, score), t)
        else:
            out.append((t, score, t))
    return out


def _segments_from_boundaries(bounds: list[tuple[float, float]], duration: float,
                              settle: float = 0.0) -> list[Segment]:
    """bounds — [(change moment, score)], the first (0, 0). → segments up to the next boundary.

    A segment boundary is the moment of the series' FIRST change (accuracy ``min_res``), the representative is
    the frame after the series' LAST change, i.e. an already settled screen."""
    runs = _settle(bounds, settle)
    segs: list[Segment] = []
    for i, (t, score, settled) in enumerate(runs):
        end = runs[i + 1][0] if i + 1 < len(runs) else duration
        if end - t <= 0:
            continue
        segs.append(Segment(start=round(t, 3), end=round(end, 3),
                            frame_time=round(min(settled + REP_OFFSET, (settled + end) / 2), 3),
                            change_score=round(score, 5)))
    return segs


def select_thumbs(thumbs: np.ndarray, duration: float, *, fps: float = 1.0,
                  threshold: float = DEFAULT_THRESHOLD,
                  mask: Optional[np.ndarray] = None,
                  settle: float = DEFAULT_SETTLE) -> list[Segment]:
    """Segments from thumbnails: a new boundary when the frame differs from the segment's ANCHOR."""
    bounds: list[tuple[float, float]] = [(0.0, 0.0)]
    anchor = thumbs[0]
    for i in range(1, len(thumbs)):
        score = diff_score(thumbs[i], anchor, mask)
        if score > threshold:
            bounds.append((i / fps, score))
            anchor = thumbs[i]
    segs = _segments_from_boundaries(bounds, duration, settle)
    for s in segs:
        idx = min(len(thumbs) - 1, int(s.frame_time * fps))
        s.std = round(float(thumbs[idx].std()), 2)
        s.dhash = dhash(thumbs[idx])
    return segs


def select_bisect(video_path: str, duration: float, *, step: float = DEFAULT_STEP,
                  min_res: float = DEFAULT_MIN_RES, threshold: float = DEFAULT_THRESHOLD,
                  mask: Optional[np.ndarray] = None, ffmpeg: Optional[str] = None,
                  progress: Optional[Callable[[float], None]] = None,
                  settle: float = DEFAULT_SETTLE) -> list[Segment]:
    """Bisection: anchors every ``step`` s, different ends → halve down to ``min_res``."""
    cache: dict[float, np.ndarray] = {}

    def frame(t: float) -> np.ndarray:
        t = round(min(max(t, 0.0), max(duration - 0.05, 0.0)), 3)
        if t not in cache:
            img = grab_small(video_path, t, ffmpeg=ffmpeg)
            if img is None:
                img = cache[max(cache)] if cache else np.zeros((THUMB_H, THUMB_W), np.uint8)
            cache[t] = img
        return cache[t]

    bounds: list[tuple[float, float]] = [(0.0, 0.0)]

    def split(t1: float, t2: float) -> None:
        score = diff_score(frame(t1), frame(t2), mask)
        if score <= threshold:
            return
        if t2 - t1 <= min_res:
            bounds.append((t2, score))
            return
        tm = (t1 + t2) / 2
        split(t1, tm)
        split(tm, t2)

    anchors = list(np.arange(0.0, duration, step)) + [max(duration - 0.05, 0.0)]
    for i in range(1, len(anchors)):
        split(float(anchors[i - 1]), float(anchors[i]))
        if progress:
            progress(i / (len(anchors) - 1))
    bounds.sort()
    segs = _segments_from_boundaries(bounds, duration, settle)
    for s in segs:
        img = frame(s.frame_time)
        s.std = round(float(img.std()), 2)
        s.dhash = dhash(img)
    return segs


def cap_segments(segs: list[Segment], max_frames: int) -> tuple[list[Segment], bool]:
    """Beyond ``max_frames`` — merge the shortest segment with a neighbour (protection against a "blinking" video)."""
    if len(segs) <= max_frames:
        return segs, False
    segs = list(segs)
    while len(segs) > max_frames:
        i = min(range(len(segs)), key=lambda k: segs[k].end - segs[k].start)
        j = i - 1 if i > 0 else i + 1
        a, b = sorted((i, j))
        merged = Segment(start=segs[a].start, end=segs[b].end, frame_time=segs[a].frame_time,
                         change_score=segs[a].change_score, std=segs[a].std, dhash=segs[a].dhash)
        segs[a:b + 1] = [merged]
    return segs, True


def mark_duplicates(segs: list[Segment], max_bits: int = 4) -> list[Optional[int]]:
    """For each segment — the index of the first NON-adjacent segment with the same screen, or None."""
    out: list[Optional[int]] = [None] * len(segs)
    for i, s in enumerate(segs):
        if not s.dhash:
            continue
        for j in range(0, i - 1):
            if out[j] is None and segs[j].dhash and hamming(s.dhash, segs[j].dhash) <= max_bits:
                out[i] = j
                break
    return out


def pick_sharpest(video_path: str, seg: Segment, *, ffmpeg: Optional[str] = None) -> float:
    """Segment ≥ 3 s: the sharpest of 3 candidates (+0.7 s, +1.5 s, the middle)."""
    if seg.end - seg.start < 3.0:
        return seg.frame_time
    cands = [seg.start + REP_OFFSET, seg.start + 1.5, (seg.start + seg.end) / 2]
    best, best_v = seg.frame_time, -1.0
    for t in cands:
        img = grab_small(video_path, t, w=320, h=180, ffmpeg=ffmpeg)
        if img is None:
            continue
        v = sharpness(img)
        if v > best_v:
            best, best_v = t, v
    return round(best, 3)


def select_keyframes(video_path: str, *, method: str = DEFAULT_METHOD,
                     step: float = DEFAULT_STEP, min_res: float = DEFAULT_MIN_RES,
                     threshold: float = DEFAULT_THRESHOLD,
                     ignore_regions: Optional[Sequence[Sequence[float]]] = None,
                     max_frames: int = DEFAULT_MAX_FRAMES,
                     settle: float = DEFAULT_SETTLE,
                     cache_dir: Optional[str] = None,
                     ffmpeg: Optional[str] = None,
                     progress: Optional[Callable[[float], None]] = None) -> dict:
    """Main entry. Returns {'segments': [Segment], 'duration', 'capped', 'method'}.

    ``cache_dir`` — a directory for the thumbnail cache (``thumbs.npz``): a repeated call with
    a different threshold does not decode the video a second time."""
    info = probe(video_path, ffmpeg)
    duration = info['duration']
    rmask = region_mask(ignore_regions)
    if method == 'bisect':
        segs = select_bisect(video_path, duration, step=step, min_res=min_res,
                             threshold=threshold, mask=rmask, ffmpeg=ffmpeg, progress=progress,
                             settle=settle)
    elif method == 'thumbs':
        thumbs = None
        cache = os.path.join(cache_dir, 'thumbs.npz') if cache_dir else None
        if cache and os.path.isfile(cache):
            try:
                with np.load(cache) as z:
                    # the cache is valid only for the same duration AND decoding frequency
                    if (float(z['duration']) == duration and 'fps' in z.files
                            and float(z['fps']) == 1.0 / min_res):
                        thumbs = z['thumbs']
            except Exception:
                logger.warning("thumbnail cache %s is corrupted — decoding again", cache, exc_info=True)
        if thumbs is None:
            thumbs = thumbs_pass(video_path, duration, fps=1.0 / min_res, ffmpeg=ffmpeg,
                                 progress=progress)
            if cache:
                try:
                    os.makedirs(cache_dir, exist_ok=True)
                    np.savez_compressed(cache, thumbs=thumbs, duration=duration, fps=1.0 / min_res)
                except Exception:
                    logger.warning("thumbnail cache not written: %s", cache, exc_info=True)
        elif progress:
            progress(1.0)
        mask = rmask & flicker_mask(thumbs)
        segs = select_thumbs(thumbs, duration, fps=1.0 / min_res, threshold=threshold, mask=mask,
                             settle=settle)
    else:
        raise ValueError(f'unknown method: {method}')
    segs, capped = cap_segments(segs, max_frames)
    return {'segments': segs, 'duration': duration, 'capped': capped, 'method': method,
            'width': info['width'], 'height': info['height']}


def classify(seg: Segment, *, ocr_chars: int, ocr_lines: int, mean_word_h: float,
             frame_h: int, min_res: float = DEFAULT_MIN_RES, ocr_ran: bool = True) -> str:
    """Heuristic frame type (without a model). Known errors — docs/video-timeline.md.

    ``ocr_ran=False`` (OCR disabled or unavailable): the text is unknown, so the frame is not
    declared "camera only" — otherwise the timeline would hide everything by default."""
    if seg.std < BLANK_STD and ocr_chars < 15:     # with text the frame is not blank
        return 'blank'
    if seg.end - seg.start < 2 * min_res and seg.change_score > 0:
        return 'transition'
    if not ocr_ran:
        return 'screen_share'
    if ocr_chars < 15:
        return 'camera_only'
    if ocr_lines <= 12 and frame_h and mean_word_h / frame_h >= 0.025:
        return 'slide'
    return 'screen_share'
