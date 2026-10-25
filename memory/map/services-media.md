# services — media, transcription, dedup

last-verified: 2026-10-21 (mcp-pipeline 4.27.0 section re-read); 2026-10-20 (retranscribe/transcript_versions section and the `retention.py:85-88` cascade); rest 2026-10-16
Part of `app/services/*`; hub — [services.md](services.md).

## Transcription / media
- **transcription_service.py** — `ModernTranscriptionService`: `transcribe_file()`, `get_available_models()`,
  `get_system_stats()`; a wrapper over whisper_manager. *GPU lock during transcription.*
- **youtube_pytubefix.py** — `download_youtube_audio()` (DI: progress/log/db), quality→bitrate, error classifier, ffmpeg-post.
- **diarization_service.py** — pyannote: `diarize_audio()`, `diarize_recording()` (mic→"self", system→full),
  `assign_speakers_to_whisper_segments()`. *Model ~1.5GB lazy; HF_TOKEN required; AAC/M4A/MP4 are auto-converted
  to WAV. `SELF_LABEL='self'`, `UNKNOWN='SPEAKER_UNKNOWN'`.*
- **live_transcribe.py** — `LiveTranscribeWorker(..., db_path=None)`: reads the PCM tail of the active session, small model,
  segments over SSE (~8 s, mic-only, in memory; the final pass is still needed). `_decode_options()` (`:121`) calls
  `vocabulary.decode_options_for('live', db_path)`; `multilingual` is not given to live.

## Pipeline services (4.27.0, mcp-pipeline; no migration; plan `docs/specs/mcp-pipeline/plan.md`)
- **transcription_run.py** — the pipeline both transcribe routes call. `TranscribeParams` (:75; `source` file|library|recording|youtube|path, `non_ui`, `actor`),
  `resolve(params) -> (filepath, source_name, meta)` :251 (`_resolve_path` :225 = `import_roots.resolve_allowed` + extension + `max_bytes`; recording not finalized -> `NOT_FINALIZED`),
  `run(params, resolved, *, job=None)` :287 (the old sync body; with a job sets `meta.stage/progress`, checks `is_cancelled()` between stages),
  `submit(params)` :683 -> `{job_id, created}` (`ASYNC_SOURCES` = library|recording|youtube|path; order: non-ui + `recording_active()` -> `RECORDING_ACTIVE`; queue -> 503;
  model not downloaded -> `MODEL_NOT_INSTALLED`; `_inflight[(source, ref)]` dedupe -> `created:false`; busy library row -> `ALREADY_RUNNING`), job kind `transcription` (batch executor),
  `_job` :738 re-checks the recording, result = `{transcription_id, source_type, source_name, language, model_used, processing_time, duplicate_of}`.
  `TranscribeError(code, status, message, sync_status=, payload=, sync_code=)` keeps the sync route's old statuses (`sync_body()`/`async_body()`).
- **transcript_ai.py** — `KINDS` polish|summarize|sentiment|translate|topics|enrich, `TARGET_LANGS` en|uk|ru; `cached(kind, tid, target_lang=, db_path)` (stored result, spends nothing),
  `run(kind, tid, ...)` :197 (bodies of the five Claude routes + enrichment), `submit()` :513 (cache hit -> `{cached:True}` no job; `enrich` while recording -> `RECORDING_ACTIVE`;
  missing key refused; live job for `(tid, kind, target_lang)` joined), job kind `transcript_ai` (batch executor). `TranscriptAIError(code, status, msg)`.
- **model_download.py** — `submit(name, actor=)` -> `{job_id, created}` (dedupe `find_active("model_download", model=name)`; unknown -> `UNKNOWN_MODEL`); faster backend fetches with
  `faster_whisper.utils.download_model(whisper_manager_new.faster_model_repo(name), cache_dir=faster_cache_dir())` (`whisper_manager_new.py:91/:85`) WITHOUT `_load_lock`;
  openai backend still `whisper_manager.download_model` (:589, under the lock). Slow executor.
- **diagnostics.py** — `build_package() -> bytes` (the zip moved out of `system.py`), `write_package(dest_dir="logs/diagnostics", keep=5)` (`<ts>.zip`, prunes the oldest), `DIAGNOSTIC_LOG_TAIL_LINES=500`.
- **import_roots.py** (stdlib) — `roots()` (`RECALL_IMPORT_ROOTS`, read per call), `resolve_allowed(path)` (raises `PathNotAllowed`; prefix siblings refused); `documents._import_roots()` delegates.

## Re-transcribe and versions (4.25.0, ADR-044, wiki `transcript-versions.md`)
- **transcript_versions.py** (sqlite3 + stdlib, no Flask; caller owns the commit) — snapshots behind the canonical `transcriptions` columns;
  `transcriptions.active_version_id` NULL = one implicit version (migration v54, `migrations.py:2206`). `write_back(conn, tid)` (refreshes the active row from the
  canonical columns + live speaker map; creates the `is_original` row lazily when the pointer is NULL), `insert_version`, `activate(conn, tid, vid)`
  (no-op when already active; else write_back, copy `_COPY_COLS` + replace `transcription_speaker_map` from `speaker_map_json`, a gone speaker -> unnamed),
  `evict(conn, tid, keep=3, protect=())` (oldest by `(created_at,id)`, never the active or a `protect` id), `list_versions`, `get_version` (active id reads the
  canonical columns, never the stale row), `delete_for`. Invariant: the active row is stale between switches, never read it as content.
- **retranscribe.py** — `submit(tid, *, model, language, diarize, db_path)` validates (order: queue -> NOT_FOUND -> NO_MEDIA -> MODEL_NOT_INSTALLED ->
  RECORDING_ACTIVE -> ALREADY_RUNNING) and queues a `retranscribe` job; `_inflight` dict (locked) = one job per record, `running_job_id(tid)`;
  `RetranscribeError(code, **extra)`. `_run` re-checks recording/media, transcribes with `vocabulary.decode_options_for`, diarizes when
  `source_type=='recording'` or `diarize is True` or the old segments had speakers, then ONE transaction `write_back -> insert_version -> activate ->
  evict(protect=(previous_main,))`, and `reembed.schedule_record_reembed` after commit. Job `meta`: `stage` queued|transcribing|diarizing|saving|done, `progress`.
  A failure rolls back: no row, no pointer change.

## vocabulary.py (K1, only reads the graph `mode=ro`, no torch)
A dictionary of "hard" names for faster-whisper.
- `build_hard_names(db_path, limit=60)` → person/project/org names (meeting rank × error aliases × boost; common words
  filtered out); `hard_names()` — cache TTL 3600 s per DB path (graph failure → `[]`, also cached); `clear_cache()`.
- `phonetic_keys`/`same_name`/`is_error_alias`: a case form, translation, transliteration uk/ru/en is NOT an error.
- `decode_options_for(source_kind, db_path)` (`file|recording|telegram|live|youtube`, otherwise `ValueError`): youtube → `{}`;
  otherwise `{<mode>: names}` + `multilingual: True` (not for live).
- Env: `RECALL_VOCAB_MODE` (`off|hotwords|initial_prompt`, default `hotwords`), `WHISPER_MULTILINGUAL` (default `1`).
- CLI `python -m app.services.vocabulary show [--db]` / `check-error-pairs <json>`. Callers: `transcription_run.py` (kind youtube|recording|other→file), `blueprints/telegram.py`.
  Token truncation is done by `whisper_manager_new._decode_kwargs`, not this module. Measurement: `tools/measure_vocabulary.py`.

- **retention.py** `purge_soft_deleted` (:66-90) now also deletes the record's `transcript_versions` rows (:85) beside the speaker map, before the transcription row.

4.26.0 provenance (migration v55, `migrations.py:2234`, TEXT columns, NULL = owner, `'mcp'` = agent): `comments.confirmed_at` (agent comment boost 0.0 until confirmed: `comments.kind_weight` :150; `retrieval` skips unconfirmed agent comments in attached comments :1050), `transcription_speaker_map.source`, `categories.created_source` (`categories.create_category(created_source=)` :583), `transcriptions.meta_source` (`record_meta.update_meta(meta_source=)` :87, written only on a real change). `commitments.apply_action(dedupe=)` :1425 / `undo_action(only_source=)` :1458. `retranscribe.submit(allow_evict=)`, `precheck`, `would_evict` :105; `transcript_versions.evict_candidate` :178.

## Dedup and repair
- **dedup_audio.py** — `hash_for(source_type, text)` (sha256 of the normalized text; only `file|youtube|recording`),
  `find_original(conn, hash, exclude_id=None)`, `mark_duplicates(db_path, dry_run=False)` (writes by default; CLI
  `python -m app.services.dedup_audio mark --dry-run`).
  *A duplicate is NOT deleted: `transcriptions.duplicate_of` (v40, `migrations.py:1591`) → the original; chunks/embeddings are not built for
  a duplicate; ingest sets `duplicate_of` before the INSERT and the enrichment status `skipped_duplicate`. `retrieval.search`
  (vector and FTS) filters `duplicate_of IS NULL`; `enrichment` skips such records with an early exit.*
- **telegram_link_repair.py** — `clear_bogus_private_links(db_path, dry_run=False)`: clears the `tg_link` invented by the old
  `_chat_link` for private chats (`tg_chat_id > 0`); leaves the rows. CLI `... clear-bogus-links --dry-run`.

## Record title and description
- **record_meta.py** (stdlib + `app.db.connection` — safe from the stdio-MCP) — `TITLE_MAX=200`, `DESCRIPTION_MAX=4000`;
  `normalize_title/description` (empty→`None`; a non-string or too long→`ValueError`), `display_name(row)` (`title` →
  `source_name` → "Record #id" via `tr('svc_embed.meta.record_fallback')` (`ValueError` texts are `svc_embed.meta.*` too)), `update_meta(conn, tid, *, title=UNSET, description=UNSET)` (`changed=False` → no UPDATE),
  `after_meta_update(tid, *, changed, db_path)` → lazy `reembed.schedule_record_reembed`.
  `source_name` is provenance, NOT overwritten.
  Consumers: `PATCH /api/history/<id>`, `PATCH /api/audio/downloads/<id>`, `/api/transcribe`, `/api/documents/upload`,
  `embeddings._load_prefix_meta`, `mcp_server.py`, comments (`target_name`).
