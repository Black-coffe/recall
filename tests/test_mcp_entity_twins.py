"""Тезки в графі: досьє сутності не має видавати частину історії за всю.

Збагачення заводить одну й ту саму річ окремо в кожному типі, тому на живому
архіві «Acmecorp» існує як project (102 зустрічі) і як org (32), а «Промінвест»
розсипана на шість рядків. Таких груп 137. `entity_dedup` їх не бачить за
побудовою — він порівнює лише в межах одного типу.

Тут перевіряється НЕ злиття (воно вже одного разу зламало споживачів —
`memory/entity-merge-moves-names-to-aliases`), а чесність: тулз показує решту
групи і суму, лишаючи граф як є.

Офлайн: тимчасова SQLite через init_database, без мережі й без важких моделей.
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
    """Звʼязки сутності із зустрічами — саме з них рахується підсумок групи."""
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
    """Ключ згортання має працювати на кирилиці — SQLite LOWER цього не робить."""
    assert m._name_key("Acmecorp") == m._name_key("AcmeCorp")
    assert m._name_key("Алгоритм") == m._name_key("алгоритм")
    assert m._name_key("Смак-Кафе") == m._name_key("Смак Кафе")
    assert m._name_key("Aegis.UA") == m._name_key("Aegis UA")
    assert m._name_key("") == ""


def test_name_key_folds_the_apostrophe_that_data_actually_uses():
    """У 9 імен архіву апостроф — U+02BC (ʼ), а U+2019 (’) не трапляється жодного разу."""
    assert m._name_key("Вʼячеслав") == m._name_key("Вячеслав")
    assert m._name_key("Вʼячеслав") == m._name_key("В’ячеслав")


def test_name_key_keeps_the_parenthetical_because_it_disambiguates_people():
    """«Юлія (Willow)» — інша жінка, ніж «Юлія» (213 зустрічей), а не та сама з приміткою.

    Зрізання дужкового хвоста здається природним («Промінвест (хаб)» → «Промінвест»),
    але на людях воно склеює різних: у графі живуть «Юлія», «Юлія (Willow)»,
    «Юлія (Ділова англійська)» і «Юлія (потенційна викладачка Military)». Досьє
    показувало їх однією групою з підсумком 218 зустрічей — рівно та брехня,
    проти якої цей механізм і зроблено.
    """
    assert m._name_key("Юлія (Willow)") != m._name_key("Юлія")
    assert m._name_key("Юлія (Willow)") != m._name_key("Юлія (Ділова англійська)")
    assert m._name_key("Дмитро (юрист)") != m._name_key("Дмитро (аналітик)")


def test_get_entity_reports_twins_across_types(db: str):
    """Досьє project-рядка каже і про org-рядок, і про підсумок по групі."""
    keep = _entity(db, "Acmecorp", "project", meetings=3, mentions=30)
    twin = _entity(db, "AcmeCorp", "org", meetings=2, mentions=20)
    _link(db, keep, [1, 2, 3], mentions=10)
    _link(db, twin, [4, 5], mentions=10)

    d = m.get_entity(keep)

    assert d["meeting_count"] == 3                         # своє число не підмінене
    assert [r["id"] for r in d["same_name_entities"]] == [twin]
    totals = d["totals_across_same_name"]
    assert totals["entities"] == 2
    assert totals["meeting_count"] == 5
    assert totals["mention_count"] == 50


def test_totals_count_shared_meetings_once(db: str):
    """Спільна зустріч не подвоюється: рядки групи часто сидять на тому самому дзвінку."""
    keep = _entity(db, "Acmecorp", "project", meetings=2, mentions=20)
    twin = _entity(db, "AcmeCorp", "org", meetings=2, mentions=20)
    _link(db, keep, [1, 2])
    _link(db, twin, [2, 3])                                # зустріч 2 — спільна

    totals = m.get_entity(keep)["totals_across_same_name"]

    assert totals["meeting_count"] == 3                    # 1, 2, 3 — а не 4


def test_totals_ignore_stale_aggregate_columns(db: str):
    """Підсумок рахується по звʼязках: колонки `entities` відстають від графа.

    На живому архіві «AcmeCorp» зберігає meeting_count=32 при 78 реальних
    звʼязках, «Acmecorp» — 102 при 114. Складання колонок давало 134 там, де
    насправді 190.
    """
    keep = _entity(db, "Промінвест", "org", meetings=0, mentions=0)     # колонки протухли
    twin = _entity(db, "промінвест", "project", meetings=0, mentions=0)
    _link(db, keep, [1, 2, 3])
    _link(db, twin, [4])

    totals = m.get_entity(keep)["totals_across_same_name"]

    assert totals["meeting_count"] == 4                    # не 0, як кажуть колонки


def test_get_entity_without_twins_stays_silent(db: str):
    """Одинока сутність не отримує зайвих полів — інакше шум на кожному досьє."""
    only = _entity(db, "Orbit Robotics", "project", meetings=34, mentions=88)

    d = m.get_entity(only)

    assert "same_name_entities" not in d
    assert "totals_across_same_name" not in d


def test_list_entities_marks_rows_that_are_split(db: str):
    """У списку видно, що рядок — лише частина групи, і куди дивитись за рештою."""
    a = _entity(db, "Промінвест", "org", meetings=20, mentions=60)
    b = _entity(db, "промінвест", "project", meetings=12, mentions=30)
    solo = _entity(db, "Експрес Пошта", "org", meetings=25, mentions=70)

    rows = {r["id"]: r for r in m.list_entities(limit=50)}

    assert rows[a]["same_name_ids"] == [b]
    assert rows[b]["same_name_ids"] == [a]
    assert "same_name_ids" not in rows[solo]


def test_type_filter_hides_half_of_the_group_but_says_so(db: str):
    """`type='org'` віддає 32 зустрічі там, де в групі 134 — рядок має це визнати."""
    _entity(db, "Acmecorp", "project", meetings=102, mentions=946)
    twin = _entity(db, "AcmeCorp", "org", meetings=32, mentions=267)

    rows = m.list_entities(type="org")

    assert [r["id"] for r in rows] == [twin]
    assert rows[0]["meeting_count"] == 32
    assert rows[0]["same_name_ids"]                      # інакше 32 читалось би як усе
