"""Autosave of a stopped recording (spec recording-autosave).

Offline: a dict-backed fake store, a captured timer factory, a temp SQLite archive and a fake
loopback HTTP. Covers the deadline (schedule -> fire, cancel, hold, the /save race), the job's
gates (already transcribed, a user-given name, the direction confidence threshold) and the
blueprint wiring (notification stop redirect, discard guard, hold).
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest
from flask import Flask

from app import state
from app.blueprints import recording as recording_module
from app.blueprints.recording import recording_bp
from app.services.recording.autosave import AutosaveJob, AutosaveScheduler

SID = 'rec_autosave01'


class FakeStore:
    def __init__(self, manifests=None):
        self.m = manifests or {SID: {'session_id': SID, 'status': 'finalized', 'name': None,
                                     'auto_name': 'Запис 2026-10-19 13:28',
                                     'total_duration_sec': 1510.0}}

    def read(self, sid):
        if sid not in self.m:
            raise KeyError(sid)
        return dict(self.m[sid])

    def modify(self, sid, fn):
        fn(self.m[sid])
        return self.m[sid]

    def list_all(self):
        return [dict(v) for v in self.m.values()]


class FakeTimer:
    def __init__(self, delay, fn, args=()):
        self.delay, self.fn, self.args = delay, fn, args
        self.cancelled = False
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn(*self.args)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_scheduler(delay=60, run=None, store=None):
    timers: list[FakeTimer] = []
    clock = Clock()
    runs: list[str] = []

    def _run(sid):
        runs.append(sid)
        return 'transcribed'

    def _factory(delay_s, fn, args=()):
        t = FakeTimer(delay_s, fn, args)
        timers.append(t)
        return t

    sched = AutosaveScheduler(store or FakeStore(), delay, run or _run,
                              clock=clock, timer_factory=_factory)
    return sched, timers, clock, runs


# ------------------------------------------------------------------ scheduler

def test_schedule_then_fire_runs_the_job_once():
    sched, timers, clock, runs = make_scheduler()
    info = sched.schedule(SID)
    assert info == {'at': 1060.0, 'status': 'pending', 'reason': None}
    assert timers[-1].delay == 60
    clock.t = 1060.0
    timers[-1].fire()
    assert runs == [SID]
    assert sched.info(SID)['status'] == 'done'


def test_cancel_before_deadline_keeps_the_job_from_running():
    sched, timers, clock, runs = make_scheduler()
    sched.schedule(SID)
    sched.cancel(SID, 'saved')
    clock.t = 2000.0
    timers[0].fn(*timers[0].args)     # even a timer that slipped through must not run it
    assert runs == []
    assert sched.info(SID)['status'] == 'cancelled'


def test_save_that_lands_before_finalize_schedules_wins():
    """/save waits for status=finalized, which finalize sets before it schedules."""
    sched, timers, _clock, runs = make_scheduler()
    sched.cancel(SID, 'saved')
    assert sched.schedule(SID)['status'] == 'cancelled'
    assert timers == []


def test_hold_moves_the_deadline_and_an_early_fire_rearms():
    sched, timers, clock, runs = make_scheduler()
    sched.schedule(SID)
    clock.t = 1030.0
    held = sched.hold(SID)
    assert held == {'at': 1630.0, 'status': 'held', 'reason': None}
    clock.t = 1060.0
    timers[0].fn(*timers[0].args)     # the first timer fires: the deadline moved, re-arm
    assert runs == []
    clock.t = 1630.0
    timers[-1].fire()
    assert runs == [SID]


def test_zero_delay_turns_the_server_autosave_off():
    sched, timers, _clock, _runs = make_scheduler(delay=0)
    assert sched.schedule(SID) == {'at': None, 'status': 'off'}
    assert timers == []


def test_restart_rearms_only_recordings_that_had_a_deadline():
    store = FakeStore({
        'rec_a': {'session_id': 'rec_a', 'autosave': {'status': 'pending', 'at': 900.0}},
        'rec_b': {'session_id': 'rec_b', 'autosave': {'status': 'cancelled', 'at': 900.0}},
        'rec_c': {'session_id': 'rec_c'},     # finalized before this feature existed
        'rec_d': {'session_id': 'rec_d', 'autosave': {'status': 'running', 'at': 900.0}},
    })
    sched, timers, _clock, _runs = make_scheduler(store=store)
    assert sorted(sched.reschedule_pending()) == ['rec_a', 'rec_d']
    assert all(t.delay == 60 for t in timers)


def test_crashing_job_is_marked_failed_not_raised():
    def boom(_sid):
        raise RuntimeError('whisper down')
    sched, timers, clock, _runs = make_scheduler(run=boom)
    sched.schedule(SID)
    clock.t = 1060.0
    timers[-1].fire()
    assert sched.info(SID)['status'] == 'failed'


# ------------------------------------------------------------------ the job

@pytest.fixture
def archive(tmp_path):
    db = str(tmp_path / 'archive.db')
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE audio_downloads (id INTEGER PRIMARY KEY, youtube_id TEXT,
            recording_session_id TEXT, file_path TEXT, title TEXT);
        CREATE TABLE transcriptions (id INTEGER PRIMARY KEY, file_path TEXT, title TEXT,
            category_id INTEGER, summary_json TEXT, deleted_at TEXT);
    """)
    conn.execute("INSERT INTO audio_downloads VALUES (415, ?, ?, 'E:/rec/final.mp3', 'Запис 2026-10-19 13:28')",
                 (f'recording_{SID}', SID))
    conn.commit()
    conn.close()
    return db


class FakeHttp:
    """Plays the app: transcribe inserts a transcript, PATCHes write the archive."""

    def __init__(self, db, confidence=0.8, titles=('Женя: оцифровка 1С і навчання Серьоги',)):
        self.db, self.confidence, self.titles = db, confidence, list(titles)
        self.calls: list[tuple[str, str, dict]] = []

    def _exec(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def __call__(self, method, url, *, data=None, form=None, timeout=60):
        path = url.split('5050', 1)[1]
        self.calls.append((method, path, data or form or {}))
        if path == '/api/transcribe':
            self._exec("INSERT INTO transcriptions (id, file_path, summary_json) "
                       "VALUES (8933, 'E:/rec/final.mp3', '{\"summary\": \"Розмова з Женею\"}')")
            return 200, {'transcription_id': 8933}
        if path.endswith('/suggest-meta'):
            return 200, {'titles': self.titles}
        if path == '/api/history/8933' and method == 'PATCH':
            self._exec('UPDATE transcriptions SET title = ? WHERE id = 8933', (data['title'],))
            return 200, {'success': True}
        if path.endswith('/suggest-category'):
            return 200, {'suggestion': {'category_id': 27, 'name': 'БАУФ - Женя AI',
                                        'confidence': self.confidence}}
        if path.endswith('/category'):
            self._exec('UPDATE transcriptions SET category_id = ? WHERE id = 8933', (data['category_id'],))
            return 200, {'success': True}
        raise AssertionError(f'unexpected call {method} {path}')


def make_job(db, http, store=None, recording=lambda: False):
    return AutosaveJob(base_url='http://127.0.0.1:5050', db_path=db, store=store or FakeStore(),
                       model='large-v3', language='auto', min_confidence=0.6,
                       is_recording=recording, http=http, sleep=lambda _s: None)


def _row(db):
    conn = sqlite3.connect(db)
    r = conn.execute('SELECT title, category_id FROM transcriptions WHERE id = 8933').fetchone()
    conn.close()
    return r


def test_job_transcribes_titles_and_files_a_confident_recording(archive):
    http = FakeHttp(archive, confidence=0.8)
    out = make_job(archive, http)(SID)
    assert out == 'transcribed_8933_titled_direction_set'
    assert _row(archive) == ('Женя: оцифровка 1С і навчання Серьоги', 27)
    form = http.calls[0][2]
    assert form == {'source_type': 'library', 'audio_download_id': '415',
                    'model': 'large-v3', 'language': 'auto'}


def test_job_leaves_direction_to_the_user_below_the_threshold(archive):
    http = FakeHttp(archive, confidence=0.55)
    out = make_job(archive, http)(SID)
    assert out.endswith('direction_ask_user')
    assert _row(archive)[1] is None
    assert not any(p.endswith('/category') for _m, p, _d in http.calls)


def test_job_never_overwrites_a_name_the_user_gave(archive):
    store = FakeStore()
    store.m[SID]['name'] = 'Дзвінок з Женею'
    http = FakeHttp(archive)
    out = make_job(archive, http, store=store)(SID)
    assert '_user_name_' in out
    assert not any(p.endswith('/suggest-meta') for _m, p, _d in http.calls)


def test_job_skips_a_recording_that_already_has_a_transcript(archive):
    conn = sqlite3.connect(archive)
    conn.execute("INSERT INTO transcriptions (id, file_path) VALUES (8000, 'E:/rec/final.mp3')")
    conn.commit()
    conn.close()
    http = FakeHttp(archive)
    assert make_job(archive, http)(SID) == 'skipped_already_transcribed'
    assert http.calls == []


def test_job_waits_for_the_recorder_before_calling_the_local_model(archive):
    """The local model shares a lock with the live copilot: no title while a call is recorded."""
    busy = iter([True, True, False] + [False] * 10)
    http = FakeHttp(archive)
    out = make_job(archive, http, recording=lambda: next(busy))(SID)
    assert '_titled_' in out


# ------------------------------------------------------------------ blueprint

@pytest.fixture
def bp(tmp_path, archive):
    app = Flask(__name__)
    app.register_blueprint(recording_bp)
    app.config.update(TESTING=True, DATABASE=archive)
    saved = (state.recording_service, state.sse_broker, state.live_transcribe_worker,
             state.recording_autosave)
    svc = MagicMock()
    svc.store = FakeStore()
    svc.get_state.return_value = {'session_id': SID, 'status': 'finalized', 'is_active': False}
    sched, _timers, _clock, _runs = make_scheduler(store=svc.store)
    state.recording_service, state.sse_broker, state.live_transcribe_worker = svc, MagicMock(), None
    state.recording_autosave = sched
    yield app.test_client(), svc, sched
    (state.recording_service, state.sse_broker, state.live_transcribe_worker,
     state.recording_autosave) = saved


def test_notification_stop_lands_on_that_recordings_save_screen(bp, monkeypatch):
    client, svc, _sched = bp
    watcher = MagicMock()
    watcher.consume.return_value = {'session_id': SID, 'exe': 'telegram.exe'}
    monkeypatch.setattr(recording_module, '_call_watcher', lambda: watcher)
    r = client.get('/api/recording/autoprompt/tok123/stop')
    assert r.status_code == 302
    assert r.headers['Location'].endswith(f'/record?stopped={SID}')
    svc.stop.assert_called_once_with(SID, name=None)


def test_discard_refuses_a_recording_that_already_has_a_transcript(bp, archive):
    client, svc, _sched = bp
    conn = sqlite3.connect(archive)
    conn.execute("INSERT INTO transcriptions (id, file_path) VALUES (8933, 'E:/rec/final.mp3')")
    conn.commit()
    conn.close()
    r = client.post(f'/api/recording/{SID}/discard')
    assert r.status_code == 409
    assert r.get_json()['error_code'] == 'RECORDING_KEPT'
    svc.discard.assert_not_called()


def test_discard_of_an_untranscribed_recording_cancels_its_autosave(bp):
    client, svc, sched = bp
    sched.schedule(SID)
    r = client.post(f'/api/recording/{SID}/discard')
    assert r.status_code == 200
    svc.discard.assert_called_once_with(SID)
    assert sched.info(SID)['status'] == 'cancelled'


def test_hold_and_state_expose_the_deadline(bp):
    client, _svc, sched = bp
    sched.schedule(SID)
    r = client.post(f'/api/recording/{SID}/autosave/hold')
    assert r.get_json()['autosave']['status'] == 'held'
    st = client.get(f'/api/recording/{SID}/state').get_json()['state']
    assert st['autosave']['status'] == 'held'
    assert st['duration_sec'] == 1510.0


# ------------------------------------------------------------------ repair round 1

def test_hold_before_finalize_survives_the_schedule():
    """The save screen opens right after POST /stop; the first keystroke can beat finalize."""
    sched, timers, clock, runs = make_scheduler()
    held = sched.hold(SID, {'name': 'Женя: оцифровка 1С'})
    assert held['status'] == 'held' and held['at'] == 1600.0
    assert timers == []                      # nothing armed before finalize
    info = sched.schedule(SID)               # finalize arrives
    assert info == {'at': 1600.0, 'status': 'held', 'reason': None}
    clock.t = 1060.0
    timers[-1].fn(*timers[-1].args)          # even a stray early fire does not run the job
    assert runs == []


def test_typed_fields_reach_the_server_autosave(archive):
    store = FakeStore()
    sched, _timers, _clock, _runs = make_scheduler(store=store)
    sched.schedule(SID)
    sched.hold(SID, {'name': 'Дзвінок з Женею', 'description': 'Про 1С', 'category_id': 27,
                     'junk': 'x'})
    http = FakeHttp(archive)
    out = make_job(archive, http, store=store)(SID)
    form = http.calls[0][2]
    assert form['title'] == 'Дзвінок з Женею'
    assert form['description'] == 'Про 1С'
    assert form['category_id'] == '27'
    assert 'junk' not in form
    assert '_user_name_' in out
    assert not any(p.endswith('/suggest-meta') for _m, p, _d in http.calls)


def test_hold_endpoint_stores_typed_fields(bp):
    client, svc, sched = bp
    sched.schedule(SID)
    r = client.post(f'/api/recording/{SID}/autosave/hold',
                    json={'name': '  Дзвінок з Женею ', 'category_id': '27'})
    assert r.status_code == 200
    fields = svc.store.m[SID]['autosave']['fields']
    assert fields == {'name': 'Дзвінок з Женею', 'category_id': 27}


def test_kept_error_code_is_registered():
    from app.core.error_codes import ERROR_CODES
    assert ERROR_CODES['RECORDING_KEPT'] == 409


# ------------------------------------------------------------------ repair round 2

def test_a_cleared_field_does_not_reach_the_server_autosave(bp):
    client, svc, sched = bp
    sched.schedule(SID)
    client.post(f'/api/recording/{SID}/autosave/hold', json={'name': 'Чернетка', 'description': 'Про 1С'})
    client.post(f'/api/recording/{SID}/autosave/hold', json={'name': '', 'description': 'Про 1С',
                                                             'category_id': None})
    assert svc.store.m[SID]['autosave']['fields'] == {'description': 'Про 1С'}


def test_only_a_successful_save_marks_the_recording_saved(bp, monkeypatch, tmp_path):
    client, svc, _sched = bp
    final = tmp_path / 'final.mp3'
    final.write_bytes(b'ID3')
    svc.store.m[SID]['final_mp3_path'] = str(final)
    svc.store.set_name = lambda sid, name: None
    from app.services.recording import library
    calls = {'n': 0}

    def flaky_register(_db, sid, manifest, name=None):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('database is locked')
        return {'download_id': 415, 'file_path': str(final), 'name': name,
                'duration_sec': 1510, 'file_size': 3, 'created': False}
    monkeypatch.setattr(library, 'register_recording', flaky_register)

    assert client.post(f'/api/recording/{SID}/save', json={'name': 'Женя'}).status_code == 500
    second = client.post(f'/api/recording/{SID}/save', json={'name': 'Женя'}).get_json()
    assert second['already_saved'] is False          # the retry after a failure must transcribe
    third = client.post(f'/api/recording/{SID}/save', json={'name': 'Женя'}).get_json()
    assert third['already_saved'] is True            # a second save screen must not


# ------------------------------------------------------------------ direction after a tab save

def _insert_transcript(db, category_id=None):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (id, file_path, category_id) VALUES (8933, 'E:/rec/final.mp3', ?)",
                 (category_id,))
    conn.commit()
    conn.close()


def make_saved_job(db, http, on_sleep=lambda _n: None):
    clock = Clock()
    naps = {'n': 0}

    def _sleep(sec):
        naps['n'] += 1
        clock.t += sec
        on_sleep(naps['n'])
    return AutosaveJob(base_url='http://127.0.0.1:5050', db_path=db, store=FakeStore(),
                       model='large-v3', language='auto', min_confidence=0.6,
                       is_recording=lambda: False, http=http, sleep=_sleep, clock=clock)


def test_tab_save_files_the_direction_once_the_tabs_transcript_lands(archive):
    """Live call 19.10.2026 (#8980): the tab saved and transcribed, the k-NN was sure (1.0),
    and the direction stayed empty because only the cancelled server job filed it."""
    http = FakeHttp(archive, confidence=1.0)
    job = make_saved_job(archive, http, on_sleep=lambda n: n == 2 and _insert_transcript(archive))
    assert job.file_saved(SID) == 'saved_8933_direction_set'
    assert _row(archive)[1] == 27
    assert not any(p == '/api/transcribe' for _m, p, _d in http.calls)


def test_tab_save_keeps_a_direction_chosen_in_the_tab(archive):
    _insert_transcript(archive, category_id=5)
    http = FakeHttp(archive, confidence=1.0)
    assert make_saved_job(archive, http).file_saved(SID) == 'saved_8933_direction_given'
    assert _row(archive)[1] == 5 and http.calls == []


def test_tab_save_without_transcription_starts_none(archive):
    http = FakeHttp(archive)
    assert make_saved_job(archive, http).file_saved(SID) == 'no_transcript'
    assert http.calls == []


def test_after_save_runs_the_follow_up_and_records_its_outcome():
    store = FakeStore()
    timers: list[FakeTimer] = []
    sched = AutosaveScheduler(store, 60, lambda _sid: 'x',
                              timer_factory=lambda d, fn, args=(): timers.append(FakeTimer(d, fn, args)) or timers[-1],
                              after_save_fn=lambda sid: 'saved_8933_direction_set')
    assert sched.after_save(SID) is True
    timers[-1].fire()
    assert store.m[SID]['autosave']['direction'] == 'saved_8933_direction_set'
    off = AutosaveScheduler(FakeStore(), 0, lambda _sid: 'x', after_save_fn=lambda sid: 'y')
    assert off.after_save(SID) is False


def test_only_the_first_of_two_saves_starts_the_direction_follow_up(bp, monkeypatch, tmp_path):
    client, svc, sched = bp
    final = tmp_path / 'final.mp3'
    final.write_bytes(b'ID3')
    svc.store.m[SID]['final_mp3_path'] = str(final)
    svc.store.set_name = lambda sid, name: None
    from app.services.recording import library
    monkeypatch.setattr(library, 'register_recording', lambda _db, sid, manifest, name=None: {
        'download_id': 415, 'file_path': str(final), 'name': name, 'duration_sec': 1510,
        'file_size': 3, 'created': False})
    started: list[str] = []
    monkeypatch.setattr(sched, 'after_save', started.append)
    first = client.post(f'/api/recording/{SID}/save', json={'name': 'Женя'}).get_json()
    second = client.post(f'/api/recording/{SID}/save', json={'name': 'Женя'}).get_json()
    assert (first['already_saved'], second['already_saved']) == (False, True)
    assert started == [SID]


def test_loopback_calls_carry_the_ui_token(monkeypatch):
    """Autosave is Recall's own server acting for the owner: its PATCH must not look like an agent."""
    from app.core import auth
    from app.services.recording import autosave
    seen: dict = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{}'

    def fake_urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return _Resp()

    monkeypatch.setattr(autosave.urllib.request, 'urlopen', fake_urlopen)
    assert autosave.http_json('PATCH', 'http://127.0.0.1:5050/api/history/1', data={'title': 'Т'}) == (200, {})
    assert seen[auth.UI_TOKEN_HEADER.lower()] == auth.ui_token()
