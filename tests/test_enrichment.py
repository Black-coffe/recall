"""Unit tests of Meeting Memory enrichment (app/services/enrichment.py) — T7.5.

Offline, a real (temporary, tmp_path) SQLite DB via init_database
(the tests/test_migrations.py pattern) — no Claude API/network. Covers:
  - _normalize / _count_mentions
  - _find_entity / _upsert_entity: creation, dedup (case/whitespace/punctuation),
    lookup via alias, idempotency of a repeated call, link to speakers
  - _recompute_entity_aggregates
  - _enrich_card: idempotency (enriched_at + version), force re-run,
    soft degradation on a Claude call failure (T6.3 retry_needed)
  - _persist_card: full round-trip (summary/topics/entities/action_items),
    idempotent re-run (old markup is overwritten, not duplicated)
  - list_unenriched_ids: filtering out already enriched / soft-deleted
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import categories, enrichment, text_polishing


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


# ============================================================
# _normalize
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("  Андрій  ", "андрій"),
    ("Andrii.", "andrii"),
    ("«Проєкт Х»", "проєкт х"),
    ("Ім'я, з комою,", "ім'я, з комою"),
    ("  багато   пробілів   тут  ", "багато пробілів тут"),
    ("", ""),
    (None, ""),
    ("(Дужки)", "дужки"),
])
def test_normalize(raw, expected):
    assert enrichment._normalize(raw) == expected


def test_normalize_preserves_cyrillic_and_latin_mix():
    assert enrichment._normalize("ТОВ 'Ромашка' LLC") == "тов 'ромашка' llc"


# ============================================================
# _count_mentions
# ============================================================

def test_count_mentions_case_insensitive_and_multiple_names():
    text = "Андрій сказав, що АНДРІЙ і Andriy зустрінуться завтра."
    n = enrichment._count_mentions(text, ["Андрій", "Andriy"])
    assert n == 3


def test_count_mentions_ignores_short_names_and_empty_input():
    assert enrichment._count_mentions("", ["Андрій"]) == 0
    assert enrichment._count_mentions("текст", []) == 0
    assert enrichment._count_mentions("A б в", ["A"]) == 0  # length < 2 is ignored


# ============================================================
# _find_entity / _upsert_entity
# ============================================================

def test_upsert_entity_creates_new(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid = enrichment._upsert_entity(c, "person", "Андрій Коваль", role="CTO")
        conn.commit()
        assert eid is not None
        row = c.execute("SELECT canonical_name, normalized_name, role FROM entities WHERE id=?",
                        (eid,)).fetchone()
        assert row["canonical_name"] == "Андрій Коваль"
        assert row["normalized_name"] == "андрій коваль"
        assert row["role"] == "CTO"


def test_upsert_entity_idempotent_exact_repeat_returns_same_id(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid1 = enrichment._upsert_entity(c, "person", "Андрій Коваль")
        eid2 = enrichment._upsert_entity(c, "person", "Андрій Коваль")
        conn.commit()
        assert eid1 == eid2
        n = c.execute("SELECT COUNT(*) AS n FROM entities WHERE type='person'").fetchone()["n"]
        assert n == 1


@pytest.mark.parametrize("variant", [
    "андрій коваль", "АНДРІЙ КОВАЛЬ", "  Андрій Коваль  ", "Андрій Коваль.", "Андрій   Коваль",
])
def test_upsert_entity_dedup_across_case_and_whitespace(db_path, variant):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid1 = enrichment._upsert_entity(c, "person", "Андрій Коваль")
        eid2 = enrichment._upsert_entity(c, "person", variant)
        conn.commit()
        assert eid1 == eid2, f"variant {variant!r} should have been deduplicated to the same entity"
        n = c.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
        assert n == 1


def test_upsert_entity_dedup_via_alias(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid1 = enrichment._upsert_entity(c, "person", "Андрій Коваль", aliases=["Дрю"])
        eid2 = enrichment._upsert_entity(c, "person", "Дрю")  # the same one, only via alias
        conn.commit()
        assert eid1 == eid2
        n = c.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
        assert n == 1


def test_find_entity_respects_type_boundary(db_path):
    """The same normalized string in another type is a separate entity (an alias is unique
    globally, so _find_entity also checks the type match)."""
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid_person = enrichment._upsert_entity(c, "person", "Фонд")
        eid_org = enrichment._upsert_entity(c, "org", "Фонд Х")  # a different name — not an alias collision
        conn.commit()
        assert eid_person != eid_org
        # a direct lookup by type must not confuse the types
        found_person = enrichment._find_entity(c, "person", "фонд")
        found_org = enrichment._find_entity(c, "org", "фонд")
        assert found_person == eid_person
        assert found_org is None  # «фонд» (without «Х») does not exist as an org


def test_upsert_entity_adds_new_aliases_incrementally(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid = enrichment._upsert_entity(c, "person", "Андрій Коваль", aliases=["Дрю"])
        enrichment._upsert_entity(c, "person", "Андрій Коваль", aliases=["A.K."])
        conn.commit()
        aliases = {r["normalized_alias"] for r in
                   c.execute("SELECT normalized_alias FROM entity_aliases WHERE entity_id=?", (eid,))}
        # _normalize strips punctuation at the EDGES of the string ("A.K." -> "a.k" — the trailing
        # dot is also edge punctuation and gets cut).
        assert {"дрю", "a.k", "андрій коваль"} <= aliases


def test_upsert_entity_fills_role_only_when_previously_empty(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid = enrichment._upsert_entity(c, "person", "Андрій Коваль")  # without a role
        enrichment._upsert_entity(c, "person", "Андрій Коваль", role="CTO")
        role1 = c.execute("SELECT role FROM entities WHERE id=?", (eid,)).fetchone()["role"]
        assert role1 == "CTO"
        # a repeated call with a DIFFERENT role does NOT overwrite the already filled one
        enrichment._upsert_entity(c, "person", "Андрій Коваль", role="CEO")
        role2 = c.execute("SELECT role FROM entities WHERE id=?", (eid,)).fetchone()["role"]
        assert role2 == "CTO"
        conn.commit()


def test_upsert_entity_invalid_type_returns_none(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert enrichment._upsert_entity(c, "animal", "Кіт") is None


@pytest.mark.parametrize("name", ["", "   ", None, ".", "  ,  "])
def test_upsert_entity_empty_or_punctuation_only_name_returns_none(db_path, name):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        assert enrichment._upsert_entity(c, "person", name) is None


def test_upsert_entity_links_speaker_when_name_matches(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO speakers (name) VALUES (?)", ("Андрій Коваль",))
        conn.commit()
        eid = enrichment._upsert_entity(c, "person", "Андрій Коваль")  # exact match
        conn.commit()
        row = c.execute("SELECT speaker_id FROM entities WHERE id=?", (eid,)).fetchone()
        assert row["speaker_id"] is not None


def test_upsert_entity_links_speaker_ascii_case_insensitive(db_path):
    """speakers.name COLLATE NOCASE — SQLite NOCASE folds only ASCII, not Cyrillic.
    We check on Latin, where this guarantee really works."""
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO speakers (name) VALUES (?)", ("John Smith",))
        conn.commit()
        eid = enrichment._upsert_entity(c, "person", "john smith")
        conn.commit()
        row = c.execute("SELECT speaker_id FROM entities WHERE id=?", (eid,)).fetchone()
        assert row["speaker_id"] is not None


def test_upsert_entity_does_not_overwrite_existing_speaker_link(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO speakers (name) VALUES (?)", ("Speaker A",))
        sid_a = c.execute("SELECT id FROM speakers WHERE name='Speaker A'").fetchone()["id"]
        eid = enrichment._upsert_entity(c, "person", "Особа")
        c.execute("UPDATE entities SET speaker_id=? WHERE id=?", (sid_a, eid))
        c.execute("INSERT INTO speakers (name) VALUES (?)", ("Особа",))
        conn.commit()
        enrichment._upsert_entity(c, "person", "Особа")  # a repeated call with a possible match
        conn.commit()
        row = c.execute("SELECT speaker_id FROM entities WHERE id=?", (eid,)).fetchone()
        assert row["speaker_id"] == sid_a


# ============================================================
# _recompute_entity_aggregates
# ============================================================

def test_recompute_entity_aggregates_sums_mentions_and_meetings(db_path):
    # Create the transcripts UP FRONT in separate short connections — do not
    # interleave with the open (uncommitted) transaction below, otherwise
    # SQLite gives "database is locked" (writer lock of competing connections).
    tid1 = _insert_transcription(db_path)
    tid2 = _insert_transcription(db_path)
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid = enrichment._upsert_entity(c, "person", "Андрій")
        c.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count) "
                  "VALUES (?, ?, 3)", (tid1, eid))
        c.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count) "
                  "VALUES (?, ?, 5)", (tid2, eid))
        enrichment._recompute_entity_aggregates(c, {eid})
        conn.commit()
        row = c.execute("SELECT mention_count, meeting_count FROM entities WHERE id=?",
                        (eid,)).fetchone()
        assert row["mention_count"] == 8
        assert row["meeting_count"] == 2


# ============================================================
# _persist_card
# ============================================================

def _sample_card():
    return {
        "summary": "Обговорили бюджет.", "key_points": ["Бюджет 40к"],
        "action_items": [{"task": "Надіслати кошторис", "owner": "Андрій Коваль", "due": "2026-05-20"}],
        "topics": ["Бюджет"],
        "people": [{"name": "Андрій Коваль", "role": "CTO", "aliases": ["Дрю"]}],
        "projects": [{"name": "Проєкт X"}],
        "orgs": ["Ромашка ТОВ"],
        "model": "claude-test",
        "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0,
    }


def test_persist_card_writes_summary_topics_and_entities(db_path):
    tid = _insert_transcription(db_path, transcript_text="Андрій Коваль обговорює бюджет проєкту X.")
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, _sample_card(), "Андрій Коваль обговорює бюджет проєкту X.",
                                 created_at="2026-05-12 10:00:00", existing_meeting_date=None)
        conn.commit()

        row = c.execute("SELECT summary_json, topics_json, meeting_date, enriched_at "
                        "FROM transcriptions WHERE id=?", (tid,)).fetchone()
        assert row["enriched_at"] is not None
        assert row["meeting_date"] == "2026-05-12"
        import json
        summary = json.loads(row["summary_json"])
        assert summary["summary"] == "Обговорили бюджет."
        assert summary["action_items"][0]["task"] == "Надіслати кошторис"

        people = c.execute("SELECT canonical_name FROM entities WHERE type='person'").fetchall()
        assert {r["canonical_name"] for r in people} == {"Андрій Коваль"}

        ai = c.execute("SELECT task, owner_name, owner_entity_id FROM action_items "
                       "WHERE transcription_id=?", (tid,)).fetchone()
        assert ai["task"] == "Надіслати кошторис"
        assert ai["owner_entity_id"] is not None  # owner resolved to a person entity


def test_persist_card_rerun_is_idempotent_not_duplicated(db_path):
    tid = _insert_transcription(db_path)
    card = _sample_card()
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, card, "текст", "2026-05-12 10:00:00", None)
        conn.commit()
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        # a second pass with a DIFFERENT set of entities — the old ones must be replaced, not added to
        card2 = dict(card, people=[{"name": "Інша Особа"}], action_items=[])
        enrichment._persist_card(c, tid, card2, "текст", "2026-05-12 10:00:00", "2026-05-12")
        conn.commit()

        links = c.execute("SELECT COUNT(*) AS n FROM meeting_entities WHERE transcription_id=?",
                          (tid,)).fetchone()["n"]
        people_names = {r["canonical_name"] for r in
                        c.execute("SELECT e.canonical_name FROM entities e "
                                  "JOIN meeting_entities me ON me.entity_id = e.id "
                                  "WHERE me.transcription_id=? AND e.type='person'", (tid,))}
        assert people_names == {"Інша Особа"}
        actions = c.execute("SELECT COUNT(*) AS n FROM action_items WHERE transcription_id=?",
                            (tid,)).fetchone()["n"]
        assert actions == 0  # overwritten with an empty list
        # the old person entity stayed in entities (the global graph), but without a link to this meeting
        old_people = c.execute("SELECT canonical_name FROM entities WHERE type='person'").fetchall()
        assert "Андрій Коваль" in {r["canonical_name"] for r in old_people}


def test_persist_card_meeting_date_falls_back_to_created_at(db_path):
    tid = _insert_transcription(db_path)
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, _sample_card(), "текст",
                                 created_at="2026-06-01 08:30:00", existing_meeting_date=None)
        conn.commit()
        row = c.execute("SELECT meeting_date FROM transcriptions WHERE id=?", (tid,)).fetchone()
        assert row["meeting_date"] == "2026-06-01"


def test_persist_card_preserves_existing_meeting_date(db_path):
    tid = _insert_transcription(db_path)
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, _sample_card(), "текст",
                                 created_at="2026-06-01 08:30:00", existing_meeting_date="2026-01-15")
        conn.commit()
        row = c.execute("SELECT meeting_date FROM transcriptions WHERE id=?", (tid,)).fetchone()
        assert row["meeting_date"] == "2026-01-15"


# ============================================================
# _enrich_card: idempotency + force + soft-degrade (T6.3)
# ============================================================

def test_enrich_card_not_found(db_path):
    res = enrichment._enrich_card(db_path, 9999)
    assert res == {"status": "not_found", "transcription_id": 9999}


def test_enrich_card_empty_body_returns_empty_status(db_path):
    tid = _insert_transcription(db_path, transcript_text="", polished_text=None)
    res = enrichment._enrich_card(db_path, tid)
    assert res == {"status": "empty", "transcription_id": tid}


def test_enrich_card_skips_when_already_enriched_same_version(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(1) or _sample_card())
    enrichment._enrich_card(db_path, tid)
    assert len(calls) == 1
    res2 = enrichment._enrich_card(db_path, tid)
    assert res2["status"] == "skipped"
    assert len(calls) == 1, "the second attempt without force must NOT call Claude again"


def test_enrich_card_force_reruns_even_if_already_enriched(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(1) or _sample_card())
    enrichment._enrich_card(db_path, tid)
    res2 = enrichment._enrich_card(db_path, tid, force=True)
    assert res2["status"] == "enriched"
    assert len(calls) == 2


def test_enrich_card_reruns_when_version_bumped(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: _sample_card())
    enrichment._enrich_card(db_path, tid)
    monkeypatch.setattr(enrichment, "ENRICHMENT_VERSION", enrichment.ENRICHMENT_VERSION + 1)
    res2 = enrichment._enrich_card(db_path, tid)
    assert res2["status"] == "enriched", "the bumped ENRICHMENT_VERSION should have forced a re-run"


def test_enrich_card_skips_telegram_without_force(db_path, monkeypatch):
    """Phase 17: TG is embed-only, a Claude card is not spent on it.

    The decision applied only on the ingest path (telegram.py:_submit_embed_only), while
    backfill and the indexer button came in through the back door: the card phase had
    no source_type check at all. On live data that is 3142 one-line chat messages
    («Ок», «Дякую») -> paid Claude calls with one click.
    """
    tid = _insert_transcription(db_path, source_type="telegram",
                                source_name="[TG] Fund: привіт")
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(1) or _sample_card())
    res = enrichment._enrich_card(db_path, tid)
    assert res["status"] == "skipped" and res["reason"] == "telegram_embed_only"
    assert not calls, "Claude must not be called on a TG record"

    # The manual bypass remains: Wave 5 will make TG enrichment selective.
    res2 = enrichment._enrich_card(db_path, tid, force=True)
    assert res2["status"] == "enriched" and len(calls) == 1


def test_list_unenriched_skips_telegram_needing_only_card(db_path):
    """The indexer must not count TG records as work if all they lack is a card:
    otherwise the user sees a volume that does not exist (records on live data)."""
    tg_tid = _insert_transcription(db_path, source_type="telegram",
                                   embedded_at="2026-05-12 10:00:00",
                                   embedding_model=enrichment.embeddings.EMBED_MODEL,
                                   embedding_version=enrichment.embeddings.EMBED_VERSION)
    file_tid = _insert_transcription(db_path, source_type="file",
                                     embedded_at="2026-05-12 10:00:00",
                                     embedding_model=enrichment.embeddings.EMBED_MODEL,
                                     embedding_version=enrichment.embeddings.EMBED_VERSION)
    ids = enrichment.list_unenriched_ids(db_path)
    assert tg_tid not in ids, "TG without a card is not work for the indexer"
    assert file_tid in ids, "an ordinary record without a card stays work"


def test_list_unenriched_keeps_telegram_needing_embed(db_path):
    """But if a TG record lacks VECTORS it is work: that is how it gets into search."""
    tg_tid = _insert_transcription(db_path, source_type="telegram")
    assert tg_tid in enrichment.list_unenriched_ids(db_path)


def test_enrich_card_soft_degrades_on_claude_exception(db_path, monkeypatch):
    """T6.3: a Claude call failure is NOT propagated — returns status=retry_needed,
    so as not to break the embed phase/the whole backfill pass."""
    tid = _insert_transcription(db_path)

    def _boom(*a, **k):
        raise RuntimeError("Claude API 529 overloaded")

    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card", _boom)
    res = enrichment._enrich_card(db_path, tid)
    assert res["status"] == "retry_needed"
    assert "529" in res["error"]
    # the row is NOT marked enriched (so the next backfill picks it up and tries again)
    row_enriched = enrichment.list_unenriched_ids(db_path)
    assert tid in row_enriched


def test_enrich_transcription_soft_degrade_still_runs_embed_phase(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: True)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: True)
    embed_calls = []

    def fake_embed(db_path_, transcription_id, force=False):
        embed_calls.append(transcription_id)
        return {"status": "embedded", "transcription_id": transcription_id, "chunks": 1}

    monkeypatch.setattr(enrichment.embeddings, "chunk_and_embed_transcription", fake_embed)

    res = enrichment.enrich_transcription(db_path, tid)
    assert res["card"]["status"] == "retry_needed"
    assert res["embed"]["status"] == "embedded"
    assert res["status"] == "done"  # the embed phase did the work despite the card-phase failure
    assert embed_calls == [tid]


# ============================================================
# list_unenriched_ids
# ============================================================

def test_list_unenriched_ids_excludes_fully_enriched(db_path, monkeypatch):
    tid_done = _insert_transcription(db_path)
    tid_pending = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: _sample_card())
    monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: False)
    enrichment._enrich_card(db_path, tid_done)
    with get_db_connection(db_path) as conn:
        conn.execute("UPDATE transcriptions SET embedded_at=CURRENT_TIMESTAMP, "
                     "embedding_model=?, embedding_version=? WHERE id=?",
                     (enrichment.embeddings.EMBED_MODEL,
                      enrichment.embeddings.EMBED_VERSION, tid_done))
        conn.commit()

    ids = enrichment.list_unenriched_ids(db_path)
    assert tid_pending in ids
    assert tid_done not in ids


def test_list_unenriched_ids_includes_stale_embedding_version(db_path, monkeypatch):
    """T6.5: a change of chunking logic is visible ONLY by embedding_version. If the work
    list does not check it, a bump of EMBED_VERSION stays a declaration: nobody
    will re-encode the archive — the indexer does not name such records."""
    tid = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: _sample_card())
    monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: False)
    enrichment._enrich_card(db_path, tid)
    with get_db_connection(db_path) as conn:
        conn.execute("UPDATE transcriptions SET embedded_at=CURRENT_TIMESTAMP, "
                     "embedding_model=?, embedding_version=? WHERE id=?",
                     (enrichment.embeddings.EMBED_MODEL,
                      enrichment.embeddings.EMBED_VERSION - 1, tid))
        conn.commit()

    assert tid in enrichment.list_unenriched_ids(db_path)

    # ...and NULL (records from before migration v28) — also old chunking, also work
    with get_db_connection(db_path) as conn:
        conn.execute("UPDATE transcriptions SET embedding_version=NULL WHERE id=?", (tid,))
        conn.commit()
    assert tid in enrichment.list_unenriched_ids(db_path)


def test_list_unenriched_ids_excludes_soft_deleted(db_path):
    tid = _insert_transcription(db_path, deleted_at=1234567890.0)
    ids = enrichment.list_unenriched_ids(db_path)
    assert tid not in ids


# ============================================================
# Repair 3 (review #3): duplicate_of is not enriched/not backfilled
# ============================================================

def test_list_unenriched_ids_excludes_duplicate(db_path):
    original = _insert_transcription(db_path)
    dup = _insert_transcription(db_path, duplicate_of=original)
    ids = enrichment.list_unenriched_ids(db_path)
    assert original in ids
    assert dup not in ids


def test_backfill_force_excludes_duplicate(db_path, monkeypatch):
    original = _insert_transcription(db_path)
    dup = _insert_transcription(db_path, duplicate_of=original)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: True)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(1) or _sample_card())
    monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: False)

    enrichment.backfill(db_path, force=True)

    assert calls == [1], "Claude must be called exactly once (for the original), not for the duplicate"
    with get_db_connection(db_path) as conn:
        dup_row = conn.execute(
            "SELECT enriched_at FROM transcriptions WHERE id=?", (dup,)).fetchone()
    assert dup_row["enriched_at"] is None


def test_enrich_transcription_skips_duplicate(db_path, monkeypatch):
    original = _insert_transcription(db_path)
    dup = _insert_transcription(db_path, duplicate_of=original)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: True)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(1) or _sample_card())
    monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: True)
    embed_calls = []
    monkeypatch.setattr(
        enrichment.embeddings, "chunk_and_embed_transcription",
        lambda db_path_, transcription_id, force=False: embed_calls.append(transcription_id)
        or {"status": "embedded", "transcription_id": transcription_id, "chunks": 1},
    )

    res = enrichment.enrich_transcription(db_path, dup)

    assert res["status"] == "skipped_duplicate"
    assert not calls, "Claude must not be called on duplicates"
    assert not embed_calls, "chunks/embeddings must not be built for a duplicate"
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT enriched_at FROM transcriptions WHERE id=?", (dup,)).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE transcription_id=?", (dup,)).fetchone()
    assert row["enriched_at"] is None
    assert chunks["n"] == 0

    # The original of the same text is enriched as before.
    res_orig = enrichment.enrich_transcription(db_path, original)
    assert res_orig["card"]["status"] == "enriched"
    assert calls == [1]


# ============================================================
# enrich_transcription: model keyword-only + passing through to extract_meeting_card
# (production-rag-wave-b-03)
# ============================================================

def test_enrich_transcription_model_is_keyword_only(db_path):
    with pytest.raises(TypeError):
        enrichment.enrich_transcription(db_path, 1, "claude-sonnet-5")  # noqa: not keyword


def test_enrich_transcription_model_reaches_extract_meeting_card(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: True)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(k.get("model")) or _sample_card())
    monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: False)
    enrichment.enrich_transcription(db_path, tid, model="claude-sonnet-5")
    assert calls == ["claude-sonnet-5"]


def test_enrich_transcription_model_none_keeps_default_behavior(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "is_available", lambda: True)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(k.get("model")) or _sample_card())
    monkeypatch.setattr(enrichment.embeddings, "is_available", lambda: False)
    enrichment.enrich_transcription(db_path, tid)  # model not passed -> None
    assert calls == [None]


# ============================================================
# text_polishing._truncate_card_body: length ceiling of extract_meeting_card
# (production-rag-wave-b-03 — there was no ceiling at all before)
# ============================================================

def test_truncate_card_body_noop_under_ceiling():
    body = "короткий транскрипт"
    assert text_polishing._truncate_card_body(body) == body


def test_truncate_card_body_cuts_head_and_tail_over_ceiling(caplog):
    body = "A" * (text_polishing._MAX_CARD_CHARS + 50_000)
    caplog.set_level("WARNING")
    result = text_polishing._truncate_card_body(body)
    assert len(result) < len(body)
    assert len(result) <= text_polishing._MAX_CARD_CHARS + len("\n\n[…]\n\n")
    assert result.startswith("A")
    assert result.endswith("A")
    assert "[…]" in result
    assert any("text too long" in r.message for r in caplog.records)


# ============================================================
# CLI backfill-cards (production-rag-wave-b-03) — card phase only, --dry-run
# ============================================================

def test_select_backfill_card_rows_excludes_telegram_deleted_duplicate_and_done(db_path):
    ok_call = _insert_transcription(db_path, source_type="file")
    ok_doc = _insert_transcription(db_path, source_type="document")
    _insert_transcription(db_path, source_type="telegram")
    _insert_transcription(db_path, deleted_at=1234567890.0)
    _insert_transcription(db_path, duplicate_of=ok_call)
    _insert_transcription(db_path, source_type="file", summary_json='{"summary": "готово"}')

    ids = {r["id"] for r in enrichment._select_backfill_card_rows(db_path, kind="all")}
    assert ids == {ok_call, ok_doc}


def test_select_backfill_card_rows_kind_calls_vs_docs(db_path):
    call_tid = _insert_transcription(db_path, source_type="youtube")
    doc_tid = _insert_transcription(db_path, source_type="document")

    calls_ids = {r["id"] for r in enrichment._select_backfill_card_rows(db_path, kind="calls")}
    docs_ids = {r["id"] for r in enrichment._select_backfill_card_rows(db_path, kind="docs")}
    assert calls_ids == {call_tid}
    assert docs_ids == {doc_tid}


def test_select_backfill_card_rows_invalid_kind_raises(db_path):
    with pytest.raises(ValueError):
        enrichment._select_backfill_card_rows(db_path, kind="bogus")


def test_select_backfill_card_rows_respects_limit(db_path):
    for _ in range(3):
        _insert_transcription(db_path, source_type="file")
    rows = enrichment._select_backfill_card_rows(db_path, limit=2)
    assert len(rows) == 2


def test_dry_run_report_counts_chars_cost_and_top10_without_writing(db_path):
    tid_short = _insert_transcription(db_path, transcript_text="а" * 100)
    tid_long = _insert_transcription(db_path, transcript_text="б" * 300)

    rows = enrichment._select_backfill_card_rows(db_path)
    report = enrichment._dry_run_report(rows, "claude-sonnet-5")

    assert report["count"] == 2
    assert report["total_chars"] == 400
    assert report["tokens_est"] == 100
    assert report["top10"][0] == {"id": tid_long, "chars": 300}
    assert report["cost_est_usd_input_only"] > 0

    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT summary_json FROM transcriptions WHERE id=?",
                           (tid_short,)).fetchone()
    assert row["summary_json"] is None


def test_backfill_cards_runs_once_then_zero_on_rerun(db_path, monkeypatch):
    _insert_transcription(db_path)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(1) or _sample_card())

    result1 = enrichment.backfill_cards(db_path)
    assert result1 == {"total": 1, "done": 1, "skipped": 0, "failed": 0}
    assert len(calls) == 1

    result2 = enrichment.backfill_cards(db_path)
    assert result2 == {"total": 0, "done": 0, "skipped": 0, "failed": 0}
    assert len(calls) == 1, "a rerun must not have called Claude again"


def test_backfill_cards_logs_and_continues_on_unexpected_error(db_path, monkeypatch):
    tid_boom = _insert_transcription(db_path)
    tid_ok = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: _sample_card())
    orig_enrich_card = enrichment._enrich_card

    def _boom_then_ok(db_path_, transcription_id, model=None, force=False, effort="medium"):
        if transcription_id == tid_boom:
            raise RuntimeError("unexpected error")
        return orig_enrich_card(db_path_, transcription_id, model=model, force=force, effort=effort)

    monkeypatch.setattr(enrichment, "_enrich_card", _boom_then_ok)
    result = enrichment.backfill_cards(db_path)
    assert result == {"total": 2, "done": 1, "skipped": 0, "failed": 1}
    assert tid_ok  # the record without the boom error did get processed


def test_cli_dry_run_after_subcommand_prints_report_without_writing(db_path, capsys):
    tid = _insert_transcription(db_path)
    assert enrichment.main(["--db", db_path, "backfill-cards", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Records without a card: 1" in out
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT summary_json FROM transcriptions WHERE id=?", (tid,)).fetchone()
    assert row["summary_json"] is None


def test_cli_dry_run_before_subcommand(db_path, capsys):
    """--dry-run works from anywhere on the line (like dedup_audio.py)."""
    _insert_transcription(db_path)
    assert enrichment.main(["--db", db_path, "--dry-run", "backfill-cards"]) == 0
    out = capsys.readouterr().out
    assert "Records without a card: 1" in out


def test_cli_without_dry_run_runs_real_backfill(db_path, monkeypatch, capsys):
    tid = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: _sample_card())
    assert enrichment.main(["--db", db_path, "backfill-cards"]) == 0
    capsys.readouterr()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT summary_json FROM transcriptions WHERE id=?", (tid,)).fetchone()
    assert row["summary_json"] is not None


def test_cli_model_override_reaches_extract_meeting_card(db_path, monkeypatch):
    _insert_transcription(db_path)
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(k.get("model")) or _sample_card())
    assert enrichment.main(
        ["--db", db_path, "backfill-cards", "--model", "claude-haiku-4-5-20251001"]) == 0
    assert calls == ["claude-haiku-4-5-20251001"]


# ============================================================
# Story 06: tag suggestions -> suggested_tags_json
# ============================================================

def _card_with_tags(tags):
    card = _sample_card()
    card["tags"] = tags
    return card


def test_filter_suggested_tags_dedup_length_and_cap():
    """Acceptance: casefold duplicates collapse, an overlong name (>40) is dropped,
    the result is truncated to exactly 5."""
    long_name = "x" * 50
    tags = ["AI", " ai ", "Інвестори", long_name, "a", "b", "c", "d"]
    result = enrichment._filter_suggested_tags(tags, existing_names=[])
    assert result == ["AI", "Інвестори", "a", "b", "c"]
    assert len(result) == 5
    assert long_name not in result


def test_filter_suggested_tags_excludes_existing_record_tags():
    tags = ["AI", "Інвестори"]
    result = enrichment._filter_suggested_tags(tags, existing_names=["ai"])
    assert result == ["Інвестори"]


def test_persist_card_missing_tags_key_leaves_suggestions_null(db_path):
    tid = _insert_transcription(db_path)
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, _sample_card(), "текст",
                                 created_at="2026-05-12 10:00:00", existing_meeting_date=None)
        conn.commit()
        row = c.execute("SELECT suggested_tags_json FROM transcriptions WHERE id=?",
                        (tid,)).fetchone()
    assert row["suggested_tags_json"] is None


def test_persist_card_writes_filtered_suggested_tags(db_path):
    tid = _insert_transcription(db_path)
    long_name = "x" * 50
    card = _card_with_tags(["AI", " ai ", "Інвестори", long_name, "a", "b", "c", "d"])
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, card, "текст",
                                 created_at="2026-05-12 10:00:00", existing_meeting_date=None)
        conn.commit()
        row = c.execute("SELECT suggested_tags_json FROM transcriptions WHERE id=?",
                        (tid,)).fetchone()
    import json
    assert json.loads(row["suggested_tags_json"]) == ["AI", "Інвестори", "a", "b", "c"]


def test_persist_card_excludes_names_already_in_record_tags(db_path):
    tid = _insert_transcription(db_path)
    with get_db_connection(db_path) as conn:
        categories.set_tags(conn, tid, add=["AI"], source="manual")
    card = _card_with_tags(["AI", "Інвестори"])
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, card, "текст",
                                 created_at="2026-05-12 10:00:00", existing_meeting_date=None)
        conn.commit()
        row = c.execute("SELECT suggested_tags_json FROM transcriptions WHERE id=?",
                        (tid,)).fetchone()
    import json
    assert json.loads(row["suggested_tags_json"]) == ["Інвестори"]


def test_enrich_card_passes_tag_vocabulary_to_extract_meeting_card(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    with get_db_connection(db_path) as conn:
        categories.ensure_tag(conn, "Інвестори")
        categories.ensure_tag(conn, "AI")
    captured = {}
    monkeypatch.setattr(
        enrichment.text_polishing, "extract_meeting_card",
        lambda *a, **k: captured.update(k) or _sample_card(),
    )
    enrichment._enrich_card(db_path, tid)
    assert sorted(captured["tag_vocabulary"]) == ["AI", "Інвестори"]


def test_backfill_cards_writes_suggested_tags_same_path(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: _card_with_tags(["AI", "Інвестори"]))
    enrichment.backfill_cards(db_path)
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT suggested_tags_json FROM transcriptions WHERE id=?",
                           (tid,)).fetchone()
    import json
    assert json.loads(row["suggested_tags_json"]) == ["AI", "Інвестори"]


def test_backfill_cards_dry_run_does_not_write_suggested_tags(db_path, monkeypatch):
    tid = _insert_transcription(db_path)
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: _card_with_tags(["AI"]))
    assert enrichment.main(["--db", db_path, "backfill-cards", "--dry-run"]) == 0
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT suggested_tags_json FROM transcriptions WHERE id=?",
                           (tid,)).fetchone()
    assert row["suggested_tags_json"] is None


# ============================================================
# Story 06: tag vocabulary in the extract_meeting_card prompt
# ============================================================

class _FakeCardBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeCardUsage:
    def __init__(self):
        self.input_tokens = 10
        self.output_tokens = 5
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _FakeCardResult:
    def __init__(self, payload: dict):
        import json
        self.content = [_FakeCardBlock(json.dumps(payload, ensure_ascii=False))]
        self.model = "claude-test"
        self.usage = _FakeCardUsage()


def test_extract_meeting_card_prompt_contains_tag_vocabulary(monkeypatch):
    captured = {}

    def fake_stream(client, timeout, request_kwargs, what="polish"):
        captured["kwargs"] = request_kwargs
        return _FakeCardResult({"summary": "s", "tags": ["AI"]})

    monkeypatch.setattr(text_polishing, "_get_client", lambda: object())
    monkeypatch.setattr(text_polishing, "_stream_with_retry", fake_stream)

    text_polishing.extract_meeting_card("текст", tag_vocabulary=["Інвестори", "AI"])

    user_msg = captured["kwargs"]["messages"][0]["content"]
    assert "Інвестори" in user_msg
    assert "AI" in user_msg


def test_extract_meeting_card_empty_vocabulary_still_asks_for_tags(monkeypatch):
    captured = {}

    def fake_stream(client, timeout, request_kwargs, what="polish"):
        captured["kwargs"] = request_kwargs
        return _FakeCardResult({"summary": "s"})

    monkeypatch.setattr(text_polishing, "_get_client", lambda: object())
    monkeypatch.setattr(text_polishing, "_stream_with_retry", fake_stream)

    text_polishing.extract_meeting_card("текст", tag_vocabulary=[])

    system_text = captured["kwargs"]["system"][0]["text"]
    assert '"tags"' in system_text
