# services — media, transcription, dedup

last-verified: 2026-10-16

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

## vocabulary.py (K1, only reads the graph `mode=ro`, no torch)
A dictionary of "hard" names for faster-whisper.
- `build_hard_names(db_path, limit=60)` → person/project/org names (meeting rank × error aliases × boost; common words
  filtered out); `hard_names()` — cache TTL 3600 s per DB path (graph failure → `[]`, also cached); `clear_cache()`.
- `phonetic_keys`/`same_name`/`is_error_alias`: a case form, translation, transliteration uk/ru/en is NOT an error.
- `decode_options_for(source_kind, db_path)` (`file|recording|telegram|live|youtube`, otherwise `ValueError`): youtube → `{}`;
  otherwise `{<mode>: names}` + `multilingual: True` (not for live).
- Env: `RECALL_VOCAB_MODE` (`off|hotwords|initial_prompt`, default `hotwords`), `WHISPER_MULTILINGUAL` (default `1`).
- CLI `python -m app.services.vocabulary show [--db]` / `check-error-pairs <json>` (exit 1 if a translation is among the "errors").
- Calls: `blueprints/transcription.py:332` (vocab_kind: youtube|recording|other→file), `blueprints/telegram.py:576`.
  Token truncation is done by `whisper_manager_new._decode_kwargs`, not this module.
- `tools/measure_vocabulary.py` — measurement: `candidates`, `run` (refuses during a live recording), `ab`, `score`.

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
