"""Нитки розмови всередині Telegram-чату (Волна 4.5).

**Проблема.** Чат із повідомлень — це 955 незвʼязаних записів, і «Ок»
фізично не несе контексту. Живий провал: на «коли зустріч з губернатором» пошук
віддав 7 слотів із 8, і всі сім — питання без жодної відповіді. Відповідь була
наступним повідомленням треда, спільних слів із запитом нема, тож її не знайшов
ні вектор, ні FTS. Дзеркальна біда з мітками: категорію дає чат цілком, тому
будь-яке повідомлення з «ACMECORP&Робота» потрапляє у зріз по Acmecorp,
навіть якщо воно про відпустку.

**Одиниця сенсу — нитка всередині чату**, не чат і не окреме повідомлення.

Чому саме такий каскад (кожна ступінь стоїть на замірі, а не на здогадці):

- **`tg_reply_to` — жорсткий оверрайд, але майже порожній.** Це те, що САМ
  Telegram знає про нитку, помилитись тут неможливо. Колонка forward-only
  (зʼявилась у Волні 4, 03.08), тож на живому архіві покриває 24 записи з 3951
  — 0.6%. Далі росте, але історію нею не зшити.

- **Косинус e5 НЕ ухвалює рішення.** Замір на архіві: reply-пари (справжня
  нитка) дають середній косинус 0.828, випадкові пари в тому ж чаті — 0.827.
  Розділення +0.02σ, тобто нуль. Центрування (відняти середнє корпусу) піднімає
  до +0.61σ — усе одно близько третини помилок. Причина видна в самих парах:
  «Дзвінка не буде?» ← «ні, нічого Адам не писав» — справжня нитка з косинусом
  0.837, як у випадкової пари. **Короткій репліці нема чого вкладати в тему:**
  «ні» не про предмет, а про регістр мовлення. Тому центроїд тут — лише
  РАНЖУВАЛЬНИК кандидатів для LLM, і жодного абсолютного порога в коді немає.

- **Рішення ухвалює локальна LLM, але не на кожне повідомлення.** Повідомлення
  кучкуються у сплески: при паузі 3 год архів згортається у 569 сплесків
  (медіана 4 повідомлення), з них 484 містять більше одного. Це 484 виклики
  замість 3900 — вісім разів дешевше при тій самій роздільній здатності.
  Склейка за вікном часу як ФІНАЛЬНА відповідь зарубана редколегією і
  правильно: у чаті з паралельними темами вона зшиває незвʼязане. Тут вона
  лише генератор кандидатів, а теми ВСЕРЕДИНІ сплеску розділяє модель.

Все локально і безкоштовно (Ollama, `LOCAL_LLM_MODEL`). Нема Ollama — модуль
деградує: сплеск лишається однією ниткою, `tg_thread_src='burst'`, і це видно
в даних, а не ховається.

CLI:
    python -m app.services.tg_threads backfill --dry-run
    python -m app.services.tg_threads backfill --chat -1001234567890
    python -m app.services.tg_threads stats
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)


# Пауза, після якої розмова вважається новим сплеском. 180 хв — з заміру
# розподілу: на 30 хв третина сплесків вироджується в одинаків (нема чого
# групувати), на 1440 хв сплеск розростається до повідомлень і в нього
# заходить кілька робочих днів. На 180 хв медіана 4, одинаків 15%.
BURST_GAP_MIN = int(os.environ.get("TG_THREAD_BURST_GAP_MIN", "180"))

# Скільки повідомлень і символів віддаємо моделі за раз. Довгі сплески (до 130
# повідомлень, до 338k символів) ріжемо на під-пачки, переносячи вже відкриті
# нитки далі — інакше промпт не влазить у контекст і модель мовчки обрізає хвіст.
MAX_BATCH_MSGS = int(os.environ.get("TG_THREAD_BATCH_MSGS", "25"))
MAX_BATCH_CHARS = int(os.environ.get("TG_THREAD_BATCH_CHARS", "6000"))

# Скільки відкритих ниток чату показувати моделі як кандидатів на продовження.
OPEN_THREAD_CANDIDATES = int(os.environ.get("TG_THREAD_CANDIDATES", "5"))

# Модель для розбиття сплесків. Окрема від LOCAL_LLM_MODEL навмисно: копілот
# працює в реальному часі під час дзвінка і налаштований на свою модель, а тут
# задача структурна (згрупувати 4–10 коротких реплік) — на замірі 7b дала те
# саме розбиття, що й 32b, за вчетверо менший час. None = взяти LOCAL_LLM_MODEL.
THREAD_MODEL = os.environ.get("TG_THREAD_MODEL") or None

# Нитка без нових повідомлень стільки днів вважається закритою і більше не
# пропонується як кандидат. Не видаляє нічого — лише прибирає з активних.
#
# 3, а не 21. Це НЕ смакова правка: на прогоні з вікном 21 день максимальний
# розрив усередині нитки упирався рівно в 21 (max=21.0 при медіані 2.8), тобто
# межу ставило саме вікно, а не зміст. частина ниток мали внутрішній розрив понад
# 3 дні, 34% — понад тиждень, і нитка «Попередження про дзвінок» тягнулась із
# 18.06 по 30.07, склеївши календар, «давай зараз з Настею» і жарти.
# Причина: моделі показують список відкритих ниток із назвами, і 7B охоче
# обирає щось зі списку — тиску «нічого не підходить» у неї немає. Тому
# продовження стримує ВІКНО, а не судження моделі. 3 дні лишають справжні
# випадки (розмова, що продовжилась наступного ранку; пʼятниця → понеділок) і
# прибирають склейку через тижні.
THREAD_IDLE_DAYS = int(os.environ.get("TG_THREAD_IDLE_DAYS", "3"))

# Нитка закривається і за ОБСЯГОМ, не лише за тишею. Без цього вона росте, доки
# в чат пишуть частіше за THREAD_IDLE_DAYS: на першому проході ниток зібрали
# 1594 повідомлення (значна частина архіву), найбільша — повідомлень за два місяці, і
# всередині неї адреса офісу, презентація, сценарії й окрема розмова про Адама.
# Це вже не тема, а чат усередині чату — тобто рівно те, від чого волна лікує.
# 30 — з розподілу: нитки до повідомлень тримають основну масу і виглядають
# темами, далі починається накопичення.
#
# Стеля МʼЯКА, і це навмисно: вона забороняє нитці приймати НОВУ розмову, але
# ніколи не рве одну розмову навпіл. Група, яку модель визнала однією темою,
# заходить цілком, тож фактичний розмір може перевищити стелю на розмір групи
# (спостережено 49 при стелі 30). Розірвати звʼязну розмову заради круглого
# числа було б гірше за саме накопичення: сенс нитки в тому, що вона ціла.
THREAD_MAX_MSGS = int(os.environ.get("TG_THREAD_MAX_MSGS", "30"))

# Обрізка тексту повідомлення у промпті. Довгі пересилання (статті, документи)
# не мають витісняти сусідів зі сплеску — для віднесення до теми вистачає початку.
_MSG_PREVIEW_CHARS = 400

_SCHEMA = {
    "type": "object",
    "properties": {
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "msgs": {"type": "array", "items": {"type": "integer"}},
                    "label": {"type": "string"},
                    "continues": {"type": "integer"},
                },
                "required": ["msgs", "label"],
            },
        }
    },
    "required": ["threads"],
}

_SYSTEM = (
    "Ти розбираєш робочу переписку на теми. Тема — це те, ПРО ЩО йдеться "
    "(проєкт, питання, домовленість), а не хто пише і коли. "
    "Питання і відповідь на нього — ОДНА тема. Коротка репліка («ок», «+», "
    "«ні», «дякую») належить темі, до якої вона реагує, а не власній. "
    "Кожне повідомлення потрапляє рівно в одну тему. Відповідай лише JSON."
)


# ============================================================
# Сплески
# ============================================================

def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def segment_bursts(messages: list, gap_min: int = BURST_GAP_MIN) -> list[list]:
    """Порізати впорядковані за часом повідомлення чату на сплески.

    Сплеск — щільна в часі пачка. Це НЕ тема (всередині сплеску їх буває
    кілька — саме тому далі йде модель), а лише кандидат, який різко звужує
    обсяг роботи: 3951 повідомлення → 569 сплесків."""
    bursts: list[list] = []
    current: list = []
    prev: Optional[datetime] = None
    for msg in messages:
        ts = _parse_date(msg["tg_date"])
        if prev is not None and ts is not None and (ts - prev) > timedelta(minutes=gap_min):
            bursts.append(current)
            current = []
        current.append(msg)
        if ts is not None:
            prev = ts
    if current:
        bursts.append(current)
    return bursts


def _batches(burst: list) -> list[list]:
    """Порізати сплеск на під-пачки, що влазять у контекст моделі."""
    out: list[list] = []
    cur: list = []
    chars = 0
    for msg in burst:
        size = min(len(msg["transcript_text"] or ""), _MSG_PREVIEW_CHARS)
        if cur and (len(cur) >= MAX_BATCH_MSGS or chars + size > MAX_BATCH_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur.append(msg)
        chars += size
    if cur:
        out.append(cur)
    return out


# ============================================================
# Кандидати-нитки
# ============================================================

def _vec(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32).astype(np.float64)


def _burst_centroid(conn, burst: list) -> Optional[np.ndarray]:
    """Середній вектор сплеску з уже порахованих чанків (GPU не потрібен)."""
    ids = [m["id"] for m in burst]
    if not ids:
        return None
    rows = conn.execute(
        f"SELECT embedding FROM chunks WHERE transcription_id IN "
        f"({','.join('?' * len(ids))}) AND chunk_index = 0 AND embedding IS NOT NULL",
        ids,
    ).fetchall()
    vecs = [_vec(r["embedding"]) for r in rows]
    vecs = [v for v in vecs if v is not None]
    if not vecs:
        return None
    mean = np.mean(vecs, axis=0)
    norm = np.linalg.norm(mean)
    return mean / norm if norm else mean


def _candidate_threads(conn, chat_id: int, before: Optional[str],
                       centroid: Optional[np.ndarray],
                       limit: int = OPEN_THREAD_CANDIDATES) -> list[dict]:
    """Відкриті нитки чату, найсхожіші на сплеск — щоб модель могла сказати
    «це продовження вчорашньої розмови», а не плодити дублікати теми.

    Косинус тут РАНЖУЄ, а не вирішує: жодного порога, просто порядок показу.
    Абсолютний поріг на цих даних необґрунтований (див. модульний docstring)."""
    rows = conn.execute(
        # msg_count < THREAD_MAX_MSGS: переповнена нитка більше не продовжується,
        # інакше вона накопичує чат за місяці і перестає бути темою.
        "SELECT id, label, centroid, last_date, msg_count FROM tg_threads "
        "WHERE chat_id = ? AND status = 'open' AND msg_count < ? "
        "AND (? IS NULL OR last_date <= ?) "
        "ORDER BY last_date DESC LIMIT 60",
        (chat_id, THREAD_MAX_MSGS, before, before),
    ).fetchall()
    cands = []
    for r in rows:
        # Нитка, що мовчить довше THREAD_IDLE_DAYS до цього сплеску, не кандидат.
        if before and r["last_date"]:
            gap = _parse_date(before), _parse_date(r["last_date"])
            if gap[0] and gap[1] and (gap[0] - gap[1]) > timedelta(days=THREAD_IDLE_DAYS):
                continue
        score = 0.0
        tv = _vec(r["centroid"])
        if tv is not None and centroid is not None:
            norm = np.linalg.norm(tv)
            if norm:
                score = float(centroid @ (tv / norm))
        cands.append({"id": r["id"], "label": r["label"], "score": score,
                      "last_date": r["last_date"], "msg_count": r["msg_count"]})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:limit]


# ============================================================
# Розбиття сплеску моделлю
# ============================================================

def _preview(text: Optional[str]) -> str:
    t = " ".join((text or "").split())
    return t[:_MSG_PREVIEW_CHARS] if t else "[без тексту]"


def _build_prompt(chat_title: str, batch: list, candidates: list[dict]) -> str:
    lines = [f"Чат: «{chat_title or 'без назви'}»", ""]
    if candidates:
        lines.append("Уже відкриті теми цього чату (можна продовжити одну з них):")
        for c in candidates:
            lines.append(f"  T{c['id']}: {c['label'] or 'без назви'}")
        lines.append("")
    lines.append("Повідомлення:")
    for i, m in enumerate(batch, start=1):
        who = m["tg_sender"] or "невідомо"
        when = (m["tg_date"] or "")[:16].replace("T", " ")
        lines.append(f"{i}. [{when} {who}] {_preview(m['transcript_text'])}")
    lines += [
        "",
        "Згрупуй ці повідомлення в теми. Кожен номер рівно в одній темі.",
        "Якщо тема продовжує вже відкриту — вкажи її номер у полі continues (без «T»).",
        'JSON: {"threads":[{"msgs":[1,2],"label":"коротка назва теми","continues":123}]}',
    ]
    return "\n".join(lines)


def split_batch(chat_title: str, batch: list, candidates: list[dict],
                model: Optional[str] = None) -> Optional[list[dict]]:
    """Віддати пачку моделі і повернути [{"msgs":[idx…], "label":…,
    "continues": thread_id|None}] у 1-based індексах пачки.

    None — модель недоступна або відповіла нерозбірливо; викликач тоді лишає
    пачку однією ниткою. Мовчазної «нитки навмання» тут не буває."""
    from app.services import local_llm

    prompt = _build_prompt(chat_title, batch, candidates)
    valid_ids = {c["id"] for c in candidates}
    try:
        resp = local_llm.generate_json(prompt, schema=_SCHEMA, system=_SYSTEM,
                                       max_tokens=800, model=model or THREAD_MODEL)
    except Exception as exc:
        logger.warning("[tg_threads] локальна модель не відповіла (%s) — "
                       "сплеск лишається однією ниткою", exc)
        return None

    data = resp.get("data") or {}
    raw = data.get("threads")
    if not isinstance(raw, list) or not raw:
        logger.warning("[tg_threads] відповідь без threads — сплеск однією ниткою")
        return None

    out: list[dict] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        idxs = [i for i in (item.get("msgs") or [])
                if isinstance(i, int) and 1 <= i <= len(batch) and i not in seen]
        if not idxs:
            continue
        seen.update(idxs)
        cont = item.get("continues")
        out.append({
            "msgs": sorted(idxs),
            "label": (item.get("label") or "").strip()[:120] or None,
            # Модель охоче вигадує id — беремо лише ті, що ми самі показали.
            "continues": cont if isinstance(cont, int) and cont in valid_ids else None,
        })
    # Повідомлення, які модель загубила, не мають зникнути з архіву. Позначаємо
    # їх окремо: це не «модель вирішила, що тема самотня», а «модель промовчала»,
    # і зливати ці два випадки в один провенанс — значить втратити можливість
    # потім перерахувати саме промахи.
    #
    # Куди їх класти. Окрема нитка на кожного — це гарантовано нитка без
    # контексту (на першому проході так вийшло повідомлень, 10.дрібна частка архіву).
    # Сусід по позиції — здогадка, але в переписці сусідні репліки сплеску
    # майже завжди про те саме, тож вона майже завжди краща за самотність.
    # Здогадку видно у провенансі (`orphan`), і її можна перерахувати окремо.
    missed = [i for i in range(1, len(batch) + 1) if i not in seen]
    if missed:
        logger.info("[tg_threads] модель не віднесла %d повідомлень — кладу до сусідів",
                    len(missed))
        for i in missed:
            host = min(out, key=lambda g: min(abs(j - i) for j in g["msgs"]),
                       default=None) if out else None
            if host is None:
                out.append({"msgs": [i], "label": None, "continues": None, "orphan": True})
            else:
                host["msgs"] = sorted(host["msgs"] + [i])
                host.setdefault("orphan_msgs", []).append(i)
    return out or None


# ============================================================
# Запис
# ============================================================

def _fallback_label(messages: list, max_chars: int = 60) -> Optional[str]:
    """Назва нитки без моделі — початок першого змістовного повідомлення.

    Потрібна не для краси: назви відкритих ниток — це рівно те, що бачить
    модель, коли вирішує «чи продовжує цей сплеск щось із раніше». Нитка без
    назви у списку кандидатів марна, а сплеск з одного повідомлення моделі не
    показують узагалі (групувати нема з чим), тож інакше він лишався б німим."""
    for m in messages:
        text = " ".join((m["transcript_text"] or "").split())
        if text and not text.startswith("["):
            return text[:max_chars]
    return None


def _create_thread(conn, chat_id: int, label: Optional[str]) -> int:
    cur = conn.execute(
        "INSERT INTO tg_threads (chat_id, label, status) VALUES (?, ?, 'open')",
        (chat_id, label))
    return cur.lastrowid


def _attach(conn, thread_id: int, messages: list, src: str) -> None:
    """Привʼязати повідомлення до нитки і перерахувати її агрегати."""
    conn.executemany(
        "UPDATE transcriptions SET tg_thread_id = ?, tg_thread_src = ? WHERE id = ?",
        [(thread_id, src, m["id"]) for m in messages])
    _refresh_thread(conn, thread_id)


def _refresh_thread(conn, thread_id: int) -> None:
    """Перерахувати centroid / msg_count / межі дат із фактичного складу нитки.

    Рахуємо ЗАНОВО, а не інкрементально: повідомлення можна перевіднести
    (рішення пересматриваемое за задумом), і накопичене середнє тоді розʼїхалось
    би з реальним складом непомітно."""
    rows = conn.execute(
        "SELECT t.tg_date, ch.embedding FROM transcriptions t "
        "LEFT JOIN chunks ch ON ch.transcription_id = t.id AND ch.chunk_index = 0 "
        "WHERE t.tg_thread_id = ? AND t.deleted_at IS NULL",
        (thread_id,),
    ).fetchall()
    if not rows:
        conn.execute("UPDATE tg_threads SET msg_count = 0 WHERE id = ?", (thread_id,))
        return
    dates = sorted(d for d in (r["tg_date"] for r in rows) if d)
    vecs = [v for v in (_vec(r["embedding"]) for r in rows) if v is not None]
    centroid_blob = None
    if vecs:
        mean = np.mean(vecs, axis=0)
        norm = np.linalg.norm(mean)
        if norm:
            mean = mean / norm
        centroid_blob = mean.astype(np.float32).tobytes()
    conn.execute(
        "UPDATE tg_threads SET msg_count = ?, first_date = ?, last_date = ?, "
        "centroid = COALESCE(?, centroid) WHERE id = ?",
        (len(rows), dates[0] if dates else None, dates[-1] if dates else None,
         centroid_blob, thread_id))
    # Закриття за обсягом. Нитка, що набрала стелю, більше не продовжується —
    # наступна розмова піде окремою ниткою (з власною назвою), а не доліпиться
    # до цієї. Тиша закриває нитку окремо, у close_idle_threads.
    if len(rows) >= THREAD_MAX_MSGS:
        conn.execute("UPDATE tg_threads SET status = 'closed', "
                     "closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP) "
                     "WHERE id = ? AND status = 'open'", (thread_id,))


def close_idle_threads(db_path: str, *, now: Optional[datetime] = None,
                       idle_days: int = THREAD_IDLE_DAYS) -> int:
    """Закрити нитки, що мовчать довше idle_days. Нічого не видаляє —
    закрита нитка просто перестає бути кандидатом на продовження."""
    cutoff = ((now or datetime.now()) - timedelta(days=idle_days)).isoformat()
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE tg_threads SET status = 'closed', closed_at = CURRENT_TIMESTAMP "
            "WHERE status = 'open' AND (last_date IS NULL OR last_date < ?)",
            (cutoff,))
        conn.commit()
        return cur.rowcount


# ============================================================
# Каскад
# ============================================================

def _reply_parent_thread(conn, msg) -> Optional[int]:
    """Ступінь 0: нитка батька по tg_reply_to. Точно і безкоштовно."""
    if not msg["tg_reply_to"]:
        return None
    row = conn.execute(
        "SELECT tg_thread_id FROM transcriptions "
        "WHERE tg_chat_id = ? AND tg_message_id = ? AND tg_thread_id IS NOT NULL",
        (msg["tg_chat_id"], msg["tg_reply_to"])).fetchone()
    return row["tg_thread_id"] if row else None


def assign_burst(conn, chat_id: int, chat_title: str, burst: list, *,
                 model: Optional[str] = None, use_llm: bool = True) -> dict:
    """Рознести один сплеск по нитках. Повертає зведення для звіту."""
    created = 0
    continued = 0
    by_reply = 0

    for batch in _batches(burst):
        centroid = _burst_centroid(conn, batch)
        before = min((m["tg_date"] for m in batch if m["tg_date"]), default=None)
        candidates = _candidate_threads(conn, chat_id, before, centroid)

        groups = split_batch(chat_title, batch, candidates, model=model) if use_llm else None
        src = "llm"
        if groups is None:
            # Деградація без вигадок: уся пачка — одна нитка, і це видно в даних
            # через tg_thread_src, тож потім можна перерахувати саме їх.
            groups = [{"msgs": list(range(1, len(batch) + 1)), "label": None,
                       "continues": None}]
            # Пачка з одного повідомлення моделі не потребувала: воно самотнє
            # тривіально, а не через збій. Змішати це з реальною деградацією —
            # значить зробити прапорець марним саме тоді, коли він потрібен.
            src = "single" if len(batch) == 1 else "burst"

        for g in groups:
            msgs = [batch[i - 1] for i in g["msgs"]]
            # Ступінь 0 має пріоритет над моделлю: Telegram знає нитку точно.
            thread_id = None
            for m in msgs:
                parent = _reply_parent_thread(conn, m)
                if parent:
                    thread_id = parent
                    by_reply += 1
                    break
            msg_src = "reply" if thread_id else src
            if thread_id is None and g["continues"]:
                thread_id = g["continues"]
                continued += 1
            if thread_id is None:
                thread_id = _create_thread(conn, chat_id,
                                           g["label"] or _fallback_label(msgs))
                created += 1
            elif g["label"]:
                # Нитка живе далі — оновлюємо назву лише якщо її не було.
                conn.execute("UPDATE tg_threads SET label = COALESCE(label, ?) WHERE id = ?",
                             (g["label"], thread_id))
            if msg_src == "llm":
                if g.get("orphan"):
                    msg_src = "orphan"
                elif len(msgs) == 1:
                    msg_src = "single"
            # Провенанс поштучний: у групі можуть лежати і рішення моделі, і
            # дописані сусідством промахи. Один src на всю групу зробив би
            # здогадку невідрізненною від рішення — а це саме те, заради чого
            # поле й існує.
            orphan_idx = set(g.get("orphan_msgs") or [])
            if orphan_idx and msg_src != "reply":
                decided = [batch[i - 1] for i in g["msgs"] if i not in orphan_idx]
                guessed = [batch[i - 1] for i in g["msgs"] if i in orphan_idx]
                if decided:
                    _attach(conn, thread_id, decided,
                            "single" if len(decided) == 1 and msg_src == "llm" else msg_src)
                _attach(conn, thread_id, guessed, "orphan")
            else:
                _attach(conn, thread_id, msgs, msg_src)

    return {"created": created, "continued": continued, "by_reply": by_reply}


def backfill(db_path: str, *, chat_id: Optional[int] = None, dry_run: bool = True,
             limit_bursts: Optional[int] = None, model: Optional[str] = None,
             use_llm: bool = True, only_unassigned: bool = True) -> dict:
    """Прохід по архіву: розкласти TG-повідомлення по нитках.

    Ідемпотентний: за замовчуванням чіпає лише повідомлення без нитки."""
    started = datetime.now()
    with get_db_connection(db_path) as conn:
        where = ["source_type = 'telegram'", "deleted_at IS NULL", "tg_date IS NOT NULL"]
        params: list[Any] = []
        if only_unassigned:
            where.append("tg_thread_id IS NULL")
        if chat_id is not None:
            where.append("tg_chat_id = ?")
            params.append(chat_id)
        rows = conn.execute(
            f"SELECT id, tg_chat_id, tg_chat_title, tg_message_id, tg_reply_to, "
            f"tg_date, tg_sender, transcript_text FROM transcriptions "
            f"WHERE {' AND '.join(where)} ORDER BY tg_chat_id, tg_date",
            params).fetchall()

        by_chat: dict[int, list] = {}
        for r in rows:
            by_chat.setdefault(r["tg_chat_id"], []).append(r)

        plan = []
        total_bursts = 0
        for cid, msgs in by_chat.items():
            bursts = segment_bursts(msgs)
            total_bursts += len(bursts)
            plan.append({"chat_id": cid, "title": msgs[0]["tg_chat_title"],
                         "messages": len(msgs), "bursts": len(bursts)})

        if dry_run:
            return {"dry_run": True, "messages": len(rows), "chats": len(by_chat),
                    "bursts": total_bursts,
                    "llm_calls_estimate": sum(
                        len(_batches(b)) for msgs in by_chat.values()
                        for b in segment_bursts(msgs) if len(b) > 1),
                    "plan": sorted(plan, key=lambda p: -p["messages"])}

        totals = {"created": 0, "continued": 0, "by_reply": 0}
        done = 0
        for cid, msgs in by_chat.items():
            title = msgs[0]["tg_chat_title"]
            for burst in segment_bursts(msgs):
                if limit_bursts is not None and done >= limit_bursts:
                    break
                # Сплеск з одного повідомлення моделі не потребує: групувати нема що.
                res = assign_burst(conn, cid, title, burst, model=model,
                                   use_llm=use_llm and len(burst) > 1)
                for k in totals:
                    totals[k] += res[k]
                done += 1
                conn.commit()
                if done % 25 == 0:
                    logger.info("[tg_threads] сплесків оброблено: %d/%d", done, total_bursts)
            if limit_bursts is not None and done >= limit_bursts:
                break
        conn.commit()

    return {"dry_run": False, "messages": len(rows), "bursts_processed": done,
            "threads_created": totals["created"], "threads_continued": totals["continued"],
            "by_reply": totals["by_reply"],
            "seconds": round((datetime.now() - started).total_seconds(), 1)}


# ============================================================
# Живий інжест
# ============================================================

def assign_incoming(db_path: str, transcription_id: int) -> Optional[int]:
    """Віднести щойно прийняте повідомлення до нитки — БЕЗ виклику моделі.

    Рішення тут навмисно попереднє (`tg_thread_src='pending'`). Причина у
    вимозі власника: рішення має бути ПЕРЕСМАТРИВАЄМИМ — повідомлення, схоже
    на нову тему, може виявитись продовженням після наступних трьох, і
    безвідкличний вибір на кожну репліку цю можливість убиває. Плюс модель на
    кожне повідомлення — це рівно та поштучна ціна, заради уникнення якої
    сплеск і вигадали.

    Тому в реальному часі працюють лише точні й безкоштовні ступені, а
    ``resettle_pending`` перерозкладає сплеск моделлю, коли той договорив."""
    with get_db_connection(db_path) as conn:
        msg = conn.execute(
            "SELECT id, tg_chat_id, tg_message_id, tg_reply_to, tg_date, transcript_text "
            "FROM transcriptions WHERE id = ? AND source_type = 'telegram'",
            (transcription_id,)).fetchone()
        if not msg or msg["tg_chat_id"] is None:
            return None

        thread_id = _reply_parent_thread(conn, msg)
        src = "reply"
        if thread_id is None:
            # Той самий сплеск, що й попереднє повідомлення чату?
            cutoff = None
            ts = _parse_date(msg["tg_date"])
            if ts is not None:
                cutoff = (ts - timedelta(minutes=BURST_GAP_MIN)).isoformat()
            row = conn.execute(
                "SELECT id FROM tg_threads WHERE chat_id = ? AND status = 'open' "
                "AND (? IS NULL OR last_date >= ?) ORDER BY last_date DESC LIMIT 1",
                (msg["tg_chat_id"], cutoff, cutoff)).fetchone()
            thread_id = (row["id"] if row
                         else _create_thread(conn, msg["tg_chat_id"],
                                             _fallback_label([msg])))
            src = "pending"

        _attach(conn, thread_id, [msg], src)
        conn.commit()
        return thread_id


def resettle_pending(db_path: str, *, model: Optional[str] = None,
                     now: Optional[datetime] = None) -> dict:
    """Перерозкласти моделлю сплески, які вже договорили.

    Бере повідомлення з ``tg_thread_src='pending'``, чий сплеск завершився
    (тиша довша за BURST_GAP_MIN), відвʼязує їх і проганяє звичайним каскадом.
    Ідемпотентно: те, що вже розклала модель, не чіпає."""
    cutoff = ((now or datetime.now()) - timedelta(minutes=BURST_GAP_MIN)).isoformat()
    processed = 0
    totals = {"created": 0, "continued": 0, "by_reply": 0}
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, tg_chat_id, tg_chat_title, tg_message_id, tg_reply_to, "
            "tg_date, tg_sender, transcript_text FROM transcriptions "
            "WHERE source_type = 'telegram' AND deleted_at IS NULL "
            "AND tg_thread_src = 'pending' AND tg_date < ? "
            "ORDER BY tg_chat_id, tg_date", (cutoff,)).fetchall()
        if not rows:
            return {"resettled": 0, **totals}

        touched = {r["id"] for r in rows}
        stale_threads = {r["tg_thread_id"] for r in conn.execute(
            f"SELECT DISTINCT tg_thread_id FROM transcriptions WHERE id IN "
            f"({','.join('?' * len(touched))}) AND tg_thread_id IS NOT NULL",
            list(touched)).fetchall()}
        conn.executemany("UPDATE transcriptions SET tg_thread_id = NULL, "
                         "tg_thread_src = NULL WHERE id = ?", [(i,) for i in touched])

        by_chat: dict[int, list] = {}
        for r in rows:
            by_chat.setdefault(r["tg_chat_id"], []).append(r)
        for cid, msgs in by_chat.items():
            for burst in segment_bursts(msgs):
                res = assign_burst(conn, cid, msgs[0]["tg_chat_title"], burst,
                                   model=model, use_llm=len(burst) > 1)
                for k in totals:
                    totals[k] += res[k]
                processed += len(burst)

        # Нитки, які спорожніли після відвʼязування, прибирати не треба —
        # _refresh_thread виставить msg_count=0, і вони не потраплять у видачу.
        for tid in stale_threads:
            if tid:
                _refresh_thread(conn, tid)
        conn.commit()
    return {"resettled": processed, **totals}


# ============================================================
# Читання (для retrieval / RAG)
# ============================================================

def thread_messages(db_path: str, thread_id: int, *, limit: int = 40) -> list[dict]:
    """Повідомлення нитки в хронологічному порядку — те, чим зшивається
    питання з відповіддю у видачі."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, tg_message_id, tg_date, tg_sender, transcript_text "
            "FROM transcriptions WHERE tg_thread_id = ? AND deleted_at IS NULL "
            "ORDER BY tg_date LIMIT ?", (thread_id, limit)).fetchall()
    return [dict(r) for r in rows]


def stats(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS threads, "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_threads, "
            "AVG(msg_count) AS avg_msgs, MAX(msg_count) AS max_msgs FROM tg_threads"
        ).fetchone()
        assigned = conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE source_type='telegram' "
            "AND deleted_at IS NULL AND tg_thread_id IS NOT NULL").fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE source_type='telegram' "
            "AND deleted_at IS NULL").fetchone()[0]
        by_src = dict(conn.execute(
            "SELECT COALESCE(tg_thread_src,'—'), COUNT(*) FROM transcriptions "
            "WHERE source_type='telegram' AND deleted_at IS NULL "
            "GROUP BY 1").fetchall())
    return {"threads": row["threads"] or 0, "open": row["open_threads"] or 0,
            "avg_messages": round(row["avg_msgs"] or 0, 1),
            "max_messages": row["max_msgs"] or 0,
            "messages_assigned": assigned, "messages_total": total,
            "by_source": by_src}


# ============================================================
# CLI
# ============================================================

def _print(res: dict) -> int:
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[list] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass

    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(prog="tg_threads",
                                description="Нитки розмови в Telegram-чатах (Волна 4.5).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backfill", help="Розкласти повідомлення по нитках")
    b.add_argument("--dry-run", action="store_true", help="Порахувати обсяг, нічого не писати")
    b.add_argument("--chat", type=int, default=None, help="Лише один чат (tg_chat_id)")
    b.add_argument("--limit-bursts", type=int, default=None, help="Обробити не більше N сплесків")
    b.add_argument("--model", default=None, help="Перекрити LOCAL_LLM_MODEL")
    b.add_argument("--no-llm", action="store_true",
                   help="Без моделі: сплеск = нитка (для замірів деградації)")
    b.add_argument("--redo", action="store_true",
                   help="Перерозкласти і вже віднесені повідомлення")

    r = sub.add_parser("resettle", help="Перерозкласти моделлю сплески, що договорили")
    r.add_argument("--model", default=None)

    c = sub.add_parser("close-idle", help="Закрити нитки, що мовчать")
    c.add_argument("--days", type=int, default=THREAD_IDLE_DAYS)

    sub.add_parser("stats", help="Скільки ниток і чим вони віднесені")

    args = p.parse_args(argv)
    if args.command == "backfill":
        return _print(backfill(args.db, chat_id=args.chat, dry_run=args.dry_run,
                               limit_bursts=args.limit_bursts, model=args.model,
                               use_llm=not args.no_llm,
                               only_unassigned=not args.redo))
    if args.command == "resettle":
        return _print(resettle_pending(args.db, model=args.model))
    if args.command == "close-idle":
        return _print({"closed": close_idle_threads(args.db, idle_days=args.days)})
    return _print(stats(args.db))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
