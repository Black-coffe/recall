# ADR-002: `comments.link_entities` stays on exact match, independent of the morph flag

- Status: proposed
- Date: 2026-08-21
- Spec: docs/specs/entity-graph-tg

## Context

From `docs/specs/entity-graph-tg/plan.md`, `## Plan deltas` (21.08.2026, ремонтний
раунд after `/vulyk-review`, BLOCK critical #1):

> Шлях коментарів не має мовчки змінювати поведінку при вмиканні морфо-прапорця:
> `comments.link_entities` лишається на точному матчі, бо замір історії 04 його не
> бачить і вимикання прапорця вже записаних звʼязків не знімає.

`TG_ENTITIES_MORPH_ENABLED` is a single env switch shared across the module, but not
every consumer of entity mentions should be governed by it: `comments.link_entities`
is a separate write path that persists links, and (a) the measurement tooling built
in story 04 (`evals/graph_links.py`) does not observe it, and (b) turning the flag
back off does not retract links already written while it was on. Toggling it would
silently and irreversibly change comment-linking behavior outside what the flag was
designed to gate.

## Options

none recorded - the delta states the chosen shape only.

## Decision

`comments.link_entities` is hard-wired to exact match (`SOURCE = "thread_match"`)
and does not read `TG_ENTITIES_MORPH_ENABLED`. Only the mention matcher inside
`tg_entities.py` that story 04 measures is gated by the flag.

## Consequences

A feature flag that lives in a shared module does not automatically extend to every
consumer of that module - each consumer decides for itself whether to read the flag,
and that decision is explicit, not inherited. Comment links stay reproducible and
reversible (owner can flip the flag without corrupting comment history); the tradeoff
is that comments will not benefit from morph-matched mentions even after the flag is
enabled, unless a future story explicitly wires it in with its own measurement.

## Invariants created

A shared feature flag governs only the code path that was measured under it
(`tg_entities` mention matching). Any other consumer that wants the same behavior
must adopt the flag explicitly, with its own read path documented, not by falling
through the shared module default.

## Revisit when

A future story adds measurement of comment-linking recall/precision under morph
matching (extending or replacing `evals/graph_links.py`'s blind spot) and the owner
decides comments should also respect the flag.
