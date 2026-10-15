# ADR-009: Робота never confuses an empty DB file with a ready schema

- Status: proposed
- Date: 2026-09-06
- Spec: docs/specs/config-registry-profiles

## Context

From `## Plan deltas` of the `config-registry-profiles` spec (2026-09-06, "`/api/health` moved to
the DB check shared with `/api/ready`, although the Non-goal of `config-registry-fix-03-робота-contract`
(round 1) forbade touching `/api/health`"):

> Trigger: `lead-review` round 2 — finding N3 ...: a DB file created empty gave
> `/api/health` `database:"ok"`/`/api/ready` `ready:true` without any schema (both endpoints only
> tried to open a connection, which itself creates the file).
>
> Decision: `app/blueprints/system.py::_check_database_ready(db_path) -> bool` — a single check for
> both endpoints: `''`/`None`/`':memory:'` → `False` without an exception; otherwise a `mode=rw` URI (without `c`,
> the file is not created) + `SELECT MAX(version) FROM schema_versions`; `True` only if there is a row and it is
> `>= 1`. The body and response codes of `/api/health` are unchanged (only the source of the `database` status),
> `app/db/connection.py` (the hot path) was deliberately not touched — a separate helper in `system.py`.

## Options

1. `mode=rw` directly in `get_db_connection()` (the application's hot path) — rejected: it would mean
   a "create/do not create the DB" flag in every call of the hot path for the sake of one narrow
   consumer (робота).
2. Leave `/api/health` as it was, recording the scenario in `## Descoped` — rejected: the scenario
   "a monitor creates an empty DB, both endpoints turn green" is cheaply closed by a separate helper,
   so merely documenting it instead of fixing it is not worth it.

## Decision

The DB робота check — for both `/api/ready` and `/api/health` — opens the file through the URI
`mode=rw` (without `c`, without auto-creation) and reads `SELECT MAX(version) FROM schema_versions`;
робота is recognized only when a row with a version `>= 1` is returned. Both endpoints go through
one and the same helper (`_check_database_ready`), not through opening a hot-path connection
(`get_db_connection`), which itself creates the file on opening.

## Consequences

Easier: no unauthenticated endpoint (monitoring, health/ready checks) can any longer accidentally
create an empty live DB by the mere fact of being called, and none of them reports "ready" until migrations
have really passed. Harder: the робота path keeps its own DB-opening helper separate from the application's hot
path — two ways to open the same file, synchronized only by a shared function name,
not by shared code with `app/db/connection.py`.

## Invariants created

No HTTP endpoint available without authorization (health/ready checks and similar) opens the DB
in a mode that creates the file if it is absent. DB робота is determined by the presence of completed
migrations (`schema_versions`), not by the mere fact that a connection opened.

## Revisit when

The hot path (`app/db/connection.py`) gets its own read-only mode (`mode=ro`/`mode=rw` without `c`
as a standard call option) — then робота can go through it directly, and the separate helper in
`system.py` becomes duplication that is worth removing.
