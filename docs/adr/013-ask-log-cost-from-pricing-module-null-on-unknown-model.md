# ADR-013: `ask_log` costs read from `pricing.py`, not a second tariff table, and record NULL for an unknown model instead of a default price

- Status: proposed
- Date: 2026-09-16
- Spec: docs/specs/production-rag-wave-a

## Context

From `docs/specs/production-rag-wave-a/plan.md`, `## Contracts` (ask_log):

> Вартість у `ask_log` рахується за таблицею цін у коді (Opus 5 $5/$25, Sonnet 5 $2/$10,
> Haiku 4.5 $1/$5 за MTok, кеш-читання 0.1× input; F8 гриля); невідома модель → NULL.

And from the repair-wave delta after round-1 review (review #9):

> Тариф Sonnet 5 (review #9): вартість у `ask_log` рахує `app/services/pricing.py`
> ($3/$15 за MTok), не таблиця $2/$10 з Assumptions; таблицю в `rag.py` не дублювали
> свідомо (T6.2).

And from story `production-rag-wave-a-04` (`## Implementation notes`):

> Ціни не дублював у `rag.py` (відхилення від букви контракту): взяв
> `app/services/pricing.py` — він існує саме тому, що дубльована таблиця тарифів колись
> рахувала за ціною чужої моделі (T6.2). `_ask_cost` додає лише контрактну поведінку
> «модель поза таблицею → NULL» (замість фолбеку pricing на дефолтний тариф).

`app/services/pricing.py:estimate_cost` itself falls back to `DEFAULT_PRICE_MODEL`'s price
for any model not in `MODEL_PRICES` - a defensible choice for its original callers, who
want *some* number rather than none. `ask_log` is different: its cost column feeds
golden-set curation and cost-tracking decisions, where a silently wrong price (charged at
the wrong model's rate) is worse than a visibly missing one.

## Options

1. Duplicate a tariff table inside `rag.py` for `ask_log`, as the plan's `## Contracts`
   literally specified. Rejected once discovered: this is the exact failure mode
   `pricing.py` was created to close (T6.2 - two tariff tables drift, one goes stale and
   silently prices a model at another model's rate).
2. Call `pricing.estimate_cost()` as-is and accept its default-price fallback for unknown
   models. Rejected: an ask_log row with a plausible-looking but wrong cost is
   indistinguishable from a correct one downstream, and the plan's acceptance bar was
   "unknown model -> NULL," not "unknown model -> best guess."
3. Call `pricing.estimate_cost()`'s price table but wrap it so an unknown model yields
   `cost_usd = NULL` explicitly in `ask_log`, rather than reusing the module's own
   fallback path. Chosen.

## Decision

`rag.py`'s `_ask_cost` reads prices from `app/services/pricing.py` (single source of
truth) and does not duplicate a tariff table. It overrides only the fallback behavior:
a model absent from `pricing.MODEL_PRICES` produces `cost_usd = NULL` in the `ask_log`
row, instead of `pricing.estimate_cost()`'s own behavior of pricing it at
`DEFAULT_PRICE_MODEL`'s rate. The plan's literal price figures ($2/$10 for Sonnet 5) are
superseded by whatever `pricing.py` currently holds ($3/$15 at time of writing) - `pricing.py`
is the number that moves when Anthropic's pricing changes, not the plan.

## Consequences

`ask_log.cost_usd` is either a real, correctly-priced number or `NULL` - never a plausible
number for the wrong model. This means golden-set tooling and any future cost dashboard
built on `ask_log` must handle `NULL` explicitly rather than treating a `0` or a stale
default as "cheap." The cost of a `pricing.py` update (a new model added) applies to
`ask_log` automatically; the cost of a model *removed* or renamed without updating
`ask_log`'s NULL-handling would silently start marking previously-priced rows as unknown.

## Invariants created

`ask_log` cost accounting never reads or maintains its own price table - it calls into
`app/services/pricing.py` and only adds the NULL-on-unknown-model behavior on top. Any
future consumer of Claude API pricing anywhere in the codebase must do the same (this is
the pre-existing T6.2 invariant; this ADR records that `ask_log` was built to respect it
even though the plan's literal contract text would have violated it).

## Revisit when

The unresolved `pricing.py` TODO (Sonnet 5's $2/$10 intro price through 2026-08-31, not
currently modeled) is investigated - if a date-dependent pricing branch is ever added to
`pricing.py`, `ask_log`'s historical rows computed under the flat $3/$15 rate will need a
documented cutover, not a silent recompute.
