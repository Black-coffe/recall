# ADR-017: The vector search branch filters to the current (embedding model, version) pair

- Status: proposed
- Date: 2026-09-17
- Spec: docs/specs/production-rag-wave-b

## Context

From `docs/specs/production-rag-wave-b/plan.md`, `## Assumptions`:

> Векторна гілка `retrieval` після історії 04 бере лише чанки записів, ембеднутих поточною парою
> (модель, версія): вектори різних епох ніколи не порівнюються (dim однаковий — сьогодні це було б
> тихо). Побічно: під час нічного re-embed на бойовій БД пошук деградує до FTS + зростаюча векторна
> частка, а не до сміття.

And `## Tradeoffs`:

> Фільтр епохи у векторній гілці (04) проти «нічого, бо після re-embed усе одно»: без фільтра ніч
> re-embed на бойовій БД = змішані вектори в кожній відповіді; ціна — один join у brute-force
> завантаженні.

`## Plan deltas` D1 records an open exception this decision does not close:

> Не вирізано, бо поза `red: 6`: `review` Major 3 (`_comment_vector_search` без фільтра епохи) і
> Major 5 ... обидва Ask 3 (GREEN у всіх сідалищ); рішення Queen — окрема історія хвилі 3 або
> запис тут.

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
