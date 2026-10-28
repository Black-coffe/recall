"""The three task writers insert proposals and respect decided rows (ADR-056).

Every writer (meeting card in `enrichment`, chat threads in `tg_tasks`, comment analysis in
`comments`) is run with its extractor faked. Checked per writer: rows are `proposed` with
`text_key`/`for_whom`/`confidence`/`importance`; a re-run deletes only the writer's own
`proposed` rows; a decided row (any non-proposed status) keeps its id and blocks the same
text from being proposed again; third-party and low-confidence candidates are not stored.

Matching is EXACT (normalised text hash within one record): a rephrased candidate is a new
key and is proposed again. That limit is deliberate and is what `evals/task_rerun_measure.py`
measures.
"""
import logging
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import comments, enrichment, task_proposals, text_polishing, tg_tasks

DECIDED = ["open", "done", "cancelled", "rejected", "expired", "legacy", "stale"]

MINE = {"task": "Надіслати кошторис", "owner": "Андрій", "for_whom": "owner",
        "confidence": 0.9, "importance": 3}
PROMISED = {"task": "Отримати договір", "owner": "Олена", "for_whom": "promised_to_owner",
            "confidence": 0.8, "importance": 1}
BARE = {"task": "Подзвонити бухгалтеру", "owner": None}
THIRD = {"task": "Олена купить квитки", "owner": "Олена", "for_whom": "third_party",
         "confidence": 0.95, "importance": 2}
UNSURE = {"task": "Може колись глянути звіт", "owner": None, "for_whom": "owner",
          "confidence": 0.3, "importance": 2}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_PROPOSAL_MIN_CONFIDENCE", raising=False)
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _rows(path, where="1=1", args=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM action_items WHERE {where} ORDER BY id", args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _exec(path, sql, args=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(sql, args)
    conn.commit()
    last = cur.lastrowid
    conn.close()
    return last


def _add_tx(path, **cols):
    cols = {"source_type": "file", "source_name": "rec.mp3",
            "transcript_text": "Розмова про бюджет.", "created_at": "2026-05-12 10:00:00", **cols}
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    return _exec(path, f"INSERT INTO transcriptions ({names}) VALUES ({marks})",
                 tuple(cols.values()))


# ============================================================
# One adapter per writer: run(candidates) -> result dict; scope helpers
# ============================================================

class EnrichmentWriter:
    name = "enrichment"

    def __init__(self, path, monkeypatch):
        self.path = path
        self.monkeypatch = monkeypatch
        self.seen = {}
        self.tid = _add_tx(path)
        monkeypatch.setattr(text_polishing, "is_available", lambda: True)

    def run(self, candidates):
        card = {"summary": "s", "key_points": [], "action_items": [dict(c) for c in candidates],
                "topics": [], "people": [], "projects": [], "orgs": [], "model": "claude-test",
                "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}
        self.monkeypatch.setattr(text_polishing, "extract_meeting_card",
                                 lambda *a, **k: (self.seen.update(k), card)[1])
        res = enrichment._enrich_card(self.path, self.tid, force=True)
        assert res["status"] == "enriched"
        return res["proposals"]


class TgWriter:
    name = "tg_tasks"

    def __init__(self, path, monkeypatch):
        self.path = path
        self.monkeypatch = monkeypatch
        self.seen = {}
        _exec(path, "INSERT INTO tg_threads (id, chat_id, label, status, msg_count, last_date) "
                    "VALUES (1, -100, 'Договір', 'open', 1, '2026-06-10T10:00:00+00:00')")
        self.tid = _add_tx(path, source_type="telegram", tg_chat_id=-100, tg_message_id=1,
                           tg_date="2026-06-01T10:00:00+00:00", tg_sender="Юля",
                           tg_thread_id=1, transcript_text="зроблю")
        monkeypatch.setattr(text_polishing, "is_available", lambda: True)

    def run(self, candidates):
        tasks = [{**c, "msg": 1} for c in candidates]
        self.monkeypatch.setattr(text_polishing, "extract_chat_tasks", lambda *a, **k: (
            self.seen.update(k), {"tasks": tasks, "model": "claude-test", "input_tokens": 1,
                                  "output_tokens": 1})[1])
        res = tg_tasks.extract_thread(self.path, 1)
        assert res["status"] == "ok"
        return {"proposed": res["tasks"], "skipped_decided": res["skipped_decided"],
                "dropped_third_party": res["dropped_third_party"],
                "dropped_low_confidence": res["dropped_low_confidence"]}


class CommentWriter:
    name = "comments"

    def __init__(self, path, monkeypatch):
        self.path = path
        self.monkeypatch = monkeypatch
        self.seen = {}
        self.tid = _add_tx(path)
        self.comment_id = comments.create(path, "transcription", self.tid, "нотатка")["id"]
        monkeypatch.setattr(enrichment, "is_available", lambda: True)

    def run(self, candidates):
        parsed = {"action_items": [dict(c) for c in candidates], "people": [], "projects": [],
                  "orgs": [], "model": "claude-test", "input_tokens": 1, "output_tokens": 1}
        self.monkeypatch.setattr(text_polishing, "extract_comment_items",
                                 lambda *a, **k: (self.seen.update(k), parsed)[1])
        res = comments.analyze(self.path, self.comment_id, force=True)
        assert res["status"] == "analyzed"
        return {"proposed": res["counts"]["action_items"], **res["proposals"]}


WRITERS = [EnrichmentWriter, TgWriter, CommentWriter]


@pytest.fixture(params=WRITERS, ids=[w.name for w in WRITERS])
def writer(request, db, monkeypatch):
    return request.param(db, monkeypatch)


def test_writer_passes_owner_names_to_the_extractor(writer, monkeypatch):
    # The production call must hand the owner's names to the prompt, otherwise the model
    # judges `for_whom` on first person alone ("Андрій, надішли договір" -> third_party).
    asked = []
    monkeypatch.setattr(task_proposals, "owner_names",
                        lambda path: (asked.append(path), ["Андрій", "@andrii"])[1])
    writer.run([MINE])
    assert asked == [writer.path]
    assert writer.seen["owner_names"] == ["Андрій", "@andrii"]


# ============================================================
# Insertion
# ============================================================

def test_first_run_inserts_proposals_with_fields(writer):
    res = writer.run([MINE, PROMISED])
    rows = _rows(writer.path)
    assert [r["status"] for r in rows] == ["proposed", "proposed"]
    assert [(r["task"], r["for_whom"], r["confidence"], r["importance"]) for r in rows] == [
        ("Надіслати кошторис", "owner", 0.9, 3), ("Отримати договір", "promised_to_owner", 0.8, 1)]
    assert [r["text_key"] for r in rows] == [task_proposals.text_key(MINE["task"]),
                                            task_proposals.text_key(PROMISED["task"])]
    assert res["proposed"] == 2 and res["skipped_decided"] == 0


def test_candidate_without_fields_is_stored_with_defaults(writer):
    writer.run([BARE])
    (row,) = _rows(writer.path)
    assert (row["status"], row["for_whom"], row["confidence"], row["importance"]) == (
        "proposed", "unknown", None, 2)


def test_third_party_and_low_confidence_are_not_stored_and_counted(writer, caplog):
    with caplog.at_level(logging.INFO):
        res = writer.run([MINE, THIRD, UNSURE])
    assert [r["task"] for r in _rows(writer.path)] == [MINE["task"]]
    assert (res["proposed"], res["dropped_third_party"], res["dropped_low_confidence"]) == (1, 1, 1)
    assert "dropped_third_party=1" in caplog.text
    assert "dropped_low_confidence=1" in caplog.text


def test_min_confidence_setting_is_honoured(writer, monkeypatch):
    monkeypatch.setenv("TASK_PROPOSAL_MIN_CONFIDENCE", "0.2")
    res = writer.run([UNSURE])
    assert res["dropped_low_confidence"] == 0
    assert [r["task"] for r in _rows(writer.path)] == [UNSURE["task"]]


def test_same_text_twice_in_one_run_is_proposed_once(writer):
    writer.run([MINE, {**MINE, "task": "  надіслати КОШТОРИС. "}])
    assert len(_rows(writer.path)) == 1


# ============================================================
# Re-runs and tombstones
# ============================================================

def test_rerun_replaces_proposals_without_duplicates(writer):
    writer.run([MINE, PROMISED])
    res = writer.run([MINE, PROMISED])
    rows = _rows(writer.path)
    assert sorted(r["task"] for r in rows) == sorted([MINE["task"], PROMISED["task"]])
    assert {r["status"] for r in rows} == {"proposed"}
    assert res["proposed"] == 2 and res["skipped_decided"] == 0


def test_rerun_drops_a_proposal_the_model_no_longer_finds(writer):
    writer.run([MINE, PROMISED])
    writer.run([MINE])
    assert [r["task"] for r in _rows(writer.path)] == [MINE["task"]]


@pytest.mark.parametrize("status", DECIDED)
def test_decided_row_survives_rerun_and_blocks_the_candidate(writer, status):
    writer.run([MINE, PROMISED])
    decided_id = _rows(writer.path, "task = ?", (MINE["task"],))[0]["id"]
    _exec(writer.path, "UPDATE action_items SET status = ? WHERE id = ?", (status, decided_id))

    res = writer.run([MINE, PROMISED])

    rows = _rows(writer.path)
    assert [(r["id"], r["status"]) for r in rows if r["task"] == MINE["task"]] == [
        (decided_id, status)]
    assert [r["status"] for r in rows if r["task"] == PROMISED["task"]] == ["proposed"]
    assert res["skipped_decided"] == 1 and res["proposed"] == 1


def test_decided_rows_keep_their_ids_over_many_reruns(writer):
    writer.run([MINE, PROMISED])
    _exec(writer.path, "UPDATE action_items SET status = 'open'")
    before = [(r["id"], r["task"], r["status"]) for r in _rows(writer.path)]
    writer.run([MINE, PROMISED])
    writer.run([MINE, PROMISED])
    assert [(r["id"], r["task"], r["status"]) for r in _rows(writer.path)] == before


def test_legacy_row_without_stored_key_blocks_the_candidate(writer):
    """A pre-v59 row has no text_key; its key is computed from the task text."""
    _exec(writer.path, "INSERT INTO action_items (transcription_id, task, status) "
                       "VALUES (?, ?, 'legacy')", (writer.tid, "Надіслати кошторис!"))
    res = writer.run([MINE])
    assert res["skipped_decided"] == 1
    assert [r["status"] for r in _rows(writer.path)] == ["legacy"]


def test_rephrased_candidate_is_proposed_again(writer):
    """The exact-key limit: a rephrasing is a different key. Covered by the measurement."""
    writer.run([MINE])
    _exec(writer.path, "UPDATE action_items SET status = 'rejected'")
    res = writer.run([{**MINE, "task": "Відправити кошторис"}])
    assert res["proposed"] == 1 and res["skipped_decided"] == 0
    assert sorted(r["status"] for r in _rows(writer.path)) == ["proposed", "rejected"]


# ============================================================
# Delete scope per writer
# ============================================================

def test_enrichment_rerun_deletes_only_this_records_proposals(db, monkeypatch):
    w = EnrichmentWriter(db, monkeypatch)
    other = _add_tx(db)
    other_id = _exec(db, "INSERT INTO action_items (transcription_id, task, status) "
                         "VALUES (?, 'Чужа пропозиція', 'proposed')", (other,))
    w.run([MINE])
    w.run([PROMISED])
    assert [r["task"] for r in _rows(db, "transcription_id = ?", (w.tid,))] == [PROMISED["task"]]
    assert [r["id"] for r in _rows(db, "transcription_id = ?", (other,))] == [other_id]


def test_tg_rerun_deletes_only_its_threads_tg_proposals(db, monkeypatch):
    w = TgWriter(db, monkeypatch)
    card_id = _exec(db, "INSERT INTO action_items (transcription_id, task, status) "
                        "VALUES (?, 'Пропозиція з картки', 'proposed')", (w.tid,))
    _exec(db, "INSERT INTO tg_threads (id, chat_id, label, status, msg_count) "
              "VALUES (2, -100, 'Інша нитка', 'open', 1)")
    other_tx = _add_tx(db, source_type="telegram", tg_chat_id=-100, tg_message_id=2,
                       tg_thread_id=2, transcript_text="інше")
    other_id = _exec(db, "INSERT INTO action_items (transcription_id, task, status, source) "
                         "VALUES (?, 'Задача іншої нитки', 'proposed', ?)",
                     (other_tx, tg_tasks.SOURCE))
    w.run([MINE])
    w.run([PROMISED])
    assert sorted(r["task"] for r in _rows(db, "transcription_id = ?", (w.tid,))) == sorted(
        [PROMISED["task"], "Пропозиція з картки"])
    assert card_id in [r["id"] for r in _rows(db)]
    assert other_id in [r["id"] for r in _rows(db)]


def test_comment_rerun_deletes_only_its_comments_proposals(db, monkeypatch):
    w = CommentWriter(db, monkeypatch)
    other_comment = comments.create(db, "transcription", w.tid, "інша нотатка")["id"]
    other_id = _exec(db, "INSERT INTO action_items (transcription_id, task, status, source, "
                         "comment_id) VALUES (?, 'Задача іншої нотатки', 'proposed', ?, ?)",
                     (w.tid, comments.ACTION_SOURCE, other_comment))
    card_id = _exec(db, "INSERT INTO action_items (transcription_id, task, status) "
                        "VALUES (?, 'Пропозиція з картки', 'proposed')", (w.tid,))
    w.run([MINE])
    w.run([PROMISED])
    assert [r["task"] for r in _rows(db, "comment_id = ?", (w.comment_id,))] == [PROMISED["task"]]
    ids = [r["id"] for r in _rows(db)]
    assert other_id in ids and card_id in ids


def test_writers_never_journal_events(writer):
    writer.run([MINE, PROMISED])
    writer.run([MINE])
    conn = sqlite3.connect(writer.path)
    n = conn.execute("SELECT COUNT(*) FROM action_item_events").fetchone()[0]
    first_shown = conn.execute(
        "SELECT COUNT(*) FROM action_items WHERE first_shown_at IS NOT NULL").fetchone()[0]
    conn.close()
    assert (n, first_shown) == (0, 0)
