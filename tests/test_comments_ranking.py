"""Шар коментарів, Волна 2: пріоритет у ранжуванні, підшивка, промпт.

Три шари пріоритету перевіряються окремо, бо ламаються теж окремо:
  1. буст у RRF — коментар виграє у транскрипта при рівній релевантності;
  2. `attach_comments` — виправлення приїжджає, навіть коли САМЕ не збіглося;
  3. `_build_context` — модель бачить його першим і з явною позначкою.
"""
import sqlite3

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import comments, embeddings, rag, retrieval


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return a / n if n else a


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    init_database(path)
    return path


@pytest.fixture
def mock_embeddings(monkeypatch):
    """EMBED_DIM=4; запит завжди [1,0,0,0]; будь-який текст кодується як
    [1,0,0,0] — тобто ВЕКТОРНА релевантність у всіх однакова. Це навмисно:
    так тест міряє саме буст типу, а не випадкову різницю косинусів."""
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_query", lambda q: _unit([1.0, 0.0, 0.0, 0.0]))
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, batch_size=32: np.stack(
                            [_unit([1.0, 0.0, 0.0, 0.0]) for _ in texts]))
    return embeddings


def _add_tx(path, name="Дзвінок", date="2026-08-01", category_id=None):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "meeting_date, category_id) VALUES ('file', ?, 'x', ?, ?)",
        (name, date, category_id))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return tid


def _add_chunk(path, tid, idx, text, vec=(1.0, 0.0, 0.0, 0.0)):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text, embedding) VALUES (?, ?, 0, 10, 'Ви', ?, ?)",
        (tid, idx, text, _unit(vec).tobytes()))
    conn.commit(); conn.close()


def _comment(path, tid, body, kind="note", **kw):
    c = comments.create(path, "transcription", tid, body, kind=kind, **kw)
    comments.index_comment(path, c["id"])
    return c


# --------------------------------------------------- шар 1: буст у ранжуванні

def test_comment_appears_in_results(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "обговорили бюджет проєкту")
    _comment(db, tid, "насправді бюджет 12 тисяч, а не 20")
    res = retrieval.search(db, "бюджет", top_k=5)
    kinds = [c["source_type"] for c in res["chunks"]]
    assert "comment" in kinds


def test_correction_outranks_transcript_at_equal_relevance(db, mock_embeddings):
    """Головна обіцянка шару: при однаковій релевантності виправлення
    власника стоїть вище сирої стенограми."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту двадцять тисяч")
    _comment(db, tid, "бюджет проєкту дванадцять тисяч", kind="correction")
    res = retrieval.search(db, "бюджет проєкту", top_k=5)
    assert res["chunks"][0]["source_type"] == "comment"
    assert res["chunks"][0]["comment_kind"] == "correction"


def test_kind_orders_comments_among_themselves(db, mock_embeddings):
    tid = _add_tx(db)
    _comment(db, tid, "бюджет питання відкрите", kind="question")
    _comment(db, tid, "бюджет виправлення суми", kind="correction")
    res = retrieval.search(db, "бюджет", top_k=5)
    got = [c["comment_kind"] for c in res["chunks"] if c["source_type"] == "comment"]
    assert got[0] == "correction"


def test_zero_weight_disables_boost(db, mock_embeddings):
    """Ручка мусить вимикатись — інакше налаштувати баланс неможливо."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    _comment(db, tid, "бюджет проєкту", kind="correction")
    boosted = retrieval.search(db, "бюджет проєкту", top_k=5)["chunks"]
    flat = retrieval.search(db, "бюджет проєкту", top_k=5, comment_weight=0.0)["chunks"]
    b = next(c for c in boosted if c["source_type"] == "comment")["score"]
    f = next(c for c in flat if c["source_type"] == "comment")["score"]
    assert b > f


def test_include_comments_false_excludes_them(db, mock_embeddings):
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    _comment(db, tid, "бюджет уточнення")
    res = retrieval.search(db, "бюджет", top_k=5, include_comments=False)
    assert all(c["source_type"] != "comment" for c in res["chunks"])


def test_comment_chunk_ids_do_not_collide_with_transcript_ids(db, mock_embeddings):
    """Простори id двох таблиць перетинаються; у спільній видачі вони мусять
    лишатись різними, інакше пруф інсайту копілота стає двозначним."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "бюджет проєкту")
    _comment(db, tid, "бюджет уточнення")
    ids = [c["chunk_id"] for c in retrieval.search(db, "бюджет", top_k=5)["chunks"]]
    assert len(ids) == len(set(ids))
    assert any(i < 0 for i in ids) and any(i > 0 for i in ids)


def test_deleted_comment_leaves_search(db, mock_embeddings):
    tid = _add_tx(db)
    c = _comment(db, tid, "унікальнетутслово")
    assert retrieval.search(db, "унікальнетутслово", top_k=5)["chunks"]
    comments.delete(db, c["id"])
    assert retrieval.search(db, "унікальнетутслово", top_k=5)["chunks"] == []


def test_comment_respects_category_scope(db, mock_embeddings):
    """Під активним напрямком коментар чужого напрямку не має протікати."""
    t_in, t_out = _add_tx(db, "in", category_id=1), _add_tx(db, "out", category_id=2)
    _comment(db, t_in, "бюджет свій")
    _comment(db, t_out, "бюджет чужий")
    res = retrieval.search(db, "бюджет", top_k=5, category_id=1)
    bodies = [c["text"] for c in res["chunks"] if c["source_type"] == "comment"]
    assert bodies == ["бюджет свій"]


def test_comments_bypass_per_meeting_cap(db, mock_embeddings):
    """Кеп «3 з одного мітингу» захищає від багатослівного дзвінка, а не від
    уточнень: чотири коментарі до одного запису не мусять душити один одного
    за цим кепом (стеля частки — окремий механізм, див. нижче)."""
    tid = _add_tx(db)
    for i in range(4):
        _comment(db, tid, f"бюджет уточнення номер {i}", kind="correction")
    res = retrieval.search(db, "бюджет уточнення", top_k=8, max_per_meeting=3,
                           comment_share=1.0)
    n = sum(1 for c in res["chunks"] if c["source_type"] == "comment")
    assert n == 4


# ------------------------------------------- стеля частки коментарів (2.5)

def test_comments_cannot_take_the_whole_top(db, mock_embeddings):
    """Знайдено заміром: без стелі коментарі забирали 115 слотів зі 120, а на
    восьми питаннях із пʼятнадцяти весь топ складався з самих коментарів —
    модель отримувала уточнення БЕЗ матеріалу, який вони уточнюють."""
    tid = _add_tx(db)
    for i in range(10):
        _comment(db, tid, f"бюджет уточнення {i}", kind="correction")
    # Чанки РОЗНЕСЕНІ по записах: інакше per-meeting cap сам обрізав би архів
    # до трьох, топ лишився б недобраним, і переповнення коментарями було б
    # законним — тобто тест міряв би не ту стелю.
    for i in range(10):
        t = _add_tx(db, f"запис {i}")
        _add_chunk(db, t, 0, f"бюджет обговорення {i}")
    res = retrieval.search(db, "бюджет", top_k=9)
    n_cm = sum(1 for c in res["chunks"] if c["source_type"] == "comment")
    assert len(res["chunks"]) == 9
    assert n_cm == 3                      # 9 * 0.34 → 3
    assert n_cm < len(res["chunks"])      # архів у топі лишився


def test_cap_keeps_at_least_one_comment_on_tiny_k(db, mock_embeddings):
    """Єдине виправлення мусить доїхати навіть при крихітному вікні."""
    tid = _add_tx(db)
    _comment(db, tid, "бюджет виправлення", kind="correction")
    for i in range(5):
        _add_chunk(db, tid, i, f"бюджет обговорення {i}")
    res = retrieval.search(db, "бюджет", top_k=2)
    assert sum(1 for c in res["chunks"] if c["source_type"] == "comment") == 1


def test_capped_comments_still_rank_first(db, mock_embeddings):
    """Стеля обмежує КІЛЬКІСТЬ, а не пріоритет: ті, що пройшли, стоять зверху."""
    tid = _add_tx(db)
    _comment(db, tid, "бюджет виправлення", kind="correction")
    for i in range(5):
        _add_chunk(db, tid, i, f"бюджет обговорення {i}")
    res = retrieval.search(db, "бюджет", top_k=6)
    assert res["chunks"][0]["source_type"] == "comment"


def test_overflow_comments_fill_empty_slots(db, mock_embeddings):
    """Якщо архівного матеріалу під запит немає, краще віддати коментарі, ніж
    порожнечу — зайві зсуваються в кінець, а не викидаються."""
    tid = _add_tx(db)
    for i in range(5):
        _comment(db, tid, f"бюджет уточнення {i}", kind="correction")
    res = retrieval.search(db, "бюджет", top_k=5)
    assert len(res["chunks"]) == 5
    assert all(c["source_type"] == "comment" for c in res["chunks"])


def test_cap_is_tunable_and_disablable(db, mock_embeddings):
    tid = _add_tx(db)
    for i in range(6):
        _comment(db, tid, f"бюджет уточнення {i}", kind="correction")
    for i in range(6):
        t = _add_tx(db, f"запис {i}")
        _add_chunk(db, t, 0, f"бюджет обговорення {i}")
    tight = retrieval.search(db, "бюджет", top_k=6, comment_share=0.0)["chunks"]
    wide = retrieval.search(db, "бюджет", top_k=6, comment_share=1.0)["chunks"]
    assert sum(1 for c in tight if c["source_type"] == "comment") == 1   # мінімум
    assert sum(1 for c in wide if c["source_type"] == "comment") == 6


def test_cap_helper_preserves_order():
    """top_k=6, share=0.34 → дозволено 2 коментарі; третій іде в хвіст, а не
    зникає, і порядок решти не рухається."""
    ids = [-1, 5, -2, 6, -3, 7]
    assert retrieval._cap_comments(ids, top_k=6, share=0.34) == [-1, 5, -2, 6, 7, -3]
    assert retrieval._cap_comments(ids, top_k=6, share=1.0) == ids


def test_search_survives_db_without_comment_tables(tmp_path, mock_embeddings):
    """Стара БД без міграції v37 не має валити пошук — це головний шлях
    усього продукту."""
    path = str(tmp_path / "old.db")
    init_database(path)
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE comment_chunks_fts; DROP TABLE comment_chunks; "
                       "DROP TABLE comments;")
    conn.commit(); conn.close()
    tid = _add_tx(path)
    _add_chunk(path, tid, 0, "бюджет проєкту")
    res = retrieval.search(path, "бюджет", top_k=5)
    assert len(res["chunks"]) == 1


# ------------------------------------------------------- шар 2: автопідшивка

def test_attach_pulls_correction_without_query_match(db, mock_embeddings):
    """Те, заради чого шар 2 існує: «Іван більше не в проєкті» не має спільних
    слів із «хто відповідає за фасад», тож бустом його не дістати."""
    tid = _add_tx(db)
    _add_chunk(db, tid, 0, "за фасад відповідає Іван")
    _comment(db, tid, "Іван більше не в проєкті", kind="correction")
    chunks = [{"transcription_id": tid, "source_type": "file", "text": "..."}]
    attached = retrieval.attach_comments(db, chunks)
    assert [a["text"] for a in attached] == ["Іван більше не в проєкті"]


def test_attach_skips_plain_notes(db, mock_embeddings):
    """Інакше кожна відповідь тягла б усі замітки картки і шар став би шумом."""
    tid = _add_tx(db)
    _comment(db, tid, "звичайна замітка", kind="note")
    attached = retrieval.attach_comments(
        db, [{"transcription_id": tid, "source_type": "file", "text": "..."}])
    assert attached == []


def test_attach_includes_pinned_of_any_kind(db, mock_embeddings):
    tid = _add_tx(db)
    _comment(db, tid, "закріплена замітка", kind="note", pinned=True)
    attached = retrieval.attach_comments(
        db, [{"transcription_id": tid, "source_type": "file", "text": "..."}])
    assert len(attached) == 1 and attached[0]["pinned"] is True


def test_attach_does_not_duplicate_a_direct_hit(db, mock_embeddings):
    tid = _add_tx(db)
    c = _comment(db, tid, "виправлення", kind="correction")
    chunks = [
        {"transcription_id": tid, "source_type": "file", "text": "..."},
        {"source_type": "comment", "comment_id": c["id"], "text": "виправлення"},
    ]
    assert retrieval.attach_comments(db, chunks) == []


def test_attach_respects_cap(db, mock_embeddings):
    tid = _add_tx(db)
    for i in range(6):
        _comment(db, tid, f"виправлення {i}", kind="correction")
    attached = retrieval.attach_comments(
        db, [{"transcription_id": tid, "source_type": "file", "text": "..."}], cap=3)
    assert len(attached) == 3


# ------------------------------------------------------------- шар 3: промпт

def test_context_puts_comments_first_and_labels_them():
    chunks = [
        {"source_type": "file", "source_name": "Дзвінок", "meeting_date": "2026-08-01",
         "text": "бюджет двадцять тисяч", "speaker": "Ви"},
        {"source_type": "comment", "comment_kind": "correction",
         "target_label": "Дзвінок", "meeting_date": "2026-08-02",
         "text": "бюджет дванадцять тисяч", "pinned": False},
    ]
    ctx = rag._build_context(chunks)
    assert ctx.startswith("[1] КОМЕНТАР ВЛАСНИКА (ВИПРАВЛЕННЯ)")
    assert "[2] Мітинг «Дзвінок»" in ctx


def test_context_numbers_attached_comments_too():
    chunks = [{"source_type": "file", "source_name": "Дзвінок",
               "meeting_date": "2026-08-01", "text": "текст"}]
    attached = [{"kind": "correction", "target_label": "Дзвінок", "date": "2026-08-02",
                 "text": "уточнення", "pinned": True, "anchor_time": 65.0}]
    ctx = rag._build_context(chunks, attached)
    assert "[1] КОМЕНТАР ВЛАСНИКА (ВИПРАВЛЕННЯ), закріплений" in ctx
    assert "до моменту ~01:05" in ctx
    assert "[2] Мітинг" in ctx


def test_context_unchanged_without_comments():
    """Регресія: без коментарів контекст мусить лишитись побайтово тим самим,
    інакше шар мовчки змінив би поведінку всіх наявних відповідей."""
    chunks = [{"source_type": "file", "source_name": "Дзвінок",
               "meeting_date": "2026-08-01", "text": "текст", "speaker": "Ви"}]
    assert rag._build_context(chunks) == (
        "[1] Мітинг «Дзвінок» (2026-08-01), спікер: Ви\nтекст")


def test_system_prompt_states_priority_rule():
    assert "КОМЕНТАРІ ВЛАСНИКА" in rag._RAG_SYSTEM_PROMPT
    assert "ВИЩИЙ пріоритет за сирий транскрипт" in rag._RAG_SYSTEM_PROMPT
    assert "«виправлення» скасовує" in rag._RAG_SYSTEM_PROMPT


# ------------------------------------------ регресії за код-рев'ю (merge)

def test_citation_numbers_match_the_sources_array():
    """Знайдено рев'ю: контекст ставив коментарі першими, а `sources` йшов у
    порядку видачі — і [2] у відповіді відкривав чуже джерело, а останній
    номер узагалі виходив за межі масиву. Обидва тепер із order_citables."""
    chunks = [
        {"source_type": "file", "source_name": "T1", "meeting_date": "2026-08-01",
         "text": "перший"},
        {"source_type": "file", "source_name": "T2", "meeting_date": "2026-08-02",
         "text": "другий"},
    ]
    attached = [{"kind": "correction", "target_label": "T1", "date": "2026-08-03",
                 "text": "виправлення", "pinned": True, "anchor_time": None}]
    sources = rag.order_citables(chunks, attached)
    ctx = rag._build_context(chunks, attached)
    assert len(sources) == 3
    # Кожен номер у контексті має вести на той самий елемент, що й sources[n-1].
    assert sources[0]["text"] == "виправлення"
    assert ctx.startswith("[1] КОМЕНТАР ВЛАСНИКА")
    assert "[2] Мітинг «T1»" in ctx and sources[1]["source_name"] == "T1"
    assert "[3] Мітинг «T2»" in ctx and sources[2]["source_name"] == "T2"


def test_order_citables_is_identity_without_comments():
    chunks = [{"source_type": "file", "source_name": "T1", "text": "x"}]
    assert rag.order_citables(chunks, []) == chunks
    assert rag.order_citables(chunks) == chunks


def test_attached_comment_is_citable_and_openable():
    """Підшитий коментар пронумеровано в контексті, отже клієнт мусить уміти
    його відкрити — у ньому має лишитись transcription_id."""
    attached = [{"kind": "correction", "target_label": "T1", "date": "2026-08-03",
                 "text": "виправлення", "transcription_id": 42}]
    s = rag.order_citables([], attached)
    assert s[0]["transcription_id"] == 42 and s[0]["source_type"] == "comment"
