# root — кореневі entry points

last-verified: 2026-10-15 (app.py труби, mcp_server.py list_pipe_runs, реєстр 165); 2026-10-13 (config.py); решта розділів — раніше
(файл >80 рядків — давній борг)

## app.py (1288)
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
`RecordingService(max_active=cfg.RECORDING_MAX_ACTIVE, app_source_factory=_app_source_factory)` (app.py:843-853);
фабрика будує `AppAudioSource` з резолвером `session_roots(exe, fallback=resolve_root_pids)` (app.py:832-841).
CSP тільки на HTML.
`_maybe_start_call_watcher()` (app.py:1189) — K2: збирає `CallWatcher` (apps/seconds/autostop з
`RECORDING_AUTOPROMPT*`/`RECORDING_AUTOSTOP_SILENCE_MIN`, `stop_fn=blueprints.recording.stop_session`, лог
`logs/call_watch.jsonl`) у `app.extensions['call_watcher']`. Кличеться лише під `__main__` (app.py:1285; при debug —
лише в reloader-child `WERKZEUG_RUN_MAIN=true`), тож під pytest потік не стартує. Не Windows / нема pycaw /
`RECORDING_AUTOPROMPT=0` / нема `recording_service` → рядок у лозі й нічого більше.
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
headless-інжест, не вимикається), `boot_finished=True` в самому кінці боту (app.py:1256).
`RECALL_LOG_FORMAT=json` (дефолт `text`) перемикає обидва
логер-хендлери (file+stream) на `JsonFormatter` (`app/core/logger.py`) до їх створення. Це читає
`GET /api/ready` (blueprint `system`, див. `memory/map/blueprints.md`) — 200 коли
`database=='ok' ∧ job_queue.bound ∧ whisper.preload in ('ready','disabled') ∧ boot_finished`, інакше 503.

**Труби (K4, 4.13.0):** `_maybe_start_pipes()` (app.py:1250) — під `__main__` разом із `_maybe_start_call_watcher()`
(app.py:1309-1311, лише в процесі, що обслуговує запити; під pytest не стартує). `PIPES_ENABLED=0` → рядок у лозі й вихід.
Збирає `PipeScheduler(load_pipes(), db_path=app.config['DATABASE'], deliver=tg_bot.send)` → `app.extensions['pipe_scheduler']`,
`.start()`. Бот не налаштований → лише warning, планувальник усе одно йде (запуски пишуться в історію як помилка). Виняток
при збірці → warning і return. Логіка — `services.md` → pipes.py.

## config.py (537)
**Призначення:** ієрархія конфігів Development/Production/Testing; `.env`-побічні ефекти
виконуються ліниво при першому `get_config()` (деталі — Gotchas нижче, вже НЕ на етапі
визначення класу).
**Entry points:** `get_config()`, `update_config(**kw)`, `init_directories()`.
**Ключові прапори:** `WHISPER_BACKEND` (faster>openai), `WHISPER_MAX_PARALLEL=1`, `WHISPER_BATCH_SIZE=8`,
`FORCE_CPU`, `RECORDING_ENABLED` (auto-detect pyaudiowpatch), `RECORDING_MAX_ACTIVE` (дефолт 2, мін 1), `TELEGRAM_*`, `COPILOT_ENABLED`,
`LOCAL_LLM_URL`/`LOCAL_LLM_MODEL` (Ollama, напр. qwen2.5:14b-instruct-q5_K_M), `MAX_CONTENT_LENGTH=20GB`.
`cfg.PROFILE` (`'desktop'|'headless'`, з `RECALL_PROFILE`) і `cfg.HEADLESS: bool` — у headless
`RECORDING_ENABLED`/`RECORDING_VIDEO_ENABLED` завжди `False`.
**Реєстр (`app/core/settings.py`, спек `config-registry-profiles` S1):** єдине джерело істини для
165 env-флагів (рахунок `^    Setting(` у `settings.py`; 4.13.0 K4: група «Труби» — `PIPES_ENABLED`=0, `PIPES_RETRY_MINUTES`=15, `PIPES_RETRY_MAX`=6, `PIPES_MODEL`=claude-sonnet-5, `TELEGRAM_BOT_TOKEN` (secret), `TELEGRAM_BOT_CHAT_ID`, `settings.py:657-680`) (назва/дефолт/тип/група/`gates`/профіль) — `REGISTRY`, `by_name()`, типізовані
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
**K1 (4.8.0):** `transcribe_with_progress(..., decode_options=None)` → `FasterWhisperBackend._decode_kwargs(handle,
opts, batched)` (whisper_manager_new.py:227): `hotwords`/`initial_prompt` — список імен склеєний по межі імені в
`max_length//2-1` (223) токенів; послідовний шлях з `hotwords` ставить `condition_on_previous_text=False` (інакше
промпт >448 токенів → CTranslate2 падає); `multilingual` → kwarg; `chunk_length` лише batched. Порожні опції → `{}`
(виклик як до K1). OpenAI-бекенд опції ігнорує (debug-лог). Джерело опцій — `app/services/vocabulary.py`.

## telegram_listener.py (560)
**Призначення:** standalone-процес. Telethon MTProto-сесія, слухає monitored-чати, тягне медіа,
POST-ить у Flask `/api/telegram/ingest`. + контрол-API (localhost:5051) для Flask.
**Entry points:** `run_listener(cfg)` (async loop + refresh кожні 30s + control-API),
control: `GET /status`, `GET /dialogs`, `POST /backfill`. `/dialogs` кожен рядок несе `_identity_fields(ent)`
(telegram_listener.py:449): `first_name/last_name/phone/bot/deleted` (None у груп/каналів; phone — цифри без «+»). CLI: `list|enable <id>|disable <id>|status`.
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
**41 read-only тулз** (4.13.0: +`list_pipe_runs`, mcp_server.py:1153; 4.10.0: +5 відео-проксі), жодного create/update/delete (стратегічне рішення власника 02.07.2026;
38 write-тулзів фізично видалено у Волні 1 — карта до цього стверджувала «~80 tools, CRUD-міст»).
Число тулзів у `about()` рахується з реєстру FastMCP (`_registered_tool_names()`), а не хардкодиться.
**Гібрид:** 17 тулзів читають SQLite напряму (`_ABOUT_DIRECT_DB`, mcp_server.py:2210, працюють БЕЗ запущеного app.py,
серед них `grep_archive`, `list_pipe_runs`), 24 — httpx-проксі на `app.py` (`_ABOUT_PROXY`, `RECALL_API_URL`,
default `http://127.0.0.1:5050`) через `_api()`, щоб не тягнути torch/e5 у stdio-процес.
`editable-title-description`-04 (4.5.0): `list_recent`/`get_transcript`/`search_archive`/
`ask_archive` (sources), ресурс `recall://transcript/{id}` і експорт-заголовок несуть
`display_name` поруч із `source_name`; `get_transcript`/`list_recent` додають `title`/
`description`. Нових тулзів немає — свідоме звуження на запис: коментарний фід
`target_name`, згадки сутностей і `research_export` лишаються на `source_name`.
**Таксономія (`directions-projects-tags`, 4.6.0, 22-23.09.2026):** `list_categories` = дерево
`categories.list_tree` (кожен вузол несе `path` «Напрямок → Проєкт», унікальний за побудовою —
`resolve_node(path) == id`). 9 тулзів отримали `tags`/`since`/`until`:
`search_archive`/`list_recent`/`list_action_items`/`ask_archive`/`research_export`/
`research_preview`/`research_summary`/`search_history` (+ `category_id` у `grep_archive`); direct-DB
тулзи будують SQL лише через `app.services.categories.filter_sql/tags_sql/date_sql`. `ask_archive`
приймає `project=` як голу назву АБО повний шлях — на 409 `ambiguous` з HTTP повертає ноту з
`candidates` (перелік шляхів), не виняток. Тоді нових тулзів не додано (було 35; з 4.10.0 — 40).
**Відео (4.10.0, mcp_server.py:1446-1601, усі — проксі на app.py):** `video_analyze(id, method, step, min_res, threshold,
settle, ignore_regions, max_frames, ocr, describe, vision_backend)` → job (стан — `get_job_status`), `get_visual_timeline`,
`get_frame(id, t | from/to)` (картинки base64 + метадані; кілька кадрів — до 12), `get_frames_for_text`, `search_visual`.
Скіл `skills/recall-video/SKILL.md`; секрети в картинках/тексті вже замасковані (`services.md` → screen_secrets).
**Фільтр приватного (`mcp-visual-privacy`, 4.11.0, `mcp_server.py:1500-1543`):** усі 5 відео-тулз проходять `_visual_refusal(tid,
images=)` → `error_code` `MCP_VISUAL_OFF|MCP_VISUAL_TEXT_ONLY|PRIVACY_CHECK_FAILED|PRIVATE`; `_stop_words()` (None = файл не
прочитався → ховати все з текстом), `_visual_text_hidden`. `get_frame` ховає кадр без `frame_text` або зі стоп-словом
(`private_hidden`), `search_visual` перевіряє ПОВНИЙ OCR/опис з БД, не уривок. `search_archive` (`:577`) і `grep_archive`
(`_grep_hide_screen`, `:816`) прибирають чанки `speaker='екран'`; `ask_archive` — через `rag` канал `mcp`. Env: `RECALL_MCP_VISUAL`
(дефолт `images`), `RECALL_PRIVATE_CATEGORIES`, `RECALL_PRIVATE_WORDS_FILE`. Логіка — `services.md` → privacy.py.
**Труби (K4):** `list_pipe_runs(pipe=None, limit=20)` (limit клемпиться 1..200) → `{"runs": pipes.list_runs(DB_PATH,…)}`;
таблиці `pipe_runs` нема (`sqlite3.OperationalError`) → `{"runs": [], "reason": …}`. Тексти брифінгів історія не зберігає.
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
**Живі тулзи (4.7.0, ADR-026 D4):** `get_active_recording` завжди несе `count`+`sessions`;
`get_live_transcript`/`get_live_copilot`/`ask_live` без `session_id` при >1 живому записі повертають
`error_code:'AMBIGUOUS_SESSION'` зі списком сесій і сесію не вгадують (`_resolve_live_session`).
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
