# ADR-004: Case folding for text comparison happens only in Python, never in SQLite `lower()`

- Status: proposed
- Date: 2026-09-04
- Spec: docs/specs/grep-explainability

## Context

From `## Plan deltas` (D1) of the `grep-explainability` spec:

> **Cyrillic in `ignore_case` (finding 1, critical).** Story 01 dictated the SQL
> `instr(lower(text), lower(?))` under the heading "this is not a free choice" — and that very line is the defect:
> SQLite `lower()` folds only ASCII, so `grep('андрій')` returns 0 on a chunk with the word `Андрій`, while
> regex mode finds it with the same query. A project note about this existed before planning
> (`SQLite LOWER не сворачивает кириллицу`) and was ignored by the plan.
> **Decision:** both modes fold case in Python (`str.casefold`), not in SQL. The literal and
> regex paths must mean the same thing under one flag.
> Rejected: `lower(x) COLLATE NOCASE` (the same ASCII boundary) and the ICU extension (not in the build).

This is not the first time this same defect occurs: the same trap
(`SQLite LOWER не сворачивает кириллицу`) had already been found and recorded in the project memory while working on entity merging
(`memory/entity-merge-moves-names-to-aliases.md`) and still got into the plan of this spec again.
The archive is predominantly Cyrillic (calls, Telegram in Ukrainian/Russian) — any future case-insensitive
text comparison will step on the same boundary unless the decision is recorded as an invariant,
rather than as a memory that has to be recalled anew each time.

## Options

1. `SQL lower()` / `LOWER(x) COLLATE NOCASE` — the simplest in SQL, but folds only ASCII;
   silently treats Cyrillic as case-sensitive. Rejected by the plan.
2. The SQLite ICU extension for Unicode-aware case-insensitive `lower()` — a correct solution,
   but the extension is absent from the project's current SQLite build. Rejected by the plan.
3. Case folding in Python (`str.casefold()`) before or during comparison, never in an SQL expression —
   costs reading a wider set of rows into Python code, but is correct for Cyrillic and Latin
   alike and does not depend on the SQLite build. Chosen.

## Decision

Any case-insensitive text comparison over this archive (search, entity deduplication,
future tools) folds case in Python (`str.casefold`) and does not rely on SQL `lower()`/
`LOWER(...) COLLATE NOCASE`. The literal and regex modes of one tool must give the
same result under one case-insensitivity flag.

## Consequences

Easier: code that compares text case-insensitively is correct at once on a Cyrillic and Latin
corpus without additional dependencies (ICU) in the build. Harder: comparisons that previously ran
entirely in SQL (and could potentially use an index/sorting on the engine side) now require
reading a wider set of rows into Python before filtering by case — a cost that ADR-005 directly
addresses for scanning tools (work is bounded by the number of rows viewed and a deadline, rather than
relying on SQL `LIMIT`).

## Invariants created

No new code that compares text case-insensitively over this archive calls SQL
`lower()`/`LOWER(...) COLLATE NOCASE` as a case-folding mechanism. Folding is done exclusively
by `str.casefold()` in Python code, applied identically to all modes of one tool.

## Revisit when

The project moves to a DBMS or SQLite extension with correct Unicode case folding out of the box
(e.g. the ICU extension enabled in the build) — then the choice can be reconsidered in light of the real
cost on the hot path.
