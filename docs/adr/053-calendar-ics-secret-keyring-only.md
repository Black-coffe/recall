# ADR-053: Calendar ICS address lives only in the OS keyring, never in the MCP process

- Status: proposed
- Date: 2026-10-26
- Spec: docs/specs/k6-calendar-ics (shipped 4.34.0)

## Context
A secret ICS address is a bearer credential: anyone holding it reads the owner's calendar. Recall has several
processes and surfaces (the Flask app, the MCP server, settings, logs, the DB) that could each end up holding it.

Assumption (verbatim, `docs/specs/k6-calendar-ics/plan.md` `## Assumptions`, "Secret storage"):

> each ICS URL lives in the OS keyring (as the Gmail password does), keyed by a source id; the DB keeps only a label, the host, enabled flag, last sync time and last error. The URL is never logged, never rendered back, never accepted as an MCP argument.

Plan delta (verbatim, same plan, `## Plan deltas`):

> 2026-10-26, story 03 build: `calendar_ics.list_sources` read the keyring for `configured`, which would pull the secret address into the MCP process; it gains `check_keyring=False` for MCP, so `app/services/calendar_ics.py` joins story 03 `## Files`.

Story notes: story 01 says "Do not route the URL through MCP, `.env` or the settings registry; do not log it, not
even the query string" and "fetch errors log a code or HTTP status, never the URL". Story 03 says "The MCP process
never touches the keyring: `calendar_ics.list_sources(check_keyring=False)`; the test makes `keyring.get_password`
raise". Repair round 1 (F3) made the keyring username the exact normalised label, the same value as the UNIQUE
`calendar_sources.label`, because «Google» and «google» had shared one keyring entry.

The one-time connect page and keyring storage follow `docs/wiki/gmail-ingest-privacy.md`. This ADR adds the rule
that the MCP process never reads the keyring, and extends that wiki rule to calendar sources.

## Options
None recorded. The delta states the chosen shape only.

## Decision
The secret ICS address is typed only on the one-time `/connect/calendar/<token>` page and stored only in the OS
keyring (service `Recall Calendar`, username = the exact normalised source label). The DB, `.env`, the settings
registry, logs, rendered pages and every MCP argument and result never hold it. The MCP process never calls the
keyring for calendar sources.

## Consequences
Not recorded beyond the above. A guard test makes `keyring.get_password` raise inside the MCP calendar tests.

## Invariants created
- Any code path reachable from `mcp_server.py` calls calendar source helpers with `check_keyring=False` (or an
  equivalent that never reads the keyring).
- No MCP tool connects, syncs or disconnects a calendar, and none accepts or returns a source URL.
- Calendar fetch errors log a code or HTTP status, never the URL or its query string.
- A keyring entry's username for a source equals the source's UNIQUE label. Two sources never share a keyring entry.

## Revisit when
Not recorded.
