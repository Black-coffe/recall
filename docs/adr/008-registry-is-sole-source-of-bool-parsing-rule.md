# ADR-008: The registry is the sole source of the boolean-flag parsing rule, not only of their defaults

- Status: proposed
- Date: 2026-09-06
- Spec: docs/specs/config-registry-fix

## Context

From `## Plan deltas` of the `config-registry-fix` spec (2026-09-06, "falsy narrowing of the
unification of boolean parsers"):

> Decision: record that unification on `settings.env_bool` (truthy `1/true/yes/on`) narrows behavior
> on junk values — `COPILOT_ENABLED`, `RECORDING_ENABLED`, `RECALL_LOCAL_TRUSTED` with a value
> outside the truthy set (e.g. `ага`) now give `False`; before the unification the corresponding ad-hoc parsers
> (`{'1','true','True'}` without `.lower()`, inverse checks against `{'0','false','no'}`, etc.) on the same
> junk input gave `True`.
>
> What was rejected: bringing the ad-hoc parsers back to preserve the accidental old behavior on
> junk values — that would contradict the very purpose of the registry as the single source of the parsing rule.

The same defect class repeated in round 3 (`docs/specs/config-registry-fix-r3/plan.md`, the delta
"`app.py` entered the Files of round 3 — three boolean parsers bypassing the registry closed"): `RECALL_BIND_ALL`,
`RECALL_PRELOAD_WHISPER`, `RECALL_LOG_TO_FILE` had their own parsers bypassing the registry in three more files
(`app.py`, `whisper_preload.py`, `app/core/logger.py`), and `RECALL_PRELOAD_WHISPER` before the move
to `env_bool` was inverted — a junk value was treated as `True`, after the move
it is treated as `False`. Three rounds in a row caught one and the same class: a boolean flag that is
read not through the registry.

## Options

None recorded - the plan fixes the chosen shape of the decision (normalize all reads onto
`settings.env_bool`), not the alternatives that were weighed.

## Decision

The rule for parsing a boolean flag (which string value means `True`/`False`) is defined exactly
once — in `settings.env_bool` — and no part of the code has its own inverse or truthy set
outside it. This applies not only to the default value of a flag (which the registry already covers as a store of
defaults), but to the very rule of reading a string from `os.environ`. The cost of unification — a change of behavior on
junk values — is accepted deliberately and recorded in the delta, not silently.

## Consequences

Easier: a new boolean flag by definition is read the same way everywhere it is added;
divergence of two parsers of the same flag (as in `RECALL_PRELOAD_WHISPER` before and after) becomes
structurally impossible. Harder: moving an existing ad-hoc parser onto `env_bool` always carries a risk of
changing behavior exactly on edge/junk values — this risk is not removed by unification, but
is moved to an explicit, documented moment of transition.

## Invariants created

No part of the code reads a boolean env flag with its own `if value in {...}`/`not in {...}` —
every read of a boolean flag goes through `settings.env_bool(name)`. A change to the set of truthy/falsy
values happens exactly once, in this function, and immediately applies to all flags.

## Revisit when

A flag is needed with semantics wider than a truthy/falsy string (for example, three-valued logic
`on|off|auto`) — then `env_bool` does not fit by construction, and the question "where is another source of the
parsing rule" will arise again for the new type.
