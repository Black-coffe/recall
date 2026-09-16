# ADR-011: Thread-context hit gets a budget floor, not the neighbours' leftover

- Status: proposed
- Date: 2026-09-16
- Spec: docs/specs/production-rag-wave-a

## Context

From `docs/specs/production-rag-wave-a/plan.md`, `## Plan deltas` (2026-09-16, repair
wave 4 after round-1 review, items #1+#2):

> Бюджет хіта в нитці: `H = min(RAG_THREAD_CHARS, max(RAG_THREAD_MSG_CHARS,
> RAG_THREAD_CHARS // 2))` — за дефолтів 4 000 проти 1 500 у сусіда, тобто «більший»
> буквально. Відкинута альтернатива «хіт = max(MSG_CHARS, залишок після сусідів)»: за
> дефолтів дає хіту рівно 1 500 = сусід, а бриф каже «больший», не «не менший». Сусіди,
> що не влазять у залишок стелі нитки, відкидаються (найближчі до хіта — першими), а не
> стискаються до маркера.

Story `production-rag-wave-a-01` gave the found (hit) message the *leftover* of
`RAG_THREAD_CHARS` after all neighbours were cut to `RAG_THREAD_MSG_CHARS` first. Round-1
review (#1, #2) showed this violates the brief's own requirement ("найденное сообщение
получает больший лимит"): at defaults the hit could shrink to a bare truncation marker,
and with a wider `RAG_THREAD_STITCH` the total window could exceed `RAG_THREAD_CHARS`
altogether. Story `production-rag-wave-a-06` fixed the allocation order.

## Options

1. Hit = leftover after neighbours are cut to `RAG_THREAD_MSG_CHARS` (original story 01
   shape) - simplest to implement, but the hit can end up smaller than a neighbour, and
   the total can overrun `RAG_THREAD_CHARS` when `RAG_THREAD_STITCH` is large.
2. Hit = `max(MSG_CHARS, leftover after neighbours)` - rejected: at default settings this
   gives the hit exactly `RAG_THREAD_MSG_CHARS`, tying it with a neighbour instead of
   exceeding it, which does not satisfy "the hit gets a *bigger* limit."
3. Hit = `min(THREAD_CHARS, max(MSG_CHARS, THREAD_CHARS // 2))`, cut first; neighbours take
   the remainder in nearest-first order, each capped at `min(MSG_CHARS, remaining)`; a
   neighbour with no room left for content beyond the truncation suffix is dropped
   entirely, and dropped neighbours are counted, not silently disappeared. Chosen.

## Decision

The hit is allocated its budget `H = min(RAG_THREAD_CHARS, max(RAG_THREAD_MSG_CHARS,
RAG_THREAD_CHARS // 2))` and truncated *first*. Neighbours are then filled into the
remaining budget in order of proximity to the hit (alternating sides), each capped at
`min(RAG_THREAD_MSG_CHARS, remaining_budget)`. The first neighbour for which the
remaining budget cannot fit anything but the truncation suffix, and every neighbour
after it in that proximity order, is dropped from `messages` and counted in
`thread["dropped"]`. `messages` itself stays chronologically ordered (the drop decision
is made in proximity order; the resulting list is filtered, not reordered).

## Consequences

The hit is now guaranteed to be at least as large as any neighbour in the same window,
and `chars_kept == sum(len(m.text) for m in messages) <= RAG_THREAD_CHARS` holds for any
combination of `RAG_THREAD_STITCH` / `RAG_THREAD_MSG_CHARS` / `RAG_THREAD_CHARS`. The
tradeoff is that a wide `RAG_THREAD_STITCH` on a chatty thread can silently drop distant
neighbours entirely rather than showing them compressed - visible only via
`thread["dropped"]`, which any future consumer of the `thread` payload must surface if it
wants the operator to know context was cut.

## Invariants created

For any thread window: `len(hit.text) >= max(len(n.text) for n in neighbours)`, and
`chars_kept <= RAG_THREAD_CHARS` regardless of `RAG_THREAD_STITCH`. Neighbours are
dropped whole (never compressed to a bare marker) once the remaining budget cannot hold
real content.

## Revisit when

A future story wants to show dropped neighbours as compressed stubs instead of omitting
them, or wants per-neighbour budget weighting (e.g. by relevance instead of proximity to
the hit) - both require reopening the allocation order decided here.
