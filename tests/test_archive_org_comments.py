"""Agent (non-UI) comments: forced provenance, owner-row guard, dedupe, confirm, search weight."""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest
from flask import Flask

from app.blueprints.comments import comments_bp
from app.core import auth
from app.db.migrations import init_database
from app.services import comments as svc
from app.services import embeddings, retrieval

UI = {auth.UI_TOKEN_HEADER: auth.ui_token()}


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    return a / np.linalg.norm(a)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_query", lambda q: _unit([1, 0, 0, 0]))
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack([_unit([1, 0, 0, 0]) for _ in texts]))
    db = str(tmp_path / "t.db")
    init_database(db)
    conn = sqlite3.connect(db)
    tid = conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                       "meeting_date) VALUES ('file', 'Дзвінок', 'x', '2026-08-01')").lastrowid
    conn.commit(); conn.close()
    app = Flask(__name__)
    app.config['DATABASE'] = db
    app.register_blueprint(comments_bp)
    return app.test_client(), tid, db


def _post(c, tid, body="Домовились про знижку 15%", headers=None, **extra):
    return c.post('/api/comments', json={'target_type': 'transcription', 'target_id': tid,
                                         'body': body, **extra}, headers=headers or {})


def test_ui_create_keeps_source_and_fields(env):
    c, tid, _ = env
    r = _post(c, tid, headers=UI, pinned=True, author='Я', source='live')
    com = r.get_json()['comment']
    assert r.status_code == 201 and com['source'] == 'live' and com['pinned'] is True
    assert com['author'] == 'Я'


def test_agent_create_forces_mcp_and_strips_keys(env):
    c, tid, _ = env
    r = _post(c, tid, pinned=True, weight=1.0, author='Я', source='ui', parent_id=1)
    com = r.get_json()['comment']
    assert r.status_code == 201
    assert com['source'] == 'mcp' and com['pinned'] is False and com['weight'] is None
    assert com['author'] is None and com['parent_id'] is None and com['confirmed_at'] is None


def test_agent_body_cap(env):
    c, tid, _ = env
    assert _post(c, tid, body='а' * 20001).status_code == 400


def test_agent_dedupe_within_window(env):
    c, tid, _ = env
    first = _post(c, tid).get_json()['comment']
    r = _post(c, tid)
    j = r.get_json()
    assert r.status_code == 200 and j['created'] is False and j['comment']['id'] == first['id']
    assert _post(c, tid, body='Інший текст').status_code == 201


def test_agent_cannot_touch_owner_rows(env):
    c, tid, db = env
    cid = _post(c, tid, headers=UI).get_json()['comment']['id']
    for r in (c.patch(f'/api/comments/{cid}', json={'body': 'x'}),
              c.delete(f'/api/comments/{cid}'),
              c.post(f'/api/comments/{cid}/restore')):
        assert r.status_code == 403 and r.get_json()['error_code'] == 'OWNER_AUTHORED'
    assert svc.get(db, cid)['body'] == 'Домовились про знижку 15%'


def test_ui_update_delete_restore_unchanged(env):
    c, tid, _ = env
    cid = _post(c, tid, headers=UI).get_json()['comment']['id']
    assert c.patch(f'/api/comments/{cid}', json={'body': 'нове'}, headers=UI).status_code == 200
    assert c.delete(f'/api/comments/{cid}', headers=UI).status_code == 200
    assert c.post(f'/api/comments/{cid}/restore', headers=UI).status_code == 200


def test_agent_edit_own_clears_confirmation(env):
    c, tid, db = env
    cid = _post(c, tid).get_json()['comment']['id']
    assert c.post(f'/api/comments/{cid}/confirm', headers=UI).get_json()['comment']['confirmed_at']
    r = c.patch(f'/api/comments/{cid}', json={'body': 'змінено агентом'})
    assert r.status_code == 200 and r.get_json()['comment']['confirmed_at'] is None
    assert c.delete(f'/api/comments/{cid}').status_code == 200
    assert c.post(f'/api/comments/{cid}/restore').status_code == 200


def test_confirm_ui_only_idempotent_noop_on_owner(env):
    c, tid, _ = env
    cid = _post(c, tid).get_json()['comment']['id']
    r = c.post(f'/api/comments/{cid}/confirm')
    assert r.status_code == 403 and r.get_json()['error_code'] == 'USE_UI'
    at = c.post(f'/api/comments/{cid}/confirm', headers=UI).get_json()['comment']['confirmed_at']
    assert at
    assert c.post(f'/api/comments/{cid}/confirm', headers=UI).get_json()['comment']['confirmed_at'] == at
    oid = _post(c, tid, body='своє', headers=UI).get_json()['comment']['id']
    r = c.post(f'/api/comments/{oid}/confirm', headers=UI)
    assert r.status_code == 200 and r.get_json()['comment']['confirmed_at'] is None
    assert c.post('/api/comments/9999/confirm', headers=UI).status_code == 404


def test_kind_weight_neutral_until_confirmed():
    assert svc.kind_weight('correction', None, 'mcp', None) == 0.0
    assert svc.kind_weight('correction', None, 'mcp', '2026-10-20') == 1.0
    assert svc.kind_weight('correction', None, 'ui', None) == 1.0
    assert svc.kind_weight('correction') == 1.0


def test_unconfirmed_agent_comment_ranks_below_owner_until_confirm(env):
    c, tid, db = env
    body = "Домовились про знижку 15%"
    owner = svc.create(db, "transcription", tid, body, kind="correction")
    agent = svc.create(db, "transcription", tid, body, kind="correction", source="mcp")
    for x in (owner, agent):
        svc.index_comment(db, x["id"])

    def scores():
        res = retrieval.search(db, "знижку", top_k=5)["chunks"]
        return {ch["comment_id"]: ch["comment_weight"] for ch in res if ch["source_type"] == "comment"}

    s = scores()
    assert agent["id"] in s and s[agent["id"]] < s[owner["id"]]
    chunks = retrieval.search(db, "знижку", top_k=5)["chunks"]
    order = [ch["comment_id"] for ch in chunks if ch["source_type"] == "comment"]
    assert order.index(owner["id"]) < order.index(agent["id"])
    svc.confirm(db, agent["id"])
    s = scores()
    assert s[agent["id"]] == s[owner["id"]]
