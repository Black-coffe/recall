# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Recall** — a local RAG archive of calls. Flask web app; the data stays on the user's
machine; external calls are optional: Anthropic API (polish/enrichment/co-pilot)
and Google Drive (weekly encrypted backup).

Capabilities:
- **Transcription**: faster-whisper backend (CTranslate2); on an RTX 3090, medium ≈ 80× realtime.
- **Recording**: server-side recorder (mic + system loopback) with live diarization and live segments over SSE.
- **Entities and graph**: pyannote for diarization; Claude extracts summary/key_points/action_items/entities.
- **Vector search + RAG chat**: e5-large embeddings (1024-dim), hybrid retrieval (RRF + recency + diversity), `/api/memory/ask` SSE stream.
- **YouTube** via pytubefix (anonymous, no sign-in), with yt-dlp as a download fallback.
- **VAD filter** (Silero) + **FTS5 search** + **Claude polish**.
- **Task dashboard** (action_items) + directions (categories), bulk labelling and k-NN auto-suggest.
- **Document ingestion**: PDF/DOCX/PPTX/XLSX/CSV/MD/images → text into RAG (OCR via Tesseract).
- **Telegram ingestion**: listens to a real Telegram ACCOUNT (MTProto/Telethon, NOT a bot) — text/voice/audio/photo/video/documents from selected chats → the same RAG pipeline. A separate process `telegram_listener.py`, managed by `app.py`; control API + chat selection in the UI; history backfill.
- **Live call co-pilot** (`app/services/copilot/`): while recording, a local model (Qwen-14B via Ollama, $0 API) runs a topic state machine on embeddings, dispatches RAG searches and catches contradictions/questions/facts → an operator widget with cards and actions. Claude verifies only what is flagged important (local-first cascade), with a budget and a safety sweep. Gated by `COPILOT_ENABLED`; everything degrades gracefully (no Ollama/key → whisper-only still works).
- **Commitments** (`app/services/commitments.py`): task deadlines are resolved from the raw phrase ("tomorrow", "by the end of the week", "Q3 2026") into an ISO date relative to the meeting date + a precision class; owners are linked to the graph; duplicates/stale items are flagged.
- **Two-layer scope** (`app/services/scope.py`): directions by Telegram chat + a project/person slice as the union of the `meeting_entities` graph, text mentions and the same-named direction — end to end in `/api/memory/ask`, MCP `ask_archive(project=…)` and the co-pilot.
- **MCP server** (`mcp_server.py`): 41 read-only tools (17 standalone direct-DB + 24 proxied to `app.py`). Deliberately read-first: no write/action tools are added.
- **Backup**: weekly encrypted restic archive on Google Drive (`scripts/backup.ps1`).

## Development Commands

```bash
# Run application (Windows) — the ONLY entry point.
# app.py starts the Telegram listener itself as a child process (if keys + session exist).
.venv/Scripts/python.exe app.py

# --- Telegram ingestion ---
# 1. api_id/api_hash keys from https://my.telegram.org → into .env:
#      TELEGRAM_API_ID=... / TELEGRAM_API_HASH=...
# 2. One-time interactive login (creates telegram.session):
.venv/Scripts/python.exe telegram_login.py
# 3. From then on app.py starts the listener automatically. Chat selection is in the UI ("Telegram" tab).
# The listener can also be run by hand (CLI): list / enable <id> / disable <id> / status
.venv/Scripts/python.exe telegram_listener.py list

# Install dependencies
.venv/Scripts/python.exe -m pip install -r requirements.txt

# Update pytubefix (for YouTube download issues)
.venv/Scripts/python.exe -m pip install -U pytubefix

# Check GPU/CUDA availability
.venv/Scripts/python.exe -c "import torch; print('CUDA:', torch.cuda.is_available())"

# Install PyTorch with CUDA 12.1
.venv/Scripts/python.exe -m pip install torch==2.5.1+cu121 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# Run tests (offline, no GPU needed). There must be NO failures.
.venv/Scripts/python.exe -m pytest -q -m "not slow"
# pytest does NOT collect the root scripts (pytest.ini: testpaths = tests) — run them separately:
.venv/Scripts/python.exe test_categories.py
.venv/Scripts/python.exe test_copilot_dispatcher.py
.venv/Scripts/python.exe test_recording_devices.py
.venv/Scripts/python.exe test_video_capture.py

# Offline passes over the data (all idempotent, all with --dry-run):
.venv/Scripts/python.exe -m app.services.commitments backfill --dry-run   # deadlines → ISO dates
.venv/Scripts/python.exe -m app.services.commitments digest               # Monday digest
.venv/Scripts/python.exe -m app.services.scope apply-chats --dry-run      # TG chats → directions
.venv/Scripts/python.exe -m app.services.scope label-rest --claude --dry-run  # auto-label the rest
```

## Architecture

### Application Structure

The application uses a **modular Flask blueprint architecture**. The entry point `app.py` (~1130 lines) boots the app, owns global singletons, and registers 14 blueprints; all API endpoints live in `app/blueprints/*` with business logic in `app/services/*`. See `memory/map/` for the full module map.

### Core Components

1. **Main Application** (`app.py`)
   - Flask boot: global singletons (`app/state.py`), blueprint registration, a few utility endpoints — NOT all API endpoints (those live in `app/blueprints/*`)
   - Background task management via `ThreadPoolExecutor` (max_workers=2)
   - Progress tracking with `OrderedDict` collections
   - Process logging system for detailed operation tracking
   - Memory management with automatic cleanup thresholds

2. **Whisper Manager** (`whisper_manager_new.py`)
   - Model loading with automatic caching in `~/.cache/whisper`
   - Audio chunking for files >10 minutes (5-min chunks with 5-sec overlap)
   - GPU/CPU device management with fallback
   - Progress callbacks for real-time updates

3. **Modular Architecture** (`app/` directory)
   - 14 blueprints in `app/blueprints/`, registered in `app.py` (direct init, NO factory pattern)
   - Service layer `app/services/*` (database, transcription, RAG, copilot, recording, …)
   - Global singletons via `app/state.py` (avoids circular imports)
   - Custom exceptions (`app/core/exceptions.py`) + rotating logger (`app/core/logger.py`)
   - Module map: `memory/map/` (overview/root/blueprints/services/copilot/recording/frontend)

4. **Configuration** (`config.py`)
   - Environment-based configs: Development, Production, Testing
   - Centralized settings for paths, limits, models, languages
   - Security settings and rate limiting

5. **Frontend** (client-routed SPA — "Recall")
   - `templates/shell.html` — SPA shell (sidebar/topbar, `#view` mount point)
   - `static/js/recall/*` — namespace/router/shell/api/ui/util/cmdk + `views/*.js` (15 screens). SSE via fetch+reader, NOT EventSource.
   - `static/css/recall.css` — editorial-catalog design, light theme only, `rc-*` class prefix
   - Service worker `static/sw.js`. Details: `memory/map/frontend.md`

### Background Task System

- YouTube downloads and transcription run in background threads
- Progress tracking via global `OrderedDict` collections
- Automatic cleanup when reaching thresholds (20% of max capacity)
- Memory limits: `MAX_DOWNLOAD_HISTORY = 100`; process logs capped at 50 entries
- ThreadPoolExecutor: max_workers = 2

### YouTube Integration

YouTube functionality uses **pytubefix** with extensive error handling; **yt-dlp is a download fallback only**
(some videos are served only to clients with a PO token). Production code — `_download_youtube_core` (`app/blueprints/youtube.py` →
`app/services/youtube_pytubefix.py`).

- **Download Strategy**: Full download first, then post-process trimming
- **Stream Selection**: `yt.streams.filter(only_audio=True).order_by('abr').desc().first()`
- **There is NO sign-in (and no cookies.txt)**: `YouTube(...)` is called without
  auth parameters; the default pytubefix client is `ANDROID_VR`, chosen precisely
  because it needs no sign-in. `YouTube.__init__` has no cookies parameter at all.
  If authorized access is ever needed, the supported paths are
  `use_oauth=True` + `token_file` or `use_po_token` with a verifier. Not cookies.
- **Post-download Trimming**: pydub AudioSegment
- **MP3 Conversion**: FFmpeg subprocess (libmp3lame, 320/256/192/128 kbps)
- **Backend choice**: `WHISPER_BACKEND=faster` (default) — faster-whisper; `=openai` — fallback
- **Polish**: optional Claude API postprocessing — `ANTHROPIC_API_KEY` in `.env`

### Database Schema

```sql
-- transcriptions table
id INTEGER PRIMARY KEY AUTOINCREMENT,
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
source_type TEXT,  -- 'file' or 'youtube'
source_name TEXT,
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
segments TEXT  -- JSON array

-- audio_downloads table
id INTEGER PRIMARY KEY AUTOINCREMENT,
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
youtube_url TEXT,
youtube_id TEXT UNIQUE,
title TEXT,
author TEXT,
duration INTEGER,
thumbnail_url TEXT,
file_path TEXT,
file_size INTEGER,
audio_quality TEXT,
audio_format TEXT,
download_time REAL,
view_count INTEGER,
like_count INTEGER,
description TEXT,
upload_date TEXT,
tags TEXT  -- JSON array
```

## Critical Implementation Details

### Memory Management
- Download progress: `ThreadSafeProgressStore`, max 100 (`MAX_DOWNLOAD_HISTORY`, cleanup at 20%)
- Process logs: `ThreadSafeProcessLogs` / global `process_logs` (max 50 entries)
- ThreadPoolExecutor: max_workers = 2

### Audio Processing Pipeline
- **Chunking threshold**: 10 minutes (600 seconds)
- **Chunk size**: 5 minutes (300 seconds)
- **Overlap**: 5 seconds between chunks
- **Audio format**: MP3 via FFmpeg conversion
- **Quality options**: 128/192/256/320 kbps

### Security Measures
- Binds `127.0.0.1` by default; a network bind is a deliberate opt-in
  (`RECALL_BIND_ALL=1` / `FLASK_HOST=0.0.0.0`) and requires an explicit `SECRET_KEY`
- Auth gate on `/api/*` (`app/core/auth.py`): remote requests require `RECALL_API_KEY`
- Root allowlist for `import-folder` (`RECALL_IMPORT_ROOTS`, deny-by-default)
- Path traversal protection: `os.path.abspath(path).startswith(base_path)`
- SQL injection prevention: all queries use parameterized statements
- File validation: `secure_filename()` + ALLOWED_EXTENSIONS check
- Input validation: bounded pagination (MAX_PAGE=1000)
- File size limit: `MAX_CONTENT_LENGTH` (up to 20GB, for video)

Full security model: `docs/SECURITY.md`.

### Error Handling
- **Rule: no bare `except` without a log.** Every `except Exception` must either
  log (`logger.debug`/`.warning`/`.exception`, even for intentional best-effort
  swallowing — one line explaining why it's safe to ignore) or be removed so the
  exception propagates. Silent `except: pass` turns real bugs into "it just
  doesn't work" reports with nothing to diagnose from.
- **Never leak `str(exc)` to the client** on an unexpected exception — internal
  messages can contain file paths, SQL, stack details. Catch-all 500s return a
  generic message; unhandled exceptions fall through to the app-wide handler in
  `app/core/error_handlers.py`, which logs the full traceback (correlated via
  request-id) and returns a generic body.
- Explicit, expected 4xx `jsonify(...)` responses are unaffected.

### Frontend JavaScript Patterns

- Request throttling with `isUpdatingLogs` flag
- Interval management to prevent duplicate timers
- Safe DOM updates with null checks
- `AbortSignal.timeout()` for fetch requests (5 second timeout)
- YouTube IFrame API integration for trimming preview

## API Endpoints

### Core Operations
- `POST /api/transcribe` - Main transcription endpoint
- `GET /api/transcription/progress/<id>` - Real-time transcription progress
- `POST /api/youtube/info` - Get YouTube video metadata
- `POST /api/youtube/download` - Download YouTube audio with optional trimming
- `GET /api/youtube/progress/<id>` - YouTube download progress
- `GET /api/process/logs/<process_id>` - Get detailed process logs

### Audio Library
- `POST /api/audio/download` - Download audio without transcription
- `GET /api/audio/downloads` - List downloads (pagination, search, sorting)
- `DELETE /api/audio/downloads/<id>` - Delete audio file
- `POST /api/audio/check-duplicate` - Check if YouTube video already downloaded
- `POST /api/audio/open-explorer/<id>` - Open file location (Windows)
- `POST /api/audio/play/<id>` - Play with default application

### History & Export
- `GET /api/history` - Paginated history with filters
- `DELETE /api/history/<id>` - Delete single transcription
- `POST /api/history/bulk_delete` - Delete multiple entries
- `POST /api/history/bulk_export` - Export multiple transcriptions
- `GET /api/export/<format>` - Export single (TXT/SRT/JSON)

### System
- `GET /api/models` - List available Whisper models
- `GET /api/system_info` - System capabilities (GPU, CPU threads)
- `GET /api/system_stats` - Real-time resource usage
- `POST /api/download_model` - Download Whisper model

## Common Issues & Solutions

### YouTube Download Failures
1. **Stream not found**: pytubefix may fail to find an audio-only stream — update pytubefix.
2. **403 Forbidden / "Sign in to confirm"**: cookies will NOT help (nothing reads them).
   Blocking is often per video, not per machine: such videos are served only to clients
   with a PO token. Metadata is covered by cycling clients (needs Node.js in PATH), the
   download by the yt-dlp fallback; if yt-dlp fails too, `pip install -U yt-dlp`. The next
   rung is `use_oauth=True` + `token_file`, not implemented.
3. **Update pytubefix**: `.venv/Scripts/python.exe -m pip install -U pytubefix`
4. **Age-restricted / private**: anonymous clients cannot fetch them.
5. **FFmpeg missing**: Check FFmpeg is in PATH or project directory.

### GPU Not Detected
1. Verify CUDA: `.venv/Scripts/python.exe -c "import torch; print(torch.cuda.is_available())"`
2. Force CPU mode: Set environment variable `FORCE_CPU=true`
3. Reinstall PyTorch with correct CUDA version (see commands above)

### Frontend Errors
- **ERR_INSUFFICIENT_RESOURCES**: Check for infinite loops in `trackDownloadProgress`
- **Process logs not updating**: Verify `processLogInterval` is properly managed
- **Null reference errors**: Elements may not exist in DOM, safe checks added
- **YouTube player not loading**: Check YouTube IFrame API is accessible

### Unicode/Encoding (Windows)
- Use logger instead of print() throughout backend
- Database uses TEXT columns with UTF-8 encoding
- Set console encoding if needed: `chcp 65001`

## Development Workflow

### Making Changes
1. Frontend changes auto-reload in browser
2. Backend changes trigger Flask auto-reload in debug mode
3. Clear browser cache if CSS/JS changes don't appear (`Ctrl+Shift+R`)

### Database Operations
- Database auto-creates on first run
- Located at `whisper_history.db` in project root
- Backup before schema changes: `copy whisper_history.db whisper_history_backup.db`

### Adding New Features
1. Update progress tracking collections if adding background tasks
2. Add process logging for user-visible operations
3. Implement proper cleanup in memory management
4. Test with both GPU and CPU modes

## External Dependencies

**Required:**
- Python 3.8+ (3.12 tested and recommended)
- FFmpeg (must be in PATH or project directory)
- 8GB+ RAM for medium/large models
- Windows 10/11 (primary platform)

**Optional:**
- NVIDIA GPU with CUDA 12.1 for acceleration
- Tesseract OCR (for images and scans in document ingestion)
- Ollama + Qwen-14B (for the co-pilot; without it the co-pilot is simply off)

## Performance Benchmarks

### RTX 3090

**faster-whisper** (default, CTranslate2 + VAD) — `medium` ≈ **80× realtime**.
Actual speed depends on the model, the language and the audio length.

**openai-whisper** (legacy / fallback) — tens of times slower:
tiny ~32×, base ~16×, small ~6×, medium ~2×, large ~1×.

### Audio Processing Limits
- **Max upload size**: `MAX_CONTENT_LENGTH` — up to 20GB (video)
- **Max YouTube duration**: 3 hours
- **Chunking threshold**: 10 minutes
- **Concurrent operations**: 2 (ThreadPoolExecutor limit)
