"""Server-side translation layer: language setting, tr/tr_n, catalog parity, key scan."""
import ast
import logging
import re
from pathlib import Path

import pytest
from flask import Flask, g

from app.core import error_handlers, i18n, i18n_catalog

ROOT = Path(__file__).resolve().parent.parent
_VARS = re.compile(r"\{(\w+)\}")


# ------------------------------------------------------------------ server_lang / tr / tr_n

@pytest.mark.parametrize("setting,expected", [
    ("auto", "uk"), ("uk", "uk"), ("en", "en"), ("fr", "uk"), ("", "uk"),
])
def test_server_lang(monkeypatch, setting, expected):
    monkeypatch.setenv("RECALL_UI_LANG", setting)
    assert i18n.server_lang() == expected


def test_tr_outside_request_is_uk():
    assert i18n.tr("core.settings.api_key_empty") == "Порожній ключ"


def test_tr_unknown_key_returns_key_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert i18n.tr("core.nope.такого_нема") == "core.nope.такого_нема"
    assert "core.nope" in caplog.text


def test_tr_vars():
    assert i18n.tr("core.system.model_downloaded", lang="uk", model="medium") == "Модель medium успішно завантажена"
    assert i18n.tr("core.system.model_downloaded", lang="en", model="medium") == "Model medium downloaded successfully"


def test_tr_n_plurals(monkeypatch):
    monkeypatch.setattr(i18n, "_catalog", {"t.x": {
        "uk": {"one": "{n} запис", "few": "{n} записи", "many": "{n} записів"},
        "en": {"one": "{n} record", "other": "{n} records"},
    }})
    uk = {n: i18n.tr_n("t.x", n, lang="uk") for n in (1, 3, 5, 11, 21, 22, 25)}
    assert uk == {1: "1 запис", 3: "3 записи", 5: "5 записів", 11: "11 записів",
                  21: "21 запис", 22: "22 записи", 25: "25 записів"}
    assert i18n.tr_n("t.x", 1, lang="en") == "1 record"
    assert i18n.tr_n("t.x", 5, lang="en") == "5 records"


# ------------------------------------------------------------------ HTTP

@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app.blueprints import settings_api
    monkeypatch.setattr(settings_api, "_env_path", lambda: tmp_path / ".env")
    app = Flask(__name__)
    app.register_blueprint(settings_api.settings_bp)
    return app.test_client(), tmp_path / ".env"


def test_ui_lang_roundtrip(client):
    c, env = client
    assert c.get("/api/settings/ui-lang").get_json() == {"success": True, "value": "auto"}
    r = c.post("/api/settings/ui-lang", json={"value": "en"})
    assert r.status_code == 200 and r.get_json() == {"success": True, "value": "en"}
    assert "RECALL_UI_LANG" in env.read_text(encoding="utf-8")
    assert "en" in env.read_text(encoding="utf-8")
    assert c.get("/api/settings/ui-lang").get_json()["value"] == "en"


def test_ui_lang_invalid_localized(client, monkeypatch):
    c, _ = client
    uk = "Невірне значення мови: очікується auto, uk або en"
    r = c.post("/api/settings/ui-lang", json={"value": "fr"})
    assert r.status_code == 400 and r.get_json()["error"] == uk
    assert c.post("/api/settings/ui-lang").status_code == 400
    r = c.post("/api/settings/ui-lang", json={"value": "fr"}, headers={"X-Recall-Lang": "en"})
    assert r.get_json()["error"] == i18n.tr("core.settings.ui_lang_invalid", lang="en")
    assert r.get_json()["error"] != uk
    r = c.post("/api/settings/ui-lang", json={"value": "fr"}, headers={"X-Recall-Lang": "uk"})
    assert r.get_json()["error"] == uk
    r = c.post("/api/settings/ui-lang", json={"value": "fr"}, headers={"X-Recall-Lang": "сміття"})
    assert r.get_json()["error"] == uk
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    r = c.post("/api/settings/ui-lang", json={"value": "fr"})
    assert r.get_json()["error"] == i18n.tr("core.settings.ui_lang_invalid", lang="en")


def test_shell_render_gets_current_ui_lang(monkeypatch):
    from app.blueprints import system
    seen = {}
    monkeypatch.setattr(system, "render_template", lambda name, **kw: seen.update(kw) or "ok")
    app = Flask(__name__)
    app.register_blueprint(system.system_bp)
    c = app.test_client()
    monkeypatch.setenv("RECALL_UI_LANG", "en")
    assert c.get("/").status_code == 200
    assert seen["ui_lang"] == "en"
    monkeypatch.setenv("RECALL_UI_LANG", "uk")
    c.get("/")
    assert seen["ui_lang"] == "uk"


def test_generic_500_localized():
    app = Flask(__name__)
    app.testing = False

    @app.before_request
    def _rid():
        g.request_id = "rid"

    @app.route("/boom")
    def boom():
        raise ValueError("секрет")

    error_handlers.register(app)
    c = app.test_client()
    uk = c.get("/boom").get_json()["error"]
    assert uk == error_handlers.GENERIC_MESSAGE
    en = c.get("/boom", headers={"X-Recall-Lang": "en"}).get_json()["error"]
    assert en == i18n.tr("core.error.internal", lang="en") and en != uk


# ------------------------------------------------------------------ catalog

def _forms(value):
    return tuple(sorted(value)) if isinstance(value, dict) else None


def _vars(value):
    texts = value.values() if isinstance(value, dict) else [value]
    return [frozenset(_VARS.findall(t)) for t in texts]


def test_catalog_parity():
    for module, messages in i18n_catalog.load_modules().items():
        for key, entry in messages.items():
            assert set(entry) == {"uk", "en"}, f"{module}:{key}: exactly uk and en are required"
            uk, en = entry["uk"], entry["en"]
            assert isinstance(uk, dict) == isinstance(en, dict), f"{module}:{key}: shape differs"
            if isinstance(uk, dict):
                assert set(uk) == {"one", "few", "many"}, f"{module}:{key}: uk forms"
                assert set(en) == {"one", "other"}, f"{module}:{key}: en forms"
            all_vars = set(_vars(uk) + _vars(en))
            assert len(all_vars) == 1, f"{module}:{key}: different {{vars}} across languages/forms"
            assert key.startswith(module + "."), f"{module}:{key}: key prefix = area"


def test_catalog_duplicate_key_fails(monkeypatch):
    monkeypatch.setattr(i18n_catalog, "load_modules", lambda: {
        "a": {"a.k": {"uk": "ключ", "en": "key"}},
        "b": {"a.k": {"uk": "ключ", "en": "key"}},
    })
    with pytest.raises(ValueError):
        i18n_catalog.merged()


def test_catalog_has_real_duplicates_none():
    i18n_catalog.merged()


def _scan_keys():
    files = [ROOT / "app.py", *(ROOT / "app").rglob("*.py")]
    for f in files:
        if "i18n_catalog" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name in ("tr", "tr_n") and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    yield f, node.args[0].value


def test_all_tr_literals_exist_in_catalog():
    catalog = i18n_catalog.merged()
    missing = [f"{f.relative_to(ROOT)}: {k}" for f, k in _scan_keys() if k not in catalog]
    assert not missing, missing
