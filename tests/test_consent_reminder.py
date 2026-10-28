"""Consent reminder on every recording start (RECORDING_CONSENT_REMINDER, wave4-client-calls-05).

One non-blocking line: the /start response and manifest flag, /state, the auto-record «started»
prompt and toast, and the agent start notice. Off leaves today's responses unchanged.
"""
from __future__ import annotations

import importlib.util
from unittest.mock import MagicMock

import pytest

from app.core.i18n import tr
from app.services import approvals
from app.services.recording import call_watch as cw
from app.services.recording import start_notice, toast
from tests.test_call_watch import AutoRec, Rig, app_snap, recording
from tests.test_recording_blueprint import _DictStore, client, fake_service, reset_devices_cache  # noqa: F401

REMINDER_EN = tr('svc_recording.consent.reminder', lang='en')
REMINDER_UK = tr('svc_recording.consent.reminder', lang='uk')
START_BODY = {'mic_device_index': 1, 'system_device_index': 2,
              'mic_device_name': 'Mic A', 'system_device_name': 'Sys B'}


@pytest.fixture
def store(fake_service):  # noqa: F811
    s = _DictStore({'rec_test123': {'session_id': 'rec_test123', 'started_by': 'ui'}})
    fake_service.store = s
    return s


def _reminder(monkeypatch, on: bool):
    monkeypatch.setenv('RECORDING_CONSENT_REMINDER', '1' if on else '0')


def test_catalog_has_uk_and_en():
    assert REMINDER_EN.startswith('Reminder') and REMINDER_UK and REMINDER_UK != REMINDER_EN


def test_start_on_flags_response_and_manifest_off_is_identical(client, fake_service, store, monkeypatch):  # noqa: F811
    _reminder(monkeypatch, True)
    on = client.post('/api/recording/start', json=START_BODY)
    assert on.status_code == 200
    on_body = on.get_json()
    assert on_body['consent_reminder'] is True
    assert store.manifests['rec_test123']['consent_reminder'] is True

    store.manifests['rec_test123'] = {'session_id': 'rec_test123', 'started_by': 'ui'}
    _reminder(monkeypatch, False)
    off = client.post('/api/recording/start', json=START_BODY)
    assert off.status_code == 200
    off_body = off.get_json()
    assert 'consent_reminder' not in off_body
    assert 'consent_reminder' not in store.manifests['rec_test123']
    assert {k: v for k, v in on_body.items() if k != 'consent_reminder'} == off_body


def test_state_reads_flag_from_manifest(client, fake_service, store):  # noqa: F811
    store.manifests['rec_test123']['consent_reminder'] = True
    assert client.get('/api/recording/rec_test123/state').get_json()['state']['consent_reminder'] is True
    del store.manifests['rec_test123']['consent_reminder']
    assert client.get('/api/recording/rec_test123/state').get_json()['state']['consent_reminder'] is False


def test_start_call_recording_carries_flag(client, fake_service, store, monkeypatch):  # noqa: F811
    from app.blueprints import recording as recording_module
    monkeypatch.setattr(recording_module.stop_control, 'arm_timer', MagicMock())
    _reminder(monkeypatch, True)
    with client.application.app_context():
        res = recording_module.start_call_recording('zoom.exe', started_by='auto_call')
    assert res == {'ok': True, 'session_id': 'rec_test123', 'consent_reminder': True}
    _reminder(monkeypatch, False)
    with client.application.app_context():
        res = recording_module.start_call_recording('zoom.exe', started_by='auto_call')
    assert res == {'ok': True, 'session_id': 'rec_test123'}


def test_agent_start_passes_flag_to_notice(client, fake_service, store, notice_stub, monkeypatch):  # noqa: F811
    from app.blueprints import recording as recording_module
    monkeypatch.setattr(recording_module, '_caller_started_by', lambda: 'agent')
    _reminder(monkeypatch, True)
    assert client.post('/api/recording/start', json=START_BODY).status_code == 200
    assert notice_stub.calls[-1]['consent_reminder'] is True
    _reminder(monkeypatch, False)
    assert client.post('/api/recording/start', json=START_BODY).status_code == 200
    assert 'consent_reminder' not in notice_stub.calls[-1]


def _started_toast(tmp_path, monkeypatch, result):
    xmls = []
    monkeypatch.setattr(toast, 'ensure_registered', lambda *a, **k: True)
    monkeypatch.setattr(toast, 'show', lambda xml, tag, **k: xmls.append(xml) or True)
    r = Rig(tmp_path, action='record', auto_record_fn=AutoRec(result))
    r.notifier = r.w._notifier = cw.ToastNotifier()
    r.run(31, app_snap())
    r.live = [recording('rec7', source={'mode': 'all'})]
    r.run(5)
    return r, xmls


def test_auto_record_started_prompt_carries_reminder(tmp_path, monkeypatch):
    r, xmls = _started_toast(tmp_path, monkeypatch,
                             {'ok': True, 'session_id': 'rec7', 'consent_reminder': True})
    [p] = r.w.pending()
    assert p['kind'] == 'started' and p['consent_reminder'] is True
    [xml] = xmls
    assert tr('svc_recording.consent.reminder') in xml


def test_auto_record_without_flag_is_todays_toast(tmp_path, monkeypatch):
    r, xmls = _started_toast(tmp_path, monkeypatch, {'ok': True, 'session_id': 'rec7'})
    [p] = r.w.pending()
    assert 'consent_reminder' not in p
    [xml] = xmls
    assert tr('svc_recording.consent.reminder') not in xml
    assert xml == toast.build_xml(
        tr('svc_recording.call.started.title'),
        tr('svc_recording.call.started.body', label='Zoom'),
        [(tr('svc_recording.call.started.stop'), r.w._url(p['id'], 'stop')),
         (tr('svc_recording.call.started.keep'), r.w._url(p['id'], 'keep'))],
        reminder=True)


def _real_notify_start():
    """conftest stubs ``notify_start`` outside test_start_notice: load an unpatched copy."""
    spec = importlib.util.spec_from_file_location('start_notice_real', start_notice.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.notify_start


@pytest.mark.parametrize('on', [True, False], ids=['on', 'off'])
def test_agent_notice_ends_with_reminder(monkeypatch, on):
    monkeypatch.setattr(approvals, 'station_visible', lambda: True)
    monkeypatch.setattr(toast, 'ensure_registered', lambda *a, **k: True)
    xmls = []
    monkeypatch.setattr(toast, 'show', lambda xml, tag, **k: xmls.append(xml) or True)
    bodies = []
    real_build = toast.build_xml
    monkeypatch.setattr(toast, 'build_xml', lambda title, body, actions, **k:
                        bodies.append(body) or real_build(title, body, actions, **k))
    assert _real_notify_start()(
        mic_name='Mic', system_apps=['zoom.exe'], system_all=False, language='uk', name='Call',
        with_screen=False, claimed_label='Claude Desktop', lang='en', consent_reminder=on) is True
    [body] = bodies
    assert body.endswith(REMINDER_EN) is on
    assert (REMINDER_EN in xmls[0]) is on
