"""Tests for app/services/recording/start_notice.py: the toast that must precede an agent start."""

import pytest

from app.core.i18n import tr
from app.services import approvals
from app.services.recording import start_notice, toast

ARGS = dict(mic_name='Мікрофон (FOX)', system_apps=['zoom.exe'], system_all=False,
            language='uk', name='Дзвінок', with_screen=False, claimed_label='Claude Desktop')


@pytest.fixture
def visible(monkeypatch):
    monkeypatch.setattr(approvals, 'station_visible', lambda: True)


@pytest.fixture
def posted(monkeypatch, visible):
    calls = []

    def fake_post(title, body, tag, icon_path):
        calls.append({'title': title, 'body': body, 'tag': tag})
        return True

    monkeypatch.setattr(start_notice, '_post', fake_post)
    return calls


def test_no_visible_desktop_refuses_without_posting(monkeypatch):
    monkeypatch.setattr(approvals, 'station_visible', lambda: False)
    monkeypatch.setattr(start_notice, '_post', lambda *a, **k: pytest.fail('posted without a desktop'))
    assert start_notice.notify_start(**ARGS, lang='en') is False


def test_ensure_registered_false_refuses(monkeypatch, visible):
    monkeypatch.setattr(toast, 'ensure_registered', lambda *a, **k: False)
    monkeypatch.setattr(toast, 'show', lambda *a, **k: pytest.fail('show called after failed register'))
    assert start_notice.notify_start(**ARGS, lang='en') is False


def test_show_false_refuses(monkeypatch, visible):
    monkeypatch.setattr(toast, 'ensure_registered', lambda *a, **k: True)
    monkeypatch.setattr(toast, 'show', lambda *a, **k: False)
    assert start_notice.notify_start(**ARGS, lang='en') is False


def test_show_raising_refuses(monkeypatch, visible):
    def boom(*a, **k):
        raise OSError('powershell gone')

    monkeypatch.setattr(toast, 'ensure_registered', lambda *a, **k: True)
    monkeypatch.setattr(toast, 'show', boom)
    assert start_notice.notify_start(**ARGS, lang='en') is False


def test_true_only_after_show_returned_true(monkeypatch, visible):
    order = []
    monkeypatch.setattr(toast, 'ensure_registered', lambda *a, **k: order.append('reg') or True)
    monkeypatch.setattr(toast, 'show', lambda xml, tag: order.append('show') or True)
    assert start_notice.notify_start(**ARGS, lang='en') is True
    assert order == ['reg', 'show']


@pytest.mark.parametrize('lang', ['uk', 'en'])
def test_body_names_every_field_in_lang(posted, lang):
    assert start_notice.notify_start(**ARGS, lang=lang) is True
    body = posted[0]['body']
    assert posted[0]['title'] == tr('capture.rec.notice.title', lang=lang)
    assert ARGS['mic_name'] in body
    assert 'zoom.exe' in body
    assert ARGS['language'] in body
    assert ARGS['name'] in body
    assert body.endswith(ARGS['claimed_label'])
    assert tr('capture.rec.notice.with_screen', lang=lang) not in body


@pytest.mark.parametrize('lang', ['uk', 'en'])
def test_system_all_and_mic_only_and_no_name(posted, lang):
    start_notice.notify_start(**{**ARGS, 'system_all': True, 'name': None}, lang=lang)
    body = posted[-1]['body']
    assert tr('capture.rec.notice.all_system', lang=lang) in body
    assert tr('capture.rec.notice.no_name', lang=lang) in body

    start_notice.notify_start(**{**ARGS, 'system_all': False, 'system_apps': []}, lang=lang)
    assert tr('capture.rec.notice.no_system', lang=lang) in posted[-1]['body']


def test_with_screen_appears_exactly_when_requested(posted):
    start_notice.notify_start(**{**ARGS, 'with_screen': True}, lang='en')
    assert 'with screen' in posted[-1]['body']
    start_notice.notify_start(**{**ARGS, 'with_screen': False}, lang='en')
    assert 'with screen' not in posted[-1]['body']


def test_post_is_called_once_with_a_fresh_tag(posted):
    start_notice.notify_start(**ARGS, lang='en')
    start_notice.notify_start(**ARGS, lang='en')
    assert len(posted) == 2
    assert posted[0]['tag'] != posted[1]['tag']
    assert posted[0]['tag'].startswith('rec-start-')


def test_post_raising_refuses_and_does_not_raise(monkeypatch, visible):
    def boom(*a, **k):
        raise RuntimeError('wrapper broke')

    monkeypatch.setattr(start_notice, '_post', boom)
    assert start_notice.notify_start(**ARGS, lang='en') is False
