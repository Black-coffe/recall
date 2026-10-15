# ADR-021: `display_name` travels next to `source_name` — provenance is never overwritten

- Status: proposed
- Date: 2026-09-21
- Spec: docs/specs/editable-title-description

## Context

From `docs/specs/editable-title-description/plan.md`, `## Assumptions`:

> **Search results** (`retrieval.search`, `rag` sources, `/api/memory/search`, MCP
> `search_archive`/`ask_archive`) carry `display_name` and `title` ALONGSIDE `source_name`; the
> `source_name` key is not overwritten (provenance, the S2/Wave A contract does not change).

From the same plan.md, `## Tradeoffs`:

> **`display_name` alongside `source_name`** versus "`COALESCE` into `source_name` at the API output":
> the latter is one SQL change per consumer, but it breaks provenance in the JSON export, MCP and the S2 contract;
> the cost of the former is that every frontend reader moves to the new field (05).

From `docs/specs/editable-title-description/brief.md`, `## Answers`, item 1 (the owner's chosen
option, recommended):

> **Storage** — New title + description fields (Recommended): Two new columns; source_name
> stays as the "origin" (the original file name). Everywhere, title is shown if it exists,
> otherwise source_name. This way the Telegram listener, when re-saving an edited message,
> will not wipe your title (it rewrites source_name specifically), and the original file name is not lost.

## Options

1. **New `display_name` field alongside `source_name`** (chosen) — `source_name` stays pure
   provenance (original filename / Telegram chat title / etc.), never mutated by a title edit;
   every reader that used to show `source_name` switches to `title || source_name` (implemented
   as `record_meta.display_name()`).
2. **`COALESCE` the owner's title into `source_name` at the API output layer** — one SQL change
   per consumer, cheaper to wire up, but overwrites provenance in the JSON export, in MCP, and in
   the S2 grep-explainability contract, and a re-saved Telegram message would silently clobber an
   owner-set title the next time the listener rewrites `source_name`.

## Decision

Add `display_name` as a computed field carried alongside `source_name` everywhere a record is
serialized (`PATCH`/`GET /api/history`, `PATCH /api/audio/downloads`, exports, RAG context
prefix, MCP tools, SPA views). `source_name` is never overwritten by a title/description edit.

## Consequences

Every existing and future reader that surfaces `source_name` to a person must be updated to read
`display_name` instead (or `title || source_name`) — this touched creation-path serializers,
RAG's `build_context_prefix`, MCP's `list_recent`/`get_transcript`/`search_archive`/`ask_archive`,
exports, comment chips (`target_name`), and SPA list/detail views (story 05, `## Tradeoffs`: "the cost
of the former is that every frontend reader moves to the new field"). In exchange, provenance stays intact: a
re-saved Telegram message can keep overwriting `source_name` without ever touching an owner-set
title, and the JSON export/MCP/S2 contracts keep their original meaning of `source_name`.

## Invariants created

- `source_name` is provenance only and is never overwritten by a title/description edit, from any
  ingestion or re-save path.
- Any surface that exposes `source_name` to a person must also expose `display_name`
  (`record_meta.display_name(row)`: `title` if non-empty after strip, else `source_name`, else
  `f"Запис #{id}"`) rather than silently falling back to `source_name` alone.
- A new consumer must not invent its own `COALESCE`/fallback logic — it calls
  `record_meta.display_name()` (or reads the already-serialized `display_name` field) so the
  fallback chain has exactly one implementation.

## Revisit when

A future surface has no room to carry two separate fields (`title`/`source_name`) and must
collapse to one value with no distinguishable provenance — that is a different tradeoff than the
one made here and needs its own decision, not a silent `COALESCE` at that one call site.
