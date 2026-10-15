# ADR-006: The shared shape of required keys has exactly one source of truth in code

- Status: proposed
- Date: 2026-09-04
- Spec: docs/specs/grep-explainability

## Context

From `## Plan deltas` (D2) of the `grep-explainability` spec:

> **Contract C2 is amended (new finding 2).** The marker for an element that did NOT pass through
> `retrieval.search` (an attached comment) was invented by the worker and has no `rrf`/`rec` keys —
> so a consumer doing `why["rrf"]` still gets the `KeyError` that was being cured.
> **Decision:** `why` on such an element carries the SAME required set of keys as on a found one
> (`src`, `rrf`, `rec`, `by`, `top`), with zero contributions... Reason: a key that is sometimes
> there and sometimes not is the same `KeyError` with a delay.

And (D3), when the same defect class came back a second time after the repair:

> **The set of required keys has exactly one source of truth in code.** Any producer of `why`
> (there are two: `retrieval._build_why` and the marker in `rag.order_citables`) takes it from there rather than rewriting it
> as a literal. Reason: the divergence of two literals was exactly NEW-2 and NEW-7. Fixing the consequence
> a third time would cost as much as removing the cause once.

## Options

None recorded - the plan fixes the chosen shape of the decision, not the alternatives that were weighed.

## Decision

When one and the same structure with a fixed set of required keys is produced by more than one
place in the code (here — `retrieval._build_why` and the marker in `rag.order_citables` for `why`), the set of
required keys is defined exactly once, in code (not in a docstring and not in a comment), and every
producer takes it from there. No producer rewrites the key list with its own literal. Elements
for which part of the data is not applicable (for example, an attached comment that did not pass through
`retrieval.search`) fill the same keys with zero/empty values — they do not omit them.

## Consequences

Easier: divergence between producers becomes structurally impossible — editing the set of keys
happens in one place, and a second producer cannot "forget" to synchronize. Harder: before the
first appearance of a second producer of such a shape, the shared definition has to be deliberately extracted (rather than
duplicating a literal "because it is simpler right now") — a small up-front discipline instead of
cheaper but brittle duplication.

## Invariants created

Any response structure with a fixed set of required keys that is produced by more than one
function/module has exactly one definition of that set in code; no producer duplicates it
as a literal. An element for which part of the computations is not applicable fills the same set of keys
with neutral values rather than omitting the keys.

## Revisit when

The same structure (`why`) gets a third producer or a field that does not fit the model
"neutral default value" — then it is worth reviewing whether the shared definition still
covers all variants without special cases in each producer.
