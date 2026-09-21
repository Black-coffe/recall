# ADR-021: `display_name` travels next to `source_name` — provenance is never overwritten

- Status: proposed
- Date: 2026-09-21
- Spec: docs/specs/editable-title-description

## Context

From `docs/specs/editable-title-description/plan.md`, `## Assumptions`:

> **Пошукові результати** (`retrieval.search`, `rag` sources, `/api/memory/search`, MCP
> `search_archive`/`ask_archive`) несуть `display_name` і `title` ПОРУЧ із `source_name`; ключ
> `source_name` не перезаписується (провенанс, контракт S2/Хвилі A не змінюється).

From the same plan.md, `## Tradeoffs`:

> **`display_name` поруч із `source_name`** проти «`COALESCE` у `source_name` на виході API»:
> друге — одна SQL-правка на споживача, але ламає провенанс у JSON-експорті, MCP і S2-контракті;
> ціна першого — кожен читач фронта переходить на нове поле (05).

From `docs/specs/editable-title-description/brief.md`, `## Answers`, item 1 (the owner's chosen
option, recommended):

> **Зберігання** — Нові поля title + description (Рекомендую): Дві нові колонки; source_name
> лишається як «походження» (оригінальне імʼя файлу). Скрізь показується title, якщо він є,
> інакше source_name. Так Telegram-слухач при повторному збереженні відредагованого повідомлення
> не затре вашу назву (він переписує саме source_name), а оригінал файлу не губиться.

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
exports, comment chips (`target_name`), and SPA list/detail views (story 05, `## Tradeoffs`: "ціна
першого — кожен читач фронта переходить на нове поле"). In exchange, provenance stays intact: a
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
