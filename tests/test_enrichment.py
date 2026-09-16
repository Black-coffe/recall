"""Юніт-тести Meeting Memory enrichment (app/services/enrichment.py) — T7.5.

Офлайн, реальна (тимчасова, tmp_path) SQLite БД через init_database
(патерн tests/test_migrations.py) — без Claude API/мережі. Покриває:
  - _normalize / _count_mentions
  - _find_entity / _upsert_entity: створення, дедуп (регістр/пробіли/пунктуація),
    пошук через alias, ідемпотентність повторного виклику, лінк на speakers
  - _recompute_entity_aggregates
  - _enrich_card: idempotency (enriched_at + version), force re-run,
    м'яка деградація на збій Claude-виклику (T6.3 retry_needed)
  - _persist_card: повний round-trip (summary/topics/entities/action_items),
    idempotent re-run (стара розмітка перезаписується, не дублюється)
  - list_unenriched_ids: фільтрація вже збагачених / soft-deleted
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services import enrichment


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
    assert enrichment._count_mentions("A б в", ["A"]) == 0  # довжина < 2 — ігнорується


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
        assert eid1 == eid2, f"варіант {variant!r} мав дедупитись до тієї ж сутності"
        n = c.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
        assert n == 1


def test_upsert_entity_dedup_via_alias(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid1 = enrichment._upsert_entity(c, "person", "Андрій Коваль", aliases=["Дрю"])
        eid2 = enrichment._upsert_entity(c, "person", "Дрю")  # той самий, тільки через alias
        conn.commit()
        assert eid1 == eid2
        n = c.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
        assert n == 1


def test_find_entity_respects_type_boundary(db_path):
    """Той самий normalized-рядок в іншому типі — окрема сутність (alias унікальний
    глобально, тому _find_entity ще й перевіряє збіг типу)."""
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid_person = enrichment._upsert_entity(c, "person", "Фонд")
        eid_org = enrichment._upsert_entity(c, "org", "Фонд Х")  # різне ім'я — не колізія alias
        conn.commit()
        assert eid_person != eid_org
        # прямий пошук по типу не має плутати типи
        found_person = enrichment._find_entity(c, "person", "фонд")
        found_org = enrichment._find_entity(c, "org", "фонд")
        assert found_person == eid_person
        assert found_org is None  # "фонд" (без Х) не існує як org


def test_upsert_entity_adds_new_aliases_incrementally(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid = enrichment._upsert_entity(c, "person", "Андрій Коваль", aliases=["Дрю"])
        enrichment._upsert_entity(c, "person", "Андрій Коваль", aliases=["A.K."])
        conn.commit()
        aliases = {r["normalized_alias"] for r in
                   c.execute("SELECT normalized_alias FROM entity_aliases WHERE entity_id=?", (eid,))}
        # _normalize стрижe пунктуацію на КРАЯХ рядка ("A.K." -> "a.k" — кінцева
        # крапка теж пунктуація-на-краю і зрізається).
        assert {"дрю", "a.k", "андрій коваль"} <= aliases


def test_upsert_entity_fills_role_only_when_previously_empty(db_path):
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        eid = enrichment._upsert_entity(c, "person", "Андрій Коваль")  # без ролі
        enrichment._upsert_entity(c, "person", "Андрій Коваль", role="CTO")
        role1 = c.execute("SELECT role FROM entities WHERE id=?", (eid,)).fetchone()["role"]
        assert role1 == "CTO"
        # повторний виклик з ІНШОЮ роллю НЕ перезаписує вже заповнену
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
        eid = enrichment._upsert_entity(c, "person", "Андрій Коваль")  # точний збіг
        conn.commit()
        row = c.execute("SELECT speaker_id FROM entities WHERE id=?", (eid,)).fetchone()
        assert row["speaker_id"] is not None


def test_upsert_entity_links_speaker_ascii_case_insensitive(db_path):
    """speakers.name COLLATE NOCASE — SQLite NOCASE фолдить лише ASCII, не кирилицю.
    Перевіряємо на латиниці, де ця гарантія справді працює."""
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
        enrichment._upsert_entity(c, "person", "Особа")  # повторний виклик з можливим матчем
        conn.commit()
        row = c.execute("SELECT speaker_id FROM entities WHERE id=?", (eid,)).fetchone()
        assert row["speaker_id"] == sid_a


# ============================================================
# _recompute_entity_aggregates
# ============================================================

def test_recompute_entity_aggregates_sums_mentions_and_meetings(db_path):
    # Транскрипти створюємо ЗАЗДАЛЕГІДЬ окремими короткими з'єднаннями — не
    # переплітаємо з відкритою (незакомміченою) транзакцією нижче, інакше
    # SQLite віддає "database is locked" (writer-лок конкуруючих з'єднань).
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
        assert ai["owner_entity_id"] is not None  # owner resolved до person-сутності


def test_persist_card_rerun_is_idempotent_not_duplicated(db_path):
    tid = _insert_transcription(db_path)
    card = _sample_card()
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        enrichment._persist_card(c, tid, card, "текст", "2026-05-12 10:00:00", None)
        conn.commit()
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        # другий прогін з ІНШИМ набором сутностей — старе має бути замінено, не додано
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
        assert actions == 0  # перезаписано порожнім списком
        # стара сутність-людина лишилась у entities (глобальний граф), але без лінку на цей мітинг
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
    assert len(calls) == 1, "друга спроба без force НЕ мала викликати Claude знову"


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
    assert res2["status"] == "enriched", "бампнута ENRICHMENT_VERSION мала форсувати re-run"


def test_enrich_card_skips_telegram_without_force(db_path, monkeypatch):
    """Phase 17: TG — embed-only, картка Claude на нього не витрачається.

    Рішення діяло лише на шляху інжесту (telegram.py:_submit_embed_only), а
    backfill і кнопка індексера заходили з чорного ходу: у card-фазі не було
    жодної перевірки source_type. На живих даних це 3142 чатових однорядковики
    («Ок», «Дякую») → платні виклики Claude одним натисканням.
    """
    tid = _insert_transcription(db_path, source_type="telegram",
                                source_name="[TG] Fund: привіт")
    calls = []
    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card",
                        lambda *a, **k: calls.append(1) or _sample_card())
    res = enrichment._enrich_card(db_path, tid)
    assert res["status"] == "skipped" and res["reason"] == "telegram_embed_only"
    assert not calls, "Claude не мав викликатись на TG-записі"

    # Ручний обхід лишається: Волна 5 зробить вибіркове збагачення TG.
    res2 = enrichment._enrich_card(db_path, tid, force=True)
    assert res2["status"] == "enriched" and len(calls) == 1


def test_list_unenriched_skips_telegram_needing_only_card(db_path):
    """Індексер не має рахувати TG-записи як роботу, якщо їм бракує лише картки:
    інакше користувач бачить обсяг, якого не існує (3142 записи на живих даних)."""
    tg_tid = _insert_transcription(db_path, source_type="telegram",
                                   embedded_at="2026-05-12 10:00:00",
                                   embedding_model=enrichment.embeddings.EMBED_MODEL,
                                   embedding_version=enrichment.embeddings.EMBED_VERSION)
    file_tid = _insert_transcription(db_path, source_type="file",
                                     embedded_at="2026-05-12 10:00:00",
                                     embedding_model=enrichment.embeddings.EMBED_MODEL,
                                     embedding_version=enrichment.embeddings.EMBED_VERSION)
    ids = enrichment.list_unenriched_ids(db_path)
    assert tg_tid not in ids, "TG без картки — не робота для індексера"
    assert file_tid in ids, "звичайний запис без картки лишається роботою"


def test_list_unenriched_keeps_telegram_needing_embed(db_path):
    """Але якщо TG-запису бракує ВЕКТОРІВ — він робота: саме так він потрапляє в пошук."""
    tg_tid = _insert_transcription(db_path, source_type="telegram")
    assert tg_tid in enrichment.list_unenriched_ids(db_path)


def test_enrich_card_soft_degrades_on_claude_exception(db_path, monkeypatch):
    """T6.3: збій Claude-виклику НЕ прокидається — повертає status=retry_needed,
    щоб не валити embed-фазу/увесь backfill-прохід."""
    tid = _insert_transcription(db_path)

    def _boom(*a, **k):
        raise RuntimeError("Claude API 529 overloaded")

    monkeypatch.setattr(enrichment.text_polishing, "extract_meeting_card", _boom)
    res = enrichment._enrich_card(db_path, tid)
    assert res["status"] == "retry_needed"
    assert "529" in res["error"]
    # рядок НЕ позначено enriched (щоб наступний backfill підхопив і спробував знову)
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
    assert res["status"] == "done"  # embed-фаза виконала роботу, попри збій card-фази
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
    """T6.5: зміна логіки чанкінгу видна ЛИШЕ по embedding_version. Якщо список
    роботи її не звіряє, бамп EMBED_VERSION лишається декларацією: перекодувати
    архів нікому — індексер таких записів не називає."""
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

    # …і NULL (записи до міграції v28) — теж стара нарізка, теж робота
    with get_db_connection(db_path) as conn:
        conn.execute("UPDATE transcriptions SET embedding_version=NULL WHERE id=?", (tid,))
        conn.commit()
    assert tid in enrichment.list_unenriched_ids(db_path)


def test_list_unenriched_ids_excludes_soft_deleted(db_path):
    tid = _insert_transcription(db_path, deleted_at=1234567890.0)
    ids = enrichment.list_unenriched_ids(db_path)
    assert tid not in ids


# ============================================================
# Ремонт 3 (review #3): duplicate_of не збагачується/не бекфіляється
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

    assert calls == [1], "Claude мав викликатись рівно раз (для оригіналу), не для дубля"
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
    assert not calls, "Claude не мав викликатись на дублі"
    assert not embed_calls, "чанки/ембеддинги не мали будуватись для дубля"
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT enriched_at FROM transcriptions WHERE id=?", (dup,)).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE transcription_id=?", (dup,)).fetchone()
    assert row["enriched_at"] is None
    assert chunks["n"] == 0

    # Оригінал того самого тексту збагачується як і раніше.
    res_orig = enrichment.enrich_transcription(db_path, original)
    assert res_orig["card"]["status"] == "enriched"
    assert calls == [1]
