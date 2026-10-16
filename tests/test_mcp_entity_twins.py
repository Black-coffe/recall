"""Namesakes in the graph: an entity dossier must not pass off part of the history as all of it.

Enrichment registers the same thing separately in each type, so in the live
archive «Acmecorp» exists as a project (meetings) and as an org (32), and «Промінвест»
is scattered over six rows. There are 137 such groups. `entity_dedup` does not see them by
construction — it compares only within one type.

What is checked here is NOT merging (it has already once broken consumers —
`memory/entity-merge-moves-names-to-aliases`) but honesty: the tool shows the rest of
the group and the sum, leaving the graph as it is.

Offline: a temporary SQLite via init_database, no network and no heavy models.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server as m
from app.db.connection import get_db_connection
from app.db.migrations import init_database


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "twins.db")
    init_database(path)
    monkeypatch.setattr(m, "DB_PATH", path)
    return path


def _entity(db: str, name: str, etype: str, meetings: int, mentions: int) -> int:
    with get_db_connection(db) as conn:
        cur = conn.execute(
            "INSERT INTO entities (type, canonical_name, normalized_name, "
            "mention_count, meeting_count) VALUES (?, ?, ?, ?, ?)",
            (etype, name, name.casefold(), mentions, meetings))
        conn.commit()
        return cur.lastrowid


def _link(db: str, entity_id: int, transcription_ids: list[int], mentions: int = 1) -> None:
    """Links of an entity to meetings — the group total is computed from exactly these."""
    with get_db_connection(db) as conn:
        for tid in transcription_ids:
            conn.execute(
                "INSERT OR IGNORE INTO transcriptions (id, source_type, source_name, "
                "transcript_text) VALUES (?, 'file', ?, 'текст')", (tid, f"rec{tid}.mp3"))
            conn.execute(
                "INSERT INTO meeting_entities (transcription_id, entity_id, mention_count, "
                "salience) VALUES (?, ?, ?, 0.5)", (tid, entity_id, mentions))
        conn.commit()


def test_name_key_folds_case_and_punctuation():
    """The folding key must work on Cyrillic — SQLite LOWER does not do that."""
    assert m._name_key("Acmecorp") == m._name_key("AcmeCorp")
    assert m._name_key("Алгоритм") == m._name_key("алгоритм")
    assert m._name_key("Смак-Кафе") == m._name_key("Смак Кафе")
    assert m._name_key("Aegis.UA") == m._name_key("Aegis UA")
    assert m._name_key("") == ""


def test_name_key_folds_the_apostrophe_that_data_actually_uses():
    """In 9 archive names the apostrophe is U+02BC (ʼ), and U+2019 (’) never occurs."""
    assert m._name_key("Вʼячеслав") == m._name_key("Вячеслав")
    assert m._name_key("Вʼячеслав") == m._name_key("В’ячеслав")


def test_name_key_keeps_the_parenthetical_because_it_disambiguates_people():
    """«Юлія (Willow)» is a different woman than «Юлія» (meetings), not the same one with a note.

    Stripping the parenthesised tail seems natural («Промінвест (хаб)» → «Промінвест»),
    but for people it glues different ones together: the graph holds «Юлія», «Юлія (Willow)»,
    «Юлія (Ділова англійська)» and «Юлія (потенційна викладачка Ділова)». The dossier
    showed them as one group with a total of meetings — exactly the lie
    this mechanism was made against.
    """
    assert m._name_key("Юлія (Willow)") != m._name_key("Юлія")
    assert m._name_key("Юлія (Willow)") != m._name_key("Юлія (Ділова англійська)")
    assert m._name_key("Дмитро (юрист)") != m._name_key("Дмитро (аналітик)")


def test_get_entity_reports_twins_across_types(db: str):
    """The project row's dossier speaks of both the org row and the group total."""
    keep = _entity(db, "Acmecorp", "project", meetings=3, mentions=30)
    twin = _entity(db, "AcmeCorp", "org", meetings=2, mentions=20)
    _link(db, keep, [1, 2, 3], mentions=10)
    _link(db, twin, [4, 5], mentions=10)

    d = m.get_entity(keep)

    assert d["meeting_count"] == 3                         # its own number is not replaced
    assert [r["id"] for r in d["same_name_entities"]] == [twin]
    totals = d["totals_across_same_name"]
    assert totals["entities"] == 2
    assert totals["meeting_count"] == 5
    assert totals["mention_count"] == 50


def test_totals_count_shared_meetings_once(db: str):
    """A shared meeting is not doubled: group rows often sit on the same call."""
    keep = _entity(db, "Acmecorp", "project", meetings=2, mentions=20)
    twin = _entity(db, "AcmeCorp", "org", meetings=2, mentions=20)
    _link(db, keep, [1, 2])
    _link(db, twin, [2, 3])                                # meeting 2 is shared

    totals = m.get_entity(keep)["totals_across_same_name"]

    assert totals["meeting_count"] == 3                    # 1, 2, 3 — not 4


def test_totals_ignore_stale_aggregate_columns(db: str):
    """The total is counted from links: the `entities` columns lag behind the graph.

    In the live archive «AcmeCorp» keeps meeting_count=32 with 78 real
    links, «Acmecorp» — 102 with 114. Adding up the columns gave 134 where the truth
    is 190.
    """
    keep = _entity(db, "Промінвест", "org", meetings=0, mentions=0)     # the columns went stale
    twin = _entity(db, "промінвест", "project", meetings=0, mentions=0)
    _link(db, keep, [1, 2, 3])
    _link(db, twin, [4])

    totals = m.get_entity(keep)["totals_across_same_name"]

    assert totals["meeting_count"] == 4                    # not 0, as the columns say


def test_get_entity_without_twins_stays_silent(db: str):
    """A lone entity gets no extra fields — otherwise noise on every dossier."""
    only = _entity(db, "Orbit Robotics", "project", meetings=34, mentions=88)

    d = m.get_entity(only)

    assert "same_name_entities" not in d
    assert "totals_across_same_name" not in d


def test_list_entities_marks_rows_that_are_split(db: str):
    """The list shows that a row is only part of a group, and where to look for the rest."""
    a = _entity(db, "Промінвест", "org", meetings=20, mentions=60)
    b = _entity(db, "промінвест", "project", meetings=12, mentions=30)
    solo = _entity(db, "Експрес Пошта", "org", meetings=25, mentions=70)

    rows = {r["id"]: r for r in m.list_entities(limit=50)}

    assert rows[a]["same_name_ids"] == [b]
    assert rows[b]["same_name_ids"] == [a]
    assert "same_name_ids" not in rows[solo]


def test_type_filter_hides_half_of_the_group_but_says_so(db: str):
    """`type='org'` gives 32 meetings where the group has 134 — the row must admit it."""
    _entity(db, "Acmecorp", "project", meetings=102, mentions=946)
    twin = _entity(db, "AcmeCorp", "org", meetings=32, mentions=267)

    rows = m.list_entities(type="org")

    assert [r["id"] for r in rows] == [twin]
    assert rows[0]["meeting_count"] == 32
    assert rows[0]["same_name_ids"]                      # otherwise 32 would read as everything
