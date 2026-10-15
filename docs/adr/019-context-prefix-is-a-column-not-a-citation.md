# ADR-019: A chunk's context prefix is a separate column that feeds search, never a citation

- Status: proposed
- Date: 2026-09-17
- Spec: docs/specs/production-rag-wave-b

## Context

From `docs/specs/production-rag-wave-b/plan.md`, `## Goal`:

> The second of four production-RAG waves. Make a chunk self-sufficient for search (a structural
> prefix from DB fields + a summary line of the unit of meaning — into the vector and into BM25, but not into citations) ...

`## Contracts`, C4:

> `chunks.context_prefix TEXT NULL` (v43). `embeddings.build_context_prefix(meta: dict, chunk: dict)
> -> str`: line 1 —
> `[дзвінок|переписка|документ] {назва|чат} · {YYYY-MM-DD} · {спікер|автор} · {напрямок | нитка: label | стор. N / секція}` (missing fields are skipped, no "None"); line 2 —
> `unit_summary_line`, if present. The embedder input = `prefix + "\n" + text` (the C3 style is applied on top);
> `chunks.text` — text only. `chunks_fts` indexes `(context_prefix, text)`, the triggers
> `chunks_ai/ad/au` are rewritten; `retrieval`/`rag`/export read only `text` (as now).

## Options

none recorded - the delta states the chosen shape only.

## Decision

The structural/summary header for a chunk is stored in its own column, `chunks.context_prefix`, fed
to the embedder as `prefix + "\n" + text` and indexed by `chunks_fts` as `(context_prefix, text)`.
Every reader that turns a chunk into something a user or the model sees — `retrieval`, `rag`,
citations, thread export — continues to read `chunks.text` only, exactly as before this column
existed.

## Consequences

Easier: vector and BM25 search can both match on structural fields (record kind, chat/title, date,
speaker/author, directory/thread-label/page) without duplicating that text inside `text` itself, and
the citation/export surface never has to filter anything back out — it simply never sees the prefix.
Accepted debt, not closed by this decision: unqualified `chunks_fts MATCH` queries that count query-
term matches (e.g. `_term_match_counts` in `retrieval.py`) now also count prefix tokens (kind label,
`нитка`, `стор`, date fragments) after re-embed — recorded by review as Minor 11.

## Invariants created

`chunks.text` never contains the structural prefix or the summary line. Any future code path that
reads a chunk for display, citation, or export reads `text`, never `context_prefix`, directly — the
same "retrieval-time-only, never shown" shape as the epoch filter and duplicate-exclusion invariants
recorded in `docs/wiki/rag-context-integrity.md`.

## Revisit when

A future feature needs the model or the user to actually see the structural header (not merely
retrieve on it) — at that point decide whether that calls for a new, explicit field on the citation,
rather than repurposing `context_prefix` for display.
