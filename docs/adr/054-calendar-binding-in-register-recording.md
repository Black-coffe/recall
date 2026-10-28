# ADR-054: Recording-to-event binding and title order live in `register_recording`

- Status: proposed
- Date: 2026-10-26
- Spec: docs/specs/k6-calendar-ics (shipped 4.34.0)

## Context
A recording reaches the library through two save paths (the tab `/save` and server reconcile/autosave). Lesson
`two-save-paths-share-defaults`: a default applied on one path only produces records that differ by how they were saved.

Plan delta (verbatim, `docs/specs/k6-calendar-ics/plan.md` `## Plan deltas`):

> 2026-10-26, story 02 build: every library row of a recording is written by `recording/library.register_recording` (both `/save` and reconcile), and both save paths transcribe through `transcription_run.run`; binding and the event title go there, not into `finalize.py` (dropped from `## Files`). `enrichment.py:342` deletes every `meeting_entities` row of a record on enrich, which would erase calendar links a minute after transcription; it now keeps `source='calendar'` rows. Story 02 `## Files` gains `library.py`, `transcription_run.py`, `enrichment.py`. Rejected: binding at transcribe time from `created_at` (the tab save can be minutes after the stop; the manifest's `started_at` is exact).

Binding rule (verbatim, same plan, `## Assumptions`, "Binding rule"):

> largest overlap between the recording's [start, end] and the event's [start − 10 min, end + 10 min]; cancelled and all-day events are skipped; overlap must cover at least a third of the recording, otherwise no binding.

Repair round 1 (F7) measured the live DB: 289 of 290 recordings have `manifest.json` with `started_at`, and 38 were
saved more than 10 min after the stop (median 111 s, p90 706 s). The backfill now uses `started_at`, with
`created_at - duration` only as the fallback.

## Options
1. Bind in `recording/library.register_recording`, the single writer of a recording's library row. Chosen.
2. Bind in `finalize.py`. Rejected: it would miss the tab-saved rows registered by `/save`.
3. Bind at transcribe time from `created_at`. Rejected: the tab save can be minutes after the stop, and the
   manifest's `started_at` is exact.

## Decision
`register_recording` (`_bind_calendar`) binds a recording to its event using the manifest `started_at` +
`total_duration_sec`. `transcription_run.run` copies `calendar_event_id` to the transcript. The title order is
fixed: the owner's name, then the event title, then Claude's `suggest-meta`. An explicit or manifest name is never
replaced. Only an automatic name (`auto_name` or the unnamed fallback) becomes the event title. Autosave skips
`suggest-meta` only when the bound event has a non-empty title. A calendar error is logged and never fails a save.

The ±10 min padding and the one-third overlap were planning defaults, not measured values. The backfill report
(ask 7, `docs/specs/k6-calendar-ics/report.md`) is where they get checked.

## Consequences
Not recorded beyond the above.

## Invariants created
- Binding a recording to an event and choosing its title from the event happen only in
  `recording/library.register_recording`. No other save path, finalize step or route does either.
- A recording's start time comes from the manifest's `started_at`. `created_at - duration` is only a fallback when
  there is no manifest.
- Title order: owner > event > Claude. Nothing renames an already-named record, the backfill included.
- A calendar failure never fails a save.

## Revisit when
The backfill report (ask 7) shows the ±10 min / one-third defaults binding the wrong event or missing too many
recordings. Otherwise not recorded.
