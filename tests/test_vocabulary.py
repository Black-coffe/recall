"""The vocabulary of "hard" names from the graph (K1, ``app/services/vocabulary.py``).

Offline, a real tmp SQLite via ``init_database``; the fixtures are Cyrillic — SQLite ``LOWER``
does not fold Cyrillic case, and that is exactly what broke (memo
``plan-dictated-sql-ignored-own-memory``).
"""
from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pytest

from app.db.migrations import init_database
from app.services import vocabulary
from app.services.live_transcribe import LiveTranscribeWorker, MIN_BYTES_FOR_PASS, _StreamState


# ============================================================
# Graph fixture
# ============================================================

def _tx(conn, text, source_type="recording", model_used="large-v3-turbo"):
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, model_used) "
        "VALUES (?, ?, ?, ?)", (source_type, "дзвінок.mp3", text, model_used))
    return cur.lastrowid


def _entity(conn, etype, name, aliases=(), tids=()):
    cur = conn.execute(
        "INSERT INTO entities (type, canonical_name, normalized_name) VALUES (?, ?, ?)",
        (etype, name, f"{etype}:{name.casefold()}"))
    eid = cur.lastrowid
    for alias in aliases:
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (?, ?, ?)",
            (eid, alias, f"{eid}:{alias.casefold()}"))
    for tid in tids:
        conn.execute(
            "INSERT INTO meeting_entities (transcription_id, entity_id) VALUES (?, ?)", (tid, eid))
    return eid


@pytest.fixture()
def graph_db(tmp_path: Path) -> str:
    """Acmecorp — 1 meeting, two error aliases in the text; «Адам» — 3 meetings, only
    case forms + an error that is not in the text; Google — the translation «Гугл»; «фонд» —
    a common word; a topic with an error — the wrong type."""
    path = str(tmp_path / "graph.db")
    init_database(path)
    conn = sqlite3.connect(path)
    try:
        t1 = _tx(conn, "Сьогодні Акмеец і Акмеетс говорили з Адамом. Адаму потрібен фонд, "
                       "фонд великий, у Гугл теж. Стратегія Стратежка.")
        t2 = _tx(conn, "Адама не було. Гугл диск лежить, фонд чекає.")
        t3 = _tx(conn, "Знову Адама кличуть, фонд мовчить.")
        # A TG text message is not Whisper: errors are not counted here.
        t4 = _tx(conn, "Акмеедз пише в чат", source_type="telegram", model_used=None)
        _entity(conn, "project", "Acmecorp", aliases=["Акмеец", "Акмеетс", "Акмеедз"], tids=[t1, t4])
        _entity(conn, "person", "Адам", aliases=["Адаму", "Адама", "Гадам"], tids=[t1, t2, t3])
        _entity(conn, "org", "Google", aliases=["Гугл", "гугл диск"], tids=[t1, t2])
        _entity(conn, "org", "фонд", aliases=["Фонду"], tids=[t1, t2, t3])
        _entity(conn, "topic", "Стратегія", aliases=["Стратежка"], tids=[t1, t2, t3])
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Explicitly off: after the K1 measurement the defaults are on (see test_defaults_are_measured_winners).
    monkeypatch.setenv(vocabulary.ENV_MODE, "off")
    monkeypatch.setenv(vocabulary.ENV_MULTILINGUAL, "0")
    vocabulary.clear_cache()
    yield
    vocabulary.clear_cache()


# ============================================================
# Error alias vs the same name
# ============================================================

@pytest.mark.parametrize("alias, canonical", [
    ("Акмеец", "Acmecorp"),
    ("Акмеетс", "Acmecorp"),
    ("Тиграм", "Telegram"),
])
def test_consonant_mangling_is_error(alias, canonical):
    assert vocabulary.is_error_alias(alias, canonical)


@pytest.mark.parametrize("alias, canonical", [
    ("Адаму", "Адам"),            # case form
    ("Фонд Робота", "Робота Fund"),
])
def test_case_forms_and_shared_stem_are_not_errors(alias, canonical):
    assert not vocabulary.is_error_alias(alias, canonical)


@pytest.mark.parametrize("alias, canonical", [
    # The owner's original case (08.10.2026).
    ("Гугл", "Google"),
    ("Телеграм", "Telegram"),
    # Neighbouring forms: ru/uk translation, Latin j as «й», compound, case of a translation.
    ("Олексій", "Алексей"),
    ("Julia Bondarenko", "Юлія"),
    ("гугл диск", "Google"),
    ("Клода", "Claude"),
    ("Ютуб", "YouTube"),
    ("Джейсон", "Jason"),
])
def test_translation_or_transliteration_is_not_error(alias, canonical):
    assert vocabulary.same_name(alias, canonical)
    assert not vocabulary.is_error_alias(alias, canonical)


def test_defect_card_check_fails_on_both_fixtures(tmp_path):
    root = Path(__file__).resolve().parent.parent / "docs" / "defects" / "fixtures"
    if not root.is_dir():
        pytest.skip("no defects library (the public mirror does not carry it)")
    for name in ("translation-counted-as-asr-error-original.json",
                 "translation-counted-as-asr-error-neighbour.json"):
        assert vocabulary.check_error_pairs(str(root / name)) == 1, name
    real = tmp_path / "real.json"
    real.write_text(json.dumps([{"alias": "Акмеец", "canonical": "Acmecorp"}], ensure_ascii=False),
                    encoding="utf-8")
    assert vocabulary.check_error_pairs(str(real)) == 0
    assert vocabulary.check_error_pairs(str(tmp_path / "missing.json")) == 0


# ============================================================
# Ranking
# ============================================================

def test_entity_with_error_aliases_ranks_above_frequent_case_forms_only(graph_db):
    names = vocabulary.build_hard_names(graph_db)
    assert names.index("Acmecorp") < names.index("Адам")
    assert names[0] == "Acmecorp"


def test_translation_alias_does_not_lift_rank(graph_db):
    # Google is more frequent than Acmecorp, but «Гугл» is a translation: zero errors.
    names = vocabulary.build_hard_names(graph_db)
    assert names.index("Acmecorp") < names.index("Google")


def test_common_lowercase_word_is_dropped(graph_db):
    assert "фонд" not in vocabulary.build_hard_names(graph_db)


def test_only_person_project_org(graph_db):
    assert "Стратегія" not in vocabulary.build_hard_names(graph_db)


def test_limit_respected(graph_db):
    assert vocabulary.build_hard_names(graph_db, limit=1) == ["Acmecorp"]


# ============================================================
# Cache with TTL
# ============================================================

def test_cache_within_ttl_skips_db_and_rebuilds_after(graph_db, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(vocabulary, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    opened = []
    real_connect = vocabulary._connect_ro
    monkeypatch.setattr(vocabulary, "_connect_ro", lambda p: opened.append(p) or real_connect(p))

    first = vocabulary.hard_names(graph_db)
    assert first and len(opened) == 1
    clock[0] += vocabulary.CACHE_TTL_S - 1
    assert vocabulary.hard_names(graph_db) == first
    assert len(opened) == 1, "within the TTL we do not touch the DB"

    conn = sqlite3.connect(graph_db)
    _entity(conn, "project", "Zentrix", aliases=["Зентрікс"])
    conn.commit()
    conn.close()
    clock[0] += 2
    assert "Zentrix" in vocabulary.hard_names(graph_db)
    assert len(opened) == 2, "after the TTL — a rebuild without a restart"


def test_broken_db_gives_empty_vocabulary(tmp_path):
    bad = tmp_path / "empty.db"
    sqlite3.connect(str(bad)).close()  # no migrations: no tables
    assert vocabulary.hard_names(str(bad)) == []


# ============================================================
# decode_options_for
# ============================================================

ALL_KINDS = ("file", "recording", "telegram", "live", "youtube")


def test_defaults_are_measured_winners(graph_db, monkeypatch):
    """K1 summary (measure/report.md): vocabulary hotwords (the owner's decision), multilingual 30 s."""
    monkeypatch.delenv(vocabulary.ENV_MODE)
    monkeypatch.delenv(vocabulary.ENV_MULTILINGUAL)
    assert vocabulary.mode() == "hotwords"
    assert vocabulary.multilingual_enabled() is True
    opts = vocabulary.decode_options_for("recording", graph_db)
    assert opts["hotwords"][0] == "Acmecorp" and opts["multilingual"] is True
    assert "chunk_length" not in opts, "the 10 s window was not accepted — 30 s remains"
    assert vocabulary.decode_options_for("youtube", graph_db) == {}


def test_flags_off_give_empty_options_without_db(graph_db, monkeypatch):
    monkeypatch.setattr(vocabulary, "_connect_ro",
                        lambda p: pytest.fail("with the flags off the DB is not read"))
    for kind in ALL_KINDS:
        assert vocabulary.decode_options_for(kind, graph_db) == {}


def test_youtube_always_empty(graph_db, monkeypatch):
    monkeypatch.setenv(vocabulary.ENV_MODE, "hotwords")
    monkeypatch.setenv(vocabulary.ENV_MULTILINGUAL, "1")
    assert vocabulary.decode_options_for("youtube", graph_db) == {}


@pytest.mark.parametrize("mode", ["hotwords", "initial_prompt"])
def test_vocab_mode_reaches_every_non_youtube_path(graph_db, monkeypatch, mode):
    monkeypatch.setenv(vocabulary.ENV_MODE, mode)
    for kind in ("file", "recording", "telegram", "live"):
        opts = vocabulary.decode_options_for(kind, graph_db)
        assert set(opts) == {mode}, kind
        assert opts[mode][0] == "Acmecorp"


def test_multilingual_skips_live(graph_db, monkeypatch):
    monkeypatch.setenv(vocabulary.ENV_MULTILINGUAL, "1")
    for kind in ("file", "recording", "telegram"):
        assert vocabulary.decode_options_for(kind, graph_db) == {"multilingual": True}
    assert vocabulary.decode_options_for("live", graph_db) == {}


def test_unknown_mode_is_off(graph_db, monkeypatch):
    monkeypatch.setenv(vocabulary.ENV_MODE, "everything")
    assert vocabulary.decode_options_for("file", graph_db) == {}


def test_unknown_kind_rejected(graph_db):
    with pytest.raises(ValueError):
        vocabulary.decode_options_for("meeting_archive", graph_db)


# ============================================================
# The live path passes decode_options
# ============================================================

class _CaptureWhisper:
    def __init__(self):
        self.kwargs = []

    def transcribe_with_progress(self, audio_path, model_name="base", language="uk",
                                 task="transcribe", progress_callback=None, **kwargs):
        self.kwargs.append(kwargs)
        return {"text": "", "segments": [], "language": "uk"}


def _run_live_pass(tmp_path, worker):
    pcm = tmp_path / "mic.pcm"
    pcm.write_bytes(b"\x00" * (MIN_BYTES_FOR_PASS + 2))
    state = _StreamState(session_id="s1", stream_name="mic", speaker_label="self",
                         pcm_path=str(pcm), sample_rate=16000, channels=1)
    worker._process_tail(state)


def test_live_pass_sends_vocabulary(graph_db, tmp_path, monkeypatch):
    monkeypatch.setenv(vocabulary.ENV_MODE, "hotwords")
    monkeypatch.setenv(vocabulary.ENV_MULTILINGUAL, "1")
    wm = _CaptureWhisper()
    _run_live_pass(tmp_path, LiveTranscribeWorker(wm, broker=None, db_path=graph_db))
    opts = wm.kwargs[0]["decode_options"]
    assert opts["hotwords"][0] == "Acmecorp"
    assert "multilingual" not in opts


def test_live_pass_flags_off_sends_empty(tmp_path):
    wm = _CaptureWhisper()
    _run_live_pass(tmp_path, LiveTranscribeWorker(wm, broker=None))
    assert wm.kwargs[0]["decode_options"] == {}
