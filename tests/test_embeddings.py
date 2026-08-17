"""Юніт-тести чанкінгу (app/services/embeddings.py) — T7.5.

Офлайн, без torch/sentence-transformers/GPU — тестуємо ЛИШЕ детерміновані
хелпери нарізки тексту (_window_text/_split_sentences/_chunk_from_segments/
_chunk_from_blocks/build_chunks), не embed_texts/embed_query (потребують
реальної моделі — поза межами цього тесту). Покриває межі нарізки:
короткі/довгі речення, кирилицю, overlap, межу _MAX_CHARS.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import embeddings


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
# _window_text — межі нарізки
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
    # Рівно один символ понад ліміт І без пунктуації для розбиття на речення —
    # _split_sentences віддає весь текст як ОДНЕ "речення", яке відразу
    # перевищує _MAX_CHARS при першому ж append → flush з overlap-хвостом.
    text = "а" * (embeddings._MAX_CHARS + 1)
    pieces = embeddings._window_text(text)
    assert len(pieces) == 2
    assert pieces[0] == text
    # ні речень, ні пробілів → різати нема по чому, хвіст лишається сирим зрізом
    assert pieces[1] == text[-embeddings._TEXT_OVERLAP:]
    assert len(pieces[1]) == embeddings._TEXT_OVERLAP


def test_window_text_long_multisentence_text_splits_with_overlap():
    sentence = "Це речення про проєкт та бюджет команди. "
    text = sentence * 60  # довший за _MAX_CHARS, багато речень-делімітерів
    pieces = embeddings._window_text(text)
    assert len(pieces) >= 2
    for p in pieces:
        assert p  # непорожні
    # overlap: хвіст попереднього шматка є префіксом наступного — але зрізаний
    # по межі речення/слова, а не посеред слова (T6.5)
    tail = embeddings._overlap_tail(pieces[0], embeddings._TEXT_OVERLAP)
    assert pieces[1].startswith(tail)
    assert pieces[1][0].isupper(), "перекриття має починатись з нового речення"


# ============================================================
# _overlap_tail — межа перекриття (T6.5)
# ============================================================

def test_overlap_tail_snaps_to_sentence_start():
    text = "Довга перша частина про бюджет команди. " * 10 + "Останнє речення про терміни."
    tail = embeddings._overlap_tail(text, 60)
    assert tail.startswith("Останнє речення")
    assert len(tail) <= 60


def test_overlap_tail_falls_back_to_word_boundary():
    """Немає кінця речення в хвості → ріжемо принаймні по межі слова, а не
    посеред нього (саме тут сирий зріз [-N:] і псував чанків архіву)."""
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
    """Без крапок/знаків — весь текст один 'sentence', overlap-хвіст все одно
    коротший за оригінал (не зациклюється)."""
    text = "слово " * 500  # ~3000 символів, без .!?, але з пробілами (split_sentences не ріже)
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
    ("SPEAKER_0", {"SPEAKER_0": ""}, "Спікер 1"),  # порожнє ім'я в мапі — fallback
    ("Гість", {}, "Гість"),  # довільна мітка проходить як є
])
def test_resolve_speaker(raw, speaker_map, expected):
    assert embeddings._resolve_speaker(raw, speaker_map) == expected


# ============================================================
# _chunk_from_segments — межі нарізки по сегментах
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
    """T6.5: сегмент, який сам більший за ліміт, НЕ тягнеться в наступний чанк
    як перекриття — інакше він дублювався б у кожному наступному вікні (до
    T6.5 такий сегмент давав три чанки, два з яких були ним же)."""
    big = "слово " * 200  # ~1200 символів > _MAX_CHARS(1000)
    segs = [_seg("коротке", 0, 1), _seg(big, 1, 2), _seg("хвіст", 2, 3)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 2
    assert chunks[0]["text"] == f"коротке {big.strip()}"
    assert chunks[1]["text"] == "хвіст"
    assert [c["chunk_index"] for c in chunks] == [0, 1]


def test_chunk_from_segments_overlap_carries_tail_on_limit_cut():
    """Розрив за лімітом = думку обрізано штучно → наступний чанк починається
    з хвоста попереднього (~_SEG_OVERLAP_CHARS), щоб контекст не загубився."""
    seg_text = "Це доволі довге речення про бюджет проєкту на наступний квартал."
    n = embeddings._MAX_CHARS // len(seg_text) + 2
    segs = [_seg(seg_text, i * 2.0, i * 2.0 + 1.9) for i in range(n)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) >= 2
    # хвіст першого чанку дослівно починає другий
    assert chunks[1]["text"].startswith(seg_text)
    assert chunks[0]["text"].endswith(seg_text)
    # перекриття обмежене: не більше кількох сегментів, а не пів-чанку
    assert len(chunks[1]["text"]) < embeddings._MAX_CHARS


def test_chunk_from_segments_overlap_never_exceeds_budget():
    """Перекриття обмежене бюджетом, а не «хоч один сегмент». Довгий сегмент,
    що спричинив розрив за лімітом, у наступний чанк НЕ переїжджає — інакше
    900-символьний монолог дублювався б в індексі і перебивав голосування
    за спікера наступного чанку."""
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
    """Якщо ліміт спрацював на ОСТАННЬОМУ сегменті, хвіст не випускається
    окремим чанком: він цілком міститься в попередньому."""
    seg_text = "Речення про бюджет команди на наступний квартал і ризики."
    n = embeddings._MAX_CHARS // len(seg_text) + 1
    segs = [_seg(seg_text, i * 2.0, i * 2.0 + 1.9) for i in range(n)]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1, "хвіст після останнього розриву — не окремий чанк"


def test_chunk_from_segments_min_chars_counts_own_text_only():
    """Поріг _MIN_CHARS рахується по СВОЄМУ тексту: перенесений хвіст не має
    достроково «дозволяти» природну межу."""
    filler = "Слова про порядок денний зустрічі команди. "
    n = embeddings._MAX_CHARS // len(filler) + 1
    segs = [_seg(filler, i * 1.0, i * 1.0 + 0.9, speaker="SPEAKER_0") for i in range(n)]
    # одразу після розриву за лімітом — короткий свій сегмент і зміна спікера
    segs.append(_seg("Коротка відповідь.", n * 1.0, n * 1.0 + 1, speaker="SPEAKER_1"))
    segs.append(_seg("І ще одна репліка.", n * 1.0 + 2, n * 1.0 + 3, speaker="SPEAKER_1"))
    chunks = embeddings._chunk_from_segments(segs, {})
    tail_chunk = chunks[-1]["text"]
    assert "Коротка відповідь." in tail_chunk and "І ще одна репліка." in tail_chunk, \
        "своїх символів менше _MIN_CHARS → різати на зміні спікера ще рано"


def test_chunk_from_segments_splits_on_speaker_change_after_min_chars():
    """T6.5: зміна спікера — природна межа. До неї чанк мав бути не коротшим
    за _MIN_CHARS, інакше діалог покришився б на репліки."""
    a = "Ми домовились підняти бюджет на маркетинг у другому кварталі. " * 6  # > _MIN_CHARS
    b = "Погоджуюсь, тоді я готую розрахунок до пʼятниці."
    segs = [_seg(a, 0, 30, speaker="SPEAKER_0"), _seg(b, 30, 35, speaker="SPEAKER_1")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 2
    assert chunks[0]["speaker"] == "Спікер 1"
    assert chunks[1]["speaker"] == "Спікер 2"
    assert chunks[1]["text"] == b
    # межа природна → перекриття НЕ додається
    assert a.strip() not in chunks[1]["text"]


def test_chunk_from_segments_short_exchange_stays_one_chunk():
    """Швидкий обмін репліками (обидві короткі) не ріжеться: _MIN_CHARS не набрано."""
    segs = [_seg("А ти дзвонив клієнту?", 0, 2, speaker="SPEAKER_0"),
            _seg("Так, вчора ввечері.", 2, 4, speaker="SPEAKER_1")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1
    assert "дзвонив" in chunks[0]["text"] and "вчора" in chunks[0]["text"]


def test_chunk_from_segments_splits_on_long_pause():
    """Пауза >= _PAUSE_BOUNDARY_SEC читається як кінець думки (без діаризації)."""
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
    """SPEAKER_UNKNOWN — діра діаризації, а не учасник: вхід/вихід у неї не ріже."""
    a = "Говоримо про терміни здачі і про те, хто відповідальний. " * 7
    segs = [_seg(a, 0, 20, speaker="SPEAKER_0"),
            _seg("Незрозумілий шматок.", 20, 21, speaker="SPEAKER_UNKNOWN"),
            _seg("Продовжую думку.", 21, 22, speaker="SPEAKER_0")]
    chunks = embeddings._chunk_from_segments(segs, {})
    assert len(chunks) == 1


def test_chunk_from_segments_timecodes_follow_retained_segments():
    """start_time/end_time беруться з СЕГМЕНТІВ, що реально лежать у чанку
    (включно з перенесеним хвостом), а не з лічильника вікна."""
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
    assert chunks[0]["speaker"] == "Спікер 1"  # SPEAKER_0 → "Спікер 1" (домінує по символах)


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
# _chunk_from_blocks — документи з провенансом (page/section)
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
    """Великий блок ріжеться sentence-aware на під-чанки з ТІЄЮ Ж page/section —
    але межа блоку ніколи не перетинається одним чанком."""
    big_text = ("Речення блоку один. " * 60)
    blocks = [
        {"text": big_text, "page": 1, "section": "A"},
        {"text": "Короткий блок два.", "page": 2, "section": "B"},
    ]
    chunks = embeddings._chunk_from_blocks(blocks)
    assert len(chunks) >= 3  # блок 1 розбито на 2+ шматки + блок 2
    assert all(c["page"] == 1 for c in chunks[:-1])
    assert chunks[-1]["page"] == 2 and chunks[-1]["text"] == "Короткий блок два."


# ============================================================
# build_chunks — вибір стратегії
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
# blob_to_vec — round-trip (не потребує моделі)
# ============================================================

def test_blob_to_vec_roundtrip():
    import numpy as np
    vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    blob = vec.tobytes()
    restored = embeddings.blob_to_vec(blob)
    assert np.allclose(restored, vec)


# ============================================================
# T6.8: EMBED_VERSION — структурна idempotency (без форс re-embed)
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
    assert calls["n"] == 1  # вдруге НЕ перекодовували


def test_embed_null_version_legacy_rows_are_reembedded(db, monkeypatch):
    """T6.5: рядки, embedded ДО міграції v28 (embedding_version=NULL), — це
    завідомо СТАРА нарізка, тож вони підлягають re-embed. Раніше NULL вважався
    сумісним (щоб міграція не перерахувала архів заднім числом); з переходом на
    природні межі це стало б тихою дірою — частина архіву назавжди з іншим
    чанкінгом."""
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
    assert calls["n"] == 2  # NULL-версія = стара нарізка → перекодовано

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT embedding_version FROM transcriptions WHERE id = ?",
                       (tid,)).fetchone()
    conn.close()
    assert row[0] == embeddings.EMBED_VERSION


def test_embed_version_bump_forces_reembed(db, monkeypatch):
    """Явний бамп EMBED_VERSION (майбутня зміна логіки чанкінгу) МАЄ
    детектуватись і форснути re-embed — перевіряємо, що структурне
    підключення справді працює (не лише декларація константи)."""
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
    assert calls["n"] == 2  # версія змінилась → перекодували


# ============================================================
# Волна 4: Telegram — автор у чанку + заглушки не стають векторами
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
    """У TG немає segments, тож build_chunks лишав speaker=None — на живих даних
    порожній у ВСІХ 5857 чанках. Через це RAG не міг сказати, ХТО це написав,
    хоча імʼя лежало в сусідній колонці."""
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
    """«[фото без тексту]» — службова позначка, а не текст: 152 однакові точки
    конкурували за місце у видачі і не означали нічого."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    tid = _add_tg_tx(db, "[фото без тексту]")
    res = embeddings.chunk_and_embed_transcription(db, tid)
    assert res["status"] == "skipped" and res["reason"] == "tg_contentless"

    conn = sqlite3.connect(db)
    chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE transcription_id = ?",
                          (tid,)).fetchone()[0]
    # embedded_at МАЄ бути проставлений: інакше індексер вічно вважатиме це роботою.
    marked = conn.execute("SELECT embedded_at IS NOT NULL FROM transcriptions WHERE id = ?",
                          (tid,)).fetchone()[0]
    conn.close()
    assert chunks == 0 and marked == 1


def test_contentless_matcher_keeps_real_text():
    assert embeddings._is_contentless_tg("[фото без тексту]")
    assert embeddings._is_contentless_tg("  [відео без розпізнаного тексту] ")
    assert not embeddings._is_contentless_tg("[Acmecorp] рахунок виставили")
    assert not embeddings._is_contentless_tg("оплата у пʼятницю")


def test_contentless_tg_removes_previously_indexed_junk(db, monkeypatch):
    """Переіндексація має ПРИБИРАТИ старі сміттєві вектори, а не лише перестати
    робити нові: інакше фільтр заглушок нічого не змінює для наявного архіву
    (спіймано на живих даних — 188 «[фото без тексту]» пережили прохід)."""
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    tid = _add_tg_tx(db, "справжній текст повідомлення")
    assert embeddings.chunk_and_embed_transcription(db, tid)["status"] == "embedded"

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE transcription_id = ?",
                        (tid,)).fetchone()[0] > 0
    # текст став заглушкою (напр. правка або повторний інжест медіа без розпізнавання)
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
