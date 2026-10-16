# blueprints-media — transcription / audio_library / youtube / recording (`app/blueprints/`)

Part of the HTTP layer; overview and the other blueprints — `blueprints.md`, memory/telegram/research — `blueprints-memory.md`.
No `url_prefix`, errors `{success,error}` + code. Logic — `services.md`.

last-verified: 2026-10-15 (english-i18n 4.16.0: all `error`/`message` in jsonify of these four files — `tr()`, keys
`capture.*`/`transcript.*`/`library.*`, see `blueprints.md` § i18n; line numbers below are from before 4.16.0, check against the code)

## transcription.py (~2780 lines) — `/api/transcribe`, `/api/history/*`, `/api/transcription/<id>/*`, `/api/export/*`, `/api/video/search`
- `POST /api/transcribe` (:236, multipart; file|youtube|recording) and `GET /transcribe/active` (:620). Optional
  `title`/`description` (normalization by `record_meta`, longer than the ceiling → 400 BEFORE work; the form takes priority over
  the copy from `audio_downloads`). K1: `decode_options=vocabulary.decode_options_for(kind, DATABASE)` (:327-333;
  kind youtube|recording, the rest → `file`; youtube → `{}`).
- History: `GET /api/history` (:634; `q` searches also by title/description via FTS5 `transcriptions_fts`; `category_id`/
  `tags`/`since`/`until` via `categories`; an item carries `title/description/display_name`, `tags`, `suggested_tags`),
  `GET/PATCH/DELETE /api/history/<id>` (:804/862/960), `POST <id>/suggest-meta` (:937), `POST <id>/restore` (:991),
  `bulk_delete` (:1013), `bulk_export` (:1059).
- `PATCH /api/history/<id>` JSON `{title?, description?}`: no keys / not a string / longer than `TITLE_MAX`/`DESCRIPTION_MAX`
  → 400; no record → 404; 200 `{success, record{...,display_name}}`; then `record_meta.update_meta` →
  `after_meta_update(..., db_path=DATABASE)` (targeted re-embed, `services/reembed.py`).
- `/api/transcription/<id>/`: `audio` (:1298), `segments/merge` (:1352), `segments/split` (:1413), Claude:
  `polish` (:1559), `summarize` (:1638), `sentiment` (:1723), `translate` (:1776), `topics` (:1879).
- `POST /api/export/<format>` (:2024): txt, srt, md, docx, json, pdf. `bulk_export`: json, txt, docx, md.
  Name/title — `display_name`; a payload without `title/source_name/id` → a neutral fallback, not "Record #None".
- Video (`video-visual-timeline`, 4.10.0; :2443-2790): `POST <tid>/analyze-video` (:2443) JSON `{force, method
  thumbs|bisect, step, min_res, threshold, settle, ignore_regions, max_frames, ocr=true, describe=false,
  vision_backend local|claude}` → job `video_analysis` (`{job_id}` | `{ready:true}` | `already_running` | 200
  `{success:false,error_code:NO_VIDEO}` :2480); `vision_backend` without `describe` = a description request; a bad method/backend → 400.
  `GET <tid>/recording-info` (:2521), `video` (:2568, stream), `keyframes` (:2597), `keyframe/<kf_id>` (:2629, the frame
  is cut on the fly), `frame?t&size=thumb|full` (:2691, JPEG `no-store`, secrets painted over, header
  `X-Recall-Redacted-Boxes`), `frames` (:2703, ≤12, `include_camera`, base64 for MCP), `visual-timeline` (:2723),
  `frames-for-text` (:2749), `GET /api/video/search` (:2767).
  `TimelineError.code` → HTTP via `_TIMELINE_STATUS` (:2650): NOT_FOUND/NO_VIDEO 404, BAD_REQUEST 400, NOT_ANALYZED 409.

## audio_library.py (~520) — `/api/audio/*`
`POST /download` (:53), `GET /downloads` (:115), `DELETE /downloads/<id>` (:327), `PATCH` (:354), `POST
/downloads/<id>/restore` (:427), `POST check-duplicate` (:446), `open-explorer/<id>` (:480), `play/<id>` (:509).
- `GET /downloads`: filters `category_id`/`tags`/`since`/`until` (`categories`); category =
  `COALESCE(transcript.category_id, audio_downloads.category_id)`; source_type youtube|recording|file; an item
  carries the tags of the latest transcript and `description` (string|null; `audio.js` prefills the modal — without it PATCH would erase the description).
- `PATCH /downloads/<id>` `{title?, description?|null}`: no keys → 400, empty `title` → 400 (no fallback,
  unlike `transcriptions.title`), no record → 404, 200 `{success, item{id,title,description}}`.

## youtube.py (~130) — `/api/youtube/*`
`POST /info` (:24), `POST /download` (:80, background, rate-limited), `GET /progress/<download_id>` (:125). Depends on: youtube_pytubefix.

## recording.py (~1070) — `/api/recording/*`, `/api/recordings/*`
Live mic/loopback recording + finalize + recovery; up to `RECORDING_MAX_ACTIVE` parallel sessions (4.7.0, ADR-026).
- `GET /screens` (:137), `/screens/<monitor_index>/preview` (:168), `/devices` (:203, 5s cache), `POST /start` (:244 →
  `_start_session` :265; device + `system_source {mode,apps}` + language + copilot), `<sid>/pause` (:446), `resume`
  (:458), `stop` (:664), `save` (:696), `discard` (:811), `state` (:838), `live-transcript` (:858), `stream` (:1033, SSE 10 Hz).
- `POST <sid>/system-source {mode,apps?,reason?: user|auto_restore}` (:473): 409 `NO_SYSTEM_STREAM`, 400
  `RETARGET_FAILED`/`INVALID_REASON`. `GET /apps` (:518) never 500 → `available:false`.
- `GET /api/recordings/recovered` (:959), `/recordings/active` (:993) → `{active,count,sessions[...],session_id,state}`;
  `session_id`/`state` only when there is exactly one session.
- `POST <sid>/save` JSON `{name, description?}`: `description` is normalized (`record_meta`) → session manifest →
  `register_recording()` → `audio_downloads.description`.
- Gotchas: over the limit → 409 `SESSION_LIMIT` (`active`,`limit`); a bad spec → 400 `UNSUPPORTED_SOURCE_SPEC` before
  start; copilot busy → start 200 with `copilot.error_code='COPILOT_BUSY'`; finalize is async (>5 min), save deadline 5 min.
- K2 autoprompt (4.9.0, :542-643; watcher = `current_app.extensions['call_watcher']`, only in the `__main__` process):
  `GET /autoprompt` (:582) → `{enabled,prompts[]}`; `POST /autoprompt/<prompt_id>/dismiss` (:591, `record` only,
  404 `NOT_FOUND`); `GET /autoprompt/<token>/<action>` (:600; record|sources|stop|keep) — Windows toast buttons,
  the token is one-time (`watcher.consume`), the response is a redirect to `/record[?app=&mic=]` or an HTML error (404/409/500).
  `record` = the same `_start_session` with `include:[exe]` + `_mic_for` (:561); `stop` → `stop_session` (:644,
  module-level, the watcher takes the same one). Mutation via GET is deliberate; protection — an unpredictable token.
