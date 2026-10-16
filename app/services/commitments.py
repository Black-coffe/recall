"""Commitments (Track 1) — normalizing deadlines and owners, stale/dedup passes, and queries.

**The problem this module closes** (grilling session of 24 Jul 2026; the brief and
plan live locally in `docs/grill/` and `docs/plans/` — deliberately not in git,
since they describe specifics of the personal archive):
`action_items.due` was stored as the raw phrase from the conversation — "tomorrow",
"Thursday", "next week", "Q3 2026" (426 distinct values over tasks, 29% filled).
The question "what must I/the team do this week" cannot be answered on such data:
there is neither a date nor a reliable owner, and every task sits in status
`open` (none had been closed since May).

**What the module does:**

1. `parse_due()` — expands a raw phrase into an ISO date relative to the **meeting date**
   (`transcriptions.meeting_date`), because "tomorrow" on 14 May and "tomorrow" yesterday are
   different days. Together with the date it returns `precision`: `day|week|month|quarter|soon|
   next_meeting|recurring`. This is deliberate — "as soon as possible" does NOT pretend to be
   an exact date but becomes `soon`; "by the next meeting" stays dateless (`next_meeting`).
   False precision in the digest is worse than an honest "no deadline".
2. Offline passes (`backfill_due`, `link_owners`, `dedup`, `mark_stale`) — separate,
   idempotent, each has `--dry-run`. NOT built into enrich, so the main path is not
   slowed down (same principle as `entity_dedup.py`).
3. Read queries (`list_commitments`, `dropped_commitments`, `stale_topics`,
   `weekly_digest`) — a shared core for the MCP tools and the UI endpoints.

**The raw phrase is not rewritten.** `due` stays as is, the computed value goes into `due_date`/
`due_precision` — we show both in the output so a parser error stays visible.

**Dependencies:** stdlib only + `app.db.connection` + `app.services.categories`
(also free of heavy dependencies — direction/tags/period filter, C6 of the
`directions-projects-tags` spec, ADR-023 section C). The module is imported from
`mcp_server.py` (a stdio process), so it must NOT pull in embeddings/torch — see
memory `mcp-stdio-no-heavy-models`.
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
from app.services import categories

logger = logging.getLogger(__name__)

# --- Precision classes ---------------------------------------------------------
P_DAY = "day"
P_WEEK = "week"
P_MONTH = "month"
P_QUARTER = "quarter"
P_SOON = "soon"                # «якнайшвидше», «найближчим часом» — the date is approximate
P_NEXT_MEETING = "next_meeting"  # «до наступної зустрічі» — there is no date by design
P_RECURRING = "recurring"      # «щомісяця» — not a deadline but a rhythm
P_EVENT = "event"              # «після дзвінка з Acmecorp», «після свят» — tied to an
                               # event, not to the calendar: the date appears when the
                               # event happens, inventing one here would be a lie

# How many days count as "in the near term" (for P_SOON). Three days is a compromise:
# fewer = the task is overdue at once, more = the sense of urgency is lost.
SOON_DAYS = 3

# Default for mark_stale: a task from a meeting older than this many days and with no
# future deadline is no longer "this week" but history. 90 days ~ a quarter.
STALE_DAYS = 90

# Why a task was dropped (`action_items.stale_reason`). There are two reasons and they
# must not be confused: age means "it was long ago", no trace means "it was not discussed again".
# The latter is evidence-based, and it can be rolled back in bulk without touching the former.
REASON_AGE = "age"
REASON_NO_TRACE = "no_trace"

# Similarity threshold for dedup (share of shared significant tokens, Jaccard).
DEDUP_THRESHOLD = 0.75

# Stem -> weekday (0 = Monday). Compared by prefix to cover
# cases: «понеділка», «наступної пʼятниці», «до четверга».
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

# Phrases that give no date but give a class.
_NEXT_MEETING_RE = re.compile(
    r"наступн\w*\s+(зустріч|встреч|дзвінк|дзвонк|звонк|созвон|колл|call)|"
    r"наступн\w*\s+(міт|мит)|на\s+наступн\w*\s+(зустріч|дзвінк)")
_RECURRING_RE = re.compile(r"^(що\w+|раз\s+(в|на)\s+\w+|регулярно|постійно|кожн\w+)")
_SOON_RE = re.compile(
    r"якнайшвидше|якомога\s+швидше|найближч\w*\s+час|найближчим\s+часом|термінов|срочн|"
    r"асап|asap|негайно|як\s+тільки|найближч\w*\s+дн|кілька\s+дн|декілька\s+дн")
# A deadline tied to an event («після дзвінка», «до зустрічі з Толіком»).
# `після` requires a trailing space so it does not catch «післязавтра».
_EVENT_RE = re.compile(
    r"\bпісля\s+|\bпосле\s+|по\s+завершенн|по\s+підсумк|"
    # «до зустрічі з Толіком»: the service word «до» is already stripped in _norm_phrase, so
    # we also catch the bare form «зустрічі з …».
    r"(^|\s)(зустріч|дзвінк|мітинг|кол)\w*\s+з\s+|найближч\w*\s+мітинг")
# Weekday abbreviations — ONLY as a whole token: by prefix «ср» would catch
# «срочно», «пт» — «птн», etc.
_WEEKDAY_ABBR = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "нд": 6, "вс": 6}

_STOPWORDS = {
    "щоб", "який", "яка", "яке", "які", "цього", "цей", "для", "про", "над", "під",
    "від", "при", "буде", "було", "має", "тому", "тобто", "також", "після", "перед",
    "разом", "потім", "тоді", "щодо", "чтобы", "который", "этого", "перед", "после",
}


# ============================================================
# Deadline parser
# ============================================================

def _norm_phrase(raw: str) -> str:
    """Normalize a raw phrase: lowercase, apostrophes, drop time and parentheses."""
    s = (raw or "").strip().lower()
    s = s.replace("ʼ", "'").replace("’", "'").replace("`", "'")
    s = re.sub(r"\([^)]*\)", " ", s)              # «07.04.2026 (вівторок)»
    # Strip the time ONLY in the colon form: the dotted form is indistinguishable from a
    # date («27.03.2026» is not 27 hours 03 minutes), and it is where the parser used to
    # silently lose every dd.mm date.
    s = re.sub(r"\b([01]?\d|2[0-3]):[0-5]\d\b", " ", s)   # «завтра до 11:30»
    s = re.sub(r"\b(о|в|до)\s+\d{1,2}\s*(год|часов|ч)\b", " ", s)
    s = re.sub(r"[«»\"]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:!?-–—")
    # Drop leading service prepositions — they do not change the date.
    s = re.sub(r"^(до|не\s+пізніше|не\s+позднее|станом\s+на|desired|by)\s+", "", s)
    return s.strip()


def _end_of_month(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _week_end(anchor: date) -> date:
    """Friday of the anchor week (working end of the week, not Sunday)."""
    return anchor + timedelta(days=(4 - anchor.weekday()))


def _next_weekday(anchor: date, wd: int, *, next_week: bool = False) -> date:
    """The nearest occurrence of a weekday STRICTLY after anchor (anchor+1..anchor+7)."""
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
    """Weekday in the phrase. «середина» is NOT counted as Wednesday."""
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
    """Parse ONE (already normalized) phrase. None means not recognized."""
    if not s:
        return None

    # --- classes without a date (checked FIRST) --------------------------------
    # Otherwise «після завтрашньої стратсесії» would become just «завтра», losing the
    # condition the deadline actually depends on.
    if _RECURRING_RE.search(s):
        return (None, P_RECURRING)
    if _NEXT_MEETING_RE.search(s):
        return (None, P_NEXT_MEETING)
    if _EVENT_RE.search(s):
        return (None, P_EVENT)

    # --- explicit dates -------------------------------------------------------
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
            # A month without a year that is already in the past -> next year.
            try:
                d = d.replace(year=year + 1)
            except ValueError:
                d = _end_of_month(year + 1, month)
        return (d, prec)

    # --- quarters -------------------------------------------------------------
    m = re.search(r"\bq([1-4])\b|\b([1-4])\s*(?:кв|квартал)", s)
    if m:
        q = int(m[1] or m[2])
        year_m = re.search(r"\b(20\d{2})\b", s)
        year = int(year_m[1]) if year_m else anchor.year
        d = _end_of_month(year, q * 3)
        if not year_m and d < anchor:
            d = _end_of_month(year + 1, q * 3)
        return (d, P_QUARTER)

    # --- «через N днів/тижнів/місяців» (in N days/weeks/months) ---------------
    m = re.search(r"через\s+(\d+)\s*(дн|тижн|недел|місяц|месяц)", s)
    if m:
        n = int(m[1])
        unit = m[2]
        if unit.startswith("дн"):
            return (anchor + timedelta(days=n), P_DAY)
        if unit.startswith(("тижн", "недел")):
            return (anchor + timedelta(weeks=n), P_WEEK)
        return (_end_of_month(*_add_months(anchor, n)), P_MONTH)
    # «через тиждень» / «протягом тижня» is +7 days from the meeting, NOT Friday of the
    # current week (the general «тижн» branch below), so we catch it separately.
    if re.search(r"(через|протягом)\s+(тиждень|тижня|неделю|недели)", s):
        return (anchor + timedelta(days=7), P_WEEK)
    m = re.search(r"протягом\s+(\d+)(?:\s*-\s*(\d+))?\s*(дн|тижн)", s)
    if m:
        n = int(m[2] or m[1])
        return ((anchor + timedelta(days=n), P_DAY) if m[3].startswith("дн")
                else (anchor + timedelta(weeks=n), P_WEEK))

    # --- «з 15-го числа» (from the 15th) --------------------------------------
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

    # --- days relative to anchor ----------------------------------------------
    if re.search(r"післязавтра|послезавтра", s):
        return (anchor + timedelta(days=2), P_DAY)
    # «наступного дня», «ранок наступного дня», «до кінця наступного дня» is
    # tomorrow relative to the meeting. Must come BEFORE the general «наступн…тиждень» branch.
    if re.search(r"наступн\w*\s+(дн|день)|следующ\w*\s+(дн|день)", s):
        return (anchor + timedelta(days=1), P_DAY)
    if re.search(r"кін(ец|ц)\w*\s+дня|до\s+кінця\s+дня|протягом\s+дня", s):
        return (anchor, P_DAY)
    if re.search(r"\bзавтра\b", s):
        return (anchor + timedelta(days=1), P_DAY)
    if re.search(r"сьогодні|сегодня|today", s):
        # «сьогодні-завтра» is already covered by the «завтра» branch above (range -> later bound)
        return (anchor, P_DAY)

    # --- weeks ----------------------------------------------------------------
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

    # --- months/years without a name ------------------------------------------
    if re.search(r"місяц|месяц", s):
        if next_week:  # «наступного місяця» (next month)
            y, mth = _add_months(anchor, 1)
            return (_end_of_month(y, mth), P_MONTH)
        return (_end_of_month(anchor.year, anchor.month), P_MONTH)
    if re.search(r"\bрок[уі]\b|\bгод[уа]\b", s):
        year = anchor.year + 1 if next_week else anchor.year
        return (date(year, 12, 31), P_MONTH)

    # --- «найближчим часом» (in the near term) — a guideline, not a date ------
    if _SOON_RE.search(s):
        return (anchor + timedelta(days=SOON_DAYS), P_SOON)

    return None


def _add_months(d: date, n: int) -> tuple[int, int]:
    total = (d.year * 12 + (d.month - 1)) + n
    return total // 12, total % 12 + 1


def parse_due(raw: Optional[str], anchor: Optional[str | date]) -> tuple[Optional[str], Optional[str]]:
    """Raw deadline phrase -> (ISO date | None, precision | None).

    Args:
        raw: the `action_items.due` value as Claude produced it («завтра», «Q3 2026»).
        anchor: the meeting date (ISO string or date) — the reference point for relative
            expressions. Without it relative phrases are not parsed (absolute ones only).

    Returns:
        (None, None) — not recognized; (None, precision) — a class without a date
        (`next_meeting`/`recurring`); (iso, precision) — parsed.
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

    # Range («понеділок-вівторок», «четвер-п'ятниця», «сьогодні-завтра»):
    # take the LATER bound — that is the deadline.
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
# Offline passes
# ============================================================

def _rows(conn, sql: str, params: Iterable = ()) -> list:
    return conn.execute(sql, tuple(params)).fetchall()


def backfill_due(db_path: str, *, dry_run: bool = False, limit: Optional[int] = None) -> dict:
    """Fill due_date/due_precision for existing tasks (idempotent)."""
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
    """The same key as enrichment._normalize (a deliberate 6-line copy: this
    module is imported from the stdio MCP and must not pull in embeddings/torch)."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t\n\r.,;:!?\"'`«»()[]{}-–—")


def link_owners(db_path: str, *, dry_run: bool = False) -> dict:
    """Link owner_name -> person entity (this also merges «Юлія»/«Юля»)."""
    stats = {"scanned": 0, "linked": 0, "unmatched": {}}
    with get_db_connection(db_path) as conn:
        index: dict[str, int] = {}
        # Entities whose OWN name is a single word («Юлія», «Андрій»). Only they
        # qualify for the by-name fallback: aliases do not, because enrichment junk
        # lands among them — the «Andrij Kovalenko» entity from a YouTube lecture got the
        # alias «Andrei», and through it 43 tasks of the archive owner
        # («Andrii Melnyk» -> first token «andrei») ended up under somebody
        # else's name. A namesake recorded under a full name does not fit either:
        # «Андрій Ткаченко» does not make the «Андрій» of a conversation into Yermak. There cannot be
        # two single-word namesakes — the schema holds UNIQUE(type, normalized_name).
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
            if eid is None and " " in key:            # «Юля Гончар» -> «юля»
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
    """Re-point tasks when a MORE PRECISE person has appeared for their `owner_name`.

    `link_owners` fills only empty values and has a first-token fallback: «Dmytro
    Lebid» without a row of his own in the graph settled on the «Dmytro» entity. Nothing
    ever unhooked him again — linking happens once, and 40 of Lebid's tasks stayed
    hanging on the row where the fallback also put 34 tasks of the external lawyer
    «Dmytro S». The "who owes whom what" digest showed one person instead of two.

    Here we move only what a FULL spelling proves: `owner_name` matched the
    entity's canonical name or alias entirely. No guesses at all —
    we deliberately do NOT repeat the first-token fallback, otherwise re-linking
    would shuffle tasks between namesakes on every pass.

    Idempotent: a second run moves nothing.
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
    # The estimate and the write must sound different: otherwise the operator sees
    # «relinked=40», applies nothing and thinks the tasks have already moved.
    logger.info("relink_owners: scanned=%s %s=%s", stats["scanned"],
                "would_relink" if dry_run else "relinked", stats["relinked"])
    return stats


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яіїєґёa-z0-9']{4,}", (text or "").lower())
            if w not in _STOPWORDS}


def _term_df(conn, term: str, *, before: Optional[str] = None,
             cache: Optional[dict] = None) -> Optional[int]:
    """In how many chunks a word occurs. None if FTS did not understand the token.

    `before` limits the count to records NO LATER than the given date, and this is not
    an optimization but a correctness requirement: rarity counted over the whole archive
    flatters the "dropped" verdict. A word that surfaced after the task has a HIGHER
    df through those mentions, so picking the "rarest" discards it — and keeps
    the words that never came up later. The metric would prove the silence by itself.
    Measured on the live archive: «ендавменту» 2 vs «стратегії» 1 in a tiny corpus — and
    a task whose subject HAD come back was declared lost.

    `cache` — a dict per call: tasks of one meeting share both the date and half of
    the words, so recounting would be paying for nothing.
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
    except Exception as exc:                   # exotic tokens break the syntax
        logger.debug("_term_df: skipped %r (%s)", term, exc)
        n = None
    if cache is not None:
        cache[key] = n
    return n


def _rare_terms(conn, task: str, *, limit: int = 2,
                before: Optional[str] = None, cache: Optional[dict] = None) -> list[str]:
    """Words by which a task can be recognized in other text — the RAREST ones.

    The longest words used to be picked here, which gave systematic lies: the longest
    word of a Ukrainian task is a generic verb («продовжувати»,
    «організувати», «переглянути»), while a task is recognized by a short proper name
    («BUEF», «Archicad», «Notion»). Measured on live data: 15 of them
    (7.5%) were declared "alive" only through a generic word — «Продовжувати
    роботу по ендавменту BUEF» counted as resurfaced because «стратегії» occurs in
    the archive 509 times. Zero errors in the opposite direction. So "what we dropped"
    stayed silent exactly where it should have spoken.

    Rarity is taken from the same index we later search with (FTS5): this is both
    accurate and cheap — the counter walks the index, not the texts.

    Words that are ABSENT from the corpus (df = 0) are discarded on purpose: zero hits
    on them prove nothing. A recognition error («ресепшіоніст» instead of
    «ресепціоніст») would otherwise make any task "dropped".

    **We look at ALL significant words, not just the longest.** The first version of this
    function kept the earlier "12 longest" selection — and so threw out exactly
    the short proper names it was written for: in a task with a long
    wording «buef» (4 characters) did not survive to the rarity count. Besides,
    the cut went over a set, so the boundary depended on `set` iteration order and the
    choice of terms was not reproducible between runs. The index counter costs
    microseconds — there was nothing to save on it.

    We sort ONLY by rarity at the time of the task and break ties alphabetically.
    The temptation to add archive-wide rarity as a second key («on an early task
    the corpus is thin and an ordinary word looks rare») is a trap: full rarity
    includes mentions AFTER the task, so a word that did resurface has a larger df and
    loses by that key to one that did not. That is exactly the rigging of the verdict
    that `before` exists to remove. The test `..._stays_silent_when_the_subject_really_came_back`
    catches exactly that.

    A known limit remains: on the earliest tasks the corpus is still small and an ordinary
    word may become the "rarest". This error goes the safe way — such a word
    turns up later and the task stays open.
    """
    scored: list[tuple[int, str]] = []
    for term in sorted(_tokens(task)):         # sorted — so the choice is reproducible
        df = _term_df(conn, term, before=before, cache=cache)
        if df:                                 # None or 0 cannot serve as proof
            scored.append((df, term))
    scored.sort()
    return [t for _, t in scored[:limit]]


def dedup(db_path: str, *, dry_run: bool = False, threshold: float = DEDUP_THRESHOLD) -> dict:
    """Mark repeats of one task from different meetings (dup_of -> the most recent).

    The status does not change: duplicates stay in the DB and are available by direct query,
    but vanish from digests (those filter on dup_of IS NULL).
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
    """Tasks from old meetings with no future deadline -> status='stale'.

    Not a deletion: `stale` stays available by direct query (`status='stale'`),
    but does not clutter the Monday digest. A task with a date in the future (e.g.
    «Q3 2026») does NOT become stale even if the meeting is old.
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
    """Drop promises whose topic went quiet: status='open' -> 'stale' (no_trace).

    Why this exists at all. `status='open'` in the archive means not "pending" but
    "nobody ever closed it": `done` is set on ONE task out of 3879. There are 1721 live
    tasks, 1242 of them without a date — and those 1242 fall into no digest window by
    date. Dropping them by age (`mark_stale`) means waiting 90 days and
    hiding the living together with the dead.

    The criterion here is different and fairer: **after the meeting the topic never
    surfaced in any source again**. That is what should be dropped, rather than
    "completed" — the product frame is a digest of commitments with a tail where most
    rows will never be fulfilled.

    **How much this drops.** Measured on 120 undated tasks, one rarest word
    declared 38% quiet. The pass drops THREE times fewer (on the live
    archive 93 of 1154, i.e. 8%), and this is not a discrepancy but different criteria: here
    BOTH terms must fail to surface. The strictness is deliberate — the decision is written to
    the DB, so we err on the side of "leave it open".

    It differs from `dropped_commitments` (the same criterion) in two ways,
    because that one is a read output and this one is a pass over the backlog:
      * no per-meeting ceiling and no limit — we drop everything that qualifies;
      * a longer quarantine (30 days vs 14): the decision writes to the DB, so
        we err on the side of "leave it open".

    A task with a date IN THE FUTURE is never dropped — same as in `mark_stale`.
    `undated_only` narrows the pass to tasks without any date.

    Idempotent: a second run finds nothing (the rows are no longer 'open'). The reverse —
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
                # Nothing to judge by — leave it open. A silent drop
                # "just in case" would destroy exactly the tasks we know
                # least about.
                stat["unjudged"] += 1
                continue
            fts_q = " OR ".join(f'"{w}"' for w in terms)
            try:
                hit = conn.execute(
                    # JOIN, not rowid IN (SELECT ...): the second form makes SQLite
                    # build the full list of archive chunks every time — 228 ms vs
                    # 0.1 ms per query, i.e. four minutes per pass instead of seconds.
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
    logger.info("sweep_dropped: candidates=%s marked=%s alive=%s unjudged=%s dry_run=%s",
                stat["candidates"], stat["marked"], stat["alive"], stat["unjudged"], dry_run)
    return stat


# ============================================================
# Read queries (shared by MCP and UI)
# ============================================================

_WINDOWS = ("this_week", "next_week", "overdue", "soon", "no_date", "all")


def _window_bounds(window: str, today: Optional[date] = None) -> tuple[Optional[str], Optional[str]]:
    """(from_iso, to_iso) for the window. None means "no bound"."""
    t = today or date.today()
    monday = t - timedelta(days=t.weekday())
    if window == "this_week":
        # The lower bound is not Monday but TODAY (when the digest is read on Monday
        # it is the same). Otherwise the windows overlap: a task due Tuesday,
        # read on Saturday, lands in both "this week" and "overdue" —
        # on the live digest that was 6 duplicates out of 15 rows. "This week" means
        # "we can still make it"; overdue items live in their own bucket.
        return (max(monday, t).isoformat(), (monday + timedelta(days=6)).isoformat())
    if window == "next_week":
        nm = monday + timedelta(days=7)
        return (nm.isoformat(), (nm + timedelta(days=6)).isoformat())
    if window == "overdue":
        return (None, (t - timedelta(days=1)).isoformat())
    if window == "soon":
        return (t.isoformat(), (t + timedelta(days=14)).isoformat())
    return (None, None)


def _scope_clauses(category_id: Optional[int], tags: Optional[list[str]],
                   since: Optional[str] = None, until: Optional[str] = None,
                   *, alias: str = "t") -> tuple[str, list]:
    """Direction/tags/period — one WHERE fragment shared by `_commitments_filter`
    and `commitments_coverage` (repair of items 1+11 of round 3: before this the denominator ignored
    tags/since/until, and `_commitments_filter` and coverage glued these same three fragments
    in two different ways — one cut its own `" AND "` by hand, the other appended them
    as is).

    Each fragment (`categories.filter_sql/tags_sql/date_sql`, C2) already starts with
    `" AND "` — we return the concatenation as is, to be attached to a ready
    `WHERE ...` without trimming. `alias` is the alias of `transcriptions` in the query (always `"t"`);
    tags always go through `ai.transcription_id` — bound only to `action_items`.
    """
    sql = ""
    params: list = []
    frag, p = categories.filter_sql(f"{alias}.category_id", category_id)
    sql += frag; params += p
    frag, p = categories.tags_sql("ai.transcription_id", tags or [])
    sql += frag; params += p
    frag, p = categories.date_sql(f"COALESCE({alias}.meeting_date, {alias}.created_at)", since, until)
    sql += frag; params += p
    return sql, params


def _commitments_filter(*, window: str, owner: Optional[str], category_id: Optional[int],
                        status: str, include_dups: bool, today: Optional[date],
                        tags: Optional[list[str]] = None, since: Optional[str] = None,
                        until: Optional[str] = None) -> tuple[str, list]:
    """Conditions for selecting tasks — shared by the listing and the counter.

    Extracted so `count_commitments` counts EXACTLY the same set that
    `list_commitments` shows. Two copies of the conditions would drift apart on the first
    edit, and a mismatch here means "shown 50, total 3" or the reverse.

    Returns a ready ` WHERE ...` (not a list of conditions): `category_id`/`tags`
    go through `app.services.categories.filter_sql/tags_sql` (C6, ADR-023
    section C) — the single source of SQL for the expansion "direction = itself + projects" and
    the OR filter of tags, rather than a second copy of the same subquery here.
    """
    where = ["t.deleted_at IS NULL"]
    params: list = []
    if status and status != "all":
        where.append("ai.status = ?")
        params.append(status)
    if not include_dups:
        where.append("ai.dup_of IS NULL")
    if owner:
        # The third branch is graph aliases. After entities are merged a person lives under
        # ONE canonical name, and all their other spellings become aliases: for
        # the archive owner that is 40 variants («Мельник», «@johndoe», «Andrii»).
        # Without this branch the question «що на Мельнику» returned zero with live data — the entity merge itself does not save it, it rather
        # moves the spellings to where the filter did not look.
        where.append("(e.canonical_name LIKE ? OR ai.owner_name LIKE ? OR EXISTS("
                     "SELECT 1 FROM entity_aliases ea "
                     "WHERE ea.entity_id = e.id AND ea.alias LIKE ?))")
        params += [f"%{owner}%"] * 3

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

    sql = " WHERE " + " AND ".join(where)
    frag, p = _scope_clauses(category_id, tags, since, until)
    sql += frag; params += p
    return sql, params


def count_commitments(db_path: str, *, window: str = "this_week", owner: Optional[str] = None,
                      category_id: Optional[int] = None, status: str = "open",
                      include_dups: bool = False, today: Optional[date] = None,
                      tags: Optional[list[str]] = None, since: Optional[str] = None,
                      until: Optional[str] = None) -> int:
    """How many tasks are ACTUALLY in the window — without the listing limit.

    Needed so that next to a truncated list there is not only "total tasks" but also
    "how many in this window". A denominator beside a truncated list without this number
    is worse than none: the reader takes the visible rows for the whole window.

    `tags`/`since`/`until` — the same axes as `list_commitments` (C6):
    `count_commitments` must count EXACTLY the same set.
    """
    if window not in _WINDOWS:
        window = "this_week"
    where_sql, params = _commitments_filter(
        window=window, owner=owner, category_id=category_id, status=status,
        include_dups=include_dups, today=today, tags=tags, since=since, until=until)
    sql = ("SELECT COUNT(*) AS n FROM action_items ai "
           "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
           "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
           + where_sql)
    with get_db_connection(db_path) as conn:
        return _rows(conn, sql, params)[0]["n"]


def list_commitments(db_path: str, *, window: str = "this_week", owner: Optional[str] = None,
                     category_id: Optional[int] = None, status: str = "open",
                     include_dups: bool = False, limit: int = 50,
                     today: Optional[date] = None,
                     tags: Optional[list[str]] = None, since: Optional[str] = None,
                     until: Optional[str] = None) -> list[dict]:
    """Tasks by window/owner/direction. Returns both the raw phrase and the date.

    `tags` — tag names (OR); together with `category_id` — intersection (AND).
    `since`/`until` — ISO date on the record's meeting date, both bounds inclusive.
    """
    if window not in _WINDOWS:
        window = "this_week"
    where_sql, params = _commitments_filter(
        window=window, owner=owner, category_id=category_id, status=status,
        include_dups=include_dups, today=today, tags=tags, since=since, until=until)

    sql = (
        "SELECT ai.id, ai.task, ai.due AS due_raw, ai.due_date, ai.due_precision, "
        "ai.status, ai.stale_at, ai.transcription_id, ai.source, "
        # `owner` is the canonical name from the graph (it merges «Юля»/«Юлія»/«Julia
        # Bondarenko» into one person), `owner_said` is how it was spoken in the task.
        # The second field is not cosmetic: in live data some tasksshowed
        # NOT the name from the task, and among them 43 tasks of the archive owner
        # («Andrii Melnyk») went under the name «Andrij Kovalenko» — an entity
        # from a YouTube lecture that got the alias «Andrei». While both names are visible,
        # a crooked link is noticeable; when only the canonical one is shown it is invisible.
        "COALESCE(e.canonical_name, ai.owner_name) AS owner, "
        "ai.owner_name AS owner_said, "
        "t.source_name AS meeting, c.name AS category, "
        # Wave 5.2: the digest must say not only "what" but also "where to write". For
        # a task from a chat the source is a concrete message, so next to the
        # text go the chat, the author of the message and a link to it. For a call these
        # fields are empty, honestly: there is no address there.
        #
        # `link` is not always present: in legacy groups (chat_id without the -100 prefix)
        # a Telegram message link physically does not exist — records
        # of 3959, working chats among them. So the address is the pair
        # (`chat_id`, `msg_id`), as already done in search output
        # (`retrieval.py`), and `link` is a convenience where possible.
        "t.source_type, t.tg_chat_title AS chat, t.tg_sender AS said_by, "
        "t.tg_link AS link, t.tg_chat_id AS chat_id, t.tg_message_id AS msg_id, "
        "COALESCE(t.meeting_date, substr(t.created_at,1,10)) AS meeting_date "
        "FROM action_items ai "
        "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
        "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
        "LEFT JOIN categories c ON c.id = t.category_id "
        + where_sql + " "
        # Overdue is sorted FRESHEST FIRST: a task overdue since yesterday
        # is still alive, while last year's is archaeology (and often just a bad date extraction).
        # The other windows — nearest deadline first.
        #
        # Tie-break is the CONVERSATION DATE, and only then id. Before Wave 5.1 id matched
        # chronology, so "id DESC" read as "fresh on top". Tasks from
        # chats were inserted last but talk about May — on id alone they pushed calls out
        # of the "no date" bucket (15 of 15) not because they were
        # more important but because they were recorded later.
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
                         status: str = "open",
                         tags: Optional[list[str]] = None, since: Optional[str] = None,
                         until: Optional[str] = None) -> dict:
    """Denominator for the windows: how many tasks have a date at all and how many do not.

    The windows `this_week`/`overdue`/`soon` filter on `due_date`, which exists in only
    some live tasks. So "this week — 5" reads as "there is
    almost nothing to do", although tasks simply have no date and will fall into no
    date window. It is the same flaw as in `stale_topics` before the text check:
    the claim is true about its own layer and false about the archive.

    We count UNDER THE SAME filters (owner/direction/status/`tags`/`since`/`until`),
    otherwise the denominator would speak about other tasks: `list_action_items(tags="AI")`
    before the repair of item 1 of round 3 returned a filtered `items` next to the archive-wide
    `coverage.total`/`note` ("3 of 3 tasks have no date" with one task
    having the tag) — the denominator must count EXACTLY the same set as `items`/
    `window_matched` (`count_commitments`), via the shared `_scope_clauses`.
    `undated_share` is the share without a date; it is the answer to "how completely
    does the window see".

    There is deliberately no `today` parameter here, although the windows have it: the denominator is
    the state of the table ("how many tasks have a date at all"), not a cut at a date. Accepting
    it and ignoring it would promise a reproducible digest for a past day while
    slipping in today's denominator.
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
    sql = ("SELECT COUNT(*) AS total, "
           "SUM(CASE WHEN ai.due_date IS NOT NULL THEN 1 ELSE 0 END) AS dated "
           "FROM action_items ai "
           "LEFT JOIN transcriptions t ON t.id = ai.transcription_id "
           "LEFT JOIN entities e ON e.id = ai.owner_entity_id "
           f"WHERE {' AND '.join(where)}")
    frag, p = _scope_clauses(category_id, tags, since, until)
    sql += frag; params += p
    with get_db_connection(db_path) as conn:
        r = _rows(conn, sql, params)[0]
    total = r["total"] or 0
    dated = r["dated"] or 0
    return {
        "total": total,
        "dated": dated,
        "undated": total - dated,
        "undated_share": round((total - dated) / total, 3) if total else 0.0,
        "note": ("date windows see only tasks with a due_date; "
                 f"{total - dated} of {total} tasks have no date and live in window='no_date'"),
    }


"""Sources where a promise is made by a participant of the conversation, i.e. we or the one
we are talking to. `document` and `youtube` are third-party material: in a PDF for investors
other companies make the "promises", in a video — a speaker on stage. We did not make them,
so they do not belong in "what we dropped"."""
_OWN_SOURCES = ("recording", "meeting_archive", "telegram", "file")


def dropped_commitments(db_path: str, *, days: int = 30, limit: int = 20,
                        min_age_days: int = 14, max_candidates: int = 300,
                        sources: Optional[Sequence[str]] = None,
                        max_per_meeting: int = 3,
                        today: Optional[date] = None) -> list[dict]:
    """Promises with no trace: the task existed, but after its meeting the topic did not surface.

    Counted via FTS5: we take the task's two RAREST terms (see `_rare_terms`
    — they are what recognizes it) and ask whether they occur in chunks of LATER records
    (any source — call, TG, document). Zero hits = the conversation never
    returned to it.

    `min_age_days` — quarantine: a task from yesterday's call is formally also "without a
    trace" (nothing came after it), but that is not forgetting, it is the normal course
    of things. We take only what had time to surface and did not.

    `sources` — record types the candidates come from (`_OWN_SOURCES` by
    default, `["all"]` removes the filter). Without it the digest filled up with other people's
    commitments: 7 of 10 "dropped" came from one investor PDF
    («Complete ISO 27001 certification audit», owner — Northwind Vision).
    They really did not surface in conversations — because they were never ours.

    `max_per_meeting` — a ceiling on the contribution of ONE record. The source filter catches
    other people's documents but not a course the owner listened to: «Интенсив по
    подбору идеи» is a `recording`, and its notes gave 7 rows of 44 (16% of
    the list) — lesson steps nobody promised. A long session pushes a dozen
    other meetings out of the output simply by being long. The same safeguard
    sits in search output (`retrieval._MAX_PER_MEETING`).
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
                     # The id pair is the address where a link does not exist (legacy groups);
                     # see the comment in list_commitments.
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
            # Rarity is over the corpus AS OF the task's meeting: otherwise later
            # mentions strike themselves out (see `_term_df`).
            terms = _rare_terms(conn, r["task"], before=r["meeting_date"], cache=df_cache)
            if len(terms) < 2:
                unjudged += 1
                continue
            fts_q = " OR ".join(f'"{w}"' for w in terms)
            try:
                hit = conn.execute(
                    # JOIN, not rowid IN (SELECT ...): the second form makes SQLite
                    # build the full list of archive chunks every time — 228 ms vs
                    # 0.1 ms per query, i.e. four minutes per pass instead of seconds.
                    "SELECT COUNT(*) AS n FROM chunks_fts f "
                    "JOIN chunks ch ON ch.id = f.rowid "
                    "JOIN transcriptions tt ON tt.id = ch.transcription_id "
                    "WHERE f.chunks_fts MATCH ? AND tt.deleted_at IS NULL AND tt.id <> ? "
                    "AND COALESCE(tt.meeting_date, substr(tt.created_at,1,10)) > ?",
                    (fts_q, r["tid"], r["meeting_date"] or lo)).fetchone()["n"]
            except Exception as exc:           # FTS syntax on exotic tokens
                logger.debug("dropped_commitments: FTS skip id=%s (%s)", r["id"], exc)
                unjudged += 1                  # as in `sweep_dropped`: not judged != judged
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
        # Silent truncation reads as "everything checked": a task whose words
        # are not in the index (the record is not chunked, or the model retold the promise
        # in its own words) did not "surface" and did not "get lost" — there is simply
        # nothing to judge it by.
        logger.info("dropped_commitments: %s tasks without usable terms "
                    "(not judged)", unjudged)
    if crowded:
        logger.info("dropped_commitments: %s tasks hidden by the ceiling "
                    "max_per_meeting=%s", crowded, max_per_meeting)
    return out


def _fts_phrase(name: str) -> Optional[str]:
    """A phrase for finding a topic name in text. «SHA (акціонерна угода Acmecorp)» -> «SHA».

    The parenthesized tail is an explanation added by enrichment, not how the topic is called
    in conversation. A core that is too short (<3 characters) is not searched: it would match
    random tokens and any topic would look alive.
    """
    head = (name or "").split("(")[0].strip()
    head = head.strip(" \t\n\r.,;:!?\"'`«»[]{}-–—")
    return head.replace('"', '""') if len(head) >= 3 else None


def stale_topics(db_path: str, *, days: int = 30, min_meetings: int = 3,
                 limit: int = 15, today: Optional[date] = None,
                 verify_text: bool = True) -> list[dict]:
    """Topics/projects that were actively discussed and vanished from conversations for N days.

    The graph supplies candidates (`meeting_entities`), but THE GRAPH ITSELF IS NOT PROOF
    OF SILENCE: enrichment covers 1035 of 3964 Telegram records, so a topic
    written about in a chat daily is "silent" for the graph from the moment it was last mentioned
    on a call. On the live archive some topicslied this way — «Робота is silent for 36
    days» with 80 mentions in the text, the last one four days before the digest.

    So we check every candidate against TEXT (FTS over chunks): if the name
    sounded later than the graph date, we take the text date and drop the topic from the list
    when the silence no longer adds up. `last_seen_source` says where the date comes
    from: `graph` — the topic was not found in the text, `text` — found later.
    `verify_text=False` returns the old (trusting) behaviour.

    Limit of the check: FTS here has no stemming (`unicode61`), so the exact form of the
    name is caught — «Резиденція» is found, «Резиденції» is not. The error goes toward
    "silent", i.e. the list stays fuller than reality, not empty.
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
    # Candidates are taken with a margin: some drop out on the text check, and
    # without a margin the digest would return fewer topics than asked for.
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
                except Exception as exc:      # FTS syntax on exotic names
                    logger.debug("stale_topics: FTS skip %r (%s)", r["canonical_name"], exc)
                    hit = None
                if hit and hit["d"] and hit["d"] > last_seen:
                    last_seen, source, mentions = hit["d"], "text", hit["n"]
            days_silent = (t - date.fromisoformat(last_seen)).days
            if days_silent < days:            # the topic is alive, the graph just does not know
                continue
            out.append({**dict(r), "last_seen": last_seen, "days_silent": days_silent,
                        "last_seen_source": source, "mentions_after_graph": mentions})
            if len(out) >= limit:
                break
    return out


def _recent_corrections(db_path: str, days: int, limit: int) -> list[dict]:
    """Correction comments for the period — a separate digest bucket (Wave 5).

    Why in the digest. Corrections are the places where THE ARCHIVE MISLEADS: the owner
    read a record and said it was actually different. Such a row is worth more than
    any task of the week, because until it is read every answer about this
    record stays wrong. But by itself it surfaces nowhere: the digest buckets
    hold only tasks and topics, and a comment is not a task.

    Empty on an old DB without migration v37 — the digest must not fail because of
    an add-on.
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
        logger.debug("[digest] corrections not attached", exc_info=True)
        return []
    return [dict(r) for r in rows]


def weekly_digest(db_path: str, *, owner: Optional[str] = None, days_back: int = 30,
                  limit: int = 15, today: Optional[date] = None) -> dict:
    """Monday digest: this week / overdue / dropped / topics that
    vanished / archive corrections."""
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
        # Places where the archive turned out untrue and the owner corrected it. Until
        # the row is read, every answer about that record stays wrong.
        "corrections": _recent_corrections(db_path, days_back, limit),
        # Without a denominator the digest reads as "five things to do this week". In fact
        # the first three buckets share only the dated quarter of tasks. The denominator
        # is computed on the table state and does not depend on `today` — see the docstring.
        "coverage": commitments_coverage(db_path, owner=owner),
    }


# ============================================================
# Changing a task's status with a journal (K5 k5-task-buttons)
# ============================================================
# The single write path for briefing buttons (source='bot') and /tasks (source='web'):
# the change and the event in `action_item_events` — in one transaction, so the journal cannot
# diverge from the state. MCP does not come here (read-only, `mcp-read-first-strategy`).

STATUS_ACTIONS = ("done", "cancelled", "open")
POSTPONE_DAYS = {"postpone:1": 1, "postpone:7": 7, "postpone:30": 30}


def _item_state(conn, item_id: int):
    row = conn.execute("SELECT id, status, due_date, due_precision FROM action_items "
                       "WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        from app.core.i18n import tr
        raise LookupError(tr("svc_tasks.err.itemNotFound", id=item_id))
    return row


def _write_event(conn, row, *, source: str, actor: Optional[str], action: str,
                 status: str, due_date: Optional[str], due_precision: Optional[str],
                 undo_of: Optional[int] = None) -> dict:
    conn.execute("UPDATE action_items SET status = ?, due_date = ?, due_precision = ? "
                 "WHERE id = ?", (status, due_date, due_precision, row["id"]))
    cur = conn.execute(
        "INSERT INTO action_item_events (item_id, at, source, actor, action, old_status, "
        "new_status, old_due_date, new_due_date, old_due_precision, new_due_precision, undo_of) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], datetime.now().isoformat(timespec="seconds"), source, actor, action,
         row["status"], status, row["due_date"], due_date, row["due_precision"],
         due_precision, undo_of))
    conn.commit()
    return {"event_id": cur.lastrowid, "item_id": row["id"], "status": status,
            "due_date": due_date}


def apply_action(db_path: str, item_id: int, action: str, *, source: str,
                 actor: Optional[str] = None, today: Optional[date] = None) -> dict:
    """Change the status (`done|cancelled|open`) or postpone the deadline (`postpone:1|7|30`).

    Postponing: `due_date` = today + N days, `due_precision='day'`, status -> `open`
    (from `stale` too — the owner has just confirmed the task is alive). The raw `due` phrase
    is not rewritten. -> `{event_id, item_id, status, due_date}`.
    Unknown task -> `LookupError`, unknown action -> `ValueError`; the DB is unchanged."""
    if action not in STATUS_ACTIONS and action not in POSTPONE_DAYS:
        from app.core.i18n import tr
        raise ValueError(tr("svc_tasks.err.unknownAction", action=action))
    with get_db_connection(db_path) as conn:
        row = _item_state(conn, item_id)
        if action in POSTPONE_DAYS:
            new_due = ((today or date.today()) + timedelta(days=POSTPONE_DAYS[action])).isoformat()
            return _write_event(conn, row, source=source, actor=actor, action=action,
                                status="open", due_date=new_due, due_precision=P_DAY)
        return _write_event(conn, row, source=source, actor=actor, action=action,
                            status=action, due_date=row["due_date"],
                            due_precision=row["due_precision"])


def undo_action(db_path: str, event_id: int, *, source: str,
                actor: Optional[str] = None) -> dict:
    """Undo an event: restore the task's status and deadline from before it.

    Only the task's LAST event (otherwise the rollback would wipe a later change) -> else `ValueError`.
    Undoing is an event too (`action='undo'`, `undo_of`), so the journal stays complete."""
    with get_db_connection(db_path) as conn:
        ev = conn.execute("SELECT * FROM action_item_events WHERE id = ?",
                          (event_id,)).fetchone()
        if ev is None:
            from app.core.i18n import tr
            raise LookupError(tr("svc_tasks.err.eventNotFound", id=event_id))
        last = conn.execute("SELECT MAX(id) AS m FROM action_item_events WHERE item_id = ?",
                            (ev["item_id"],)).fetchone()["m"]
        if last != event_id or ev["action"] == "undo":
            from app.core.i18n import tr
            raise ValueError(tr("svc_tasks.err.undoLastOnly"))
        row = _item_state(conn, ev["item_id"])
        return _write_event(conn, row, source=source, actor=actor, action="undo",
                            status=ev["old_status"], due_date=ev["old_due_date"],
                            due_precision=ev["old_due_precision"], undo_of=event_id)


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
        # dry_run is the default here: the command moves tasks that are ALREADY linked.
        # An explicit --dry-run beats --apply: of the two readings of intent we take
        # the one that writes nothing.
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
        # Writes to the DB, so the estimate is the default, not behind a flag.
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
        description="Normalization of task deadlines/owners + digests (Track 1).")
    p.add_argument("--db", default=default_db)
    p.add_argument("--dry-run", action="store_true", help="Write nothing to the DB")
    # --dry-run is also accepted AFTER the subcommand (`backfill --dry-run`) — otherwise
    # the most important safeguard works only with the "right" word order.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Write nothing to the DB")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", parents=[common], help="due → due_date/due_precision")
    sub.add_parser("owners", parents=[common], help="owner_name → owner_entity_id")
    rl = sub.add_parser("relink", parents=[common],
                        help="Re-point tasks to a more precise person "
                             "(estimate by default)")
    rl.add_argument("--apply", action="store_true", help="Apply instead of estimating")
    d = sub.add_parser("dedup", parents=[common], help="Mark duplicate tasks (dup_of)")
    d.add_argument("--threshold", type=float, default=DEDUP_THRESHOLD)
    s = sub.add_parser("stale", parents=[common],
                       help="Old tasks with no future deadline -> stale")
    s.add_argument("--days", type=int, default=STALE_DAYS)
    g = sub.add_parser("digest", help="Show the digest (read-only)")
    g.add_argument("--owner", default=None)
    # The digest looks at 30 days, but the backlog is older: 1242 live tasks without a date
    # stretch over three months, and what is "dropped" among them is visible only with a wider window.
    # The pass is not instant (word rarity is counted over the index), hence a separate
    # command rather than part of the digest.
    dr = sub.add_parser("dropped", help="Promises with no trace over a longer period (read-only)")
    dr.add_argument("--days", type=int, default=120, help="Window depth, days")
    dr.add_argument("--min-age", type=int, default=14,
                    help="Quarantine: fresh agreements are not counted as lost")
    dr.add_argument("--limit", type=int, default=50)
    # 300 is the digest ceiling, and for a wider window it is meaningless: candidates
    # are selected by `meeting_date DESC`, so over 120 days (tasks) the older
    # backlog — exactly what the command exists for — never reached the check.
    dr.add_argument("--max-candidates", type=int, default=3000)

    sw = sub.add_parser("sweep-dropped", parents=[common],
                        help="Drop promises whose topic went quiet (open -> stale, no_trace)")
    sw.add_argument("--days", type=int, default=180, help="Window depth, days")
    sw.add_argument("--min-age", type=int, default=30,
                    help="Quarantine: fresh agreements are not dropped")
    sw.add_argument("--undated-only", action="store_true",
                    help="Only tasks without a date")
    sw.add_argument("--apply", action="store_true",
                    help="Apply (without it — an estimate)")

    args = p.parse_args(argv)
    if args.command == "dedup" and not hasattr(args, "threshold"):
        args.threshold = DEDUP_THRESHOLD
    return _cmd(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
