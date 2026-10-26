# ADR-052: MCP tool cap raised from 90 to 100

- Status: proposed
- Date: 2026-10-26
- Spec: docs/specs/mcp-recording-live (decided at plan approval 2026-10-25; shipped 4.32.0)

## Context
The mcp-full-control report §3.0 capped the MCP registry at 90 tools. After `mcp-telegram-automation` the registry
was planned at 89, and `mcp-recording-live` adds six tools (`recording_devices`, `get_copilot_timeline`,
`recording`, `copilot`, `recording_stop`, `recording_cancel_own`), which takes it to 95.

Plan delta (verbatim, `docs/specs/mcp-recording-live/plan.md` `## Plan deltas`):

> 2026-10-25, owner at approval: the report §3.0 tool cap is raised from 90 to 100 (95 after this spec); the Q1 read merges move to `mcp-triggers`.

Reason (verbatim, same plan, `## Assumptions`, "Tool count"):

> Recommended: the Queen records a plan delta raising the §3.0 cap to 100 (the 90 figure waited on a Desktop measurement that never happened; the Q1 merges break the `recall` skill, `test_mcp_about` and `test_tool_count_docs`). Owner veto: run the Q1 merges (−9) as a separate Tier 2 spec before story 06.

Approval line: "tool cap raised to 100" (owner, 2026-10-25, recommended option 1).

## Options
1. Raise the cap to 100 and move the Q1 read merges to `mcp-triggers`. Chosen.
2. Keep 90 and run the Q1 merges (-9 tools) as a separate Tier 2 spec before story 06. Rejected: the merges break the
   `recall` skill, `test_mcp_about` and `test_tool_count_docs`, and the 90 figure was never backed by the Desktop
   measurement it was waiting on.

## Decision
The report §3.0 MCP tool cap is 100. After `mcp-recording-live` the registry holds 95 tools. The Q1 read merges are
no longer a precondition for adding tools; they move to `mcp-triggers`.

## Consequences
Not recorded beyond the above. Five tools of headroom are left after 4.32.0.

## Invariants created
- The MCP registry (`_TOOL_CLASS` in `mcp_server.py`) holds at most 100 tools. A spec that would go past 100 has to
  decide again: merge tools or raise the cap, with the owner.
- The tool count in the guards and docs (`test_tool_count_docs`, `PRODUCT.md`, `CLAUDE.md`, `CLAUDE.vulyk.md`,
  `docs/MCP_SETUP.md`) comes from the measured tree, not from the plan.

## Revisit when
Not recorded.
