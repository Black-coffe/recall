"""T7.7: tests for the shared helpers extracted out of the five near-identical
Claude endpoints (polish/summarize/sentiment/translate/topics) in
app/blueprints/transcription.py — _load_speaker_context (row → segments/
has_speakers/speaker_map) and _call_claude_endpoint (RuntimeError → 400,
anything else → logged + generic 500).

Also covers one representative endpoint end-to-end (polish) through the real
Flask blueprint with text_polishing monkeypatched, to lock in that the
dedup didn't change wiring (cache hit, diarized vs plain dispatch, error
mapping, response shape).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from app import state
from app.blueprints.transcription import _call_claude_endpoint, _load_speaker_context
from app.db.migrations import init_database


# ============================================================ _call_claude_endpoint

class TestCallClaudeEndpoint:
    def test_success_returns_result_and_no_error(self):
        result, err = _call_claude_endpoint("polish", lambda x: x.upper(), "hi")
        assert result == "HI"
        assert err is None

    def test_runtime_error_maps_to_400_with_raw_message(self, app_ctx):
        def _boom():
            raise RuntimeError("ANTHROPIC_API_KEY не встановлено.")
        result, err = _call_claude_endpoint("polish", _boom)
        assert result is None
        response, status = err
        assert status == 400
        assert response.get_json()["error"] == "ANTHROPIC_API_KEY не встановлено."

    def test_generic_exception_maps_to_500_with_default_error_msg(self, app_ctx):
        def _boom():
            raise ValueError("boom")
        result, err = _call_claude_endpoint("polish", _boom)
        assert result is None
        response, status = err
        assert status == 500
        assert response.get_json()["error"] == "Помилка Claude API. Перевірте логи."

    def test_generic_exception_uses_custom_error_msg(self, app_ctx):
        def _boom():
            raise ValueError("boom")
        result, err = _call_claude_endpoint("topics", _boom, error_msg="Помилка Claude API.")
        response, status = err
        assert status == 500
        assert response.get_json()["error"] == "Помилка Claude API."

    def test_forwards_args_and_kwargs(self):
        def fn(a, b, *, c):
            return a + b + c
        result, err = _call_claude_endpoint("x", fn, 1, 2, c=3)
        assert err is None
        assert result == 6


@pytest.fixture
def app_ctx():
    app = Flask(__name__)
    with app.app_context():
        yield app


# ============================================================ _load_speaker_context

@pytest.fixture
def conn_with_speaker_map(tmp_path: Path):
    db_path = str(tmp_path / 'speaker_ctx.db')
    init_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, segments, language) "
        "VALUES ('file', 't', 'hi', '[]', 'uk')"
    )
    tid = cur.lastrowid
    speaker_id = conn.execute(
        "INSERT INTO speakers (name, color) VALUES ('Андрій', '#fff')"
    ).lastrowid
    conn.execute(
        "INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) VALUES (?, ?, ?)",
        (tid, 'SPEAKER_00', speaker_id),
    )
    # Unnamed label — present in the map table but no linked speaker row.
    conn.execute(
        "INSERT INTO transcription_speaker_map (transcription_id, raw_label, speaker_id) VALUES (?, ?, NULL)",
        (tid, 'SPEAKER_01'),
    )
    conn.commit()
    try:
        yield conn, tid
    finally:
        conn.close()


class TestLoadSpeakerContext:
    def test_empty_segments_json_returns_no_speakers(self, conn_with_speaker_map):
        conn, tid = conn_with_speaker_map
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, None)
        assert segments == []
        assert has_speakers is False
        assert speaker_map == {}

    def test_non_diarized_segments_returns_no_speakers(self, conn_with_speaker_map):
        conn, tid = conn_with_speaker_map
        segs_json = json.dumps([{'start': 0, 'end': 1, 'text': 'hi'}])
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, segs_json)
        assert has_speakers is False
        assert speaker_map == {}

    def test_diarized_segments_loads_named_speaker_only(self, conn_with_speaker_map):
        conn, tid = conn_with_speaker_map
        segs_json = json.dumps([
            {'start': 0, 'end': 1, 'text': 'hi', 'speaker': 'SPEAKER_00'},
            {'start': 1, 'end': 2, 'text': 'yo', 'speaker': 'SPEAKER_01'},
        ])
        segments, has_speakers, speaker_map = _load_speaker_context(conn, tid, segs_json)
        assert has_speakers is True
        assert len(segments) == 2
        # SPEAKER_01 has no linked speaker (name NULL) — must NOT appear in the map
        # (endpoints fall back to "Спікер N" for it), matching the original inline code.
        assert speaker_map == {'SPEAKER_00': 'Андрій'}


# ============================================================ polish endpoint (integration)

class _FakeMetrics:
    def inc(self, *a, **k):
        pass

    def observe_duration(self, *a, **k):
        pass


@pytest.fixture
def polish_client(tmp_path, monkeypatch):
    from app.blueprints.transcription import transcription_bp

    db_path = str(tmp_path / 'polish_test.db')
    init_database(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, segments, language) "
        "VALUES ('file', 't', 'raw text here', '[]', 'uk')"
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()

    app = Flask(__name__)
    app.config['DATABASE'] = db_path
    app.register_blueprint(transcription_bp)
    monkeypatch.setattr(state, 'metrics', _FakeMetrics())
    return app.test_client(), db_path, tid


class TestPolishEndpointDedupWiring:
    def test_api_key_missing_returns_400(self, polish_client, monkeypatch):
        from app.services import text_polishing
        monkeypatch.setattr(text_polishing, 'is_available', lambda: False)
        client, _, tid = polish_client
        r = client.post(f'/api/transcription/{tid}/polish', json={})
        assert r.status_code == 400
        assert 'ANTHROPIC_API_KEY' in r.get_json()['error']

    def test_plain_transcript_calls_polish_transcript_not_diarized(self, polish_client, monkeypatch):
        from app.services import text_polishing
        monkeypatch.setattr(text_polishing, 'is_available', lambda: True)
        calls = {}
        def _fake_polish(text, model=None):
            calls['polish_transcript'] = text
            return {"polished_text": "Polished.", "model": "claude-x",
                    "input_tokens": 1, "output_tokens": 2,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0}
        monkeypatch.setattr(text_polishing, 'polish_transcript', _fake_polish)

        client, db_path, tid = polish_client
        r = client.post(f'/api/transcription/{tid}/polish', json={})
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        assert data['polished_text'] == 'Polished.'
        assert data['cached'] is False
        assert calls['polish_transcript'] == 'raw text here'

        # Second call hits the cache — text_polishing must NOT be invoked again.
        monkeypatch.setattr(text_polishing, 'polish_transcript',
                             lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached")))
        r2 = client.post(f'/api/transcription/{tid}/polish', json={})
        assert r2.status_code == 200
        assert r2.get_json()['cached'] is True

    def test_runtime_error_from_claude_call_maps_to_400(self, polish_client, monkeypatch):
        from app.services import text_polishing
        monkeypatch.setattr(text_polishing, 'is_available', lambda: True)
        def _boom(text, model=None):
            raise RuntimeError("ANTHROPIC_API_KEY не встановлено. Створіть .env у корені проекту з ключем.")
        monkeypatch.setattr(text_polishing, 'polish_transcript', _boom)

        client, _, tid = polish_client
        r = client.post(f'/api/transcription/{tid}/polish', json={'force': True})
        assert r.status_code == 400
        assert r.get_json()['error'].startswith('ANTHROPIC_API_KEY')

    def test_unexpected_error_from_claude_call_maps_to_500(self, polish_client, monkeypatch):
        from app.services import text_polishing
        monkeypatch.setattr(text_polishing, 'is_available', lambda: True)
        def _boom(text, model=None):
            raise ValueError("network blew up")
        monkeypatch.setattr(text_polishing, 'polish_transcript', _boom)

        client, _, tid = polish_client
        r = client.post(f'/api/transcription/{tid}/polish', json={'force': True})
        assert r.status_code == 500
        assert r.get_json()['error'] == 'Помилка Claude API. Перевірте логи.'
