# ADR-014: RAG context is assembled chronologically, with the later-dated source overriding an earlier one on conflict

- Status: proposed
- Date: 2026-09-16
- Spec: docs/specs/production-rag-wave-a

## Context

From `docs/specs/production-rag-wave-a/plan.md`, `## Requirements` quoted in story
`production-rag-wave-a-03`:

> Контекст собирается в хронологическом порядке; в промпте: позднейшая версия ведёт,
> ранняя показывается как «было», комментарий владельца выше обеих.

And the repair-wave delta refining chunk ordering within one record (review #6, story
`production-rag-wave-a-09`):

> Хронологія: «чанки однієї записи — за позицією» історія 03 реалізувала як порядок
> ретривалу — ремонт 09 повертає `chunk_index` (контракт уточнено вище).

`order_citables()` previously ordered non-comment sources by retrieval rank (relevance),
which meant two chunks describing the same agreement at different dates could appear in
either order depending on which one the retriever scored higher - forcing the model to
guess which version was current. The fix, and the system-prompt rule that makes it
actionable, both needed to survive independent of retrieval ranking.

## Options

none recorded - the delta and stories state the chosen shape only.

## Decision

`order_citables()` sorts non-comment sources by `meeting_date` ascending (sources without
a date go last; the sort is stable, so equal-date items keep retrieval order), with
owner comments always first regardless of date. Within one transcription record, chunks
are ordered by `chunk_index` after the date sort, not by retrieval rank. Citation numbers
`[n]` are assigned in this final chronological order. `_RAG_SYSTEM_PROMPT` carries an
explicit rule: fragments are presented in chronological order (owner comments first);
when two fragments describe the same agreement, the later-numbered one among ordinary
fragments leads, the earlier one is referenced as "was," and an owner comment outranks
both regardless of date.

## Consequences

The model's citation numbers `[n]` and the chronological reading order are now the same
thing - a future story that wants to re-introduce relevance-based ordering must either
accept that it changes what `[n]` means to the model, or keep chronology for citation
numbering and add relevance as a separate signal (e.g. a "most relevant" marker) rather
than reordering. The system prompt's date-conflict rule depends on every source type
(meeting/document/telegram) carrying a comparable date field; a new source type without
one falls to the end of the order and gets no special conflict handling.

## Invariants created

`order_citables()` never reorders by retrieval rank for non-comment sources; the only
sort keys are (owner-comment-first, date ascending, chunk_index within one record).
Citation numbers `[n]` always match this chronological order, not retrieval rank. The
system prompt's "later version leads, comment outranks both" rule must stay synchronized
with this ordering - changing one without the other reintroduces the original conflict-
resolution ambiguity.

## Revisit when

A future story needs retrieval relevance to influence which fragments are shown at all
(top-k selection) as opposed to how already-selected fragments are ordered - that is a
different layer (`retrieval.search`'s ranking, explicitly out of scope for story 03) and
should not be conflated with this ordering decision.
