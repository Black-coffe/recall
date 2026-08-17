"""Entity dedup — offline embedding-based fuzzy-merge (Wave 4, T6.7).

**Проблема.** `enrichment._normalize`/`_find_entity`/`_upsert_entity` дедуплікують
сутності (`entities`) лише за точним нормалізованим рядком (lowercase + обрізана
пунктуація) + явними aliases, які дав Claude в одному виклику. Тому «Олена
Петренко» на одному дзвінку і «Олена» / «Петренко» на іншому — без явного
aliasing — лишаються ОКРЕМИМИ сутностями: граф (головна фіча RAG — «хто що
обіцяв») засмічується дублями, а mention_count/meeting_count розмазуються між
кількома id замість накопичення на одній.

**Рішення тут — periodic OFFLINE-прохід (НЕ інлайн у enrich, щоб не гальмувати
основний шлях збагачення):**

1. `find_merge_candidates()` — для кожного `type` окремо (person/project/org/
   topic НЕ змішуються) рахує embedding canonical-імен (переюзає
   `app.services.embeddings.embed_texts` — той самий e5-ембеддер, що і чанки
   транскриптів; НІЯКОЇ нової моделі не вантажиться), рахує попарну
   cosine-схожість (вектори з `embed_texts` вже L2-нормалізовані → cosine =
   dot product) і повертає пари понад поріг. **Тільки читає БД, нічого не
   змінює.**
2. `merge_entities(keep_id, merge_id)` — явний merge ОДНІЄЇ конкретної пари.
   Викликається лише після підтвердження (людина переглянула кандидатів).
   НЕ автоматичний: жодна функція тут сама не вирішує «злити», лише виконує
   вже підтверджений merge. Той самий принцип, що м'який merge спікерів T4.6
   (Волна 2) — фальшивий automerge ризикує зіллити різних людей.

**Ідемпотентність.** `merge_entities` фізично видаляє `merge_id` з `entities`
(FK `ON DELETE CASCADE` підчищає залишкові `entity_aliases`/`meeting_entities`
цієї сутності). Повторний прохід `find_merge_candidates` просто більше не
побачить вже злиту сутність кандидатом — вона видалена. Повторний
`merge_entities` з тим самим (вже неіснуючим) `merge_id` — no-op
(`{"status": "not_found"}`), нічого не ламає.

**Точка виклику — CLI, окремо від app.py/enrich (НЕ інлайн):**

    .venv/Scripts/python.exe -m app.services.entity_dedup list
    .venv/Scripts/python.exe -m app.services.entity_dedup list --type person --threshold 0.9
    .venv/Scripts/python.exe -m app.services.entity_dedup list --json
    .venv/Scripts/python.exe -m app.services.entity_dedup merge 12 47        # питає y/n
    .venv/Scripts/python.exe -m app.services.entity_dedup merge 12 47 --yes  # без інтерактиву

**Свідомо НЕ підключено як MCP-тулза в цій хвилі** (нотатка, не забування):
(a) власник задав MCP read-first — нові WRITE-тулзи не додаються, а
`merge_entities` є write-дією; (b) `mcp_server.py` принципово не імпортує
torch/embeddings у своєму процесі (memory: mcp-stdio-no-heavy-models) —
коректний read-тулз для пошуку кандидатів вимагав би або порушення цього
правила, або нового `app.py`/blueprint-проксі-ендпоінта (територія іншого
воркера цієї хвилі, T7.4). CLI лишається основною точкою виклику для T6.7;
MCP/HTTP-обгортку можна додати окремою карткою пізніше без зміни цього модуля.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from typing import Optional

from app.db.connection import get_db_connection
from app.services import embeddings
from app.services.enrichment import (
    _VALID_ENTITY_TYPES,
    _normalize,
    _recompute_entity_aggregates,
)

logger = logging.getLogger(__name__)

# Поріг cosine-схожості e5-ембедингів canonical-імен для кандидата на merge.
# Емпіричний старт (типові e5 sentence-similarity пороги — 0.85-0.90 для
# "той самий референт, інше формулювання"); свідомо консервативний (вище —
# менше false positives), бо merge підтверджується людиною, а не автоматом.
DEFAULT_THRESHOLD = 0.86


# ============================================================
# 1. Пошук кандидатів (read-only)
# ============================================================

_NAME_PUNCT = re.compile(r"[\s\-_.'`ʼ’«»\"]+")

# Порядок «сили» типу при виборі keep. Вирішує видимість, а не важливість:
# `scope.resolve_scope` бере project|org|person, тож рядок, що став `topic`,
# випадає зі зрізу за проєктом, скільки б звʼязків він не мав.
_TYPE_RANK = {"person": 0, "project": 1, "org": 2, "topic": 3}


def _fold_name(name: Optional[str]) -> str:
    """Ключ згортання написання. Рахуємо в Python: SQLite LOWER не чіпає кирилицю."""
    return _NAME_PUNCT.sub("", (name or "").casefold().strip())


def find_cross_type_twins(db_path: str, *, include_person: bool = False) -> list[dict]:
    """Точні тезки, розкидані по РІЗНИХ типах — без моделі й без порогу.

    `find_merge_candidates` порівнює лише в межах типу, тому головного класу
    дублів не бачить: збагачення заводить одну річ окремо в кожному типі
    («Acmecorp» як project і як org). Тут збігається все написання, тож здогаду
    немає і ембединги не потрібні.

    `keep` обирається СПОЧАТКУ за типом, і лише потім за кількістю звʼязків.
    Звʼязки все одно переїжджають на keep, тож вибір впливає тільки на тип — а
    тип вирішує видимість: `scope.resolve_scope` бере `project|org|person` і не
    бачить `topic`. Коли keep обирався за звʼязками, сім проєктів на живому
    архіві потрапили в topic і зникли з `ask_archive(project=…)` та копілота.

    Пари «людина + не-людина» звичайний прохід не бере (`merge` вимагає для них
    окремого `--allow-person` після перевірки очима), але й решту групи вони не
    ховають: з групи {person Ткачук, org Ткачук, project Ткачук} лишається
    пара org+project, яку злити можна й треба.
    """
    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT id, type, canonical_name FROM entities").fetchall()
        links = {r["id"]: r["n"] for r in conn.execute(
            "SELECT me.entity_id id, COUNT(DISTINCT me.transcription_id) n "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.deleted_at IS NULL GROUP BY me.entity_id")}

    groups: dict[str, list] = {}
    for r in rows:
        k = _fold_name(r["canonical_name"])
        if k:
            groups.setdefault(k, []).append(r)

    out: list[dict] = []
    for members in groups.values():
        if not include_person and any(r["type"] == "person" for r in members):
            # Викидаємо саме людські рядки, а не всю групу: інакше «Ткачук»
            # ховав би пару org+project, яку злити можна.
            members = [r for r in members if r["type"] != "person"]
        types = {r["type"] for r in members}
        if len(members) < 2 or len(types) < 2:
            continue
        ordered = sorted(members, key=lambda r: (_TYPE_RANK.get(r["type"], 9),
                                                 -links.get(r["id"], 0)))
        out.append({
            "keep": {"id": ordered[0]["id"], "type": ordered[0]["type"],
                     "name": ordered[0]["canonical_name"],
                     "links": links.get(ordered[0]["id"], 0)},
            "merge": [{"id": r["id"], "type": r["type"], "name": r["canonical_name"],
                       "links": links.get(r["id"], 0)} for r in ordered[1:]],
        })
    out.sort(key=lambda g: -g["keep"]["links"])
    return out


def find_merge_candidates(
    db_path: str,
    etype: Optional[str] = None,
    threshold: float = DEFAULT_THRESHOLD,
    limit: Optional[int] = None,
) -> list[dict]:
    """Кандидати на merge за embedding-схожістю canonical-імен, У МЕЖАХ ОДНОГО
    `type` (person з person, project з project, ...). Нічого не пише в БД.

    Returns: список {"type","id1","name1","id2","name2","similarity"},
    відсортований за similarity desc (найімовірніші дублі — першими).
    """
    if not embeddings.is_available():
        raise RuntimeError(
            f"embeddings недоступні: {embeddings.unavailability_reason()}"
        )

    types = [etype] if etype else list(_VALID_ENTITY_TYPES)
    candidates: list[dict] = []

    with get_db_connection(db_path) as conn:
        for t in types:
            rows = conn.execute(
                "SELECT id, canonical_name FROM entities WHERE type = ? ORDER BY id",
                (t,),
            ).fetchall()
            if len(rows) < 2:
                continue
            ids = [r["id"] for r in rows]
            names = [r["canonical_name"] for r in rows]
            vecs = embeddings.embed_texts(names)  # [N, dim], L2-нормалізовані
            if vecs.shape[0] != len(ids):
                logger.warning("[entity_dedup] embed_texts повернув %d векторів на %d імен (type=%s) — пропуск",
                               vecs.shape[0], len(ids), t)
                continue
            sims = vecs @ vecs.T  # cosine, бо нормалізовані
            n = len(ids)
            for i in range(n):
                for j in range(i + 1, n):
                    sim = float(sims[i, j])
                    if sim >= threshold:
                        candidates.append({
                            "type": t,
                            "id1": ids[i], "name1": names[i],
                            "id2": ids[j], "name2": names[j],
                            "similarity": round(sim, 4),
                        })

    candidates.sort(key=lambda c: c["similarity"], reverse=True)
    if limit:
        candidates = candidates[:limit]
    return candidates


# ============================================================
# 2. Merge (explicit, підтверджуваний)
# ============================================================

def _first_token_person_twin(c, gone, *, exclude_id: int):
    """Однослівний person-тезка, якому дістануться майбутні задачі зниклого імені.

    Дзеркалить фолбек `commitments.link_owners`: «Юля Гончар» без свого рядка
    шукається як «юля». Тут те саме, але наперед — щоб побачити шкоду ДО злиття,
    а не через місяць у зводі.
    """
    first = (gone["normalized_name"] or "").split(" ")[0]
    if not first or first == gone["normalized_name"]:
        return None                      # однослівне імʼя фолбеком і так не ловиться
    return c.execute(
        "SELECT id, type, canonical_name FROM entities "
        "WHERE type = 'person' AND normalized_name = ? AND id <> ?",
        (first, exclude_id)).fetchone()


def merge_entities(db_path: str, keep_id: int, merge_id: int,
                   allow_cross_type: bool = False,
                   allow_person: bool = False) -> dict:
    """Явний merge ОДНІЄЇ підтвердженої пари: `merge_id` зникає, `keep_id`
    успадковує aliases, meeting-звʼязки (mention_count СУМУЄТЬСЯ при
    перетині на тому самому мітингу, salience — max), action_items, і
    role/description/speaker_id якщо в keep їх ще не було. Агрегати
    (`mention_count`/`meeting_count`) на `keep_id` перераховуються з нуля
    через `enrichment._recompute_entity_aggregates` (ідемпотентно).

    `allow_cross_type` знімає заборону на різні типи. Заборона правильна для
    ембединг-кандидатів (там схожість імен нічого не каже про природу речі), але
    сліпа до головного класу дублів у цьому архіві: збагачення заводить ОДНУ річ
    окремо в кожному типі, тому «Acmecorp» живе як project і як org. Таких груп
    133 із 152, і жодну з них цей merge без прапорця не бере. Тип лишається від
    `keep_id` — тому keep треба обирати за кількістю звʼязків, а не за id.

    При кросс-типовому злитті mention_count на СПІЛЬНІЙ зустрічі береться як
    MAX, а не сума: це те саме імʼя в тому самому тексті, на яке дивляться два
    рядки графа, тож додавання рахувало б ті самі згадки двічі. У межах одного
    типу поведінка не змінюється — там сума лишається сумою.

    `allow_person` знімає заборону «людина + не-людина» — і тільки після того,
    як пару подивилися очима. Заборона стоїть тому, що більшість таких пар —
    РІЗНІ речі під одним написанням (біблійна Єва і фірма «Єва»), але меншість
    справді одна: агенція, названа прізвищем власника («Ткачук»), бригада,
    заведена і як людина, і як організація. Прапорець не вимикає захист, а
    переносить його на перевірні умови: людський рядок, який ЗНИКАЄ, не сміє
    мати ні задач, ні привʼязаного спікера — саме через них ішла шкода
    (`link_owners`, `speakers._entity_index`). Написання, які при цьому вперше
    входять у person-індекс, повертаємо в `person_index_added`, щоб рішення було
    видно в логу, а не лише в наслідках.

    Returns {"status": "merged"|"not_found"|"error", ...}.
    """
    if keep_id == merge_id:
        return {"status": "error", "error": "keep_id == merge_id"}

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        keep = c.execute("SELECT * FROM entities WHERE id = ?", (keep_id,)).fetchone()
        merge = c.execute("SELECT * FROM entities WHERE id = ?", (merge_id,)).fetchone()
        if not keep or not merge:
            return {"status": "not_found", "keep_id": keep_id, "merge_id": merge_id}
        cross_type = keep["type"] != merge["type"]
        if cross_type and not allow_cross_type:
            return {"status": "error",
                    "error": f"type mismatch: keep={keep['type']} merge={merge['type']}"}
        # Людина і не-людина не зливаються НІКОЛИ, навіть із прапорцем. «Ткачук»
        # — і людина, і агентство; «Вербич» — і людина, і бренд. Крім того, обидва
        # напрями псують person-індекси: імʼя проєкту, ставши аліасом людини,
        # прив'язує спікера до неспівпадаючої сутності (`speakers._entity_index`),
        # а імʼя людини, поїхавши в project, зникає з індексу `link_owners` —
        # і задачі тієї людини лишаються без власника.
        report_person_index = False
        if cross_type and "person" in (keep["type"], merge["type"]):
            if not allow_person:
                return {"status": "error",
                        "error": (f"person merge refused: keep={keep['type']} "
                                  f"merge={merge['type']} — злиття людини з не-людиною "
                                  f"робиться лише вручну, після перевірки очима")}
            if merge["type"] == "person":
                # Людський рядок зникає: перевіряємо саме те, що ламалося раніше.
                owned = c.execute(
                    "SELECT COUNT(*) FROM action_items WHERE owner_entity_id = ?",
                    (merge_id,)).fetchone()[0]
                if owned or merge["speaker_id"] is not None:
                    return {"status": "error",
                            "error": (f"person merge refused: #{merge_id} має "
                                      f"задач {owned} і speaker_id="
                                      f"{merge['speaker_id']} — вони втратять "
                                      f"людину. Спершу перевісь їх на keep")}
                # Порожній зараз рядок ще не означає, що шкоди не буде ПОТІМ:
                # імʼя виходить із person-індексу `link_owners`, і наступна задача
                # «Вербич Петренко» піде фолбеком по першому токену — просто до
                # однослівного тезки. Це той самий дефект, через який 43 задачі
                # власника поїхали під чужим імʼям, і `relink` його не лікує
                # (його індекс теж person-only). Тому при живому тезці — відмова.
                twin = _first_token_person_twin(c, merge, exclude_id=keep_id)
                if twin:
                    return {"status": "error",
                            "error": (f"person merge refused: імʼя '{merge['canonical_name']}' "
                                      f"вийде з person-індексу, і майбутні задачі з ним "
                                      f"фолбек по першому токену віддасть тезці "
                                      f"[{twin['type']}] #{twin['id']} '{twin['canonical_name']}'. "
                                      f"Спершу прибери тезку або перейменуй")}
            else:
                # Людський рядок лишається: чужі написання ВХОДЯТЬ у person-індекс.
                # Рахуємо ПІСЛЯ переносу — див. нижче: частина написань може
                # лишитись у третьої сутності (normalized_alias UNIQUE глобально),
                # і звіт про «увійшло» був би неправдою.
                report_person_index = True

        now = datetime.now().isoformat(timespec="seconds")

        # Написання поглинутого рядка збираємо ДО переносу аліасів — після нього
        # вони вже належать keep. Вони потрібні «липкості» злиття
        # (`enrichment._find_merged_entity`), яка звіряє саме написання: без них
        # достатньо було збігу ТИПУ, і чужа сутність того ж типу мовчки
        # поглиналась при наступному enrich.
        gone_aliases = sorted({merge["normalized_name"]} | {
            r["normalized_alias"] for r in c.execute(
                "SELECT normalized_alias FROM entity_aliases WHERE entity_id = ?",
                (merge_id,))})

        # --- aliases: усі наявні aliases merge_id -> aliases keep_id, + canonical_name
        # merge_id теж стає alias'ом keep_id. UPDATE OR IGNORE (не DELETE+INSERT):
        # normalized_alias UNIQUE глобально — якщо keep_id вже має той самий alias
        # (інший entity_id), той конкретний рядок лишається на merge_id і буде
        # прибраний нижче через CASCADE при видаленні entities-рядка merge_id —
        # без втрати інформації, бо keep вже має цей alias.
        c.execute(
            "UPDATE OR IGNORE entity_aliases SET entity_id = ? WHERE entity_id = ?",
            (keep_id, merge_id),
        )
        c.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized_alias) "
            "VALUES (?, ?, ?)",
            (keep_id, merge["canonical_name"], merge["normalized_name"]),
        )

        person_index_added: list[str] = []
        if report_person_index:
            # Тільки те, що РЕАЛЬНО осіло в keep: із гонки за глобально унікальне
            # написання поглинутий рядок міг вийти з порожніми руками.
            person_index_added = [
                r["alias"] for r in c.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_id = ? "
                    "AND normalized_alias IN "
                    f"({', '.join('?' * len(gone_aliases))})",
                    (keep_id, *gone_aliases))]

        # --- meeting_entities: перенести з сумуванням при конфлікті на тому ж мітингу ---
        # `source` переносимо разом із рядком: `tg_entities` чистить СВОЇ звʼязки
        # запитом `DELETE … WHERE source = ?`, і рядок, що втратив походження,
        # переживає перебудову як привид, а `tg_entities.stats()` зараховує його
        # Клоду. На архіві, де 88% — Telegram, це не дрібниця.
        me_rows = c.execute(
            "SELECT transcription_id, mention_count, salience, role_in_meeting, source "
            "FROM meeting_entities WHERE entity_id = ?",
            (merge_id,),
        ).fetchall()
        # MAX замість суми — коли написання ОДНЕ й те саме, просто заведене двічі
        # («Acmecorp»[project] і «AcmeCorp»[org]): у тексті це одні й ті самі
        # слова, на які дивляться два рядки графа. Вирішує саме збіг імені, а не
        # факт різних типів: «Acmecorp» і «Acmecorp Ltd» — різні рядки, які Клод
        # рахував окремо, тож там сума лишається сумою. Інакше mention_count
        # занижувався б, а він тягне за собою ранжування і фільтри «від N згадок».
        # Той самий ключ згортання, що й у `find_cross_type_twins`: `normalized_name`
        # лишає внутрішні пробіли й пунктуацію, тож «Acme Corp» і «AcmeCorp»
        # шукач показував однією групою, а тут вони вважались різними — і згадки
        # складались там, де мали братись як MAX.
        same_name = _fold_name(keep["canonical_name"]) == _fold_name(merge["canonical_name"])
        on_conflict = ("mention_count = MAX(mention_count, excluded.mention_count), "
                       if same_name else
                       "mention_count = mention_count + excluded.mention_count, ")
        for r in me_rows:
            c.execute(
                "INSERT INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, salience, role_in_meeting, source) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(transcription_id, entity_id) DO UPDATE SET "
                + on_conflict +
                # Походження keep-рядка НЕ чіпаємо. `source IS NULL` тут означає
                # не «невідомо», а «звʼязок від Клода», і `tg_entities` такі
                # свідомо не чіпає як надійніші. Перезапис на 'telegram' віддав би
                # рядок під `DELETE … WHERE source='telegram'`, і перебудова
                # відтворила б його голим: без salience, ролі й накопичених згадок.
                "salience = MAX(COALESCE(salience, 0), COALESCE(excluded.salience, 0))",
                (r["transcription_id"], keep_id, r["mention_count"], r["salience"],
                 r["role_in_meeting"], r["source"]),
            )

        # --- action_items: перепризначити власника ---
        c.execute(
            "UPDATE action_items SET owner_entity_id = ? WHERE owner_entity_id = ?",
            (keep_id, merge_id),
        )

        # --- доповнити keep полями merge, якщо порожні ---
        if not keep["role"] and merge["role"]:
            c.execute("UPDATE entities SET role = ? WHERE id = ?", (merge["role"], keep_id))
        if not keep["description"] and merge["description"]:
            c.execute("UPDATE entities SET description = ? WHERE id = ?",
                      (merge["description"], keep_id))
        if not keep["speaker_id"] and merge["speaker_id"]:
            c.execute("UPDATE entities SET speaker_id = ? WHERE id = ?",
                      (merge["speaker_id"], keep_id))

        # --- аудит-слід у metadata_json (best-effort) ---
        try:
            meta = json.loads(keep["metadata_json"]) if keep["metadata_json"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        merged_from = meta.get("merged_from") or []
        merged_from.append({"id": merge_id, "name": merge["canonical_name"],
                            "type": merge["type"], "aliases": gone_aliases, "at": now})
        meta["merged_from"] = merged_from
        c.execute(
            "UPDATE entities SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), now, keep_id),
        )

        # --- видалити merge_id: CASCADE підчищає залишкові aliases/meeting_entities ---
        c.execute("DELETE FROM entities WHERE id = ?", (merge_id,))

        _recompute_entity_aggregates(c, {keep_id})
        conn.commit()

    logger.info("[entity_dedup] merged id=%s ('%s') -> id=%s ('%s')",
                merge_id, merge["canonical_name"], keep_id, keep["canonical_name"])
    out = {
        "status": "merged",
        "keep_id": keep_id, "merge_id": merge_id,
        "kept_name": keep["canonical_name"], "merged_name": merge["canonical_name"],
    }
    if person_index_added:
        out["person_index_added"] = person_index_added
        logger.info("[entity_dedup] у person-індекс увійшли написання: %s",
                    ", ".join(person_index_added))
    return out


# ============================================================
# 3. Split — зворотна беда: в одному рядку сидять різні люди
# ============================================================

# Мінімальна довжина написання, щоб вважати його доказом у тексті. Коротке
# («Юр», «Юл») трапляється підрядком у половині архіву і доказом бути не може.
# Ціна порогу: коли ціле імʼя сутності коротше за нього («Юра»), її бік доказів у
# тексті порожній — тож звʼязок переїде, якщо доведене лише відчеплене написання.
# Це і є потрібна поведінка: рухаємо тільки доказане.
_EVIDENCE_MIN_LEN = 4


def _pattern(spelling: str) -> "re.Pattern":
    """Написання з ПОЧАТКУ слова, будь-яке закінчення дозволене.

    Голий підрядок брехав: аліас «Слав» знайшовся у 160 текстах, бо сидить
    усередині «Слава», «Славік» і «Вʼячеслав». Межа лише спереду, а не з обох
    боків, бо українські відмінки — це і є суфікс: «Гринчука», «Вересу»,
    «Настею» мусять зараховуватись написанню «Гринчук»/«Верес»/«Настя».
    """
    return re.compile(r"(?<!\w)" + re.escape(spelling.lower()))

# Розділювачі складеного власника («Юлія Бондаренка, Дмитро Лебідь», «Юля / Діма»):
# такий рядок вказує на двох людей, і жодній зі сторін він не доказ.
_OWNER_SPLIT = re.compile(r"[,/;+&]| та | і | and ", re.IGNORECASE)


def _alias_rows(c, entity_id: int) -> list:
    return c.execute(
        "SELECT id, alias, normalized_alias FROM entity_aliases WHERE entity_id = ? "
        "ORDER BY LENGTH(alias) DESC", (entity_id,)).fetchall()


def _evidence_pairs(spellings: list[str]) -> list[tuple[str, "re.Pattern"]]:
    """Написання, довгі достатньо, щоб бути доказом — від довгих до коротких."""
    return [(s, _pattern(s))
            for s in sorted(set(spellings), key=len, reverse=True)
            if len(s) >= _EVIDENCE_MIN_LEN]


def _winning_occurrences(text_low: str, pairs: list[tuple[str, "re.Pattern"]]) -> list[str]:
    """Кому належить кожна згадка в тексті: на позиції виграє НАЙДОВШЕ написання.

    Одну згадку не можна зарахувати двом. «Слава Гринчук показав макет» — це
    доказ Гринчука, а не присутності Слави: коротке імʼя тут лише частина
    повного. І навпаки, «Юлія Бондаренка підтвердила» — доказ Бондаренкої, а не
    будь-якої Юлії. Тому обидві сторони змагаються за ту саму позицію тексту, і
    довше написання забирає її разом із коротшими, що всередині нього.

    Затирання однієї сторони перед пошуком іншої (перший підхід) саме тут і
    ламалось: відчеплюване «Юлія» стирало собою «Юлія Бондаренка», доказ того,
    хто лишається, зникав — і чужа зустріч переїжджала.
    """
    hits = sorted((m.start(), -len(s), s)
                  for s, pat in pairs for m in pat.finditer(text_low))
    won: list[str] = []
    end = -1
    for start, neg_len, spelling in hits:
        if start < end:          # накрите довшим збігом — та сама згадка
            continue
        end = start - neg_len
        won.append(spelling)
    return won


def inspect_entity(db_path: str, entity_id: int) -> dict:
    """Докази «хто саме» всередині одного рядка графа. Нічого не змінює.

    Показує по кожному аліасу: скільки задач названо саме цим написанням
    (`action_items.owner_name` — сире імʼя з транскрипту, воно збігається з
    аліасом у ~96% рядків) і в скількох звʼязаних текстах це написання видно.
    Це і є матеріал для рішення про `split_entity`: рядок «Слава» тримає і
    `Слава Гринчук`, і `Вʼячеслав Верес`, і чужий `Stepan` — але розділити їх
    можна лише там, де написання назвало себе саме.
    """
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        ent = c.execute(
            "SELECT id, type, canonical_name, role, description, mention_count, "
            "meeting_count FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not ent:
            return {"status": "not_found", "entity_id": entity_id}
        aliases = _alias_rows(c, entity_id)
        tasks = c.execute(
            "SELECT ai.id, ai.owner_name, ai.status FROM action_items ai "
            "JOIN transcriptions t ON t.id = ai.transcription_id "
            "WHERE ai.owner_entity_id = ? AND t.deleted_at IS NULL", (entity_id,)).fetchall()
        links = c.execute(
            "SELECT me.transcription_id tid, COALESCE(t.transcript_text, '') txt "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ? AND t.deleted_at IS NULL", (entity_id,)).fetchall()

    # Канонічне імʼя — теж написання, і рядка в `entity_aliases` для нього може
    # НЕ бути: `_upsert_entity` заводить його через INSERT OR IGNORE проти
    # глобального UNIQUE, тож коли написання вже забрала чужа сутність (та сама
    # біда, задля якої існує цей модуль), аліас не завівся. Без нього власні
    # задачі рядка виглядали б як «власник поза аліасами», а `split_entity` усе
    # одно рахує канонічне ім'я серед тих, хто лишається — числа розходились би.
    spellings = [r["alias"] for r in aliases]
    if _fold_name(ent["canonical_name"]) not in {_fold_name(a) for a in spellings}:
        spellings.insert(0, ent["canonical_name"])
    by_alias: dict[str, dict] = {a: {"tasks": 0, "texts": 0} for a in spellings}
    fold_to_alias: dict[str, str] = {}
    for a in spellings:                       # довше написання виграє ключ згортання
        fold_to_alias.setdefault(_fold_name(a), a)

    ambiguous_owners = 0
    unmatched_owners: dict[str, int] = {}
    for t in tasks:
        owner = (t["owner_name"] or "").strip()
        hit = fold_to_alias.get(_fold_name(owner))
        if hit:
            by_alias[hit]["tasks"] += 1
        elif _OWNER_SPLIT.search(owner):
            ambiguous_owners += 1
        else:
            unmatched_owners[owner] = unmatched_owners.get(owner, 0) + 1

    text_only_short = 0
    pairs = _evidence_pairs(spellings)
    for r in links:
        # Та сама атрибуція, що й у `split_entity`: «Слава Гринчук» — рядок
        # Гринчука, а не доказ обох. Інакше `inspect` обіцяв би тексти написанню,
        # яке split не віддасть, і числа двох тулзів розходились би.
        seen = set(_winning_occurrences(r["txt"].lower(), pairs))
        for s in seen:
            by_alias[s]["texts"] += 1
        if not seen:
            text_only_short += 1

    return {
        "status": "ok",
        "entity": {"id": ent["id"], "type": ent["type"], "name": ent["canonical_name"],
                   "role": ent["role"], "mention_count": ent["mention_count"],
                   "meeting_count": ent["meeting_count"]},
        "tasks_total": len(tasks),
        "links_total": len(links),
        # Звʼязки, де жодного довгого написання не видно — тримаються на короткому
        # імені й доказу не мають. Split їх не рухає, і це видно наперед.
        "links_short_name_only": text_only_short,
        "tasks_ambiguous_owner": ambiguous_owners,
        "tasks_unmatched_owner": sorted(unmatched_owners.items(), key=lambda kv: -kv[1]),
        "aliases": [{"alias": a, "tasks": by_alias[a]["tasks"],
                     "texts": by_alias[a]["texts"],
                     "evidence": len(a) >= _EVIDENCE_MIN_LEN}
                    for a in spellings],
    }


def split_entity(db_path: str, source_id: int, move_aliases: list[str], *,
                 target_id: Optional[int] = None, new_name: Optional[str] = None,
                 drop: bool = False, dry_run: bool = True) -> dict:
    """Відчепити написання від сутності — разом із задачами й звʼязками, які
    доказово належать саме їм. Обернена до `merge_entities` операція.

    Потрібна там, де збагачення склеїло РІЗНИХ людей в один рядок: id 147
    «Дмитро» тримає і `Дмитро Лебідь`, і `М.Верес` — двох живих людей, а на рядку
    висить 40 задач. Merge тут не допоможе, а без відчеплення дефект не
    косметичний, а активний: `entity_aliases.normalized_alias` UNIQUE ГЛОБАЛЬНО,
    тож чуже написання серед аліасів перехоплює чужу людину і на майбутнє —
    `enrichment._find_entity` віддасть Славу на person «Stepan», бо `Stepan`
    лежить у Слави в аліасах.

    Адресат — один із трьох:
      * `target_id` — існуюча сутність ТОГО САМОГО типу (`Stepan` → id 459 «Степан»);
      * `new_name` — нова сутність типу source (`Юра Литвин` окремо від `Юра`);
      * `drop=True` — написання є смітником, не людиною (`JSON` у Джейсона,
        `EULA` у Юлії): аліас зникає, задачі з таким власником лишаються без
        власника (NULL — честніше за неправильного власника), доказові звʼязки
        видаляються.

    **Доказ, а не здогад.** Задача переїжджає, лише якщо `owner_name` — це саме
    відчеплюване написання. Звʼязок переїжджає, лише якщо в тексті видно
    відчеплюване написання і НЕ видно жодного з тих, що лишаються; якщо видно
    обидві сторони — звʼязок КОПІЮЄТЬСЯ (у тексті справді двоє), а не рухається;
    якщо не видно нічого (згадка коротким імʼям) — лишається джерелу. Тому
    `dry_run` за замовчуванням: спершу дивимось на числа.

    Returns {"status": "ok"|"error"|"not_found", ...} з підрахунками.
    """
    if not move_aliases:
        return {"status": "error", "error": "move_aliases порожній"}
    modes = [target_id is not None, bool(new_name), drop]
    if sum(1 for m in modes if m) != 1:
        return {"status": "error",
                "error": "потрібен РІВНО один адресат: target_id | new_name | drop"}
    if target_id is not None and target_id == source_id:
        return {"status": "error", "error": "target_id == source_id"}

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        src = c.execute("SELECT * FROM entities WHERE id = ?", (source_id,)).fetchone()
        if not src:
            return {"status": "not_found", "source_id": source_id}
        tgt = None
        if target_id is not None:
            tgt = c.execute("SELECT * FROM entities WHERE id = ?", (target_id,)).fetchone()
            if not tgt:
                return {"status": "not_found", "target_id": target_id}
            # Тип не змінюємо навіть із прапорцем: аліас людини, поїхавши в
            # project, зникає з person-індексів (`speakers._entity_index`,
            # `commitments.link_owners`) — той самий дефект, що й у merge.
            if tgt["type"] != src["type"]:
                return {"status": "error",
                        "error": f"type mismatch: source={src['type']} target={tgt['type']}"}

        rows = _alias_rows(c, source_id)
        want = {_fold_name(a) for a in move_aliases if _fold_name(a)}
        moving = [r for r in rows if _fold_name(r["alias"]) in want]
        staying = [r for r in rows if _fold_name(r["alias"]) not in want]
        found = {_fold_name(r["alias"]) for r in moving}
        missing = sorted(a for a in move_aliases if _fold_name(a) not in found)
        if not moving:
            return {"status": "error",
                    "error": f"жодного з написань немає в аліасах #{source_id}: {missing}"}
        # Канонічне імʼя лишається джерелу: воно тримає `normalized_name`, за яким
        # рядок знаходить `_find_entity`, і UNIQUE(type, normalized_name). Забрати
        # його — це перейменування сутності, інша операція.
        if _fold_name(src["canonical_name"]) in want:
            return {"status": "error",
                    "error": (f"'{src['canonical_name']}' — канонічне імʼя #{source_id}; "
                              "відчепити його не можна (це перейменування, не split)")}
        # Порожній `staying` перейменуванням НЕ є: канонічне імʼя лишається завжди
        # (перевірка вище), а рядка в `entity_aliases` для нього може не бути —
        # `_upsert_entity` заводить його через INSERT OR IGNORE проти глобального
        # UNIQUE і програє гонку чужій сутності. Саме такий рядок — «Дмитро» з
        # єдиним аліасом `М.Верес` — і є те, задля чого split існує; гвардія на
        # `not staying` відмовляла б рівно в цьому випадку й ні в якому іншому.

        moving_spellings = [r["alias"] for r in moving]
        staying_spellings = [r["alias"] for r in staying] + [src["canonical_name"]]

        # Адресата перевіряємо ДО прикидки, а не перед самим записом: інакше
        # dry-run обіцяє split, який `--apply` потім відхилить («вже існує #N»),
        # а прикидка — єдина поверхня, за якою оператор ухвалює рішення.
        canonical = norm = None
        if new_name:
            canonical = new_name.strip()
            norm = _normalize(canonical)
            if not norm:
                return {"status": "error", "error": "new_name порожній після нормалізації"}
            clash = c.execute(
                "SELECT id FROM entities WHERE type = ? AND normalized_name = ?",
                (src["type"], norm)).fetchone()
            if clash:
                return {"status": "error",
                        "error": (f"[{src['type']}] '{canonical}' вже існує (#{clash['id']}) "
                                  f"— використай target_id={clash['id']}")}

        # М'яко видалені зустрічі беремо нарівні з живими — так само, як
        # `merge_entities`. `POST /api/transcription/<id>/restore` повертає їх у
        # життя, і якби split їх обійшов, після відновлення задача лишилась би за
        # старим власником, а звʼязок — без написання, що його тримало.
        # У числах прикидки їх видно окремо: в UI цих зустрічей зараз немає.
        # --- задачі: доказ — сире імʼя з транскрипту ---
        moving_folds = {_fold_name(r["alias"]) for r in moving}
        task_rows = c.execute(
            "SELECT ai.id, ai.owner_name, t.deleted_at FROM action_items ai "
            "JOIN transcriptions t ON t.id = ai.transcription_id "
            "WHERE ai.owner_entity_id = ?", (source_id,)).fetchall()
        moving_tasks = [r for r in task_rows
                        if _fold_name(r["owner_name"] or "") in moving_folds]
        task_ids = [r["id"] for r in moving_tasks]

        # --- звʼязки: доказ — написання в тексті зустрічі ---
        link_rows = c.execute(
            "SELECT me.transcription_id tid, me.mention_count, me.salience, "
            "me.role_in_meeting, me.source, t.deleted_at, "
            "COALESCE(t.transcript_text, '') txt "
            "FROM meeting_entities me JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ?", (source_id,)).fetchall()
        # Пари спільні на обидві сторони: вони змагаються за ті самі позиції
        # тексту, і кожну згадку забирає рівно одна (див. `_winning_occurrences`).
        evidence_pairs = _evidence_pairs(moving_spellings + staying_spellings)
        moving_evidence = {s for s in moving_spellings if len(s) >= _EVIDENCE_MIN_LEN}
        to_move, to_copy = [], []
        mine_mentions: dict[int, int] = {}
        for r in link_rows:
            won = _winning_occurrences(r["txt"].lower(), evidence_pairs)
            mine = [s for s in won if s in moving_evidence]
            theirs = [s for s in won if s not in moving_evidence]
            if mine and not theirs:
                to_move.append(r)
            elif mine and theirs:
                to_copy.append(r)
                mine_mentions[r["tid"]] = len(mine)

        # У режимі `drop` копії не робляться — спільний текст просто лишається
        # джерелу. Рахувати його «скопійованим» означало б показати links_left=0
        # там, де звʼязки нікуди не зникли; прикидка — єдина поверхня рішення.
        copied = [] if drop else to_copy
        deleted_links = sum(1 for r in to_move + copied if r["deleted_at"])
        deleted_tasks = sum(1 for r in moving_tasks if r["deleted_at"])
        report = {
            "status": "ok",
            "dry_run": dry_run,
            "source": {"id": source_id, "type": src["type"], "name": src["canonical_name"]},
            "target": ({"id": tgt["id"], "name": tgt["canonical_name"]} if tgt else
                       ({"new_name": new_name, "type": src["type"]} if new_name else
                        {"drop": True})),
            "aliases_moved": moving_spellings,
            "aliases_not_found": missing,
            "tasks_moved": len(task_ids),
            "links_moved": len(to_move),
            "links_copied": len(copied),
            "links_left": len(link_rows) - len(to_move) - len(copied),
            # Скільки з порахованого — зі зустрічей у кошику: в UI їх зараз немає,
            # але після відновлення вони мають бути вже за правильним власником.
            "from_deleted_meetings": {"tasks": deleted_tasks, "links": deleted_links},
            # У drop спільний текст лишається джерелу і входить у links_left.
            "links_shared_kept": len(to_copy) if drop else 0,
        }
        if dry_run:
            return report

        now = datetime.now().isoformat(timespec="seconds")

        # --- адресат: створити нову сутність, якщо просили ---
        if new_name:
            cur = c.execute(
                "INSERT INTO entities (type, canonical_name, normalized_name, "
                "first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (src["type"], canonical, norm, now, now))
            target_id = cur.lastrowid
            report["target"] = {"id": target_id, "name": canonical, "created": True}

        alias_ids = [r["id"] for r in moving]
        marks = ",".join("?" for _ in alias_ids)
        if drop:
            c.execute(f"DELETE FROM entity_aliases WHERE id IN ({marks})", alias_ids)
        else:
            # Просте UPDATE без OR IGNORE: `normalized_alias` UNIQUE ГЛОБАЛЬНО, тож
            # двох рядків з одним написанням у різних сутностей не буває — конфлікту
            # тут не існує. Якби індекс колись зняли, IntegrityError порве всю
            # транзакцію, і це краще за тихо застряглий у джерела аліас.
            c.execute(f"UPDATE entity_aliases SET entity_id = ? WHERE id IN ({marks})",
                      [target_id, *alias_ids])

        # --- задачі ---
        if task_ids:
            tmarks = ",".join("?" for _ in task_ids)
            # NULL, а не source: без власника задача видна в «нічиїх», а з
            # неправильним власником вона бреше у зводі.
            c.execute(f"UPDATE action_items SET owner_entity_id = ? WHERE id IN ({tmarks})",
                      [None if drop else target_id, *task_ids])

        # --- звʼязки ---
        moved_tids = [r["tid"] for r in to_move]
        if drop:
            if moved_tids:
                lmarks = ",".join("?" for _ in moved_tids)
                c.execute(f"DELETE FROM meeting_entities WHERE entity_id = ? "
                          f"AND transcription_id IN ({lmarks})", [source_id, *moved_tids])
        else:
            # Перенесений звʼязок несе свої числа як є — текст говорить лише про
            # відчеплену людину, тож рядок від початку був її. Копії дістається
            # рівно стільки згадок, скільки виграли ЇЇ написання: у спільному
            # тексті обидва рядки графа дивляться на різні слова, і брати чужу
            # суму — означало б приписати адресату згадки джерела.
            for r, mentions in (
                [(r, r["mention_count"]) for r in to_move]
                + [(r, mine_mentions[r["tid"]]) for r in to_copy]
            ):
                c.execute(
                    "INSERT INTO meeting_entities (transcription_id, entity_id, "
                    "mention_count, salience, role_in_meeting, source) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(transcription_id, entity_id) DO UPDATE SET "
                    # Сума, а не MAX: у адресата вже може бути рядок на цю зустріч,
                    # зароблений ВЛАСНИМ іменем, а сюди приходять згадки інших слів
                    # — те саме правило, що й у `merge_entities` для різних написань
                    # (MAX там лише коли написання одне й те саме).
                    "mention_count = mention_count + excluded.mention_count, "
                    "salience = MAX(COALESCE(salience, 0), COALESCE(excluded.salience, 0))",
                    (r["tid"], target_id, mentions, r["salience"], r["role_in_meeting"],
                     r["source"]))
            # Спільний текст: у джерела забираємо рівно те, що переїхало. Інакше
            # згадки рахуються двічі — і в джерела, і в адресата. Нижче за 1 не
            # опускаємось: рядок лишається живим (він же й далі згаданий своїм
            # написанням), а сам `mention_count` від enrichment завищений і без
            # нас — там сирий підрахунок підрядків, де «Слава Гринчук» дає +3.
            for r in to_copy:
                c.execute("UPDATE meeting_entities SET mention_count = MAX(1, ? - ?) "
                          "WHERE entity_id = ? AND transcription_id = ?",
                          (r["mention_count"], mine_mentions[r["tid"]], source_id, r["tid"]))
            if moved_tids:
                lmarks = ",".join("?" for _ in moved_tids)
                c.execute(f"DELETE FROM meeting_entities WHERE entity_id = ? "
                          f"AND transcription_id IN ({lmarks})", [source_id, *moved_tids])

        # --- слід злиття не має повертати відчеплене назад ---
        # `enrichment._find_merged_entity` віддає джерело за написанням із
        # `merged_from[].aliases`. Якби ми його там лишили, наступний enrich
        # знову привʼязав би відчеплене імʼя до джерела — split «не прилипав» би
        # так само, як колись не прилипав merge.
        try:
            meta = json.loads(src["metadata_json"]) if src["metadata_json"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        gone_norms = {r["normalized_alias"] for r in moving}
        kept_history = []
        for gone in meta.get("merged_from") or []:
            if not isinstance(gone, dict):
                continue
            spellings = [s for s in (gone.get("aliases") or []) if s not in gone_norms]
            if gone.get("aliases") is not None and not spellings:
                continue          # від запису не лишилось написань — прибираємо цілком
            if gone.get("aliases") is not None:
                gone = {**gone, "aliases": spellings}
            elif _normalize(gone.get("name") or "") in gone_norms:
                continue          # старий запис без списку: сам був цим написанням
            kept_history.append(gone)
        if kept_history:
            meta["merged_from"] = kept_history
        else:
            meta.pop("merged_from", None)
        meta.setdefault("split_off", []).append({
            "aliases": moving_spellings,
            "to_id": None if drop else target_id,
            "to_name": None if drop else report["target"].get("name") or new_name,
            "dropped": bool(drop),
            "tasks": len(task_ids), "links_moved": len(to_move),
            "links_copied": len(to_copy), "at": now,
        })
        c.execute("UPDATE entities SET metadata_json = ?, updated_at = ? WHERE id = ?",
                  (json.dumps(meta, ensure_ascii=False), now, source_id))

        touched = {source_id}
        if not drop:
            tmeta = {}
            if tgt and tgt["metadata_json"]:
                try:
                    loaded = json.loads(tgt["metadata_json"])
                    tmeta = loaded if isinstance(loaded, dict) else {}
                except (json.JSONDecodeError, TypeError):
                    tmeta = {}
            tmeta.setdefault("split_from", []).append({
                "id": source_id, "name": src["canonical_name"],
                "aliases": moving_spellings, "at": now,
            })
            c.execute("UPDATE entities SET metadata_json = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(tmeta, ensure_ascii=False), now, target_id))
            touched.add(target_id)

        _recompute_entity_aggregates(c, touched)
        conn.commit()

    logger.info("[entity_dedup] split #%s '%s': %s -> %s (задач %d, звʼязків %d+%d)",
                source_id, src["canonical_name"], moving_spellings,
                "DROP" if drop else f"#{target_id}", len(task_ids), len(to_move), len(to_copy))
    return report


# ============================================================
# 4. Alias — приписати написання руками
# ============================================================

def add_aliases(db_path: str, entity_id: int, aliases: list[str], *,
                dry_run: bool = True) -> dict:
    """Дописати сутності написання, під яким її знає ІНШИЙ шар архіву.

    Дірка, заради якої це існує: аліас вміло заводити лише збагачення (Клод) і
    `merge` як побічний ефект. Тому «Dmytro Lebid» з поля `tg_sender` і «Дмитро
    Лебідь» з графа лишаються двома різними людьми для всіх, хто зводить шари
    (`speakers._entity_index`, `commitments.link_owners`), і злити їх можна було
    хіба що злиттям цілих сутностей — а сутності відправника не існує взагалі.

    Чому не merge: у переписці збіг іде по ОДНОМУ токену після транслітерації
    («Лебідь» = «Lebid»), і це слабка ознака — форма імені буває іншою, прізвище
    спільним у різних людей. Аліас лишає написання на місці й нічого не поглинає.

    Чуже написання не забираємо мовчки. Дивимось ОБИДВА місця, де написання може
    вже жити: `entity_aliases` (UNIQUE глобально) і `entities.normalized_name` —
    бо `enrichment._find_entity` шукає спершу по канонічному імені, а `split
    --to-name` створює рядок узагалі без alias-рядка. Аліас, доданий поверх
    чужого канонічного імені, був би мертвим вантажем, а `relink` після нього
    ще й перевісив би задачі на ту чужу сутність.
    """
    if not aliases:
        return {"status": "error", "error": "aliases порожній"}
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        ent = c.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not ent:
            return {"status": "not_found", "entity_id": entity_id}

        added, already, taken, skipped = [], [], [], []
        for raw in aliases:
            spelling = (raw or "").strip()
            na = _normalize(spelling)
            if not na:
                skipped.append(raw)
                continue
            owner = c.execute(
                "SELECT a.entity_id AS id, e.canonical_name, e.type FROM entity_aliases a "
                "JOIN entities e ON e.id = a.entity_id WHERE a.normalized_alias = ?",
                (na,)).fetchone()
            if owner is None:
                owner = c.execute(
                    "SELECT id, canonical_name, type FROM entities WHERE normalized_name = ?",
                    (na,)).fetchone()
            if owner and owner["id"] == entity_id:
                already.append(spelling)
                continue
            if owner:
                taken.append({"alias": spelling, "entity_id": owner["id"],
                              "name": owner["canonical_name"], "type": owner["type"]})
                continue
            added.append(spelling)
            if not dry_run:
                c.execute(
                    "INSERT INTO entity_aliases (entity_id, alias, normalized_alias) "
                    "VALUES (?, ?, ?)", (entity_id, spelling, na))
        if not dry_run and added:
            c.execute("UPDATE entities SET updated_at = ? WHERE id = ?",
                      (datetime.now().isoformat(timespec="seconds"), entity_id))
            conn.commit()

    if not dry_run and added:
        logger.info("[entity_dedup] alias +%s -> #%s '%s'",
                    added, entity_id, ent["canonical_name"])
    return {"status": "ok", "dry_run": dry_run,
            "entity": {"id": entity_id, "type": ent["type"],
                       "name": ent["canonical_name"]},
            "added": added, "already": already, "taken": taken, "skipped": skipped}


# ============================================================
# Написання, які насправді є загальними словами
# ============================================================

def find_junk_aliases(db_path: str, *, min_lower: int = 5,
                      max_caps_share: float = 0.2) -> list[dict]:
    """Аліаси, які корпус пише з МАЛОЇ літери — тобто загальні слова, а не імена.

    Звідки береться сміття: збагачення заводить у граф усе, що Клод назвав
    сутністю, разом із варіантами написання. Так у графі опинились аліаси
    «документ», «договір», «проєкт», «інвестор». Кожен із них — це пастка для
    будь-якого шару, що звіряє текст із графом: `tg_entities` мусить тримати
    правило «власна назва пишеться з великої» саме через них, а зріз за
    проєктом ловить «Проєкту» в чужому реченні і приписує його Nova Dance Center.

    Доказ беремо там само, де й `tg_entities` — у вживанні. Власну назву пишуть
    з великої літери всюди, а не лише на початку речення; загальне слово —
    з малої. Тому рахуємо по всьому архіву дві величини: скільки разів написання
    зустрілось з великої літери в СЕРЕДИНІ речення і скільки — з малої.
    Переважає мала — це слово, а не імʼя.

    Правило власної назви імпортуємо з `tg_entities`, а не переписуємо: два
    визначення того самого розійшлись би на першій же правці, і аудит почав би
    показувати не те, на чому стоять справжні звʼязки.

    Нічого не змінює. Зняти знайдене — `split <id> --alias «…» --drop`.
    """
    from app.services import tg_entities

    with get_db_connection(db_path) as conn:
        # Канонічне імʼя теж треба судити, і не через `entity_aliases`: свій
        # alias-рядок заводиться через INSERT OR IGNORE проти глобального
        # UNIQUE, тож коли написання вже забрала чужа сутність, рядка немає.
        # На живій базі таких 58 із 2692 — серед них рівно та форма, яку команда
        # й шукає («Zero», «Notion», «Форест»). А звʼязки `tg_entities` ставить
        # і за канонічним імʼям теж (`load_names` читає обидва джерела).
        ph = ",".join("?" * len(tg_entities.LINKABLE_TYPES))
        rows = conn.execute(
            "SELECT a.alias, a.entity_id, e.type, e.canonical_name, e.normalized_name "
            "FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
            f"WHERE e.type IN ({ph}) AND a.alias NOT LIKE '% %' "
            "UNION "
            "SELECT e.canonical_name AS alias, e.id AS entity_id, e.type, "
            "       e.canonical_name, e.normalized_name "
            "FROM entities e "
            f"WHERE e.type IN ({ph}) AND e.canonical_name NOT LIKE '% %'",
            (*tg_entities.LINKABLE_TYPES, *tg_entities.LINKABLE_TYPES)).fetchall()
        # Скільки звʼязків тримає сутність саме за звіркою з текстом: це і є
        # ціна помилки, якщо написання виявиться сміттям.
        link_rows = conn.execute(
            "SELECT entity_id, COUNT(*) n FROM meeting_entities WHERE source = ? "
            "GROUP BY entity_id", (tg_entities.SOURCE,)).fetchall()
        texts = conn.execute(
            "SELECT transcript_text FROM transcriptions "
            "WHERE deleted_at IS NULL AND transcript_text IS NOT NULL").fetchall()

    watched: dict[str, list] = {}
    handles: set[str] = set()
    for r in rows:
        key = tg_entities._key(r["alias"])
        if len(key) < tg_entities.MIN_NAME_LEN:
            continue
        # Телеграм-нік — власна назва за домовленістю, і пишеться завжди з
        # малої: правило великої літери його засуджує, хоча «@jbondarenko» ніяким
        # загальним словом не є. Судити нікнейми цим методом не можна взагалі.
        # Дивимось не лише на аліас із «@»: те саме написання приїжджає ще й
        # канонічним імʼям, уже без собачки, і тоді гард не спрацьовував.
        if r["alias"].lstrip().startswith("@"):
            handles.add(key)
        watched.setdefault(key, []).append(r)
    with get_db_connection(db_path) as conn:
        for row in conn.execute("SELECT DISTINCT tg_sender FROM transcriptions "
                                "WHERE tg_sender IS NOT NULL"):
            handles.add(tg_entities._key(row["tg_sender"]))
    for key in handles:
        watched.pop(key, None)
    if not watched:
        return []

    caps: dict[str, int] = {}
    lower: dict[str, int] = {}
    for row in texts:
        text = row["transcript_text"]
        for m in tg_entities._WORD.finditer(text):
            word = m.group(0)
            key = word.casefold()
            if key not in watched:
                continue
            if word[:1].islower():
                lower[key] = lower.get(key, 0) + 1
            # Доказ рахуємо ТИМ САМИМ правилом, яким ставляться звʼязки —
            # інакше аудит радить зняти написання, що саме зараз працює:
            # «ACMECORP» у звичайному реченні для `find_mentions` є назвою, а
            # аудит його не бачив узагалі (окремий гард на ALL-CAPS) і видавав
            # caps=0 при шести живих звʼязках.
            elif tg_entities.written_as_proper_noun(text, word, m.start()):
                caps[key] = caps.get(key, 0) + 1

    links = {r["entity_id"]: r["n"] for r in link_rows}
    out = []
    for key, entries in watched.items():
        lo, up = lower.get(key, 0), caps.get(key, 0)
        if lo < min_lower or (up + lo) == 0:
            continue
        share = up / (up + lo)
        if share >= max_caps_share:
            continue
        for r in entries:
            out.append({
                "alias": r["alias"], "entity_id": r["entity_id"], "type": r["type"],
                "entity": r["canonical_name"],
                # Канонічне імʼя теж лежить в аліасах: зняти його аліасом мало —
                # рядок лишиться в графі під тим самим словом.
                "is_canonical": tg_entities._key(r["canonical_name"]) == key,
                "lower": lo, "caps_mid_sentence": up, "caps_share": round(share, 3),
                "entity_links": links.get(r["entity_id"], 0),
            })
    out.sort(key=lambda d: (-d["lower"], d["alias"]))
    return out


# ============================================================
# CLI
# ============================================================

def _cmd_junk_aliases(args: argparse.Namespace) -> int:
    found = find_junk_aliases(args.db, min_lower=args.min_lower,
                              max_caps_share=args.max_caps_share)
    if args.json:
        print(json.dumps(found[:args.limit], ensure_ascii=False, indent=2))
        return 0
    if not found:
        print("Написань, схожих на загальні слова, не знайдено.")
        return 0
    shown = found[:args.limit]
    print(f"Знайдено {len(found)} написань, які корпус пише переважно з малої "
          f"(показано {len(shown)}):\n")
    for r in shown:
        mark = " ← КАНОНІЧНЕ ІМʼЯ" if r["is_canonical"] else ""
        print(f"  «{r['alias']}» → [{r['type']}] #{r['entity_id']} '{r['entity']}'{mark}")
        print(f"      з малої {r['lower']}, з великої в середині речення "
              f"{r['caps_mid_sentence']} ({r['caps_share']:.0%}); "
              f"звʼязків у сутності: {r['entity_links']}")
    print("\nЗняти написання: python -m app.services.entity_dedup split <entity_id> "
          "--alias «НАПИСАННЯ» --drop --apply")
    print("Канонічне імʼя аліасом не знімається — там потрібне злиття або "
          "перейменування сутності.")
    return 0


def _cmd_twins(args: argparse.Namespace) -> int:
    groups = find_cross_type_twins(args.db, include_person=args.include_person)
    if args.json:
        print(json.dumps(groups, ensure_ascii=False, indent=2))
        return 0
    if not groups:
        print("Точних тезок у різних типах не знайдено.")
        return 0
    print(f"Знайдено {len(groups)} груп "
          f"(рядків зникне: {sum(len(g['merge']) for g in groups)}):\n")
    for g in groups:
        k = g["keep"]
        print(f"  keep [{k['type']}] #{k['id']} '{k['name']}' ({k['links']} звʼязків)")
        for m in g["merge"]:
            print(f"     ← [{m['type']}] #{m['id']} '{m['name']}' ({m['links']} звʼязків)")
    print("\nЩоб злити пару: python -m app.services.entity_dedup merge "
          "<keep_id> <merge_id> --allow-cross-type")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    candidates = find_merge_candidates(
        args.db, etype=args.type, threshold=args.threshold, limit=args.limit,
    )
    if args.json:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return 0
    if not candidates:
        print(f"Кандидатів на merge не знайдено (поріг={args.threshold}).")
        return 0
    print(f"Знайдено {len(candidates)} кандидатів (поріг={args.threshold}):\n")
    for cnd in candidates:
        print(f"  [{cnd['type']}] #{cnd['id1']} '{cnd['name1']}'  <->  "
              f"#{cnd['id2']} '{cnd['name2']}'   sim={cnd['similarity']}")
    print("\nЩоб злити пару: "
          "python -m app.services.entity_dedup merge <keep_id> <merge_id>")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    from app.db.connection import get_db_connection as _gdc
    with _gdc(args.db) as conn:
        keep = conn.execute("SELECT type, canonical_name FROM entities WHERE id = ?",
                            (args.keep_id,)).fetchone()
        merge = conn.execute("SELECT type, canonical_name FROM entities WHERE id = ?",
                             (args.merge_id,)).fetchone()
    if not keep or not merge:
        print(f"Не знайдено: keep_id={args.keep_id} ({'є' if keep else 'НЕМА'}), "
              f"merge_id={args.merge_id} ({'є' if merge else 'НЕМА'})")
        return 1
    print(f"Буде злито: [{merge['type']}] #{args.merge_id} '{merge['canonical_name']}' "
          f"-> [{keep['type']}] #{args.keep_id} '{keep['canonical_name']}'")
    if keep["type"] != merge["type"]:
        if not args.allow_cross_type:
            print("  Різні типи — потрібен --allow-cross-type. Нічого не зроблено.")
            return 1
        if "person" in (keep["type"], merge["type"]) and not args.allow_person:
            print("  Людина і не-людина: злиття заборонено (див. merge_entities).")
            print("  Якщо пару вже подивилися очима і це справді одне — "
                  "додай --allow-person.")
            return 1
        print(f"  УВАГА: різні типи, підсумковий тип буде '{keep['type']}'")
    if not args.yes:
        answer = input("Підтвердити merge? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "так", "т"):
            print("Скасовано.")
            return 1
    result = merge_entities(args.db, args.keep_id, args.merge_id,
                            allow_cross_type=args.allow_cross_type,
                            allow_person=args.allow_person)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "merged" else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    info = inspect_entity(args.db, args.entity_id)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info.get("status") == "ok" else 1
    if info.get("status") != "ok":
        print(f"Сутність #{args.entity_id} не знайдена.")
        return 1
    e = info["entity"]
    print(f"[{e['type']}] #{e['id']} '{e['name']}'  role={e['role'] or '—'}")
    print(f"  згадок {e['mention_count']}, зустрічей {e['meeting_count']}, "
          f"задач {info['tasks_total']}, звʼязків {info['links_total']} "
          f"(з них без довгого написання в тексті: {info['links_short_name_only']})")
    if info["tasks_ambiguous_owner"]:
        print(f"  задач зі складеним власником («X та Y»): "
              f"{info['tasks_ambiguous_owner']} — доказом не є")
    if info["tasks_unmatched_owner"]:
        print(f"  власники поза аліасами: {info['tasks_unmatched_owner'][:8]}")
    print("\n  аліас                          задач  текстів")
    for a in info["aliases"]:
        mark = " " if a["evidence"] else "·"   # · = коротке, у текстах не шукаємо
        print(f"  {mark}{a['alias'][:30]:<30} {a['tasks']:>5}  {a['texts']:>7}")
    print("\nВідчепити: python -m app.services.entity_dedup split "
          f"{e['id']} --alias '<написання>' (--to-id N | --to-name '...' | --drop) --apply")
    return 0


def _cmd_alias(args: argparse.Namespace) -> int:
    res = add_aliases(args.db, args.entity_id, args.add, dry_run=not args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("status") != "ok":
        return 1
    if res.get("taken"):
        for t in res["taken"]:
            print(f"  ЗАЙНЯТО: '{t['alias']}' вже за [{t['type']}] #{t['entity_id']} "
                  f"'{t['name']}' — не чіпаємо. Якщо це та сама людина, тут потрібен merge")
    if res.get("dry_run"):
        print("\nЦе була ПРИКИДКА (нічого не змінено). Додай --apply, щоб виконати.")
    if not res.get("added") and res.get("taken"):
        # Нічого не приписано, і причина — чужі написання. Для скрипта це відмова,
        # а не «зроблено»: рівно так само поводиться merge, коли пару відхилено.
        return 1
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    res = split_entity(args.db, args.source_id, args.alias, target_id=args.to_id,
                       new_name=args.to_name, drop=args.drop, dry_run=not args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("status") != "ok":
        return 1
    if res.get("dry_run"):
        print("\nЦе була ПРИКИДКА (нічого не змінено). Додай --apply, щоб виконати.")
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    parser = argparse.ArgumentParser(
        prog="entity_dedup",
        description="Offline embedding-based fuzzy dedup сутностей графа памʼяті (T6.7).",
    )
    parser.add_argument("--db", default=default_db, help="Шлях до SQLite БД")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Показати кандидатів на merge (read-only)")
    p_list.add_argument("--type", choices=list(_VALID_ENTITY_TYPES), default=None)
    p_list.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.add_argument("--json", action="store_true", help="Вивід як JSON")
    p_list.set_defaults(func=_cmd_list)

    p_twins = sub.add_parser("twins", help="Точні тезки в різних типах (без моделі)")
    p_twins.add_argument("--include-person", action="store_true",
                         help="Показати й пари «людина + не-людина» (merge бере їх "
                              "лише з --allow-person, після перевірки очима)")
    p_twins.add_argument("--json", action="store_true", help="Вивід як JSON")
    p_twins.set_defaults(func=_cmd_twins)

    p_merge = sub.add_parser("merge", help="Злити пару сутностей (підтверджувано)")
    p_merge.add_argument("keep_id", type=int, help="ID сутності, що лишається")
    p_merge.add_argument("merge_id", type=int, help="ID сутності, що зникає")
    p_merge.add_argument("--yes", action="store_true", help="Без інтерактивного підтвердження")
    p_merge.add_argument("--allow-cross-type", action="store_true",
                         help="Дозволити злиття різних типів (project+org тощо); "
                              "підсумковий тип — від keep_id")
    p_merge.add_argument("--allow-person", action="store_true",
                         help="Дозволити пару «людина + не-людина» — лише коли її "
                              "подивилися очима і це справді одне (агенція на "
                              "прізвище власника). Людський рядок, що зникає, все "
                              "одно не сміє мати задач і спікера")
    p_merge.set_defaults(func=_cmd_merge)

    p_alias = sub.add_parser("alias", help="Дописати написання сутності (напр. з tg_sender)")
    p_alias.add_argument("entity_id", type=int)
    p_alias.add_argument("--add", action="append", default=[], required=True,
                         metavar="НАПИСАННЯ", help="Написання; можна кілька разів")
    p_alias.add_argument("--apply", action="store_true",
                         help="Без нього — прикидка (нічого не змінюється)")
    p_alias.set_defaults(func=_cmd_alias)

    p_junk = sub.add_parser(
        "junk-aliases", help="Написання, які корпус пише з малої літери (read-only)")
    p_junk.add_argument("--min-lower", type=int, default=5,
                        help="Скільки разів слово має зустрітись з малої, щоб судити")
    p_junk.add_argument("--max-caps-share", type=float, default=0.2,
                        help="Частка написань з великої, від якої це таки імʼя "
                             "(рівно на межі — вже імʼя)")
    p_junk.add_argument("--limit", type=int, default=40)
    p_junk.add_argument("--json", action="store_true", help="Вивід як JSON")
    p_junk.set_defaults(func=_cmd_junk_aliases)

    p_insp = sub.add_parser("inspect", help="Докази «хто саме» всередині сутності (read-only)")
    p_insp.add_argument("entity_id", type=int)
    p_insp.add_argument("--json", action="store_true", help="Вивід як JSON")
    p_insp.set_defaults(func=_cmd_inspect)

    p_split = sub.add_parser(
        "split", help="Відчепити написання (+ доказові задачі й звʼязки) від сутності")
    p_split.add_argument("source_id", type=int, help="ID сутності, з якої відчеплюємо")
    p_split.add_argument("--alias", action="append", default=[], required=True,
                         metavar="НАПИСАННЯ", help="Аліас до відчеплення (можна кілька разів)")
    dest = p_split.add_mutually_exclusive_group(required=True)
    dest.add_argument("--to-id", type=int, default=None,
                      help="Приліпити до існуючої сутності того ж типу")
    dest.add_argument("--to-name", default=None, help="Створити нову сутність із цим імʼям")
    dest.add_argument("--drop", action="store_true",
                      help="Написання — смітник (JSON, EULA): аліас видалити, "
                           "задачі лишити без власника, доказові звʼязки видалити")
    p_split.add_argument("--apply", action="store_true",
                         help="Виконати (без прапорця — лише прикидка)")
    p_split.set_defaults(func=_cmd_split)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
