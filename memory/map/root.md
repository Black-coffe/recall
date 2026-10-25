# root — core: app.py boot, config, state, whisper

last-verified: 2026-10-25 (mcp-telegram-automation 4.31.0: call watcher publisher only; other app.py line numbers :1000+ shifted, trust names); 2026-10-21 (mcp-pipeline 4.27.0: whisper_manager_new section only; app.py/config unchanged by it); 2026-10-19 (autosave wiring; app.py line numbers not re-swept except the autosave ones)
Adjacent slice: `root-processes.md` (telegram_*, mcp_server.py).

## app.py
**Purpose:** the main Flask server: boot, blueprints, background services, singletons, HTTP lifecycle.
**Entry points:** `get_db_connection()` :84, `init_database()` :89, `add_process_log(pid,stage,msg,progress,status)` :462,
`_download_youtube_core(url,id,save_to_library,quality,start_time,end_time)` :511 (DI adapter), `check_ffmpeg_on_startup()` :558,
`cleanup()` :582 (atexit), `_maybe_launch_telegram_listener()` :1153, `_maybe_start_call_watcher()` :1196, `_maybe_start_pipes()` :1257, `_maybe_start_mail()` :1290.
`.env` is loaded by explicit repo-root path (:33), not by search.
**Types:** `ThreadSafeProgressStore` :96 (LRU 100 + SSE), `ThreadSafeProcessLogs` :175. All singletons are assembled here → `app.state.init()`.
**Hooks (4.22.0):** after the blueprints `_auth.register` :1091, then `approval_gate.register` :1094 (D routes need a native dialog), then
`audit_hooks.register` :1097 (`mcp_actions` row per non-GET request); boot purge `retention.purge_mcp_actions` :934 (90 days).
**Blueprints:** 18 registered at :1067-1084; `connect_bp` (4.19.0, `/connect/...`, outside `/api/` so the auth gate
does not see it; it guards itself, `blueprints.md` § connect.py) and the last, `automation_bp` (4.20.0, `/api/automation/events`).
CSP `form-action 'self'` :1044.
**Gotchas:**
- telegram_listener is spawned once (gate `WERKZEUG_RUN_MAIN`); SSE publishing happens outside the lock; CSP on HTML only.
- `RecordingService(max_active=cfg.RECORDING_MAX_ACTIVE, app_source_factory=...)` :847; `_app_source_factory` :842 builds
  `AppAudioSource` with the resolver `session_roots(exe, fallback=resolve_root_pids)`.
- Recording autosave (4.23.0): `_rec_autosave_job` (`AutosaveJob`) + `_rec_autosave` (`AutosaveScheduler(..., after_save_fn=_rec_autosave_job.file_saved)`,
  built :858-876 before recovery, `None` on headless/no service :939,943) -> `state.init(recording_autosave=...)` :1057. The finalize
  callback schedules it (:727-745), boot re-arms `reschedule_pending()` :925. Logic — `recording.md`.
- Headless (`cfg.HEADLESS`): the recorder/live/copilot branches do not touch `app.services.recording.*` (log
  "profile=headless: recorder off"); telegram ingestion stays alive.
- The background services `_maybe_start_call_watcher()`, `_maybe_start_pipes()` and `_maybe_start_mail()` are called only under `__main__`
  (:1346-1348; with debug — only in the reloader child `WERKZEUG_RUN_MAIN=true`), they do not start under pytest.
- Mail (4.18.0): `MAIL_POLL_ENABLED=0` -> no thread; else `MailPoller(app.config['DATABASE'])` :1301 -> `app.extensions['mail_poller']`; a start failure only warns
  (`POST /api/mail/sync` then 503 `MAIL_POLLER_OFF`). Logic — `services-mail.md`.
- Call watcher (K2): `_maybe_start_call_watcher` :1244 passes `publisher=lambda event, data: sse_broker.publish("system:calls", event, data)` (:1296, 4.31.0) -> ring channel read by MCP `wait_for`; `CallWatcher` -> `app.extensions['call_watcher']` (:1301, also read by `automation.py` status); not Windows / no pycaw / `RECORDING_AUTOPROMPT=0` / no `recording_service` -> log line and exit.
- Pipes (K4/K5): `PIPES_ENABLED=0` -> exit. `PipeScheduler(...)` :1272 -> `app.extensions['pipe_scheduler']`; with `tg_bot.is_configured()` `ButtonPoller` :1284 -> `app.extensions['brief_buttons']`. Logic — `services.md`.
**Робота (`app.state.робота`, read by `GET /api/ready`, formula in `blueprints.md`):** keys
`database/job_queue/whisper/embeddings/telegram/boot_finished` (no `migrations`, `app/state.py:45-49`); whisper —
`start_tracked_preload` :642 (`app/services/whisper_preload.py`); `boot_finished=True` :1316.

## config.py
**Purpose:** `Config` :30 → `DevelopmentConfig` :330 / `ProductionConfig` :337 / `TestingConfig` :357.
**Entry points:** `get_config()` :495, `update_config(**kw)` :513, `init_directories()` :520.
**Flags:** `WHISPER_BACKEND` (faster>openai) :165, `WHISPER_MAX_PARALLEL` :168, `WHISPER_BATCH_SIZE` :170, `FORCE_CPU` :159,
`RECORDING_ENABLED` (auto-detect pyaudiowpatch), `RECORDING_MAX_ACTIVE` (min 1) :209, `COPILOT_ENABLED` :314,
`LOCAL_LLM_URL`/`LOCAL_LLM_MODEL` :315, `MAX_CONTENT_LENGTH` 20GB (Testing 10MB :363). `PROFILE` ('desktop'|'headless', from
`RECALL_PROFILE`) and `HEADLESS` are set by `_initialize_dynamic_config()` :478-487; in headless `RECORDING_ENABLED`/
`RECORDING_VIDEO_ENABLED` are always False.
**Gotchas:**
- Side effects (`_detect_recording_enabled`, `_detect_telegram_enabled`, `SECRET_KEY` hard-fail/autogen) run lazily once at the first
  `get_config()` (`_config_initialized` :469). The autogenerated `SECRET_KEY` is written through `env_file.set_env_var` (:421-427).
  `import config` does not pull in `pyaudiowpatch`/`telethon` and does not write `.env`; it loads the repo-root `.env` by explicit path (:25).
- `.env` has ONE writer: `app/core/env_file.py` — `ENV_PATH`, `set_env_var(key, value, *, path=None)` (module lock, `set_key(quote_mode=
  "always")`, also sets `os.environ`), `unset_env_var`. Callers: `settings_api.py`, `settings_secrets.py`, `config.py`. Stdlib + python-dotenv.
- `WHISPER_MAX_PARALLEL=1`: `WhisperModel.transcribe()` is not thread-safe on a shared handle (>1 → crash 0xC0000409).
**Registry (`app/core/settings.py`):** the single source of 177 env flags (4.23.0: +4 `RECORDING_AUTOSAVE_{SEC,MODEL,LANGUAGE,MIN_CONFIDENCE}` :197-211) (4.20.0 adds `RECALL_MCP_PROFILE` :656) (+`RECALL_UI_LANG` `auto|uk|en`, :77; `tests/conftest.py:14`
forces `auto` in every test, because the owner's `.env` is loaded by `config.py`/`app.py`) (count by `^    Setting(`): `REGISTRY`, `by_name()`,
`env/env_bool/env_int/env_float`, `profile()`, `render_env_example()` (CLI `python -m app.core.settings env-example`).
Setting descriptions are English (4.17.0). No heavy dependencies (does not import `app.*`/`flask`/`torch`/`config`). Groups: `«Труби»` (`PIPES_ENABLED`=0, `PIPES_RETRY_MINUTES`,
`PIPES_RETRY_MAX`, `PIPES_MODEL`, `TELEGRAM_BOT_TOKEN` secret, `TELEGRAM_BOT_CHAT_ID`); `RECALL_PUBLIC_URL`; `«Auth / доступ»`,
`RECALL_PROXY_ALLOWED_IPS`; `Mail` (:328; `MAIL_POLL_ENABLED`=1, `MAIL_POLL_MINUTES`=5, `MAIL_BACKFILL_MONTHS`=12, `MAIL_ATTACH_MAX_MB`=25;
no secret setting — the app password never goes through the registry; the Anthropic key is written to `.env` by `settings_secrets`, not a registry value).
**Auth gate (`app/core/auth.py`):** one `before_request` on `/api/*` (exempt: OPTIONS, non-`/api/`, `/api/health`, `/api/ready`).
`RECALL_LOCAL_TRUSTED` + localhost lets only a direct request through without a key. With `X-Forwarded-For`/`Tailscale-User-Login` —
proxied (`_proxied_client`, `auth.py:100`): let through only if the single XFF address ∈ `RECALL_PROXY_ALLOWED_IPS`
(a chain → unknown), otherwise `RECALL_API_KEY` (without it 401). Check: `scripts/check_proxy_trust.py`.
4.20.0 boundary: proxy headers are `_PROXY_HEADERS` :99 (+`Forwarded`, `X-Real-IP`). A direct loopback request (`is_direct_loopback`
:134) is local-trusted only with a loopback `Host` (`host_ok` :130; DNS rebinding -> 401 on `/api/*`), and a non-GET one with a foreign
`Origin` gets 403 `ORIGIN_REJECTED` (`origin_ok` :139; `Origin: null` only with `Sec-Fetch-Site` same-origin/none; gate :230). Per-boot UI token:
`ui_token()` :110, header `X-Recall-UI` (`UI_TOKEN_HEADER` :103), `classify_request()` :114 -> `("ui","ui_token")` or `("unknown",None)`;
`X-Recall-Actor` never changes it; since 4.22.0 the approval gate reads the class (D ceilings, the `ui` line of the dialog; `services-infra.md`). The shell renders it in `<meta name="recall-ui-token">` only when `is_local_trusted()`.

## app/state.py
**Purpose:** module-level singletons for blueprints without circular imports (single-process).
**Entry points:** `init(**kwargs)` :79 — sets in bulk; an unknown key → `KeyError`.
**Singletons:** whisper_manager, executor (batch pool), live_executor (live-critical, recording_finalize only), job_queue,
download_progress, process_logs, sse_broker, active_library_transcriptions, system_monitor, file_cleanup, metrics, limiter,
ffmpeg_available, cfg, робота, recording_service, recording_recovery_log, live_transcribe_worker, copilot_service,
copilot_worker, recording_autosave (4.23.0, :65, `None` = off), add_log, RATE_LIMIT_{TRANSCRIBE,YOUTUBE,POLISH}.
**Gotchas:** blueprints do not import app.py — only `from app import state as _state`.

## whisper_manager_new.py
**Purpose:** the transcription engine with pluggable backends; LRU model cache (~3), inference under a semaphore, GPU→CPU fallback on OOM.
**Entry points:** `get_available_models()` (from `faster_whisper._MODELS`), `download_model`, `load_model`,
`transcribe_with_progress(audio,model,language,task,cb,decode_options=None)`, `get_audio_duration`, `get_system_info`,
`reset_cuda`, `cleanup_temp_files`; methods on `ModernWhisperManager` :420.
**Types:** `WhisperBackend(ABC)` :121 → `OpenAIWhisperBackend` :153 (fallback, 5-min chunks+overlap), `FasterWhisperBackend` :196
(float16 GPU/int8 CPU, Silero VAD, BatchedInferencePipeline >600s). `_select_default_backend` :393, `_create_backend` :406.
`_models: OrderedDict`, `_load_lock`, `_inference_semaphore` (size from `WHISPER_MAX_PARALLEL`, :445).
**4.27.0:** `faster_cache_dir()` :85 and `faster_model_repo(name)` :91 (module-level) let `services/model_download.py` fetch a model with `faster_whisper.utils.download_model` without `_load_lock`; `ModernWhisperManager.download_model` :589 (load on CPU under `_load_lock`) remains the openai-backend path.
**Gotchas:**
- faster-whisper streams segments lazily — if you don't consume the generator, you lose the progress callback. OOM → sync GPU, CPU fallback, reload.
- `_decode_kwargs(handle, opts, batched)` :227: `hotwords`/`initial_prompt` — names joined within `max_length//2-1` (223) tokens
  (`_join_names_within_budget` :99); sequential `hotwords` sets `condition_on_previous_text=False`; `chunk_length` only batched. Empty
  options → `{}`; the OpenAI backend ignores them. Source: `app/services/vocabulary.py`.
