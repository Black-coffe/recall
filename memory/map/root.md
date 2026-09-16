# root — кореневі entry points

## app.py (1190)
**Призначення:** головний Flask-сервер. Boot, реєстрація blueprints, фонові сервіси,
глобальні singleton-и, HTTP-lifecycle.
**Entry points:** `get_db_connection()`, `init_database()`, `add_process_log(pid,stage,msg,progress,status)`,
`get_process_logs(pid,last_n)`, `trim_audio_file(...)`, `extract_audio_from_video(...)`,
`_download_youtube_core(url,id,save_to_library,quality,times)` (адаптер DI), `_maybe_launch_telegram_listener()`,
`check_ffmpeg_on_startup()`, `cleanup()` (atexit).
**Типи/стан:** `ThreadSafeProgressStore` (LRU max 100 + SSE), `ThreadSafeProcessLogs` (max 50 PID).
Усі singleton-и створюються тут і йдуть у `app.state.init()`.
**Gotchas:** telegram_listener спавниться **один раз** (gate `WERKZEUG_RUN_MAIN`, інакше reloader дублює).
SSE-публікація поза локом. `_recording_finalize_callback` кладе finalize в job_queue (non-blocking) → 'finalized' через SSE.
CSP тільки на HTML.
**Профіль/headless (спек `config-registry-profiles`, S2):** при `cfg.HEADLESS` blueprint recorder
не піднімається — `RecordingService`/`LiveTranscribeWorker`/`CopilotWorker`-гілки взагалі не
торкаються `app.services.recording.*` (лог `"profile=headless: recorder off"`), бо `cfg.RECORDING_ENABLED`
форсовано `False` ще у `config.py` (нижче), без спроби `import pyaudiowpatch`. Кожна фаза boot-у
пише в `app.state.робота` (dict з ключами `database/job_queue/whisper/embeddings/telegram/boot_finished`
— `migrations` НЕ входить у контракт, `app/state.py:45-49` пояснює чому: виняток із
`app/db/migrations.py` вбиває процес до робота, тож стан 'error' для нього недосяжний):
`database` одразу після `init_database()`, `job_queue` після `recover_crashed()`,
`whisper.preload` через фоновий watcher-потік, що `join()`-ить
`whisper_preload.start_tracked_preload()` (app.py:625-626, логіка прогріву винесена в
`app/services/whisper_preload.py`, стани `disabled|loading|ready|failed`),
`telegram` — всередині `_maybe_launch_telegram_listener()` (лишається живим і в headless — це і є
headless-інжест, не вимикається), `boot_finished=True` в самому кінці боту (app.py:1162).
`RECALL_LOG_FORMAT=json` (дефолт `text`) перемикає обидва
логер-хендлери (file+stream) на `JsonFormatter` (`app/core/logger.py`) до їх створення. Це читає
`GET /api/ready` (blueprint `system`, див. `memory/map/blueprints.md`) — 200 коли
`database=='ok' ∧ job_queue.bound ∧ whisper.preload in ('ready','disabled') ∧ boot_finished`, інакше 503.

## config.py (537)
**Призначення:** ієрархія конфігів Development/Production/Testing; `.env`-побічні ефекти
виконуються ліниво при першому `get_config()` (деталі — Gotchas нижче, вже НЕ на етапі
визначення класу).
**Entry points:** `get_config()`, `update_config(**kw)`, `init_directories()`.
**Ключові прапори:** `WHISPER_BACKEND` (faster>openai), `WHISPER_MAX_PARALLEL=1`, `WHISPER_BATCH_SIZE=8`,
`FORCE_CPU`, `RECORDING_ENABLED` (auto-detect pyaudiowpatch), `TELEGRAM_*`, `COPILOT_ENABLED`,
`LOCAL_LLM_URL`/`LOCAL_LLM_MODEL` (Ollama, напр. qwen2.5:14b-instruct-q5_K_M), `MAX_CONTENT_LENGTH=20GB`.
`cfg.PROFILE` (`'desktop'|'headless'`, з `RECALL_PROFILE`) і `cfg.HEADLESS: bool` — у headless
`RECORDING_ENABLED`/`RECORDING_VIDEO_ENABLED` завжди `False`.
**Реєстр (`app/core/settings.py`, спек `config-registry-profiles` S1):** єдине джерело істини для
132 env-флагів (назва/дефолт/тип/група/`gates`/профіль) — `REGISTRY`, `by_name()`, типізовані
`env/env_bool/env_int/env_float`, `profile()`, `render_env_example()` (CLI
`python -m app.core.settings env-example` генерує `.env.example`). Модуль без важких залежностей
(не імпортує `app.*`/`flask`/`torch`/`config` — памʼятка `mcp-stdio-no-heavy-models`); `config.py`
бере скалярні дефолти звідти замість дубльованих літералів (три свідомі винятки на прямому
`os.environ.get`: `TELEGRAM_SESSION`, `TELEGRAM_API_ID`, `_detect_telegram_enabled` — див.
`## Implementation notes` story 01).
**Gotchas:** `_detect_recording_enabled()`/`_detect_telegram_enabled()` і перевірка `SECRET_KEY`
(hard-fail/автоген/запис у `.env`) **більше не на імпорті** — перенесені у `_initialize_dynamic_config()`,
що виконується лінивo один раз при **першому** `get_config()` (прапорець `_config_initialized`).
`import config` сам по собі більше не імпортує `pyaudiowpatch`/`telethon` і не кидає/не пише `.env`.
`WHISPER_MAX_PARALLEL=1` бо `WhisperModel.transcribe()` НЕ тред-сейф на спільному handle (>1 → краш 0xC0000409).

## whisper_manager_new.py (867)
**Призначення:** двигун транскрипції з pluggable-бекендами (faster-whisper / openai-whisper);
LRU-кеш моделей (~3), інференс під семафором, GPU→CPU фолбек на OOM.
**Entry points:** `get_available_models()` (динамічний каталог із `faster_whisper._MODELS`),
`download_model(name)`, `load_model(name)`, `transcribe_with_progress(audio,model,language,task,cb)`,
`get_audio_duration()`, `get_system_info()`, `reset_cuda()`, `cleanup_temp_files()`.
**Типи:** `WhisperBackend(ABC)` → `FasterWhisperBackend` (float16 GPU/int8 CPU, Silero VAD,
BatchedInferencePipeline для >600s) + `OpenAIWhisperBackend` (фолбек, 5-хв чанки+overlap).
`_models: OrderedDict` (LRU), `_load_lock`, `_inference_semaphore`.
**Gotchas:** faster-whisper **стрімить** сегменти лениво — не споживаєш генератор → втрачаєш progress-callback.
Каталог моделей оновлюється сам при апгрейді бібліотеки. OOM → sync GPU, CPU-фолбек, reload.

## telegram_listener.py (560)
**Призначення:** standalone-процес. Telethon MTProto-сесія, слухає monitored-чати, тягне медіа,
POST-ить у Flask `/api/telegram/ingest`. + контрол-API (localhost:5051) для Flask.
**Entry points:** `run_listener(cfg)` (async loop + refresh кожні 30s + control-API),
control: `GET /status`, `GET /dialogs`, `POST /backfill`. CLI: `list|enable <id>|disable <id>|status`.
**Gotchas:** окремий процес із успадкованими stdout/stderr. Asyncio один потік — контрол-хендлери
через `run_coroutine_threadsafe`. Дедуп по (chat_id,message_id) на боці Flask. FloodWait → throttle 0.5s.
Telethon-сесію не відкрити двома клієнтами — Flask ходить лише по HTTP.

## telegram_login.py (138)
**Призначення:** одноразовий інтерактивний QR-логін (+ 2FA). Створює `telegram.session`.
**Gotchas:** QR-PNG містить токен → видаляється після логіну/таймауту. 2FA-промпт треба
в реальному терміналі (subprocess не побачить ввід). `os.startfile()` Windows-специфіка.

## telegram_common.py (44)
**Призначення:** спільний shared-secret для localhost Flask↔listener.
**Entry points:** `control_token()`, конст. `CONTROL_TOKEN_HEADER="X-Telegram-Token"`.
**Gotchas:** токен-файл O_CREAT|O_EXCL (race-free), mode 0o600. Тільки для localhost IPC, не WAN.

## mcp_server.py (785)
**Призначення:** окремий MCP-сервер — **read-first** міст до Recall для Claude Code/Desktop.
**32 read-only тулзів**, жодного create/update/delete (стратегічне рішення власника 02.07.2026;
38 write-тулзів фізично видалено у Волні 1 — карта до цього стверджувала «~80 tools, CRUD-міст»).
Число тулзів у `about()` рахується з реєстру FastMCP (`_registered_tool_names()`), а не хардкодиться.
**Гібрид:** 15 тулзів читають SQLite напряму (`_ABOUT_DIRECT_DB`, працюють БЕЗ запущеного app.py,
серед них `grep_archive`), 17 — httpx-проксі на `app.py` (`_ABOUT_PROXY`, `RECALL_API_URL`,
default `http://127.0.0.1:5050`) через `_api()`, щоб не тягнути torch/e5 у stdio-процес.
**Зобовʼязання (Трек 1):** `weekly_digest`, `list_action_items(window/owner/category/status)`,
`list_dropped_commitments`, `list_stale_topics` — поверх `app.services.commitments`.
**Зріз (Трек 2):** `ask_archive(project=…)` звужує до проєкту/людини. `ask_archive` шле
`channel="mcp"` у `/api/memory/ask` (Хвиля A production-RAG, 16.09.2026) — питання з MCP і
UI осідають в одній таблиці `ask_log` (сировина для golden-set, `evals/build_golden.py
--from-ask-log`); оцінка (👍/👎) з MCP не збирається, нових write-тулзів для цього не додано.
**Grep (grep-explainability, S2):** `grep_archive(pattern, regex=, ignore_case=, context=, limit=,
source_type=, transcription_id=, days=)` — буквальний/regex-пошук ТОЧНОГО РЯДКА по `chunks`
(без ембеддингів, без ранжування) поверх `app.services.archive_grep.grep()`; для ID/сум/@ніків,
які FTS5-токенізація й reranker ховають.
**Транспорти:** stdio (**default**, клієнт сам спавнить) або http (`--transport http`, Bearer).
**Resources:** `recall://about` (огляд+статистика+карта тулзів), `recall://transcript/{id}`,
`recall://entity/{id}`.
**Env:** `RECALL_API_URL`, `RECALL_MCP_READONLY=1` (аварійний блок записів — зараз ні на що не
впливає, бо записів нема), `RECALL_MCP_KEY` (лише http), `RECALL_MCP_DEBUG_LOG=1` (лог кожного
виклику в `logs/mcp_calls.log` — увімкнено 24.07.2026; до того 488 викликів пішли в нікуди).
`.env` вантажиться з каталогу скрипта (cwd-незалежно → user-scope конектор з будь-якого проєкту).
**Gotchas:** ML-імпорти (retrieval→torch) ЛІНИВІ — інакше stdio health-check Claude Code
впирається в таймаут. `claude mcp add ... -- ... --transport stdio` ВІДКИДАЄ трейлінг-аргументи
→ саме тому stdio зроблено дефолтом. Proxy-тулзи потребують запущеного app.py (інакше грейсфул
`{"error": ...}`). `_safe_original_path()` — анти-traversal проти allowed-dirs. http без
`RECALL_MCP_KEY` → відмова старту.

## app/state.py (64)
**Призначення:** module-level singleton-и для blueprints без циклічних імпортів.
**Entry points:** `init(**kwargs)` — масово сетить усе (виклик з app.py).
**Singletons:** whisper_manager, executor, job_queue, download_progress, process_logs, sse_broker,
active_library_transcriptions, system_monitor, file_cleanup, metrics, limiter, ffmpeg_available, cfg,
add_log, recording_service, recording_recovery_log, live_transcribe_worker, copilot_service, copilot_worker,
RATE_LIMIT_*.
**Gotchas:** blueprints **не імпортують app.py** — лише `from app import state as _state`. Single-process pattern.
