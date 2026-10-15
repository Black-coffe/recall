# ADR-012: Audio/YouTube dedup hashes normalized-but-not-casefolded text; duplicates keep their row and stay out of search and enrichment

- Status: proposed
- Date: 2026-09-16
- Spec: docs/specs/production-rag-wave-a

## Context

From `docs/specs/production-rag-wave-a/plan.md`, `## Assumptions`:

> Dedup at ingest — for `file`/`youtube`/`recording`; we do not touch `telegram` and `document`
> (TG has its own key chat_id+msg_id, documents are already deduplicated). The hash is the same
> `document_parser.content_hash` over the normalized `transcript_text`.
>
> A duplicate at ingest is NOT rejected (the audio is already transcribed, the money is spent): the record
> is saved with `duplicate_of` and immediately kept out of search. Chunks/embeddings of the duplicate are not built.

And from story `production-rag-wave-a-02` (`## Implementation notes`):

> `document_parser.content_hash()` hashes the RAW text — so for audio it is called as
> `content_hash(_normalize_text(text))`.

And from story `production-rag-wave-a-07` (`## Goal`, repair round item #3), extending the
same invariant to enrichment:

> No indexing or enrichment path creates chunks, embeddings or a paid Claude card
> for a record with `duplicate_of IS NOT NULL`.

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
survive, visible in the Library as "duplicate #N". The known accepted debt: if the *original*
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
