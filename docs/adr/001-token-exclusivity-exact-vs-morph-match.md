# ADR-001: Exact-match tokens are exclusive to the morph matcher, regardless of alias word count

- Status: proposed
- Date: 2026-08-21
- Spec: docs/specs/entity-graph-tg

## Context

From `docs/specs/entity-graph-tg/plan.md`, `## Plan deltas` (21.08.2026, ремонтний
раунд after `/vulyk-review`, BLOCK critical #1):

> Тригер: `lead-review` довів, що при `TG_ENTITIES_MORPH_ENABLED=1` одна згадка
> зараховується двом сутностям, коли точний збіг довший за одне слово, і що тест
> гарда 1 не може почервоніти.
>
> Кожен токен, спожитий точним збігом, має бути закритий для морфо-гілки незалежно
> від того, зі скількох слів складається назва, що його спожила; тест гарда 1 має
> падати, якщо гард 1 прибрати.

This concretizes the long-standing graph invariant "one writing cannot be credited
to two entities" (`memory/entity-merge-moves-names-to-aliases.md`) for the new morph
matcher introduced in this spec (`tg_entities.py`, `SOURCE_MORPH = "thread_morph"`).
The original guard only excluded single-word exact matches from the morph branch;
multi-word exact matches (e.g. a two-word canonical name) leaked their tokens back
into morph candidacy, letting one mention count twice.

## Options

none recorded - the delta states the chosen shape only.

## Decision

Token consumption by an exact match closes those tokens to the morph branch
unconditionally, independent of how many words the consuming name has. The guard-1
test must fail red if the guard is removed (a red-test requirement, not just a
behavior fix).

## Consequences

Any future extension of the morph matcher (new stemming rules, new alias sources)
inherits a hard exclusivity boundary against exact-match token spans - it cannot
special-case multi-word names to reclaim tokens. Makes the "one writing, one entity"
invariant enforceable by a red test instead of relying on manual review to catch
word-count edge cases.

## Invariants created

A token consumed by an exact match is never eligible for morph-branch matching,
regardless of the word count of the exact-match name that consumed it.

## Revisit when

A future story needs a token to legitimately match two candidate entities (e.g.
genuinely ambiguous short names) - that is a different problem (see the existing
`"ambiguous": true` group handling in `find_alias_cross_type_collisions`) and should
not be solved by loosening this exclusivity.
