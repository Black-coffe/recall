"""Шар коментарів, Волна 5: MCP, експорт, звід.

Спільна вимога всіх трьох поверхонь: коментар мусить приїхати з ПОЗНАЧКОЮ, що
це не репліка з розмови. Без неї вигрузка/відповідь змішує два шари, і той, що
важить більше, губиться саме там, де він потрібен.
"""
import json
import sqlite3

import pytest

from app.blueprints import transcription as tx_bp
from app.db.migrations import init_database
from app.services import comments
from app.services.copilot import export as cp_export


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _add_tx(path, name="Дзвінок"):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text) "
        "VALUES ('file', ?, 'текст')", (name,))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


ROWS = [
    {"kind": "correction", "body": "насправді сума 12 тисяч", "pinned": 1,
     "anchor_time": 65.0, "created_at": "2026-08-17 10:00:00"},
    {"kind": "note", "body": "передзвонити після свят", "pinned": 0,
     "anchor_time": None, "created_at": "2026-08-17 11:00:00"},
]


# ------------------------------------------------------ рендер для експорту

def test_text_block_marks_provenance_and_leads(db):
    out = tx_bp._comments_text(ROWS)
    assert out.startswith("КОМЕНТАРІ ВЛАСНИКА")
    assert "[ВИПРАВЛЕННЯ] [закріплено] ~01:05" in out
    assert "правильні саме вони" in out          # правило пріоритету в тексті
    assert "насправді сума 12 тисяч" in out


def test_md_block_marks_provenance(db):
    out = tx_bp._comments_md(ROWS)
    assert out.startswith("## Коментарі власника")
    assert "**ВИПРАВЛЕННЯ** · закріплено · `01:05`" in out
    assert "передзвонити після свят" in out


def test_blocks_are_empty_without_comments(db):
    """Регресія: без коментарів вигрузка мусить лишитись такою, як була."""
    assert tx_bp._comments_text([]) == ""
    assert tx_bp._comments_md([]) == ""


def test_timecode_formatting():
    assert tx_bp._cm_ts(None) == ""
    assert tx_bp._cm_ts(0) == "00:00"
    assert tx_bp._cm_ts(65.4) == "01:05"
    assert tx_bp._cm_ts(3600) == "60:00"


# --------------------------------------------------------- копілот-експорт

_TIMELINE = {
    "session": {"id": 1, "transcription_id": None, "recording_session_id": "rec_x"},
    "events": [], "topics": [], "aggregates": {},
}
_CMS = [
    {"kind": "correction", "body": "клієнт передумав", "pinned": False,
     "anchor_time": 65.0, "created_at": "2026-08-17"},
]


def test_copilot_md_places_comment_on_its_timecode():
    tr = {"source_name": "Дзвінок",
          "segments": [{"start": 10.0, "speaker": "self", "text": "початок"},
                       {"start": 120.0, "speaker": "self", "text": "кінець"}]}
    out = cp_export.build_markdown(_TIMELINE, tr, _CMS)
    body = out.split("## Діалог")[1]
    # Коментар на 01:05 мусить стояти МІЖ репліками 00:10 і 02:00 — у вигрузці
    # видно, що саме говорили, коли оператор вписав своє уточнення.
    assert body.index("початок") < body.index("клієнт передумав") < body.index("кінець")
    assert "✍ **ВИПРАВЛЕННЯ** (оператор)" in out


def test_copilot_md_has_own_section_for_comments():
    out = cp_export.build_markdown(_TIMELINE, None, _CMS)
    assert "## Коментарі оператора" in out
    # Людське — перед машинним: зведення підказок іде після.
    if "## Зведення підказок" in out:
        assert out.index("## Коментарі оператора") < out.index("## Зведення підказок")


def test_copilot_md_unchanged_without_comments():
    a = cp_export.build_markdown(_TIMELINE, None, [])
    b = cp_export.build_markdown(_TIMELINE, None)
    assert a == b
    assert "Коментарі оператора" not in a


def test_copilot_json_keeps_comments_separate_from_notes():
    """Підказки згенерувала модель, коментарі написала людина. Змішавши їх,
    споживач втратив би саме ту різницю, заради якої коментарі й важать більше."""
    d = cp_export.build_json(_TIMELINE, None, _CMS)
    assert d["notes"] == []
    assert d["operator_comments"][0]["text"] == "клієнт передумав"
    assert d["operator_comments"][0]["kind"] == "correction"


def test_copilot_digest_leads_with_operator_comments():
    out = cp_export.notes_digest(_TIMELINE, {"source_name": "Дзвінок"}, _CMS)
    assert "Коментарі оператора під час дзвінка:" in out
    # Таймкод у форматі самого копілота (`_ts`), а не власному — коментарі
    # стоять в одній стрічці з нотатками й мусять читатись однаково.
    assert f"[{cp_export._ts(65.0)}] (виправлення) клієнт передумав" in out


def test_copilot_digest_unchanged_without_comments():
    assert cp_export.notes_digest(_TIMELINE, None, []) == \
        cp_export.notes_digest(_TIMELINE, None)


# --------------------------------------------------------------- звід (MCP)

def test_digest_surfaces_corrections(db):
    from app.services import commitments
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "насправді сума 12k", kind="correction")
    comments.create(db, "transcription", tid, "звичайна замітка", kind="note")
    d = commitments.weekly_digest(db)
    assert [c["body"] for c in d["corrections"]] == ["насправді сума 12k"]
    assert d["corrections"][0]["target_name"] == "Дзвінок"


def test_digest_corrections_empty_without_them(db):
    from app.services import commitments
    assert commitments.weekly_digest(db)["corrections"] == []


def test_digest_survives_db_without_comments_table(db):
    """Звід не має падати через надбудову, якої на старій БД ще немає."""
    from app.services import commitments
    conn = sqlite3.connect(db)
    conn.executescript("DROP TABLE comment_chunks_fts; DROP TABLE comment_chunks; "
                       "DROP TABLE comments;")
    conn.commit(); conn.close()
    assert commitments.weekly_digest(db)["corrections"] == []


# ------------------------------------------------------------- MCP-читання

def _mcp(db_path, monkeypatch):
    import mcp_server
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path)
    return mcp_server


def test_mcp_list_comments_filters(db, monkeypatch):
    m = _mcp(db, monkeypatch)
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "виправлення", kind="correction")
    comments.create(db, "transcription", tid, "замітка", kind="note")
    comments.create(db, "recording_session", "rec_x", "жива", source="live")

    assert len(m.list_comments()) == 3
    assert [c["body"] for c in m.list_comments(kind="correction")] == ["виправлення"]
    assert len(m.list_comments(target_type="transcription", target_id=str(tid))) == 2
    # Рядковий ключ мусить працювати так само, як числовий.
    assert [c["body"] for c in m.list_comments(target_type="recording_session",
                                               target_id="rec_x")] == ["жива"]


def test_mcp_list_comments_carries_target_name(db, monkeypatch):
    m = _mcp(db, monkeypatch)
    tid = _add_tx(db, "Розмова з підрядником")
    comments.create(db, "transcription", tid, "уточнення")
    assert m.list_comments()[0]["target_name"] == "Розмова з підрядником"


def test_mcp_get_transcript_includes_comments(db, monkeypatch):
    m = _mcp(db, monkeypatch)
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "насправді 12k", kind="correction")
    d = m.get_transcript(tid)
    assert [c["body"] for c in d["comments"]] == ["насправді 12k"]


def test_mcp_get_transcript_omits_empty_comments(db, monkeypatch):
    m = _mcp(db, monkeypatch)
    tid = _add_tx(db)
    assert "comments" not in m.get_transcript(tid)


def test_mcp_survives_db_without_comments_table(db, monkeypatch):
    m = _mcp(db, monkeypatch)
    tid = _add_tx(db)
    conn = sqlite3.connect(db)
    conn.executescript("DROP TABLE comment_chunks_fts; DROP TABLE comment_chunks; "
                       "DROP TABLE comments;")
    conn.commit(); conn.close()
    assert m.list_comments() == []
    assert m.get_transcript(tid)["id"] == tid       # читання транскрипту ціле


def test_mcp_tools_stay_read_only(db, monkeypatch):
    """Рішення «MCP → read-first» лишається чинним: write-тулзи коментарів
    свідомо НЕ додаються — коментар пишеться з інтерфейсу Recall."""
    m = _mcp(db, monkeypatch)
    for name in ("add_comment", "create_comment", "delete_comment", "update_comment"):
        assert not hasattr(m, name), f"зʼявилась write-тулза {name}"
