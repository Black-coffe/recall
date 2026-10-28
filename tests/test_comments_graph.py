"""Comments layer, Wave 3: graph links and analysis via Claude.

The two layers are checked separately because they have different costs and different guarantees:
  A — local name matching, free, works right on ingest;
  B — Claude on demand, paid, never automatic.
"""
import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import comments, embeddings, tg_entities


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    return a / (np.linalg.norm(a) or 1.0)


@pytest.fixture(autouse=True)
def _clean_names_cache():
    tg_entities.reset_names_cache()
    yield
    tg_entities.reset_names_cache()


@pytest.fixture
def mock_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))


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


def _add_entity(path, etype, name):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO entities (type, canonical_name, normalized_name) VALUES (?, ?, ?)",
        (etype, name, name.casefold()))
    eid = cur.lastrowid
    conn.commit(); conn.close()
    return eid


_ANY = object()   # sentinel: None is a real filter "source IS NULL"


def _links(path, tid, source=_ANY):
    conn = sqlite3.connect(path)
    sql = "SELECT entity_id FROM meeting_entities WHERE transcription_id = ?"
    params = [tid]
    if source is not _ANY:
        sql += " AND source IS ?"
        params.append(source)
    rows = {r[0] for r in conn.execute(sql, params)}
    conn.close()
    return rows


# ------------------------------------------------- v38: provenance of derivatives

def test_v38_adds_provenance_columns(db):
    conn = sqlite3.connect(db)
    acols = {r[1] for r in conn.execute("PRAGMA table_info(action_items)")}
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(comments)")}
    ver = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
    conn.close()
    assert "comment_id" in acols
    assert {"analyzed_at", "analyzed_model"} <= ccols
    assert ver >= 38


def test_v38_idempotent(db):
    init_database(db)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM schema_versions WHERE version = 38").fetchone()[0]
    conn.close()
    assert n == 1


# ------------------------------------------------------ layer A: local graph

def test_mention_in_comment_links_entity(db):
    tid = _add_tx(db)
    eid = _add_entity(db, "person", "Мельник")
    comments.create(db, "transcription", tid, "тут насправді вирішував Мельник")
    res = comments.link_entities(db, tid)
    assert res["written"] == 1
    assert _links(db, tid, comments.ENTITY_SOURCE) == {eid}


def test_link_marks_provenance_not_plain(db):
    """A mention in a comment is "named in a remark ABOUT the meeting", not "named
    AT the meeting". Without the mark, a slice by person would mix two different claims."""
    tid = _add_tx(db)
    _add_entity(db, "project", "Acmecorp")
    comments.create(db, "transcription", tid, "по Acmecorp рішення скасовано")
    comments.link_entities(db, tid)
    assert _links(db, tid, None) == set()                       # source IS NULL — empty
    assert len(_links(db, tid, comments.ENTITY_SOURCE)) == 1


def test_link_does_not_touch_foreign_links(db):
    """Links of a Claude card (source NULL) and of TG (thread_match) are foreign."""
    tid = _add_tx(db)
    other = _add_entity(db, "person", "Хтось Інший")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, "
                 "mention_count, source) VALUES (?, ?, 1, NULL)", (tid, other))
    conn.commit(); conn.close()
    _add_entity(db, "project", "Acmecorp")
    comments.create(db, "transcription", tid, "по Acmecorp все стало")
    comments.link_entities(db, tid)
    assert other in _links(db, tid, None)


def test_link_never_overwrites_stronger_claim(db):
    """If the entity is already attached by a card ("it was said at the meeting"), the comment
    must not overwrite that provenance with its own, weaker one."""
    tid = _add_tx(db)
    eid = _add_entity(db, "person", "Мельник")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, "
                 "mention_count, source) VALUES (?, ?, 5, NULL)", (tid, eid))
    conn.commit(); conn.close()
    comments.create(db, "transcription", tid, "уточнення: Мельник підтвердив")
    comments.link_entities(db, tid)
    conn = sqlite3.connect(db)
    src, cnt = conn.execute("SELECT source, mention_count FROM meeting_entities "
                            "WHERE transcription_id = ? AND entity_id = ?",
                            (tid, eid)).fetchone()
    conn.close()
    assert src is None and cnt == 5


def test_link_is_recomputed_from_all_live_comments(db):
    """The recompute goes by RECORDING, not by a single comment: meeting_entities
    has no field for a comment, so "removing this comment's links" is impossible
    without touching the neighbours."""
    tid = _add_tx(db)
    e1 = _add_entity(db, "person", "Мельник")
    e2 = _add_entity(db, "project", "Acmecorp")
    comments.create(db, "transcription", tid, "питання до Мельник")
    c2 = comments.create(db, "transcription", tid, "а по Acmecorp тиша")
    comments.link_entities(db, tid)
    assert _links(db, tid, comments.ENTITY_SOURCE) == {e1, e2}
    # Deleting the second comment must remove ITS link and leave the first.
    comments.delete(db, c2["id"])
    assert _links(db, tid, comments.ENTITY_SOURCE) == {e1}


def test_delete_removes_orphaned_mention(db):
    tid = _add_tx(db)
    _add_entity(db, "project", "Acmecorp")
    c = comments.create(db, "transcription", tid, "по Acmecorp все")
    comments.link_entities(db, tid)
    assert _links(db, tid, comments.ENTITY_SOURCE)
    comments.delete(db, c["id"])
    assert _links(db, tid, comments.ENTITY_SOURCE) == set()


def test_restore_brings_mention_back(db):
    tid = _add_tx(db)
    eid = _add_entity(db, "project", "Acmecorp")
    c = comments.create(db, "transcription", tid, "по Acmecorp все")
    comments.delete(db, c["id"])
    comments.restore(db, c["id"])
    assert _links(db, tid, comments.ENTITY_SOURCE) == {eid}


def test_lowercase_mention_is_not_a_link(db):
    """A rule shared with ingest: a proper name is recognized by a capital letter not
    at the start of a sentence. Otherwise «том» and «адам» would clutter the graph."""
    tid = _add_tx(db)
    _add_entity(db, "project", "Барselona")
    comments.create(db, "transcription", tid, "тут йшлося про барselona загалом")
    comments.link_entities(db, tid)
    assert _links(db, tid, comments.ENTITY_SOURCE) == set()


def test_link_is_idempotent(db):
    tid = _add_tx(db)
    _add_entity(db, "project", "Acmecorp")
    comments.create(db, "transcription", tid, "по Acmecorp все")
    first = comments.link_entities(db, tid)["written"]
    second = comments.link_entities(db, tid)["written"]
    assert first == second == 1


# ---------------------------------------------------------- anchor (resolve)

def test_resolve_transcription_by_target(db):
    tid = _add_tx(db)
    assert comments.resolve_transcription(db, "transcription", tid) == tid
    # An entity/direction has no anchor — a normal state, not an error.
    assert comments.resolve_transcription(db, "entity", 1) is None
    assert comments.resolve_transcription(db, "recording_session", 7) is None


def test_resolve_media_card_via_shared_file(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, "
                 "file_path) VALUES ('u', 'y1', 'Аудіо', 'X:/a/final.mp3')")
    aid = conn.execute("SELECT id FROM audio_downloads").fetchone()[0]
    conn.execute("INSERT INTO transcriptions (source_type, source_name, "
                 "transcript_text, file_path) VALUES ('file', 'Аудіо', 't', 'X:/a/final.mp3')")
    tid = conn.execute("SELECT id FROM transcriptions").fetchone()[0]
    conn.commit(); conn.close()
    assert comments.resolve_transcription(db, "audio_download", aid) == tid


def test_resolve_media_card_without_transcript(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO audio_downloads (youtube_url, youtube_id, title, "
                 "file_path) VALUES ('u', 'y2', 'Без тексту', 'X:/b/final.mp3')")
    aid = conn.execute("SELECT id FROM audio_downloads").fetchone()[0]
    conn.commit(); conn.close()
    assert comments.resolve_transcription(db, "audio_download", aid) is None


def test_link_for_comment_is_quiet_without_anchor(db):
    c = comments.create(db, "recording_session", 5, "оператор: клієнт передумав",
                        source="live")
    assert comments.link_entities_for_comment(db, c["id"])["status"] == "no_anchor"


def test_relink_all_converges(db):
    t1, t2 = _add_tx(db, "a"), _add_tx(db, "b")
    _add_entity(db, "project", "Acmecorp")
    comments.create(db, "transcription", t1, "по Acmecorp все")
    comments.create(db, "transcription", t2, "знову Acmecorp")
    assert comments.relink_all(db, dry_run=True)["transcriptions"] == 2
    assert comments.relink_all(db)["written"] == 2
    assert comments.relink_all(db)["written"] == 2     # idempotent


# -------------------------------------------------- layer B: analysis (Claude)

_PARSED = {
    "action_items": [
        {"task": "переписати договір", "owner": "власник",
         "due": "до пʼятниці", "due_date": "2026-08-21"},
    ],
    "people": [{"name": "Мельник", "role": "юрист", "aliases": ["Андрієвск"]}],
    "projects": [{"name": "Acmecorp", "aliases": []}],
    "orgs": [],
    "model": "claude-test", "input_tokens": 10, "output_tokens": 5,
    "cache_read_tokens": 0, "cache_creation_tokens": 0,
}


@pytest.fixture
def mock_claude(monkeypatch):
    """Claude is mocked entirely: the test checks the WRITING of the result, not the model."""
    from app.services import enrichment, text_polishing
    calls = []
    monkeypatch.setattr(enrichment, "is_available", lambda: True)

    def _fake(text, comment_date=None, model=None, timeout=120.0, effort="low",
              owner_names=()):
        calls.append({"text": text, "date": comment_date, "effort": effort})
        return dict(_PARSED)
    monkeypatch.setattr(text_polishing, "extract_comment_items", _fake)
    return calls


@pytest.fixture
def mock_claude_per_comment(mock_claude, monkeypatch):
    """Like mock_claude, but each comment yields its own task text: since ADR-056 a task
    whose text already exists in the record is not proposed a second time."""
    from app.services import text_polishing

    def _fake(text, comment_date=None, model=None, timeout=120.0, effort="low",
              owner_names=()):
        parsed = dict(_PARSED)
        parsed["action_items"] = [{**it, "task": f"{it['task']} ({text})"}
                                  for it in _PARSED["action_items"]]
        return parsed
    monkeypatch.setattr(text_polishing, "extract_comment_items", _fake)
    return mock_claude


def test_analyze_writes_tasks_and_entities(db, mock_claude):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "треба переписати договір до пʼятниці")
    res = comments.analyze(db, c["id"])
    assert res["status"] == "analyzed"
    assert res["counts"] == {"entities": 2, "action_items": 1}

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT task, owner_name, due, due_date, source, comment_id "
                       "FROM action_items").fetchone()
    conn.close()
    assert row == ("переписати договір", "власник", "до пʼятниці", "2026-08-21",
                   comments.ACTION_SOURCE, c["id"])


def test_analyze_links_found_entities_to_graph(db, mock_claude):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "текст")
    comments.analyze(db, c["id"])
    assert len(_links(db, tid, comments.ENTITY_SOURCE)) == 2


def test_analyze_is_idempotent_and_rewrites_only_own_tasks(db, mock_claude_per_comment):
    """This is why comment_id appeared in v38: a repeated analysis must neither
    breed duplicates nor erase tasks of a NEIGHBOURING comment of the same recording."""
    tid = _add_tx(db)
    c1 = comments.create(db, "transcription", tid, "перший")
    c2 = comments.create(db, "transcription", tid, "другий")
    comments.analyze(db, c1["id"])
    comments.analyze(db, c2["id"])
    assert comments.analyze(db, c1["id"])["status"] == "skipped"
    comments.analyze(db, c1["id"], force=True)

    conn = sqlite3.connect(db)
    total = conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0]
    per_comment = dict(conn.execute(
        "SELECT comment_id, COUNT(*) FROM action_items GROUP BY comment_id"))
    conn.close()
    assert total == 2
    assert per_comment == {c1["id"]: 1, c2["id"]: 1}


def test_analyze_marks_comment_and_exposes_flag(db, mock_claude):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "текст")
    assert comments.get(db, c["id"])["analyzed"] is False
    comments.analyze(db, c["id"])
    got = comments.get(db, c["id"])
    assert got["analyzed"] is True and got["analyzed_model"] == "claude-test"


def test_analyze_skips_without_anchor_before_paying(db, mock_claude):
    """Without a transcript there is nowhere to put the task — we do not call the model at all."""
    c = comments.create(db, "recording_session", 9, "текст", source="live")
    assert comments.analyze(db, c["id"])["status"] == "no_anchor"
    assert mock_claude == []            # no Claude call at all


def test_analyze_degrades_without_api_key(db, monkeypatch):
    from app.services import enrichment
    monkeypatch.setattr(enrichment, "is_available", lambda: False)
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "текст")
    assert comments.analyze(db, c["id"])["status"] == "unavailable"


def test_analyze_survives_claude_failure(db, monkeypatch):
    from app.services import enrichment, text_polishing
    monkeypatch.setattr(enrichment, "is_available", lambda: True)

    def _boom(*a, **kw):
        raise RuntimeError("Claude returned invalid JSON")
    monkeypatch.setattr(text_polishing, "extract_comment_items", _boom)
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "текст")
    res = comments.analyze(db, c["id"])
    assert res["status"] == "retry_needed"
    # The comment stays whole and unmarked — the analysis can be repeated.
    assert comments.get(db, c["id"])["analyzed"] is False


def test_analyze_passes_comment_date_for_relative_deadlines(db, mock_claude):
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "до кінця тижня")
    comments.analyze(db, c["id"])
    assert mock_claude[0]["date"] == str(c["created_at"])[:10]
    assert mock_claude[0]["effort"] == "low"


def test_analyze_never_runs_on_create(db, mock_claude, mock_embeddings):
    """The analysis is paid — it must NOT start by itself when a comment is created."""
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "треба переписати договір")
    comments.index_comment(db, c["id"])
    comments.link_entities_for_comment(db, c["id"])
    assert mock_claude == []
    assert comments.get(db, c["id"])["analyzed"] is False


# ------------------------------------------ regressions from the code review (merge)

def test_deleting_a_comment_takes_its_tasks_with_it(db, mock_claude):
    """Found by the review: tasks extracted by Claude from a comment stayed "open" in
    the dashboard and in the digest forever, and the source became invisible — the owner
    deleted the comment as erroneous, and three tasks from it keep demanding attention."""
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "треба переписати договір")
    comments.analyze(db, c["id"])
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 1
    conn.close()

    comments.delete(db, c["id"])
    conn = sqlite3.connect(db)
    left = conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0]
    conn.close()
    assert left == 0


def test_delete_does_not_touch_other_comments_tasks(db, mock_claude_per_comment):
    tid = _add_tx(db)
    c1 = comments.create(db, "transcription", tid, "перший")
    c2 = comments.create(db, "transcription", tid, "другий")
    comments.analyze(db, c1["id"])
    comments.analyze(db, c2["id"])
    comments.delete(db, c1["id"])
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT comment_id FROM action_items").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [c2["id"]]


def test_restored_comment_can_be_analyzed_again(db, mock_claude):
    """Deleting clears analyzed_at, so after restore the analysis is possible —
    otherwise the comment would stay "analyzed" without a single task."""
    tid = _add_tx(db)
    c = comments.create(db, "transcription", tid, "треба переписати договір")
    comments.analyze(db, c["id"])
    comments.delete(db, c["id"])
    comments.restore(db, c["id"])
    assert comments.get(db, c["id"])["analyzed"] is False
    assert comments.analyze(db, c["id"])["status"] == "analyzed"
