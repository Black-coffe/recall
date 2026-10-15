# services — retrieval, RAG chat, grep

last-verified: 2026-10-15

Part of `app/services/*`; hub — [services.md](services.md). Env registry — `app/core/settings.py`.

- **retrieval.py** — `search(db_path, query, top_k=8, ..., rewrite=None, ...)` (`:712`): hybrid vector (brute-force numpy
  cosine, everything in RAM; >100k chunks → sqlite-vec needed) + FTS5 BM25 via RRF. `scope_tids=` narrows to a list of records
  (the list is computed by `scope.scope_filter_ids`; retrieval knows nothing about the graph). Recency boost (`RAG_RECENCY_HALF_LIFE_DAYS`=180),
  per-meeting cap (`RAG_MAX_PER_MEETING`=3), optional `rerank=` (a cross-encoder over the pool `RAG_RERANK_POOL_SIZE`=24 BEFORE the
  diversity cap; OFF).
  - Filters: both branches `deleted_at IS NULL AND duplicate_of IS NULL`; the vector one — ONLY the current epoch
    (`embedding_model = ? AND embedding_version = ?`).
  - ⚠️ `_fts_search` (`:290`): the record/scope filter next to `chunks_fts MATCH` — ONLY `+rowid IN (…)`
    (`retrieval.py:309,315`), never a bare `rowid IN`: SQLite hands it to FTS5 as a vtab constraint and MATCH is run for
    every id (13–57 s → ≤0.12 s). The same in `research.py:134`. Do not "simplify" it back. `_term_match_counts` (`:185`)
    keeps a bare `rowid IN` (the set is ≤`candidate_k`).
  - Rewrite (C7): `rewrite=None` reads `RAG_QUERY_REWRITE` (default 0); `True` adds vector+FTS subqueries of the variants under
    the SAME labels `vector`/`fts`, merged by the same RRF. The fallback without a comments index (a DB without v37) drops only the
    `comment_*` lists by label.
  - Explain (S2): `explain=True` → `why` on every result (`WHY_REQUIRED_KEYS=src,rrf,rec,by,top`,
    `build_placeholder_why()`), + `stages/weights/final_raw/search_capped`; `why["stages"]` keeps the smallest position per label.
    Does not change the ranking.
  - `attach_thread_context(db_path, chunks, max_msgs=6)` (`:1094`): a character ceiling per TG thread — `RAG_THREAD_MSG_CHARS`=1500
    (neighbours), `RAG_THREAD_CHARS`=8000 (the whole thread), `RAG_THREAD_STITCH`=6. `hit_budget = min(thread_limit,
    max(msg_limit, thread_limit//2))`; neighbours in order of proximity, each ≤ `min(msg_limit, remaining)`; those that did not fit are
    dropped into `thread["dropped"]`. A truncated one carries `truncated: true` and the suffix `…[обрізано, ще {K} симв.]`; `messages` are
    chronological, `thread` carries `chars_total/chars_kept/dropped`.
- **query_rewrite.py** (C7) — `rewrite_query(question, max_variants=3, model=None) -> list[str]`: 1-3 variants from Claude
  (`RAG_QUERY_REWRITE_MODEL`, default Haiku 4.5); `[]` on an empty/single-word query or any failure (best-effort).
- **archive_grep.py** (S2) — `grep(db_path, pattern, regex=, ignore_case=, context=, limit=, source_type=,
  transcription_id=, days=, max_scan=, deadline_seconds=)` (`:169`): a literal/regex search for an EXACT STRING over `chunks` with
  ±context neighbours, without embeddings and ranking; output `meeting_date DESC, chunk_index`. *Casefold only in Python
  (SQLite `lower()` does not fold Cyrillic); no `ORDER BY` in SQL; `max_scan` counts the rows viewed; the deadline (default 5 s)
  is checked BETWEEN rows. Searches `chunks`, not `transcript_text` — a string on a chunk seam will not be found
  (`chunk_boundary_caveat`). Does not import torch/numpy/retrieval — loaded into the stdio-MCP. MCP: `grep_archive`.*
- **reranker.py** (T6.4) — `rerank()`: cross-encoder `BAAI/bge-reranker-v2-m3`; lazy singleton; OFF
  (`RECALL_RERANK_ENABLED`), only for the RAG chat from `rag.py`; failure → the original order.
- **rag.py** — "Ask Archive": `answer_question()` (`:333`)/`answer_question_stream()` (`:491`, `channel="ui"`). The answer comes
  ONLY from chunks + citations [n]. The default k is `_DEFAULT_TOP_K=12` (`RAG_TOP_K`, `:50`; a single point for `/api/memory/ask*` and MCP
  `ask_archive`; recall@8 67.9% vs @12 82.1%). `project=` narrows via `_resolve_project`/`scope`; an unknown name does NOT
  zero out the search. Prompt caching; model `models.get_default_model()`; rerank via `retrieval.search(rerank=_RERANK_ENABLED)`.
  - `order_citables(chunks, attached_comments)` (`:126`) — the single numbering [n] and `sources`: the owner's comments first, the rest
    by `meeting_date` ascending; tie-break `(rank of the first appearance of the tid in retrieval, chunk_index)`.
  - `ASK_CHANNELS=("ui","mcp")` (unknown → `ui`); both are logged to `ask_log` (v41) via `_log_ask`; cost —
    `_ask_cost` on top of `pricing.estimate_cost`. Only successful answers are written. `rate_ask(db_path, ask_id, rating,
    note=None)` → `False` if there is no row. The answer and the SSE `done` carry `ask_id`.
  - `_mcp_screen_filter` (`:320`) — a privacy filter for screen chunks only for `channel='mcp'`
    ([services-video-privacy.md](services-video-privacy.md)).
- **categorize.py** — `suggest_category()`: k-NN over neighbour embeddings. *Min 2 labelled categories, dim must match (1024).*
