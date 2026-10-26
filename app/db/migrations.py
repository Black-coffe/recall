"""SQLite migrations with schema versioning.

Current versions:
  v1 - Base tables transcriptions + audio_downloads
  v2 - Indexes on created_at, source_type, language, youtube_id, title
  v3 - FTS5 virtual table + triggers for transcriptions
  v4 - polished_text/polished_at/polished_model columns (Claude polish)
  v5 - source_type in audio_downloads + recording_* columns (Phase 9.6)
  v6 - speakers + transcription_speaker_map (Phase 10 diarization)
  v7 - embedding BLOB in transcription_speaker_map (Phase 10.6 voice fingerprinting)
  v8 - summary_json + summary_at + summary_model (Phase 12.5 Claude summarize)
  v9 - translations_json (Phase 12.11 multi-lang Claude translation cache)
  v10 - segment_bookmarks + saved_searches (Phase 12.12)
  v11 - topics_json (Phase 12.19 Claude topic auto-tags)
  v12 - sentiment_json (Phase 12.25 per-speaker sentiment analysis)
  v13 - Meeting Memory: entities + entity_aliases + meeting_entities +
        action_items + enriched_* columns (Phase 13 - end-to-end RAG archive)
  v14 - chunks (+embedding BLOB) + chunks_fts + embedded_* columns
        (Phase 13B - local embeddings on the 3090 + hybrid search)
  v15 - categories + transcriptions.category_id (Phase 14 - directions/sections
        for clean RAG: fund/personal/AI/... are filtered end to end)
  v16 - document_* columns on transcriptions (Phase 16A - document
        upload PDF/DOCX/MD: source_type='document', the original in file_path)
  v17 - chunks.page/section + transcriptions.structure_json (Phase 16B -
        page/slide provenance for RAG citations; PDF/PPTX blocks)
  v18 - tg_* columns on transcriptions + tg_monitored_chats (Phase 17 -
        ingestion from a real Telegram account: source_type='telegram',
        chat/sender/link provenance, dedup by (chat_id,message_id))
  v19 - copilot_sessions + copilot_topics + copilot_events (Phase 19 - the live
        call copilot: session settings at start (vector/mode/importance/
        budget/local-only), topic memory with centroids, history of insights/
        triggers for viewing and export)
  v20 - categories.name_norm (casefold-normalized uniqueness key): SQLite
        COLLATE NOCASE folds only ASCII, so Cyrillic ("Фонд" vs "фонд") duplicates
        got through. name_norm = name.strip().casefold() with a UNIQUE index guarantees
        case-insensitive uniqueness for any alphabet (Phase 14.1)
  v22 - recording_video_tracks (new table) + audio_downloads.has_video +
        audio_downloads.primary_video_path (Phase 22 - screen video capture:
        per-monitor tracks tied to a recording; idempotent upsert)
  v23 - recording_video_tracks.region_x/y/w/h (Phase 22 region capture -
        monitor-local pixels of the captured region; NULL for full-monitor)
  v24 - video_keyframes (new table: scene frames + OCR text) +
        transcriptions.video_analysis_at/video_keyframes_count (Phase 23B-A -
        video understanding: keyframe OCR -> searchable RAG chunks, speaker='екран')
  v26 - jobs (new table, T2.1 - REMEDIATION_PLAN Wave 1): JobQueue
        persistence. Previously the state of background tasks (YouTube download, transcription,
        backfill enrichment, import-folder, recording finalize) lived only in
        an in-memory dict - a process restart/crash lost progress silently. The table is
        the source of truth on disk; at app.py start, queued/running jobs from the
        previous run are marked 'crashed' (analogous to recording
        SessionStore.recover_orphaned).

  v27 - soft-delete + speaker un-merge snapshot (T4.6, REMEDIATION_PLAN
        Wave 2, Option A): transcriptions.deleted_at / audio_downloads.
        deleted_at (epoch seconds, NULL = live record) - a single DELETE
        no longer erases the row at once (and the audio_downloads file is NOT
        removed at once either), it only hides it from all read paths (list/search/RAG/
        counters/dashboard); POST .../restore brings it back. Bulk-delete
        stays physical (no undo) - a deliberate narrowing. The table
        speaker_merges keeps a before-snapshot of every merge_speakers
        (deleted speakers rows + the previous transcription_speaker_map
        mapping + entities.speaker_id, which FK ON DELETE SET NULL zeroed) -
        POST /api/speakers/unmerge/<merge_id> restores it. Both mechanisms
        have a grace period (RECALL_SOFTDELETE_GRACE_DAYS, default 7 days) -
        app.services.retention.purge_soft_deleted() physically removes
        the expired (file + row / snapshot) at app.py start.

  v28 - transcriptions.embedding_version (T6.8): structural idempotency of
        re-embed when the chunking logic changes.

  v29 - action_items: due_date / due_precision / stale_at / dup_of (Track 1;
        the plan and measurement are in the local docs/plans/, outside the repository: they contain
        specifics of the personal archive). Deadlines were stored only as a raw
        phrase ("завтра", "четвер", "Q3 2026" - 426 distinct values), so the
        question "what has to be done this week" could not be queried.
        due_date is a computed ISO date relative to the meeting date, due_precision is
        honesty instead of false precision ("якнайшвидше" does not pretend to be a date).
        The raw phrase in due is NOT rewritten: the output shows both, so that
        a parser mistake is visible. stale_at/dup_of are for the offline passes of
        app.services.commitments (they delete nothing).

  v30 - TG: meeting_date = the message day instead of the ingest date (Wave 0).
  v31 - TG: tg_reply_to / tg_sender_id / tg_grouped_id / tg_edit_date (Wave 4).

  v32 - tg_threads + transcriptions.tg_thread_id/tg_thread_src (Wave 4.5):
        the unit of meaning in a conversation is a THREAD inside a chat, not the chat and not a single
        message. A chat is not a topic: in fund chats several projects run at once,
        so a category inherited from the chat assigns every message
        one direction regardless of content. The thread lives in its own
        table, because the chunks schema forbids a chunk over several records
        (transcription_id NOT NULL + UNIQUE(transcription_id, chunk_index)),
        while the message records stay as they were - dedup, links and provenance
        do not break.
  v43 - chunks.context_prefix + chunks_fts over (context_prefix, text) (Wave B):
        the chunk context header (type, title/chat, date, speaker/author,
        direction|thread|page + a summary line of the unit of meaning) takes part in
        the vector and BM25, but does NOT end up in citations - `chunks.text` stays
        bare text.
  v45 - two-layer taxonomy (spec directions-projects-tags, ADR-023 section B):
        `categories.parent_id` (NULL = direction, otherwise a project inside a
        direction; exactly two levels, checked in app code `app/services/
        categories.py`, there is no CHECK in the schema) + `tags`/`record_tags`/
        `tg_chat_tags` (a controlled tag vocabulary, attached ONLY to
        transcriptions) + `transcriptions.suggested_tags_json` (Claude hints,
        not yet accepted - the truth lives in `record_tags`).
        DDL only: no data transfer, `name_norm` is not recomputed.
  v46 - embedding_epochs (spec directions-projects-tags, story 17): the DB
        remembers by which context-prefix FORMAT each epoch
        (`model`, `version`) of embeddings was built. The epoch number is an act of configuration
        (`EMBED_VERSION` in `.env`, ADR-016), so by itself it does not distinguish
        "the format changed" from "all is well": under a number that already exists in the DB,
        a new prefix would lie next to the old ones, and `reembed.stale_ids` would never
        see them (the pair matches). The backfill stamps everything that existed BEFORE
        v46 with format 1 - the code declares its `embeddings.PREFIX_FORMAT` and
        REFUSES to write chunks into an epoch built with a different format
        (ADR-017: a vector never mixes epochs). DDL only + one backfill.
  v47 - node name uniqueness is among SIBLINGS, not across the whole table (spec
        directions-projects-tags, wave 6, ADR-023 section E "replaced 2026-09-22"):
        "Маркетинг" can live both under "Фонд Робота" and under "Фріланс", and
        the direction "Маркетинг" sits next to a foreign project of the same name. The first
        NON-additive migration of the spec: the inline `name TEXT NOT NULL UNIQUE COLLATE
        NOCASE` (v15) cannot be dropped by ALTER, so the table is rebuilt
        (CREATE new -> copy with the same ids -> DROP -> RENAME). The global
        UNIQUE index `idx_categories_name_norm` (v20) dies along with the old
        table; instead there is `idx_categories_parent_name` UNIQUE on
        `(COALESCE(parent_id, 0), name_norm)`. `slug UNIQUE` stays.

Run: init_database(db_path) at app start.
Idempotent - every migration does INSERT OR IGNORE into schema_versions.
"""
import logging
import sqlite3

logger = logging.getLogger(__name__)


def init_database(db_path: str):
    """Creates the DB (if absent) and applies all pending migrations."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # WAL - better concurrency
    c.execute('PRAGMA journal_mode=WAL')

    # Schema versions table
    c.execute('''CREATE TABLE IF NOT EXISTS schema_versions
                 (
                     version INTEGER PRIMARY KEY,
                     applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                     description TEXT
                 )''')

    c.execute('SELECT MAX(version) FROM schema_versions')
    row = c.fetchone()
    current_version = row[0] if row[0] is not None else 0

    # === v1: Base tables ===
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
        logger.info("Migration v1: Base tables created")

    # === v2: Indexes ===
    if current_version < 2:
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_youtube_id ON audio_downloads(youtube_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_created_at ON audio_downloads(created_at DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audio_title ON audio_downloads(title)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at ON transcriptions(created_at DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_source_type ON transcriptions(source_type)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_language ON transcriptions(language)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (2, 'Added indexes on transcriptions and audio_downloads')")
        logger.info("Migration v2: Indexes created")

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
        logger.info("Migration v3: FTS5 index created")

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
        logger.info("Migration v4: polished_* columns added")

    # === v5: source_type + recording columns in audio_downloads (Phase 9.6) ===
    # We extend the existing audio_downloads table instead of creating a new
    # recordings table - the UI and queries already work over audio_downloads,
    # duplicating the logic is not needed. Recordings (Phase 9) will have
    # source_type='recording', YouTube downloads - 'youtube' (default).
    if current_version < 5:
        c.execute("PRAGMA table_info(audio_downloads)")
        cols = {row[1] for row in c.fetchall()}
        if 'source_type' not in cols:
            # DEFAULT 'youtube' is automatically assigned to old rows
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
        logger.info("Migration v5: source_type + recording_* columns added")

    # === v6: Speakers + transcription_speaker_map (Phase 10 diarization) ===
    # speakers - a global table of "known" speakers. When renamed in one
    # transcript, the name is put into speakers (by UNIQUE name) and used
    # as an autocomplete hint in subsequent transcripts.
    #
    # transcription_speaker_map - per-transcript mapping raw_label ("SPEAKER_00",
    # "self", "SPEAKER_UNKNOWN") -> speaker_id. NULL speaker_id = "not named yet",
    # the frontend shows "Спікер N" by the numeric part of raw_label.
    #
    # Seed row "Ви" (is_self=1) - for auto-routing the mic stream in recordings.
    # FK enforcement is not mandatory (other tables are also without PRAGMA foreign_keys=ON);
    # cascade-delete is performed explicitly in app code when deleting a transcription/speaker.
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
        # Seed "Ви" - for is_self=1 (the mic stream of recordings). Not updated if already present.
        c.execute("INSERT OR IGNORE INTO speakers (name, is_self) VALUES ('Ви', 1)")
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (6, 'speakers + transcription_speaker_map for diarization')")
        logger.info("Migration v6: speakers + transcription_speaker_map created")

    # === v7: embedding in transcription_speaker_map (Phase 10.6) ===
    # Voice fingerprinting: during diarization pyannote returns an embedding per
    # SPEAKER_NN cluster (192-256 floats depending on the model). We store
    # raw bytes in the map for further:
    # 1. Auto-match: on the next transcribe we compare the embeddings of new
    #    SPEAKER_NN with all speakers.embedding via cosine similarity.
    #    Match >0.85 -> auto-link to an existing speaker_id.
    # 2. PATCH speakers: on manual naming we take the embedding from the map and
    #    average it into speakers.embedding (running average).
    #
    # speakers.embedding was already added in v6.
    if current_version < 7:
        c.execute("PRAGMA table_info(transcription_speaker_map)")
        cols = {row[1] for row in c.fetchall()}
        if 'embedding' not in cols:
            c.execute('ALTER TABLE transcription_speaker_map ADD COLUMN embedding BLOB')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (7, 'embedding BLOB у transcription_speaker_map for voice fingerprinting')")
        logger.info("Migration v7: embedding column added to transcription_speaker_map")

    # === v8: Summary + key points + action items (Phase 12.5) ===
    if current_version < 8:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols:  # the table exists
            if 'summary_json' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN summary_json TEXT')
            if 'summary_at' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN summary_at TIMESTAMP')
            if 'summary_model' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN summary_model TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (8, 'summary_json + summary_at + summary_model for Claude summarize')")
        logger.info("Migration v8: summary_* columns added")

    # === v9: Translations cache (Phase 12.11) ===
    if current_version < 9:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'translations_json' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN translations_json TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (9, 'translations_json for multi-lang Claude translation cache')")
        logger.info("Migration v9: translations_json column added")

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
        logger.info("Migration v10: bookmarks + saved_searches created")

    # === v11: Topic auto-tags (Phase 12.19) ===
    if current_version < 11:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'topics_json' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN topics_json TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (11, 'topics_json for Claude topic auto-tags')")
        logger.info("Migration v11: topics_json column added")

    # === v12: Sentiment analysis (Phase 12.25) ===
    if current_version < 12:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'sentiment_json' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN sentiment_json TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (12, 'sentiment_json per-speaker sentiment')")
        logger.info("Migration v12: sentiment_json column added")

    # === v13: Meeting Memory - end-to-end entities layer + action items (Phase 13) ===
    # Turns Whisper from an "archive of separate transcripts" into a RAG memory system:
    # people / projects / organizations become first-class entities shared across
    # ALL meetings (with aliases to deduplicate spelling variants -
    # Acmecorp / Акме / Acmez -> one entity). Enables end-to-end queries:
    # "all meetings about project X", "all tasks for Andrii", "where Y was mentioned".
    #
    # Why normalized tables and not JSON columns (like topics_json):
    # topics_json is free text within ONE transcript, cannot be merged across
    # meetings. Cross-meeting search needs an entity graph = JOINs.
    #
    # enriched_* on transcriptions - an idempotency marker: backfill and the auto-job
    # skip the already enriched (enriched_at IS NOT NULL), and changing
    # enrichment_version lets one force a re-run when prompts are updated.
    if current_version < 13:
        # --- entities: canonical people/projects/organizations/topics ---
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

        # --- entity_aliases: spelling variants -> one entity ---
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

        # --- meeting_entities: which entity in which meeting + weight ---
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

        # --- action_items: tasks/agreements, linked to the owner entity ---
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

        # --- enriched_* markers on transcriptions (idempotency) ---
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
                # the date of the meeting itself (for meeting_archive import - from the file name/content);
                # for native transcriptions = created_at, filled in at enrich.
                c.execute('ALTER TABLE transcriptions ADD COLUMN meeting_date TEXT')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (13, 'Meeting Memory: entities + aliases + meeting_entities + action_items + enriched_* markers')")
        logger.info("Migration v13: Meeting Memory (entities/action_items/enriched_*) created")

    # === v14: Chunks + local embeddings + chunk-level FTS5 (Phase 13B) ===
    # A semantic (vector) RAG layer. A transcript is cut into chunks (windows by
    # speaker turns with overlap), each chunk gets a local embedding
    # (multilingual-e5-large on the RTX 3090, float32 BLOB). Search is a hybrid:
    #   vector cosine (semantics) + chunks_fts BM25 (keywords) -> RRF merge.
    # The chunk level (not the transcript level) is needed for citations: date+speaker+timecode.
    #
    # embedded_* on transcriptions - an idempotency marker separate from enriched_*:
    # embeddings are computed LOCALLY (without Claude), so they may exist even without
    # ANTHROPIC_API_KEY; the skip logic is independent of card-enrichment.
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

        # chunk-level FTS5 (external content) + auto-sync triggers (like transcriptions_fts)
        c.execute("PRAGMA table_info(chunks_fts)")
        # FTS5 is a virtual table; PRAGMA table_info returns its columns if it exists
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
        logger.info("Migration v14: chunks + chunks_fts + embedded_* created")

    # === v15: Categories / directions (Phase 14) ===
    # The "direction" dimension on every transcript (Fund / Personal / AI / ...).
    # Goal: clean RAG - search/answers/graph can be limited to one section,
    # so as not to mix work, personal and downloaded YouTube ("hodgepodge").
    # One direction per record (category_id), a managed list (the categories table).
    # We do not inline the FK in ALTER (like the rest of the schema) - integrity is in app code: when
    # a category is deleted we first null transcriptions.category_id.
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
                ('Фонд Робота', 'work', '#1c7cf2', 'fa-building-columns', 1),
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

        # bulk-assign: meeting_archive -> «Фонд Робота» (user decision).
        # Only if the transcriptions table exists (cols non-empty) - otherwise UPDATE
        # fails with "no such table" on a DB without transcriptions (as in the rest of the block - if cols).
        if cols:
            fund = c.execute("SELECT id FROM categories WHERE slug = 'work'").fetchone()
            if fund:
                c.execute(
                    "UPDATE transcriptions SET category_id = ? "
                    "WHERE source_type = 'meeting_archive' AND category_id IS NULL",
                    (fund[0],))

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (15, 'categories + transcriptions.category_id (Phase 14 напрямки)')")
        logger.info("Migration v15: categories + category_id created")

    # === v16: Document ingestion (Phase 16A) ===
    # Documents (PDF / DOCX / Markdown / ...) reuse THE SAME transcriptions
    # table with source_type='document'. Once the parsed text lies
    # in transcript_text - the whole lower stack (chunks + embeddings + chunks_fts +
    # enrichment + entity graph + directions + RAG chat + History) works unchanged.
    # Here only the additional document metadata (type, page count, hash for dedup).
    # file_path is reused for the path to the saved ORIGINAL file.
    #
    # Chunk provenance (p. N / sheet) will be added in 16B - for the plain-text chunking of 16A
    # (_chunk_from_text) extra columns are not needed.
    if current_version < 16:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols:
            if 'doc_type' not in cols:
                # 'pdf' | 'docx' | 'md' | 'txt' | ... (source extension)
                c.execute('ALTER TABLE transcriptions ADD COLUMN doc_type TEXT')
            if 'original_filename' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN original_filename TEXT')
            if 'page_count' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN page_count INTEGER')
            if 'byte_size' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN byte_size INTEGER')
            if 'content_hash' not in cols:
                # sha256 of the parsed text - for dedup of repeated uploads (16E)
                c.execute('ALTER TABLE transcriptions ADD COLUMN content_hash TEXT')
            if 'parsed_at' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN parsed_at TIMESTAMP')
            if 'parser_version' not in cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN parser_version INTEGER')
            c.execute('CREATE INDEX IF NOT EXISTS idx_transcriptions_content_hash ON transcriptions(content_hash)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (16, 'document_* columns on transcriptions (Phase 16A document ingestion)')")
        logger.info("Migration v16: document_* columns added")

    # === v17: Document provenance - page/slide for citations (Phase 16B) ===
    # PDF/PPTX are parsed into blocks (page/slide). So that RAG citations point to
    # "p. 3 / slide 5", chunks get page+section. structure_json on
    # transcriptions persists the blocks {text,page,section} - so that on re-embed
    # (model/chunking change) provenance is restored without re-parsing.
    #
    # Audio transcripts and text documents (md/txt/docx) have no structure ->
    # structure_json IS NULL -> the chunker falls back to the old behavior (segments/text),
    # and chunks.page/section stay NULL. No regression for audio.
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
        logger.info("Migration v17: provenance columns (chunks.page/section + structure_json) added")

    # === v18: Telegram ingestion (Phase 17) ===
    # Listening to a real TG ACCOUNT (MTProto/Telethon). Every message
    # of the monitored chats becomes a record in transcriptions with source_type='telegram':
    # text - directly, voice/audio/video - via faster-whisper, photos - OCR,
    # documents - document_parser. Once the text is in transcript_text - the whole
    # lower stack (chunks+embeddings+enrichment+graph+directions+RAG) works unchanged.
    #
    # tg_* columns - message provenance (where from/from whom/when/link), so RAG
    # can cite "chat X, from Y, 2026-05-29". Dedup: UNIQUE(tg_chat_id,
    # tg_message_id) - protection from double ingest when real-time and backfill
    # overlap on one message.
    #
    # tg_monitored_chats - which chats to listen to (managed from the UI by checkboxes). enabled=0
    # -> the listener ignores. category_id -> which direction Recall puts records from this
    # chat into (fund chats -> "Фонд Робота" automatically). last_message_id is the cursor
    # for backfill/resume (from where to load the history).
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
            # Dedup of TG messages. Partial index - only over TG records (where
            # tg_message_id NOT NULL), so that NULLs of other source_types do not conflict.
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
        logger.info("Migration v18: Telegram (tg_* columns + tg_monitored_chats) created")

    # === v19: Co-pilot - the live call copilot (Phase 19) ===
    # Skeleton of a live copilot session. At recording start the operator sets the vector
    # (category), mode (light/medium/hard), importance (low/medium/high),
    # the budget and the "local only" toggle -> a row in copilot_sessions with a full
    # settings snapshot (config_json - the resolved mode x importance matrix).
    #
    # copilot_topics - the topic memory of the session (an e5 centroid + the cached RAG context
    # of the topic), so that returning to an old topic reloads the old context instead of
    # searching anew (Step 2-3).
    # copilot_events - a historical track of everything the copilot did (topic changes,
    # retrieval, local/verified insights, escalations, operator actions, tokens),
    # tied to the recording timecode (ts_offset_sec) - for viewing and export
    # (Step 7-8). At Step 1 the tables are only created; filling comes later.
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
        logger.info("Migration v19: Co-pilot (copilot_sessions/topics/events) created")

    # === v20: categories.name_norm - a casefold uniqueness key (Cyrillic too) ===
    # SQLite COLLATE NOCASE folds only ASCII a-z -> "Фонд" and "фонд" passed as
    # different. name_norm = casefold(name) + a UNIQUE index closes the hole end to end.
    if current_version < 20:
        c.execute("PRAGMA table_info(categories)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'name_norm' not in cols:
            c.execute('ALTER TABLE categories ADD COLUMN name_norm TEXT')
            # backfill from existing names
            for cid, name in c.execute("SELECT id, name FROM categories").fetchall():
                c.execute("UPDATE categories SET name_norm = ? WHERE id = ?",
                          ((name or '').strip().casefold(), cid))
            # UNIQUE index. If the user already has casefold duplicates - we do not fail
            # the migration (the index stays a plain one; uniqueness is held by app code).
            try:
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name_norm "
                          "ON categories(name_norm)")
            except sqlite3.IntegrityError:
                logger.warning("Migration v20: there are casefold duplicates of directions - "
                               "using a plain index, uniqueness is enforced by app code")
                c.execute("CREATE INDEX IF NOT EXISTS idx_categories_name_norm "
                          "ON categories(name_norm)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (20, 'categories.name_norm casefold-ключ унікальності (Phase 14.1 кирилиця-дублі)')")
        logger.info("Migration v20: categories.name_norm created")

    # === v21: own category_id in audio_downloads (recording UX) ===
    # Previously the audio library took the direction ONLY from the linked transcript
    # (JOIN transcriptions by file_path). An untranscribed recording -> direction
    # None -> not caught by the direction filter, although the user set a direction when
    # recording. We give the recording row its own category_id (from the copilot/recording
    # session), and the library does COALESCE(transcript category, own).
    if current_version < 21:
        c.execute("PRAGMA table_info(audio_downloads)")
        cols = {row[1] for row in c.fetchall()}
        if 'category_id' not in cols:
            c.execute('ALTER TABLE audio_downloads ADD COLUMN category_id INTEGER')
            c.execute('CREATE INDEX IF NOT EXISTS idx_audio_category_id '
                      'ON audio_downloads(category_id)')
            # Backfill of existing records: for source_type='recording' we take
            # the category from copilot_sessions by recording_session_id.
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
                # copilot_sessions may not exist in a very old DB - we do not fail
                pass
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (21, 'audio_downloads.category_id — власний напрямок запису (recording UX)')")
        logger.info("Migration v21: audio_downloads.category_id created + backfill")

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
        logger.info("Migration v22: recording_video_tracks + video columns")

    # === v23: region columns on recording_video_tracks (Phase 22 region capture) ===
    if current_version < 23:
        c.execute("PRAGMA table_info(recording_video_tracks)")
        cols = {row[1] for row in c.fetchall()}
        for col in ('region_x', 'region_y', 'region_w', 'region_h'):
            if col not in cols:
                c.execute(f'ALTER TABLE recording_video_tracks ADD COLUMN {col} INTEGER')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (23, 'recording_video_tracks region_x/y/w/h (Phase 22 region capture)')")
        logger.info("Migration v23: region columns on recording_video_tracks")

    # === v24: Video understanding — keyframe OCR → RAG chunks (Phase 23B-A) ===
    # video_keyframes stores every selected frame (scene-detection or
    # uniform fallback): timestamp in the video, jpg path, OCR text, score.
    # Chunks with text != '' are inserted into chunks with speaker='екран' and section in
    # the format 'екран MM:SS' -> hybrid (vector+FTS) search covers the on-screen text too.
    # transcriptions.video_analysis_at / video_keyframes_count - an idempotency marker
    # (analogous to embedded_at / enriched_at for the audio layer).
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
        logger.info("Migration v24: video_keyframes + video_analysis_at/video_keyframes_count created")

    # === v25: vision description of frames (Phase 23B) ===
    # video_keyframes.vision_text - a short description of "what is shown on screen" from a
    # VL model (local Qwen2.5-VL via Ollama, $0, or Claude vision).
    # Goes into the RAG chunk (speaker='екран') next to OCR -> searchable even for frames
    # without on-screen text (photos/diagrams). vision_model/vision_at - provenance.
    if current_version < 25:
        c.execute("PRAGMA table_info(video_keyframes)")
        vcols = {row[1] for row in c.fetchall()}
        if vcols:  # the table exists (created in v24)
            if 'vision_text' not in vcols:
                c.execute('ALTER TABLE video_keyframes ADD COLUMN vision_text TEXT')
            if 'vision_model' not in vcols:
                c.execute('ALTER TABLE video_keyframes ADD COLUMN vision_model TEXT')
            if 'vision_at' not in vcols:
                c.execute('ALTER TABLE video_keyframes ADD COLUMN vision_at TIMESTAMP')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES (25, 'video_keyframes vision_text/vision_model/vision_at (Phase 23B keyframe vision descriptions)')")
        logger.info("Migration v25: vision columns on video_keyframes")

    # === v26: jobs - JobQueue persistence (T2.1, REMEDIATION_PLAN Wave 1) ===
    # JobQueue used to keep state only in memory (a dict) - a process restart/crash
    # lost the progress of long tasks (YouTube download, transcription, backfill
    # enrichment, import-folder, recording finalize) silently, with no trace
    # for the user. The jobs table is the source of truth on disk; the dict in JobQueue
    # stays a "hot" cache on top of it (reading /api/jobs does not go to
    # SQLite on every request). At app.py start all rows with state IN
    # ('queued','running') are marked 'crashed' (analogous to recording
    # SessionStore.recover_orphaned) - a job no longer vanishes without explanation.
    #
    # DDL + version-insert - in one explicit transaction (BEGIN/COMMIT), so that
    # a process crash in the middle of the migration does not leave a half-state (table created,
    # but the version not recorded in schema_versions -> a repeated start would try
    # to create it again; CREATE TABLE IF NOT EXISTS is idempotent anyway,
    # but atomicity here is an explicit safeguard, not a reliance on that alone).
    # If conn is already in a transaction (the typical path - a fresh DB, where v1..v25 just
    # accumulated uncommitted INSERTs) - we do not try to open a nested
    # transaction, but simply take part in the one already open (it will commit together
    # with the rest of the migrations in the final conn.commit() below).
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
        logger.info("Migration v26: jobs table created (JobQueue persistence)")

    # === v27: soft-delete + speaker un-merge snapshot (T4.6, Wave 2) ===
    # transcriptions.deleted_at / audio_downloads.deleted_at (epoch seconds,
    # REAL; NULL = live record) - a single DELETE sets a mark instead of
    # physical deletion (the audio file is NOT erased at once either - the main
    # P0 from the audit: often the only copy). All read paths (list/search/FTS/
    # vector-retrieval/counters/RAG/dashboard) additionally filter
    # `deleted_at IS NULL` in app code (see transcription.py/audio_library.py/
    # retrieval.py/memory.py/...). Bulk-delete stays physical - a deliberate
    # choice of Option A (undo only for single deletes).
    #
    # speaker_merges - a before-snapshot of merge_speakers (Phase 12.8) for
    # unmerge: deleted speakers rows (embedding/usage_count/color),
    # the previous transcription_speaker_map mapping (which raw_labels pointed
    # to which speaker_id BEFORE the relink) and entities.speaker_id, which FK
    # ON DELETE SET NULL zeroed when merge_ids were deleted from speakers.
    # merged_speaker_ids - a JSON list for a quick filter/diagnostics;
    # before_json - the full snapshot for restoring (embedding in base64).
    # restored_at - NULL until restored; purge removes the snapshot by
    # merged_at (regardless of restored_at - the possibility of undo always
    # has a time limit, while the merge itself is not rolled back).
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
        logger.info("Migration v27: deleted_at (transcriptions/audio_downloads) + speaker_merges created")

    # === v28: transcriptions.embedding_version - structural idempotency (T6.8) ===
    # Until now chunk_and_embed_transcription compared ONLY embedding_model - a change
    # of the chunking LOGIC (without a model name change, e.g. T6.5) was not detected as
    # "re-embed needed". We add a nullable column next to embedding_model:
    # NULL (existing records before this migration) was treated as "compatible with the current
    # EMBED_VERSION" - so that the migration does not force a mass re-embed retroactively.
    # WARNING 14.08.2026 (T6.5, EMBED_VERSION=2): the NULL semantics were changed to "old
    # slicing -> re-embed needed" - exactly when the slicing really changed.
    # The source of truth is embeddings.chunk_and_embed_transcription and the mirror
    # condition in enrichment.list_unenriched_ids; here only the history of the column.
    if current_version < 28:
        c.execute("PRAGMA table_info(transcriptions)")
        cols = {row[1] for row in c.fetchall()}
        if cols and 'embedding_version' not in cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN embedding_version INTEGER')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(28, 'transcriptions.embedding_version — структурна idempotency для "
                  "re-embed при зміні логіки чанкінгу (T6.8)')")
        logger.info("Migration v28: transcriptions.embedding_version created")

    # === v29: action_items - normalized deadline + stale/dup (Track 1) ======
    # The raw phrase stays in due (we do not rewrite it!), computed fields appear
    # next to it. NULL due_date = "the date is not derivable" (e.g. "до наступної зустрічі") -
    # this is NOT an error but a separate class, visible through due_precision.
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
        logger.info("Migration v29: action_items due_date/due_precision/stale_at/dup_of created")

    # === v30: TG - event date from tg_date (Wave 0 of the Telegram roadmap) ===========
    # _persist never wrote meeting_date, and the whole stack below dates a record via
    # COALESCE(meeting_date, substr(created_at,1,10)) - i.e. by the MOMENT OF INGEST.
    # On live data: meeting_date is empty in 3142 of 3360 TG records, and all 218
    # filled ones (set by the card phase from created_at) are wrong too - e.g. tx=328
    # has 2026-05-29 for a message from 2026-02-10. It broke two things at once:
    # the recency boost in retrieval.py (old correspondence looked fresh) and the anchor of
    # commitments.parse_due (the raw phrase "до пʼятниці" was expanded relative to the upload day).
    # Lossless repair - tg_date is filled for ALL TG records.
    # 'localtime': tg_date is in UTC, but the day of the conversation is needed (see _meeting_date).
    # Goes BEFORE any history catch-up: otherwise it would only multiply the discrepancy.
    if current_version < 30:
        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if {'tg_date', 'meeting_date', 'source_type'} <= tcols:
            c.execute(
                "UPDATE transcriptions SET meeting_date = date(tg_date, 'localtime') "
                "WHERE source_type = 'telegram' AND tg_date IS NOT NULL "
                "AND (meeting_date IS NULL OR meeting_date <> date(tg_date, 'localtime'))"
            )
            logger.info("Migration v30: event date fixed in %d TG records", c.rowcount)

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(30, 'TG: meeting_date = день повідомлення (tg_date) замість дати "
                  "інжесту — recency-буст і якір parse_due (Волна 0)')")

    # === v31: TG - signals Telegram already gives and we threw away (Wave 4) ======
    # tg_reply_to - the EXACT conversation thread from Telegram itself. Without it "what is this
    # a reply to" has to be guessed: in a chat with parallel topics, gluing by a
    # time window stitches the unrelated, and that is why a search for
    # «коли зустріч з губернатором» returned 7 questions without a single answer - the answer was
    # the next message of the thread, with no shared words with the query.
    # tg_sender_id - a stable author key (tg_sender is a display name
    # that a person changes). tg_grouped_id - an album (several media = one message
    # to a person). tg_edit_date - an edit is visible only through it.
    # The columns are forward-only: the later they appear, the fewer messages will
    # get them, so the migration goes BEFORE the rest of Wave 4.
    if current_version < 31:
        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            for col, decl in (("tg_sender_id", "INTEGER"), ("tg_reply_to", "INTEGER"),
                              ("tg_grouped_id", "INTEGER"), ("tg_edit_date", "TEXT")):
                if col not in tcols:
                    c.execute(f"ALTER TABLE transcriptions ADD COLUMN {col} {decl}")
            # The "show the whole thread" lookup goes from parent to children.
            c.execute("CREATE INDEX IF NOT EXISTS idx_tx_tg_reply "
                      "ON transcriptions(tg_chat_id, tg_reply_to) "
                      "WHERE tg_reply_to IS NOT NULL")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(31, 'TG: tg_reply_to/tg_sender_id/tg_grouped_id/tg_edit_date — "
                  "нитка розмови, стабільний автор, альбоми, правки (Волна 4)')")
        logger.info("Migration v31: TG signals (reply_to/sender_id/grouped_id/edit_date) created")

    # === v32: conversation threads inside a chat (Wave 4.5) ========================
    # The problem we are treating: in a chat of messages these are 955 unconnected records,
    # and "Ок" physically carries no context. A live failure: for
    # «коли зустріч з губернатором» search returned 7 slots out of 8, and all seven were questions with no
    # answer (the answer was the next message of the thread, no shared words with
    # the query -> neither the vector nor FTS finds it).
    #
    # Why a thread and not a chat: in "Main Team Chat" Acmecorp,
    # Nova Dance, legal questions and LP updates run at once, but the category is given by the whole chat
    # (_resolve_category reads tg_monitored_chats.category_id). Measured: in 4
    # fund chats messages, all with one direction, while inside
    # «Ковальчука» is mentioned 23 times - and in no other chat. The system knew WHERE
    # it was said and did not know WHAT ABOUT.
    #
    # centroid - the centroid of the thread messages' e5 vectors. We keep it NOT as a
    # decider but as a candidate ranker: a measurement on the live archive showed
    # that the raw cosine between messages of one chat does not distinguish a continuation
    # of a topic from a random pair (reply pairs 0.828 vs random 0.827,
    # separation +0.02 sigma). A short reply has nothing to embed into a topic - "no"
    # is not about the subject but about the speech register. The decision is made by the local LLM
    # (see app/services/tg_threads.py), the centroid only narrows its choice.
    #
    # tg_thread_src stores HOW the decision was made - without it one can neither
    # tell an exact thread from a guess, nor recompute only the guesses when
    # the prompt changes. The values are deliberately NOT merged into one "auto":
    #   reply  - from tg_reply_to, this is knowledge of Telegram itself (cannot be wrong);
    #   llm    - the model assigned it to a topic out of several messages;
    #   single - the message is alone: either the model decided so, or it was
    #            the only one in the burst (nothing to group with);
    #   pending- a preliminary decision on live ingest (the model has not been asked yet);
    #            resettle_pending will re-lay it when the burst finishes talking;
    #   burst  - DEGRADATION: there was no model, the batch stayed one thread;
    #   orphan - DEGRADATION: the model stayed silent about this message.
    # The last two are exactly what one must be able to find and recompute later.
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
            # Lookup of a chat's open threads when assigning a new burst.
            c.execute('CREATE INDEX idx_tg_threads_chat ON tg_threads(chat_id, status, last_date)')

        c.execute("PRAGMA table_info(transcriptions)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            for col, decl in (("tg_thread_id", "INTEGER"), ("tg_thread_src", "TEXT")):
                if col not in tcols:
                    c.execute(f"ALTER TABLE transcriptions ADD COLUMN {col} {decl}")
            # Stitching a thread's neighbours on output goes from the thread to its messages.
            c.execute("CREATE INDEX IF NOT EXISTS idx_tx_tg_thread "
                      "ON transcriptions(tg_thread_id, tg_date) "
                      "WHERE tg_thread_id IS NOT NULL")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(32, 'tg_threads + transcriptions.tg_thread_id/tg_thread_src — "
                  "нитка розмови як одиниця сенсу в переписці (Волна 4.5)')")
        logger.info("Migration v32: tg_threads + tg_thread_id created")

    # === v33: live chat profile (Wave 4.5.2) ===============================
    # The owner's wording: "every new message should not be unique -
    # it should supplement the context of its group, constantly expand it".
    #
    # WHAT CHANGED AFTER v32. In the original design the profile had to guess topics from
    # the raw stream of messages. Now topics are open threads, i.e. an already
    # computed fact, not a model guess. So the profile keeps exactly
    # what is NOT derived from threads and is a property of the chat itself: who is here and what
    # they do, what hangs unanswered, what was agreed.
    #
    # Hard numbers (how much was written, when someone appeared, when they last wrote)
    # are computed by SQL and the model is NOT trusted with them: that is a fact, and an invented number in a
    # person's profile is worse than its absence. The model receives them ready-made and writes only
    # the role description and the digest.
    #
    # messages_seen / last_message_date - a watermark: the profile is updated by
    # a threshold (N new messages or once a day), NEVER on every
    # message. Otherwise it is exactly those 3142 paid calls that Wave 0
    # put a guard against - only now local, but just as pointless.
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
        logger.info("Migration v33: tg_chat_context created")

    # === v34: provenance of graph links (Wave 4.5.3) =========================
    # Telegram is the vast majority of the archive and yet almost absent from the entity graph: 154
    # records out of 3951. Because of this the project slice, list_stale_topics and the Monday
    # digest are blind to the bulk of the archive.
    #
    # We treat it WITHOUT a model. The graph already knows entities from calls and documents,
    # so TG should not be enriched anew (a 7B on live data would breed duplicates
    # "Акме/Acmecorp/акмекорп" in a graph where duplicates are already an unsolved problem), but
    # linked to what is already known.
    #
    # source distinguishes HOW the link was set: NULL - links inherited from
    # Claude enrichment, 'thread_match' - found by matching the thread text against
    # the graph. Without it one can neither recompute only the automatic ones nor
    # tell a model extraction from a string match, and these differ in reliability.
    if current_version < 34:
        c.execute("PRAGMA table_info(meeting_entities)")
        cols = {row[1] for row in c.fetchall()}
        if cols and "source" not in cols:
            c.execute("ALTER TABLE meeting_entities ADD COLUMN source TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_me_source ON meeting_entities(source)")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(34, 'meeting_entities.source — провенанс звʼязку графа; TG "
                  "звʼязується з наявними сутностями по нитках (Волна 4.5.3)')")
        logger.info("Migration v34: meeting_entities.source created")

    # === v35: commitments from correspondence (Wave 5.1) ===========================
    # Wave 0 forbade a Claude card on telegram: a card on a one-liner
    # "Ок" is useless, and over records also paid. But tasks in correspondence ARE
    # really handed out - just not in one message but in a thread. Now that
    # the thread exists (v32), it becomes the unit of enrichment, and the guard is lifted not
    # entirely but selectively: first a local triage, then Claude only on
    # what was selected.
    #
    # WHY TRIAGE BY MODEL, NOT BY REGEX. The wave plan said "promise markers +
    # top-8 senders + length". A measurement over threads: the marker list
    # ("треба", "надішлю", "чекаю", ...) fires on a share of the threads - that is not a filter.
    # On a pilot of 40 threads the regex and the 7B diverged in 13 of 40 cases, and in
    # the vast majority it was not the regex that was right: "треба" in "треба визнати" is
    # not a commitment, while "Давай я зараз уточню" is a commitment without any
    # marker. Triage is local ($0, ~4 s per thread), so the cost of a mistake is time.
    #
    # triage_msgs - a watermark: a thread that grew after the verdict will be
    # reassessed; a thread that did not change spends neither the model nor Claude.
    #
    # action_items.source distinguishes the origin of a task: NULL - inherited
    # (a Claude card of a call/document), 'tg_thread' - extracted from a correspondence
    # thread. Without it a rerun would either erase foreign rows or
    # breed duplicates; besides, the digest shows whether a task was spoken aloud on
    # a call or stayed as text in a chat.
    if current_version < 35:
        c.execute("PRAGMA table_info(tg_threads)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            for col, decl in (("triage", "TEXT"), ("triage_at", "TIMESTAMP"),
                              ("triage_model", "TEXT"), ("triage_msgs", "INTEGER"),
                              ("tasks_at", "TIMESTAMP"), ("tasks_model", "TEXT")):
                if col not in tcols:
                    c.execute(f"ALTER TABLE tg_threads ADD COLUMN {col} {decl}")
            # The queries "who has not been triaged yet" and "whom triage marked" - both
            # frequent (passes go in batches), both on these two columns.
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
        logger.info("Migration v35: thread triage + action_items.source created")

    if current_version < 36:
        # Why a task ended up in stale. Until now there was one reason (age), so
        # there was no column; now there are two - age and "the topic went quiet", and without
        # provenance a mass un-marking can be neither explained nor rolled back
        # selectively. Existing rows are all from `mark_stale`, i.e. 'age'.
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
        logger.info("Migration v36: action_items.stale_reason created")

    # === v37: comments layer (Comments Layer) ================================
    # The owner's comment on any archive card: a clarification, a correction,
    # an emphasis. It goes into the same search as the transcript, but with a HIGHER weight -
    # it is not a raw verbatim but a deliberately written sentence about what is
    # actually meant.
    #
    # WHY A SEPARATE INDEX, NOT ROWS IN `chunks`. Two reasons, and the first is
    # blocking: `chunks.transcription_id` is NOT NULL with an FK. A comment on a Media
    # library file that is not transcribed yet cannot physically be put into `chunks`, and
    # dropping NOT NULL is a rebuild of a hot table of 100k+ rows, under
    # which hangs an FTS5 external-content with content_rowid='id' and three triggers.
    # The second reason would remain valid even if the first vanished: a separate
    # index makes the comments priority ONE explicit knob instead of
    # a coefficient smeared over the shared pipeline, and lets one narrow
    # a query to comments (or exclude them) without a filter over the whole table.
    # Plus reindexing comments (thousands, not hundreds of thousands) does not touch
    # chunks_fts - a mass re-embed over chunks once already took search
    # from 9s to 64s (see memory/eval-verdict-flips-with-k).
    #
    # THE TARGET IS POLYMORPHIC. (target_type, target_id) instead of an FK to one table:
    # one must comment on a transcript, a media library file, a task, an entity
    # and a recording session. The registry of allowed types lives in the service layer
    # (app/services/comments.py TARGETS), not in the DB - exactly like
    # meeting_entities.source and action_items.source in this same schema.
    #
    # kind is NOT DECORATIVE. It sets the weight in ranking and whether to attach a
    # comment to the output when it did not itself match the query: 'correction'
    # ("насправді сума 12k, а не 20k") is critical precisely where there are no shared words
    # with the question. weight is a manual override; NULL means "take from kind".
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
            # The main access is "all live comments of this card": the card list,
            # the counter in the output, auto-attaching in RAG. deleted_at is in the key so that
            # soft-deleted ones are not read from disk at all.
            c.execute('CREATE INDEX idx_comments_target '
                      'ON comments(target_type, target_id, deleted_at)')
            c.execute('CREATE INDEX idx_comments_created ON comments(created_at DESC)')
            # "Who has not been indexed yet" - the hot query of the indexer/backfill.
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

        # FTS5 external-content + three triggers - a verbatim mirror of chunks_fts
        # (v14). The same form deliberately: the lexical path of comments must
        # behave exactly like the lexical path of transcripts, otherwise the hybrid
        # would compare signals of different calibration.
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
        logger.info("Migration v37: comments layer (comments/comment_chunks/FTS) created")

    # === v38: provenance of comment derivatives (Wave 3) =====================
    # A comment is not only searched - it gives birth to links. Two derivatives:
    #
    # 1. GRAPH LINKS. Written to meeting_entities with source='comment' -
    #    no new column needed, source (v34) already exists. But provenance here is not
    #    a formality: a mention in a comment means "named in a remark ABOUT
    #    the meeting", not "named AT the meeting". Without the label the person slice would quietly
    #    mix two different claims (lesson derived-claims-need-second-source).
    #
    # 2. TASKS. Here a column is lacking. action_items.source='comment' would say
    #    only "from some comment", and one transcript can have ten of them.
    #    Without comment_id a re-parse would have to either erase tasks
    #    extracted from NEIGHBOURING comments, or breed duplicates - both options are
    #    bad, and both silent. comment_id gives exact idempotency:
    #    we rewrite exactly our own rows.
    #
    # analyzed_at/analyzed_model on comments - a "Claude already parsed this" marker.
    # The parse is paid and launched by a button, so the interface must tell
    # "not parsed yet" from "parsed, no tasks found": without the marker the second
    # looks like the first, and the owner pays for the same comment twice.
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
        logger.info("Migration v38: provenance of comment-derived rows created")

    # === v39: string key of the comment target (Wave 4) =========================
    # A miscalculation of v37: `comments.target_id INTEGER` silently assumed that every
    # card is addressed by a number. A recording session is not: its id is `rec_<hex16>`
    # (`RecordingService._make_session_id`), and it is needed precisely when
    # a comment is most valuable - during a call, when the transcript does not exist yet.
    #
    # Why not stuff the string into the same column. SQLite would allow it: INTEGER
    # affinity leaves non-numeric strings as text. But then one column would hold
    # two types, every `int(target_id)` in the service would become a mine, and the query
    # `WHERE target_id = 42` could catch the row "42". A separate column makes
    # the difference explicit: the target type dictates by which key it is addressed
    # (`comments.TARGETS`), and the service switches once in `_target_clause`.
    #
    # `target_id` for string targets holds 0 (`comments.STR_TARGET_ID`): the column was
    # created in v37 as NOT NULL, and dropping the constraint would require rebuilding
    # a table referenced by an FK from `comment_chunks`. Zero is safe -
    # AUTOINCREMENT starts from 1, so id=0 belongs to no row.
    #
    # The index mirrors idx_comments_target - the same selection ("all live
    # comments of this card"), only the key differs.
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
        logger.info("Migration v39: comments.target_key created")

    # === v40: transcriptions.duplicate_of - audio/YouTube duplicates =============
    # The same call uploaded twice (a file re-upload, a video repeat,
    # a repeated transcription of a recording) produced two full records with identical
    # text - and both climbed into the RAG output, eating the slots of the per-meeting cap
    # with the same content.
    #
    # The record is NOT deleted and does not get deleted_at: the duplicate stays in
    # the Library (it may have its own comments, file, tasks), but it is
    # marked `duplicate_of` -> the id of the original (the smallest id of the group) and
    # excluded from search on a par with soft-deleted.
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
        logger.info("Migration v40: transcriptions.duplicate_of created")

    # === v41: ask_log - a log of questions to the archive with the owner's rating ==============
    # The golden set used to be filled from `logs/mcp_calls.log` - but that is only the MCP channel,
    # only the question text (truncated to 600 characters) and no signal about the
    # quality of the answer. Here lies the fact of the answer itself: what was asked, through which
    # channel (`ui`/`mcp`), in which scope, which sources were cited, how much it
    # cost and - the main thing - the owner's thumbs up/down. `evals/build_golden.py
    # --from-ask-log` mines candidates from here, thumbs down first.
    #
    # ONLY successful answers are written (a Claude failure creates no row): the
    # golden set does not need questions the system failed on for a network
    # reason. `rating`/`note`/`rated_at` are NULL until rated; `cost_usd` is NULL
    # if the model is outside the tariff table (better empty than the price of the wrong
    # model - the same mistake T6.2 fixed in pricing.py).
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
        logger.info("Migration v41: ask_log created")

    # === v42: provenance of the TG thread summary (Wave B, app/services/summaries.py) ==
    # `tg_threads.summary` (from v32) already existed, but nobody wrote or read it -
    # the thread summary was not a "unit of meaning with a summary" on a par with
    # a call/document. The columns here are the same provenance that already exists for
    # triage/task extraction (triage_at/triage_model/triage_msgs): when, by which
    # model, and most importantly - `summary_source_ids_json` = the ids of EXACTLY those messages
    # (`transcriptions.id`) that made it into the prompt, since a preview ceiling (as in
    # tg_tasks) means not all thread messages fit there.
    # `summary_msgs` is a watermark: a thread that grew needs a new summary,
    # but is not re-summarized on every message.
    if current_version < 42:
        c.execute("PRAGMA table_info(tg_threads)")
        tcols = {row[1] for row in c.fetchall()}
        if tcols:
            for col, decl in (("summary_source_ids_json", "TEXT"),
                              ("summary_at", "TEXT"),
                              ("summary_model", "TEXT"),
                              ("summary_msgs", "INTEGER")):
                if col not in tcols:
                    c.execute(f"ALTER TABLE tg_threads ADD COLUMN {col} {decl}")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(42, 'tg_threads.summary_source_ids_json/summary_at/summary_model/"
                  "summary_msgs — провенанс сводки нитки (Волна B)')")
        logger.info("Migration v42: tg_threads.summary provenance created")

    # === v43: chunks.context_prefix + chunks_fts over (context_prefix, text) ====
    # Wave B, story 05. A chunk was self-sufficient only for a person who already knows
    # where it comes from: the bare text of the reply went into the vector and BM25, so "what about
    # Acmecorp in September" did not find a message where the chat name is present but the reply
    # itself has none. `context_prefix` is one or two lines from DB FIELDS (type, title/chat,
    # date, speaker/author, direction | thread | page) + a summary line of the unit of
    # meaning (`summaries.unit_summary_line`). It is written by `embeddings.
    # build_context_prefix` at embedding time.
    #
    # The prefix takes part in search and does NOT take part in citations: `chunks.text`
    # stays bare text, and retrieval/rag/export read exactly `text`
    # (the column lists there are explicit, no SELECT *).
    #
    # chunks_fts is external content (`content='chunks'`), so the FTS columns
    # are matched to the chunks columns BY NAME: a column can be added to the index
    # only by recreating the virtual table + `rebuild`. Both triggers are rewritten for
    # both fields. An unqualified `MATCH` (retrieval, commitments, archive_grep)
    # searches ALL columns - no separate edit to the consumers is needed.
    # Idempotency: the rebuild is done only if chunks_fts still has no
    # context_prefix column (not just by the version number).
    if current_version < 43:
        c.execute("PRAGMA table_info(chunks)")
        chunk_cols = {row[1] for row in c.fetchall()}
        if chunk_cols:
            if 'context_prefix' not in chunk_cols:
                c.execute('ALTER TABLE chunks ADD COLUMN context_prefix TEXT')

            c.execute("PRAGMA table_info(chunks_fts)")
            fts_cols = {row[1] for row in c.fetchall()}
            if 'context_prefix' not in fts_cols:
                c.execute('DROP TRIGGER IF EXISTS chunks_ai')
                c.execute('DROP TRIGGER IF EXISTS chunks_ad')
                c.execute('DROP TRIGGER IF EXISTS chunks_au')
                c.execute('DROP TABLE IF EXISTS chunks_fts')
                c.execute('''
                    CREATE VIRTUAL TABLE chunks_fts USING fts5(
                        context_prefix, text, content='chunks', content_rowid='id',
                        tokenize='unicode61 remove_diacritics 1'
                    )
                ''')
                c.execute('''
                    CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
                        INSERT INTO chunks_fts(rowid, context_prefix, text)
                        VALUES (new.id, new.context_prefix, new.text);
                    END
                ''')
                c.execute('''
                    CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, context_prefix, text)
                        VALUES ('delete', old.id, old.context_prefix, old.text);
                    END
                ''')
                c.execute('''
                    CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, context_prefix, text)
                        VALUES ('delete', old.id, old.context_prefix, old.text);
                        INSERT INTO chunks_fts(rowid, context_prefix, text)
                        VALUES (new.id, new.context_prefix, new.text);
                    END
                ''')
                # Existing rows (mostly with a NULL prefix) must remain
                # findable by their text: rebuild rereads the content table.
                c.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(43, 'chunks.context_prefix + chunks_fts по (context_prefix, text) — "
                  "контекстний заголовок чанка у вектор і BM25 (Волна B)')")
        logger.info("Migration v43: chunks.context_prefix + chunks_fts rebuild")

    # === v44: transcriptions.title/description + these columns in transcriptions_fts ===
    # Spec editable-title-description, story 01. `source_name` stays
    # provenance (file name, YouTube title, TG message preview) and is
    # NOT overwritten: the own title lives in a separate `title` column, so a
    # TG re-persist that updates `source_name` does not erase a manual rename.
    #
    # transcriptions_fts is external content (`content='transcriptions'`), so the
    # index columns are matched to the table columns BY NAME: a column can be added
    # only by recreating the virtual table + `rebuild`
    # (the same pattern as v43 for chunks_fts). The column order is kept:
    # `transcript_text` stays zeroth, because `snippet(transcriptions_fts, 0, ...)`
    # in `GET /api/history` addresses it by number.
    # Idempotency: the rebuild is done only if the FTS still has no
    # `title` column (not just by the version number).
    if current_version < 44:
        c.execute("PRAGMA table_info(transcriptions)")
        tx_cols = {row[1] for row in c.fetchall()}
        if tx_cols:
            if 'title' not in tx_cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN title TEXT')
            if 'description' not in tx_cols:
                c.execute('ALTER TABLE transcriptions ADD COLUMN description TEXT')

            c.execute("PRAGMA table_info(transcriptions_fts)")
            fts_cols = {row[1] for row in c.fetchall()}
            if fts_cols and 'title' not in fts_cols:
                c.execute('DROP TRIGGER IF EXISTS transcriptions_ai')
                c.execute('DROP TRIGGER IF EXISTS transcriptions_ad')
                c.execute('DROP TRIGGER IF EXISTS transcriptions_au')
                c.execute('DROP TABLE IF EXISTS transcriptions_fts')
                c.execute('''
                    CREATE VIRTUAL TABLE transcriptions_fts USING fts5(
                        transcript_text, source_name, youtube_title, title, description,
                        content='transcriptions',
                        content_rowid='id',
                        tokenize='unicode61 remove_diacritics 1'
                    )
                ''')
                c.execute('''
                    CREATE TRIGGER transcriptions_ai
                    AFTER INSERT ON transcriptions BEGIN
                        INSERT INTO transcriptions_fts(rowid, transcript_text, source_name,
                                                       youtube_title, title, description)
                        VALUES (new.id, new.transcript_text, new.source_name,
                                new.youtube_title, new.title, new.description);
                    END
                ''')
                c.execute('''
                    CREATE TRIGGER transcriptions_ad
                    AFTER DELETE ON transcriptions BEGIN
                        INSERT INTO transcriptions_fts(transcriptions_fts, rowid, transcript_text,
                                                       source_name, youtube_title, title, description)
                        VALUES ('delete', old.id, old.transcript_text, old.source_name,
                                old.youtube_title, old.title, old.description);
                    END
                ''')
                c.execute('''
                    CREATE TRIGGER transcriptions_au
                    AFTER UPDATE ON transcriptions BEGIN
                        INSERT INTO transcriptions_fts(transcriptions_fts, rowid, transcript_text,
                                                       source_name, youtube_title, title, description)
                        VALUES ('delete', old.id, old.transcript_text, old.source_name,
                                old.youtube_title, old.title, old.description);
                        INSERT INTO transcriptions_fts(rowid, transcript_text, source_name,
                                                       youtube_title, title, description)
                        VALUES (new.id, new.transcript_text, new.source_name,
                                new.youtube_title, new.title, new.description);
                    END
                ''')
                # Existing rows (all with title/description = NULL) must remain
                # findable by source_name/text: rebuild rereads the table.
                c.execute("INSERT INTO transcriptions_fts(transcriptions_fts) VALUES('rebuild')")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(44, 'transcriptions.title/description + transcriptions_fts rebuild — "
                  "власна назва й опис запису, редаговані після збереження')")
        logger.info("Migration v44: transcriptions.title/description + transcriptions_fts rebuild")

    # === v45: direction tree (parent_id) + tag vocabulary ===================
    # Spec directions-projects-tags, story 01 (tracer), ADR-023 section B.
    #
    # `categories.parent_id` is the second (and last) level of the same table:
    # NULL = direction, non-NULL = a project inside a direction. There is deliberately NO CHECK
    # in the schema: "exactly two levels" is checked by app code
    # (`app/services/categories.py`), because SQLite cannot change a CHECK without
    # recreating the table, and a future third level (if the owner ever
    # asks for it) must not run into a copy-over migration.
    #
    # Tags attach ONLY to `transcriptions` (ADR section B): audio in the Media library
    # shows the tags of its transcript by the same COALESCE path as the direction.
    # `record_tags.source` ('manual'|'claude'|'chat') is needed for
    # detaching: removing a tag from a TG chat removes only rows with source='chat',
    # a manual tag stays. Claude hints live separately
    # (`suggested_tags_json`) - filters read only `record_tags`.
    #
    # The migration is additive: no data transfer, `name_norm` of existing directions
    # is NOT recomputed (the `categories.norm_name` key matches the old one).
    if current_version < 45:
        c.execute("PRAGMA table_info(categories)")
        cat_cols = {row[1] for row in c.fetchall()}
        if cat_cols and 'parent_id' not in cat_cols:
            c.execute('ALTER TABLE categories ADD COLUMN parent_id INTEGER')
        if cat_cols:
            c.execute('CREATE INDEX IF NOT EXISTS idx_categories_parent '
                      'ON categories(parent_id)')

        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tags'")
        if not c.fetchone():
            c.execute('''CREATE TABLE tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_norm TEXT NOT NULL UNIQUE,     -- NFKC+casefold-ключ (кирилиця теж)
                color TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')

        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_tags'")
        if not c.fetchone():
            c.execute('''CREATE TABLE record_tags (
                transcription_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',   -- manual | claude | chat
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (transcription_id, tag_id)
            )''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_record_tags_tag ON record_tags(tag_id)')

        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_chat_tags'")
        if not c.fetchone():
            c.execute('''CREATE TABLE tg_chat_tags (
                chat_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, tag_id)
            )''')

        c.execute("PRAGMA table_info(transcriptions)")
        tx_cols = {row[1] for row in c.fetchall()}
        if tx_cols and 'suggested_tags_json' not in tx_cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN suggested_tags_json TEXT')

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(45, 'categories.parent_id (напрямок → проєкт) + tags/record_tags/"
                  "tg_chat_tags + transcriptions.suggested_tags_json (ADR-023)')")
        logger.info("Migration v45: categories.parent_id + tag dictionary created")

    # === v46: embedding_epochs - the prefix format of each epoch =================
    # Spec directions-projects-tags, story 17 (ADR-023 section D, ADR-017).
    #
    # A table row = "the epoch (model, version) was built with prefix format N".
    # The backfill sets format 1 for everything already in `transcriptions`: any
    # pair that existed BEFORE this migration was built with the prefix of the previous
    # spec. Rows with `embedding_version IS NULL` (embedded before v28) do not
    # get into the table - `INSERT OR IGNORE` silently skips them on
    # `NOT NULL`, and the code's current pair never has a NULL version.
    #
    # DDL only + this backfill: no data is transferred, chunks are not touched.
    if current_version < 46:
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embedding_epochs'")
        if not c.fetchone():
            c.execute('''CREATE TABLE embedding_epochs (
                model TEXT NOT NULL,
                version INTEGER NOT NULL,
                prefix_format INTEGER NOT NULL,     -- 1 = префікс до спеки directions-projects-tags
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (model, version)
            )''')

        # `transcriptions` may not exist yet (an artificial DB in migration tests,
        # where the schema is raised from an intermediate version) - then there is nothing to backfill.
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcriptions'")
        if c.fetchone():
            c.execute("INSERT OR IGNORE INTO embedding_epochs (model, version, prefix_format) "
                      "SELECT DISTINCT embedding_model, embedding_version, 1 "
                      "FROM transcriptions WHERE embedding_model IS NOT NULL")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(46, 'embedding_epochs: формат контекстного префікса кожної епохи "
                  "(наявні пари = формат 1, ADR-023 §D)')")
        logger.info("Migration v46: embedding_epochs created")

    # === v47: node name uniqueness - among SIBLINGS ========================
    # Spec directions-projects-tags, wave 6 (ADR-023 section E, a dated replacement).
    #
    # Before v47 a node name was unique across the WHOLE table (inline `UNIQUE COLLATE
    # NOCASE` on `name` from v15 + a UNIQUE index on `name_norm` from v20), so
    # "Маркетинг" could not live at once under "Фонд Робота" and under "Фріланс" -
    # yet this is exactly how the live archive is laid out. Now uniqueness is among siblings:
    # directions among themselves, projects of one parent among themselves; a direction and a foreign
    # project may coincide.
    #
    # An inline UNIQUE cannot be dropped by ALTER - this is the first NON-additive migration
    # of the spec, i.e. a table rebuild. Two traps, because of which everything here is as it is:
    #   1. **FK.** `tg_monitored_chats.category_id` has `FOREIGN KEY ... ON DELETE
    #      SET NULL` (v18). With foreign_keys enabled `DROP TABLE categories`
    #      acts as a DELETE of all rows and SILENTLY nulls the direction of every
    #      monitored chat. Hence `PRAGMA foreign_keys=OFF` BEFORE the transaction
    #      (a PRAGMA inside a transaction is a no-op), the previous value
    #      is restored afterwards, and `PRAGMA foreign_key_check` proves that nothing
    #      is left dangling.
    #      **The guard is DEFENSIVE, not load-bearing (measured 22.09.2026, wave 7):** on
    #      the path the migration actually takes FKs are already off - `init_database`
    #      opens a raw `sqlite3.connect` (`:139`), and there `PRAGMA
    #      foreign_keys` is 0 by default. `foreign_keys=ON` is turned on by
    #      `app/db/connection.py:40`, i.e. a DIFFERENT connection - not this one. We keep it for
    #      the case of calling `init_database` on a connection with FKs enabled
    #      (the trap is proven by `test_v47_drop_table_with_fk_on_would_null_chat_category`
    #      on a hand-built FK-ON connection; ADR-023 section E, the wave 7 paragraph).
    #   2. **Uniqueness that may not be achievable.** As in v20: if the live DB already
    #      has two siblings with the same `name_norm`, the migration does not fail -
    #      the index stays a plain one, and uniqueness is held by app code
    #      (`categories._assert_free_sibling_name`, a check in Python BEFORE the write).
    #
    # Idempotency is by the FACT of the schema, not just by the version number: if there is
    # `idx_categories_parent_name` and no global UNIQUE on
    # `name`/`name_norm` -> no rebuild is needed.
    if current_version < 47:
        c.execute("PRAGMA table_info(categories)")
        cat_info = c.fetchall()
        if cat_info:
            unique_indexes = []
            for idx in c.execute("PRAGMA index_list(categories)").fetchall():
                if not idx[2]:            # (seq, name, unique, origin, partial)
                    continue
                idx_cols = [r[2] for r in
                            c.execute(f"PRAGMA index_info('{idx[1]}')").fetchall()]
                unique_indexes.append((idx[1], idx_cols))
            has_sibling_index = any(n == 'idx_categories_parent_name'
                                    for n, _ in unique_indexes)
            # Global uniqueness = UNIQUE on exactly one column name/name_norm.
            # The siblings index does not fall under this: its first column is the expression
            # COALESCE(...), and PRAGMA index_info returns None for it.
            global_unique = [n for n, cols in unique_indexes
                             if cols in (['name'], ['name_norm'])]

            if global_unique or not has_sibling_index:
                conn.commit()             # PRAGMA has no effect inside a transaction
                # Defensive (see trap 1 above): on the init_database path FKs are already
                # off, prev_fk is 0 here - the guard is for the case of an FK-ON connection.
                prev_fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
                c.execute("PRAGMA foreign_keys=OFF")
                try:
                    col_names = [row[1] for row in cat_info]
                    defs = []
                    for _cid, cname, ctype, notnull, dflt, pk in cat_info:
                        if pk:
                            defs.append(f"{cname} INTEGER PRIMARY KEY AUTOINCREMENT")
                            continue
                        piece = f"{cname} {ctype or 'TEXT'}"
                        if notnull:
                            piece += " NOT NULL"
                        if dflt is not None:
                            piece += f" DEFAULT {dflt}"
                        if cname == 'slug':
                            piece += " UNIQUE"    # the only uniqueness that remains
                        defs.append(piece)
                    cols_sql = ", ".join(col_names)

                    c.execute("BEGIN")
                    c.execute("DROP TABLE IF EXISTS categories_new")
                    c.execute("CREATE TABLE categories_new (\n  "
                              + ",\n  ".join(defs) + "\n)")
                    c.execute(f"INSERT INTO categories_new ({cols_sql}) "
                              f"SELECT {cols_sql} FROM categories")
                    c.execute("DROP TABLE categories")
                    c.execute("ALTER TABLE categories_new RENAME TO categories")
                    c.execute('CREATE INDEX IF NOT EXISTS idx_categories_parent '
                              'ON categories(parent_id)')
                    try:
                        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
                                  "idx_categories_parent_name "
                                  "ON categories(COALESCE(parent_id, 0), name_norm)")
                    except sqlite3.IntegrityError:
                        logger.warning("Migration v47: there are siblings with the same name_norm - "
                                       "using a plain index, uniqueness "
                                       "is enforced by app code")
                        c.execute("CREATE INDEX IF NOT EXISTS idx_categories_parent_name "
                                  "ON categories(COALESCE(parent_id, 0), name_norm)")
                    conn.commit()
                    broken = c.execute("PRAGMA foreign_key_check").fetchall()
                    if broken:
                        logger.warning("Migration v47: foreign_key_check is not empty "
                                       "(%s rows) - check the references to categories",
                                       len(broken))
                finally:
                    c.execute(f"PRAGMA foreign_keys={'ON' if prev_fk else 'OFF'}")

        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(47, 'categories: унікальність назви серед братів "
                  "(idx_categories_parent_name) замість глобальної (ADR-023 §E)')")
        logger.info("Migration v47: node name uniqueness - among siblings")

    # === v48: visual video timeline (spec video-visual-timeline) ============
    # Additive: the frame span (end_sec), type, repeat by content, change score,
    # OCR word boxes (for painting over secrets on the fly), time within the video itself.
    # Frame images are NO LONGER stored - `image_path` stays for old
    # Phase 23 rows and is NULL for new ones. `transcriptions.video_analysis_params` is
    # the algorithm version + parameters: different ones -> recomputation.
    if current_version < 48:
        c.execute("PRAGMA table_info(video_keyframes)")
        vk_cols = {r[1] for r in c.fetchall()}
        if vk_cols:
            for col, decl in (('end_sec', 'REAL'), ('frame_type', 'TEXT'),
                              ('duplicate_of', 'INTEGER'), ('change_score', 'REAL'),
                              ('ocr_boxes_json', 'TEXT'), ('video_time_sec', 'REAL')):
                if col not in vk_cols:
                    c.execute(f'ALTER TABLE video_keyframes ADD COLUMN {col} {decl}')
        c.execute("PRAGMA table_info(transcriptions)")
        tx_cols = {r[1] for r in c.fetchall()}
        if tx_cols and 'video_analysis_params' not in tx_cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN video_analysis_params TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(48, 'video_keyframes: відрізок, тип, повтор, рамки OCR; "
                  "transcriptions.video_analysis_params')")
        logger.info("Migration v48: visual video timeline")

    # === v49: screen_secrets - raw secrets from the screen ==========================
    # Passwords/tokens from frame OCR. In video_keyframes and chunks only the mask
    # "[🔒 секрет]" lies; the raw values are here, and are read ONLY by the local script
    # `python -m app.services.screen_secrets reveal` (TTY + confirmation, into a file).
    # No HTTP endpoint and no MCP tool reads this table.
    if current_version < 49:
        c.execute('''CREATE TABLE IF NOT EXISTS screen_secrets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        keyframe_id INTEGER NOT NULL
                            REFERENCES video_keyframes(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        value TEXT NOT NULL,
                        bbox_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_screen_secrets_kf ON screen_secrets(keyframe_id)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(49, 'screen_secrets: сирі секрети з кадрів, лише для локального reveal')")
        logger.info("Migration v49: screen_secrets")

    # === v50: pipe_runs - history of scheduled pipes (K4) =======================
    # Every pipe run (`app/services/pipes.py`) is a row: schedule/catch-up/manual,
    # what it was rendered with (claude|plain), cost, error. Additive.
    if current_version < 50:
        c.execute('''CREATE TABLE IF NOT EXISTS pipe_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pipe TEXT NOT NULL,
                        trigger TEXT NOT NULL,
                        scheduled_for TEXT,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        rendered_by TEXT,
                        cost_usd REAL DEFAULT 0,
                        error TEXT,
                        chars INTEGER DEFAULT 0)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_pipe_runs_pipe ON pipe_runs(pipe, started_at)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(50, 'pipe_runs: історія труб за розкладом (K4)')")
        logger.info("Migration v50: pipe_runs")

    # === v51: log of task status/deadline changes (K5 k5-task-buttons) ==========
    # Who closed / postponed a task and when - from the briefing buttons (bot) and from /tasks (web).
    # `undo_of` - an undo event references the undone one.
    if current_version < 51:
        c.execute('''CREATE TABLE IF NOT EXISTS action_item_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_id INTEGER NOT NULL,
                        at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        actor TEXT,
                        action TEXT NOT NULL,
                        old_status TEXT,
                        new_status TEXT,
                        old_due_date TEXT,
                        new_due_date TEXT,
                        old_due_precision TEXT,
                        new_due_precision TEXT,
                        undo_of INTEGER)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_action_item_events_item '
                  'ON action_item_events(item_id, id)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(51, 'action_item_events: журнал змін статусу задач (K5)')")
        logger.info("Migration v51: action_item_events")

    # === v52: Gmail ingest (gmail-ingest-01) =====================================
    # Accounts hold only the address: the app password lives in the OS keyring.
    if current_version < 52:
        c.execute('''CREATE TABLE IF NOT EXISTS mail_accounts (
                        id INTEGER PRIMARY KEY,
                        address TEXT NOT NULL UNIQUE,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        last_poll_at TEXT,
                        last_error_code TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS mail_labels (
                        id INTEGER PRIMARY KEY,
                        account_id INTEGER NOT NULL,
                        folder TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        category_id INTEGER,
                        enrich INTEGER NOT NULL DEFAULT 0,
                        system INTEGER NOT NULL DEFAULT 0,
                        uidvalidity INTEGER,
                        last_uid INTEGER,
                        synced_since TEXT,
                        last_sync_at TEXT,
                        last_error_code TEXT,
                        UNIQUE(account_id, folder))''')
        c.execute('''CREATE TABLE IF NOT EXISTS mail_attachments (
                        email_tid INTEGER NOT NULL,
                        document_tid INTEGER NOT NULL,
                        filename TEXT,
                        content_type TEXT,
                        size_bytes INTEGER,
                        PRIMARY KEY(email_tid, document_tid))''')
        c.execute("PRAGMA table_info(transcriptions)")
        tx_cols = {r[1] for r in c.fetchall()}
        if tx_cols:
            for col, decl in (('mail_account_id', 'INTEGER'), ('mail_message_id', 'TEXT'),
                              ('mail_thread_key', 'TEXT'), ('mail_in_reply_to', 'TEXT'),
                              ('mail_from', 'TEXT'), ('mail_to', 'TEXT'),
                              ('mail_subject', 'TEXT'), ('mail_folder', 'TEXT'),
                              ('mail_enrich', 'INTEGER')):
                if col not in tx_cols:
                    c.execute(f'ALTER TABLE transcriptions ADD COLUMN {col} {decl}')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_mail_msg '
                      'ON transcriptions(mail_account_id, mail_message_id) '
                      'WHERE mail_message_id IS NOT NULL')
            c.execute('CREATE INDEX IF NOT EXISTS idx_tx_mail_thread '
                      'ON transcriptions(mail_thread_key)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(52, 'Gmail ingest: mail_accounts, mail_labels, mail_attachments, transcriptions.mail_*')")
        logger.info("Migration v52: gmail ingest")

    # === v53: mcp_actions - audit of every mutating /api request (mcp-foundations-b) ===
    # One row per non-GET /api call: who (classified + claimed labels), what, the gate's
    # decision, the outcome. No body, no query string, no secret. Purged after 90 days.
    if current_version < 53:
        c.execute('''CREATE TABLE IF NOT EXISTS mcp_actions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        at TEXT NOT NULL,
                        request_id TEXT,
                        actor TEXT NOT NULL,
                        claimed_actor TEXT,
                        claimed_tool TEXT,
                        method TEXT NOT NULL,
                        path TEXT NOT NULL,
                        endpoint TEXT,
                        action TEXT,
                        object_ref TEXT,
                        summary TEXT,
                        decision TEXT,
                        http_status INTEGER,
                        error_code TEXT,
                        duration_ms INTEGER)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_mcp_actions_at ON mcp_actions(at)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(53, 'mcp_actions: audit of mutating API requests')")
        logger.info("Migration v53: mcp_actions")

    # === v54: transcript_versions - side store of re-transcribed versions (ADR-044) ===
    # The canonical transcriptions columns stay the only thing readers use; NULL pointer =
    # one implicit version. The original row is created lazily by the version service.
    if current_version < 54:
        c.execute('''CREATE TABLE IF NOT EXISTS transcript_versions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transcription_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        is_original INTEGER NOT NULL DEFAULT 0,
                        model_used TEXT,
                        language TEXT,
                        processing_time REAL,
                        transcript_text TEXT,
                        segments TEXT,
                        polished_text TEXT,
                        polished_at TEXT,
                        polished_model TEXT,
                        speaker_map_json TEXT)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_transcript_versions_tx '
                  'ON transcript_versions(transcription_id, created_at)')
        c.execute("PRAGMA table_info(transcriptions)")
        tx_cols = [r[1] for r in c.fetchall()]
        if tx_cols and 'active_version_id' not in tx_cols:
            c.execute('ALTER TABLE transcriptions ADD COLUMN active_version_id INTEGER')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(54, 'transcript_versions + transcriptions.active_version_id')")
        logger.info("Migration v54: transcript_versions")

    # === v55: provenance columns for agent (MCP) writes; NULL = owner/UI, 'mcp' = agent ===
    if current_version < 55:
        for table, col in (('comments', 'confirmed_at'), ('transcription_speaker_map', 'source'),
                           ('categories', 'created_source'), ('transcriptions', 'meta_source')):
            c.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in c.fetchall()]
            if cols and col not in cols:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {col} TEXT')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(55, 'provenance columns: comments.confirmed_at, transcription_speaker_map.source, "
                  "categories.created_source, transcriptions.meta_source')")
        logger.info("Migration v55: provenance columns")

    # === v56: tg_chat_events - journal of chat direction / enabled changes (mcp-telegram-automation) ===
    # The previous value is recorded at change time so a change can be undone; values are text
    # (category_id or '' for none; '0'|'1' for enabled).
    if current_version < 56:
        c.execute('''CREATE TABLE IF NOT EXISTS tg_chat_events (
                        id INTEGER PRIMARY KEY,
                        at TEXT NOT NULL,
                        chat_id INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('category','enabled')),
                        old_value TEXT,
                        new_value TEXT,
                        source TEXT NOT NULL,
                        actor TEXT,
                        undone_at TEXT,
                        undone_by_event_id INTEGER)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_tg_chat_events_chat '
                  'ON tg_chat_events(chat_id, kind, id)')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(56, 'tg_chat_events: journal of chat direction and enabled changes')")
        logger.info("Migration v56: tg_chat_events")

    # === v57: copilot reingest link (mcp-recording-live) - a repeat reingest returns the same row ===
    if current_version < 57:
        c.execute("PRAGMA table_info(copilot_sessions)")
        cs_cols = [r[1] for r in c.fetchall()]
        for col, typ in (('reingest_transcription_id', 'INTEGER'), ('reingest_by', 'TEXT')):
            if cs_cols and col not in cs_cols:
                c.execute(f'ALTER TABLE copilot_sessions ADD COLUMN {col} {typ}')
        c.execute("INSERT OR IGNORE INTO schema_versions (version, description) VALUES "
                  "(57, 'copilot_sessions.reingest_transcription_id + reingest_by')")
        logger.info("Migration v57: copilot reingest link")

    conn.commit()
    conn.close()
