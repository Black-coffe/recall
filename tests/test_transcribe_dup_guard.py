"""Test for the anti-dup guard of the library path of /api/transcribe (Fix 1, 03.07.2026).

A repeated POST with the same audio_download_id while an active transcription is already
running (the marker in state.active_library_transcriptions) used to start a SECOND
full whisper job on the same file (GPU time ×2). The guard must
return 409 BEFORE any call to whisper_manager.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from flask import Flask

from app import state
from app.blueprints.transcription import transcription_bp
from app.db.migrations import init_database


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


class _WhisperManagerMustNotBeCalled:
    """The guard must fire BEFORE any access to whisper - if
    transcribe_with_progress() is called after all, the test fails loudly
    instead of a silent duplicate."""

    def transcribe_with_progress(self, *args, **kwargs):
        raise AssertionError(
            "whisper_manager.transcribe_with_progress() was called - "
            "the 409 guard should have stopped the request EARLIER"
        )


@pytest.fixture
def client_with_library_audio(tmp_path: Path, monkeypatch):
    db_path = str(tmp_path / 'dup_guard_test.db')
    init_database(db_path)

    audio_file = tmp_path / 'lib_audio.mp3'
    audio_file.write_bytes(b'fake mp3 bytes')
    aid = _insert_audio_download(db_path, file_path=str(audio_file), source_type='youtube')

    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(transcription_bp)

    monkeypatch.setattr(state, 'whisper_manager', _WhisperManagerMustNotBeCalled())
    monkeypatch.setattr(state, 'active_library_transcriptions', {})

    return app.test_client(), aid


class TestLibraryAntiDupGuard:
    def test_second_post_while_active_returns_409(self, client_with_library_audio):
        client, aid = client_with_library_audio
        existing_marker = {
            'audio_download_id': aid,
            'started_at': time.time(),
            'stage': 'transcribing',
            'progress': 0.5,
        }
        state.active_library_transcriptions[aid] = existing_marker

        r = client.post('/api/transcribe', data={
            'source_type': 'library',
            'audio_download_id': str(aid),
        })

        assert r.status_code == 409
        body = r.get_json()
        assert body['success'] is False
        assert body['error_code'] == 'ALREADY_RUNNING'
        assert body['active'] == existing_marker

        # The marker stays - it is the record of the FIRST (still active) job,
        # the guard must not have touched it.
        assert state.active_library_transcriptions[aid] == existing_marker

    def test_marker_not_overwritten_or_second_job_started(self, client_with_library_audio):
        """The second submit must neither overwrite the marker with a new
        started_at/stage nor (transitively, via _WhisperManagerMustNotBeCalled)
        start a second whisper job."""
        client, aid = client_with_library_audio
        original_marker = {
            'audio_download_id': aid,
            'started_at': 12345.0,
            'stage': 'diarizing',
            'progress': 0.9,
        }
        state.active_library_transcriptions[aid] = dict(original_marker)

        r = client.post('/api/transcribe', data={
            'source_type': 'library',
            'audio_download_id': str(aid),
        })

        assert r.status_code == 409
        assert state.active_library_transcriptions[aid] == original_marker

    def test_no_active_marker_falls_through_to_normal_flow(self, client_with_library_audio):
        """Without an existing marker the guard does not interfere - the request goes on
        (and fails at whisper_manager, because we deliberately planted a mock that
        fails with an AssertionError - confirms that we reached the normal
        flow, and not the 409)."""
        client, aid = client_with_library_audio
        assert aid not in state.active_library_transcriptions

        r = client.post('/api/transcribe', data={
            'source_type': 'library',
            'audio_download_id': str(aid),
        })

        # We reached whisper_manager (the exception is caught, the endpoint returns 500,
        # NOT 409) - proves that without an active marker the guard does not block.
        assert r.status_code == 500
        assert r.get_json()['success'] is False
        assert r.get_json().get('error_code') != 'ALREADY_RUNNING'
        # The marker is removed in finally after a failed request.
        assert aid not in state.active_library_transcriptions
