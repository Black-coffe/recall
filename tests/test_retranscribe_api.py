"""Re-transcribe job + versions routes (ADR-044): refusals, success, eviction, activate, list fields."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from app import state
from app.blueprints.audio_library import audio_bp
from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database
from app.services import diarization_service as ds
from app.services import reembed, retranscribe
from app.services import transcript_versions as tv
from app.services.job_queue import JobQueue

OLD = 'Добрий день, обговорюємо бюджет проєкту'
NEW = 'Здравствуйте, обсуждаем маркетинговую стратегию'


class FakeWhisper:
    def __init__(self, text=NEW):
        self.text = text
        self.calls = []

    def get_available_models(self):
        return [{'name': 'large-v3', 'downloaded': True}, {'name': 'tiny', 'downloaded': False}]

    def transcribe_with_progress(self, path, model, language, **kw):
        self.calls.append((path, model, language))
        cb = kw.get('progress_callback')
        if cb:
            cb({'status': 'processing', 'progress': 50})
        return {'text': self.text, 'language': language,
                'segments': [{'start': 0.0, 'end': 2.0, 'text': self.text}]}


class FakeDiarService:
    available = True

    @classmethod
    def get_instance(cls):
        return cls()

    def is_available(self):
        return self.available


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / 'rt.db')
    init_database(db)
    media = tmp_path / 'call.mp3'
    media.write_bytes(b'x')
    app = Flask(__name__)
    app.register_blueprint(transcription_bp)
    app.register_blueprint(audio_bp)
    app.config.update(TESTING=True, DATABASE=db)
    queue = JobQueue(executor=ThreadPoolExecutor(max_workers=1),
                     slow_executor=ThreadPoolExecutor(max_workers=1))
    whisper = FakeWhisper()
    monkeypatch.setattr(state, 'job_queue', queue, raising=False)
    monkeypatch.setattr(state, 'whisper_manager', whisper, raising=False)
    monkeypatch.setattr(retranscribe, 'recording_active', lambda **kw: False)
    reembed_calls = []
    monkeypatch.setattr(reembed, 'schedule_record_reembed',
                        lambda tid, *, db_path: reembed_calls.append((tid, db_path)) or 'queued')
    monkeypatch.setattr(ds, 'DiarizationService', FakeDiarService)
    return type('Env', (), {'app': app, 'client': app.test_client(), 'db': db, 'media': str(media),
                            'queue': queue, 'whisper': whisper, 'reembed': reembed_calls,
                            'mp': monkeypatch})()


def _db(env):
    c = sqlite3.connect(env.db)
    c.row_factory = sqlite3.Row
    return c


def _tx(env, *, file_path='auto', source_type='file', segments=None):
    path = env.media if file_path == 'auto' else file_path
    segs = segments if segments is not None else [{'start': 0, 'end': 2, 'text': OLD}]
    with _db(env) as c:
        cur = c.execute(
            "INSERT INTO transcriptions (source_type, source_name, file_path, transcript_text, "
            "segments, language, model_used, created_at) VALUES (?, 'Дзвінок', ?, ?, ?, 'uk', "
            "'small', '2026-01-01 10:00:00')",
            (source_type, path, OLD, json.dumps(segs, ensure_ascii=False)))
        return cur.lastrowid


def _run(env, tid, model='large-v3', **body):
    resp = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': model, **body})
    if resp.status_code == 202:
        env.queue.get(resp.get_json()['job_id'])._future.result(timeout=10)
    return resp


def _job(env, resp):
    return env.queue.get(resp.get_json()['job_id'])


def _versions(env, tid):
    with _db(env) as c:
        return c.execute('SELECT * FROM transcript_versions WHERE transcription_id = ? '
                         'ORDER BY id', (tid,)).fetchall()


def _pointer(env, tid):
    with _db(env) as c:
        return c.execute('SELECT active_version_id FROM transcriptions WHERE id = ?',
                         (tid,)).fetchone()[0]


# ---- refusals --------------------------------------------------------------------

def test_refuses_record_without_media(env, tmp_path):
    for path in (None, '', str(tmp_path / 'gone.mp3')):
        tid = _tx(env, file_path=path)
        resp = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': 'large-v3'})
        assert resp.status_code == 404
        assert resp.get_json()['error_code'] == 'NO_MEDIA'


def test_refuses_unknown_record(env):
    resp = env.client.post('/api/history/9999/retranscribe', json={'model': 'large-v3'})
    assert resp.status_code == 404
    assert resp.get_json()['error_code'] == 'NOT_FOUND'


def test_refuses_model_not_installed(env):
    tid = _tx(env)
    for model in ('tiny', 'nonexistent', ''):
        resp = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': model})
        assert resp.status_code == 400
        assert resp.get_json()['error_code'] == 'MODEL_NOT_INSTALLED'


def test_refuses_during_recording(env):
    tid = _tx(env)
    env.mp.setattr(retranscribe, 'recording_active', lambda **kw: True)
    resp = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': 'large-v3'})
    assert resp.status_code == 409
    assert resp.get_json()['error_code'] == 'RECORDING_ACTIVE'
    assert _versions(env, tid) == []


def test_job_fails_when_recording_started_while_waiting(env):
    tid = _tx(env)
    calls = {'n': 0}

    def probe(**kw):
        calls['n'] += 1
        return calls['n'] > 1  # free at submit, busy when the job starts

    env.mp.setattr(retranscribe, 'recording_active', probe)
    resp = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': 'large-v3'})
    assert resp.status_code == 202
    job = _job(env, resp)
    with pytest.raises(retranscribe.RetranscribeError):
        job._future.result(timeout=10)
    assert job.state == 'failed'
    assert job.error_code == 'RECORDING_ACTIVE'
    assert env.whisper.calls == []
    assert _versions(env, tid) == [] and _pointer(env, tid) is None


def test_already_running_returns_live_job_and_clears_after(env):
    tid = _tx(env)
    gate = __import__('threading').Event()
    original = env.whisper.transcribe_with_progress

    def slow(*a, **kw):
        gate.wait(10)
        return original(*a, **kw)

    env.whisper.transcribe_with_progress = slow
    first = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': 'large-v3'})
    assert first.status_code == 202
    job_id = first.get_json()['job_id']
    assert retranscribe.running_job_id(tid) == job_id
    second = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': 'large-v3'})
    assert second.status_code == 409
    assert second.get_json()['error_code'] == 'ALREADY_RUNNING'
    assert second.get_json()['job_id'] == job_id
    listing = env.client.get(f'/api/history/{tid}/versions').get_json()
    assert listing['running_job_id'] == job_id
    gate.set()
    env.queue.get(job_id)._future.result(timeout=10)
    assert retranscribe.running_job_id(tid) is None


# ---- success ---------------------------------------------------------------------

def test_success_creates_main_version_and_reembeds(env):
    tid = _tx(env)
    resp = _run(env, tid, language='ru')
    assert resp.status_code == 202
    body = resp.get_json()
    assert body['transcription_id'] == tid and body['model'] == 'large-v3'
    job = _job(env, resp)
    assert job.state == 'completed'
    assert job.meta['stage'] == 'done' and job.meta['progress'] == 100
    rows = _versions(env, tid)
    assert [r['is_original'] for r in rows] == [1, 0]
    new = rows[1]
    assert new['model_used'] == 'large-v3' and new['language'] == 'ru'
    assert new['polished_text'] is None and new['processing_time'] is not None
    with _db(env) as c:
        rec = c.execute('SELECT * FROM transcriptions WHERE id = ?', (tid,)).fetchone()
    assert rec['active_version_id'] == new['id']
    assert rec['transcript_text'] == NEW and rec['model_used'] == 'large-v3'
    assert env.reembed == [(tid, env.db)]
    assert env.whisper.calls[0][2] == 'ru'
    result = job._future.result()
    assert result == {'version_id': new['id'], 'evicted_version_ids': [], 'model': 'large-v3',
                      'segment_count': 1, 'reembed': 'queued'}


def test_language_defaults_to_the_records(env):
    tid = _tx(env)
    _run(env, tid)
    assert env.whisper.calls[0][2] == 'uk'
    assert _versions(env, tid)[1]['language'] == 'uk'


def test_plain_file_is_not_diarized(env):
    tid = _tx(env)
    env.mp.setattr(ds, 'run_diarization_for_audio',
                   lambda *a, **k: pytest.fail('diarization must not run'))
    assert _run(env, tid).status_code == 202
    assert json.loads(_versions(env, tid)[1]['speaker_map_json']) == []


def _fake_diar(env, labels=('SPEAKER_00',), fail=False):
    def run(path, segs, **kw):
        assert kw.get('return_embeddings') is True
        if fail:
            raise RuntimeError('diarization exploded')
        return ([{**s, 'speaker': labels[0]} for s in segs], list(labels), 1.0,
                {l: [0.1, 0.2, 0.3] for l in labels})
    env.mp.setattr(ds, 'run_diarization_for_audio', run)


def test_diarizes_when_main_has_speakers_and_matches_voice(env):
    tid = _tx(env, segments=[{'start': 0, 'end': 2, 'text': OLD, 'speaker': 'SPEAKER_00'}])
    _fake_diar(env)
    with _db(env) as c:
        sid = c.execute("INSERT INTO speakers (name, embedding) VALUES ('Олена', ?)",
                        (tv._list_to_blob([0.1, 0.2, 0.3]),)).lastrowid
    env.mp.setattr(ds, 'find_matching_speaker', lambda emb, saved: (sid, 0.99))
    assert _run(env, tid).status_code == 202
    new = _versions(env, tid)[1]
    assert json.loads(new['segments'])[0]['speaker'] == 'SPEAKER_00'
    entry = json.loads(new['speaker_map_json'])[0]
    assert entry['raw_label'] == 'SPEAKER_00' and entry['speaker_id'] == sid
    assert entry['embedding'] == pytest.approx([0.1, 0.2, 0.3])
    with _db(env) as c:  # the live speaker map mirrors the new main version
        row = c.execute('SELECT speaker_id FROM transcription_speaker_map WHERE transcription_id = ?',
                        (tid,)).fetchone()
    assert row['speaker_id'] == sid


def test_recording_and_explicit_flag_trigger_diarization(env):
    _fake_diar(env)
    rec = _tx(env, source_type='recording')
    flagged = _tx(env)
    assert _run(env, rec).status_code == 202
    assert _run(env, flagged, diarize=True).status_code == 202
    for tid in (rec, flagged):
        assert json.loads(_versions(env, tid)[1]['speaker_map_json'])[0]['raw_label'] == 'SPEAKER_00'


def test_failed_diarization_leaves_no_version_and_no_pointer_change(env):
    tid = _tx(env, source_type='recording')
    _fake_diar(env, fail=True)
    resp = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': 'large-v3'})
    job = _job(env, resp)
    with pytest.raises(RuntimeError):
        job._future.result(timeout=10)
    assert job.state == 'failed'
    assert _versions(env, tid) == [] and _pointer(env, tid) is None
    with _db(env) as c:
        assert c.execute('SELECT transcript_text FROM transcriptions WHERE id = ?',
                         (tid,)).fetchone()[0] == OLD
    assert env.reembed == []
    assert retranscribe.running_job_id(tid) is None


def test_failure_inside_the_transaction_rolls_everything_back(env):
    tid = _tx(env)
    env.mp.setattr(tv, 'evict', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    resp = env.client.post(f'/api/history/{tid}/retranscribe', json={'model': 'large-v3'})
    with pytest.raises(RuntimeError):
        _job(env, resp)._future.result(timeout=10)
    assert _versions(env, tid) == [] and _pointer(env, tid) is None
    with _db(env) as c:
        assert c.execute('SELECT transcript_text FROM transcriptions WHERE id = ?',
                         (tid,)).fetchone()[0] == OLD
    assert env.reembed == []


# ---- eviction --------------------------------------------------------------------

def test_fourth_version_evicts_the_original_and_reports_it(env):
    tid = _tx(env)  # the original counts: it becomes a row on the first run
    first = _job(env, _run(env, tid))._future.result()
    second = _job(env, _run(env, tid))._future.result()
    assert first['evicted_version_ids'] == [] and second['evicted_version_ids'] == []
    original_id = [r['id'] for r in _versions(env, tid) if r['is_original']][0]
    third = _job(env, _run(env, tid))._future.result()
    assert third['evicted_version_ids'] == [original_id]
    rows = _versions(env, tid)
    assert len(rows) == 3 and not any(r['is_original'] for r in rows)
    assert _pointer(env, tid) == third['version_id']


# ---- versions routes -------------------------------------------------------------

def test_versions_list_get_and_activate(env):
    tid = _tx(env)
    empty = env.client.get(f'/api/history/{tid}/versions').get_json()
    assert empty == {'success': True, 'active_version_id': None, 'running_job_id': None,
                     'versions': []}
    new_id = _job(env, _run(env, tid))._future.result()['version_id']
    listing = env.client.get(f'/api/history/{tid}/versions').get_json()
    assert listing['active_version_id'] == new_id
    assert [v['is_active'] for v in listing['versions']] == [False, True]
    original_id = listing['versions'][0]['id']

    got = env.client.get(f'/api/history/{tid}/versions/{new_id}').get_json()
    assert got['version']['transcript_text'] == NEW and got['version']['is_active'] is True
    other = env.client.get(f'/api/history/{tid}/versions/{original_id}').get_json()
    assert other['version']['transcript_text'] == OLD

    env.reembed.clear()
    act = env.client.post(f'/api/history/{tid}/versions/{original_id}/activate')
    assert act.status_code == 200
    assert act.get_json() == {'success': True, 'active_version_id': original_id, 'reembed': 'queued'}
    assert env.reembed == [(tid, env.db)]
    with _db(env) as c:
        assert c.execute('SELECT transcript_text FROM transcriptions WHERE id = ?',
                         (tid,)).fetchone()[0] == OLD


def test_version_routes_404(env):
    tid = _tx(env)
    other = _tx(env)
    _job(env, _run(env, other))._future.result()
    foreign = _versions(env, other)[0]['id']
    assert env.client.get('/api/history/9999/versions').status_code == 404
    assert env.client.get(f'/api/history/{tid}/versions/{foreign}').status_code == 404
    resp = env.client.post(f'/api/history/{tid}/versions/{foreign}/activate')
    assert resp.status_code == 404 and resp.get_json()['error_code'] == 'NOT_FOUND'
    assert env.client.post('/api/history/9999/versions/1/activate').status_code == 404


def test_activate_refused_while_a_rerun_is_in_flight(env):
    tid = _tx(env)
    env.mp.setitem(retranscribe._inflight, tid, 'job-live')
    resp = env.client.post(f'/api/history/{tid}/versions/1/activate')
    assert resp.status_code == 409
    assert resp.get_json()['error_code'] == 'ALREADY_RUNNING'
    assert resp.get_json()['job_id'] == 'job-live'


# ---- list fields -----------------------------------------------------------------

def test_history_carries_has_media_and_active_version_id(env, tmp_path):
    with_media = _tx(env)
    without = _tx(env, file_path=str(tmp_path / 'missing.mp3'))
    new_id = _job(env, _run(env, with_media))._future.result()['version_id']
    items = {i['id']: i for i in env.client.get('/api/history').get_json()['transcriptions']}
    assert items[with_media]['has_media'] is True
    assert items[with_media]['active_version_id'] == new_id
    assert items[without]['has_media'] is False and items[without]['active_version_id'] is None
    assert 'file_path' not in items[with_media]
    one = env.client.get(f'/api/history/{with_media}').get_json()
    assert one['has_media'] is True and one['active_version_id'] == new_id
    assert env.client.get(f'/api/history/{without}').get_json()['has_media'] is False


def test_audio_downloads_carry_transcription_id(env):
    tid = _tx(env)
    with _db(env) as c:
        c.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type) "
                  "VALUES ('u1', 'a1', 'Дзвінок', ?, 'file')", (env.media,))
        c.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, file_path, source_type) "
                  "VALUES ('u2', 'a2', 'Без тексту', '/nowhere.mp3', 'file')")
    items = {i['title']: i for i in env.client.get('/api/audio/downloads').get_json()['downloads']}
    assert items['Дзвінок']['transcription_id'] == tid
    assert items['Без тексту']['transcription_id'] is None
