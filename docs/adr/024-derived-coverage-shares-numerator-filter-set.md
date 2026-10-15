# ADR-024: A derived coverage total must share the exact filter predicate of the list it is reported beside

- Status: proposed
- Date: 2026-09-22
- Spec: docs/specs/directions-projects-tags

## Context

The round 3 review (`directions-projects-tags`, critical 1) found that `commitments_coverage`
could compute the denominator over one set of records while the `items`/`window_matched` of the same
response went over another, narrowed by `category_id`/`tags`/`since`/`until`. The Plan deltas of wave 5
record the decision as follows:

> **Decision 1 — the `coverage` denominator (critical 1):** "the same set on all axes" was chosen, not
> "the docstring names what is ignored": the memory note `derived-claims-need-second-source` exists precisely
> because of figures placed next to a list that come from a different set; the docstring already promises "the denominator under the same
> filters". C6 is rewritten: `commitments_coverage(tags, since, until)` + one helper
> `_scope_clauses` for both readers (closes minor 11 without a separate test of the `" AND "` contract).
> Story 12 narrowed this in its notes ("if coverage accepts…" was conditional) — the plan did not
> record the delta then; it is recorded now.

and in the wave 5 summary:

> **Contracts changed (for 16/17/18/19):** … C6 — the denominator over all axes, a shared helper.

This spec (`commitments.py`, `_scope_clauses`) closes one reader. The bug class — "a derived
number (total/coverage/count) shown next to a list, computed over a different set of filters
than the list itself" — has already happened three times before this spec under other names (the memory note
`derived-claims-need-second-source`: the graph covers 26% of TG, the trust coefficient was computed not over
the same sample as the list in the response). The next reader that shows a total/coverage next to a
list (a new dashboard, a new MCP tool) may repeat the same defect in a file where there is no shared
`commitments.py` helper.

## Options

1. **Fix — the same helper that builds the SQL for the list also builds it for the denominator** (chosen).
2. Document the discrepancy in the docstring ("the denominator ignores these filters") — rejected: the docstring
   already promised the opposite, and a number next to a narrowed list still looks like a reference figure.

The delta names no other options.

## Decision

Any derived metric of the "total"/"coverage"/"window_matched"/"count" kind that an API or an MCP tool
shows next to a list of records must be computed over the **same** set of filters
(`category_id`, `tags`, `since`, `until`, …) as the list itself — through one shared
SQL-fragment builder per domain (in `commitments.py` this is `_scope_clauses`), not through a separate
subquery that assembles its own set of conditions. Duplicating the filter conditions for the "number" and for the "list" in
one endpoint is forbidden.

## Consequences

Easier: when the bug class recurs (a list + a number next to it), the fix is "use the same builder",
not a manual SQL audit. Harder: not recorded — the delta does not name what this costs today's code
outside `commitments.py`.

## Invariants created

- A count/coverage/total figure surfaced next to a filtered list is computed from the identical
  filter predicate as that list, via one shared SQL-fragment builder per domain — never a
  hand-rolled second filter.

## Revisit when

Not recorded — the delta does not name a specific trigger for revisiting.
