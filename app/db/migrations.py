"""SQLite міграції з версіонуванням схеми.

Поточні версії:
  v1 — Базові таблиці transcriptions + audio_downloads
  v2 — Індекси на created_at, source_type, language, youtube_id, title
  v3 — FTS5 virtual table + триггери для transcriptions
  v4 — polished_text/polished_at/polished_model колонки (Claude polish)
  v5 — source_type у audio_downloads + recording_* колонки (Phase 9.6)
  v6 — speakers + transcription_speaker_map (Phase 10 diarization)
  v7 — embedding BLOB у transcription_speaker_map (Phase 10.6 voice fingerprinting)
  v8 — summary_json + summary_at + summary_model (Phase 12.5 Claude summarize)
  v9 — translations_json (Phase 12.11 multi-lang Claude translation cache)
  v10 — segment_bookmarks + saved_searches (Phase 12.12)
  v11 — topics_json (Phase 12.19 Claude topic auto-tags)
  v12 — sentiment_json (Phase 12.25 per-speaker sentiment analysis)
  v13 — Meeting Memory: entities + entity_aliases + meeting_entities +
        action_items + enriched_* колонки (Phase 13 — наскрізний RAG-архів)
  v14 — chunks (+embedding BLOB) + chunks_fts + embedded_* колонки
        (Phase 13B — локальні embeddings на 3090 + гібридний пошук)
  v15 — categories + transcriptions.category_id (Phase 14 — напрямки/розділи
        для чистого RAG: фонд/особисте/AI/... фільтруються наскрізно)
  v16 — document_* колонки на transcriptions (Phase 16A — підвантаження
        документів PDF/DOCX/MD: source_type='document', оригінал у file_path)
  v17 — chunks.page/section + transcriptions.structure_json (Phase 16B —
        провенанс сторінка/слайд для RAG-цитат; PDF/PPTX блоки)
  v18 — tg_* колонки на transcriptions + tg_monitored_chats (Phase 17 —
        ingestion з реального Telegram-акаунта: source_type='telegram',
        провенанс чат/відправник/лінк, дедуп по (chat_id,message_id))
  v19 — copilot_sessions + copilot_topics + copilot_events (Phase 19 — живий
        ко-пілот дзвінка: налаштування сесії на старті (вектор/режим/важливість/
        бюджет/лише-локально), топік-память з центроїдами, історія інсайтів/
        тригерів для перегляду й експорту)
  v20 — categories.name_norm (casefold-нормалізований ключ унікальності): SQLite
        COLLATE NOCASE згортає лише ASCII, тож кирилиця («Фонд» vs «фонд») дублі
        проходили. name_norm = name.strip().casefold() з UNIQUE-індексом гарантує
        регістронезалежну унікальність для будь-якого алфавіту (Phase 14.1)
  v22 — recording_video_tracks (нова таблиця) + audio_downloads.has_video +
        audio_downloads.primary_video_path (Phase 22 — захоплення відео екрану:
        per-monitor треки, прив'язані до запису; ідемпотентний upsert)
  v23 — recording_video_tracks.region_x/y/w/h (Phase 22 region capture —
        монітор-локальні пікселі захопленого регіону; NULL для full-monitor)
  v24 — video_keyframes (нова таблиця: кадри сцени + OCR-текст) +
        transcriptions.video_analysis_at/video_keyframes_count (Phase 23B-A —
        video understanding: keyframe OCR → searchable RAG chunks, speaker='екран')
  v26 — jobs (нова таблиця, T2.1 — REMEDIATION_PLAN Волна 1): персистентність
        JobQueue. Раніше стан фонових задач (YouTube download, транскрипція,
        backfill enrichment, import-folder, recording finalize) жив лише в
        dict у пам'яті — рестарт/крах процесу губив прогрес мовчки. Таблиця —
        джерело істини на диску; на старті app.py queued/running job'и з
        попереднього запуску позначаються 'crashed' (аналог recording
        SessionStore.recover_orphaned).

  v27 — soft-delete + speaker un-merge snapshot (T4.6, REMEDIATION_PLAN
        Волна 2, Варіант A): transcriptions.deleted_at / audio_downloads.
        deleted_at (epoch-секунди, NULL = живий запис) — одиничний DELETE
        більше не стирає рядок одразу (і файл audio_downloads — теж НЕ
        одразу), а лише ховає його з усіх read-шляхів (список/пошук/RAG/
        лічильники/дашборд); POST .../restore повертає. Bulk-delete
        лишається фізичним (без undo) — свідоме звуження. Таблиця
        speaker_merges зберігає before-снапшот кожного merge_speakers
        (видалені speakers-рядки + попередній transcription_speaker_map
        мапінг + entities.speaker_id, які FK ON DELETE SET NULL обнулив) —
        POST /api/speakers/unmerge/<merge_id> відновлює. Обидва механізми
        мають grace-період (RECALL_SOFTDELETE_GRACE_DAYS, дефолт 7 днів) —
        app.services.retention.purge_soft_deleted() фізично прибирає
        прострочене (файл + рядок / снапшот) на старті app.py.

  v28 — transcriptions.embedding_version (T6.8): структурна idempotency
        re-embed при зміні логіки чанкінгу.

  v29 — action_items: due_date / due_precision / stale_at / dup_of (Трек 1;
        план і замір — у локальних docs/plans/, поза репозиторієм: містять
        конкретику особистого архіву). Дедлайни зберігались лише як сира
        фраза («завтра», «четвер», «Q3 2026» — 426 різних значень), тож
        питання «що треба зробити цього тижня» було непридатне до запиту.
        due_date — обчислена ISO-дата відносно дати зустрічі, due_precision —
        чесність замість фальшивої точності («якнайшвидше» не вдає дату).
        Сира фраза в due НЕ переписується: у видачі показуємо обидві, щоб
        помилка парсера була видима. stale_at/dup_of — для offline-проходів
        app.services.commitments (нічого не видаляють).

  v30 — TG: meeting_date = день повідомлення замість дати інжесту (Волна 0).
  v31 — TG: tg_reply_to / tg_sender_id / tg_grouped_id / tg_edit_date (Волна 4).

  v32 — tg_threads + transcriptions.tg_thread_id/tg_thread_src (Волна 4.5):
        одиниця сенсу в переписці — НИТКА всередині чату, а не чат і не окреме
        повідомлення. Чат — це не тема: у фонд-чатах одночасно йдуть кілька
        проєктів, тож категорія, успадкована від чату, приписує кожному
        повідомленню один напрямок незалежно від змісту. Нитка живе окремою
        таблицею, бо схема chunks забороняє чанк поверх кількох записів
        (transcription_id NOT NULL + UNIQUE(transcription_id, chunk_index)),
        а записи-повідомлення лишаються як були — дедуп, лінки і провенанс
        не ламаються.

Запуск: init_database(db_path) на старті app.
Idempotent — кожна міграція робить INSERT OR IGNORE у schema_versions.
"""
import logging
import sqlite3

logger = logging.getLogger(__name__)


def init_database(db_path: str):
    """Створює БД (якщо немає) і застосовує всі pending міграції."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # WAL — кращ. конкурентність
    c.execute('PRAGMA journal_mode=WAL')

    # Таблиця версій схеми
    c.execute('''CREATE TABLE IF NOT EXISTS schema_versions
                 (
                     version INTEGER PRIMARY KEY,
                     applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                     description TEXT
                 )''')

    c.execute('SELECT MAX(version) FROM schema_versions')
    row = c.fetchone()
    current_version = row[0] if row[0] is not None else 0

    # === v1: Базові таблиці ===
    if current_version < 1:
        c.execute('''CREATE TABLE IF NOT EXISTS transcriptions
                     (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         source_type TEXT NOT NULL,
                         source_name TEXT NOT NULL,
                         source_url TEXT,
                         youtube_id TEXT,
                         youtube_title TEXT,
                         youtube_author TEXT,
                         youtube_duration INTEGER,
                         youtube_thumbnail TEXT,
                         file_path TEXT,
                         transcript_text TEXT,
                         language TEXT,
                         model_used TEXT,
                         processing_time REAL,
                         segments TEXT
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS audio_downloads
                     (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         youtube_url TEXT NOT NULL,
                         youtube_id TEXT NOT NULL UNIQUE,
                         title TEXT NOT NULL,
                         author TEXT,
                         duration INTEGER,
                         thumbnail_url TEXT,
                         file_path TEXT NOT NULL,
                         file_size INTEGER,
                         audio_quality TEXT,
                         audio_format TEXT,
                         download_time REAL,
                         view_count INTEGER,
                         like_count INTEGER,
                         description TEXT,
                         upload_date TEXT,
                         tags TEXT
                     )''')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (1, 'Initial schema: transcriptions + audio_downloads')")
        logger.info("Міграція v1: Базові таблиці створено")

    # === v2: Індекси ===
    if current_version < 2:
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_youtube_id ON audio_downloads(youtube_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_created_at ON audio_downloads(created_at DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_title ON audio_downloads(title)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at ON transcriptions(created_at DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_source_type ON transcriptions(source_type)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_language ON transcriptions(language)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (2, 'Added indexes on transcriptions and audio_downloads')")
        logger.info("Міграція v2: Індекси створено")

    # === v3: FTS5 ===
    if current_version < 3:
        c.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS transcriptions_fts USING fts5(
                transcript_text, source_name, youtube_title,
                content='transcriptions',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 1'
            )
        ''')
        c.execute('''
            CREATE TRIGGER IF NOT EXISTS transcriptions_ai
            AFTER INSERT ON transcriptions BEGIN
                INSERT INTO transcriptions_fts(rowid, transcript_text, source_name, youtube_title)
                VALUES (new.id, new.transcript_text, new.source_name, new.youtube_title);
            END
        ''')
        c.execute('''
            CREATE TRIGGER IF NOT EXISTS transcriptions_ad
            AFTER DELETE ON transcriptions BEGIN
                INSERT INTO transcriptions_fts(transcriptions_fts, rowid, transcript_text, source_name, youtube_title)
                VALUES ('delete', old.id, old.transcript_text, old.source_name, old.youtube_title);
            END
        ''')
        c.execute('''
            CREATE TRIGGER IF NOT EXISTS transcriptions_au
            AFTER UPDATE ON transcriptions BEGIN
                INSERT INTO transcriptions_fts(transcriptions_fts, rowid, transcript_text, source_name, youtube_title)
                VALUES ('delete', old.id, old.transcript_text, old.source_name, old.youtube_title);
                INSERT INTO transcriptions_fts(rowid, transcript_text, source_name, youtube_title)
                VALUES (new.id, new.transcript_text, new.source_name, new.youtube_title);
            END
        ''')
        c.execute("INSERT INTO transcriptions_fts(transcriptions_fts) VALUES('rebuild')")
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (3, 'FTS5 full-text index for transcriptions')")
        logger.info("Міграція v3: FTS5 індекс створено")

    # === v4: Claude polish columns ===
    if current_version < 4:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if 'polished_text' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN polished_text TEXT')
        if 'polished_at' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN polished_at TIMESTAMP')
        if 'polished_model' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN polished_model TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (4, 'polished_text + polished_at + polished_model for Claude postprocessing')")
        logger.info("Міграція v4: колонки polished_* додані")

    # === v5: source_type + recording columns у audio_downloads (Phase 9.6) ===
    # Розширюємо існуючу таблицю audio_downloads замість створення нової
    # таблиці recordings — UI і запити вже працюють по audio_downloads,
    # дублювання логіки не потрібне. Записи (Phase 9) будуть мати
    # source_type='recording', YouTube-завантаження — 'youtube' (default).
    if current_version < 5:
        c.execute("PRAGMA table_info(audio_downloads)")
        cols = {row[1] for row in c.fetchall()}
        if 'source_type' not in cols:
            # DEFAULT 'youtube' автоматично присвоюється старим рядкам
            c.execute("ALTER TABLE audio_downloads ADD COLUMN source_type TEXT NOT NULL DEFAULT 'youtube'")
        if 'recording_session_id' not in cols:
            c.execute('ALTER TABLE audio_downloads ADD COLUMN recording_session_id TEXT')
        if 'recording_segments' not in cols:
            c.execute('ALTER TABLE audio_downloads ADD COLUMN recording_segments INTEGER DEFAULT 1')
        if 'recording_duration_sec' not in cols:
            c.execute('ALTER TABLE audio_downloads ADD COLUMN recording_duration_sec REAL')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_source_type ON audio_downloads(source_type)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_recording_session_id ON audio_downloads(recording_session_id)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (5, 'source_type + recording_* columns in audio_downloads')")
        logger.info("Міграція v5: source_type + recording_* колонки додані")

    # === v6: Speakers + transcription_speaker_map (Phase 10 diarization) ===
    # speakers — глобальна таблиця "знайомих" спікерів. При перейменуванні в одному
    # transcript-і ім'я підставляється в speakers (по UNIQUE name) і використовується
    # як autocomplete-підказка в наступних transcript-ах.
    #
    # transcription_speaker_map — per-transcript mapping raw_label ("SPEAKER_00",
    # "self", "SPEAKER_UNKNOWN") → speaker_id. NULL speaker_id = "ще не названо",
    # frontend показує "Спікер N" по числовій частині raw_label.
    #
    # Сід-рядок "Ви" (is_self=1) — для авто-маршрутизації mic-стріму у recording-ах.
    # FK enforcement не обов'язковий (інші таблиці теж без PRAGMA foreign_keys=ON);
    # cascade-delete виконується явно у app-коді при видаленні transcription/speaker.
    if current_version < 6:
        c.execute("PRAGMA table_info(speakers)")
        if not c.fetchall():
            c.execute('''CREATE TABLE speakers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                color TEXT,
                is_self INTEGER NOT NULL DEFAULT 0,
                usage_count INTEGER NOT NULL DEFAULT 0,
                embedding BLOB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        c.execute("PRAGMA table_info(transcription_speaker_map)")
        if not c.fetchall():
            c.execute('''CREATE TABLE transcription_speaker_map (
                transcription_id INTEGER NOT NULL,
                raw_label TEXT NOT NULL,
                speaker_id INTEGER,
                PRIMARY KEY (transcription_id, raw_label),
                FOREIGN KEY (transcription_id) REFERENCES transcriptions(id) ON DELETE CASCADE,
                FOREIGN KEY (speaker_id) REFERENCES speakers(id) ON DELETE SET NULL
            )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_speakers_name ON speakers(name COLLATE NOCASE)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_speakers_usage ON speakers(usage_count DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_tspeaker_map_speaker ON transcription_speaker_map(speaker_id)')
        # Seed "Ви" — для is_self=1 (mic-стрім recording-ів). Не оновлюємо якщо вже є.
        c.execute("INSERT OR IGNORE INTO speakers (name, is_self) VALUES ('Ви', 1)")
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (6, 'speakers + transcription_speaker_map for diarization')")
        logger.info("Міграція v6: speakers + transcription_speaker_map створено")

    # === v7: embedding у transcription_speaker_map (Phase 10.6) ===
    # Voice fingerprinting: при діарізації pyannote повертає embedding per
    # SPEAKER_NN cluster (192-256 floats залежно від моделі). Зберігаємо
    # raw bytes у map для подальшого:
    # 1. Auto-match: при наступному transcribe порівнюємо embeddings нових
    #    SPEAKER_NN з усіма speakers.embedding через cosine similarity.
    #    Match >0.85 → auto-link до існуючого speaker_id.
    # 2. PATCH speakers: при ручному іменуванні беремо embedding з map і
    #    усереднюємо у speakers.embedding (running average).
    #
    # speakers.embedding вже додано у v6.
    if current_version < 7:
        c.execute("PRAGMA table_info(transcription_speaker_map)")
        cols = {row[1] for row in c.fetchall()}
        if 'embedding' not in cols:
            c.execute('ALTER TABLE transcription_speaker_map ADD COLUMN embedding BLOB')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (7, 'embedding BLOB у transcription_speaker_map for voice fingerprinting')")
        logger.info("Міграція v7: embedding колонка додана у transcription_speaker_map")

    # === v8: Summary + key points + action items (Phase 12.5) ===
    if current_version < 8:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols:  # таблиця існує
            if 'summary_json' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN summary_json TEXT')
            if 'summary_at' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN summary_at TIMESTAMP')
            if 'summary_model' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN summary_model TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (8, 'summary_json + summary_at + summary_model for Claude summarize')")
        logger.info("Міграція v8: summary_* колонки додані")

    # === v9: Translations cache (Phase 12.11) ===
    if current_version < 9:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'translations_json' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN translations_json TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (9, 'translations_json for multi-lang Claude translation cache')")
        logger.info("Міграція v9: translations_json колонка додана")

    # === v10: Bookmarks + saved searches (Phase 12.12) ===
    if current_version < 10:
        c.execute("PRAGMA table_info(segment_bookmarks)")
        if not c.fetchall():
            c.execute('''CREATE TABLE segment_bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transcription_id INTEGER NOT NULL,
                segment_index INTEGER NOT NULL,
                segment_start REAL NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(transcription_id, segment_index),
                FOREIGN KEY (transcription_id) REFERENCES transcriptions(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_bookmarks_tx ON segment_bookmarks(transcription_id)')
        c.execute("PRAGMA table_info(saved_searches)")
        if not c.fetchall():
            c.execute('''CREATE TABLE saved_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                query_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP,
                use_count INTEGER NOT NULL DEFAULT 0
            )''')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (10, 'segment_bookmarks + saved_searches')")
        logger.info("Міграція v10: bookmarks + saved_searches створено")

    # === v11: Topic auto-tags (Phase 12.19) ===
    if current_version < 11:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'topics_json' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN topics_json TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (11, 'topics_json for Claude topic auto-tags')")
        logger.info("Міграція v11: topics_json колонка додана")

    # === v12: Sentiment analysis (Phase 12.25) ===
    if current_version < 12:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'sentiment_json' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN sentiment_json TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (12, 'sentiment_json per-speaker sentiment')")
        logger.info("Міграція v12: sentiment_json колонка додана")

    # === v13: Meeting Memory — наскрізний шар сутностей + action items (Phase 13) ===
    # Перетворює Whisper з "архіву окремих транскриптів" на RAG-систему пам'яті:
    # люди / проєкти / організації стають first-class сутностями, спільними для
    # ВСІХ мітингів (з аліасами для дедуплікації варіантів написання —
    # Acmecorp / Акме / Acmez → одна сутність). Дозволяє наскрізні запити:
    # "усі мітинги про проєкт X", "усі задачі на Андрія", "де згадували Y".
    #
    # Чому нормалізовані таблиці, а не JSON-колонки (як topics_json):
    # topics_json — вільний текст у межах ОДНОГО транскрипту, не злити між
    # мітингами. Для cross-meeting пошуку потрібен граф сутностей = JOIN-и.
    #
    # enriched_* на transcriptions — маркер ідемпотентності: backfill і авто-job
    # пропускають уже збагачені (enriched_at IS NOT NULL), а зміна
    # enrichment_version дозволяє форсувати re-run при оновленні промптів.
    if current_version < 13:
        # --- entities: канонічні люди/проєкти/організації/теми ---
        c.execute("PRAGMA table_info(entities)")
        if not c.fetchall():
            c.execute('''CREATE TABLE entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,                 -- 'person' | 'project' | 'org' | 'topic'
                canonical_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,      -- lowercase+trim для дедупу
                role TEXT,                          -- посада/роль (для person)
                description TEXT,
                speaker_id INTEGER,                 -- лінк на speakers (голосовий відбиток)
                mention_count INTEGER NOT NULL DEFAULT 0,
                meeting_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT,
                UNIQUE(type, normalized_name),
                FOREIGN KEY (speaker_id) REFERENCES speakers(id) ON DELETE SET NULL
            )''')
            c.execute('CREATE INDEX idx_entities_type ON entities(type)')
            c.execute('CREATE INDEX idx_entities_norm ON entities(normalized_name)')
            c.execute('CREATE INDEX idx_entities_mentions ON entities(mention_count DESC)')

        # --- entity_aliases: варіанти написання → одна сутність ---
        c.execute("PRAGMA table_info(entity_aliases)")
        if not c.fetchall():
            c.execute('''CREATE TABLE entity_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                normalized_alias TEXT NOT NULL UNIQUE,
                FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_aliases_entity ON entity_aliases(entity_id)')

        # --- meeting_entities: яка сутність у якому мітингу + вага ---
        c.execute("PRAGMA table_info(meeting_entities)")
        if not c.fetchall():
            c.execute('''CREATE TABLE meeting_entities (
                transcription_id INTEGER NOT NULL,
                entity_id INTEGER NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 1,
                salience REAL,                      -- 0..1 важливість у цьому мітингу
                role_in_meeting TEXT,               -- контекстна роль/нотатка
                PRIMARY KEY (transcription_id, entity_id),
                FOREIGN KEY (transcription_id) REFERENCES transcriptions(id) ON DELETE CASCADE,
                FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_me_entity ON meeting_entities(entity_id)')

        # --- action_items: задачі/домовленості, лінкуються на власника-сутність ---
        c.execute("PRAGMA table_info(action_items)")
        if not c.fetchall():
            c.execute('''CREATE TABLE action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transcription_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                owner_name TEXT,                    -- ім'я з транскрипту (як є)
                owner_entity_id INTEGER,            -- resolved до person-сутності
                due TEXT,                           -- термін (сирий рядок)
                status TEXT NOT NULL DEFAULT 'open',-- open | done | cancelled
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (transcription_id) REFERENCES transcriptions(id) ON DELETE CASCADE,
                FOREIGN KEY (owner_entity_id) REFERENCES entities(id) ON DELETE SET NULL
            )''')
            c.execute('CREATE INDEX idx_action_tx ON action_items(transcription_id)')
            c.execute('CREATE INDEX idx_action_owner ON action_items(owner_entity_id)')
            c.execute('CREATE INDEX idx_action_status ON action_items(status)')

        # --- enriched_* маркери на transcriptions (ідемпотентність) ---
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols:
            if 'enriched_at' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN enriched_at TIMESTAMP')
            if 'enriched_model' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN enriched_model TEXT')
            if 'enrichment_version' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN enrichment_version INTEGER')
            if 'meeting_date' not in cols:
                # дата власне мітингу (для імпорту meeting_archive — з імені файлу/контенту);
                # для нативних транскрипцій = created_at, заповнюється при enrich.
                c.execute('ALTER TABLE transcriptions ADD COLUMN meeting_date TEXT')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (13, 'Meeting Memory: entities + aliases + meeting_entities + action_items + enriched_* markers')")
        logger.info("Міграція v13: Meeting Memory (entities/action_items/enriched_*) створено")

    # === v14: Chunks + локальні embeddings + chunk-level FTS5 (Phase 13B) ===
    # Семантичний (векторний) RAG-шар. Транскрипт ріжеться на чанки (вікна по
    # ходах спікера з перекриттям), кожен чанк отримує локальний embedding
    # (multilingual-e5-large на RTX 3090, float32 BLOB). Пошук — гібрид:
    #   vector cosine (семантика) + chunks_fts BM25 (ключові слова) → RRF-злиття.
    # Чанк-рівень (а не транскрипт-рівень) потрібен для цитат: дата+спікер+таймкод.
    #
    # embedded_* на transcriptions — окремий від enriched_* маркер ідемпотентності:
    # embeddings рахуються ЛОКАЛЬНО (без Claude), тож можуть бути навіть без
    # ANTHROPIC_API_KEY; skip-логіка незалежна від card-enrichment.
    if current_version < 14:
        c.execute("PRAGMA table_info(chunks)")
        if not c.fetchall():
            c.execute('''CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transcription_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_time REAL,                    -- сек (NULL якщо без таймкодів)
                end_time REAL,
                speaker TEXT,                       -- домінантний спікер чанку
                text TEXT NOT NULL,
                embedding BLOB,                     -- float32 normalized vector (np.tobytes)
                token_estimate INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(transcription_id, chunk_index),
                FOREIGN KEY (transcription_id) REFERENCES transcriptions(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_chunks_tx ON chunks(transcription_id)')

        # chunk-level FTS5 (external content) + тригери авто-синку (як transcriptions_fts)
        c.execute("PRAGMA table_info(chunks_fts)")
        # FTS5 — virtual table; PRAGMA table_info повертає її колонки якщо існує
        has_chunks_fts = bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone())
        if not has_chunks_fts:
            c.execute('''
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                    text, content='chunks', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 1'
                )
            ''')
            c.execute('''
                CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END
            ''')
            c.execute('''
                CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
                END
            ''')
            c.execute('''
                CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END
            ''')

        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols:
            if 'embedded_at' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN embedded_at TIMESTAMP')
            if 'embedding_model' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN embedding_model TEXT')
            if 'chunk_count' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN chunk_count INTEGER')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (14, 'chunks + chunks_fts + embedded_* for local embeddings hybrid search')")
        logger.info("Міграція v14: chunks + chunks_fts + embedded_* створено")

    # === v15: Categories / напрямки (Phase 14) ===
    # Вимір "напрямок" на кожному транскрипті (Фонд / Особисте / AI / ...).
    # Мета: чистий RAG — пошук/відповіді/граф можна обмежити одним розділом,
    # щоб не змішувати робоче, особисте і скачані YouTube ("солянка").
    # Один напрямок на запис (category_id), керований список (таблиця categories).
    # FK не інлайнимо в ALTER (як решта схеми) — цілісність у app-коді: при
    # видаленні категорії спершу обнуляємо transcriptions.category_id.
    if current_version < 15:
        c.execute("PRAGMA table_info(categories)")
        if not c.fetchall():
            c.execute('''CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                slug TEXT UNIQUE,
                color TEXT,
                icon TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            seeds = [
                ('Робота', 'work', '#1c7cf2', 'fa-building-columns', 1),
                ('Особисте', 'personal', '#e91e63', 'fa-user', 2),
                ('AI / професія', 'ai', '#7c4dff', 'fa-robot', 3),
                ('Будівництво', 'construction', '#fb8c00', 'fa-helmet-safety', 4),
                ('Фріланс', 'freelance', '#00897b', 'fa-laptop-code', 5),
            ]
            c.executemany(
                "INSERT OR IGNORE INTO categories (name, slug, color, icon, sort_order) "
                "VALUES (?, ?, ?, ?, ?)", seeds)

        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'category_id' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN category_id INTEGER')
            c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_category ON transcriptions(category_id)')

        # bulk-assign: meeting_archive → Робота (рішення користувача).
        # Лише якщо таблиця transcriptions існує (cols непорожній) — інакше UPDATE
        # падає «no such table» на БД без transcriptions (як у решті блоку — if cols).
        if cols:
            fund = c.execute("SELECT id FROM categories WHERE slug = 'work'").fetchone()
            if fund:
                c.execute(
                    "UPDATE transcriptions SET category_id = ? "
                    "WHERE source_type = 'meeting_archive' AND category_id IS NULL",
                    (fund[0],))

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (15, 'categories + transcriptions.category_id (Phase 14 напрямки)')")
        logger.info("Міграція v15: categories + category_id створено")

    # === v16: Document ingestion (Phase 16A) ===
    # Документи (PDF / DOCX / Markdown / …) переіспользують ТУ САМУ таблицю
    # transcriptions з source_type='document'. Як тільки розібраний текст лежить
    # у transcript_text — увесь нижній стек (chunks + embeddings + chunks_fts +
    # enrichment + граф сутностей + напрямки + RAG-чат + Історія) працює без змін.
    # Тут лише додаткові метадані документа (тип, к-сть сторінок, хеш для дедупу).
    # file_path переіспользуємо під шлях до збереженого ОРИГІНАЛУ файлу.
    #
    # Чанк-провенанс (стор. N / лист) додамо у 16B — для plain-text чанкінгу 16A
    # (_chunk_from_text) додаткові колонки не потрібні.
    if current_version < 16:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols:
            if 'doc_type' not in cols:
                # 'pdf' | 'docx' | 'md' | 'txt' | ... (розширення-джерело)
                c.execute('ALTER TABLE transcriptions ADD COLUMN doc_type TEXT')
            if 'original_filename' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN original_filename TEXT')
            if 'page_count' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN page_count INTEGER')
            if 'byte_size' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN byte_size INTEGER')
            if 'content_hash' not in cols:
                # sha256 розібраного тексту — для дедупу повторних завантажень (16E)
                c.execute('ALTER TABLE transcriptions ADD COLUMN content_hash TEXT')
            if 'parsed_at' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN parsed_at TIMESTAMP')
            if 'parser_version' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN parser_version INTEGER')
            c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_content_hash ON transcriptions(content_hash)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (16, 'document_* columns on transcriptions (Phase 16A document ingestion)')")
        logger.info("Міграція v16: document_* колонки додані")

    # === v17: Провенанс документів — сторінка/слайд для цитат (Phase 16B) ===
    # PDF/PPTX розбираються на блоки (сторінка/слайд). Щоб RAG-цитати вказували
    # «стор. 3 / слайд 5», чанки отримують page+section. structure_json на
    # transcriptions персистить блоки {text,page,section} — щоб при re-embed
    # (зміна моделі/чанкінгу) провенанс відновлювався без повторного парсингу.
    #
    # Аудіо-транскрипти й текстові документи (md/txt/docx) структури не мають →
    # structure_json IS NULL → чанкер падає на стару поведінку (segments/text),
    # а chunks.page/section лишаються NULL. Жодної регресії для аудіо.
    if current_version < 17:
        c.execute("PRAGMA table_info(chunks)")
        chunk_cols = {row[1] for row in c.fetchall()}
        if chunk_cols:
            if 'page' not in chunk_cols:
                c.execute('ALTER TABLE chunks ADD COLUMN page INTEGER')
            if 'section' not in chunk_cols:
                c.execute('ALTER TABLE chunks ADD COLUMN section TEXT')
        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols and 'structure_json' not in tcols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN structure_json TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (17, 'chunks.page/section + transcriptions.structure_json (Phase 16B document provenance)')")
        logger.info("Міграція v17: provenance колонки (chunks.page/section + structure_json) додані")

    # === v18: Telegram ingestion (Phase 17) ===
    # Слухання реального TG-АКАУНТА (MTProto/Telethon). Кожне повідомлення
    # моніторених чатів стає записом у transcriptions з source_type='telegram':
    # текст — напряму, голос/аудіо/відео — через faster-whisper, фото — OCR,
    # документи — document_parser. Як тільки текст у transcript_text — весь
    # нижній стек (chunks+embeddings+enrichment+граф+напрямки+RAG) працює без змін.
    #
    # tg_* колонки — провенанс повідомлення (звідки/від кого/коли/лінк), щоб RAG
    # міг цитувати «чат X, від Y, 2026-05-29». Дедуп: UNIQUE(tg_chat_id,
    # tg_message_id) — захист від подвійного інжесту, коли real-time і backfill
    # перетинаються по одному повідомленню.
    #
    # tg_monitored_chats — які чати слухати (керується з UI галочками). enabled=0
    # → слухач ігнорує. category_id → у який напрямок Recall класти записи з цього
    # чату (фонд-чати → «Робота» автоматично). last_message_id — курсор
    # для backfill/resume (звідки догружати історію).
    if current_version < 18:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols:
            if 'tg_chat_id' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN tg_chat_id INTEGER')
            if 'tg_chat_title' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN tg_chat_title TEXT')
            if 'tg_sender' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN tg_sender TEXT')
            if 'tg_message_id' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN tg_message_id INTEGER')
            if 'tg_date' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN tg_date TEXT')
            if 'tg_link' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN tg_link TEXT')
            # Дедуп TG-повідомлень. Partial index — лише по TG-записах (де
            # tg_message_id NOT NULL), щоб NULL'и інших source_type не конфліктували.
            c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_tg_msg
                         ON transcriptions(tg_chat_id, tg_message_id)
                         WHERE tg_message_id IS NOT NULL''')

        c.execute("PRAGMA table_info(tg_monitored_chats)")
        if not c.fetchall():
            c.execute('''CREATE TABLE tg_monitored_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                chat_type TEXT,                     -- 'user' | 'group' | 'channel'
                enabled INTEGER NOT NULL DEFAULT 1,
                category_id INTEGER,                -- у який напрямок Recall класти
                last_message_id INTEGER,            -- курсор backfill/resume
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
            )''')
            c.execute('CREATE INDEX idx_tg_monitored_enabled ON tg_monitored_chats(enabled)')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (18, 'Telegram ingestion: tg_* provenance + tg_monitored_chats (Phase 17)')")
        logger.info("Міграція v18: Telegram (tg_* колонки + tg_monitored_chats) створено")

    # === v19: Co-pilot — живий ко-пілот дзвінка (Phase 19) ===
    # Каркас сесії живого ко-пілота. На старті запису оператор задає вектор
    # (category), режим (light/medium/hard), важливість (low/medium/high),
    # бюджет і тумблер «лише локально» → рядок у copilot_sessions з повним
    # знімком налаштувань (config_json — резолв матриці режим×важливість).
    #
    # copilot_topics — топік-память сесії (центроїд e5 + закешований RAG-контекст
    # теми), щоб повернення до старої теми перевантажувало старий контекст, а не
    # шукало заново (Крок 2-3).
    # copilot_events — історична доріжка всього, що робив ко-пілот (зміни тем,
    # retrieval, локальні/перевірені інсайти, ескалації, дії оператора, токени),
    # з прив'язкою до таймкоду запису (ts_offset_sec) — для перегляду й експорту
    # (Крок 7-8). На Кроці 1 таблиці лише створюються; наповнення — далі.
    if current_version < 19:
        c.execute("PRAGMA table_info(copilot_sessions)")
        if not c.fetchall():
            c.execute('''CREATE TABLE copilot_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_session_id TEXT,          -- rec_... (manifest sid); NULL дозволено
                transcription_id INTEGER,           -- лінк після finalize (NULL поки нема)
                category_id INTEGER,                -- вектор/напрямок дзвінка
                mode TEXT NOT NULL DEFAULT 'medium',        -- 'light' | 'medium' | 'hard'
                importance TEXT NOT NULL DEFAULT 'medium',  -- 'low' | 'medium' | 'high'
                api_enabled INTEGER NOT NULL DEFAULT 1,     -- 0 = лише локально (без Claude)
                budget_usd REAL,                    -- ліміт $ на сесію (NULL = дефолт матриці)
                status TEXT NOT NULL DEFAULT 'active',      -- 'active' | 'ended'
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                config_json TEXT,                   -- повний знімок резолвлених налаштувань
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cache_read INTEGER NOT NULL DEFAULT 0,
                cost_estimate REAL NOT NULL DEFAULT 0,
                model_local TEXT,
                model_api TEXT,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
            )''')
            c.execute('CREATE INDEX idx_copilot_sessions_rec ON copilot_sessions(recording_session_id)')
            c.execute('CREATE INDEX idx_copilot_sessions_tx ON copilot_sessions(transcription_id)')

        c.execute("PRAGMA table_info(copilot_topics)")
        if not c.fetchall():
            c.execute('''CREATE TABLE copilot_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                copilot_session_id INTEGER NOT NULL,
                topic_index INTEGER NOT NULL,
                label TEXT,
                centroid BLOB,                      -- float32 e5-вектор (np.tobytes)
                first_ts REAL,                      -- offset sec у записі
                last_ts REAL,
                retrieval_cache_json TEXT,          -- закешовані RAG-чанки теми
                summary TEXT,
                UNIQUE(copilot_session_id, topic_index),
                FOREIGN KEY (copilot_session_id) REFERENCES copilot_sessions(id) ON DELETE CASCADE
            )''')

        c.execute("PRAGMA table_info(copilot_events)")
        if not c.fetchall():
            c.execute('''CREATE TABLE copilot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                copilot_session_id INTEGER NOT NULL,
                ts_wall TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ts_offset_sec REAL,                 -- зсув у записі (синхрон з транскриптом)
                kind TEXT NOT NULL,                 -- topic_shift|topic_return|retrieval|
                                                    -- insight_local|insight_verified|escalation|
                                                    -- operator_action|usage|safety_sweep
                topic_id INTEGER,                   -- copilot_topics.id (NULL дозволено)
                source TEXT,                        -- 'local' | 'api'
                confidence REAL,
                payload_json TEXT,
                tokens_in INTEGER,
                tokens_out INTEGER,
                operator_action TEXT,               -- dismiss|pin|thumbs_up|thumbs_down|escalate
                FOREIGN KEY (copilot_session_id) REFERENCES copilot_sessions(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_copilot_events_session ON copilot_events(copilot_session_id)')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (19, 'Co-pilot: copilot_sessions + copilot_topics + copilot_events (Phase 19 живий ко-пілот)')")
        logger.info("Міграція v19: Co-pilot (copilot_sessions/topics/events) створено")

    # === v20: categories.name_norm — casefold-ключ унікальності (кирилиця теж) ===
    # SQLite COLLATE NOCASE згортає тільки ASCII a-z → «Фонд» і «фонд» проходили як
    # різні. name_norm = casefold(name) + UNIQUE-індекс закриває дірку наскрізь.
    if current_version < 20:
        c.execute("PRAGMA table_info(categories)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'name_norm' not in cols:
            c.execute('ALTER TABLE categories ADD COLUMN name_norm TEXT')
            # backfill з наявних назв
            for cid, name in c.execute("SELECT id, name FROM categories").fetchall():
                c.execute("UPDATE categories SET name_norm = ? WHERE id = ?",
                          ((name or '').strip().casefold(), cid))
            # UNIQUE-індекс. Якщо у користувача вже є casefold-дублі — не валимо
            # міграцію (індекс лишається звичайним; унікальність тримає app-код).
            try:
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name_norm "
                          "ON categories(name_norm)")
            except sqlite3.IntegrityError:
                logger.warning("Міграція v20: є casefold-дублі напрямків — "
                               "ставлю звичайний індекс, унікальність контролює app-код")
                c.execute("CREATE INDEX IF NOT EXISTS idx_categories_name_norm "
                          "ON categories(name_norm)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (20, 'categories.name_norm casefold-ключ унікальності (Phase 14.1 кирилиця-дублі)')")
        logger.info("Міграція v20: categories.name_norm створено")

    # === v21: власна category_id у audio_downloads (recording UX) ===
    # Раніше напрямок аудіо бібліотека брала ТІЛЬКИ з привʼязаного транскрипту
    # (JOIN transcriptions по file_path). Нетранскрибований запис → напрямок
    # None → не ловиться фільтром по напрямку, хоча юзер задав напрямок при
    # записі. Даємо recording-рядку власну category_id (з copilot/recording-
    # сесії), а бібліотека робить COALESCE(transcript-категорія, власна).
    if current_version < 21:
        c.execute("PRAGMA table_info(audio_downloads)")
        cols = {row[1] for row in c.fetchall()}
        if 'category_id' not in cols:
            c.execute('ALTER TABLE audio_downloads ADD COLUMN category_id INTEGER')
            c.execute('CREATE INDEX IF NOT EXISTS idx_audio_category_id '
                      'ON audio_downloads(category_id)')
            # Backfill існуючих записів: для source_type='recording' беремо
            # категорію з copilot_sessions по recording_session_id.
            try:
                c.execute('''
                    UPDATE audio_downloads
                       SET category_id = (
                           SELECT cs.category_id FROM copilot_sessions cs
                            WHERE cs.recording_session_id = audio_downloads.recording_session_id
                              AND cs.category_id IS NOT NULL
                            ORDER BY cs.id DESC LIMIT 1)
                     WHERE source_type = 'recording'
                       AND recording_session_id IS NOT NULL
                       AND category_id IS NULL
                ''')
            except sqlite3.OperationalError:
                # copilot_sessions може не існувати у дуже старій БД — не валимо
                pass
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (21, 'audio_downloads.category_id — власний напрямок запису (recording UX)')")
        logger.info("Міграція v21: audio_downloads.category_id створено + backfill")

    # === v22: Screen video capture (Phase 22) ===
    if current_version < 22:
        c.execute("PRAGMA table_info(recording_video_tracks)")
        if not c.fetchall():
            c.execute('''CREATE TABLE recording_video_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_session_id TEXT NOT NULL,
                audio_download_id INTEGER,
                track_id TEXT NOT NULL,
                monitor_index INTEGER,
                monitor_label TEXT,
                mode TEXT NOT NULL DEFAULT 'full',
                file_path TEXT,
                codec TEXT,
                fps INTEGER,
                start_offset_sec REAL,
                duration_sec REAL,
                status TEXT NOT NULL DEFAULT 'recording',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(recording_session_id, track_id),
                FOREIGN KEY (audio_download_id) REFERENCES audio_downloads(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_rvt_session ON recording_video_tracks(recording_session_id)')
            c.execute('CREATE INDEX idx_rvt_download ON recording_video_tracks(audio_download_id)')
        c.execute("PRAGMA table_info(audio_downloads)")
        cols = {row[1] for row in c.fetchall()}
        if 'has_video' not in cols:
            c.execute('ALTER TABLE audio_downloads ADD COLUMN has_video INTEGER NOT NULL DEFAULT 0')
        if 'primary_video_path' not in cols:
            c.execute('ALTER TABLE audio_downloads ADD COLUMN primary_video_path TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (22, 'recording_video_tracks + has_video/primary_video_path (Phase 22 screen video)')")
        logger.info("Міграція v22: recording_video_tracks + video колонки")

    # === v23: region columns on recording_video_tracks (Phase 22 region capture) ===
    if current_version < 23:
        c.execute("PRAGMA table_info(recording_video_tracks)")
        cols = {row[1] for row in c.fetchall()}
        for col in ('region_x', 'region_y', 'region_w', 'region_h'):
            if col not in cols:
                c.execute(f'ALTER TABLE recording_video_tracks ADD COLUMN {col} INTEGER')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (23, 'recording_video_tracks region_x/y/w/h (Phase 22 region capture)')")
        logger.info("Міграція v23: region columns на recording_video_tracks")

    # === v24: Video understanding — keyframe OCR → RAG chunks (Phase 23B-A) ===
    # video_keyframes зберігає кожен відібраний кадр (scene-detection або
    # рівномірний fallback): timestamp у відео, шлях до jpg, OCR-текст, score.
    # Чанки з text != '' вставляються в chunks із speaker='екран' і section у
    # форматі 'екран MM:SS' → гібридний (vector+FTS) пошук охоплює і екранний текст.
    # transcriptions.video_analysis_at / video_keyframes_count — маркер ідемпотентності
    # (аналог embedded_at / enriched_at для audio-шару).
    if current_version < 24:
        # --- video_keyframes ---
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_keyframes'")
        if not c.fetchone():
            c.execute('''CREATE TABLE video_keyframes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transcription_id INTEGER NOT NULL,
                ts_offset_sec REAL NOT NULL,
                image_path TEXT,
                ocr_text TEXT,
                scene_score REAL,
                source TEXT DEFAULT 'scene',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (transcription_id) REFERENCES transcriptions(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_vkf_transcription ON video_keyframes(transcription_id)')

        # --- video_analysis_at + video_keyframes_count on transcriptions ---
        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            if 'video_analysis_at' not in tcols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN video_analysis_at TIMESTAMP')
            if 'video_keyframes_count' not in tcols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN video_keyframes_count INTEGER DEFAULT 0')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (24, 'video_keyframes + transcriptions.video_analysis_at/video_keyframes_count (Phase 23B-A video OCR→RAG)')")
        logger.info("Міграція v24: video_keyframes + video_analysis_at/video_keyframes_count створено")

    # === v25: vision-опис кадрів (Phase 23B) ===
    # video_keyframes.vision_text — короткий опис «що показано на екрані» від
    # VL-моделі (локальна Qwen2.5-VL через Ollama, $0, або Claude vision).
    # Іде в RAG-чанк (speaker='екран') поряд з OCR → шукабельно навіть для кадрів
    # без тексту на екрані (фото/діаграми). vision_model/vision_at — провенанс.
    if current_version < 25:
        c.execute("PRAGMA table_info(video_keyframes)")
        vcols = {row[1] for row in c.fetchall()}
        if vcols:  # таблиця існує (створена у v24)
            if 'vision_text' not in vcols:
                c.execute('ALTER TABLE video_keyframes ADD COLUMN vision_text TEXT')
            if 'vision_model' not in vcols:
                c.execute('ALTER TABLE video_keyframes ADD COLUMN vision_model TEXT')
            if 'vision_at' not in vcols:
                c.execute('ALTER TABLE video_keyframes ADD COLUMN vision_at TIMESTAMP')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (25, 'video_keyframes vision_text/vision_model/vision_at (Phase 23B keyframe vision descriptions)')")
        logger.info("Міграція v25: vision колонки на video_keyframes")

    # === v26: jobs — персистентність JobQueue (T2.1, REMEDIATION_PLAN Волна 1) ===
    # JobQueue досі тримала стан лише в пам'яті (dict) — рестарт/крах процесу
    # губив прогрес довгих задач (YouTube download, транскрипція, backfill
    # enrichment, import-folder, recording finalize) мовчки, без жодного сліду
    # для користувача. Таблиця jobs — джерело істини на диску; dict у JobQueue
    # лишається "гарячим" кешем поверх неї (читання /api/jobs не ходить у
    # SQLite на кожен запит). На старті app.py всі рядки зі state IN
    # ('queued','running') позначаються 'crashed' (аналог recording
    # SessionStore.recover_orphaned) — job більше не зникає без пояснення.
    #
    # DDL + version-insert — в одній явній транзакції (BEGIN/COMMIT), щоб
    # краш процесу посеред міграції не лишав напів-стан (таблиця створена,
    # але версія в schema_versions не записана → повторний старт спробував
    # би створити її знову; CREATE TABLE IF NOT EXISTS і так ідемпотентний,
    # але атомарність тут — явна страховка, а не покладання лише на це).
    # Якщо conn вже в транзакції (типовий шлях — свіжа БД, де v1..v25 щойно
    # накопичили незакомічені INSERT'и) — не намагаємось відкрити вкладену
    # транзакцію, а просто беремо участь у вже відкритій (закомітиться разом
    # з рештою міграцій у фінальному conn.commit() нижче).
    if current_version < 26:
        began_here = not conn.in_transaction
        if began_here:
            c.execute('BEGIN IMMEDIATE')
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                error TEXT,
                meta_json TEXT,
                updated_at REAL NOT NULL
            )''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)')
            c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (26, 'jobs table for JobQueue persistence (T2.1 crash recovery)')")
            if began_here:
                conn.commit()
        except Exception:
            if began_here:
                conn.rollback()
            raise
        logger.info("Міграція v26: таблиця jobs створена (персистентність JobQueue)")

    # === v27: soft-delete + speaker un-merge snapshot (T4.6, Волна 2) ===
    # transcriptions.deleted_at / audio_downloads.deleted_at (epoch-секунди,
    # REAL; NULL = живий запис) — одиничний DELETE ставить позначку замість
    # фізичного видалення (аудіо-файл теж НЕ стирається одразу — головний
    # P0 з аудиту: часто єдина копія). Усі read-шляхи (список/пошук/FTS/
    # vector-retrieval/лічильники/RAG/дашборд) додатково фільтрують
    # `deleted_at IS NULL` в app-коді (див. transcription.py/audio_library.py/
    # retrieval.py/memory.py/...). Bulk-delete лишається фізичним — свідомий
    # вибір Варіанту A (undo лише для одиничних).
    #
    # speaker_merges — before-снапшот merge_speakers (Phase 12.8) для
    # unmerge: видалені speakers-рядки (embedding/usage_count/color),
    # попередній transcription_speaker_map мапінг (які raw_label вказували
    # на які speaker_id ДО релінку) і entities.speaker_id, що FK
    # ON DELETE SET NULL обнулив при видаленні merge_ids зі speakers.
    # merged_speaker_ids — JSON-список для швидкого фільтра/діагностики;
    # before_json — повний снапшот для відновлення (embedding у base64).
    # restored_at — NULL поки не відновлено; purge прибирає снапшот по
    # merged_at (незалежно від restored_at — можливість undo завжди має
    # межу в часі, сам merge при цьому не відкочується).
    if current_version < 27:
        began_here = not conn.in_transaction
        if began_here:
            c.execute('BEGIN IMMEDIATE')
        try:
            c.execute("PRAGMA table_info(transcriptions)")
            tcols = {row[1] for row in c.fetchall()}
            if tcols and 'deleted_at' not in tcols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN deleted_at REAL')
                c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_deleted_at '
                          'ON transcriptions(deleted_at)')

            c.execute("PRAGMA table_info(audio_downloads)")
            acols = {row[1] for row in c.fetchall()}
            if acols and 'deleted_at' not in acols:
                c.execute('ALTER TABLE audio_downloads ADD COLUMN deleted_at REAL')
                c.execute('CREATE INDEX IF NOT EXISTS idx_audio_deleted_at '
                          'ON audio_downloads(deleted_at)')

            c.execute('''CREATE TABLE IF NOT EXISTS speaker_merges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keep_id INTEGER NOT NULL,
                merged_speaker_ids TEXT NOT NULL,
                before_json TEXT NOT NULL,
                merged_at REAL NOT NULL,
                restored_at REAL
            )''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_speaker_merges_merged_at '
                      'ON speaker_merges(merged_at)')

            c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                      "(27, 'soft-delete (transcriptions/audio_downloads.deleted_at) + "
                      "speaker_merges snapshot for unmerge (T4.6 Волна 2)')")
            if began_here:
                conn.commit()
        except Exception:
            if began_here:
                conn.rollback()
            raise
        logger.info("Міграція v27: deleted_at (transcriptions/audio_downloads) + speaker_merges створено")

    # === v28: transcriptions.embedding_version — структурна idempotency (T6.8) ===
    # Дотепер chunk_and_embed_transcription звіряв ЛИШЕ embedding_model — зміна
    # ЛОГІКИ чанкінгу (без зміни назви моделі, напр. T6.5) не детектувалась як
    # "потрібен re-embed". Додаємо nullable-колонку поряд з embedding_model:
    # NULL (наявні записи до цієї міграції) трактувалось як "сумісно з поточною
    # EMBED_VERSION" — щоб міграція не форсила масовий re-embed заднім числом.
    # ⚠️ 14.08.2026 (T6.5, EMBED_VERSION=2): семантику NULL змінено на "стара
    # нарізка → потрібен re-embed" — саме тоді, коли нарізка справді змінилась.
    # Джерело істини — embeddings.chunk_and_embed_transcription і дзеркальна
    # умова в enrichment.list_unenriched_ids; тут лише історія колонки.
    if current_version < 28:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'embedding_version' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN embedding_version INTEGER')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(28, 'transcriptions.embedding_version — структурна idempotency для "
                  "re-embed при зміні логіки чанкінгу (T6.8)')")
        logger.info("Міграція v28: transcriptions.embedding_version створено")

    # === v29: action_items — нормалізований дедлайн + stale/dup (Трек 1) ======
    # Сира фраза лишається в due (не переписуємо!), поряд зʼявляються обчислені
    # поля. NULL due_date = «дата не виводиться» (напр. «до наступної зустрічі») —
    # це НЕ помилка, а окремий клас, видимий через due_precision.
    if current_version < 29:
        c.execute("PRAGMA table_info(action_items)")
        acols = {row[1] for row in c.fetchall()}
        if acols:
            for col, decl in (("due_date", "TEXT"), ("due_precision", "TEXT"),
                              ("stale_at", "TEXT"), ("dup_of", "INTEGER")):
                if col not in acols:
                    c.execute(f"ALTER TABLE action_items ADD COLUMN {col} {decl}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_action_items_status_due "
                      "ON action_items(status, due_date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_action_items_owner_status "
                      "ON action_items(owner_entity_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_action_items_dup_of "
                      "ON action_items(dup_of)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(29, 'action_items.due_date/due_precision/stale_at/dup_of — "
                  "нормалізовані дедлайни + offline stale/dedup (Трек 1)')")
        logger.info("Міграція v29: action_items due_date/due_precision/stale_at/dup_of створено")

    # === v30: TG — дата події з tg_date (Волна 0 роадмапу Telegram) ===========
    # _persist ніколи не писав meeting_date, а весь стек нижче датує запис через
    # COALESCE(meeting_date, substr(created_at,1,10)) — тобто МОМЕНТОМ ІНЖЕСТУ.
    # На живих даних: meeting_date порожній у 3142 з 3360 TG-записів, а всі 218
    # заповнених (їх ставила card-фаза з created_at) теж хибні — напр. tx=328
    # має 2026-05-29 при повідомленні від 2026-02-10. Ламало дві речі одразу:
    # recency-буст у retrieval.py (стара переписка виглядала свіжою) і якір
    # commitments.parse_due (сира фраза «до пʼятниці» розгорталась відносно дня
    # завантаження). Ремонт без втрат — tg_date заповнений у ВСІХ TG-записів.
    # 'localtime': tg_date у UTC, а потрібен день розмови (див. _meeting_date).
    # Йде ДО будь-якої догонки історії: інакше вона лише множила б розбіжність.
    if current_version < 30:
        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if {'tg_date', 'meeting_date', 'source_type'} <= tcols:
            c.execute(
                "UPDATE transcriptions SET meeting_date = date(tg_date, 'localtime') "
                "WHERE source_type = 'telegram' AND tg_date IS NOT NULL "
                "AND (meeting_date IS NULL OR meeting_date <> date(tg_date, 'localtime'))"
            )
            logger.info("Міграція v30: дату події виправлено у %d TG-записах", c.rowcount)

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(30, 'TG: meeting_date = день повідомлення (tg_date) замість дати "
                  "інжесту — recency-буст і якір parse_due (Волна 0)')")

    # === v31: TG — сигнали, які Telegram уже дає, а ми викидали (Волна 4) ======
    # tg_reply_to — ТОЧНА нитка розмови від самого Telegram. Без неї «на що це
    # відповідь» доводиться вгадувати: у чаті з паралельними темами склейка за
    # вікном часу зшиває незвʼязане, і саме тому пошук на «коли зустріч з
    # губернатором» віддавав 7 запитань без жодної відповіді — відповідь була
    # наступним повідомленням треда, спільних слів із запитом нема.
    # tg_sender_id — стабільний ключ автора (tg_sender це відображуване імʼя,
    # яке людина міняє). tg_grouped_id — альбом (кілька медіа = одне повідомлення
    # для людини). tg_edit_date — правку видно лише через неї.
    # Колонки forward-only: чим пізніше зʼявляться, тим менше повідомлень їх
    # отримає, тому міграція йде ДО решти Волни 4.
    if current_version < 31:
        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            for col, decl in (("tg_sender_id", "INTEGER"), ("tg_reply_to", "INTEGER"),
                              ("tg_grouped_id", "INTEGER"), ("tg_edit_date", "TEXT")):
                if col not in tcols:
                    c.execute(f"ALTER TABLE transcriptions ADD COLUMN {col} {decl}")
            # Пошук «покажи весь тред» іде від батька до дітей.
            c.execute("CREATE INDEX IF NOT EXISTS idx_tx_tg_reply "
                      "ON transcriptions(tg_chat_id, tg_reply_to) "
                      "WHERE tg_reply_to IS NOT NULL")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(31, 'TG: tg_reply_to/tg_sender_id/tg_grouped_id/tg_edit_date — "
                  "нитка розмови, стабільний автор, альбоми, правки (Волна 4)')")
        logger.info("Міграція v31: TG-сигнали (reply_to/sender_id/grouped_id/edit_date) створено")

    # === v32: нитки розмови всередині чату (Волна 4.5) ========================
    # Проблема, яку лікуємо: у чаті повідомлень — це 955 незвʼязаних записів,
    # і «Ок» фізично не несе контексту. Живий провал: на «коли зустріч з
    # губернатором» пошук віддав 7 слотів із 8, і всі сім — питання без жодної
    # відповіді (відповідь була наступним повідомленням треда, спільних слів із
    # запитом нема → не знаходить ні вектор, ні FTS).
    #
    # Чому нитка, а не чат: у «Main Team Chat» одночасно йдуть Acmecorp,
    # Nova Dance, юридичні питання й LP-апдейти, але категорію дає чат цілком
    # (_resolve_category читає tg_monitored_chats.category_id). Заміряно: у 4
    # фонд-чатах повідомлень, усі з одним напрямком, при тому що всередині
    # Ковальчука згадується 23 рази — і в жодному іншому чаті. Система знала ДЕ
    # сказано і не знала ПРО ЩО.
    #
    # centroid — центроїд e5-векторів повідомлень нитки. Тримаємо його НЕ як
    # вирішувач, а як ранжувальник кандидатів: замір на живому архіві показав,
    # що сирий косинус між повідомленнями одного чату не відрізняє продовження
    # теми від випадкової пари (reply-пари 0.828 проти випадкових 0.827,
    # розділення +0.02σ). Короткій репліці нема чого вкладати в тему — «ні»
    # не про предмет, а про регістр мовлення. Рішення ухвалює локальна LLM
    # (див. app/services/tg_threads.py), центроїд лише звужує їй вибір.
    #
    # tg_thread_src зберігає, ЧИМ ухвалено рішення — без цього неможливо ні
    # відрізнити точну нитку від здогадки, ні перерахувати лише здогадки при
    # зміні промпта. Значення навмисно НЕ зливаються в одне «авто»:
    #   reply  — з tg_reply_to, це знання самого Telegram (помилки бути не може);
    #   llm    — модель віднесла до теми з кількох повідомлень;
    #   single — повідомлення самотнє: або модель так вирішила, або воно було
    #            єдиним у сплеску (групувати не було з чим);
    #   pending— попереднє рішення на живому інжесті (моделі ще не питали);
    #            resettle_pending перерозкладе, коли сплеск договорить;
    #   burst  — ДЕГРАДАЦІЯ: моделі не було, пачка лишилась однією ниткою;
    #   orphan — ДЕГРАДАЦІЯ: модель промовчала про це повідомлення.
    # Останні два — саме те, що треба вміти знайти й перерахувати потім.
    if current_version < 32:
        c.execute("PRAGMA table_info(tg_threads)")
        if not c.fetchall():
            c.execute('''CREATE TABLE tg_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                label TEXT,
                status TEXT NOT NULL DEFAULT 'open',   -- open | closed
                centroid BLOB,                         -- float32 e5, середнє по нитці
                msg_count INTEGER NOT NULL DEFAULT 0,
                first_date TEXT,                       -- ISO tg_date першого повідомлення
                last_date TEXT,
                category_id INTEGER,                   -- 4.5.1a: мітка НА НИТЦІ
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP
            )''')
            # Пошук відкритих ниток чату при віднесенні нового сплеску.
            c.execute('CREATE INDEX idx_tg_threads_chat ON tg_threads(chat_id, status, last_date)')

        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            for col, decl in (("tg_thread_id", "INTEGER"), ("tg_thread_src", "TEXT")):
                if col not in tcols:
                    c.execute(f"ALTER TABLE transcriptions ADD COLUMN {col} {decl}")
            # Зшивання сусідів нитки при видачі йде від нитки до повідомлень.
            c.execute("CREATE INDEX IF NOT EXISTS idx_tx_tg_thread "
                      "ON transcriptions(tg_thread_id, tg_date) "
                      "WHERE tg_thread_id IS NOT NULL")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(32, 'tg_threads + transcriptions.tg_thread_id/tg_thread_src — "
                  "нитка розмови як одиниця сенсу в переписці (Волна 4.5)')")
        logger.info("Міграція v32: tg_threads + tg_thread_id створено")

    # === v33: живий профіль чату (Волна 4.5.2) ===============================
    # Формулювання власника: «кожне нове повідомлення не має бути унікальним —
    # воно має доповнювати контекст своєї групи, постійно розширювати його».
    #
    # ЩО ЗМІНИЛОСЬ ПІСЛЯ v32. У початковому задумі профіль мав вгадувати теми з
    # сирого потоку повідомлень. Тепер теми — це відкриті нитки, тобто вже
    # порахований факт, а не здогадка моделі. Тому в профілі лишається рівно
    # те, що НЕ виводиться з ниток і є властивістю саме чату: хто тут і чим
    # займається, що висить без відповіді, до чого дійшли.
    #
    # Тверді числа (скільки написав, коли зʼявився, коли писав востаннє)
    # рахує SQL і НЕ довіряє їх моделі: це факт, а вигадане число в профілі
    # людини гірше за його відсутність. Модель отримує їх готовими і пише лише
    # опис ролі й звід.
    #
    # messages_seen / last_message_date — водяний знак: профіль оновлюється за
    # порогом (N нових повідомлень або раз на добу), НІКОЛИ не на кожне
    # повідомлення. Інакше це рівно ті 3142 платні виклики, від яких Волна 0
    # поставила гард — тільки тепер локальні, але так само безглузді.
    if current_version < 33:
        c.execute("PRAGMA table_info(tg_chat_context)")
        if not c.fetchall():
            c.execute('''CREATE TABLE tg_chat_context (
                chat_id INTEGER PRIMARY KEY,
                summary TEXT,                  -- про що цей чат загалом
                participants_json TEXT,        -- [{name, messages, first_seen, last_seen, role}]
                topics_json TEXT,              -- відкриті нитки на момент оновлення
                open_questions_json TEXT,      -- питання, що висять без відповіді
                decisions_json TEXT,           -- до чого дійшли
                model TEXT,                    -- чим згенеровано (для перерахунку)
                messages_seen INTEGER,         -- водяний знак: скільки повідомлень враховано
                last_message_date TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(33, 'tg_chat_context — живий профіль чату поверх ниток: учасники, "
                  "відкриті питання, рішення; оновлення за порогом (Волна 4.5.2)')")
        logger.info("Міграція v33: tg_chat_context створено")

    # === v34: провенанс звʼязків графа (Волна 4.5.3) =========================
    # Telegram — переважна більшість архіву і при цьому майже відсутній у графі сутностей: 154
    # записи з 3951. Через це зріз по проєкту, list_stale_topics і понедільний
    # звід сліпі до основної маси архіву.
    #
    # Лікуємо БЕЗ моделі. Граф уже знає сутностей із дзвінків і документів,
    # тож TG треба не збагачувати заново (7B на живих даних наплодила б дублів
    # «Акме/Acmecorp/акмекорп» у графі, де дублі й так невирішена проблема), а
    # звʼязати з тим, що вже відоме.
    #
    # source розрізняє, ЧИМ поставлено звʼязок: NULL — успадковані звʼязки від
    # Claude-збагачення, 'thread_match' — знайдені звірянням тексту нитки з
    # графом. Без цього неможливо ні перерахувати лише автоматичні, ні
    # відрізнити витяг моделі від збігу рядків, а це різні за надійністю речі.
    if current_version < 34:
        c.execute("PRAGMA table_info(meeting_entities)")
        cols = {row[1] for row in c.fetchall()}
        if cols and "source" not in cols:
            c.execute("ALTER TABLE meeting_entities ADD COLUMN source TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_me_source ON meeting_entities(source)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(34, 'meeting_entities.source — провенанс звʼязку графа; TG "
                  "звʼязується з наявними сутностями по нитках (Волна 4.5.3)')")
        logger.info("Міграція v34: meeting_entities.source створено")

    # === v35: зобовʼязання з переписки (Волна 5.1) ===========================
    # Волна 0 заборонила Claude-картку на telegram: картка на однорядковику
    # «Ок» марна, а на живих даних ще й платна. Але задачі в переписці РЕАЛЬНО
    # роздаються — просто не в одному повідомленні, а в нитці. Тепер, коли
    # нитка є (v32), одиницею збагачення стає вона, і гард знімається не
    # цілком, а вибірково: спершу локальний триаж, потім Claude лише по
    # відібраному.
    #
    # ЧОМУ ТРИАЖ МОДЕЛЛЮ, А НЕ РЕГЕКСОМ. План волни говорив «маркери обіцянки +
    # топ-8 відправників + довжина». Замір по 545 нитках: список маркерів
    # («треба», «надішлю», «чекаю», …) спрацьовує на частина ниток — це не фільтр.
    # На пілоті з ниток регекс і 7B розійшлись у 13 випадках з 40, і в
    # переважній більшості мав рацію не регекс: «треба» в «треба визнати» —
    # не зобовʼязання, а «Давай я зараз уточню» — зобовʼязання без жодного
    # маркера. Триаж локальний ($0, ~4 с на нитку), тож ціна помилки — час.
    #
    # triage_msgs — водяний знак: нитка, яка виросла після вердикту, буде
    # переоцінена; нитка, що не змінилась, не витрачає ні модель, ні Claude.
    #
    # action_items.source розрізняє походження задачі: NULL — успадковані
    # (Claude-картка дзвінка/документа), 'tg_thread' — витягнуті з нитки
    # переписки. Без цього повторний прогін або стирав би чужі рядки, або
    # плодив дублі; крім того, у зводі видно, чи задача прозвучала голосом на
    # дзвінку, чи лишилась текстом у чаті.
    if current_version < 35:
        c.execute("PRAGMA table_info(tg_threads)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            for col, decl in (("triage", "TEXT"), ("triage_at", "TIMESTAMP"),
                              ("triage_model", "TEXT"), ("triage_msgs", "INTEGER"),
                              ("tasks_at", "TIMESTAMP"), ("tasks_model", "TEXT")):
                if col not in tcols:
                    c.execute(f"ALTER TABLE tg_threads ADD COLUMN {col} {decl}")
            # Вибірка «кого ще не триажили» і «кого триаж позначив» — обидві
            # часті (проходи йдуть пачками), обидві по цих двох колонках.
            c.execute("CREATE INDEX IF NOT EXISTS idx_tg_threads_triage "
                      "ON tg_threads(triage, tasks_at)")

        c.execute("PRAGMA table_info(action_items)")
        acols = {row[1] for row in c.fetchall()}
        if acols and "source" not in acols:
            c.execute("ALTER TABLE action_items ADD COLUMN source TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_action_items_source "
                      "ON action_items(source)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(35, 'tg_threads.triage/tasks_at + action_items.source — "
                  "зобовʼязання з переписки: локальний триаж нитки, Claude лише "
                  "по відібраному (Волна 5.1)')")
        logger.info("Міграція v35: триаж ниток + action_items.source створено")

    if current_version < 36:
        # Чому задача опинилась у stale. Досі причина була одна (вік), тож
        # колонки не було; тепер їх дві — вік і «тема замовкла», і без
        # провенансу масове зняття неможливо ні пояснити, ні відкотити
        # вибірково. Наявні рядки — усі від `mark_stale`, тобто 'age'.
        c.execute("PRAGMA table_info(action_items)")
        acols = {row[1] for row in c.fetchall()}
        if acols and "stale_reason" not in acols:
            c.execute("ALTER TABLE action_items ADD COLUMN stale_reason TEXT")
            c.execute("UPDATE action_items SET stale_reason = 'age' "
                      "WHERE status = 'stale' AND stale_reason IS NULL")
            c.execute("CREATE INDEX IF NOT EXISTS idx_action_items_stale_reason "
                      "ON action_items(stale_reason)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(36, 'action_items.stale_reason — чому задачу знято: age | no_trace')")
        logger.info("Міграція v36: action_items.stale_reason створено")

    # === v37: шар коментарів (Comments Layer) ================================
    # Коментар власника на будь-якій картці архіву: уточнення, виправлення,
    # акцент. Іде в той самий пошук, що й транскрипт, але з БІЛЬШОЮ вагою —
    # це не сира стенограма, а свідомо написане речення про те, що насправді
    # мається на увазі.
    #
    # ЧОМУ ОКРЕМИЙ ІНДЕКС, А НЕ РЯДКИ В `chunks`. Дві причини, і перша —
    # блокуюча: `chunks.transcription_id` NOT NULL з FK. Коментар на файлі
    # Медіатеки, який ще не транскрибовано, у `chunks` фізично не покласти, а
    # знімати NOT NULL — це перебудова гарячої таблиці на 100k+ рядків, під
    # якою висить FTS5 external-content з content_rowid='id' і три тригери.
    # Друга причина лишалась би чинною, навіть якби перша зникла: окремий
    # індекс робить пріоритет коментарів ОДНІЄЮ явною ручкою замість
    # коефіцієнта, розмазаного по спільному пайплайну, і дозволяє звузити
    # запит до коментарів (або виключити їх) без фільтра по всій таблиці.
    # Плюс переіндексація коментарів (їх тисячі, не сотні тисяч) не чіпає
    # chunks_fts — масовий re-embed по chunks уже одного разу клав пошук
    # з 9с до 64с (див. memory/eval-verdict-flips-with-k).
    #
    # ЦІЛЬ ПОЛІМОРФНА. (target_type, target_id) замість FK на одну таблицю:
    # коментувати треба і транскрипт, і файл медіатеки, і задачу, і сутність,
    # і сесію запису. Реєстр допустимих типів живе в сервісному шарі
    # (app/services/comments.py TARGETS), не в БД — рівно як
    # meeting_entities.source і action_items.source у цій же схемі.
    #
    # kind НЕ ДЕКОРАТИВНИЙ. Він задає вагу в ранжуванні і те, чи підшивати
    # коментар до видачі, коли він сам не збігся із запитом: 'correction'
    # («насправді сума 12k, а не 20k») критичний саме там, де спільних слів
    # із питанням немає. weight — ручний override; NULL означає «взяти з kind».
    if current_version < 37:
        c.execute("PRAGMA table_info(comments)")
        if not c.fetchall():
            c.execute('''CREATE TABLE comments (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type       TEXT NOT NULL,      -- transcription | audio_download | ...
                target_id         INTEGER NOT NULL,
                body              TEXT NOT NULL,      -- будь-яка мова
                kind              TEXT NOT NULL DEFAULT 'note',
                weight            REAL,               -- NULL → похідна від kind
                pinned            INTEGER NOT NULL DEFAULT 0,
                anchor_time       REAL,               -- сек від початку запису
                anchor_chunk_id   INTEGER,
                author            TEXT,
                source            TEXT NOT NULL DEFAULT 'ui',   -- ui | live | mcp | import
                lang              TEXT,
                parent_id         INTEGER,            -- нитка коментарів
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at        TIMESTAMP,
                deleted_at        TIMESTAMP,          -- soft-delete, як усюди в схемі
                embedded_at       TIMESTAMP,
                embedding_model   TEXT,
                embedding_version INTEGER
            )''')
            # Головний доступ — «усі живі коментарі цієї картки»: список картки,
            # лічильник у видачі, автопідшивка в RAG. deleted_at у ключі, щоб
            # soft-deleted не читались з диска взагалі.
            c.execute('CREATE INDEX idx_comments_target '
                      'ON comments(target_type, target_id, deleted_at)')
            c.execute('CREATE INDEX idx_comments_created ON comments(created_at DESC)')
            # «Кого ще не проіндексовано» — гарячий запит індексера/бекфілу.
            c.execute('CREATE INDEX idx_comments_pending ON comments(embedded_at)')

        c.execute("PRAGMA table_info(comment_chunks)")
        if not c.fetchall():
            c.execute('''CREATE TABLE comment_chunks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id     INTEGER NOT NULL,
                chunk_index    INTEGER NOT NULL,
                text           TEXT NOT NULL,
                embedding      BLOB,                 -- float32 normalized (np.tobytes)
                token_estimate INTEGER,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(comment_id, chunk_index),
                FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE
            )''')
            c.execute('CREATE INDEX idx_comment_chunks_cid ON comment_chunks(comment_id)')

        # FTS5 external-content + три тригери — дослівне дзеркало chunks_fts
        # (v14). Форма та сама навмисно: лексичний шлях коментарів має
        # поводитись рівно як лексичний шлях транскриптів, інакше гібрид
        # порівнював би різнокаліброві сигнали.
        has_cc_fts = bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comment_chunks_fts'"
        ).fetchone())
        if not has_cc_fts:
            c.execute('''
                CREATE VIRTUAL TABLE comment_chunks_fts USING fts5(
                    text, content='comment_chunks', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 1'
                )
            ''')
            c.execute('''
                CREATE TRIGGER comment_chunks_ai AFTER INSERT ON comment_chunks BEGIN
                    INSERT INTO comment_chunks_fts(rowid, text) VALUES (new.id, new.text);
                END
            ''')
            c.execute('''
                CREATE TRIGGER comment_chunks_ad AFTER DELETE ON comment_chunks BEGIN
                    INSERT INTO comment_chunks_fts(comment_chunks_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                END
            ''')
            c.execute('''
                CREATE TRIGGER comment_chunks_au AFTER UPDATE ON comment_chunks BEGIN
                    INSERT INTO comment_chunks_fts(comment_chunks_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                    INSERT INTO comment_chunks_fts(rowid, text) VALUES (new.id, new.text);
                END
            ''')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(37, 'comments + comment_chunks + comment_chunks_fts — шар "
                  "коментарів власника: власний вектор/FTS-індекс, поліморфна ціль, "
                  "kind задає вагу в ранжуванні')")
        logger.info("Міграція v37: шар коментарів (comments/comment_chunks/FTS) створено")

    # === v38: провенанс похідних від коментаря (Волна 3) =====================
    # Коментар не лише шукається — він народжує звʼязки. Дві похідні:
    #
    # 1. ЗВʼЯЗКИ З ГРАФОМ. Пишуться в meeting_entities з source='comment' —
    #    новою колонки не треба, source (v34) уже є. Але провенанс тут не
    #    формальність: згадка в коментарі означає «названо в репліці ПРО
    #    зустріч», а не «названо НА зустрічі». Без мітки зріз за людиною тихо
    #    змішав би два різні твердження (урок derived-claims-need-second-source).
    #
    # 2. ЗАДАЧІ. Ось тут колонки бракує. action_items.source='comment' сказало
    #    б лише «з якогось коментаря», а на одному транскрипті їх може бути
    #    десять. Без comment_id повторний розбір мусив би або стирати задачі,
    #    витягнуті з СУСІДНІХ коментарів, або плодити дублі — обидва варіанти
    #    погані, і обидва мовчазні. comment_id дає точну ідемпотентність:
    #    переписуємо рівно свої рядки.
    #
    # analyzed_at/analyzed_model на comments — маркер «Claude уже розбирав».
    # Розбір платний і запускається кнопкою, тож інтерфейс мусить розрізняти
    # «ще не розбирали» і «розібрали, задач не знайшлось»: без маркера друге
    # виглядає як перше, і власник платить за той самий коментар двічі.
    if current_version < 38:
        c.execute("PRAGMA table_info(action_items)")
        acols = {row[1] for row in c.fetchall()}
        if acols and "comment_id" not in acols:
            c.execute("ALTER TABLE action_items ADD COLUMN comment_id INTEGER")
            c.execute("CREATE INDEX IF NOT EXISTS idx_action_items_comment "
                      "ON action_items(comment_id)")

        c.execute("PRAGMA table_info(comments)")
        ccols = {row[1] for row in c.fetchall()}
        if ccols:
            for col, decl in (("analyzed_at", "TIMESTAMP"), ("analyzed_model", "TEXT")):
                if col not in ccols:
                    c.execute(f"ALTER TABLE comments ADD COLUMN {col} {decl}")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(38, 'action_items.comment_id + comments.analyzed_* — провенанс "
                  "похідних від коментаря: граф (source=comment) і задачі (Волна 3)')")
        logger.info("Міграція v38: провенанс похідних від коментаря створено")

    # === v39: рядковий ключ цілі коментаря (Волна 4) =========================
    # Прорахунок v37: `comments.target_id INTEGER` мовчки припускав, що кожна
    # картка адресується числом. Сесія запису — ні: її id це `rec_<hex16>`
    # (`RecordingService._make_session_id`), і саме вона потрібна тоді, коли
    # коментар найцінніший — під час дзвінка, коли транскрипту ще не існує.
    #
    # Чому не запхати рядок у ту саму колонку. SQLite дозволив би: INTEGER-
    # affinity лишає нечислові рядки текстом. Але тоді одна колонка тримала б
    # два типи, кожен `int(target_id)` у сервісі став би міною, а запит
    # `WHERE target_id = 42` міг би зловити рядок «42». Окрема колонка робить
    # відмінність явною: тип цілі диктує, яким ключем вона адресується
    # (`comments.TARGETS`), і сервіс перемикається один раз у `_target_clause`.
    #
    # `target_id` у рядкових цілей тримає 0 (`comments.STR_TARGET_ID`): колонка
    # створена в v37 як NOT NULL, а знімати обмеження довелось би перебудовою
    # таблиці, на яку посилається FK з `comment_chunks`. Нуль безпечний —
    # AUTOINCREMENT починає з 1, тож id=0 не належить жодному рядку.
    #
    # Індекс дзеркалить idx_comments_target — вибірка та сама («усі живі
    # коментарі цієї картки»), лише ключ інший.
    if current_version < 39:
        c.execute("PRAGMA table_info(comments)")
        ccols = {row[1] for row in c.fetchall()}
        if ccols and "target_key" not in ccols:
            c.execute("ALTER TABLE comments ADD COLUMN target_key TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_comments_target_key "
                      "ON comments(target_type, target_key, deleted_at)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(39, 'comments.target_key — рядковий ключ цілі (сесія запису "
                  "rec_<hex>): коментар під час дзвінка, коли транскрипту ще немає')")
        logger.info("Міграція v39: comments.target_key створено")

    # === v40: transcriptions.duplicate_of — дублі аудіо/YouTube =============
    # Той самий дзвінок, завантажений двічі (перезалив файлу, відео-повтор,
    # повторна транскрипція запису), давав два повноцінні записи з однаковим
    # текстом — і обидва лізли у видачу RAG, з'їдаючи слоти per-meeting cap
    # тим самим вмістом.
    #
    # Запис НЕ видаляється і не отримує deleted_at: дубль лишається в
    # Бібліотеці (у нього можуть бути свої коментарі, файл, задачі), але
    # позначений `duplicate_of` → id оригіналу (найменший id групи) і
    # виключений з пошуку нарівні з soft-deleted.
    if current_version < 40:
        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols and "duplicate_of" not in tcols:
            c.execute("ALTER TABLE transcriptions ADD COLUMN duplicate_of INTEGER")
        if tcols:
            c.execute("CREATE INDEX IF NOT EXISTS idx_transcriptions_duplicate_of "
                      "ON transcriptions(duplicate_of)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(40, 'transcriptions.duplicate_of — дублі аудіо/YouTube: запис "
                  "лишається, але не індексується і не бере участі в пошуку')")
        logger.info("Міграція v40: transcriptions.duplicate_of створено")

    # === v41: ask_log — лог питань до архіву з оцінкою власника ==============
    # Golden-set наповнювався з `logs/mcp_calls.log` — а це лише MCP-канал,
    # лише текст питання (обрізаний до 600 символів) і жодного сигналу про
    # якість відповіді. Тут лежить сам факт відповіді: що спитали, яким
    # каналом (`ui`/`mcp`), у якому скоупі, які джерела процитовано, скільки
    # коштувало і — головне — 👍/👎 власника. `evals/build_golden.py
    # --from-ask-log` мінить звідси кандидатів, 👎 першими.
    #
    # Пишуться ЛИШЕ успішні відповіді (збій Claude рядка не створює): у
    # golden-set не потрібні питання, на які система впала з мережевої
    # причини. `rating`/`note`/`rated_at` — NULL до оцінки; `cost_usd` NULL,
    # якщо модель поза таблицею тарифів (краще порожньо, ніж ціна не тієї
    # моделі — та сама помилка, що лікував T6.2 у pricing.py).
    if current_version < 41:
        c.execute('''CREATE TABLE IF NOT EXISTS ask_log
                     (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         channel TEXT NOT NULL DEFAULT 'ui',
                         question TEXT NOT NULL,
                         scope_json TEXT,
                         k INTEGER,
                         model TEXT,
                         source_ids_json TEXT,
                         input_tokens INTEGER,
                         output_tokens INTEGER,
                         cache_read_tokens INTEGER,
                         cost_usd REAL,
                         answer TEXT,
                         rating INTEGER,
                         note TEXT,
                         rated_at TIMESTAMP
                     )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_ask_log_created ON ask_log(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ask_log_rating ON ask_log(rating)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(41, 'ask_log — лог питань UI+MCP з відповіддю, джерелами, токенами, "
                  "вартістю і оцінкою власника (сировина для golden-set)')")
        logger.info("Міграція v41: ask_log створено")

    conn.commit()
    conn.close()
