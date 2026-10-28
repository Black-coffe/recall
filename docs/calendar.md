# Calendar (secret ICS links, read-only)

Recall reads one or more calendars through their secret ICS address. Nothing is written back
to any calendar and no OAuth is involved.

## Connect a calendar

1. Get the secret address.
   - Google Calendar: Settings → the calendar → Integrate calendar → **Secret address in iCal format**.
   - Outlook (Microsoft 365 or outlook.com): Settings → Calendar → Shared calendars →
     **Publish a calendar**, choose the calendar and **Can view all details**, copy the ICS link.
     A work tenant may have publishing turned off by its admin, and a published feed may carry
     no attendees; the Calendars row then shows `with attendees: 0`.
2. In Recall: Settings → **Calendars**, type a name (for example `Google` or `Outlook Work`)
   and press **Connect**. A one-time local page opens (10 minutes, three attempts); paste the
   address there. The page checks that it is `https://` or `webcal://` on
   `calendar.google.com`, `outlook.office365.com`, `outlook.office.com` or `outlook.live.com`
   and that it returns a calendar, then shows how many events of the next and last 30 days it
   found and how many carry attendees.

The address is a password: it is stored only in the Windows credential store
(`Recall Calendar`), never in the database, `.env`, logs or MCP. Connecting the same name again
replaces the address. **Disconnect** removes the address, the calendar's events and the
recordings' links to them, and asks for confirmation in the Windows dialog.

## Sync

`CALENDAR_SYNC_ENABLED` (default `1`) and `CALENDAR_SYNC_MINUTES` (default `15`). Each sync
expands recurring events for −30…+30 days (excluded dates and moved occurrences included). An
occurrence that disappears from the feed or is cancelled is kept as `cancelled`, so a recording
bound to it keeps its link.

## Recording ↔ event

- When a recording is saved it is bound to the event with the largest overlap (the event widened
  by 10 minutes each side; at least a third of the recording must overlap). Cancelled and
  all-day events never bind.
- Title order: the name you gave the recording, then the event title, then the title Claude
  suggests. An event-titled recording is not renamed by the autosave.
- Attendees are linked to people who already exist in the graph (name, alias or a merged
  spelling). No new person is created and emails are never added as aliases.

Past recordings: first pull the whole history once,
`python -m app.services.calendar_ics sync --full` (the periodic sync keeps only ±30 days; it prints
the earliest timed event the feeds gave; all-day events do not count, they never bind). Then `python -m app.services.calendar_bind backfill --dry-run` shows what would be
bound (per calendar, plus the number of people before and after, which must be equal); without
`--dry-run` it writes the links. Existing titles are never changed. The start of each recording
comes from its session `manifest.json` (`--sessions`, default `recordings/sessions`); only when
that file is gone is it estimated as the save time minus the duration.
The report's `calendar_earliest` is the oldest stored event, `before_calendar` counts recordings
older than it (no event can exist for them) and `coverable` is the denominator for the bind rate.

## MCP

`list_calendar_events(start, end)` lists events in a window (default: the last 24 hours and the
next 7 days) with attendees and the bound `transcription_id`. `get_transcript` returns
`calendar_event` for a bound record. Events bound to a private-direction record, and titles with a
stop word from `private.txt`, are hidden.
