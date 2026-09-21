"""T7.1: unit tests for the resolve_*_source() functions extracted out of the
former /api/transcribe god-function (app/blueprints/transcription.py).

Each resolve_*_source() reads the current Flask request and either returns
(filepath, source_name, meta) or raises _SourceResolutionError(payload,
status) — exactly the (jsonify, status) the old inline branches used to
`return`. Tests call the resolvers directly inside app.test_request_context(),
no HTTP round-trip needed, per REMEDIATION_PLAN T7.1 acceptance criteria.
"""
from __future__ import annotations

import io
import os
import sqlite3
import time
from pathlib import Path

import pytest
from flask import Flask

from app import state
from app.blueprints.transcription import (
    _SourceResolutionError,
    resolve_file_source,
    resolve_library_source,
    resolve_recording_source,
    resolve_youtube_source,
)
from app.db.migrations import init_database


@pytest.fixture
def app(tmp_path: Path):
    upload_dir = tmp_path / 'uploads'
    upload_dir.mkdir()
    db_path = str(tmp_path / 'resolve_test.db')
    init_database(db_path)
    flask_app = Flask(__name__)
    flask_app.config['UPLOAD_FOLDER'] = str(upload_dir)
    flask_app.config['DATABASE'] = db_path
    return flask_app


def _insert_audio_download(db_path: str, **overrides) -> int:
    fields = {
        'youtube_url': 'https://youtu.be/abc',
        'youtube_id': 'abc123',
        'title': 'Test video',
        'author': 'Author',
        'duration': 120,
        'thumbnail_url': 'http://x/thumb.jpg',
        'file_path': None,
        'source_type': 'youtube',
    }
    fields.update(overrides)
    cols = ', '.join(fields.keys())
    placeholders = ', '.join('?' * len(fields))
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f'INSERT INTO audio_downloads ({cols}) VALUES ({placeholders})',
            list(fields.values()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ============================================================ resolve_file_source

class TestResolveFileSource:
    def test_happy_path_saves_file_and_returns_meta(self, app):
        with app.test_request_context(
            '/api/transcribe', method='POST',
            data={'audio': (io.BytesIO(b'fake mp3 bytes'), 'meeting.mp3')},
            content_type='multipart/form-data',
        ):
            filepath, source_name, meta = resolve_file_source()
            assert os.path.isfile(filepath)
            assert filepath.startswith(app.config['UPLOAD_FOLDER'])
            assert source_name == 'meeting.mp3'
            assert meta == {
                'youtube_info': {}, 'download_id': None,
                'library_audio_id': None, 'library_recording_sid': None,
                'source_type': 'file',
                # editable-title-description-02: лише 'library' заповнює це.
                'title': None, 'description': None,
            }

    def test_missing_audio_field_raises_400(self, app):
        with app.test_request_context('/api/transcribe', method='POST', data={}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_file_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'Файл не знайдено'

    def test_empty_filename_raises_400(self, app):
        with app.test_request_context(
            '/api/transcribe', method='POST',
            data={'audio': (io.BytesIO(b''), '')},
            content_type='multipart/form-data',
        ):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_file_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'Файл не вибрано'

    def test_disallowed_extension_raises_400(self, app):
        with app.test_request_context(
            '/api/transcribe', method='POST',
            data={'audio': (io.BytesIO(b'x'), 'malware.exe')},
            content_type='multipart/form-data',
        ):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_file_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'Непідтримуваний формат файлу'


# ============================================================ resolve_youtube_source

class TestResolveYoutubeSource:
    def test_happy_path_returns_meta_and_logs(self, app, monkeypatch, tmp_path):
        audio_file = tmp_path / 'yt.mp3'
        audio_file.write_bytes(b'x')
        monkeypatch.setattr(state, 'download_progress', {
            'dl1': {'status': 'completed', 'file_path': str(audio_file),
                    'info': {'title': 'A YouTube video'}},
        })
        logged = []
        monkeypatch.setattr(state, 'add_log', lambda *a: logged.append(a))

        with app.test_request_context('/api/transcribe', method='POST', data={'download_id': 'dl1'}):
            filepath, source_name, meta = resolve_youtube_source()
            assert filepath == str(audio_file)
            assert source_name == 'A YouTube video'
            assert meta['source_type'] == 'youtube'
            assert meta['download_id'] == 'dl1'
            assert meta['youtube_info'] == {'title': 'A YouTube video'}
        assert logged and logged[0][0] == 'dl1'

    def test_missing_download_id_raises_400(self, app, monkeypatch):
        monkeypatch.setattr(state, 'download_progress', {})
        with app.test_request_context('/api/transcribe', method='POST', data={}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_youtube_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'Невірний ID завантаження'

    def test_not_completed_raises_400(self, app, monkeypatch):
        monkeypatch.setattr(state, 'download_progress', {'dl1': {'status': 'downloading'}})
        with app.test_request_context('/api/transcribe', method='POST', data={'download_id': 'dl1'}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_youtube_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'Завантаження ще не завершено'


# ============================================================ resolve_recording_source

class _FakeStore:
    def __init__(self, manifest):
        self._manifest = manifest

    def read(self, sid):
        if self._manifest is None:
            raise KeyError(sid)
        return self._manifest


class _FakeRecordingService:
    def __init__(self, manifest):
        self.store = _FakeStore(manifest)


class TestResolveRecordingSource:
    def test_missing_sid_raises_400(self, app, monkeypatch):
        with app.test_request_context('/api/transcribe', method='POST', data={}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_recording_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == "recording_session_id обов'язковий"

    def test_recording_service_unavailable_raises_503(self, app, monkeypatch):
        monkeypatch.setattr(state, 'recording_service', None)
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'recording_session_id': 'rec_1'}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_recording_source()
            assert exc.value.status == 503

    def test_session_not_found_raises_404(self, app, monkeypatch):
        monkeypatch.setattr(state, 'recording_service', _FakeRecordingService(None))
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'recording_session_id': 'rec_missing'}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_recording_source()
            assert exc.value.status == 404
            assert exc.value.payload['error'] == 'Recording сесія не знайдена'

    def test_not_finalized_raises_400(self, app, monkeypatch):
        monkeypatch.setattr(state, 'recording_service',
                             _FakeRecordingService({'status': 'recording'}))
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'recording_session_id': 'rec_1'}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_recording_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'Recording ще не finalized'

    def test_missing_final_mp3_raises_400(self, app, monkeypatch):
        monkeypatch.setattr(state, 'recording_service', _FakeRecordingService({
            'status': 'finalized', 'final_mp3_path': None,
        }))
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'recording_session_id': 'rec_1'}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_recording_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'Final MP3 не створений'

    def test_happy_path_returns_meta(self, app, monkeypatch, tmp_path):
        mp3 = tmp_path / 'final.mp3'
        mp3.write_bytes(b'x')
        monkeypatch.setattr(state, 'recording_service', _FakeRecordingService({
            'status': 'finalized', 'final_mp3_path': str(mp3), 'name': 'Standup',
        }))
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'recording_session_id': 'rec_1'}):
            filepath, source_name, meta = resolve_recording_source()
            assert filepath == str(mp3)
            assert source_name == 'Standup'
            assert meta['source_type'] == 'recording'
            assert meta['library_audio_id'] is None


# ============================================================ resolve_library_source

class TestResolveLibrarySource:
    def test_missing_audio_download_id_raises_400(self, app):
        with app.test_request_context('/api/transcribe', method='POST', data={}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_library_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == "audio_download_id обов'язковий"

    def test_non_numeric_audio_download_id_raises_400(self, app):
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'audio_download_id': 'abc'}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_library_source()
            assert exc.value.status == 400
            assert exc.value.payload['error'] == 'audio_download_id мусить бути числом'

    def test_row_not_found_raises_404(self, app):
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'audio_download_id': '99999'}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_library_source()
            assert exc.value.status == 404

    def test_missing_file_on_disk_raises_400(self, app):
        aid = _insert_audio_download(app.config['DATABASE'], file_path='/no/such/file.mp3')
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'audio_download_id': str(aid)}):
            with pytest.raises(_SourceResolutionError) as exc:
                resolve_library_source()
            assert exc.value.status == 400
            assert 'Файл відсутній' in exc.value.payload['error']

    def test_youtube_row_inherits_source_type_and_youtube_info(self, app, tmp_path):
        audio = tmp_path / 'lib_yt.mp3'
        audio.write_bytes(b'x')
        aid = _insert_audio_download(
            app.config['DATABASE'], file_path=str(audio), source_type='youtube',
            title='Library YT video',
        )
        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'audio_download_id': str(aid)}):
            filepath, source_name, meta = resolve_library_source()
            assert filepath == str(audio)
            assert source_name == 'Library YT video'
            assert meta['source_type'] == 'youtube'
            assert meta['library_audio_id'] == aid
            assert meta['youtube_info']['video_id'] == 'abc123'
            assert meta['library_recording_sid'] is None

    def test_recording_row_inherits_source_type_and_sid(self, app, tmp_path):
        audio = tmp_path / 'lib_rec.mp3'
        audio.write_bytes(b'x')
        aid = _insert_audio_download(
            app.config['DATABASE'], file_path=str(audio), source_type='recording',
            title='Library recording',
        )
        conn = sqlite3.connect(app.config['DATABASE'])
        try:
            conn.execute(
                "UPDATE audio_downloads SET recording_session_id = ? WHERE id = ?",
                ('rec_xyz', aid),
            )
            conn.commit()
        finally:
            conn.close()

        with app.test_request_context('/api/transcribe', method='POST',
                                       data={'audio_download_id': str(aid)}):
            filepath, source_name, meta = resolve_library_source()
            assert meta['source_type'] == 'recording'
            assert meta['library_recording_sid'] == 'rec_xyz'
            assert meta['youtube_info'] == {}
