"""Visual timeline of a recording: frame + phrases over the same interval (spec video-visual-timeline, story 03).

Reads what ``video_analysis.analyze_video`` wrote (``video_keyframes``) and the
transcript (``transcriptions.segments``). Frame images are not stored:
``render_frame`` cuts a frame from the video on demand and paints over secret boxes
(``screen_secrets``). If the recording has not been analyzed yet, there are no boxes — then the frame is
OCR-ed on the fly so that a secret does not go out.

The text here is already masked (story 02); this module does not read raw secrets.
"""
from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from typing import Optional

from app.core.i18n import tr
from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

HIDDEN_TYPES = ('camera_only', 'blank')
SIZES = {'thumb': 960, 'full': 1920}
MAX_FRAMES_PER_CALL = 12


class TimelineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _hms(t: float) -> str:
    t = max(0.0, float(t or 0))
    return f'{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}'


def _load(conn, tid: int):
    tx = conn.execute("SELECT id, source_name, title, segments, video_analysis_at, "
                      "video_keyframes_count, video_analysis_params "
                      "FROM transcriptions WHERE id = ?", (tid,)).fetchone()
    if not tx:
        raise TimelineError('NOT_FOUND', f'recording #{tid} not found')
    rows = conn.execute(
        "SELECT id, ts_offset_sec, end_sec, video_time_sec, frame_type, duplicate_of, "
        "change_score, ocr_text, vision_text, vision_model, ocr_boxes_json "
        "FROM video_keyframes WHERE transcription_id = ? ORDER BY ts_offset_sec", (tid,)).fetchall()
    try:
        segs = json.loads(tx['segments'] or '[]')
    except ValueError:
        logger.warning("segments of recording #%s is not JSON — timeline without transcript", tid)
        segs = []
    return tx, rows, segs


def _transcript_for(segs: list, start: float, end: float, shift: float) -> list:
    """Transcript segments that intersect [start, end] (frame time + shift)."""
    a, b = start + shift, end + shift
    out = []
    for s in segs:
        ss, se = float(s.get('start') or 0), float(s.get('end') or 0)
        if se > a and ss < b:
            out.append({'speaker': s.get('speaker'), 'start': round(ss, 2), 'end': round(se, 2),
                        'text': (s.get('text') or '').strip()})
    return out


def _item(tid: int, r, segs: list, shift: float, with_transcript: bool,
          track_offset: float = 0.0) -> dict:
    start = float(r['ts_offset_sec'] or 0)
    end = float(r['end_sec'] if r['end_sec'] is not None else start)
    # the representative's time in the timeline = time in the video + the track's shift
    frame_time = start if r['video_time_sec'] is None else \
        float(r['video_time_sec']) + track_offset
    item = {
        'segment_id': f'{tid}:{r["id"]}',
        'keyframe_id': r['id'],
        'start': round(start, 2), 'end': round(end, 2),
        'frame_time': round(frame_time, 2),
        'frame_url': f'/api/transcription/{tid}/frame?t={frame_time:.2f}',
        'frame_type': r['frame_type'],
        'ocr_text': r['ocr_text'] or '',
        'description': r['vision_text'] or None,
        'duplicate_of': r['duplicate_of'],
        'change_score': r['change_score'],
    }
    if with_transcript:
        item['transcript'] = _transcript_for(segs, start, end, shift)
    return item


def _track_offset(db_path: str, tid: int) -> float:
    from app.services import video_analysis as va
    info = va.resolve_recording_video(db_path, tid)
    return float((info or {}).get('start_offset_sec') or 0.0)


def build(db_path: str, tid: int, *, start: Optional[float] = None, end: Optional[float] = None,
          with_transcript: bool = True, include_camera: bool = False, shift: float = 0.0,
          limit: int = 50, offset: int = 0) -> dict:
    """Paginated timeline. ``shift`` is the transcript's shift relative to the video (s)."""
    with get_db_connection(db_path) as conn:
        tx, rows, segs = _load(conn, tid)
    if not tx['video_analysis_at']:
        return {'transcription_id': tid, 'status': 'not_analyzed', 'total': 0, 'items': [],
                'hint': tr('svc_pipes.timeline.hint.notAnalyzed')}
    picked = [r for r in rows
              if (include_camera or r['frame_type'] not in HIDDEN_TYPES)
              and (start is None or (r['end_sec'] or r['ts_offset_sec']) >= start)
              and (end is None or r['ts_offset_sec'] <= end)]
    limit = max(1, min(int(limit or 50), 200))
    page = picked[offset:offset + limit]
    toff = _track_offset(db_path, tid)
    try:
        params = json.loads(tx['video_analysis_params'] or 'null')
    except ValueError:
        logger.debug("video_analysis_params is not JSON", exc_info=True)
        params = None
    return {
        'transcription_id': tid, 'title': tx['title'] or tx['source_name'],
        'status': 'ready', 'analyzed_at': tx['video_analysis_at'], 'params': params,
        'total': len(picked), 'hidden': len(rows) - len([r for r in rows if include_camera
                                                         or r['frame_type'] not in HIDDEN_TYPES]),
        'offset': offset, 'next_offset': offset + limit if offset + limit < len(picked) else None,
        'items': [_item(tid, r, segs, shift, with_transcript, toff) for r in page],
    }


def to_markdown(result: dict) -> str:
    """Timeline for reading by a human and an agent."""
    lines = [tr('svc_pipes.timeline.md.title', title=result.get('title') or '',
                tid=result.get('transcription_id')), '']
    if result.get('status') != 'ready':
        return '\n'.join(lines + [f"_{result.get('hint') or result.get('status')}_"])
    for it in result.get('items') or []:
        lines.append(f"## {_hms(it['start'])} – {_hms(it['end'])} · "
                     f"{it['frame_type'] or tr('svc_pipes.timeline.md.frame')}")
        lines.append(f"![{tr('svc_pipes.timeline.md.frame')}]({it['frame_url']})")
        if it.get('duplicate_of'):
            lines.append(tr('svc_pipes.timeline.md.repeat', frame=it['duplicate_of']))
        ocr = ' '.join((it.get('ocr_text') or '').split())
        if ocr:
            lines.append(tr('svc_pipes.timeline.md.onScreen',
                            text=f"{ocr[:400]}{'…' if len(ocr) > 400 else ''}"))
        if it.get('description'):
            lines.append(tr('svc_pipes.timeline.md.description', text=it['description']))
        if it.get('transcript'):
            lines.append(tr('svc_pipes.timeline.md.spoken'))
            for s in it['transcript']:
                lines.append(f"- {s.get('speaker') or '?'} ({_hms(s['start'])}): \"{s['text']}\"")
        lines.append('')
    if result.get('next_offset') is not None:
        lines.append(tr('svc_pipes.timeline.md.next', offset=result['next_offset'],
                        total=result['total']))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# frames on the fly
# ---------------------------------------------------------------------------

def _covering(rows, t: float):
    best = None
    for r in rows:
        if r['ts_offset_sec'] <= t and (r['end_sec'] is None or t < r['end_sec']):
            best = r
    return best or (rows[-1] if rows and t >= rows[-1]['ts_offset_sec'] else (rows[0] if rows else None))


def render_frame(db_path: str, tid: int, t: float, *, size: str = 'thumb',
                 ffmpeg: Optional[str] = None):
    """→ (PIL frame with secrets painted over, segment metadata). ``t`` is the timeline time."""
    from app.services import screen_secrets as ss
    from app.services import video_analysis as va
    from app.services import video_frames as vf
    info = va.resolve_recording_video(db_path, tid)
    if info is None:
        raise TimelineError('NO_VIDEO', f'recording #{tid} has no video')
    offset = float(info.get('start_offset_sec') or 0.0)
    with get_db_connection(db_path) as conn:
        _, rows, _ = _load(conn, tid)
    video_t = max(0.0, float(t) - offset)
    img = vf.grab_frame(info['primary_video_path'], video_t,
                        max_width=SIZES.get(size, SIZES['thumb']), ffmpeg=ffmpeg)
    row = _covering(rows, float(t))
    is_representative = (row is not None and row['video_time_sec'] is not None
                         and abs(float(row['video_time_sec']) - video_t) < 0.05)
    # frame_text — the text of exactly THIS frame (secrets masked) or None if there was no OCR:
    # MCP checks stop words against it (spec mcp-visual-privacy)
    if is_representative and row['ocr_boxes_json'] is not None:
        # exactly the frame that was OCR-ed during analysis — its secret boxes are exact
        boxes, w, h = ss.secret_boxes(row['ocr_boxes_json'])
        frame_text = row['ocr_text'] or ''
    elif is_representative and row['ocr_text'] is not None:
        boxes, w, h = [], 0, 0                  # the same frame: OCR ran, no secrets
        frame_text = row['ocr_text'] or ''
    else:
        # any other moment: a secret could appear inside the segment (a token
        # line does not open a new segment, scrolling is merged by settle) — so
        # we look for secrets on exactly this frame
        boxes, w, h, frame_text = _live_secret_boxes(info['primary_video_path'], video_t, ffmpeg)
    meta = {'t': round(float(t), 2), 'size': size, 'redacted_boxes': len(boxes),
            'frame_text': frame_text}
    if row is not None:
        meta.update({'keyframe_id': row['id'], 'start': row['ts_offset_sec'],
                     'end': row['end_sec'], 'frame_type': row['frame_type'],
                     'ocr_text': row['ocr_text'] or '', 'description': row['vision_text']})
    return ss.redact(img, boxes, w, h), meta


def _live_secret_boxes(video: str, video_t: float, ffmpeg: Optional[str]):
    from app.services import document_parser as dp
    from app.services import screen_secrets as ss
    from app.services import video_analysis as va
    from app.services import video_frames as vf
    if not dp.ocr_available():
        logger.warning("OCR unavailable — a frame without analysis is returned fully painted over")
        return [[0, 0, 10 ** 6, 10 ** 6]], 1, 1, None
    img = vf.grab_frame(video, video_t, max_width=va._OCR_MAX_WIDTH, ffmpeg=ffmpeg)
    text, words = va.ocr_words(img)
    found = ss.scan_all(text, words)
    return ([words[s.word_index][:4] for s in found if s.word_index is not None], img.width,
            img.height, ss.mask_text(text, found) or '')


def jpeg_b64(img, quality: int = 80) -> str:
    buf = BytesIO()
    img.save(buf, 'JPEG', quality=quality)
    return base64.b64encode(buf.getvalue()).decode('ascii')


def frames(db_path: str, tid: int, *, t: Optional[float] = None, start: Optional[float] = None,
           end: Optional[float] = None, limit: int = 6, size: str = 'thumb',
           include_camera: bool = False, ffmpeg: Optional[str] = None) -> dict:
    """One frame by ``t`` or the segments' representatives in [start, end] (≤ limit, ≤ 12)."""
    limit = max(1, min(int(limit or 6), MAX_FRAMES_PER_CALL))
    if t is not None:
        times = [float(t)]
    else:
        res = build(db_path, tid, start=start, end=end, with_transcript=False,
                    include_camera=include_camera, limit=200)
        if res['status'] != 'ready':
            raise TimelineError('NOT_ANALYZED', 'the recording has not been cut yet — run video_analyze first '
                                '(or pass t to take one frame without analysis)')
        items = [i for i in res['items'] if not i.get('duplicate_of')]
        if len(items) > limit:                      # evenly across the interval
            step = len(items) / limit
            items = [items[int(k * step)] for k in range(limit)]
        times = [i['frame_time'] for i in items]
    out = []
    for ft in times:
        img, meta = render_frame(db_path, tid, ft, size=size, ffmpeg=ffmpeg)
        meta['image_b64'] = jpeg_b64(img)
        meta['mime'] = 'image/jpeg'
        out.append(meta)
    return {'transcription_id': tid, 'count': len(out), 'frames': out}


def frames_for_text(db_path: str, tid: int, *, query: Optional[str] = None,
                    segment_index: Optional[int] = None, window: float = 5.0,
                    shift: float = 0.0, max_matches: int = 5) -> dict:
    """A phrase (substring, case-insensitive) or a segment index → frames in the ±window."""
    with get_db_connection(db_path) as conn:
        tx, rows, segs = _load(conn, tid)
    if segment_index is not None:
        if not 0 <= int(segment_index) < len(segs):
            raise TimelineError('NOT_FOUND', f'segment {segment_index} does not exist')
        matches = [(int(segment_index), segs[int(segment_index)])]
    else:
        q = (query or '').casefold().strip()
        if not q:
            raise TimelineError('BAD_REQUEST', 'query or segment_index is required')
        matches = [(i, s) for i, s in enumerate(segs) if q in (s.get('text') or '').casefold()]
        matches = matches[:max_matches]
    toff = _track_offset(db_path, tid)
    out = []
    for i, s in matches:
        a = float(s.get('start') or 0) - shift - window
        b = float(s.get('end') or 0) - shift + window
        frames_ = [_item(tid, r, segs, shift, False, toff) for r in rows
                   if (r['end_sec'] or r['ts_offset_sec']) >= a and r['ts_offset_sec'] <= b]
        out.append({'segment_index': i, 'speaker': s.get('speaker'),
                    'start': s.get('start'), 'end': s.get('end'),
                    'text': (s.get('text') or '').strip(), 'frames': frames_})
    return {'transcription_id': tid, 'analyzed': bool(tx['video_analysis_at']),
            'matches': out}


def search_visual(db_path: str, query: str, *, transcription_ids: Optional[list] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None,
                  limit: int = 20) -> dict:
    """Search across OCR and frame descriptions of the whole archive (substring, casefold in Python —
    SQLite LOWER does not fold Cyrillic). The text is already masked."""
    q = (query or '').casefold().strip()
    if not q:
        raise TimelineError('BAD_REQUEST', 'empty query')
    sql = ("SELECT k.id, k.transcription_id, k.ts_offset_sec, k.end_sec, k.video_time_sec, "
           "k.frame_type, "
           "k.ocr_text, k.vision_text, t.title, t.source_name, t.meeting_date, t.created_at "
           "FROM video_keyframes k JOIN transcriptions t ON t.id = k.transcription_id "
           "WHERE (k.ocr_text IS NOT NULL OR k.vision_text IS NOT NULL)")
    args: list = []
    if transcription_ids:
        sql += f" AND k.transcription_id IN ({','.join('?' * len(transcription_ids))})"
        args += [int(x) for x in transcription_ids]
    if date_from:
        sql += " AND COALESCE(t.meeting_date, date(t.created_at)) >= ?"
        args.append(date_from)
    if date_to:
        sql += " AND COALESCE(t.meeting_date, date(t.created_at)) <= ?"
        args.append(date_to)
    hits = []
    offsets: dict = {}
    with get_db_connection(db_path) as conn:
        for r in conn.execute(sql + " ORDER BY k.transcription_id DESC, k.ts_offset_sec", args):
            hay = f"{r['ocr_text'] or ''}\n{r['vision_text'] or ''}"
            pos = hay.casefold().find(q)
            if pos < 0:
                continue
            snippet = ' '.join(hay[max(0, pos - 80):pos + len(q) + 80].split())
            tid_ = r['transcription_id']
            if tid_ not in offsets:
                offsets[tid_] = _track_offset(db_path, tid_)
            ft = (float(r['video_time_sec']) + offsets[tid_]) if r['video_time_sec'] is not None \
                else float(r['ts_offset_sec'])
            hits.append({'transcription_id': r['transcription_id'],
                         'title': r['title'] or r['source_name'],
                         'date': r['meeting_date'] or (r['created_at'] or '')[:10],
                         'keyframe_id': r['id'], 'start': r['ts_offset_sec'], 'end': r['end_sec'],
                         'timecode': _hms(r['ts_offset_sec']), 'frame_type': r['frame_type'],
                         'snippet': snippet,
                         'frame_time': round(ft, 2),
                         'frame_url': f"/api/transcription/{tid_}/frame?t={ft:.2f}"})
            if len(hits) >= limit:
                break
    return {'query': query, 'count': len(hits), 'results': hits}
