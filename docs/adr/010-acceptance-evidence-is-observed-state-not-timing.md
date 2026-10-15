# ADR-010: Evidence of a state transition is a trace the product itself left, not timing and not a variable that is computed and thrown away

- Status: proposed
- Date: 2026-09-06
- Spec: docs/specs/config-registry-fix-r3

## Context

From `## Plan deltas` of the `config-registry-fix-r3` spec ("N1 was confirmed by half — and the other half
must be read as refuted"):

> What is true: `test_headless_boot_ready_waits_for_preload` really proved nothing —
> `saw_not_ready` was recorded and thrown away by the line `_ = saw_not_ready` (291), so the 503 window might
> never have been observed, and the test stayed green.
>
> What is NOT true: the report's conclusion that the `large-v3-turbo` warm-up in "4.12 seconds... either is not performed
> or is performed not by the path used in prod" was reasoning, not a measurement. ... Here the Queen wrote
> an invented mechanism — "CTranslate2 simply serves the already cached model through memory-mapping that
> fast" — which nobody observed. That was untrue, not a refinement of wording. The measured
> cause is different: the child process boot log says `Model loaded on GPU: NVIDIA GeForce RTX 3090`,
> warm-up 1.7s — the model loads onto the GPU..., despite the fact that the `_spawn_headless` harness sets
> `FORCE_CPU=1`.
>
> Why this is recorded: the next round must not rediscover the "impossible speed" as a finding.
> Speed in itself was never evidence; evidence is what the product wrote to the log.

## Options

None recorded - the plan fixes the chosen shape of the decision (observe the log, not timing), not the
alternatives that were weighed.

## Decision

An acceptance test that proves a state transition (for example, `loading → ready`) accepts as evidence only
an independent trace that the product itself left during execution — a log line, a state field value,
a DB record — and not (a) speed or execution timing by itself, and (b) a value that the test
computes and then ignores (`_ = saw_not_ready`), not checking it against anything. If the expected trace
is absent from the product, the test fails, rather than silently staying green on an opaque path.

## Consequences

Easier: the next repair round does not spend time rediscovering "suspicious speed" as a bug —
speed stops being an argument in itself. Harder: every state-transition test requires the
code under test to really leave an observable trace (a log, a field, a returned value) — a hastily
written "wait N seconds and believe" can no longer be passed off as evidence.

## Invariants created

An acceptance test that asserts a state transition either (1) observes an independent trace left
by the product and fails in its absence, or (2) is honestly marked as not proving the transition.
A computed but never checked variable (`_ = x`) is not evidence. A claim about the cause of behavior
("it is fast because...") without observed confirmation is recorded as reasoning, not as a fact.

## Revisit when

The project gets a standard mechanism for tracing state transitions (for example, a structured event log
that tests read directly) — then the "trace in the log" rule can be narrowed to the concrete format of this
mechanism instead of an arbitrary `assert "текст" in full_log`.
