# ADR-005: Bounded scans over the archive cap REAL work and honestly report the arbitrariness of the slice

- Status: proposed
- Date: 2026-09-04
- Spec: docs/specs/grep-explainability

## Context

From `## Plan deltas` (D1) of the `grep-explainability` spec:

> **`max_scan` must bound the work, not the output (findings 4, 5).** Sorting by a
> non-indexed expression forces SQLite to run the filter over the whole join up to `LIMIT`, so in
> regex mode the pattern runs on every row of the archive regardless of `max_scan`.
> **Decision:** a cursor without `ORDER BY`, stopping after `max_scan` rows examined, sorting the already
> collected matches in Python. Plus a wall-clock deadline, checked between rows: a model-supplied regex
> with catastrophic backtracking has no right to hang the connector's stdio process. **The limit of this
> protection, named honestly:** the check sits BETWEEN rows, so one row with unbounded
> backtracking can still exceed the deadline; chunks are small, so in practice this is bounded.
> Rejected: an index for sorting (a write cost on a hot table for the sake of a diagnostic tool).

And (D2), the same mechanism, a consequence for the semantics of the slice:

> **A truncated scan must say that it is truncated ARBITRARILY (new finding 3).** By removing `ORDER BY`,
> we changed the meaning of truncation: previously the newest `max_scan` rows were examined, now it is an arbitrary
> prefix. **Decision:** document this in the docstring and in the response; do NOT bring `ORDER BY` back (it
> was exactly the reason the ceiling bounded nothing).

## Options

1. `ORDER BY` + SQL `LIMIT` to bound the scan ceiling — simple, but a non-indexed `ORDER BY`
   forces the engine to run the predicate over the whole join BEFORE `LIMIT` is applied: the ceiling itself bounds
   nothing when the predicate is expensive (regex). Rejected by the plan.
2. An index for sorting — removes the non-indexed `ORDER BY`, but costs a write on a hot
   table for the sake of a diagnostic tool. Rejected by the plan.
3. A cursor without `ORDER BY`, stopping after `max_scan` rows actually examined + a wall-clock
   deadline checked between rows; sorting of the already collected matches — in Python. Chosen.

## Decision

Any future tool that runs an expensive predicate (regex or equivalent) over a scan
of `chunks`/`transcriptions` bounds the work ceiling by the number of rows ACTUALLY viewed and by a
wall-clock deadline checked between rows — and does not rely on SQL `LIMIT` after a
non-indexed `ORDER BY`. When the ceiling really truncates the result, the tool's response honestly
marks that the truncated subset is an ARBITRARY prefix of the scan, not "the newest N" or any other
meaningful order; sorting is suitable only for already collected (post-filter) results.

## Consequences

Easier: the scan ceiling really bounds work regardless of the predicate cost, and there is no temptation to
add an index for the sake of a one-off diagnostic tool. Harder: a truncated result cannot be read as
"the newest/most relevant N" — the consumer (human or model) must explicitly see a warning
about arbitrariness and, if needed, narrow the filter (date/source/id) instead of raising `limit`.
An accepted debt, named honestly: the deadline check between rows does not save from one row with
catastrophic backtracking inside the predicate itself — on small chunks this is considered acceptable.

## Invariants created

A scan that applies an expensive predicate to a large table of this archive stops on the counter of
rows ACTUALLY examined and on a wall-clock deadline checked between rows — not on SQL `LIMIT`
after a non-indexed `ORDER BY`. Any response with a truncated result of such a scan calls the
slice arbitrary (not ordered by relevance or recency) rather than staying silent about it.

## Revisit when

Such a scan grows from a diagnostic tool into a path that runs often / on the product's hot path
— then the cost of an index for sorting (rejected here for the sake of a write on a hot table)
is worth recounting against the real call frequency.
