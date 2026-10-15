# ADR-025: Copilot system-level notices travel through the insight/log_event channel, never through `copilot_status`

- Status: proposed
- Date: 2026-09-22
- Spec: docs/specs/directions-projects-tags

## Context

Wave 7 of this spec fixed `copilot/worker._scope_entity_ids` so that an ambiguous call scope
(a direction namesake) is not silently swallowed as "not found". This is an extension of the spec beyond all
earlier `## Files` (the file itself was in none of stories 01–21), and the decision is recorded explicitly:

> **Decision 4 — fix the copilot (major 3 r0 / minor 11 r1), do not accept it as a risk:**
> `copilot/worker.py` was not in `## Files` of any of stories 01–21 — this is a **spec extension**,
> marked explicitly (C10, Assumptions)… If the owner does not want a copilot change in this spec, 25
> is dropped with one word and the risk goes to `## Descoped` as accepted — but then "never silently" on a
> live call is not met.

The first edition (story 25) showed the warning through `copilot_status`. The round 6 review
found that this channel does not reach the operator (the copilot state is redrawn on the next tick and
the warning disappears, leaving no trace in the session history). Wave 8 replaced the channel:

> **Decision 3 — the copilot through the existing insight path (27).** A card + `log_event` instead of
> `copilot_status`: it is both rendered by the widget and lies in the session stream (MCP `get_live_copilot`
> sees it — memory note `live-mcp-agent-gets-raw-data`), and does not redraw the state. `record.js` is in
> `## Files` (it was not in 25, hence the critical finding). Rejected: a banner through `applyCopilotStatus`
> (hidden by the very first `setCpStatus`, does not get into the events).

Contract C10 (the plan's October) confirms the form: a `log_event` event + `copilot_insight` with its own
`kind`, the card is rendered by `record.js`, lies in the session stream (`get_state`/`get_live_copilot`/
timeline), outside `_card_gate` and `cards_shown`; `copilot_status` with `state:"quiet"` is not published
for this case.

## Options

1. **Chosen:** a system warning — a `log_event` event + a `copilot_insight` card with its own
   `kind`, the same rendering path as insights (`record.js`), explicitly outside the card budget
   (`_card_gate`, `cards_shown`) and outside the insight counter.
2. Rejected: a banner through `copilot_status`/`applyCopilotStatus` — the state is redrawn by the very next
   `setCpStatus` tick and never reaches the persistent session events (`get_live_copilot` does not see it,
   it does not stay in the timeline/export).
3. Rejected (considered as the wave 7 alternative): accept the hole as a risk and not fix the copilot in
   this spec — contradicts the owner's direct requirement "never silently".

## Decision

Any **system** message from the copilot to the operator (not an analytical insight from the model) —
an ambiguous scope, future classes of warnings of this kind — goes through the same event model
as insights: `log_event` + an entry in `copilot_insight` with its own `kind`, rendered by the existing card
path (`record.js`), and therefore stays in `get_state`/`get_live_copilot`/timeline/export.
It is explicitly excluded from the card budget (`_card_gate`, `cards_shown` do not count it) and from the counter of
shown insights. The `copilot_status` channel is not used for system messages — it
lives for only one tick and does not persist.

## Consequences

Easier: system warnings are visible to the operator and to the agent (MCP) without a new table/migration —
the existing event model `copilot_sessions/topics/events` (v19) is reused. Harder: not recorded — the delta
does not name what this costs today's code outside `worker.py`/`record.js`.

## Invariants created

- A copilot notice that is not a model-generated insight (a system-level warning, e.g. an
  ambiguous call scope) is delivered as a `log_event` + `copilot_insight` entry with its own
  `kind`, rendered through the existing insight card path, and is exempt from the card budget
  (`_card_gate`, `cards_shown`) and the insight counter.
- `copilot_status` is never the channel for a notice that must persist — it is overwritten by the
  next status tick and is invisible to `get_live_copilot`/session history/export.

## Revisit when

Not recorded — the delta does not name a specific trigger for revisiting.
