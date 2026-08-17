"""Тести гібридного retrieval (Phase 15A): свіжість + диверсифікація.

Embeddings мокаються (EMBED_DIM=4, фіксований query-вектор) — реальна модель
e5-large (2GB) не вантажиться. FTS5 — справжній (триггери chunks_ai наповнюють
chunks_fts при INSERT у chunks).
"""
import math
import sqlite3
from datetime import datetime

import numpy as np
import pytest

from app.db.migrations import init_database
from app.services import embeddings, reranker, retrieval


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
    """is_available()=True, EMBED_DIM=4, embed_query → фіксований [1,0,0,0]."""
    monkeypatch.setattr(embeddings, "EMBED_DIM", 4)
    monkeypatch.setattr(embeddings, "is_available", lambda: True)
    monkeypatch.setattr(embeddings, "embed_query",
                        lambda q: _unit([1.0, 0.0, 0.0, 0.0]))
    return embeddings


def _add_tx(path, source_name, meeting_date=None, category_id=None,
            source_type="file", transcript="x"):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
        "meeting_date, category_id) VALUES (?, ?, ?, ?, ?)",
        (source_type, source_name, transcript, meeting_date, category_id),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _add_chunk(path, tid, idx, text, vec):
    """vec — список float (буде нормалізовано); None → без embedding (тільки FTS)."""
    blob = _unit(vec).tobytes() if vec is not None else None
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO chunks (transcription_id, chunk_index, start_time, end_time, "
        "speaker, text, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, idx, idx * 1.0, idx * 1.0 + 1, None, text, blob),
    )
    conn.commit()
    conn.close()


# ============================================================
# _recency_factor
# ============================================================

def test_recency_factor_today_is_one():
    today = datetime.now().strftime("%Y-%m-%d")
    assert retrieval._recency_factor(today) == pytest.approx(1.0, abs=0.05)


def test_recency_factor_half_life():
    half = retrieval._RECENCY_HALF_LIFE_DAYS
    past = (datetime.now() - __import__("datetime").timedelta(days=half)).strftime("%Y-%m-%d")
    assert retrieval._recency_factor(past) == pytest.approx(0.5, abs=0.05)


def test_recency_factor_unknown_is_zero():
    assert retrieval._recency_factor(None) == 0.0
    assert retrieval._recency_factor("not-a-date") == 0.0


# ============================================================
# Базова поведінка
# ============================================================

def test_empty_query_returns_empty(db, mock_embeddings):
    assert retrieval.search(db, "")["chunks"] == []
    assert retrieval.search(db, "   ")["chunks"] == []


def test_no_results_returns_empty(db, mock_embeddings):
    # Жодного чанка в БД
    res = retrieval.search(db, "zzznotfoundzzz")
    assert res["chunks"] == []
    assert res["vector_available"] is True


# ============================================================
# Диверсифікація: один митинг не з'їдає весь топ
# ============================================================

def test_per_meeting_cap_limits_domination(db, mock_embeddings):
    big = _add_tx(db, "Великий мітинг")
    small = _add_tx(db, "Інший мітинг")
    q = [1.0, 0, 0, 0]  # усі чанки максимально релевантні (cosine 1)
    for i in range(5):
        _add_chunk(db, big, i, f"alpha beta gamma {i}", q)
    _add_chunk(db, small, 0, "alpha beta gamma small", q)

    # Запит-слово відсутнє в тексті → FTS порожній, працює лише вектор
    res = retrieval.search(db, "qqqzzz", top_k=4, max_per_meeting=2, recency_weight=0)
    tids = [c["transcription_id"] for c in res["chunks"]]
    assert len(res["chunks"]) == 4
    assert tids.count(big) <= 2 + 2  # кеп=2, недобір дозволено добити; але small має бути
    assert small in tids, "малий мітинг мусить потрапити завдяки диверсифікації"
    assert len(set(tids)) >= 2


def test_no_cap_lets_one_meeting_dominate(db, mock_embeddings):
    big = _add_tx(db, "Великий мітинг")
    small = _add_tx(db, "Інший мітинг")
    q = [1.0, 0, 0, 0]
    for i in range(5):
        _add_chunk(db, big, i, f"alpha {i}", q)
    _add_chunk(db, small, 0, "alpha small", q)

    res = retrieval.search(db, "qqqzzz", top_k=4, max_per_meeting=0, recency_weight=0)
    tids = [c["transcription_id"] for c in res["chunks"]]
    assert tids == [big, big, big, big]  # без кепу великий мітинг займає всі слоти


# ============================================================
# Свіжість: при рівній релевантності виграє новіший
# ============================================================

def test_recency_promotes_newer(db, mock_embeddings):
    old = _add_tx(db, "Старий", meeting_date="2020-01-01")
    new = _add_tx(db, "Новий", meeting_date=datetime.now().strftime("%Y-%m-%d"))
    # OLD трохи релевантніший (cosine 1.0), NEW трохи менше (cosine 0.95)
    _add_chunk(db, old, 0, "alpha", [1.0, 0, 0, 0])
    _add_chunk(db, new, 0, "alpha", [0.95, math.sqrt(1 - 0.95 ** 2), 0, 0])

    # Без свіжості перемагає релевантніший OLD
    res0 = retrieval.search(db, "qqqzzz", top_k=2, recency_weight=0)
    assert res0["chunks"][0]["transcription_id"] == old

    # Зі свіжістю NEW (сьогодні) обганяє OLD попри трохи нижчу релевантність
    res1 = retrieval.search(db, "qqqzzz", top_k=2, recency_weight=0.25)
    assert res1["chunks"][0]["transcription_id"] == new


# ============================================================
# FTS-fallback: працює без embeddings
# ============================================================

def test_fts_only_when_embeddings_unavailable(db, monkeypatch):
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db, "Лексичний")
    _add_chunk(db, tid, 0, "рішення про бюджет фонду", None)

    res = retrieval.search(db, "бюджет")
    assert res["vector_available"] is False
    assert len(res["chunks"]) == 1
    assert res["chunks"][0]["transcription_id"] == tid
    assert "fts" in res["chunks"][0]["matched_by"]


# ============================================================
# Фільтр напрямку (category_id)
# ============================================================

def test_category_filter(db, mock_embeddings):
    a = _add_tx(db, "Фонд", category_id=1)
    b = _add_tx(db, "Особисте", category_id=2)
    _add_chunk(db, a, 0, "спільне слово альфа", [1.0, 0, 0, 0])
    _add_chunk(db, b, 0, "спільне слово альфа", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz", category_id=1)
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tids == {a}


def test_category_none_filter(db, mock_embeddings):
    a = _add_tx(db, "Без напрямку", category_id=None)
    b = _add_tx(db, "З напрямком", category_id=1)
    _add_chunk(db, a, 0, "альфа", [1.0, 0, 0, 0])
    _add_chunk(db, b, 0, "альфа", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz", category_id="none")
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tids == {a}


# ============================================================
# T6.8: м'який FTS relevance-cutoff (довгі запити, мінімум 2 збіги термів)
# ============================================================

def test_fts_short_query_keeps_single_word_match(db, monkeypatch):
    """1-2-слівний запит НЕ фільтрується — єдиний збіг сам є сигналом."""
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db, "Короткий запит")
    _add_chunk(db, tid, 0, "тут згадується бюджет фонду", None)

    res = retrieval.search(db, "бюджет")
    assert len(res["chunks"]) == 1
    assert res["chunks"][0]["transcription_id"] == tid


def test_fts_long_query_filters_single_term_overlap(db, monkeypatch):
    """Запит з 3+ слів: чанк з ОДНИМ спільним словом відкидається, чанк з
    2+ спільними словами лишається — recall не обнуляється, шум прибирається."""
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    weak = _add_tx(db, "Слабкий збіг")
    strong = _add_tx(db, "Сильний збіг")
    # weak: лише слово "бюджет" спільне з запитом
    _add_chunk(db, weak, 0, "бюджет випадковий текст без стосунку", None)
    # strong: усі три слова запиту присутні
    _add_chunk(db, strong, 0, "бюджет проєкту команди обговорили сьогодні", None)

    res = retrieval.search(db, "бюджет проєкту команди")
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert strong in tids
    assert weak not in tids


def test_fts_long_query_soft_fallback_when_filter_empties_results(db, monkeypatch):
    """Якщо cutoff-фільтр з'їдає ВСІ хіти (жоден чанк не має 2+ збігів) —
    відкатуємось на нефільтрований список: recall НЕ обнуляється."""
    monkeypatch.setattr(embeddings, "is_available", lambda: False)
    tid = _add_tx(db, "Єдиний слабкий збіг")
    _add_chunk(db, tid, 0, "бюджет це все що тут є", None)

    res = retrieval.search(db, "бюджет проєкту команди")
    tids = {c["transcription_id"] for c in res["chunks"]}
    assert tid in tids, "фільтр не мусить обнулити ЄДИНИЙ (хай і слабкий) хіт"


# ============================================================
# T6.4: rerank=False (за замовчуванням) НЕ чіпає reranker
# ============================================================

def test_rerank_default_off_does_not_call_reranker(db, mock_embeddings, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("reranker.rerank НЕ мусить викликатись, коли rerank=False")
    monkeypatch.setattr(reranker, "rerank", _boom)

    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz")  # rerank не переданий -> дефолт False
    assert len(res["chunks"]) == 1
    assert "rerank_score" not in res["chunks"][0]


# ============================================================
# T6.4: rerank=True переранжовує пул за скором мока
# ============================================================

def test_rerank_true_reorders_pool_and_tags_score(db, mock_embeddings, monkeypatch):
    """3 чанки з однаковою векторною релевантністю (cosine 1.0) — RRF+recency
    їх не розрізняє (порядок довільний/стабільний), але мок-reranker явно
    віддає перевагу chunk з текстом "c" — і фінальний порядок мусить це
    відобразити."""
    tid = _add_tx(db, "Мітинг")
    q = [1.0, 0, 0, 0]
    _add_chunk(db, tid, 0, "a", q)
    _add_chunk(db, tid, 1, "b", q)
    _add_chunk(db, tid, 2, "c", q)

    def _fake_rerank(query, candidates, text_key="text"):
        order = {"c": 3.0, "b": 2.0, "a": 1.0}
        scored = sorted(candidates, key=lambda c: order.get(c[text_key], 0.0), reverse=True)
        out = []
        for c in scored:
            c2 = dict(c)
            c2["rerank_score"] = order.get(c[text_key], 0.0)
            out.append(c2)
        return out
    monkeypatch.setattr(reranker, "rerank", _fake_rerank)

    res = retrieval.search(db, "qqqzzz", top_k=3, max_per_meeting=0, rerank=True)
    texts = [c["text"] for c in res["chunks"]]
    assert texts == ["c", "b", "a"]
    assert res["chunks"][0]["rerank_score"] == pytest.approx(3.0)


def test_rerank_true_tail_beyond_pool_untouched(db, mock_embeddings, monkeypatch):
    """Пул обмежений rerank_pool_size — кандидати за межами пулу лишаються у
    вихідному RRF+recency порядку (reranker.rerank взагалі не бачить хвіст)."""
    tid = _add_tx(db, "Мітинг")
    q = [1.0, 0, 0, 0]
    for i in range(4):
        _add_chunk(db, tid, i, f"chunk{i}", q)

    seen_texts = []
    def _tracking_rerank(query, candidates, text_key="text"):
        seen_texts.extend(c[text_key] for c in candidates)
        return candidates  # без змін порядку
    monkeypatch.setattr(reranker, "rerank", _tracking_rerank)

    res = retrieval.search(db, "qqqzzz", top_k=4, max_per_meeting=0,
                           rerank=True, rerank_pool_size=2)
    assert len(seen_texts) == 2, "reranker мусить бачити лише пул (2), не всі 4 кандидати"
    assert len(res["chunks"]) == 4  # хвіст все одно повертається (просто не реранжований)


def test_rerank_graceful_degradation_via_real_reranker(db, mock_embeddings, monkeypatch):
    """Наскрізний шлях (search -> reranker.rerank), reranker недоступний —
    результат ідентичний rerank=False (не валить пошук, не губить кандидатів)."""
    monkeypatch.setattr(reranker, "is_available", lambda: False)
    tid = _add_tx(db, "Мітинг")
    _add_chunk(db, tid, 0, "альфа бета", [1.0, 0, 0, 0])

    res = retrieval.search(db, "qqqzzz", rerank=True)
    assert len(res["chunks"]) == 1
    assert "rerank_score" not in res["chunks"][0]


# ============================================================
# Волна 4: диверсифікація — для Telegram джерело це ЧАТ, а не запис
# ============================================================

def _row(source_type, tid, chat_id=None):
    return {"source_type": source_type, "transcription_id": tid, "tg_chat_id": chat_id}


def test_group_key_uses_chat_for_telegram():
    """У TG запис = одне повідомлення, тож кеп «3 на транскрипт» не спрацьовував
    НІКОЛИ: вісім повідомлень з одного чату займали всі вісім слотів, витісняючи
    дзвінки й документи."""
    a = retrieval._group_key(_row("telegram", 100, -1001234567890))
    b = retrieval._group_key(_row("telegram", 101, -1001234567890))
    assert a == b, "два повідомлення одного чату — одне джерело"


def test_group_key_keeps_transcript_for_others():
    a = retrieval._group_key(_row("recording", 5))
    b = retrieval._group_key(_row("recording", 6))
    assert a != b


def test_group_key_falls_back_when_chat_id_missing():
    assert retrieval._group_key(_row("telegram", 7, None)) == ("tx", 7)


def test_diversify_caps_telegram_by_chat():
    ranked = [1, 2, 3, 4, 5]
    groups = {cid: ("tg", -100) for cid in ranked}
    picked = retrieval._diversify(ranked, groups, top_k=5, max_per_meeting=2)
    # кеп лишає 2, решту добираємо переповненням — але порядок показує, що
    # обмеження спрацювало (раніше воно не спрацьовувало взагалі)
    assert picked[:2] == [1, 2]


# ============================================================
# Волна 4.5: джерело — НИТКА, і зшивання нитки при видачі
# ============================================================

def _tg_row(tid, chat_id=-100, thread_id=None):
    return {"source_type": "telegram", "transcription_id": tid,
            "tg_chat_id": chat_id, "tg_thread_id": thread_id}


def test_group_key_prefers_thread_over_chat():
    """У фонд-чаті одночасно йдуть кілька проєктів. Кеп по чату душив би їх
    одне одним, хоча це різні розмови."""
    a = retrieval._group_key(_tg_row(100, thread_id=7))
    b = retrieval._group_key(_tg_row(101, thread_id=8))
    assert a != b, "різні нитки одного чату — різні джерела"


def test_group_key_same_thread_is_one_source():
    a = retrieval._group_key(_tg_row(100, thread_id=7))
    b = retrieval._group_key(_tg_row(101, thread_id=7))
    assert a == b


def test_group_key_falls_back_to_chat_before_threads_exist():
    """Поки нитки не розкладені, поведінка має лишатись як у Волні 4."""
    a = retrieval._group_key(_tg_row(100, thread_id=None))
    b = retrieval._group_key(_tg_row(101, thread_id=None))
    assert a == b == ("tg", -100)


def _seed_thread(path, msgs, thread_id=1, chat_id=-100):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO tg_threads (id, chat_id, label, status) "
                 "VALUES (?, ?, 'Узгодження зустрічі', 'open')", (thread_id, chat_id))
    for i, (msg_id, sender, text) in enumerate(msgs):
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, transcript_text, "
            "tg_chat_id, tg_message_id, tg_date, tg_sender, tg_thread_id) "
            "VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?)",
            (f"[TG] {text[:20]}", text, chat_id, msg_id,
             f"2026-06-0{i + 1}T10:00:00+00:00", sender, thread_id))
    conn.commit()
    conn.close()


def test_thread_context_brings_the_answer_with_the_question(db):
    """Живий провал, заради якого волна й робилась: знахідка — ПИТАННЯ, а
    відповідь наступною реплікою, і спільних слів із запитом у неї немає."""
    _seed_thread(db, [(1, "Юля", "Давайте в четвер о 10:00? Чи всім зручно?"),
                      (2, "Настя", "чт – з 09:00 до 11:00, після 16:45")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 1,
               "text": "Давайте в четвер о 10:00? Чи всім зручно?"}]
    retrieval.attach_thread_context(db, chunks)
    texts = [m["text"] for m in chunks[0]["thread"]["messages"]]
    assert any("09:00 до 11:00" in t for t in texts), "відповідь має приїхати з питанням"


def test_thread_context_marks_which_message_was_found(db):
    _seed_thread(db, [(1, "Юля", "питання"), (2, "Настя", "відповідь")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 2,
               "text": "відповідь"}]
    retrieval.attach_thread_context(db, chunks)
    hits = [m for m in chunks[0]["thread"]["messages"] if m["is_hit"]]
    assert len(hits) == 1 and hits[0]["tg_message_id"] == 2


def test_thread_context_window_is_centred_on_the_hit(db):
    _seed_thread(db, [(i, "A", f"повідомлення {i}") for i in range(1, 10)])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 8,
               "text": "повідомлення 8"}]
    retrieval.attach_thread_context(db, chunks, max_msgs=4)
    ids = [m["tg_message_id"] for m in chunks[0]["thread"]["messages"]]
    assert len(ids) == 4
    assert 8 in ids, "вікно має містити саму знахідку, а не початок нитки"


def test_thread_context_noop_without_threads(db):
    """Записи без нитки лишаються як були — це не помилка, а стан «ще не
    розкладено»."""
    chunks = [{"source_type": "telegram", "tg_thread_id": None, "text": "x"}]
    retrieval.attach_thread_context(db, chunks)
    assert "thread" not in chunks[0]


def test_thread_context_skips_single_message_thread(db):
    """Нитка з одного повідомлення нічого не додає — не роздуваємо промпт."""
    _seed_thread(db, [(1, "Юля", "одне повідомлення")])
    chunks = [{"source_type": "telegram", "tg_thread_id": 1, "tg_message_id": 1,
               "text": "одне повідомлення"}]
    retrieval.attach_thread_context(db, chunks)
    assert "thread" not in chunks[0]
