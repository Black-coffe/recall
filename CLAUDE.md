# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Recall** — локальний RAG-архів дзвінків. Flask web app, дані лежать на машині
користувача, зовнішні виклики опційні: Anthropic API (polish/enrichment/копілот)
і Google Drive (щотижневий шифрований backup).

Можливості:
- **Транскрипція**: faster-whisper backend (CTranslate2), на RTX 3090 medium ≈ 80× realtime.
- **Запис**: серверний recorder (mic + system loopback) з live-діаризацією і live-сегментами через SSE.
- **Сутності та граф**: pyannote для діаризації, Claude витягує summary/key_points/action_items/entities.
- **Vector search + RAG-чат**: e5-large embeddings (1024-dim), гібридний retrieval (RRF + recency + diversity), `/api/memory/ask` SSE-стрім.
- **YouTube** через pytubefix (анонімно, без авторизації).
- **VAD filter** (Silero) + **FTS5 search** + **Claude polish**.
- **Дашборд задач** (action_items) + напрямки (категорії), bulk-розмітка і k-NN auto-suggest.
- **Document ingestion**: PDF/DOCX/PPTX/XLSX/CSV/MD/зображення → текст у RAG (OCR через Tesseract).
- **Telegram ingestion**: слухання реального TG-АКАУНТА (MTProto/Telethon, НЕ бот) — text/voice/audio/photo/video/документи з обраних чатів → той самий RAG-пайплайн. Окремий процес `telegram_listener.py`, керований з `app.py`; control-API + UI-вибір чатів; backfill історії.
- **Живий ко-пілот дзвінка** (`app/services/copilot/`): під час запису локальна модель (Qwen-14B через Ollama, $0 API) веде топік-стейт-машину на ембеддингах, диспетчеризує RAG-пошук і ловить протиріччя/питання/факти → віджет оператора з картками й діями. Claude верифікує лише позначене важливим (local-first cascade), з бюджетом і safety-sweep. Гейтиться `COPILOT_ENABLED`; усе деградує (нема Ollama/ключа → whisper-only працює).
- **Зобовʼязання** (`app/services/commitments.py`): дедлайни задач розгортаються з сирої фрази («завтра», «до кінця тижня», «Q3 2026») в ISO-дату відносно дати зустрічі + клас точності; власники звʼязані з графом; дублі/протухле позначені.
- **Двошаровий скоуп** (`app/services/scope.py`): напрямки за Telegram-чатами + зріз за проєктом/людиною як обʼєднання графа `meeting_entities`, текстової згадки і однойменного напрямку — наскрізно в `/api/memory/ask`, MCP `ask_archive(project=…)` і копілоті.
- **MCP-сервер** (`mcp_server.py`): 30 read-only тулзів (14 автономних direct-DB + 16 проксі на `app.py`). Свідомо read-first: write/action-тулзи не додаються.
- **Backup**: щотижневий шифрований restic-архів на Google Drive (`scripts/backup.ps1`).

## Development Commands

```bash
# Run application (Windows) — ЄДИНА точка запуску.
# app.py сам піднімає слухача Telegram як дочірній процес (якщо є ключі + сесія).
.venv/Scripts/python.exe app.py

# --- Telegram ingestion ---
# 1. Ключі api_id/api_hash з https://my.telegram.org → у .env:
#      TELEGRAM_API_ID=... / TELEGRAM_API_HASH=...
# 2. Одноразовий інтерактивний логін (створює telegram.session):
.venv/Scripts/python.exe telegram_login.py
# 3. Далі app.py автоматично запускає слухача. Вибір чатів — у UI (таб «Telegram»).
# Слухача можна гонять і вручну (CLI): list / enable <id> / disable <id> / status
.venv/Scripts/python.exe telegram_listener.py list

# Install dependencies
.venv/Scripts/python.exe -m pip install -r requirements.txt

# Update pytubefix (for YouTube download issues)
.venv/Scripts/python.exe -m pip install -U pytubefix

# Check GPU/CUDA availability
.venv/Scripts/python.exe -c "import torch; print('CUDA:', torch.cuda.is_available())"

# Install PyTorch with CUDA 12.1
.venv/Scripts/python.exe -m pip install torch==2.5.1+cu121 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# Run tests (offline, no GPU needed). Падінь бути НЕ має.
.venv/Scripts/python.exe -m pytest -q -m "not slow"
# Кореневі скрипти pytest НЕ збирає (pytest.ini: testpaths = tests) — гонити окремо:
.venv/Scripts/python.exe test_categories.py
.venv/Scripts/python.exe test_copilot_dispatcher.py
.venv/Scripts/python.exe test_recording_devices.py
.venv/Scripts/python.exe test_video_capture.py

# Офлайн-проходи по даних (усі ідемпотентні, усі з --dry-run):
.venv/Scripts/python.exe -m app.services.commitments backfill --dry-run   # дедлайни → ISO-дати
.venv/Scripts/python.exe -m app.services.commitments digest               # понеділковий звід
.venv/Scripts/python.exe -m app.services.scope apply-chats --dry-run      # TG-чати → напрямки
.venv/Scripts/python.exe -m app.services.scope label-rest --claude --dry-run  # автопозначення решти
```

## Architecture

### Application Structure

The application uses a **modular Flask blueprint architecture**. The entry point `app.py` (~1130 lines) boots the app, owns global singletons, and registers 14 blueprints; all API endpoints live in `app/blueprints/*` with business logic in `app/services/*`. See `docs/map/` for the full module map.

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
   - Module map: `docs/map/` (overview/root/blueprints/services/copilot/recording/frontend)

4. **Configuration** (`config.py`)
   - Environment-based configs: Development, Production, Testing
   - Centralized settings for paths, limits, models, languages
   - Security settings and rate limiting

5. **Frontend** (client-routed SPA — "Recall")
   - `templates/shell.html` — SPA shell (sidebar/topbar, `#view` mount point)
   - `static/js/recall/*` — namespace/router/shell/api/ui/util/cmdk + `views/*.js` (15 screens). SSE via fetch+reader, NOT EventSource.
   - `static/css/recall.css` — editorial-catalog design, light theme only, `rc-*` class prefix
   - Service worker `static/sw.js`. Details: `docs/map/frontend.md`

### Background Task System

- YouTube downloads and transcription run in background threads
- Progress tracking via global `OrderedDict` collections
- Automatic cleanup when reaching thresholds (20% of max capacity)
- Memory limits: `MAX_DOWNLOAD_HISTORY = 100`; process logs capped at 50 entries
- ThreadPoolExecutor: max_workers = 2

### YouTube Integration

YouTube functionality uses **pytubefix** with extensive error handling (NOT yt-dlp).
Production code — `_download_youtube_core` (`app/blueprints/youtube.py` →
`app/services/youtube_pytubefix.py`).

- **Download Strategy**: Full download first, then post-process trimming
- **Stream Selection**: `yt.streams.filter(only_audio=True).order_by('abr').desc().first()`
- **Авторизації НЕМА (і cookies.txt не існує)**: `YouTube(...)` викликається без
  auth-параметрів, дефолтний клієнт pytubefix — `ANDROID_VR`, який саме тому й
  обраний, що не потребує входу. У `YouTube.__init__` параметра для cookies нема
  взагалі. Якщо колись знадобиться авторизований доступ, підтримувані шляхи —
  `use_oauth=True` + `token_file` або `use_po_token` з верифікатором. Не cookies.
- **Post-download Trimming**: pydub AudioSegment
- **MP3 Conversion**: FFmpeg subprocess (libmp3lame, 320/256/192/128 kbps)
- **Backend choice**: `WHISPER_BACKEND=faster` (default) — faster-whisper; `=openai` — fallback
- **Polish**: optional Claude API postprocessing — `ANTHROPIC_API_KEY` у `.env`

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
- Bind `127.0.0.1` за замовчуванням; мережевий bind — свідомий opt-in
  (`RECALL_BIND_ALL=1` / `FLASK_HOST=0.0.0.0`) і вимагає явного `SECRET_KEY`
- Auth-гейт на `/api/*` (`app/core/auth.py`): віддалені запити вимагають `RECALL_API_KEY`
- Allowlist коренів для `import-folder` (`RECALL_IMPORT_ROOTS`, deny-by-default)
- Path traversal protection: `os.path.abspath(path).startswith(base_path)`
- SQL injection prevention: all queries use parameterized statements
- File validation: `secure_filename()` + ALLOWED_EXTENSIONS check
- Input validation: bounded pagination (MAX_PAGE=1000)
- File size limit: `MAX_CONTENT_LENGTH` (up to 20GB, for video)

Повна модель безпеки — `docs/SECURITY.md`.

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
1. **Stream not found**: pytubefix може не знайти audio-only stream — оновити pytubefix.
2. **403 Forbidden / "Sign in to confirm"**: cookies тут НЕ допоможуть (їх ніхто не
   читає). Спершу оновити pytubefix — блокування зазвичай означає, що клієнт
   `ANDROID_VR` застарів. Якщо не допомогло, потрібен `use_oauth=True` +
   `token_file` — це правка `_download_youtube_core`, зараз не реалізовано.
3. **Update pytubefix**: `.venv/Scripts/python.exe -m pip install -U pytubefix`
4. **Age-restricted / private**: анонімний клієнт їх не візьме.
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
- Tesseract OCR (для зображень і сканів у document ingestion)
- Ollama + Qwen-14B (для копілота; без нього копілот просто вимкнений)

## Performance Benchmarks

### RTX 3090

**faster-whisper** (default, CTranslate2 + VAD) — `medium` ≈ **80× realtime**.
Фактична швидкість залежить від моделі, мови та довжини аудіо.

**openai-whisper** (legacy / fallback) — у десятки разів повільніше:
tiny ~32×, base ~16×, small ~6×, medium ~2×, large ~1×.

### Audio Processing Limits
- **Max upload size**: `MAX_CONTENT_LENGTH` — up to 20GB (video)
- **Max YouTube duration**: 3 hours
- **Chunking threshold**: 10 minutes
- **Concurrent operations**: 2 (ThreadPoolExecutor limit)
