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

## Дедуп аудіо/YouTube/записів (Хвиля A production-RAG, 16.09.2026)
- **dedup_audio.py** — `hash_for(source_type, text)` (sha256 нормалізованого тексту, лише
  `file|youtube|recording` — Telegram і document мають свій дедуп), `find_original(conn,
  content_hash_value, exclude_id=None)` (найстаріший живий НЕ-дубль з тим самим хешем),
  `mark_duplicates(db_path, dry_run=False)` (офлайн-прохід по вже накопиченому архіву,
  дефолт пише в БД — `--dry-run` вмикає сухий режим). CLI: `python -m app.services.dedup_audio
  mark --dry-run`. *Дубль НЕ видаляється: `transcriptions.duplicate_of` (migration v40) вказує
  на оригінал, чанки/ембеддинги для дубля не будуються, інжест (`transcription.py`) виставляє
  `duplicate_of` до INSERT і статус збагачення `skipped_duplicate`. `retrieval.search` (vector
  і FTS) фільтрує `duplicate_of IS NULL` поруч із `deleted_at IS NULL`. `enrichment.py`
  (`list_unenriched_ids`, `backfill --force`, `enrich_transcription`) пропускає
  `duplicate_of IS NOT NULL` записи раннім виходом.*
- **telegram_link_repair.py** (test-log-isolation-02) — `clear_bogus_private_links(db_path,
  dry_run=False)`: одноразовий ремонт `tg_link`, вигаданого старим `_chat_link` для особистих
  чатів (`tg_chat_id > 0`) як `t.me/<username>/<msg_id>` — такого посилання на конкретне
  повідомлення в особистому листуванні не існує. Чистить лише поле, рядки лишає. CLI:
  `python -m app.services.telegram_link_repair clear-bogus-links --dry-run`. `_chat_link` у
  `telegram_listener.py` більше не вигадує посилань для приватних чатів.

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
  *Хвиля A: обидві гілки (vector і FTS) фільтрують `duplicate_of IS NULL` поруч із
  `deleted_at IS NULL` — дубль лишається в Бібліотеці, але не в пошуку.*
  `attach_thread_context(db_path, chunks, max_msgs=6)` — стеля символів на TG-нитку в контексті
  (раніше без ліміту, до 353k вхідних токенів на одне питання): env `RAG_THREAD_MSG_CHARS`
  (дефолт 1500, сусіди) і `RAG_THREAD_CHARS` (дефолт 8000, уся підшита нитка), обидва в реєстрі
  `app/core/settings.py`. Бюджет хіта `hit_budget = min(thread_limit, max(msg_limit, thread_limit
  // 2))` — ніколи не менший за бюджет сусіда. Сусіди беруться в порядку близькості до хіта (не
  хронологічно), кожен ≤ `min(msg_limit, remaining)`; сусід, якому не лишилось місця навіть під
  суфікс обрізки, і всі дальші (у порядку близькості) відкидаються — рахунок у `thread["dropped"]`.
  Обрізаний текст несе `truncated: true` і суфікс `…[обрізано, ще {K} симв.]`. Підсумковий
  `messages` завжди хронологічний (порядок вікна), `thread` несе `chars_total`/`chars_kept`/`dropped`.
  *grep-explainability (S2): `explain: bool = False` — кожен результат несе `why`-словник
  (`WHY_REQUIRED_KEYS = src,rrf,rec,by,top`; `build_placeholder_why()` — те саме для коментарів/
  фолбеків); `explain=True` додає `why["stages"]` (позиція+сирий `sim`/`bm25` на кожній стадії),
  `weights`, `final_raw`, `search_capped`. Не впливає на ранжування чи порядок видачі.*
- **archive_grep.py** (grep-explainability, S2) — `grep(db_path, pattern, regex=, ignore_case=,
  context=, limit=, source_type=, transcription_id=, days=, max_scan=, deadline_seconds=)`:
  буквальний/regex пошук ТОЧНОГО РЯДКА по `chunks` з ±context сусідніми чанками — без ембеддингів
  і без ранжування, для ID/сум/@ніків, які FTS5-токенізація й reranker ховають за неточним збігом.
  Сортування видачі — `meeting_date DESC, chunk_index`, НЕ релевантність. *Казфолд ВИКЛЮЧНО в Python
  (`str.casefold`) — SQLite `lower()` згортає лише ASCII; без `ORDER BY` у SQL (інакше SQLite
  прогнав би весь джойн до LIMIT), `max_scan` рахує переглянуті рядки, дедлайн стінного часу
  перевіряється МІЖ рядками (захист stdio-MCP від катастрофічного бектрекінгу regex). Пошук по
  `chunks`, не по `transcriptions.transcript_text` — рядок, розрізаний швом чанкування, не
  знайдеться (`chunk_boundary_caveat`). Не імпортує torch/numpy/`retrieval` — вантажиться у
  stdio-MCP. MCP-тулза: `grep_archive`.*
- **reranker.py** (T6.4) — `rerank()`: локальний cross-encoder `BAAI/bge-reranker-v2-m3` (той самий
  sentence-transformers стек, що e5). *Lazy singleton як embeddings.py; OFF за замовчуванням
  (`RECALL_RERANK_ENABLED`), вмикається ЛИШЕ для RAG-чату з `rag.py`; graceful degradation → вихідний
  порядок без rerank.*
- **rag.py** — «Ask Archive»: `answer_question()`/`answer_question_stream()` (сигнатури — колишні
  `query()`/`query_stream()`). Контекст із чанків, відповідь ТІЛЬКИ з них + цитати [n].
  *Дефолт k — `_DEFAULT_TOP_K`=12 (env `RAG_TOP_K`), єдина точка правди для `/api/memory/ask*` і MCP
  `ask_archive`: на k=8 правильне джерело часто стоїть одразу за зрізом (recall@8 67.9% vs recall@12 82.1%).*
  `project=` (Трек 2) звужує до проєкту/людини; невідома назва НЕ обнуляє пошук — краще ширша
  відповідь, ніж мовчання через друкарську помилку.
  *Prompt caching; модель з CLAUDE_MODEL; T6.4 rerank точково через `retrieval.search(rerank=_RERANK_ENABLED)`.*
  *Хвиля A production-RAG (16.09.2026): `order_citables(chunks, attached_comments)` — єдина
  функція для нумерації [n] і для `sources`, коментарі власника першими, решта — хронологічно за
  `meeting_date` зростанням (system prompt: «пізніша версія веде», рання показується як «було»),
  тай-брейк при однаковій даті — `(ранг першої появи transcription_id у порядку ретривалу,
  chunk_index)`, тож чанки одного запису йдуть підряд. `answer_question[_stream](..., channel:
  str = "ui")` — обидва канали (UI і MCP) логуються в таблицю `ask_log` (migration v41) через
  `_log_ask()`; вартість — `_ask_cost()` поверх `app.services.pricing.estimate_cost` (єдине
  джерело тарифів, НЕ окрема таблиця в rag.py). Пишуться лише успішні відповіді — збій Claude
  не лишає рядка. `ASK_CHANNELS = ("ui", "mcp")`; невідомий канал falls back на `"ui"`.
  `rate_ask(db_path, ask_id, rating, note=None)` — оцінка власника (1/-1) + замітка, `False`
  якщо рядка нема. Відповідь і SSE-подія `done` несуть `ask_id`.*
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

## Граф сутностей — дедуп/ремонт (Волна 4, T6.7 + історії 01-04, 21.08.2026)
- **entity_dedup.py** — офлайн-інструменти для якості графа `entities`, CLI (`python -m
  app.services.entity_dedup <cmd>`), MCP свідомо не підключено (write-дії, memory:
  mcp-read-first-strategy). Read-only розвідка: `find_merge_candidates()` (embedding-
  схожість canonical-імен У МЕЖАХ типу), `find_cross_type_twins()`/CLI `twins` (точні
  тезки в РІЗНИХ типах — «Acmecorp» як project і org, 133 з 152 груп не бачить
  `find_merge_candidates`), `find_alias_cross_type_collisions()`/CLI `twins --with-aliases`
  (аліас однієї сутності = канонічна назва іншого типу; 3+ учасники → `ambiguous: true`,
  до злиття не пропонується), `find_junk_aliases`/CLI `junk-aliases` (написання, які корпус
  пише переважно з малої), `find_morph_blockers()`/CLI `blockers` (сутності, що зламають
  морфо-матчер `tg_entities` — `generic_canonical` коли проблемне написання це САМЕ
  canonical_name, не аліас; `case_form` коли однослівна назва — це відмінкова форма,
  зловлена буквально з TG-тексту, напр. project «Україні» #3785), `inspect_entity()`/CLI
  `inspect` (докази «хто саме» всередині одного рядка графа — задачі за `owner_name`,
  тексти за написанням). Write, усі підтверджувані (dry-run за замовчуванням, `--apply`/
  `--yes` виконує): `merge_entities(keep_id, merge_id, allow_cross_type=, allow_person=)`,
  `split_entity(source_id, move_aliases, target_id|new_name|drop, dry_run=)`,
  `add_aliases()`/CLI `alias --add`, `rename_entity()`/CLI `rename` (канонічну назву; стара
  лишається аліасом, слід у `metadata_json["renamed_from"]`; при колізії `UNIQUE(type,
  normalized_name)` — відмова з підказкою «тут потрібен merge»).
  *Написання рахується в Python (`_fold_name`), не SQLite LOWER (кирилицю не згортає).
  Одну згадку в тексті не можна зарахувати двом сутностям — при перетині виграє
  НАЙДОВШЕ написання (`_winning_occurrences`, спільна логіка `inspect`/`split`).
  Людина+не-людина в merge заборонено НАВІТЬ з `--allow-cross-type`, знімається лише
  окремим `--allow-person` після перевірки очима; людський рядок, що зникає, не сміє
  мати задач чи `speaker_id`.*
- **tg_entities.py** — TG-повідомлення → граф сутностей БЕЗ моделі (доказ із написання:
  велика літера, не на початку речення). `SOURCE = "thread_match"` (точний збіг
  канонічної назви/аліасу). Історія 03 додала морфо-гілку: `SOURCE_MORPH = "thread_morph"`
  — окреме значення провенансу, щоб не підмінювати `SOURCE`; вмикач `TG_ENTITIES_MORPH_ENABLED`
  (env, дефолт `"0"` — **вимкнено**, читається функцією `_morph_enabled()`, не константою
  модуля, щоб перемикався без рестарту); власний стемер `_stem()`/`_CASE_SUFFIXES` (грубе
  зрізання укр./рос. відмінкових суфіксів, без pymorphy/snowball/nltk — заборонено планом
  історії), `stems_for()`/`_build_stems()` (лише для ОДНОСЛІВНИХ назв — словосполуки
  морфологія не чіпає). `find_mentions()` — точний + (за прапорцем) морфо; нове
  `find_exact_mentions()` — НІКОЛИ не запускає морфо-гілку незалежно від прапорця,
  для споживачів, чий контракт бачить лише точний матчер (зараз — `comments.link_entities`).
  Кеш назв (`names_for()`) памʼятає СТАН прапорця, під яким пораховані основи — прапорець
  може перемкнутись між прогрівом і читанням кешу.
  *⚠️ Дорого коштувало: (1) морфо-гілка за замовчуванням ВИМКНЕНА і вмикається лише
  свідомо, після розбору `blockers` — мердж історії сам собою поведінки не міняє;
  (2) багатослівний точний збіг закриває для морфо-гілки УСІ свої слова-позиції
  (`exact_word_positions`), не лише перше — інакше слово всередині багатослівної назви
  лишається відкритим і морфо-гілка може віддати його ІНШІЙ сутності (одна згадка —
  двом власникам). Третій гард — та сама перевірка на ALL-CAPS шапку (`is_shouting`),
  що й для точних збігів.*
- **comments.py** — `link_entities` перейшов на `tg_entities.find_exact_mentions` (історія
  04): шар коментарів навмисно НЕ підхоплює морфо-гілку разом із прапорцем TG-інжесту —
  вмикання `TG_ENTITIES_MORPH_ENABLED` для повідомлень не повинно тихо змінювати те, що
  пише інший шар.
- **evals/graph_links.py** (нове, історія 04) — read-only замір впливу морфо-гілки на
  ЗНІМКУ БД (`file:...?mode=ro`; відмова відкривати бойову без `--yes-live`, працює
  вбудований гард `is_live_db()` проти `Config.DATABASE`). Два зрізи над тими самими TG-
  повідомленнями — exact-only (як живе зараз) і exact+morph (прапорець піднятий лише на
  час підрахунку, у БД нічого не пишеться) — і diff за сутністю; другий шар доказу —
  кількість РІЗНИХ повідомлень/чатів (памʼять `derived-claims-need-second-source`: сумарне
  число ховає приріст від одного чату). Перший замір на знімку: 2699 → 3016 звʼязків
  (+317). CLI: `python -m evals.graph_links --db <шлях> [--json-out] [--top-n]`.

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
