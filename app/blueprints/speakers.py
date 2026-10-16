"""Speaker management endpoints (Phase 10.4).

CRUD over the global speakers table + bulk-rename per transcription.

Endpoints:
- GET    /api/speakers                              - list (with optional ?q= for autocomplete)
- POST   /api/speakers                              - create a new one
- PUT    /api/speakers/<id>                         - rename/change the color
- DELETE /api/speakers/<id>                         - delete (forbidden for is_self=1)
- PATCH  /api/transcriptions/<id>/speakers          — bulk: {raw_label: name|null}
- POST   /api/speakers/merge                        - merge several into one (snapshot for undo)
- POST   /api/speakers/unmerge/<merge_id>           - roll a merge back from the snapshot (T4.6)
- GET    /api/speakers/<id>/samples                 - sample phrases (merge preview, T4.6)

Cyrillic case-insensitive matching is done at the app level (Python
str.lower() supports Unicode case folding, unlike SQLite
COLLATE NOCASE which only works for ASCII).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections import defaultdict

from flask import Blueprint, current_app, jsonify, request

from app.core.i18n import tr
from app.repositories import speakers as speakers_repo
from app.repositories import transcriptions as tx_repo


logger = logging.getLogger(__name__)
speakers_bp = Blueprint('speakers', __name__)


# Basic name validation: 1-100 characters, not only whitespace. We do not do an inline regex
# here to prevent SQL/HTML injection - parameterized
# SQLite queries + escapeHTML on the frontend cover that.
_NAME_MAX_LEN = 100
_COLOR_RE = re.compile(r'^#?[0-9a-fA-F]{6}$')


def _get_db():
    from app.db.connection import get_db_connection
    return get_db_connection(current_app.config['DATABASE'])


def _normalize_name(name: str) -> str:
    """Trim + collapse internal whitespace. Does not lower-case
    (the display keeps the original case)."""
    return ' '.join(name.split())


def _validate_name(raw: str | None) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, tr('settings.speakers.err.name_type')
    name = _normalize_name(raw)
    if not name:
        return None, tr('settings.speakers.err.name_empty')
    if len(name) > _NAME_MAX_LEN:
        return None, tr('settings.speakers.err.name_long', max=_NAME_MAX_LEN)
    return name, None


def _validate_color(raw) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None  # ok, optional
    if not isinstance(raw, str):
        return None, tr('settings.speakers.err.color_type')
    if not _COLOR_RE.match(raw):
        return None, tr('settings.speakers.err.color_hex')
    return raw if raw.startswith('#') else f'#{raw}', None


def _find_speaker_case_insensitive(conn, name: str):
    """Looks up a speaker with case-insensitive matching (Cyrillic-safe).

    SQLite COLLATE NOCASE only works for ASCII, so we filter in Python.
    For small tables (<1000 rows) this is O(n) and acceptable.
    """
    target = name.lower()
    for row in conn.execute("SELECT id, name FROM speakers"):
        if row['name'].lower() == target:
            return row
    return None


def _serialize_speaker(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'color': row['color'],
        'is_self': bool(row['is_self']),
        'usage_count': row['usage_count'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


# ---------------------------------------------------------------- list / autocomplete

@speakers_bp.route('/api/speakers', methods=['GET'])
def list_speakers():
    """List of speakers. ?q= for autocomplete (starts-with, case-insensitive).

    Sorting: usage_count desc, then name asc. This puts the most
    "likely" suggestion first (a frequently used name).
    """
    q_raw = (request.args.get('q') or '').strip()
    limit = max(1, min(50, request.args.get('limit', 50, type=int)))

    with _get_db() as conn:
        rows = conn.execute('''
            SELECT id, name, color, is_self, usage_count, created_at, updated_at
            FROM speakers
            ORDER BY usage_count DESC, name COLLATE NOCASE ASC
        ''').fetchall()

    if q_raw:
        # Cyrillic-safe starts-with: Python str.lower() handles Unicode
        q_lower = q_raw.lower()
        filtered = [r for r in rows if r['name'].lower().startswith(q_lower)]
    else:
        filtered = rows

    return jsonify({
        'speakers': [_serialize_speaker(r) for r in filtered[:limit]],
        'total': len(filtered),
    })


# ---------------------------------------------------------------- create

@speakers_bp.route('/api/speakers', methods=['POST'])
def create_speaker():
    """Create a new speaker. If the name (case-insensitive) already exists -
    returns 409 with a reference to the existing ID."""
    data = request.get_json(silent=True) or {}
    name, name_err = _validate_name(data.get('name'))
    if name_err:
        return jsonify({'success': False, 'error': name_err}), 400
    color, color_err = _validate_color(data.get('color'))
    if color_err:
        return jsonify({'success': False, 'error': color_err}), 400

    with _get_db() as conn:
        existing = _find_speaker_case_insensitive(conn, name)
        if existing:
            full = speakers_repo.get_full_by_id(conn, existing['id'])
            return jsonify({
                'success': False,
                'error': tr('settings.speakers.err.exists'),
                'existing_speaker': _serialize_speaker(full),
            }), 409

        c = conn.cursor()
        c.execute(
            'INSERT INTO speakers (name, color) VALUES (?, ?)',
            (name, color),
        )
        new_id = c.lastrowid
        conn.commit()
        row = speakers_repo.get_full_by_id(conn, new_id)

    return jsonify({'success': True, 'speaker': _serialize_speaker(row)}), 201


# ---------------------------------------------------------------- update

@speakers_bp.route('/api/speakers/<int:speaker_id>', methods=['PUT'])
def update_speaker(speaker_id):
    """Rename or change the color. is_self is not editable through this API."""
    data = request.get_json(silent=True) or {}
    fields_to_update: list[tuple[str, object]] = []

    if 'name' in data:
        name, name_err = _validate_name(data.get('name'))
        if name_err:
            return jsonify({'success': False, 'error': name_err}), 400
        fields_to_update.append(('name', name))
    if 'color' in data:
        color, color_err = _validate_color(data.get('color'))
        if color_err:
            return jsonify({'success': False, 'error': color_err}), 400
        fields_to_update.append(('color', color))

    if not fields_to_update:
        return jsonify({'success': False, 'error': tr('settings.speakers.err.nothing_to_update')}), 400

    with _get_db() as conn:
        existing = conn.execute(
            'SELECT id, name FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not existing:
            return jsonify({'success': False, 'error': tr('settings.speakers.err.not_found')}), 404

        # On rename: check for a case-insensitive conflict (excluding this same id)
        new_name = next((v for k, v in fields_to_update if k == 'name'), None)
        if new_name and new_name.lower() != existing['name'].lower():
            conflict = _find_speaker_case_insensitive(conn, new_name)
            if conflict and conflict['id'] != speaker_id:
                return jsonify({
                    'success': False,
                    'error': tr('settings.speakers.err.name_taken', name=new_name),
                }), 409

        set_clause = ', '.join(f'{k} = ?' for k, _ in fields_to_update)
        params = [v for _, v in fields_to_update] + [speaker_id]
        conn.execute(
            f'UPDATE speakers SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            params,
        )
        conn.commit()
        row = speakers_repo.get_full_by_id(conn, speaker_id)

    return jsonify({'success': True, 'speaker': _serialize_speaker(row)})


# ---------------------------------------------------------------- delete

@speakers_bp.route('/api/speakers/<int:speaker_id>', methods=['DELETE'])
def delete_speaker(speaker_id):
    """Delete a speaker. Forbidden for is_self=1 (needed by the system).

    Phase 10.3 spec: ON DELETE SET NULL in transcription_speaker_map → all
    rows with this speaker_id get NULL (raw_label stays, the frontend
    will show them as "Спікер N" again). foreign_keys=ON in the connection
    ensures the cascade fires.
    """
    with _get_db() as conn:
        row = conn.execute(
            'SELECT id, is_self FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': tr('settings.speakers.err.not_found')}), 404
        if row['is_self']:
            return jsonify({
                'success': False,
                'error': tr('settings.speakers.err.self_delete'),
            }), 400
        conn.execute('DELETE FROM speakers WHERE id = ?', (speaker_id,))
        conn.commit()

    return jsonify({'success': True})


# ---------------------------------------------------------------- bulk rename per transcript

@speakers_bp.route('/api/transcriptions/<int:transcription_id>/speakers', methods=['PATCH'])
def patch_transcription_speakers(transcription_id):
    """Bulk mapping raw_label → name for a specific transcript.

    Body: {
        "mapping": {
            "SPEAKER_00": "Андрій",         // creates/finds → sets speaker_id
            "SPEAKER_01": null,             // clears the mapping (becomes unnamed)
            "self": "Андрій"                // rebind self to another name
        }
    }

    Returns: the full updated speaker map after the operation + an increment of
    usage_count for newly created bindings.
    """
    data = request.get_json(silent=True) or {}
    mapping = data.get('mapping', {})
    if not isinstance(mapping, dict):
        return jsonify({'success': False, 'error': tr('settings.speakers.err.mapping_type')}), 400

    with _get_db() as conn:
        if not tx_repo.exists(conn, transcription_id):
            return jsonify({'success': False, 'error': tr('settings.speakers.err.tx_not_found')}), 404

        c = conn.cursor()
        usage_increments: list[int] = []  # speaker_ids whose usage_count must be raised

        # Phase 10.6: for voice fingerprinting take the embedding from the map
        from app.services.diarization_service import (
            average_embeddings, blob_to_embedding, embedding_to_blob,
        )

        for raw_label, value in mapping.items():
            if not isinstance(raw_label, str) or not raw_label:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                # Clear the mapping
                c.execute(
                    'UPDATE transcription_speaker_map SET speaker_id = NULL '
                    'WHERE transcription_id = ? AND raw_label = ?',
                    (transcription_id, raw_label),
                )
                continue

            # Find or create the speaker by name
            name, err = _validate_name(value)
            if err:
                return jsonify({
                    'success': False,
                    'error': tr('settings.speakers.err.bad_name_for', label=raw_label, err=err),
                }), 400
            existing = _find_speaker_case_insensitive(conn, name)
            if existing:
                speaker_id = existing['id']
            else:
                c.execute('INSERT INTO speakers (name) VALUES (?)', (name,))
                speaker_id = c.lastrowid

            # Phase 10.6: fetch the embedding from transcription_speaker_map for
            # this raw_label (saved by the diarization step during transcribe).
            map_row = c.execute(
                'SELECT embedding FROM transcription_speaker_map '
                'WHERE transcription_id = ? AND raw_label = ?',
                (transcription_id, raw_label),
            ).fetchone()
            new_embedding = blob_to_embedding(map_row['embedding']) if map_row else None

            # Upsert the mapping. If the row does not exist yet (e.g. a fresh frontend
            # adds a mapping for labels that were not detected automatically) -
            # create it. PRIMARY KEY on (transcription_id, raw_label) supports this
            # via INSERT OR REPLACE.
            c.execute(
                'INSERT OR REPLACE INTO transcription_speaker_map '
                '(transcription_id, raw_label, speaker_id, embedding) '
                'VALUES (?, ?, ?, ?)',
                (transcription_id, raw_label, speaker_id,
                 map_row['embedding'] if map_row else None),
            )
            usage_increments.append(speaker_id)

            # Running average in speakers.embedding. Weight of the old = usage_count
            # (how many times this speaker_id has already been used), of the new = 1.
            if new_embedding:
                speaker_row = c.execute(
                    'SELECT embedding, usage_count FROM speakers WHERE id = ?',
                    (speaker_id,),
                ).fetchone()
                old_embedding = blob_to_embedding(speaker_row['embedding']) if speaker_row else None
                old_usage = speaker_row['usage_count'] if speaker_row else 0
                merged = average_embeddings(
                    old_embedding, new_embedding,
                    weight_a=max(1, old_usage),  # min 1 so as not to divide by 0 on the first
                    weight_b=1.0,
                )
                merged_blob = embedding_to_blob(merged)
                c.execute(
                    'UPDATE speakers SET embedding = ? WHERE id = ?',
                    (merged_blob, speaker_id),
                )

        # Increment usage_count for new bindings (unique by speaker_id)
        for sid in set(usage_increments):
            c.execute(
                'UPDATE speakers SET usage_count = usage_count + 1, '
                'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (sid,),
            )
        conn.commit()

        # Return the updated map
        speaker_rows = conn.execute('''
            SELECT m.raw_label, m.speaker_id, s.name, s.color, s.is_self
            FROM transcription_speaker_map m
            LEFT JOIN speakers s ON s.id = m.speaker_id
            WHERE m.transcription_id = ?
        ''', (transcription_id,)).fetchall()

    return jsonify({
        'success': True,
        'speakers': [
            {
                'raw_label': r['raw_label'],
                'speaker_id': r['speaker_id'],
                'name': r['name'],
                'color': r['color'],
                'is_self': bool(r['is_self']) if r['is_self'] is not None else False,
            }
            for r in speaker_rows
        ],
    })


# ---------------------------------------------------------------- stats (Phase 12.10)

def _entity_index(conn) -> dict:
    """A person's spelling → the graph entity id, when it is unambiguous.

    Needed to bring two layers together: «Слава Верес» in diarization and «Veres
    Viacheslav» in the correspondence are one person, but the tables do not know it.
    The graph knows some of the links (entity 478 «Юлія» has «Julia
    Bondarenko» among its aliases), so we take ITS links rather than guessing from name similarity.

    We take ONLY `type='person'`: both lists are about people, and a link means
    "this is the same person". Without this filter a channel or bot whose name coincides with
    the name of a project or organization would get an `entity_id` and land in the merged
    counter - i.e. the layers would "converge" through an entity that is not a person.

    Keys are folded with `casefold()` in Python: SQLite `LOWER` does not touch Cyrillic.
    Ambiguous spellings (the same name leads to several entities) are deliberately
    left without an id - a guess here would cost more than a dash.
    """
    idx: dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT id, canonical_name AS name FROM entities "
                          "WHERE type = 'person'"):
        if r['name']:
            idx[r['name'].casefold().strip()].add(r['id'])
    for r in conn.execute('SELECT a.entity_id, a.alias FROM entity_aliases a '
                          "JOIN entities e ON e.id = a.entity_id WHERE e.type = 'person'"):
        if r['alias']:
            idx[r['alias'].casefold().strip()].add(r['entity_id'])
    return {k: next(iter(v)) for k, v in idx.items() if len(v) == 1}


@speakers_bp.route('/api/speakers/stats', methods=['GET'])
def speakers_stats():
    """Aggregated statistics of each speaker across all transcripts.

    Returns for each speaker: transcripts_count, total_seconds (sum of
    end-start of all segments of this speaker), words_count (split text),
    first_seen, last_seen (created_at of the oldest / newest
    transcript in which they are present).

    O(N) over transcriptions with diarization. We parse the segments JSON in
    Python - without the JSON1 SQLite extension this cannot be done in SQL.

    **Two layers, not one.** `speakers` is diarization, i.e. the VOICES from
    calls and recordings: some recordings. The rest of the archive is correspondence, where the
    author is known exactly (`tg_sender`, 35 people over messages) and where there are
    neither seconds nor diarization. While the response was silent about this, "statistics for
    each speaker" described a small share of the archive and read as all of it.

    So the body has three parts: `speakers` (voices), `tg_senders` (correspondence) and
    `coverage` (what exactly was counted). The layers' numbers are NOT added: seconds of speech
    and a message count are different units.
    """
    with _get_db() as conn:
        # Base stats
        rows = conn.execute('''
            SELECT s.id, s.name, s.color, s.is_self, s.usage_count, s.created_at, s.updated_at,
                   -- Count by `t`, not by `m`: the link in the map stays even
                   -- after a soft delete of the record, so `m` would give a deleted one.
                   COUNT(DISTINCT t.id) as transcripts_count,
                   -- The date of the EVENT, not of the upload. `created_at` here meant "when the
                   -- record got into the archive", whereas next to it `tg_senders`
                   -- carries the message date: identically named fields with different
                   -- meanings push toward exactly the comparison of layers that the
                   -- tool description warns against.
                   MAX(COALESCE(t.meeting_date, substr(t.created_at,1,10))) as last_seen,
                   MIN(COALESCE(t.meeting_date, substr(t.created_at,1,10))) as first_seen
            FROM speakers s
            LEFT JOIN transcription_speaker_map m ON m.speaker_id = s.id
            -- The deletion condition is in ON, not in WHERE: otherwise the LEFT JOIN becomes
            -- an inner one and speakers without records disappear from the list.
            LEFT JOIN transcriptions t ON t.id = m.transcription_id
                                      AND t.deleted_at IS NULL
            GROUP BY s.id
            ORDER BY s.usage_count DESC, s.name COLLATE NOCASE
        ''').fetchall()

        # Map (tx_id, raw_label) → speaker_id for aggregation by segments
        map_rows = conn.execute(
            'SELECT transcription_id, raw_label, speaker_id '
            'FROM transcription_speaker_map WHERE speaker_id IS NOT NULL'
        ).fetchall()

        by_tx: dict[int, dict[str, int]] = defaultdict(dict)
        for r in map_rows:
            by_tx[r['transcription_id']][r['raw_label']] = r['speaker_id']

        seconds_per_speaker: dict[int, float] = defaultdict(float)
        words_per_speaker: dict[int, int] = defaultdict(int)

        if by_tx:
            tx_rows = tx_repo.get_many_by_ids(conn, list(by_tx.keys()), columns=('id', 'segments'))
            for tx in tx_rows:
                if not tx['segments']:
                    continue
                try:
                    segs = json.loads(tx['segments'])
                except (json.JSONDecodeError, TypeError):
                    continue
                labels = by_tx[tx['id']]
                for seg in segs:
                    raw = seg.get('speaker')
                    if not raw or raw not in labels:
                        continue
                    sid = labels[raw]
                    try:
                        dur = float(seg.get('end', 0)) - float(seg.get('start', 0))
                    except (TypeError, ValueError):
                        dur = 0
                    if dur > 0:
                        seconds_per_speaker[sid] += dur
                    text = seg.get('text') or ''
                    if text:
                        words_per_speaker[sid] += len(text.split())

        ent_idx = _entity_index(conn)

        # The second layer: correspondence authors. The units here are different (messages, chats),
        # so it is a separate list, not extra rows added to `speakers`.
        tg_rows = conn.execute('''
            SELECT tg_sender AS name, COUNT(*) AS messages,
                   COUNT(DISTINCT tg_chat_id) AS chats,
                   MIN(COALESCE(meeting_date, substr(created_at,1,10))) AS first_seen,
                   MAX(COALESCE(meeting_date, substr(created_at,1,10))) AS last_seen
            FROM transcriptions
            WHERE source_type = 'telegram' AND deleted_at IS NULL
              AND tg_sender IS NOT NULL AND tg_sender <> ''
            GROUP BY tg_sender
            ORDER BY messages DESC
        ''').fetchall()

        # `deleted_at IS NULL` is mandatory here: deleting a single record is soft
        # (sets `deleted_at`), and the `transcription_speaker_map` rows stay
        # until a bulk cleanup. Without the filter the numerator would hold deleted ones while
        # the denominator no longer counts them - and "some recordings" would overstate the
        # diarization coverage. Exactly the class of claim this branch guards against.
        diarized = conn.execute(
            'SELECT COUNT(DISTINCT m.transcription_id) AS n '
            'FROM transcription_speaker_map m '
            'JOIN transcriptions t ON t.id = m.transcription_id '
            'WHERE t.deleted_at IS NULL'
        ).fetchone()['n']
        total_tx = conn.execute(
            'SELECT COUNT(*) AS n FROM transcriptions WHERE deleted_at IS NULL'
        ).fetchone()['n']

    tg_senders = [{
        'name': r['name'],
        'messages': r['messages'],
        'chats': r['chats'],
        'first_seen': r['first_seen'],
        'last_seen': r['last_seen'],
        # The same graph link as for speakers: if both layers carry
        # the same entity_id - it is one person, and the graph knows it, not a guess.
        'entity_id': ent_idx.get((r['name'] or '').casefold().strip()),
    } for r in tg_rows]

    speakers = []
    total_seconds_all = 0.0
    for r in rows:
        sid = r['id']
        ts = round(seconds_per_speaker.get(sid, 0.0), 1)
        total_seconds_all += ts
        speakers.append({
            'id': sid,
            'name': r['name'],
            'color': r['color'],
            'is_self': bool(r['is_self']),
            'usage_count': r['usage_count'],
            'transcripts_count': r['transcripts_count'] or 0,
            'total_seconds': ts,
            'words_count': words_per_speaker.get(sid, 0),
            'first_seen': r['first_seen'],
            'last_seen': r['last_seen'],
            'created_at': r['created_at'],
            'entity_id': ent_idx.get((r['name'] or '').casefold().strip()),
        })

    linked = sum(1 for s in tg_senders if s['entity_id'])
    return jsonify({
        'speakers': speakers,
        'total_speakers': len(speakers),
        'total_seconds': round(total_seconds_all, 1),
        'tg_senders': tg_senders,
        'total_tg_senders': len(tg_senders),
        'coverage': {
            'diarized_transcripts': diarized,
            'total_transcripts': total_tx,
            'telegram_messages': sum(s['messages'] for s in tg_senders),
            'tg_senders_linked_to_graph': linked,
            'note': tr('settings.speakers.stats_note', diarized=diarized, total_tx=total_tx,
                       linked=linked, senders=len(tg_senders)),
        },
    })


# ---------------------------------------------------------------- speaker timeline (Phase 12.23)

@speakers_bp.route('/api/speakers/<int:speaker_id>/timeline', methods=['GET'])
def speaker_timeline(speaker_id):
    """A speaker's activity by day for the last N days (default: 30).

    For each day in the interval returns:
    - transcripts_count (DISTINCT transcript_ids in which the speaker is present)
    - total_seconds (sum of duration of all segments of this speaker)
    - words_count

    The frontend builds a day-by-day bar chart.
    """
    days = max(1, min(365, request.args.get('days', 30, type=int)))

    with _get_db() as conn:
        speaker = conn.execute(
            'SELECT id, name FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not speaker:
            return jsonify({'success': False, 'error': tr('settings.speakers.err.not_found')}), 404

        # All transcripts in which this speaker is present in the last N days.
        rows = conn.execute('''
            SELECT t.id, t.created_at, t.segments, m.raw_label
            FROM transcriptions t
            JOIN transcription_speaker_map m ON m.transcription_id = t.id
            WHERE m.speaker_id = ?
              AND t.created_at >= datetime('now', ? || ' days')
            ORDER BY t.created_at DESC
        ''', (speaker_id, f'-{days}')).fetchall()

    # Aggregation per-day. Key — YYYY-MM-DD.
    by_day: dict[str, dict] = defaultdict(lambda: {
        'transcripts': set(),
        'total_seconds': 0.0,
        'words_count': 0,
    })

    for r in rows:
        try:
            day = r['created_at'][:10]  # 'YYYY-MM-DD HH:MM:SS' → 'YYYY-MM-DD'
            segs = json.loads(r['segments']) if r['segments'] else []
        except (json.JSONDecodeError, TypeError):
            continue
        bucket = by_day[day]
        bucket['transcripts'].add(r['id'])
        for seg in segs:
            if seg.get('speaker') != r['raw_label']:
                continue
            try:
                dur = float(seg.get('end', 0)) - float(seg.get('start', 0))
            except (TypeError, ValueError):
                dur = 0
            if dur > 0:
                bucket['total_seconds'] += dur
            text = seg.get('text') or ''
            if text:
                bucket['words_count'] += len(text.split())

    # Turn the set into a count + serialize
    timeline = [
        {
            'date': day,
            'transcripts_count': len(b['transcripts']),
            'total_seconds': round(b['total_seconds'], 1),
            'words_count': b['words_count'],
        }
        for day, b in sorted(by_day.items())
    ]

    return jsonify({
        'speaker_id': speaker_id,
        'speaker_name': speaker['name'],
        'days': days,
        'timeline': timeline,
        'summary': {
            'total_seconds': round(sum(d['total_seconds'] for d in timeline), 1),
            'total_words': sum(d['words_count'] for d in timeline),
            'active_days': len(timeline),
            'total_transcripts': len(set().union(*(b['transcripts'] for b in by_day.values()))),
        },
    })


# ---------------------------------------------------------------- merge speakers (Phase 12.8)

def _blob_b64(blob) -> str | None:
    return base64.b64encode(blob).decode('ascii') if blob else None


def _b64_blob(s) -> bytes | None:
    return base64.b64decode(s) if s else None


@speakers_bp.route('/api/speakers/merge', methods=['POST'])
def merge_speakers():
    """Merge several speakers into one.

    Body: {
        "keep_id": 3,                    // which to keep
        "merge_ids": [7, 12]             // which to pour in and delete
    }

    Actions:
    1. All transcription_speaker_map.speaker_id ∈ merge_ids → keep_id
    2. speakers.embedding of keep_id is updated as a weighted average
       (weight = each one's usage_count, min 1).
    3. speakers.usage_count keep_id += sum(usage_count merge_ids)
    4. DELETE merge_ids from speakers.

    Forbidden:
    - keep_id in merge_ids
    - merge_ids contains is_self=1 (the system "Ви")

    T4.6 (REMEDIATION_PLAN Wave 2): before steps 1-4 it records a before-snapshot
    (the deleted speakers rows + the previous map mapping + entities.speaker_id,
    which the FK ON DELETE SET NULL will null out) into ``speaker_merges`` - so that
    ``POST /api/speakers/unmerge/<merge_id>`` can roll the merge back during the
    grace period (``RECALL_SOFTDELETE_GRACE_DAYS``). The response additionally
    contains ``merge_id`` for this undo.
    """
    data = request.get_json(silent=True) or {}
    keep_id = data.get('keep_id')
    merge_ids = data.get('merge_ids', [])

    if not isinstance(keep_id, int) or keep_id <= 0:
        return jsonify({'success': False, 'error': tr('settings.speakers.err.keep_id_int')}), 400
    if not isinstance(merge_ids, list) or not merge_ids:
        return jsonify({'success': False, 'error': tr('settings.speakers.err.merge_ids_list')}), 400
    try:
        merge_ids = [int(x) for x in merge_ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': tr('settings.speakers.err.merge_ids_nums')}), 400
    if keep_id in merge_ids:
        return jsonify({'success': False, 'error': tr('settings.speakers.err.keep_in_merge')}), 400

    from app.services.diarization_service import (
        average_embeddings, blob_to_embedding, embedding_to_blob,
    )

    with _get_db() as conn:
        c = conn.cursor()
        keep_row = c.execute(
            'SELECT id, name, color, is_self, embedding, usage_count, created_at, updated_at '
            'FROM speakers WHERE id = ?',
            (keep_id,),
        ).fetchone()
        if not keep_row:
            return jsonify({'success': False, 'error': tr('settings.speakers.err.keep_not_found')}), 404

        # Verify that all merge_ids exist and are not is_self
        placeholders = ','.join('?' * len(merge_ids))
        merge_rows = c.execute(
            f'SELECT id, name, color, is_self, embedding, usage_count, created_at, updated_at '
            f'FROM speakers WHERE id IN ({placeholders})',
            merge_ids,
        ).fetchall()
        if len(merge_rows) != len(set(merge_ids)):
            return jsonify({'success': False, 'error': tr('settings.speakers.err.some_not_found')}), 404
        if any(r['is_self'] for r in merge_rows):
            return jsonify({
                'success': False,
                'error': tr('settings.speakers.err.self_merge'),
            }), 400

        # --- T4.6: before-snapshot (BEFORE any mutations) ---
        map_before = c.execute(
            f'SELECT transcription_id, raw_label, speaker_id '
            f'FROM transcription_speaker_map WHERE speaker_id IN ({placeholders})',
            merge_ids,
        ).fetchall()
        entities_before = c.execute(
            f'SELECT id, speaker_id FROM entities WHERE speaker_id IN ({placeholders})',
            merge_ids,
        ).fetchall()
        before_json = json.dumps({
            'keep': {
                'id': keep_row['id'],
                'embedding_b64': _blob_b64(keep_row['embedding']),
                'usage_count': keep_row['usage_count'],
            },
            'merged_speakers': [
                {
                    'id': r['id'], 'name': r['name'], 'color': r['color'],
                    'is_self': r['is_self'], 'usage_count': r['usage_count'],
                    'embedding_b64': _blob_b64(r['embedding']),
                    'created_at': r['created_at'], 'updated_at': r['updated_at'],
                }
                for r in merge_rows
            ],
            'map_relinked': [
                {'transcription_id': r['transcription_id'], 'raw_label': r['raw_label'],
                 'speaker_id': r['speaker_id']}
                for r in map_before
            ],
            'entities_relinked': [
                {'id': r['id'], 'speaker_id': r['speaker_id']} for r in entities_before
            ],
        }, ensure_ascii=False)

        # 1. Re-link the map: all maps from merge_ids → keep_id.
        # If one transcript has rows with different raw_labels for different
        # merge_ids - after the UPDATE all will point to keep_id, which is OK
        # (the PRIMARY KEY is on (transcription_id, raw_label), not speaker_id).
        c.execute(
            f'UPDATE transcription_speaker_map SET speaker_id = ? '
            f'WHERE speaker_id IN ({placeholders})',
            [keep_id] + merge_ids,
        )
        relinked = c.rowcount

        # 2. Average the embeddings - weighted by usage_count, min 1.
        keep_embedding = blob_to_embedding(keep_row['embedding'])
        keep_weight = max(1, keep_row['usage_count'] or 0)
        merged_embedding = keep_embedding
        merged_weight = keep_weight
        for r in merge_rows:
            other = blob_to_embedding(r['embedding'])
            if other is None:
                continue
            other_w = max(1, r['usage_count'] or 0)
            merged_embedding = average_embeddings(
                merged_embedding, other,
                weight_a=merged_weight, weight_b=other_w,
            )
            merged_weight += other_w

        new_blob = embedding_to_blob(merged_embedding) if merged_embedding is not None else None

        # 3. Update keep: embedding + bumped usage_count
        total_usage = (keep_row['usage_count'] or 0) + sum(r['usage_count'] or 0 for r in merge_rows)
        c.execute(
            'UPDATE speakers SET embedding = ?, usage_count = ?, '
            'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (new_blob, total_usage, keep_id),
        )

        # 4. Delete merge_ids (entities.speaker_id → FK ON DELETE SET NULL,
        # so the entities_relinked snapshot above was taken BEFORE this step)
        c.execute(
            f'DELETE FROM speakers WHERE id IN ({placeholders})',
            merge_ids,
        )

        # 5. Persist the before-snapshot for undo
        merged_at = time.time()
        c.execute(
            'INSERT INTO speaker_merges (keep_id, merged_speaker_ids, before_json, merged_at) '
            'VALUES (?, ?, ?, ?)',
            (keep_id, json.dumps(merge_ids), before_json, merged_at),
        )
        merge_id = c.lastrowid

        conn.commit()

        kept = conn.execute(
            'SELECT id, name, color, is_self, usage_count, created_at, updated_at '
            'FROM speakers WHERE id = ?', (keep_id,),
        ).fetchone()

    logger.info(
        f"[merge] keep_id={keep_id} ({keep_row['name']}) absorbed "
        f"{len(merge_rows)} speakers ({[r['name'] for r in merge_rows]}); "
        f"relinked {relinked} map rows, total_usage={total_usage}, merge_id={merge_id}"
    )
    return jsonify({
        'success': True,
        'speaker': _serialize_speaker(kept),
        'merged_count': len(merge_rows),
        'relinked_map_rows': relinked,
        'merge_id': merge_id,
    })


# ---------------------------------------------------------------- unmerge (T4.6)

@speakers_bp.route('/api/speakers/unmerge/<int:merge_id>', methods=['POST'])
def unmerge_speakers(merge_id):
    """Roll back merge_speakers from the snapshot (undo) while it has not expired
    (RECALL_SOFTDELETE_GRACE_DAYS) and has not been restored before.

    Actions (mirror of merge):
    1. Recreate the deleted speakers rows (the same id - AUTOINCREMENT
       allows an explicit INSERT with an id; no new speaker could have taken
       this number since).
    2. Restore transcription_speaker_map.speaker_id for the SPECIFIC
       (transcription_id, raw_label) pairs changed by this merge (map_before) -
       NOT all current rows on keep_id (so as not to touch later changes by others).
    3. Restore entities.speaker_id nulled by the FK ON DELETE SET NULL -
       only if it is still NULL (we do not overwrite manual changes after the merge).
    4. Restore the embedding/usage_count of keep_id from the snapshot.
    5. Mark speaker_merges.restored_at.

    409 if the snapshot has already been used (restored_at IS NOT NULL) or
    has expired the grace period and was removed by the purge (then the row no longer exists - 404).
    """
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute(
            'SELECT id, keep_id, before_json, restored_at FROM speaker_merges WHERE id = ?',
            (merge_id,),
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': tr('settings.speakers.err.merge_not_found')}), 404
        if row['restored_at'] is not None:
            return jsonify({'success': False, 'error': tr('settings.speakers.err.merge_restored')}), 409

        try:
            snap = json.loads(row['before_json'])
        except (json.JSONDecodeError, TypeError):
            return jsonify({'success': False, 'error': tr('settings.speakers.err.snapshot_bad')}), 500

        # 1. Recreate the deleted speakers (INSERT OR IGNORE - if someone has already
        # created a new speaker with the same id, theoretically impossible because of
        # AUTOINCREMENT, but just in case we do not crash).
        for sp in snap.get('merged_speakers', []):
            c.execute(
                'INSERT OR IGNORE INTO speakers '
                '(id, name, color, is_self, usage_count, embedding, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (sp['id'], sp['name'], sp.get('color'), sp.get('is_self', 0),
                 sp.get('usage_count', 0), _b64_blob(sp.get('embedding_b64')),
                 sp.get('created_at'), sp.get('updated_at')),
            )

        # 2. Restore the map rows of exactly this merge (only if raw_label still
        # belongs to the transcript - the row could have been deleted/changed since).
        for m in snap.get('map_relinked', []):
            c.execute(
                'UPDATE transcription_speaker_map SET speaker_id = ? '
                'WHERE transcription_id = ? AND raw_label = ?',
                (m['speaker_id'], m['transcription_id'], m['raw_label']),
            )

        # 3. Restore entities.speaker_id - only where it is still NULL (so as not to
        # overwrite the user's manual reassignment after the merge).
        for e in snap.get('entities_relinked', []):
            c.execute(
                'UPDATE entities SET speaker_id = ? WHERE id = ? AND speaker_id IS NULL',
                (e['speaker_id'], e['id']),
            )

        # 4. Restore the embedding/usage_count of keep_id from the snapshot.
        keep_snap = snap.get('keep')
        if keep_snap:
            c.execute(
                'UPDATE speakers SET embedding = ?, usage_count = ?, '
                'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (_b64_blob(keep_snap.get('embedding_b64')), keep_snap.get('usage_count', 0),
                 keep_snap['id']),
            )

        # 5. Mark the snapshot as used.
        c.execute(
            'UPDATE speaker_merges SET restored_at = ? WHERE id = ?',
            (time.time(), merge_id),
        )
        conn.commit()

        restored_ids = [sp['id'] for sp in snap.get('merged_speakers', [])]

    logger.info(
        f"[unmerge] merge_id={merge_id}: restored speakers={restored_ids}, "
        f"keep_id={row['keep_id']}"
    )
    return jsonify({
        'success': True,
        'merge_id': merge_id,
        'restored_speaker_ids': restored_ids,
        'keep_id': row['keep_id'],
    })


# ---------------------------------------------------------------- merge-preview samples (T4.6)

@speakers_bp.route('/api/speakers/<int:speaker_id>/samples', methods=['GET'])
def speaker_samples(speaker_id):
    """A few sample phrases of a speaker - a light merge preview (T4.6), so the frontend
    shows examples BEFORE merging without heavy stats/timeline aggregations.

    Query: ?limit=N (default 5, max 20).
    Returns: {success, speaker_id, speaker_name, samples: [{transcription_id,
    source_name, start, end, text}]}.
    """
    limit = max(1, min(20, request.args.get('limit', 5, type=int)))
    with _get_db() as conn:
        speaker = conn.execute(
            'SELECT id, name FROM speakers WHERE id = ?', (speaker_id,)
        ).fetchone()
        if not speaker:
            return jsonify({'success': False, 'error': tr('settings.speakers.err.not_found')}), 404

        rows = conn.execute('''
            SELECT m.transcription_id, m.raw_label, t.source_name, t.segments
            FROM transcription_speaker_map m
            JOIN transcriptions t ON t.id = m.transcription_id
            WHERE m.speaker_id = ? AND t.deleted_at IS NULL
            ORDER BY t.created_at DESC
        ''', (speaker_id,)).fetchall()

    samples: list[dict] = []
    for r in rows:
        if len(samples) >= limit:
            break
        try:
            segs = json.loads(r['segments']) if r['segments'] else []
        except (json.JSONDecodeError, TypeError):
            continue
        for seg in segs:
            if len(samples) >= limit:
                break
            if seg.get('speaker') != r['raw_label']:
                continue
            text = (seg.get('text') or '').strip()
            if not text:
                continue
            samples.append({
                'transcription_id': r['transcription_id'],
                'source_name': r['source_name'],
                'start': seg.get('start'),
                'end': seg.get('end'),
                'text': text,
            })

    return jsonify({
        'success': True,
        'speaker_id': speaker_id,
        'speaker_name': speaker['name'],
        'samples': samples,
    })
