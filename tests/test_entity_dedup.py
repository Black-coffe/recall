"""Unit tests of offline fuzzy dedup of entities (app/services/entity_dedup.py) — T6.7.

Offline, a real (tmp_path) SQLite DB via init_database (the same pattern as
tests/test_enrichment.py) — WITHOUT the Claude API/network and WITHOUT the real e5 model:
embeddings.embed_texts is replaced (monkeypatch) with deterministic vectors,
to check the logic of candidate search/threshold without loading torch/e5.

Covers:
  - find_merge_candidates: pairs above the threshold, split by type, the threshold filters,
    RuntimeError when embeddings are unavailable.
  - merge_entities: transfer of aliases/meeting_entities (summing on
    conflict)/action_items/role/description/speaker_id, the metadata_json
    audit trail, deletion of merge_id, recomputation of keep_id aggregates,
    type-mismatch/keep==merge/not_found errors, idempotence of a repeated
    merge (of an already deleted merge_id).
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
                            salience=None, role_in_meeting=None, source=None) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO meeting_entities (transcription_id, entity_id, mention_count, "
            "salience, role_in_meeting, source) VALUES (?, ?, ?, ?, ?, ?)",
            (tid, eid, mentions, salience, role_in_meeting, source),
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
    """Returns a fake embed_texts(names) -> np.ndarray[N, dim], L2-normalized,
    from the given name -> raw vector dict (unknown names -> an orthogonal
    pseudo-random vector by the name hash, so as not to collide with the map)."""
    def _fake_embed_texts(names, batch_size=32):
        rows = []
        for n in names:
            raw = vec_map.get(n)
            if raw is None:
                # a deterministic "unique" vector for an unknown name
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
        "Олена": [0.95, 0.05, 0.0, 0.0],          # high similarity with "Олена Петренко"
        "Іван Коваль": [0.0, 0.0, 1.0, 0.0],       # orthogonal, not a candidate
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

    # Threshold above the actual similarity -> no candidates.
    assert entity_dedup.find_merge_candidates(db_path, etype="person", threshold=0.999) == []


def test_find_merge_candidates_separates_by_type(db_path, monkeypatch):
    # Identical vectors, but DIFFERENT types -> must not be matched with each other.
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
        "B": [0.99, 0.01, 0.0, 0.0],   # A-B very similar
        "C": [0.90, 0.10, 0.0, 0.0],   # A-C less similar (but still > threshold)
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
    monkeypatch.setattr(embeddings, "unavailability_reason", lambda: "torch is not installed")
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
    # t1: both entities mentioned in THE SAME meeting -> a conflict on transfer, we sum.
    _insert_meeting_entity(db_path, t1, keep, mentions=2, salience=0.4)
    _insert_meeting_entity(db_path, t1, merge, mentions=3, salience=0.7)
    # t2: only merge -> a plain entity_id transfer.
    _insert_meeting_entity(db_path, t2, merge, mentions=1, salience=0.2)

    ai_id = _insert_action_item(db_path, t1, "Підготувати звіт", owner_entity_id=merge)

    res = entity_dedup.merge_entities(db_path, keep, merge)
    assert res["status"] == "merged"
    assert res["keep_id"] == keep
    assert res["merge_id"] == merge

    with get_db_connection(db_path) as conn:
        # the merge entity is deleted
        assert conn.execute("SELECT id FROM entities WHERE id = ?", (merge,)).fetchone() is None

        keep_row = conn.execute("SELECT * FROM entities WHERE id = ?", (keep,)).fetchone()
        assert keep_row["role"] == "ПМ"                 # inherited, because keep was empty
        assert keep_row["description"] == "коротко"
        meta = json.loads(keep_row["metadata_json"])
        assert meta["merged_from"][0]["id"] == merge

        # aliases: canonical "Олена" + "О. Петренко" are now aliases of keep
        aliases = {r["normalized_alias"] for r in conn.execute(
            "SELECT normalized_alias FROM entity_aliases WHERE entity_id = ?", (keep,))}
        assert _normalize("Олена") in aliases
        assert _normalize("О. Петренко") in aliases

        # meeting_entities: t1 — summed (2+3=5), t2 — transferred
        me_t1 = conn.execute(
            "SELECT mention_count, salience FROM meeting_entities "
            "WHERE transcription_id = ? AND entity_id = ?", (t1, keep)).fetchone()
        assert me_t1["mention_count"] == 5
        assert me_t1["salience"] == pytest.approx(0.7)
        me_t2 = conn.execute(
            "SELECT mention_count FROM meeting_entities "
            "WHERE transcription_id = ? AND entity_id = ?", (t2, keep)).fetchone()
        assert me_t2["mention_count"] == 1
        # no leftover rows on the deleted merge_id
        leftover = conn.execute(
            "SELECT COUNT(*) AS n FROM meeting_entities WHERE entity_id = ?", (merge,)
        ).fetchone()["n"]
        assert leftover == 0

        # action_items: the owner is reassigned
        ai_row = conn.execute("SELECT owner_entity_id FROM action_items WHERE id = ?",
                              (ai_id,)).fetchone()
        assert ai_row["owner_entity_id"] == keep

        # aggregates recomputed: meeting_count by keep's meeting_entities = 2 (t1,t2),
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

    # A repeated merge of an already nonexistent merge_id -> not_found, no crash/side effects.
    second = entity_dedup.merge_entities(db_path, keep, merge)
    assert second["status"] == "not_found"

    with get_db_connection(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM entities WHERE id = ?", (keep,)).fetchone()["n"]
        assert n == 1


# ============================================================
# Cross-type merge (12.08.2026)
# ============================================================

def test_cross_type_merge_is_refused_by_default(db_path):
    """The ban stays by default: it is right for embedding candidates."""
    a = _insert_entity(db_path, "person", "Хтось")
    b = _insert_entity(db_path, "org", "Хтось Інк")

    res = entity_dedup.merge_entities(db_path, a, b)

    assert res["status"] == "error" and "type mismatch" in res["error"]


def test_cross_type_merge_joins_one_thing_written_twice(db_path):
    """The main class of archive duplicates: one thing registered both as project and as org.

    Enrichment creates the entity separately in each type, so «Acmecorp» lived as
    project (114 links) and as org (78) — 133 such groups out of 152. The type stays
    from `keep_id`, links and tasks move over, the merge row disappears.
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
        assert row["type"] == "project"                     # type from keep
        assert row["meeting_count"] == 2                    # both meetings
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id = ?",
                            (gone,)).fetchone()[0] == 0
        assert conn.execute("SELECT owner_entity_id FROM action_items WHERE id = ?",
                            (task,)).fetchone()[0] == keep
        aliases = {r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (keep,))}
        assert {"AcmeCorp", "Acme Corp"} <= aliases
        meta = json.loads(conn.execute("SELECT metadata_json FROM entities WHERE id = ?",
                                       (keep,)).fetchone()[0])
        assert meta["merged_from"][0]["type"] == "org"      # trace of the type that vanished


def test_cross_type_merge_does_not_double_count_shared_meeting(db_path):
    """A shared meeting: the same mentions seen by two graph rows.

    A same-type merge sums mention_count — there are two DIFFERENT names («Олена Петренко»
    and «Олена»), and the sum is meaningful. In a cross-type one it is one spelling,
    so a sum would count the same words twice: we take MAX.
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
                            (keep, t)).fetchone()[0] == 6      # MAX, not 10
        assert conn.execute("SELECT meeting_count FROM entities WHERE id = ?",
                            (keep,)).fetchone()[0] == 1


def test_same_type_merge_still_sums_mentions(db_path):
    """The old behavior within a type has not changed."""
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
    """A merge must "stick": the next enrich must not recreate the merged row.

    `_find_entity` looked up an alias with a type check, and `normalized_alias` is
    unique globally. So after an org→project merge the spelling belonged to
    project, the org branch found nothing and created the row ANEW — and
    with zero aliases (all `INSERT OR IGNORE` stumbled on the global UNIQUE).
    I.e. the mass pass over the 133 groups would have rolled itself back, and the resurrected rows
    would have become unreachable for search by alias.
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

    assert again == keep                       # the merged one was found, not a new one created
    assert types == ["project"]


def test_person_and_non_person_never_merge(db_path):
    """«Ткачук» is both a person and an agency. The flag gives no right to this."""
    person = _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")

    res = entity_dedup.merge_entities(db_path, person, org, allow_cross_type=True)
    back = entity_dedup.merge_entities(db_path, org, person, allow_cross_type=True)

    assert res["status"] == "error" and "person merge refused" in res["error"]
    assert back["status"] == "error"
    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2


def test_cross_type_with_different_names_still_sums(db_path):
    """MAX is decided by a SPELLING MATCH, not by a difference of types.

    «Acmecorp» and «Acmecorp Ltd» are different rows that Claude counted separately on the same
    meeting. If MAX switched on from the mere fact of different types, mentions
    would be understated, and they drive ranking and "from N mentions" filters.
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
    """`source` travels with the link: otherwise the row becomes a ghost for tg_entities.

    The cleanup of Telegram links goes by the query `DELETE … WHERE source = ?`, so
    a link that lost its origin survives a rebuild, and `stats()` credits
    it to Claude. In an archive where 88% is Telegram, this is not a small thing.
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
    """Cross-type pairs must be visible to the tool, not only by hand in SQL."""
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "AcmeCorp")
    _insert_entity(db_path, "person", "Ткачук")
    _insert_entity(db_path, "org", "Ткачук")
    _insert_entity(db_path, "project", "Самотній")
    t = _insert_transcription(db_path, source_name="c.mp3")
    _insert_meeting_entity(db_path, t, keep)

    groups = entity_dedup.find_cross_type_twins(db_path)

    assert len(groups) == 1                                  # the person pair is hidden
    assert groups[0]["keep"]["id"] == keep                   # keep — by links
    assert [m["id"] for m in groups[0]["merge"]] == [gone]
    assert len(entity_dedup.find_cross_type_twins(db_path, include_person=True)) == 2


def test_stickiness_does_not_swallow_pairs_left_unmerged(db_path):
    """Stickiness works only where a merge really happened.

    A tempting edit is to look up an alias with no type check at all. Then the org
    «Ткачук» would find the person «Ткачук», and the 8 pairs the owner deliberately left
    unmerged would collapse on their own, without any merge.
    """
    from app.services import enrichment

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        person = enrichment._upsert_entity(c, "person", "Ткачук")
        conn.commit()

        found = enrichment._find_entity(c, "org", "ткачук")
        assert found is None                       # a person is not handed out as an org

        org = enrichment._upsert_entity(c, "org", "Ткачук")
        conn.commit()
        assert org != person
        assert c.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2


def test_keep_is_chosen_by_type_not_by_link_count(db_path):
    """Type decides visibility, so `topic` cannot become the primary over `project`.

    `scope.resolve_scope` takes project|org|person and does not see topic. When keep
    was chosen by links, seven projects of the live archive were absorbed by topics and
    vanished from the project slice — although links move to
    keep anyway, i.e. choosing by type loses nothing.
    """
    topic = _insert_entity(db_path, "topic", "task tracker")
    project = _insert_entity(db_path, "project", "Task Tracker")
    t1 = _insert_transcription(db_path, source_name="a.mp3")
    t2 = _insert_transcription(db_path, source_name="b.mp3")
    t3 = _insert_transcription(db_path, source_name="c.mp3")
    for t in (t1, t2, t3):
        _insert_meeting_entity(db_path, t, topic)      # the topic has more links
    _insert_meeting_entity(db_path, t1, project)

    group = entity_dedup.find_cross_type_twins(db_path)[0]

    assert group["keep"]["id"] == project
    assert [m["id"] for m in group["merge"]] == [topic]


def test_person_in_group_does_not_hide_the_mergeable_rest(db_path):
    """{person, org, project} under one name: the org+project pair is visible."""
    _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")
    project = _insert_entity(db_path, "project", "Ткачук")
    _insert_meeting_entity(db_path, _insert_transcription(db_path, source_name="a.mp3"), project)

    groups = entity_dedup.find_cross_type_twins(db_path)

    assert len(groups) == 1
    assert groups[0]["keep"]["id"] == project
    assert [m["id"] for m in groups[0]["merge"]] == [org]


def _rowcount(db_path: str, table: str) -> int:
    with get_db_connection(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ============================================================
# find_alias_cross_type_collisions
# ============================================================

def test_alias_cross_type_finds_wynn_dubai(db_path):
    """«WYNN Дубай» is an alias of org #670 and at the same time the canonical name of project #2301
    (the end-to-end tracer case from the story)."""
    org = _insert_entity(db_path, "org", "Wynn Casino Las Vegas")
    _insert_alias(db_path, org, "WYNN Дубай")
    project = _insert_entity(db_path, "project", "WYNN Дубай")

    groups = entity_dedup.find_alias_cross_type_collisions(db_path)

    assert len(groups) == 1
    g = groups[0]
    assert not g.get("ambiguous")
    assert g["evidence"] == {"alias_of": org, "alias": "WYNN Дубай"}
    assert g["keep"]["id"] == project          # project is above org in _TYPE_RANK
    assert [m["id"] for m in g["merge"]] == [org]


def test_alias_cross_type_ignores_own_alias(db_path):
    """An alias that matches the entity's OWN canonical name is not a collision."""
    e = _insert_entity(db_path, "org", "Робота Fund")
    _insert_alias(db_path, e, "робота fund")  # the same fold key, the same entity

    assert entity_dedup.find_alias_cross_type_collisions(db_path) == []


def test_alias_cross_type_same_type_is_not_our_class(db_path):
    """An org alias == the canonical name of ANOTHER org is not this function's cross-type class."""
    a = _insert_entity(db_path, "org", "Alpha")
    _insert_alias(db_path, a, "Beta")
    _insert_entity(db_path, "org", "Beta")

    assert entity_dedup.find_alias_cross_type_collisions(db_path) == []


def test_alias_cross_type_three_referents_are_ambiguous(db_path):
    """One spelling cannot be credited to two: three different entities — ambiguous,
    none gets into the merge proposal."""
    org = _insert_entity(db_path, "org", "Робота Fund")
    _insert_alias(db_path, org, "Робота")
    project = _insert_entity(db_path, "project", "Робота")
    topic = _insert_entity(db_path, "topic", "робота")

    groups = entity_dedup.find_alias_cross_type_collisions(db_path)

    assert len(groups) == 1
    g = groups[0]
    assert g["ambiguous"] is True
    assert "keep" not in g and "merge" not in g
    assert {m["id"] for m in g["members"]} == {org, project, topic}


def test_alias_cross_type_person_excluded_by_default(db_path):
    p = _insert_entity(db_path, "person", "Руслан")
    _insert_alias(db_path, p, "Робота")
    project = _insert_entity(db_path, "project", "Робота")

    assert entity_dedup.find_alias_cross_type_collisions(db_path) == []

    included = entity_dedup.find_alias_cross_type_collisions(db_path, include_person=True)
    assert len(included) == 1
    assert {included[0]["keep"]["id"], included[0]["merge"][0]["id"]} == {p, project}


def test_alias_cross_type_folds_cyrillic_case_not_via_sqlite_lower(db_path):
    """Folding is `_fold_name`/Python `casefold`, a Cyrillic pair that SQLite
    LOWER does not narrow."""
    org = _insert_entity(db_path, "org", "СВІТЛО")
    _insert_alias(db_path, org, "Світло Груп")
    project = _insert_entity(db_path, "project", "світло груп")

    groups = entity_dedup.find_alias_cross_type_collisions(db_path)

    assert len(groups) == 1
    assert {groups[0]["keep"]["id"], groups[0]["merge"][0]["id"]} == {org, project}


def test_alias_cross_type_is_read_only(db_path):
    org = _insert_entity(db_path, "org", "Wynn Casino Las Vegas")
    _insert_alias(db_path, org, "WYNN Дубай")
    _insert_entity(db_path, "project", "WYNN Дубай")

    before = {t: _rowcount(db_path, t) for t in ("entities", "entity_aliases")}
    entity_dedup.find_alias_cross_type_collisions(db_path)
    after = {t: _rowcount(db_path, t) for t in ("entities", "entity_aliases")}

    assert before == after


# ============================================================
# CLI: twins --with-aliases
# ============================================================

def test_twins_cli_json_carries_alias_collisions(db_path, capsys):
    org = _insert_entity(db_path, "org", "Wynn Casino Las Vegas")
    _insert_alias(db_path, org, "WYNN Дубай")
    project = _insert_entity(db_path, "project", "WYNN Дубай")

    entity_dedup.main(["--db", db_path, "twins", "--with-aliases", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict) and "with_aliases" in payload
    ids = {payload["with_aliases"][0]["keep"]["id"],
           payload["with_aliases"][0]["merge"][0]["id"]}
    assert ids == {org, project}


def test_twins_cli_without_flag_is_byte_identical(db_path, capsys):
    org = _insert_entity(db_path, "org", "Wynn Casino Las Vegas")
    _insert_alias(db_path, org, "WYNN Дубай")
    _insert_entity(db_path, "project", "WYNN Дубай")
    keep = _insert_entity(db_path, "project", "Acmecorp")
    gone = _insert_entity(db_path, "org", "AcmeCorp")
    _insert_meeting_entity(db_path, _insert_transcription(db_path, source_name="c.mp3"), keep)

    entity_dedup.main(["--db", db_path, "twins", "--json"])
    without_flag = capsys.readouterr().out

    assert json.loads(without_flag) == entity_dedup.find_cross_type_twins(db_path)


def test_fold_key_decides_max_the_same_way_the_finder_groups(db_path):
    """«Acme Corp» and «AcmeCorp» are one spelling for the finder, hence MAX for mentions too."""
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
    """`source IS NULL` is "a link from Claude", not "unknown".

    `tg_entities` deliberately does not touch such rows as more reliable. If a merge
    overwrote NULL with 'telegram', the next rebuild would delete the row and
    recreate it bare — without salience, role and accumulated mentions.
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
    """Having absorbed one org, keep must not appropriate ANY org from its aliases."""
    from app.services import enrichment

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        keep = enrichment._upsert_entity(c, "project", "Acmecorp", aliases=["BH"])
        gone = enrichment._upsert_entity(c, "org", "Acmecorp Ltd")
        conn.commit()
    entity_dedup.merge_entities(db_path, keep, gone, allow_cross_type=True)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert enrichment._find_entity(c, "org", "bh") is None            # someone else's org
        assert enrichment._find_entity(c, "org", "acmecorp ltd") == keep  # the absorbed one


# ============================================================
# Split — the inverse trouble: different people sit in one row
# ============================================================

def _mixed_person(db_path: str):
    """The row «Дмитро» holding two people: «Дмитро Лебідь» and «М.Верес».

    Returns (entity_id, {key: transcription_id}).
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
    """Evidence for the decision: how many tasks named exactly this spelling and where it is in the text."""
    eid, tids = _mixed_person(db_path)
    _insert_action_item(db_path, tids["moved"], "звірити ставку", eid, owner_name="М.Верес")
    _insert_action_item(db_path, tids["both"], "готувати звіт", eid, owner_name="Дмитро Лебідь")
    _insert_action_item(db_path, tids["neither"], "подзвонити", eid, owner_name="Дмитро")
    _insert_action_item(db_path, tids["both"], "спільна задача", eid,
                        owner_name="Дмитро Лебідь, М.Верес")

    info = entity_dedup.inspect_entity(db_path, eid)

    assert info["status"] == "ok"
    assert info["tasks_total"] == 4 and info["links_total"] == 3
    assert info["tasks_ambiguous_owner"] == 1          # «X, Y» is not evidence
    assert info["links_short_name_only"] == 1          # a text with no spelling at all
    per = {a["alias"]: a for a in info["aliases"]}
    assert per["М.Верес"]["tasks"] == 1 and per["М.Верес"]["texts"] == 2
    assert per["Дмитро Лебідь"]["tasks"] == 1 and per["Дмитро Лебідь"]["texts"] == 1
    assert per["Дмитро"]["tasks"] == 1


def test_inspect_not_found(db_path):
    assert entity_dedup.inspect_entity(db_path, 999)["status"] == "not_found"


def test_split_is_a_dry_run_until_asked(db_path):
    """Numbers first, then the edit: without --apply nothing changes."""
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
    """What is proven moves: a task by owner_name, a link where only the detached one is visible.

    A shared text (both visible) is COPIED, not moved; a text with no
    long spelling stays with the source — there is nothing to prove with.
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
    assert tids["moved"] not in src_links                      # moved over
    assert {tids["both"], tids["neither"]} <= src_links        # a copy + the unproven
    # The moved row carries its own numbers; the copy counts its OWN mentions, not the other's 5.
    assert links[tids["moved"]]["mention_count"] == 3
    assert links[tids["moved"]]["salience"] == 0.7
    assert links[tids["moved"]]["role_in_meeting"] == "вирішував"
    assert links[tids["both"]]["mention_count"] == 1
    assert owners[mine] == new_id and owners[theirs] == eid
    assert alias_owner["М.Верес"] == new_id
    assert alias_owner["Дмитро Лебідь"] == eid
    assert agg[new_id] == (4, 2)                               # 3 + 1, two meetings
    # In the shared meeting the source gave up the same one mention the copy took:
    # 5 - 1 = 4, plus 1 in the unproven one. The archive total became (9), not 10 — a mention cannot
    # be counted twice just as it cannot be credited to two graph rows.
    assert agg[eid] == (5, 2)


def test_the_shared_first_name_does_not_block_the_split(db_path):
    """«Слава Гринчук» is evidence of «Гринчук», not of the presence of «Слава».

    The short name is part of the second person's FULL name, so without competing for a
    position every link would look like "both mentioned" — and a row that entirely
    belongs to «Гринчук» would stay hanging on «Слава».
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
    """The mirror case: we detach the SHORT name, and the full one stays.

    «Юлія Бондаренка підтвердила бюджет» is evidence of «Бондаренка», and the meeting must
    stay with her. Erasing the detached «Юлія» (the first approach) would erase
    «Юлія Бондаренка» too: the evidence for the one who stays would vanish, and someone else's meeting
    would move entirely. So the sides compete for a position and do not erase.
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
    assert src == {hers, shared}          # both stayed with the source
    assert copies == {shared: 1}          # the copy — one OWN mention, not the other's 6


def test_inspect_counts_the_canonical_name_without_an_alias_row(db_path):
    """The canonical name is a spelling too, and there may be no row in `entity_aliases` for it.

    `_upsert_entity` creates it via INSERT OR IGNORE against the global
    UNIQUE, so when another entity has already taken the spelling, the alias was not created. Without
    it the row's own task would look like "owner outside the aliases", although split
    counts the canonical one among those who stay anyway.
    """
    yulia = _insert_entity(db_path, "person", "Юлія Бондаренка")
    _insert_alias(db_path, yulia, "Юлія")          # no row for the canonical one
    tid = _insert_transcription(
        db_path, transcript_text="Юлія Бондаренка підтвердила бюджет.")
    _insert_meeting_entity(db_path, tid, yulia, mentions=3)
    _insert_action_item(db_path, tid, "звірити кошторис", yulia,
                        owner_name="Юлія Бондаренка")

    info = entity_dedup.inspect_entity(db_path, yulia)
    per = {a["alias"]: a for a in info["aliases"]}

    assert per["Юлія Бондаренка"]["tasks"] == 1
    assert per["Юлія Бондаренка"]["texts"] == 1
    assert per["Юлія"]["texts"] == 0               # the mention was taken by the full name
    assert info["tasks_unmatched_owner"] == []
    assert info["links_short_name_only"] == 0


def test_short_alias_inside_a_longer_word_is_not_evidence(db_path):
    """The alias «Слав» sits inside «Ярослав» — the word boundary at the front cuts that off."""
    eid = _insert_entity(db_path, "person", "Слава")
    for a in ("Слава", "Слав", "Гринчук"):
        _insert_alias(db_path, eid, a)
    tid = _insert_transcription(db_path, transcript_text="Гринчук і Ярослав зайдуть завтра.")
    _insert_meeting_entity(db_path, tid, eid, mentions=2)

    info = entity_dedup.inspect_entity(db_path, eid)
    per = {a["alias"]: a for a in info["aliases"]}
    assert per["Слав"]["texts"] == 0          # «Ярослав» is not evidence
    assert per["Гринчук"]["texts"] == 1

    # But a case form is: «Гринчука» starts with the spelling «Гринчук».
    tid2 = _insert_transcription(db_path, transcript_text="Питання до Гринчука закрите.")
    _insert_meeting_entity(db_path, tid2, eid, mentions=1)
    per2 = {a["alias"]: a for a in entity_dedup.inspect_entity(db_path, eid)["aliases"]}
    assert per2["Гринчук"]["texts"] == 2


def test_split_can_glue_a_stranger_to_the_person_who_owns_the_name(db_path):
    """`Stepan` among the aliases of «Слава» is «Степан»; the destination can be an existing row."""
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
    """A person's name, moving into project, vanishes from the person indexes — the same boundary as in merge."""
    person = _insert_entity(db_path, "person", "Джейсон")
    _insert_alias(db_path, person, "Джейсон")
    _insert_alias(db_path, person, "Генрі")
    proj = _insert_entity(db_path, "project", "Генрі")

    res = entity_dedup.split_entity(db_path, person, ["Генрі"], target_id=proj, dry_run=False)
    assert res["status"] == "error" and "type mismatch" in res["error"]


def test_drop_takes_garbage_out_and_leaves_no_owner(db_path):
    """`JSON` among the aliases of «Джейсон» is not a person. A task without an owner is more honest than someone else's."""
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
        # The trash must not resurrect: for 'json' there is no entity at all any more.
        assert enrichment._find_entity(c, "person", "json") is None


def test_split_does_not_let_the_merge_trail_pull_the_name_back(db_path):
    """The merge trail returned the source by spelling — after detaching it must not.

    Otherwise the next enrich would again bind the detached name to the source: split
    would "not stick" just as merge once did not.
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
        assert enrichment._find_entity(c, "person", "м.верес") == keep   # it stuck

    entity_dedup.split_entity(db_path, keep, ["М.Верес"], drop=True, dry_run=False)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert enrichment._find_entity(c, "person", "м.верес") is None
        meta = json.loads(c.execute("SELECT metadata_json FROM entities WHERE id = ?",
                                    (keep,)).fetchone()["metadata_json"])
    assert "merged_from" not in meta          # no spellings left from the record
    assert meta["split_off"][0]["dropped"] is True


def test_split_refuses_to_rename_instead_of_splitting(db_path):
    """The canonical name and "all aliases" are a rename, a different operation."""
    eid, _ = _mixed_person(db_path)

    canon = entity_dedup.split_entity(db_path, eid, ["Дмитро"], new_name="Дмитро Лебідь")
    assert canon["status"] == "error" and "canonical" in canon["error"]

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
    assert stranger["status"] == "error" and "among the aliases" in stranger["error"]


def test_split_into_an_existing_name_points_at_the_row_that_holds_it(db_path):
    """UNIQUE(type, normalized_name): we do not create a new entity with a taken name.

    And the estimate must say the same as the application: otherwise it promises a split
    that `--apply` rejects, while the estimate is the only surface for deciding.
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
    """An empty `staying` is not a rename: the canonical name always stays.

    A row in `entity_aliases` for the canonical one may be missing (INSERT OR IGNORE against the
    global UNIQUE lost the race to another entity) — and this is exactly the row
    split exists for. A guard on "ALL aliases are detached" would refuse
    precisely here and in no other case.
    """
    eid = _insert_entity(db_path, "person", "Дмитро")
    _insert_alias(db_path, eid, "М.Верес")          # no canonical row
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
    assert canon == "Дмитро"                          # the source stayed itself


def test_drop_does_not_report_links_it_leaves_behind(db_path):
    """`drop` makes no copies — the shared text stays with the source and must be in links_left.

    Otherwise the estimate shows "nothing will remain" where only a part disappears, and
    it is the only surface on which the operator makes the decision.
    """
    eid, tids = _mixed_person(db_path)

    res = entity_dedup.split_entity(db_path, eid, ["М.Верес"], drop=True)

    assert res["links_moved"] == 1
    assert res["links_copied"] == 0                   # there are no copies in drop
    assert res["links_shared_kept"] == 1              # the shared text stays
    assert res["links_left"] == 2                     # shared + unproven

    entity_dedup.split_entity(db_path, eid, ["М.Верес"], drop=True, dry_run=False)
    with get_db_connection(db_path) as conn:
        left = {r["transcription_id"] for r in conn.execute(
            "SELECT transcription_id FROM meeting_entities WHERE entity_id = ?", (eid,))}
    assert left == {tids["both"], tids["neither"]}     # exactly what the report promised


def test_split_sums_mentions_when_the_target_already_saw_that_meeting(db_path):
    """The target already has a row for this meeting — the mentions add up, they do not take MAX.

    The same rule as in `merge_entities`: MAX only when the spelling is ONE and the
    same. Here the target earned its row by its own name, and the mentions of other words
    of the same text come to it.
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
    assert got == 7                                   # 4 of its own + 3 moved


def test_split_reaches_meetings_in_the_recycle_bin(db_path):
    """A softly deleted meeting is split too — it can still be restored.

    `POST /api/transcription/<id>/restore` brings it back to life; if split
    bypassed it, after the restore the task would stay with the old owner, and the
    link would lack the spelling that held it. In the numbers it is visible separately: the UI
    does not have this meeting right now.
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
    """A CLI spelling is checked by the fold key: dots, spaces and case do not matter.

    Otherwise detaching would fail exactly where it is needed: «М.Верес»,
    «М. Верес» and «м.верес » are the same spelling typed differently.
    """
    eid, _ = _mixed_person(db_path)

    res = entity_dedup.split_entity(db_path, eid, [" м. верес "], drop=True, dry_run=False)

    assert res["status"] == "ok" and res["aliases_moved"] == ["М.Верес"]
    with get_db_connection(db_path) as conn:
        rows = {r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (eid,))}
    assert rows == {"Дмитро", "Дмитро Лебідь", "Дима"}


# ============================================================
# allow_person: the ban is lifted only by hand and only on the safe side
# ============================================================

def test_person_merge_still_refused_without_the_flag(db_path):
    """The default has not changed: an agency named after the owner's surname must first be looked at."""
    person = _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")

    res = entity_dedup.merge_entities(db_path, person, org, allow_cross_type=True)

    assert res["status"] == "error" and "person merge refused" in res["error"]


def test_allow_person_merges_and_names_what_entered_the_person_index(db_path):
    """The spellings of the absorbed non-person become person-index keys — so they are visible in the response."""
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
    """The person row disappears — the tasks would be left without a person. The flag does NOT allow this."""
    org = _insert_entity(db_path, "org", "Вербич")
    person = _insert_entity(db_path, "person", "Вербич")
    tid = _insert_transcription(db_path)
    _insert_action_item(db_path, tid, "Підписати меморандум", owner_entity_id=person)

    res = entity_dedup.merge_entities(db_path, org, person, allow_cross_type=True,
                                      allow_person=True)

    assert res["status"] == "error" and "has 1 tasks" in res["error"]
    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM entities WHERE id = ?",
                            (person,)).fetchone()["c"] == 1


# ============================================================
# alias: assign a spelling under which another layer knows the thing
# ============================================================

def test_alias_is_a_dry_run_until_asked(db_path):
    eid = _insert_entity(db_path, "person", "Дмитро Лебідь")

    res = entity_dedup.add_aliases(db_path, eid, ["Dmytro Lebid"])

    assert res["status"] == "ok" and res["added"] == ["Dmytro Lebid"]
    with get_db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM entity_aliases").fetchone()["c"] == 0


def test_alias_does_not_take_a_spelling_that_belongs_to_someone_else(db_path):
    """`normalized_alias` is UNIQUE globally — we do not silently take someone else's spelling."""
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
    """A spelling may already live NOT in the aliases: `split --to-name` creates a row without an alias row.

    An alias assigned on top of such a canonical name is dead weight
    (`enrichment._find_entity` looks by canonical first), and `relink` after
    it would also re-hang tasks onto that foreign entity.
    """
    mine = _insert_entity(db_path, "person", "Дмитро Лебідь")
    theirs = _insert_entity(db_path, "person", "Dmytro Lebid")   # without a row in aliases

    res = entity_dedup.add_aliases(db_path, mine, ["Dmytro Lebid"], dry_run=False)

    assert res["added"] == []
    assert res["taken"] == [{"alias": "Dmytro Lebid", "entity_id": theirs,
                             "name": "Dmytro Lebid", "type": "person"}]


def test_allow_person_refuses_when_a_first_name_twin_would_inherit_the_future(db_path):
    """A row that is empty now does not mean there will be no harm later.

    The name leaves the person index, and the next task «Вербич Петренко» will go
    through the first-token fallback — to the single-word namesake «Вербич».
    """
    org = _insert_entity(db_path, "org", "Вербич")
    gone = _insert_entity(db_path, "person", "Вербич Петренко")
    _insert_entity(db_path, "person", "Вербич")     # the namesake who would get the fallback

    res = entity_dedup.merge_entities(db_path, org, gone, allow_cross_type=True,
                                      allow_person=True)

    assert res["status"] == "error" and "first-token fallback" in res["error"]


def test_allow_person_reports_only_spellings_that_really_landed_on_keep(db_path):
    """A spelling could have stayed with a third entity — then a report on "entered" would be untrue."""
    person = _insert_entity(db_path, "person", "Ткачук")
    org = _insert_entity(db_path, "org", "Ткачук")          # without its own alias row
    _insert_alias(db_path, org, "Федеріва")
    third = _insert_entity(db_path, "person", "Хтось")
    _insert_alias(db_path, third, "Ткачук")   # the spelling is already taken by a third

    res = entity_dedup.merge_entities(db_path, person, org, allow_cross_type=True,
                                      allow_person=True)

    assert res["status"] == "merged"
    assert res["person_index_added"] == ["Федеріва"], \
        "«Ткачук» stayed with the third entity — it cannot be reported"
    with get_db_connection(db_path) as conn:
        owner = conn.execute("SELECT entity_id FROM entity_aliases WHERE alias = ?",
                             ("Ткачук",)).fetchone()["entity_id"]
    assert owner == third


# ============================================================
# Spellings that are actually common words
# ============================================================

def test_junk_alias_is_found_by_lowercase_usage(db_path):
    """«документ» as a project alias: the corpus writes this word lowercase."""
    eid = _insert_entity(db_path, "project", "Стратегія розвитку області")
    _insert_alias(db_path, eid, "документ")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="надішли документ на пошту")

    found = entity_dedup.find_junk_aliases(db_path)

    assert [r["alias"] for r in found] == ["документ"]
    assert found[0]["entity_id"] == eid and found[0]["lower"] == 6
    assert found[0]["caps_mid_sentence"] == 0


def test_real_name_survives_the_audit(db_path):
    """A proper name is written with a capital in the middle of a sentence too — it is not junk."""
    eid = _insert_entity(db_path, "project", "Ковальчука")
    _insert_alias(db_path, eid, "Ковальчука")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="сьогодні Ковальчука підтвердила бюджет")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_sentence_start_is_not_evidence_of_a_name(db_path):
    """A capital after a period is punctuation; otherwise junk justifies itself."""
    eid = _insert_entity(db_path, "project", "Стратегія розвитку області")
    _insert_alias(db_path, eid, "документ")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="Все готово. Документ надіслано.")
        _insert_transcription(db_path, transcript_text="надішли документ на пошту")

    found = entity_dedup.find_junk_aliases(db_path)
    assert found and found[0]["caps_mid_sentence"] == 0


def test_uppercase_headings_do_not_prove_a_name(db_path):
    """A CONTRACT HEADER gives a capital to all words in a row — and proves nothing.

    But ONE all-caps word inside an ordinary sentence counts as evidence, because
    `find_mentions` counts it the same way: diverging here, the audit began advising
    to remove a spelling that is producing links right now.
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
    """The canonical name lies in the aliases too, but removing it as an alias is half the job:
    the row would stay in the graph under the same common word."""
    eid = _insert_entity(db_path, "org", "Фонд")
    _insert_alias(db_path, eid, "Фонд")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="кошти надійшли від фонд партнерів")

    found = entity_dedup.find_junk_aliases(db_path)
    assert found and found[0]["is_canonical"] is True


def test_rare_words_are_not_judged(db_path):
    """Two mentions are too few to call a spelling junk."""
    eid = _insert_entity(db_path, "project", "Стратегія розвитку області")
    _insert_alias(db_path, eid, "документ")
    _insert_transcription(db_path, transcript_text="надішли документ на пошту")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_telegram_handles_are_not_judged_by_capitalisation(db_path):
    """«@jbondarenko» is written lowercase by convention, not because it is a word."""
    eid = _insert_entity(db_path, "person", "jbondarenko")
    _insert_alias(db_path, eid, "@jbondarenko")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="це питання до @jbondarenko сьогодні")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_all_caps_name_is_not_called_junk(db_path):
    """Regression: the audit advised removing a spelling that is producing links right now.

    `find_mentions` considers «ACMECORP» in an ordinary sentence a name and sets
    a link, while the audit did not count ALL-CAPS at all — and showed caps=0 with
    live links, i.e. it proposed destroying correct data.
    """
    eid = _insert_entity(db_path, "project", "Acmecorp")
    _insert_alias(db_path, eid, "Acmecorp")
    for _ in range(6):
        _insert_transcription(db_path,
                              transcript_text="рахунок від ACMECORP надійшов сьогодні")

    assert entity_dedup.find_junk_aliases(db_path) == []


def test_shouting_header_still_proves_nothing(db_path):
    """But in a solid header a capital letter does not become evidence — all the words there are such."""
    eid = _insert_entity(db_path, "project", "Тексти")
    _insert_alias(db_path, eid, "Тексти")
    for _ in range(6):
        _insert_transcription(db_path,
                              transcript_text="ЩОДО ТЕКСТИ СТАТЕЙ У РОЗДІЛ WHAT WE DO\n"
                                              "далі йдуть тексти статей у розділі")

    found = entity_dedup.find_junk_aliases(db_path)
    assert found and found[0]["caps_mid_sentence"] == 0


def test_canonical_name_without_alias_row_is_audited(db_path):
    """Its own alias row may not get created (the global UNIQUE), while the links exist."""
    other = _insert_entity(db_path, "org", "Нуль")
    _insert_alias(db_path, other, "Zero")          # the spelling was taken by another entity
    eid = _insert_entity(db_path, "org", "Zero")   # there will be no alias row here
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="залишок zero на рахунку")

    found = entity_dedup.find_junk_aliases(db_path)
    assert eid in {r["entity_id"] for r in found}


def test_handle_without_at_sign_is_still_a_handle(db_path):
    """The canonical name of a handle arrives without «@» — the guard must catch it too."""
    eid = _insert_entity(db_path, "person", "jbondarenko")
    _insert_alias(db_path, eid, "@jbondarenko")
    for _ in range(8):
        _insert_transcription(db_path, transcript_text="це питання до jbondarenko сьогодні")

    assert entity_dedup.find_junk_aliases(db_path) == []


# ============================================================
# Morphology blockers — a read-only report (entity-graph-tg-02)
# ============================================================

def test_blockers_finds_generic_canonical(db_path):
    """«Фонд» is a generic canonical name, not removed as an alias."""
    eid = _insert_entity(db_path, "org", "Фонд")
    _insert_alias(db_path, eid, "Фонд")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="кошти надійшли від фонд партнерів")

    found = entity_dedup.find_morph_blockers(db_path)

    assert len(found) == 1
    assert found[0]["entity_id"] == eid
    assert found[0]["reason"] == "generic_canonical"
    assert found[0]["disputed"] is False


def test_blockers_finds_case_form_entity(db_path):
    """project «Україні» is a case form, the links are mostly from an exact TG match."""
    from app.services import tg_entities

    eid = _insert_entity(db_path, "project", "Україні")
    for _ in range(2):
        tid = _insert_transcription(db_path)
        _insert_meeting_entity(db_path, tid, eid, source=tg_entities.SOURCE)
    tid = _insert_transcription(db_path)
    _insert_meeting_entity(db_path, tid, eid, source=None)

    found = entity_dedup.find_morph_blockers(db_path)

    assert len(found) == 1
    assert found[0]["entity_id"] == eid
    assert found[0]["reason"] == "case_form"
    assert found[0]["links_total"] == 3
    assert found[0]["links_thread_match"] == 2
    assert found[0]["disputed"] is False


def test_blockers_ignores_case_form_when_links_are_mostly_from_enrichment(db_path):
    """The same word form, but the links are mostly from Claude — not a blocker."""
    from app.services import tg_entities

    eid = _insert_entity(db_path, "project", "Україні")
    for _ in range(2):
        tid = _insert_transcription(db_path)
        _insert_meeting_entity(db_path, tid, eid, source=None)
    tid2 = _insert_transcription(db_path)
    _insert_meeting_entity(db_path, tid2, eid, source=tg_entities.SOURCE)

    assert entity_dedup.find_morph_blockers(db_path) == []


def test_blockers_ignores_short_and_multi_word_names(db_path):
    """A short (<MIN_NAME_LEN) and a multi-word name are not touched by the morph folder."""
    from app.services import tg_entities

    short = _insert_entity(db_path, "project", "Кий")           # 3 letters, the suffix is not stripped due to length
    multi = _insert_entity(db_path, "project", "Дім Данс Ігор")  # a phrase
    for eid in (short, multi):
        tid = _insert_transcription(db_path)
        _insert_meeting_entity(db_path, tid, eid, source=tg_entities.SOURCE)

    assert entity_dedup.find_morph_blockers(db_path) == []


def test_blockers_does_not_double_report_same_entity(db_path):
    """A generic canonical name that is also a case form — counted once, by the first pass."""
    from app.services import tg_entities

    eid = _insert_entity(db_path, "org", "Сенсу")
    _insert_alias(db_path, eid, "Сенсу")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="ми поговорили про сенсу цього рішення")
    for _ in range(3):
        tid = _insert_transcription(db_path)
        _insert_meeting_entity(db_path, tid, eid, source=tg_entities.SOURCE)

    found = entity_dedup.find_morph_blockers(db_path)

    matches = [r for r in found if r["entity_id"] == eid]
    assert len(matches) == 1
    assert matches[0]["reason"] == "generic_canonical"


def test_blockers_marks_known_disputed_names(db_path):
    """«Місто» is from the story's explicit list, stays a proposal only with a mark."""
    from app.services import tg_entities

    eid = _insert_entity(db_path, "project", "Місто")
    for _ in range(3):
        tid = _insert_transcription(db_path)
        _insert_meeting_entity(db_path, tid, eid, source=tg_entities.SOURCE)

    found = entity_dedup.find_morph_blockers(db_path)

    assert found and found[0]["entity_id"] == eid
    assert found[0]["disputed"] is True


def test_blockers_cli_json(db_path, capsys):
    eid = _insert_entity(db_path, "org", "Фонд")
    _insert_alias(db_path, eid, "Фонд")
    for _ in range(6):
        _insert_transcription(db_path, transcript_text="кошти надійшли від фонд партнерів")

    entity_dedup.main(["--db", db_path, "blockers", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload == entity_dedup.find_morph_blockers(db_path)


# ============================================================
# rename — renaming the canonical name (entity-graph-tg-02)
# ============================================================

def test_rename_dry_run_does_not_write(db_path):
    eid = _insert_entity(db_path, "org", "Фонд")

    res = entity_dedup.rename_entity(db_path, eid, "Фонд Розвитку")

    assert res == {"status": "ok", "dry_run": True, "changed": True,
                   "entity": {"id": eid, "type": "org",
                              "old_name": "Фонд", "new_name": "Фонд Розвитку"}}
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT canonical_name FROM entities WHERE id = ?",
                           (eid,)).fetchone()
    assert row["canonical_name"] == "Фонд"


def test_rename_apply_writes_and_leaves_old_name_sticky(db_path):
    eid = _insert_entity(db_path, "org", "Фонд")

    res = entity_dedup.rename_entity(db_path, eid, "Фонд Розвитку", dry_run=False)

    assert res["status"] == "ok" and res["changed"] is True
    with get_db_connection(db_path) as conn:
        ent = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        alias = conn.execute("SELECT entity_id FROM entity_aliases WHERE normalized_alias = ?",
                             (_normalize("Фонд"),)).fetchone()
    assert ent["canonical_name"] == "Фонд Розвитку"
    assert ent["normalized_name"] == _normalize("Фонд Розвитку")
    assert alias is not None and alias["entity_id"] == eid
    meta = json.loads(ent["metadata_json"])
    assert meta["renamed_from"] == [{"old_name": "Фонд", "new_name": "Фонд Розвитку",
                                     "at": meta["renamed_from"][0]["at"]}]


def test_rename_is_idempotent(db_path):
    eid = _insert_entity(db_path, "org", "Фонд")
    entity_dedup.rename_entity(db_path, eid, "Фонд Розвитку", dry_run=False)

    res = entity_dedup.rename_entity(db_path, eid, "Фонд Розвитку", dry_run=False)

    assert res == {"status": "ok", "dry_run": False, "changed": False,
                   "entity": {"id": eid, "type": "org",
                              "old_name": "Фонд Розвитку", "new_name": "Фонд Розвитку"}}
    with get_db_connection(db_path) as conn:
        ent = conn.execute("SELECT metadata_json FROM entities WHERE id = ?",
                           (eid,)).fetchone()
    meta = json.loads(ent["metadata_json"])
    assert len(meta["renamed_from"]) == 1, "the repeated call was not supposed to append anything"


def test_rename_refuses_unique_collision(db_path):
    rival = _insert_entity(db_path, "org", "Акмеед")
    eid = _insert_entity(db_path, "org", "Фонд")

    res = entity_dedup.rename_entity(db_path, eid, "Акмеед", dry_run=False)

    assert res["status"] == "error"
    assert res["rival"] == {"id": rival, "name": "Акмеед"}
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT canonical_name FROM entities WHERE id = ?",
                           (eid,)).fetchone()
    assert row["canonical_name"] == "Фонд", "the collision was not supposed to write anything"


def test_rename_not_found(db_path):
    assert entity_dedup.rename_entity(db_path, 999999, "Щось") == \
        {"status": "not_found", "entity_id": 999999}


def test_rename_cli_apply_flag_gates_the_write(db_path, capsys):
    eid = _insert_entity(db_path, "org", "Фонд")

    entity_dedup.main(["--db", db_path, "rename", str(eid), "Фонд Розвитку"])
    capsys.readouterr()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT canonical_name FROM entities WHERE id = ?",
                           (eid,)).fetchone()
    assert row["canonical_name"] == "Фонд", "without --apply the CLI was not supposed to write anything"

    entity_dedup.main(["--db", db_path, "rename", str(eid), "Фонд Розвитку", "--apply"])
    capsys.readouterr()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT canonical_name FROM entities WHERE id = ?",
                           (eid,)).fetchone()
    assert row["canonical_name"] == "Фонд Розвитку"
