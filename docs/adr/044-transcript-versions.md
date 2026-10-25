# ADR-044: Transcript versions: a snapshot table behind the canonical columns

- Status: proposed
- Date: 2026-10-19
- Spec: docs/specs/retranscribe-versions

## Context
The owner wants any record that still has its media re-transcribed by any locally installed Whisper model, from
the SPA (library, media library) and from the MCP; up to three versions kept per record, each labelled with its
model; the newest becomes the main one at once; any kept version can be made main again; two versions can be
compared side by side; on a switch only search and RAG are rebuilt, while summary, tasks and entities stay and
polished text is per version (brief, answers 1-5).

Today there is exactly one transcript per record and every consumer reads it from the same place:
`transcriptions.transcript_text / segments / model_used / language / polished_*`. The FTS5 triggers fire on those
columns; `embeddings.chunk_and_embed_transcription` (embeddings.py:949) reads `polished_text or transcript_text`,
`segments` and the record's `transcription_speaker_map` rows; the MCP, the comments feed (anchored by
`anchor_time` seconds, not by segment id), the polish/translate/export routes and the SPA transcript page all read
the same columns. `transcription_speaker_map` is keyed by `(transcription_id, raw_label)` and is what turns
`SPEAKER_00` into a name everywhere (transcription.py:1516 `_load_speaker_context`, speakers.py throughout).
Segment edits (`_save_segments`, transcription.py:1320) and polish (:1602) write the canonical columns in place.
`/api/transcribe` runs Whisper synchronously inside the request (:331), which an MCP call cannot wait for
(`_api` times out at 60 s), and nothing in that route refuses work while a live recording holds the GPU.

Cascades are manual in this schema: `bulk_delete` (transcription.py:1020) deletes `transcription_speaker_map`
rows by hand, so a new child table must be deleted by hand too.

The decision needed: where versions live and how the "main" one relates to the canonical columns, so that none
of the consumers above has to learn about versions.

## Options
1. **Snapshot table for every version, canonical columns mirror the active one** (`transcript_versions`, pointer
   `transcriptions.active_version_id`, original row created lazily on the first re-transcribe). Consumers stay
   untouched; the cost is one write-back rule on switch so the active row does not go stale.
2. **Archive table for non-active versions only**, the active one lives solely in `transcriptions`. No pointer and
   no write-back, but a version loses its id every time it is swapped (the previous active is re-inserted), the
   active version has no `created_at` of its own without a new column on `transcriptions`, and the compare and
   "return this one" links cannot be stable.
3. **Version-scoped canonical columns** (every reader joins the versions table for the active row). Rejected
   without a spike: it rewrites every consumer listed above and the FTS triggers for no benefit the owner asked for.
4. **Eager backfill** of option 1 (a migration snapshots every existing record). Rejected: it copies `segments`
   JSON for the whole archive, most of which has no media and can never be re-transcribed (Telegram text, documents).

## Decision
Option 1, lazy. The deciding factor is that the canonical columns remain the only thing the archive reads, so
FTS, chunks, MCP, comments, speakers, polish, export and the SPA need no change; the versions table is a
side store that only the version service touches, and undoing it is a `DROP TABLE` plus one unused column.

### Storage (migration v54, additive)
```
CREATE TABLE IF NOT EXISTS transcript_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    transcription_id INTEGER NOT NULL,
    created_at       TEXT NOT NULL,          -- original: transcriptions.created_at; others: time of the re-run
    is_original      INTEGER NOT NULL DEFAULT 0,
    model_used       TEXT,
    language         TEXT,
    processing_time  REAL,
    transcript_text  TEXT,
    segments         TEXT,                   -- JSON, same shape as transcriptions.segments
    polished_text    TEXT,
    polished_at      TEXT,
    polished_model   TEXT,
    speaker_map_json TEXT                    -- JSON [{raw_label, speaker_id, embedding: [float]|null}]
);
CREATE INDEX IF NOT EXISTS idx_transcript_versions_tx ON transcript_versions(transcription_id, created_at);
ALTER TABLE transcriptions ADD COLUMN active_version_id INTEGER;   -- NULL = single implicit version
```
`active_version_id IS NULL` means the record has never been re-transcribed: the canonical columns are the only
copy and the versions API reports an empty list. The original row is inserted the first time a record is
re-transcribed, with `created_at = transcriptions.created_at`, `is_original = 1` and the record's speaker map
rows as `speaker_map_json`.

### One service, three operations (`app/services/transcript_versions.py`, no Flask import)
- `write_back(conn, tid)`: if the pointer is NULL, insert the original row from the canonical columns and set
  the pointer; otherwise copy the canonical columns (`transcript_text, segments, model_used, language,
  processing_time, polished_*`) and the current `transcription_speaker_map` rows into the row the pointer names.
  This is the only place that refreshes the active row, so segment edits and polish keep writing the canonical
  columns exactly as today (answer to question 6: merge/split edit the active transcript in place, do not create a
  version, and reach the version row at the next switch-away).
- `activate(conn, tid, vid)`: `write_back`, then copy the target row's columns into `transcriptions`, replace the
  record's `transcription_speaker_map` rows from the target's `speaker_map_json` (a `speaker_id` is kept only if
  that `speakers` row still exists), set the pointer. The FTS triggers fire on the UPDATE as they do for
  `_save_segments`. The caller schedules `reembed.schedule_record_reembed(tid, db_path=...)` after commit;
  `chunk_and_embed_transcription(force=True)` overwrites the chunks from the new canonical text.
- `evict(conn, tid)`: while the record has more than 3 rows, delete the oldest by `(created_at, id)` whose id is
  not the pointer. Runs inside the same transaction as the insert of a new version, after `activate`, never on
  its own and never by request.

`delete_transcription` and `bulk_delete_transcriptions` delete the record's `transcript_versions` rows next to
the speaker-map rows (transcription.py:1020), because cascades are manual in this schema.

### Re-transcribe execution
A `job_queue` job of kind `"retranscribe"`, added to `SLOW_KINDS` (job_queue.py:44), so one runs at a time
on the slow executor and never holds the two shared workers. The synchronous `/api/transcribe` path is not
reused and not refactored: the job calls the same services it calls (`state.whisper_manager.transcribe_with_progress`
with `vocabulary.decode_options_for(kind, db)`, then `run_diarization_for_audio(..., return_embeddings=True)`)
and ends with one transaction: `write_back` -> insert the new row (`polished_* = NULL`, `speaker_map_json` from
the diarization result with fingerprint auto-match as at transcription.py:475-499) -> `activate` -> `evict` ->
commit -> `schedule_record_reembed`. A failed job leaves no row and no pointer change.

- `POST /api/history/<int:tid>/retranscribe`, body `{"model": str, "language"?: str, "diarize"?: bool}`,
  returns 202 `{"job_id", "transcription_id", "model"}`. Refusals: 404 `NO_MEDIA` when `file_path` is empty or
  not a file (documents, Telegram text, media deleted after extraction); 400 `MODEL_NOT_INSTALLED` when the model
  is not `downloaded: true` in `whisper_manager.get_available_models()`; 409 `RECORDING_ACTIVE` while
  `recording_active()` (the GPU and Whisper belong to the live call; the job re-checks at start and fails with the
  same code if a recording began while it waited); 409 `ALREADY_RUNNING` with the existing `job_id` when a
  retranscribe job for this record is in flight (one in flight per record, kept in a locked module dict cleared in
  the job's `finally`). Default `language` is the record's `language` column; a model is the only thing the owner
  chooses in the UI.
- Diarization on re-run: when `source_type == 'recording'`, or the active version's segments carry a `speaker`
  field, or the request says `diarize: true`. Labels are re-identified by voice fingerprint against `speakers`
  (the same auto-match the first run uses), so a named person usually keeps the name; an unmatched label shows
  as an unnamed speaker to rename, exactly as on a first transcription.
- Progress and completion: the SPA and the MCP poll `GET /api/jobs/<id>` (`get_job_status` / `wait_for`); job
  `meta` carries `transcription_id, model, stage, progress`; the result carries `version_id` and
  `evicted_version_ids`.

### Versions API and switch
- `GET /api/history/<int:tid>/versions` -> `{"active_version_id", "running_job_id", "versions": [{id, created_at,
  is_original, is_active, model_used, language, segment_count, has_polished, has_speakers}]}`; empty list while
  the pointer is NULL.
- `GET /api/history/<int:tid>/versions/<int:vid>` -> the row's content (`transcript_text, segments,
  polished_text, speaker_map`); for the active id the content comes from the canonical columns, never from the row.
- `POST /api/history/<int:tid>/versions/<int:vid>/activate` -> `activate` in one transaction, then
  `schedule_record_reembed`; returns `{"active_version_id", "reembed": "queued|deferred|pending|skipped"}`;
  409 `ALREADY_RUNNING` while a retranscribe job for the record is in flight. No recording refusal: it is a DB
  copy, and `schedule_record_reembed` already defers itself during a recording.
- Compare is client side: the transcript page loads two versions and aligns phrases by time in JS (answer 4). No
  server diff endpoint.

### What a switch touches and what it leaves alone
Touched: the canonical transcript columns, `model_used`, `language`, `processing_time`, `polished_*` (per
version), the speaker-map rows, FTS (by trigger), chunks and vectors (by re-embed). Left alone, per the owner:
`summary_*`, `action_items`, `meeting_entities`, `translations_json`, `topics_json`, `structure_json`,
`category_id`, comments (time anchored), the copilot timeline, the visual timeline. `content_hash` and
`duplicate_of` are left alone too: they describe the upload event (`find_original` must keep matching a later
upload of the same audio against the original text) and a duplicate stays unindexed whichever version is active.

### Approval gate
`retranscribe` and `activate` are W tools: both create or re-point data and a switch is undone by switching
back. Eviction is a physical delete, but it is the retention rule the owner stated (ask 4), it is automatic
inside a create and nothing requests it, so it is not a D action and raises no dialog; the route names contain
none of `delete|merge|purge|backfill`, so `tests/test_endpoints_smoke.py` needs no `_D_EXEMPT` entry. A manual
"delete a version" is not requested and is not added; if it ever is, it joins `_D_ROUTES`.

### MCP
Four proxy tools on `mcp_server.py`, all `_ABOUT_PROXY`: `list_whisper_models` (R, `GET /api/models` filtered
to `downloaded: true`), `retranscribe` (W, limits family `transcripts`, `paid: False`, `args_allow`
`("transcription_id", "model", "language", "diarize")`, returns `job_id` or `already_running` like
`video_analyze`), `transcript_versions` (R, list or one version by `version_id`), `activate_transcript_version`
(W). The owner's ask names the first two; the other two follow ADR-042 (the MCP reaches what the UI reaches) and
are the agent's only way to undo a re-run it started. Count guards move 51 -> 55 (46 read-only + 9 write);
`_READ_PROFILE_EXTRA` stays `{"video_analyze"}` (a re-run costs minutes of GPU, so it is not offered to the
read profile).

## Consequences
Easier: every existing reader keeps working unchanged; the feature is one service module, one migration, three
routes, one job kind, four proxy tools and SPA work; undo is `DROP TABLE transcript_versions` plus ignoring the
column. Only re-transcribed records grow (at most 3 copies of `segments`).

Harder / accepted debt:
- The active version's row is stale between a switch-away and the next; readers must resolve the active id from
  the canonical columns (one rule in `GET .../versions/<vid>`).
- A re-run shares the single slow executor with `video_analysis`, `youtube_download`, `doc_import` and the batch
  passes: a re-transcribe can wait behind a download. An executor of its own was not worth a second pool.
- A version's `speaker_map_json` can name a speaker that was later merged or deleted; on activation that label
  comes back unnamed and the owner renames it. Remapping through merge history was not built.
- `structure_json`, summaries and translations were produced from another version's text; the owner chose to keep
  them, so they can disagree with the active transcript until re-enriched by the existing backfill.
- Each new version is diarized afresh when the active one had speakers; the speaker-map table always mirrors the
  active version, so stats that count map rows follow the switch.

## Invariants created
1. `transcriptions.transcript_text / segments / model_used / language / polished_*` are the only transcript any
   consumer reads; a version row is read only by the version service and the versions API.
2. `transcript_versions` rows other than the active one are authoritative snapshots; the active row is refreshed
   only by `write_back` and must never be read as content: resolve `active_version_id` to the canonical columns.
3. `active_version_id IS NULL` means one implicit version; the original row is created lazily by the first
   re-transcribe with `created_at = transcriptions.created_at`, never by a migration.
4. Segment edits and polish write the canonical columns only; they do not create a version.
5. A record keeps at most 3 versions; eviction removes the oldest non-active row inside the transaction that
   inserted the new one and is never exposed as a request.
6. `transcription_speaker_map` always describes the active version; switching replaces its rows from the target's
   `speaker_map_json`, and a `speaker_id` is written only if the `speakers` row exists.
7. Every switch (new version or manual) schedules `reembed.schedule_record_reembed(tid, db_path=...)` after
   commit; summary, tasks, entities, translations, comments, `content_hash` and `duplicate_of` are not touched.
8. `retranscribe` is a `job_queue` kind in `SLOW_KINDS`; it refuses with 409 `RECORDING_ACTIVE` at submit and
   fails with it at start; one in flight per record (409 `ALREADY_RUNNING` with the live `job_id`).
9. Deleting a transcription deletes its `transcript_versions` rows by hand, next to the speaker-map rows.

## Revisit when
- The owner asks for more than 3 versions, a manual version delete, or per-version summaries/tasks: the mirror
  stops being cheap and option 3 (version-scoped readers) should be re-costed.
- A second consumer needs a version-scoped speaker map (e.g. speaker stats across versions): add
  `version_id` to `transcription_speaker_map` instead of growing `speaker_map_json`.
- Re-transcribe waits behind downloads often enough to be noticed: give it an executor of its own.
- `whisper_history.db` growth from `segments` copies becomes measurable: store a diff or compress the column.

## Amendment (4.26.0, 2026-10-20)
The "not a D action, no dialog" point no longer holds for agents: a non-UI retranscribe that would evict a kept version is gated by the Windows dialog (`approval_gate.py` conditional D routes, spec mcp-archive-org ask 9). UI behaviour is unchanged.
