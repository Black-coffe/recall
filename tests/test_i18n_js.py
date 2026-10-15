"""Клієнтський шар перекладу: паритет словників uk/en, скан ключів, вибір мови.

Словники `static/js/recall/i18n/*.js` вантажаться через node з фейковим
`window.Recall` (без браузера). Тест сам підхоплює нові файли словників і нові
виклики `R.t('…')` у `static/js/recall/**` — правок не потребує.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js" / "recall"
DICTS = sorted((JS / "i18n").glob("*.js"))
SHELL = ROOT / "templates" / "shell.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not in PATH")

_VAR = re.compile(r"\{(\w+)\}")
_CALL = re.compile(r"(?:\bR\.t|\bRecall\.t|\bT|\bL)\(\s*(['\"])([a-z]\w*\.[\w.]*\w)\1")
_ATTR = re.compile(r"data-i18n(?:-title|-placeholder|-aria-label)?=\"([^\"]+)\"")


def _node(script, *args):
    r = subprocess.run(["node", "-e", script, *args], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _load(path):
    out = _node(
        "const fs=require('fs');let got=null;"
        "global.window={Recall:{i18n:{add:(a,b)=>{got={area:a,b:b};}}}};global.Recall=window.Recall;"
        "eval(fs.readFileSync(process.argv[1],'utf8'));"
        "console.log(JSON.stringify(got));",
        str(path))
    return json.loads(out)


def _vars(val):
    forms = val.values() if isinstance(val, dict) else [val]
    return [frozenset(_VAR.findall(f)) for f in forms]


def _all_keys():
    keys = set()
    for p in DICTS:
        keys |= set(_load(p)["b"]["uk"])
    return keys


def test_dictionary_files_exist():
    names = {p.stem for p in DICTS}
    assert {"core", "capture", "transcript", "settings", "library", "knowledge", "telegram"} <= names


@pytest.mark.parametrize("path", DICTS, ids=lambda p: p.stem)
def test_dictionary_parity(path):
    d = _load(path)
    uk, en = d["b"]["uk"], d["b"]["en"]
    assert set(uk) == set(en), sorted(set(uk) ^ set(en))
    for key in uk:
        assert key.startswith(d["area"] + "."), key
        u, e = uk[key], en[key]
        assert isinstance(u, dict) == isinstance(e, dict), "plural in one language only: " + key
        if isinstance(e, dict):
            assert "other" in e, key
            assert "other" in u or "many" in u, key
        allvars = _vars(u) + _vars(e)
        assert all(v == allvars[0] for v in allvars), "different {vars}: " + key


def test_every_used_key_exists():
    keys = _all_keys()
    used = {}
    for p in JS.rglob("*.js"):
        if "i18n" in p.relative_to(JS).parts or p.name == "i18n.js":
            continue
        for m in _CALL.finditer(p.read_text(encoding="utf-8")):
            used.setdefault(m.group(2), p.name)
    html = SHELL.read_text(encoding="utf-8")
    for m in _ATTR.finditer(html):
        used.setdefault(m.group(1), "shell.html")
    missing = {k: f for k, f in used.items() if k not in keys}
    assert not missing, missing


_RUN = r"""
const fs=require('fs');
function boot(attr, langs){
  const attrs={'data-ui-lang':attr}; const html={getAttribute:k=>attrs[k]||null,setAttribute:(k,v)=>{attrs[k]=v;}};
  global.document={documentElement:html,querySelectorAll:()=>[]};
  Object.defineProperty(globalThis,'navigator',{value:{languages:langs,language:langs[0]},configurable:true,writable:true});
  global.window={Recall:{}};global.Recall=window.Recall;
  eval(fs.readFileSync(process.argv[1],'utf8'));
  return {R:window.Recall,html:attrs};
}
const out={};
const cases=[['auto',['uk-UA']],['auto',['en-US']],['auto',['ru']],['auto',['de']],['en',['uk-UA']],['uk',['en-US']]];
out.langs=cases.map(c=>{const b=boot(c[0],c[1]);return [b.R.i18n.lang,b.html.lang];});
const b=boot('uk',['en']);
b.R.i18n.add('x',{uk:{'x.d':{one:'{count} день',few:'{count} дні',many:'{count} днів',other:'o'},'x.only':'uk'},en:{'x.d':{one:'{count} day',other:'{count} days'}}});
out.few=b.R.t('x.d',{count:3});
const e=boot('en',['uk']);
e.R.i18n.add('x',{uk:{'x.d':{one:'a',other:'b'}},en:{'x.d':{one:'{count} day',other:'{count} days'}}});
out.other=e.R.t('x.d',{count:3});
let warned=0;console.warn=()=>{warned++;};
out.unknown=e.R.t('x.nope');out.warned=warned;
out.header=e.R.i18n.headers();
console.log(JSON.stringify(out));
"""


def test_language_detection_plurals_and_fallback():
    out = json.loads(_node(_RUN, str(JS / "i18n.js")))
    assert out["langs"] == [["uk", "uk"], ["en", "en"], ["en", "en"], ["en", "en"], ["en", "en"], ["uk", "uk"]]
    assert out["few"] == "3 дні"
    assert out["other"] == "3 days"
    assert out["unknown"] == "x.nope" and out["warned"] == 1
    assert out["header"] == {"X-Recall-Lang": "en"}
