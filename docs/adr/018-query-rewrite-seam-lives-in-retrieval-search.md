# ADR-018: The query-rewrite flag lives in `retrieval.search`, not in `rag.py`

- Status: proposed
- Date: 2026-09-17
- Spec: docs/specs/production-rag-wave-b

## Context

From `docs/specs/production-rag-wave-b/plan.md`, `## Goal`:

> ... і покласти шов переписування запиту у `retrieval.search` за прапорцем ...

`## Contracts`, C7:

> `query_rewrite.rewrite_query(question, *, max_variants=3, model=None) -> list[str]` — 1–3
> формулювання без оригіналу; `[]` на будь-який збій. `retrieval.search(..., rewrite: bool | None =
> None)`: `None` → `RAG_QUERY_REWRITE` (реєстр, дефолт `0`, гаряче читання); варіанти йдуть тими ж
> vector+FTS гілками й зливаються наявним RRF до recency/rerank/cap; `explain=True` додає
> `why["rewrites"]`. `rag.py` передає `rewrite=` явно з env, як `_RERANK_ENABLED`.
> `RAG_QUERY_REWRITE_MODEL` (реєстр, дефолт Haiku 4.5).

And the post-ship operating procedure:

> Переписування запиту: `RAG_QUERY_REWRITE=1` для окремого прогону гейта на тому ж знімку →
> `compare` з підписами; вмикати в `.env` лише після цифри.

## Options

none recorded - the delta states the chosen shape only.

## Decision

The rewrite flag and its execution (fan-out into variant vector+FTS subqueries, RRF fusion) live
inside `retrieval.search(..., rewrite: bool | None = None)`, reading `RAG_QUERY_REWRITE` from the
settings registry when not passed explicitly. `rag.py` reads and forwards the same registry key
explicitly, mirroring the existing `_RERANK_ENABLED` pattern rather than holding its own copy.

## Consequences

Easier: the retrieval-only eval gate can turn rewriting on for a measurement run by calling
`retrieval.search` (or setting the env flag) directly, without going through `rag.py` or a full,
Claude-synthesized answer — the post-ship procedure runs exactly this ("окремий прогін гейта").
Harder: any future retrieval-level query transformation must be wired the same way (a registry-backed
flag read at the `retrieval.search` layer) to stay measurable the same way, rather than being added
inside `rag.py` where the gate cannot reach it; the `why["stages"]` merge-by-label behaviour and the
`comment_*`-label-based fallback exist specifically to keep rewrite variants indistinguishable from
the original at the `vector`/`fts` label level, so they don't have to renegotiate the `by`/`matched_by`
contract (S2 C2).

## Invariants created

A retrieval-time query transformation (rewrite, and any future one of the same shape) is added as an
optional stage inside `retrieval.search`'s existing vector/FTS branches, behind a registry-backed
flag — not inside `rag.py` — so the retrieval-only eval gate can exercise it without invoking
Claude-based answer synthesis or incurring per-answer cost.

## Revisit when

Wave C's descoped "scope extraction from the question" or a dual-mode context redesign changes what
`rag.py` hands to `retrieval.search` — at that point re-check whether the rewrite seam still belongs
at this layer.
