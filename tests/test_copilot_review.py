"""Copilot review screen /copilot (wave4-client-calls-08).

Service: `list_sessions` rows carry shown / suppressed / thumbs counts over a temporary DB.
Static: the view is read-only (no action posts), wired in shell.html and router.js, sw.js bumped.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from app.blueprints.copilot import copilot_bp
from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.copilot.service import CopilotService

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js" / "recall"
VIEW = JS / "views" / "copilot.js"
DICT = JS / "i18n" / "copilot.js"
SHELL = ROOT / "templates" / "shell.html"
ROUTER = JS / "router.js"
SW = ROOT / "static" / "sw.js"


@pytest.fixture()
def svc(tmp_path):
    db = str(tmp_path / "t.db")
    init_database(db)
    return CopilotService(db)


def _session(svc) -> int:
    with get_db_connection(svc._db_path) as conn:
        cur = conn.execute("INSERT INTO copilot_sessions (recording_session_id, mode, status) "
                           "VALUES ('rec-1', 'medium', 'ended')")
        conn.commit()
        return cur.lastrowid


def _fixture(svc) -> int:
    cs = _session(svc)
    a = svc.log_event(cs, 'insight_local', payload={'text': 'a', 'shown': True})
    svc.log_event(cs, 'insight_local', payload={'text': 'b', 'shown': False})
    svc.log_event(cs, 'insight_local', payload={'text': 'c'})
    svc.log_event(cs, 'insight_verified', payload={'text': 'd', 'shown': False})
    svc.log_event(cs, 'scope_notice', payload={'text': 'scope', 'shown': False})
    svc.log_action(cs, 'thumbs_up', ref_event_id=a, payload={'actor': 'ui'})
    svc.log_action(cs, 'thumbs_down', ref_event_id=a)
    svc.log_action(cs, 'thumbs_up', ref_event_id=a, payload={'actor': 'mcp'})
    return cs


def test_list_sessions_counts(svc):
    _fixture(svc)
    row = svc.list_sessions()[0]
    assert (row['shown'], row['suppressed'], row['thumbs_up'], row['thumbs_down']) == (2, 2, 1, 1)


def test_sessions_route_carries_counts(svc, monkeypatch):
    _fixture(svc)
    from app import state
    monkeypatch.setattr(state, 'copilot_service', svc, raising=False)
    app = Flask(__name__)
    app.register_blueprint(copilot_bp)
    r = app.test_client().get('/api/copilot/sessions')
    assert r.status_code == 200, r.get_data(as_text=True)
    row = r.get_json()['sessions'][0]
    assert {k: row[k] for k in ('shown', 'suppressed', 'thumbs_up', 'thumbs_down')} == \
        {'shown': 2, 'suppressed': 2, 'thumbs_up': 1, 'thumbs_down': 1}


def test_view_is_read_only():
    src = VIEW.read_text(encoding="utf-8")
    assert '/action' not in src
    assert not re.search(r"R\.api\.(post|put|patch|del)", src)
    assert "method:" not in src
    assert "/api/copilot/sessions" in src and "/timeline" in src
    assert "/export?format=md" in src and "/export?format=json" in src
    assert "payload.shown === false" in src


def test_shell_wires_scripts_and_nav():
    html = SHELL.read_text(encoding="utf-8")
    i_dict = html.find("js/recall/i18n/copilot.js")
    i_view = html.find("js/recall/views/copilot.js")
    assert i_dict != -1 and i_view != -1 and i_dict < i_view
    assert re.search(r'<a class="rc-nav__item" href="/copilot" data-route="copilot">', html)


def test_router_has_route():
    assert re.search(r"pattern:\s*'/copilot',\s*view:\s*'copilot'", ROUTER.read_text(encoding="utf-8"))


def test_sw_version():
    assert "const VERSION = 'recall-v97';" in SW.read_text(encoding="utf-8")


def test_dictionary_both_languages():
    src = DICT.read_text(encoding="utf-8")
    assert src.count('"copilot.nav":') == 2
