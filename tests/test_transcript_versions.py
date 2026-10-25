"""Transcript versions side store (ADR-044): service, FTS follow-through, delete cascade."""
from __future__ import annotations

import json
import sqlite3

import pytest
from flask import Flask

from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database
from app.services import transcript_versions as tv

OLD = 'Добрий день, обговорюємо бюджет проєкту'
NEW = 'Здравствуйте, обсуждаем маркетинговую стратегию'


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / 'tv.db')
    init_database(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _tx(conn, text=OLD):
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, segments, "
        "language, model_used, polished_text, created_at) "
        "VALUES ('file', 'Дзвінок', ?, ?, 'uk', 'small', 'Відполірований старий', "
        "'2026-01-01 10:00:00')",
        (text, json.dumps([{'start': 0, 'end': 2, 'text': text}], ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


def _speaker(conn, name='Олена'):
    return conn.execute('INSERT INTO speakers (name) VALUES (?)', (name,)).lastrowid


def _map(conn, tid, sid, label='SPEAKER_00'):
    conn.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) "
                 "VALUES (?, ?, ?)", (tid, label, sid))


def _new(conn, tid, text=NEW, model='large-v3'):
    return tv.insert_version(conn, tid, model_used=model, language='ru', processing_time=1.5,
                             transcript_text=text,
                             segments=[{'start': 0, 'end': 2, 'text': text}], speaker_map=[])


def _fts(conn, word):
    return [r[0] for r in conn.execute(
        'SELECT rowid FROM transcriptions_fts WHERE transcriptions_fts MATCH ?', (word,))]


def test_write_back_null_pointer_creates_original(conn):
    tid = _tx(conn)
    sid = _speaker(conn)
    _map(conn, tid, sid)
    vid = tv.write_back(conn, tid)
    row = conn.execute('SELECT * FROM transcript_versions WHERE id = ?', (vid,)).fetchone()
    assert row['is_original'] == 1
    assert row['created_at'] == '2026-01-01 10:00:00'
    assert row['polished_text'] == 'Відполірований старий'
    assert json.loads(row['speaker_map_json'])[0]['speaker_id'] == sid
    assert conn.execute('SELECT active_version_id FROM transcriptions WHERE id=?',
                        (tid,)).fetchone()[0] == vid


def test_write_back_set_pointer_refreshes_only_active(conn):
    tid = _tx(conn)
    first = tv.write_back(conn, tid)
    other = _new(conn, tid)
    conn.execute("UPDATE transcriptions SET transcript_text = 'Правка вручну' WHERE id = ?", (tid,))
    assert tv.write_back(conn, tid) == first
    assert conn.execute('SELECT transcript_text FROM transcript_versions WHERE id=?',
                        (first,)).fetchone()[0] == 'Правка вручну'
    assert conn.execute('SELECT transcript_text FROM transcript_versions WHERE id=?',
                        (other,)).fetchone()[0] == NEW


def test_activate_copies_columns_and_updates_fts(conn):
    tid = _tx(conn)
    assert _fts(conn, 'бюджет') == [tid]
    vid = _new(conn, tid)
    tv.activate(conn, tid, vid)
    rec = conn.execute('SELECT * FROM transcriptions WHERE id = ?', (tid,)).fetchone()
    assert rec['transcript_text'] == NEW and rec['model_used'] == 'large-v3'
    assert rec['language'] == 'ru' and rec['polished_text'] is None
    assert rec['active_version_id'] == vid
    assert _fts(conn, 'маркетинговую') == [tid]
    assert _fts(conn, 'бюджет') == []


def test_activate_back_restores_original_polish_and_speakers(conn):
    tid = _tx(conn)
    sid = _speaker(conn)
    _map(conn, tid, sid)
    vid = _new(conn, tid)
    tv.activate(conn, tid, vid)
    assert conn.execute('SELECT COUNT(*) FROM transcription_speaker_map').fetchone()[0] == 0
    orig = conn.execute('SELECT id FROM transcript_versions WHERE is_original=1').fetchone()[0]
    tv.activate(conn, tid, orig)
    rec = conn.execute('SELECT * FROM transcriptions WHERE id = ?', (tid,)).fetchone()
    assert rec['transcript_text'] == OLD and rec['polished_text'] == 'Відполірований старий'
    rows = conn.execute('SELECT speaker_id FROM transcription_speaker_map').fetchall()
    assert [r[0] for r in rows] == [sid]


def test_activate_drops_missing_speaker_and_rejects_foreign_vid(conn):
    tid, other = _tx(conn), _tx(conn, 'Інший запис')
    vid = tv.insert_version(conn, tid, model_used='m', language='uk', processing_time=1,
                            transcript_text=NEW, segments=[],
                            speaker_map=[{'raw_label': 'SPEAKER_01', 'speaker_id': 9999,
                                          'embedding': [0.5, 0.25]}])
    tv.activate(conn, tid, vid)
    r = conn.execute('SELECT speaker_id, embedding FROM transcription_speaker_map').fetchone()
    assert r[0] is None and r[1] is not None
    with pytest.raises(LookupError):
        tv.activate(conn, other, vid)


def test_evict_keeps_three_never_active_even_if_oldest(conn):
    tid = _tx(conn)
    ids = [tv.write_back(conn, tid)]
    for i in range(3):
        v = _new(conn, tid, f'Текст {i}')
        conn.execute('UPDATE transcript_versions SET created_at = ? WHERE id = ?',
                     (f'2026-02-0{i + 1} 10:00:00', v))
        ids.append(v)
    # The original (oldest) is active: it survives and the next oldest goes.
    assert tv.evict(conn, tid) == [ids[1]]
    left = [r[0] for r in conn.execute('SELECT id FROM transcript_versions ORDER BY id')]
    assert left == [ids[0], ids[2], ids[3]]


def test_evict_removes_oldest_by_created_at_then_id(conn):
    tid = _tx(conn)
    first = tv.write_back(conn, tid)
    vids = [_new(conn, tid, f'Текст {i}') for i in range(3)]
    for i, v in enumerate(vids):
        conn.execute('UPDATE transcript_versions SET created_at = ? WHERE id = ?',
                     (f'2026-02-0{i + 1} 10:00:00', v))
    assert tv.evict(conn, tid) == [vids[0]]
    left = [r[0] for r in conn.execute('SELECT id FROM transcript_versions ORDER BY id')]
    assert left == [first, vids[1], vids[2]]


def test_evict_protects_the_main_the_owner_had_before_the_run(conn):
    # Council round 1, ask 4: the owner-chosen main is the oldest row; a re-run must not drop it.
    tid = _tx(conn)
    orig = tv.write_back(conn, tid)
    chosen = _new(conn, tid, 'Обрана власником', 'large-v3')
    conn.execute('UPDATE transcript_versions SET created_at = ? WHERE id = ?',
                 ('2026-02-01 10:00:00', chosen))
    tv.activate(conn, tid, chosen)
    assert tv.evict(conn, tid) == []
    third = _new(conn, tid, 'Третя', 'medium')
    conn.execute('UPDATE transcript_versions SET created_at = ? WHERE id = ?',
                 ('2026-02-02 10:00:00', third))
    tv.activate(conn, tid, third)
    tv.evict(conn, tid)  # 3 rows: original, chosen, third
    assert conn.execute('SELECT COUNT(*) FROM transcript_versions').fetchone()[0] == 3
    # Owner makes `chosen` main again; the next run (write_back -> insert -> activate -> evict).
    tv.activate(conn, tid, chosen)
    previous_main = tv.write_back(conn, tid)
    assert previous_main == chosen
    conn.execute('UPDATE transcript_versions SET created_at = ? WHERE id = ?',
                 ('2026-01-01 09:00:00', chosen))  # make it the oldest row outright
    fourth = _new(conn, tid, 'Четверта', 'small')
    tv.activate(conn, tid, fourth)
    assert tv.evict(conn, tid, protect=(previous_main,)) == [orig]
    left = [r[0] for r in conn.execute('SELECT id FROM transcript_versions ORDER BY id')]
    assert left == [chosen, third, fourth]


def test_activate_already_main_keeps_canonical_edits_and_live_speakers(conn):
    # Council round 1, finding 1: re-activating the main version must be a no-op.
    tid = _tx(conn)
    tv.write_back(conn, tid)
    vid = _new(conn, tid)
    tv.activate(conn, tid, vid)
    conn.execute("UPDATE transcriptions SET polished_text = 'ВІДПОЛІРОВАНО', "
                 "transcript_text = 'Правка після перемикання' WHERE id = ?", (tid,))
    _map(conn, tid, _speaker(conn))
    tv.activate(conn, tid, vid)
    rec = conn.execute('SELECT * FROM transcriptions WHERE id = ?', (tid,)).fetchone()
    assert rec['transcript_text'] == 'Правка після перемикання'
    assert rec['polished_text'] == 'ВІДПОЛІРОВАНО'
    assert rec['active_version_id'] == vid
    assert conn.execute('SELECT COUNT(*) FROM transcription_speaker_map WHERE transcription_id = ?',
                        (tid,)).fetchone()[0] == 1


def test_list_versions_empty_until_pointer_set(conn):
    tid = _tx(conn)
    assert tv.list_versions(conn, tid) == {'active_version_id': None, 'versions': []}
    vid = _new(conn, tid)
    tv.activate(conn, tid, vid)
    res = tv.list_versions(conn, tid)
    assert res['active_version_id'] == vid and len(res['versions']) == 2
    by = {v['id']: v for v in res['versions']}
    assert by[vid]['is_active'] and not by[vid]['has_polished']
    orig = next(v for v in res['versions'] if v['is_original'])
    assert orig['has_polished'] and orig['segment_count'] == 1


def test_get_version_active_reads_canonical_and_live_speakers(conn):
    tid = _tx(conn)
    vid = _new(conn, tid)
    tv.activate(conn, tid, vid)
    sid = _speaker(conn, 'Ігор')
    conn.execute("UPDATE transcriptions SET transcript_text = 'Свіжа правка' WHERE id = ?", (tid,))
    _map(conn, tid, sid)
    v = tv.get_version(conn, tid, vid)
    assert v['is_active'] and v['transcript_text'] == 'Свіжа правка'
    assert v['speaker_map'] == [{'raw_label': 'SPEAKER_00', 'speaker_id': sid, 'name': 'Ігор'}]
    assert tv.get_version(conn, tid, 99999) is None


def test_bulk_delete_route_removes_versions(tmp_path):
    db = str(tmp_path / 'bd.db')
    init_database(db)
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.config['TESTING'] = True
    app.config['DATABASE'] = db
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    tid = _tx(c)
    tv.activate(c, tid, _new(c, tid))
    c.commit()
    assert c.execute('SELECT COUNT(*) FROM transcript_versions').fetchone()[0] == 2
    r = app.test_client().post('/api/history/bulk_delete', json={'ids': [tid]})
    assert r.status_code == 200
    c2 = sqlite3.connect(db)
    assert c2.execute('SELECT COUNT(*) FROM transcript_versions').fetchone()[0] == 0
    assert c2.execute('SELECT COUNT(*) FROM transcriptions').fetchone()[0] == 0
    c.close()
    c2.close()
