"""Зобовʼязання (Трек 1) — нормалізація дедлайнів, власників, stale/dedup + запити.

**Проблема, яку закриває модуль** (гриль-сесія 24.07.2026; бриф і план лежать
локально в `docs/grill/` та `docs/plans/` — у git їх немає навмисно, бо вони
описують конкретику особистого архіву):
`action_items.due` зберігався як сира фраза з розмови — «завтра», «четвер»,
«наступний тиждень», «Q3 2026» (426 різних значень на задачу, заповнено 29%).
Питання «що треба зробити мені/команді цього тижня» на таких даних не відповідається
в принципі: немає ні дати, ні надійного власника, а всі задачі висять у статусі
`open` (жодна не закривалась з травня).

**Що робить модуль:**

1. `parse_due()` — розгортає сиру фразу в ISO-дату відносно **дати зустрічі**
   (`transcriptions.meeting_date`), бо «завтра» 14 травня і «завтра» вчора — різні дні.
   Разом з датою повертає `precision`: `day|week|month|quarter|soon|next_meeting|
   recurring`. Це навмисно — «якнайшвидше» НЕ вдає точну дату, а стає `soon`;
   «до наступної зустрічі» лишається без дати (`next_meeting`). Фальшива точність
   у зводі гірша за чесне «терміну немає».
2. Offline-проходи (`backfill_due`, `link_owners`, `dedup`, `mark_stale`) — окремі,
   ідемпотентні, кожен має `--dry-run`. НЕ вбудовані в enrich, щоб не гальмувати
   основний шлях (той самий принцип, що `entity_dedup.py`).
3. Read-запити (`list_commitments`, `dropped_commitments`, `stale_topics`,
   `weekly_digest`) — спільне ядро для MCP-тулзів і UI-ендпоінтів.

**Сира фраза не переписується.** `due` лишається як є, обчислене йде в `due_date`/
`due_precision` — у видачі показуємо обидві, щоб помилка парсера була видимою.

**Залежності:** лише stdlib + `app.db.connection`. Модуль імпортується з
`mcp_server.py` (stdio-процес), тому НЕ тягне embeddings/torch — див.
memory `mcp-stdio-no-heavy-models`.

CLI:
    python -m app.services.commitments backfill --dry-run
    python -m app.services.commitments owners|dedup|stale|digest
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

# --- Класи точності ------------------------------------------------------------
P_DAY = "day"
P_WEEK = "week"
P_MONTH = "month"
P_QUARTER = "quarter"
P_SOON = "soon"                # «якнайшвидше», «найближчим часом» — дата орієнтовна
P_NEXT_MEETING = "next_meeting"  # «до наступної зустрічі» — дати немає принципово
P_RECURRING = "recurring"      # «щомісяця» — не дедлайн, а ритм
P_EVENT = "event"              # «після дзвінка з Acmecorp», «після свят» — привʼязка
                               # до події, а не до календаря: дата зʼявиться, коли
                               # подія станеться, вигадувати її тут — брехня

# Скільки днів вважати «найближчим часом» (для P_SOON). Три дні — компроміс:
# менше = задача одразу протермінована, більше = губиться відчуття терміновості.
SOON_DAYS = 3

# Дефолт для mark_stale: задача зі зустрічі, старшої за стільки днів, і без
# майбутнього терміну — вже не «цього тижня», а історія. 90 днів ≈ квартал.
STALE_DAYS = 90

# Чому задачу знято (`action_items.stale_reason`). Причин дві, і плутати їх не
# можна: вік — це «давно було», відсутність сліду — «про це більше не говорили».
# Друге доказове, і саме його можна відкотити гуртом, не чіпаючи перше.
REASON_AGE = "age"
REASON_NO_TRACE = "no_trace"

# Поріг схожості для dedup (частка спільних значущих токенів, Jaccard).
DEDUP_THRESHOLD = 0.75

# Стем → день тижня (0 = понеділок). Порівнюємо за префіксом, щоб покрити
# відмінки: «понеділка», «наступної пʼятниці», «до четверга».
_WEEKDAYS: dict[str, int] = {
    "понеділ": 0, "понедельник": 0,
    "вівтор": 1, "вторник": 1, "второк": 1,
    "серед": 2, "сред": 2,
    "четвер": 3, "четверг": 3,
    "п'ятниц": 4, "пятниц": 4, "пʼятниц": 4,
    "субот": 5, "суббот": 5,
    "неділ": 6, "недел": 6, "воскрес": 6,
}

_MONTHS: dict[str, int] = {
    "січ": 1, "янв": 1, "january": 1,
    "лют": 2, "февр": 2, "february": 2,
    "берез": 3, "март": 3, "march": 3,
    "квіт": 4, "апрел": 4, "april": 4,
    "трав": 5, "мая": 5, "май": 5, "may": 5,
    "черв": 6, "июн": 6, "june": 6,
    "лип": 7, "июл": 7, "july": 7,
    "серп": 8, "август": 8, "august": 8,
    "верес": 9, "сентябр": 9, "september": 9,
    "жовт": 10, "октябр": 10, "october": 10,
    "листопад": 11, "ноябр": 11, "november": 11,
    "груд": 12, "декабр": 12, "december": 12,
}

# Фрази, що НЕ дають дати, але дають клас.
_NEXT_MEETING_RE = re.compile(
    r"наступн\w*\s+(зустріч|встреч|дзвінк|дзвонк|звонк|созвон|колл|call)|"
    r"наступн\w*\s+(міт|мит)|на\s+наступн\w*\s+(зустріч|дзвінк)")
_RECURRING_RE = re.compile(r"^(що\w+|раз\s+(в|на)\s+\w+|регулярно|постійно|кожн\w+)")
_SOON_RE = re.compile(
    r"якнайшвидше|якомога\s+швидше|найближч\w*\s+час|найближчим\s+часом|термінов|срочн|"
    r"асап|asap|негайно|як\s+тільки|найближч\w*\s+дн|кілька\s+дн|декілька\s+дн")
# Термін, привʼязаний до події («після дзвінка», «до зустрічі з Толіком»).
# `після` з обовʼязковим пробілом, щоб не зачепити «післязавтра».
_EVENT_RE = re.compile(
    r"\bпісля\s+|\bпосле\s+|по\s+завершенн|по\s+підсумк|"
    # «до зустрічі з Толіком»: службове «до» вже зрізане в _norm_phrase, тому
    # ловимо і голу форму «зустрічі з …».
    r"(^|\s)(зустріч|дзвінк|мітинг|кол)\w*\s+з\s+|найближч\w*\s+мітинг")
# Абревіатури днів тижня — ТІЛЬКИ як цілий токен: за префіксом «ср» зловило б
# «срочно», «пт» — «птн» тощо.
_WEEKDAY_ABBR = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "нд": 6, "вс": 6}

_STOPWORDS = {
    "щоб", "який", "яка", "яке", "які", "цього", "цей", "для", "про", "над", "під",
    "від", "при", "буде", "було", "має", "тому", "тобто", "також", "після", "перед",
    "разом", "потім", "тоді", "щодо", "чтобы", "который", "этого", "перед", "после",
}


# ============================================================
# Парсер дедлайну
# ============================================================

def _norm_phrase(raw: str) -> str:
    """Нормалізація сирої фрази: lowercase, апострофи, час і дужки геть."""
    s = (raw or "").strip().lower()
    s = s.replace("ʼ", "'").replace("’", "'").replace("`", "'")
    s = re.sub(r"\([^)]*\)", " ", s)              # «07.04.2026 (вівторок)»
    # Час прибираємо ЛИШЕ у формі з двокрапкою: крапкова форма нерозрізнима з
    # датою («27.03.2026» — не 27 годин 03 хвилини), і саме на ній парсер раніше
    # мовчки втрачав усі dd.mm-дати.
    s = re.sub(r"\b([01]?\d|2[0-3]):[0-5]\d\b", " ", s)   # «завтра до 11:30»
    s = re.sub(r"\b(о|в|до)\s+\d{1,2}\s*(год|часов|ч)\b", " ", s)
    s = re.sub(r"[«»\"]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:!?-–—")
    # Прибрати службові прийменники на початку — вони не змінюють дату.
    s = re.sub(r"^(до|не\s+пізніше|не\s+позднее|станом\s+на|desired|by)\s+", "", s)
    return s.strip()


def _end_of_month(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _week_end(anchor: date) -> date:
    """Пʼятниця тижня anchor (робочий кінець тижня, не неділя)."""
    return anchor + timedelta(days=(4 - anchor.weekday()))


def _next_weekday(anchor: date, wd: int, *, next_week: bool = False) -> date:
    """Найближче настання дня тижня СТРОГО після anchor (anchor+1..anchor+7)."""
    delta = (wd - anchor.weekday()) % 7
    if delta == 0:
        delta = 7
    d = anchor + timedelta(days=delta)
    if next_week and d.isocalendar()[:2] == anchor.isocalendar()[:2]:
        d += timedelta(days=7)
    return d


def _match_prefix(token: str, table: dict[str, int]) -> Optional[int]:
    for stem, val in table.items():
        if token.startswith(stem):
            return val
    return None


def _find_weekday(s: str) -> Optional[int]:
    """День тижня у фразі. «середина» НЕ вважається середою."""
    for token in re.findall(r"[а-яіїєґёa-z']+", s):
        if token in _WEEKDAY_ABBR:
            return _WEEKDAY_ABBR[token]
        if token.startswith("середин"):
            continue
        wd = _match_prefix(token, _WEEKDAYS)
        if wd is not None:
            return wd
    return None


def _find_month(s: str) -> Optional[int]:
    for token in re.findall(r"[а-яіїєґёa-z]+", s):
        m = _match_prefix(token, _MONTHS)
        if m is not None:
            return m
    return None


def _parse_one(s: str, anchor: date) -> Optional[tuple[Optional[date], str]]:
    """Розібрати ОДНУ (вже нормалізовану) фразу. None — не розпізнано."""
    if not s:
        return None

    # --- класи без дати (перевіряємо ПЕРШИМИ) ---------------------------------
    # Інакше «після завтрашньої стратсесії» стало б просто «завтра», втративши
    # умову, від якої термін насправді залежить.
    if _RECURRING_RE.search(s):
        return (None, P_RECURRING)
    if _NEXT_MEETING_RE.search(s):
        return (None, P_NEXT_MEETING)
    if _EVENT_RE.search(s):
        return (None, P_EVENT)

    # --- явні дати ------------------------------------------------------------
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        try:
            return (date(int(m[1]), int(m[2]), int(m[3])), P_DAY)
        except ValueError:
            return None
    m = re.search(r"\b(\d{4})-(\d{2})\b", s)
    if m and 1 <= int(m[2]) <= 12:
        return (_end_of_month(int(m[1]), int(m[2])), P_MONTH)
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", s)
    if m:
        year = int(m[3])
        year += 2000 if year < 100 else 0
        try:
            return (date(year, int(m[2]), int(m[1])), P_DAY)
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})\b(?!\s*[./]\d)", s)
    if m and 1 <= int(m[2]) <= 12:
        try:
            d = date(anchor.year, int(m[2]), int(m[1]))
        except ValueError:
            return None
        if d < anchor:
            d = date(anchor.year + 1, d.month, d.day)
        return (d, P_DAY)

    # --- «5 січня», «21 квітня 2026», «середина січня 2026» -------------------
    month = _find_month(s)
    if month is not None:
        year_m = re.search(r"\b(20\d{2})\b", s)
        year = int(year_m[1]) if year_m else anchor.year
        day_m = re.search(r"\b(\d{1,2})\b(?!\s*[:.]\d)", s)
        if "середин" in s:
            d = date(year, month, 15)
            prec = P_WEEK
        elif day_m and 1 <= int(day_m[1]) <= 31 and not re.search(r"\b(20\d{2})\b", day_m[0]):
            try:
                d = date(year, month, int(day_m[1]))
            except ValueError:
                return None
            prec = P_DAY
        else:
            d = _end_of_month(year, month)
            prec = P_MONTH
        if not year_m and d < anchor:
            # Місяць без року і вже в минулому → наступний рік.
            try:
                d = d.replace(year=year + 1)
            except ValueError:
                d = _end_of_month(year + 1, month)
        return (d, prec)

    # --- квартали -------------------------------------------------------------
    m = re.search(r"\bq([1-4])\b|\b([1-4])\s*(?:кв|квартал)", s)
    if m:
        q = int(m[1] or m[2])
        year_m = re.search(r"\b(20\d{2})\b", s)
        year = int(year_m[1]) if year_m else anchor.year
        d = _end_of_month(year, q * 3)
        if not year_m and d < anchor:
            d = _end_of_month(year + 1, q * 3)
        return (d, P_QUARTER)

    # --- «через N днів/тижнів/місяців» ----------------------------------------
    m = re.search(r"через\s+(\d+)\s*(дн|тижн|недел|місяц|месяц)", s)
    if m:
        n = int(m[1])
        unit = m[2]
        if unit.startswith("дн"):
            return (anchor + timedelta(days=n), P_DAY)
        if unit.startswith(("тижн", "недел")):
            return (anchor + timedelta(weeks=n), P_WEEK)
        return (_end_of_month(*_add_months(anchor, n)), P_MONTH)
    # «через тиждень» / «протягом тижня» — це +7 днів від зустрічі, а НЕ пʼятниця
    # поточного тижня (загальна гілка «тижн» нижче), тому ловимо окремо.
    if re.search(r"(через|протягом)\s+(тиждень|тижня|неделю|недели)", s):
        return (anchor + timedelta(days=7), P_WEEK)
    m = re.search(r"протягом\s+(\d+)(?:\s*-\s*(\d+))?\s*(дн|тижн)", s)
    if m:
        n = int(m[2] or m[1])
        return ((anchor + timedelta(days=n), P_DAY) if m[3].startswith("дн")
                else (anchor + timedelta(weeks=n), P_WEEK))

    # --- «з 15-го числа» ------------------------------------------------------
    m = re.search(r"\b(\d{1,2})\s*-?\s*(?:го|е)?\s*числа", s)
    if m:
        day = int(m[1])
        try:
            d = date(anchor.year, anchor.month, day)
        except ValueError:
            return None
        if d < anchor:
            y, mth = _add_months(anchor, 1)
            d = date(y, mth, min(day, monthrange(y, mth)[1]))
        return (d, P_DAY)

    # --- дні відносно anchor --------------------------------------------------
    if re.search(r"післязавтра|послезавтра", s):
        return (anchor + timedelta(days=2), P_DAY)
    # «наступного дня», «ранок наступного дня», «до кінця наступного дня» — це
    # завтра відносно зустрічі. Має стояти ДО загальної гілки «наступн…тиждень».
    if re.search(r"наступн\w*\s+(дн|день)|следующ\w*\s+(дн|день)", s):
        return (anchor + timedelta(days=1), P_DAY)
    if re.search(r"кін(ец|ц)\w*\s+дня|до\s+кінця\s+дня|протягом\s+дня", s):
        return (anchor, P_DAY)
    if re.search(r"\bзавтра\b", s):
        return (anchor + timedelta(days=1), P_DAY)
    if re.search(r"сьогодні|сегодня|today", s):
        # «сьогодні-завтра» вже покрито гілкою «завтра» вище (діапазон → пізніша межа)
        return (anchor, P_DAY)

    # --- тижні ----------------------------------------------------------------
    next_week = bool(re.search(r"наступн|следующ|майбутн", s))
    if re.search(r"вихідн|выходн", s):
        base = anchor + timedelta(days=(5 - anchor.weekday()) % 7 or 7)
        return (base + timedelta(days=7) if next_week else base, P_DAY)
    wd = _find_weekday(s)
    if wd is not None:
        return (_next_weekday(anchor, wd, next_week=next_week), P_DAY)
    if re.search(r"тижн|тиждень|недел", s):
        we = _week_end(anchor)
        return ((we + timedelta(days=7), P_WEEK) if next_week else (we, P_WEEK))

    # --- місяці/роки без назви ------------------------------------------------
    if re.search(r"місяц|месяц", s):
        if next_week:  # «наступного місяця»
            y, mth = _add_months(anchor, 1)
            return (_end_of_month(y, mth), P_MONTH)
        return (_end_of_month(anchor.year, anchor.month), P_MONTH)
    if re.search(r"\bрок[уі]\b|\bгод[уа]\b", s):
        year = anchor.year + 1 if next_week else anchor.year
        return (date(year, 12, 31), P_MONTH)

    # --- «найближчим часом» — орієнтир, а не дата -----------------------------
    if _SOON_RE.search(s):
        return (anchor + timedelta(days=SOON_DAYS), P_SOON)

    return None


def _add_months(d: date, n: int) -> tuple[int, int]:
    total = (d.year * 12 + (d.month - 1)) + n
    return total // 12, total % 12 + 1


def parse_due(raw: Optional[str], anchor: Optional[str | date]) -> tuple[Optional[str], Optional[str]]:
    """Сира фраза дедлайну → (ISO-дата | None, precision | None).

    Args:
        raw: значення `action_items.due` як його дав Claude («завтра», «Q3 2026»).
        anchor: дата зустрічі (ISO-рядок або date) — точка відліку для відносних
            виразів. Без неї відносні фрази не розбираються (тільки абсолютні).

    Returns:
        (None, None) — не розпізнано; (None, precision) — клас без дати
        (`next_meeting`/`recurring`); (iso, precision) — розібрано.
    """
    s = _norm_phrase(raw or "")
    if not s:
        return (None, None)

    if isinstance(anchor, str):
        try:
            anchor_d = date.fromisoformat(anchor[:10])
        except (ValueError, TypeError):
            anchor_d = None
    else:
        anchor_d = anchor
    if anchor_d is None:
        anchor_d = date.today()

    # Діапазон («понеділок-вівторок», «четвер-п'ятниця», «сьогодні-завтра»):
    # беремо ПІЗНІШУ межу — це і є дедлайн.
    parts = [p.strip() for p in re.split(r"\s*[-–—/]\s*|\s+або\s+|\s+или\s+", s) if p.strip()]
    results: list[tuple[Optional[date], str]] = []
    if len(parts) > 1:
        for p in parts:
            r = _parse_one(p, anchor_d)
            if r:
                results.append(r)
    if not results:
        r = _parse_one(s, anchor_d)
        if r:
            results.append(r)
    if not results:
        return (None, None)

    dated = [r for r in results if r[0] is not None]
    if not dated:
        return (None, results[0][1])
    best = max(dated, key=lambda r: r[0])
    return (best[0].isoformat(), best[1])


# ============================================================
# Offline-проходи
# ============================================================

def _rows(conn, sql: str, params: Iterable = ()) -> list:
    return conn.execute(sql, tuple(params)).fetchall()


def backfill_due(db_path: str, *, dry_run: bool = False, limit: Optional[int] = None) -> dict:
    """Заповнити due_date/due_precision для наявних задач (ідемпотентно)."""
    stats = {"scanned": 0, "parsed": 0, "unparsed": 0, "updated": 0, "by_precision": {}}
    samples: list[dict] = []
    sql = (
        "SELECT ai.id, ai.due, ai.due_date, ai.due_precision, "
        "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS anchor "
        "FROM action_items ai LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
        "WHERE ai.due IS NOT NULL AND trim(ai.due) <> ''"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    with get_db_connection(db_path) as conn:
        rows = _rows(conn, sql)
        updates: list[tuple] = []
        for r in rows:
            stats["scanned"] += 1
            iso, prec = parse_due(r["due"], r["anchor"])
            if prec is None:
                stats["unparsed"] += 1
                continue
            stats["parsed"] += 1
            stats["by_precision"][prec] = stats["by_precision"].get(prec, 0) + 1
            if r["due_date"] == iso and r["due_precision"] == prec:
                continue
            updates.append((iso, prec, r["id"]))
            if len(samples) < 25:
                samples.append({"id": r["id"], "raw": r["due"], "anchor": r["anchor"],
                                "due_date": iso, "precision": prec})
        if updates and not dry_run:
            conn.executemany(
                "UPDATE action_items SET due_date = ?, due_precision = ? WHERE id = ?",
                updates)
            conn.commit()
        stats["updated"] = len(updates)
    stats["samples"] = samples
    logger.info("backfill_due: scanned=%(scanned)s parsed=%(parsed)s unparsed=%(unparsed)s "
                "updated=%(updated)s", stats)
    return stats


def _normalize_name(name: str) -> str:
    """Той самий ключ, що enrichment._normalize (свідома копія 6 рядків: цей
    модуль імпортується з stdio-MCP і не має тягнути embeddings/torch)."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t\n\r.,;:!?\"'`«»()[]{}-–—")


def link_owners(db_path: str, *, dry_run: bool = False) -> dict:
    """Прив'язати owner_name → person-сутність (це ж зливає «Юлія»/«Юля»)."""
    stats = {"scanned": 0, "linked": 0, "unmatched": {}}
    with get_db_connection(db_path) as conn:
        index: dict[str, int] = {}
        # Сутності, чиє ВЛАСНЕ імʼя — одне слово («Юлія», «Андрій»). Тільки вони
        # годяться для фолбеку по імені: аліаси для цього непридатні, бо серед
        # них трапляється сміття збагачення — сутності «Andrij Kovalenko» з
        # ютуб-лекції дістався аліас «Andrei», і через нього 43 задачі власника
        # архіву («Andrii Melnyk» → перший токен «andrei») поїхали під
        # чужим імʼям. Тезка, записана повним імʼям, теж не підходить: «Андрій
        # Ткаченко» не робить «Андрія» з розмови Ткаченкоом. Двох однослівних тезок
        # бути не може — схема тримає UNIQUE(type, normalized_name).
        short: dict[str, int] = {}
        for r in _rows(conn, "SELECT id, normalized_name FROM entities WHERE type = 'person'"):
            if r["normalized_name"]:
                index.setdefault(r["normalized_name"], r["id"])
                if " " not in r["normalized_name"]:
                    short.setdefault(r["normalized_name"], r["id"])
        for r in _rows(conn, "SELECT a.entity_id, a.normalized_alias FROM entity_aliases a "
                             "JOIN entities e ON e.id = a.entity_id WHERE e.type = 'person'"):
            if r["normalized_alias"]:
                index.setdefault(r["normalized_alias"], r["entity_id"])

        updates = []
        for r in _rows(conn, "SELECT id, owner_name FROM action_items "
                             "WHERE owner_entity_id IS NULL AND owner_name IS NOT NULL "
                             "AND trim(owner_name) <> ''"):
            stats["scanned"] += 1
            key = _normalize_name(r["owner_name"])
            eid = index.get(key)
            if eid is None and " " in key:            # «Юля Гончар» → «юля»
                eid = short.get(key.split(" ")[0])
            if eid is None:
                stats["unmatched"][key] = stats["unmatched"].get(key, 0) + 1
                continue
            updates.append((eid, r["id"]))
        if updates and not dry_run:
            conn.executemany("UPDATE action_items SET owner_entity_id = ? WHERE id = ?", updates)
            conn.commit()
        stats["linked"] = len(updates)
    stats["unmatched"] = dict(sorted(stats["unmatched"].items(), key=lambda kv: -kv[1])[:20])
    logger.info("link_owners: scanned=%(scanned)s linked=%(linked)s", stats)
    return stats


def relink_owners(db_path: str, *, dry_run: bool = True) -> dict:
    """Перевісити задачі, коли для їхнього `owner_name` зʼявилася ТОЧНІША людина.

    `link_owners` заповнює лише порожнє і має фолбек по першому токену: «Dmytro
    Lebid» без свого рядка в графі осідав на сутності «Dmytro». Далі його вже
    ніщо не знімало — прив'язка робиться один раз, і 40 задач Лебедя лишались
    висіти на рядку, куди фолбек ще й доклав 34 задачі зовнішнього юриста
    «Dmytro S». Свод «хто кому що винен» показував одну людину замість двох.

    Тут рухаємо тільки те, що доведено ПОВНИМ написанням: `owner_name` збігся з
    канонічним іменем або аліасом сутності цілком. Здогадів немає взагалі —
    фолбеку по першому токену тут свідомо НЕ повторюємо, інакше перевішування
    ганяло б задачі між тезками на кожному проході.

    Ідемпотентна: другий прогін не рухає нічого.
    """
    stats = {"dry_run": dry_run, "scanned": 0, "relinked": 0, "moves": {}}
    with get_db_connection(db_path) as conn:
        exact: dict[str, int] = {}
        for r in _rows(conn, "SELECT id, normalized_name FROM entities WHERE type = 'person'"):
            if r["normalized_name"]:
                exact.setdefault(r["normalized_name"], r["id"])
        for r in _rows(conn, "SELECT a.entity_id, a.normalized_alias FROM entity_aliases a "
                             "JOIN entities e ON e.id = a.entity_id WHERE e.type = 'person'"):
            if r["normalized_alias"]:
                exact.setdefault(r["normalized_alias"], r["entity_id"])

        names = {r["id"]: r["canonical_name"] for r in
                 _rows(conn, "SELECT id, canonical_name FROM entities")}
        updates = []
        for r in _rows(conn, "SELECT id, owner_name, owner_entity_id FROM action_items "
                             "WHERE owner_entity_id IS NOT NULL AND owner_name IS NOT NULL "
                             "AND trim(owner_name) <> ''"):
            stats["scanned"] += 1
            eid = exact.get(_normalize_name(r["owner_name"]))
            if eid is None or eid == r["owner_entity_id"]:
                continue
            updates.append((eid, r["id"]))
            key = (f"{r['owner_name']}: #{r['owner_entity_id']} "
                   f"'{names.get(r['owner_entity_id'], '?')}' → #{eid} '{names.get(eid, '?')}'")
            stats["moves"][key] = stats["moves"].get(key, 0) + 1
        if updates and not dry_run:
            conn.executemany("UPDATE action_items SET owner_entity_id = ? WHERE id = ?",
                             updates)
            conn.commit()
        stats["relinked"] = len(updates)
    stats["moves"] = dict(sorted(stats["moves"].items(), key=lambda kv: -kv[1]))
    # Прикидка і запис мусять звучати по-різному: інакше оператор бачить
    # «relinked=40», нічого не застосовує і думає, що задачі вже переїхали.
    logger.info("relink_owners: scanned=%s %s=%s", stats["scanned"],
                "would_relink" if dry_run else "relinked", stats["relinked"])
    return stats


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яіїєґёa-z0-9']{4,}", (text or "").lower())
            if w not in _STOPWORDS}


def _term_df(conn, term: str, *, before: Optional[str] = None,
             cache: Optional[dict] = None) -> Optional[int]:
    """У скількох чанках трапляється слово. None — якщо FTS не зрозумів токен.

    `before` обмежує рахунок записами НЕ ПІЗНІШЕ вказаної дати, і це не
    оптимізація, а вимога коректності: рідкість, порахована по всьому архіву,
    підіграє вердикту «упущено». Слово, яке після задачі якраз і спливало, має
    через ті згадки БІЛЬШИЙ df, тож вибір «найрідкісніших» його відкидає — і
    лишає ті слова, яких потім не було. Тобто метрика сама собі доводить
    відсутність. Замір на живому архіві: «ендавменту» 2 проти «стратегії» 1 у
    крихітному корпусі — і задача, чий предмет ПОВЕРНУВСЯ, оголошувалась
    загубленою.

    `cache` — словник на один виклик: задачі однієї зустрічі ділять і дату, і
    половину слів, тож повторний рахунок був би платою ні за що.
    """
    key = (term, before)
    if cache is not None and key in cache:
        return cache[key]
    try:
        if before is None:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks_fts WHERE chunks_fts MATCH ?",
                (f'"{term}"',)).fetchone()["n"]
        else:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks_fts f "
                "JOIN chunks ch ON ch.id = f.rowid "
                "JOIN transcriptions t ON t.id = ch.transcription_id "
                "WHERE f.chunks_fts MATCH ? AND t.deleted_at IS NULL "
                "AND COALESCE(t.meeting_date, substr(t.created_at,1,10)) <= ?",
                (f'"{term}"', before)).fetchone()["n"]
    except Exception as exc:                   # екзотичні токени ламають синтаксис
        logger.debug("_term_df: пропущено %r (%s)", term, exc)
        n = None
    if cache is not None:
        cache[key] = n
    return n


def _rare_terms(conn, task: str, *, limit: int = 2,
                before: Optional[str] = None, cache: Optional[dict] = None) -> list[str]:
    """Слова, за якими задачу можна впізнати в іншому тексті — НАЙРІДКІСНІШІ.

    Раніше тут стояли найдовші слова, і це давало систематичну брехню: найдовше
    слово української задачі — це загальне дієслово («продовжувати»,
    «організувати», «переглянути»), а впізнає задачу коротка власна назва
    («BUEF», «Archicad», «Notion»). Заміряно на живих даних: 15 із них
    (7.5%) оголошувались «живими» лише через загальне слово — «Продовжувати
    роботу по ендавменту BUEF» вважалась спливлою, бо «стратегії» трапляється в
    архіві 509 разів. Помилок у зворотний бік — нуль. Тобто «що ми упустили»
    мовчало саме там, де мало говорити.

    Рідкість беремо з того самого індексу, яким потім шукаємо (FTS5): це і
    точно, і дешево — лічильник іде по індексу, не по текстах.

    Слова, яких у корпусі НЕМА (df = 0), відкидаємо навмисно: нуль влучань по
    них нічого не доводить. Помилка розпізнавання («ресепшіоніст» замість
    «ресепціоніст») інакше робила б будь-яку задачу «упущеною».

    **Дивимось УСІ значущі слова, а не найдовші з них.** Перша версія цієї
    функції зберігала попередній відбір «12 найдовших» — і тим самим викидала
    рівно ті короткі власні назви, заради яких її й писали: у задачі з довгим
    формулюванням «buef» (4 символи) не доживав до підрахунку рідкості. Плюс
    зріз ішов по множині, тобто межа залежала від порядку обходу `set` і вибір
    термінів не відтворювався між запусками. Лічильник по індексу коштує
    мікросекунди — економити на ньому не було на чому.

    Сортуємо ЛИШЕ за рідкістю на момент задачі, а рівних розводимо за абеткою.
    Спокуса додати другим ключем рідкість у всьому архіві («на ранній задачі
    корпус тонкий, і звичайне слово виглядає рідкісним») — пастка: у повній
    рідкості сидять і згадки ПІСЛЯ задачі, тож слово, яке потім спливало, має
    більший df і таким ключем програє тому, яке не спливало. Це рівно те
    підігрування вердикту, задля усунення якого й існує `before`. Тест
    `..._stays_silent_when_the_subject_really_came_back` ловить саме це.

    Лишається відома межа: на найраніших задачах корпус ще малий, і звичайне
    слово може стати «найрідкіснішим». Помилка ця йде в безпечний бік — таке
    слово знайдеться пізніше, і задача лишиться відкритою.
    """
    scored: list[tuple[int, str]] = []
    for term in sorted(_tokens(task)):         # sorted — щоб вибір відтворювався
        df = _term_df(conn, term, before=before, cache=cache)
        if df:                                 # None або 0 — доказом бути не може
            scored.append((df, term))
    scored.sort()
    return [t for _, t in scored[:limit]]


def dedup(db_path: str, *, dry_run: bool = False, threshold: float = DEDUP_THRESHOLD) -> dict:
    """Позначити повтори однієї задачі з різних зустрічей (dup_of → найсвіжіша).

    Статус не змінюється: дублі лишаються в БД і доступні за прямим запитом,
    але зникають зі зводів (ті фільтрують dup_of IS NULL).
    """
    stats = {"groups": 0, "marked": 0, "samples": []}
    with get_db_connection(db_path) as conn:
        rows = _rows(conn,
                     "SELECT ai.id, ai.task, ai.owner_entity_id, ai.owner_name, "
                     "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS anchor "
                     "FROM action_items ai LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
                     "WHERE ai.status = 'open' AND ai.dup_of IS NULL "
                     "AND t.deleted_at IS NULL ORDER BY ai.id")
        buckets: dict[str, list] = {}
        for r in rows:
            key = (str(r["owner_entity_id"]) if r["owner_entity_id"]
                   else _normalize_name(r["owner_name"] or ""))
            buckets.setdefault(key, []).append(r)

        updates: list[tuple] = []
        for items in buckets.values():
            toks = {r["id"]: _tokens(r["task"]) for r in items}
            seen: set[int] = set()
            for i, a in enumerate(items):
                if a["id"] in seen or not toks[a["id"]]:
                    continue
                group = [a]
                for b in items[i + 1:]:
                    if b["id"] in seen or not toks[b["id"]]:
                        continue
                    ta, tb = toks[a["id"]], toks[b["id"]]
                    jac = len(ta & tb) / len(ta | tb)
                    if jac >= threshold:
                        group.append(b)
                        seen.add(b["id"])
                if len(group) < 2:
                    continue
                stats["groups"] += 1
                canonical = max(group, key=lambda r: ((r["anchor"] or ""), r["id"]))
                for r in group:
                    if r["id"] != canonical["id"]:
                        updates.append((canonical["id"], r["id"]))
                if len(stats["samples"]) < 15:
                    stats["samples"].append({
                        "keep": {"id": canonical["id"], "task": canonical["task"][:80]},
                        "dups": [{"id": r["id"], "task": r["task"][:80]}
                                 for r in group if r["id"] != canonical["id"]],
                    })
        if updates and not dry_run:
            conn.executemany("UPDATE action_items SET dup_of = ? WHERE id = ?", updates)
            conn.commit()
        stats["marked"] = len(updates)
    logger.info("dedup: groups=%(groups)s marked=%(marked)s", stats)
    return stats


def mark_stale(db_path: str, *, days: int = STALE_DAYS, dry_run: bool = False,
               today: Optional[date] = None) -> dict:
    """Задачі зі старих зустрічей без майбутнього терміну → status='stale'.

    Не видалення: `stale` лишається доступним прямим запитом (`status='stale'`),
    але не засмічує понеділковий звід. Задача з датою в майбутньому (напр.
    «Q3 2026») stale НЕ стає, навіть якщо зустріч давня.
    """
    t = today or date.today()
    cutoff = (t - timedelta(days=days)).isoformat()
    today_iso = t.isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection(db_path) as conn:
        rows = _rows(conn,
                     "SELECT ai.id FROM action_items ai "
                     "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
                     "WHERE ai.status = 'open' "
                     "AND COALESCE(t.meeting_date, substr(t.created_at,1,10)) < ? "
                     "AND (ai.due_date IS NULL OR ai.due_date < ?)",
                     (cutoff, today_iso))
        ids = [r["id"] for r in rows]
        if ids and not dry_run:
            conn.executemany(
                "UPDATE action_items SET status = 'stale', stale_at = ?, "
                "stale_reason = ? WHERE id = ?",
                [(now, REASON_AGE, i) for i in ids])
            conn.commit()
    logger.info("mark_stale: days=%s marked=%s", days, len(ids))
    return {"days": days, "marked": len(ids), "cutoff": cutoff}


def sweep_dropped(db_path: str, *, days: int = 180, min_age_days: int = 30,
                  dry_run: bool = True, undated_only: bool = False,
                  sources: Optional[Sequence[str]] = None,
                  today: Optional[date] = None) -> dict:
    """Зняти обіцянки, чия тема замовкла: status='open' → 'stale' (no_trace).

    Чому це взагалі потрібно. `status='open'` в архіві означає не «висить», а
    «ніхто ніколи не закривав»: `done` стоїть у ОДНІЄЇ задачі з 3879. Живих
    задач 1721, з них 1242 без дати — і ці 1242 не потрапляють у жодне вікно
    зводу за датою. Зняти їх за віком (`mark_stale`) — це чекати 90 днів і
    ховати разом і живе, і мертве.

    Критерій тут інший і чесніший: **після зустрічі тема більше не спливала в
    жодному джерелі**. Ось таке і треба зняти, а не «виконати» — рамка продукту
    говорить про звід зобовʼязань із хвостом, де більшість рядків ніколи не
    будуть виконані.

    **Скільки це знімає.** Заміром на живих даних без дати одне найрідкісніше
    слово оголошувало замовклими 38%. Прохід знімає ВТРИЧІ менше (на живому
    архіві 93 з 1154, тобто 8%), і це не розбіжність, а різні критерії: тут
    мусять НЕ спливти ОБИДВА терміни. Строгість навмисна — рішення пишеться в
    БД, тож помиляємось на користь «лишити відкритою».

    Відрізняється від `dropped_commitments` (той самий критерій) двома речами,
    бо там видача для читання, а тут прохід по завалу:
      * немає стелі на зустріч і немає ліміту — знімаємо все, що підпадає;
      * карантин довший (30 днів проти 14): рішення пише в БД, тож помилятись
        на користь «лишити відкритою».

    Задача з датою В МАЙБУТНЬОМУ не знімається ніколи — як і в `mark_stale`.
    `undated_only` звужує прохід до задач без дати взагалі.

    Ідемпотентний: вдруге не знайде нічого (рядки вже не 'open'). Зворотна дія —
    `UPDATE action_items SET status='open', stale_at=NULL, stale_reason=NULL
    WHERE stale_reason='no_trace'`.
    """
    t = today or date.today()
    lo = (t - timedelta(days=days)).isoformat()
    hi = (t - timedelta(days=min_age_days)).isoformat()
    today_iso = t.isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    src = tuple(sources) if sources else _OWN_SOURCES

    where = ["ai.status = 'open'", "ai.dup_of IS NULL", "t.deleted_at IS NULL",
             "COALESCE(t.meeting_date, substr(t.created_at,1,10)) BETWEEN ? AND ?",
             "(ai.due_date IS NULL OR ai.due_date < ?)"]
    params: list = [lo, hi, today_iso]
    if "all" not in src:
        where.append("t.source_type IN (%s)" % ",".join("?" * len(src)))
        params += list(src)
    if undated_only:
        where.append("ai.due_date IS NULL")

    stat = {"dry_run": dry_run, "candidates": 0, "marked": 0, "alive": 0,
            "unjudged": 0, "undated": 0, "dated": 0, "window": [lo, hi],
            "samples": []}
    df_cache: dict = {}
    with get_db_connection(db_path) as conn:
        rows = _rows(conn,
                     "SELECT ai.id, ai.task, ai.due_date, t.id AS tid, "
                     "COALESCE(e.canonical_name, ai.owner_name) AS owner, "
                     "t.source_name AS meeting, "
                     "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date "
                     "FROM action_items ai "
                     "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
                     "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
                     f"WHERE {' AND '.join(where)} ORDER BY meeting_date, ai.id",
                     params)
        stat["candidates"] = len(rows)
        doomed: list[int] = []
        for r in rows:
            terms = _rare_terms(conn, r["task"], before=r["meeting_date"], cache=df_cache)
            if len(terms) < 2:
                # Нема за чим судити — лишаємо відкритою. Мовчазне зняття
                # «про всяк випадок» знищило б рівно ті задачі, про які ми
                # найменше знаємо.
                stat["unjudged"] += 1
                continue
            fts_q = " OR ".join(f'"{w}"' for w in terms)
            try:
                hit = conn.execute(
                    # JOIN, а не rowid IN (SELECT …): друга форма змушує SQLite
                    # щоразу будувати повний список чанків архіву — 228 мс проти
                    # 0.1 мс на запит, тобто чотири хвилини на прохід замість секунд.
                    "SELECT COUNT(*) AS n FROM chunks_fts f "
                    "JOIN chunks ch ON ch.id = f.rowid "
                    "JOIN transcriptions tt ON tt.id = ch.transcription_id "
                    "WHERE f.chunks_fts MATCH ? AND tt.deleted_at IS NULL AND tt.id <> ? "
                    "AND COALESCE(tt.meeting_date, substr(tt.created_at,1,10)) > ?",
                    (fts_q, r["tid"], r["meeting_date"] or lo)).fetchone()["n"]
            except Exception as exc:
                logger.debug("sweep_dropped: FTS skip id=%s (%s)", r["id"], exc)
                stat["unjudged"] += 1
                continue
            if hit:
                stat["alive"] += 1
                continue
            doomed.append(r["id"])
            stat["dated" if r["due_date"] else "undated"] += 1
            if len(stat["samples"]) < 25:
                stat["samples"].append({"id": r["id"], "task": r["task"][:110],
                                        "owner": r["owner"], "terms": terms,
                                        "meeting": r["meeting"],
                                        "meeting_date": r["meeting_date"]})
        stat["marked"] = len(doomed)
        if doomed and not dry_run:
            conn.executemany(
                "UPDATE action_items SET status = 'stale', stale_at = ?, "
                "stale_reason = ? WHERE id = ?",
                [(now, REASON_NO_TRACE, i) for i in doomed])
            conn.commit()
    logger.info("sweep_dropped: кандидатів=%s знято=%s живих=%s без вердикту=%s dry_run=%s",
                stat["candidates"], stat["marked"], stat["alive"], stat["unjudged"], dry_run)
    return stat


# ============================================================
# Read-запити (спільні для MCP і UI)
# ============================================================

_WINDOWS = ("this_week", "next_week", "overdue", "soon", "no_date", "all")


def _window_bounds(window: str, today: Optional[date] = None) -> tuple[Optional[str], Optional[str]]:
    """(from_iso, to_iso) для вікна. None означає «без межі»."""
    t = today or date.today()
    monday = t - timedelta(days=t.weekday())
    if window == "this_week":
        # Нижня межа — не понеділок, а СЬОГОДНІ (коли звід читають у понеділок,
        # це те саме). Інакше вікна перетинаються: задача з дедлайном у вівторок,
        # прочитана в суботу, потрапляє і в «цього тижня», і в «протерміновано» —
        # на живому зводі це було 6 дублів із 15 рядків. «Цього тижня» означає
        # «ще встигаємо», прострочене живе у своєму ведрі.
        return (max(monday, t).isoformat(), (monday + timedelta(days=6)).isoformat())
    if window == "next_week":
        nm = monday + timedelta(days=7)
        return (nm.isoformat(), (nm + timedelta(days=6)).isoformat())
    if window == "overdue":
        return (None, (t - timedelta(days=1)).isoformat())
    if window == "soon":
        return (t.isoformat(), (t + timedelta(days=14)).isoformat())
    return (None, None)


def _commitments_filter(*, window: str, owner: Optional[str], category_id: Optional[int],
                        status: str, include_dups: bool,
                        today: Optional[date]) -> tuple[list[str], list]:
    """Умови вибірки задач — спільні для видачі і для лічильника.

    Винесено, щоб `count_commitments` рахував ТОЧНО ту саму множину, яку
    показує `list_commitments`. Дві копії умов розʼїхались би на першій же
    правці, а розбіжність тут — це «показано 50, всього 3» або навпаки.
    """
    where = ["t.deleted_at IS NULL"]
    params: list = []
    if status and status != "all":
        where.append("ai.status = ?")
        params.append(status)
    if not include_dups:
        where.append("ai.dup_of IS NULL")
    if owner:
        # Третя гілка — аліаси графа. Після злиття сутностей людина живе під
        # ОДНИМ канонічним імʼям, а всі інші її написання стають аліасами: у
        # власника архіву це 40 варіантів («Мельник», «@johndoe», «Andrii»).
        # Без цієї гілки питання «що на Мельнику» давало нуль при 204 живих
        # задачах — саме злиття сутностей від цього і не рятує, а навпаки
        # переносить написання туди, куди фільтр не дивився.
        where.append("(e.canonical_name LIKE ? OR ai.owner_name LIKE ? OR EXISTS("
                     "SELECT 1 FROM entity_aliases ea "
                     "WHERE ea.entity_id = e.id AND ea.alias LIKE ?))")
        params += [f"%{owner}%"] * 3
    if category_id is not None:
        where.append("t.category_id = ?")
        params.append(int(category_id))

    if window == "no_date":
        where.append("ai.due_date IS NULL")
    elif window != "all":
        lo, hi = _window_bounds(window, today)
        if lo:
            where.append("ai.due_date >= ?")
            params.append(lo)
        if hi:
            where.append("ai.due_date <= ?")
            params.append(hi)
        where.append("ai.due_date IS NOT NULL")
    return where, params


def count_commitments(db_path: str, *, window: str = "this_week", owner: Optional[str] = None,
                      category_id: Optional[int] = None, status: str = "open",
                      include_dups: bool = False, today: Optional[date] = None) -> int:
    """Скільки задач у вікні НАСПРАВДІ — без ліміту видачі.

    Потрібне, щоб поруч із обрізаним списком стояло не лише «всього задач», а й
    «скільки в цьому вікні». Знаменник біля обрізаного списку без цього числа
    робить гірше, ніж його відсутність: читач вважає видимі рядки всім вікном.
    """
    if window not in _WINDOWS:
        window = "this_week"
    where, params = _commitments_filter(
        window=window, owner=owner, category_id=category_id, status=status,
        include_dups=include_dups, today=today)
    sql = ("SELECT COUNT(*) AS n FROM action_items ai "
           "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
           "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
           f"WHERE {' AND '.join(where)}")
    with get_db_connection(db_path) as conn:
        return _rows(conn, sql, params)[0]["n"]


def list_commitments(db_path: str, *, window: str = "this_week", owner: Optional[str] = None,
                     category_id: Optional[int] = None, status: str = "open",
                     include_dups: bool = False, limit: int = 50,
                     today: Optional[date] = None) -> list[dict]:
    """Задачі за вікном/власником/напрямком. Повертає і сиру фразу, і дату."""
    if window not in _WINDOWS:
        window = "this_week"
    where, params = _commitments_filter(
        window=window, owner=owner, category_id=category_id, status=status,
        include_dups=include_dups, today=today)

    sql = (
        "SELECT ai.id, ai.task, ai.due AS due_raw, ai.due_date, ai.due_precision, "
        "ai.status, ai.stale_at, ai.transcription_id, ai.source, "
        # `owner` — канонічне імʼя з графа (воно зливає «Юля»/«Юлія»/«Julia
        # Bondarenko» в одну людину), `owner_said` — як прозвучало в задачі.
        # Друге поле не косметика: на живих даних частина задачпоказували
        # НЕ те імʼя, що в задачі, і серед них 43 задачі власника архіву
        # («Andrii Melnyk») їхали під іменем «Andrij Kovalenko» — сутність
        # із ютуб-лекції, якій дістався аліас «Andrei». Поки видно обидва імені,
        # крива звʼязка помітна; коли видно лише канонічне — вона невидима.
        "COALESCE(e.canonical_name, ai.owner_name) AS owner, "
        "ai.owner_name AS owner_said, "
        "t.source_name AS meeting, c.name AS category, "
        # Волна 5.2: звід має казати не лише «що», а й «куди написати». Для
        # задачі з переписки джерело — конкретне повідомлення, тож поруч із
        # текстом їдуть чат, автор репліки і посилання на неї. Для дзвінка ці
        # поля порожні, і це чесно: там адреси немає.
        #
        # `link` є не завжди: у legacy-групах (chat_id без префікса -100)
        # посилання на повідомлення у Telegram фізично не існує — це 1453 записи
        # з 3959, серед них робочі чати. Тому адресою служить пара
        # (`chat_id`, `msg_id`), як уже зроблено у видачі пошуку
        # (`retrieval.py`), а `link` — зручність там, де він можливий.
        "t.source_type, t.tg_chat_title AS chat, t.tg_sender AS said_by, "
        "t.tg_link AS link, t.tg_chat_id AS chat_id, t.tg_message_id AS msg_id, "
        "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date "
        "FROM action_items ai "
        "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
        "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
        "LEFT JOIN categories c ON c.id = t.category_id "
        f"WHERE {' AND '.join(where)} "
        # Протерміноване сортуємо СВІЖИМ ВГОРУ: задача, що прострочена вчора,
        # ще жива, а торішня — археологія (і часто просто кривий витяг дати).
        # Решта вікон — найближчий дедлайн першим.
        #
        # Тай-брейк — ДАТА РОЗМОВИ, і лише потім id. До Волни 5.1 id збігався з
        # хронологією, тож «id DESC» читалось як «свіже вгорі». Задачі з
        # переписки вставлені останніми, але кажуть про травень — на одному лише
        # id вони витіснили дзвінки з ведра «без дати» (15 із 15) не тому, що
        # важливіші, а тому, що записані пізніше.
        + ("ORDER BY ai.due_date DESC, meeting_date DESC, ai.id DESC LIMIT ?"
           if window == "overdue"
           else "ORDER BY ai.due_date IS NULL, ai.due_date, meeting_date DESC, "
                "ai.id DESC LIMIT ?")
    )
    params.append(min(int(limit), 200))
    with get_db_connection(db_path) as conn:
        return [dict(r) for r in _rows(conn, sql, params)]


def commitments_coverage(db_path: str, *, owner: Optional[str] = None,
                         category_id: Optional[int] = None,
                         status: str = "open") -> dict:
    """Знаменник до вікон: скільки задач узагалі має дату, а скільки її не має.

    Вікна `this_week`/`overdue`/`soon` фільтрують по `due_date`, а він є лише в
    частина живих задач. Тому «цього тижня — 5» читається як «справ
    майже немає», хоча задач просто не мають дати й у жодне вікно за датою
    не потраплять. Це та сама вада, що й у `stale_topics` до перевірки текстом:
    твердження вірне про свій шар і хибне про архів.

    Рахуємо ПІД ТИМИ Ж фільтрами (власник/напрямок/статус), інакше знаменник
    говорив би про чужі задачі. `undated_share` — частка без дати; вона і є
    відповідь на «наскільки повно вікно бачить».

    Параметра `today` тут свідомо немає, хоча вікна його мають: знаменник — це
    стан таблиці («скільки задач узагалі має дату»), а не зріз на дату. Приймати
    його й ігнорувати означало б обіцяти відтворюваний звід за минулий день і
    підсовувати сьогоднішній знаменник.
    """
    where = ["t.deleted_at IS NULL", "ai.dup_of IS NULL"]
    params: list = []
    if status and status != "all":
        where.append("ai.status = ?")
        params.append(status)
    if owner:
        where.append("(e.canonical_name LIKE ? OR ai.owner_name LIKE ? OR EXISTS("
                     "SELECT 1 FROM entity_aliases ea "
                     "WHERE ea.entity_id = e.id AND ea.alias LIKE ?))")
        params += [f"%{owner}%"] * 3
    if category_id is not None:
        where.append("t.category_id = ?")
        params.append(int(category_id))
    sql = ("SELECT COUNT(*) AS total, "
           "SUM(CASE WHEN ai.due_date IS NOT NULL THEN 1 ELSE 0 END) AS dated "
           "FROM action_items ai "
           "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
           "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
           f"WHERE {' AND '.join(where)}")
    with get_db_connection(db_path) as conn:
        r = _rows(conn, sql, params)[0]
    total = r["total"] or 0
    dated = r["dated"] or 0
    return {
        "total": total,
        "dated": dated,
        "undated": total - dated,
        "undated_share": round((total - dated) / total, 3) if total else 0.0,
        "note": ("вікна за датою бачать лише задачі з due_date; "
                 f"{total - dated} із {total} задач дати не мають і живуть у window='no_date'"),
    }


"""Джерела, де обіцянку дає учасник розмови, тобто ми або той, з ким ми
говоримо. `document` і `youtube` — матеріали третіх осіб: у PDF інвесторам
«обіцянки» дають чужі компанії, у ролику — спікер зі сцени. Ми їх не давали,
тож у «що ми упустили» їм не місце."""
_OWN_SOURCES = ("recording", "meeting_archive", "telegram", "file")


def dropped_commitments(db_path: str, *, days: int = 30, limit: int = 20,
                        min_age_days: int = 14, max_candidates: int = 300,
                        sources: Optional[Sequence[str]] = None,
                        max_per_meeting: int = 3,
                        today: Optional[date] = None) -> list[dict]:
    """Обіцянки без сліду: задача була, а після її зустрічі тема не спливала.

    Рахуємо по FTS5: беремо два НАЙРІДКІСНІШІ терміни задачі (див. `_rare_terms`
    — саме вони її впізнають) і питаємо, чи є вони у чанках ПІЗНІШИХ записів
    (будь-якого джерела — дзвінок, TG, документ). Нуль влучань = розмова про це
    більше не поверталась.

    `min_age_days` — карантин: задача зі вчорашнього дзвінка формально теж «без
    сліду» (після неї ще нічого не було), але це не забуття, а нормальний хід
    справ. Беремо лише те, що мало час спливти й не спливло.

    `sources` — типи записів, з яких беруться кандидати (`_OWN_SOURCES` за
    замовчуванням, `["all"]` знімає фільтр). Без нього звід заповнювався чужими
    зобовʼязаннями: 7 із 10 «упущених» приїхали з одного інвесторського PDF
    («Complete ISO 27001 certification audit», власник — Northwind Vision).
    Вони справді не спливали в розмовах — бо ніколи й не були нашими.

    `max_per_meeting` — стеля на внесок ОДНОГО запису. Фільтр за джерелом ловить
    чужі документи, але не ловить курс, який власник прослухав: «Интенсив по
    подбору идеи» — це `recording`, і його конспект дав 7 рядків із 44 (16%
    списку) — кроки уроку, які нікому не обіцяли. Довга сесія витісняє з видачі
    десяток інших зустрічей просто тим, що вона довга. Той самий запобіжник
    стоїть у видачі пошуку (`retrieval._MAX_PER_MEETING`).
    """
    t = today or date.today()
    lo = (t - timedelta(days=days)).isoformat()
    hi = (t - timedelta(days=min_age_days)).isoformat()
    src = tuple(sources) if sources else _OWN_SOURCES
    src_sql, src_params = "", []
    if "all" not in src:
        src_sql = " AND t.source_type IN (%s)" % ",".join("?" * len(src))
        src_params = list(src)
    out: list[dict] = []
    df_cache: dict = {}
    unjudged = 0
    with get_db_connection(db_path) as conn:
        rows = _rows(conn,
                     "SELECT ai.id, ai.task, ai.due, ai.due_date, "
                     "COALESCE(e.canonical_name, ai.owner_name) AS owner, "
                     "ai.owner_name AS owner_said, "
                     "t.source_name AS meeting, t.id AS tid, t.source_type, "
                     "t.tg_chat_title AS chat, t.tg_sender AS said_by, t.tg_link AS link, "
                     # Пара id — адреса там, де посилання не існує (legacy-групи);
                     # див. коментар у list_commitments.
                     "t.tg_chat_id AS chat_id, t.tg_message_id AS msg_id, "
                     "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date "
                     "FROM action_items ai "
                     "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
                     "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
                     "WHERE ai.status = 'open' AND ai.dup_of IS NULL AND t.deleted_at IS NULL "
                     "AND COALESCE(t.meeting_date, substr(t.created_at,1,10)) BETWEEN ? AND ?"
                     + src_sql +
                     " ORDER BY meeting_date DESC LIMIT ?",
                     (lo, hi, *src_params, max_candidates))
        per_meeting: dict = {}
        crowded = 0
        for r in rows:
            if len(out) >= limit:
                break
            if per_meeting.get(r["tid"], 0) >= max_per_meeting:
                crowded += 1
                continue
            # Рідкість — по корпусу СТАНОМ НА зустріч задачі: інакше пізніші
            # згадки самі себе й викреслюють (див. `_term_df`).
            terms = _rare_terms(conn, r["task"], before=r["meeting_date"], cache=df_cache)
            if len(terms) < 2:
                unjudged += 1
                continue
            fts_q = " OR ".join(f'"{w}"' for w in terms)
            try:
                hit = conn.execute(
                    # JOIN, а не rowid IN (SELECT …): друга форма змушує SQLite
                    # щоразу будувати повний список чанків архіву — 228 мс проти
                    # 0.1 мс на запит, тобто чотири хвилини на прохід замість секунд.
                    "SELECT COUNT(*) AS n FROM chunks_fts f "
                    "JOIN chunks ch ON ch.id = f.rowid "
                    "JOIN transcriptions tt ON tt.id = ch.transcription_id "
                    "WHERE f.chunks_fts MATCH ? AND tt.deleted_at IS NULL AND tt.id <> ? "
                    "AND COALESCE(tt.meeting_date, substr(tt.created_at,1,10)) > ?",
                    (fts_q, r["tid"], r["meeting_date"] or lo)).fetchone()["n"]
            except Exception as exc:           # FTS-синтаксис на екзотичних токенах
                logger.debug("dropped_commitments: FTS skip id=%s (%s)", r["id"], exc)
                unjudged += 1                  # як і в `sweep_dropped`: не судимо ≠ судимо
                continue
            if hit == 0:
                per_meeting[r["tid"]] = per_meeting.get(r["tid"], 0) + 1
                out.append({"id": r["id"], "task": r["task"], "owner": r["owner"],
                            "owner_said": r["owner_said"],
                            "due_raw": r["due"], "due_date": r["due_date"],
                            "meeting": r["meeting"], "meeting_date": r["meeting_date"],
                            "source_type": r["source_type"], "chat": r["chat"],
                            "said_by": r["said_by"], "link": r["link"],
                            "chat_id": r["chat_id"], "msg_id": r["msg_id"],
                            "terms": terms})
    if unjudged:
        # Мовчазне обрізання читається як «перевірено все»: задача, чиїх слів
        # нема в індексі (запис не почанковано, або модель переказала обіцянку
        # своїми словами), не «спливала» і не «загубилась» — про неї просто
        # нічим судити.
        logger.info("dropped_commitments: %s задач без придатних термінів "
                    "(не судимо)", unjudged)
    if crowded:
        logger.info("dropped_commitments: %s задач приховано стелею "
                    "max_per_meeting=%s", crowded, max_per_meeting)
    return out


def _fts_phrase(name: str) -> Optional[str]:
    """Фраза для пошуку назви теми в тексті. «SHA (акціонерна угода Acmecorp)» → «SHA».

    Дужковий хвіст — це пояснення, додане збагаченням, а не те, як тему звуть у
    розмові. Надто коротке ядро (<3 символів) не шукаємо: воно збіглося б із
    випадковими токенами і будь-яка тема здавалась би живою.
    """
    head = (name or "").split("(")[0].strip()
    head = head.strip(" \t\n\r.,;:!?\"'`«»[]{}-–—")
    return head.replace('"', '""') if len(head) >= 3 else None


def stale_topics(db_path: str, *, days: int = 30, min_meetings: int = 3,
                 limit: int = 15, today: Optional[date] = None,
                 verify_text: bool = True) -> list[dict]:
    """Теми/проєкти, які активно обговорювались і зникли з розмов на N днів.

    Кандидатів дає граф (`meeting_entities`), але САМ ГРАФ НЕ Є ДОКАЗОМ
    МОВЧАННЯ: збагачення покриває 1035 із 3964 Telegram-записів, тож тема,
    яку щодня пишуть у чаті, для графа «замовкла» тоді, коли її востаннє згадали
    на дзвінку. На живому архіві так брехали частина тем— «Робота мовчить 36
    днів» при 80 згадках у тексті, остання за чотири дні до зводу.

    Тому кожного кандидата перевіряємо ТЕКСТОМ (FTS по чанках): якщо назва
    звучала пізніше за графову дату, беремо текстову дату і тему з переліку
    прибираємо, коли мовчання вже не набирається. `last_seen_source` каже, звідки
    дата: `graph` — тема не знайшлась у тексті, `text` — знайшлась пізніше.
    `verify_text=False` повертає стару (довірливу) поведінку.

    Межа перевірки: FTS тут без стемінгу (`unicode61`), тож ловиться точна форма
    назви — «Резиденція» знайдеться, «Резиденції» ні. Помилка від цього йде в
    бік «мовчить», тобто список лишається повнішим за реальність, а не порожнім.
    """
    t = today or date.today()
    cutoff = (t - timedelta(days=days)).isoformat()
    sql = (
        "SELECT e.id, e.canonical_name, e.type, COUNT(DISTINCT me.transcription_id) AS meetings, "
        "MAX(COALESCE(tr.meeting_date, substr(tr.created_at,1,10))) AS last_seen "
        "FROM entities e "
        "JOIN meeting_entities me ON me.entity_id = e.id "
        "JOIN transcriptions tr ON tr.id = me.transcription_id AND tr.deleted_at IS NULL "
        "WHERE e.type IN ('project', 'org', 'topic') "
        "GROUP BY e.id HAVING meetings >= ? AND last_seen < ? "
        "ORDER BY meetings DESC, last_seen DESC LIMIT ?"
    )
    # Кандидатів беремо з запасом: частина відсіється на перевірці текстом, і
    # без запасу звід віддавав би менше тем, ніж просили.
    cand_limit = max(limit * 4, limit) if verify_text else limit
    out: list[dict] = []
    with get_db_connection(db_path) as conn:
        rows = _rows(conn, sql, (min_meetings, cutoff, cand_limit))
        for r in rows:
            if not r["last_seen"]:
                continue
            last_seen, source, mentions = r["last_seen"], "graph", 0
            phrase = _fts_phrase(r["canonical_name"]) if verify_text else None
            if phrase:
                try:
                    hit = conn.execute(
                        "SELECT MAX(COALESCE(tt.meeting_date, substr(tt.created_at,1,10))) AS d, "
                        "COUNT(*) AS n "
                        "FROM chunks_fts f JOIN chunks ch ON ch.id = f.rowid "
                        "JOIN transcriptions tt ON tt.id = ch.transcription_id "
                        "WHERE chunks_fts MATCH ? AND tt.deleted_at IS NULL "
                        "AND COALESCE(tt.meeting_date, substr(tt.created_at,1,10)) > ?",
                        (f'"{phrase}"', last_seen)).fetchone()
                except Exception as exc:      # FTS-синтаксис на екзотичних назвах
                    logger.debug("stale_topics: FTS skip %r (%s)", r["canonical_name"], exc)
                    hit = None
                if hit and hit["d"] and hit["d"] > last_seen:
                    last_seen, source, mentions = hit["d"], "text", hit["n"]
            days_silent = (t - date.fromisoformat(last_seen)).days
            if days_silent < days:            # тема жива, просто граф про це не знає
                continue
            out.append({**dict(r), "last_seen": last_seen, "days_silent": days_silent,
                        "last_seen_source": source, "mentions_after_graph": mentions})
            if len(out) >= limit:
                break
    return out


def _recent_corrections(db_path: str, days: int, limit: int) -> list[dict]:
    """Коментарі-виправлення за період — окреме відро зводу (Волна 5).

    Навіщо у зводі. Виправлення — це місця, де АРХІВ ВВОДИТЬ В ОМАНУ: власник
    прочитав запис і сказав, що насправді було інакше. Такий рядок дорожчий за
    будь-яку задачу тижня, бо доки він не прочитаний, кожна відповідь по цьому
    запису лишається неправильною. Але сам по собі він ніде не спливає: у
    відрах зводу лише задачі й теми, а коментар — не задача.

    Порожньо на старій БД без міграції v37 — звід не має падати через
    надбудову.
    """
    from app.db.connection import get_db_connection
    try:
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT c.id, c.body, c.kind, c.created_at, c.anchor_time, "
                "c.target_type, c.target_id, t.source_name AS target_name "
                "FROM comments c "
                "LEFT JOIN transcriptions t ON c.target_type = 'transcription' "
                "AND t.id = c.target_id "
                "WHERE c.deleted_at IS NULL AND c.kind = 'correction' "
                "AND c.created_at >= datetime('now', ?) "
                "ORDER BY c.created_at DESC LIMIT ?",
                (f"-{int(days)} days", int(limit))).fetchall()
    except Exception:
        logger.debug("[digest] виправлення не долучено", exc_info=True)
        return []
    return [dict(r) for r in rows]


def weekly_digest(db_path: str, *, owner: Optional[str] = None, days_back: int = 30,
                  limit: int = 15, today: Optional[date] = None) -> dict:
    """Понеділковий звід: цього тижня / протерміновано / упущено / теми, що
    зникли / виправлення архіву."""
    return {
        "generated_for": (today or date.today()).isoformat(),
        "this_week": list_commitments(db_path, window="this_week", owner=owner,
                                      limit=limit, today=today),
        "overdue": list_commitments(db_path, window="overdue", owner=owner,
                                    limit=limit, today=today),
        "no_date": list_commitments(db_path, window="no_date", owner=owner,
                                    limit=limit, today=today),
        "dropped": dropped_commitments(db_path, days=days_back, limit=limit, today=today),
        "stale_topics": stale_topics(db_path, days=days_back, limit=10, today=today),
        # Місця, де архів виявився неправдивим і власник це виправив. Доки
        # рядок не прочитаний, кожна відповідь по тому запису лишається хибною.
        "corrections": _recent_corrections(db_path, days_back, limit),
        # Без знаменника звід читається як «справ на тиждень — пʼять». Насправді
        # три перші відра ділять між собою лише датовану чверть задач. Знаменник
        # рахується на стан таблиці й не залежить від `today` — див. докстрінг.
        "coverage": commitments_coverage(db_path, owner=owner),
    }


# ============================================================
# CLI
# ============================================================

def _cmd(args) -> int:
    db = args.db
    if args.command == "backfill":
        res = backfill_due(db, dry_run=args.dry_run)
    elif args.command == "owners":
        res = link_owners(db, dry_run=args.dry_run)
    elif args.command == "relink":
        # Тут dry_run за замовчуванням: команда рухає ВЖЕ привʼязані задачі.
        # Явний --dry-run переважає --apply: із двох прочитань наміру беремо
        # те, що нічого не пише.
        res = relink_owners(db, dry_run=args.dry_run or not args.apply)
    elif args.command == "dedup":
        res = dedup(db, dry_run=args.dry_run, threshold=args.threshold)
    elif args.command == "stale":
        res = mark_stale(db, days=args.days, dry_run=args.dry_run)
    elif args.command == "digest":
        res = weekly_digest(db, owner=args.owner)
    elif args.command == "dropped":
        res = dropped_commitments(db, days=args.days, limit=args.limit,
                                  min_age_days=args.min_age,
                                  max_candidates=args.max_candidates)
    elif args.command == "sweep-dropped":
        # Пише в БД, тому прикидка — за замовчуванням, а не за прапорцем.
        res = sweep_dropped(db, days=args.days, min_age_days=args.min_age,
                            undated_only=args.undated_only,
                            dry_run=args.dry_run or not args.apply)
    else:  # pragma: no cover
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(
        prog="commitments",
        description="Нормалізація дедлайнів/власників задач + зводи (Трек 1).")
    p.add_argument("--db", default=default_db)
    p.add_argument("--dry-run", action="store_true", help="Нічого не писати в БД")
    # --dry-run приймається і ПІСЛЯ підкоманди (`backfill --dry-run`) — інакше
    # найважливіший запобіжник спрацьовує лише при «правильному» порядку слів.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Нічого не писати в БД")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", parents=[common], help="due → due_date/due_precision")
    sub.add_parser("owners", parents=[common], help="owner_name → owner_entity_id")
    rl = sub.add_parser("relink", parents=[common],
                        help="Перевісити задачі на точнішу людину "
                             "(за замовчуванням — прикидка)")
    rl.add_argument("--apply", action="store_true", help="Виконати, а не прикинути")
    d = sub.add_parser("dedup", parents=[common], help="Позначити дублі задач (dup_of)")
    d.add_argument("--threshold", type=float, default=DEDUP_THRESHOLD)
    s = sub.add_parser("stale", parents=[common],
                       help="Старі задачі без майбутнього терміну → stale")
    s.add_argument("--days", type=int, default=STALE_DAYS)
    g = sub.add_parser("digest", help="Показати звід (read-only)")
    g.add_argument("--owner", default=None)
    # Звід дивиться на 30 днів, а завал старший: 1242 живі задачі без дати
    # тягнуться на три місяці, і «упущене» серед них видно лише ширшим вікном.
    # Прохід не миттєвий (рідкість слів рахується по індексу), тому окремою
    # командою, а не в зводі.
    dr = sub.add_parser("dropped", help="Обіцянки без сліду за довший період (read-only)")
    dr.add_argument("--days", type=int, default=120, help="Глибина вікна, днів")
    dr.add_argument("--min-age", type=int, default=14,
                    help="Карантин: свіжі домовленості не рахуються загубленими")
    dr.add_argument("--limit", type=int, default=50)
    # 300 — стеля зводу, і для ширшого вікна вона беззмістовна: кандидати
    # відбираються `meeting_date DESC`, тож на 120 днях (задачі) старіший
    # завал — саме той, заради якого команда й існує, — до перевірки не доходив.
    dr.add_argument("--max-candidates", type=int, default=3000)

    sw = sub.add_parser("sweep-dropped", parents=[common],
                        help="Зняти обіцянки, чия тема замовкла (open → stale, no_trace)")
    sw.add_argument("--days", type=int, default=180, help="Глибина вікна, днів")
    sw.add_argument("--min-age", type=int, default=30,
                    help="Карантин: свіжі домовленості не знімаємо")
    sw.add_argument("--undated-only", action="store_true",
                    help="Лише задачі без дати")
    sw.add_argument("--apply", action="store_true",
                    help="Виконати (без нього — прикидка)")

    args = p.parse_args(argv)
    if args.command == "dedup" and not hasattr(args, "threshold"):
        args.threshold = DEDUP_THRESHOLD
    return _cmd(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
