"""Comments layer, Wave 4: live call.

Three things that break separately here:
  1. the string target key — a recording session is `rec_<hex>`, not a number;
  2. moving to the transcript after transcription (otherwise everything dictated
     during the call stays on an id not visible from any screen);
  3. feeding comments to the copilot as the operator's priority signal.
"""
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import comments


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


SID = "rec_a1b2c3d4e5f60718"


def _add_tx(path, name="Дзвінок", file_path=None):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "file_path) VALUES ('recording', ?, 'текст', ?)", (name, file_path))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


# ------------------------------------------------- v39: string target key

def test_v39_adds_target_key(db):
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(comments)")}
    ver = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
    conn.close()
    assert "target_key" in cols and ver >= 39


def test_v39_idempotent(db):
    init_database(db)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM schema_versions WHERE version = 39").fetchone()[0]
    conn.close()
    assert n == 1


def test_session_comment_uses_string_key(db):
    c = comments.create(db, "recording_session", SID, "клієнт передумав",
                        source="live", anchor_time=137.0)
    assert c["target_key"] == SID
    assert c["target_id"] == comments.STR_TARGET_ID
    assert c["target_ref"] == SID


def test_session_comments_are_listed_by_string_key(db):
    comments.create(db, "recording_session", SID, "перше", source="live", anchor_time=10)
    comments.create(db, "recording_session", "rec_other", "чуже", source="live")
    rows = comments.list_for(db, "recording_session", SID)
    assert [r["body"] for r in rows] == ["перше"]


def test_session_comments_sort_by_moment_not_typing_time(db):
    """During a call comments are read in the order of the conversation. The second was entered
    later, but it concerns an earlier moment — and must stand first."""
    comments.create(db, "recording_session", SID, "пізній момент",
                    source="live", anchor_time=600)
    comments.create(db, "recording_session", SID, "ранній момент",
                    source="live", anchor_time=60)
    rows = comments.list_for(db, "recording_session", SID)
    assert [r["body"] for r in rows] == ["ранній момент", "пізній момент"]


def test_counts_work_for_string_keys(db):
    comments.create(db, "recording_session", SID, "a", source="live")
    comments.create(db, "recording_session", SID, "b", source="live")
    comments.create(db, "recording_session", "rec_zzz", "c", source="live")
    assert comments.counts_for(db, "recording_session", [SID, "rec_zzz", "rec_nope"]) \
        == {SID: {"n": 2, "corrections": 0}, "rec_zzz": {"n": 1, "corrections": 0}}


def test_numeric_target_rejects_garbage_key(db):
    tid = _add_tx(db)
    comments.create(db, "transcription", tid, "ок")     # a number — accepted
    with pytest.raises(comments.CommentError):
        comments.create(db, "transcription", "rec_abc", "не число")


def test_empty_session_key_rejected(db):
    with pytest.raises(comments.CommentError):
        comments.create(db, "recording_session", "   ", "текст", source="live")


def test_string_and_numeric_targets_do_not_collide(db):
    """The target_id of string targets holds 0 — it must not be caught as card #0
    and must not mix with numeric targets."""
    tid = _add_tx(db)
    comments.create(db, "recording_session", SID, "сесія", source="live")
    comments.create(db, "transcription", tid, "транскрипт")
    assert len(comments.list_for(db, "transcription", tid)) == 1
    assert len(comments.list_for(db, "recording_session", SID)) == 1


# ----------------------------------------------------- move to the transcript

def test_retarget_moves_session_comments_with_anchor(db):
    tid = _add_tx(db)
    comments.create(db, "recording_session", SID, "тут він передумав",
                    source="live", anchor_time=137.0)
    comments.create(db, "recording_session", SID, "друге", source="live", anchor_time=200.0)
    moved = comments.retarget(db, "recording_session", SID, "transcription", tid)
    assert moved == 2
    rows = comments.list_for(db, "transcription", tid)
    assert [r["anchor_time"] for r in rows] == [137.0, 200.0]
    assert all(r["target_key"] is None for r in rows)
    assert comments.list_for(db, "recording_session", SID) == []


def test_retarget_touches_only_its_own_session(db):
    tid = _add_tx(db)
    comments.create(db, "recording_session", SID, "свій", source="live")
    comments.create(db, "recording_session", "rec_other", "чужий", source="live")
    assert comments.retarget(db, "recording_session", SID, "transcription", tid) == 1
    assert len(comments.list_for(db, "recording_session", "rec_other")) == 1


def test_retarget_is_safe_when_nothing_to_move(db):
    tid = _add_tx(db)
    assert comments.retarget(db, "recording_session", "rec_empty",
                             "transcription", tid) == 0


def test_retarget_rebuilds_graph_on_new_target(db):
    """After the move, mentions from the comments must appear in the new target's graph —
    while the comment hung on the session there was no anchor and a link was impossible."""
    tid = _add_tx(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO entities (type, canonical_name, normalized_name) "
                 "VALUES ('project', 'Acmecorp', 'acmecorp')")
    eid = conn.execute("SELECT id FROM entities").fetchone()[0]
    conn.commit(); conn.close()

    comments.create(db, "recording_session", SID, "по Acmecorp домовились",
                    source="live")
    comments.retarget(db, "recording_session", SID, "transcription", tid)
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT entity_id, source FROM meeting_entities "
                       "WHERE transcription_id = ?", (tid,)).fetchone()
    conn.close()
    assert row == (eid, comments.ENTITY_SOURCE)


def test_session_resolves_to_transcript_through_media_card(db):
    """While there is no transcript there is no anchor (a normal state during
    a call). As soon as it appears, the chain session → card → transcript closes."""
    assert comments.resolve_transcription(db, "recording_session", SID) is None
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, "
                 "file_path, source_type, recording_session_id) "
                 "VALUES ('', ?, 'Запис', 'X:/r/final.mp3', 'recording', ?)", (SID, SID))
    conn.commit(); conn.close()
    assert comments.resolve_transcription(db, "recording_session", SID) is None
    tid = _add_tx(db, file_path="X:/r/final.mp3")
    assert comments.resolve_transcription(db, "recording_session", SID) == tid


# ------------------------------------------------ feeding the copilot (worker)

class _WS:
    def __init__(self, sid):
        self.recording_session_id = sid


def _worker(db_path):
    from app.services.copilot.worker import CopilotWorker
    w = CopilotWorker.__new__(CopilotWorker)      # without starting threads/LLM
    w._db_path = db_path
    return w


def test_operator_comments_enter_copilot_window(db):
    comments.create(db, "recording_session", SID, "клієнт передумав щодо ціни",
                    source="live", anchor_time=65.0)
    got = _worker(db)._operator_comments(_WS(SID))
    assert len(got) == 1
    assert got[0]["source_type"] == "comment"
    assert got[0]["text"] == "клієнт передумав щодо ціни"
    assert got[0]["anchor_label"] == "01:05"


def test_operator_comment_ids_cannot_clash_with_archive_chunks(db):
    """The copilot window already holds archive chunks (positive ids) and comment chunks
    from retrieval (negative, from another table). The third space must be its own,
    otherwise the insight's evidence_chunk_ids point to who knows what."""
    from app.services.copilot.worker import CopilotWorker
    c = comments.create(db, "recording_session", SID, "текст", source="live")
    got = _worker(db)._operator_comments(_WS(SID))
    assert got[0]["chunk_id"] == CopilotWorker._OPERATOR_CHUNK_BASE - c["id"]
    assert got[0]["chunk_id"] < -1_000_000
    assert got[0]["chunk_id"] != -c["id"]        # not the retrieval space


def test_operator_comments_are_capped_to_the_latest(db):
    from app.services.copilot.worker import CopilotWorker
    for i in range(8):
        comments.create(db, "recording_session", SID, f"коментар {i}",
                        source="live", anchor_time=float(i))
    got = _worker(db)._operator_comments(_WS(SID))
    assert len(got) == CopilotWorker._OPERATOR_COMMENTS
    assert got[-1]["text"] == "коментар 7"       # exactly the LATEST ones


def test_operator_comments_degrade_quietly(db):
    """The copilot must not fail either without a DB or on an old schema without the table."""
    w = _worker(db)
    assert w._operator_comments(_WS(None)) == []
    w._db_path = None
    assert w._operator_comments(_WS(SID)) == []

    conn = sqlite3.connect(db)
    conn.executescript("DROP TABLE comment_chunks_fts; DROP TABLE comment_chunks; "
                       "DROP TABLE comments;")
    conn.commit(); conn.close()
    assert _worker(db)._operator_comments(_WS(SID)) == []


def test_dispatcher_labels_operator_comment_distinctly(db):
    """Without an explicit mark the 7B would read the operator's line as one more old
    archive fragment — i.e. the most precise signal in the window would lose weight."""
    from app.services.copilot.dispatcher import _fmt_chunks
    txt = _fmt_chunks([
        {"chunk_id": -1000000005, "source_type": "comment", "live_operator": True,
         "text": "клієнт передумав", "anchor_label": "01:05"},
        {"chunk_id": 7, "source_name": "Стара зустріч",
         "meeting_date": "2026-01-01", "text": "щось із архіву"},
    ])
    assert "КОМЕНТАР ОПЕРАТОРА, 01:05" in txt
    assert "(Стара зустріч, 2026-01-01)" in txt


def test_dispatcher_format_unchanged_without_comments():
    """Regression: a window without comments must be formatted byte for byte as before."""
    from app.services.copilot.dispatcher import _fmt_chunks
    assert _fmt_chunks([{"chunk_id": 7, "source_name": "Зустріч",
                         "meeting_date": "2026-01-01", "text": "текст"}]) \
        == "[chunk_id=7] (Зустріч, 2026-01-01) текст"


# ------------------------------------------ regressions from the code review (merge)

def test_pinned_live_comments_are_not_the_first_dropped(db):
    """Found by the review: `list_for` returns pinned ones FIRST, and the "latest N" slice
    cut off exactly them — i.e. exactly what the operator deliberately raised
    fell out of the copilot window."""
    from app.services.copilot.worker import CopilotWorker
    comments.create(db, "recording_session", SID, "закріплене", source="live",
                    anchor_time=1.0, pinned=True)
    for i in range(6):
        comments.create(db, "recording_session", SID, f"звичайне {i}",
                        source="live", anchor_time=float(10 + i))
    got = _worker(db)._operator_comments(_WS(SID))
    texts = [c["text"] for c in got]
    assert len(got) == CopilotWorker._OPERATOR_COMMENTS
    assert "закріплене" in texts
    assert texts[-1] == "звичайне 5"        # and the newest ones are in place too


def test_live_comments_are_flagged_as_operator_input(db):
    """The flag tells a live line of this call from an archive comment
    that arrived from RAG — otherwise the dispatcher labels both the same."""
    comments.create(db, "recording_session", SID, "текст", source="live")
    assert _worker(db)._operator_comments(_WS(SID))[0]["live_operator"] is True


def test_dispatcher_does_not_call_archive_comment_an_operator_remark():
    """Found by the review: archive comments also have source_type='comment' (RAG
    returns them), and without telling them apart last year's clarification on ANOTHER project
    was labelled as "the operator just said", with no source or date."""
    from app.services.copilot.dispatcher import _fmt_chunks
    txt = _fmt_chunks([
        {"chunk_id": -5, "source_type": "comment", "target_label": "Стара зустріч",
         "meeting_date": "2026-01-01", "text": "торішнє уточнення"},
        {"chunk_id": -1000000007, "source_type": "comment", "live_operator": True,
         "anchor_label": "01:05", "text": "щойно вписано"},
    ])
    assert "КОМЕНТАР ВЛАСНИКА до «Стара зустріч», 2026-01-01" in txt
    assert "торішнє уточнення" in txt
    assert "(КОМЕНТАР ОПЕРАТОРА, 01:05) щойно вписано" in txt
    # An archive comment must NOT pass for an operator's line.
    assert "КОМЕНТАР ОПЕРАТОРА) торішнє" not in txt
