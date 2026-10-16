"""document_ingest.ingest_file: filter-before-hash, dedup, flags (no network, fakes only)."""
import inspect
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import document_ingest as di


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    init_database(path)
    calls = {"enrich": 0, "describe": 0}
    monkeypatch.setattr(di, "submit_enrichment", lambda *a, **k: calls.__setitem__("enrich", calls["enrich"] + 1))
    monkeypatch.setattr(di, "describe_tabular", lambda *a, **k: calls.__setitem__("describe", calls["describe"] + 1) or 0)
    return path, calls


def _file(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_no_flask_import():
    assert "flask" not in inspect.getsource(di)


def test_created_and_duplicate(db, tmp_path):
    path, _ = db
    f = _file(tmp_path, "a.txt", "hello world")
    r1 = di.ingest_file(path, f, original_name="a.txt")
    assert r1["status"] == "created" and r1["transcription_id"]
    r2 = di.ingest_file(path, _file(tmp_path, "b.txt", "hello world"), original_name="b.txt")
    assert r2 == {"status": "duplicate", "transcription_id": r1["transcription_id"]}


def test_text_filter_applies_to_stored_text_and_hash(db, tmp_path):
    path, _ = db
    f = _file(tmp_path, "a.txt", "зустріч з Андрієм завтра")
    flt = lambda t: t.replace("Андрієм", "[NAME]")
    r = di.ingest_file(path, f, original_name="a.txt", text_filter=flt)
    assert r["status"] == "created"
    with sqlite3.connect(path) as c:
        text, h = c.execute("SELECT transcript_text, content_hash FROM transcriptions WHERE id=?",
                            (r["transcription_id"],)).fetchone()
    assert "Андрієм" not in text and "[NAME]" in text
    assert h == di.document_parser.content_hash(text)
    # the same file with the masked token typed literally is a duplicate
    f2 = _file(tmp_path, "b.txt", "зустріч з [NAME] завтра")
    assert di.ingest_file(path, f2, original_name="b.txt")["status"] == "duplicate"


def test_flags_off_skip_enrichment_and_description(db, tmp_path):
    path, calls = db
    di.ingest_file(path, _file(tmp_path, "a.txt", "one"), original_name="a.txt",
                   enrich=False, describe_tables=False)
    assert calls == {"enrich": 0, "describe": 0}
    di.ingest_file(path, _file(tmp_path, "b.txt", "two"), original_name="b.txt")
    assert calls == {"enrich": 1, "describe": 1}


def test_unreadable_and_empty_do_not_raise(db, tmp_path):
    path, _ = db
    assert di.ingest_file(path, str(tmp_path / "missing.txt"), original_name="missing.txt")["status"] == "error"
    assert di.ingest_file(path, _file(tmp_path, "e.txt", "  "), original_name="e.txt")["status"] in ("error", "no_text")
    assert di.ingest_file(path, _file(tmp_path, "x.zzz", "a"), original_name="x.zzz")["status"] == "error"
