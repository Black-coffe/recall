# ADR-036: Every undelivered brief leaves a data-free trace in the delivery channel

- Status: proposed
- Date: 2026-10-15
- Spec: docs/specs/k4-scheduled-pipes

## Context
Plan delta, 2026-10-15 (fix story `k4-scheduled-pipes-06`), verbatim:

> - 2026-10-15 · After GREEN of round 3 — the owner's second correction, fix story `k4-scheduled-pipes-06` (cut by the Queen):
>
  > `«Если фильтр приватного остановит брифинг, бот просто промолчит: не будет ни свода, ни сообщения об ошибке.»` (If the privacy filter stops the briefing, the bot will simply stay silent: there will be neither a digest nor an error message.)
>
  The story 02 criterion "`blocked` → deliver not called" is changed: the briefing does not go out, but the bot gets the message
  `«не надіслано: фільтр приватного — <клас причини>»` ("not sent: privacy filter — <reason class>"), without data and without the stop-word. The second Descoped item
  (`PrivacyError` without a message) is closed. The card `pipe-failure-loses-brief` is extended to this form.

Plan delta, 2026-10-15 (fix story `k4-scheduled-pipes-07`), verbatim:

> - 2026-10-15 · After GREEN of round 4 — the owner's third correction, fix story `k4-scheduled-pipes-07` (cut by the Queen):
>
  > `«Утренняя сводка вопросов может молчать каждый будний день, если Recall не знает, кто владелец Telegram, и это молчание не отличить от «вопросов нет».»` (The morning digest of questions may stay silent every weekday if Recall does not know who the Telegram owner is, and this silence cannot be told apart from "no questions".)
>
  An empty result with a `reason` is "not collected", not "empty": `skip_if_empty` does not skip it, the reason
  line is added by code. The third Descoped item is closed.

## Options
1. A blocked or uncollectable brief is silently dropped (`blocked` -> deliver not called; reasoned-empty treated as empty and skipped) - the shape before these deltas; silence is indistinguishable from "nothing to report".
2. The brief is withheld but the channel receives a notice naming the cause class - the chosen shape.

No other alternatives recorded.

## Decision
Option 2. Deciding factor, from the owner (twice): silence in the channel must never be ambiguous between "nothing to say" and "something went wrong".

## Consequences
Not recorded.

## Invariants created
- The only silent outcome is a genuine `skipped` (all data lists empty, no `reason`). Every other non-delivery sends a notice to the channel.
- A privacy `blocked` run sends `«не надіслано: фільтр приватного — <клас причини>»` (the runtime notice text); the notice carries no data and no stop-word.
- An empty result carrying a `reason` means "not collected", not "empty": `skip_if_empty` must not skip it, and the reason line is added by code, not by the model.
- Covered by the check in `docs/defects/pipe-failure-loses-brief.md`.

## Revisit when
Not recorded.
