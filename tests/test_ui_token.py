"""Per-boot UI token: rendered in the shell for trusted requests, classified, sent on every write."""
import pathlib
import re

from flask import Flask

from app.core import auth
from app.blueprints.system import system_bp

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LOOP = {"REMOTE_ADDR": "127.0.0.1"}


def _classify(headers):
    app = Flask(__name__, template_folder=_ROOT / 'templates', static_folder=_ROOT / 'static')
    with app.test_request_context("/api/x", headers=headers):
        return auth.classify_request()


def test_shell_carries_token_for_trusted_request():
    app = Flask(__name__, template_folder=_ROOT / 'templates', static_folder=_ROOT / 'static')
    app.register_blueprint(system_bp)
    r = app.test_client().get("/", environ_overrides=_LOOP)
    assert f'name="recall-ui-token" content="{auth.ui_token()}"'.encode() in r.data
    assert auth.ui_token()


def test_shell_token_empty_for_untrusted_proxied(monkeypatch):
    monkeypatch.setenv("RECALL_PROXY_ALLOWED_IPS", "100.64.0.7")
    app = Flask(__name__, template_folder=_ROOT / 'templates', static_folder=_ROOT / 'static')
    app.register_blueprint(system_bp)
    r = app.test_client().get("/", headers={"X-Forwarded-For": "198.51.100.9"}, environ_overrides=_LOOP)
    assert b'name="recall-ui-token" content=""' in r.data


def test_classify_request():
    assert _classify({"X-Recall-UI": auth.ui_token()}) == ("ui", "ui_token")
    assert _classify({"X-Recall-UI": "wrong"}) == ("unknown", None)
    assert _classify({}) == ("unknown", None)
    assert _classify({"X-Recall-Actor": "mcp"}) == ("unknown", None)
    assert _classify({"X-Recall-Actor": "ui"}) == ("unknown", None)


def test_every_non_get_site_sends_the_header():
    root = pathlib.Path(__file__).resolve().parent.parent / "static" / "js" / "recall"
    bad = []
    for f in root.rglob("*.js"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"fetch\(|xhr\.open\(", text):
            window = text[m.start(): m.start() + 400]
            if not re.search(r"method:\s*'(POST|PUT|PATCH|DELETE)'|xhr\.open\('(POST|PUT|PATCH|DELETE)'", window):
                continue
            if not re.search(r"UH\(|uiHeaders\(|X-Recall-UI", text[m.start(): m.start() + 600]):
                bad.append(f"{f.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not bad, bad
