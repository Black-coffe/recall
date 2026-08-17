"""Шар коментарів, Волна 4: живий дзвінок.

Три речі, які тут ламаються окремо:
  1. рядковий ключ цілі — сесія запису це `rec_<hex>`, а не число;
  2. переїзд на транскрипт після транскрибування (інакше все, надиктоване по
     ходу дзвінка, лишається на id, якого не видно з жодного екрана);
  3. подача коментарів у копілот як пріоритетного сигналу оператора.
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


# ------------------------------------------------- v39: рядковий ключ цілі

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
    """Під час дзвінка коментарі читаються за ходом розмови. Другий вписали
    пізніше, але він стосується ранішого моменту — і має стояти першим."""
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
    comments.create(db, "transcription", tid, "ок")     # число — приймається
    with pytest.raises(comments.CommentError):
        comments.create(db, "transcription", "rec_abc", "не число")


def test_empty_session_key_rejected(db):
    with pytest.raises(comments.CommentError):
        comments.create(db, "recording_session", "   ", "текст", source="live")


def test_string_and_numeric_targets_do_not_collide(db):
    """target_id рядкових цілей тримає 0 — він не має ловитись як картка #0
    і не має змішуватись із числовими цілями."""
    tid = _add_tx(db)
    comments.create(db, "recording_session", SID, "сесія", source="live")
    comments.create(db, "transcription", tid, "транскрипт")
    assert len(comments.list_for(db, "transcription", tid)) == 1
    assert len(comments.list_for(db, "recording_session", SID)) == 1


# ----------------------------------------------------- переїзд на транскрипт

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
    """Після переїзду згадки з коментарів мусять зʼявитись у графі нової цілі —
    поки коментар висів на сесії, якоря не було й звʼязок був неможливий."""
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
    """Поки транскрипта немає — якоря немає (це нормальний стан під час
    дзвінка). Щойно він зʼявився — ланцюг сесія → картка → транскрипт замикається."""
    assert comments.resolve_transcription(db, "recording_session", SID) is None
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, "
                 "file_path, source_type, recording_session_id) "
                 "VALUES ('', ?, 'Запис', 'X:/r/final.mp3', 'recording', ?)", (SID, SID))
    conn.commit(); conn.close()
    assert comments.resolve_transcription(db, "recording_session", SID) is None
    tid = _add_tx(db, file_path="X:/r/final.mp3")
    assert comments.resolve_transcription(db, "recording_session", SID) == tid


# ------------------------------------------------ подача в копілот (worker)

class _WS:
    def __init__(self, sid):
        self.recording_session_id = sid


def _worker(db_path):
    from app.services.copilot.worker import CopilotWorker
    w = CopilotWorker.__new__(CopilotWorker)      # без запуску потоків/LLM
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
    """У вікні копілота вже живуть чанки архіву (додатні id) і чанки коментарів
    із retrieval (відʼємні, з іншої таблиці). Третій простір мусить бути своїм,
    інакше evidence_chunk_ids інсайту вказує невідомо на що."""
    from app.services.copilot.worker import CopilotWorker
    c = comments.create(db, "recording_session", SID, "текст", source="live")
    got = _worker(db)._operator_comments(_WS(SID))
    assert got[0]["chunk_id"] == CopilotWorker._OPERATOR_CHUNK_BASE - c["id"]
    assert got[0]["chunk_id"] < -1_000_000
    assert got[0]["chunk_id"] != -c["id"]        # не простір retrieval


def test_operator_comments_are_capped_to_the_latest(db):
    from app.services.copilot.worker import CopilotWorker
    for i in range(8):
        comments.create(db, "recording_session", SID, f"коментар {i}",
                        source="live", anchor_time=float(i))
    got = _worker(db)._operator_comments(_WS(SID))
    assert len(got) == CopilotWorker._OPERATOR_COMMENTS
    assert got[-1]["text"] == "коментар 7"       # саме ОСТАННІ


def test_operator_comments_degrade_quietly(db):
    """Копілот не має падати ні без БД, ні на старій схемі без таблиці."""
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
    """Без явної мітки 7B прочитала б репліку оператора як ще один старий
    фрагмент архіву — тобто найточніший сигнал у вікні втратив би вагу."""
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
    """Регресія: вікно без коментарів має форматуватись побайтово як раніше."""
    from app.services.copilot.dispatcher import _fmt_chunks
    assert _fmt_chunks([{"chunk_id": 7, "source_name": "Зустріч",
                         "meeting_date": "2026-01-01", "text": "текст"}]) \
        == "[chunk_id=7] (Зустріч, 2026-01-01) текст"


# ------------------------------------------ регресії за код-рев'ю (merge)

def test_pinned_live_comments_are_not_the_first_dropped(db):
    """Знайдено рев'ю: `list_for` віддає закріплені ПЕРШИМИ, а зріз «останні N»
    відрізав саме їх — тобто з вікна копілота випадало рівно те, що оператор
    свідомо підняв."""
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
    assert texts[-1] == "звичайне 5"        # і найсвіжіші теж на місці


def test_live_comments_are_flagged_as_operator_input(db):
    """Прапорець відрізняє живу репліку цього дзвінка від архівного коментаря,
    що приїхав із RAG — інакше диспетчер підписує обидва однаково."""
    comments.create(db, "recording_session", SID, "текст", source="live")
    assert _worker(db)._operator_comments(_WS(SID))[0]["live_operator"] is True


def test_dispatcher_does_not_call_archive_comment_an_operator_remark():
    """Знайдено рев'ю: архівні коментарі теж мають source_type='comment' (їх
    віддає RAG), і без розрізнення торішнє уточнення до ЧУЖОГО проєкту
    підписувалось як «оператор щойно сказав», ще й без джерела й дати."""
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
    # Архівний коментар НЕ має видаватись за репліку оператора.
    assert "КОМЕНТАР ОПЕРАТОРА) торішнє" not in txt
