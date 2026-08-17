# services — `app/services/*` (core)

Бізнес-логіка поза HTTP. Copilot та recording — окремі файли карти
([copilot.md](copilot.md), [recording.md](recording.md)).

## Транскрипція / медіа
- **transcription_service.py** — `ModernTranscriptionService`: `transcribe_file()`,
  `get_available_models()`, `get_system_stats()`. Обгортка над whisper_manager. *GPU-лок під час транскрипції.*
- **youtube_pytubefix.py** — `download_youtube_audio()` (DI: progress/log/db для тестабельності),
  quality→bitrate, класифікатор помилок, ffmpeg-post.
- **diarization_service.py** — pyannote: `diarize_audio()`, `diarize_recording()` (mic→"self",
  system→повна), `assign_speakers_to_whisper_segments()`. *Модель ~1.5GB lazy; HF_TOKEN потрібен;
  авто-конверт AAC/M4A/MP4→WAV. Лейбли: SELF_LABEL='self', UNKNOWN='SPEAKER_UNKNOWN'.*
- **live_transcribe.py** — `LiveTranscribeWorker`: читає PCM-хвіст активної сесії, транскрибує
  small-моделлю, шле сегменти по SSE. *interval ~8s; mic-only MVP; у памʼяті, не БД; фінал усе одно потрібен.*

## RAG (пошук + чат)
- **embeddings.py** — локальні e5-large (1024-dim): `embed_text()` (query:/passage: префікси),
  три чанкери: `_chunk_from_segments()` (аудіо), `_chunk_from_blocks()` (документи, page/section),
  `_chunk_from_text()` (плоский текст). *T6.5: межа аудіо-чанку — зміна спікера або пауза ≥1.5s
  при наборі ≥350 СВОЇХ символів, `_MAX_CHARS`=1000 лише страховка, перекриття ≤150 симв — тільки
  при обриві за лімітом і тільки тим, що влазить у бюджет. Зміна нарізки = бамп `EMBED_VERSION`
  (зараз 2) → re-embed через індексер; після масового проходу `enrichment.optimize_chunk_index()`
  зливає сегменти `chunks_fts` (без нього пошук деградує 9с → 64с).
  Замір: чанки p50≈304 токени, ліміт 512 не тисне. Lazy singleton + lock;
  кеш ~/.cache/huggingface ~2.2GB; mute-mode якщо нема torch.*
- **retrieval.py** — `search()`: гібрид vector(брут-форс numpy cosine, усе в RAM) + FTS5 BM25 через RRF,
  `scope_tids=` — звуження до списку записів (Трек 2; сам список рахує `scope.scope_filter_ids`,
  retrieval навмисно не знає про граф сутностей),
  recency-boost (half-life ~180д), per-meeting cap (≤3 чанки/зустріч), опційний `rerank=` (T6.4, OFF за
  замовчуванням) — cross-encoder переранжовує топ-пул (~24) ПЕРЕД diversity cap. *>100k чанків → треба sqlite-vec.*
- **reranker.py** (T6.4) — `rerank()`: локальний cross-encoder `BAAI/bge-reranker-v2-m3` (той самий
  sentence-transformers стек, що e5). *Lazy singleton як embeddings.py; OFF за замовчуванням
  (`RECALL_RERANK_ENABLED`), вмикається ЛИШЕ для RAG-чату з `rag.py`; graceful degradation → вихідний
  порядок без rerank.*
- **rag.py** — «Ask Archive»: `query_stream()`/`query()`. Контекст із чанків, відповідь ТІЛЬКИ з них + цитати [n].
  *Дефолт k — `_DEFAULT_TOP_K`=12 (env `RAG_TOP_K`), єдина точка правди для `/api/memory/ask*` і MCP
  `ask_archive`: на k=8 правильне джерело часто стоїть одразу за зрізом (recall@8 67.9% vs recall@12 82.1%).*
  `project=` (Трек 2) звужує до проєкту/людини; невідома назва НЕ обнуляє пошук — краще ширша
  відповідь, ніж мовчання через друкарську помилку.
  *Prompt caching; модель з CLAUDE_MODEL; T6.4 rerank точково через `retrieval.search(rerank=_RERANK_ENABLED)`.*
- **categorize.py** — `suggest_category()`: k-NN голосування по ембеддингах сусідів. *Мін 2 розмічені
  категорії (cold-start), dim має збігатись (1024).*

## Збагачення / документи
- **enrichment.py** — «Meeting Memory»: `enrich_transcription()` — 1 виклик Claude → summary/key_points/
  action_items/entities → upsert графа (people/projects/orgs + aliases + mention-salience). *Ідемпотентно по
  `enriched_at`; ENRICHMENT_VERSION форсить ре-ран.*
- **document_parser.py** — `parse_document()` → (text, blocks). Парсери по розширенню (lazy-import):
  md/txt/docx/pdf/pptx/xlsx/csv/зображення. OCR Tesseract (≤50 стор.), таблиці→markdown(+NL-опис Claude).
  *PyMuPDF/python-pptx/openpyxl опц. (ParserUnavailable); PARSER_VERSION → ре-парс.*
- **text_polishing.py** — Claude пост-процесор: `polish_text()`, `polish_diarized_text()` (стрім),
  `extract_meeting_card()`, `describe_sheets()`. *Lazy Anthropic-клієнт; prompt caching ~10% input;
  модель — `models.get_default_model()` (дефолт `claude-opus-5` з 28.07.2026, override env `CLAUDE_MODEL`; T6.2).*
- **archive_import.py** — bulk-імпорт `meeting_archive/MM_YYYY/*.md` у transcriptions. *Ідемпотентно по source_url=abs-path.*
- **research.py** — `find_mentions()`, `export_originals()` (0 токенів), `export_summary()` (map-reduce Haiku
  по фрагментах). *FTS5+substring; ±2 сегменти / ±400 символів; ≤12 фрагментів/запис; батч 40k символів.*

## Зобовʼязання і скоуп (Треки 1-2, 24-25.07.2026)
- **commitments.py** — нормалізація дедлайнів і зводи. `parse_due(raw, anchor)` розгортає сиру
  фразу («завтра», «до кінця тижня», «Q3 2026») в ISO-дату відносно **дати зустрічі** + клас
  точності (`day|week|month|quarter|soon|event|next_meeting|recurring`). Offline-проходи
  (`backfill_due`, `link_owners`, `dedup`, `mark_stale`) — ідемпотентні, кожен з `--dry-run`.
  Read-функції (`list_commitments`, `dropped_commitments`, `stale_topics`, `weekly_digest`) —
  спільне ядро для MCP і UI. *Лише stdlib + app.db.connection: імпортується зі stdio-MCP,
  тож без embeddings/torch. Сира фраза в `due` НЕ переписується — у видачі обидві.*
  *Волна 5.2: задача з переписки несе адресу — `chat`/`said_by` + пара `chat_id`/`msg_id`.
  Пара, а не лише `link`: у legacy-групах (chat_id без `-100`) посилання на повідомлення
  фізично не існує — 1453 записи з 3959, тож на самому `link` третина архіву лишилась би без
  відповіді «куди написати». Тай-брейк сортування — `meeting_date DESC`, і лише потім `id`:
  після 5.1 id більше не збігається з хронологією, і задачі з переписки (записані останніми,
  але про травень) витісняли дзвінки з ведра «без дати» — 15 із 15.*
  *Обкатка зводу на живому архіві (08.08.2026) — чотири правки правдивості:
  (1) `stale_topics` підтверджує мовчання ТЕКСТОМ (FTS), бо граф покриває 26% Telegram і
  брехав про частина тем(«Робота мовчить 36 днів» при 80 згадках, остання за 4 дні до
  зводу); поле `last_seen_source` = `graph|text`, форма назви точна — відмінки не ловляться,
  тож помилка йде в бік «мовчить». (2) `this_week` рахується від СЬОГОДНІ, а не від
  понеділка: вікна перетинались і 6 рядків із 15 читались двічі. (3) `dropped_commitments`
  бере лише розмовні джерела (`_OWN_SOURCES`) — 7 із 10 «загублених» приїхали з чужого
  інвесторського PDF; список НЕ детермінований на живому архіві за побудовою. (4) у видачі
  поруч із канонічним `owner` їде `owner_said` (як назвали в задачі), а фолбек `link_owners`
  по імені бере лише однослівну сутність: аліас «Andrei» у сутності «Andrij Kovalenko»
  (ютуб-лекція) забирав 43 задачі власника архіву.*
  *Фільтр `owner` дивиться і на АЛІАСИ графа, не лише на канонічне та сире імʼя. Після
  злиття сутностей (`entity_dedup merge`) усі написання людини переїжджають в аліаси —
  без цієї гілки злиття робить гірше: «що на Мельнику» давало 0 при живих даних, тепер 204. Підрядок навмисне: «Андрій» ловить і тезок (294), звузити можна
  повним імʼям.*
- **scope.py** — двошаровий скоуп архіву. `apply_chat_categories()` розкладає Telegram-чати по
  напрямках за локальною картою (`data/chat_categories.local.json`, поза git);
  `label_uncategorized()` — автопозначення решти трьома сигналами (назва запису → k-NN з
  коригуванням приорів → Claude Haiku як арбітр); `scope_filter_ids()` — зріз за проєктом/людиною
  як **обʼєднання** графа `meeting_entities`, текстової згадки (FTS5+назва) і однойменного напрямку.
  *Обʼєднання, а не лише граф: meeting_entities є тільки у збагачених (~16%) — «чистий» графовий
  зріз мовчки ховав решту. Порівняння напрямків — за `name_norm`, бо SQLite LOWER() не згортає кирилицю.
  Волна 4.5: `_expand_to_threads()` розширює зріз до цілих ниток — назва проєкту в переписці звучить
  раз, а рішення по ньому в сусідніх репліках без назви.*

## Нитки переписки (Волна 4.5, 07.08.2026)
- **tg_threads.py** — одиниця сенсу в Telegram це **нитка всередині чату**, не чат і не окреме
  повідомлення. `segment_bursts()` ріже чат на сплески (пауза `TG_THREAD_BURST_GAP_MIN`, дефолт
  180 хв → 3951 повідомлення згортається у 569 сплесків); `split_batch()` віддає сплеск локальній
  моделі, яка розділяє його на теми і може продовжити вже відкриту нитку; `assign_incoming()` —
  попереднє віднесення на живому інжесті БЕЗ моделі (`tg_thread_src='pending'`);
  `resettle_pending()` перерозкладає сплеск моделлю, коли розмова стихне; `close_idle_threads()`,
  `stats()`. CLI: `backfill --dry-run`, `resettle`, `close-idle`, `stats`.
  *Косинус e5 тут НЕ вирішує, а лише ранжує кандидатів: заміряно на архіві — reply-пари дають
  0.828, випадкові пари того ж чату 0.827, розділення +0.02σ. Короткій репліці нема чого вкладати
  в тему. Абсолютного порога в коді немає навмисно. Рішення ухвалює локальна модель
  (`TG_THREAD_MODEL`, окремо від `LOCAL_LLM_MODEL` копілота), і не на кожне повідомлення, а на
  сплеск — 521 виклик замість 3900. Нема Ollama → сплеск лишається однією ниткою, і це ВИДНО в
  `tg_thread_src` (`burst`/`orphan` = деградація), а не ховається.
  Злипання ниток стримує ВІКНО кандидатів (`TG_THREAD_IDLE_DAYS`=3), а не судження моделі:
  на прогоні з вікном 21 максимальний розрив усередині нитки упирався рівно в 21 при медіані
  2.8 — 7B охоче обирає щось зі списку, тиску «нічого не підходить» у неї немає. Плюс мʼяка
  стеля обсягу (`TG_THREAD_MAX_MSGS`=30): забороняє приймати НОВУ розмову, але не рве поточну.*
- **tg_chat_context.py** (4.5.2) — живе досьє чату ПОВЕРХ ниток: `participants()` (тверді числа
  з SQL), `open_threads()` (поточні теми = відкриті нитки), `refresh_chat()` (модель дописує
  ролі/звід/відкриті питання/рішення), `needs_refresh()` (поріг `TG_CONTEXT_MIN_NEW`=30 нових
  повідомлень АБО `TG_CONTEXT_MAX_AGE_H`=24 год), `get_context()`. CLI: `refresh --dry-run|--force`,
  `show --chat`. MCP: `get_chat_context`.
  *Після v32 теми беруться з ниток, а не вгадуються з потоку — модель не вирішує те, що вже
  пораховано. Числа учасників модель НЕ переписує: вигадане число в профілі живої людини гірше
  за його відсутність. Посилання на нитки, яких не показували, відкидаються. Оновлення за
  порогом, НІКОЛИ не на кожне повідомлення. Нема Ollama → лишається тверда частина,
  `summary=None` + `model=None` це позначають. Модуль легкий (без torch) — імпортується зі
  stdio-MCP.*
- **tg_entities.py** (4.5.3) — Telegram у графі сутностей БЕЗ моделі: `load_names()` (канонічні
  назви + аліаси, типи person/project/org, від 4 символів), `find_mentions()`, `link_threads()`,
  `stats()`. CLI: `link --dry-run|--chat`, `stats`. Провенанс — `meeting_entities.source`
  ('thread_match'; NULL = успадковані звʼязки Claude, вони надійніші й не перезаписуються).
  *Граф уже знав сутностей із дзвінків — TG треба було не збагачувати заново (7B наплодила
  б дублів у графі, де дедуплікація невирішена), а звʼязати з відомим. Наївний збіг не годиться:
  збагачення записало в аліаси звичайні слова («документ» → проєкт, «Тому» → особа, 144 збіги на
  слові «тому»). Частота теж не рятує — серед частих і сміття («модель» 7.5%), і ключові люди
  («адам» 7.0%). Працює доказ ІЗ МІСЦЯ ВЖИВАННЯ: згадка зараховується, лише якщо написана з
  великої літери і не на початку речення. Звʼязок — на ПОВІДОМЛЕННЯ зі згадкою, не на нитку:
  розширення до нитки вже робить `scope._expand_to_threads`, а дубль у графі дав би 43 482 рядки
  замість 2 752 і зробив би `meeting_count` та `list_stale_topics` безглуздими. Результат:
  TG у графі 154 → записів, сутностей отримали правдиву дату останньої згадки.*
- **tg_tasks.py** (5.1) — зобовʼязання з переписки каскадом: `triage_candidates()`/`triage_thread()`/
  `triage_all()` (локальна 7B, $0: чи є в нитці домовленість), `extract_candidates()`/
  `extract_thread()`/`extract_all()` (Claude по відібраному → `action_items` з `source='tg_thread'`),
  `stats()`. CLI: `triage --dry-run|--chat|--force|--limit`, `extract …`, `stats`.
  *Гард Волни 0 (жодного Claude на `source_type='telegram'`) знімається ВИБІРКОВО. Регекс-фільтр
  із плану волни («маркери обіцянки + топ-8 відправників + довжина») заміряно і відкинуто:
  маркери спрацьовують на живих даних з 545 — це не фільтр; на пілоті з ниток регекс і 7B
  розійшлись у 13 випадках, і мав рацію переважно не регекс («треба» в «треба визнати» — не
  зобовʼязання, «Давай я зараз уточню» — зобовʼязання без маркера). Модель бачить ПРЕВʼЮ кожного
  повідомлення, а не суцільний текст: найбільша нитка — 337 тис. символів, і це документ,
  надісланий у чат. Задача чіпляється до КОНКРЕТНОГО повідомлення (модель повертає його номер) —
  звідси 5.2 «куди написати»: `chat`/`said_by`/`link` у `list_commitments`. `triage_msgs` —
  водяний знак: нитка, що підросла після вердикту, переоцінюється. Повторний прогін стирає лише
  рядки з `source='tg_thread'`, задачі з карток дзвінків не чіпає. Нема Ollama → `skipped`
  з причиною; збій Claude → нитка без `tasks_at` чекає наступного проходу.*

- **tg_questions.py** (5.3) — питання без відповіді: `resolve_self()` (хто власник архіву),
  `is_question()`, `addressed_to_me()`, `open_questions(days, window_h, chat_id)`. MCP —
  `list_open_questions`. CLI: `list`, `whoami [--refresh]`.
  *Ані Claude, ані Ollama — лише SQL, тому нічого не персиститься: список рахується на запит.
  Три опори плану волни знято заміром. `tg_reply_to` — частина записів (де є, віримо; будувати
  не можна). Локальна 7B адресата НЕ визначає: на 14 живих питаннях влучила 1 раз, назвала
  «@нік_власника, вдасться долучитись?» питанням не до власника і вигадувала адресатів —
  тому адресата доводить СТРУКТУРА (лічка або @згадка ніка), а групові питання без звернення
  (233 з 294) свідомо не показуються, їх кількість видно в `skipped_group`. «Немає моєї
  репліки в нитці» — хибний тест: з 10 явно адресованих питань 4 мали відповідь через
  0.0–0.4 год у СУСІДНІЙ нитці того ж чату (нитки розмічені моделлю), тож присутність
  рахується по ЧАТУ у вікні `window_h` (доба). Нік власника бере `/status` слухача
  (кеш `data/tg_self.local.json`, кеш без ніка вважається неповним і перепитується);
  env `TELEGRAM_SELF_NAME`/`TELEGRAM_SELF_USERNAME` перекриває. Здогадка «власник — у
  найбільшій кількості чатів» НЕ використовується: на живих даних вказує на двох інших людей.*

## Інфраструктура
- **job_queue.py** — `JobQueue.submit(kind,fn,meta)` поверх executor; `Job` (state-машина, `is_cancelled()`).
  *Cancel кооперативний; history capped.*
- **sse_broker.py** — in-memory pub/sub: `publish()`, `subscribe()`, `stream_events()`. *Queue.Full тихо
  дропає; keepalive 15s; single-process (multi → треба Redis).*
- **system_monitor.py** — демон-снапшот CPU/GPU/RAM кожні N сек (щоб не блокувати API на cpu_percent). *non-blocking read.*
- **file_cleanup.py** — демон чистки orphan upload/youtube temp (не чіпає БД-referenced). *read-only DB.*
- **metrics.py** — `MetricsRegistry`: `inc()`, `observe_duration()`, `render()` (Prometheus text).
- **file_manager.py** — `FileManager`: info/list/validate/checksum, `FileInfo` dataclass.
- **model_updates.py** — демон перевірки PyPI на нову faster-whisper (CHECK_INTERVAL_DAYS=14, fail-silent).
- **local_llm.py** — Ollama HTTP-клієнт (Qwen 14B): `is_available()`, `generate()`, `generate_json()`, `warmup()`.
  *$0; **єдиний `_GEN_LOCK` серіалізує всі локальні виклики** (GPU-контеншн); тільки urllib.*
- **pricing.py** — `MODEL_PRICES` + `estimate_cost()`. Єдине джерело цін (шерять research.py і copilot/escalate.py).
