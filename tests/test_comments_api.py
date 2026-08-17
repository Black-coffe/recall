"""Шар коментарів, Волна 1: HTTP-контракт.

`state.job_queue` тут None, тож блюпринт індексує синхронно — це і перевіряє
тест `test_create_indexes_synchronously_without_queue`: коментар, який ніколи
не проіндексувався, гірший за повільний POST.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
from flask import Flask

from app.blueprints.comments import comments_bp
from app.db.migrations import init_database
from app.services import comments as svc
from app.services import embeddings


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    return a / (np.linalg.norm(a) or 1.0)


@pytest.fixture(autouse=True)
def mock_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))


@pytest.fixture
def client_and_tx(tmp_path: Path):
    db_path = str(tmp_path / 'comments_api.db')
    init_database(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text) "
        "VALUES ('file', 'Дзвінок', 'текст')")
    tid = cur.lastrowid
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path) "
                 "VALUES ('u', 'yid', 'Аудіо', 'f.mp3')")
    aid = conn.execute("SELECT id FROM audio_downloads").fetchone()[0]
    conn.commit(); conn.close()

    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(comments_bp)
    return app.test_client(), tid, aid, db_path


def test_meta_exposes_kinds_and_weights(client_and_tx):
    c, *_ = client_and_tx
    d = c.get('/api/comments/meta').get_json()
    keys = [k['key'] for k in d['kinds']]
    assert keys[0] == 'correction'          # найважчий — першим
    assert d['default_kind'] == 'note'
    assert 'transcription' in d['targets'] and 'audio_download' in d['targets']


def test_create_list_update_delete_cycle(client_and_tx):
    c, tid, _, _ = client_and_tx
    r = c.post('/api/comments', json={
        'target_type': 'transcription', 'target_id': tid,
        'body': 'Насправді сума 12k', 'kind': 'correction'})
    assert r.status_code == 201
    cid = r.get_json()['comment']['id']

    got = c.get(f'/api/comments?target_type=transcription&target_id={tid}').get_json()
    assert [x['body'] for x in got['comments']] == ['Насправді сума 12k']

    r2 = c.patch(f'/api/comments/{cid}', json={'pinned': True})
    assert r2.get_json()['comment']['pinned'] is True

    assert c.delete(f'/api/comments/{cid}').status_code == 200
    assert c.get(f'/api/comments?target_type=transcription&target_id={tid}'
                 ).get_json()['comments'] == []
    assert c.post(f'/api/comments/{cid}/restore').status_code == 200


def test_create_indexes_synchronously_without_queue(client_and_tx):
    c, tid, _, db_path = client_and_tx
    r = c.post('/api/comments', json={
        'target_type': 'transcription', 'target_id': tid, 'body': 'уточнення'})
    assert r.get_json()['comment']['index_status'] == 'embedded'
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM comment_chunks").fetchone()[0]
    conn.close()
    assert n == 1


def test_edit_body_requeues_index(client_and_tx):
    c, tid, _, _ = client_and_tx
    cid = c.post('/api/comments', json={
        'target_type': 'transcription', 'target_id': tid,
        'body': 'стара'}).get_json()['comment']['id']
    body = c.patch(f'/api/comments/{cid}', json={'body': 'нова'}).get_json()
    # Правка скидає позначку, і блюпринт мусить переіндексувати одразу —
    # інакше пошук віддавав би текст, якого на картці вже немає.
    assert body['comment']['index_status'] == 'embedded'


def test_counts_endpoint(client_and_tx):
    c, tid, aid, _ = client_and_tx
    c.post('/api/comments', json={'target_type': 'transcription',
                                  'target_id': tid, 'body': 'a'})
    c.post('/api/comments', json={'target_type': 'audio_download',
                                  'target_id': aid, 'body': 'b'})
    d = c.post('/api/comments/counts',
               json={'target_type': 'transcription', 'ids': [tid, 999]}).get_json()
    assert d['counts'] == {str(tid): {'n': 1, 'corrections': 0}}


def test_comment_on_media_card(client_and_tx):
    c, _, aid, _ = client_and_tx
    r = c.post('/api/comments', json={
        'target_type': 'audio_download', 'target_id': aid,
        'body': 'з 14:20 говорить підрядник'})
    assert r.status_code == 201


def test_bad_input_is_400_not_500(client_and_tx):
    c, tid, _, _ = client_and_tx
    assert c.post('/api/comments', json={'target_type': 'planet',
                                         'target_id': 1, 'body': 'x'}).status_code == 400
    assert c.post('/api/comments', json={'target_type': 'transcription',
                                         'target_id': tid, 'body': '  '}).status_code == 400
    assert c.post('/api/comments', json={'target_type': 'transcription',
                                         'target_id': 99999, 'body': 'x'}).status_code == 400
    assert c.get('/api/comments?target_type=transcription').status_code == 400
    assert c.patch('/api/comments/12345', json={'body': 'x'}).status_code == 404
    assert c.delete('/api/comments/12345').status_code == 404


def test_recent_feed(client_and_tx):
    c, tid, _, _ = client_and_tx
    c.post('/api/comments', json={'target_type': 'transcription', 'target_id': tid,
                                  'body': 'a', 'kind': 'correction'})
    c.post('/api/comments', json={'target_type': 'transcription', 'target_id': tid,
                                  'body': 'b'})
    assert len(c.get('/api/comments/recent').get_json()['comments']) == 2
    assert len(c.get('/api/comments/recent?kind=correction').get_json()['comments']) == 1


def test_analyze_endpoint_returns_derived(client_and_tx, monkeypatch):
    from app.services import enrichment, text_polishing
    monkeypatch.setattr(enrichment, "is_available", lambda: True)
    monkeypatch.setattr(text_polishing, "extract_comment_items",
                        lambda *a, **kw: {
                            "action_items": [{"task": "переписати договір",
                                              "owner": "власник", "due": None,
                                              "due_date": None}],
                            "people": [], "projects": [], "orgs": [],
                            "model": "claude-test", "input_tokens": 1,
                            "output_tokens": 1, "cache_read_tokens": 0})
    c, tid, _, _ = client_and_tx
    cid = c.post('/api/comments', json={
        'target_type': 'transcription', 'target_id': tid,
        'body': 'треба переписати договір'}).get_json()['comment']['id']

    r = c.post(f'/api/comments/{cid}/analyze')
    assert r.status_code == 200
    assert r.get_json()['result']['counts']['action_items'] == 1
    assert r.get_json()['comment']['analyzed'] is True

    d = c.get(f'/api/comments/{cid}/derived').get_json()
    assert [t['task'] for t in d['action_items']] == ['переписати договір']


def test_analyze_without_api_key_is_503(client_and_tx, monkeypatch):
    from app.services import enrichment
    monkeypatch.setattr(enrichment, "is_available", lambda: False)
    c, tid, _, _ = client_and_tx
    cid = c.post('/api/comments', json={
        'target_type': 'transcription', 'target_id': tid,
        'body': 'x'}).get_json()['comment']['id']
    assert c.post(f'/api/comments/{cid}/analyze').status_code == 503


def test_analyze_without_anchor_is_409(client_and_tx, monkeypatch):
    from app.services import enrichment
    monkeypatch.setattr(enrichment, "is_available", lambda: True)
    c, _, _, _ = client_and_tx
    cid = c.post('/api/comments', json={
        'target_type': 'recording_session', 'target_id': 3,
        'body': 'по ходу дзвінка', 'source': 'live'}).get_json()['comment']['id']
    r = c.post(f'/api/comments/{cid}/analyze')
    assert r.status_code == 409
    assert r.get_json()['result']['status'] == 'no_anchor'


def test_analyze_unknown_comment_is_404(client_and_tx):
    c, *_ = client_and_tx
    assert c.post('/api/comments/54321/analyze').status_code == 404
    assert c.get('/api/comments/54321/derived').status_code == 404


def test_create_links_graph_without_claude(client_and_tx):
    """Шар А працює на інжесті сам: жодного ключа, жодної кнопки."""
    c, tid, _, db_path = client_and_tx
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO entities (type, canonical_name, normalized_name) "
                 "VALUES ('project', 'Acmecorp', 'acmecorp')")
    eid = conn.execute("SELECT id FROM entities").fetchone()[0]
    conn.commit(); conn.close()

    c.post('/api/comments', json={'target_type': 'transcription',
                                  'target_id': tid, 'body': 'по Acmecorp все стало'})
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT entity_id, source FROM meeting_entities "
                       "WHERE transcription_id = ?", (tid,)).fetchone()
    conn.close()
    assert row == (eid, 'comment')


def test_service_and_api_agree_on_kinds(client_and_tx):
    """UI бере ваги з /api/comments/meta саме щоб не розійтись із ранжуванням —
    тест фіксує, що ендпоінт віддає ті самі ваги, які застосовує retrieval."""
    c, *_ = client_and_tx
    d = c.get('/api/comments/meta').get_json()
    assert {k['key']: k['weight'] for k in d['kinds']} == svc.KIND_WEIGHTS
