---
domain: rag
tags: [retrieval, rag, dedup, ask-log, telegram, embeddings, query-rewrite]
related: [map/services.md, map/blueprints.md, entity-graph-provenance]
last-verified: 2026-09-21
---

# RAG context: duplicates are excluded, threads are capped, order is chronological, every answer is logged

Four invariants introduced by production-RAG Wave A (16.09.2026, spec
`production-rag-wave-a`), all measured against a live failure before being written:
a single question against a Telegram thread cost 352,936 input tokens because
thread stitching had no character ceiling, and duplicate audio/YouTube uploads
occupied real top-k slots with identical text. Wave B (17.09.2026, spec
`production-rag-wave-b`, shipped 4.4.0 at `247ba0b`) adds three more, all below.

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

**The chunk's context prefix goes into the vector and into BM25, never into a
citation.** `app.services.embeddings.build_context_prefix(meta, chunk)` builds a header of
up to three lines from database fields alone: line 1 — type, title/chat, date,
speaker/author, and a directory/thread-label/page tail; then an optional
`опис: …` line carrying the owner's own `transcriptions.description` (flattened
— newlines become spaces — and cut at `_PREFIX_DESCRIPTION_MAX` = 300
characters, so a long description cannot crowd the chunk's own text out of the
embedder's window); then `app.services.summaries.unit_summary_line()` if one
exists. The embedder's
input is `prefix + "\n" + text` (the model-family style from `_style_for` layers
on top of that); `chunks.text` stays text-only. Migration v43 adds
`chunks.context_prefix` and rebuilds `chunks_fts` to index `(context_prefix,
text)` instead of `text` alone, so BM25 can match on the structural fields too.
Every reader that turns a chunk into something the user or the model sees —
`retrieval`/`rag`/citations/thread export — still reads only `text`; the prefix
is a retrieval-time addition, never shown. `summaries.unit_summary_line` is
SQL-only (no torch/anthropic at import time), so `embeddings.py` can call it
from the stdio MCP process.

**The vector branch never mixes embedding epochs.** A chunk's row carries the
`(embedding_model, embedding_version)` pair it was embedded with
(`transcriptions.embedding_model`/`embedding_version`, read from
`app.services.embeddings.EMBED_MODEL`/`EMBED_VERSION` — both now env-configured,
defaults unchanged at `intfloat/multilingual-e5-large`/`2`).
`app.services.retrieval.search`'s vector branch filters
`embedding_model = ? AND embedding_version = ?` against the *current* pair,
alongside `deleted_at`/`duplicate_of`. Without this, a nightly re-embed of the
live archive would silently compare cosine distances across two different
embedding spaces of the same dimension — a wrong answer that looks like a
right one. The side effect: mid-re-embed, search degrades toward FTS plus a
growing vector share, never toward garbage.

**Re-embedding onto a new (model, version) pair happens only on a snapshot,
never on the live database by default.** `app.services.reembed.run(db_path,
limit=, dry_run=)` re-runs `chunk_and_embed_transcription` for every record
whose pair doesn't match the process's current `EMBED_MODEL`/`EMBED_VERSION`,
then calls `enrichment.optimize_chunk_index()` exactly once at the end (only if
something was actually rewritten) — the same FTS-fragmentation fix Wave A's
bulk passes depend on. The point re-embed after an owner edits a title/description
(`record_meta.after_meta_update` → `reembed.schedule_record_reembed`) takes its
`db_path` as a **required** keyword straight from the request that made the
edit (`current_app.config["DATABASE"]`) — there is deliberately no "`None` =
live database" default, because that default let a test-suite PATCH against a
temporary DB rewrite the chunks of live records with the same ids.
`reembed.is_live_db()` refuses to touch a path that
resolves to `Config.DATABASE` unless `--yes-live` is passed; the intended flow
is snapshot A (current pair) → snapshot B (candidate pair) → `evals.gate` on
both → `evals.compare` → a human decision, before the live `.env` or live DB
are touched at all.

**The query-rewrite flag lives in `retrieval.search`, not in `rag.py`, so the
eval gate can measure it directly.** `app.services.query_rewrite.rewrite_query`
asks Claude (default Haiku 4.5, `RAG_QUERY_REWRITE_MODEL`) for 1-3 alternative
phrasings of the same question; best-effort by construction — any API/JSON
failure returns `[]` and a warning, never an exception. `search(...,
rewrite: bool | None = None)`: `None` reads the hot env `RAG_QUERY_REWRITE`
(settings registry, default off) so a running process picks up a flip without
restart; `rag.py` also reads the same registry key explicitly rather than
hold its own copy (both readers go through `settings.env_bool`, ADR-008 — two
independent parsers of the same flag was a Wave B council finding, fixed
before ship). When enabled, each variant adds its own vector+FTS subquery
under the *same* `vector`/`fts` labels as the original (the `by`/`matched_by`
contract from grep-explainability §S2 C2 does not gain a new label for
rewritten variants) — RRF fuses all of them before recency/rerank/cap run,
unaware anything was rewritten. `explain=True` adds `why["rewrites"]`
(the variant strings actually tried); `why["stages"]` for a label that
appears in more than one input list (original + variant) keeps the entry
with that label's best (lowest) position, it does not get overwritten by
whichever list was merged last. The old comment index absent on an
un-migrated DB is handled by dropping input lists by their `comment_*` label,
not by slicing the list positionally — a positional slice would also drop
rewrite-variant lists that got appended earlier in the same call.

See `memory/map/services.md` (RAG section, Дедуп section) for current line-level
detail and `docs/wiki/entity-graph-provenance.md` for the sibling rule that a
single mention can only ever belong to one graph entity — the same "one truth
owns the slot" shape as duplicate exclusion here.
