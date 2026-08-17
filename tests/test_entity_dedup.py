"""Юніт-тести offline fuzzy-dedup сутностей (app/services/entity_dedup.py) — T6.7.

Офлайн, реальна (tmp_path) SQLite БД через init_database (той самий патерн, що
tests/test_enrichment.py) — БЕЗ Claude API/мережі і БЕЗ реальної e5-моделі:
embeddings.embed_texts підмінюється (monkeypatch) детермінованими векторами,
щоб перевірити логіку пошуку кандидатів/порогу без завантаження torch/e5.

Покриває:
  - find_merge_candidates: пари понад поріг, поділ за type, поріг фільтрує,
    RuntimeError коли embeddings недоступні.
  - merge_entities: перенесення aliases/meeting_entities (сумування при
    конфлікті)/action_items/role/description/speaker_id, аудит-слід
    metadata_json, видалення merge_id, перерахунок агрегатів keep_id,
    type-mismatch/keep==merge/not_found помилки, ідемпотентність повторного
    merge (вже видаленого merge_id).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import embeddings, entity_dedup
from app.services.enrichment import _normalize


# ============================================================
# Fixtures / helpers
# ============================================================

@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    init_database(path)
    return path


def _insert_transcription(db_path: str, **overrides) -> int:
    defaults = dict(
        source_type="file", source_name="rec.mp3",
        transcript_text="Розмова про бюджет проєкту.",
        segments=None, structure_json=None,
        created_at="2026-05-12 10:00:00",
    )
    defaults.update(overrides)
    cols = list(defaults.keys())
    placeholders = ", ".join("?" for _ in cols)
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO transcriptions ({', '.join(cols)}) VALUES ({placeholders})",
            [defaults[c] for c in cols],
        )
        conn.commit()
        return cur.lastrowid


def _insert_entity(db_path: str, etype: str, name: str, **overrides) -> int:
    norm = _normalize(name)
    defaults = dict(role=None, description=None, speaker_id=None,
                    mention_count=0, meeting_count=0, metadata_json=None)
    defaults.update(overrides)
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO entities (type, canonical_name, normalized_name, role, "
            "description, speaker_id, mention_count, meeting_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (etype, name, norm, defaults["role"], defaults["description"],
             defaults["speaker_id"], defaults["mention_count"],
             defaults["meeting_count"], defaults["metadata_json"]),
        )
        conn.commit()
        return cur.lastrowid


def _insert_alias(db_path: str, entity_id: int, alias: str) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (?, ?, ?)",
            (entity_id, alias, _normalize(alias)),
        )
        conn.commit()


def _insert_meeting_entity(db_path: str, tid: int, eid: int, mentions: int = 1,
                            salience=None, role_in_meeting=None) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO meeting_entities (transcription_id, entity_id, mention_count, "
            "salience, role_in_meeting) VALUES (?, ?, ?, ?, ?)",
            (tid, eid, mentions, salience, role_in_meeting),
        )
        conn.commit()


def _insert_action_item(db_path: str, tid: int, task: str, owner_entity_id=None,
                        owner_name=None) -> int:
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO action_items (transcription_id, task, owner_entity_id, "
            "owner_name, status) VALUES (?, ?, ?, ?, 'open')",
            (tid, task, owner_entity_id, owner_name),
        )
        conn.commit()
        return cur.lastrowid


def _fake_embed_factory(vec_map: dict[str, list[float]], dim: int = 4):
    """Повертає fake embed_texts(names) -> np.ndarray[N, dim], L2-нормалізовані,
    за заданим name -> raw vector словником (невідомі імена -> ортогональний
    випадковий вектор за хешем імені, щоб не колізувати з мапою)."""
    def _fake_embed_texts(names, batch_size=32):
        rows = []
        for n in names:
            raw = vec_map.get(n)
            if raw is None:
                # детермінований "унікальний" вектор для невідомого імені
                h = abs(hash(n)) % 1000
                raw = [0.0] * dim
                raw[h % dim] = 1.0
                raw[(h + 1) % dim] = 0.001
            v = np.array(raw, dtype=np.float32)
            norm = np.linalg.norm(v)
            rows.append(v / norm if norm else v)
        return np.vstack(rows) if rows else np.zeros((0, dim), dtype=np.float32)
    return _fake_embed_texts


# ============================================================
# find_merge_candidates
# ============================================================

def test_find_merge_candidates_pairs_similar_names(db_path, monkeypatch):
    _insert_entity(db_path, "person", "Олена Петренко")
    _insert_entity(db_path, "person", "Олена")
    _insert_entity(db_path, "person", "Іван Коваль")

    vec_map = {
        "Олена Петренко": [1.0, 0.0, 0.0, 0.0],
        "Олена": [0.95, 0.05, 0.0, 0.0],          # висока схожість з "Олена Петренко"
        "Іван Коваль": [0.0, 0.0, 1.0, 0.0],       # ортогональний, не кандидат
    }
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_factory(vec_map))

    candidates = entity_dedup.find_merge_candidates(db_path, etype="person", threshold=0.9)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["type"] == "person"
    assert {c["name1"], c["name2"]} == {"Олена Петренко", "Олена"}
    assert c["similarity"] >= 0.9


def test_find_merge_candidates_threshold_filters(db_path, monkeypatch):
    _insert_entity(db_path, "person", "Олена Петренко")
    _insert_entity(db_path, "person", "Олена")
    vec_map = {
        "Олена Петренко": [1.0, 0.0, 0.0, 0.0],
        "Олена": [0.95, 0.05, 0.0, 0.0],
    }
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_factory(vec_map))

    # Поріг вище фактичної схожості -> нема кандидатів.
    assert entity_dedup.find_merge_candidates(db_path, etype="person", threshold=0.999) == []


def test_find_merge_candidates_separates_by_type(db_path, monkeypatch):
    # Однакові вектори, але РІЗНІ типи -> не повинні зіставлятись одне з одним.
    _insert_entity(db_path, "person", "Ромашка")
    _insert_entity(db_path, "org", "Ромашка")
    vec_map = {"Ромашка": [1.0, 0.0, 0.0, 0.0]}
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_factory(vec_map))

    candidates = entity_dedup.find_merge_candidates(db_path, threshold=0.5)
    assert candidates == []


def test_find_merge_candidates_sorted_desc_and_limit(db_path, monkeypatch):
    _insert_entity(db_path, "person", "A")
    _insert_entity(db_path, "person", "B")
    _insert_entity(db_path, "person", "C")
    vec_map = {
        "A": [1.0, 0.0, 0.0, 0.0],
        "B": [0.99, 0.01, 0.0, 0.0],   # A-B дуже схожі
        "C": [0.90, 0.10, 0.0, 0.0],   # A-C менш схожі (але й досі > поріг)
    }
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_factory(vec_map))

    candidates = entity_dedup.find_merge_candidates(db_path, threshold=0.8)
    assert len(candidates) >= 2
    sims = [c["similarity"] for c in candidates]
    assert sims == sorted(sims, reverse=True)

    limited = entity_dedup.find_merge_candidates(db_path, threshold=0.8, limit=1)
    assert len(limited) == 1
    assert limited[0]["similarity"] == sims[0]


def test_find_merge_candidates_raises_when_embeddings_unavailable(db_path, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    monkeypatch.setattr(embeddings, "unavailability_reason", lambda: "torch не встановлено")
    with pytest.raises(RuntimeError):
        entity_dedup.find_merge_candidates(db_path)


def test_find_merge_candidates_no_op_on_empty_or_singleton(db_path, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_factory({}))
    assert entity_dedup.find_merge_candidates(db_path) == []

    _insert_entity(db_path, "person", "Соло")
    assert entity_dedup.find_merge_candidates(db_path, etype="person") == []


# ============================================================
# merge_entities
# ============================================================

def test_merge_entities_keep_id_equals_merge_id(db_path):
    eid = _insert_entity(db_path, "person", "Хтось")
    res = entity_dedup.merge_entities(db_path, eid, eid)
    assert res["status"] == "error"


def test_merge_entities_not_found(db_path):
    eid = _insert_entity(db_path, "person", "Хтось")
    res = entity_dedup.merge_entities(db_path, eid, 99999)
    assert res["status"] == "not_found"


def test_merge_entities_type_mismatch(db_path):
    a = _insert_entity(db_path, "person", "Хтось")
    b = _insert_entity(db_path, "org", "Хтось Інк")
    res = entity_dedup.merge_entities(db_path, a, b)
    assert res["status"] == "error"
    assert "type mismatch" in res["error"]


def test_merge_entities_full_roundtrip(db_path):
    keep = _insert_entity(db_path, "person", "Олена Петренко", role=None, speaker_id=None)
    merge = _insert_entity(db_path, "person", "Олена", role="ПМ", description="коротко")
    _insert_alias(db_path, merge, "О. Петренко")

    t1 = _insert_transcription(db_path, source_name="call1.mp3")
    t2 = _insert_transcription(db_path, source_name="call2.mp3")
    # t1: обидві сутності згадані у ТОМУ Ж мітингу -> конфлікт при переносі, сумуємо.
    _insert_meeting_entity(db_path, t1, keep, mentions=2, salience=0.4)
    _insert_meeting_entity(db_path, t1, merge, mentions=3, salience=0.7)
    # t2: лише merge -> просте перенесення entity_id.
    _insert_meeting_entity(db_path, t2, merge, mentions=1, salience=0.2)

    ai_id = _insert_action_item(db_path, t1, "Підготувати звіт", owner_entity_id=merge)

    res = entity_dedup.merge_entities(db_path, keep, merge)
    assert res["status"] == "merged"
    assert res["keep_id"] == keep
    assert res["merge_id"] == merge

    with get_db_connection(db_path) as conn:
        # merge-сутність видалена
        assert conn.execute("SELECT id FROM entities WHERE id = ?", (merge,)).fetchone() is None

        keep_row = conn.execute("SELECT * FROM entities WHERE id = ?", (keep,)).fetchone()
        assert keep_row["role"] == "ПМ"                 # успадковано, бо в keep було порожньо
        assert keep_row["description"] == "коротко"
        meta = json.loads(keep_row["metadata_json"])
        assert meta["merged_from"][0]["id"] == merge

        # aliases: canonical "Олена" + "О. Петренко" тепер aliases keep
        aliases = {r["normalized_alias"] for r in conn.execute(
            "SELECT normalized_alias FROM entity_aliases WHERE entity_id = ?", (keep,))}
        assert _normalize("Олена") in aliases
        assert _normalize("О. Петренко") in aliases

        # meeting_entities: t1 — сумовано (2+3=5), t2 — перенесено
        me_t1 = conn.execute(
            "SELECT mention_count, salience FROM meeting_entities "
            "WHERE transcription_id = ? AND entity_id = ?", (t1, keep)).fetchone()
        assert me_t1["mention_count"] == 5
        assert me_t1["salience"] == pytest.approx(0.7)
        me_t2 = conn.execute(
            "SELECT mention_count FROM meeting_entities "
            "WHERE transcription_id = ? AND entity_id = ?", (t2, keep)).fetchone()
        assert me_t2["mention_count"] == 1
        # жодних залишкових рядків на видалений merge_id
        leftover = conn.execute(
            "SELECT COUNT(*) AS n FROM meeting_entities WHERE entity_id = ?", (merge,)
        ).fetchone()["n"]
        assert leftover == 0

        # action_items: owner переприязано
        ai_row = conn.execute("SELECT owner_entity_id FROM action_items WHERE id = ?",
                              (ai_id,)).fetchone()
        assert ai_row["owner_entity_id"] == keep

        # агрегати перераховані: meeting_count по meeting_entities keep = 2 (t1,t2),
        # mention_count = 5 + 1 = 6
        assert keep_row["meeting_count"] == 2
        assert keep_row["mention_count"] == 6


def test_merge_entities_speaker_id_inherited_when_keep_empty(db_path):
    with get_db_connection(db_path) as conn:
        cur = conn.execute("INSERT INTO speakers (name) VALUES (?)", ("Олена П.",))
        conn.commit()
        speaker_id = cur.lastrowid

    keep = _insert_entity(db_path, "person", "Олена Петренко")
    merge = _insert_entity(db_path, "person", "Олена", speaker_id=speaker_id)

    res = entity_dedup.merge_entities(db_path, keep, merge)
    assert res["status"] == "merged"
    with get_db_connection(db_path) as conn:
        keep_row = conn.execute("SELECT speaker_id FROM entities WHERE id = ?", (keep,)).fetchone()
        assert keep_row["speaker_id"] == speaker_id


def test_merge_entities_is_idempotent_on_repeat(db_path):
    keep = _insert_entity(db_path, "person", "Олена Петренко")
    merge = _insert_entity(db_path, "person", "Олена")

    first = entity_dedup.merge_entities(db_path, keep, merge)
    assert first["status"] == "merged"

    # Повторний merge вже неіснуючого merge_id -> not_found, без падіння/побічних ефектів.
    second = entity_dedup.merge_entities(db_path, keep, merge)
    assert second["status"] == "not_found"

    with get_db_connection(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM entities WHERE id = ?", (keep,)).fetchone()["n"]
        assert n == 1


# ============================================================
# Кросс-типове злиття (12.08.2026)
# ============================================================

def test_cross_type_merge_is_refused_by_default(db_path):
    """Заборона лишається за замовчуванням: для ембединг-кандидатів вона слушна."""
    a = _insert_entity(db_path, "person", "Хтось")
    b = _insert_entity(db_path, "org", "Хтось Інк")

    res = entity_dedup.merge_entities(db_path, a, b)

    assert res["status"] == "error" and "type mismatch" in res["error"]


def test_cross_type_merge_joins_one_thing_written_twice(db_path):
    """Головний клас дублів архіву: одна річ заведена і як project, і як org.

    Збагачення створює сутність окремо в кожному типі, тому «Acmecorp» жив як
    project (114 звʼязків) і як org (78) — 133 такі групи зі 152. Тип лишається
    від `keep_id`, звʼязки й задачі переїжджають, merge-рядок зникає.
    """
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "AcmeCorp", role="портфельна компанія")
    _insert_alias(db_path, gone, "Acme Corp")
    t1 = _insert_transcription(db_path, source_name="call1.mp3")
    t2 = _insert_transcription(db_path, source_name="call2.mp3")
    _insert_meeting_entity(db_path, t1, keep, mentions=3)
    _insert_meeting_entity(db_path, t2, gone, mentions=5)
    task = _insert_action_item(db_path, t2, "Підписати SHA", owner_entity_id=gone)

    res = entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)
    assert res["status"] == "merged"

    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT type, meeting_count FROM entities WHERE id = ?",
                           (keep,)).fetchone()
        assert row["type"] == "project"                     # тип від keep
        assert row["meeting_count"] == 2                    # обидві зустрічі
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id = ?",
                            (gone,)).fetchone()[0] == 0
        assert conn.execute("SELECT owner_entity_id FROM action_items WHERE id = ?",
                            (task,)).fetchone()[0] == keep
        aliases = {r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (keep,))}
        assert {"AcmeCorp", "Acme Corp"} <= aliases
        meta = json.loads(conn.execute("SELECT metadata_json FROM entities WHERE id = ?",
                                       (keep,)).fetchone()[0])
        assert meta["merged_from"][0]["type"] == "org"      # слід типу, що зник


def test_cross_type_merge_does_not_double_count_shared_meeting(db_path):
    """Спільна зустріч: ті самі згадки, на які дивляться два рядки графа.

    Однотипний merge складає mention_count — там дві РІЗНІ назви («Олена
    Петренко» і «Олена»), і сума осмислена. У кросс-типовому це одне написання,
    тож сума рахувала б ті самі слова двічі: беремо MAX.
    """
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "AcmeCorp")
    t = _insert_transcription(db_path, source_name="shared.mp3")
    _insert_meeting_entity(db_path, t, keep, mentions=4)
    _insert_meeting_entity(db_path, t, gone, mentions=6)

    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT mention_count FROM meeting_entities "
                            "WHERE entity_id = ? AND transcription_id = ?",
                            (keep, t)).fetchone()[0] == 6      # MAX, не 10
        assert conn.execute("SELECT meeting_count FROM entities WHERE id = ?",
                            (keep,)).fetchone()[0] == 1


def test_same_type_merge_still_sums_mentions(db_path):
    """Стара поведінка в межах типу не змінилась."""
    keep = _insert_entity(db_path, "person", "Олена Петренко")
    gone = _insert_entity(db_path, "person", "Олена")
    t = _insert_transcription(db_path, source_name="shared.mp3")
    _insert_meeting_entity(db_path, t, keep, mentions=4)
    _insert_meeting_entity(db_path, t, gone, mentions=6)

    entity_dedup.merge_entities(db_path, keep, gone)

    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT mention_count FROM meeting_entities "
                            "WHERE entity_id = ? AND transcription_id = ?",
                            (keep, t)).fetchone()[0] == 10


def test_merge_survives_the_next_enrichment(db_path):
    """Злиття має «прилипати»: наступний enrich не сміє відтворити злитий рядок.

    `_find_entity` шукав аліас із перевіркою типу, а `normalized_alias`
    унікальний глобально. Тому після злиття org→project написання належало
    project, org-гілка не знаходила нічого і створювала рядок НАНОВО — причому
    з нулем аліасів (усі `INSERT OR IGNORE` спотикались об глобальний UNIQUE).
    Тобто масовий прохід по 133 групах відкотився б сам, а воскреслі рядки
    стали б недосяжними для пошуку за псевдонімом.
    """
    from app.services import enrichment

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        keep = enrichment._upsert_entity(c, "project", "Acmecorp", aliases=["Акме"])
        gone = enrichment._upsert_entity(c, "org", "Acmecorp", aliases=["Acme Corp"])
        conn.commit()
    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        again = enrichment._upsert_entity(c, "org", "Acmecorp", aliases=["Acme Corp"])
        conn.commit()
        types = [r["type"] for r in c.execute("SELECT type FROM entities")]

    assert again == keep                       # знайшлась злита, а не створилась нова
    assert types == ["project"]


def test_person_and_non_person_never_merge(db_path):
    """«Ткачук» — і людина, і агентство. Прапорець на це права не дає."""
    person = _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")

    res = entity_dedup.merge_entities(db_path, person, org, allow_cross_type=True)
    back = entity_dedup.merge_entities(db_path, org, person, allow_cross_type=True)

    assert res["status"] == "error" and "person merge refused" in res["error"]
    assert back["status"] == "error"
    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2


def test_cross_type_with_different_names_still_sums(db_path):
    """MAX вирішує ЗБІГ НАПИСАННЯ, а не різниця типів.

    «Acmecorp» і «Acmecorp Ltd» — різні рядки, які Клод рахував окремо на тій
    самій зустрічі. Якби MAX вмикався від самого факту різних типів, згадки
    занижувались би, а вони тягнуть ранжування і фільтри «від N згадок».
    """
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "Acmecorp Ltd")
    t = _insert_transcription(db_path, source_name="shared.mp3")
    _insert_meeting_entity(db_path, t, keep, mentions=4)
    _insert_meeting_entity(db_path, t, gone, mentions=6)

    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT mention_count FROM meeting_entities "
                            "WHERE entity_id = ? AND transcription_id = ?",
                            (keep, t)).fetchone()[0] == 10


def test_merge_carries_link_provenance(db_path):
    """`source` їде разом зі звʼязком: інакше рядок стає привидом для tg_entities.

    Чистка телеграмних звʼязків іде запитом `DELETE … WHERE source = ?`, тож
    звʼязок, що втратив походження, переживає перебудову, а `stats()` зараховує
    його Клоду. В архіві, де 88% — Telegram, це не дрібниця.
    """
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "AcmeCorp")
    t = _insert_transcription(db_path, source_name="tg.mp3")
    with get_db_connection(db_path) as conn:
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, "
                     "mention_count, salience, source) VALUES (?, ?, 2, 0.4, 'telegram')",
                     (t, gone))
        conn.commit()

    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT source FROM meeting_entities WHERE entity_id = ?",
                            (keep,)).fetchone()[0] == "telegram"


def test_twins_finder_surfaces_what_the_flag_exists_for(db_path):
    """Кросс-типові пари має бути видно інструментом, а не лише руками в SQL."""
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "AcmeCorp")
    _insert_entity(db_path, "person", "Ткачук")
    _insert_entity(db_path, "org", "Ткачук")
    _insert_entity(db_path, "project", "Самотній")
    t = _insert_transcription(db_path, source_name="c.mp3")
    _insert_meeting_entity(db_path, t, keep)

    groups = entity_dedup.find_cross_type_twins(db_path)

    assert len(groups) == 1                                  # person-пара прихована
    assert groups[0]["keep"]["id"] == keep                   # keep — за звʼязками
    assert [m["id"] for m in groups[0]["merge"]] == [gone]
    assert len(entity_dedup.find_cross_type_twins(db_path, include_person=True)) == 2


def test_stickiness_does_not_swallow_pairs_left_unmerged(db_path):
    """Липкість працює лише там, де злиття справді було.

    Спокуслива правка — шукати аліас без перевірки типу взагалі. Тоді org
    «Ткачук» знайшов би людину Ткачук, і 8 пар, які власник свідомо лишив
    нерозділеними, схлопнулись би самі, без жодного merge.
    """
    from app.services import enrichment

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        person = enrichment._upsert_entity(c, "person", "Ткачук")
        conn.commit()

        found = enrichment._find_entity(c, "org", "ткачук")
        assert found is None                       # людина не віддається як org

        org = enrichment._upsert_entity(c, "org", "Ткачук")
        conn.commit()
        assert org != person
        assert c.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2


def test_keep_is_chosen_by_type_not_by_link_count(db_path):
    """Тип вирішує видимість, тож `topic` не може стати головним над `project`.

    `scope.resolve_scope` бере project|org|person і не бачить topic. Коли keep
    обирався за звʼязками, сім проєктів живого архіву поглинулись темами і
    зникли зі зрізу за проєктом — при тому, що звʼязки й так переїжджають на
    keep, тобто вибір за типом нічого не втрачає.
    """
    topic = _insert_entity(db_path, "topic", "task tracker")
    project = _insert_entity(db_path, "project", "Task Tracker")
    t1 = _insert_transcription(db_path, source_name="a.mp3")
    t2 = _insert_transcription(db_path, source_name="b.mp3")
    t3 = _insert_transcription(db_path, source_name="c.mp3")
    for t in (t1, t2, t3):
        _insert_meeting_entity(db_path, t, topic)      # у теми звʼязків більше
    _insert_meeting_entity(db_path, t1, project)

    group = entity_dedup.find_cross_type_twins(db_path)[0]

    assert group["keep"]["id"] == project
    assert [m["id"] for m in group["merge"]] == [topic]


def test_person_in_group_does_not_hide_the_mergeable_rest(db_path):
    """{person, org, project} під одним іменем: пару org+project видно."""
    _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")
    project = _insert_entity(db_path, "project", "Ткачук")
    _insert_meeting_entity(db_path, _insert_transcription(db_path, source_name="a.mp3"), project)

    groups = entity_dedup.find_cross_type_twins(db_path)

    assert len(groups) == 1
    assert groups[0]["keep"]["id"] == project
    assert [m["id"] for m in groups[0]["merge"]] == [org]


def test_fold_key_decides_max_the_same_way_the_finder_groups(db_path):
    """«Acme Corp» і «AcmeCorp» — одне написання для шукача, отже й MAX для згадок."""
    keep = _insert_entity(db_path, "project", "Acme Corp")
    gone = _insert_entity(db_path, "org", "AcmeCorp")
    t = _insert_transcription(db_path, source_name="shared.mp3")
    _insert_meeting_entity(db_path, t, keep, mentions=4)
    _insert_meeting_entity(db_path, t, gone, mentions=6)

    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT mention_count FROM meeting_entities "
                            "WHERE entity_id = ?", (keep,)).fetchone()[0] == 6


def test_claude_link_keeps_its_provenance(db_path):
    """`source IS NULL` — це «звʼязок від Клода», а не «невідомо».

    `tg_entities` такі рядки свідомо не чіпає як надійніші. Якби злиття
    перезаписало NULL на 'telegram', наступна перебудова видалила б рядок і
    відтворила голим — без salience, ролі й накопичених згадок.
    """
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "AcmeCorp")
    t = _insert_transcription(db_path, source_name="shared.mp3")
    _insert_meeting_entity(db_path, t, keep, mentions=3, salience=0.9,
                           role_in_meeting="обговорювали")
    with get_db_connection(db_path) as conn:
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, "
                     "mention_count, source) VALUES (?, ?, 1, 'telegram')", (t, gone))
        conn.commit()

    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT source, salience, role_in_meeting FROM meeting_entities "
                           "WHERE entity_id = ?", (keep,)).fetchone()
    assert row["source"] is None
    assert row["salience"] == 0.9 and row["role_in_meeting"] == "обговорювали"


def test_stickiness_matches_the_spelling_not_just_the_type(db_path):
    """Поглинувши один org, keep не має привласнювати БУДЬ-ЯКИЙ org зі своїх аліасів."""
    from app.services import enrichment

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        keep = enrichment._upsert_entity(c, "project", "Acmecorp", aliases=["BH"])
        gone = enrichment._upsert_entity(c, "org", "Acmecorp Ltd")
        conn.commit()
    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert enrichment._find_entity(c, "org", "bh") is None            # чужий org
        assert enrichment._find_entity(c, "org", "acmecorp ltd") == keep  # поглинутий


# ============================================================
# Split — зворотна беда: в одному рядку сидять різні люди
# ============================================================

def _mixed_person(db_path: str):
    """Рядок «Дмитро», у якому сидять двоє: Дмитро Лебідь і М.Верес.

    Повертає (entity_id, {ключ: transcription_id}).
    """
    eid = _insert_entity(db_path, "person", "Дмитро", role="юрист")
    for a in ("Дмитро", "Дмитро Лебідь", "М.Верес", "Дима"):
        _insert_alias(db_path, eid, a)
    tids = {
        "moved": _insert_transcription(db_path, transcript_text="М.Верес підтвердив бюджет."),
        "both": _insert_transcription(
            db_path, transcript_text="Дмитро Лебідь і М.Верес обговорили ставку."),
        "neither": _insert_transcription(db_path, transcript_text="Він зайде пізніше."),
    }
    _insert_meeting_entity(db_path, tids["moved"], eid, mentions=3, salience=0.7,
                           role_in_meeting="вирішував")
    _insert_meeting_entity(db_path, tids["both"], eid, mentions=5, salience=0.4)
    _insert_meeting_entity(db_path, tids["neither"], eid, mentions=1)
    return eid, tids


def test_inspect_shows_which_spelling_named_itself(db_path):
    """Докази для рішення: скільки задач назвали саме це написання і де воно в тексті."""
    eid, tids = _mixed_person(db_path)
    _insert_action_item(db_path, tids["moved"], "звірити ставку", eid, owner_name="М.Верес")
    _insert_action_item(db_path, tids["both"], "готувати звіт", eid, owner_name="Дмитро Лебідь")
    _insert_action_item(db_path, tids["neither"], "подзвонити", eid, owner_name="Дмитро")
    _insert_action_item(db_path, tids["both"], "спільна задача", eid,
                        owner_name="Дмитро Лебідь, М.Верес")

    info = entity_dedup.inspect_entity(db_path, eid)

    assert info["status"] == "ok"
    assert info["tasks_total"] == 4 and info["links_total"] == 3
    assert info["tasks_ambiguous_owner"] == 1          # «X, Y» — доказом не є
    assert info["links_short_name_only"] == 1          # текст без жодного написання
    per = {a["alias"]: a for a in info["aliases"]}
    assert per["М.Верес"]["tasks"] == 1 and per["М.Верес"]["texts"] == 2
    assert per["Дмитро Лебідь"]["tasks"] == 1 and per["Дмитро Лебідь"]["texts"] == 1
    assert per["Дмитро"]["tasks"] == 1


def test_inspect_not_found(db_path):
    assert entity_dedup.inspect_entity(db_path, 999)["status"] == "not_found"


def test_split_is_a_dry_run_until_asked(db_path):
    """Спершу числа, потім правка: без --apply нічого не змінюється."""
    eid, tids = _mixed_person(db_path)
    _insert_action_item(db_path, tids["moved"], "звірити", eid, owner_name="М.Верес")

    res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], new_name="М. Верес")

    assert res["status"] == "ok" and res["dry_run"] is True
    assert res["tasks_moved"] == 1
    assert res["links_moved"] == 1 and res["links_copied"] == 1 and res["links_left"] == 1
    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM entity_aliases "
                            "WHERE entity_id = ?", (eid,)).fetchone()["c"] == 4


def test_split_moves_only_what_the_text_proves(db_path):
    """Переїжджає доказане: задача за owner_name, звʼязок, де видно лише відчеплене.

    Спільний текст (видно обох) КОПІЮЄТЬСЯ, а не рухається; текст, де жодного
    довгого написання немає, лишається джерелу — довести нічим.
    """
    eid, tids = _mixed_person(db_path)
    mine = _insert_action_item(db_path, tids["moved"], "звірити", eid, owner_name="М.Верес")
    theirs = _insert_action_item(db_path, tids["both"], "звіт", eid, owner_name="Дмитро Лебідь")

    res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], new_name="М. Верес",
                                    dry_run=False)
    assert res["status"] == "ok"
    new_id = res["target"]["id"]

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        links = {r["transcription_id"]: r for r in c.execute(
            "SELECT * FROM meeting_entities WHERE entity_id = ?", (new_id,))}
        src_links = {r["transcription_id"] for r in c.execute(
            "SELECT transcription_id FROM meeting_entities WHERE entity_id = ?", (eid,))}
        owners = {r["id"]: r["owner_entity_id"] for r in c.execute(
            "SELECT id, owner_entity_id FROM action_items")}
        alias_owner = {r["alias"]: r["entity_id"] for r in c.execute(
            "SELECT alias, entity_id FROM entity_aliases")}
        agg = {r["id"]: (r["mention_count"], r["meeting_count"]) for r in c.execute(
            "SELECT id, mention_count, meeting_count FROM entities")}

    assert set(links) == {tids["moved"], tids["both"]}
    assert tids["moved"] not in src_links                      # переїхав
    assert {tids["both"], tids["neither"]} <= src_links        # копія + недоказане
    # Перенесений рядок несе свої числа; копія рахує ВЛАСНІ згадки, а не чужі 5.
    assert links[tids["moved"]]["mention_count"] == 3
    assert links[tids["moved"]]["salience"] == 0.7
    assert links[tids["moved"]]["role_in_meeting"] == "вирішував"
    assert links[tids["both"]]["mention_count"] == 1
    assert owners[mine] == new_id and owners[theirs] == eid
    assert alias_owner["М.Верес"] == new_id
    assert alias_owner["Дмитро Лебідь"] == eid
    assert agg[new_id] == (4, 2)                               # 3 + 1, дві зустрічі
    # У спільній зустрічі джерело віддало ту саму одну згадку, яку забрала копія:
    # 5 - 1 = 4, плюс 1 у недоказаній. Сума архіву стала (9), а не 10 — згадку не
    # можна порахувати двічі так само, як не можна зарахувати її двом рядкам.
    assert agg[eid] == (5, 2)


def test_the_shared_first_name_does_not_block_the_split(db_path):
    """«Слава Гринчук» — це доказ Гринчука, а не присутності Слави.

    Коротке імʼя є частиною ПОВНОГО імені другої людини, тож без змагання за
    позицію кожен звʼязок виглядав би «згадані обидва» — і рядок, що цілком
    належить Гринчуку, лишався б висіти на Славі.
    """
    slava = _insert_entity(db_path, "person", "Слава")
    for a in ("Слава", "Слава Гринчук", "Гринчук", "Слава Верес"):
        _insert_alias(db_path, slava, a)
    only_balbek = _insert_transcription(
        db_path, transcript_text="Слава Гринчук показав макет фасаду.")
    truly_both = _insert_transcription(
        db_path, transcript_text="Гринчук здав макет, а Слава Верес порахував модель.")
    _insert_meeting_entity(db_path, only_balbek, slava, mentions=4)
    _insert_meeting_entity(db_path, truly_both, slava, mentions=6)

    res = entity_dedup.split_entity(db_path, slava, ["Слава Гринчук", "Гринчук"],
                                    new_name="Слава Гринчук", dry_run=False)

    assert res["status"] == "ok"
    assert res["links_moved"] == 1 and res["links_copied"] == 1
    with get_db_connection(db_path) as conn:
        src = {r["transcription_id"] for r in conn.execute(
            "SELECT transcription_id FROM meeting_entities WHERE entity_id = ?", (slava,))}
    assert src == {truly_both}


def test_the_full_name_that_stays_keeps_its_own_meeting(db_path):
    """Дзеркальний випадок: відчеплюємо КОРОТКЕ імʼя, а повне лишається.

    «Юлія Бондаренка підтвердила бюджет» — доказ Бондаренкої, і зустріч має
    лишитись їй. Затирання відчеплюваного «Юлія» (перший підхід) стерло б і
    «Юлія Бондаренка»: доказ того, хто лишається, зник би, і чужа зустріч
    переїхала б цілком. Тому сторони змагаються за позицію, а не затирають.
    """
    yulia = _insert_entity(db_path, "person", "Юлія Бондаренка")
    for a in ("Юлія Бондаренка", "Юлія"):
        _insert_alias(db_path, yulia, a)
    hers = _insert_transcription(
        db_path, transcript_text="Юлія Бондаренка підтвердила бюджет.")
    shared = _insert_transcription(
        db_path, transcript_text="Юлія Бондаренка здала звіт, а Юлія Кравець — склад.")
    _insert_meeting_entity(db_path, hers, yulia, mentions=5)
    _insert_meeting_entity(db_path, shared, yulia, mentions=6)

    res = entity_dedup.split_entity(db_path, yulia, ["Юлія"], new_name="Юлія Кравець",
                                    dry_run=False)

    assert res["status"] == "ok"
    assert res["links_moved"] == 0 and res["links_copied"] == 1
    new_id = res["target"]["id"]
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        src = {r["transcription_id"] for r in c.execute(
            "SELECT transcription_id FROM meeting_entities WHERE entity_id = ?", (yulia,))}
        copies = {r["transcription_id"]: r["mention_count"] for r in c.execute(
            "SELECT transcription_id, mention_count FROM meeting_entities "
            "WHERE entity_id = ?", (new_id,))}
    assert src == {hers, shared}          # обидві лишились джерелу
    assert copies == {shared: 1}          # копії — одна ВЛАСНА згадка, а не чужі 6


def test_inspect_counts_the_canonical_name_without_an_alias_row(db_path):
    """Канонічне імʼя — теж написання, і рядка в `entity_aliases` для нього може не бути.

    `_upsert_entity` заводить його через INSERT OR IGNORE проти глобального
    UNIQUE, тож коли написання вже забрала чужа сутність, аліас не завівся. Без
    нього власна задача виглядала б як «власник поза аліасами», хоч split усе
    одно рахує канонічне серед тих, хто лишається.
    """
    yulia = _insert_entity(db_path, "person", "Юлія Бондаренка")
    _insert_alias(db_path, yulia, "Юлія")          # рядка для канонічного немає
    tid = _insert_transcription(
        db_path, transcript_text="Юлія Бондаренка підтвердила бюджет.")
    _insert_meeting_entity(db_path, tid, yulia, mentions=3)
    _insert_action_item(db_path, tid, "звірити кошторис", yulia,
                        owner_name="Юлія Бондаренка")

    info = entity_dedup.inspect_entity(db_path, yulia)
    per = {a["alias"]: a for a in info["aliases"]}

    assert per["Юлія Бондаренка"]["tasks"] == 1
    assert per["Юлія Бондаренка"]["texts"] == 1
    assert per["Юлія"]["texts"] == 0               # згадку забрало повне імʼя
    assert info["tasks_unmatched_owner"] == []
    assert info["links_short_name_only"] == 0


def test_short_alias_inside_a_longer_word_is_not_evidence(db_path):
    """Аліас «Слав» сидить усередині «Ярослав» — межа слова спереду це відсікає."""
    eid = _insert_entity(db_path, "person", "Слава")
    for a in ("Слава", "Слав", "Гринчук"):
        _insert_alias(db_path, eid, a)
    tid = _insert_transcription(db_path, transcript_text="Гринчук і Ярослав зайдуть завтра.")
    _insert_meeting_entity(db_path, tid, eid, mentions=2)

    info = entity_dedup.inspect_entity(db_path, eid)
    per = {a["alias"]: a for a in info["aliases"]}
    assert per["Слав"]["texts"] == 0          # «Ярослав» доказом не є
    assert per["Гринчук"]["texts"] == 1

    # А відмінок — є: «Гринчука» починається з написання «Гринчук».
    tid2 = _insert_transcription(db_path, transcript_text="Питання до Гринчука закрите.")
    _insert_meeting_entity(db_path, tid2, eid, mentions=1)
    per2 = {a["alias"]: a for a in entity_dedup.inspect_entity(db_path, eid)["aliases"]}
    assert per2["Гринчук"]["texts"] == 2


def test_split_can_glue_a_stranger_to_the_person_who_owns_the_name(db_path):
    """`Stepan` серед аліасів Слави — це Степан; адресат може бути існуючим рядком."""
    slava = _insert_entity(db_path, "person", "Слава")
    for a in ("Слава", "Слава Верес", "Stepan"):
        _insert_alias(db_path, slava, a)
    stepan = _insert_entity(db_path, "person", "Степан")
    _insert_alias(db_path, stepan, "Степан")
    tid = _insert_transcription(db_path, transcript_text="Stepan прислав кошторис.")
    _insert_meeting_entity(db_path, tid, slava, mentions=2)
    task = _insert_action_item(db_path, tid, "кошторис", slava, owner_name="Stepan")

    res = entity_dedup.split_entity(db_path, slava, ["Stepan"], target_id=stepan,
                                    dry_run=False)
    assert res["status"] == "ok"

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert c.execute("SELECT entity_id FROM entity_aliases WHERE alias = 'Stepan'"
                         ).fetchone()["entity_id"] == stepan
        assert c.execute("SELECT owner_entity_id FROM action_items WHERE id = ?",
                         (task,)).fetchone()["owner_entity_id"] == stepan
        assert c.execute("SELECT COUNT(*) c FROM meeting_entities WHERE entity_id = ?",
                         (slava,)).fetchone()["c"] == 0
        meta = json.loads(c.execute("SELECT metadata_json FROM entities WHERE id = ?",
                                    (stepan,)).fetchone()["metadata_json"])
    assert meta["split_from"][0]["id"] == slava
    assert meta["split_from"][0]["aliases"] == ["Stepan"]


def test_split_target_of_another_type_is_refused(db_path):
    """Імʼя людини, поїхавши в project, зникає з person-індексів — та сама межа, що в merge."""
    person = _insert_entity(db_path, "person", "Джейсон")
    _insert_alias(db_path, person, "Джейсон")
    _insert_alias(db_path, person, "Генрі")
    proj = _insert_entity(db_path, "project", "Генрі")

    res = entity_dedup.split_entity(db_path, person, ["Генрі"], target_id=proj, dry_run=False)
    assert res["status"] == "error" and "type mismatch" in res["error"]


def test_drop_takes_garbage_out_and_leaves_no_owner(db_path):
    """`JSON` серед аліасів Джейсона — не людина. Задача без власника честніша за чужого."""
    from app.services import enrichment

    eid = _insert_entity(db_path, "person", "Джейсон")
    for a in ("Джейсон", "JSON"):
        _insert_alias(db_path, eid, a)
    junk = _insert_transcription(db_path, transcript_text="Відповідь приходить у JSON форматі.")
    real = _insert_transcription(db_path, transcript_text="Джейсон дав контакт.")
    _insert_meeting_entity(db_path, junk, eid, mentions=4)
    _insert_meeting_entity(db_path, real, eid, mentions=2)
    task = _insert_action_item(db_path, junk, "розібрати формат", eid, owner_name="JSON")

    res = entity_dedup.split_entity(db_path, eid, ["JSON"], drop=True, dry_run=False)
    assert res["status"] == "ok" and res["links_moved"] == 1

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert c.execute("SELECT COUNT(*) c FROM entity_aliases WHERE alias = 'JSON'"
                         ).fetchone()["c"] == 0
        assert c.execute("SELECT owner_entity_id FROM action_items WHERE id = ?",
                         (task,)).fetchone()["owner_entity_id"] is None
        tids = {r["transcription_id"] for r in c.execute(
            "SELECT transcription_id FROM meeting_entities WHERE entity_id = ?", (eid,))}
        assert tids == {real}
        assert c.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 1
        # Смітник не має воскресати: за 'json' сутності більше немає взагалі.
        assert enrichment._find_entity(c, "person", "json") is None


def test_split_does_not_let_the_merge_trail_pull_the_name_back(db_path):
    """Слід злиття віддавав джерело за написанням — після відчеплення не має.

    Інакше наступний enrich знову привʼязав би відчеплене імʼя до джерела: split
    «не прилипав» би так само, як колись не прилипав merge.
    """
    from app.services import enrichment

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        keep = enrichment._upsert_entity(c, "person", "Дмитро", aliases=["Дмитро Лебідь"])
        gone = enrichment._upsert_entity(c, "person", "М.Верес")
        conn.commit()
    entity_dedup.merge_entities(db_path, keep, gone)
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert enrichment._find_entity(c, "person", "м.верес") == keep   # прилипло

    entity_dedup.split_entity(db_path, keep, ["М.Верес"], drop=True, dry_run=False)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert enrichment._find_entity(c, "person", "м.верес") is None
        meta = json.loads(c.execute("SELECT metadata_json FROM entities WHERE id = ?",
                                    (keep,)).fetchone()["metadata_json"])
    assert "merged_from" not in meta          # від запису не лишилось написань
    assert meta["split_off"][0]["dropped"] is True


def test_split_refuses_to_rename_instead_of_splitting(db_path):
    """Канонічне імʼя і «всі аліаси» — це перейменування, інша операція."""
    eid, _ = _mixed_person(db_path)

    canon = entity_dedup.split_entity(db_path, eid, ["Дмитро"], new_name="Дмитро Лебідь")
    assert canon["status"] == "error" and "канонічне" in canon["error"]

    everything = entity_dedup.split_entity(
        db_path, eid, ["Дмитро", "Дмитро Лебідь", "М.Верес", "Дима"], new_name="Хтось")
    assert everything["status"] == "error"


def test_split_guards_on_destination_and_unknown_spelling(db_path):
    eid, _ = _mixed_person(db_path)

    assert entity_dedup.split_entity(db_path, eid, [])["status"] == "error"
    assert entity_dedup.split_entity(db_path, eid, ["М.Верес"])["status"] == "error"
    assert entity_dedup.split_entity(db_path, eid, ["М.Верес"], new_name="X",
                                     drop=True)["status"] == "error"
    assert entity_dedup.split_entity(db_path, eid, ["М.Верес"],
                                     target_id=eid)["status"] == "error"
    assert entity_dedup.split_entity(db_path, 999, ["М.Верес"],
                                     drop=True)["status"] == "not_found"
    stranger = entity_dedup.split_entity(db_path, eid, ["Кого тут немає"], drop=True)
    assert stranger["status"] == "error" and "немає в аліасах" in stranger["error"]


def test_split_into_an_existing_name_points_at_the_row_that_holds_it(db_path):
    """UNIQUE(type, normalized_name): нову сутність із зайнятим імʼям не створюємо.

    І прикидка мусить казати те саме, що застосування: інакше вона обіцяє split,
    який `--apply` відхилить, а прикидка — єдина поверхня, за якою вирішують.
    """
    eid, _ = _mixed_person(db_path)
    other = _insert_entity(db_path, "person", "М. Верес")

    for dry in (True, False):
        res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], new_name="М. Верес",
                                        dry_run=dry)
        assert res["status"] == "error" and f"#{other}" in res["error"]
    assert entity_dedup.split_entity(db_path, eid, ["М.Верес"],
                                     new_name="   ")["status"] == "error"


def test_split_works_when_the_canonical_name_has_no_alias_row(db_path):
    """Порожній `staying` — не перейменування: канонічне імʼя лишається завжди.

    Рядка в `entity_aliases` для канонічного може не бути (INSERT OR IGNORE проти
    глобального UNIQUE програв гонку чужій сутності) — і це рівно той рядок,
    задля якого split існує. Гвардія на «відчеплюються ВСІ аліаси» відмовляла б
    саме тут, і ні в якому іншому випадку.
    """
    eid = _insert_entity(db_path, "person", "Дмитро")
    _insert_alias(db_path, eid, "М.Верес")          # канонічного рядка немає
    tid = _insert_transcription(db_path, transcript_text="М.Верес підтвердив бюджет.")
    _insert_meeting_entity(db_path, tid, eid, mentions=2)

    res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], new_name="М. Верес",
                                    dry_run=False)

    assert res["status"] == "ok" and res["links_moved"] == 1
    with get_db_connection(db_path) as conn:
        owner = conn.execute("SELECT entity_id FROM entity_aliases "
                             "WHERE alias = 'М.Верес'").fetchone()["entity_id"]
        canon = conn.execute("SELECT canonical_name FROM entities "
                             "WHERE id = ?", (eid,)).fetchone()["canonical_name"]
    assert owner == res["target"]["id"]
    assert canon == "Дмитро"                          # джерело лишилось собою


def test_drop_does_not_report_links_it_leaves_behind(db_path):
    """`drop` копій не робить — спільний текст лишається джерелу і має бути в links_left.

    Інакше прикидка показує «нічого не лишиться» там, де зникає лише частина, а
    вона — єдина поверхня, за якою оператор ухвалює рішення.
    """
    eid, tids = _mixed_person(db_path)

    res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], drop=True)

    assert res["links_moved"] == 1
    assert res["links_copied"] == 0                   # у drop копій не буває
    assert res["links_shared_kept"] == 1              # спільний текст лишається
    assert res["links_left"] == 2                     # спільний + недоказаний

    entity_dedup.split_entity(db_path, eid, ["М.Верес"], drop=True, dry_run=False)
    with get_db_connection(db_path) as conn:
        left = {r["transcription_id"] for r in conn.execute(
            "SELECT transcription_id FROM meeting_entities WHERE entity_id = ?", (eid,))}
    assert left == {tids["both"], tids["neither"]}     # рівно те, що обіцяв звіт


def test_split_sums_mentions_when_the_target_already_saw_that_meeting(db_path):
    """У адресата вже є рядок на цю зустріч — згадки складаються, а не беруть MAX.

    Те саме правило, що й у `merge_entities`: MAX лише коли написання ОДНЕ й те
    саме. Тут адресат заробив свій рядок власним імʼям, а приходять до нього
    згадки інших слів того ж тексту.
    """
    eid, tids = _mixed_person(db_path)
    other = _insert_entity(db_path, "person", "М. Верес")
    _insert_meeting_entity(db_path, tids["moved"], other, mentions=4)

    res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], target_id=other,
                                    dry_run=False)

    assert res["status"] == "ok"
    with get_db_connection(db_path) as conn:
        got = conn.execute("SELECT mention_count FROM meeting_entities WHERE entity_id = ? "
                           "AND transcription_id = ?",
                           (other, tids["moved"])).fetchone()["mention_count"]
    assert got == 7                                   # 4 своїх + 3 перенесених


def test_split_reaches_meetings_in_the_recycle_bin(db_path):
    """Мʼяко видалену зустріч split теж розводить — її ще можуть відновити.

    `POST /api/transcription/<id>/restore` повертає її в життя; якби split її
    обійшов, після відновлення задача лишилась би за старим власником, а
    звʼязок — без написання, що його тримало. У числах вона видна окремо: в UI
    цієї зустрічі зараз немає.
    """
    eid = _insert_entity(db_path, "person", "Дмитро")
    for a in ("Дмитро", "М.Верес"):
        _insert_alias(db_path, eid, a)
    gone = _insert_transcription(db_path, transcript_text="М.Верес підтвердив бюджет.",
                                 deleted_at="2026-08-01 10:00:00")
    _insert_meeting_entity(db_path, gone, eid, mentions=2)
    task = _insert_action_item(db_path, gone, "звірити", eid, owner_name="М.Верес")

    res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], new_name="М. Верес",
                                    dry_run=False)

    assert res["status"] == "ok"
    assert res["from_deleted_meetings"] == {"tasks": 1, "links": 1}
    new_id = res["target"]["id"]
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        owner = c.execute("SELECT owner_entity_id FROM action_items WHERE id = ?",
                          (task,)).fetchone()["owner_entity_id"]
        link = c.execute("SELECT entity_id FROM meeting_entities WHERE transcription_id = ?",
                         (gone,)).fetchone()["entity_id"]
    assert owner == new_id and link == new_id


def test_split_matches_the_spelling_the_way_the_finder_groups_it(db_path):
    """Написання з CLI звіряється ключем згортання: крапки, пробіли й регістр не мають значення.

    Інакше відчеплення не спрацювало б саме там, де воно потрібне: «М.Верес»,
    «М. Верес» і «м.верес » — те саме написання, набране по-різному.
    """
    eid, _ = _mixed_person(db_path)

    res = entity_dedup.split_entity(db_path, eid, [" м. верес "], drop=True, dry_run=False)

    assert res["status"] == "ok" and res["aliases_moved"] == ["М.Верес"]
    with get_db_connection(db_path) as conn:
        rows = {r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (eid,))}
    assert rows == {"Дмитро", "Дмитро Лебідь", "Дима"}


# ============================================================
# allow_person: заборона знімається лише руками і лише на безпечному боці
# ============================================================

def test_person_merge_still_refused_without_the_flag(db_path):
    """Дефолт не змінився: агенцію на прізвище власника треба спершу подивитись."""
    person = _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")

    res = entity_dedup.merge_entities(db_path, person, org, allow_cross_type=True)

    assert res["status"] == "error" and "person merge refused" in res["error"]


def test_allow_person_merges_and_names_what_entered_the_person_index(db_path):
    """Написання поглинутої не-людини стають ключами person-індексів — тож їх видно у відповіді."""
    person = _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")
    _insert_alias(db_path, org, "Федеріва")

    res = entity_dedup.merge_entities(db_path, person, org, allow_cross_type=True,
                                      allow_person=True)

    assert res["status"] == "merged"
    assert set(res["person_index_added"]) == {"Ткачук", "Федеріва"}
    with get_db_connection(db_path) as conn:
        aliases = {r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (person,))}
    assert "Федеріва" in aliases


def test_allow_person_still_refuses_when_the_person_row_holds_owners(db_path):
    """Зникає людський рядок — задачі лишились би без людини. Прапорець це НЕ дозволяє."""
    org = _insert_entity(db_path, "org", "Вербич")
    person = _insert_entity(db_path, "person", "Вербич")
    tid = _insert_transcription(db_path)
    _insert_action_item(db_path, tid, "Підписати меморандум", owner_entity_id=person)

    res = entity_dedup.merge_entities(db_path, org, person, allow_cross_type=True,
                                      allow_person=True)

    assert res["status"] == "error" and "задач 1" in res["error"]
    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM entities WHERE id = ?",
                            (person,)).fetchone()["c"] == 1


# ============================================================
# alias: приписати написання, під яким річ знає інший шар
# ============================================================

def test_alias_is_a_dry_run_until_asked(db_path):
    eid = _insert_entity(db_path, "person", "Дмитро Лебідь")

    res = entity_dedup.add_aliases(db_path, eid, ["Dmytro Lebid"])

    assert res["status"] == "ok" and res["added"] == ["Dmytro Lebid"]
    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM entity_aliases").fetchone()["c"] == 0


def test_alias_does_not_take_a_spelling_that_belongs_to_someone_else(db_path):
    """`normalized_alias` UNIQUE глобально — чуже написання не забираємо мовчки."""
    mine = _insert_entity(db_path, "person", "Дмитро Лебідь")
    theirs = _insert_entity(db_path, "person", "Dmytro Sh")
    _insert_alias(db_path, theirs, "Dmytro S")

    res = entity_dedup.add_aliases(db_path, mine, ["Dmytro S", "Dmytro Lebid"],
                                   dry_run=False)

    assert res["added"] == ["Dmytro Lebid"]
    assert res["taken"] == [{"alias": "Dmytro S", "entity_id": theirs,
                             "name": "Dmytro Sh", "type": "person"}]
    with get_db_connection(db_path) as conn:
        owner = conn.execute("SELECT entity_id FROM entity_aliases WHERE alias = ?",
                             ("Dmytro S",)).fetchone()["entity_id"]
    assert owner == theirs


def test_alias_is_idempotent_and_reports_what_was_already_there(db_path):
    eid = _insert_entity(db_path, "person", "Слава")
    entity_dedup.add_aliases(db_path, eid, ["Veres Viacheslav"], dry_run=False)

    res = entity_dedup.add_aliases(db_path, eid, ["Veres Viacheslav"], dry_run=False)

    assert res["added"] == [] and res["already"] == ["Veres Viacheslav"]


def test_alias_sees_a_spelling_that_is_someone_elses_canonical_name(db_path):
    """Написання може вже жити НЕ в аліасах: `split --to-name` заводить рядок без alias-рядка.

    Приписаний поверх такого канонічного імені аліас — мертвий вантаж
    (`enrichment._find_entity` шукає спершу по канонічному), а `relink` після
    нього ще й перевісив би задачі на ту чужу сутність.
    """
    mine = _insert_entity(db_path, "person", "Дмитро Лебідь")
    theirs = _insert_entity(db_path, "person", "Dmytro Lebid")   # без рядка в aliases

    res = entity_dedup.add_aliases(db_path, mine, ["Dmytro Lebid"], dry_run=False)

    assert res["added"] == []
    assert res["taken"] == [{"alias": "Dmytro Lebid", "entity_id": theirs,
                             "name": "Dmytro Lebid", "type": "person"}]


def test_allow_person_refuses_when_a_first_name_twin_would_inherit_the_future(db_path):
    """Порожній зараз рядок ще не значить, що шкоди не буде потім.

    Імʼя виходить із person-індексу, і наступна задача «Вербич Петренко» піде
    фолбеком по першому токену — до однослівного тезки «Вербич».
    """
    org = _insert_entity(db_path, "org", "Вербич")
    gone = _insert_entity(db_path, "person", "Вербич Петренко")
    _insert_entity(db_path, "person", "Вербич")     # тезка, якому дістанеться фолбек

    res = entity_dedup.merge_entities(db_path, org, gone, allow_cross_type=True,
                                      allow_person=True)

    assert res["status"] == "error" and "фолбек по першому токену" in res["error"]


def test_allow_person_reports_only_spellings_that_really_landed_on_keep(db_path):
    """Написання могло лишитись у третьої сутності — тоді звіт про «увійшло» був би неправдою."""
    person = _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")          # без власного alias-рядка
    _insert_alias(db_path, org, "Федеріва")
    third = _insert_entity(db_path, "person", "Хтось")
    _insert_alias(db_path, third, "Ткачук")   # написання вже зайняте третьою

    res = entity_dedup.merge_entities(db_path, person, org, allow_cross_type=True,
                                      allow_person=True)

    assert res["status"] == "merged"
    assert res["person_index_added"] == ["Федеріва"], \
        "«Ткачук» лишилось у третьої сутності — звітувати про нього не можна"
    with get_db_connection(db_path) as conn:
        owner = conn.execute("SELECT entity_id FROM entity_aliases WHERE alias = ?",
                             ("Ткачук",)).fetchone()["entity_id"]
    assert owner == third


# ============================================================
# Написання, які насправді є загальними словами
# ============================================================

def test_junk_alias_is_found_by_lowercase_usage(db_path):
    """«документ» як аліас проєкту: корпус пише це слово з малої літери."""
    eid = _insert_entity(db_path, "project", "Стратегія розвитку області")
    _insert_alias(db_path, eid, "документ")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="надішли документ на пошту")

    found = entity_dedup.find_junk_aliases(db_path)

    assert [r["alias"] for r in found] == ["документ"]
    assert found[0]["entity_id"] == eid and found[0]["lower"] == 6
    assert found[0]["caps_mid_sentence"] == 0


def test_real_name_survives_the_audit(db_path):
    """Власну назву пишуть з великої і в середині речення — це не сміття."""
    eid = _insert_entity(db_path, "project", "Ковальчука")
    _insert_alias(db_path, eid, "Ковальчука")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="сьогодні Ковальчука підтвердила бюджет")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_sentence_start_is_not_evidence_of_a_name(db_path):
    """Велика літера після крапки — пунктуація; інакше сміття виправдовує себе."""
    eid = _insert_entity(db_path, "project", "Стратегія розвитку області")
    _insert_alias(db_path, eid, "документ")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="Все готово. Документ надіслано.")
        _insert_transcription(db_path, transcript_text="надішли документ на пошту")

    found = entity_dedup.find_junk_aliases(db_path)
    assert found and found[0]["caps_mid_sentence"] == 0


def test_uppercase_headings_do_not_prove_a_name(db_path):
    """ШАПКА ДОГОВОРУ дає велику літеру всім словам підряд — і не доводить нічого.

    А от ОДНЕ слово капсом усередині звичайного речення рахується доказом, бо
    так само його рахує `find_mentions`: розійшовшись тут, аудит починав радити
    зняти написання, яке саме зараз дає звʼязки.
    """
    eid = _insert_entity(db_path, "project", "Стратегія розвитку області")
    _insert_alias(db_path, eid, "договір")
    for _ in range(6):
        _insert_transcription(db_path,
                              transcript_text="ЦЕЙ ДОГОВІР УКЛАДЕНО МІЖ СТОРОНАМИ")
        _insert_transcription(db_path, transcript_text="підпиши договір будь ласка")

    found = entity_dedup.find_junk_aliases(db_path)
    assert found and found[0]["caps_mid_sentence"] == 0


def test_canonical_name_is_flagged_separately(db_path):
    """Канонічне імʼя лежить і в аліасах, але зняти його аліасом — півсправи:
    рядок лишиться в графі під тим самим загальним словом."""
    eid = _insert_entity(db_path, "org", "Фонд")
    _insert_alias(db_path, eid, "Фонд")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="кошти надійшли від фонд партнерів")

    found = entity_dedup.find_junk_aliases(db_path)
    assert found and found[0]["is_canonical"] is True


def test_rare_words_are_not_judged(db_path):
    """Двох згадок мало, щоб назвати написання сміттям."""
    eid = _insert_entity(db_path, "project", "Стратегія розвитку області")
    _insert_alias(db_path, eid, "документ")
    _insert_transcription(db_path, transcript_text="надішли документ на пошту")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_telegram_handles_are_not_judged_by_capitalisation(db_path):
    """«@jbondarenko» пишеться з малої за домовленістю, а не тому, що це слово."""
    eid = _insert_entity(db_path, "person", "jbondarenko")
    _insert_alias(db_path, eid, "@jbondarenko")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="це питання до @jbondarenko сьогодні")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_all_caps_name_is_not_called_junk(db_path):
    """Регресія: аудит радив зняти написання, яке саме зараз дає звʼязки.

    `find_mentions` вважає «ACMECORP» у звичайному реченні назвою і ставить
    звʼязок, а аудит ALL-CAPS не рахував узагалі — і показував caps=0 при
    живих звʼязках, тобто пропонував знищити правильні дані.
    """
    eid = _insert_entity(db_path, "project", "Acmecorp")
    _insert_alias(db_path, eid, "Acmecorp")
    for _ in range(6):
        _insert_transcription(db_path,
                              transcript_text="рахунок від ACMECORP надійшов сьогодні")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_shouting_header_still_proves_nothing(db_path):
    """Але в суцільній шапці велика літера доказом не стає — там усі слова такі."""
    eid = _insert_entity(db_path, "project", "Тексти")
    _insert_alias(db_path, eid, "Тексти")
    for _ in range(6):
        _insert_transcription(db_path,
                              transcript_text="ЩОДО ТЕКСТИ СТАТЕЙ У РОЗДІЛ WHAT WE DO\n"
                                              "далі йдуть тексти статей у розділі")

    found = entity_dedup.find_junk_aliases(db_path)
    assert found and found[0]["caps_mid_sentence"] == 0


def test_canonical_name_without_alias_row_is_audited(db_path):
    """Своя alias-строка може не завестись (глобальний UNIQUE), а звʼязки — є."""
    other = _insert_entity(db_path, "org", "Нуль")
    _insert_alias(db_path, other, "Zero")          # написання забрала чужа сутність
    eid = _insert_entity(db_path, "org", "Zero")   # тут alias-рядка вже не буде
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="залишок zero на рахунку")

    found = entity_dedup.find_junk_aliases(db_path)
    assert eid in {r["entity_id"] for r in found}


def test_handle_without_at_sign_is_still_a_handle(db_path):
    """Канонічне імʼя нікнейма приїжджає без «@» — гард має ловити і його."""
    eid = _insert_entity(db_path, "person", "jbondarenko")
    _insert_alias(db_path, eid, "@jbondarenko")
    for _ in range(8):
        _insert_transcription(db_path, transcript_text="це питання до jbondarenko сьогодні")

    assert entity_dedup.find_junk_aliases(db_path) == []
