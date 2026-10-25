"""Transcript versions side store (ADR-044).

The canonical ``transcriptions`` columns stay the only transcript any consumer reads.
``transcript_versions`` holds snapshots; ``transcriptions.active_version_id`` names the
one mirrored by the canonical columns (NULL = single implicit version).

sqlite3 + stdlib only, no Flask. The caller owns the connection and the commit.
"""
from __future__ import annotations

import json
import struct
from typing import Any, Optional

_EMB_TAG = b'F32V'
_COPY_COLS = ('transcript_text', 'segments', 'model_used', 'language', 'processing_time',
              'polished_text', 'polished_at', 'polished_model')


def _blob_to_list(blob: Optional[bytes]) -> Optional[list]:
    if not blob or len(blob) < 4 or bytes(blob[:4]) != _EMB_TAG:
        return None
    payload = bytes(blob[4:])
    if len(payload) % 4:
        return None
    return list(struct.unpack(f'<{len(payload) // 4}f', payload))


def _list_to_blob(emb) -> Optional[bytes]:
    if not emb:
        return None
    floats = [float(x) for x in emb]
    return _EMB_TAG + struct.pack(f'<{len(floats)}f', *floats)


def _row(conn, sql: str, params: tuple):
    cur = conn.execute(sql, params)
    r = cur.fetchone()
    if r is None:
        return None
    if hasattr(r, 'keys'):
        return dict(r)
    return dict(zip([d[0] for d in cur.description], r))


def _rows(conn, sql: str, params: tuple) -> list:
    cur = conn.execute(sql, params)
    out = cur.fetchall()
    if out and not hasattr(out[0], 'keys'):
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in out]
    return [dict(r) for r in out]


def _live_speaker_map(conn, tid: int) -> list:
    rows = _rows(conn,
                 'SELECT raw_label, speaker_id, embedding FROM transcription_speaker_map '
                 'WHERE transcription_id = ? ORDER BY raw_label', (tid,))
    return [{'raw_label': r['raw_label'], 'speaker_id': r['speaker_id'],
             'embedding': _blob_to_list(r['embedding'])} for r in rows]


def _record(conn, tid: int) -> dict:
    rec = _row(conn, 'SELECT * FROM transcriptions WHERE id = ?', (tid,))
    if rec is None:
        raise LookupError(f'transcription {tid} not found')
    return rec


def _insert_row(conn, tid: int, *, created_at, is_original: int, values: dict,
                speaker_map: list) -> int:
    cur = conn.execute(
        'INSERT INTO transcript_versions (transcription_id, created_at, is_original, '
        'model_used, language, processing_time, transcript_text, segments, polished_text, '
        'polished_at, polished_model, speaker_map_json) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (tid, created_at, is_original, values.get('model_used'), values.get('language'),
         values.get('processing_time'), values.get('transcript_text'), values.get('segments'),
         values.get('polished_text'), values.get('polished_at'), values.get('polished_model'),
         json.dumps(speaker_map, ensure_ascii=False)))
    return cur.lastrowid


def write_back(conn, tid: int) -> int:
    """Refresh the active version row from the canonical columns (creates the original
    row when the pointer is NULL). Returns the active version id."""
    rec = _record(conn, tid)
    speaker_map = _live_speaker_map(conn, tid)
    vid = rec.get('active_version_id')
    if vid is None:
        vid = _insert_row(conn, tid, created_at=rec['created_at'], is_original=1,
                          values=rec, speaker_map=speaker_map)
        conn.execute('UPDATE transcriptions SET active_version_id = ? WHERE id = ?', (vid, tid))
        return vid
    sets = ', '.join(f'{c} = ?' for c in _COPY_COLS)
    conn.execute(
        f'UPDATE transcript_versions SET {sets}, speaker_map_json = ? '
        'WHERE id = ? AND transcription_id = ?',
        tuple(rec[c] for c in _COPY_COLS)
        + (json.dumps(speaker_map, ensure_ascii=False), vid, tid))
    return vid


def insert_version(conn, tid: int, *, model_used, language, processing_time, transcript_text,
                   segments, speaker_map) -> int:
    """Insert a new (non-original, unpolished) version row. Does not activate it."""
    if not isinstance(segments, str) and segments is not None:
        segments = json.dumps(segments, ensure_ascii=False)
    now = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]
    values = {'model_used': model_used, 'language': language,
              'processing_time': processing_time, 'transcript_text': transcript_text,
              'segments': segments}
    return _insert_row(conn, tid, created_at=now, is_original=0, values=values,
                       speaker_map=list(speaker_map or []))


def activate(conn, tid: int, vid: int) -> None:
    """Make version ``vid`` the main one: write back the current one, copy the target into
    the canonical columns and the speaker map, move the pointer."""
    target = _row(conn, 'SELECT * FROM transcript_versions WHERE id = ? AND transcription_id = ?',
                  (vid, tid))
    if target is None:
        raise LookupError(f'version {vid} is not a version of transcription {tid}')
    current = _record(conn, tid).get('active_version_id')
    if current == vid:
        # Already main: the canonical columns and the live speaker map are the truth
        # (polish, segment edits, speaker links since the last switch); the snapshot is stale.
        return
    # The target row is authoritative; only the outgoing one is refreshed.
    write_back(conn, tid)
    sets = ', '.join(f'{c} = ?' for c in _COPY_COLS)
    conn.execute(f'UPDATE transcriptions SET {sets}, active_version_id = ? WHERE id = ?',
                 tuple(target[c] for c in _COPY_COLS) + (vid, tid))
    try:
        entries = json.loads(target.get('speaker_map_json') or '[]')
    except ValueError:
        entries = []
    conn.execute('DELETE FROM transcription_speaker_map WHERE transcription_id = ?', (tid,))
    for e in entries:
        sid = e.get('speaker_id')
        if sid is not None and conn.execute(
                'SELECT 1 FROM speakers WHERE id = ?', (sid,)).fetchone() is None:
            sid = None
        conn.execute(
            'INSERT OR IGNORE INTO transcription_speaker_map '
            '(transcription_id, raw_label, speaker_id, embedding) VALUES (?, ?, ?, ?)',
            (tid, e.get('raw_label'), sid, _list_to_blob(e.get('embedding'))))


def _victims(ids: list, safe: set, keep: int) -> list:
    """The eviction rule: oldest first (``ids`` in ``created_at, id`` order) while more than
    ``keep`` remain, skipping every id in ``safe``."""
    left, out = list(ids), []
    while len(left) > keep:
        victim = next((i for i in left if i not in safe), None)
        if victim is None:
            break
        left.remove(victim)
        out.append(victim)
    return out


def _ordered(conn, tid: int) -> list:
    return _rows(conn, 'SELECT id, model_used, created_at FROM transcript_versions '
                       'WHERE transcription_id = ? ORDER BY created_at, id', (tid,))


def evict(conn, tid: int, keep: int = 3, protect=()) -> list:
    """Delete the oldest rows while the record has more than ``keep``, never the active one
    and never an id in ``protect`` (the main version the owner had before a re-run)."""
    safe = {_record(conn, tid).get('active_version_id'), *protect}
    removed = _victims([r['id'] for r in _ordered(conn, tid)], safe, keep)
    for victim in removed:
        conn.execute('DELETE FROM transcript_versions WHERE id = ?', (victim,))
    return removed


def evict_candidate(conn, tid: int, keep: int = 3, protect=()) -> Optional[dict]:
    """The row a re-run would drop now, without writing: ``{id, model_used, created_at}`` or None.

    Mirrors the run (write_back -> insert_version -> activate -> evict(protect=previous main)):
    the original row write_back would create lazily and the new row are counted and both are
    safe, as is the current main; the victim comes from the same rule ``evict`` applies."""
    rec = _record(conn, tid)
    rows = _ordered(conn, tid)
    pending = ['original'] if rec.get('active_version_id') is None else []
    pending.append('new')
    safe = {rec.get('active_version_id'), *protect, *pending}
    victims = _victims([r['id'] for r in rows] + pending, safe, keep)
    if not victims:
        return None
    return next(r for r in rows if r['id'] == victims[0])


def _segments(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return val if isinstance(val, list) else []


def list_versions(conn, tid: int) -> dict:
    rec = _record(conn, tid)
    active = rec.get('active_version_id')
    if active is None:
        return {'active_version_id': None, 'versions': []}
    out = []
    for r in _rows(conn, 'SELECT * FROM transcript_versions WHERE transcription_id = ? '
                         'ORDER BY created_at, id', (tid,)):
        is_active = r['id'] == active
        if is_active:  # a stale active row is never reported as content
            r = {**r, **{c: rec[c] for c in _COPY_COLS}}
            has_spk = bool(conn.execute(
                'SELECT 1 FROM transcription_speaker_map WHERE transcription_id = ? LIMIT 1',
                (tid,)).fetchone())
        else:
            try:
                has_spk = bool(json.loads(r.get('speaker_map_json') or '[]'))
            except ValueError:
                has_spk = False
        out.append({'id': r['id'], 'created_at': r['created_at'],
                    'is_original': bool(r['is_original']), 'is_active': is_active,
                    'model_used': r['model_used'], 'language': r['language'],
                    'segment_count': len(_segments(r['segments'])),
                    'has_polished': bool(r['polished_text']), 'has_speakers': has_spk})
    return {'active_version_id': active, 'versions': out}


def get_version(conn, tid: int, vid: int) -> Optional[dict]:
    rec = _record(conn, tid)
    row = _row(conn, 'SELECT * FROM transcript_versions WHERE id = ? AND transcription_id = ?',
               (vid, tid))
    if row is None:
        return None
    is_active = rec.get('active_version_id') == vid
    if is_active:
        src = {**row, **{c: rec[c] for c in _COPY_COLS}}
        entries = _live_speaker_map(conn, tid)
    else:
        src = row
        try:
            entries = json.loads(row.get('speaker_map_json') or '[]')
        except ValueError:
            entries = []
    names = {}
    for e in entries:
        sid = e.get('speaker_id')
        if sid is not None and sid not in names:
            sp = _row(conn, 'SELECT name FROM speakers WHERE id = ?', (sid,))
            names[sid] = sp['name'] if sp else None
    return {'id': vid, 'is_active': is_active, 'model_used': src['model_used'],
            'language': src['language'], 'created_at': row['created_at'],
            'transcript_text': src['transcript_text'], 'segments': _segments(src['segments']),
            'polished_text': src['polished_text'],
            'speaker_map': [{'raw_label': e.get('raw_label'), 'speaker_id': e.get('speaker_id'),
                             'name': names.get(e.get('speaker_id'))} for e in entries]}


def delete_for(conn, tid: int) -> int:
    return conn.execute('DELETE FROM transcript_versions WHERE transcription_id = ?',
                        (tid,)).rowcount
