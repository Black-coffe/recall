"""Тести чанкінгу (Phase 16B провенанс блоків).

Лёгкі — embeddings імпортується без torch (модель вантажиться лише при encode).
build_chunks НЕ викликає модель → безпечно під час запису/транскрибації.
"""
import json

from app.services import embeddings as e


def test_blocks_keep_page_provenance():
    blocks = [
        {"text": "Перша сторінка про бюджет.", "page": 1, "section": "Бюджет"},
        {"text": "Друга сторінка деталей.", "page": 2, "section": "Деталі"},
    ]
    chunks = e.build_chunks(None, "", {}, json.dumps(blocks))
    assert len(chunks) == 2
    assert chunks[0]["page"] == 1 and chunks[0]["section"] == "Бюджет"
    assert chunks[1]["page"] == 2 and chunks[1]["section"] == "Деталі"


def test_large_block_splits_but_keeps_same_page():
    big = {"text": "Речення номер один. " * 300, "page": 5, "section": "Розділ"}
    chunks = e.build_chunks(None, "", {}, json.dumps([big]))
    assert len(chunks) > 1  # розбився на під-чанки
    assert all(c["page"] == 5 for c in chunks)
    assert all(c["section"] == "Розділ" for c in chunks)
    # індекси послідовні
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_blocks_do_not_cross_page_boundary():
    # короткі блоки → кожен лишається окремим чанком зі своєю сторінкою
    blocks = [{"text": f"Сторінка {i}", "page": i, "section": None} for i in range(1, 6)]
    chunks = e.build_chunks(None, "", {}, json.dumps(blocks))
    assert [c["page"] for c in chunks] == [1, 2, 3, 4, 5]


def test_audio_segments_path_unaffected():
    seg = json.dumps([{"text": "привіт усім", "start": 0.0, "end": 1.5, "speaker": "self"}])
    chunks = e.build_chunks(seg, "привіт усім", {})
    assert chunks and chunks[0].get("page") is None
    assert "speaker" in chunks[0]
    assert chunks[0]["start_time"] == 0.0


def test_plain_text_path_unaffected():
    chunks = e.build_chunks(None, "просто текст без структури", {})
    assert chunks and chunks[0].get("page") is None
    assert chunks[0]["text"] == "просто текст без структури"


def test_empty_structure_falls_back_to_text():
    # structure_json з порожніми блоками → не падаємо, йдемо у текстовий шлях
    chunks = e.build_chunks(None, "запасний текст", {}, json.dumps([{"text": "", "page": 1}]))
    assert chunks and chunks[0]["text"] == "запасний текст"
    assert chunks[0].get("page") is None
