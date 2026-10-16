"""Unit tests of chunking (app/services/embeddings.py) - T7.5.

Offline, without torch/sentence-transformers/GPU: we test ONLY the deterministic
text-cutting helpers (_window_text/_split_sentences/_chunk_from_segments/
_chunk_from_blocks/build_chunks). Covers the cutting boundaries: short/long
sentences, Cyrillic, overlap, the _MAX_CHARS boundary.

production-rag-wave-b-04: embed_texts/embed_query are covered too, but through
a replaced `_encode` (a fake that remembers the input strings); the real model is NOT
loaded. This checks only the choice of prefix by `_style_for()`, not the
encoding itself."""
from __future__ import annotations

import importlib
import logging
import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import categories, embeddings


# ============================================================
# _split_sentences
# ============================================================

def test_split_sentences_basic_punctuation():
    text = "Перше речення. Друге речення! Третє речення?"
    assert embeddings._split_sentences(text) == [
        "Перше речення.", "Друге речення!", "Третє речення?",
    ]


def test_split_sentences_ellipsis_and_double_newline():
    text = "Хм… цікаво.\n\nНовий абзац тут."
    parts = embeddings._split_sentences(text)
    assert parts == ["Хм…", "цікаво.", "Новий абзац тут."]


def test_split_sentences_empty_string():
    assert embeddings._split_sentences("") == []


# ============================================================
# _window_text - cutting boundaries
# ============================================================

def test_window_text_empty_returns_empty_list():
    assert embeddings._window_text("") == []
    assert embeddings._window_text("   ") == []


def test_window_text_short_text_single_piece_unchanged():
    text = "Коротке українське речення про бюджет проєкту."
    pieces = embeddings._window_text(text)
    assert pieces == [text]


def test_window_text_exactly_at_max_chars_boundary_single_piece():
    text = "а" * embeddings._MAX_CHARS
    pieces = embeddings._window_text(text)
    assert len(pieces) == 1
    assert pieces[0] == text


def test_window_text_one_char_over_boundary_splits():
    # Exactly one character over the limit AND no punctuation to split into sentences -
    # _split_sentences returns the whole text as ONE "sentence", which at once
    # exceeds _MAX_CHARS on the very first append -> flush with an overlap tail.
    text = "а" * (embeddings._MAX_CHARS + 1)
    pieces = embeddings._window_text(text)
    assert len(pieces) == 2
    assert pieces[0] == text
    # no sentences, no spaces -> nothing to cut by, the tail stays a raw slice
    assert pieces[1] == text[-embeddings._TEXT_OVERLAP:]
    assert len(pieces[1]) == embeddings._TEXT_OVERLAP


def test_window_text_long_multisentence_text_splits_with_overlap():
    sentence = "Це речення про проєкт та бюджет команди. "
    text = sentence * 60  # longer than _MAX_CHARS, many sentence delimiters
    pieces = embeddings._window_text(text)
    assert len(pieces) >= 2
    for p in pieces:
        assert p  # non-empty
    # overlap: the tail of the previous piece is a prefix of the next one, but cut
    # at a sentence/word boundary, not mid-word (T6.5)
    tail = embeddings._overlap_tail(pieces[0], embeddings._TEXT_OVERLAP)
    assert pieces[1].startswith(tail)
    assert pieces[1][0].isupper(), "the overlap must start with a new sentence"


# ============================================================
# _overlap_tail - overlap boundary (T6.5)
# ============================================================

def test_overlap_tail_snaps_to_sentence_start():
    text = "Довга перша частина про бюджет команди. " * 10 + "Останнє речення про терміни."
    tail = embeddings._overlap_tail(text, 60)
    assert tail.startswith("Останнє речення")
    assert len(tail) <= 60


def test_overlap_tail_falls_back_to_word_boundary():
    """No sentence end in the tail -> cut at least at a word boundary, not
    mid-word (this is where the raw slice [-N:] spoiled chunks of the archive)."""
    text = "слово-одне слово-два слово-три слово-чотири слово-пʼять"
    tail = embeddings._overlap_tail(text, 20)
    assert not tail.startswith("во-") and not tail.startswith("о-")
    assert text.endswith(tail)
    assert tail.split()[0] in text.split()


def test_overlap_tail_short_text_returned_whole():
    assert embeddings._overlap_tail("Коротко.", 150) == "Коротко."
    assert embeddings._overlap_tail("будь-що", 0) == ""


def test_window_text_cyrillic_sentence_boundaries_respected():
    text = ("Перше речення українською мовою про проєкт Х. " * 40) + \
           ("Друге, зовсім інше речення про фінансування Y! " * 40)
    pieces = embeddings._window_text(text)
    assert all(len(p) <= embeddings._MAX_CHARS + embeddings._TEXT_OVERLAP for p in pieces)
    joined = "".join(pieces)
    assert "проєкт Х" in joined and "фінансування Y" in joined


def test_window_text_no_sentence_delimiters_still_terminates():
    """No periods/marks: the whole text is one 'sentence', the overlap tail is still
    shorter than the original (does not loop)."""
    text = "слово " * 500  # ~3000 characters, no .!?, but with spaces (split_sentences does not cut)
    pieces = embeddings._window_text(text)
    assert len(pieces) >= 1
    assert all(isinstance(p, str) and p for p in pieces)


# ============================================================
# _chunk_from_text
# ============================================================

def test_chunk_from_text_produces_sequential_indices_no_timecodes():
    text = ("Речення номер один. " * 60) + ("Речення номер два. " * 60)
    chunks = embeddings._chunk_from_text(text)
    assert len(chunks) >= 2
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c["start_time"] is None and c["end_time"] is None and c["speaker"] is None
               for c in chunks)


def test_chunk_from_text_empty_returns_empty_list():
    assert embeddings._chunk_from_text("") == []


# ============================================================
# _resolve_speaker
# ============================================================

@pytest.mark.parametrize("raw,speaker_map,expected", [
    (None, {}, None),
    ("self", {}, "Ви"),
    ("SPEAKER_UNKNOWN", {}, None),
    ("SPEAKER_0", {}, "Спікер 1"),
    ("SPEAKER_3", {}, "Спікер 4"),
    ("SPEAKER_0", {"SPEAKER_0": "Андрій"}, "Андрій"),
    ("SPEAKER_0", {"SPEAKER_0": ""}, "Спікер 1"),  # empty name in the map - fallback
    ("Гість", {}, "Гість"),  # an arbitrary label passes as is
])
def test_resolve_speaker(raw, speaker_map, expected):
    assert embeddings._resolve_speaker(raw, speaker_map) == expected


# ============================================================
# _chunk_from_segments - cutting boundaries by segments
# ============================================================

def _seg(text, start, end, speaker=None):
    return {"text": text, "start": start, "end": end, "speaker": speaker}


def test_chunk_from_segments_empty_list():
    assert embeddings._chunk_from_segments([], {}) == []


def test_chunk_from_segments_ignores_non_dict_and_empty_text():
    segs = [_seg("", 0, 1), "not-a-dict", _seg("   ", 1, 2), _seg("Реальний текст.", 2, 3)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Реальний текст."


def test_chunk_from_segments_merges_short_segments_into_one_chunk():
    segs = [_seg("Перше.", 0, 1), _seg("Друге.", 1, 2), _seg("Третє.", 2, 3)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1
    assert chunks[0]["start_time"] == 0
    assert chunks[0]["end_time"] == 3
    assert chunks[0]["text"] == "Перше. Друге. Третє."


def test_chunk_from_segments_huge_segment_not_duplicated_into_next_chunk():
    """T6.5: a segment that is itself bigger than the limit does NOT get dragged into the next chunk
    as overlap; otherwise it would be duplicated in every following window (before
    T6.5 such a segment gave three chunks, two of which were itself)."""
    big = "слово " * 200  # ~1200 characters > _MAX_CHARS(1000)
    segs = [_seg("коротке", 0, 1), _seg(big, 1, 2), _seg("хвіст", 2, 3)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 2
    assert chunks[0]["text"] == f"коротке {big.strip()}"
    assert chunks[1]["text"] == "хвіст"
    assert [c["chunk_index"] for c in chunks] == [0, 1]


def test_chunk_from_segments_overlap_carries_tail_on_limit_cut():
    """A break at the limit = the thought was cut artificially -> the next chunk starts
    with the tail of the previous one (~_SEG_OVERLAP_CHARS) so the context is not lost."""
    seg_text = "Це доволі довге речення про бюджет проєкту на наступний квартал."
    n = embeddings._MAX_CHARS // len(seg_text) + 2
    segs = [_seg(seg_text, i * 2.0, i * 2.0 + 1.9) for i in range(n)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) >= 2
    # the tail of the first chunk verbatim starts the second
    assert chunks[1]["text"].startswith(seg_text)
    assert chunks[0]["text"].endswith(seg_text)
    # the overlap is bounded: no more than a few segments, not half a chunk
    assert len(chunks[1]["text"]) < embeddings._MAX_CHARS


def test_chunk_from_segments_overlap_never_exceeds_budget():
    """The overlap is bounded by the budget, not by "at least one segment". A long segment
    that caused the break at the limit does NOT move into the next chunk; otherwise a
    900-character monologue would be duplicated in the index and skew the vote
    for the speaker of the next chunk."""
    long_seg = "Розгорнута репліка про стратегію фонду та наступні кроки. " * 18  # >_MAX_CHARS
    segs = [_seg("Коротко про порядок денний.", 0, 2, speaker="SPEAKER_0"),
            _seg(long_seg, 2, 40, speaker="SPEAKER_0"),
            _seg("Далі про терміни.", 40, 42, speaker="SPEAKER_0")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 2
    assert long_seg.strip() in chunks[0]["text"]
    assert long_seg.strip() not in chunks[1]["text"]
    assert chunks[1]["text"] == "Далі про терміни."


def test_chunk_from_segments_no_tail_only_chunk_at_end():
    """If the limit fired on the LAST segment, the tail is not emitted
    as a separate chunk: it is wholly contained in the previous one."""
    seg_text = "Речення про бюджет команди на наступний квартал і ризики."
    n = embeddings._MAX_CHARS // len(seg_text) + 1
    segs = [_seg(seg_text, i * 2.0, i * 2.0 + 1.9) for i in range(n)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1, "the tail after the last break is not a separate chunk"


def test_chunk_from_segments_min_chars_counts_own_text_only():
    """The _MIN_CHARS threshold is counted on the chunk's OWN text: the carried tail must not
    "allow" a natural boundary prematurely."""
    filler = "Слова про порядок денний зустрічі команди. "
    n = embeddings._MAX_CHARS // len(filler) + 1
    segs = [_seg(filler, i * 1.0, i * 1.0 + 0.9, speaker="SPEAKER_0") for i in range(n)]
    # right after a break at the limit: a short own segment and a speaker change
    segs.append(_seg("Коротка відповідь.", n * 1.0, n * 1.0 + 1, speaker="SPEAKER_1"))
    segs.append(_seg("І ще одна репліка.", n * 1.0 + 2, n * 1.0 + 3, speaker="SPEAKER_1"))
    chunks = embeddings._chunk_from_segments(segs, {})
    tail_chunk = chunks[-1]["text"]
    assert "Коротка відповідь." in tail_chunk and "І ще одна репліка." in tail_chunk, \
        "own characters are fewer than _MIN_CHARS -> too early to cut on a speaker change"


def test_chunk_from_segments_splits_on_speaker_change_after_min_chars():
    """T6.5: a speaker change is a natural boundary. Before it the chunk must be no shorter
    than _MIN_CHARS, otherwise the dialogue would shatter into utterances."""
    a = "Ми домовились підняти бюджет на маркетинг у другому кварталі. " * 6  # > _MIN_CHARS
    b = "Погоджуюсь, тоді я готую розрахунок до пʼятниці."
    segs = [_seg(a, 0, 30, speaker="SPEAKER_0"), _seg(b, 30, 35, speaker="SPEAKER_1")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 2
    assert chunks[0]["speaker"] == "Спікер 1"
    assert chunks[1]["speaker"] == "Спікер 2"
    assert chunks[1]["text"] == b
    # the boundary is natural -> NO overlap is added
    assert a.strip() not in chunks[1]["text"]


def test_chunk_from_segments_short_exchange_stays_one_chunk():
    """A quick exchange of utterances (both short) is not cut: _MIN_CHARS is not reached."""
    segs = [_seg("А ти дзвонив клієнту?", 0, 2, speaker="SPEAKER_0"),
            _seg("Так, вчора ввечері.", 2, 4, speaker="SPEAKER_1")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1
    assert "дзвонив" in chunks[0]["text"] and "вчора" in chunks[0]["text"]


def test_chunk_from_segments_splits_on_long_pause():
    """A pause >= _PAUSE_BOUNDARY_SEC is read as the end of a thought (without diarization)."""
    a = "Обговорили постачальника і зупинились на другому варіанті. " * 7  # > _MIN_CHARS
    segs = [_seg(a, 0, 20), _seg("Тепер про наступний квартал.", 25, 27)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 2
    assert chunks[1]["text"] == "Тепер про наступний квартал."
    assert chunks[1]["start_time"] == 25


def test_chunk_from_segments_short_pause_is_not_a_boundary():
    a = "Обговорили постачальника і зупинились на другому варіанті. " * 7
    segs = [_seg(a, 0, 20), _seg("І одразу далі по темі.", 20.3, 22)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1


def test_chunk_from_segments_speaker_unknown_is_not_a_turn_change():
    """SPEAKER_UNKNOWN is a diarization hole, not a participant: entering/leaving it does not cut."""
    a = "Говоримо про терміни здачі і про те, хто відповідальний. " * 7
    segs = [_seg(a, 0, 20, speaker="SPEAKER_0"),
            _seg("Незрозумілий шматок.", 20, 21, speaker="SPEAKER_UNKNOWN"),
            _seg("Продовжую думку.", 21, 22, speaker="SPEAKER_0")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1


def test_chunk_from_segments_timecodes_follow_retained_segments():
    """start_time/end_time are taken from the SEGMENTS actually lying in the chunk
    (including the carried tail), not from the window counter."""
    a = "Довга репліка про план на квартал і ризики по ньому. " * 8
    segs = [_seg(a, 5, 40, speaker="SPEAKER_0"),
            _seg("Коротка відповідь.", 45, 47, speaker="SPEAKER_1")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert chunks[0]["start_time"] == 5 and chunks[0]["end_time"] == 40
    assert chunks[-1]["start_time"] == 45 and chunks[-1]["end_time"] == 47


def test_chunk_from_segments_dominant_speaker_by_char_count():
    segs = [
        _seg("Дуже довгий текст від спікера А, більше символів явно.", 0, 5, speaker="SPEAKER_0"),
        _seg("Коротко Б", 5, 6, speaker="SPEAKER_1"),
    ]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1
    assert chunks[0]["speaker"] == "Спікер 1"  # SPEAKER_0 -> "Спікер 1" (dominates by characters)


def test_chunk_from_segments_resolves_speaker_via_speaker_map():
    segs = [_seg("Привіт усім.", 0, 1, speaker="SPEAKER_0")]
    chunks = embeddings._chunk_from_segments(segs, {"SPEAKER_0": "Андрій Коваль"})
    assert chunks[0]["speaker"] == "Андрій Коваль"


def test_chunk_from_segments_chunk_index_sequential_across_multiple_flushes():
    big = "слово " * 200
    segs = [_seg(big, 0, 1), _seg(big, 1, 2), _seg(big, 2, 3)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) >= 2
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


# ============================================================
# _chunk_from_blocks - documents with provenance (page/section)
# ============================================================

def test_chunk_from_blocks_preserves_page_and_section_per_chunk():
    blocks = [
        {"text": "Перший блок тексту.", "page": 1, "section": "Вступ"},
        {"text": "Другий блок тексту.", "page": 2, "section": "Основне"},
    ]
    chunks = embeddings._chunk_from_blocks(blocks)
    assert len(chunks) == 2
    assert chunks[0]["page"] == 1 and chunks[0]["section"] == "Вступ"
    assert chunks[1]["page"] == 2 and chunks[1]["section"] == "Основне"


def test_chunk_from_blocks_skips_empty_blocks_and_non_dicts():
    blocks = [{"text": ""}, "not-a-dict", {"text": "  "}, {"text": "Валідний блок", "page": 1}]
    chunks = embeddings._chunk_from_blocks(blocks)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Валідний блок"


def test_chunk_from_blocks_windows_do_not_cross_block_boundary():
    """A large block is cut sentence-aware into sub-chunks with THE SAME page/section,
    but a block boundary is never crossed by one chunk."""
    big_text = ("Речення блоку один. " * 60)
    blocks = [
        {"text": big_text, "page": 1, "section": "A"},
        {"text": "Короткий блок два.", "page": 2, "section": "B"},
    ]
    chunks = embeddings._chunk_from_blocks(blocks)
    assert len(chunks) >= 3  # block 1 split into 2+ pieces + block 2
    assert all(c["page"] == 1 for c in chunks[:-1])
    assert chunks[-1]["page"] == 2 and chunks[-1]["text"] == "Короткий блок два."


# ============================================================
# build_chunks - strategy choice
# ============================================================

def test_build_chunks_prefers_structure_json_over_segments_and_text():
    import json
    structure = json.dumps([{"text": "Блок з провенансом", "page": 1}])
    segments = json.dumps([{"text": "Сегмент", "start": 0, "end": 1}])
    chunks = embeddings.build_chunks(segments, "плоский текст", {}, structure_json=structure)
    assert len(chunks) == 1
    assert chunks[0]["page"] == 1
    assert chunks[0]["text"] == "Блок з провенансом"


def test_build_chunks_prefers_segments_over_plain_text():
    import json
    segments = json.dumps([{"text": "Сегмент з таймкодом", "start": 5, "end": 10}])
    chunks = embeddings.build_chunks(segments, "плоский текст", {})
    assert len(chunks) == 1
    assert chunks[0]["start_time"] == 5
    assert chunks[0]["text"] == "Сегмент з таймкодом"


def test_build_chunks_falls_back_to_plain_text_when_no_segments():
    chunks = embeddings.build_chunks(None, "Просто текст без сегментів.", {})
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Просто текст без сегментів."
    assert chunks[0]["start_time"] is None


def test_build_chunks_falls_back_to_plain_text_on_invalid_json():
    chunks = embeddings.build_chunks("не json{{{", "Текст-фолбек.", {})
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Текст-фолбек."


def test_build_chunks_empty_segments_list_falls_back_to_text():
    import json
    chunks = embeddings.build_chunks(json.dumps([]), "Резервний текст.", {})
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Резервний текст."


# ============================================================
# blob_to_vec - round-trip (needs no model)
# ============================================================

def test_blob_to_vec_roundtrip():
    import numpy as np
    vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    blob = vec.tobytes()
    restored = embeddings.blob_to_vec(blob)
    assert np.allclose(restored, vec)


# ============================================================
# T6.8: EMBED_VERSION - structural idempotency (without forcing re-embed)
# ============================================================

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


def _add_tx(path, transcript_text="Текст транскрипту для ембедингу."):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text) "
        "VALUES ('file', 'test', ?)", (transcript_text,))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _fake_embed_texts(texts, batch_size=32):
    return np.zeros((len(texts), embeddings.EMBED_DIM), dtype=np.float32)


def test_embed_stores_current_embed_version(db, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    tid = _add_tx(db)
    res = embeddings.chunk_and_embed_transcription(db, tid)
    assert res["status"] == "embedded"

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT embedding_model, embedding_version FROM transcriptions WHERE id = ?",
        (tid,)).fetchone()
    conn.close()
    assert row[0] == embeddings.EMBED_MODEL
    assert row[1] == embeddings.EMBED_VERSION


def test_embed_skips_when_model_and_version_match(db, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    calls = {"n": 0}

    def fake_embed(texts, batch_size=32):
        calls["n"] += 1
        return _fake_embed_texts(texts, batch_size)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    tid = _add_tx(db)
    embeddings.chunk_and_embed_transcription(db, tid)
    assert calls["n"] == 1

    res2 = embeddings.chunk_and_embed_transcription(db, tid)
    assert res2["status"] == "skipped"
    assert calls["n"] == 1  # NOT re-encoded the second time


def test_embed_null_version_legacy_rows_are_reembedded(db, monkeypatch):
    """T6.5: rows embedded BEFORE migration v28 (embedding_version=NULL) are
    certainly the OLD cutting, so they are subject to re-embed. Before, NULL was considered
    compatible (so that the migration would not recompute the archive retroactively); with the move to
    natural boundaries that would become a silent hole: part of the archive forever with a different
    chunking."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    calls = {"n": 0}

    def fake_embed(texts, batch_size=32):
        calls["n"] += 1
        return _fake_embed_texts(texts, batch_size)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    tid = _add_tx(db)
    embeddings.chunk_and_embed_transcription(db, tid)
    assert calls["n"] == 1

    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET embedding_version = NULL WHERE id = ?", (tid,))
    conn.commit()
    conn.close()

    res = embeddings.chunk_and_embed_transcription(db, tid)
    assert res["status"] == "embedded"
    assert calls["n"] == 2  # NULL version = old cutting -> re-encoded

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT embedding_version FROM transcriptions WHERE id = ?",
                       (tid,)).fetchone()
    conn.close()
    assert row[0] == embeddings.EMBED_VERSION


def test_embed_version_bump_forces_reembed(db, monkeypatch):
    """An explicit EMBED_VERSION bump (a future change of the chunking logic) MUST
    be detected and force a re-embed; we check that the structural
    wiring really works (not just a declaration of the constant)."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    calls = {"n": 0}

    def fake_embed(texts, batch_size=32):
        calls["n"] += 1
        return _fake_embed_texts(texts, batch_size)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    tid = _add_tx(db)
    embeddings.chunk_and_embed_transcription(db, tid)
    assert calls["n"] == 1

    monkeypatch.setattr(embeddings, "EMBED_VERSION", embeddings.EMBED_VERSION + 1)
    res = embeddings.chunk_and_embed_transcription(db, tid)
    assert res["status"] == "embedded"
    assert calls["n"] == 2  # the version changed -> re-encoded


def test_embed_version_roundtrip_via_env_pair(db, monkeypatch):
    """production-rag-wave-b-04: the "stale / current" decision is made
    by the pair (EMBED_MODEL, EMBED_VERSION), which now comes from env, so
    rolling `EMBED_VERSION` back returns the same state as before the bump.
    Every change of the pair makes an existing record stale (status=embedded), and under
    an unchanged pair it is immediately current again (status=skipped)."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    base = embeddings.EMBED_VERSION
    tid = _add_tx(db)
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    # Version bump (a new pair in .env) -> the record is stale, after re-encoding it is current.
    monkeypatch.setattr(embeddings, "EMBED_VERSION", base + 1)
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "skipped"

    # Rollback with the same .env line -> the record is stale again for the old pair,
    # and after re-encoding under it it is current again, as before the bump.
    monkeypatch.setattr(embeddings, "EMBED_VERSION", base)
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "skipped"

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT embedding_model, embedding_version FROM transcriptions "
                       "WHERE id = ?", (tid,)).fetchone()
    conn.close()
    assert row == (embeddings.EMBED_MODEL, base)


def test_embed_model_and_version_come_from_env(monkeypatch):
    """Story promise: the Qwen3 pair is switched on by TWO `.env` lines, the rollback by
    the same two. We check exactly the env reading at module import (reload),
    not literals: the model, version and prefix style must travel together.

    Gate 9 (finding 15): do NOT pin the "original" to what currently lies in
    `embeddings.EMBED_MODEL/EMBED_VERSION` at test start; that depends on
    whether `load_dotenv()` was already called somewhere in the session (the owner's `.env`
    carries its own version). We pin the literal code defaults
    (`app/services/embeddings.py`) and at each step explicitly control env through
    monkeypatch, independent of the pytest file collection order."""
    DEFAULT_MODEL = "intfloat/multilingual-e5-large"
    DEFAULT_VERSION = 2

    monkeypatch.setenv("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    monkeypatch.setenv("EMBED_VERSION", "5")
    try:
        importlib.reload(embeddings)
        assert embeddings.EMBED_MODEL == "Qwen/Qwen3-Embedding-0.6B"
        assert embeddings.EMBED_VERSION == 5
        assert embeddings._STYLE == "qwen3"
    finally:
        # The rollback is also with explicit values (not with whatever happens to be in os.environ
        # of this session), otherwise the test again depends on file order.
        monkeypatch.setenv("EMBED_MODEL", DEFAULT_MODEL)
        monkeypatch.setenv("EMBED_VERSION", str(DEFAULT_VERSION))
        importlib.reload(embeddings)

    assert embeddings.EMBED_MODEL == DEFAULT_MODEL
    assert embeddings.EMBED_VERSION == DEFAULT_VERSION
    assert embeddings._STYLE == embeddings._style_for(DEFAULT_MODEL)


# ============================================================
# Wave 4: Telegram - author in the chunk + stubs do not become vectors
# ============================================================

def _add_tg_tx(db_path: str, text: str, sender: str | None = "Адам", chat_id: int = -100500):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "tg_chat_id, tg_chat_title, tg_sender, tg_message_id) "
        "VALUES ('telegram', '[TG] Fund: x', ?, ?, 'Fund', ?, 1)",
        (text, chat_id, sender))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def test_tg_sender_lands_in_chunk_speaker(db, monkeypatch):
    """TG has no segments, so build_chunks left speaker=None - empty in ALL 5857
    chunks on live data. Because of this RAG could not say WHO wrote it,
    although the name lay in the neighbouring column."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    tid = _add_tg_tx(db, "домовились про оплату в пʼятницю")
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    conn = sqlite3.connect(db)
    speakers = [r[0] for r in conn.execute(
        "SELECT speaker FROM chunks WHERE transcription_id = ?", (tid,))]
    conn.close()
    assert speakers and all(s == "Адам" for s in speakers)


def test_contentless_tg_placeholder_is_not_embedded(db, monkeypatch):
    """"[фото без тексту]" is a service marker, not text: 152 identical points
    competed for a place in the results and meant nothing."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    tid = _add_tg_tx(db, "[фото без тексту]")
    res = embeddings.chunk_and_embed_transcription(db, tid)
    assert res["status"] == "skipped" and res["reason"] == "tg_contentless"

    conn = sqlite3.connect(db)
    chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE transcription_id = ?",
                          (tid,)).fetchone()[0]
    # embedded_at MUST be set: otherwise the indexer would consider this work forever.
    marked = conn.execute("SELECT embedded_at IS NOT NULL FROM transcriptions WHERE id = ?",
                          (tid,)).fetchone()[0]
    conn.close()
    assert chunks == 0 and marked == 1


def test_contentless_matcher_keeps_real_text():
    assert embeddings._is_contentless_tg("[фото без тексту]")
    assert embeddings._is_contentless_tg("  [відео без розпізнаного тексту] ")
    assert not embeddings._is_contentless_tg("[Acmecorp] рахунок виставили")
    assert not embeddings._is_contentless_tg("оплата у пʼятницю")


# ============================================================
# production-rag-wave-b-04: _style_for() - prefix style by model family
# ============================================================

@pytest.mark.parametrize("name,expected", [
    ("intfloat/multilingual-e5-large", "e5"),
    ("intfloat/multilingual-E5-LARGE", "e5"),
    ("Qwen/Qwen3-Embedding-0.6B", "qwen3"),
    ("qwen/qwen3-embedding-4b", "qwen3"),
    ("BAAI/bge-m3", "plain"),
    ("", "plain"),
])
def test_style_for_families(name, expected):
    assert embeddings._style_for(name) == expected


def _capture_encode(monkeypatch):
    """Replaces _encode with a fake that remembers which strings came in
    (without loading the real model)."""
    seen: list[list[str]] = []

    def _fake(texts, batch_size=32):
        seen.append(list(texts))
        return np.zeros((len(texts), embeddings.EMBED_DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "_encode", _fake)
    return seen


def test_embed_texts_e5_style_adds_passage_prefix(monkeypatch):
    monkeypatch.setattr(embeddings, "_STYLE", "e5")
    seen = _capture_encode(monkeypatch)
    embeddings.embed_texts(["Кирилична фраза про бюджет."])
    assert seen == [["passage: Кирилична фраза про бюджет."]]


def test_embed_texts_qwen3_style_no_passage_prefix(monkeypatch):
    monkeypatch.setattr(embeddings, "_STYLE", "qwen3")
    seen = _capture_encode(monkeypatch)
    embeddings.embed_texts(["Кирилична фраза про бюджет."])
    assert seen == [["Кирилична фраза про бюджет."]]


def test_embed_texts_plain_style_no_prefix(monkeypatch):
    monkeypatch.setattr(embeddings, "_STYLE", "plain")
    seen = _capture_encode(monkeypatch)
    embeddings.embed_texts(["Кирилична фраза про бюджет."])
    assert seen == [["Кирилична фраза про бюджет."]]


def test_embed_query_e5_style_adds_query_prefix(monkeypatch):
    monkeypatch.setattr(embeddings, "_STYLE", "e5")
    seen = _capture_encode(monkeypatch)
    embeddings.embed_query("Що вирішили щодо бюджету?")
    assert seen == [["query: Що вирішили щодо бюджету?"]]


def test_embed_query_qwen3_style_adds_instruction(monkeypatch):
    monkeypatch.setattr(embeddings, "_STYLE", "qwen3")
    seen = _capture_encode(monkeypatch)
    embeddings.embed_query("Що вирішили щодо бюджету?")
    assert seen == [[f"Instruct: {embeddings._QWEN3_QUERY_TASK}\nQuery: Що вирішили щодо бюджету?"]]


def test_embed_query_plain_style_no_prefix(monkeypatch):
    monkeypatch.setattr(embeddings, "_STYLE", "plain")
    seen = _capture_encode(monkeypatch)
    embeddings.embed_query("Що вирішили щодо бюджету?")
    assert seen == [["Що вирішили щодо бюджету?"]]


# ============================================================
# production-rag-wave-b-04: EMBED_DIM - default before loading, the fact after
# ============================================================

def test_embed_dim_default_before_load():
    assert embeddings.EMBED_DIM == 1024


class _FakeModel:
    def __init__(self, dim):
        self._dim = dim

    def get_sentence_embedding_dimension(self):
        return self._dim


def test_apply_model_dim_updates_embed_dim():
    original = embeddings.EMBED_DIM
    try:
        result = embeddings._apply_model_dim(_FakeModel(768))
        assert result == 768
        assert embeddings.EMBED_DIM == 768
    finally:
        embeddings.EMBED_DIM = original


def test_contentless_tg_removes_previously_indexed_junk(db, monkeypatch):
    """Re-indexing must REMOVE the old junk vectors, not merely stop
    making new ones; otherwise the stub filter changes nothing for the existing archive
    (caught on live data: 188 "[фото без тексту]" survived the pass)."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    tid = _add_tg_tx(db, "справжній текст повідомлення")
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE transcription_id = ?",
                        (tid,)).fetchone()[0] > 0
    # the text became a stub (e.g. an edit or a repeated media ingest without recognition)
    conn.execute("UPDATE transcriptions SET transcript_text = '[фото без тексту]' WHERE id = ?",
                 (tid,))
    conn.commit()
    conn.close()

    res = embeddings.chunk_and_embed_transcription(db, tid, force=True)
    assert res["status"] == "skipped" and res["reason"] == "tg_contentless"

    conn = sqlite3.connect(db)
    left = conn.execute("SELECT COUNT(*) FROM chunks WHERE transcription_id = ?",
                        (tid,)).fetchone()[0]
    count_col = conn.execute("SELECT chunk_count FROM transcriptions WHERE id = ?",
                             (tid,)).fetchone()[0]
    conn.close()
    assert left == 0 and count_col == 0


# ============================================================
# production-rag-wave-b-05: chunk context prefix
# ============================================================

def test_context_prefix_call_has_title_date_speaker_and_category():
    meta = {"source_type": "file", "title": "Планерка Фонду",
            "date": "2026-09-15", "category": "Фонд Робота",
            "thread_label": None, "summary_line": None}
    prefix = embeddings.build_context_prefix(meta, {"speaker": "Андрій"})
    assert prefix == "[дзвінок] Планерка Фонду · 2026-09-15 · Андрій · Фонд Робота"


def test_context_prefix_telegram_has_chat_author_and_thread_label():
    meta = {"source_type": "telegram", "title": "Фонд · загальний",
            "date": "2026-09-16", "category": "Фонд Робота",
            "thread_label": "бюджет вересня", "summary_line": None}
    prefix = embeddings.build_context_prefix(meta, {"speaker": "Оксана"})
    # directions-projects-tags-03: the direction now stands in chats too, before
    # the thread label (epoch 2 did not print it here at all).
    assert prefix == ("[переписка] Фонд · загальний · 2026-09-16 · Оксана · "
                      "Фонд Робота · нитка: бюджет вересня")


def test_context_prefix_document_uses_page_then_section():
    meta = {"source_type": "document", "title": "Статут фонду.pdf",
            "date": "2026-03-01", "category": "Фонд Робота",
            "thread_label": None, "summary_line": None}
    with_page = embeddings.build_context_prefix(
        meta, {"speaker": None, "page": 7, "section": "Розділ II"})
    assert with_page == ("[документ] Статут фонду.pdf · 2026-03-01 · "
                         "Фонд Робота · стор. 7")
    with_section = embeddings.build_context_prefix(
        meta, {"speaker": None, "page": None, "section": "Розділ II"})
    assert with_section == ("[документ] Статут фонду.pdf · 2026-03-01 · "
                            "Фонд Робота · Розділ II")


def test_context_prefix_skips_missing_fields_and_never_prints_none():
    meta = {"source_type": "file", "title": None, "date": None,
            "category": None, "thread_label": None, "summary_line": None}
    prefix = embeddings.build_context_prefix(meta, {"speaker": None})
    assert prefix == "[дзвінок]"
    assert "None" not in prefix


def test_context_prefix_rejects_non_iso_date():
    meta = {"source_type": "file", "title": "Дзвінок", "date": "невідомо"}
    assert embeddings.build_context_prefix(meta, {}) == "[дзвінок] Дзвінок"
    meta["date"] = "2026-09-15 12:30:00"
    assert embeddings.build_context_prefix(meta, {}) == "[дзвінок] Дзвінок · 2026-09-15"


def test_context_prefix_second_line_is_unit_summary():
    meta = {"source_type": "file", "title": "Планерка", "date": "2026-09-15",
            "summary_line": "Домовились перенести оплату на жовтень."}
    prefix = embeddings.build_context_prefix(meta, {"speaker": "Андрій"})
    assert prefix.split("\n") == [
        "[дзвінок] Планерка · 2026-09-15 · Андрій",
        "Домовились перенести оплату на жовтень.",
    ]


def test_context_prefix_is_deterministic():
    meta = {"source_type": "telegram", "title": "Acmecorp", "date": "2026-09-16",
            "thread_label": "рахунок", "summary_line": "Рахунок виставлено."}
    chunk = {"speaker": "Оксана"}
    assert (embeddings.build_context_prefix(meta, chunk)
            == embeddings.build_context_prefix(meta, chunk))


def test_embed_writes_context_prefix_and_keeps_text_clean(db, monkeypatch):
    """The prefix goes into the embedder and into a separate column; `chunks.text` is byte for byte
    the same text that was in the transcript (quotes must not carry a header)."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    seen: list[list[str]] = []

    def fake_embed(texts, batch_size=32):
        seen.append(list(texts))
        return _fake_embed_texts(texts, batch_size)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    body = "Домовились про оплату в пʼятницю."
    tid = _add_tg_tx(db, body)

    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT text, context_prefix FROM chunks WHERE transcription_id = ?",
                       (tid,)).fetchone()
    conn.close()
    assert row[0] == body                       # the text is clean, byte for byte
    assert row[1].startswith("[переписка] Fund")
    assert "Адам" in row[1]
    # the fake embedder SAW the prefix: it is what goes into the vector
    assert seen and seen[0][0] == row[1] + "\n" + body


def test_embed_prefix_carries_unit_summary_of_call(db, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    tid = _add_tx(db, "Обговорили кошторис і терміни здачі.")
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE transcriptions SET meeting_date = '2026-09-15', "
        "summary_json = ? WHERE id = ?",
        ('{"summary": "Кошторис погоджено. Терміни зсунуто."}', tid))
    conn.commit()
    conn.close()

    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    conn = sqlite3.connect(db)
    prefix = conn.execute("SELECT context_prefix FROM chunks WHERE transcription_id = ?",
                          (tid,)).fetchone()[0]
    conn.close()
    lines = prefix.split("\n")
    assert lines[0] == "[дзвінок] test · 2026-09-15"
    assert lines[1] == "Кошторис погоджено."


def test_prefix_only_term_is_searchable_via_fts(db, monkeypatch):
    """The chat title sounds in the prefix and does NOT sound in the utterance text: search must
    find it (prefix in BM25), and the output must carry clean text."""
    from app.services import retrieval

    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    body = "рахунок виставили в понеділок, чекаємо оплату"
    tid = _add_tg_tx(db, body, sender="Оксана")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET tg_chat_title = 'Барселона' WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    # the vector branch is off: the test has neither a model nor real vectors
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    res = retrieval.search(db, "Барселона", top_k=5)
    assert [c["transcription_id"] for c in res["chunks"]] == [tid]
    assert res["chunks"][0]["text"] == body
    assert "Барселона" not in res["chunks"][0]["text"]
    assert "context_prefix" not in res["chunks"][0]


# ============================================================
# editable-title-description-03: own title and description in the prefix
# ============================================================

def test_context_prefix_uses_own_title_instead_of_source_name():
    meta = {"source_type": "file", "title": "Планерка Фонду",
            "date": "2026-09-15", "category": "Фонд Робота",
            "description": None, "summary_line": None}
    prefix = embeddings.build_context_prefix(meta, {"speaker": "Андрій"})
    assert prefix.split("\n")[0].startswith("[дзвінок] Планерка Фонду ·")


def test_context_prefix_description_line_is_flat_and_capped():
    long_tail = "деталі " * 100
    meta = {"source_type": "file", "title": "Планерка", "date": "2026-09-15",
            "description": f"Розбір\nбюджету на жовтень. {long_tail}",
            "summary_line": None}
    lines = embeddings.build_context_prefix(meta, {"speaker": None}).split("\n")
    assert lines[0] == "[дзвінок] Планерка · 2026-09-15"
    assert lines[1].startswith("опис: Розбір бюджету на жовтень. деталі")
    assert "\n" not in lines[1]
    assert len(lines[1]) == len("опис: ") + embeddings._PREFIX_DESCRIPTION_MAX


def test_context_prefix_without_description_has_no_description_line():
    meta = {"source_type": "file", "title": "Планерка", "date": "2026-09-15",
            "description": None, "summary_line": "Кошторис погоджено."}
    assert embeddings.build_context_prefix(meta, {"speaker": None}).split("\n") == [
        "[дзвінок] Планерка · 2026-09-15",
        "Кошторис погоджено.",
    ]
    empty = dict(meta, description="   \n  ")
    assert "опис" not in embeddings.build_context_prefix(empty, {})
    assert "None" not in embeddings.build_context_prefix(empty, {})


def test_load_prefix_meta_prefers_title_and_carries_description(db):
    tid = _add_tx(db, "Обговорили кошторис.")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET title = ?, description = ? WHERE id = ?",
                 ("Планерка Фонду", "Розбір кошторису на жовтень", tid))
    conn.commit()
    conn.row_factory = sqlite3.Row
    meta = embeddings._load_prefix_meta(conn, tid)
    conn.close()
    assert meta["title"] == "Планерка Фонду"
    assert meta["description"] == "Розбір кошторису на жовтень"


def test_load_prefix_meta_falls_back_to_chat_title_for_telegram(db):
    """Without an own title the prefix stays byte for byte as before story 03."""
    tid = _add_tg_tx(db, "рахунок виставили")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    meta = embeddings._load_prefix_meta(conn, tid)
    conn.close()
    assert meta["title"] == "Fund" and meta["description"] is None


def test_description_only_term_is_searchable_and_result_carries_display_name(db, monkeypatch):
    """The term sounds ONLY in the record description: search must find the chunk through
    the prefix, return clean text and name the record by its own title."""
    from app.services import retrieval

    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    body = "домовились закрити питання до понеділка"
    tid = _add_tx(db, body)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE transcriptions SET title = ?, description = ? WHERE id = ?",
                 ("Планерка Фонду", "Про ремонт Андріївського узвозу", tid))
    conn.commit()
    conn.close()
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    res = retrieval.search(db, "Андріївського", top_k=5)
    assert [c["transcription_id"] for c in res["chunks"]] == [tid]
    hit = res["chunks"][0]
    assert hit["text"] == body
    assert "Андріївського" not in hit["text"]
    assert hit["display_name"] == "Планерка Фонду"
    assert hit["source_name"] == "test"


# ============================================================
# directions-projects-tags-03: direction path and tags in the prefix, epoch 3
# ============================================================

def _seed_tree(db_path, direction="Фонд Робота", project="Акмекорп"):
    """A direction + a project inside it. Returns (direction_id, project_id)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    d = _ensure_node(conn, direction)
    p = _ensure_node(conn, project, parent_id=d)
    conn.commit()
    conn.close()
    return d, p


def _ensure_node(conn, name, parent_id=None):
    """id of a tree node by name - init_database already seeds some names."""
    row = conn.execute("SELECT id FROM categories WHERE name_norm = ?",
                       (categories.norm_name(name),)).fetchone()
    if row:
        conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?",
                     (parent_id, row[0]))
        return row[0]
    return categories.create_category(conn, name, parent_id=parent_id)["id"]


def _set_category(db_path, tid, cid):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE transcriptions SET category_id = ? WHERE id = ?", (cid, tid))
    conn.commit()
    conn.close()


def _tag(db_path, tid, *names):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    categories.set_tags(conn, tid, add=list(names))
    conn.commit()
    conn.close()


def test_context_prefix_path_goes_before_type_tail_for_telegram():
    """The direction path is now in chats too - BEFORE the thread label."""
    meta = {"source_type": "telegram", "title": "Фонд · загальний",
            "date": "2026-09-16", "category": "Акмекорп",
            "category_path": "Фонд Робота → Акмекорп",
            "thread_label": "бюджет вересня", "summary_line": None}
    prefix = embeddings.build_context_prefix(meta, {"speaker": "Оксана"})
    assert prefix == ("[переписка] Фонд · загальний · 2026-09-16 · Оксана · "
                      "Фонд Робота → Акмекорп · нитка: бюджет вересня")


def test_context_prefix_path_goes_before_page_for_document():
    meta = {"source_type": "document", "title": "Статут фонду.pdf",
            "date": "2026-03-01", "category": "Акмекорп",
            "category_path": "Фонд Робота → Акмекорп",
            "thread_label": None, "summary_line": None}
    prefix = embeddings.build_context_prefix(
        meta, {"speaker": None, "page": 7, "section": "Розділ II"})
    assert prefix == ("[документ] Статут фонду.pdf · 2026-03-01 · "
                      "Фонд Робота → Акмекорп · стор. 7")


def test_context_prefix_path_for_call_keeps_epoch2_position():
    meta = {"source_type": "file", "title": "Планерка Фонду",
            "date": "2026-09-15", "category": "Акмекорп",
            "category_path": "Фонд Робота → Акмекорп", "summary_line": None}
    prefix = embeddings.build_context_prefix(meta, {"speaker": "Андрій"})
    assert prefix == ("[дзвінок] Планерка Фонду · 2026-09-15 · Андрій · "
                      "Фонд Робота → Акмекорп")


def test_context_prefix_direction_without_project_has_no_arrow():
    meta = {"source_type": "telegram", "title": "Фонд · загальний",
            "date": "2026-09-16", "category": "Фонд Робота",
            "category_path": "Фонд Робота", "thread_label": None,
            "summary_line": None}
    prefix = embeddings.build_context_prefix(meta, {"speaker": "Оксана"})
    assert prefix.endswith("· Фонд Робота")
    assert "→" not in prefix


def test_context_prefix_tags_line_after_line_one():
    meta = {"source_type": "file", "title": "Планерка", "date": "2026-09-15",
            "category_path": "Фонд Робота", "tags": ["AI", "інвестори"],
            "description": "Розбір бюджету", "summary_line": "Кошторис погоджено."}
    assert embeddings.build_context_prefix(meta, {"speaker": None}).split("\n") == [
        "[дзвінок] Планерка · 2026-09-15 · Фонд Робота",
        "теги: AI, інвестори",
        "опис: Розбір бюджету",
        "Кошторис погоджено.",
    ]


def test_context_prefix_tags_line_keeps_at_most_five():
    meta = {"source_type": "file", "title": "Планерка", "date": "2026-09-15",
            "tags": ["AI", "гроші", "право", "найм", "склад", "шостий"]}
    line = embeddings.build_context_prefix(meta, {}).split("\n")[1]
    assert line == "теги: AI, гроші, право, найм, склад"
    assert "шостий" not in line


def test_context_prefix_tags_line_cut_by_whole_tag_under_sixty():
    meta = {"source_type": "file", "title": "Планерка", "date": "2026-09-15",
            "tags": ["довгий тег про інвестиції", "ще один довгий тег сюди",
                     "третій"]}
    line = embeddings.build_context_prefix(meta, {}).split("\n")[1]
    assert len(line) <= embeddings._PREFIX_TAGS_LINE_MAX
    assert line == "теги: довгий тег про інвестиції, ще один довгий тег сюди"
    # a truncated name is not a tag: either the tag is whole or it is absent
    assert "третій" not in line


def test_context_prefix_without_direction_and_tags_is_byte_identical_to_epoch2():
    """A record without a direction and tags gives exactly the same line as before the story."""
    for meta, chunk, expected in (
        ({"source_type": "file", "title": "Планерка", "date": "2026-09-15",
          "category": None, "category_path": None, "tags": [],
          "thread_label": None, "summary_line": None},
         {"speaker": "Андрій"}, "[дзвінок] Планерка · 2026-09-15 · Андрій"),
        ({"source_type": "telegram", "title": "Fund", "date": "2026-09-16",
          "category": None, "category_path": None, "tags": [],
          "thread_label": "рахунок", "summary_line": None},
         {"speaker": "Оксана"}, "[переписка] Fund · 2026-09-16 · Оксана · нитка: рахунок"),
        ({"source_type": "document", "title": "Статут.pdf", "date": "2026-03-01",
          "category": None, "category_path": None, "tags": [],
          "thread_label": None, "summary_line": None},
         {"speaker": None, "page": 7}, "[документ] Статут.pdf · 2026-03-01 · стор. 7"),
    ):
        assert embeddings.build_context_prefix(meta, chunk) == expected


def test_load_prefix_meta_builds_path_and_sorts_tags_by_norm(db):
    _, pid = _seed_tree(db)
    tid = _add_tx(db, "Обговорили кошторис.")
    _set_category(db, tid, pid)
    _tag(db, tid, "інвестори", "AI")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    meta = embeddings._load_prefix_meta(conn, tid)
    conn.close()
    assert meta["category"] == "Акмекорп"
    assert meta["category_path"] == "Фонд Робота → Акмекорп"
    assert meta["tags"] == ["AI", "інвестори"]   # order by name_norm, not by insertion


def test_load_prefix_meta_direction_node_has_plain_path(db):
    did, _ = _seed_tree(db)
    tid = _add_tx(db, "Обговорили кошторис.")
    _set_category(db, tid, did)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    meta = embeddings._load_prefix_meta(conn, tid)
    conn.close()
    assert meta["category_path"] == "Фонд Робота" and meta["tags"] == []


def test_tag_lands_in_prefix_and_is_searchable_but_not_in_text(db, monkeypatch):
    """The tag sounds only in the prefix: search by it finds the chunk (BM25 over
    context_prefix), and `chunks.text` stays byte for byte clean."""
    from app.services import retrieval

    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    body = "домовились закрити питання до понеділка"
    tid = _add_tx(db, body)
    _tag(db, tid, "інвестори")
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    conn = sqlite3.connect(db)
    prefix = conn.execute("SELECT context_prefix FROM chunks WHERE transcription_id = ?",
                          (tid,)).fetchone()[0]
    conn.close()
    assert "теги: інвестори" in prefix.split("\n")

    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    res = retrieval.search(db, "інвестори", top_k=5)
    assert [c["transcription_id"] for c in res["chunks"]] == [tid]
    assert res["chunks"][0]["text"] == body
    assert "інвестори" not in res["chunks"][0]["text"]


def test_embed_version_code_default_stays_two(monkeypatch):
    """ADR-016: a change of the prefix format does NOT move the code default; without an
    environment variable the module still gives epoch 2 (the same one as on `main`).

    We unset the variable explicitly, because the owner's `.env` carries its own version and it could have already
    reached `os.environ` through someone's `load_dotenv()`; the test does not read the file at
    all, so it is green both with `.env` in the root and without it."""
    monkeypatch.delenv("EMBED_VERSION", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    try:
        importlib.reload(embeddings)
        assert embeddings.EMBED_VERSION == 2
        assert embeddings.EMBED_MODEL == "intfloat/multilingual-e5-large"
    finally:
        monkeypatch.setenv("EMBED_VERSION", "2")
        monkeypatch.setenv("EMBED_MODEL", "intfloat/multilingual-e5-large")
        importlib.reload(embeddings)


def test_embed_version_epoch_comes_from_env(monkeypatch):
    """A new epoch is switched on by a `.env` line, not by a code edit: the live DB holds the pair
    (e5-large, 4), so the prefix format of this spec travels under number 5."""
    monkeypatch.setenv("EMBED_VERSION", "5")
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    try:
        importlib.reload(embeddings)
        assert embeddings.EMBED_VERSION == 5
    finally:
        monkeypatch.setenv("EMBED_VERSION", "2")
        monkeypatch.setenv("EMBED_MODEL", "intfloat/multilingual-e5-large")
        importlib.reload(embeddings)


# ============================================================
# directions-projects-tags-17: prefix format guard (epoch <-> PREFIX_FORMAT)
# ============================================================

def _stamp_epoch(path, model, version, prefix_format):
    """The row the v46 backfill (or a previous pass) leaves behind."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO embedding_epochs (model, version, prefix_format) "
                 "VALUES (?, ?, ?)", (model, version, prefix_format))
    conn.commit()
    conn.close()


def _epochs(path):
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT model, version, prefix_format FROM embedding_epochs "
                        "ORDER BY version").fetchall()
    conn.close()
    return rows


def _chunk_count(path, tid):
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM chunks WHERE transcription_id = ?",
                     (tid,)).fetchone()[0]
    conn.close()
    return n


def _status(db):
    conn = sqlite3.connect(db)
    try:
        return embeddings.epoch_status(conn)
    finally:
        conn.close()


def test_epoch_status_reads_format_of_current_pair(db, monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    assert _status(db) == {"ok": True, "reason": "new_epoch",
                           "pair": (embeddings.EMBED_MODEL, 4), "db_format": None}

    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    assert _status(db)["reason"] == "format_mismatch"

    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, embeddings.PREFIX_FORMAT)
    status = _status(db)
    assert status["reason"] == "ok" and status["db_format"] == embeddings.PREFIX_FORMAT


def test_epoch_status_without_table_falls_back_to_existing_pairs(db, monkeypatch):
    """DB below v46 (step 1 of the runbook: a check BEFORE restart): there is no memory of the
    format yet, so a pair already present in `transcriptions` counts as the old
    format; this is exactly how the migration backfill stamps it."""
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transcriptions (source_type, source_name, transcript_text, "
                 "embedding_model, embedding_version) VALUES ('file', 'Планерка', "
                 "'Обговорили кошторис.', ?, 4)", (embeddings.EMBED_MODEL,))
    conn.execute("DROP TABLE embedding_epochs")
    conn.commit()

    assert embeddings.epoch_status(conn)["reason"] == "format_mismatch"
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 5)
    assert embeddings.epoch_status(conn)["reason"] == "new_epoch"
    conn.close()


def test_embed_refuses_to_write_new_format_into_older_epoch(db, monkeypatch, caplog):
    """A restart with a forgotten `.env`: epoch 4 was built with format 1, the code writes
    format 2 - no chunk at all and EXACTLY one ERROR per process."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 4)
    monkeypatch.setattr(embeddings, "_epoch_mismatch_logged", False)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    first = _add_tx(db)
    second = _add_tx(db)

    with caplog.at_level(logging.ERROR, logger="app.services.embeddings"):
        res = embeddings.chunk_and_embed_transcription(db, first)
        res2 = embeddings.chunk_and_embed_transcription(db, second)

    assert res["status"] == "epoch_mismatch" and res2["status"] == "epoch_mismatch"
    assert _chunk_count(db, first) == 0 and _chunk_count(db, second) == 0
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1                      # do not spam on every record
    message = errors[0].getMessage()
    assert "EMBED_VERSION" in message
    assert f"{embeddings.EMBED_MODEL} / v4" in message
    # the record stayed "un-embedded"; `run` will pick it up after the epoch is raised
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT embedded_at, embedding_version FROM transcriptions "
                       "WHERE id = ?", (first,)).fetchone()
    conn.close()
    assert row[0] is None and row[1] is None


def test_embed_writes_and_registers_epoch_after_version_bump(db, monkeypatch):
    """A raised `EMBED_VERSION` means the epoch is free: chunks are written, and the DB remembers
    which format built it."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(embeddings, "_epoch_mismatch_logged", False)
    _stamp_epoch(db, embeddings.EMBED_MODEL, 4, 1)
    tid = _add_tx(db)

    monkeypatch.setattr(embeddings, "EMBED_VERSION", 5)
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    assert _chunk_count(db, tid) > 0
    assert _epochs(db) == [(embeddings.EMBED_MODEL, 4, 1),
                           (embeddings.EMBED_MODEL, 5, embeddings.PREFIX_FORMAT)]


def test_fresh_install_registers_its_own_epoch(db, monkeypatch):
    """An empty DB and the default epoch 2: the guard bothers nobody, and the epoch format
    is fixed from the very first record (PREFIX_FORMAT is not a second EMBED_VERSION)."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 2)
    monkeypatch.setattr(embeddings, "_epoch_mismatch_logged", False)
    tid = _add_tx(db)

    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"
    assert _epochs(db) == [(embeddings.EMBED_MODEL, 2, embeddings.PREFIX_FORMAT)]

    # the second epoch is registered by a separate row, the old one is not touched
    monkeypatch.setattr(embeddings, "EMBED_VERSION", 3)
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"
    assert _epochs(db) == [(embeddings.EMBED_MODEL, 2, embeddings.PREFIX_FORMAT),
                           (embeddings.EMBED_MODEL, 3, embeddings.PREFIX_FORMAT)]
