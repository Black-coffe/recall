"""recording_active() and RECORDING_BLOCKED live in one module."""
from types import SimpleNamespace

from app import state
from app.services import reembed
from app.services.recording import state as rec_state


def _with_service(monkeypatch, service):
    monkeypatch.setattr(state, "recording_service", service, raising=False)


def test_no_service_means_no_recording(monkeypatch):
    _with_service(monkeypatch, None)
    assert rec_state.recording_active() is False


def test_live_sessions_count_including_two(monkeypatch):
    svc = SimpleNamespace(active_session_ids=lambda: [])
    _with_service(monkeypatch, svc)
    assert rec_state.recording_active() is False
    svc.active_session_ids = lambda: ["a", "b"]
    assert rec_state.recording_active() is True


def test_falls_back_to_single_session_id(monkeypatch):
    _with_service(monkeypatch, SimpleNamespace(active_session_id="a"))
    assert rec_state.recording_active() is True
    _with_service(monkeypatch, SimpleNamespace(active_session_id=None))
    assert rec_state.recording_active() is False


def test_broken_service_is_no_recording(monkeypatch):
    def boom():
        raise RuntimeError("x")
    _with_service(monkeypatch, SimpleNamespace(active_session_ids=boom))
    assert rec_state.recording_active() is False


def test_blocked_set_and_single_definition():
    assert rec_state.RECORDING_BLOCKED == frozenset({
        "transcribe", "video_analyze:describe_local", "transcript_suggest_meta",
        "transcript_ai:enrich", "settings:copilot_disable"})
    assert reembed._recording_active is rec_state.recording_active
