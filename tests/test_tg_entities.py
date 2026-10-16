"""Tests of linking Telegram to the entity graph (Wave 4.5.3).

No model and no network. The main thing checked is the rule "a mention must
look like a proper name in the text itself": it is what separates «Адам» from
«документ» and «Том» from «тому». Without it the graph becomes worse than it was, because
enrichment wrote ordinary words into the aliases.
"""
import sqlite3

import pytest

from app.db.migrations import init_database
from app.services import tg_entities


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _entity(path, name, etype="person", aliases=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO entities (type, canonical_name, normalized_name) VALUES (?, ?, ?)",
        (etype, name, name.casefold()))
    eid = cur.lastrowid
    for a in aliases:
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                     "VALUES (?, ?, ?)", (eid, a, a.casefold()))
    conn.commit()
    conn.close()
    return eid


def _msg(path, text, *, thread_id=1, chat_id=-100, msg_id=None):
    conn = sqlite3.connect(path)
    if msg_id is None:
        msg_id = (conn.execute("SELECT COALESCE(MAX(tg_message_id),0) FROM transcriptions "
                               "WHERE tg_chat_id=?", (chat_id,)).fetchone()[0] or 0) + 1
    conn.execute("INSERT OR IGNORE INTO tg_threads (id, chat_id, status) VALUES (?, ?, 'open')",
                 (thread_id, chat_id))
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, tg_chat_id, "
        "tg_message_id, tg_date, tg_thread_id) VALUES ('telegram', ?, ?, ?, ?, "
        "'2026-06-01T10:00:00+00:00', ?)",
        (f"[TG] {text[:20]}", text, chat_id, msg_id, thread_id))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _links(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute("SELECT * FROM meeting_entities")]
    conn.close()
    return out


# ============================================================
# The proper-name rule
# ============================================================

def test_capitalised_mention_is_linked(db):
    eid = _entity(db, "Адам")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("сьогодні Адам підтвердив платіж", names) == {eid}


def test_lowercase_mention_is_ignored(db):
    """«документ» as a project alias caught every invoice in the archive."""
    _entity(db, "Стратегія розвитку області", etype="project", aliases=["документ"])
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("надішліть документ на пошту", names) == set()


def test_common_word_colliding_with_name_is_ignored(db):
    """«Тому» as a person alias gave 144 matches on the word «тому» (= because of that)."""
    _entity(db, "Том", aliases=["Тому", "Тома"])
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("я тому і питаю про це", names) == set()


def test_sentence_start_does_not_prove_a_name(db):
    """A capital letter after a period proves nothing."""
    _entity(db, "Стратегія розвитку області", etype="project", aliases=["документ"])
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("Все готово. Документ надіслано.", names) == set()
    assert tg_entities.find_mentions("Документ надіслано", names) == set()


def test_real_name_after_sentence_start_still_found(db):
    """But a real name must not get lost because it stands in the middle."""
    eid = _entity(db, "Ковальчука", etype="project")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("Все готово. По Ковальчука домовились.", names) == {eid}


def test_multiword_name_is_matched(db):
    eid = _entity(db, "Київський авіаційний інститут", etype="org")
    names = tg_entities.load_names(db)
    got = tg_entities.find_mentions("зустріч у Київський авіаційний інститут завтра", names)
    assert got == {eid}


def test_short_names_are_skipped(db):
    """«Ок», «БХ» catch junk and are of no use."""
    _entity(db, "БХ", etype="org")
    assert "бх" not in tg_entities.load_names(db)


def test_topic_entities_are_not_linkable(db):
    """entities of type topic are free text («планування», «бюджет»):
    they would catch half the archive and mean nothing."""
    _entity(db, "Планування", etype="topic")
    assert tg_entities.load_names(db) == {}


def test_word_boundary_is_respected(db):
    _entity(db, "Make", etype="org")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("це Makefile проєкту", names) == set()


# ============================================================
# The link is hung on the THREAD
# ============================================================

def test_link_marks_only_the_message_with_the_mention(db):
    """The temptation was to hang the entity on the whole thread, but the expansion to the thread is already
    done by scope._expand_to_threads. A duplicate in the graph would give 43 482 rows instead of
    ~2 thousand and would make meeting_count and list_stale_topics meaningless."""
    eid = _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука треба інвестора", thread_id=1)
    _msg(db, "а скільки там виходить?", thread_id=1)
    _msg(db, "ок, беремо", thread_id=1)
    res = tg_entities.link_threads(db, dry_run=False)
    assert res["threads_with_mentions"] == 1
    assert res["messages_linked"] == 1, "only the message with the mention"
    assert {l["entity_id"] for l in _links(db)} == {eid}
    assert len(_links(db)) == 1


def test_other_threads_are_untouched(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука треба інвестора", thread_id=1)
    _msg(db, "зовсім інша розмова", thread_id=2)
    tg_entities.link_threads(db, dry_run=False)
    linked = {l["transcription_id"] for l in _links(db)}
    assert len(linked) == 1


def test_dry_run_writes_nothing(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    res = tg_entities.link_threads(db, dry_run=True)
    assert res["dry_run"] is True and res["messages_linked"] == 1
    assert _links(db) == []


def test_link_is_idempotent(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    tg_entities.link_threads(db, dry_run=False)
    tg_entities.link_threads(db, dry_run=False)
    assert len(_links(db)) == 1


def test_link_records_its_provenance(db):
    """A string match and a model extraction are things of different reliability."""
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    tg_entities.link_threads(db, dry_run=False)
    assert _links(db)[0]["source"] == tg_entities.SOURCE


def test_existing_claude_link_is_not_overwritten(db):
    """A link from enrichment is more reliable — it has salience and a role."""
    eid = _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились", thread_id=1)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, mention_count, "
                 "salience, source) VALUES (?, ?, 7, 0.9, NULL)", (tid, eid))
    conn.commit()
    conn.close()

    tg_entities.link_threads(db, dry_run=False)
    row = [l for l in _links(db) if l["transcription_id"] == tid][0]
    assert row["source"] is None
    assert row["salience"] == 0.9
    assert row["mention_count"] == 7


def test_relink_drops_only_own_links(db):
    """A rewrite removes its own links and leaves foreign ones."""
    other = _entity(db, "Барселона", etype="org")
    tid = _msg(db, "тут нічого не згадано", thread_id=1)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, source) "
                 "VALUES (?, ?, NULL)", (tid, other))
    conn.commit()
    conn.close()
    tg_entities.link_threads(db, dry_run=False)
    assert len(_links(db)) == 1 and _links(db)[0]["source"] is None


def test_messages_without_thread_are_skipped(db):
    """Threads are not split yet — there is nothing to link by."""
    _entity(db, "Ковальчука", etype="project")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                 "tg_chat_id, tg_message_id, tg_date) VALUES ('telegram', 'x', "
                 "'по Ковальчука домовились', -100, 1, '2026-06-01T10:00:00+00:00')")
    conn.commit()
    conn.close()
    res = tg_entities.link_threads(db, dry_run=False)
    assert res["messages_total"] == 0
    assert _links(db) == []


def test_stats_report_coverage(db):
    _entity(db, "Ковальчука", etype="project")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    tg_entities.link_threads(db, dry_run=False)
    got = tg_entities.stats(db)
    assert got["telegram_linked"] == 1
    assert got["links_by_source"].get(tg_entities.SOURCE) == 1
    assert got["top_entities"][0]["name"] == "Ковальчука"


# ============================================================
# Linking on ingest (the incremental path)
# ============================================================

@pytest.fixture(autouse=True)
def _clean_names_cache():
    """The names cache lives at module level, while each test has its own temporary DB."""
    tg_entities.reset_names_cache()
    yield
    tg_entities.reset_names_cache()


def test_link_message_links_single_record(db):
    """A new message gets into the graph without a full pass over the archive."""
    eid = _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились")
    res = tg_entities.link_message(db, tid)
    assert res["status"] == "ok" and res["written"] == 1
    assert [(r["transcription_id"], r["entity_id"], r["source"]) for r in _links(db)] == \
        [(tid, eid, tg_entities.SOURCE)]


def test_link_message_is_idempotent(db):
    """A repeat (e.g. re-embedding) does not multiply links."""
    _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились")
    tg_entities.link_message(db, tid)
    tg_entities.link_message(db, tid)
    assert len(_links(db)) == 1


def test_link_message_forgets_mentions_removed_by_edit(db):
    """A text edit removes the mention: the MessageEdited subscription leads here too."""
    _entity(db, "Ковальчука", etype="project")
    tid = _msg(db, "по Ковальчука домовились")
    tg_entities.link_message(db, tid)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET transcript_text = ? WHERE id = ?",
                 ("домовились по іншому обʼєкту", tid))
    conn.commit()
    conn.close()
    assert tg_entities.link_message(db, tid)["written"] == 0
    assert _links(db) == []


def test_link_message_keeps_foreign_links(db):
    """A link from Claude enrichment is more reliable — we do not touch it."""
    other = _entity(db, "Барселона", etype="org")
    tid = _msg(db, "тут нічого не згадано")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, source) "
                 "VALUES (?, ?, NULL)", (tid, other))
    conn.commit()
    conn.close()
    tg_entities.link_message(db, tid)
    assert len(_links(db)) == 1 and _links(db)[0]["source"] is None


def test_link_message_works_without_thread(db):
    """The text proves the mention, not the thread: a split failure does not leave a record outside the graph."""
    eid = _entity(db, "Ковальчука", etype="project")
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_message_id, tg_date) VALUES ('telegram', 'x', "
        "'по Ковальчука домовились', -100, 7, '2026-06-01T10:00:00+00:00')")
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    assert tg_entities.link_message(db, tid)["written"] == 1
    assert _links(db)[0]["entity_id"] == eid


def test_link_message_skips_non_telegram(db):
    """Calls and documents must be enriched by Claude, not by a spelling search."""
    _entity(db, "Ковальчука", etype="project")
    conn = sqlite3.connect(db)
    cur = conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text) "
                       "VALUES ('recording', 'дзвінок', 'по Ковальчука домовились')")
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    assert tg_entities.link_message(db, tid)["status"] == "not_found"
    assert _links(db) == []


def test_names_cache_is_reused_and_resettable(db):
    """The cache is needed, because otherwise every message reads 6200 names."""
    _entity(db, "Ковальчука", etype="project")
    first = tg_entities.names_for(db)
    _entity(db, "Барселона", etype="org")
    assert tg_entities.names_for(db) is first          # the new entity is not visible yet
    tg_entities.reset_names_cache()
    assert "барселона" in tg_entities.names_for(db)


# ============================================================
# Morphological matcher (story 03) — off by default
# ============================================================

@pytest.fixture
def morph_on(monkeypatch):
    """Explicitly turns the flag on for the duration of the test."""
    monkeypatch.setenv("TG_ENTITIES_MORPH_ENABLED", "1")


@pytest.fixture
def morph_off(monkeypatch):
    """Explicitly turns the flag OFF. It is needed explicitly, not "by default": since
    21.08.2026 `TG_ENTITIES_MORPH_ENABLED=1` is in the live `.env`, and any
    test in the suite that pulls in config/dotenv leaves it in `os.environ` — a test
    about the disabled branch must own its environment, not borrow it."""
    monkeypatch.delenv("TG_ENTITIES_MORPH_ENABLED", raising=False)


def test_morph_disabled_by_default_leaves_inflected_form_unlinked(db, morph_off):
    """Without the flag «Юлією» is not linked — exactly the current behavior."""
    _entity(db, "Юлія")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("домовились із Юлією про дзвінок", names) == set()


def test_morph_enabled_links_inflected_forms(db, morph_on):
    """The 14.08 measurement: «Юлією», «Тетяни», «Промприладом» are forms that the exact
    matcher misses."""
    julia = _entity(db, "Юлія")
    tetiana = _entity(db, "Тетяна")
    promprylad = _entity(db, "Промприлад", etype="org")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("домовились із Юлією про дзвінок", names) == {julia}
    assert tg_entities.find_mentions("питання від Тетяни ще відкрите", names) == {tetiana}
    assert tg_entities.find_mentions("рахунок за Промприладом не пройшов", names) == {promprylad}


def test_morph_guard1_exact_wordform_wins_on_its_position(db, morph_on):
    """Guard 1: a token consumed by an exact match (here a multi-word one) is closed
    to the morph branch — even when the stem UNAMBIGUOUSLY (without help from guard 2)
    points to ANOTHER entity. The pair deliberately has no common stem with "Дім Данс":
    if guard 1 is removed, guard 2 will not catch this case, and the test will go red."""
    dim_dans = _entity(db, "Дім Данс", etype="project")  # exact key "дім данс"
    dansy = _entity(db, "Дансі", etype="org")             # stem "данс" — the same as in the token "Данс"
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("команда Дім Данс перемогла", names) == {dim_dans}


def test_morph_multiword_exact_closes_all_its_tokens():
    """A reproduction from the review: `exact_word_positions.add(i)` stood only under
    `size == 1`, so a word INSIDE a multi-word exact match stayed
    open to the morph branch and was credited to a SECOND entity. One
    spelling cannot be credited to two."""
    names = {"дім данс": 1, "данси": 2}
    stems = tg_entities._build_stems(names)
    assert tg_entities._mentions_core(
        "команда Дім Данс перемогла", names, stems) == {1: tg_entities.SOURCE}


def test_morph_guard2_ambiguous_stem_is_rejected(db, morph_on):
    """«Русланою» → both «Руслан» and «Руслана»: the stem is ambiguous, no link.
    One mention cannot be credited to two (entity-merge-moves-names-to-aliases)."""
    _entity(db, "Руслан")
    _entity(db, "Руслана")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("зустрілись з Русланою учора", names) == set()


def test_morph_guard3_all_caps_is_not_proof(db, morph_on):
    """Guard 3: a morph match passes the same `written_as_proper_noun` —
    an ALL-CAPS header is not proof, even when the stem is unambiguous."""
    _entity(db, "Тетяна")
    names = tg_entities.load_names(db)
    assert tg_entities.find_mentions("ЗУСТРІЧ ЩОДО ТЕТЯНИ ЗАВТРА ВРАНЦІ", names) == set()


def test_morph_link_message_writes_separate_source(db, morph_on):
    """`SOURCE_MORPH` is written separately from `SOURCE` in meeting_entities.source."""
    julia = _entity(db, "Юлія")
    tid = _msg(db, "домовились із Юлією про дзвінок")
    res = tg_entities.link_message(db, tid)
    assert res["status"] == "ok" and res["written"] == 1
    row = _links(db)[0]
    assert row["entity_id"] == julia
    assert row["source"] == tg_entities.SOURCE_MORPH
    assert tg_entities.SOURCE_MORPH != tg_entities.SOURCE


def test_morph_relink_replaces_only_its_own_rows(db, morph_on):
    """A repeat pass deletes only its own `thread_morph` rows: foreign ones (NULL) and
    `thread_match` stay untouched."""
    julia = _entity(db, "Юлія")
    other = _entity(db, "Барселона", etype="org")
    tid = _msg(db, "домовились із Юлією про дзвінок")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id, source) "
                 "VALUES (?, ?, NULL)", (tid, other))
    conn.commit()
    conn.close()

    tg_entities.link_message(db, tid)
    tg_entities.link_message(db, tid)     # idempotent, its own row is rewritten

    links = _links(db)
    assert len(links) == 2
    morph_rows = [l for l in links if l["source"] == tg_entities.SOURCE_MORPH]
    foreign_rows = [l for l in links if l["source"] is None]
    assert [l["entity_id"] for l in morph_rows] == [julia]
    assert [l["entity_id"] for l in foreign_rows] == [other]


def test_morph_link_threads_does_not_touch_exact_match_rows(db, morph_on):
    """An exact and a morphological link on different messages of one thread
    do not interfere with each other on a repeat pass."""
    exact = _entity(db, "Ковальчука", etype="project")
    julia = _entity(db, "Юлія")
    _msg(db, "по Ковальчука домовились", thread_id=1)
    _msg(db, "уточнила Юлією питання", thread_id=1)

    tg_entities.link_threads(db, dry_run=False)
    tg_entities.link_threads(db, dry_run=False)   # idempotent

    links = _links(db)
    assert len(links) == 2
    by_source = {l["source"]: l["entity_id"] for l in links}
    assert by_source[tg_entities.SOURCE] == exact
    assert by_source[tg_entities.SOURCE_MORPH] == julia


def test_morph_stats_reports_new_source(db, morph_on):
    """`stats()` shows the breakdown by the new `source` value without a separate edit —
    grouping by the actual values in the DB."""
    _entity(db, "Юлія")
    tid = _msg(db, "домовились із Юлією про дзвінок")
    tg_entities.link_message(db, tid)
    got = tg_entities.stats(db)
    assert got["links_by_source"].get(tg_entities.SOURCE_MORPH) == 1


def test_morph_stems_cache_is_reset_with_names_cache(db, morph_on):
    """Stems are cached together with the names (`stems_for`) and reset by the same
    `reset_names_cache()`."""
    _entity(db, "Юлія")
    first = tg_entities.stems_for(db)
    _entity(db, "Роман")
    assert tg_entities.stems_for(db) is first           # the new stem is not visible yet
    tg_entities.reset_names_cache()
    assert "роман" in tg_entities.stems_for(db)


def test_morph_stems_cache_recomputes_after_flag_flip(db, monkeypatch):
    """A cache warmed while the flag is off leaves the stems slot `{}` — and
    without a check of the flag state this empty slot would be served after
    turning it on too: `link_message` then deletes its `SOURCE_MORPH` rows and has nothing to
    rewrite them with (the stems are empty although the flag is already ON)."""
    julia = _entity(db, "Юлія")
    tg_entities.names_for(db)                          # warming the cache under OFF
    monkeypatch.setenv("TG_ENTITIES_MORPH_ENABLED", "1")
    tid = _msg(db, "домовились із Юлією про дзвінок")

    res = tg_entities.link_message(db, tid)

    assert res["written"] == 1
    row = _links(db)[0]
    assert row["entity_id"] == julia
    assert row["source"] == tg_entities.SOURCE_MORPH
