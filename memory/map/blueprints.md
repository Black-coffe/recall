# blueprints — HTTP-шар (`app/blueprints/`)

14 blueprints, ~135 endpoints. **Реєструються БЕЗ `url_prefix`** — повний шлях у кожному
маршруті. Усі читають singleton-и через `from app import state as _state`. Помилки —
JSON `{success,error}` + HTTP-код (400/401/403/404/409/500/503).

## system.py (~390) — `/`, `/api/health`, `/api/ready`, `/api/models/*`, `/api/system_*`, `/api/polish/*`, `/api/metrics`
Health/робота, каталог/завантаження Whisper-моделей, системні метрики, shell-роутинг.
`GET /` — SPA-shell (catch-all, після /api/*). `GET /sw.js` (no-cache). `GET /api/health`,
`/api/models`, `POST /api/download_model`, `/api/models/{update-status,check-updates}`,
`/api/system_info`, `/api/system_stats`, `/api/polish/availability` (тіри Claude), `/api/metrics` (Prometheus).
`APP_VERSION = "4.0.1"` (system.py:37) — джерело для `version` у health/ready/system_info.
`GET /api/ready` (system.py:138-177, config-registry-profiles T03) — читає `app.state.робота`
(див. `memory/map/root.md`) + живий `_check_database_ready()` (106-136): відкриває ІСНУЮЧИЙ файл
БД (`mode=rw`, не автостворює) і вимагає таблицю `schema_versions` з `MAX(version) >= 1`.
200 коли `database=='ok' ∧ job_queue.bound ∧ whisper.preload in ('ready','disabled') ∧ boot_finished`,
інакше 503. `migrations` НЕ входить у формулу (недосяжний стан, `app/state.py:45-49`).
`GET /api/health` тепер кличе той самий `_check_database_ready()` (Round 3, історія 03) — не
створює БД як побічний ефект, як робив старий шлях через `get_db_connection`.

## transcription.py (~1600 — найбільший) — `/api/transcribe`, `/api/history/*`, `/api/transcription/<id>/*`, `/api/export/*`
Ядро: транскрипція file/youtube, історія, пост-обробка. `POST /api/transcribe`, `/api/transcribe/active`,
`GET /api/history`, `/api/history/<id>`, `DELETE`, `bulk_delete`, `bulk_export`, `<id>/audio`,
`<id>/segments/{merge,split}`, `<id>/{polish,summarize,sentiment,translate,topics}` (Claude SSE),
`POST /api/export/<format>` (txt/srt/json). Залежить: text_polishing, enrichment, whisper_manager.

## memory.py (~673) — `/api/memory/*`
Граф памʼяті + RAG. Категорії (CRUD/merge), `<id>/category`, `suggest-category` (k-NN), `bulk-category`,
`<id>/enrich`, `backfill`(+status, SSE-канал "backfill"), `import`, `GET search` (hybrid), `POST ask` + `ask/stream` (SSE),
`entities`(+`<id>`), `action-items`(+`<id>` PATCH), `stats`. Залежить: enrichment, retrieval, rag, embeddings.
`GET search`/`POST ask`/`POST ask/stream` усі приймають `explain` (query `?explain=1` або JSON
`{"explain": true}`, парситься `_parse_explain()`) — прокидається в `retrieval.search`/`rag.answer_question[_stream]`,
`why` виживає в `sources` кожного результату (T2, S2).
**Gotchas:** entity `min_meetings=2` (шумофільтр) якщо нема `q`; category casefold-уніка per-lang; action-items period по meeting_date.
Фільтр `owner` — ТОЧНИЙ збіг (значення приходить із чипа фасета) по канонічному імені АБО по аліасах графа; сире
`owner_name` звʼязаної задачі свідомо не фільтрує, інакше список стає довшим за число на чипі. Ключ аліаса рахує
`enrichment._normalize`, а не SQL `LOWER()` — той не згортає кирилицю. У MCP (`commitments.list_commitments`) правило
інше й навмисно ширше: підрядок по канонічному, сирому та аліасах — там вільний текст, а не чипи.

## recording.py (~571) — `/api/recording/*`, `/api/recordings/*`
Живий запис mic/loopback + finalize + recovery. `GET /devices` (5s cache), `POST /start`
(mic/system device + language + copilot), `<sid>/{pause,resume,stop,save,discard,state,stream(SSE)}`,
`/recordings/{active,recovered}`. Залежить: recording_service, live_transcribe_worker, copilot_worker.
**Gotchas:** finalize async (може >5хв на довгих); 409 якщо сесія вже активна; SSE level 10Hz; save deadline 5хв.

## audio_library.py (~329) — `/api/audio/*`
Аудіотека (YouTube + записи). `POST /download`, `GET /downloads` (page/filter), `DELETE /downloads/<id>`,
`check-duplicate`, `open-explorer/<id>`, `play/<id>`. **Gotchas:** Phase 21 категорія через
`COALESCE(transcript.category_id, audio_downloads.category_id)`; source_type=youtube|recording|file.

## youtube.py (~131) — `/api/youtube/*`
`POST /info`, `POST /download` (фон, rate-limited), `GET /progress/<id>`. Залежить: youtube_pytubefix.

## documents.py (~456) — `/api/documents/*`
`POST /upload` (sync-parse), `<id>/reparse`, `import-folder` (фон+SSE). Дедуп по content_hash;
OCR опц.; опис таблиць Claude. Залежить: document_parser, text_polishing.

## telegram.py (~760) — `/api/telegram/*`
`POST /ingest` (localhost+token), `GET /status`, `/dialogs`, `/coverage`, `/chats`, `POST /chats`,
`/backfill`, `POST /repair`, `POST /edited`, `POST /deleted`.
`POST /repair` (Волна 3) — точковий ремонт дірок по відсутніх id: `get_messages(ids=…)`
віддає `None` на видалених, тож «втратив слухач» відокремлено від «видалив автор».
Лише для супергруп `-100…` (у решті id з глобальної послідовності акаунта) —
для інших `/backfill` за датами. `POST /edited` / `POST /deleted` (Волна 4) —
правки і видалення: раніше архів писався один раз і не лагодився.
Медіа: рядок створюється синхронно заглушкою, job робить `UPDATE` (`_finalize_media`) —
інакше повідомлення «в польоті» невидиме для дедупу і watermark.
Auth shared-secret; media-path замкнено в TELEGRAM_MEDIA_DIR; дедуп (chat_id,message_id);
для backfill — embeddings без Claude (висока гучність).
`meeting_date` пишеться з `tg_date` (`_meeting_date`, локальний день) — без цього весь стек
датував TG моментом інжесту через `COALESCE(meeting_date, created_at)`; міграція v30 полагодила
всі 3360 наявних записів.
`GET /coverage` (Волна 1) — архів проти живого Telegram по кожному моніторенему чату:
`archived_last_date` / `live_last_date` / `lag_days` / `unread_count` / `migrated_to` + вердикт
`status` (ok|behind|never_ingested|migrated|not_listed|unknown). Живий курсор — з проходу
`iter_dialogs` у слухача (`Dialog.message`), без другої Telethon-сесії. **Gotcha:**
`behind_messages` лише для супергруп `-100…` — у legacy-групах і особистих чатах `message_id`
з глобальної послідовності акаунта (15 чатів з 19), арифметика по id там безглузда.
Слухач офлайн → 200 з `live:false` і архівною половиною (не 503, як `/dialogs`).

## copilot.py (~300) — `/api/copilot/*`
`POST /start`, `GET /sessions`, `<id>/{timeline,export,state}`, `POST <id>/{reingest,settings,action,stop}`,
`GET /by-transcription/<tid>`, `/availability`. **503 якщо COPILOT_ENABLED=False.** action:
dismiss|pin|thumbs_up/down|escalate. Settings мінять режим/важливість на льоту.

## speakers.py (~694) — `/api/speakers/*`, `/api/transcriptions/<id>/speakers`
Глобальні спікери (casefold-матчинг, кирилиця-сейф) + per-transcript діаризація.
`GET/POST /speakers`, `PUT/DELETE <id>`, `PATCH /transcriptions/<id>/speakers` (bulk map),
`/speakers/{stats,<id>/timeline,merge}`. **Gotchas:** casefold у Python (не SQLite NOCASE); ембеддинги усереднюються.

## bookmarks.py (~191) — `/api/transcription/<id>/bookmarks`, `/api/bookmarks/*`, `/api/saved-searches/*`
Закладки на сегментах + збережені пошуки. UNIQUE (transcription_id, segment_index); query як JSON.

## events.py (~82) — `/api/events/*`, `/api/process/logs/*`, `/api/jobs/*`
SSE-стрім + черга задач. `GET /api/events/<pid>` (SSE: progress/log/segment/complete/error),
`/api/process/logs/<pid>`, `/api/jobs`(+`<id>`, `POST <id>/cancel`).
**Gotchas:** SSE-headers `Cache-Control:no-cache`, `X-Accel-Buffering:no`, keep-alive.
