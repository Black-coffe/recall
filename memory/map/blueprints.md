# blueprints — HTTP-шар (`app/blueprints/`)

14 blueprints, ~135 endpoints. **Реєструються БЕЗ `url_prefix`** — повний шлях у кожному
маршруті. Усі читають singleton-и через `from app import state as _state`. Помилки —
JSON `{success,error}` + HTTP-код (400/401/403/404/409/500/503).

## system.py (~390) — `/`, `/api/health`, `/api/ready`, `/api/models/*`, `/api/system_*`, `/api/polish/*`, `/api/metrics`
Health/робота, каталог/завантаження Whisper-моделей, системні метрики, shell-роутинг.
`GET /` — SPA-shell (catch-all, після /api/*). `GET /sw.js` (no-cache). `GET /api/health`,
`/api/models`, `POST /api/download_model`, `/api/models/{update-status,check-updates}`,
`/api/system_info`, `/api/system_stats`, `/api/polish/availability` (тіри Claude), `/api/metrics` (Prometheus).
`APP_VERSION = "4.7.0"` (system.py:37) — джерело для `version` у health/ready/system_info.
`GET /api/ready` (system.py:138-177, config-registry-profiles T03) — читає `app.state.робота`
(див. `memory/map/root.md`) + живий `_check_database_ready()` (106-136): відкриває ІСНУЮЧИЙ файл
БД (`mode=rw`, не автостворює) і вимагає таблицю `schema_versions` з `MAX(version) >= 1`.
200 коли `database=='ok' ∧ job_queue.bound ∧ whisper.preload in ('ready','disabled') ∧ boot_finished`,
інакше 503. `migrations` НЕ входить у формулу (недосяжний стан, `app/state.py:45-49`).
`GET /api/health` тепер кличе той самий `_check_database_ready()` (Round 3, історія 03) — не
створює БД як побічний ефект, як робив старий шлях через `get_db_connection`.

## transcription.py (~1600 — найбільший) — `/api/transcribe`, `/api/history/*`, `/api/transcription/<id>/*`, `/api/export/*`
Ядро: транскрипція file/youtube, історія, пост-обробка. `POST /api/transcribe`, `/api/transcribe/active`,
`GET /api/history`, `/api/history/<id>`, `PATCH /api/history/<id>`, `DELETE`, `bulk_delete`, `bulk_export`, `<id>/audio`,
`<id>/segments/{merge,split}`, `<id>/{polish,summarize,sentiment,translate,topics}` (Claude SSE),
`POST /api/export/<format>` (txt/srt/json). Залежить: text_polishing, enrichment, whisper_manager.
`editable-title-description` (4.5.0): `GET /api/history` (список) і `GET /api/history/<id>` (деталь)
кожен елемент несе `title`/`description`/`display_name` (`record_meta.display_name()`) поруч із
`source_name`; `?q=` шукає і по `title`/`description` (FTS5, `transcriptions_fts` v44).
`directions-projects-tags` (4.6.0): `GET /api/history` приймає `category_id`/`tags`/`since`/`until`
(`app.services.categories`); кожен елемент несе `tags:[{id,name,color}]` і `suggested_tags:[names]`.
`PATCH
/api/history/<int:id>` — JSON `{"title"?: str|null, "description"?: str|null}`, жодного ключа → 400,
не-рядок у полі → 400, довжина > `TITLE_MAX`/`DESCRIPTION_MAX` → 400, нема запису → 404, інакше 200
`{"success": true, "record": {...,"display_name"}}`; викликає `record_meta.update_meta` →
`record_meta.after_meta_update(..., db_path=current_app.config["DATABASE"])` (точковий re-embed при
реальній зміні, `app/services/reembed.py`). `POST /api/transcribe` (multipart) приймає необовʼязкові
`title`/`description` для будь-якого `source_type` (нормалізація `record_meta`, довше за стелю → 400
до початку роботи; явне значення форми має пріоритет над копією з `audio_downloads`). Експорти
(`/api/export/<fmt>`, `bulk_export`) використовують `display_name` у заголовку й імені файлу; payload
без `title`/`source_name`/`id` — старий нейтральний фолбек, не `Запис #None`.

## memory.py (~1290) — `/api/memory/*`
Граф памʼяті + RAG. Категорії (CRUD/merge, дерево — див. нижче), `<id>/category`, `suggest-category`
(k-NN), `bulk-category`, теги (CRUD/merge, `<id>/tags` PUT, `<id>/tags/suggestions` POST),
`<id>/enrich`, `backfill`(+status, SSE-канал "backfill"), `import`, `GET search` (hybrid), `POST ask` + `ask/stream` (SSE),
`POST ask/<int:ask_id>/rate` (Хвиля A, 16.09.2026: `{rating: 1|-1, note?}` → `rag.rate_ask`,
записує в `ask_log`; кнопки 👍/👎 в `ask.js`),
`entities`(+`<id>`), `action-items`(+`<id>` PATCH), `stats`. Залежить: enrichment, retrieval, rag, embeddings.
`POST ask`/`ask/stream` приймають `channel` (`"ui"`, дефолт; MCP `ask_archive` шле `"mcp"`) —
кожне успішне питання обох каналів логується в `ask_log` (`rag._log_ask`).
`GET search`/`POST ask`/`POST ask/stream` усі приймають `explain` (query `?explain=1` або JSON
`{"explain": true}`, парситься `_parse_explain()`) — прокидається в `retrieval.search`/`rag.answer_question[_stream]`,
`why` виживає в `sources` кожного результату (T2, S2).
**`directions-projects-tags` (4.6.0, 22-23.09.2026, ADR-023):** дерево напрямок→проєкт (`parent_id`,
migration v45) + теги — `app/services/categories.py` (`memory/map/services.md`). `GET
/api/memory/categories` = `categories.list_tree` (кожен вузол несе `path` «Напрямок → Проєкт»).
`POST/PATCH/DELETE .../categories[/<id>][/merge]` — 409 `duplicate` (дубль серед братів,
`conflict:{id,path}`), 409 `has_projects` (видалення/злиття напрямку з проєктами), `bad_name` на
`→` у назві; `PATCH` з новим `parent_id` (reparent) ставить усе піддерево в re-embed, як і `name`.
`GET search`/`POST ask[/stream]`/`entities`/`action-items` приймають `category_id` (розкриває
піддерево), `tags` (через кому, OR; з `category_id` — AND), `since`/`until` (`YYYY-MM-DD`, невалідна
→ 400 українською з `categories.parse_date`). `POST ask[/stream]` `project=` — резолвиться через
`categories.resolve_node` ДО виклику `rag`: шлях або єдиний збіг голої назви → скоуп, кілька збігів
→ 409 `{"error":"ambiguous","candidates":[шляхи]}`, rag не викликається.
Теги: `GET/POST /api/memory/tags`, `PATCH/DELETE .../tags/<id>`, `POST .../tags/merge`, `PUT
/api/memory/<id>/tags` (`{"add":[names],"remove":[names]}`), `POST .../tags/suggestions`
(`{"accept":[names],"dismiss":[names]}` — лише зі `suggested_tags_json`, інакше 400). Зміна
`name` тега/категорії або `parent_id` → `reembed:{mode,count,stale}` у відповіді
(`categories.schedule_records_reembed`, `memory/map/services.md`).
**Gotchas:** entity `min_meetings=2` (шумофільтр) якщо нема `q`; category-назва унікальна casefold
СЕРЕД БРАТІВ (v47, не по всій таблиці — застаріла з 4.6.0); action-items period по meeting_date.
Фільтр `owner` — ТОЧНИЙ збіг (значення приходить із чипа фасета) по канонічному імені АБО по аліасах графа; сире
`owner_name` звʼязаної задачі свідомо не фільтрує, інакше список стає довшим за число на чипі. Ключ аліаса рахує
`enrichment._normalize`, а не SQL `LOWER()` — той не згортає кирилицю. У MCP (`commitments.list_commitments`) правило
інше й навмисно ширше: підрядок по канонічному, сирому та аліасах — там вільний текст, а не чипи.

## recording.py (~960) — `/api/recording/*`, `/api/recordings/*`
Живий запис mic/loopback + finalize + recovery, до `RECORDING_MAX_ACTIVE` паралельних сесій (4.7.0, ADR-026).
`GET /devices` (5s cache), `POST /start` (mic/system device + `system_source {mode,apps}` + language +
copilot), `<sid>/{pause,resume,stop,save,discard,state,live-transcript,stream(SSE)}`,
`POST <sid>/system-source {mode, apps?, reason?: user|auto_restore}` (retarget; 409 `NO_SYSTEM_STREAM`,
400 `RETARGET_FAILED`/`INVALID_REASON`), `GET /apps` (звучні застосунки, ніколи не 500 → `available:false`),
`/recordings/{active,recovered}`. `GET /recordings/active` → `{active, count, sessions:[{session_id, state,
name, auto_name, elapsed_seconds, streams, system_source, copilot_active}], session_id, state}` —
`session_id`/`state` лише коли сесія рівно одна.
**Gotchas:** понад ліміт → 409 `SESSION_LIMIT` (`active`, `limit`); невалідний spec → 400
`UNSUPPORTED_SOURCE_SPEC` до старту; копілот зайнятий іншим записом → старт 200 з
`copilot.error_code='COPILOT_BUSY'`; finalize async (може >5хв); SSE level 10Hz; save deadline 5хв.
`POST <sid>/save` (`editable-title-description`-02, 4.5.0) JSON `{"name": str, "description"?: str}`
— `description` нормалізується (`record_meta`) і йде в маніфест сесії → `register_recording()` →
`audio_downloads.description`.

## audio_library.py (~329) — `/api/audio/*`
Аудіотека (YouTube + записи). `POST /download`, `GET /downloads` (page/filter), `DELETE /downloads/<id>`,
`check-duplicate`, `open-explorer/<id>`, `play/<id>`. **Gotchas:** Phase 21 категорія через
`COALESCE(transcript.category_id, audio_downloads.category_id)`; source_type=youtube|recording|file.
`GET /downloads` (`directions-projects-tags`, 4.6.0) приймає `tags`/`since`/`until` поруч із
`category_id` (`app.services.categories`); кожен елемент несе теги останнього транскрипту.
`PATCH /api/audio/downloads/<int:id>` (`editable-title-description`-02/07, 4.5.0) JSON
`{"title"?: str, "description"?: str|null}` — жоден ключ → 400, порожній `title` → 400 (нема
фолбека, на відміну від `transcriptions.title`), нема запису → 404, інакше 200
`{"success": true, "item": {"id","title","description"}}`. `GET /downloads` (кожен елемент) віддає
`description` (рядок або `null`) поруч із `title` — звідти `audio.js` префілить модалку; без цього
ключа наступний PATCH тихо стирав би збережений опис (рада, раунд 1, ask 1).

## youtube.py (~131) — `/api/youtube/*`
`POST /info`, `POST /download` (фон, rate-limited), `GET /progress/<id>`. Залежить: youtube_pytubefix.

## documents.py (~456) — `/api/documents/*`
`POST /upload` (sync-parse), `<id>/reparse`, `import-folder` (фон+SSE). Дедуп по content_hash;
OCR опц.; опис таблиць Claude. Залежить: document_parser, text_polishing.
`POST /upload` (`editable-title-description`-02, 4.5.0) multipart приймає необовʼязкові `title`/
`description` (нормалізація `record_meta`, валідація ДО парсингу файлу).

## telegram.py (~760) — `/api/telegram/*`
`POST /ingest` (localhost+token), `GET /status`, `/dialogs`, `/coverage`, `/chats`, `POST /chats`,
`/backfill`, `POST /repair`, `POST /edited`, `POST /deleted`.
`GET /chats` (`directions-projects-tags`, 4.6.0) — фільтр `category_id`; кожен рядок несе
`tags:[{id,name,color}]`. `PUT /api/telegram/chats/<int(signed=True):chat_id>/tags`
`{"tags":[names]}` — повна заміна набору чату (`categories.apply_chat_tags`), ретроактивно на всі
`source='chat'` повідомлення цього чату; відвʼязування знімає лише `source='chat'`.
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

## research.py — `/api/research/*`
Дослідження бренду (Phase 18), логіка в `app/services/research.py` (`memory/map/services.md`).
`GET /preview` (0 токенів), `POST /originals` (markdown фрагментів), `POST /summary` (SSE,
map-reduce Haiku). `directions-projects-tags` (4.6.0, C4): усі три приймають `category_id`/`tags`/
`since`/`until` через `_parse_axes()` → `app.services.categories.parse_filter/parse_tags/parse_date`.

## copilot.py (~300) — `/api/copilot/*`
`POST /start`, `GET /sessions`, `<id>/{timeline,export,state}`, `POST <id>/{reingest,settings,action,stop}`,
`GET /by-transcription/<tid>`, `/availability`, `POST /live-ask`. **503 якщо COPILOT_ENABLED=False.** action:
dismiss|pin|thumbs_up/down|escalate. Settings мінять режим/важливість на льоту.
`POST /move {from_session_id, to_session_id, settings?}` (4.7.0) — завершує копілот на A, стартує на B;
не атомарно: провал старту → 200 `COPILOT_MOVE_FAILED` з `ended_from:true`. `CopilotBusyError` → 409
`COPILOT_BUSY` (`owner_session_id`); кілька живих записів без `session_id` → `AMBIGUOUS_SESSION`.

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
