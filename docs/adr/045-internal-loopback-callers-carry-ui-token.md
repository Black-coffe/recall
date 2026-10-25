# ADR-045: Recall's own loopback callers carry the per-boot UI token

- Status: proposed
- Date: 2026-10-20
- Spec: docs/specs/mcp-archive-org

## Context
Spec `mcp-archive-org` (4.26.0) made every non-UI write behave as an agent write (`classify_request()[0] != "ui"`):
forced `source='mcp'` provenance, caps, owner-row guards, `JOB_ACTIVE` refusals. Recall's own server code that calls
its HTTP API over loopback sent no UI token, so it was classified as an agent. Plan delta, repair round 1:

> 2026-10-20, repair round 1: finding 2 (autosave title PATCH refused as an agent) has its root in `app/services/recording/autosave.py` `http_json`, which sends no UI token although it is Recall's own server acting for the owner. Story 07 `## Files` gets that file and `tests/test_recording_autosave.py`; keying on `X-Recall-Actor` was rejected (any caller could claim it).

ADR-042 decision 4 already says `X-Recall-Actor` is a label that never changes a decision; this ADR adds the rule for
code inside Recall that acts for the owner.

## Options
1. Recall's loopback caller sends the per-boot UI token - chosen.
2. Key the UI/agent decision on `X-Recall-Actor` - rejected: any caller could claim it.

## Decision
Server-side Recall code that calls its own `/api/*` over loopback on the owner's behalf (first case: the recording
autosave `http_json` in `app/services/recording/autosave.py`) sends the per-boot UI token and is thereby classified
`ui`. No header label, flag or caller name is used to exempt internal callers from the non-UI rules.

## Consequences
A token-bearing internal caller is classified `ui`, so it skips the agent-only rules (MCP provenance stamps, job-active refusals). That is intended: it acts for the owner, not as an agent. D actions still go through the Windows dialog for every caller (report §11 A1), so the token does not bypass the gate.

## Invariants created
- A new in-process or loopback caller that acts for the owner must send the per-boot UI token; without it, it is an
  agent (non-UI) caller and gets agent provenance and agent refusals.
- `X-Recall-Actor` (or any other self-declared label) never makes a caller `ui` (restates ADR-042 decision 4 for
  internal callers).

## Revisit when
Not recorded.
