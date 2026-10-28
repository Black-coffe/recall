"""RAG "Ask the archive" (Phase 13C).

User question -> hybrid retrieval over the chunks of all meetings -> Claude
composes the answer STRICTLY from the found fragments, with [n] citations to specific
meetings (date + speaker + timecode). This is the "launched Claude Code in
meeting_archive and asked" experience, but built into Whisper.

Anti-hallucination: the model answers only from the provided context; if the data
is missing, it says so. Every source is returned to the client for clickable citations.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterator, Optional

from app.core import settings as _settings
from app.services import retrieval, text_polishing
from app.services import claude_retry
from app.services.claude_retry import is_retryable_error
from app.services.models import get_default_model, supports_adaptive_thinking


logger = logging.getLogger(__name__)

# T6.4: optional local cross-encoder rerank (app/services/retrieval.py
# rerank=True -> app/services/reranker.py). OFF BY DEFAULT -- enabled
# selectively only here (the "Ask the archive" RAG chat, the highest cost of a top-k mistake), and
# NOT in retrieval.search() globally -- copilot/categorize/MCP search stay
# without rerank and without a change in latency. We read the env directly (like
# COPILOT_ENABLED/embeddings.py) so that the module stays standalone-testable.
_RERANK_ENABLED = _settings.env_bool("RECALL_RERANK_ENABLED")

# production-rag-wave-b-07: query rewriting (retrieval.py `rewrite=`,
# app/services/query_rewrite.py) -- the same pattern as rerank above: default
# OFF, read at import. production-rag-wave-b-08 (Major 2, ADR-008):
# no code reads a bool flag with its own set of truthy values -- the same
# `env_bool` with which `retrieval.search` resolves its hot default.
_QUERY_REWRITE_ENABLED = _settings.env_bool("RAG_QUERY_REWRITE")

# How many fragments go into the answer context. 12, not 8 -- per the measurements of the
# eval harness on the golden-set (14.08.2026, after T6.5): recall@8 67.9% vs
# recall@12 82.1%, source_name_hit 78.6% vs 85.7%. That is, at k=8 some of the
# right sources stand right BEYOND the cut (a typical case --
# a source at the 9th position). Cost: +4 fragments in the prompt = +1.5k input
# tokens per question; output is unchanged. The copilot is NOT included here -- it has
# its own attention budgets per mode (5/7/9, Track 3).
_DEFAULT_TOP_K = int(os.environ.get("RAG_TOP_K", "12"))


_RAG_SYSTEM_PROMPT = """Ти — асистент по архіву робочих зустрічей/дзвінків компанії.

Тобі дають ПИТАННЯ і пронумеровані ФРАГМЕНТИ транскриптів різних мітингів
(кожен з номером [n], назвою мітингу, датою, спікером). Твоя задача — відповісти
на питання, спираючись ВИКЛЮЧНО на ці фрагменти.

Окремо тобі можуть дати КОМЕНТАРІ ВЛАСНИКА АРХІВУ — це не репліки з розмови, а
речення, які власник свідомо написав ПРО запис уже після нього: уточнення,
виправлення, акцент. Вони мають ВИЩИЙ пріоритет за сирий транскрипт:

- Якщо коментар СУПЕРЕЧИТЬ тому, що сказано у фрагменті транскрипту — вір
  коментарю, відповідай за ним і прямо скажи, що в записі було інакше.
- Коментар типу «виправлення» скасовує відповідне місце транскрипту, навіть
  якщо транскрипт звучить упевнено.
- Посилайся на коментарі так само, як на фрагменти — за їхнім номером [n].
- Не вигадуй коментарів і не приписуй власнику того, чого він не писав.

ФРАГМЕНТИ подаються тобі в ХРОНОЛОГІЧНОМУ порядку за датою джерела (коментарі
власника — завжди першими, далі решта від найранішої дати до найпізнішої).
Якщо кілька фрагментів описують ОДНУ Й ТУ Ж домовленість, суму чи рішення, але
з різними датами — веде ПІЗНІША версія (вищий номер [n] серед звичайних
фрагментів), а ранішу згадуй як «було: …», не як актуальний факт. Коментар
власника, якщо він є, усе одно вищий за обидві версії — правило вище.

Правила:
- Відповідай мовою питання (зазвичай українською).
- Використовуй ТІЛЬКИ інформацію з фрагментів. НЕ вигадуй, не додавай знань ззовні.
- Після кожного твердження став посилання на джерело у форматі [n] (можна кілька: [1][3]).
- Якщо фрагментів недостатньо для відповіді — чесно скажи, що в архіві бракує
  інформації, і вкажи що саме знайшлося дотичного (якщо є).
- Будь конкретним: імена, цифри, дати, рішення, домовленості — як у фрагментах.
- Структуруй відповідь (абзаци / короткі пункти), якщо це доречно. Без води.

Не повторюй саме питання. Не додавай преамбул типу «Ось відповідь»."""


def _fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


_COMMENT_KIND_LABEL = {
    "correction": "ВИПРАВЛЕННЯ",
    "decision": "РІШЕННЯ",
    "note": "НОТАТКА",
    "context": "КОНТЕКСТ",
    "question": "ПИТАННЯ",
}


def _fmt_comment(i: int, kind: str, label: str, date, text: str,
                 anchor_time=None, pinned: bool = False) -> str:
    """Comment header in the context.

    The words «КОМЕНТАР ВЛАСНИКА» stand first deliberately: without an explicit provenance
    mark the model would read a clarification as one more utterance from the call and
    weigh it on a par with it -- i.e. the whole point of the layer would vanish exactly where
    the answer is formulated.
    """
    head = f"[{i}] КОМЕНТАР ВЛАСНИКА ({_COMMENT_KIND_LABEL.get(kind, 'НОТАТКА')})"
    if pinned:
        head += ", закріплений"
    head += f" до «{label}»"
    if date:
        head += f" ({date})"
    ts = _fmt_time(anchor_time)
    if ts:
        head += f", до моменту ~{ts}"
    return f"{head}\n{text}"


def order_citables(chunks: list[dict],
                   attached_comments: Optional[list[dict]] = None) -> list[dict]:
    """A single order of sources -- both for the [n] numbering and for the `sources` array.

    WHY ONE FUNCTION, NOT TWO LISTS. The context puts comments first
    (the priority rule in the system prompt works only if the model
    read the clarification BEFORE the transcript it corrects), while `sources`
    used to be returned in output order. The numbers drifted apart: on chunks
    [t1, t2] plus one attached correction the context gave [1]=comment,
    [2]=t1, [3]=t2, while the client opened `sources[n-1]` -- i.e. [2] led to t2,
    and [3] went out of the array's bounds altogether and stayed bare text.
    The discrepancy hit exactly the case the layer was made for:
    any pinned/correction comment on a found record shifted ALL
    references by one.

    Attached comments also make it into the list: they are numbered in
    the context, so the client must be able to open them (they have
    `transcription_id`).

    `why` on every item. Found `chunks` already carry `why` from
    `retrieval.search` (contract C2). Attached comments are a different path: they
    came from `attach_comments` (an SQL query for pinned/correction by the cards
    of the output), which never called `retrieval.search`, so they have no `why`
    organically. A silent absence of the key triggers `KeyError` in the
    consumer -- so we set an explicit provenance marker instead of scoring.
    The marker carries THE SAME required set of C2 keys (`src`, `rrf`, `rec`,
    `by`, `top`), with zero contributions (`rrf`/`rec` = 0.0, `by` = []) and
    a fifth legal value of `top` -- `"attached"` (plan D2): a key that is
    sometimes there and sometimes not is the same `KeyError`, deferred. The list of
    keys is not rewritten as a literal here -- it is taken from
    `retrieval.build_placeholder_why` (single source of truth, story 13, plan D3).

    CHRONOLOGY (story 03). The rest (non-comments) goes by source date
    ascending -- the model reads "was" BEFORE "became" the way a human
    would read the archive in order, and the system prompt relies exactly on this ("the later
    version leads", below). The date is `meeting_date` (for a meeting/document -- the date of
    the recording/file, for telegram -- the same column, filled with the message's
    `tg_date`; `retrieval.search` puts it into all three types equally via
    `COALESCE(t.meeting_date, substr(t.created_at,1,10))`). Chunks without a date
    should not be expected, but if one turns up -- they go to the end, not to
    an arbitrary place.

    CHUNKS OF ONE RECORD (story 09, repair 5). The tie-break at equal date is
    NOT "retrieval order as is", but `(record_rank, chunk_index)`: `record_rank` is the
    position of the FIRST appearance of this `transcription_id` among `rest` (in the order
    in which the chunks came from retrieval), `chunk_index` is the field `retrieval.search`
    already puts into every hydrated row. This way two chunks of one meeting go
    consecutively by `chunk_index` (not interleaved "as found"), and the retrieval order
    stays the tie-break only BETWEEN records. A chunk without `chunk_index` (comment-
    like layers among `rest`) does not fail -- the record rank is enough, it keeps its
    place in the retrieval order.
    """
    comment_chunks = [c for c in chunks if c.get("source_type") == "comment"]
    attached = [dict(ac, source_type="comment", attached=True,
                     why=ac.get("why") or retrieval.build_placeholder_why(
                         "comment", "attached",
                         note="attached without search (pinned/correction)"))
                for ac in (attached_comments or [])]
    rest = [c for c in chunks if c.get("source_type") != "comment"]
    record_rank: dict = {}
    for i, c in enumerate(rest):
        tid = c.get("transcription_id")
        if tid not in record_rank:
            record_rank[tid] = i
    rest.sort(key=lambda c: (
        c.get("meeting_date") is None,
        c.get("meeting_date") or "",
        record_rank[c.get("transcription_id")],
        c.get("chunk_index") if c.get("chunk_index") is not None else 0,
    ))
    return comment_chunks + attached + rest


def _build_context(chunks: list[dict], attached_comments: Optional[list[dict]] = None) -> str:
    """Context for Claude. Order and numbering come from `order_citables`."""
    parts = []
    for i, ch in enumerate(order_citables(chunks, attached_comments), 1):
        source_type = ch.get("source_type")
        if source_type == "comment":
            # An attached comment comes from `attach_comments` (keys kind/
            # date/anchor_time), a standalone finding -- from retrieval
            # (comment_kind/meeting_date/start_time). We read both forms.
            parts.append(_fmt_comment(
                i,
                ch.get("comment_kind") or ch.get("kind"),
                ch.get("target_label") or ch.get("source_name"),
                ch.get("meeting_date") or ch.get("date"),
                ch["text"],
                ch.get("start_time") if ch.get("start_time") is not None
                else ch.get("anchor_time"),
                ch.get("pinned")))
            continue
        if source_type == "telegram":
            # A correspondence is NOT a meeting. Earlier this took «Мітинг «[TG] чат: сніпет»»,
            # i.e. the model retold the vast majority of the corpus as "at the meeting you decided",
            # and without even knowing the author. The chat is taken out separately (not as part of
            # source_name), the author too: in TG it is tg_sender, which now arrives
            # in chunks.speaker.
            chat = ch.get("tg_chat_title") or ch.get("source_name")
            head = f"[{i}] Telegram, чат «{chat}»"
            if ch.get("meeting_date"):
                head += f" ({ch['meeting_date']})"
            if ch.get("speaker"):
                head += f", від: {ch['speaker']}"
            if ch.get("tg_reply_to"):
                head += f", відповідь на повідомлення {ch['tg_reply_to']}"
            # Thread (Wave 4.5): in a correspondence a finding is often a QUESTION, and
            # the answer is the next reply, which has no words in common with the
            # query, so on its own it is never found. We give the conversation
            # as a piece and explicitly mark what exactly was found -- otherwise the model will not
            # tell the finding from the context and will cite the neighbour.
            thread = ch.get("thread")
            if thread and thread.get("messages"):
                label = thread.get("label")
                head += f"\nНитка «{label}»" if label else "\nНитка розмови"
                if thread.get("total_messages"):
                    head += f" ({len(thread['messages'])} з {thread['total_messages']} повідомлень)"
                lines = []
                for m in thread["messages"]:
                    mark = "→ " if m.get("is_hit") else "  "
                    when = (m.get("date") or "")[:16].replace("T", " ")
                    who = m.get("sender") or "невідомо"
                    lines.append(f"{mark}[{when} {who}] {m.get('text') or ''}")
                parts.append(f"{head}\n" + "\n".join(lines))
                continue
            parts.append(f"{head}\n{ch['text']}")
            continue

        kind = "Документ" if source_type == "document" else "Мітинг"
        # We name the record the way the owner named it (`display_name` --
        # `title`, otherwise the provenance `source_name`): the model cites the name the
        # person sees in the list, not "rec_2026-09-17_final.mp3".
        head = f"[{i}] {kind} «{ch.get('display_name') or ch['source_name']}»"
        if ch.get("meeting_date"):
            head += f" ({ch['meeting_date']})"
        if ch.get("speaker"):
            head += f", спікер: {ch['speaker']}"
        # Provenance of documents -- page / slide / sheet (+ section heading)
        page = ch.get("page")
        if page:
            dt = ch.get("doc_type")
            label = "слайд" if dt == "pptx" else ("лист" if dt in ("xlsx", "csv") else "стор.")
            head += f", {label} {page}"
            if ch.get("section"):
                head += f" «{ch['section']}»"
        ts = _fmt_time(ch.get("start_time"))
        if ts:
            head += f", ~{ts}"
        parts.append(f"{head}\n{ch['text']}")
    return "\n\n".join(parts)


def _build_request_kwargs(question: str, chunks: list[dict], model: Optional[str],
                          attached_comments: Optional[list[dict]] = None) -> tuple[dict, str]:
    """Shared construction of the request to Claude (for sync and stream). Returns (kwargs, model)."""
    model = model or get_default_model()
    user_message = (
        f"ПИТАННЯ: {question}\n\n"
        f"ФРАГМЕНТИ АРХІВУ:\n\n{_build_context(chunks, attached_comments)}"
    )
    kwargs = dict(
        model=model,
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": _RAG_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "medium"}
    return kwargs, model


def _resolve_project(db_path: str, project: Optional[str]) -> Optional[list[int]]:
    """Project/people names -> entity ids to narrow the search (Track 2).

    The second scope layer: the category is not enough, because «Фонд Робота» is more than half of the corpus.
    An empty result (the name was not found) does NOT narrow the search to zero -- we return
    None, i.e. "search everywhere": a wider answer is better than silence because of a typo
    in the project name.
    """
    if not project:
        return None
    from app.services import scope
    ids = scope.scope_filter_ids(db_path, project)
    if not ids:
        logger.info("rag: project/person «%s» not found in the graph -- searching without narrowing", project)
        return None
    return ids


def _mcp_screen_filter(db_path: str, channel: str, chunks: list) -> list:
    """A question from MCP: text from the screen (speaker='екран') passes the privacy filter BEFORE
    the Claude call -- private records, stop words, RECALL_MCP_VISUAL=off (spec
    mcp-visual-privacy). The ui channel (Recall's own interface) is not affected by the filter."""
    if channel != "mcp":
        return chunks
    from app.services import privacy
    kept, hidden = privacy.filter_screen_chunks(db_path, chunks)
    if hidden:
        logger.info("[rag] mcp: %d screen chunk(s) hidden by the privacy filter", hidden)
    return kept


def answer_question(
    db_path: str,
    question: str,
    top_k: int = _DEFAULT_TOP_K,
    model: Optional[str] = None,
    timeout: float = 180.0,
    category_id: Optional[int] = None,
    project: Optional[str] = None,
    explain: bool = False,
    channel: str = "ui",
    tags: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Answer a question about the archive with citations. category_id -- restrict by direction
    (a direction id also covers its projects -- `categories.filter_sql`).
    explain -- Story 05: passed through to `retrieval.search`, the `why` of each
    chunk survives to `sources` through `order_citables`/`attach_thread_context`.
    tags/since/until -- directions-projects-tags-03 (contract C5): passed through
    to `retrieval.search` as is (OR over tags, inclusive ISO dates); parsing a phrase
    like "for the week" is done by the caller, not Recall.

    Returns {"answer", "sources": [chunks], "model", "found", token usage}.
    """
    question = (question or "").strip()
    if not question:
        return {"answer": "", "sources": [], "found": 0, "model": ""}

    scope_tids = _resolve_project(db_path, project)
    res = retrieval.search(db_path, question, top_k=top_k, category_id=category_id,
                           scope_tids=scope_tids,
                           rerank=_RERANK_ENABLED,
                           rewrite=_QUERY_REWRITE_ENABLED,
                           explain=explain,
                           tags=tags, since=since, until=until)
    chunks = _mcp_screen_filter(db_path, channel,
                                retrieval.attach_thread_context(db_path, res["chunks"]))
    if not chunks:
        return {
            "answer": "У архіві не знайдено релевантної інформації за цим запитом.",
            "sources": [], "found": 0, "model": "",
            "vector_available": res.get("vector_available", False),
        }

    # Priority layer 2: corrections and pinned comments of those records that
    # made it into the output -- even if they themselves did not match the query.
    attached = retrieval.attach_comments(db_path, chunks)
    # `sources` must be THE SAME list that is numbered in the context --
    # otherwise [n] in the answer points to the wrong source (see order_citables).
    sources = order_citables(chunks, attached)

    client = text_polishing._get_client()
    request_kwargs, model = _build_request_kwargs(question, chunks, model, attached)

    logger.info("[rag] ask: model=%s, chunks=%d, comments=%d, q=%r",
                model, len(chunks), len(attached), question[:80])
    # Retry on transient errors (429/5xx/timeout) -- T6.3. The whole call
    # again on each attempt, safe here: get_final_message() gives nothing
    # to the consumer until it fully completes.
    result = text_polishing._stream_with_retry(client, timeout, request_kwargs, what="rag-ask")

    answer = "".join(b.text for b in result.content if b.type == "text").strip()
    usage = result.usage
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
    ask_id = _log_ask(db_path, channel=channel, question=question, top_k=top_k,
                      model=result.model, sources=sources, answer=answer,
                      category_id=category_id, project=project,
                      input_tokens=input_tokens, output_tokens=output_tokens,
                      cache_read_tokens=cache_read_tokens)
    return {
        "answer": answer,
        "sources": sources,
        "found": len(sources),
        "model": result.model,
        "vector_available": res.get("vector_available", False),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "ask_id": ask_id,
    }


#: Allowed question channels (contract `ask_log.channel`).
ASK_CHANNELS = ("ui", "mcp")


def _ask_cost(model: str, input_tokens: int, output_tokens: int,
              cache_read_tokens: int) -> Optional[float]:
    """$ per question or None for a model outside the price table.

    The table lives in `app/services/pricing.py` (the single point of truth -- its
    absence once gave a silent estimate at the price of a FOREIGN model, T6.2).
    Here an explicit None was added instead of a fallback to the default tariff: in the log
    an empty cost is better than a plausible falsehood.
    """
    from app.services import pricing
    if not model or model not in pricing.MODEL_PRICES:
        return None
    return pricing.estimate_cost(model, input_tokens, output_tokens, cache_read_tokens)


def _log_ask(db_path: str, *, channel: str, question: str, top_k: int,
             model: str, sources: list[dict], answer: str,
             category_id: Optional[int], project: Optional[str],
             input_tokens: int, output_tokens: int, cache_read_tokens: int) -> Optional[int]:
    """Write a successful answer to `ask_log` -> row id (`ask_id`) or None.

    Best-effort: the question log has no right to break the answer itself, which has already been
    paid for (and no right to fail on a DB without migration v41 -- offline tests
    run `answer_question` on `:memory:`). A failure is written to the log, not outward.
    """
    from app.db.connection import get_db_connection
    try:
        scope = {"category_id": category_id, "project": project}
        source_ids = []
        for s in sources:
            tid = s.get("transcription_id")
            if tid is not None and tid not in source_ids:
                source_ids.append(tid)
        cost = _ask_cost(model, input_tokens, output_tokens, cache_read_tokens)
        with get_db_connection(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO ask_log (channel, question, scope_json, k, model, "
                "source_ids_json, input_tokens, output_tokens, cache_read_tokens, "
                "cost_usd, answer) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (channel if channel in ASK_CHANNELS else "ui", question,
                 json.dumps(scope, ensure_ascii=False), top_k, model,
                 json.dumps(source_ids), input_tokens, output_tokens,
                 cache_read_tokens, cost, answer))
            conn.commit()
            return cur.lastrowid
    except Exception as exc:
        logger.warning("[rag] ask_log: failed to record the question: %s", exc)
        return None


def rate_ask(db_path: str, ask_id: int, rating: int, note: Optional[str] = None) -> bool:
    """Owner's rating of an `ask_log` row (1 / -1) + a note. False -- no such id.

    The rating is overwritten: the owner has the right to change their mind, the history of ratings
    is needed by nobody -- the last verdict for the golden-set is what's needed.
    """
    from app.db.connection import get_db_connection
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE ask_log SET rating = ?, note = ?, rated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (int(rating), (note or "").strip() or None, int(ask_id)))
        conn.commit()
        return cur.rowcount > 0


def _sse(event: str, data: dict) -> str:
    """Build one SSE frame. data is JSON-encoded (newlines are preserved as \\n)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def answer_question_stream(
    db_path: str,
    question: str,
    top_k: int = _DEFAULT_TOP_K,
    model: Optional[str] = None,
    timeout: float = 180.0,
    category_id: Optional[int] = None,
    project: Optional[str] = None,
    explain: bool = False,
    channel: str = "ui",
    tags: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Iterator[str]:
    """Streaming version of answer_question. category_id -- restrict by direction. explain --
    Story 05, the same as in answer_question (does not change the SSE event format).
    tags/since/until -- the same axes as in answer_question (contract C5).
    Yields SSE frames:
      event: sources -- {sources, found, vector_available} (right after retrieval)
      event: delta   -- {text} (answer tokens as they arrive)
      event: done    — {model, *_tokens}
      event: error   — {error}
    """
    question = (question or "").strip()
    if not question:
        yield _sse("error", {"error": "Порожнє питання"})
        return

    scope_tids = _resolve_project(db_path, project)
    res = retrieval.search(db_path, question, top_k=top_k, category_id=category_id,
                           scope_tids=scope_tids,
                           rerank=_RERANK_ENABLED,
                           rewrite=_QUERY_REWRITE_ENABLED,
                           explain=explain,
                           tags=tags, since=since, until=until)
    chunks = _mcp_screen_filter(db_path, channel,
                                retrieval.attach_thread_context(db_path, res["chunks"]))
    attached = retrieval.attach_comments(db_path, chunks)
    # The same order as in the context: the client resolves [n] as sources[n-1],
    # so attached comments must be HERE, not in a separate field -- they are
    # numbered on a par with the rest (see order_citables).
    sources = order_citables(chunks, attached)
    yield _sse("sources", {
        "sources": sources, "found": len(sources),
        "retrieved": len(chunks),      # how many the search actually found
        "attached": len(attached),     # how many were attached without a match
        "vector_available": res.get("vector_available", False),
    })

    if not chunks:
        yield _sse("delta", {"text": "У архіві не знайдено релевантної інформації за цим запитом."})
        yield _sse("done", {"found": 0, "model": ""})
        return

    try:
        client = text_polishing._get_client()
        request_kwargs, model = _build_request_kwargs(question, chunks, model, attached)
        logger.info("[rag] ask-stream: model=%s, chunks=%d, comments=%d, q=%r",
                    model, len(chunks), len(attached), question[:80])

        # Retry on transient errors (429/5xx/timeout) -- T6.3. We repeat the
        # WHOLE call again, BUT only while in this attempt no delta has yet
        # been given to the consumer (the SSE client) -- after the first delta a retry is
        # dangerous (duplicated/corrupted output), so further the error is
        # propagated as is to the outer except.
        attempt = 0
        final = None
        while True:
            yielded_any = False
            try:
                with client.with_options(timeout=timeout).messages.stream(**request_kwargs) as stream:
                    for text in stream.text_stream:
                        if text:
                            yielded_any = True
                            yield _sse("delta", {"text": text})
                    final = stream.get_final_message()
                break
            except Exception as exc:
                if yielded_any or attempt >= claude_retry.DEFAULT_MAX_RETRIES or not is_retryable_error(exc):
                    raise
                delay = claude_retry.backoff_delay(attempt)
                logger.warning(
                    "[rag] ask-stream: transient error (attempt %d/%d): %s -- retry in %.1fs",
                    attempt + 1, claude_retry.DEFAULT_MAX_RETRIES + 1, exc, delay,
                )
                time.sleep(delay)
                attempt += 1

        usage = final.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        # We take the text from the final message, not from the accumulated deltas --
        # it is the same string, but without the risk of diverging from it on a retry.
        answer = "".join(b.text for b in (final.content or []) if b.type == "text").strip()
        ask_id = _log_ask(db_path, channel=channel, question=question, top_k=top_k,
                          model=final.model, sources=sources, answer=answer,
                          category_id=category_id, project=project,
                          input_tokens=input_tokens, output_tokens=output_tokens,
                          cache_read_tokens=cache_read_tokens)
        yield _sse("done", {
            "model": final.model,
            "found": len(chunks),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "ask_id": ask_id,
        })
    except Exception as e:
        logger.error("[rag] ask-stream failed: %s", e, exc_info=True)
        yield _sse("error", {"error": "Помилка RAG. Перевірте логи."})
