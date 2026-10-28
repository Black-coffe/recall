# blueprints-media — transcription / audio_library / youtube / recording (`app/blueprints/`)

Part of the HTTP layer; overview and the other blueprints — `blueprints.md`, memory/telegram/research — `blueprints-memory.md`.
No `url_prefix`, errors `{success,error}` + code. Logic — `services.md`.

last-verified: 2026-10-28 (wave2-recording-calls 4.36.0: recording.py gain/participants/403 bullets only); 2026-10-25 (live-video routes, 4.30.0); 2026-10-21 (mcp-pipeline 4.27.0: transcription.py/audio_library.py/youtube.py re-read, ALL transcription.py line numbers re-swept after the extraction into `transcription_run.py`/`transcript_ai.py`); recording.py section content from 2026-10-20 (its line numbers were dropped, not re-swept); i18n keys `capture.*`/`transcript.*`/`library.*` (`blueprints.md` § i18n)

Gated (`approval_gate._D_ROUTES`, runs before the view): `transcription.delete_transcription`, `bulk_delete_transcriptions`,
`audio_library.delete_audio_download`. Conditional (non-UI only): `transcription.retranscribe_transcription` (would evict a version),
`transcription.analyze_video_endpoint` (4.27.0, Claude description of frames, action `video_describe_claude`). `merge_segments` is exempt. See `services-infra.md`.

## transcription.py (~2230 lines) — `/api/transcribe*`, `/api/history/*`, `/api/transcription/<id>/*`, `/api/export/*`, `/api/video/search`
- `POST /api/transcribe` (:154, multipart; file|youtube|recording): since 4.27.0 a thin adapter (`resolve_*_source` :58-199 build `TranscribeParams`, then `transcription_run.resolve` + `run` inline);
  same responses, no recording guard. Optional `title`/`description` (`record_meta`, too long -> 400 BEFORE work). K1 vocabulary lives in `transcription_run.py:358`.
- `POST /api/transcribe/async` (:211, JSON `{source: library|recording|youtube|path, audio_download_id|recording_session_id|url|path (`_ASYNC_REF_FIELD`), model?, language?, diarize?, title?, description?, category_id?}`):
  `transcription_run.submit` -> 202 `{success, job_id, created:true}`, 200 `created:false` for a live duplicate. Refusals via `TranscribeError.async_body()`: 409 `RECORDING_ACTIVE` (non-UI only)/`NOT_FINALIZED`
  (+`session_id`)/`ALREADY_RUNNING`, 404 `NOT_FOUND`, 400 `INVALID_ARGUMENT`/`UNSUPPORTED_EXTENSION`/`MODEL_NOT_INSTALLED`, 403 `PATH_NOT_ALLOWED`, 413 `FILE_TOO_LARGE`, 503 `UNAVAILABLE`.
  `GET /api/transcribe/active` (:265) also lists async library jobs (registry entry carries `job_id`).
- History: `GET /api/history` (:279; `q` also by title/description via FTS5; `category_id`/`tags`/`since`/`until`; items carry `title/description/display_name`, `tags`, `has_media`),
  `GET /api/history/<id>` (:454), `PATCH` (:607; `{title?, description?}`, 400 on bad/too long, 404; non-UI -> `JOB_ACTIVE` while a job runs on the record, stamps `meta_source='mcp'`; then `record_meta.after_meta_update`),
  `POST <id>/suggest-meta` (:678), `DELETE` (:700, soft; rows go in `retention.purge_soft_deleted`), `POST <id>/restore` (:731), `bulk_delete` (:753, also deletes `transcript_versions`), `bulk_export` (:803).
- Re-transcribe + versions (4.25.0, ADR-044): `POST <id>/retranscribe` (:523; `{model, language?, diarize?}` -> 202 `{job_id, transcription_id, model}`; errors 404 `NO_MEDIA`/`NOT_FOUND`,
  400 `MODEL_NOT_INSTALLED`, 409 `RECORDING_ACTIVE`/`ALREADY_RUNNING`, 503 `UNAVAILABLE`; conditionally gated, else job fails `EVICTION_NOT_APPROVED`), `GET .../versions` (:555), `.../versions/<vid>` (:567), `POST .../versions/<vid>/activate` (:581; 409 `ALREADY_RUNNING` while a job runs).
- `/api/transcription/<id>/`: `audio` (:1044), `segments/merge` (:1098), `segments/split` (:1159). Claude actions (4.27.0: thin wrappers, the bodies are `transcript_ai.run`, keeping `{force, model}` and `cached`):
  `polish` :1298, `summarize` :1309, `sentiment` :1318, `translate` :1324, `topics` :1335.
  `POST .../ai-job` (:1344; body only `{kind, target_lang?}`, extra field -> 400 via `_async_bad`; cache hit -> 200 `{success, cached:true, status:"done", result}` with no job; else 202 `{cached:false, job_id}` or 200 `created:false`;
  `enrich` during a recording -> 409 `RECORDING_ACTIVE`; `transcript_ai.TranscriptAIError` -> `e.body()`/`e.status`).
- `POST /api/export/<format>` (:1458): txt, srt, md, docx, json, pdf. Name/title — `display_name`.
- Video (4.10.0): `POST <tid>/analyze-video` (:1876) JSON `{force, method thumbs|bisect, step, min_res, threshold, settle, ignore_regions, max_frames, ocr=true, describe=false, vision_backend local|claude}` -> job `video_analysis`
  (`{job_id}` | `{ready:true}` | `already_running` | 200 `{success:false,error_code:NO_VIDEO}`); `vision_backend` without `describe` = a description request. 4.27.0: a non-UI request that would describe through Claude is gated.
  Reads: `recording-info` :1954, `video` :2001, `keyframes` :2030, `keyframe/<kf_id>` :2062, `frame` :2124 (secrets painted over), `frames` :2136 (<=12), `visual-timeline` :2156, `frames-for-text` :2182, `GET /api/video/search` :2200.

## audio_library.py (~520) — `/api/audio/*`
`POST /download` (:55), `GET /downloads` (:119), `DELETE /downloads/<id>` (:331), `PATCH` (:358), `POST /downloads/<id>/restore` (:431), `POST check-duplicate` (:450), `open-explorer/<id>` (:484), `play/<id>` (:513).
- 4.27.0: `force` in the `/download` body counts only for a `ui` caller (`audio_library.py:82`); an agent's repeat answers `exists` for the stored row.
- `GET /downloads`: filters `category_id`/`tags`/`since`/`until`; category = `COALESCE(transcript.category_id, audio_downloads.category_id)`; source_type youtube|recording|file; items carry latest-transcript tags and `description`.
- `PATCH /downloads/<id>` `{title?, description?|null}`: no keys -> 400, empty `title` -> 400, no record -> 404, 200 `{success, item{id,title,description}}`.

## youtube.py (~130) — `/api/youtube/*`
`POST /info` (:25; 4.27.0: `get_json(silent=True)`, a missing/non-object body -> 400 `INVALID_ARGUMENT`), `POST /download` (:84, background, rate-limited), `GET /progress/<download_id>` (:129). Depends on: youtube_pytubefix.

## recording.py (~1070) — `/api/recording/*`, `/api/recordings/*`
Live mic/loopback recording + finalize + recovery; up to `RECORDING_MAX_ACTIVE` parallel sessions (4.7.0, ADR-026). Details and line numbers — `recording.md`.
- `GET /screens`, `/devices` (5s cache), `POST /start` (-> `_start_session`; device + `system_source {mode,apps}` + language + copilot), `<sid>/pause|resume|stop|save|discard|state|live-transcript`, `stream` (SSE 10 Hz).
- `POST <sid>/system-source {mode,apps?,reason?: user|auto_restore}`: 409 `NO_SYSTEM_STREAM`, 400 `RETARGET_FAILED`/`INVALID_REASON`. `GET /apps` never 500 -> `available:false`.
- Live video (4.30.0, `recording.py:1080-1268`, GET, service `live_frames`): `<sid>/live-video` (monitors, fps, `readable_until`), `<sid>/live-frame?t=&track=&size=thumb|full`
  (image/jpeg + `X-Recall-Frame-T|Video-T`, no-store), `<sid>/live-frames` (`t` alone = one frame, else `start|cursor,end,mode,step,min_res,limit 1..12`; JSON frames with `image_b64`+`text`).
  Errors via `_live_video_error` + `error_codes.status_for`: 400 `INVALID_ARGUMENT`, 404 `NOT_FOUND|NO_VIDEO|NOT_RECORDED`, 409 `PAST_LIVE_EDGE`; not live -> 200 `available:false` + `reason_code`. i18n `capture.live_video.*`.
  No private/stop-word filter here (the MCP tools apply it).
- 4.36.0: `POST <sid>/gain {mic_db?, system_db?}` (+`/start` `gain`; out of +-20 or non-number 400 `INVALID_ARGUMENT`; `<sid>/state` carries `gain`), `GET /api/recording/participants[?q=]` -> `{suggested, matches}` (+`/start` `participants: [int]`),
  `DeviceAccessDeniedError` -> 403 `DEVICE_ACCESS_DENIED` + `stream`. The call prompt `record` action runs `start_call_recording` (default mic + all system sound), not the app's own sound. Details — `recording.md`.
- `GET /api/recordings/recovered[?ack=0|1]` (ack=0 reads without clearing; the shell acks after showing), `/recordings/active` -> `{active,count,sessions[...],session_id,state}` (`session_id`/`state` only with exactly one session).
- Autosave (4.23.0): `POST <sid>/autosave/hold` (name/description/category_id; no scheduler -> `status:'off'`; unknown sid 404), `GET /api/recordings/uncategorized` (banner feed), `<sid>/state` carries `autosave`;
  `save` marks `autosave.saved` and on the first save calls `sched.after_save`; `discard` -> 409 `RECORDING_KEPT` when a transcript exists or autosave is running/done.
- `POST <sid>/save` JSON `{name, description?}` -> manifest -> `register_recording()` -> `audio_downloads.description`.
- Gotchas: over the limit -> 409 `SESSION_LIMIT`; bad spec -> 400 `UNSUPPORTED_SOURCE_SPEC`; copilot busy -> start 200 with `copilot.error_code='COPILOT_BUSY'`; finalize is async (>5 min), save deadline 5 min.
- K2 autoprompt (4.9.0; watcher = `current_app.extensions['call_watcher']`): `GET /autoprompt`, `POST /autoprompt/<prompt_id>/dismiss`, `GET /autoprompt/<token>/<action>` (record|sources|stop|keep; one-time token, mutation via GET is deliberate).
