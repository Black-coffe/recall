"""MCP `get_live_video` / `get_live_frames` (story live-video-frames-04, contract C3).

Both are `_api` proxies to the app.py live-frame routes; here `_api` is mocked (no network, no
app.py) and the privacy filter at the MCP output is checked: mode, stop words, no-OCR frames.
"""
from __future__ import annotations

import base64

import httpx
import pytest

import mcp_server as m

JPEG = base64.b64encode(b'\xff\xd8\xff\xe0jpeg').decode()


@pytest.fixture
def env(tmp_path, monkeypatch):
    words = tmp_path / 'private.txt'
    # a stem, not a word: «Петренк» catches «Петренко» and «Петренку»
    words.write_text('Петренк\n', encoding='utf-8')
    monkeypatch.setenv('RECALL_PRIVATE_WORDS_FILE', str(words))
    monkeypatch.setenv('RECALL_MCP_VISUAL', 'images')
    return words


@pytest.fixture
def api(monkeypatch):
    calls = []
    responses = {}

    def fake(method, path, *, params=None, body=None, write=False, timeout=60.0):
        calls.append((method, path, params))
        for key, val in responses.items():
            if path.endswith(key):
                return val() if callable(val) else val
        return {'error': 'unexpected', 'error_code': 'INTERNAL_ERROR'}
    monkeypatch.setattr(m, '_api', fake)
    fake.calls, fake.responses = calls, responses
    return fake


def _frame(t, text, img=True):
    f = {'t': t, 'video_t': t - 3.0, 'segment': 0, 'width': 960, 'height': 540,
         'text': text, 'ocr': text is not None}
    if img:
        f['image_b64'] = JPEG
    return f


def _frames_resp(frames, next_t=None):
    return {'success': True, 'session_id': 'rec_1', 'track_id': 'mon0', 'mode': 'step',
            'frames': frames, 'count': len(frames), 'next_t': next_t, 'readable_until': 40.0,
            'scanned_until': 30.0, 'truncated': next_t is not None}


def test_registry_entries_copy_get_frame():
    for name in ('get_live_video', 'get_live_frames'):
        assert m._TOOL_CLASS[name] == m._TOOL_CLASS['get_frame']
        assert name in m._ABOUT_PROXY
        assert name in m._registered_tool_names()


def test_explicit_session_status_passthrough(env, api):
    api.responses['/live-video'] = {'success': True, 'session_id': 'rec_1', 'available': True,
                                    'tracks': [{'track_id': 'mon0', 'fps': 2,
                                                'readable_until': 12.0}]}
    out = m.get_live_video(session_id='rec_1')
    assert api.calls == [('GET', '/api/recording/rec_1/live-video', None)]
    assert out['tracks'][0]['readable_until'] == 12.0 and out['visual_mode'] == 'images'


def test_session_none_resolves_the_only_recording(env, api):
    api.responses['/api/recordings/active'] = {'success': True, 'active': True,
                                               'session_id': 'rec_a', 'count': 1,
                                               'sessions': [{'session_id': 'rec_a'}]}
    api.responses['/live-frames'] = _frames_resp([_frame(5, 'Budget')])
    out = m.get_live_frames(t=5)
    assert [c[1] for c in api.calls] == ['/api/recordings/active',
                                         '/api/recording/rec_a/live-frames']
    assert out[0]['count'] == 1


@pytest.mark.parametrize('call', [lambda: m.get_live_video(), lambda: m.get_live_frames(t=1)])
def test_several_recordings_are_ambiguous_without_second_call(env, api, call):
    api.responses['/api/recordings/active'] = {
        'success': True, 'active': True, 'session_id': None, 'count': 2,
        'sessions': [{'session_id': 'rec_a'}, {'session_id': 'rec_b'}]}
    out = call()
    assert out['error_code'] == 'AMBIGUOUS_SESSION'
    assert len(api.calls) == 1


def test_app_down_is_an_envelope_not_a_raise(env, monkeypatch):
    def refuse(*a, **k):
        raise httpx.ConnectError('down')
    monkeypatch.setattr(m.httpx, 'request', refuse)
    for out in (m.get_live_video(), m.get_live_frames(t=1),
                m.get_live_frames(session_id='rec_1', t=1)):
        assert out['error_code'] == 'APP_UNREACHABLE' and out['ok'] is False


def test_cursor_and_next_t_pass_verbatim(env, api):
    api.responses['/live-frames'] = _frames_resp([_frame(10, 'a'), _frame(11, 'b')],
                                                 next_t=11.437)
    out = m.get_live_frames(session_id='rec_1', start=0, end=30, cursor=9.123, limit=2)
    params = api.calls[0][2]
    assert params['cursor'] == 9.123 and params['limit'] == 2
    assert out[0]['next_t'] == 11.437


def test_limit_over_twelve_refused_mcp_side(env, api):
    out = m.get_live_frames(session_id='rec_1', start=0, end=60, limit=13)
    assert out['error_code'] == 'INVALID_ARGUMENT'
    assert api.calls == []


def test_frames_are_images_with_meta_and_null_text_is_dropped(env, api):
    api.responses['/live-frames'] = _frames_resp([_frame(1, 'Budget Q3'), _frame(2, None),
                                                  _frame(3, '')])
    out = m.get_live_frames(session_id='rec_1', start=0, end=5)
    head, images = out[0], out[1:]
    assert [f['t'] for f in head['frames']] == [1, 3]
    assert all({'t', 'video_t', 'text'} <= set(f) for f in head['frames'])
    assert 'image_b64' not in head['frames'][0]
    assert len(images) == 2
    from fastmcp.utilities.types import Image
    assert all(isinstance(i, Image) for i in images)
    assert head['private_hidden'] == 1


def test_mode_text_returns_meta_only(env, api, monkeypatch):
    monkeypatch.setenv('RECALL_MCP_VISUAL', 'text')
    api.responses['/live-frames'] = _frames_resp([_frame(1, 'Budget')])
    out = m.get_live_frames(session_id='rec_1', t=1)
    assert isinstance(out, dict) and out['frames'][0]['text'] == 'Budget'
    assert out['visual_mode'] == 'text'
    api.responses['/live-video'] = {'success': True, 'available': True, 'tracks': []}
    assert m.get_live_video(session_id='rec_1')['visual_mode'] == 'text'


def test_mode_off_refuses_both_without_api(env, api, monkeypatch):
    monkeypatch.setenv('RECALL_MCP_VISUAL', 'off')
    assert m.get_live_video(session_id='rec_1')['error_code'] == 'MCP_VISUAL_OFF'
    assert m.get_live_frames(session_id='rec_1', t=1)['error_code'] == 'MCP_VISUAL_OFF'
    assert api.calls == []


def test_live_frame_with_stop_word_is_hidden(env, api):
    api.responses['/live-frames'] = _frames_resp([
        _frame(1, 'Budget'), _frame(2, 'лист Петренку'), _frame(3, 'Roadmap')])
    out = m.get_live_frames(session_id='rec_1', start=0, end=5)
    head, images = out[0], out[1:]
    assert [f['t'] for f in head['frames']] == [1, 3]
    assert head['private_hidden'] == 1 and len(images) == 2
    # an unreadable stop list hides every frame (fail closed)
    env.write_bytes(b'\xff\xfe\xfa')
    out = m.get_live_frames(session_id='rec_1', start=0, end=5)
    assert out[0]['frames'] == [] and out[0]['private_hidden'] == 3 and len(out) == 1


def test_after_finalize_points_to_archive_tools(env, api):
    api.responses['/live-frames'] = {'success': True, 'available': False,
                                     'reason': 'recording finalized'}
    out = m.get_live_frames(session_id='rec_1', t=1)
    assert out['available'] is False and out['reason'] == 'recording finalized'
    assert 'get_frame' in out['hint'] and 'get_visual_timeline' in out['hint']
    api.responses['/live-video'] = {'success': True, 'available': False,
                                    'reason': 'recording finalized'}
    out = m.get_live_video(session_id='rec_1')
    assert out['available'] is False and 'get_frame' in out['hint']


def test_route_error_passes_through(env, api):
    api.responses['/live-frames'] = {'ok': False, 'error_code': 'PAST_LIVE_EDGE',
                                     'error': 'past edge', 'readable_until': 20.0}
    out = m.get_live_frames(session_id='rec_1', t=99)
    assert out['error_code'] == 'PAST_LIVE_EDGE' and out['readable_until'] == 20.0
