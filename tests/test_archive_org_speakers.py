"""Agent (non-UI) speaker writes: existing ids only, no embedding fold, self speaker protected."""
from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from app.blueprints.speakers import speakers_bp
from app.core import auth
from app.db.migrations import init_database
from app.services.diarization_service import embedding_to_blob

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "t.db")
    init_database(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tid = conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text) "
                       "VALUES ('file', 'Дзвінок', 'x')").lastrowid
    a = conn.execute("INSERT INTO speakers (name, embedding, usage_count) VALUES ('Андрій', ?, 2)",
                     (b'\x01\x02\x03\x04',)).lastrowid
    conn.execute("INSERT INTO speakers (name) VALUES ('Олена Коваль')")
    me = conn.execute("SELECT id FROM speakers WHERE is_self = 1").fetchone()['id']
    emb = embedding_to_blob([1.0, 0.0, 0.0, 0.0])
    conn.execute("INSERT INTO transcription_speaker_map (transcription_id, raw_label, embedding) "
                 "VALUES (?, 'SPEAKER_01', ?)", (tid, emb))
    conn.commit(); conn.close()
    app = Flask(__name__)
    app.config['DATABASE'] = db
    app.register_blueprint(speakers_bp)
    return app.test_client(), tid, a, me, db


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _patch(c, tid, mapping, headers=None):
    return c.patch(f'/api/transcriptions/{tid}/speakers', json={'mapping': mapping}, headers=headers or {})


def test_agent_name_string_refused(env):
    c, tid, a, me, db = env
    r = _patch(c, tid, {'SPEAKER_01': 'Андрій'})
    assert r.status_code == 400 and r.get_json()['error_code'] == 'SPEAKER_ID_REQUIRED'


def test_agent_unknown_and_self_refused_and_atomic(env):
    c, tid, a, me, db = env
    r = _patch(c, tid, {'SPEAKER_01': a, 'SPEAKER_02': 9999})
    assert r.status_code == 404 and r.get_json()['error_code'] == 'NOT_FOUND'
    r = _patch(c, tid, {'SPEAKER_01': a, 'SPEAKER_02': me})
    assert r.status_code == 403 and r.get_json()['error_code'] == 'IS_SELF'
    assert _q(db, "SELECT speaker_id, source FROM transcription_speaker_map WHERE raw_label='SPEAKER_01'") \
        == [(None, None)]


def test_agent_map_sets_mcp_and_keeps_embedding(env):
    c, tid, a, me, db = env
    n = _q(db, "SELECT count(*) FROM speakers")[0][0]
    r = _patch(c, tid, {'SPEAKER_01': a})
    assert r.status_code == 200
    assert _q(db, "SELECT speaker_id, source FROM transcription_speaker_map WHERE raw_label='SPEAKER_01'") \
        == [(a, 'mcp')]
    assert _q(db, "SELECT embedding FROM speakers WHERE id=?", a)[0][0] == b'\x01\x02\x03\x04'
    assert _q(db, "SELECT count(*) FROM speakers")[0][0] == n
    r = _patch(c, tid, {'SPEAKER_01': None})
    assert r.status_code == 200
    assert _q(db, "SELECT speaker_id FROM transcription_speaker_map WHERE raw_label='SPEAKER_01'") == [(None,)]


def test_agent_rename(env):
    c, tid, a, me, db = env
    r = c.put(f'/api/speakers/{me}', json={'name': 'Хтось'})
    assert r.status_code == 403 and r.get_json()['error_code'] == 'IS_SELF'
    r = c.put(f'/api/speakers/{a}', json={'name': 'Андрій Б'})
    assert r.status_code == 200 and r.get_json()['speaker']['name'] == 'Андрій Б'


def test_ui_map_creates_and_folds(env):
    c, tid, a, me, db = env
    r = _patch(c, tid, {'SPEAKER_01': 'Максим'}, headers=UI)
    assert r.status_code == 200
    rows = _q(db, "SELECT s.name, m.source FROM transcription_speaker_map m "
                  "JOIN speakers s ON s.id=m.speaker_id WHERE raw_label='SPEAKER_01'")
    assert rows == [('Максим', None)]
    assert _q(db, "SELECT embedding FROM speakers WHERE name='Максим'")[0][0] is not None
