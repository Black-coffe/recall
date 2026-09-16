# overview — архітектура та процеси

## Три процеси

Recall — це **монолітний Flask-сервер** + **2 окремі дочірні процеси**:

1. **`app.py`** — головний Flask (порт 5050). Усі HTTP API, фонові задачі,
   запис, ко-пілот, RAG. Глобальний стан — у `app/state.py` singleton-ах.
2. **`telegram_listener.py`** — окремий процес (Telethon/MTProto). Підіймається
   `app.py` через `subprocess.Popen` (якщо `TELEGRAM_ENABLED` + є `telegram.session`).
   Контрол-API на localhost:5051; шле повідомлення назад у Flask `/api/telegram/ingest`.
3. **`mcp_server.py`** — окремий MCP-сервер: **read-first** міст до Recall для
   Claude Code/Desktop (32 read-only тулзів; write-поверхня прибрана у Волні 1). Stdio-конектор (**default**) спавнить сам клієнт
   (`claude mcp add`, user-scope) — окремо піднімати не треба; http+ключ — опційно.
   Гібрид: читає SQLite напряму, а записи/live/AI-дії проксіює на запущений `app.py`.

Усі три читають одну SQLite БД (`whisper_history.db`) та спільні модулі
`app/services/*` напряму. MCP читає БД без Flask, але для записів/live/AI-дій
проксіює HTTP на запущений `app.py` (гібрид).

## Послідовність запуску (`app.py`)

1. `.env` → logging (RotatingFileHandler + console) → глушіння шумних бібліотек.
2. Flask + CORS (тільки localhost:5050) → створення папок (upload/transcripts/
   youtube/documents/telegram_media/recordings).
3. `whisper_manager` (бекенд auto-detect: faster > openai).
4. БД + міграції (`app/db/migrations.py`).
5. Фонові демони: `system_monitor`, `model_updates` (staleness-gated), опц. `file_cleanup`.
6. `recording_service` + recovery осиротілих сесій (якщо `RECORDING_ENABLED`).
7. `live_transcribe_worker` (model='small', interval 8s) — якщо є recording_service.
8. `copilot_service` + `copilot_worker` — якщо `COPILOT_ENABLED` + LLM доступний.
9. `app.state.init(**singletons)` — інʼєкція всіх singleton-ів.
10. Реєстрація 14 blueprints (БЕЗ url_prefix — повні шляхи в маршрутах).
11. Прикручування rate-limiter-ів до конкретних view-функцій.
12. Спавн telegram_listener (gated `WERKZEUG_RUN_MAIN`, щоб reloader не дублював).
13. `app.run(threaded=True)` — кожен SSE-конект у власному потоці.

**Shutdown** (atexit): stop system_monitor → stop file_cleanup →
`executor.shutdown(wait=True)` → kill telegram_listener → flush логів.

## Наскрізні механізми

- **SSE**: in-memory pub/sub `sse_broker`. Endpoint `/api/events/<process_id>`.
  `ThreadSafeProgressStore` і `ThreadSafeProcessLogs` авто-публікують у broker.
  Термінальні події (`complete`/`error`) закривають конект. **Фронт читає SSE через
  fetch+reader, НЕ EventSource** (werkzeug keep-alive deadlock — див. frontend.md).
- **Фонові задачі**: `job_queue` поверх `ThreadPoolExecutor(max_workers=2)`. State-машина
  queued→running→completed/failed/cancelled. Cancel кооперативний (`job.is_cancelled()`).
- **Логи**: `whisper_app.log` (10MB×5). Глушаться anthropic/httpx/hf; torchaudio→CRITICAL.
- **Rate-limit**: flask_limiter memory-backed, по IP. 429 → JSON.
- **Безпека**: security-headers тільки на HTML (CSP не на JSON/файли, щоб не ламати fetch);
  path-traversal guard; bind за замовчуванням `127.0.0.1` (localhost-only; на весь LAN —
  лише opt-in через `RECALL_BIND_ALL=1`/`FLASK_HOST=0.0.0.0`, з warning у логах —
  аутентифікації поки нема, T1.2); shared-secret для telegram IPC.

## БД (SQLite, `whisper_history.db`)

Ключові таблиці: `transcriptions` (центральна — усі джерела: file/youtube/recording/
document/telegram/copilot/meeting_archive), `audio_downloads` (Аудіотека), `segments`/
speaker-map, `chunks`+embeddings (RAG), `categories`/`entities`/`meeting_entities`/
`action_items`/`entity_aliases` (граф памʼяті), `segment_bookmarks`, `saved_searches`,
`tg_monitored_chats`, `copilot_sessions`/`topics`/`events`, `ask_log` (v41 — лог питань
UI+MCP). Доступ — контекст-менеджер `get_db_connection()`. Міграції — `app/db/migrations.py`
(поточна **v41**: v40 `transcriptions.duplicate_of` — дублі аудіо/YouTube; v41 `ask_log` —
питання/канал/скоуп/джерела/токени/вартість/оцінка власника, Хвиля A production-RAG 16.09.2026).

## Windows-специфіка

- WASAPI-запис через `pyaudiowpatch` (Linux/macOS → recording auto-disabled).
- Forward-slash у коді; Pathlib транслює. UTF-8 reconfigure stdout у telegram-скриптах.
- FFmpeg у PATH (відсутній → YouTube вимкнено, решта працює).
- `os.startfile()` для QR-PNG у telegram_login (грейсфул-фолбек на іншій ОС).
