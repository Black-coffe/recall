"""Meeting Memory — оркестратор збагачення транскриптів (Phase 13).

Перетворює сирий транскрипт на структуровану "картку мітингу" + наповнює
наскрізний граф сутностей (люди / проєкти / організації) і таблицю action items,
щоб Whisper працював як RAG-архів корпоративної памʼяті.

Потік (один Claude-виклик на мітинг — text_polishing.extract_meeting_card):
  1. Читаємо транскрипт + сегменти + speaker-map з БД.
  2. Будуємо diarized-текст ('Імʼя: репліка') якщо є діаризація.
  3. extract_meeting_card → summary/key_points/action_items/people/projects/orgs/topics.
  4. У транзакції:
     - UPDATE transcriptions: summary_json + topics_json (формат як в існуючих
       ендпоінтах, щоб UI summary/topics працював), enriched_* маркери.
     - upsert entities + aliases, лінк meeting_entities, перерахунок агрегатів.
     - перезапис action_items (idempotent re-run).

Ідемпотентність: enriched_at IS NOT NULL → skip (якщо force=False). Зміна
ENRICHMENT_VERSION дозволяє форсувати масовий re-run при оновленні промптів.

НЕ залежить від flask app context — приймає db_path явно (тестовно, можна
викликати з JobQueue-воркера або backfill-скрипта).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime
from typing import Callable, Optional

from app.db.connection import get_db_connection
from app.repositories import transcriptions as tx_repo
from app.services import commitments, embeddings, models, pricing, text_polishing


logger = logging.getLogger(__name__)

# Бамп при зміні промпту/схеми збагачення → backfill(force_version=True) переробить старі.
ENRICHMENT_VERSION = 1

_VALID_ENTITY_TYPES = ("person", "project", "org", "topic")


# ============================================================
# Нормалізація / дедуплікація
# ============================================================

def _normalize(name: str) -> str:
    """Канонічний ключ для дедупу: lowercase, прибрати пунктуацію по краях,
    схлопнути пробіли. Зберігає кирилицю/латиницю/цифри."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" \t\n\r.,;:!?\"'`«»()[]{}-–—")
    return s


def _count_mentions(text: str, names: list[str]) -> int:
    """Скільки разів сутність (canonical + aliases) згадана у тексті.
    Грубий case-insensitive підрахунок підрядків — для salience/ваги."""
    if not text or not names:
        return 0
    low = text.lower()
    total = 0
    for n in names:
        n = (n or "").strip().lower()
        if len(n) < 2:
            continue
        total += low.count(n)
    return total


# ============================================================
# Граф сутностей
# ============================================================

def _find_entity(c, etype: str, normalized: str) -> Optional[int]:
    """Знайти сутність за нормалізованим іменем (canonical або alias)."""
    if not normalized:
        return None
    row = c.execute(
        "SELECT id FROM entities WHERE type = ? AND normalized_name = ?",
        (etype, normalized),
    ).fetchone()
    if row:
        return row["id"]
    # через alias (alias унікальний глобально; перевіряємо що тип збігається)
    row = c.execute(
        "SELECT e.id FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
        "WHERE a.normalized_alias = ? AND e.type = ?",
        (normalized, etype),
    ).fetchone()
    if row:
        return row["id"]
    return _find_merged_entity(c, etype, normalized)


def _find_merged_entity(c, etype: str, normalized: str) -> Optional[int]:
    """Сутність, у яку вже злили рядок ЦЬОГО типу з таким написанням.

    Без цього кросс-типове злиття не «прилипає»: після merge org→project
    написання належить project, гілка з перевіркою типу не знаходить нічого, і
    наступний enrich створює org НАНОВО — причому з нулем аліасів, бо всі його
    `INSERT OR IGNORE` спотикаються об глобальний UNIQUE `normalized_alias`.
    Рядок воскресає недосяжним для пошуку за псевдонімом, а кожне нове
    написання плодить ще один.

    Дивимось саме на слід злиття (`metadata_json.merged_from`), а не просто на
    аліас без перевірки типу: інакше org «Ткачук» мовчки ставав би людиною
    Ткачук — тобто пари, які власник свідомо НЕ злив, схлопувались би самі.
    Розбір JSON у Python, бо на JSON1 в цьому проєкті не покладаємось.

    Збіг ТИПУ недостатній: рядок, що колись поглинув якийсь org, інакше
    привласнював би будь-який org, чиє написання випадково лежить серед його
    аліасів («BH» при поглинутому «Acmecorp Ltd»). Тому звіряємо ще й написання
    — зі списку `aliases` поглинутого або, для старих записів без нього, з його
    канонічним імʼям.
    """
    rows = c.execute(
        "SELECT e.id, e.metadata_json FROM entity_aliases a "
        "JOIN entities e ON e.id = a.entity_id "
        "WHERE a.normalized_alias = ? AND e.metadata_json IS NOT NULL",
        (normalized,),
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(meta, dict):
            continue
        for gone in meta.get("merged_from") or []:
            if not isinstance(gone, dict) or gone.get("type") != etype:
                continue
            spellings = gone.get("aliases")
            if not spellings:                      # записи до появи поля
                spellings = [_normalize(gone.get("name") or "")]
            if normalized in spellings:
                return row["id"]
    return None


def _upsert_entity(
    c,
    etype: str,
    name: str,
    role: Optional[str] = None,
    aliases: Optional[list[str]] = None,
) -> Optional[int]:
    """Знайти або створити сутність; додати aliases; оновити роль якщо була порожня.

    Returns entity_id або None якщо імʼя порожнє/невалідне.
    """
    if etype not in _VALID_ENTITY_TYPES:
        return None
    canonical = (name or "").strip()
    norm = _normalize(canonical)
    if not norm:
        return None

    aliases = aliases or []
    # Спроба знайти за canonical і за кожним alias
    entity_id = _find_entity(c, etype, norm)
    if entity_id is None:
        for a in aliases:
            entity_id = _find_entity(c, etype, _normalize(a))
            if entity_id is not None:
                break

    now = datetime.now().isoformat(timespec="seconds")
    if entity_id is None:
        cur = c.execute(
            "INSERT INTO entities (type, canonical_name, normalized_name, role, "
            "first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (etype, canonical, norm, (role or None), now, now),
        )
        entity_id = cur.lastrowid
    else:
        # Доповнити роль якщо її ще не було
        if role:
            existing = c.execute(
                "SELECT role FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if existing and not existing["role"]:
                c.execute(
                    "UPDATE entities SET role = ?, updated_at = ? WHERE id = ?",
                    (role, now, entity_id),
                )

    # Зареєструвати aliases (canonical теж як alias — для майбутніх lookup'ів)
    for a in [canonical, *aliases]:
        na = _normalize(a)
        if not na:
            continue
        c.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized_alias) "
            "VALUES (?, ?, ?)",
            (entity_id, a.strip(), na),
        )

    # Лінк person → speakers (голосовий відбиток), якщо є збіг по імені
    if etype == "person":
        sp = c.execute(
            "SELECT id FROM speakers WHERE name = ? COLLATE NOCASE LIMIT 1",
            (canonical,),
        ).fetchone()
        if sp:
            c.execute(
                "UPDATE entities SET speaker_id = ? WHERE id = ? AND speaker_id IS NULL",
                (sp["id"], entity_id),
            )

    return entity_id


def _recompute_entity_aggregates(c, entity_ids: set[int]) -> None:
    """Перерахувати mention_count / meeting_count з meeting_entities (idempotent)."""
    for eid in entity_ids:
        c.execute(
            "UPDATE entities SET "
            "mention_count = COALESCE((SELECT SUM(mention_count) FROM meeting_entities WHERE entity_id = ?), 0), "
            "meeting_count = COALESCE((SELECT COUNT(*) FROM meeting_entities WHERE entity_id = ?), 0), "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (eid, eid, eid),
        )


# ============================================================
# Diarized-текст для збагачення
# ============================================================

def _diarized_text_for(c, transcription_id: int, segments_json: Optional[str]) -> Optional[str]:
    """Зібрати 'Імʼя: репліка' формат із сегментів + speaker-map, якщо є діаризація."""
    if not segments_json:
        return None
    try:
        segments = json.loads(segments_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not segments or not any(s.get("speaker") for s in segments if isinstance(s, dict)):
        return None
    rows = c.execute(
        "SELECT m.raw_label, s.name FROM transcription_speaker_map m "
        "LEFT JOIN speakers s ON s.id = m.speaker_id WHERE m.transcription_id = ?",
        (transcription_id,),
    ).fetchall()
    speaker_map = {r["raw_label"]: r["name"] for r in rows if r["name"]}
    return text_polishing._format_diarized_input(segments, speaker_map)


# ============================================================
# Запис картки
# ============================================================

def _persist_card(c, transcription_id: int, card: dict, transcript_text: str,
                  created_at: Optional[str], existing_meeting_date: Optional[str]) -> None:
    """Записати результат extract_meeting_card у БД (у межах транзакції caller'а)."""
    now = datetime.now().isoformat(timespec="seconds")

    # --- summary_json у форматі існуючого summary-ендпоінта ---
    summary_payload = {
        "summary": card.get("summary", ""),
        "key_points": card.get("key_points", []),
        "action_items": card.get("action_items", []),
    }
    # --- topics_json у форматі існуючого topics-ендпоінта ---
    topics_payload = {"topics": card.get("topics", []), "model": card.get("model", "")}

    # meeting_date: зберегти існуючу, інакше — дата created_at
    meeting_date = existing_meeting_date
    if not meeting_date and created_at:
        meeting_date = str(created_at)[:10]

    c.execute(
        "UPDATE transcriptions SET summary_json = ?, summary_at = CURRENT_TIMESTAMP, "
        "summary_model = ?, topics_json = ?, enriched_at = CURRENT_TIMESTAMP, "
        "enriched_model = ?, enrichment_version = ?, meeting_date = ? WHERE id = ?",
        (
            json.dumps(summary_payload, ensure_ascii=False),
            card.get("model", ""),
            json.dumps(topics_payload, ensure_ascii=False),
            card.get("model", ""),
            ENRICHMENT_VERSION,
            meeting_date,
            transcription_id,
        ),
    )

    # --- сутності: зібрати старі лінки (для перерахунку), стерти, перезаписати ---
    old_links = c.execute(
        "SELECT entity_id FROM meeting_entities WHERE transcription_id = ?",
        (transcription_id,),
    ).fetchall()
    affected = {r["entity_id"] for r in old_links}

    c.execute("DELETE FROM meeting_entities WHERE transcription_id = ?", (transcription_id,))
    c.execute("DELETE FROM action_items WHERE transcription_id = ?", (transcription_id,))

    # person / project / org → entities + meeting_entities
    person_index: dict[str, int] = {}  # normalized canonical/alias → entity_id (для resolve owner)

    def _ingest(items, etype):
        for item in items:
            if isinstance(item, str):
                name, role, aliases = item, None, []
            elif isinstance(item, dict):
                name = item.get("name") or ""
                role = item.get("role")
                aliases = item.get("aliases") or []
            else:
                continue
            eid = _upsert_entity(c, etype, name, role=role, aliases=aliases)
            if eid is None:
                continue
            affected.add(eid)
            mentions = _count_mentions(transcript_text, [name, *aliases]) or 1
            c.execute(
                "INSERT OR REPLACE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, role_in_meeting) "
                "VALUES (?, ?, ?, ?)",
                (transcription_id, eid, mentions, role),
            )
            if etype == "person":
                person_index[_normalize(name)] = eid
                for a in aliases:
                    person_index[_normalize(a)] = eid

    _ingest(card.get("people", []), "person")
    _ingest(card.get("projects", []), "project")
    _ingest(card.get("orgs", []), "org")
    # topics теж як сутності (для уніфікованих сторінок сутностей / фасетного пошуку)
    _ingest([{"name": t} for t in card.get("topics", [])], "topic")

    # --- action_items → нормалізована таблиця, owner resolve до person-сутності ---
    for ai in card.get("action_items", []):
        if not isinstance(ai, dict):
            continue
        task = (ai.get("task") or "").strip()
        if not task:
            continue
        owner_name = ai.get("owner")
        owner_eid = person_index.get(_normalize(owner_name)) if owner_name else None
        if owner_name and owner_eid is None:
            owner_eid = _find_entity(c, "person", _normalize(owner_name))
        # Трек 1: дедлайн зберігаємо і сирою фразою (due), і нормалізованою датою.
        # Пріоритет — due_date від моделі (вона бачила контекст розмови), але
        # тільки якщо це справді ISO-дата; інакше рахуємо парсером від дати
        # зустрічі. Так поле не залежить від того, чи послухалась модель.
        raw_due = ai.get("due")
        due_date, due_prec = commitments.parse_due(raw_due, meeting_date)
        model_due = (ai.get("due_date") or "").strip() if isinstance(ai.get("due_date"), str) else ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", model_due):
            due_date, due_prec = model_due, (due_prec or commitments.P_DAY)
        c.execute(
            "INSERT INTO action_items (transcription_id, task, owner_name, "
            "owner_entity_id, due, due_date, due_precision, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (transcription_id, task, owner_name, owner_eid, raw_due, due_date, due_prec),
        )

    _recompute_entity_aggregates(c, affected)


# ============================================================
# Публічний API
# ============================================================

def is_available() -> bool:
    """Card-збагачення можливе лише з налаштованим Claude API ключем."""
    return text_polishing.is_available()


def any_available() -> bool:
    """Чи є сенс запускати збагачення взагалі: Claude (card) АБО локальні embeddings."""
    return text_polishing.is_available() or embeddings.is_available()


def enrich_transcription(
    db_path: str,
    transcription_id: int,
    *,
    model: Optional[str] = None,
    force: bool = False,
    effort: str = "medium",
) -> dict:
    """Повне збагачення транскрипту = card (Claude) + embeddings (локально).

    Дві НЕЗАЛЕЖНІ фази з окремою idempotency:
    - card: сутності/summary/action items через Claude (enriched_at маркер).
    - embed: чанки + вектори локально (embedded_at маркер) — працює без API key.

    effort: 'low' для масового backfill (×3 швидше/дешевше), 'medium' для
    живої авто-транскрипції (якість).

    Returns {"transcription_id", "status", "card": {...}, "embed": {...}}.
    status: "done" якщо хоч одна фаза щось зробила, інакше "skipped".
    """
    with get_db_connection(db_path) as conn:
        dup_row = tx_repo.get_by_id(conn, transcription_id, columns=("id", "duplicate_of"))
    if dup_row and dup_row["duplicate_of"] is not None:
        # Ремонт 3 (review #3): дублі (dedup_audio.py) не отримують ані картку
        # Claude, ані чанки/ембеддинги — той самий early-exit і те саме слово
        # "skipped_duplicate", яким інжест (transcription.py) вже позначає
        # enrichment у відповіді /api/transcribe.
        skip = {"status": "skipped_duplicate", "transcription_id": transcription_id}
        return {"transcription_id": transcription_id, "status": "skipped_duplicate",
                "card": skip, "embed": skip}

    card_res = _enrich_card(db_path, transcription_id, model=model, force=force, effort=effort) \
        if text_polishing.is_available() else {"status": "unavailable"}

    embed_res = embeddings.chunk_and_embed_transcription(db_path, transcription_id, force=force) \
        if embeddings.is_available() else {"status": "unavailable"}

    if card_res.get("status") == "not_found" and embed_res.get("status") == "not_found":
        return {"transcription_id": transcription_id, "status": "not_found",
                "card": card_res, "embed": embed_res}
    did_work = card_res.get("status") == "enriched" or embed_res.get("status") == "embedded"
    return {
        "transcription_id": transcription_id,
        "status": "done" if did_work else "skipped",
        "card": card_res,
        "embed": embed_res,
    }


def _enrich_card(
    db_path: str,
    transcription_id: int,
    model: Optional[str] = None,
    force: bool = False,
    effort: str = "medium",
) -> dict:
    """Card-фаза: сутності/summary/action items через Claude. Idempotent (enriched_at).

    Returns {"status": "enriched"|"skipped"|"not_found"|"empty", ...}.
    """
    # --- READ (коротке зʼєднання, до мережевого виклику) ---
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        row = tx_repo.get_by_id(
            conn, transcription_id,
            columns=("id", "transcript_text", "polished_text", "segments", "enriched_at",
                     "enrichment_version", "created_at", "meeting_date", "source_type"),
        )
        if not row:
            return {"status": "not_found", "transcription_id": transcription_id}

        # Phase 17: Telegram іде embed-only — Claude-картка на чатовому
        # однорядковику («Ок», «Дякую») марна, а на живих даних ще й платна.
        # Рішення діяло ЛИШЕ на шляху інжесту (telegram.py:_submit_embed_only),
        # а backfill і кнопка індексера заходили з чорного ходу: тут не було
        # жодної перевірки source_type. force=True лишає ручний обхід, а Волна 5
        # замінить це на вибірковий фільтр (маркери обіцянки + довжина).
        if row["source_type"] == "telegram" and not force:
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "telegram_embed_only"}

        already = bool(row["enriched_at"]) and (row["enrichment_version"] == ENRICHMENT_VERSION)
        if already and not force:
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "already_enriched"}

        body_text = (row["polished_text"] or row["transcript_text"] or "")
        if not body_text.strip():
            return {"status": "empty", "transcription_id": transcription_id}

        diarized = _diarized_text_for(c, transcription_id, row["segments"])
        created_at = row["created_at"]
        existing_meeting_date = row["meeting_date"]

    # --- Claude call (поза БД-зʼєднанням — довга мережева операція) ---
    # T6.3: раніше НЕ було try/except тут — збій одного транскрипту (мережа,
    # некоректний JSON від моделі, вичерпані retry на транзиентних помилках)
    # прокидався з _enrich_card() і рвав УСЮ пару фаз enrich_transcription()
    # (embed-фаза, яка НЕ залежить від Claude, теж не встигала виконатись).
    # М'яка деградація: позначаємо "retry_needed" замість жорсткого падіння —
    # виклик embed-фази і решта backfill-проходу тривають.
    try:
        card = text_polishing.extract_meeting_card(
            body_text, diarized_text=diarized, model=model, effort=effort,
            meeting_date=(existing_meeting_date or (str(created_at)[:10] if created_at else None)),
        )
    except Exception as e:
        logger.warning("[enrich] tx=%s card-фаза збій (retry_needed): %s", transcription_id, e)
        return {"status": "retry_needed", "transcription_id": transcription_id,
                "error": str(e)}

    # --- WRITE ---
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        _persist_card(c, transcription_id, card, body_text, created_at, existing_meeting_date)
        conn.commit()

    logger.info(
        "[enrich] tx=%s done: people=%d projects=%d orgs=%d topics=%d actions=%d "
        "(in=%d cache_read=%d out=%d)",
        transcription_id, len(card.get("people", [])), len(card.get("projects", [])),
        len(card.get("orgs", [])), len(card.get("topics", [])),
        len(card.get("action_items", [])), card.get("input_tokens", 0),
        card.get("cache_read_tokens", 0), card.get("output_tokens", 0),
    )
    return {
        "status": "enriched",
        "transcription_id": transcription_id,
        "model": card.get("model", ""),
        "counts": {
            "people": len(card.get("people", [])),
            "projects": len(card.get("projects", [])),
            "orgs": len(card.get("orgs", [])),
            "topics": len(card.get("topics", [])),
            "action_items": len(card.get("action_items", [])),
        },
        "input_tokens": card.get("input_tokens", 0),
        "output_tokens": card.get("output_tokens", 0),
        "cache_read_tokens": card.get("cache_read_tokens", 0),
    }


def list_unenriched_ids(db_path: str, limit: Optional[int] = None) -> list[int]:
    """ID транскриптів, яким потрібна робота: card (Claude) АБО embed (вектори) ще
    не зроблено / застаріло. Над-вибірка безпечна — фази skip'аються по-окремо."""
    with get_db_connection(db_path) as conn:
        # T4.6: не палимо Claude/embedding-токени на soft-deleted (undo-вікно
        # ≠ дійсний контент; такі рядки й так скоро прибере grace-purge).
        # Telegram потрапляє сюди ЛИШЕ через embed-фазу: картка йому не належить
        # (див. _enrich_card), тож без цієї умови індексер рахував би 3142
        # чатових повідомлення як «роботу» і показував чесному користувачу
        # обсяг, якого не існує. `IS NOT` — NULL-safe (source_type може бути NULL).
        # T6.5: embed-фаза застаріває не лише від зміни МОДЕЛІ, а й від зміни
        # логіки чанкінгу — а це видно тільки по embedding_version. Без цієї
        # умови бамп EMBED_VERSION лишався б декларацією: chunk_and_embed
        # перерахував би запис, але список роботи його б не назвав, тож ніхто
        # б не покликав. Умова дзеркалить idempotency-перевірку в
        # embeddings.chunk_and_embed_transcription (NULL = стара нарізка).
        sql = (
            "SELECT id FROM transcriptions WHERE deleted_at IS NULL "
            "AND duplicate_of IS NULL AND ("
            "(source_type IS NOT 'telegram' AND (enriched_at IS NULL OR "
            " enrichment_version IS NULL OR enrichment_version < ?)) "
            "OR embedded_at IS NULL OR embedding_model IS NULL OR embedding_model != ? "
            "OR embedding_version IS NULL OR embedding_version != ?) "
            "ORDER BY id"
        )
        params: list = [ENRICHMENT_VERSION, embeddings.EMBED_MODEL, embeddings.EMBED_VERSION]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [r["id"] for r in conn.execute(sql, params).fetchall()]


def backfill(
    db_path: str,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    effort: str = "medium",
    progress_cb: Optional[Callable[[dict], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> dict:
    """Прогнати збагачення по всій історії (як /indexer у meeting_archive).

    Idempotent — пропускає вже збагачені (якщо force=False). Помилка на одному
    транскрипті не валить увесь прохід (логується, лічиться у failed).
    """
    if force:
        with get_db_connection(db_path) as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM transcriptions WHERE deleted_at IS NULL "
                "AND duplicate_of IS NULL ORDER BY id"
            ).fetchall()]
        if limit:
            ids = ids[:limit]
    else:
        ids = list_unenriched_ids(db_path, limit=limit)

    total = len(ids)
    done = failed = skipped = 0
    logger.info("[backfill] start: %d транскриптів (force=%s)", total, force)

    for i, tid in enumerate(ids):
        if cancel_cb and cancel_cb():
            logger.info("[backfill] скасовано на %d/%d", i, total)
            break
        try:
            res = enrich_transcription(db_path, tid, model=model, force=force, effort=effort)
            if res["status"] == "done":
                done += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            logger.error("[backfill] tx=%s failed: %s", tid, e, exc_info=True)
        if progress_cb:
            progress_cb({
                "processed": i + 1, "total": total,
                "done": done, "skipped": skipped, "failed": failed,
                "current_id": tid,
            })

    if done:
        optimize_chunk_index(db_path)

    result = {"total": total, "done": done, "skipped": skipped, "failed": failed}
    logger.info("[backfill] завершено: %s", result)
    return result


def optimize_chunk_index(db_path: str) -> None:
    """Злити сегменти FTS5-індексу чанків після масової перезаписи.

    Кожен ембединг робить DELETE+INSERT чанків запису, а тригери
    `chunks_ai/ad/au` пишуть у `chunks_fts` — тисяча таких транзакцій лишає
    індекс покришеним на сегменти. Ціна виміряна на живому архіві після
    re-embed записів (T6.5): `_fts_search` — **64с** проти 9с, тобто
    пошук ставав непридатним саме після проходу, який мав його покращити.
    Сам `optimize` коштує ~0.4с і зводить 7797 сегментів до 2287.
    Помилка тут не має валити backfill: індекс лишиться робочим, лише
    повільним."""
    try:
        with get_db_connection(db_path) as conn:
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            conn.commit()
        logger.info("[backfill] chunks_fts optimize виконано")
    except Exception:
        logger.warning("[backfill] chunks_fts optimize не вдався — пошук лишиться "
                       "повільним до наступного проходу", exc_info=True)


# ============================================================
# CLI: backfill-cards (Волна B production-rag) — лише card-фаза
# ============================================================
#
# `backfill()`/`list_unenriched_ids()` вище лишаються як є (HTTP-шлях через
# job_queue) — цей CLI офлайн, і бере записи вужче: лише картка (без
# embed-фази), лише ті, кому вона взагалі бракує (`summary_json IS NULL`),
# і з ручним вибором моделі — дефолт `models.get_default_model()` (Opus 5)
# дорогий на великих архівах документів.

#: --kind → source_type. 'all' лишає лише базовий фільтр (не Telegram).
_KIND_SOURCE_TYPES = {
    "calls": ("file", "youtube", "recording"),
    "docs": ("document",),
}


def _select_backfill_card_rows(db_path: str, kind: str = "all",
                               limit: Optional[int] = None) -> list[dict]:
    """Записи без картки: не Telegram (embed-only, T Волна 0), не soft-deleted,
    не дублі (dedup_audio). `kind` звужує до source_type."""
    sql = ("SELECT id, source_type, source_name, transcript_text, polished_text "
           "FROM transcriptions WHERE summary_json IS NULL "
           "AND source_type IS NOT 'telegram' AND deleted_at IS NULL "
           "AND duplicate_of IS NULL")
    params: list = []
    if kind != "all":
        types = _KIND_SOURCE_TYPES.get(kind)
        if not types:
            raise ValueError(f"невідомий --kind: {kind!r}")
        sql += f" AND source_type IN ({','.join('?' * len(types))})"
        params.extend(types)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _dry_run_report(rows: list[dict], model: str) -> dict:
    """Оцінка обсягу/вартості без жодного запису. tokens ≈ символи/4 (грубо,
    без реального токенайзера); вартість — лише вхід (вихід невідомий заздалегідь)."""
    lengths = [
        (r["id"], len(r.get("polished_text") or r.get("transcript_text") or ""))
        for r in rows
    ]
    total_chars = sum(n for _, n in lengths)
    tokens_est = total_chars // 4
    cost_est = pricing.estimate_cost(model, tokens_est, 0)
    top10 = sorted(lengths, key=lambda t: -t[1])[:10]
    return {
        "dry_run": True, "count": len(rows), "model": model,
        "total_chars": total_chars, "tokens_est": tokens_est,
        "cost_est_usd_input_only": cost_est,
        "top10": [{"id": tid, "chars": n} for tid, n in top10],
    }


def backfill_cards(db_path: str, model: Optional[str] = None,
                   limit: Optional[int] = None, kind: str = "all") -> dict:
    """Реальний прохід CLI: лише card-фаза (`_enrich_card`), НЕ embed.

    Idempotent через саму вибірку (`summary_json IS NULL`) — повторний запуск
    без нових записів обробляє 0. Збій Claude на одному записі не валить прохід:
    `_enrich_card` вже ловить його і повертає "retry_needed" (лишається в
    вибірці для наступного проходу), тут ловимо лише неочікувані помилки.
    """
    rows = _select_backfill_card_rows(db_path, kind=kind, limit=limit)
    total = len(rows)
    done = skipped = failed = 0
    logger.info("[backfill-cards] start: %d записів (kind=%s, model=%s)",
                total, kind, model or models.get_default_model())
    for r in rows:
        try:
            res = _enrich_card(db_path, r["id"], model=model)
            if res.get("status") == "enriched":
                done += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            logger.error("[backfill-cards] tx=%s failed: %s", r["id"], e, exc_info=True)
    result = {"total": total, "done": done, "skipped": skipped, "failed": failed}
    logger.info("[backfill-cards] завершено: %s", result)
    return result


def _print_dry_run_report(report: dict) -> None:
    print(f"Записів без картки: {report['count']}")
    print(f"Символів разом: {report['total_chars']}")
    print(f"Оцінка токенів (символи/4): {report['tokens_est']}")
    print(f"Оцінка вартості лише за вхід ({report['model']}): "
          f"${report['cost_est_usd_input_only']:.4f}")
    print("Топ-10 найдовших:")
    for item in report["top10"]:
        print(f"  #{item['id']}: {item['chars']} символів")


def _cmd(args) -> int:
    if args.command != "backfill-cards":  # pragma: no cover
        return 2
    eff_model = args.model or models.get_default_model()
    if args.dry_run:
        rows = _select_backfill_card_rows(args.db, kind=args.kind, limit=args.limit)
        _print_dry_run_report(_dry_run_report(rows, eff_model))
        return 0
    result = backfill_cards(args.db, model=args.model, limit=args.limit, kind=args.kind)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(
        prog="enrichment",
        description="Офлайн-бекфіл карток (summary/entities/action items) "
                    "дзвінків і документів — БЕЗ embed-фази.")
    p.add_argument("--db", default=default_db)
    # --dry-run має рятувати з будь-якого місця рядка (як у dedup_audio.py):
    # верхній флаг у окремий dest, фінальне значення — OR обох.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Нічого не писати, лише оцінка обсягу/вартості")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="Нічого не писати, лише оцінка обсягу/вартості")
    common.add_argument("--model", default=None,
                        help="Override моделі Claude (дефолт — models.get_default_model())")
    common.add_argument("--limit", type=int, default=None)
    common.add_argument("--kind", choices=("calls", "docs", "all"), default="all",
                        help="calls=file/youtube/recording, docs=document, all=обидва")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "backfill-cards", parents=[common],
        help="Картка Claude для записів без summary_json (не Telegram/дублі/видалені)")

    args = p.parse_args(argv)
    args.dry_run = args.dry_run or args.dry_run_pre
    return _cmd(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
