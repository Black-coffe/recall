"""Тест для anti-dup guard'а library-шляху /api/transcribe (Фикс 1, 03.07.2026).

Повторний POST з тим самим audio_download_id, поки активна транскрипція вже
йде (маркер у state.active_library_transcriptions), раніше запускав ДРУГУ
повну whisper-задачу на тому самому файлі (GPU-час ×2). Guard мусить
повернути 409 ДО будь-якого виклику whisper_manager.
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
    """Guard мусить спрацювати ДО будь-якого звернення до whisper — якщо
    transcribe_with_progress() усе ж викликаний, тест провалиться явно
    замість тихого дубля."""

    def transcribe_with_progress(self, *args, **kwargs):
        raise AssertionError(
            "whisper_manager.transcribe_with_progress() викликаний — "
            "409-guard мав зупинити запит РАНІШЕ"
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

        # Маркер лишається — це запис ПЕРШОЇ (все ще активної) задачі,
        # guard не мав його чіпати.
        assert state.active_library_transcriptions[aid] == existing_marker

    def test_marker_not_overwritten_or_second_job_started(self, client_with_library_audio):
        """Другий сабміт не повинен ані перезаписати маркер новим
        started_at/stage, ані (транзитивно, через _WhisperManagerMustNotBeCalled)
        запустити другу whisper-задачу."""
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
        """Без існуючого маркера guard не втручається — запит іде далі
        (і впаде вже на whisper_manager, бо ми навмисно підклали mock, що
        падає з AssertionError — підтверджує, що дійшли до нормального
        флоу, а не 409)."""
        client, aid = client_with_library_audio
        assert aid not in state.active_library_transcriptions

        r = client.post('/api/transcribe', data={
            'source_type': 'library',
            'audio_download_id': str(aid),
        })

        # Дійшли до whisper_manager (виняток впіймано, endpoint віддає 500,
        # НЕ 409) — доводить, що без активного маркера гард не блокує.
        assert r.status_code == 500
        assert r.get_json()['success'] is False
        assert r.get_json().get('error_code') != 'ALREADY_RUNNING'
        # Маркер після невдалого запиту прибирається у finally.
        assert aid not in state.active_library_transcriptions
