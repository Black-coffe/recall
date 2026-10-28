# services — infrastructure

last-verified: 2026-10-28 (wave2-recording-calls 4.36.0: DEVICE_ACCESS_DENIED); 2026-10-28 (task-approval 4.35.0: v59, INVALID_TRANSITION); 2026-10-25 (mcp-telegram-automation 4.31.0: gate line, error codes); 2026-10-21 (mcp-pipeline 4.27.0: job result, SLOW_KINDS, error codes, gate lines re-read); 2026-10-20 (retranscribe-versions 4.25.0: SLOW_KINDS, error_codes); 2026-10-19 (mcp-mail-and-keys, 4.24.0; gate/summaries line numbers re-read)

Part of `app/services/*`; hub — [services.md](services.md).

- **app/core/i18n.py** (140, not a service, but shared infrastructure; `app/core/i18n_catalog/`) — `LANGS`, `ui_lang_setting()`,
  `server_lang()`, `request_lang()`, `tr`/`tr_n`, `localize_sse`. Catalog modules incl. `svc_*` (service texts, 4.17.0; list in `services.md`). An unknown key → returns the key + a warning. `RECALL_UI_LANG`
  is read from `os.environ` on every call. Contract details — `blueprints.md` § i18n, `docs/wiki/ui-language.md`.
- **job_queue.py** — `JobQueue.submit(kind, fn, meta)` on top of three executors: batch (main), live (`live_kinds`, recording_finalize)
  and slow (`SLOW_KINDS` :44 — `enrichment_backfill`, `reembed_batch`, `doc_import`, `video_analysis`, `youtube_download`,
  `audio_download`, `retranscribe` (4.25.0), `model_download` (4.27.0); `transcription` and `transcript_ai` jobs stay on the batch executor; 1 worker, built inside `JobQueue` unless `slow_executor=` is passed, `_executor_for_kind` :140), so two slow passes
  run one after the other. `Job` (state machine incl. `crashed`, `is_cancelled()`). Cancel is cooperative; history is bounded.
  4.20.0: on an exception the job gets `error = tr("svc_jobs.error.failed")` (the raw text only goes to `logger.exception`) and
  `error_code` = the exception's `error_code` attribute if `UPPER_SNAKE`, else `JOB_FAILED` (:180); `recover_crashed` writes
  `svc_jobs.error.crashed` + `JOB_CRASHED`. No DB column: for a persisted row `to_dict()` derives the code from the state
  (`_STATE_ERROR_CODES` :50). `to_dict()` adds `error_code`, `cancel_requested`, `cancel_ignored` (= state `completed` and a cancel
  was requested, :83).
  4.27.0: the runner stores the task's return value as `Job.result` (`_json_safe_result` :63, cap `RESULT_MAX_BYTES`, `result_truncated`; persisted inside
  `jobs.meta_json` under reserved keys `__result__`/`__result_truncated__`, no column, `_split_stored_meta` :416); `to_dict()` carries both;
  `find_active(kind, **meta_eq)` :230; `FINISHED_STATES` :53. `GET /api/jobs/<id>?wait=&include_log=&tail=` — `blueprints.md` § events.py.
- **sse_broker.py** — in-memory pub/sub: `publish()`, `subscribe(channel, history=None, events=None)`, `stream_events()`. `Queue.Full`
  silently drops; keepalive 15 s; single-process (multi → Redis). Event ring (4.20.0): channels starting with `RING_PREFIXES`
  (`recording:`, `system:`, `pipes`, `doc_import`, `backfill`) keep the last `RING_SIZE=500` events each, except `RING_EXCLUDED_EVENTS`
  (`level`, `chunk_saved`); one monotonic `_seq` per broker (shared by all channels), `boot_id` per process. `cursor(channel)` ->
  `"<boot_id>:<seq>"`; `read_since(channel, after, events)` -> `{events, next, gap, reset}` (`reset`: foreign boot or malformed cursor, reads
  from the ring start; `gap`: events after the cursor were evicted). Live SSE payloads are unchanged. Reader — `blueprints.md` § automation.py.
- **app/core/env_file.py** (4.20.0) — the only `.env` writer, `set_env_var`/`unset_env_var` under one `threading.Lock` (details in `root.md`).
- **app/core/error_codes.py** (4.20.0, stdlib only so `mcp_server.py` can import it) — `ERROR_CODES: dict[code, canonical HTTP status]`,
  4.30.0 adds `PAST_LIVE_EDGE` 409 and `NOT_RECORDED` 404 (`error_codes.py:108-109`), `status_for(code)` (unknown -> 500), `code_for_status(status)`. `tests/test_error_codes.py` checks only that every `error_code` literal in
  MCP / blueprints / auth is a key; it does not check that a route answers with the canonical status; dynamic codes (`MailError.code`)
  were added by hand. The auth gate's `code` key (`HOST_REJECTED`, `ORIGIN_REJECTED`) is not an `error_code` literal.
  4.27.0 adds `NOT_FINALIZED` 409, `PATH_NOT_ALLOWED` 403, `UNSUPPORTED_EXTENSION` 400, `FILE_TOO_LARGE` 413, `UNKNOWN_MODEL` 400 (:102-106).
  4.22.0 adds `APPROVAL_DENIED` 403, `APPROVAL_UNAVAILABLE` 503, `APPROVAL_PENDING` 409, `RATE_CEILING` 429 (:82-86); `RECORDING_ACTIVE` 409 (:57); 4.24.0 adds `USE_GATED_ROUTE` 400 (:78, a plain mail route refuses what only its gated twin may do); 4.25.0 adds `NO_MEDIA` 404 (:57) and `MODEL_NOT_INSTALLED` 400 (:58) for re-transcribe (`ALREADY_RUNNING` 409 :39 was already there).
- **Approval gate (4.22.0, wiki `approval-gate.md`)** — three layers:
  - `app/core/approval_gate.py`: `_D_ROUTES` :47 (Flask endpoint -> action; a new destructive route is added by hand, `_D_EXEMPT` :63
    lists the deliberate non-gated ones; `tests/test_endpoints_smoke.py` fails on an unlisted delete/merge/purge/backfill), `D_ACTIONS` :40
    (14 actions; 4.24.0 added `mailbox_disconnect|mailbox_delete|mailbox_backfill|mailbox_label_enrich`, 16 routes in `_D_ROUTES`),
    `_gate()` :143 (order: D ceiling per actor class 3/min + 20/day `_take` :91 -> 429; non-`ui` during a recording
    `recording_active(default=True)` -> 409 `RECORDING_ACTIVE`; `approval_summaries.build`; `approvals.busy()` -> 409; `approvals.ask`
    -> approved / 403 / 409 / 503), `register(app)` :211. Registered in `app.py:1093` after `auth.register`, before `audit_hooks`.
    `Precondition`/`Unsummarisable` give the ceiling slot back (`_untake` :107). `X-Recall-Note` -> `_clean_note` (60 chars, no «»).
  - 4.26.0 (line numbers superseded by 4.27.0 below): `_CONDITIONAL_D_ROUTES` = `transcription.retranscribe_transcription` gated for a non-`ui` caller only when `retranscribe.would_evict`; the view passes `allow_evict` to `retranscribe.submit`, else the job fails `EVICTION_NOT_APPROVED` (409). `_NON_UI_CHECKS`: private-direction delete/merge -> 403 `PRIVATE_CATEGORY_CHANGE`; `transcript delete` with a live job -> 409 `JOB_ACTIVE`. `error_codes.py` +`NOT_MCP_EVENT` 403, `UNDO_NOT_LAST` 409, `OWNER_AUTHORED` 403, `USE_UI` 403, `SPEAKER_ID_REQUIRED` 400, `IS_SELF` 403, `JOB_ACTIVE` 409, `BULK_LIMIT` 400, `EVICTION_NOT_APPROVED` 409.
  - 4.27.0 (current lines; the older line numbers above are stale): `D_ACTIONS` :46 now 18 (+`document_import_folder`, `document_reparse`, `video_describe_claude`); `_D_ROUTES` :55 = 19 entries
    (+`documents.import_folder`, `documents.reparse_document`, `documents.reparse_job`; the last two share `document_reparse`); `_CONDITIONAL_D_ROUTES` :104 +=
    `transcription.analyze_video_endpoint` -> `_video_describes_with_claude` :85 (non-`ui`, `describe` true or implied by a sent `vision_backend`, effective backend `claude`; non-dict body raises -> gated);
    `_D_EXEMPT` :109, `_gate` :199, `register` :270. `approval_summaries`: `_import_folder` :421 (uses `documents.import_folder_plan`; a refusal the view would give -> `Precondition`, no dialog),
    `_document_reparse` :442 (`documents.reparse_target`, `ReparseError`), `_video_describe_claude` :458 (no video -> 200 `NO_VIDEO` Precondition), `BUILDERS` :474, `_NON_UI_CHECKS` :533, `build` :540, `render` :605, `one_line` :625.
    `recording/state.py::RECORDING_BLOCKED` :16 declares `transcribe`, `video_analyze:describe_local`, `transcript_suggest_meta`, `transcript_ai:enrich`, `settings:copilot_disable`; `recording_active` :25.
    `job_queue.active_job_for_transcription` is now :432.
  - 4.32.0 (mcp-recording-live): `D_ACTIONS` :50 now 21 (+`recording_stop`, `recording_cancel_own`; routes `recording.stop_recording`, `recording.discard_recording` :134-135, `_always`, so gated for non-`ui` only);
    `RECORDING_ALLOWED_ACTIONS` :60 exempts exactly those two from the mid-recording `RECORDING_ACTIVE` refusal (check :245); `X-Recall-Note` (`_clean_note`, :282) = the agent's quoted name in the dialog;
    `approval_summaries._recording_stop` :568 / `_recording_cancel_own` :582 (manifest + live snapshot, never the request). `error_codes.py` +`OWNER_SESSION` 403, `NOTIFY_UNAVAILABLE` 503, `CANCEL_WINDOW_CLOSED` 409 (:117-119).
    Migration v57 (`migrations.py:2268`): `copilot_sessions.reingest_transcription_id` + `reingest_by`.
  - 4.31.0: `D_ACTIONS` :46 now 19 (+`telegram_enable_chat`), `_D_ROUTES` :56 = 20 (+`telegram.tg_enable_chat`); `approval_summaries._telegram_enable_chat` :351 (dialog text from DB). `error_codes.py` +`PRIVATE_CATEGORY_CHANGE` 403 (:76, was mail-only, now also chats),
    `LISTENER_OFFLINE` 503 (:111), `NOT_MONITORED` 404 (:115); `UNDO_NOT_LAST` 409 (:93) is also the chat-undo refusal. Texts `approvals.*`, `automation.*`, `telegram.*` catalogs gained the keys.
  - 4.34.0: `D_ACTIONS` +`calendar_disconnect`, `_D_ROUTES` = 21 (+`settings_api.disconnect_calendar`); `approval_summaries._calendar_disconnect` :405 (label + event count from DB, `_KIND_TABLE["calendar"]`); `error_codes.py` +`CALENDAR_POLLER_OFF` 503 (:80).
  - 4.35.0 (task-approval): no new D route (approve/reject/edit of a proposal are not destructive); `error_codes.py:41` `INVALID_TRANSITION` 409 is the refusal of a status-machine move (`commitments.apply_action` `ValueError`). Migration v59 (`migrations.py:2318`): `action_items` +`for_whom, confidence, importance, text_key, first_shown_at, decided_at, pre_legacy_status` (+2 indexes), `action_item_events.payload_json`, every pre-existing row -> `status='legacy'`. Setting `TASK_PROPOSAL_MIN_CONFIDENCE` (`settings.py:455`).
  - 4.36.0 (wave2-recording-calls): `error_codes.py` +`DEVICE_ACCESS_DENIED` 403 (:123, Windows refused a capture stream).
  - `app/services/approvals.py` (stdlib only): `ask(text, timeout_s)` :120 -> `Decision(status approved|denied|unavailable|pending)`;
    `APPROVAL_TIMEOUT_S=45`; `_slot` lock = one dialog at a time (`busy()` :116); `_show_dialog` :72 = `MessageBoxW` on a daemon thread
    (the test seam, replaced in `tests/conftest.py`); on timeout `_close_window` :101 posts «No» by the random-nonce title.
  - `app/services/approval_summaries.py`: `BUILDERS` :387 per action (4.24.0: `_mailbox_disconnect` :340, `_mailbox_backfill` :350 validates `months`/`account_id`, no
    account = all mailboxes, `_mailbox_label_enrich` :369, settings key removal), `build()` :407 -> `Summary | None` (None = path id names
    nothing, the view answers 404), `Precondition`, `Unsummarisable`, `render()` :459 (dialog text, UI language `server_lang()`; `_extra_line()` adds
    the mailbox address for label-enrich and whether a key is stored for key removal), `one_line()` :479 (audit text).
    Texts: `app/core/i18n_catalog/approvals.py` (`approvals.*`, auto-discovered by `pkgutil`).
- **Audit (4.22.0)** — `app/core/audit_hooks.py`: `register(app)` :120 (before_request timer, after_request `_write`, teardown for a 500);
  `_should_audit()` :55 (non-GET `/api/*` and `/connect/*`; the listener's valid `X-Telegram-Token` skips only non-D routes);
  `_audit_path()` :65 redacts a connect token. `app/repositories/mcp_actions.py`: `insert` :35, `query` :45 (limit 1..200, `before_id`),
  `purge_older_than(days=90)` :95; `retention.purge_mcp_actions` :119 runs at boot beside `purge_soft_deleted` (`app.py:934`).
  `recording/state.py::recording_active(*, default=False)` :23 — the gate passes `default=True` (fail closed); older callers keep fail-open.
- **system_monitor.py** — daemon snapshot of CPU/GPU/RAM every N sec (the API does not block on `cpu_percent`).
- **file_cleanup.py** — daemon that cleans orphan upload/youtube temp files (does not touch DB-referenced ones; read-only DB).
- **metrics.py** — `MetricsRegistry`: `inc()`, `observe_duration()`, `render()` (Prometheus text).
- **file_manager.py** — `FileManager`: info/list/validate/checksum, `FileInfo`.
- **model_updates.py** — daemon that checks PyPI for a new faster-whisper (`CHECK_INTERVAL_DAYS=14`, `start_background_check`,
  fail-silent).
- **local_llm.py** — Ollama HTTP client: `is_available()`, `generate()`, `generate_json()`, `warmup()`, `resolve_model()`;
  `LOCAL_LLM_MODEL` default `qwen2.5:14b-instruct-q5_K_M` (`:51`), family substitution if the exact model is missing. $0; **a single
  `_GEN_LOCK` (`:46`) serializes all local calls** (GPU contention); urllib only.
- **models.py** — `get_default_model()` (`DEFAULT_MODEL` default `claude-opus-5`, override env `CLAUDE_MODEL` without restart).
- **pricing.py** — `MODEL_PRICES` + `estimate_cost()`; the single source of prices (rag, research, copilot/escalate, summaries).
