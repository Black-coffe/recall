"""Гібридний пошук по архіву (Phase 13B + 15A).

Поєднує два сигнали на рівні ЧАНКІВ:
  1. Векторний (семантика) — cosine між embedding запиту і embedding'ами чанків.
  2. Лексичний (ключові слова) — chunks_fts BM25.
Злиття — Reciprocal Rank Fusion (RRF): стабільне, не потребує калібрування шкал.

Після злиття (Phase 15A) застосовуємо два пост-процеси, щоб результат був
КОРИСНИМ, а не лише релевантним:
  - Свіжість (recency): мʼякий буст недавніх мітингів, щоб «що ми вирішили
    востаннє» не тонуло серед однаково-релевантних старих фрагментів. Вага
    помірна — це тай-брейкер, а не домінанта (не ламає «timeless» питання).
  - Диверсифікація (per-meeting cap): обмежуємо к-сть чанків з ОДНОГО мітингу
    у топі, щоб один багатослівний дзвінок не з'їв усі слоти контексту і
    відповідь спиралась на кілька різних джерел.

Повертає ранжовані чанки з провенансом (транскрипт, назва, дата, спікер, таймкод)
— основа і для /api/memory/search, і для RAG-чату "Запитай архів".

Векторний пошук — brute-force numpy (вантажить усі embedding'и в памʼять). Для
поточного масштабу (тисячі чанків) це <100мс. За потреби пізніше — sqlite-vec.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

import numpy as np

from app.core import settings as _settings
from app.db.connection import get_db_connection
from app.services import embeddings, reranker


logger = logging.getLogger(__name__)

_RRF_K = 60

# --- Свіжість (recency) ---
# final = rrf_score * (1 + RECENCY_WEIGHT * recency01), де recency01 ∈ [0,1]
# згасає з піврозпадом RECENCY_HALF_LIFE_DAYS. Вага помірна навмисно: семантика
# (RRF) лишається головним сигналом, свіжість лише розводить рівних.
_RECENCY_WEIGHT = float(os.environ.get("RAG_RECENCY_WEIGHT", "0.25"))
_RECENCY_HALF_LIFE_DAYS = float(os.environ.get("RAG_RECENCY_HALF_LIFE_DAYS", "180"))

# --- Диверсифікація ---
# Скільки чанків з одного мітингу максимум потрапляє у фінальний топ.
_MAX_PER_MEETING = int(os.environ.get("RAG_MAX_PER_MEETING", "3"))

# --- T6.8: м'який FTS relevance-cutoff ---
# _fts_or_query OR-ить КОЖНЕ слово запиту — потрібно для recall (природні
# питання), але для довгих запитів чанк з ОДНИМ випадковим спільним словом
# (напр. "як") усе одно потрапляє у RRF як лексичний хіт. Для запитів від
# _FTS_CUTOFF_MIN_WORDS слів вимагаємо принаймні _FTS_MIN_MATCHES реальних
# збігів термів — рахуємо ДЕШЕВО (лише всередині вже звуженої candidate_k
# множини, не по всій таблиці). Короткі запити (1-2 слова) НЕ фільтруються
# — там єдиний збіг і є сигналом.
_FTS_CUTOFF_MIN_WORDS = int(os.environ.get("RAG_FTS_CUTOFF_MIN_WORDS", "3"))
_FTS_MIN_MATCHES = int(os.environ.get("RAG_FTS_MIN_MATCHES", "2"))

# --- T6.4: локальний cross-encoder rerank (опційно, за замовчуванням OFF) ---
# Скільки топ-кандидатів (за RRF+recency) прогоняти через reranker.rerank().
# 20-30 — баланс: досить, щоб виправити помилки RRF у релевантному хвості,
# але не весь candidate_k (~40+) — той все одно не потрапить у top_k, а
# cross-encoder forward-прохід дорожчий за косинус. Кандидати ЗА межами пулу
# лишаються у вихідному RRF+recency порядку (rerank лише покращує, не псує).
_RERANK_POOL_SIZE = int(os.environ.get("RAG_RERANK_POOL_SIZE", "24"))

# --- Шар коментарів (Волна 2) ---
# Коментар — це не ще один фрагмент розмови, а речення, яке власник написав ПРО
# запис: уточнення, виправлення, акцент. Тому при рівній релевантності він має
# перемагати сирий транскрипт, а не ділити з ним місце.
#
# Множник: final *= (1 + _COMMENT_WEIGHT * kind_weight), де kind_weight ∈ [0,1]
# приходить з `comments.KIND_WEIGHTS` (correction 1.0 … question 0.4). Тобто
# виправлення отримує до +60% до скора, а відкрите питання — до +24%. Вага
# помірна навмисно, з тієї ж причини, що й у recency: це має бути СИЛЬНИЙ
# тай-брейкер, а не глушилка, після якої на будь-яке питання видаються самі
# лише коментарі.
#
# Коментарі окремо шукаються (`comment_chunks`/`comment_chunks_fts`), бо
# лежать в окремому індексі — чому саме так, розписано в міграції v37.
_COMMENT_WEIGHT = float(os.environ.get("RAG_COMMENT_WEIGHT", "0.6"))

#: Скільки кандидатів брати з індексу коментарів. Їх на порядки менше, ніж
#: чанків транскриптів, тож повний candidate_k тут — марна робота; але й надто
#: мало брати не можна, інакше буст нема на чому застосувати.
_COMMENT_CANDIDATE_CAP = int(os.environ.get("RAG_COMMENT_CANDIDATE_CAP", "20"))

#: Яку ЧАСТКУ топу коментарі можуть зайняти щонайбільше.
#:
#: Стеля зʼявилась не з обережності, а за замірами (Волна 2.5,
#: `evals/comments_eval.py`). Кожен коментар — власна група диверсифікації, щоб
#: кеп «не більше N з одного мітингу» його не душив. Наслідок виявився таким:
#: на 30 посаджених коментарях видача віддала їм 115 слотів зі 120 при k=8, а
#: на восьми питаннях із пʼятнадцяти ВЕСЬ топ-12 складався з самих коментарів.
#:
#: Це і є справжня поломка, а не «сильний буст»: модель отримувала уточнення
#: без матеріалу, який вони уточнюють. Виправлення «насправді сума 12k» без
#: фрагмента, де названо 20k, не має про що виправляти — і відповідь виходить
#: гіршою, ніж була б узагалі без шару.
#:
#: Третина — компроміс: пріоритет лишається (коментарі виграють свої слоти й
#: стоять першими), але дві третини топу гарантовано тримає архів. Мінімум
#: один слот завжди: єдине виправлення мусить доїхати навіть при k=2.
_COMMENT_MAX_SHARE = float(os.environ.get("RAG_COMMENT_MAX_SHARE", "0.34"))


def _cap_comments(ranked_ids: list[int], top_k: int,
                  share: float) -> list[int]:
    """Обмежити частку коментарів у топі, зберігши їхній порядок.

    Зайві коментарі не викидаються, а зсуваються В КІНЕЦЬ: якщо архівного
    матеріалу під запит просто немає, краще віддати коментарі, ніж порожнечу.
    """
    if share >= 1.0:
        return ranked_ids
    allowed = max(1, int(top_k * share))
    kept, overflow, seen = [], [], 0
    for cid in ranked_ids:
        if _is_comment_key(cid):
            seen += 1
            (kept if seen <= allowed else overflow).append(cid)
        else:
            kept.append(cid)
    return kept + overflow


def _recency_factor(meeting_date: Optional[str], now: Optional[datetime] = None) -> float:
    """recency01 ∈ [0,1]: 1.0 = сьогодні, ~0.5 через піврозпад, →0 для давніх.
    Невідома/некоректна дата → 0.0 (нейтрально, без бусту)."""
    if not meeting_date:
        return 0.0
    s = str(meeting_date)[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            d = datetime.strptime(s, fmt)
            break
        except ValueError:
            d = None
    if d is None:
        return 0.0
    now = now or datetime.now()
    age_days = (now - d).days
    if age_days <= 0:
        return 1.0
    if _RECENCY_HALF_LIFE_DAYS <= 0:
        return 0.0
    return float(0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS))


def _fts_words(query: str) -> list[str]:
    """Токени запиту, що йдуть у FTS OR-запит (>1 символу)."""
    words = re.findall(r"\w+", query, re.UNICODE)
    return [w for w in words if len(w) > 1]


def _fts_or_query(query: str) -> str:
    """OR-запит для FTS5: кожне слово в лапках через OR. Для гібриду потрібна
    РЕКОЛЛ-семантика (vector дає точність), тож OR краще за AND-of-all-words
    зі стандартного sanitize_fts_query (надто строгий для природних питань)."""
    words = _fts_words(query)
    if not words:
        return ""
    return " OR ".join(f'"{w}"' for w in words)


def _term_match_counts(conn, words: list[str], rowids: list[int]) -> dict[int, int]:
    """Для кожного rowid у candidate-множині — скільки з OR-термів РЕАЛЬНО
    збіглося (а не лише «потрапив у топ-N по bm25 через один рідкісний
    термін»). Дешево: candidate-множина вже мала (≤ candidate_k), запит —
    по одному терму, обмежений тим самим rowid-набором (індекс FTS5)."""
    counts: dict[int, int] = {rid: 0 for rid in rowids}
    if not rowids:
        return counts
    placeholders = ",".join("?" * len(rowids))
    for w in words:
        rows = conn.execute(
            f"SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            f"AND rowid IN ({placeholders})",
            [f'"{w}"'] + rowids,
        ).fetchall()
        for r in rows:
            counts[r["rowid"]] = counts.get(r["rowid"], 0) + 1
    return counts


def _scope_sql(scope_tids: Optional[list[int]]) -> tuple[str, list]:
    """Фрагмент WHERE, що звужує пошук до заданого списку записів.

    Другий шар скоупу (Трек 2): категорія 1:1 не рятує, бо «Робота» —
    це ~більша частина корпусу, а всередині живуть різні проєкти. Сам список рахує
    `app.services.scope.scope_filter_ids` (граф сутностей ∪ текстова згадка) —
    retrieval навмисно не знає про граф, він лише фільтрує за id.
    """
    if not scope_tids:
        return "", []
    ph = ",".join("?" * len(scope_tids))
    return f" AND ch.transcription_id IN ({ph})", list(scope_tids)


def _vector_search(db_path: str, query: str, candidate_k: int,
                   category_id: Optional[int] = None,
                   scope_tids: Optional[list[int]] = None) -> list[tuple[int, float]]:
    """Top-N чанків за cosine. Returns [(chunk_id, score)] спадно."""
    if not embeddings.is_available():
        return []
    qvec = embeddings.embed_query(query)
    if not np.any(qvec):
        return []
    # T4.6: soft-deleted транскрипти НЕ мають спливати у RAG/пошуку — фільтр
    # завжди активний, незалежно від category_id (не лише в category-гілках).
    # production-rag-wave-b-04: поруч — епоха ембедингів (embedding_model +
    # embedding_version). Vector-хіти рахуємо ТІЛЬКИ по чанках поточної пари —
    # інакше зміна EMBED_MODEL/EMBED_VERSION мовчки мішала б косинуси різних
    # моделей/нарізок (BLOB того самого розміру не означає ту саму модель).
    sql = ("SELECT ch.id, ch.embedding FROM chunks ch WHERE ch.embedding IS NOT NULL "
           "AND ch.transcription_id IN (SELECT id FROM transcriptions "
           "WHERE deleted_at IS NULL AND duplicate_of IS NULL "
           "AND embedding_model = ? AND embedding_version = ?)")
    params: list = [embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]
    if category_id == 'none':
        sql += " AND ch.transcription_id IN (SELECT id FROM transcriptions WHERE category_id IS NULL)"
    elif category_id is not None:
        sql += (" AND ch.transcription_id IN "
                "(SELECT id FROM transcriptions WHERE category_id = ?)")
        params.append(category_id)
    scope_sql, scope_params = _scope_sql(scope_tids)
    sql += scope_sql
    params += scope_params
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    ids = np.empty(len(rows), dtype=np.int64)
    mat = np.empty((len(rows), embeddings.EMBED_DIM), dtype=np.float32)
    n = 0
    for r in rows:
        vec = embeddings.blob_to_vec(r["embedding"])
        if vec.shape[0] != embeddings.EMBED_DIM:
            # Фільтр по (embedding_model, embedding_version) вище вже мав
            # звузити рядки до поточної пари — розбіжність виміру тут означає
            # пошкоджений BLOB чи розсинхрон версії/dim, а не звичайний шлях
            # старіння. Раніше пропускалось мовчки — жодного сліду в логу,
            # якщо архів раптом набрав несумісні вектори.
            logger.error(
                "[retrieval] chunk id=%s: розмір вектора %d != EMBED_DIM=%d "
                "(модель/версія %s) — пропускаємо", r["id"], vec.shape[0],
                embeddings.EMBED_DIM, embeddings.EMBED_MODEL)
            continue
        ids[n] = r["id"]
        mat[n] = vec
        n += 1
    if n == 0:
        return []
    sims = mat[:n] @ qvec  # обидва нормалізовані → dot = cosine
    top = np.argsort(-sims)[:candidate_k]
    return [(int(ids[i]), float(sims[i])) for i in top]


def _fts_search(db_path: str, query: str, candidate_k: int,
                category_id: Optional[int] = None,
                scope_tids: Optional[list[int]] = None) -> list[tuple[int, float]]:
    """Top-N чанків за BM25. Returns [(chunk_id, -rank)] кращі першими."""
    words = _fts_words(query)
    fts_q = " OR ".join(f'"{w}"' for w in words) if words else ""
    if not fts_q:
        return []
    # T4.6: як і у _vector_search — soft-deleted виключаємо завжди, не лише
    # коли задано category_id.
    sql = ("SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? "
           "AND rowid IN (SELECT id FROM chunks WHERE transcription_id IN "
           "(SELECT id FROM transcriptions WHERE deleted_at IS NULL "
           "AND duplicate_of IS NULL))")
    params: list = [fts_q]
    if category_id == 'none':
        sql += (" AND rowid IN (SELECT id FROM chunks WHERE transcription_id IN "
                "(SELECT id FROM transcriptions WHERE category_id IS NULL))")
    elif category_id is not None:
        sql += (" AND rowid IN (SELECT id FROM chunks WHERE transcription_id IN "
                "(SELECT id FROM transcriptions WHERE category_id = ?))")
        params.append(category_id)
    if scope_tids:
        ph_s = ",".join("?" * len(scope_tids))
        sql += (" AND rowid IN (SELECT id FROM chunks WHERE transcription_id IN "
                f"({ph_s}))")
        params += list(scope_tids)
    sql += " ORDER BY rank LIMIT ?"
    params.append(candidate_k)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        # rank: менше = краще (bm25). Повертаємо -rank як score (більше = краще).
        hits = [(int(r["rowid"]), -float(r["rank"])) for r in rows]

        # T6.8: м'який relevance-cutoff для довгих запитів (>= _FTS_CUTOFF_MIN_WORDS
        # слів) — вимагаємо >= _FTS_MIN_MATCHES реальних збігів термів, інакше чанк
        # з ОДНИМ випадковим спільним словом засмічує RRF. Короткі запити (1-2
        # слова) НЕ фільтруються — recall там навмисно широкий (сам OR і є сигнал).
        # Якщо фільтр з’їдає ВСІ хіти (напр. рідкісні терміни рознесені по різних
        # чанках) — відкатуємось на нефільтрований список: НЕ обнуляти recall.
        if len(words) >= _FTS_CUTOFF_MIN_WORDS and hits:
            rowids = [cid for cid, _ in hits]
            counts = _term_match_counts(conn, words, rowids)
            filtered = [(cid, score) for cid, score in hits
                       if counts.get(cid, 0) >= _FTS_MIN_MATCHES]
            if filtered:
                hits = filtered
    return hits


# ============================================================
# Коментарі (Волна 2)
# ============================================================

# Ідентифікатор чанка коментаря у спільній видачі — ВІДʼЄМНИЙ id рядка
# `comment_chunks`. Простір id двох таблиць перетинається (в обох є рядок №5),
# а споживачі (copilot: `valid_ids`, `chunk_meta`, `shown_chunks`) працюють з
# `chunk_id` як з непрозорою ідентичністю в межах одного списку — тож пара
# однакових id зробила б пруф інсайту двозначним. Знак розводить простори без
# жодної правки нижче за течією: жоден споживач не ходить у БД за `chunk_id`.
def _cc_key(comment_chunk_id: int) -> int:
    return -int(comment_chunk_id)


def _is_comment_key(key: int) -> bool:
    return key < 0


def _comment_scope_sql(category_id, scope_tids: Optional[list[int]]) -> tuple[str, list]:
    """Звуження індексу коментарів під той самий скоуп, що й транскрипти.

    Коментар прикріплений до КАРТКИ, а скоуп заданий списком транскриптів. Тому
    під активним скоупом (напрямок або проєкт) лишаємо лише коментарі, ціль яких
    — транскрипт із цього скоупу. Коментарі на інших типах карток (файл
    Медіатеки, задача, сутність) під скоупом НЕ показуємо: довести їх належність
    до зрізу нічим, а тихо протягнути повз фільтр — гірше, ніж не показати.
    Без скоупу беруться всі.
    """
    if category_id is None and not scope_tids:
        return "", []
    sub = ("SELECT id FROM transcriptions WHERE deleted_at IS NULL")
    params: list = []
    if category_id == 'none':
        sub += " AND category_id IS NULL"
    elif category_id is not None:
        sub += " AND category_id = ?"
        params.append(category_id)
    if scope_tids:
        sub += f" AND id IN ({','.join('?' * len(scope_tids))})"
        params += list(scope_tids)
    return (f" AND c.target_type = 'transcription' AND c.target_id IN ({sub})", params)


def _comment_base_where(category_id, scope_tids) -> tuple[str, list]:
    """WHERE-хвіст, спільний для векторного і лексичного пошуку коментарів."""
    sql = " AND c.deleted_at IS NULL"
    scope_sql, params = _comment_scope_sql(category_id, scope_tids)
    return sql + scope_sql, params


def _comment_vector_search(db_path: str, query: str, candidate_k: int,
                           category_id=None,
                           scope_tids: Optional[list[int]] = None
                           ) -> list[tuple[int, float]]:
    if not embeddings.is_available():
        return []
    qvec = embeddings.embed_query(query)
    if not np.any(qvec):
        return []
    where, params = _comment_base_where(category_id, scope_tids)
    sql = ("SELECT cc.id, cc.embedding FROM comment_chunks cc "
           "JOIN comments c ON c.id = cc.comment_id "
           "WHERE cc.embedding IS NOT NULL" + where)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    ids, vecs = [], []
    for r in rows:
        vec = embeddings.blob_to_vec(r["embedding"])
        if vec.shape[0] != embeddings.EMBED_DIM:
            continue  # модель змінилась — несумісні пропускаємо, як і в чанках
        ids.append(int(r["id"]))
        vecs.append(vec)
    if not ids:
        return []
    sims = np.stack(vecs) @ qvec
    top = np.argsort(-sims)[:candidate_k]
    return [(_cc_key(ids[i]), float(sims[i])) for i in top]


def _comment_fts_search(db_path: str, query: str, candidate_k: int,
                        category_id=None,
                        scope_tids: Optional[list[int]] = None
                        ) -> list[tuple[int, float]]:
    words = _fts_words(query)
    if not words:
        return []
    fts_q = " OR ".join(f'"{w}"' for w in words)
    where, params = _comment_base_where(category_id, scope_tids)
    sql = ("SELECT f.rowid AS rid, f.rank AS rank FROM comment_chunks_fts f "
           "JOIN comment_chunks cc ON cc.id = f.rowid "
           "JOIN comments c ON c.id = cc.comment_id "
           "WHERE comment_chunks_fts MATCH ?" + where + " ORDER BY f.rank LIMIT ?")
    with get_db_connection(db_path) as conn:
        rows = conn.execute(sql, [fts_q] + params + [candidate_k]).fetchall()
    # М'який cutoff, що діє на транскриптах (_FTS_MIN_MATCHES), тут НЕ
    # застосовуємо: коментар — це одне-два речення, і вимога «два терми з
    # запиту в одному чанку» відсікала б рівно ті короткі виправлення, заради
    # яких шар і зроблено («сума 12k, не 20k» проти питання на десять слів).
    return [(_cc_key(int(r["rid"])), -float(r["rank"])) for r in rows]


def _hydrate_comments(db_path: str, keys: list[int]) -> dict[int, dict]:
    """Провенанс чанків коментарів: сам коментар, його тип/вага і назва картки,
    до якої він прикріплений."""
    if not keys:
        return {}
    ids = [-k for k in keys]
    ph = ",".join("?" * len(ids))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT cc.id AS cc_id, cc.text, cc.chunk_index, "
            f"c.id AS comment_id, c.target_type, c.target_id, c.kind, c.weight, "
            f"c.pinned, c.anchor_time, c.author, c.source AS comment_source, "
            f"substr(c.created_at, 1, 10) AS comment_date, "
            f"t.source_name AS tx_name, t.source_type AS tx_source_type, "
            f"COALESCE(t.meeting_date, substr(t.created_at, 1, 10)) AS tx_date, "
            f"a.title AS audio_title "
            f"FROM comment_chunks cc "
            f"JOIN comments c ON c.id = cc.comment_id "
            f"LEFT JOIN transcriptions t ON c.target_type = 'transcription' AND t.id = c.target_id "
            f"LEFT JOIN audio_downloads a ON c.target_type = 'audio_download' AND a.id = c.target_id "
            f"WHERE cc.id IN ({ph}) AND c.deleted_at IS NULL",
            ids).fetchall()
    return {_cc_key(r["cc_id"]): r for r in rows}


def _target_label(row) -> str:
    """Людська назва картки, до якої написано коментар — потрібна і для
    відповіді RAG («до чого це уточнення»), і для кліку у видачі."""
    if row["target_type"] == "transcription":
        return row["tx_name"] or f"Запис #{row['target_id']}"
    if row["target_type"] == "audio_download":
        return row["audio_title"] or f"Аудіо #{row['target_id']}"
    return f"{row['target_type']} #{row['target_id']}"


def _comment_entry(key: int, row, meta: dict) -> dict:
    """Елемент видачі для чанка коментаря — навмисно тієї ж форми, що й для
    чанка транскрипту (`chunk_id`, `text`, `score`, `meeting_date`), щоб усі
    наявні споживачі (copilot, MCP, фронт) працювали без правок; відрізняє їх
    поле `source_type='comment'`."""
    from app.services import comments as comments_svc
    return {
        "chunk_id": key,                      # відʼємний — див. _cc_key
        "comment_id": row["comment_id"],
        "comment_chunk_id": -key,
        "source_type": "comment",
        "comment_kind": row["kind"],
        "comment_weight": comments_svc.kind_weight(row["kind"], row["weight"]),
        "pinned": bool(row["pinned"]),
        "author": row["author"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "target_label": _target_label(row),
        "transcription_id": row["target_id"] if row["target_type"] == "transcription" else None,
        "source_name": _target_label(row),
        "doc_type": None,
        "meeting_date": row["comment_date"],
        "target_date": row["tx_date"],
        "speaker": row["author"],
        "start_time": row["anchor_time"],
        "end_time": None,
        "page": None,
        "section": None,
        "text": row["text"],
        "score": round(meta["final"], 5),
        "matched_by": sorted(set(meta["modes"])),
    }


def _why_top(cid: int, meta: dict, rerank_moved: set[int],
            rec_w: float, com_w: float) -> str:
    """Яка стадія РЕАЛЬНО зрушила позицію результату (Історія 09, план D1).

    Було: фіксований ланцюжок пріоритетів, що називав стадію, яка МОГЛА
    спрацювати (recency повертався щойно `_recency_factor` дав щось додатне,
    навіть при `rec_w=0`, коли множник = 1.0 і нічого не змінив). Тепер —
    порівнюємо ФАКТИЧНІ внески: rerank важить найбільше, коли справді
    переставив цього кандидата (позиційна зміна, не множник — порівнювати її
    магнітудою з recency/comment нема сенсу); інакше — множник з більшим
    фактичним внеском (`rec_w * recency` проти `comment_delta`); якщо жоден
    не зрушив нічого — `rrf`."""
    if cid in rerank_moved:
        return "rerank"
    rec_delta = rec_w * meta.get("recency", 0.0)
    com_delta = meta.get("comment_delta", 0.0)
    if rec_delta <= 0.0 and com_delta <= 0.0:
        return "rrf"
    return "comment_boost" if com_delta > rec_delta else "recency"


# Обовʼязкові ключі `why` (контракт C2) — ОДНЕ джерело істини. Обидва
# виробники (`_build_why` нижче і маркер приєднаного коментаря в
# `rag.order_citables`, через `build_placeholder_why`) беруть перелік
# звідси, а не переписують його літералом — рівно той дефект, який
# ремонтували двічі (NEW-2, NEW-7) і полагодили в корені історією 13
# (план D3). Порядок значень при використанні `dict(zip(...))` мусить
# лишатись src/rrf/rec/by/top.
WHY_REQUIRED_KEYS: tuple[str, ...] = ("src", "rrf", "rec", "by", "top")


def build_placeholder_why(source_type: str, top: str, note: Optional[str] = None) -> dict:
    """`why` для елементів, що не пройшли через `search()` (напр. підшиті
    коментарі в `rag.order_citables`) — нульові внески, той самий
    обовʼязковий набір ключів `WHY_REQUIRED_KEYS`. Додатковий `note`
    (дозволений контрактом C2) пояснює словами, чому внески нульові."""
    why: dict = dict(zip(WHY_REQUIRED_KEYS, (source_type, 0.0, 0.0, [], top)))
    if note:
        why["note"] = note
    return why


def _build_why(cid: int, meta: dict, source_type: str, matched_by: list[str],
               rerank_scores: dict[int, float], rerank_moved: set[int],
               stage_maps: dict[str, dict[int, tuple[int, float]]],
               explain: bool, rec_w: float, com_w: float,
               capped: dict[str, bool],
               rewrite_variants: Optional[list[str]] = None) -> dict:
    """Контракт C2 (`docs/specs/grep-explainability/plan.md`): компактний `why`
    на кожному результаті, `stages` — лише коли `explain=True`. Не змінює
    ранжування — лише читає вже пораховані `meta`/`rerank_scores`."""
    why: dict = dict(zip(WHY_REQUIRED_KEYS, (
        source_type,
        round(meta["score"], 5),
        round(meta.get("recency", 0.0), 5),
        matched_by,
        _why_top(cid, meta, rerank_moved, rec_w, com_w),
    )))
    if cid in rerank_scores:
        why["rr"] = round(rerank_scores[cid], 5)
    if explain:
        stages: dict = {}
        for label, smap in stage_maps.items():
            hit = smap.get(cid)
            if hit is None:
                continue
            pos, raw = hit
            field = "sim" if label in ("vector", "comment_vector") else "bm25"
            stages[label] = {"pos": pos, field: raw}
        why["stages"] = stages
        why["weights"] = {"recency": rec_w, "comment": com_w}
        why["final_raw"] = meta["final"]
        # Прапорець на ВЕСЬ пошук (однаковий на кожному результаті), не на
        # цього кандидата — знахідка 12/09: назва мусить це відбивати, щоб
        # "capped" не читалось як "цей результат зачепило".
        why["search_capped"] = dict(capped)
        # production-rag-wave-b-07: варіанти переписаного запиту, що дали
        # додаткові підзапити цього виклику `search()` (порожньо, коли
        # rewrite вимкнено). Той самий прапорець на ВЕСЬ пошук, не per-кандидат.
        why["rewrites"] = list(rewrite_variants or [])
    return why


def _rrf(result_lists: list[list[tuple[int, float]]],
         labels: Optional[list[str]] = None) -> dict[int, dict]:
    """Reciprocal Rank Fusion. Returns {chunk_id: {"score", "modes"}}.

    `labels` іменує списки у `modes` (провенанс «чим знайдено»). Без нього
    поведінка як була: перші два — vector/fts.
    """
    fused: dict[int, dict] = {}
    for mode_idx, results in enumerate(result_lists):
        if labels and mode_idx < len(labels):
            mode = labels[mode_idx]
        else:
            mode = ("vector", "fts")[mode_idx] if mode_idx < 2 else f"m{mode_idx}"
        for position, (cid, _score) in enumerate(results):
            entry = fused.setdefault(cid, {"score": 0.0, "modes": []})
            entry["score"] += 1.0 / (_RRF_K + position + 1)
            entry["modes"].append(mode)
    return fused


def _group_key(row) -> tuple:
    """Що вважати одним джерелом при диверсифікації видачі.

    Для Telegram запис = одне повідомлення, тож кеп max_per_meeting по
    транскрипту не обмежує нічого. Джерело для TG:

    - НИТКА, якщо вона є (Волна 4.5) — це найточніший рівень: у фонд-чаті
      одночасно йдуть Acmecorp, Nova Dance і юридичні питання, і кеп по чату
      душив би їх одне одним, хоча це різні розмови;
    - інакше ЧАТ — як було у Волні 4, поки нитки не розкладені.

    Для решти джерел — транскрипт, як і було."""
    if row["source_type"] == "telegram":
        thread_id = row["tg_thread_id"] if "tg_thread_id" in row.keys() else None
        if thread_id is not None:
            return ("tgt", thread_id)
        if row["tg_chat_id"] is not None:
            return ("tg", row["tg_chat_id"])
    return ("tx", row["transcription_id"])


def _diversify(ranked_ids: list[int], tid_of: dict[int, object],
               top_k: int, max_per_meeting: int) -> list[int]:
    """Відібрати top_k чанків з обмеженням max_per_meeting на одне ДЖЕРЕЛО.

    Джерело — не завжди транскрипт: для Telegram запис = одне повідомлення, тож
    кеп «3 на транскрипт» там не спрацьовував НІКОЛИ, і вісім повідомлень з
    одного чату спокійно займали всі вісім слотів, витісняючи дзвінки й
    документи. Для TG джерелом рахується ЧАТ (див. _group_key).

    Недобір (якщо кеп лишив порожні слоти) заповнюємо рештою у порядку рангу —
    краще трохи дублів з одного мітингу, ніж віддати менше за top_k."""
    if max_per_meeting <= 0:
        return ranked_ids[:top_k]
    selected: list[int] = []
    overflow: list[int] = []
    counts: dict[int, int] = {}
    for cid in ranked_ids:
        tid = tid_of.get(cid)
        if len(selected) < top_k and counts.get(tid, 0) < max_per_meeting:
            selected.append(cid)
            counts[tid] = counts.get(tid, 0) + 1
        else:
            overflow.append(cid)
    i = 0
    while len(selected) < top_k and i < len(overflow):
        selected.append(overflow[i])
        i += 1
    return selected


def _apply_rerank(query: str, ranked_ids: list[int], by_id: dict,
                  pool_size: int) -> tuple[list[int], dict[int, float]]:
    """T6.4: переранжувати ТОП-пул (pool_size) кандидатів локальним
    cross-encoder'ом (reranker.rerank) за реальною семантичною релевантністю
    до запиту. Хвіст ЗА межами пулу лишається у вихідному RRF+recency
    порядку — приєднується після переранжованого пулу без змін.

    Порядок відносно евристик: rerank застосовується ПІСЛЯ RRF+recency (вони
    вже звузили і впорядкували кандидатів), АЛЕ ПЕРЕД diversity cap — тобто
    rerank вирішує ЯКІ чанки семантично найрелевантніші (включно з порядком
    усередині одного мітингу), а cap далі лише не дає одному мітингу
    зайняти увесь топ. Це зберігає diversity-поведінку незмінною (той самий
    _diversify виклик), одночасно роблячи вхідний порядок точнішим.

    Graceful degradation прозоро успадковується від reranker.rerank(): якщо
    модель недоступна/впала — пул повертається у вихідному порядку, і
    результат ідентичний виклику без rerank.

    Returns (новий ranked_ids, {chunk_id: rerank_score} лише для пулу).
    """
    if len(ranked_ids) <= 1 or pool_size <= 0:
        return ranked_ids, {}
    pool_ids = ranked_ids[:pool_size]
    tail_ids = ranked_ids[pool_size:]
    items = [{"id": cid, "text": (by_id[cid]["text"] or "")} for cid in pool_ids]
    t0 = time.time()
    reranked = reranker.rerank(query, items, text_key="text")
    logger.debug("[retrieval] rerank pool=%d за %.3fs", len(items), time.time() - t0)
    new_pool_ids = [it["id"] for it in reranked]
    scores = {it["id"]: it["rerank_score"] for it in reranked if "rerank_score" in it}
    return new_pool_ids + tail_ids, scores


def search(db_path: str, query: str, top_k: int = 8,
           candidate_k: Optional[int] = None,
           category_id: Optional[int] = None,
           recency_weight: Optional[float] = None,
           max_per_meeting: Optional[int] = None,
           rerank: bool = False,
           rerank_pool_size: Optional[int] = None,
           scope_tids: Optional[list[int]] = None,
           include_comments: bool = True,
           comment_weight: Optional[float] = None,
           comment_share: Optional[float] = None,
           explain: bool = False,
           rewrite: Optional[bool] = None) -> dict:
    """Гібридний пошук. category_id — обмежити одним напрямком (None = усі).
    scope_tids — звузити до конкретних записів (другий шар скоупу поверх
    категорії); список рахує `app.services.scope.scope_filter_ids`.
    recency_weight/max_per_meeting — override дефолтів (для тестів/тюнінгу).
    rerank — опційний другий етап переранжирування локальним cross-encoder'ом
    (T6.4), за замовчуванням OFF; вмикається точково лише для RAG-чату
    (див. app/services/rag.py) — не чіпає copilot/categorize/MCP-виклики.
    include_comments — домішувати коментарі власника (Волна 2) з бустом за
    типом; comment_weight — override дефолту (для тестів/тюнінгу);
    comment_share — стеля на частку коментарів у топі (Волна 2.5).
    explain — Історія 02 (`docs/specs/grep-explainability`): False (дефолт) —
    компактний `why` на кожному результаті, True — додає `why["stages"]` з
    позицією і сирим скором на кожній стадії, де кандидат зустрівся. Не
    впливає на ранжування чи порядок видачі.
    rewrite — production-rag-wave-b-07: `None` (дефолт) читає гарячий env
    `RAG_QUERY_REWRITE`; `False`/вимкнений env — `query_rewrite.rewrite_query`
    НЕ викликається взагалі, видача побайтово як без прапорця. `True` — 1-3
    альтернативних формулювання запиту (Claude) дають ДОДАТКОВІ vector+FTS
    підзапити в той самий список перед RRF (`_rrf` їх не розрізняє від
    оригіналу — не чіпає RRF/recency/cap/rerank/комент-шар).
    Returns {"query", "chunks": [...], "vector_available": bool}."""
    query = (query or "").strip()
    if not query:
        return {"query": query, "chunks": [], "vector_available": embeddings.is_available()}

    rec_w = _RECENCY_WEIGHT if recency_weight is None else recency_weight
    cap = _MAX_PER_MEETING if max_per_meeting is None else max_per_meeting
    com_w = _COMMENT_WEIGHT if comment_weight is None else comment_weight
    comment_share = (_COMMENT_MAX_SHARE if comment_share is None else comment_share)
    if rewrite is None:
        rewrite = _settings.env_bool("RAG_QUERY_REWRITE")

    candidate_k = candidate_k or max(top_k * 5, 40)
    vec_hits = _vector_search(db_path, query, candidate_k, category_id, scope_tids)
    fts_hits = _fts_search(db_path, query, candidate_k, category_id, scope_tids)

    lists = [vec_hits, fts_hits]
    labels = ["vector", "fts"]

    # production-rag-wave-b-07: варіанти переписаного запиту — ДОДАТКОВІ
    # vector+FTS підзапити в той самий список перед RRF, з тими самими
    # labels "vector"/"fts" (RRF і matched_by не розрізняють, ЧИЙ це
    # формулювання — оригінал чи варіант). rewrite_query() best-effort:
    # порожній список при вимкненому прапорці, збою API чи невалідному JSON.
    rewrite_variants: list[str] = []
    if rewrite:
        from app.services import query_rewrite
        rewrite_variants = query_rewrite.rewrite_query(query)
        for variant in rewrite_variants:
            lists.append(_vector_search(db_path, variant, candidate_k, category_id, scope_tids))
            labels.append("vector")
            lists.append(_fts_search(db_path, variant, candidate_k, category_id, scope_tids))
            labels.append("fts")

    if include_comments:
        # Індекс коментарів може бути відсутній на старій БД (міграція v37 ще
        # не застосована) — тоді просто працюємо як раніше. Валити пошук через
        # відсутню таблицю не можна: це головний шлях усього продукту.
        ck = min(candidate_k, _COMMENT_CANDIDATE_CAP)
        try:
            comment_lists = [
                _comment_vector_search(db_path, query, ck, category_id, scope_tids),
                _comment_fts_search(db_path, query, ck, category_id, scope_tids),
            ]
        except sqlite3.OperationalError as exc:
            # Стара БД без міграції v37 (індекс коментарів) — відкидаємо ЛИШЕ
            # комент-списки, а не позиційний зріз [:2]: з увімкненим `rewrite`
            # перед ними вже стоять додаткові vector/fts підзапити варіантів
            # (production-rag-wave-b-07), і зріз [:2] мовчки викидав би їх усі.
            logger.debug("[retrieval] індекс коментарів недоступний: %s", exc)
        else:
            lists += comment_lists
            labels += ["comment_vector", "comment_fts"]

    # explain=True: позиція+сирий скор кожного кандидата в кожному вхідному
    # списку (до RRF) — саме те, чого рангу самого по собі бракує (C2).
    # Мітки НЕ перейменовуються для варіантів переписаного запиту (History 07,
    # контракт S2 C2: `by` лишається vector/fts) — тож label може повторитись
    # (оригінал + варіант(и), кожен зі своїм списком vector/fts). Тому це
    # ЗЛИТТЯ по мітці, а не перезапис останнім списком: кандидат, знайдений і
    # оригіналом, і варіантом, лишає запис із найкращою (найменшою) позицією
    # саме того списку.
    stage_maps: dict[str, dict[int, tuple[int, float]]] = {}
    for lbl, lst in zip(labels, lists):
        smap = stage_maps.setdefault(lbl, {})
        for pos, (cid, raw) in enumerate(lst):
            cur = smap.get(cid)
            if cur is None or pos < cur[0]:
                smap[cid] = (pos, raw)

    fused = _rrf(lists, labels)
    if not fused:
        return {"query": query, "chunks": [], "vector_available": embeddings.is_available()}

    # Гідратуємо провенанс УСІХ кандидатів (≤ ~2*candidate_k рядків) — потрібні
    # transcription_id (диверсифікація) і meeting_date (свіжість) ДО фінального зрізу.
    comment_keys = [k for k in fused if _is_comment_key(k)]
    comment_rows = _hydrate_comments(db_path, comment_keys)
    candidate_ids = [k for k in fused if not _is_comment_key(k)]
    rows = []
    if candidate_ids:
        placeholders = ",".join("?" * len(candidate_ids))
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT ch.id, ch.transcription_id, ch.chunk_index, ch.start_time, "
                f"ch.end_time, ch.speaker, ch.text, ch.page, ch.section, "
                f"t.source_name, t.source_type, t.doc_type, "
                f"t.tg_chat_id, t.tg_chat_title, t.tg_message_id, t.tg_link, t.tg_reply_to, "
                f"t.tg_thread_id, "
                f"COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date "
                f"FROM chunks ch JOIN transcriptions t ON t.id = ch.transcription_id "
                f"WHERE ch.id IN ({placeholders}) AND t.deleted_at IS NULL "
                f"AND t.duplicate_of IS NULL",
                candidate_ids,
            ).fetchall()
    by_id = {r["id"]: r for r in rows}

    # Свіжість: домножуємо RRF-скор на мʼякий recency-буст.
    # Коментарі отримують ДОДАТКОВИЙ множник за типом (correction > note >
    # question) — саме він і робить уточнення власника вагомішим за сиру
    # стенограму при однаковій релевантності.
    now = datetime.now()
    for cid, meta in fused.items():
        if _is_comment_key(cid):
            cr = comment_rows.get(cid)
            if cr is None:
                meta["recency"] = 0.0
                meta["final"] = 0.0
                continue
            from app.services import comments as comments_svc
            kw = comments_svc.kind_weight(cr["kind"], cr["weight"])
            rec = _recency_factor(cr["comment_date"], now)
            meta["recency"] = rec
            # Фактичний внесок множника коментаря — потрібен _why_top, щоб
            # порівнювати РЕАЛЬНІ внески, а не саму наявність ключа коментаря.
            meta["comment_delta"] = com_w * kw
            meta["final"] = meta["score"] * (1.0 + rec_w * rec) * (1.0 + com_w * kw)
            continue
        r = by_id.get(cid)
        rec = _recency_factor(r["meeting_date"], now) if r else 0.0
        meta["recency"] = rec
        meta["final"] = meta["score"] * (1.0 + rec_w * rec)

    # Ранжуємо за фінальним скором (відкидаємо чанки без провенансу), потім кеп.
    def _hydrated(k: int) -> bool:
        return (k in comment_rows) if _is_comment_key(k) else (k in by_id)

    ranked_ids = [cid for cid, _ in sorted(
        ((cid, m) for cid, m in fused.items() if _hydrated(cid)),
        key=lambda kv: kv[1]["final"], reverse=True)]

    rerank_scores: dict[int, float] = {}
    rerank_moved: set[int] = set()
    if rerank:
        pool = _RERANK_POOL_SIZE if rerank_pool_size is None else rerank_pool_size
        text_of = {k: (comment_rows[k] if _is_comment_key(k) else by_id[k])
                   for k in ranked_ids}
        pre_pool_ids = ranked_ids[:pool]
        ranked_ids, rerank_scores = _apply_rerank(query, ranked_ids, text_of, pool)
        post_pool_ids = ranked_ids[:pool]
        # Хто зі стадії rerank справді змінив позицію — не «хто в пулі»
        # (_why_top мусить казати "rerank" лише коли позицію дійсно зрушено).
        rerank_moved = {cid for i, cid in enumerate(post_pool_ids)
                        if i >= len(pre_pool_ids) or pre_pool_ids[i] != cid}

    # Стеля на частку коментарів у топі — ДО диверсифікації, бо саме вона
    # вирішує, хто взагалі бореться за слоти. Замір показав, що без неї
    # коментарі забирають майже все (див. _COMMENT_MAX_SHARE).
    _pre_cap_ids = ranked_ids
    ranked_ids = _cap_comments(ranked_ids, top_k, comment_share)
    # explain: чи стеля коментарів взагалі щось змінила в цьому виклику
    # (глобальний прапорець на весь пошук, не per-кандидат — сам _cap_comments
    # не позначає, ЯКІ саме кандидати посунуті).
    _comment_share_capped = ranked_ids != _pre_cap_ids

    # Кожен коментар — власна група диверсифікації. Кеп «не більше N з одного
    # джерела» існує проти багатослівного дзвінка, що зʼїдає всі слоти; у
    # коментарів такої проблеми немає за побудовою (це одне-два речення), а
    # під спільним ключем із транскриптом вони конкурували б із ним за той
    # самий ліміт — тобто уточнення витісняло б контекст, який пояснює.
    tid_of = {cid: (("cm", comment_rows[cid]["comment_id"]) if _is_comment_key(cid)
                    else _group_key(by_id[cid]))
              for cid in ranked_ids}
    _naive_top = ranked_ids[:top_k]
    chosen = _diversify(ranked_ids, tid_of, top_k, cap)
    # explain: чи диверсифікація змінила наївний зріз топ-k (той самий підхід,
    # що й вище для коментарів — прапорець на весь пошук).
    _diversity_capped = chosen != _naive_top
    _capped = {"comment_share": _comment_share_capped, "diversity": _diversity_capped}

    out = []
    for cid in chosen:
        meta = fused[cid]
        if _is_comment_key(cid):
            entry = _comment_entry(cid, comment_rows[cid], meta)
            if cid in rerank_scores:
                entry["rerank_score"] = round(rerank_scores[cid], 5)
            entry["why"] = _build_why(cid, meta, entry["source_type"], entry["matched_by"],
                                      rerank_scores, rerank_moved, stage_maps, explain, rec_w, com_w,
                                      _capped, rewrite_variants)
            out.append(entry)
            continue
        r = by_id[cid]
        entry = {
            "chunk_id": cid,
            "transcription_id": r["transcription_id"],
            "source_name": r["source_name"],
            "source_type": r["source_type"],
            "doc_type": r["doc_type"],
            "meeting_date": r["meeting_date"],
            "speaker": r["speaker"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "page": r["page"],
            "section": r["section"],
            "text": r["text"],
            "score": round(meta["final"], 5),
            "matched_by": sorted(set(meta["modes"])),
        }
        # Провенанс Telegram: без (tg_chat_id, tg_message_id) знахідку неможливо
        # відкрити в треді, а tg_chat_title — єдине, що каже, У ЯКОМУ чаті це
        # сказано (source_name це «[TG] чат: сніпет», з якого чат доводилось
        # виколупувати регуляркою). tg_link є не завжди: у legacy-групах
        # посилання на повідомлення фізично не існує, тому міст будується на парі id.
        if r["source_type"] == "telegram":
            entry.update({
                "tg_chat_id": r["tg_chat_id"],
                "tg_chat_title": r["tg_chat_title"],
                "tg_message_id": r["tg_message_id"],
                "tg_link": r["tg_link"],
                "tg_reply_to": r["tg_reply_to"],
                "tg_thread_id": r["tg_thread_id"],
            })
        if cid in rerank_scores:
            entry["rerank_score"] = round(rerank_scores[cid], 5)
        entry["why"] = _build_why(cid, meta, entry["source_type"], entry["matched_by"],
                                  rerank_scores, rerank_moved, stage_maps, explain, rec_w, com_w,
                                  _capped, rewrite_variants)
        out.append(entry)
    return {"query": query, "chunks": out, "vector_available": embeddings.is_available()}


#: Скільки підшитих коментарів максимум додавати до однієї відповіді. Стеля
#: потрібна, бо картка з тридцятьма уточненнями інакше вижерла б усе вікно
#: контексту, витіснивши сам матеріал, який ці уточнення пояснюють.
_ATTACH_COMMENT_CAP = int(os.environ.get("RAG_ATTACH_COMMENT_CAP", "8"))


def attach_comments(db_path: str, chunks: list[dict],
                    cap: int = _ATTACH_COMMENT_CAP) -> list[dict]:
    """Підшити до видачі закріплені коментарі та виправлення тих карток, які у
    неї потрапили (Волна 2, шар 2 пріоритету).

    Навіщо. Буст у ранжуванні працює тільки тоді, коли коментар САМ збігся із
    запитом. Але найцінніший коментар часто не збігається: «Іван більше не в
    проєкті» не має спільних слів із «хто відповідає за фасад», а «насправді
    сума 12k» знайдеться лише якщо в питанні вже названо суму. Тобто рівно ті
    уточнення, заради яких шар і зроблено, буст пропускав би.

    Тому: якщо у видачі є фрагмент запису X, то `pinned`-коментарі та
    коментарі-виправлення цього X приїжджають у контекст незалежно від
    збігу — механіка дзеркалить `attach_thread_context` для TG-ниток.
    Підшиваються ЛИШЕ ті типи, що в `comments.ATTACH_KINDS`, плюс закріплені
    вручну: якби підшивалось усе підряд, кожна відповідь тягла б усі замітки
    картки і шар перетворився б на шум.

    Повертає СПИСОК коментарів (не мутує chunks) — rag рендерить їх окремим
    пріоритетним блоком, а не вперемішку з цитатами.
    """
    from app.services import comments as comments_svc

    tids = {ch.get("transcription_id") for ch in chunks
            if ch.get("source_type") != "comment" and ch.get("transcription_id")}
    already = {ch.get("comment_id") for ch in chunks
               if ch.get("source_type") == "comment"}
    if not tids:
        return []

    kinds = sorted(comments_svc.ATTACH_KINDS)
    ph_t = ",".join("?" * len(tids))
    ph_k = ",".join("?" * len(kinds))
    try:
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT c.id, c.target_id, c.body, c.kind, c.weight, c.pinned, "
                f"c.author, c.anchor_time, substr(c.created_at, 1, 10) AS comment_date, "
                f"t.source_name AS tx_name "
                f"FROM comments c "
                f"LEFT JOIN transcriptions t ON t.id = c.target_id "
                f"WHERE c.deleted_at IS NULL AND c.target_type = 'transcription' "
                f"AND c.target_id IN ({ph_t}) "
                f"AND (c.pinned = 1 OR c.kind IN ({ph_k})) "
                f"ORDER BY c.pinned DESC, c.created_at DESC",
                list(tids) + kinds).fetchall()
    except sqlite3.OperationalError as exc:
        logger.debug("[retrieval] коментарі не підшито (немає таблиці?): %s", exc)
        return []

    out = []
    for r in rows:
        if r["id"] in already:
            continue                      # уже приїхав як самостійна знахідка
        out.append({
            "comment_id": r["id"],
            "transcription_id": r["target_id"],
            "target_label": r["tx_name"] or f"Запис #{r['target_id']}",
            "kind": r["kind"],
            "weight": comments_svc.kind_weight(r["kind"], r["weight"]),
            "pinned": bool(r["pinned"]),
            "author": r["author"],
            "anchor_time": r["anchor_time"],
            "date": r["comment_date"],
            "text": r["body"],
            "attached": True,
        })
        if len(out) >= cap:
            break
    return out


# Скільки сусідів по нитці підшивати до знахідки. 6 — компроміс: типова нитка
# коротка (медіана сплеску 4 повідомлення), а довгу переписку цілком тягнути в
# промпт немає сенсу — вона витіснить інші джерела з вікна.
_THREAD_STITCH = int(os.environ.get("RAG_THREAD_STITCH", "6"))


def _truncate_chars(text: str, limit: int) -> tuple[str, bool]:
    """Обрізає ``text`` до ``limit`` символів включно з суфіксом обрізки.

    Суфікс розмірюється по верхній межі (к-сть цифр у довжині всього тексту —
    точна к-сть обрізаних символів не більша за неї), тому результат гарантовано
    вкладається в ``limit`` без ітеративного підбору."""
    n = len(text)
    if n <= limit:
        return text, False
    reserve = len(f"…[обрізано, ще {n} симв.]")
    kept = max(limit - reserve, 0)
    omitted = n - kept
    suffix = f"…[обрізано, ще {omitted} симв.]"
    return text[:kept] + suffix, True


def attach_thread_context(db_path: str, chunks: list[dict],
                          max_msgs: int = _THREAD_STITCH) -> list[dict]:
    """Підшити до TG-знахідок сусідів їхньої нитки (Волна 4.5).

    Навіщо. У переписці знахідка часто є ПИТАННЯМ, а потрібна відповідь — вона
    наступним повідомленням, і спільних слів із запитом у неї немає, тож її не
    дістане ні вектор, ні FTS. Живий провал: на «коли зустріч з губернатором»
    видача віддала 7 слотів із 8, і всі сім — питання без жодної відповіді.
    Нитка (v32) дає ту саму розмову цілком, тож відповідь приїжджає разом із
    питанням і НЕ займає окремий слот видачі.

    Мутує елементи списку (додає ``thread``), повертає той самий список.
    Знахідки без нитки лишаються як були — це не помилка, а стан «нитки ще
    не розкладені для цього чату»."""
    thread_ids = {ch.get("tg_thread_id") for ch in chunks
                  if ch.get("source_type") == "telegram" and ch.get("tg_thread_id")}
    if not thread_ids:
        return chunks

    ids = list(thread_ids)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT tg_thread_id, tg_message_id, tg_date, tg_sender, transcript_text "
            f"FROM transcriptions WHERE tg_thread_id IN ({','.join('?' * len(ids))}) "
            f"AND deleted_at IS NULL ORDER BY tg_thread_id, tg_date",
            ids).fetchall()
        labels = dict(conn.execute(
            f"SELECT id, label FROM tg_threads WHERE id IN ({','.join('?' * len(ids))})",
            ids).fetchall())

    by_thread: dict[int, list] = {}
    for r in rows:
        by_thread.setdefault(r["tg_thread_id"], []).append(r)

    # Стелі читаються функцією (не константою модуля) — перемикаються без
    # рестарту процесу, як і решта env-налаштувань у settings.py.
    msg_limit = _settings.env_int("RAG_THREAD_MSG_CHARS")
    thread_limit = _settings.env_int("RAG_THREAD_CHARS")

    for ch in chunks:
        tid = ch.get("tg_thread_id")
        msgs = by_thread.get(tid) if tid else None
        if not msgs or len(msgs) < 2:
            continue
        # Вікно навколо самої знахідки, а не початок нитки: у довгій розмові
        # перші повідомлення можуть не мати стосунку до того, що знайшли.
        hit = next((i for i, m in enumerate(msgs)
                    if m["tg_message_id"] == ch.get("tg_message_id")), 0)
        half = max_msgs // 2
        start = max(0, hit - half)
        window = msgs[start:start + max_msgs]

        chars_total = sum(len(m["transcript_text"] or "") for m in window)

        hit_pos = hit - start  # позиція знахідки всередині window
        hit_row = window[hit_pos]

        # Хіт отримує бюджет, який ніколи не менший за бюджет сусіда: не
        # менше msg_limit, але не більше половини стелі нитки (щоб лишити
        # місце бодай на одного сусіда), і в жодному разі не більше стелі.
        hit_budget = min(thread_limit, max(msg_limit, thread_limit // 2))
        hit_text, hit_truncated = _truncate_chars(hit_row["transcript_text"] or "", hit_budget)
        remaining = thread_limit - len(hit_text)

        included: dict[int, dict] = {
            hit_row["tg_message_id"]: {
                "tg_message_id": hit_row["tg_message_id"], "date": hit_row["tg_date"],
                "sender": hit_row["tg_sender"], "text": hit_text, "is_hit": True,
            }
        }
        if hit_truncated:
            included[hit_row["tg_message_id"]]["truncated"] = True

        # Сусіди в порядку близькості до хіта (найближчий за tg_date першим,
        # з обох боків по черзі) — не в хронологічному порядку вікна.
        neighbour_order = []
        dist = 1
        while hit_pos - dist >= 0 or hit_pos + dist < len(window):
            if hit_pos - dist >= 0:
                neighbour_order.append(window[hit_pos - dist])
            if hit_pos + dist < len(window):
                neighbour_order.append(window[hit_pos + dist])
            dist += 1

        dropped = 0
        for idx, m in enumerate(neighbour_order):
            budget = min(msg_limit, remaining)
            raw = m["transcript_text"] or ""
            n = len(raw)
            if n > budget:
                reserve = len(f"…[обрізано, ще {n} симв.]")
                kept = max(budget - reserve, 0)
                if kept <= 0:
                    # Залишок не вміщує нічого крім суфікса — сусід і всі
                    # дальші (у порядку близькості) відкидаються.
                    dropped = len(neighbour_order) - idx
                    break
            text, truncated = _truncate_chars(raw, max(budget, 0))
            remaining -= len(text)
            msg = {"tg_message_id": m["tg_message_id"], "date": m["tg_date"],
                   "sender": m["tg_sender"], "text": text, "is_hit": False}
            if truncated:
                msg["truncated"] = True
            included[m["tg_message_id"]] = msg

        # Порядок messages лишається хронологічним (порядком вікна).
        messages = [included[m["tg_message_id"]] for m in window
                    if m["tg_message_id"] in included]

        chars_kept = sum(len(m["text"]) for m in messages)
        ch["thread"] = {
            "thread_id": tid,
            "label": labels.get(tid),
            "total_messages": len(msgs),
            "chars_total": chars_total,
            "chars_kept": chars_kept,
            "dropped": dropped,
            "messages": messages,
        }
    return chunks
