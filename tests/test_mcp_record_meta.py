"""Спека editable-title-description, історія 04: `display_name`/`title`/`description`
у read-тулзах MCP.

Direct-DB тулзи (`get_transcript`, `list_recent`, ресурс `recall://transcript/{id}`)
перевіряються на тимчасовій SQLite (`init_database`), без запущеного app.py.
Проксі-тулзи (`search_archive`, `ask_archive`) перевіряються з замоканим `_api()` —
тулз лише переупаковує JSON app.py, не рахує display_name сам, тож тест ловить
саме втрату поля при переупакуванні, а не його обчислення.

Кириличні фікстури — той самий урок, що й у `tests/test_record_meta.py`
(`plan-dictated-sql-ignored-own-memory`).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import mcp_server as m
from app.db.migrations import init_database
from app.utils.proc import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "record_meta.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def _add(db_path, *, source_name="Дзвінок із Барʼєрами.mp3",
         text="Обговорили бюджет Барселони", title=None, description=None,
         source_type="file"):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "title, description) VALUES (?, ?, ?, ?, ?)",
        (source_type, source_name, text, title, description),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


# ============================================================
# Direct-DB: get_transcript
# ============================================================

def test_get_transcript_with_title(db):
    tid = _add(db, source_name="call_2026.mp3", title="Нарада щодо Гуцульщини",
               description="Про бюджет")
    d = m.get_transcript(tid)
    assert d["display_name"] == "Нарада щодо Гуцульщини"
    assert d["source_name"] == "call_2026.mp3"
    assert d["title"] == "Нарада щодо Гуцульщини"
    assert d["description"] == "Про бюджет"


def test_get_transcript_without_title_falls_back(db):
    tid = _add(db, source_name="Зустріч з Андрієм.mp3")
    d = m.get_transcript(tid)
    assert d["display_name"] == "Зустріч з Андрієм.mp3"
    assert d["source_name"] == "Зустріч з Андрієм.mp3"
    assert d["title"] is None


# ============================================================
# Direct-DB: list_recent
# ============================================================

def test_list_recent_exposes_meta(db):
    with_title = _add(db, source_name="call_a.mp3", title="Нарада з Іваном",
                      description="Про бюджет")
    without_title = _add(db, source_name="call_b.mp3")
    items = {i["id"]: i for i in m.list_recent(limit=20)}

    assert items[with_title]["display_name"] == "Нарада з Іваном"
    assert items[with_title]["title"] == "Нарада з Іваном"
    assert items[with_title]["description"] == "Про бюджет"
    assert items[without_title]["display_name"] == "call_b.mp3"
    assert items[without_title]["title"] is None


# ============================================================
# Ресурс recall://transcript/{id} — заголовок
# ============================================================

def test_transcript_resource_header_uses_title(db):
    tid = _add(db, source_name="call_2026.mp3", title="Нарада щодо Гуцульщини",
               text="повний текст")
    md = m.transcript_resource(str(tid))
    assert md.startswith("# Нарада щодо Гуцульщини")


def test_transcript_resource_header_falls_back_to_source_name(db):
    tid = _add(db, source_name="Зустріч з Андрієм.mp3", text="повний текст")
    md = m.transcript_resource(str(tid))
    assert md.startswith("# Зустріч з Андрієм.mp3")


# ============================================================
# Проксі: search_archive/ask_archive не губить display_name
# ============================================================

def test_search_archive_passes_through_display_name(monkeypatch):
    def fake_api(method, path, **kwargs):
        assert path == "/api/memory/search"
        return {
            "query": "бюджет",
            "vector_available": True,
            "chunks": [{"transcription_id": 1, "source_name": "call_2026.mp3",
                        "display_name": "Нарада щодо Гуцульщини", "text": "..."}],
        }

    monkeypatch.setattr(m, "_api", fake_api)
    res = m.search_archive("бюджет")
    assert res["chunks"][0]["display_name"] == "Нарада щодо Гуцульщини"
    assert res["chunks"][0]["source_name"] == "call_2026.mp3"


def test_ask_archive_passes_through_display_name_in_sources(monkeypatch):
    def fake_api(method, path, **kwargs):
        assert path == "/api/memory/ask"
        return {
            "answer": "...",
            "sources": [{"transcription_id": 1, "source_name": "call_2026.mp3",
                        "display_name": "Нарада щодо Гуцульщини"}],
        }

    monkeypatch.setattr(m, "_api", fake_api)
    res = m.ask_archive("що вирішили по бюджету?")
    assert res["sources"][0]["display_name"] == "Нарада щодо Гуцульщини"


# ============================================================
# about() — кількість тулзів не змінилась
# ============================================================

def test_about_bucket_sum_still_matches_registered_count():
    registered = m._registered_tool_names()
    bucketed = list(m._ABOUT_DIRECT_DB) + list(m._ABOUT_PROXY)
    assert len(bucketed) == len(registered)


# ============================================================
# stdio-безпека: без torch на рівні модуля
# ============================================================

def test_import_mcp_server_does_not_import_torch():
    """Гейт-9 (знахідка 1): якщо перевіряти `sys.modules` у ТОМУ Ж процесі
    pytest, результат залежить від порядку збирання файлів — `test_endpoints_smoke.py`
    (раніше за абеткою) імпортує `app.py` -> `whisper_manager_new` -> torch,
    і torch лишається в `sys.modules` для всіх наступних тестів. Свіжий
    підпроцес (`sys.executable`) не успадковує це — саме ізоляцію обіцяла
    ACC історії 04 (`python -c "import mcp_server"`)."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, mcp_server; sys.exit(1 if 'torch' in sys.modules else 0)"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"mcp_server затягнув torch у sys.modules підпроцесу "
        f"(stdout={proc.stdout!r} stderr={proc.stderr!r})"
    )
