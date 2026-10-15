# ADR-015: Standalone-process file logging gates on `"pytest" in sys.modules`, not `PYTEST_CURRENT_TEST`

- Status: proposed
- Date: 2026-09-16
- Spec: docs/specs/test-log-isolation

## Context

From `docs/specs/test-log-isolation/plan.md`:

> Both standalone processes attach a `RotatingFileHandler` to their live log at module import,
> unconditionally (`telegram_listener.py:56-73`, `mcp_server.py:54-72`). Tests
> import these modules — and write to the same files. The consequence is not cosmetic: the live
> process log stops being a witness when analyzing an incident, which already cost
> a wrong conclusion on 12.09.2026.
>
> In the application the same problem is solved: `app/core/logger.py:48-56` enables the file
> handler only under `RECALL_LOG_TO_FILE=1`. The settings registry already knows
> `PYTEST_CURRENT_TEST` as an external variable (`app/core/settings.py:608`).

The registry already listed `PYTEST_CURRENT_TEST` as a known external variable, making it
the obvious first choice for the gate. It does not work for this case: pytest sets
`PYTEST_CURRENT_TEST` only during a test's setup/call/teardown phases, not during
collection - and `telegram_listener.py`/`mcp_server.py` install their file handler at
*import* time, which for a test module happens during collection, before
`PYTEST_CURRENT_TEST` is set. The comment added at the fix site (`telegram_listener.py:63-75`)
records both the failed first choice and the measurement that led to the working one:

> "pytest" in sys.modules is put there by pytest right at startup, before test collection, so it
> is the reliable indicator here. The console handler stays under pytest too, but pytest has already
> attached its own handlers to root, so this basicConfig() is a no-op and the StreamHandler is not
> attached at all (measured).

## Options

1. Gate on `PYTEST_CURRENT_TEST` (consistent with the existing settings-registry entry).
   Rejected: empty at module-import/collection time, so it does not fire when the bug
   actually happens (import during collection, not during a running test).
2. Gate on `RECALL_LOG_TO_FILE`-style explicit opt-in env var, mirroring `app/core/
   logger.py`. Not chosen for these two modules: they are meant to log to file by default
   in production (unlike the shared `whisper_ui` logger, which nothing reads), so an
   opt-in flag would require every real deployment to set it - the goal here is only to
   suppress logging during test collection/execution, not to make file logging opt-in.
3. Gate on `"pytest" in sys.modules` - true from the moment the pytest process starts,
   before any test collection or import happens. Chosen.

## Decision

`telegram_listener.py` and `mcp_server.py` each check `"pytest" in sys.modules` before
attaching their `RotatingFileHandler` to the process-owned log file
(`telegram_listener.log`, `mcp_server.log` respectively). The console/stream handler is
still attached under pytest (it is a no-op in practice, since pytest has already wired
its own root handlers), so this only suppresses the file write, not all logging.

## Consequences

A test suite that imports these modules - even only at collection time, never executing
their code - no longer writes to the production log files, which restores their value as
an incident-analysis witness. The cost is that `"pytest" in sys.modules` is a slightly
unusual check (module-presence rather than the "designed for this" env var); a future
maintainer unaware of this ADR could "simplify" it back to `PYTEST_CURRENT_TEST` and
silently reintroduce the bug, since the failure mode (collection-time import) is not
obvious without having hit it.

## Invariants created

Any new standalone process module (not going through `app/core/logger.py`) that attaches
a file handler at import time must gate that attachment on `"pytest" in sys.modules`,
not `PYTEST_CURRENT_TEST` - the latter is documented in `app/core/settings.py`'s
`IGNORED_ENV_NAMES` as a known external variable but is not a valid test-detection signal
at import/collection time.

## Revisit when

A future refactor moves these standalone processes onto `app/core/logger.py`'s
`RECALL_LOG_TO_FILE`-gated setup instead of their own ad hoc handler wiring - at that
point this gate becomes redundant with the opt-in default and can be retired, provided
the new setup is verified not to write during collection either.
