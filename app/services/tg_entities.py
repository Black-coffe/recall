"""Telegram у графі сутностей (Волна 4.5.3).

**Проблема.** Telegram — переважна більшість архіву і майже відсутній у графі: 154 записи з
3951. Через це зріз по проєкту, `list_stale_topics` і понедільний звід сліпі
до основної маси архіву — вони бачать лише дзвінки й документи.

**Лікуємо без моделі.** Граф уже знає сутностей із дзвінків. Витягувати
сутності з TG заново локальною 7B — значить наплодити дублів («Акме»,
«Acmecorp», «акмекорп») у графі, де дедуплікація і так невирішена. Тому TG не
збагачуємо, а **звʼязуємо з тим, що вже відоме**: шукаємо в тексті згадки
канонічних назв і аліасів.

**Чому наївний збіг не годиться.** Збагачення записало в аліаси звичайні
слова, і на живих даних це давало сміття:

    [project] «Стратегія розвитку Волинської області» ← аліас «документ»
    [project] «Договір для Миколи/Адама»              ← аліас «договір»
    [person]  «Том»                                   ← аліас «Тому» (144 збіги
                                                        на слові «тому»)

**Частота теж не рятує.** Заміряно по всьому архіву: серед імен, що трапляються
частіше за частина записів, вперемішку і сміття («документ» 5.5%, «модель» 7.5%), і
ключові люди («адам» 7.0%, «андрій» 6.4%). Поріг за частотою вирізав би саме
те, заради чого граф і потрібен.

**Що працює — доказ із місця вживання.** Згадка зараховується, лише якщо в
самому повідомленні вона написана як власна назва: з великої літери і не на
початку речення (велика літера після крапки нічого не доводить). «документ» у
реченні пишуть з малої, «Адам» — з великої. Заміряно: сміття з топу зникає
повністю, а Ковальчука — той самий випадок, заради якого волна робилась, — стає
видимою з 43 згадками.

Ціна — нижча повнота (частина повідомлень проти 40% наївно), але звʼязок у графі
живе довго і псує зрізи назавжди, тож точність тут важливіша за повноту.

**Звʼязок ставимо на ПОВІДОМЛЕННЯ, де згадка, а не на всю нитку.** Спокуса
була протилежна — «назва звучить раз, а рішення в сусідніх репліках» — але
розширення до нитки ВЖЕ зроблено в 4.5.1a (`scope._expand_to_threads`), і воно
працює від `tg_thread_id`, не від графа. Дублювати його тут — зіпсувати
статистику заради того, що й так є: заміряно, нитки тягнуть до сутностей
кожна (медіана 3, p90 19), і звʼязок «кожне повідомлення × кожна сутність
нитки» дав би 43 482 рядки проти 10 441 наявних. Тоді `entities.meeting_count`
сказав би «Адам згадується у записів», а `list_stale_topics` вважав би
кожну сутність нитки згаданою в день останнього повідомлення — тобто зробив би
свіжим те, про що не говорили місяцями.

Два механізми складаються: граф відповідає «де САМЕ згадано», нитка —
«що навколо цього говорили».

**Звʼязування живе на інжесті, а не лише в CLI.** Прохід по всьому архіву —
разова операція, і поки він був єдиним способом, кожне нове повідомлення
лишалось поза графом до наступного ручного запуску. Тепер `link_message`
викликається з того ж job'а, що ставить ембединги (`_submit_embed_only`), одразу
після розкладання по нитках. Повний прохід лишається — він потрібен після змін
у графі (злиття/розділення сутностей додає написання, за якими старі
повідомлення тепер знаходяться).

CLI:
    python -m app.services.tg_entities link --dry-run
    python -m app.services.tg_entities link --chat -1001234567890
    python -m app.services.tg_entities stats
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

#: Провенанс звʼязків, які ставить цей модуль (meeting_entities.source).
SOURCE = "thread_match"

#: Провенанс звʼязків морфологічного матчера (історія 03) — ОКРЕМЕ значення
#: від `SOURCE`: точна словоформа і згадка лише за основою слова — різні за
#: надійністю докази, і `entity_dedup.py`/`comments.py` читають саме `SOURCE`,
#: тож морфо-звʼязки не повинні підмінювати його значення.
SOURCE_MORPH = "thread_morph"

#: Вимикач морфо-матчера. Дефолт "0" — вимкнено: мердж цієї історії сам собою
#: не змінює поведінку системи, доки власник свідомо не увімкне прапорець
#: (після розбору блокерів історії 02). Читається функцією, а не одноразовою
#: константою модуля (як `rag.py:33`), щоб прапорець можна було перемикати між
#: викликами (тести, повторний запуск CLI) без перезапуску процесу.
def _morph_enabled() -> bool:
    return os.environ.get("TG_ENTITIES_MORPH_ENABLED", "0").strip() in ("1", "true", "True")

#: 'topic' навмисно поза грою: 2153 сутності вільним текстом («планування»,
#: «бюджет») зловлять половину архіву і нічого не означатимуть. Граф-зрізи
#: будуються на людях, проєктах і організаціях.
LINKABLE_TYPES = ("person", "project", "org")

#: Коротші імена ловлять сміття («Ок», «БХ») і не дають користі.
MIN_NAME_LEN = 4

#: Скільки слів може містити назва (довші не шукаємо — це вже не імʼя).
MAX_NAME_WORDS = 6

_WORD = re.compile(r"\w+", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?…\n]\s*$")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", (text or "").strip()).casefold()


def _key(text: str) -> str:
    """Ключ пошуку: слова через один пробіл, без пунктуації і регістру."""
    return " ".join(_WORD.findall(_norm(text)))


#: Суфікси відмінків (укр./рос.), які згортаємо перед порівнянням основ —
#: власне рішення без залежностей (pymorphy/snowball/nltk заборонені планом
#: історії, див. Non-goals): не морфологія, а грубе зрізання закінчень,
#: достатнє, щоб «Юлією»/«Тетяни»/«Промприладом» зійшлися з базовою формою
#: («Юлія»/«Тетяна»/«Промприлад»). Найдовші перевіряються першими, щоб не
#: зрізати лише частину довшого закінчення як коротшого.
_CASE_SUFFIXES = tuple(sorted({
    "ями", "ами", "еві", "єві", "ові",
    "ою", "ею", "єю", "ом", "ем", "єм", "ів", "им", "ім", "ий", "ій",
    "а", "я", "у", "ю", "і", "и", "е", "о", "й",
}, key=len, reverse=True))

#: Скільки літер має лишитись після зрізання суфікса — коротший залишок
#: частіше збігається випадково, ніж по суті (напр. «ти» → «т»).
_STEM_MIN_LEN = 3


def _stem(word: str) -> str:
    """Груба основа слова для морфологічного зіставлення (очікує вже
    згорнутий регістр/NFKC, як `lowered`/ключі `names`)."""
    for suf in _CASE_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= _STEM_MIN_LEN:
            return word[:-len(suf)]
    return word


def _build_stems(names: dict[str, int]) -> dict[str, set[int]]:
    """Основа → множина entity_id серед ОДНОСЛІВНИХ назв/аліасів. Багатослівні
    ключі (`names` містить і їх) морфологія не чіпає — відмінювання
    словосполук виходить за межі простого суфіксного згортання."""
    stems: dict[str, set[int]] = {}
    for key, eid in names.items():
        if " " in key:
            continue
        stems.setdefault(_stem(key), set()).add(eid)
    return stems


def load_names(db_path: str) -> dict[str, int]:
    """Канонічні назви + аліаси → entity_id. Канонічна назва має пріоритет:
    той самий рядок може бути аліасом іншої сутності, і тоді точна назва
    правдивіша за варіант написання."""
    names: dict[str, int] = {}
    ph = ",".join("?" * len(LINKABLE_TYPES))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, canonical_name AS name FROM entities WHERE type IN ({ph})",
            LINKABLE_TYPES).fetchall()
        alias_rows = conn.execute(
            f"SELECT a.entity_id AS id, a.alias AS name FROM entity_aliases a "
            f"JOIN entities e ON e.id = a.entity_id WHERE e.type IN ({ph})",
            LINKABLE_TYPES).fetchall()
    for r in list(rows) + list(alias_rows):
        key = _key(r["name"])
        if len(key) >= MIN_NAME_LEN and len(key.split()) <= MAX_NAME_WORDS:
            names.setdefault(key, r["id"])
    return names


def _mentions_core(text: Optional[str], names: dict[str, int],
                    stems: dict[str, set[int]]) -> dict[int, str]:
    """entity_id → провенанс (`SOURCE`/`SOURCE_MORPH`). `stems` порожній —
    морфо-гілка не запускається взагалі (прапорець вимкнений, див.
    `_find_mentions_detailed`/`stems_for`).

    Вимога великої літери не на початку речення — єдине, що відділяє «Адам»
    від «документ» і «Том» від «тому» (див. модульний docstring). Це не
    евристика заради економії: без неї топ згадок на живому архіві складається
    зі сміття, і граф стає гіршим, ніж був.

    Три гарди морфо-гілки (історія 03): (1) точна словоформа на позиції
    вирішує все — морфо-гілка на цій позиції не запускається, навіть якщо за
    основою слово вказує на ІНШУ сутність; (2) основа мусить вказувати рівно
    на одну сутність — «Русланою» при наявних «Руслан» і «Руслана» неоднозначна
    і відкидається (одну згадку не можна зарахувати двом); (3) той самий
    `written_as_proper_noun`, що й для точних збігів — ALL-CAPS не доказ.
    """
    result: dict[int, str] = {}
    if not text:
        return result
    tokens = [(m.group(0), m.start()) for m in _WORD.finditer(text)]
    if not tokens:
        return result
    lowered = [t[0].casefold() for t in tokens]

    exact_word_positions: set[int] = set()
    for size in range(1, MAX_NAME_WORDS + 1):
        for i in range(len(tokens) - size + 1):
            entity_id = names.get(" ".join(lowered[i:i + size]))
            if entity_id is None:
                continue
            # Гард 1 працює на РІВНІ ТОКЕНА, не збігу: усі слова, спожиті
            # цим точним збігом (не лише перше), закриваються для морфо-гілки
            # — інакше слово всередині багатослівної назви лишається
            # відкритим і морфо-гілка може зарахувати його ІНШІЙ сутності
            # (одне написання не можна зараховувати двом).
            exact_word_positions.update(range(i, i + size))
            if entity_id in result:
                continue
            word, pos = tokens[i]
            if written_as_proper_noun(text, word, pos):
                result[entity_id] = SOURCE

    if stems:
        for i, (word, pos) in enumerate(tokens):
            if i in exact_word_positions:
                continue  # Гард 1: точна словоформа на позиції вже вирішила
            candidates = stems.get(_stem(lowered[i]))
            if not candidates or len(candidates) != 1:
                continue  # Гард 2: основа мусить вказувати рівно на одну сутність
            entity_id = next(iter(candidates))
            if entity_id in result:
                continue
            if written_as_proper_noun(text, word, pos):  # Гард 3: не капс-шапка
                result[entity_id] = SOURCE_MORPH
    return result


def _find_mentions_detailed(text: Optional[str], names: dict[str, int]) -> dict[int, str]:
    """`_mentions_core`, що сама рахує основи (для чистих викликів без
    db_path — CLI/`find_mentions`/тести). Основи рахуються лише коли прапорець
    увімкнений: коли вимкнений, морфо-гілка гарантовано не запускається."""
    stems = _build_stems(names) if _morph_enabled() else {}
    return _mentions_core(text, names, stems)


def find_mentions(text: Optional[str], names: dict[str, int]) -> set[int]:
    """entity_id, згадані в тексті ЯК ВЛАСНІ НАЗВИ (точно чи, за увімкненого
    прапорця, за основою слова — див. `_mentions_core`)."""
    return set(_find_mentions_detailed(text, names))


def find_exact_mentions(text: Optional[str], names: dict[str, int]) -> set[int]:
    """`find_mentions`, що НІКОЛИ не запускає морфо-гілку — незалежно від
    `TG_ENTITIES_MORPH_ENABLED`. Для споживачів, чий замір бачить лише точний
    матчер (`comments.link_entities`): вмикання прапорця для інжесту TG не має
    тихо змінювати те, що пише інша підсистема."""
    return set(_mentions_core(text, names, {}))


def is_shouting(text: str, pos: int) -> bool:
    """Чи набраний рядок навколо позиції ВЕЛИКИМИ ЛІТЕРАМИ суцільно.

    У шапці договору («ЩОДО ТЕКСТІВ СТАТЕЙ У РОЗДІЛ WHAT WE DO») з великої
    написані всі слова підряд, тож велика літера там не відрізняє власну назву
    від загального слова — а саме на цій відмінності стоїть увесь модуль.
    """
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start:end if end != -1 else len(text)]
    letters = [ch for ch in line if ch.isalpha()]
    if len(letters) < 8:                       # надто коротко, щоб робити висновок
        return False
    return sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.8


def written_as_proper_noun(text: str, word: str, pos: int) -> bool:
    """Чи написане слово в цьому місці як власна назва.

    Одне визначення на два шари: `find_mentions` ставить за ним звʼязки, а
    аудит написань (`entity_dedup.find_junk_aliases`) за ним же рахує докази
    «це таки імʼя». Розійшовшись, вони починають суперечити один одному:
    аудит радив зняти написання, яке саме зараз дає звʼязки.
    """
    if not word[:1].isupper():
        return False
    before = text[:pos]
    # Велика літера на початку рядка чи речення нічого не доводить:
    # «Документ надіслано» — це не назва проєкту.
    if not before.strip() or _SENTENCE_END.search(before):
        return False
    return not is_shouting(text, pos)


#: Скільки секунд тримаємо словник назв між повідомленнями. Кеш потрібен, бо
#: на інжесті `load_names` — це 6200 рядків на КОЖНЕ повідомлення, а граф за
#: секунди не змінюється. Термін короткий свідомо: нова сутність із дзвінка або
#: злиття мають почати ловитись самі, без перезапуску застосунку.
_NAMES_TTL = float(os.environ.get("TG_ENTITIES_NAMES_TTL", "300"))

_names_lock = threading.Lock()
#: (timestamp, назви, основи, стан прапорця під яким основи побудовано).
#: Основи рахуються разом із назвами й живуть у тому самому кеші/TTL —
#: reset_names_cache() скидає обидва одним рухом. Четвертий елемент існує,
#: бо прапорець може перемкнутись МІЖ прогрівом і читанням кешу (тести,
#: увімкнення на живому процесі без рестарту): прогрів під OFF лишає слот
#: `{}` порожнім, і без цієї перевірки він віддавався б і під ON — морфо-
#: прохід тоді видаляє свої рядки і не має чим їх переписати.
_names_cache: dict[str, tuple[float, dict[str, int], dict[str, set[int]], bool]] = {}


def names_for(db_path: str, *, force: bool = False) -> dict[str, int]:
    """Словник назв із коротким кешем (див. `_NAMES_TTL`)."""
    now = time.monotonic()
    morph_now = _morph_enabled()
    with _names_lock:
        hit = _names_cache.get(db_path)
        if hit and not force and (now - hit[0]) < _NAMES_TTL and hit[3] == morph_now:
            return hit[1]
    names = load_names(db_path)          # поза локом: читання БД може бути довгим
    # Основи рахуємо лише за увімкненого прапорця — вимкнений матчер не
    # платить за них нічого, навіть на прогріві кешу.
    stems = _build_stems(names) if morph_now else {}
    with _names_lock:
        _names_cache[db_path] = (now, names, stems, morph_now)
    return names


def stems_for(db_path: str, *, force: bool = False) -> dict[str, set[int]]:
    """Основи слів для морфо-матчера — той самий кеш, що й `names_for`
    (прогріває/оновлює його за потреби)."""
    names_for(db_path, force=force)
    with _names_lock:
        return _names_cache[db_path][2]


def reset_names_cache() -> None:
    """Забути кеш назв і основ. Потрібне тестам і після масових змін у графі."""
    with _names_lock:
        _names_cache.clear()


def link_message(db_path: str, transcription_id: int) -> dict:
    """Звʼязати ОДНЕ повідомлення з графом — крок інжесту.

    Ідемпотентно: свої звʼязки цього запису переписуються, чужі (від
    Claude-збагачення, `source IS NULL`) не чіпаються. Викликається після
    правки повідомлення так само, як після першої появи: текст змінився —
    згадки могли зʼявитись або зникнути.

    Нитка тут НЕ потрібна (на відміну від повного проходу): згадка доводиться
    текстом самого повідомлення, а не тим, до якої розмови його віднесли. Якщо
    розкладання по нитках не спрацювало, запис усе одно потрапляє в граф.
    """
    names = names_for(db_path)
    if not names:
        return {"transcription_id": transcription_id, "status": "no_entities"}

    morph_on = _morph_enabled()
    stems = stems_for(db_path) if morph_on else {}

    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, transcript_text FROM transcriptions "
            "WHERE id = ? AND source_type = 'telegram' AND deleted_at IS NULL",
            (transcription_id,)).fetchone()
        if row is None:
            return {"transcription_id": transcription_id, "status": "not_found"}

        hits = _mentions_core(row["transcript_text"], names, stems)
        conn.execute("DELETE FROM meeting_entities WHERE source = ? AND transcription_id = ?",
                     (SOURCE, transcription_id))
        if morph_on:
            # Морфо-прохід чіпає лише свої рядки — точний SOURCE прибирається
            # окремим DELETE вище і морфо-гілки не стосується.
            conn.execute("DELETE FROM meeting_entities WHERE source = ? AND transcription_id = ?",
                         (SOURCE_MORPH, transcription_id))
        written = 0
        for eid, src in hits.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO meeting_entities "
                "(transcription_id, entity_id, mention_count, source) "
                "VALUES (?, ?, 1, ?)", (transcription_id, eid, src))
            written += cur.rowcount
        conn.commit()
    return {"transcription_id": transcription_id, "status": "ok",
            "mentions": len(hits), "written": written}


def link_threads(db_path: str, *, chat_id: Optional[int] = None,
                 dry_run: bool = True) -> dict:
    """Звʼязати нитки Telegram із наявними сутностями графа.

    Ідемпотентно: звʼязки з нашим провенансом переписуються, чужі (від
    Claude-збагачення, source IS NULL) не чіпаються — вони надійніші."""
    names = load_names(db_path)
    if not names:
        return {"error": "у графі немає сутностей для звʼязування"}

    morph_on = _morph_enabled()
    stems = _build_stems(names) if morph_on else {}

    where = ["source_type = 'telegram'", "deleted_at IS NULL", "tg_thread_id IS NOT NULL"]
    params: list = []
    if chat_id is not None:
        where.append("tg_chat_id = ?")
        params.append(chat_id)

    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, tg_thread_id, transcript_text FROM transcriptions "
            f"WHERE {' AND '.join(where)}", params).fetchall()

        # Згадка — там, де вона написана. Розширення до нитки живе окремо
        # (scope._expand_to_threads) і не має дублюватись у графі.
        per_message: dict[int, dict[int, str]] = {}
        threads_seen: set[int] = set()
        threads_touched: set[int] = set()
        for r in rows:
            threads_seen.add(r["tg_thread_id"])
            hits = _mentions_core(r["transcript_text"], names, stems)
            if hits:
                per_message[r["id"]] = hits
                threads_touched.add(r["tg_thread_id"])

        result = {"threads_total": len(threads_seen),
                  "threads_with_mentions": len(threads_touched),
                  "messages_total": len(rows), "messages_linked": len(per_message),
                  "entities": len({e for s in per_message.values() for e in s}),
                  "links": sum(len(s) for s in per_message.values())}
        if dry_run:
            return {"dry_run": True, **result}

        # Свої звʼязки прибираємо перед перезаписом; чужі лишаються. Точний і
        # морфо-провенанс приберемо окремими DELETE — морфо-прохід чіпає лише
        # свої рядки, коли прапорець вимкнений SOURCE_MORPH навіть не чіпаємо.
        own_sources = (SOURCE, SOURCE_MORPH) if morph_on else (SOURCE,)
        tids = [r["id"] for r in rows]
        for i in range(0, len(tids), 500):
            batch = tids[i:i + 500]
            ph = ",".join("?" * len(batch))
            for src in own_sources:
                conn.execute(
                    f"DELETE FROM meeting_entities WHERE source = ? AND transcription_id IN "
                    f"({ph})", [src, *batch])

        written = 0
        for tid, entity_map in per_message.items():
            for eid, src in entity_map.items():
                # INSERT OR IGNORE: якщо звʼязок уже поставив Claude —
                # лишаємо його версію (вона з salience і роллю).
                cur = conn.execute(
                    "INSERT OR IGNORE INTO meeting_entities "
                    "(transcription_id, entity_id, mention_count, source) "
                    "VALUES (?, ?, 1, ?)", (tid, eid, src))
                written += cur.rowcount
        conn.commit()

    return {"dry_run": False, **result, "written": written}


def stats(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        total_tg = conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE source_type='telegram' "
            "AND deleted_at IS NULL").fetchone()[0]
        linked_tg = conn.execute(
            "SELECT COUNT(DISTINCT me.transcription_id) FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE t.source_type='telegram'").fetchone()[0]
        by_source = dict(conn.execute(
            "SELECT COALESCE(source, 'claude'), COUNT(*) FROM meeting_entities "
            "GROUP BY 1").fetchall())
        top = conn.execute(
            "SELECT e.type, e.canonical_name, COUNT(DISTINCT me.transcription_id) n "
            "FROM meeting_entities me JOIN entities e ON e.id = me.entity_id "
            "WHERE me.source = ? GROUP BY e.id ORDER BY n DESC LIMIT 10",
            (SOURCE,)).fetchall()
    return {"telegram_total": total_tg, "telegram_linked": linked_tg,
            "links_by_source": by_source,
            "top_entities": [{"type": r["type"], "name": r["canonical_name"],
                              "records": r["n"]} for r in top]}


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

    p = argparse.ArgumentParser(prog="tg_entities",
                                description="Telegram у графі сутностей (Волна 4.5.3).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)
    l = sub.add_parser("link", help="Звʼязати нитки з наявними сутностями")
    l.add_argument("--dry-run", action="store_true")
    l.add_argument("--chat", type=int, default=None)
    sub.add_parser("stats", help="Покриття графа")

    args = p.parse_args(argv)
    if args.command == "link":
        return _print(link_threads(args.db, chat_id=args.chat, dry_run=args.dry_run))
    return _print(stats(args.db))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
