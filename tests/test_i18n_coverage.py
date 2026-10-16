"""Translation completeness guard: no Cyrillic literals in the SPA/template/jsonify, server errors are localized."""
import ast
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
CYR = re.compile("[А-Яа-яІіЇїЄєҐґЁёЪъЫыЭэ]")

# (path relative to the root, literal fragment) -> reason. Add only NON-interface text.
# (file, literal predicate) -> reason. Only NON-interface text.
_LANG_NAMES = {"'Українська'", "'Русский'", "'English'"}
JS_ALLOW = [
    ("static/js/recall/util.js", lambda s: len(s) <= 8, "таблиця транслітерації слагів (1 літера)"),
    ("static/js/recall/views/telegram.js", lambda s: s in ("/ё/", "'е'"), "нормалізація ё->е при порівнянні імен"),
    ("static/js/recall/views/settings.js", lambda s: s in _LANG_NAMES, "самоназва мови (endonym) не перекладається"),
    ("static/js/recall/util.js", lambda s: s in _LANG_NAMES, "самоназва мови (endonym) не перекладається"),
    ("static/js/recall/views/transcript.js", lambda s: s.startswith(("'і та а але", "'и но или")),
     "списки стоп-слів uk/ru для підсвітки термінів"),
    ("static/js/recall/ui.js", lambda s: s.startswith("/^Запис ("),
     "детектор авто-імені запису, збереженого в БД українською"),
    ("static/js/recall/views/transcript.js", lambda s: "'Джерело'" in s or s == "'Ко-пілот'",
     "fallback частини слагу URL; зміна ламає наявні посилання"),
    ("static/js/recall/research_export.js", lambda s: "а-яіїєґ" in s, "клас кирилиці в слагу імені файлу"),
]


def _skip_template(src, i):
    """i — position of the opening backtick; returns the position after the closing one (with nested ${ {} `` })."""
    n, j = len(src), i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2; continue
        if ch == "`":
            return j + 1
        if src.startswith("${", j):
            j += 2; b = 0
            while j < n:
                e = src[j]
                if e in "'\"":
                    j += 1
                    while j < n and src[j] != e:
                        j += 2 if src[j] == "\\" else 1
                elif e == "`":
                    j = _skip_template(src, j) - 1
                elif e == "{":
                    b += 1
                elif e == "}":
                    if b == 0:
                        break
                    b -= 1
                j += 1
        j += 1
    return n


def _js_literals(src):
    """Returns (string, literal) for string/template literals and regexes; skips comments."""
    out, i, n, line = [], 0, len(src), 1
    prev = ""
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1; i += 1; continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2); j = n if j < 0 else j + 2
            line += src.count("\n", i, j); i = j; continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append((line, src[i:j + 1])); i = j + 1; prev = "x"; continue
        if c == "`":
            j = _skip_template(src, i)
            out.append((line, src[i:j])); line += src.count(chr(10), i, j); i = j; prev = "x"; continue
        if c == "/" and (prev == "" or prev in "(,=:[!&|?{};"):
            j, cls = i + 1, False
            while j < n and src[j] != "\n":
                if src[j] == "\\": j += 2; continue
                if src[j] == "[": cls = True
                elif src[j] == "]": cls = False
                elif src[j] == "/" and not cls: break
                j += 1
            out.append((line, src[i:j + 1])); i = j + 1; prev = "x"; continue
        if not c.isspace():
            prev = c
        i += 1
    return out


def _allowed_js(rel, lit):
    return any(k == rel and pred(lit) for (k, pred, _why) in JS_ALLOW)


def test_no_cyrillic_literals_in_spa_scripts():
    bad = []
    for p in sorted((ROOT / "static/js/recall").rglob("*.js")):
        if "i18n" in p.relative_to(ROOT / "static/js/recall").parts[:1] and p.parent.name == "i18n":
            continue
        rel = p.relative_to(ROOT).as_posix()
        for line, lit in _js_literals(p.read_text(encoding="utf-8")):
            if CYR.search(lit) and not _allowed_js(rel, lit):
                bad.append(f"{rel}:{line}: {lit[:70]}")
    assert not bad, "Cyrillic literals outside the dictionaries:\n" + "\n".join(bad)


class _Shell(HTMLParser):
    VOID = {"meta", "link", "br", "img", "input", "hr", "source"}

    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        names = [a for a, _ in attrs]
        marked = any(a.startswith("data-i18n") for a in names)
        if not marked and not any(self.stack):
            for a, v in attrs:
                if v and CYR.search(v):
                    self.bad.append(f"{self.getpos()[0]}: <{tag} {a}>")
        if tag not in self.VOID:
            self.stack.append(marked)

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if CYR.search(data) and not any(self.stack):
            self.bad.append(f"{self.getpos()[0]}: {data.strip()[:60]}")


def test_shell_html_text_is_marked_for_translation():
    src = re.sub(r"<!--.*?-->", "", (ROOT / "templates/shell.html").read_text(encoding="utf-8"), flags=re.S)
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    p = _Shell()
    p.feed(src)
    assert not p.bad, "Cyrillic outside data-i18n in shell.html:\n" + "\n".join(p.bad)


def _py_files():
    return sorted((ROOT / "app/blueprints").glob("*.py")) + [ROOT / "app.py"]


def test_no_cyrillic_literals_in_jsonify_calls():
    bad = []
    for p in _py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "jsonify":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and CYR.search(sub.value):
                        bad.append(f"{p.name}:{sub.lineno}: {sub.value[:60]}")
    assert not bad, "Cyrillic in jsonify:\n" + "\n".join(bad)


def test_invalid_input_errors_are_english_with_header(monkeypatch, tmp_path):
    from app.blueprints import settings_api
    monkeypatch.setattr(settings_api, "_env_path", lambda: tmp_path / ".env")
    app = Flask(__name__)
    app.register_blueprint(settings_api.settings_bp)
    c = app.test_client()
    h = {"X-Recall-Lang": "en"}
    cases = [
        ("/api/settings/ui-lang", {"value": "fr"}),
        ("/api/settings/anthropic-key", {"api_key": 5}),
        ("/api/settings/anthropic-key", {"api_key": ""}),
    ]
    seen = 0
    for url, body in cases:
        r = c.post(url, json=body, headers=h)
        if r.status_code >= 400 and r.get_json() and "error" in r.get_json():
            seen += 1
            assert not CYR.search(r.get_json()["error"]), (url, r.get_json())
    assert seen >= 2
