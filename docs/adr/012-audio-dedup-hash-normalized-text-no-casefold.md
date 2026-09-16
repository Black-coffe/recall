# ADR-012: Audio/YouTube dedup hashes normalized-but-not-casefolded text; duplicates keep their row and stay out of search and enrichment

- Status: proposed
- Date: 2026-09-16
- Spec: docs/specs/production-rag-wave-a

## Context

From `docs/specs/production-rag-wave-a/plan.md`, `## Assumptions`:

> Дедуп на інжесті — для `file`/`youtube`/`recording`; `telegram` і `document` не чіпаємо
> (у TG свій ключ chat_id+msg_id, документи вже дедуплюються). Хеш — той самий
> `document_parser.content_hash` над нормалізованим `transcript_text`.
>
> Дубль на інжесті НЕ відхиляється (аудіо вже транскрибовано, гроші витрачені): запис
> зберігається з `duplicate_of` і одразу поза пошуком. Чанки/ембеддинги дубля не будуються.

And from story `production-rag-wave-a-02` (`## Implementation notes`):

> `document_parser.content_hash()` хешує СИРИЙ текст — тому для аудіо він викликається як
> `content_hash(_normalize_text(text))`.

And from story `production-rag-wave-a-07` (`## Goal`, repair round item #3), extending the
same invariant to enrichment:

> Жоден шлях індексації чи збагачення не створює чанків, ембеддингів або платної картки
> Claude для запису з `duplicate_of IS NOT NULL`.

Two properties of this design are easy to re-litigate by accident in a future story: that
the hash is computed over `_normalize_text(text)` (not raw text, and not casefolded on top
of that), and that a detected duplicate is *never* rejected or deleted at ingest time -
only marked and excluded downstream.

## Options

1. Reject the duplicate outright at ingest (do not insert a second row) - rejected: audio
   has already been transcribed at real cost by the time the hash is known; discarding the
   row throws away money already spent and any metadata unique to the second upload.
2. Fold case in the hash (`casefold()` on top of `_normalize_text`) - rejected: this is a
   *document* dedup concern, not shared here; `_normalize_text` is reused as-is from
   `document_parser` specifically so casing decisions for documents are not silently
   changed by a change made for audio.
3. Insert the duplicate row, set `duplicate_of`, and exclude it from search and enrichment
   downstream (both `retrieval.search` and `enrichment.list_unenriched_ids`/`backfill`/
   `enrich_transcription`). Chosen.

## Decision

`hash_for()` computes `content_hash(_normalize_text(text))` for `file`/`youtube`/
`recording` transcripts only, reusing `document_parser`'s existing normalization and hash
function unchanged (no casefold added). On a hash match, the new record is inserted with
`duplicate_of = <original id>` (original = lowest id in the group) and is never rejected.
`retrieval.search` filters `duplicate_of IS NULL` alongside `deleted_at IS NULL` in every
branch (vector, FTS, chunk hydration). `enrichment.list_unenriched_ids`, `backfill(force=
True)`, and `enrich_transcription()` all add the same filter/early-exit
(`status="skipped_duplicate"`), so a duplicate never gets chunks, embeddings, or a paid
Claude enrichment card. `telegram` and `document` sources are untouched (they already have
their own dedup keys).

## Consequences

A duplicate audio/YouTube upload costs nothing extra after the first transcription (no
enrichment call, no embeddings) but is not silently lost - the row and its metadata
survive, visible in the Library as "дубль #N". The known accepted debt: if the *original*
of a duplicate group is later deleted, the duplicate does not get re-promoted to take its
place - it stays excluded from search until a human intervenes. `grep_archive` and other
surfaces outside `retrieval.search` do not filter duplicates in this wave.

## Invariants created

Any code path that builds chunks, embeddings, or an enrichment card for a transcription
row must check `duplicate_of IS NULL` first. Any code path that returns search results
must exclude `duplicate_of IS NOT NULL` alongside `deleted_at IS NOT NULL`. The dedup hash
for audio/YouTube/recording is computed over `_normalize_text(text)`, not raw text and not
casefolded text - a proposal to change document dedup's normalization must not silently
change what audio dedup considers identical, and vice versa.

## Revisit when

A future wave (Wave D, named in the plan as deferred) wants near-duplicate detection
(fuzzy match, not exact hash) or wants the original's deletion to re-promote a surviving
duplicate - both are explicitly out of scope here and were deferred by the owner.
