"""Brand research / large export (Phase 18).

Collects from ALL digitised sources (audio transcripts, documents, Telegram,
YouTube, recordings) every mention of a brand/topic and returns it in two forms:

  1. ORIGINALS (0 tokens, no AI) - a continuous markdown: for each mention
     the verbatim fragment (the place of the mention +- context) + provenance (when/who/from where)
     + a link to the original (Recall / file:// / absolute path) + a link to
     the full transcript. Structured by date top to bottom (fresh - higher).

  2. SUMMARY (cheap model, only over the found fragments) - Claude (Haiku
     by default) makes a structured report by map-reduce EXCLUSIVELY over the extracted
     fragments (not whole transcripts) -> a minimum of tokens.

The search is lexical: FTS5 narrows the candidates (fast), then a Python scan
by substring in transcript_text/segments gives exact fragments and the mention counter.
The user sets spelling variants separated by commas («Horeca, хорека, HoReCa»).

Does NOT depend on the flask app context - takes db_path explicitly (testable).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterator, Optional

from app.db.connection import get_db_connection
from app.repositories import transcriptions as tx_repo
from app.services import text_polishing
from app.services.categories import (
    date_sql, filter_sql, parse_date, parse_filter, parse_tags, tags_sql,
)


logger = logging.getLogger(__name__)

# --- Fragment context ---
SEG_CTX = 2                 # +- segments around a mention (audio)
TEXT_CTX_CHARS = 400        # +- characters around a mention (text/documents)
SHORT_TEXT = 800            # shorter text is taken whole (TG messages)
MAX_RECORDS = 3000          # safety cap
MAX_FRAGMENTS_PER_RECORD = 12  # so that one wordy record does not bloat the file

# --- Summary (map-reduce) ---
SUMMARY_BATCH_CHARS = 40000  # ~12k tokens per batch -> one Haiku call

# Rates ($/Mtok) - a shared source with copilot/escalate (app/services/pricing.py).
from app.services.pricing import MODEL_PRICES as _PRICES
from app.services import models as _models
from app.services.claude_retry import call_with_retry
_MODEL_ALIASES = {
    "haiku": _models.HAIKU_4_5,
    "sonnet": _models.SONNET_4_6,
    "opus": _models.OPUS_4_8,
}
DEFAULT_SUMMARY_MODEL = _models.HAIKU_4_5

SRC_LABEL = {
    "youtube": "YouTube", "file": "Файл", "recording": "Запис",
    "document": "Документ", "telegram": "Telegram", "library": "Аудіотека",
    "meeting_archive": "Архів",
}


# ============================================================
# Terms / FTS
# ============================================================

def parse_terms(q: str) -> list[str]:
    """'Horeca, хорека ; HoReCa' -> ['Horeca', 'хорека', 'HoReCa'] (deduped, no empties)."""
    if not q:
        return []
    raw = re.split(r"[,;\n]+", q)
    out, seen = [], set()
    for t in raw:
        t = t.strip()
        if not t:
            continue
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _fts_query(terms: list[str]) -> str:
    """FTS5 OR query: each term as a quoted phrase. Quotes inside are escaped."""
    parts = []
    for t in terms:
        safe = t.replace('"', '""').strip()
        if safe:
            parts.append(f'"{safe}"')
    return " OR ".join(parts)


def entity_terms(db_path: str, entity_id: int) -> "tuple[str, list[str]] | None":
    """Entity -> (canonical name, [canon + all aliases]) for a lexical
    mention search. None if there is no entity. Aliases = "spelling variants"
    that the user would otherwise type by hand on /research."""
    with get_db_connection(db_path) as conn:
        ent = conn.execute("SELECT canonical_name FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not ent:
            return None
        aliases = [r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (entity_id,)).fetchall()]
    name = ent["canonical_name"]
    terms, seen = [], set()
    for t in [name, *aliases]:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    return name, terms


def _candidate_ids(conn, fts_q: str, category_id, *,
                   tags: "list[str] | None" = None,
                   since: "str | None" = None, until: "str | None" = None) -> list[int]:
    """Candidate transcript IDs via FTS5, with a direction/tags/period filter.

    T4.6: soft-deleted are always excluded (not only when category_id is given).
    The direction id expands to itself and its projects (`categories.filter_sql`);
    tags - OR over names; period - inclusive on both bounds.
    """
    cat_frag, cat_params = filter_sql("category_id", category_id)
    tags_frag, tags_params = tags_sql("id", tags or [])
    date_frag, date_params = date_sql("COALESCE(meeting_date, created_at)", since, until)
    # `+rowid`: otherwise the id list goes into FTS5 as a constraint and MATCH runs for
    # each id separately (fts-filter-pushdown; 0.39 s -> 0.001 s on the snapshot).
    sql = ("SELECT rowid FROM transcriptions_fts WHERE transcriptions_fts MATCH ? "
           "AND +rowid IN (SELECT id FROM transcriptions WHERE deleted_at IS NULL"
           + cat_frag + tags_frag + date_frag + ")")
    params: list = [fts_q, *cat_params, *tags_params, *date_params]
    rows = conn.execute(sql, params).fetchall()
    return [int(r["rowid"]) for r in rows]


# ============================================================
# Fragment extraction
# ============================================================

# A substring catches word forms ("ліцензія" -> "ліцензії"), but short terms ("BH",
# "Акме") would hook foreign words that way. So <=3 characters are matched at a word boundary (\b),
# longer ones as a substring (tolerates inflection and OCR tails).
_SHORT_TERM = 3


def _build_matchers(terms: list[str]) -> list:
    """[('sub', lowterm) | ('re', compiled)] - one matcher per term."""
    out = []
    for t in terms:
        t = t.strip()
        if not t:
            continue
        if len(t) <= _SHORT_TERM:
            out.append(("re", re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE | re.UNICODE)))
        else:
            out.append(("sub", t.lower()))
    return out


def _any_match(text: str, matchers: list) -> bool:
    if not text:
        return False
    low = text.lower()
    for kind, v in matchers:
        if kind == "sub":
            if v in low:
                return True
        elif v.search(text):
            return True
    return False


def _count_mentions(text: str, matchers: list) -> int:
    if not text:
        return 0
    low = text.lower()
    n = 0
    for kind, v in matchers:
        n += low.count(v) if kind == "sub" else len(v.findall(text))
    return n


def _iter_positions(text: str, matchers: list):
    """Yield (start, end) of each mention (for context windows in continuous text)."""
    low = text.lower()
    for kind, v in matchers:
        if kind == "sub":
            start = 0
            while True:
                idx = low.find(v, start)
                if idx < 0:
                    break
                yield idx, idx + len(v)
                start = idx + len(v)
        else:
            for m in v.finditer(text):
                yield m.start(), m.end()


def _speaker_map(conn, tid: int) -> dict:
    """{raw_label: name} for a transcript (named speakers only)."""
    rows = conn.execute(
        "SELECT m.raw_label, s.name FROM transcription_speaker_map m "
        "JOIN speakers s ON s.id = m.speaker_id WHERE m.transcription_id = ?",
        (tid,),
    ).fetchall()
    return {r["raw_label"]: r["name"] for r in rows if r["name"]}


def _speaker_label(raw: str, spmap: dict) -> str:
    if not raw:
        return ""
    if raw in spmap:
        return spmap[raw]
    if raw == "self":
        return "Ви"
    m = re.match(r"^SPEAKER_(\d+)$", raw)
    return f"Спікер {int(m.group(1)) + 1}" if m else raw


def _fmt_ts(sec) -> str:
    if sec is None:
        return ""
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def _fragments_from_segments(segments: list, matchers: list, spmap: dict) -> list[str]:
    """Segment ranges around mentions (+- SEG_CTX), merged. Each fragment is
    lines '[mm:ss] Name: text'."""
    hits = [i for i, s in enumerate(segments) if _any_match(s.get("text", ""), matchers)]
    if not hits:
        return []
    # Intervals [i-ctx, i+ctx], merge overlapping/adjacent ones.
    ranges: list[list[int]] = []
    for i in hits:
        a, b = max(0, i - SEG_CTX), min(len(segments) - 1, i + SEG_CTX)
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a, b])
    out = []
    for a, b in ranges[:MAX_FRAGMENTS_PER_RECORD]:
        lines = []
        for s in segments[a:b + 1]:
            who = _speaker_label(s.get("speaker", ""), spmap)
            ts = _fmt_ts(s.get("start"))
            prefix = ""
            if ts:
                prefix += f"[{ts}] "
            if who:
                prefix += f"{who}: "
            lines.append(prefix + (s.get("text") or "").strip())
        out.append("\n".join(lines).strip())
    return out


def _fragments_from_text(text: str, matchers: list) -> list[str]:
    """Windows +- TEXT_CTX_CHARS around each mention in continuous text,
    snapped to word boundaries and merged on overlap."""
    if not text:
        return []
    spans: list[list[int]] = []
    for s, e in _iter_positions(text, matchers):
        spans.append([max(0, s - TEXT_CTX_CHARS), min(len(text), e + TEXT_CTX_CHARS)])
    if not spans:
        return []
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = []
    for a, b in merged[:MAX_FRAGMENTS_PER_RECORD]:
        # snap to word boundaries
        if a > 0:
            sp = text.find(" ", a)
            a = sp + 1 if 0 <= sp < b else a
        if b < len(text):
            sp = text.rfind(" ", a, b)
            b = sp if sp > a else b
        frag = text[a:b].strip()
        prefix = "… " if a > 0 else ""
        suffix = " …" if b < len(text) else ""
        out.append(prefix + frag + suffix)
    return out


def _record_who(row) -> str:
    """Short 'who/from where' line by source type."""
    st = row["source_type"]
    if st == "telegram":
        bits = []
        if row["tg_chat_title"]:
            bits.append(f"чат «{row['tg_chat_title']}»")
        if row["tg_sender"]:
            bits.append(f"від {row['tg_sender']}")
        return ", ".join(bits)
    if st == "youtube" and row["youtube_author"]:
        return f"автор {row['youtube_author']}"
    if st == "document" and row["doc_type"]:
        return (row["doc_type"] or "").upper()
    return ""


def _abs_path(file_path: Optional[str]) -> Optional[str]:
    if not file_path:
        return None
    try:
        ap = os.path.abspath(file_path)
        return ap if os.path.isfile(ap) else None
    except Exception:
        return None


def collect(db_path: str, q: str, category_id=None, display=None, *,
           tags: "list[str] | None" = None,
           since: "str | None" = None, until: "str | None" = None) -> dict:
    """Find all mentions and extract fragments (NO AI). display - a nice name
    for headings (e.g. the entity's canonical name). `tags`/`since`/`until` -
    the same C4 axes as in /api/history (the direction id is already expanded to projects
    by the caller via `categories.parse_filter`). Returns a dict with records
    (by date descending), terms, display and stats. Each record:
      {id, source_type, title, date_display, date_sort, who, category_name,
       file_path(abs|None), mentions, fragments[str], title_only}."""
    terms = parse_terms(q)
    if not terms:
        return {"terms": [], "display": display, "records": [],
                "stats": {"records": 0, "mentions": 0, "by_source": {}}}
    matchers = _build_matchers(terms)
    fts_q = _fts_query(terms)

    with get_db_connection(db_path) as conn:
        cat_names = {r["id"]: r["name"] for r in
                     conn.execute("SELECT id, name FROM categories").fetchall()}
        ids = _candidate_ids(conn, fts_q, category_id, tags=tags, since=since, until=until)
        if len(ids) > MAX_RECORDS:
            logger.warning("[research] %d candidates > MAX_RECORDS=%d - truncating", len(ids), MAX_RECORDS)
            ids = ids[:MAX_RECORDS]

        records = []
        for tid in ids:
            row = tx_repo.get_by_id(conn, tid)
            if not row:
                continue
            text = row["transcript_text"] or ""
            segments = json.loads(row["segments"]) if row["segments"] else []

            fragments: list[str] = []
            if segments and _any_match(text, matchers):
                fragments = _fragments_from_segments(segments, matchers, _speaker_map(conn, tid))
            if not fragments:
                if text and len(text) <= SHORT_TEXT and _any_match(text, matchers):
                    fragments = [text.strip()]
                else:
                    fragments = _fragments_from_text(text, matchers)

            mentions = _count_mentions(text, matchers)
            date_sort = (row["meeting_date"] or (row["created_at"] or "")[:10] or "")
            records.append({
                "id": tid,
                "source_type": row["source_type"],
                "title": row["source_name"] or row["youtube_title"] or f"#{tid}",
                "date_sort": str(date_sort),
                "date_display": row["created_at"] or str(date_sort),
                "who": _record_who(row),
                "category_name": cat_names.get(row["category_id"]),
                "file_path": _abs_path(row["file_path"]),
                "mentions": mentions,
                "fragments": fragments,
                "title_only": not fragments,
            })

    # Fresh on top: by date_sort descending, then id descending.
    records.sort(key=lambda r: (r["date_sort"], r["id"]), reverse=True)

    by_source: dict = {}
    total_mentions = 0
    for r in records:
        by_source[r["source_type"]] = by_source.get(r["source_type"], 0) + 1
        total_mentions += r["mentions"]
    return {
        "terms": terms,
        "display": display,
        "records": records,
        "stats": {"records": len(records), "mentions": total_mentions, "by_source": by_source},
    }


# ============================================================
# Markdown - originals
# ============================================================

def _highlight(text: str, terms: list[str]) -> str:
    """Highlight mentions in **bold** (case-insensitive) without breaking markdown.
    Short terms (<=3) - at a word boundary, like the search."""
    if not terms:
        return text
    parts = []
    for t in sorted(terms, key=len, reverse=True):
        esc = re.escape(t)
        parts.append(rf"\b{esc}\b" if len(t) <= _SHORT_TERM else esc)
    pattern = re.compile("(" + "|".join(parts) + ")", re.IGNORECASE | re.UNICODE)
    return pattern.sub(lambda m: f"**{m.group(0)}**", text)


def _file_links_md(rec: dict, base_url: str) -> str:
    """Three ways to reach the original: Recall / file:// / absolute path."""
    app_link = f"[Відкрити в Recall]({base_url}transcript/{rec['id']})"
    ap = rec.get("file_path")
    if not ap:
        return app_link
    try:
        uri = Path(ap).as_uri()
    except Exception:
        uri = ""
    parts = [app_link]
    if uri:
        parts.append(f"[Відкрити файл]({uri})")
    parts.append(f"`{ap}`")
    return " · ".join(parts)


def _fmt_date_human(s: str) -> str:
    """'2026-05-29 14:03:11' -> '2026-05-29 14:03'; date-only is left as is."""
    if not s:
        return ""
    s = str(s)
    return s[:16] if len(s) >= 16 and " " in s else s[:10]


def render_originals_md(collected: dict, base_url: str) -> str:
    terms = collected["terms"]
    records = collected["records"]
    st = collected["stats"]
    terms_disp = collected.get("display") or ", ".join(terms)

    out = [f"# Дослідження: {terms_disp}", ""]
    out.append(f"> **Згадок:** {st['mentions']} у **{st['records']}** записах · "
               f"джерела: {_by_source_line(st['by_source'])}")
    out.append("> ")
    out.append("> Дослівні фрагменти, структуровано за датою (свіже — згори). "
               "Згадки виділені **жирним**.")
    out.append("")
    out.append("---")
    out.append("")

    if not records:
        out.append("_Нічого не знайдено._")
        return "\n".join(out)

    for rec in records:
        label = SRC_LABEL.get(rec["source_type"], rec["source_type"])
        date_h = _fmt_date_human(rec["date_display"] or rec["date_sort"])
        title = rec["title"]
        if len(title) > 90:
            title = title[:90].rstrip() + "…"
        out.append(f"## {date_h} · {label} — {title}")
        out.append("")
        meta = []
        if rec["who"]:
            meta.append(f"**Джерело:** {label} · {rec['who']}")
        else:
            meta.append(f"**Джерело:** {label}")
        if rec["category_name"]:
            meta.append(f"**Напрямок:** {rec['category_name']}")
        meta.append(f"**Згадок:** {rec['mentions']}")
        out.append(" · ".join(meta))
        out.append("")
        out.append(f"**Оригінал:** {_file_links_md(rec, base_url)}")
        out.append("")

        if rec["title_only"]:
            out.append("> _Згадка в назві / метаданих запису (у тілі тексту не знайдено фрагмента)._")
        else:
            blocks = []
            for frag in rec["fragments"]:
                hl = _highlight(frag, terms)
                quoted = "\n".join("> " + ln for ln in hl.split("\n"))
                blocks.append(quoted)
            out.append("\n>\n> …\n>\n".join(blocks))
        out.append("")
        out.append("---")
        out.append("")

    return "\n".join(out)


def _by_source_line(by_source: dict) -> str:
    if not by_source:
        return "—"
    return ", ".join(f"{SRC_LABEL.get(k, k)} {v}" for k, v in
                     sorted(by_source.items(), key=lambda kv: -kv[1]))


# ============================================================
# Summary - map-reduce over fragments (cheap model)
# ============================================================

_SUMMARY_SYSTEM = """Ти — аналітик, що готує стислий бізнес-звіт по бренду/темі на основі
витягнутих фрагментів з архіву (дзвінки, документи, Telegram, відео).

Тобі дають ТЕРМІН(и) і пронумеровані ФРАГМЕНТИ (кожен з номером [n], датою, типом
джерела, автором). Зроби структурований звіт markdown'ом ВИКЛЮЧНО на основі цих
фрагментів.

Правила:
- Українською мовою. Чисто, по-діловому, без води і без преамбул.
- Спирайся ТІЛЬКИ на надані фрагменти. НЕ вигадуй, не додавай знань ззовні.
- Структура: короткий огляд → хронологія (від свіжого до старого, з датами) →
  ключові факти/рішення/цифри → хто залучений → відкриті питання (якщо є).
- Конкретика: імена, цифри, дати, домовленості — як у фрагментах.
- Де доречно — посилайся на джерело [n].
- Поверни ЧИСТИЙ markdown без обгорток ```."""

_REDUCE_SYSTEM = """Ти зводиш кілька часткових конспектів по одному бренду/темі в ОДИН
цілісний бізнес-звіт markdown.

Правила:
- Українською. Без повторів, без преамбул, без обгорток ```.
- Структура: короткий огляд → хронологія (свіже→старе, з датами) →
  ключові факти/рішення/цифри → хто залучений → відкриті питання.
- Не додавай нічого, чого немає у часткових конспектах. Збережи дати й цифри."""


def _summary_blocks(records: list[dict]) -> list[str]:
    """Text blocks per record (fragments only, no markdown highlight)."""
    blocks = []
    for i, rec in enumerate(records, 1):
        if rec["title_only"]:
            continue
        label = SRC_LABEL.get(rec["source_type"], rec["source_type"])
        date_h = _fmt_date_human(rec["date_display"] or rec["date_sort"])
        head = f"[{i}] {date_h} · {label} «{rec['title']}»"
        if rec["who"]:
            head += f" ({rec['who']})"
        body = "\n".join(rec["fragments"])
        blocks.append(f"{head}\n{body}")
    return blocks


def _batch(blocks: list[str], max_chars: int) -> list[list[str]]:
    batches, cur, size = [], [], 0
    for b in blocks:
        if cur and size + len(b) > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b) + 2
    if cur:
        batches.append(cur)
    return batches


def _resolve_model(model: Optional[str]) -> str:
    if not model:
        return DEFAULT_SUMMARY_MODEL
    return _MODEL_ALIASES.get(model.lower(), model)


def _call_claude(client, model: str, system: str, user: str, timeout: float = 180.0):
    kwargs = dict(
        model=model, max_tokens=4096,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if _models.supports_adaptive_thinking(model):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "low"}

    def _do():
        with client.with_options(timeout=timeout).messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    # Retry on transient errors (429/5xx/timeout) - T6.3.
    final = call_with_retry(_do, what="research-summary")
    text = "".join(b.text for b in final.content if b.type == "text").strip()
    u = final.usage
    return text, (getattr(u, "input_tokens", 0) or 0), (getattr(u, "output_tokens", 0) or 0)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _cost(model: str, inp: int, outp: int) -> float:
    pin, pout = _PRICES.get(model, _PRICES[DEFAULT_SUMMARY_MODEL])
    return round(inp / 1_000_000 * pin + outp / 1_000_000 * pout, 4)


def summarize_stream(db_path: str, q: str, category_id=None,
                     model: Optional[str] = None, display=None, *,
                     tags: "list[str] | None" = None,
                     since: "str | None" = None, until: "str | None" = None) -> Iterator[str]:
    """SSE: brand overview via map-reduce (fragments only, cheap model).
    display - a nice name for the heading (e.g. the entity name). `tags`/`since`/
    `until` - passed through to `collect` without reinterpretation.
    Events: start -> progress* -> done{markdown,...} | error."""
    terms = parse_terms(q)
    if not terms:
        yield _sse("error", {"error": "Порожній запит"})
        return
    if not text_polishing.is_available():
        yield _sse("error", {"error": "ANTHROPIC_API_KEY не налаштовано"})
        return

    collected = collect(db_path, q, category_id, display=display,
                       tags=tags, since=since, until=until)
    records = collected["records"]
    blocks = _summary_blocks(records)
    if not blocks:
        yield _sse("error", {"error": "Не знайдено фрагментів для саммарі"})
        return

    model = _resolve_model(model)
    batches = _batch(blocks, SUMMARY_BATCH_CHARS)
    terms_disp = display or ", ".join(terms)
    yield _sse("start", {"records": collected["stats"]["records"],
                         "fragments": len(blocks), "batches": len(batches), "model": model})

    try:
        client = text_polishing._get_client()
        in_tok = out_tok = 0

        if len(batches) == 1:
            yield _sse("progress", {"step": 1, "total": 1, "label": "Аналізую джерела…"})
            user = (f"ТЕРМІН(и): {terms_disp}\n\nФРАГМЕНТИ АРХІВУ:\n\n"
                    + "\n\n".join(batches[0]))
            md, i, o = _call_claude(client, model, _SUMMARY_SYSTEM, user)
            in_tok += i
            out_tok += o
        else:
            partials = []
            for bi, batch in enumerate(batches, 1):
                yield _sse("progress", {"step": bi, "total": len(batches) + 1,
                                        "label": f"Аналізую джерела (частина {bi}/{len(batches)})…"})
                user = (f"ТЕРМІН(и): {terms_disp}\n\nФРАГМЕНТИ АРХІВУ (частина {bi}):\n\n"
                        + "\n\n".join(batch))
                p, i, o = _call_claude(client, model, _SUMMARY_SYSTEM, user)
                partials.append(p)
                in_tok += i
                out_tok += o
            yield _sse("progress", {"step": len(batches) + 1, "total": len(batches) + 1,
                                    "label": "Зводжу в підсумковий звіт…"})
            user = (f"ТЕРМІН(и): {terms_disp}\n\nЧАСТКОВІ КОНСПЕКТИ:\n\n"
                    + "\n\n---\n\n".join(partials))
            md, i, o = _call_claude(client, model, _REDUCE_SYSTEM, user)
            in_tok += i
            out_tok += o

        header = (f"# Саммарі: {terms_disp}\n\n"
                  f"> **Огляд по {collected['stats']['records']} записах** "
                  f"({collected['stats']['mentions']} згадок) · "
                  f"джерела: {_by_source_line(collected['stats']['by_source'])}  \n"
                  f"> _Згенеровано {model} по витягнутих фрагментах._\n\n---\n\n")
        markdown = header + md
        yield _sse("done", {
            "markdown": markdown,
            "model": model,
            "stats": collected["stats"],
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost": _cost(model, in_tok, out_tok),
        })
    except Exception as e:
        logger.error("[research] summarize failed: %s", e, exc_info=True)
        yield _sse("error", {"error": "Помилка генерації саммарі. Перевірте логи."})
