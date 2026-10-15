# ADR-035: A scheduled moment closes only on a terminal outcome; failures retry within a bound

- Status: proposed
- Date: 2026-10-15
- Spec: docs/specs/k4-scheduled-pipes

## Context
Plan delta, 2026-10-15 (fix story `k4-scheduled-pipes-05`), verbatim:

> - 2026-10-15 · After GREEN of round 2, before ship — the owner's correction, fix story `k4-scheduled-pipes-05` (cut by the Queen):
>
  > `«Если в понедельник в 9:00 брифинг не отправится, Recall не попробует ещё раз, и свод за эту неделю пропадёт.»` (If the briefing is not sent on Monday at 9:00, Recall will not try again, and this week's digest will be lost.)
>
  A run with status `error`, a hung `running` and a manual `pipes run` no longer close the schedule moment:
  a retry after `PIPES_RETRY_MINUTES` (15) up to `PIPES_RETRY_MAX` (6) attempts. The first Descoped item (a manual run
  suppresses the catch-up) is closed by this same story. The card `docs/defects/pipe-failure-loses-brief.md` with `check:`.

## Options
1. Any recorded run (including `error`, stuck `running`, or a manual run) closes the scheduled moment - the shape before this delta; a single failure silently loses that period's brief.
2. Only terminal outcomes close the moment; failures retry on a fixed interval up to a cap - the chosen shape.

No other alternatives recorded.

## Decision
Option 2. Deciding factor, from the owner: a failed Monday 9:00 run must not mean the week's digest is lost.

## Consequences
Not recorded beyond the decision itself. (A brief may now arrive late, up to `PIPES_RETRY_MINUTES * PIPES_RETRY_MAX` after its slot; the delta does not discuss this.)

## Invariants created
- A scheduled moment is closed only by a scheduled/catch-up run whose outcome is `ok`, `skipped` or `blocked`.
- `error` and a stuck `running` do not close it; a manual `pipes run` never closes it.
- Retries are bounded: every `PIPES_RETRY_MINUTES` (default 15), at most `PIPES_RETRY_MAX` (default 6) attempts. No unbounded loops, no batch catch-up.
- Regression check lives in `docs/defects/pipe-failure-loses-brief.md`.

## Revisit when
Not recorded.
