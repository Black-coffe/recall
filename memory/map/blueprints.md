# blueprints — HTTP layer (`app/blueprints/`), overview + system/events/settings/comments/documents/copilot/speakers/bookmarks

15 blueprints, ~180 route decorators, registered at `app.py:1062-1076` (imports :63-77) WITHOUT `url_prefix` — the full
path in every route. Singletons — `from app import state`. Errors — JSON `{success,error}` + 400/401/403/404/409/500/503.
Other slices: `blueprints-media.md` (transcription, audio_library, youtube, recording), `blueprints-memory.md` (memory, research, telegram).

last-verified: 2026-10-16

## system.py (~400) — `/`, `/api/health|ready|models*|system_*|polish/*|metrics`
- `APP_VERSION = "4.16.0"` (system.py:38; mirror `app/__init__.py:6`) — `version` in health/ready/system_info.
- `GET /<path>` SPA shell (:41-43; `api/…` → 404 JSON, `HEADLESS` → 404 `headless`; renders `shell.html` with
  `ui_lang=ui_lang_setting()` :56), `/favicon.ico` (:59), `/sw.js` (:70, no-cache + `Service-Worker-Allowed`), `/api/models`
  (:187), `POST /download_model` (:193), `/models/update-status` (:205), `POST /models/check-updates` (:215), `/system_info`
  (:223), `GET /api/system/diagnostic-package` (:291, zip), `/system_stats` (:335), `/polish/availability` (:345, Claude tiers;
  texts `tr('settings.polish.*')`), `/metrics` (:380, Prometheus).
- `GET /api/health` (:89): `_check_database_ready()` (:107; an EXISTING file `mode=rw`, does not auto-create; requires
  `schema_versions` with `MAX(version)>=1`) ≠ ok or no ffmpeg → `degraded`.
- `GET /api/ready` (:144): `state.робота` (`app/state.py:50`; see `root.md`) + a live DB check. 200 when
  `database=='ok' ∧ job_queue.bound ∧ whisper.preload ∈ (ready,disabled) ∧ boot_finished`, otherwise 503
  (`робота` None → 503 `робота not initialised`). `migrations` is outside the formula (unreachable state, `state.py:45-49`).

## events.py (~80) — SSE + queue
`GET /api/process/logs/<process_id>` (:21), `GET /api/events/<process_id>` (:38, SSE progress/log/segment/complete/error;
`Cache-Control:no-cache`, `X-Accel-Buffering:no`, `Connection:keep-alive` :53-55), `GET /api/jobs` (:59), `/jobs/<job_id>`
(:68), `POST /jobs/<job_id>/cancel` (:76).

## settings_api.py (~200) — `/api/settings/*` (blueprint `settings_api`, `settings_bp`)
`GET anthropic-key/status` (:84), `POST anthropic-key` (:91), `DELETE` (:128), `POST anthropic-key/test` (:141),
`POST copilot/toggle` (:183), `GET /ui-lang` (:214) → `{success,value}`, `POST /ui-lang {value: auto|uk|en}` (:219; anything else → 400
`tr('core.settings.ui_lang_invalid')`; a write failure → 500). The key/language are written to `.env` (`_set_env_var` :64 also sets
`os.environ` → takes effect without a restart; `_unset_env_var`).

## i18n in the HTTP layer (english-i18n, 4.16.0)
All error/status texts in `jsonify` — `tr('<area>.<key>', **vars)` / `tr_n` from `app/core/i18n.py`
(language = `request_lang()`: the `X-Recall-Lang` header → explicit `RECALL_UI_LANG` → uk). Catalog — `app/core/i18n_catalog/<area>.py`
(`MESSAGES`, `core|capture|transcript|settings|library|knowledge|telegram` + service modules `svc_boot|embed|llm|media|pipes|recording|retrieval|tasks|taxonomy|telegram`, 4.17.0; a duplicate key → `ValueError` at import,
`i18n_catalog/__init__.py:25-36`). The catch-all 500 (`error_handlers.py:64`) — `tr("core.error.internal")`;
`GENERIC_MESSAGE` stays a uk constant. The services' SSE (research/rag stream uk strings outside the request context)
is filtered by `localize_sse(frames, lang, keys)` (`research.py:141`, `memory.py:858`). Since 4.17.0 internal exception texts, logs and the process journal are English; user-facing
service texts go through `svc_*` keys and follow the request language. Guard: `tests/test_i18n_coverage.py` (no Cyrillic in `jsonify` of blueprints/`app.py`).

## comments.py (~260) — `/api/comments/*` (Wave 1/3, `services/comments.py`)
`GET /meta` (:80), `GET ?target_type&target_id` (:95), `/recent` (:114), `POST /counts` (:128, ≤5000 ids), `POST` (:146 →
201 `{comment}`, `CommentError` → 400), `PATCH /<id>` (:174, missing → 404), `DELETE` (:195, soft), `POST /<id>/analyze`
(:202, synchronous Claude: 404 not_found, 503 no ANTHROPIC_API_KEY, 409 `no_anchor`, 502 `retry_needed`), `GET
/<id>/derived` (:234), `POST /<id>/restore` (:253). Indexing is not in the request: `_schedule_index` → `state.job_queue`,
without a queue — synchronous. `target_id` may be a string (a recording session) — coerced in the service according to `TARGETS`.

## documents.py (~460) — `/api/documents/*`
`POST /upload` (:206, sync-parse; multipart accepts `title`/`description`, validation BEFORE parsing), `POST /<tid>/reparse`
(:297), `POST /import-folder` (:449, background+SSE). Dedup by content_hash; OCR opt.; table description by Claude. Depends on: document_parser, text_polishing.

## copilot.py (~550) — `/api/copilot/*`
`GET /availability` (:84), `POST /live-ask` (:176), `POST /start` (:232), `POST /move` (:255), `GET /sessions` (:307),
`/<id>/timeline` (:322), `/<id>/export` (:360), `POST /<id>/reingest` (:386), `GET /by-transcription/<tid>` (:428),
`/<id>/state` (:442), `POST /<id>/settings` (:458), `/<id>/action` (:491; dismiss|pin|thumbs_up|thumbs_down|escalate), `/<id>/stop` (:532).
- 503 when `state.copilot_service is None` (COPILOT_ENABLED=False; `_require_service`, :12/:37-39), EXCEPT `live-ask`
  (:184 — not a session step, without `_require_service`).
- `POST /move {from_session_id,to_session_id,settings?}` (4.7.0): ends the copilot on A, starts it on B, not atomic:
  a start failure → 200 `COPILOT_MOVE_FAILED` with `ended_from:true`. `CopilotBusyError` → 409 `COPILOT_BUSY`
  (`owner_session_id`); several live recordings without `session_id` → `AMBIGUOUS_SESSION`.

## speakers.py (~1000) — `/api/speakers/*`, `/api/transcriptions/<id>/speakers`
`GET/POST /speakers` (:104/136), `PUT/DELETE /<id>` (:172/223), `PATCH /api/transcriptions/<id>/speakers` (:251, bulk map),
`GET /stats` (:414), `/<id>/timeline` (:587), `POST /merge` (:680), `POST /unmerge/<merge_id>` (:862), `GET /<id>/samples` (:961).
Gotchas: casefold in Python (not SQLite NOCASE; Cyrillic-safe); embeddings are averaged.

## bookmarks.py (~190)
`GET/POST /api/transcription/<id>/bookmarks` (:34/46), `DELETE/PATCH /api/bookmarks/<id>` (:94/105), `GET/POST
/api/saved-searches` (:122/141), `DELETE /saved-searches/<id>` (:165), `POST /saved-searches/<id>/use` (:176).
UNIQUE (transcription_id, segment_index); the query is stored as JSON.
