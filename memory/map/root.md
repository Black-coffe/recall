# root — core: app.py boot, config, state, whisper

last-verified: 2026-10-16
Adjacent slice: `root-processes.md` (telegram_*, mcp_server.py).

## app.py
**Purpose:** the main Flask server: boot, blueprints, background services, singletons, HTTP lifecycle.
**Entry points:** `get_db_connection()` :80, `init_database()` :85, `add_process_log(pid,stage,msg,progress,status)` :458,
`_download_youtube_core(url,id,save_to_library,quality,start_time,end_time)` :507 (DI adapter), `check_ffmpeg_on_startup()` :554,
`cleanup()` :578 (atexit), `_maybe_launch_telegram_listener()` :1146, `_maybe_start_call_watcher()` :1189, `_maybe_start_pipes()` :1250.
**Types:** `ThreadSafeProgressStore` :92 (LRU 100 + SSE), `ThreadSafeProcessLogs` :171. All singletons are assembled here → `app.state.init()`.
**Gotchas:**
- telegram_listener is spawned once (gate `WERKZEUG_RUN_MAIN`); SSE publishing happens outside the lock; CSP on HTML only.
- `RecordingService(max_active=cfg.RECORDING_MAX_ACTIVE, app_source_factory=...)` :843; `_app_source_factory` :838 builds
  `AppAudioSource` with the resolver `session_roots(exe, fallback=resolve_root_pids)`.
- Headless (`cfg.HEADLESS`): the recorder/live/copilot branches do not touch `app.services.recording.*` (log
  "profile=headless: recorder off"); telegram ingestion stays alive.
- The background services `_maybe_start_call_watcher()` and `_maybe_start_pipes()` are called only under `__main__`
  (:1320-1321; with debug — only in the reloader child `WERKZEUG_RUN_MAIN=true`), they do not start under pytest.
- Call watcher (K2): `CallWatcher` with `RECORDING_AUTOPROMPT*`/`RECORDING_AUTOSTOP_SILENCE_MIN`,
  `stop_fn=blueprints.recording.stop_session`, log `logs/call_watch.jsonl`, → `app.extensions['call_watcher']`.
  Not Windows / no pycaw / `RECORDING_AUTOPROMPT=0` / no `recording_service` → a log line and exit.
- Pipes (K4/K5): `PIPES_ENABLED=0` → log and exit. `PipeScheduler(load_pipes(), db_path, deliver=tg_bot.send, buttons=True)` :1265
  → `app.extensions['pipe_scheduler']`. Bot not configured → warning, the scheduler stays alive. With `tg_bot.is_configured()`
  `ButtonPoller(db_path)` :1277 → `app.extensions['brief_buttons']` (daemon long-poll; failure → warning). Logic — `services.md`.
- `RECALL_LOG_FORMAT=json` switches both logger handlers to `JsonFormatter` (`app/core/logger.py`).
**Робота (`app.state.робота`, read by `GET /api/ready`, see `blueprints.md`):** keys
`database/job_queue/whisper/embeddings/telegram/boot_finished`; `migrations` is NOT included (`app/state.py:45-49`).
`database` after `init_database()`; `job_queue` after `recover_crashed()`; whisper — `start_tracked_preload(whisper_manager,
_state.робота)` :638 (`app/services/whisper_preload.py`, states `disabled|loading|ready|failed`); `telegram` in
`_maybe_launch_telegram_listener()`; `boot_finished=True` :1290. `/api/ready`: 200 if `database=='ok' ∧ job_queue.bound ∧
whisper.preload ∈ (ready,disabled) ∧ boot_finished`, otherwise 503.

## config.py
**Purpose:** `Config` :30 → `DevelopmentConfig` :330 / `ProductionConfig` :337 / `TestingConfig` :357.
**Entry points:** `get_config()` :500, `update_config(**kw)` :518, `init_directories()` :525.
**Flags:** `WHISPER_BACKEND` (faster>openai) :165, `WHISPER_MAX_PARALLEL` :168, `WHISPER_BATCH_SIZE` :170, `FORCE_CPU` :159,
`RECORDING_ENABLED` (auto-detect pyaudiowpatch), `RECORDING_MAX_ACTIVE` (min 1) :209, `COPILOT_ENABLED` :314,
`LOCAL_LLM_URL`/`LOCAL_LLM_MODEL` :315, `MAX_CONTENT_LENGTH` 20GB (Testing 10MB :363). `PROFILE` ('desktop'|'headless', from
`RECALL_PROFILE`) and `HEADLESS` are set by `_initialize_dynamic_config()` :478-487; in headless `RECORDING_ENABLED`/
`RECORDING_VIDEO_ENABLED` are always False.
**Gotchas:**
- Side effects (`_detect_recording_enabled`, `_detect_telegram_enabled`, `SECRET_KEY` hard-fail/autogen/write to `.env`)
  run lazily once at the first `get_config()` (`_config_initialized` :474). `import config` does not pull in
  `pyaudiowpatch`/`telethon` and does not write `.env`.
- `WHISPER_MAX_PARALLEL=1`: `WhisperModel.transcribe()` is not thread-safe on a shared handle (>1 → crash 0xC0000409).
**Registry (`app/core/settings.py`):** the single source of 168 env flags (+`RECALL_UI_LANG` `auto|uk|en`, :77; `tests/conftest.py:14`
forces `auto` in every test, because the owner's `.env` is loaded by `config.py`/`app.py`) (count by `^    Setting(`): `REGISTRY`, `by_name()`,
`env/env_bool/env_int/env_float`, `profile()`, `render_env_example()` (CLI `python -m app.core.settings env-example`).
Setting descriptions are English (4.17.0). No heavy dependencies (does not import `app.*`/`flask`/`torch`/`config`). Groups: `«Труби»` (`PIPES_ENABLED`=0, `PIPES_RETRY_MINUTES`,
`PIPES_RETRY_MAX`, `PIPES_MODEL`, `TELEGRAM_BOT_TOKEN` secret, `TELEGRAM_BOT_CHAT_ID`); `RECALL_PUBLIC_URL`; `«Auth / доступ»`,
`RECALL_PROXY_ALLOWED_IPS`.
**Auth gate (`app/core/auth.py`):** one `before_request` on `/api/*` (exempt: OPTIONS, non-`/api/`, `/api/health`, `/api/ready`).
`RECALL_LOCAL_TRUSTED` + localhost lets only a direct request through without a key. With `X-Forwarded-For`/`Tailscale-User-Login` —
proxied (`_proxied_client`, `auth.py:100`): let through only if the single XFF address ∈ `RECALL_PROXY_ALLOWED_IPS`
(a chain → unknown), otherwise `RECALL_API_KEY` (without it 401). Check: `scripts/check_proxy_trust.py`.

## app/state.py
**Purpose:** module-level singletons for blueprints without circular imports (single-process).
**Entry points:** `init(**kwargs)` :79 — sets in bulk; an unknown key → `KeyError`.
**Singletons:** whisper_manager, executor (batch pool), live_executor (live-critical, recording_finalize only), job_queue,
download_progress, process_logs, sse_broker, active_library_transcriptions, system_monitor, file_cleanup, metrics, limiter,
ffmpeg_available, cfg, робота, recording_service, recording_recovery_log, live_transcribe_worker, copilot_service,
copilot_worker, add_log, RATE_LIMIT_{TRANSCRIBE,YOUTUBE,POLISH}.
**Gotchas:** blueprints do not import app.py — only `from app import state as _state`.

## whisper_manager_new.py
**Purpose:** the transcription engine with pluggable backends; LRU model cache (~3), inference under a semaphore, GPU→CPU fallback on OOM.
**Entry points:** `get_available_models()` (from `faster_whisper._MODELS`), `download_model`, `load_model`,
`transcribe_with_progress(audio,model,language,task,cb,decode_options=None)`, `get_audio_duration`, `get_system_info`,
`reset_cuda`, `cleanup_temp_files`; methods on `ModernWhisperManager` :420.
**Types:** `WhisperBackend(ABC)` :121 → `OpenAIWhisperBackend` :153 (fallback, 5-min chunks+overlap), `FasterWhisperBackend` :196
(float16 GPU/int8 CPU, Silero VAD, BatchedInferencePipeline >600s). `_select_default_backend` :393, `_create_backend` :406.
`_models: OrderedDict`, `_load_lock`, `_inference_semaphore` (size from `WHISPER_MAX_PARALLEL`, :445).
**Gotchas:**
- faster-whisper streams segments lazily — if you don't consume the generator, you lose the progress callback.
- OOM → sync GPU, CPU fallback, reload.
- `_decode_kwargs(handle, opts, batched)` :227: `hotwords`/`initial_prompt` — names joined on a name boundary within
  `max_length//2-1` (223) tokens (`_join_names_within_budget` :99); the sequential path with `hotwords` sets
  `condition_on_previous_text=False`; `multilingual` → kwarg; `chunk_length` only batched. Empty options → `{}`.
  The OpenAI backend ignores the options. The source of the options is `app/services/vocabulary.py`.
