# ADR-059: The in-call "what did I miss" summary uses Claude Sonnet under a per-call cap, never the local LLM

- Status: proposed
- Date: 2026-10-28
- Spec: docs/specs/wave4-client-calls

## Context
During a call the owner can ask for 3-5 lines on what was decided, promised and asked of him. From plan.md
`## Goal`, verbatim:

> During a call a button on the record view (and an MCP tool) asks Claude Sonnet for 3-5 lines on what was decided, promised and asked of the owner since his last utterance, or over the last 2/5/10 minutes, or the whole call, under a per-call spend cap and never on an agent-started recording; the local LLM is never used for it (it would silence the copilot).

From plan.md `## Assumptions`, "What did I miss (Ask 4)", verbatim:

> One Claude call with the Sonnet model id `escalate.py` already uses for its normal verdicts, through `claude_retry.call_with_retry`; cost estimated before the call with `pricing.estimate_cost` and added to an in-memory per-session total; over `LIVE_MISSED_BUDGET_USD` -> 429 `LIVE_BUDGET_EXHAUSTED`. Nothing is persisted (like `live-ask`). An agent-class session -> 403 `AGENT_SESSION` for every caller.

## Options
none recorded - the delta states the chosen shape only.

## Decision
The live summary is one Claude Sonnet call (the model id `escalate.py` uses), through `claude_retry.call_with_retry`,
with the cost estimated before the call and capped per session. Deciding factor: a local LLM call during a recording
would silence the copilot.

## Consequences
Not recorded. (The agent-session refusal follows ADR-051 and is not decided here.)

## Invariants created
- An LLM feature that runs during a live recording does not call the local LLM.
- A paid live call estimates its cost with `pricing.estimate_cost` before the call and refuses with 429
  `LIVE_BUDGET_EXHAUSTED` once the per-session total would pass its budget setting.
- The live summary persists nothing.

## Revisit when
Not recorded.
