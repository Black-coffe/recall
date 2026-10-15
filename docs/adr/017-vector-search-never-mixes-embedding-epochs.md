# ADR-017: The vector search branch filters to the current (embedding model, version) pair

- Status: proposed
- Date: 2026-09-17
- Spec: docs/specs/production-rag-wave-b

## Context

From `docs/specs/production-rag-wave-b/plan.md`, `## Assumptions`:

> After story 04 the `retrieval` vector branch takes only record chunks embedded with the current pair
> (model, version): vectors of different epochs are never compared (dim is the same — today that would be
> silent). Incidentally: during a nightly re-embed on the live DB, search degrades to FTS + a growing vector
> share, not to garbage.

And `## Tradeoffs`:

> An epoch filter in the vector branch (04) versus "nothing, since after re-embed it is all the same": without the filter a night of
> re-embed on the live DB = mixed vectors in every answer; the cost is one join in the brute-force
> load.

`## Plan deltas` D1 records an open exception this decision does not close:

> Not cut out, because outside `red: 6`: `review` Major 3 (`_comment_vector_search` without an epoch filter) and
> Major 5 ... both Ask 3 (GREEN in all seats); the Queen's decision — a separate story of wave 3 or
> a record here.

## Options

1. No filter — rely on a re-embed eventually reaching every record, so epochs "even out" on their
   own. Rejected: during a nightly re-embed of the live DB, this would silently dot-product vectors
   from two different embedding spaces of the same dimension — a wrong answer that looks like a
   right one, not a visible failure.
2. Filter the vector branch's join to the *current* `(embedding_model, embedding_version)` pair,
   alongside the existing `deleted_at`/`duplicate_of` filters. Chosen.

## Decision

`app.services.retrieval.search`'s vector branch joins on `embedding_model = ? AND embedding_version
= ?` for the process's currently configured pair. A chunk embedded under a previous pair is invisible
to vector search until it has been re-embedded onto the current one.

## Consequences

Easier: a partial or in-progress re-embed of the live archive degrades gracefully — search shifts
toward FTS plus a growing vector share, never toward cross-epoch noise. Harder: one extra join in
every brute-force vector load. Open exception, not closed by this decision: the comment-vector
branch (`_comment_vector_search`) has no equivalent filter (review Major 3); Plan delta D1 defers
its disposition to "Queen decides: separate Wave 3 story or record here" rather than recording one.
Until that's closed, a compare run with comments enabled risks mixing epochs on that one branch.

## Invariants created

Any vector-search code path that reads chunk embeddings for ranking (not just the primary
`retrieval.search` branch) filters to the currently configured `(embedding_model,
embedding_version)` pair before comparing vectors, unless it explicitly documents why it doesn't
(as `categorize`'s k-NN currently does, by plan: "non-goal" during an in-progress re-embed).

## Revisit when

The comment-vector branch's epoch filter gap is closed (a Wave 3+ story, or an explicit accepted-risk
record), or `categorize`'s k-NN filtering is revisited once a full re-embed makes the corpus
single-epoch again.
