# ADR-036: Every undelivered brief leaves a data-free trace in the delivery channel

- Status: proposed
- Date: 2026-10-15
- Spec: docs/specs/k4-scheduled-pipes

## Context
Plan delta, 2026-10-15 (fix story `k4-scheduled-pipes-06`), verbatim:

> - 2026-10-15 · Після GREEN раунду 3 — друга поправка власника, fix-історія `k4-scheduled-pipes-06` (ріже Queen):
>
>   > «Если фильтр приватного остановит брифинг, бот просто промолчит: не будет ни свода, ни сообщения об ошибке.»
>
>   Критерій story 02 «`blocked` → deliver не викликано» змінено: брифінг не йде, але в бот іде повідомлення
>   «не надіслано: фільтр приватного — <клас причини>», без даних і без стоп-слова. Другий пункт Descoped
>   (`PrivacyError` без повідомлення) закрито. Карточка `pipe-failure-loses-brief` розширена на цю форму.

Plan delta, 2026-10-15 (fix story `k4-scheduled-pipes-07`), verbatim:

> - 2026-10-15 · Після GREEN раунду 4 — третя поправка власника, fix-історія `k4-scheduled-pipes-07` (ріже Queen):
>
>   > «Утренняя сводка вопросов может молчать каждый будний день, если Recall не знает, кто владелец Telegram, и это молчание не отличить от «вопросов нет».»
>
>   Порожній результат із `reason` — «не зібрано», а не «порожньо»: `skip_if_empty` його не пропускає, рядок
>   причини додає код. Третій пункт Descoped закрито.

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
- A privacy `blocked` run sends «не надіслано: фільтр приватного — <клас причини>»; the notice carries no data and no stop-word.
- An empty result carrying a `reason` means "not collected", not "empty": `skip_if_empty` must not skip it, and the reason line is added by code, not by the model.
- Covered by the check in `docs/defects/pipe-failure-loses-brief.md`.

## Revisit when
Not recorded.
