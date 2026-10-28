# ADR-057: Pre-meeting cards use an event-relative ticker, not the cron pipe scheduler

- Status: proposed
- Date: 2026-10-28
- Spec: docs/specs/wave4-client-calls

## Context
The owner asked for a Telegram card N minutes before a calendar event. The pipes
scheduler already delivers briefings to the bot. From plan.md `## Rejected alternative`, verbatim:

> Sending the pre-meeting card through `pipes.PipeScheduler` as a cron pipe (a morning "today's meetings" briefing): it would reuse `run_pipe` and `pipe_runs` for free, but the ask is a card N minutes before the event, the scheduler is cron-only with a 30 s tick and a per-moment marker, not a per-event one, and a daily digest gives the owner the card hours before the call, when the tasks and last calls are stale. The ticker keeps the pipes delivery chain (filter -> cards -> stop-word re-check -> bot) and adds only the event-relative trigger.

From plan.md `## Assumptions`, "Card trigger (Ask 1)", verbatim:

> a `MeetingBriefTicker` thread (60 s, modelled on `CalendarPoller`) sends a card for every timed, non-cancelled event whose start falls within the next `MEETING_BRIEF_LEAD_MINUTES`; marker `(event_id, start_utc)` in `meeting_brief_sent` with `status sent|failed|skipped`; a failed send is retried every tick until the event starts and is never marked sent.

## Options
1. Cron pipe in `pipes.PipeScheduler` (a morning "today's meetings" digest) - reuses `run_pipe` and `pipe_runs`;
   cron-only, per-moment marker, card arrives hours early with stale tasks and calls.
2. A dedicated `MeetingBriefTicker` thread with a per-event marker (chosen) - fires relative to each event's start;
   a second scheduler-like thread to maintain.

## Decision
Option 2. Deciding factor: the trigger is relative to each event, which the cron-only scheduler with a per-moment
marker cannot express, and a daily digest would show stale data.

## Consequences
Recorded: the ticker reuses the pipes delivery chain and adds only the trigger. Other consequences not recorded.

## Invariants created
- An event-relative delivery keys its sent-marker on the event occurrence `(event_id, start_utc)`, not on a
  schedule moment.
- An event-relative delivery goes through the same pipes delivery chain: filter -> cards -> stop-word re-check ->
  bot (owner chat only); it adds a trigger, never a second rendering or privacy path.
- A failed send is retried until the event starts and is never marked sent.

## Revisit when
Not recorded.
