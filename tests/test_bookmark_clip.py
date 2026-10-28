"""Bookmark clip: ffmpeg cut without a console window, zip with the text, refusals as the audio route."""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest
from flask import Flask

from app.blueprints.bookmarks import bookmarks_bp
from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database
from app.services import bookmark_clip, privacy
from app.utils import proc

FAKE_MP3 = b'ID3fake-mp3-bytes'
SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Перший сегмент"},
    {"start": 20.0, "end": 26.0, "text": "Домовились про бюджет", "speaker": "SPEAKER_01"},
    {"start": 95.0, "end": 99.0, "text": "Кінець"},
]


class FakeRun:
    """Records every subprocess.run call; ffprobe answers the duration, ffmpeg the bytes."""

    def __init__(self, duration=100.0, returncode=0):
        self.calls: list = []
        self.duration = duration
        self.returncode = returncode

    def __call__(self, *a, **kw):
        cmd = a[0] if a else kw.get('args')
        self.calls.append((cmd, kw))
        if 'ffprobe' in str(cmd[0]):
            out = f'{self.duration}\n'.encode()
        else:
            out = FAKE_MP3
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=out, stderr=b'boom')


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(bookmark_clip.subprocess, 'run', fake)
    monkeypatch.setattr(bookmark_clip.shutil, 'which', lambda name: f'C:/bin/{name}.exe')
    return fake


def _ffmpeg_window(fake):
    cmd, kw = [c for c in fake.calls if 'ffmpeg' in str(c[0][0])][-1]
    return float(cmd[cmd.index('-ss') + 1]), float(cmd[cmd.index('-to') + 1]), kw


def test_cut_clamps_and_hides_console(fake_run):
    assert bookmark_clip.cut('a.mp3', 2.0, 4.0, pad=5) == FAKE_MP3
    lo, hi, kw = _ffmpeg_window(fake_run)
    assert (lo, hi) == (0.0, 9.0)
    assert kw['creationflags'] == proc.NO_WINDOW
    assert all(c[1]['creationflags'] == proc.NO_WINDOW for c in fake_run.calls)

    bookmark_clip.cut('a.mp3', 95.0, 99.0, pad=5)
    lo, hi, _ = _ffmpeg_window(fake_run)
    assert (lo, hi) == (90.0, 100.0)


def test_cut_nonzero_exit_raises(fake_run):
    fake_run.returncode = 1
    with pytest.raises(bookmark_clip.ClipUnavailable):
        bookmark_clip.cut('a.mp3', 1.0, 2.0, pad=0)


def test_cut_without_ffmpeg_raises(monkeypatch):
    monkeypatch.setattr(bookmark_clip.shutil, 'which', lambda name: None)
    with pytest.raises(bookmark_clip.ClipUnavailable):
        bookmark_clip.cut('a.mp3', 1.0, 2.0, pad=0)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    db_path = str(tmp_path / 'clip.db')
    init_database(db_path)
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    audio = uploads / 'call.mp3'
    audio.write_bytes(b'not really audio')
    outside = tmp_path / 'elsewhere.mp3'
    outside.write_bytes(b'x')

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO categories (id, name, name_norm, parent_id) "
                 "VALUES (901, 'ClipPrivate', 'clipprivate', NULL)")
    ids = {}
    for key, path, cat in (('ok', audio, None), ('gone', uploads / 'missing.mp3', None),
                           ('outside', outside, None), ('private', audio, 901)):
        cur = conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, segments, "
            "language, file_path, category_id) VALUES ('file', 'call', 't', ?, 'uk', ?, ?)",
            (json.dumps(SEGMENTS, ensure_ascii=False), str(path), cat))
        tid = cur.lastrowid
        bid = conn.execute(
            "INSERT INTO segment_bookmarks (transcription_id, segment_index, segment_start, note) "
            "VALUES (?, 1, 20.0, 'важливо')", (tid,)).lastrowid
        ids[key] = (tid, bid)
    conn.commit()
    conn.close()
    monkeypatch.setenv('RECALL_PRIVATE_CATEGORIES', 'ClipPrivate')

    app = Flask(__name__)
    app.config.update(DATABASE=db_path, UPLOAD_FOLDER=str(uploads), BASE_DIR=str(tmp_path))
    app.register_blueprint(bookmarks_bp)
    app.register_blueprint(transcription_bp)
    return app.test_client(), ids, db_path


def test_clip_zip(env, fake_run):
    c, ids, _ = env
    tid, bid = ids['ok']
    r = c.get(f'/api/bookmarks/{bid}/clip')
    assert r.status_code == 200
    assert r.mimetype == 'application/zip'
    cd = r.headers['Content-Disposition']
    assert 'attachment' in cd and f'clip-{tid}-1.zip' in cd
    zf = zipfile.ZipFile(io.BytesIO(r.data))
    assert sorted(zf.namelist()) == ['clip.mp3', 'clip.txt']
    assert zf.read('clip.mp3') == FAKE_MP3
    txt = zf.read('clip.txt').decode('utf-8')
    assert 'Домовились про бюджет' in txt
    assert 'SPEAKER_01' in txt
    assert '00:00:20' in txt and '00:00:26' in txt
    assert 'важливо' in txt
    lo, hi, kw = _ffmpeg_window(fake_run)
    assert (lo, hi) == (15.0, 31.0)
    assert kw['creationflags'] == proc.NO_WINDOW


def test_clip_pad_param(env, fake_run):
    c, ids, _ = env
    _, bid = ids['ok']
    assert c.get(f'/api/bookmarks/{bid}/clip?pad=0').status_code == 200
    assert _ffmpeg_window(fake_run)[:2] == (20.0, 26.0)
    for bad in ('70', '-1', 'abc', 'nan'):
        r = c.get(f'/api/bookmarks/{bid}/clip?pad={bad}')
        assert r.status_code == 400, bad
        assert r.get_json()['error_code'] == 'INVALID_ARGUMENT'


def test_clip_unknown_bookmark(env, fake_run):
    c, _, _ = env
    r = c.get('/api/bookmarks/99999/clip')
    assert r.status_code == 404
    assert r.get_json()['error_code'] == 'NOT_FOUND'


def test_clip_file_gone(env, fake_run):
    c, ids, _ = env
    tid, bid = ids['gone']
    r = c.get(f'/api/bookmarks/{bid}/clip')
    assert r.status_code == 404
    assert r.get_json()['error_code'] == 'NO_MEDIA'
    assert c.get(f'/api/transcription/{tid}/audio').status_code == 404


def test_clip_ffmpeg_failure_is_503(env, fake_run):
    c, ids, _ = env
    fake_run.returncode = 1
    r = c.get(f'/api/bookmarks/{ids["ok"][1]}/clip')
    assert r.status_code == 503
    assert r.get_json()['error_code'] == 'UNAVAILABLE'


@pytest.mark.parametrize('key', ['outside', 'private'])
def test_clip_refused_as_audio_route(env, fake_run, key):
    c, ids, db_path = env
    tid, bid = ids[key]
    if key == 'private':
        assert privacy.private_tids(db_path, [tid]) == {tid}
    audio = c.get(f'/api/transcription/{tid}/audio')
    clip = c.get(f'/api/bookmarks/{bid}/clip')
    assert clip.status_code == audio.status_code
    if audio.status_code >= 400:
        assert (clip.get_json() or {}).get('error_code') == (audio.get_json() or {}).get('error_code')
