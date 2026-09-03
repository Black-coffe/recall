# ADR-003: Measurement tooling must not mutate the DB snapshot it measures

- Status: proposed
- Date: 2026-08-21
- Spec: docs/specs/entity-graph-tg

## Context

From `docs/specs/entity-graph-tg/plan.md`, `## Plan deltas` (21.08.2026, ремонтний
раунд after `/vulyk-review`, BLOCK critical #1):

> Оснастка заміру не має змінювати файл знімка, який міряє, включно з його
> заголовком: функції, що відкривають власне читально-писальне зʼєднання
> (`get_db_connection` з `PRAGMA journal_mode=WAL`), працюють з тимчасовою копією, а
> не зі знімком.

`evals/graph_links.py` (story 04) measures graph links on a DB snapshot in two
configurations (exact-only / exact+morph) and diffs them. The project's shared
`get_db_connection` helper opens a read-write connection under
`PRAGMA journal_mode=WAL`, which writes to the file (including its header) merely by
opening it - even if the measurement code issues no writes of its own. Run against
the snapshot file directly, this would mutate the artifact the tool exists to measure
reproducibly.

## Options

none recorded - the delta states the chosen shape only.

## Decision

Any code path in measurement/eval tooling that needs the shared read-write
connection helper (`get_db_connection`, WAL mode) operates on a temporary copy of the
snapshot, never on the snapshot file itself.

## Consequences

Snapshot-based measurements stay reproducible and diffable across runs and across
people, at the cost of an extra copy step (disk I/O, temp-file lifecycle) before
every measurement run. Generalizes past this one script: any future tool that opens
the shared connection helper against a fixed artifact inherits the same requirement.

## Invariants created

A read-write connection (WAL-mode `get_db_connection`) is never opened directly
against a file that is meant to remain a stable, re-measurable snapshot; open a
temporary copy instead.

## Revisit when

The project adds a read-only variant of `get_db_connection` (e.g. `sqlite3.connect`
with `mode=ro` URI) that cannot mutate the file it opens - at that point tooling
could target the snapshot directly instead of copying it.
