# blueprints — HTTP layer (`app/blueprints/`), overview + system/events/settings/comments/documents/copilot/speakers/bookmarks

18 blueprints (+`mail` 4.18.0, +`connect` 4.19.0, +`automation` 4.20.0), registered at `app.py:1067-1084` (imports :64-81) WITHOUT `url_prefix` — the full
path in every route. Singletons — `from app import state`. Errors — JSON `{success,error}` + 400/401/403/404/409/500/503; the SPA reads these
shapes and they are NOT rewritten. Newer routes add `error_code`; every `error_code` literal must be a key of `app/core/error_codes.py`
(`tests/test_error_codes.py`). MCP normalises the shape on its side (`root-processes.md`). The auth gate itself answers with `code`, not `error_code` (`auth.py:232`).
Other slices: `blueprints-media.md` (transcription, audio_library, youtube, recording), `blueprints-memory.md` (memory, research, telegram).

last-verified: 2026-10-25 (mcp-telegram-automation 4.31.0: automation.py status + pipe run; `APP_VERSION` in system.py:34 still reads "4.30.0"); 2026-10-21 (mcp-pipeline 4.27.0: system/events/documents/gate lines re-read); 2026-10-19 (mcp-mail-and-keys 4.24.0: mail gated routes, system.py APP_VERSION; 4.23.3 version bump; 4.23.2 version bump; recording-autosave, 4.23.0: new recording routes in `blueprints-media.md`; rest mcp-foundations-b 4.22.0)

**Approval gate (4.22.0, 4.24.0, 4.27.0, 4.31.0 +`telegram.tg_enable_chat` = 20 entries):** 19 endpoints were destructive and answer only after a native dialog (`approval_gate._D_ROUTES` :55; 4.27.0 adds `documents.import_folder`, `documents.reparse_document`, `documents.reparse_job`; `transcription.analyze_video_endpoint` is conditional, see `services-infra.md`; table and
order of checks — `services-infra.md`): transcription delete/bulk-delete, memory delete/merge category and tag + `start_backfill`,
speakers delete/merge, `telegram.tg_backfill`, `settings_api.clear_anthropic_key`, `audio_library.delete_audio_download`, and four mail
routes (`mail_accounts_delete` = `mailbox_delete`, `mail_accounts_disconnect`, `mail_backfill`, `mail_label_enrich`). They may also answer 400 `BAD_REQUEST` (+`reason`), 403 `APPROVAL_DENIED`, 409 `APPROVAL_PENDING` /
`RECORDING_ACTIVE`, 429 `RATE_CEILING`, 503 `APPROVAL_UNAVAILABLE` before the view runs. Every non-GET `/api/*` is audited (`mcp_actions`).

**Agent writes (4.26.0, ADR-042):** routes tell the owner's SPA from an agent with `auth.classify_request()[0] == 'ui'` (per-boot UI token); a non-ui caller is stamped `source='mcp'`. comments.py: agent create strips `weight/pinned/author/parent_id/source`, identical body within 10 min -> `{created:false}`; PATCH/DELETE/restore on a non-`mcp` row -> 403 `OWNER_AUTHORED` (`_owner_guard`); agent PATCH clears `confirmed_at`; new `POST /api/comments/<id>/confirm` is ui-only (else 403 `USE_UI`). bookmarks.py: DELETE returns `deleted_snapshot`, agent note <=2000 (`INVALID_ARGUMENT`). speakers.py: agent rename of self -> `IS_SELF`; `PATCH /api/transcriptions/<id>/speakers` for an agent takes existing speaker ids only and writes `transcription_speaker_map.source='mcp'` without touching speaker embeddings (UI path resets source to NULL). Loopback calls from `recording/autosave.py::http_json` carry the UI token so they count as `ui`. system.py `APP_VERSION = "4.26.0"` (:39; `app/__init__.py:6`) — the 4.23.3 value below is stale.

## system.py (~400) — `/`, `/api/health|ready|models*|system_*|polish/*|metrics`
- `APP_VERSION = "4.30.0"` (system.py:34; mirror `app/__init__.py:6`) — `version` in health/ready/system_info. The 4.26.0 line in the block above is stale.
- 4.27.0: `POST /api/download_model` (:198) submits/joins the `model_download` job and blocks on its future, same `{success,message}` shape (+`job_id`); `POST /api/models/download-job`
  (:223, 202 `{success, job_id, created}`; unknown model 400 `UNKNOWN_MODEL`); `POST /api/system/diagnostic-package/file` (:278, `diagnostics.write_package()`, 500 `INTERNAL_ERROR` on OSError).
  `GET /diagnostic-package` (:267) streams `diagnostics.build_package()`.
- `GET /<path>` SPA shell (:43; `api/…` → 404 JSON, `HEADLESS` → 404; a direct loopback request with a foreign `Host` → 403
  `HOST_REJECTED` JSON, :57 — the ONLY non-`/api/` place that checks Host; renders `shell.html` with `ui_lang` and `ui_token`, empty unless
  `auth.is_local_trusted()`), `/favicon.ico`, `/sw.js` (:74, no-cache + `Service-Worker-Allowed`), `/api/models`, `POST /download_model`, `/models/update-status`,
  `POST /models/check-updates`, `/system_info`, `GET /api/system/diagnostic-package` (zip), `/system_stats`, `/polish/availability`
  (Claude tiers; `tr('settings.polish.*')`), `/metrics` (Prometheus).
- `GET /api/health` (:94): `_check_database_ready()` (:107; an EXISTING file `mode=rw`, does not auto-create; requires
  `schema_versions` with `MAX(version)>=1`) ≠ ok or no ffmpeg → `degraded`.
- `GET /api/ready` (:149): `state.робота` (see `root.md`) + a live DB check. 200 when `database=='ok' ∧ job_queue.bound ∧
  whisper.preload ∈ (ready,disabled) ∧ boot_finished`, else 503 (`робота` None → 503). `migrations` is outside the formula.

## events.py (~85) — `GET /api/process/logs/<id>`, `/api/events/<id>` (SSE), `/api/jobs`, `/jobs/<id>`, `POST /jobs/<id>/cancel`
A job dict carries `error_code`, `cancel_requested`, `cancel_ignored` (`services-infra.md`). Cancel of a job that exists but already
finished -> 400 `{success:false, error, cancel_ignored:true}` (:82); a missing job -> 400 without the flag.
4.27.0: `GET /api/jobs/<id>` (:83) takes `wait` (0..25 s, 250 ms steps until a finished state), `include_log=1` + `tail` (1..200, default 50; `log` = tail of `state.process_logs[job_id]`, `[]` if the kind never logged); the dict carries `result`/`result_truncated`. `GET /api/jobs` (:59) filters `kind`, `active_only`.

## automation.py (~225; 4.31.0 adds status + pipe run) — `GET /api/automation/events?channel=&events=&after=&timeout=` (blueprint `automation_bp`)
4.31.0: `GET /api/automation/status[?sections=calls,pipes,pollers,telegram]` (:165; unknown section 400 `INVALID_ARGUMENT`): `calls` (watcher allowlist: kind/exe/label/age_sec/session_id, never ids or URLs),
`pipes` (schedule, tools, budget, `last_run`, `cooldown_sec`), `pollers` (scheduler/brief_buttons/mail present), `telegram` (`listener_online`, monitored/enabled counts, `bot_configured`).
`POST /api/pipes/<name>/run` (:192): unknown pipe 404 `NOT_FOUND`, no scheduler 409 `PIPES_DISABLED`, no bot 503 `NOT_CONFIGURED`, running/fresh `running` row 409 `ALREADY_RUNNING` (+`job_id`),
cooldown 429 `COOLDOWN` (+`retry_after_sec`), else 202 `{job_id, created, poll}`; job kind `pipe_run`, delivers to the fixed bot recipient with `trigger='mcp'`.
Long poll over the `sse_broker` ring for MCP `wait_for`. Channel must start with `sse_broker.RING_PREFIXES` else 400 `INVALID_ARGUMENT`;
`timeout` clamped 0..25 s; `recording:<sid>` with an unknown session (or no `recording_service`) -> 404 `NOT_FOUND`; the 5th concurrent
waiter (`MAX_WAITERS=4`) -> 429 `TOO_MANY_WAITERS`. Default `events` on `recording:` = status, live_segment, copilot_insight, source_changed,
error. `recording:<sid>` without `after` -> `{already:true, state, next}` (state from `recording_service.get_state`); otherwise `{events:[{seq,
event,data,ts,provenance_kind}], next, timed_out, gap, reset}`; `provenance_kind`: live_segment->transcript, copilot_insight->copilot, else
system. Texts `tr('automation.err.*')` (`i18n_catalog/automation.py`). Gotcha: live-segment text is returned as the ring holds it; this route applies no
private/mask filter (neither does `/api/recording/<sid>/live-transcript`, which `get_live_transcript` proxies).

## settings_api.py (~240) — `/api/settings/*` (blueprint `settings_api`, `settings_bp`)
`GET anthropic-key/status` (:92), `POST anthropic-key` (:99; saving is `settings_secrets.save_anthropic_key`, shared with the connect
page), `POST anthropic-key/connect-link` (:120, 4.19.0: issues a `connect_links` `anthropic_key` link -> `{connect_url, expires_in,
already_configured}`), `DELETE anthropic-key` (:132), `POST anthropic-key/test` (:145), `POST copilot/toggle` (:187), `GET /ui-lang`
(:216) → `{success,value}`, `POST /ui-lang {value: auto|uk|en}` (:221; else 400 `tr('core.settings.ui_lang_invalid')`). Key/language go to
`.env` through `env_file.set_env_var`/`unset_env_var` (`_set_env_var` :66, `_unset_env_var` :73; also set `os.environ`). An `after_request` logs `actor=mcp tool= route= status=` for `X-Recall-Actor: mcp`.

## connect.py (~150, 4.19.0) — `GET|POST /connect/<kind>/<token>` (blueprint `connect_bp`; `kind` = `mail|anthropic_key`)
The ONLY place a password or API key is typed. Not under `/api/`, so outside the auth gate; `_guard_ok` (:34) is its own gate: remote_addr
`127.0.0.1|::1`, no proxy header (`auth._PROXY_HEADERS`, 4 names), `Host` loopback, and on POST `Origin` == `http://<host>` (`Origin: null` only
with `Sec-Fetch-Site` same-origin/none, :47) else 403. Unknown kind 404; dead/expired/consumed token -> 410 page. Form field `secret`.
Mail: `ma.connect_account` (test login + LIST first); key: `settings_secrets.save_anthropic_key`. Failure -> `connect_links.record_failure`
(3rd failure kills the link; message + attempts left, status 400/500/502); success -> `connect_links.consume`, mail page lists labels. Responses
carry `Cache-Control:no-store`, `Referrer-Policy:same-origin`, `X-Frame-Options:DENY`; the secret is never logged or rendered back (only
`MailError.code`/exception type are logged). Template `templates/connect.html`; texts `i18n_catalog/connect.py` (`connect.*`).

## i18n in the HTTP layer (english-i18n, 4.16.0)
All error/status texts in `jsonify` — `tr('<area>.<key>', **vars)` / `tr_n` (`app/core/i18n.py`; language = `request_lang()`:
`X-Recall-Lang` → `RECALL_UI_LANG` → uk). Catalog `app/core/i18n_catalog/<area>.py` (`MESSAGES`: `core|capture|transcript|settings|library|
knowledge|telegram|mail|connect|automation` + `svc_*` service modules; duplicate key → `ValueError` at import, `__init__.py:25-36`). Catch-all 500
(`error_handlers.py:64`) — `tr("core.error.internal")`. Services' SSE is filtered by `localize_sse` (`research.py:141`, `memory.py:858`).
Logs/exception texts are English. Guard: `tests/test_i18n_coverage.py` (no Cyrillic in `jsonify`).

## comments.py (~260) — `/api/comments/*` (Wave 1/3, `services/comments.py`)
`GET /meta`, `GET ?target_type&target_id`, `/recent`, `POST /counts` (≤5000 ids), `POST` (201 `{comment}`, `CommentError` → 400),
`PATCH /<id>` (missing → 404), `DELETE` (soft), `POST /<id>/analyze` (synchronous Claude: 404, 503 no ANTHROPIC_API_KEY, 409 `no_anchor`,
502 `retry_needed`), `GET /<id>/derived`, `POST /<id>/restore`. Indexing: `_schedule_index` → `state.job_queue` (no queue — synchronous).
`target_id` may be a string (a recording session) — coerced in the service according to `TARGETS`.

## documents.py (~460) — `/api/documents/*`
`POST /upload` (sync-parse; multipart `title`/`description`, validation BEFORE parsing), `POST /<tid>/reparse` (:257, gated `document_reparse`), `POST /import-folder` (:422, gated `document_import_folder`; response carries `job_id`)
(background+SSE; `_run_folder_import` :326 calls `document_ingest.ingest_file(..., store=_copy_into_docs)`). 4.27.0: `reparse(db_path, tid)` :207 is the shared body; `POST /<tid>/reparse-job` (:270, 202 `{job_id}`, same gate action);
`POST /ingest-paths` (:474; 1..5 paths, each through `import_roots.resolve_allowed` + extension + size via `_check_ingest_path` :453 BEFORE anything starts; job kind `doc_import`, result `items[]`);
`import_folder_plan(data)` :380 is also what `approval_summaries` reads; `_import_roots()` :369 delegates to `services/import_roots.py`. Dedup by content_hash; OCR opt.;
table description by Claude. Persistence chain: `services/document_ingest.py`. Depends on: document_parser, document_ingest, text_polishing.

## mail.py (~270) — `/api/mail/*` (blueprint `mail_bp`, 4.18.0; JSON `{success,...}`, errors `tr('mail.err.*')` + `error_code`)
`GET /accounts`, `POST /accounts {address, app_password}` (-> `ma.connect_account`: test login + LIST first, only then keyring+row; 201
`{account, labels}`; `INVALID_ADDRESS`/`EMPTY_PASSWORD`/`AUTH_FAILED` 400, `CONNECT_FAILED`/`FOLDER_MISSING`/`PROTOCOL` 502; body never logged),
`DELETE /accounts/<id>` (:124, hard delete, GATED `mailbox_delete`, 404), `GET /accounts/<id>/labels[?refresh=1]` (refresh without keyring entry -> 409 `NO_PASSWORD`),
`POST /accounts/<id>/labels/refresh`, `PATCH /labels/<id> {enabled?, enrich:false only, category_id? (null or a ROOT direction)}`
-> enabling triggers a sync, `POST /sync {account_id?, months? 1..12}` -> 202 (no poller -> 503 `MAIL_POLLER_OFF`), `GET /status`.
Gated routes (4.24.0, body validated by `approval_summaries` first): `DELETE /accounts/<id>/credentials` (:132, `mailbox_disconnect`: soft,
`ma.disconnect_account`; reply `labels_disabled, disabled_label_ids, records_kept, recovery`), `POST /backfill {months 13..120 (BACKFILL_MAX_MONTHS),
account_id?}` (:253, `mailbox_backfill`, 200), `POST /labels/<id>/enrich` (:267, `mailbox_label_enrich`, `update_label(enrich=True)`).
`PATCH enrich:true` and `/sync` months>12 refuse EVERY caller with 400 `USE_GATED_ROUTE` (+`route`), `SYNC_MAX_MONTHS=12` :61.
`POST /connect-link {address}` (4.19.0) -> `{connect_url, expires_in:600, already_configured, address}`; an address that
`looks_like_secret` -> 400 `SECRET_IN_ARGUMENT`. The poller is reached only via `current_app.extensions['mail_poller']`.
**MCP caller limits** (header `X-Recall-Actor: mcp`, `_is_mcp` :65; `MCP_MAX_MONTHS=12`): a
`category_id` change that moves a record out of a private direction (`privacy.category_change_exposes`) -> 403 `PRIVATE_CATEGORY_CHANGE`
(check failure 503 `PRIVACY_CHECK_FAILED`); enabling a label syncs with `first_months_cap=12` (+`backfill_months` in the reply).
An `after_request` logs `actor=mcp tool= route= status=`. Logic — `services-mail.md`.

## copilot.py (~550) — `/api/copilot/*`
`availability`, `live-ask`, `start`, `move`, `sessions`, `/<id>/timeline|export|reingest|state|settings|stop`, `by-transcription/<tid>`,
`/<id>/action` (dismiss|pin|thumbs_up|thumbs_down|escalate).
- 503 when `state.copilot_service is None` (`_require_service`), EXCEPT `live-ask` (not a session step).
- `POST /move {from_session_id,to_session_id,settings?}`: ends the copilot on A, starts on B, not atomic: a start failure → 200
  `COPILOT_MOVE_FAILED` with `ended_from:true`. `CopilotBusyError` → 409 `COPILOT_BUSY`; several live recordings without `session_id` →
  `AMBIGUOUS_SESSION`.

## speakers.py (~1000) — `/api/speakers/*`, `/api/transcriptions/<id>/speakers`
`GET/POST /speakers`, `PUT/DELETE /<id>`, `PATCH /api/transcriptions/<id>/speakers` (bulk map), `GET /stats`, `/<id>/timeline`,
`POST /merge`, `POST /unmerge/<merge_id>`, `GET /<id>/samples`. Gotchas: casefold in Python (Cyrillic-safe); embeddings are averaged.

## bookmarks.py (~190) — `/api/transcription/<id>/bookmarks`, `/api/bookmarks/<id>`, `/api/saved-searches[/<id>[/use]]`
UNIQUE (transcription_id, segment_index); the saved query is stored as JSON.
