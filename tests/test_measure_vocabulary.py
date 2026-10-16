"""The K1 measurement tool (``tools/measure_vocabulary.py``), offline, no GPU.

The fixtures are Cyrillic: the selection of error aliases and the mixed-language detector work
precisely on Cyrillic (memo ``plan-dictated-sql-ignored-own-memory``). The names are invented
(Zentrix / «Зентріц»): the mirror sanitization map does not touch them, so the phonetics of the tests
is the same in both repositories.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import measure_vocabulary as mv  # noqa: E402
from app.db.migrations import init_database  # noqa: E402
from app.services import vocabulary  # noqa: E402


# ============================================================
# Fixture: a graph + recordings with files on disk
# ============================================================

def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "m.db")
    init_database(path)
    audio = tmp_path / "rec.mp3"
    audio.write_bytes(b"fake")
    segs = [
        _seg(0, 8, "Сьогодні по Зентріц говоримо довго і детально про все"),
        _seg(8, 16, "Адаму треба відповісти до пʼятниці щодо звіту"),
        _seg(16, 24, "Я в Гугл шукав відповідь, але нічого не знайшов"),
        _seg(24, 33, "погода сьогодні тепла і можна гуляти довго по місту ввечері"),
        _seg(33, 40, "Зентрець знову прислали лист із питаннями"),
    ] + [_seg(40 + i * 10, 50 + i * 10, f"звичайна розмова номер {i} без жодних імен про роботу") for i in range(60)]
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, model_used, file_path, "
        "language, segments) VALUES ('recording', 'дзвінок', ?, 'large-v3-turbo', ?, 'uk', ?)",
        (" ".join(s["text"] for s in segs), str(audio), json.dumps(segs, ensure_ascii=False)))
    tid = cur.lastrowid

    def entity(etype, name, aliases):
        eid = conn.execute("INSERT INTO entities (type, canonical_name, normalized_name) VALUES (?, ?, ?)",
                           (etype, name, f"{etype}:{name.casefold()}")).lastrowid
        for a in aliases:
            conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (?, ?, ?)",
                         (eid, a, f"{eid}:{a.casefold()}"))
        conn.execute("INSERT INTO meeting_entities (transcription_id, entity_id) VALUES (?, ?)", (tid, eid))

    entity("project", "Zentrix", ["Зентріц", "Зентрець"])
    entity("person", "Адам", ["Адаму"])
    entity("org", "Google", ["Гугл"])
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(vocabulary.ENV_MODE, raising=False)
    monkeypatch.delenv(vocabulary.ENV_MULTILINGUAL, raising=False)
    vocabulary.clear_cache()


# ============================================================
# Selection of error aliases
# ============================================================

def test_cyrillic_error_alias_selected_case_form_and_translation_not(db):
    conn = mv.connect_ro(db)
    try:
        targets = mv.error_alias_targets(conn, ["Zentrix", "Адам", "Google"])
    finally:
        conn.close()
    by_name = {t["canonical"]: t["aliases"] for t in targets}
    assert sorted(by_name["Zentrix"]) == sorted(["Зентріц", "Зентрець"])
    assert "Адам" not in by_name, "«Адаму» is a case form, not an error"
    assert "Google" not in by_name, "«Гугл» is a translation, not an error"


@pytest.mark.parametrize("alias, canonical, expected", [
    ("Зентріц", "Zentrix", True),
    ("Вадю", "Вадим", True),
    ("штучний інтелект", "Anthropic", False),
    ("Спікер 7", "Руслан", False),
    ("AdWords", "Google", False),
])
def test_only_mangling_like_aliases_are_measured(alias, canonical, expected):
    assert mv.looks_like_mangling(alias, canonical) is expected


def test_candidates_are_deterministic(db):
    a = mv.build_candidates(db, seed=7)
    b = mv.build_candidates(db, seed=7)
    assert a == b
    assert {f["alias"] for f in a["names"]} <= {"Зентріц", "Зентрець"}
    assert all(f["kind"] == "control" for f in a["control"])
    assert not any("Зентріц" in f["text"] or "Гугл" in f["text"] for f in a["control"])


# ============================================================
# Mixed language
# ============================================================

def test_rapid_alternation_detected_within_window():
    segs = [
        _seg(0, 5, "Слухай, я вже відправив звіт"),
        _seg(5, 9, "Да, я это видел, спасибо"),
        _seg(9, 14, "Добре, тоді завтра обговоримо"),
        _seg(14, 18, "Хорошо, созвонимся, всё понятно"),
    ]
    w = mv.mixed_window(segs)
    assert w is not None and w["switches"] == 3
    assert w["end"] - w["start"] <= mv.MIXED_WINDOW_S


def test_languages_far_apart_are_not_one_window():
    segs = [_seg(0, 10, "Добре, тоді завтра обговоримо їхній звіт")] + \
           [_seg(10 + i * 10, 20 + i * 10, "звичайна розмова") for i in range(8)] + \
           [_seg(100, 110, "Хорошо, всё понятно, созвонимся")]
    assert mv.mixed_window(segs) is None


def test_lang_of():
    assert mv.lang_of("це їхній звіт") == "uk"
    assert mv.lang_of("это всё понятно") == "ru"
    assert mv.lang_of("ok") is None


# ============================================================
# The owner's review
# ============================================================

def test_reviewed_ids_honour_unchecked_and_struck_rows():
    md = "\n".join([
        "- [x] N01 · #1 запис · 00:01–00:05 · «Зентріц» → Zentrix · текст",
        "- [ ] N02 · #1 запис · 00:06–00:09 · «Шадом» → Адам · текст",
        "- [x] ~~N03 · #2 запис · «Тім» → Діма~~",
        "~~- [x] N04 · #2 запис~~",
        "- [x] C01 · #3 запис · 00:10–00:20 · контроль",
        "- M01 · #4 запис · змішана",
    ])
    assert mv.reviewed_ids(md) == {"N01", "C01"}


def test_fragments_md_lists_rows_checked(db):
    c = mv.build_candidates(db)
    md = mv.render_fragments_md(c)
    assert mv.reviewed_ids(md) == {f["id"] for f in c["names"] + c["control"]}


# ============================================================
# Run: refusal during a recording, the same options as prod
# ============================================================

def test_run_refuses_while_recording(monkeypatch):
    monkeypatch.setattr(mv, "recording_active", lambda api, timeout=5.0: True)
    with pytest.raises(SystemExit, match="recording is in progress"):
        mv.refuse_if_recording("http://x")


def test_run_proceeds_when_api_down(monkeypatch, capsys):
    monkeypatch.setattr(mv, "recording_active", lambda api, timeout=5.0: None)
    mv.refuse_if_recording("http://x")
    assert "unavailable" in capsys.readouterr().out


def test_decode_options_match_prod_function(db):
    assert mv.decode_options("baseline", db) == {}
    assert mv.decode_options("hotwords", db)["hotwords"][0] == "Zentrix"
    assert set(mv.decode_options("initial_prompt", db)) == {"initial_prompt"}
    assert mv.decode_options("ml30", db) == {"multilingual": True}
    assert mv.decode_options("ml10", db) == {"multilingual": True, "chunk_length": 10}
    import os
    assert vocabulary.ENV_MODE not in os.environ, "prod flags do not remain after the measurement"


class _FakeWM:
    """A manager fake: records the duration with which the manager would pick the path (batched > 600 s)."""

    def __init__(self):
        self.seen = []

    def _get_duration_ffprobe(self, path):
        return 5.0

    def transcribe_with_progress(self, path, model_name, language, decode_options=None, **kw):
        self.seen.append((self._get_duration_ffprobe(path), decode_options))
        return {"text": " текст ", "language": language}


def test_fragment_of_long_recording_decodes_batched_then_restores():
    wm = _FakeWM()
    out = mv.transcribe(wm, "clip.wav", "m", "uk", {"hotwords": ["X"]}, 1800.0)
    assert out["text"] == "текст"
    assert wm.seen[0] == (1800.0, {"hotwords": ["X"]})
    assert wm._get_duration_ffprobe("clip.wav") == 5.0, "the real duration is restored"


# ============================================================
# Blind pairs
# ============================================================

def _mixed(n):
    return [{"id": f"M{i:02d}", "tid": i, "name": f"запис {i}", "start": 0, "end": 60} for i in range(1, n + 1)]


def test_ab_pairs_blind_with_separate_key():
    mixed = _mixed(3)
    raw = {"variants": {
        "baseline": {f["id"]: {"text": f"базовий {f['id']}"} for f in mixed},
        "ml30": {f["id"]: {"text": f"мульти {f['id']}"} for f in mixed},
        # M01 in ml10 equals baseline → a tie, not shown.
        "ml10": {f["id"]: {"text": (f"базовий {f['id']}" if f["id"] == "M01" else f"вікно10 {f['id']}")}
                 for f in mixed},
    }}
    md, key = mv.build_ab(mixed, raw, seed=1)
    assert len(key["pairs"]) == 5
    assert key["auto_ties"] == [{"fragment": "M01", "variant": "ml10"}]
    for name in ("baseline", "ml30", "ml10"):
        assert name not in md, "variant names do not get into the blind file"
    assert sum(1 for line in md.splitlines() if line.strip() == "Вердикт:") == 5
    sides = {(p["A"], p["B"]) for p in key["pairs"].values()}
    assert any(a == "baseline" for a, _ in sides) and any(b == "baseline" for _, b in sides), \
        "the baseline side is shuffled"
    assert mv.build_ab(mixed, raw, seed=1) == (md, key), "deterministic by seed"


# ============================================================
# Summary (story 03)
# ============================================================

@pytest.mark.parametrize("alias, canonical, word", [
    ("Мельнюк", "Василь Мельник", "Мельник"),
    ("Rebuild", "Upbuild Ukraine", "Upbuild"),
    ("Севолу", "Всеволод Мороз", "Всеволод"),
    ("Зентріц", "Zentrix", "Zentrix"),
])
def test_target_word_is_the_mangled_part(alias, canonical, word):
    assert mv.target_word(alias, canonical) == word


def test_strict_hit_by_stem_in_any_case_tolerant_takes_translation():
    assert mv.strict_hit("Треба написати Адаму сьогодні", "Адам")
    assert not mv.strict_hit("через Клода він проговорює", "Claude")
    assert mv.tolerant_hit("через Клода він проговорює", "Claude")
    assert not mv.tolerant_hit("через клоти він проговорює", "Claude")


def _frag(fid, alias, canonical):
    return {"id": fid, "alias": alias, "canonical": canonical}


def test_score_names_counts_only_reviewed_rows():
    frags = [_frag("N01", "Зентріц", "Zentrix"), _frag("N02", "Шадом", "Адам"), _frag("N03", "Росан", "Руслан")]
    raw = {"variants": {
        "baseline": {"N01": {"text": "по зентріц"}, "N02": {"text": "і там Шадом"}, "N03": {"text": "Руслан тут"}},
        "hotwords": {"N01": {"text": "по Zentrix"}, "N02": {"text": "і там Адам"}, "N03": {"text": "Росан тут"}},
    }}
    s = mv.score_names(frags, raw, keep={"N01", "N02"})
    assert (s["baseline"]["strict"], s["hotwords"]["strict"], s["hotwords"]["n"]) == (0, 2, 2)
    assert "N03" not in s["hotwords"]["per"], "a row struck by the owner is not counted"


def test_score_control_flags_inserted_vocabulary_name():
    frags = [{"id": "C01"}, {"id": "C02"}]
    raw = {"variants": {
        "baseline": {"C01": {"text": "погода тепла"}, "C02": {"text": "звичайна розмова"}},
        "hotwords": {"C01": {"text": "погода тепла, Zentrix"}, "C02": {"text": "звичайна розмова"}},
    }}
    c = mv.score_control(frags, raw, keep={"C01", "C02"}, vocab=["Zentrix", "Адам"])["hotwords"]
    assert c["extra"] == [{"fragment": "C01", "name": "Zentrix"}]
    assert [d["fragment"] for d in c["diverged"]] == ["C01"]


def test_score_speed_uses_median_seconds():
    raw = {"reps": 3, "variants": {
        "baseline": {"S1": {"seconds": 10.0}, "S2": {"seconds": 20.0}},
        "hotwords": {"S1": {"seconds": 11.0}, "S2": {"seconds": 22.0}},
    }}
    s = mv.score_speed(raw)
    assert s["hotwords"]["total"] == pytest.approx(0.10)
    assert s["_reps"] == 3


def test_score_ab_maps_sides_through_key():
    key = {"pairs": {"1": {"variant": "ml30", "A": "ml30", "B": "baseline"},
                     "2": {"variant": "ml30", "A": "baseline", "B": "ml30"},
                     "3": {"variant": "ml10", "A": "baseline", "B": "ml10"}},
           "auto_ties": [{"fragment": "M04", "variant": "ml10"}]}
    md = "## Пара 1 · x\n\nВердикт: А\n\n## Пара 2 · x\n\nВердикт: А\n\n## Пара 3 · x\n\nВердикт: =\n"
    ab = mv.score_ab(mv.parse_verdicts(md), key)
    assert ab["ml30"] == {"wins": 1, "losses": 1, "ties": 0, "missing": 0}
    assert ab["ml10"] == {"wins": 0, "losses": 0, "ties": 2, "missing": 0}


def _decision_inputs(slow=0.05, extra=(), base_hits=3, hits=7, ml30_wins=7, ml10_wins=6):
    names = {"baseline": {"strict": base_hits}, "hotwords": {"strict": hits}, "initial_prompt": {"strict": hits}}
    control = {v: {"extra": list(extra)} for v in ("hotwords", "initial_prompt")}
    speed = {"hotwords": {"total": slow}, "initial_prompt": {"total": slow + 0.01}}
    ab = {"ml30": {"wins": ml30_wins, "missing": 0}, "ml10": {"wins": ml10_wins, "missing": 0}}
    return names, control, speed, ab


def test_decide_vocab_needs_gain_speed_and_clean_control():
    assert mv.decide(*_decision_inputs())["vocab_mode"] == "hotwords"
    assert mv.decide(*_decision_inputs(slow=0.111))["vocab_mode"] == "off"
    assert mv.decide(*_decision_inputs(extra=[{"fragment": "C01", "name": "X"}]))["vocab_mode"] == "off"
    assert mv.decide(*_decision_inputs(hits=3))["vocab_mode"] == "off"


def test_decide_multilingual_majority_and_window():
    d = mv.decide(*_decision_inputs(ml30_wins=7, ml10_wins=6))
    assert d["multilingual"] and d["chunk_length"] is None
    d = mv.decide(*_decision_inputs(ml30_wins=5, ml10_wins=6))
    assert d["multilingual"] and d["chunk_length"] == 10
    assert not mv.decide(*_decision_inputs(ml30_wins=5, ml10_wins=5))["multilingual"], "«almost» is not a win"


def test_owner_override_kept_next_to_rule():
    d = mv.decide(*_decision_inputs(slow=0.111), owner_vocab="hotwords", owner_note="слово власника")
    assert (d["vocab_mode"], d["vocab_rule"], d["owner_note"]) == ("hotwords", "off", "слово власника")
