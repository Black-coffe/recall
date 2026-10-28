# ADR-058: MCP meeting brief proxies an HTTP route; the stdio process does not read the DB for it

- Status: proposed
- Date: 2026-10-28
- Spec: docs/specs/wave4-client-calls

## Context
The meeting brief needs `enrichment` and `commitments`, and it has three consumers: the ticker, the HTTP route and
the MCP tool. From plan.md `## Assumptions`, "MCP brief (Ask 2)", verbatim:

> `get_meeting_brief` proxies a new `GET /api/meeting-brief` route through `_api()` like the other 77 proxy tools, instead of reading the DB in the stdio process: `enrichment` and `commitments` are not verified light enough for the stdio import rule (`memory/mcp-stdio-no-heavy-models`), and one server-side `filter_brief` then serves the ticker, the route and the tool. The tool needs app.py running, as every proxy tool does.

## Options
none recorded - the delta states the chosen shape only. (The plan names the shape it avoided, direct DB reads in
the stdio process, but does not record it as a weighed option.)

## Decision
`get_meeting_brief` is a proxy tool over `GET /api/meeting-brief` through `_api()`. Deciding factors: the stdio
import rule, and a single server-side `filter_brief` for every consumer.

## Consequences
Recorded: the tool needs app.py running, as every proxy tool does. Other consequences not recorded.

## Invariants created
- An MCP tool whose data needs a service module that is not verified light enough for the stdio import rule
  (`memory/mcp-stdio-no-heavy-models`) proxies an app route through `_api()`; it does not import that module in the
  stdio process.
- The privacy filter for data with several consumers runs server-side in one place (`filter_brief` for the brief);
  the MCP tool never applies its own copy.

## Revisit when
Not recorded.
