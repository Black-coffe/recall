---
domain: rag
tags: [retrieval, rag, dedup, ask-log, telegram]
related: [map/services.md, map/blueprints.md, entity-graph-provenance]
last-verified: 2026-09-16
---

# RAG context: duplicates are excluded, threads are capped, order is chronological, every answer is logged

Four invariants introduced by production-RAG Wave A (16.09.2026, spec
`production-rag-wave-a`), all measured against a live failure before being written:
a single question against a Telegram thread cost 352,936 input tokens because
thread stitching had no character ceiling, and duplicate audio/YouTube uploads
occupied real top-k slots with identical text.

**A duplicate record stays in the archive but never in search.**
`transcriptions.duplicate_of` (migration v40, `app/db/migrations.py`) points a
copy at its original; the record is never deleted and keeps its own comments,
file and tasks (UI shows "дубль #N", `static/js/recall/views/library.js`).
`app.services.dedup_audio.hash_for(source_type, text)` computes the dedup key
only for `file`/`youtube`/`recording` — Telegram and documents have their own
dedup and are deliberately excluded. `find_original(conn, content_hash_value,
exclude_id=None)` is checked at ingest (`app/blueprints/transcription.py`)
before INSERT; a duplicate gets no chunks/embeddings and enrichment status
`skipped_duplicate`. Every consumer of `duplicate_of` must filter it alongside
`deleted_at`, not instead of it:
- `app.services.retrieval.search` — both the vector and FTS branches filter
  `duplicate_of IS NULL`.
- `app.services.enrichment` — `list_unenriched_ids()`, `backfill(--force)` and
  `enrich_transcription()` all skip `duplicate_of IS NOT NULL` with an early
  exit (three separate guards, not one shared query — `<id>/enrich` needs its
  own check).
- `app.services.archive_grep.grep()` and other surfaces outside
  `retrieval.search` do **not** filter duplicates yet — known gap, not an
  oversight.
The offline pass `app.services.dedup_audio.mark_duplicates(db_path,
dry_run=False)` backfills `duplicate_of` for records ingested before this
existed; default writes, `--dry-run` is opt-in on the CLI.

**Telegram thread context has a two-level character ceiling, not a message
count.** `app.services.retrieval.attach_thread_context` reads
`RAG_THREAD_MSG_CHARS` (default 1500, per neighbour) and `RAG_THREAD_CHARS`
(default 8000, whole stitched thread) from the settings registry
(`app/core/settings.py`). The found message (the "hit") always gets a budget
at least as large as any neighbour:
`hit_budget = min(RAG_THREAD_CHARS, max(RAG_THREAD_MSG_CHARS, RAG_THREAD_CHARS // 2))`.
Neighbours are added in order of proximity to the hit (not chronological
order) until the remaining thread budget runs out; a neighbour that has no
room even for the truncation suffix is dropped, and so is everything farther
away in the proximity order (`thread["dropped"]` counts them). Truncated text
carries `truncated: true` and a `…[обрізано, ще {K} симв.]` suffix. The
`messages` array handed to the prompt is always chronological regardless of
the order neighbours were selected in.

**Context order is chronological, and later beats earlier.**
`app.services.rag.order_citables(chunks, attached_comments)` is the single
function that decides both the `[n]` numbering and the `sources` array order
— they must never diverge, because a pinned/correction comment shifts every
downstream number by one if the two lists are built separately (that bug is
why this is one function). Owner comments come first. Everything else sorts
by `meeting_date` ascending (missing date sorts last), with a tie-break of
`(rank of transcription_id's first appearance in retrieval order, chunk_index)`
so that chunks of the same record stay adjacent instead of interleaving in
retrieval order. The system prompt tells the model the later version leads
and the earlier one is context ("was"), with the owner's comment outranking
both.

**Every successful `ask_archive` call, from either channel, is logged once.**
`app.services.rag.answer_question`/`answer_question_stream` take `channel:
str = "ui"` (`ASK_CHANNELS = ("ui", "mcp")`, unknown values fall back to
`"ui"`) and write one row to `ask_log` (migration v41) via `_log_ask()` —
question, channel, scope, k, model, source ids, token counts, cost
(`_ask_cost()` over `app.services.pricing.estimate_cost`, the one source of
model prices — never a second price table in `rag.py`), and the answer text.
**A failed call writes nothing** — a partial/errored row would pollute the
golden-set mining that reads this table (`evals/build_golden.py
--from-ask-log`). The response and the SSE `done` event both carry `ask_id`.
`POST /api/memory/ask/<int:ask_id>/rate` (`app/blueprints/memory.py`) lets the
owner attach a rating (`1`/`-1`) and a note (`rag.rate_ask`); the MCP channel
does not collect a rating — no new write tool was added for it (owner
decision, `mcp-read-first-strategy`), the UI 👍/👎 in `ask.js` is the only rating
surface.

See `memory/map/services.md` (RAG section, Дедуп section) for current line-level
detail and `docs/wiki/entity-graph-provenance.md` for the sibling rule that a
single mention can only ever belong to one graph entity — the same "one truth
owns the slot" shape as duplicate exclusion here.
