"""Screen video frames of a recording that is still running (spec live-video-frames, story 02).

Everything is computed on request from the growing fragmented MP4 that screen capture writes
(``+frag_keyframe+empty_moov+default_base_moof``); nothing is written to the session, the
manifest or the DB, and no thread is started.

* Readable edge — ``readable_until(path)`` walks the top-level MP4 boxes (stdlib ``struct``) and
  sums ``trun`` sample durations of every ``moof`` whose ``mdat`` is fully on disk. ffprobe's
  ``format=duration`` is never used: it over-reports a partially written file.
* Time axis — every ``t`` is a recording-timeline second (the live-transcript axis). A video
  track is a list of capture files (``segments``, plan C5: one per start/resume), each placed on
  the timeline by its own ``start_offset_sec``: ``video_t = t - start_offset_sec``. A legacy
  track without ``segments`` is one segment (``path`` + ``start_offset_sec``); other capture
  files of it on disk cannot be placed and are only counted (``segments_unmapped``).
* Frames — the frame on screen at ``t`` (the frame whose timestamp is the last one <= ``t``),
  secret boxes painted over and the OCR text masked (``video_timeline._live_secret_boxes``).

``store`` (SessionStore), ``service`` (RecordingService) and ``ffmpeg`` (binary path) are
injected, so the module does not need Flask or ``app.py``.
"""
from __future__ import annotations

import glob
import logging
import math
import os
import re
import struct
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)

MAX_FRAMES_PER_CALL = 12
LIVE_SCAN_MAX_THUMBS = 300
DEFAULT_FPS = 30.0
# a time up to this share of a frame before a frame's timestamp still selects that frame:
# absorbs millisecond rounding of `t` / `next_t` on the way through JSON and argv
_FRAME_TOLERANCE = 0.05
_STATUS_FINALIZED = 'finalized'


class LiveFramesError(Exception):
    """``code`` in NOT_FOUND | NO_VIDEO | INVALID_ARGUMENT | PAST_LIVE_EDGE | NOT_RECORDED."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------
# fragmented MP4: readable edge
# ---------------------------------------------------------------------------

def _boxes(buf: bytes, start: int = 0, end: Optional[int] = None):
    """(type, payload_start, box_end) of the boxes inside ``buf[start:end]``."""
    end = len(buf) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack('>I4s', buf[pos:pos + 8])
        head = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack('>Q', buf[pos + 8:pos + 16])[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            return
        yield kind.decode('latin-1'), pos + head, pos + size
        pos += size


def _child(buf: bytes, start: int, end: int, kind: str):
    for k, s, e in _boxes(buf, start, end):
        if k == kind:
            return s, e
    return None


def _parse_moov(moov: bytes) -> tuple[Optional[int], dict, dict]:
    """→ (video track id, {track_id: timescale}, {track_id: trex default_sample_duration})."""
    timescales: dict = {}
    trex: dict = {}
    video_id = None
    first_id = None
    for k, s, e in _boxes(moov):
        if k == 'trak':
            tkhd = _child(moov, s, e, 'tkhd')
            mdia = _child(moov, s, e, 'mdia')
            if not tkhd or not mdia:
                continue
            ver = moov[tkhd[0]]
            tid = struct.unpack('>I', moov[tkhd[0] + (20 if ver == 1 else 12):][:4])[0]
            mdhd = _child(moov, mdia[0], mdia[1], 'mdhd')
            if mdhd:
                mver = moov[mdhd[0]]
                off = mdhd[0] + (20 if mver == 1 else 12)
                timescales[tid] = struct.unpack('>I', moov[off:off + 4])[0]
            hdlr = _child(moov, mdia[0], mdia[1], 'hdlr')
            if first_id is None:
                first_id = tid
            if hdlr and moov[hdlr[0] + 8:hdlr[0] + 12] == b'vide' and video_id is None:
                video_id = tid
        elif k == 'mvex':
            for k2, s2, e2 in _boxes(moov, s, e):
                if k2 == 'trex' and e2 - s2 >= 24:
                    tid, _, dur = struct.unpack('>III', moov[s2 + 4:s2 + 16])
                    trex[tid] = dur
    return (video_id if video_id is not None else first_id), timescales, trex


def _parse_moof(moof: bytes, track_id: Optional[int], trex: dict):
    """→ (tfdt base or None, summed sample duration) of the track's ``traf`` in one moof."""
    for k, s, e in _boxes(moof):
        if k != 'traf':
            continue
        base = None
        total = 0
        default_dur = None
        tfhd = _child(moof, s, e, 'tfhd')
        if tfhd:
            flags = int.from_bytes(moof[tfhd[0] + 1:tfhd[0] + 4], 'big')
            tid = struct.unpack('>I', moof[tfhd[0] + 4:tfhd[0] + 8])[0]
            if track_id is not None and tid != track_id:
                continue
            off = tfhd[0] + 8
            if flags & 0x01:
                off += 8
            if flags & 0x02:
                off += 4
            if flags & 0x08:
                default_dur = struct.unpack('>I', moof[off:off + 4])[0]
            if default_dur is None:
                default_dur = trex.get(tid, 0)
        tfdt = _child(moof, s, e, 'tfdt')
        if tfdt:
            if moof[tfdt[0]] == 1:
                base = struct.unpack('>Q', moof[tfdt[0] + 4:tfdt[0] + 12])[0]
            else:
                base = struct.unpack('>I', moof[tfdt[0] + 4:tfdt[0] + 8])[0]
        for k2, s2, e2 in _boxes(moof, s, e):
            if k2 != 'trun':
                continue
            flags = int.from_bytes(moof[s2 + 1:s2 + 4], 'big')
            count = struct.unpack('>I', moof[s2 + 4:s2 + 8])[0]
            off = s2 + 8 + (4 if flags & 0x01 else 0) + (4 if flags & 0x04 else 0)
            if not flags & 0x100:
                total += count * (default_dur or 0)
                continue
            stride = 4 * sum(1 for f in (0x100, 0x200, 0x400, 0x800) if flags & f)
            for i in range(count):
                p = off + i * stride
                if p + 4 > e2:
                    break
                total += struct.unpack('>I', moof[p:p + 4])[0]
        return base, total
    return None, 0


def readable_until(path: str) -> Optional[float]:
    """Video-file seconds covered by complete fragments (``moof`` + its whole ``mdat``).

    ``None`` when the file is missing or has no complete fragment yet. Never reads ffprobe."""
    try:
        size = os.path.getsize(path)
        fh = open(path, 'rb')
    except OSError:
        return None
    video_id, timescales, trex = None, {}, {}
    first = None
    edge = None
    running = 0
    with fh:
        pos = 0
        pending = None          # (base, duration) of a moof waiting for its mdat
        while pos + 8 <= size:
            fh.seek(pos)
            head = fh.read(16)
            if len(head) < 8:
                break
            box_size, kind = struct.unpack('>I4s', head[:8])
            if box_size == 1:
                if len(head) < 16:
                    break
                box_size = struct.unpack('>Q', head[8:16])[0]
            elif box_size == 0:
                break               # "to end of file": still being written
            if box_size < 8 or pos + box_size > size:
                break               # the box is not fully on disk yet
            if kind == b'moov':
                fh.seek(pos)
                body = fh.read(box_size)
                video_id, timescales, trex = _parse_moov(body[8:])
            elif kind == b'moof':
                fh.seek(pos)
                body = fh.read(box_size)
                pending = _parse_moof(body[8:], video_id, trex)
            elif kind == b'mdat' and pending is not None:
                base, dur = pending
                pending = None
                if base is None:
                    base = running
                if first is None:
                    first = base
                running = base + dur
                edge = running if edge is None else max(edge, running)
            pos += box_size
    timescale = timescales.get(video_id) if video_id is not None else None
    if edge is None or not timescale:
        return None
    sec = (edge - first) / float(timescale)
    return round(sec, 4) if sec > 0 else None


# ---------------------------------------------------------------------------
# session, tracks, segments
# ---------------------------------------------------------------------------

def _segment_n(path: str) -> int:
    m = re.search(r'_seg(\d+)\.mp4$', os.path.basename(path or ''))
    return int(m.group(1)) if m else 0


def _track_segments(track: dict) -> list[dict]:
    """Track → [{n, path, start (timeline), readable_until (timeline) | None, video_until}]."""
    raw = track.get('segments')
    if not raw:
        raw = [{'n': _segment_n(track.get('path') or ''), 'path': track.get('path'),
                'start_offset_sec': track.get('start_offset_sec') or 0.0}]
    out = []
    for seg in raw:
        start = float(seg.get('start_offset_sec') or 0.0)
        ru = readable_until(seg.get('path') or '') if seg.get('path') else None
        out.append({'n': int(seg.get('n') or 0), 'path': seg.get('path'),
                    'start': round(start, 4), 'video_until': ru,
                    'readable_until': round(start + ru, 4) if ru is not None else None})
    return sorted(out, key=lambda s: s['start'])


def _unmapped(track: dict, segments: list[dict]) -> int:
    """Capture files of this monitor on disk that no segment places on the timeline."""
    path = track.get('path') or ''
    folder = os.path.dirname(path)
    if not folder or track.get('monitor_index') is None:
        return 0
    idx = int(track['monitor_index'])
    known = {os.path.normcase(os.path.abspath(s['path'])) for s in segments if s.get('path')}
    files = glob.glob(os.path.join(glob.escape(folder), f'video_mon{idx}.mp4'))
    files += glob.glob(os.path.join(glob.escape(folder), f'video_mon{idx}_seg*.mp4'))
    return sum(1 for f in files if os.path.normcase(os.path.abspath(f)) not in known)


def _track_edge(segments: list[dict]) -> Optional[float]:
    edges = [s['readable_until'] for s in segments if s['readable_until'] is not None]
    return max(edges) if edges else None


def _session(sid: str, store, service) -> dict:
    """→ {manifest, state, tracks, available, reason}. Unknown sid → NOT_FOUND."""
    try:
        manifest = store.read(sid)
    except Exception as e:     # SessionStoreError: bad id or no manifest
        logger.debug("live frames: session %r not readable: %s", sid, e)
        raise LiveFramesError('NOT_FOUND', f'session {sid} not found') from e
    state: dict = {}
    if service is not None:
        try:
            state = service.get_state(sid) or {}
        except Exception as e:  # SessionNotFoundError: the store had it, the service lost it
            logger.debug("live frames: get_state(%r) failed: %s", sid, e)
            raise LiveFramesError('NOT_FOUND', f'session {sid} not found') from e
    status = state.get('status') or manifest.get('status')
    is_active = bool(state.get('is_active'))
    reason = None
    if status == _STATUS_FINALIZED:
        reason = 'finalized'
    elif not is_active:
        reason = 'not_active'
    tracks = list((manifest.get('streams') or {}).get('video') or [])
    return {'manifest': manifest, 'state': state, 'status': status, 'is_active': is_active,
            'available': reason is None, 'reason': reason, 'tracks': tracks}


def _unavailable(sid: str, sess: dict) -> dict:
    return {'session_id': sid, 'available': False, 'reason': sess['reason'],
            'is_active': sess['is_active'], 'status': sess['status'],
            'elapsed_seconds': float(sess['state'].get('elapsed_seconds') or 0.0)}


def _pick_track(sess: dict, track: Optional[str]) -> dict:
    tracks = sess['tracks']
    if not tracks:
        raise LiveFramesError('NO_VIDEO', 'the session records no screen video')
    if track is None:
        if len(tracks) == 1:
            return tracks[0]
        ids = [t.get('track_id') for t in tracks]
        raise LiveFramesError('INVALID_ARGUMENT',
                              f'several video tracks, pass track= one of {ids}', track_ids=ids)
    for t in tracks:
        if t.get('track_id') == track:
            return t
    ids = [t.get('track_id') for t in tracks]
    raise LiveFramesError('INVALID_ARGUMENT', f'unknown track {track!r}, one of {ids}',
                          track_ids=ids)


def _fps(track: dict) -> float:
    try:
        fps = float(track.get('fps') or 0)
    except (TypeError, ValueError):
        fps = 0.0
    return fps if fps > 0 else DEFAULT_FPS


def _ranges(segments: list[dict]) -> list[list[float]]:
    return [[s['start'], s['readable_until']] for s in segments if s['readable_until'] is not None]


def _locate(t: float, track: dict, segments: list[dict]) -> dict:
    """The segment that holds timeline ``t`` or a typed refusal."""
    for seg in segments:
        if seg['readable_until'] is not None and seg['start'] <= t <= seg['readable_until']:
            return seg
    edge = _track_edge(segments)
    if edge is None or t > edge:
        raise LiveFramesError('PAST_LIVE_EDGE',
                              f't={t:.3f} is past the readable edge of {track.get("track_id")}',
                              readable_until=edge, track_id=track.get('track_id'))
    raise LiveFramesError('NOT_RECORDED', f't={t:.3f} is not in a recorded range',
                          readable_ranges=_ranges(segments), track_id=track.get('track_id'))


def _next_start(t: float, segments: list[dict]) -> Optional[dict]:
    """The first readable segment starting after ``t`` (to step over a pause)."""
    for seg in segments:
        if seg['readable_until'] is not None and seg['start'] > t:
            return seg
    return None


def _seek_time(video_t: float, fps: float, video_until: float) -> float:
    """Seek point that makes ffmpeg return the frame shown at ``video_t``.

    ffmpeg's accurate input seek returns the first frame whose timestamp is >= the seek point,
    so a time a hair past a frame's timestamp (millisecond rounding) would skip to the next
    frame. Snap to the frame index and seek a quarter frame before it."""
    last = max(0, int(math.ceil(video_until * fps - 1e-6)) - 1)
    k = min(last, max(0, int(math.floor(video_t * fps + _FRAME_TOLERANCE))))
    return max(0.0, (k - 0.25) / fps)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def status(sid: str, *, store, service) -> dict:
    """Recorded monitors, their fps and the readable edge of each track."""
    sess = _session(sid, store, service)
    if not sess['available']:
        return {**_unavailable(sid, sess), 'tracks': []}
    if not sess['tracks']:
        raise LiveFramesError('NO_VIDEO', 'the session records no screen video')
    tracks = []
    for t in sess['tracks']:
        segs = _track_segments(t)
        current = segs[-1] if segs else {'n': 0}
        tracks.append({
            'track_id': t.get('track_id'), 'monitor_index': t.get('monitor_index'),
            'monitor_label': t.get('monitor_label'), 'fps': _fps(t),
            'width': t.get('width'), 'height': t.get('height'),
            'start_offset_sec': t.get('start_offset_sec'),
            'readable_until': _track_edge(segs),
            'segment': current['n'],
            'segments': [{'n': s['n'], 'start': s['start'], 'readable_until': s['readable_until']}
                         for s in segs],
            'segments_unmapped': _unmapped(t, segs),
            'status': t.get('status'),
        })
    return {'session_id': sid, 'available': True, 'reason': None,
            'is_active': sess['is_active'], 'status': sess['status'],
            'elapsed_seconds': float(sess['state'].get('elapsed_seconds') or 0.0),
            'tracks': tracks}


def _render(track: dict, seg: dict, t: float, size: str, ffmpeg: Optional[str]) -> dict:
    from app.services import screen_secrets as ss
    from app.services import video_frames as vf
    from app.services import video_timeline as vt
    video_t = t - seg['start']
    seek = _seek_time(video_t, _fps(track), seg['video_until'])
    img = vf.grab_frame(seg['path'], seek, max_width=vt.SIZES.get(size, vt.SIZES['thumb']),
                        ffmpeg=ffmpeg)
    boxes, w, h, text = vt._live_secret_boxes(seg['path'], seek, ffmpeg)
    img = ss.redact(img, boxes, w, h)
    buf = BytesIO()
    img.save(buf, 'JPEG', quality=80)
    return {'t': round(t, 4), 'video_t': round(video_t, 4), 'track_id': track.get('track_id'),
            'segment': seg['n'], 'width': img.width, 'height': img.height,
            'jpeg': buf.getvalue(), 'text': text, 'ocr': text is not None}


def frame_at(sid: str, t: float, *, track: Optional[str] = None, size: str = 'thumb',
             store, service, ffmpeg: Optional[str] = None) -> dict:
    """The masked frame on screen at timeline ``t`` + its masked OCR text.

    A session that is no longer live → ``{available: False, reason, ...}`` instead of a frame."""
    sess = _session(sid, store, service)
    if not sess['available']:
        return _unavailable(sid, sess)
    tr_ = _pick_track(sess, track)
    t = float(t)
    if t < 0 or math.isnan(t):
        raise LiveFramesError('INVALID_ARGUMENT', 't must be >= 0')
    segs = _track_segments(tr_)
    seg = _locate(t, tr_, segs)
    return _render(tr_, seg, t, size, ffmpeg)


def frames_range(sid: str, start: Optional[float], end: Optional[float] = None, *,
                 mode: str = 'step', step: float = 1.0, min_res: float = 1.0, limit: int = 6,
                 cursor: Optional[float] = None, track: Optional[str] = None,
                 size: str = 'thumb', store, service, ffmpeg: Optional[str] = None) -> dict:
    """Frames in [start, end] as a step or as screen changes only, at most 12 per call.

    ``cursor`` (a previous ``next_t``) replaces ``start``; ``end`` past the readable edge is
    clamped (``end`` in the answer); ``next_t`` is where the next call continues, ``None`` when
    the requested range is fully covered."""
    sess = _session(sid, store, service)
    if not sess['available']:
        return {**_unavailable(sid, sess), 'frames': [], 'count': 0, 'next_t': None,
                'readable_until': None, 'scanned_until': None, 'truncated': False}
    tr_ = _pick_track(sess, track)
    fps = _fps(tr_)
    if mode not in ('step', 'changes'):
        raise LiveFramesError('INVALID_ARGUMENT', f'unknown mode {mode!r}')
    try:
        limit = max(1, min(int(limit or 6), MAX_FRAMES_PER_CALL))
        step = float(step)
        min_res = float(min_res)
        begin = float(cursor if cursor is not None else (start or 0.0))
    except (TypeError, ValueError) as e:
        raise LiveFramesError('INVALID_ARGUMENT', f'bad range argument: {e}') from e
    tol = 1e-6
    if mode == 'step' and step < 1.0 / fps - tol:
        raise LiveFramesError('INVALID_ARGUMENT', f'step must be >= 1/fps ({1.0 / fps:.4f} s)')
    if mode == 'changes' and min_res < 1.0 / fps - tol:
        raise LiveFramesError('INVALID_ARGUMENT', f'min_res must be >= 1/fps ({1.0 / fps:.4f} s)')
    if begin < 0:
        raise LiveFramesError('INVALID_ARGUMENT', 'start must be >= 0')
    segs = _track_segments(tr_)
    edge = _track_edge(segs)
    if edge is None or begin > edge:
        raise LiveFramesError('PAST_LIVE_EDGE',
                              f'start={begin:.3f} is past the readable edge',
                              readable_until=edge, track_id=tr_.get('track_id'))
    want_end = float(end) if end is not None else edge
    if want_end < begin:
        raise LiveFramesError('INVALID_ARGUMENT', 'end must be >= start')
    clamped = want_end > edge
    stop = min(want_end, edge)
    base = {'session_id': sid, 'available': True, 'track_id': tr_.get('track_id'), 'mode': mode,
            'start': round(begin, 4), 'end': round(stop, 4), 'end_clamped': clamped,
            'readable_until': edge}
    if mode == 'step':
        return {**base, **_range_step(tr_, segs, begin, stop, step, limit, clamped, size, ffmpeg)}
    return {**base, **_range_changes(tr_, segs, begin, stop, min_res, limit, clamped, size,
                                     ffmpeg)}


def _covering(t: float, segs: list[dict]) -> Optional[dict]:
    for seg in segs:
        if seg['readable_until'] is not None and seg['start'] <= t <= seg['readable_until']:
            return seg
    return None


def _range_step(track, segs, begin, stop, step, limit, clamped, size, ffmpeg) -> dict:
    frames = []
    t = begin
    k = 0
    next_t = None
    while t <= stop + 1e-9:
        seg = _covering(t, segs)
        if seg is None:                 # inside a pause: continue at the next segment
            nxt = _next_start(t, segs)
            if nxt is None or nxt['start'] > stop:
                break
            begin, k, t = nxt['start'], 0, nxt['start']
            continue
        if len(frames) >= limit:
            next_t = t
            break
        frames.append(_render(track, seg, t, size, ffmpeg))
        k += 1
        t = begin + k * step
    truncated = next_t is not None
    if next_t is None and clamped:
        next_t = t                      # the live edge moves on: continue from here later
    last = frames[-1]['t'] if frames else None
    return {'frames': frames, 'count': len(frames), 'next_t': next_t,
            'scanned_until': last, 'truncated': truncated}


def _range_changes(track, segs, begin, stop, min_res, limit, clamped, size, ffmpeg) -> dict:
    from app.services import video_frames as vf
    seg = _covering(begin, segs)
    if seg is not None and begin >= seg['readable_until'] - 1e-6 \
            and _next_start(begin, segs) is not None:
        seg = None                      # cursor at the end of a segment before a pause: cross it
    if seg is None:
        seg = _next_start(begin, segs)
        if seg is None or seg['start'] > stop:
            return {'frames': [], 'count': 0, 'next_t': (stop if clamped else None),
                    'scanned_until': stop, 'truncated': False}
        begin = seg['start']
    win_end = min(stop, seg['readable_until'], begin + LIVE_SCAN_MAX_THUMBS * min_res)
    rate = 1.0 / min_res
    video_start = begin - seg['start']
    duration = max(win_end - begin, 0.0)
    try:
        thumbs = vf.thumbs_pass(seg['path'], duration, fps=rate, ffmpeg=ffmpeg,
                                start=video_start, window=duration + 1e-3)
    except vf.NoVideoError:
        logger.debug("live frames: no thumbnails in [%.3f, %.3f]", begin, win_end, exc_info=True)
        thumbs = None
    changes: list = []
    scanned = begin
    if thumbs is not None and len(thumbs):
        thumbs = thumbs[:LIVE_SCAN_MAX_THUMBS]
        scanned = begin + (len(thumbs) - 1) * min_res
        mask = vf.region_mask(None) & vf.flicker_mask(thumbs)
        segs_found = vf.select_thumbs(thumbs, (len(thumbs)) * min_res, fps=rate,
                                      threshold=vf.DEFAULT_THRESHOLD, mask=mask,
                                      settle=vf.DEFAULT_SETTLE)
        changes = [s for s in segs_found if s.change_score > 0]
    frames = []
    next_t = None
    for i, ch in enumerate(changes):
        if len(frames) >= limit:
            # the next call anchors on the thumbnail before this change and sees it again
            next_t = round(begin + ch.start - min_res, 4)
            break
        change_t = begin + ch.start
        rep = min(begin + ch.frame_time, begin + ch.end, scanned)
        item = _render(track, seg, max(rep, change_t), size, ffmpeg)
        item['change_t'] = round(change_t, 4)
        item['change_score'] = ch.change_score
        frames.append(item)
    truncated = next_t is not None
    if next_t is None:
        after = _next_start(scanned, segs)
        more_in_range = scanned < stop - 1e-6 or (after is not None and after['start'] <= stop)
        if more_in_range or clamped:
            # a window shorter than one thumbnail step must still move the cursor on
            next_t = round(scanned if scanned > begin else win_end, 4)
    return {'frames': frames, 'count': len(frames), 'next_t': next_t,
            'scanned_until': round(scanned, 4), 'truncated': truncated}
