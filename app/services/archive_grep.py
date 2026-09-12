"""Буквальний і regex-пошук по чанках архіву (grep-explainability, історія 01,
ремонт — історія 07 / Plan deltas D1).

**Проблема:** `chunks_fts` (FTS5, `tokenize='unicode61 remove_diacritics 1'`)
токенізує текст — розбиває `ID-4471`, `1 200 000`, `@nick` на окремі токени
і не знаходить їх як рядок. Reranker поверх векторного/FTS-пошуку додатково
ховає точні ідентифікатори, суми й імена за семантично близькими, але не
точними збігами. `grep()` — окремий, без ембеддингів і без ранжування,
прохід по `chunks`, що відповідає рівно на «де в архіві зустрічається цей
рядок (або цей regex)».

**Контракт** (від `docs/specs/grep-explainability/plan.md` §C1 — джерело
істини, не змінювати мовчки): сигнатура `grep()` і форма кожного елемента
`matches` фіксовані, бо історія 04 (MCP-тулза) будується поверх них без змін.
Історія 07 додала до верхнього рівня відповіді сумісні поля (`match_total`,
`truncated_reason`, `chunk_boundary_caveat`, `arbitrary_scan_caveat`) — старі
ключі не перейменовані і не прибрані.

**Кирилиця (Plan deltas D1, знахідка 1 — критична).** Регістр згортається
ВИКЛЮЧНО в Python (`str.casefold`), ніколи в SQL: SQLite `lower()` згортає
лише ASCII, тож `instr(lower(text), lower(?))` мовчки повертає 0 на кирилиці.
Буквальний режим казфолдить рядок і голку в Python; regex-режим використовує
`re.IGNORECASE` (для str-патернів у Python він теж працює на юнікод-таблицях
регістру) — обидва мусять означати одне й те саме під `ignore_case=True`.

**Сканування (Plan deltas D1, знахідки 2-3).** Без `ORDER BY` у SQL-запиті:
сортування по невіндексованому виразу змусило б SQLite прогнати фільтр по
всьому джойну до `LIMIT`. Курсор віддає рядки в природному порядку, кожен
перевіряється в Python; `max_scan` рахує саме ПЕРЕГЛЯНУТІ рядки і зупиняє
сам скан (а не тільки видачу). Знайдені збіги сортуються вже в Python
(`meeting_date DESC, chunk_index`). Додатково — дедлайн стінного часу,
перевірений МІЖ рядками: захищає stdio-процес MCP від regex-патерну з
катастрофічним бектрекінгом. Це не рятує від одного рядка, чия власна
перевірка вже перевищує дедлайн, — лише від накопичення по багатьох рядках.

**Межа чанків (Plan deltas D1, знахідка 8) — не лікуємо.** Пошук іде по
`chunks`, не по повному тексту транскрипції: рядок, розрізаний швом
чанкування навпіл, чесно не знайдеться. Наскрізний пошук по
`transcriptions.transcript_text` — окрема поверхня пошуку, лишена в роадмепі.

**Не імпортує** `torch`, `numpy`-ембеддинги, `app.services.retrieval` — модуль
має вантажитись у stdio-процесі MCP без важких моделей (памʼятка
`mcp-stdio-no-heavy-models`).
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from app.db.connection import get_db_connection

# COALESCE — Telegram-повідомлення часто без явної meeting_date, тоді дата
# зустрічі відсутня і датою слугує дата запису в архів (як у retrieval.py).
_MEETING_DATE_EXPR = "COALESCE(t.meeting_date, substr(t.created_at, 1, 10))"

_EXCERPT_RADIUS = 80  # символів по кожен бік першого збігу

# Дедлайн стінного часу на весь скан, перевіряється МІЖ рядками (не всередині
# виконання одного regex-виклику). Тести можуть підмінити константу через
# monkeypatch, щоб не чекати справжні секунди на катастрофічний бектрекінг.
_DEFAULT_DEADLINE_SECONDS = 5.0

_CHUNK_BOUNDARY_CAVEAT = (
    "Пошук іде по chunks, не по повному тексту транскрипції: рядок, "
    "розрізаний швом чанкування навпіл, тут не знайдеться (наскрізний пошук "
    "по transcriptions.transcript_text — окрема, ще не зроблена поверхня)."
)

# Показується ЛИШЕ коли скан зупинився достроково (max_scan/deadline), а не
# коли truncated_reason == "limit". Без ORDER BY курсор SQLite віддає рядки в
# природному порядку, тож дострокова зупинка оглядає ДОВІЛЬНИЙ префікс, а не
# найновіші max_scan рядків: результат після сортування — найновіше СЕРЕД
# ПЕРЕГЛЯНУТОГО, не найновіше в архіві (Plan deltas D2, знахідка 3).
_ARBITRARY_SCAN_CAVEAT = (
    "Скан зупинився достроково (max_scan або дедлайн), не дійшовши до кінця "
    "таблиці: без ORDER BY переглянута підмножина рядків — довільний префікс "
    "природного порядку курсора, а НЕ гарантовано найновіші записи. matches "
    "відсортовані за meeting_date лише серед того, що встигли переглянути."
)


def _compile_pattern(pattern: str, ignore_case: bool) -> re.Pattern:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Некоректний regex '{pattern}': {exc}") from exc


def _make_excerpt(text: str, start: int, end: int) -> str:
    lo = max(0, start - _EXCERPT_RADIUS)
    hi = min(len(text), end + _EXCERPT_RADIUS)
    excerpt = text[lo:hi]
    if lo > 0:
        excerpt = "…" + excerpt
    if hi < len(text):
        excerpt = excerpt + "…"
    return excerpt


def _find_all(haystack: str, needle: str) -> list[int]:
    """Індекси всіх НЕпересічних входжень `needle` у `haystack` (як `str.count`)."""
    positions: list[int] = []
    start = 0
    step = len(needle)
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + step
    return positions


def _match_row(
    text: str,
    *,
    regex: bool,
    compiled: re.Pattern | None,
    needle: str,
    ignore_case: bool,
) -> tuple[int, str] | None:
    """Перевіряє ОДИН уже прочитаний рядок на збіг. Викликається рівно один
    раз на переглянутий рядок — саме цей виклик рахує тест на `max_scan`,
    доводячи, що стеля обмежує роботу, а не тільки видачу.

    Повертає `(match_count, excerpt)` навколо ФАКТИЧНОГО збігу, або `None`.
    """
    if regex:
        found = list(compiled.finditer(text))
        if not found:
            return None
        return len(found), _make_excerpt(text, found[0].start(), found[0].end())

    haystack = text.casefold() if ignore_case else text
    positions = _find_all(haystack, needle)
    if not positions:
        return None
    start = positions[0]
    # вікно уривка — по фактичній довжині збігу (casefold-голки), не по
    # довжині сирого патерну (Plan deltas / знахідка 6)
    return len(positions), _make_excerpt(text, start, start + len(needle))


def _get_context(conn: sqlite3.Connection, transcription_id: int, chunk_index: int, context: int) -> tuple[list[dict], list[dict]]:
    if context <= 0:
        return [], []
    before_rows = conn.execute(
        "SELECT chunk_index, text FROM chunks "
        "WHERE transcription_id = ? AND chunk_index BETWEEN ? AND ? "
        "ORDER BY chunk_index",
        (transcription_id, chunk_index - context, chunk_index - 1),
    ).fetchall()
    after_rows = conn.execute(
        "SELECT chunk_index, text FROM chunks "
        "WHERE transcription_id = ? AND chunk_index BETWEEN ? AND ? "
        "ORDER BY chunk_index",
        (transcription_id, chunk_index + 1, chunk_index + context),
    ).fetchall()
    before = [{"chunk_index": r["chunk_index"], "text": r["text"]} for r in before_rows]
    after = [{"chunk_index": r["chunk_index"], "text": r["text"]} for r in after_rows]
    return before, after


def grep(
    db_path: str,
    pattern: str,
    *,
    regex: bool = False,
    ignore_case: bool = True,
    context: int = 1,
    limit: int = 20,
    source_type: str | None = None,
    transcription_id: int | None = None,
    days: int | None = None,
    max_scan: int = 200_000,
) -> dict[str, Any]:
    """Буквальний або regex-пошук по `chunks`, з метаданими і ±context сусідами.

    Порядок результатів — `meeting_date DESC, chunk_index` (не релевантність:
    тут немає ні векторного, ні FTS-ранжування, ні reranker), обчислений у
    Python над уже зібраними збігами (SQL-запит навмисно без `ORDER BY` —
    див. докстрінг модуля). `truncated=True`, якщо вперлись у `max_scan`
    (переглянуто рядків стільки, скільки дозволено, і, можливо, лишились
    непереглянуті), у дедлайн стінного часу, або в `limit` (знайдено серед
    переглянутого більше, ніж показано) — `truncated_reason` каже, яка саме
    причина, а `match_total` — скільки знайдено серед ПЕРЕГЛЯНУТОГО (не в
    усьому архіві, якщо скан зупинився достроково).

    Рядки без дати (`meeting_date is None`) сортуються ОСТАННІМИ, після всіх
    датованих — контракт C1 оголошує `meeting_date: str | None`, а порівняння
    None зі str падає `TypeError`, тому вони виокремлені з сортування за датою
    і дописані в кінець без зміни взаємного порядку.

    Дострокова зупинка (`truncated_reason` — `"max_scan"` або `"deadline"`)
    оглядає ДОВІЛЬНИЙ префікс природного порядку курсора, не «найновіші
    max_scan рядків» — SQL навмисно без `ORDER BY` (див. докстрінг модуля).
    У такому разі відповідь несе `arbitrary_scan_caveat` з поясненням; коли
    зупинка сталась лише через `limit` (весь скан завершено), це поле — `None`.

    Регістр (`ignore_case=True`) згортається в Python (`str.casefold`) для
    обох режимів — НІКОЛИ в SQL (`SQLite lower()` не згортає кирилицю).

    Порожній чи пробільний `pattern` — помилка (`ValueError`), а не весь
    архів.

    Межа чанків: рядок, розрізаний швом чанкування, тут не знайдеться (див.
    докстрінг модуля і `chunk_boundary_caveat` у відповіді).
    """
    if not pattern or not pattern.strip():
        raise ValueError(
            "Порожній або пробільний патерн не приймається — інакше grep() "
            "тихо повернув би увесь архів."
        )

    compiled = _compile_pattern(pattern, ignore_case) if regex else None
    # для буквального режиму голка казфолдиться один раз тут, а не в SQL
    needle = pattern.casefold() if (not regex and ignore_case) else pattern

    where = ["t.deleted_at IS NULL"]
    params: list[Any] = []

    if source_type is not None:
        where.append("t.source_type = ?")
        params.append(source_type)
    if transcription_id is not None:
        where.append("ch.transcription_id = ?")
        params.append(transcription_id)
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        where.append(f"{_MEETING_DATE_EXPR} >= ?")
        params.append(cutoff)

    # Навмисно БЕЗ ORDER BY і без текстового предиката в SQL (Plan deltas D1):
    # і фільтр по тексту (regex/casefold), і фінальне сортування — у Python
    # нижче, щоб max_scan обмежував саме роботу сканування.
    sql = (
        "SELECT ch.id AS chunk_id, ch.transcription_id, ch.chunk_index, ch.text, "
        "ch.speaker, ch.start_time, t.source_type, t.source_name, "
        f"t.tg_chat_title, t.tg_link, {_MEETING_DATE_EXPR} AS meeting_date "
        "FROM chunks ch JOIN transcriptions t ON t.id = ch.transcription_id "
        f"WHERE {' AND '.join(where)}"
    )

    deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
    scanned = 0
    hit_scan_ceiling = False
    hit_deadline = False
    found: list[tuple[sqlite3.Row, int, str]] = []

    with get_db_connection(db_path) as conn:
        cur = conn.execute(sql, params)
        for row in cur:
            if scanned >= max_scan:
                hit_scan_ceiling = True
                break
            if time.monotonic() >= deadline:
                hit_deadline = True
                break
            scanned += 1
            text = row["text"] or ""
            result = _match_row(text, regex=regex, compiled=compiled, needle=needle, ignore_case=ignore_case)
            if result is not None:
                match_count, excerpt = result
                found.append((row, match_count, excerpt))

        match_total = len(found)

        # meeting_date DESC, chunk_index ASC — стабільне подвійне сортування
        # відтворює колишній SQL ORDER BY, прибраний зі скану вище. C1 оголошує
        # meeting_date як str | None (Telegram-рядки без дати зустрічі і без
        # created_at теж не виключені) — Python не вміє порівнювати None зі
        # str, тож NULL-рядки сортуються окремо і йдуть в кінець, не змінюючи
        # взаємний порядок датованих (Plan deltas D2, знахідка 1).
        found.sort(key=lambda item: item[0]["chunk_index"])
        dated = [item for item in found if item[0]["meeting_date"] is not None]
        undated = [item for item in found if item[0]["meeting_date"] is None]
        dated.sort(key=lambda item: item[0]["meeting_date"], reverse=True)
        found = dated + undated

        hit_limit_ceiling = match_total > limit
        selected = found[:limit]

        matches = []
        for row, match_count, excerpt in selected:
            context_before, context_after = _get_context(
                conn, row["transcription_id"], row["chunk_index"], context
            )
            matches.append({
                "chunk_id": row["chunk_id"],
                "transcription_id": row["transcription_id"],
                "chunk_index": row["chunk_index"],
                "source_type": row["source_type"],
                "source_name": row["source_name"],
                "meeting_date": row["meeting_date"],
                "speaker": row["speaker"],
                "start_time": row["start_time"],
                "tg_chat_title": row["tg_chat_title"],
                "tg_link": row["tg_link"],
                "match_count": match_count,
                "excerpt": excerpt,
                "context_before": context_before,
                "context_after": context_after,
            })

    if hit_deadline:
        truncated_reason = "deadline"
    elif hit_scan_ceiling:
        truncated_reason = "max_scan"
    elif hit_limit_ceiling:
        truncated_reason = "limit"
    else:
        truncated_reason = None

    return {
        "pattern": pattern,
        "regex": regex,
        "count": len(matches),
        "match_total": match_total,
        "scanned": scanned,
        "truncated": truncated_reason is not None,
        "truncated_reason": truncated_reason,
        "matches": matches,
        "chunk_boundary_caveat": _CHUNK_BOUNDARY_CAVEAT,
        "arbitrary_scan_caveat": (
            _ARBITRARY_SCAN_CAVEAT if truncated_reason in ("max_scan", "deadline") else None
        ),
    }
