---
domain: transcript-versions
tags: [transcription, whisper, versions, adr-044]
related: [adr/044-transcript-versions.md, rag-context-integrity.md, recording-sources-and-sessions.md, approval-gate.md]
last-verified: 2026-10-20
---

# Transcript versions: the canonical columns stay the only transcript

A record can be re-transcribed with another installed Whisper model; up to 3 versions are kept in `transcript_versions`.
Code: `app/services/transcript_versions.py` (storage), `app/services/retranscribe.py` (job), routes in
`app/blueprints/transcription.py` (`/api/history/<id>/retranscribe|versions`), migration v54. Decision: ADR-044.

## Invariants
- `transcriptions.transcript_text / segments / model_used / language / polished_*` are the only transcript any consumer reads (FTS, chunks, MCP,
  comments, export). A version row is read only by the version service and the versions API.
- The active version's row is a stale snapshot between switches: `write_back` refreshes it only when the owner switches away or a re-run starts.
  `list_versions` and `get_version` therefore answer the active id from the canonical columns and the live speaker map.
- `active_version_id IS NULL` = one implicit version; the `is_original` row is created lazily by the first re-run, never by the migration.
- Segment edits and polish write the canonical columns in place and never create a version; polished text travels with its version.
- Max 3 rows per record. `evict` drops the oldest by `(created_at, id)`, never the active row and never the main version the owner had before
  the run (`protect`); it runs only inside the transaction that inserted a version, never on request.
- `transcription_speaker_map` always describes the active version; a switch replaces its rows from `speaker_map_json`, and a speaker that no
  longer exists comes back unnamed.
- A new version is written in one transaction (`write_back -> insert_version -> activate -> evict`); a failed job leaves no row and no pointer change.
  Re-embed (`schedule_record_reembed`) is scheduled after commit, for search and RAG only; summary, tasks, entities, comments, `content_hash`
  and `duplicate_of` stay as they were.
- Refusals: `NO_MEDIA` (file_path empty or not a file), `MODEL_NOT_INSTALLED`, `RECORDING_ACTIVE` (at submit and again at job start),
  `ALREADY_RUNNING` (one job per record, locked dict in `retranscribe.py`; also blocks activate). The job kind `retranscribe` is in `SLOW_KINDS`.
- Deleting a record removes its version rows by hand (no cascades): `bulk_delete` directly, a soft delete through `retention.purge_soft_deleted`.
- `retranscribe` and `activate_transcript_version` are MCP W tools. 4.26.0: for a non-`ui` caller a re-run that would drop a version is a conditional D action (`transcript_version_evict`, `approval_gate._CONDITIONAL_D_ROUTES`): native dialog first; the job passes `allow_evict` and fails `EVICTION_NOT_APPROVED` with nothing written if eviction was not approved. The owner's UI is never gated. A switch is still undone by switching back.
