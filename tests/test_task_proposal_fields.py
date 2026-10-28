"""Task proposal fields (task-approval story 02, ADR-056 decisions 2 and 5).

The extraction prompts ask for `for_whom`/`confidence`/`importance` and get the owner's
names; `task_proposals` normalises, filters and keys the candidates; the re-run
measurement script compares re-extracted candidates with existing tasks without writing.
"""
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import settings
from app.db.migrations import init_database
from app.services import task_proposals, text_polishing, tg_questions
from evals import task_rerun_measure


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, text):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.model = "fake-model"
        self.usage = SimpleNamespace(input_tokens=1, output_tokens=1,
                                     cache_read_input_tokens=0, cache_creation_input_tokens=0)


@pytest.fixture
def captured(monkeypatch):
    """Capture the request of each extraction call and answer with a canned JSON."""
    box = {"answer": "{}", "requests": []}
    monkeypatch.setattr(text_polishing, "_get_client", lambda: object())

    def _fake_stream(client, timeout, request_kwargs, what="polish"):
        box["requests"].append(request_kwargs)
        return _FakeResult(box["answer"])
    monkeypatch.setattr(text_polishing, "_stream_with_retry", _fake_stream)
    return box


def _prompt_texts(req):
    return req["system"][0]["text"], req["messages"][0]["content"]


_CALLS = {
    "summary": lambda names: text_polishing.summarize_transcript("Текст зустрічі", owner_names=names),
    "card": lambda names: text_polishing.extract_meeting_card("Текст зустрічі", owner_names=names),
    "comment": lambda names: text_polishing.extract_comment_items("Треба надіслати договір",
                                                                 owner_names=names),
    "chat": lambda names: text_polishing.extract_chat_tasks(
        [{"n": 1, "sender": "Юлія", "date": "2026-10-20", "text": "Зроблю до пʼятниці"}],
        owner_names=names),
}


@pytest.mark.parametrize("kind", sorted(_CALLS))
def test_prompt_asks_for_the_three_fields_and_names_the_owner(captured, kind):
    _CALLS[kind](["Андрій", "Мельник"])
    system, user = _prompt_texts(captured["requests"][-1])
    for field in ("for_whom", "confidence", "importance"):
        assert field in system, f"{kind}: {field} missing from the prompt"
    for value in ("owner", "promised_to_owner", "third_party"):
        assert f'"{value}"' in system, f"{kind}: {value} missing from the prompt"
    assert "1 | 2 | 3" in system or "1|2|3" in system
    assert "Андрій, Мельник" in user


@pytest.mark.parametrize("kind", sorted(_CALLS))
def test_no_owner_names_adds_no_owner_line(captured, kind):
    _CALLS[kind](())
    _, user = _prompt_texts(captured["requests"][-1])
    assert "for_whom" not in user


def test_card_parser_passes_new_fields_through(captured):
    captured["answer"] = (
        '{"action_items": [{"task": "Надіслати договір", "owner": "Андрій", "due": null, '
        '"due_date": null, "for_whom": "owner", "confidence": 0.8, "importance": 3, '
        '"extra": "kept"}]}')
    item = text_polishing.extract_meeting_card("Текст")["action_items"][0]
    assert item["for_whom"] == "owner" and item["confidence"] == 0.8
    assert item["importance"] == 3 and item["extra"] == "kept"


def test_old_shape_responses_still_parse(captured):
    captured["answer"] = '{"tasks": [{"task": "Зробити звіт", "owner": null, "msg": 1}]}'
    out = text_polishing.extract_chat_tasks(
        [{"n": 1, "sender": "Юлія", "text": "Зроблю звіт"}])
    assert out["tasks"] == [{"task": "Зробити звіт", "owner": None, "msg": 1}]
    captured["answer"] = '{"action_items": [{"task": "Подзвонити", "owner": null}]}'
    assert text_polishing.extract_comment_items("Подзвонити")["action_items"] == [
        {"task": "Подзвонити", "owner": None}]
    assert text_polishing.summarize_transcript("x")["action_items"] == [
        {"task": "Подзвонити", "owner": None}]


# ---------------------------------------------------------------------------
# text_key / normalize_candidate / filter_candidates
# ---------------------------------------------------------------------------

def test_text_key_ignores_case_nfc_punctuation_and_whitespace():
    base = task_proposals.text_key("Надіслати договір Юлії")
    assert task_proposals.text_key("  надіслати   ДОГОВІР, Юлії! ") == base
    # NFD form of U+0439 (U+0438 + U+0306 combining breve) vs the composed letter
    assert task_proposals.text_key("Мій звіт") == task_proposals.text_key("Мій звіт")
    assert task_proposals.text_key("Імʼя файлу") == task_proposals.text_key("Ім'я файлу")
    assert len(base) == 40


def test_text_key_differs_for_a_different_word():
    assert task_proposals.text_key("Надіслати договір") != task_proposals.text_key("Підписати договір")


def test_normalize_candidate_defaults():
    out = task_proposals.normalize_candidate({"task": "Зробити"})
    assert (out["for_whom"], out["confidence"], out["importance"]) == ("unknown", None, 2)
    assert out["task"] == "Зробити"


@pytest.mark.parametrize("raw,expected", [
    ({"importance": 7}, 3), ({"importance": 0}, 1), ({"importance": -2}, 1),
    ({"importance": "3"}, 3), ({"importance": "high"}, 2), ({"importance": True}, 2),
], ids=["above", "zero", "negative", "string", "junk", "bool"])
def test_normalize_candidate_clamps_importance(raw, expected):
    assert task_proposals.normalize_candidate(raw)["importance"] == expected


@pytest.mark.parametrize("raw,expected", [
    ({"confidence": 1.7}, 1.0), ({"confidence": -0.3}, 0.0), ({"confidence": "0.4"}, 0.4),
    ({"confidence": "sure"}, None), ({"confidence": float("nan")}, None),
], ids=["above", "below", "string", "junk", "nan"])
def test_normalize_candidate_clamps_confidence(raw, expected):
    assert task_proposals.normalize_candidate(raw)["confidence"] == expected


@pytest.mark.parametrize("value,expected", [
    ("owner", "owner"), ("Promised_To_Owner", "promised_to_owner"), ("third_party", "third_party"),
    ("me", "unknown"), (None, "unknown"), (3, "unknown"),
])
def test_normalize_candidate_for_whom(value, expected):
    assert task_proposals.normalize_candidate({"for_whom": value})["for_whom"] == expected


def test_filter_candidates_drops_third_party_and_low_confidence():
    items = [
        {"task": "a", "for_whom": "owner", "confidence": 0.9},
        {"task": "b", "for_whom": "third_party", "confidence": 0.9},
        {"task": "c", "for_whom": "owner", "confidence": 0.5},
        {"task": "d", "for_whom": "promised_to_owner"},          # confidence None: kept
        {"task": "e", "confidence": 0.6},                         # exactly at the threshold
        {"task": "f", "for_whom": "third_party", "confidence": 0.1},
    ]
    kept, dropped = task_proposals.filter_candidates(items, min_confidence=0.6)
    assert [k["task"] for k in kept] == ["a", "d", "e"]
    assert dropped == {"third_party": 2, "low_confidence": 1}
    assert kept[1]["confidence"] is None and kept[2]["for_whom"] == "unknown"


def test_existing_keys_reads_every_status_and_falls_back_to_task():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE action_items (id INTEGER PRIMARY KEY, transcription_id INTEGER, "
                 "task TEXT, status TEXT, text_key TEXT)")
    conn.executemany(
        "INSERT INTO action_items (transcription_id, task, status, text_key) VALUES (?, ?, ?, ?)",
        [(1, "Надіслати договір", "rejected", task_proposals.text_key("Надіслати договір")),
         (1, "Зробити звіт", "legacy", None),
         (2, "Чужий запис", "open", task_proposals.text_key("Чужий запис"))])
    keys = task_proposals.existing_keys(conn, 1)
    assert keys == {task_proposals.text_key("надіслати договір"),
                    task_proposals.text_key("Зробити звіт.")}


# ---------------------------------------------------------------------------
# owner_names / min_confidence
# ---------------------------------------------------------------------------

@pytest.fixture
def graph_db(tmp_path):
    db = str(tmp_path / "graph.db")
    init_database(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO entities (id, type, canonical_name, normalized_name) "
                 "VALUES (1, 'person', 'Андрій', 'андрій')")
    conn.executemany(
        "INSERT INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (?, ?, ?)",
        [(1, "Мельник", "мельник"), (1, "@johndoe", "@johndoe")])
    conn.commit()
    conn.close()
    return db


def test_owner_names_returns_canonical_and_aliases(graph_db, monkeypatch):
    monkeypatch.setattr(tg_questions, "resolve_self", lambda db, **kw: {
        "id": None, "name": "Andrii Melnyk", "username": "johndoe", "source": "env"})
    assert task_proposals.owner_names(graph_db) == ["Андрій", "@johndoe", "Мельник"]


def test_owner_names_matches_canonical_name(graph_db, monkeypatch):
    monkeypatch.setattr(tg_questions, "resolve_self", lambda db, **kw: {
        "id": None, "name": "АНДРІЙ", "username": None, "source": "env"})
    assert task_proposals.owner_names(graph_db)[0] == "Андрій"


def test_owner_names_empty_when_nothing_resolves(graph_db, monkeypatch, caplog):
    monkeypatch.setattr(tg_questions, "resolve_self", lambda db, **kw: None)
    with caplog.at_level(logging.DEBUG, logger="app.services.task_proposals"):
        assert task_proposals.owner_names(graph_db) == []
    assert len([r for r in caplog.records if r.name == "app.services.task_proposals"]) == 1


def test_owner_names_empty_when_resolve_raises(graph_db, monkeypatch):
    def _boom(db, **kw):
        raise OSError("listener down")
    monkeypatch.setattr(tg_questions, "resolve_self", _boom)
    assert task_proposals.owner_names(graph_db) == []


def test_min_confidence_from_registry(monkeypatch):
    monkeypatch.delenv("TASK_PROPOSAL_MIN_CONFIDENCE", raising=False)
    assert task_proposals.min_confidence() == 0.6
    monkeypatch.setenv("TASK_PROPOSAL_MIN_CONFIDENCE", "0.75")
    assert task_proposals.min_confidence() == 0.75
    monkeypatch.setenv("TASK_PROPOSAL_MIN_CONFIDENCE", "lots")
    assert task_proposals.min_confidence() == 0.6


def test_min_confidence_setting_registered():
    s = settings.by_name()["TASK_PROPOSAL_MIN_CONFIDENCE"]
    assert s.kind == "float" and float(s.default) == 0.6 and s.gates.strip()


# ---------------------------------------------------------------------------
# evals/task_rerun_measure.py
# ---------------------------------------------------------------------------

@pytest.fixture
def archive_db(tmp_path):
    db = str(tmp_path / "archive.db")
    init_database(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO transcriptions (id, source_type, source_name, transcript_text, meeting_date) "
        "VALUES (?, 'recording', ?, ?, '2026-10-01')",
        [(1, "Зустріч 1", "Андрій: надішлю договір до пʼятниці."),
         (2, "Зустріч 2", "Юлія: зроблю звіт для фонду.")])
    conn.executemany(
        "INSERT INTO action_items (transcription_id, task, status) VALUES (?, ?, 'open')",
        [(1, "Надіслати договір"), (2, "Підготувати звіт для фонду")])
    conn.commit()
    conn.close()
    return db


def _fake_extractor(calls):
    def run(record):
        calls.append(record["id"])
        if record["id"] == 1:
            return [{"task": "надіслати договір!", "for_whom": "owner", "confidence": 0.9,
                     "importance": 2}]
        return [{"task": "Зробити звіт для фонду", "owner": "Юлія"}]     # non-compliant
    return run


def test_measure_dry_run_prints_estimate_without_calling(archive_db, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []
    rc = task_rerun_measure.main(["--db", archive_db, "--sample", "5", "--dry-run"],
                                 extractor=_fake_extractor(calls))
    out = capsys.readouterr().out
    assert rc == 0 and calls == []
    assert "sample: 2 records" in out and "characters" in out and "estimated cost $" in out


def test_measure_refuses_without_key(archive_db, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []
    rc = task_rerun_measure.main(
        ["--db", archive_db, "--out", str(tmp_path / "r.md")], extractor=_fake_extractor(calls))
    assert rc != 0 and calls == []
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "r.md").exists()


def test_measure_writes_report_and_does_not_write_db(archive_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")
    before = sqlite3.connect(archive_db).execute("SELECT COUNT(*) FROM action_items").fetchone()
    out = tmp_path / "report.md"
    calls = []
    rc = task_rerun_measure.main(["--db", archive_db, "--out", str(out)],
                                 extractor=_fake_extractor(calls))
    assert rc == 0 and sorted(calls) == [1, 2]
    text = out.read_text(encoding="utf-8")
    header = ("| record id | candidate | nearest existing | exact_hit | jaccard | for_whom "
              "| confidence | missing_fields | verdict |")
    assert header in text
    assert "| 1 | надіслати договір! | Надіслати договір | True | 1.0 | owner | 0.9 | False |" in text
    assert "| 2 | Зробити звіт для фонду | Підготувати звіт для фонду | False |" in text
    assert "- candidates: 2" in text
    assert "- exact hits: 1 (50.0%)" in text
    assert "non-compliance (missing for_whom/confidence/importance): 1 (50.0%)" in text
    after = sqlite3.connect(archive_db).execute("SELECT COUNT(*) FROM action_items").fetchone()
    assert before == after


def test_measure_exits_non_zero_when_every_extraction_fails(archive_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")

    def _boom(record):
        raise RuntimeError("AuthenticationError")
    rc = task_rerun_measure.main(["--db", archive_db, "--out", str(tmp_path / "r.md")],
                                 extractor=_boom)
    assert rc == 3
