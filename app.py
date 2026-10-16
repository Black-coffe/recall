# File: WhisperUI/app.py
# Path: WhisperUI/app.py
# Description: Main Flask server with YouTube integration

from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context, g, has_request_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import os
import sys
import json
from pathlib import Path
import time
from datetime import datetime
import sqlite3
import threading
import queue
import subprocess
import logging
from logging.handlers import RotatingFileHandler
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from contextlib import contextmanager
import atexit
from config import get_config
from app.core.i18n import tr
# Load .env as early as possible — before services read env vars
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from whisper_manager_new import ModernWhisperManager
from app.services.sse_broker import broker as sse_broker
from app.services.job_queue import JobQueue
from app.services.system_monitor import monitor as system_monitor
from app.services import text_polishing
from app.services.file_cleanup import FileCleanupService
from app.services.metrics import metrics
from app.utils.fts import sanitize_fts_query as _sanitize_fts_query
from app.utils.youtube_id import extract_youtube_id
from app.utils.proc import NO_WINDOW, silence_pydub_console_windows
from app.utils.files import (
    allowed_file,
    is_video_file,
    format_srt_timestamp as format_timestamp,
    ALLOWED_EXTENSIONS,
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_VIDEO_EXTENSIONS,
)
from app.db.connection import get_db_connection as _get_db_connection_raw
from app.db.migrations import init_database as _init_database_raw
from app.utils.audio import (
    trim_audio_file as _trim_audio_file_raw,
    extract_audio_from_video as _extract_audio_from_video_raw,
)
from app.services.youtube_pytubefix import download_youtube_audio as _download_youtube_audio_service
from app import state as _state
from app.core import settings as _settings
from app.blueprints.system import system_bp as _system_bp
from app.blueprints.youtube import youtube_bp as _youtube_bp
from app.blueprints.audio_library import audio_bp as _audio_bp
from app.blueprints.transcription import transcription_bp as _transcription_bp
from app.blueprints.events import events_bp as _events_bp
from app.blueprints.recording import recording_bp as _recording_bp
from app.blueprints.speakers import speakers_bp as _speakers_bp
from app.blueprints.bookmarks import bookmarks_bp as _bookmarks_bp
from app.blueprints.memory import memory_bp as _memory_bp
from app.blueprints.documents import documents_bp as _documents_bp
from app.blueprints.telegram import telegram_bp as _telegram_bp
from app.blueprints.research import research_bp as _research_bp
from app.blueprints.copilot import copilot_bp as _copilot_bp
from app.blueprints.settings_api import settings_bp as _settings_bp
from app.blueprints.comments import comments_bp as _comments_bp


def get_db_connection():
    """Wrapper that supplies the DB path from app.config (Phase 5.2)."""
    return _get_db_connection_raw(app.config['DATABASE'])


def init_database():
    """Wrapper that uses app.config['DATABASE'] (Phase 5.2)."""
    _init_database_raw(app.config['DATABASE'])
import torch
from pydub import AudioSegment


class ThreadSafeProgressStore:
    """Thread-safe store for operation progress.

    On set/cleanup it publishes events to the SSE broker (if available) — this
    lets the frontend subscribe via /api/events/<id> instead of polling.
    """

    def __init__(self, max_size=100, sse_event_name="progress"):
        self._lock = threading.Lock()
        self._data = OrderedDict()
        self._max_size = max_size
        self._sse_event_name = sse_event_name

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._cleanup()
        # Publish OUTSIDE the lock
        try:
            sse_broker.publish(key, self._sse_event_name, value)
            # If the status is terminal — forward a 'complete'/'error' event to close the SSE.
            status = value.get("status") if isinstance(value, dict) else None
            if status == "completed":
                sse_broker.publish(key, "complete", value)
            elif status == "error":
                sse_broker.publish(key, "error", value)
        except Exception as e:
            logger.debug(f"SSE publish failed: {e}")

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def __contains__(self, key):
        with self._lock:
            return key in self._data

    def __len__(self):
        with self._lock:
            return len(self._data)

    def _cleanup(self):
        """Removes only finished (completed/error) entries on overflow.

        T2.4 (Wave 2): previously did a blind ``popitem(last=False)`` —
        evicting the OLDEST entry regardless of status, i.e. it could drop
        an operation still running (the user lost the progress bar). Now — as in
        ``ThreadSafeProcessLogs._cleanup()`` next door: we look for the oldest
        terminal (``completed``/``error``) entries and clean only those. If
        the whole store is full of active operations beyond the limit — we do not
        remove anything (temporarily exceeding ``max_size``), only log a
        warning; this is not a magic limit that matters more than losing progress.
        """
        if len(self._data) <= self._max_size:
            return
        to_remove = []
        for key, value in self._data.items():
            status = value.get('status') if isinstance(value, dict) else None
            if status in ('completed', 'error'):
                to_remove.append(key)
            if len(self._data) - len(to_remove) <= self._max_size:
                break
        if not to_remove:
            logger.warning(
                "ThreadSafeProgressStore: limit %d exceeded (%d entries), "
                "all active — nothing evicted.",
                self._max_size, len(self._data),
            )
            return
        for key in to_remove:
            del self._data[key]

    def cleanup_batch(self, count):
        """Removes the count oldest entries"""
        with self._lock:
            for _ in range(min(count, len(self._data))):
                self._data.popitem(last=False)


class ThreadSafeProcessLogs:
    """Thread-safe store for process logs"""

    def __init__(self, max_size=50):
        self._lock = threading.Lock()
        self._data = OrderedDict()
        self._max_size = max_size

    def append(self, process_id, entry):
        with self._lock:
            if process_id not in self._data:
                self._data[process_id] = []
            self._data[process_id].append(entry)
            self._cleanup()
        try:
            sse_broker.publish(process_id, "log", entry)
            status = entry.get("status") if isinstance(entry, dict) else None
            if status == "completed":
                sse_broker.publish(process_id, "complete", entry)
            elif status == "error":
                sse_broker.publish(process_id, "error", entry)
        except Exception as e:
            logger.debug(f"SSE publish failed: {e}")

    def get(self, process_id, last_n=None):
        with self._lock:
            logs = self._data.get(process_id, [])
            if last_n:
                return logs[-last_n:]
            return list(logs)

    def __contains__(self, process_id):
        with self._lock:
            return process_id in self._data

    def _cleanup(self):
        """Removes only finished processes on overflow"""
        if len(self._data) <= self._max_size:
            return
        to_remove = []
        for pid, logs in self._data.items():
            if logs and logs[-1].get('status') in ('completed', 'error'):
                to_remove.append(pid)
            if len(self._data) - len(to_remove) <= self._max_size:
                break
        for pid in to_remove:
            del self._data[pid]


# Logging setup with RotatingFileHandler
cfg = get_config()

# T8.2 (Wave 4): request-id/correlation-id in every log record. The root logger
# (basicConfig below) is shared by app.py + all blueprints/services (they all
# do logging.getLogger(__name__), which propagates to root) — so ONE
# Filter on the handler is enough, rather than a LoggerAdapter on every logger call across the
# codebase. g.request_id is set in _assign_request_id (the before_request below,
# registered FIRST — earlier than the auth gate app/core/auth.py), so even a 401 from the
# auth gate gets into the log with the same id that goes to the client in X-Request-Id.
class _RequestIdLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, 'request_id', '-') if has_request_context() else '-'
        return True


# config-registry-profiles S2: RECALL_LOG_FORMAT=json switches both
# handlers (file+stream) to single-line JSON (app/core/logger.JsonFormatter);
# the default 'text' keeps the previous human-readable format unchanged.
if _settings.env('RECALL_LOG_FORMAT').strip().lower() == 'json':
    from app.core.logger import JsonFormatter
    _log_formatter = JsonFormatter()
else:
    _log_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [req:%(request_id)s] - %(message)s'
    )

_log_handlers = []

# test-log-isolation-03: under pytest we do not open the live LOG_FILE on
# import (a full run pulls in app.py via test_endpoints_smoke, already at
# the collection stage) — the same "pytest" not in sys.modules as in
# telegram_listener.py/app/services/commitments.py. PYTEST_CURRENT_TEST does not
# fit here: pytest sets it only for the test execution phase, not for
# collection, when this module is already imported. The console handler stays
# under pytest too.
if "pytest" not in sys.modules:
    _file_handler = RotatingFileHandler(
        getattr(cfg, 'LOG_FILE', 'whisper_app.log'),
        maxBytes=getattr(cfg, 'LOG_MAX_SIZE', 10 * 1024 * 1024),  # 10MB
        backupCount=getattr(cfg, 'LOG_BACKUP_COUNT', 5),
        encoding='utf-8'
    )
    _file_handler.setFormatter(_log_formatter)
    _file_handler.addFilter(_RequestIdLogFilter())
    _log_handlers.append(_file_handler)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)
_stream_handler.addFilter(_RequestIdLogFilter())
_log_handlers.append(_stream_handler)

logging.basicConfig(
    level=getattr(logging, getattr(cfg, 'LOG_LEVEL', 'INFO'), logging.INFO),
    handlers=_log_handlers
)
logger = logging.getLogger(__name__)

# Quiet down noisy third-party loggers. At DEBUG level they inflated the log to
# ~10MB: HTTP clients log every request/byte (anthropic stream, HF downloads),
# and torchaudio/torio during diarization throws tracebacks "FFmpeg extension is not
# available" (non-fatal — there is a fallback). werkzeug logs EVERY HTTP request at INFO
# (per-request access spam + sometimes a traceback when emitting the access line) — at WARNING
# only real warnings/errors remain. Our app.* loggers are not touched.
for _noisy in ('anthropic', 'httpx', 'httpcore', 'urllib3', 'huggingface_hub',
               'filelock', 'matplotlib', 'werkzeug'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
for _quiet in ('torio', 'torchaudio'):
    logging.getLogger(_quiet).setLevel(logging.CRITICAL)

# A multi-process RotatingFileHandler on the shared whisper_ui.log (app + listener +
# stdio mcp_server) sometimes cannot roll over on Windows (WinError 32 — the file is
# held by another process) → logging printed "--- Logging error ---" + a traceback on
# every record. We neither crash the process nor spam: we silence the logging errors
# themselves (the rollover is quietly skipped). The root of the contention is further reduced via RECALL_LOG_TO_FILE.
logging.raiseExceptions = False

app = Flask(__name__)
# We take the port from cfg.PORT (env FLASK_PORT), not hardcoded: on a non-standard
# port a hard-coded 5050 in the origins list would silently cut off our own frontend.
CORS(app, origins=[f'http://localhost:{cfg.PORT}', f'http://127.0.0.1:{cfg.PORT}'])

# Configuration from config.py
app.config.from_object(cfg)

# T8.2 (Wave 4): generate a short request-id FIRST among app-level
# before_request hooks (Flask runs them in registration order) — registered
# here, BEFORE register_blueprint(...) and BEFORE auth.register(app) (Wave 1, T1.2) further
# down the file. Thanks to this g.request_id is available already when the auth gate
# decides to return 401 — its `logger.warning(...)` also goes through the root logger
# and picks up request_id via _RequestIdLogFilter above, although app/core/auth.py
# itself is not touched. Light (one uuid4 hex slice), overhead — microseconds.
@app.before_request
def _assign_request_id():
    g.request_id = uuid.uuid4().hex[:12]


@app.after_request
def _add_request_id_header(response):
    request_id = getattr(g, 'request_id', None)
    if request_id:
        response.headers['X-Request-Id'] = request_id
    return response

# Phase 17: Jinja template auto-reload. In non-debug Flask caches shell.html in
# memory → HTML edits are not visible until the process restarts. For a local single-user
# app it is cheap (an mtime check per render) and removes the need to restart on
# every UI template edit. Static files (CSS/JS) are read fresh anyway.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# T1.5 (Wave 1), refined by config-registry-fix S4 (finding 14): the
# SECRET_KEY check is now SINGLE and runs as a hard-fail in config.py — LAZILY,
# inside `_finalize_secret_key()`, which `get_config()` calls on the
# FIRST access (here — `cfg = get_config()` above in the file), NOT at the moment of
# `import config`. Earlier (T1.5) it really was module level;
# config-registry-profiles S3 moved the side effects into lazy initialization.
# See config.py near `_finalize_secret_key`/`_initialize_dynamic_config`
# for details. The duplication of the check itself is removed here (in app.py).

# Phase 6: rate limiting.
# One Limiter, always on. The limits are chosen so that a single user
# locally does not notice them (60/min for transcribe, i.e. 1 per second).
# Polish — strictly limited always (protects the Anthropic wallet from
# an accidental infinite loop in the frontend).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)
# Custom limits. Configured via env variables.
RATE_LIMIT_TRANSCRIBE = os.environ.get('RATE_LIMIT_TRANSCRIBE', '60/minute')
RATE_LIMIT_YOUTUBE = os.environ.get('RATE_LIMIT_YOUTUBE', '30/minute')
RATE_LIMIT_POLISH = os.environ.get('RATE_LIMIT_POLISH', '20/hour')
# Phase 9.10 hardening: prevents accumulation of session folders under
# script-driven abuse. Each session = ~10MB/min on disk, so the
# limit must be low.
RATE_LIMIT_RECORDING_START = os.environ.get('RATE_LIMIT_RECORDING_START', '10/minute')
logger.info(
    f"Rate limits: transcribe={RATE_LIMIT_TRANSCRIBE}, "
    f"youtube={RATE_LIMIT_YOUTUBE}, polish={RATE_LIMIT_POLISH}"
)


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "success": False,
        "error": tr("core.error.rate_limit", desc=e.description),
    }), 429

# Create the required folders
for folder in [cfg.UPLOAD_FOLDER, cfg.TRANSCRIPTS_FOLDER, getattr(cfg, 'MODELS_FOLDER', 'models'),
               cfg.YOUTUBE_FOLDER, getattr(cfg, 'DOCUMENTS_FOLDER', 'documents'),
               str(getattr(cfg, 'TELEGRAM_MEDIA_DIR', 'telegram_media'))]:
    os.makedirs(folder, exist_ok=True)

# ALLOWED_*_EXTENSIONS are now imported from app.utils.files (Phase 5.1)


# Handler for the too-large-file error
@app.errorhandler(413)
def request_entity_too_large(error):
    max_size_gb = app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024 * 1024)
    return jsonify({
        'success': False,
        'error': tr("core.error.file_too_large", max_gb=f"{max_size_gb:.1f}")
    }), 413


# Initialize the Whisper manager
whisper_manager = ModernWhisperManager()

# Thread-safe stores for progress
MAX_DOWNLOAD_HISTORY = 100
download_progress = ThreadSafeProgressStore(max_size=MAX_DOWNLOAD_HISTORY)

# ThreadPoolExecutors for background tasks.
#
# T2.5 (Wave 2): previously ALL background jobs (youtube/audio download, doc_import,
# enrichment/enrichment_backfill, tg_embed/tg_transcribe, video_analysis,
# recording_finalize) went through ONE ThreadPoolExecutor(max_workers=2).
# A long batch (e.g. enrichment_backfill over the whole archive — sequential
# Claude API calls on hundreds of transcriptions, can run for minutes) could occupy
# both workers for a long time → the latency-critical recording_finalize (the user has just
# pressed "stop" and is waiting for the finished mp3 + registration in the library) waited in
# the queue behind the batch task — a "hang" with no visible cause for the user.
#
# Decision: two separate pools instead of one shared (simpler than a priority
# queue on top of one executor — no need to touch the submit/cancel semantics).
# JobQueue (app/services/job_queue.py) routes a job by kind: 'recording_finalize'
# → live_executor, everything else → executor (batch). The job registry/persist/
# get_job_status stays SINGLE for both pools.
#
# Sizes (both overridable via env, defaults are conservative):
#  - LIVE_EXECUTOR_WORKERS=1: recording_finalize — ffmpeg conversion
#    PCM→WAV→MP3, purely CPU/disk, does not use the GPU. In the typical scenario at most
#    one recording session is active at a time (one user, one recorder),
#    so 1 worker is enough, and it is always free — it does not wait behind a batch.
#  - BATCH_EXECUTOR_WORKERS=2: same as in the previous single pool. Here and below
#    go tg_transcribe / video_analysis(local) — they MAY load the GPU
#    (faster-whisper), so we deliberately do NOT enlarge this pool — total GPU
#    contention stays the same as it was (at most 2 parallel GPU tasks
#    from this pool; live_executor does not touch the GPU, so in the worst case + 0 to VRAM).
#  Total: 1 + 2 = 3 threads instead of 2 — the increase is only at the expense of the pool that is non-critical
#  for VRAM (ffmpeg).
LIVE_EXECUTOR_WORKERS = int(os.environ.get('LIVE_EXECUTOR_WORKERS', '1'))
BATCH_EXECUTOR_WORKERS = int(os.environ.get('BATCH_EXECUTOR_WORKERS', '2'))
executor = ThreadPoolExecutor(max_workers=BATCH_EXECUTOR_WORKERS, thread_name_prefix="batch")
live_executor = ThreadPoolExecutor(max_workers=LIVE_EXECUTOR_WORKERS, thread_name_prefix="live")

# Job queue (Phase 2, split into pools in T2.5): a wrapper over the executors for
# tracking states and cancel. 'recording_finalize' is the only kind that
# currently needs the live pool; the other kinds (see the comment above) stay on the
# batch pool with no behaviour change.
job_queue = JobQueue(
    executor=executor,
    live_executor=live_executor,
    live_kinds=frozenset({'recording_finalize'}),
)

# Thread-safe store for process logs
MAX_PROCESS_LOGS = 50
process_logs = ThreadSafeProcessLogs(max_size=MAX_PROCESS_LOGS)


def trim_audio_file(input_path, output_path, start_time, end_time, process_id=None):
    """Wrapper over app.utils.audio.trim_audio_file (Phase 5.4).
    Preserves the previous API: process_id as the 5th argument.
    """
    add_log = None
    if process_id:
        def add_log(stage, message, progress=None, status="processing"):
            add_process_log(process_id, stage, message, progress, status)
    return _trim_audio_file_raw(input_path, output_path, start_time, end_time, add_log=add_log)


def add_process_log(process_id, stage, message, progress=None, status="processing"):
    """
    Adds an entry to the process log (thread-safe)
    """
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "stage": stage,
        "message": message,
        "progress": progress,
        "status": status,
        "elapsed": time.time() if progress == 0 else None
    }

    process_logs.append(process_id, log_entry)
    logger.info(f"[{process_id}] [{stage}] {message}")


def get_process_logs(process_id, last_n=None):
    """Get the process logs (thread-safe)"""
    return process_logs.get(process_id, last_n)


# init_database is now in app.db.migrations (Phase 5.2)


# _sanitize_fts_query is now imported from app.utils.fts (Phase 6.4 — for testing)


# allowed_file, is_video_file are now in app.utils.files (Phase 5.1)


def extract_audio_from_video(video_path, audio_path, process_id=None):
    """Wrapper over app.utils.audio.extract_audio_from_video (Phase 5.4)."""
    add_log = None
    if process_id:
        def add_log(stage, message, progress=None, status="processing"):
            add_process_log(process_id, stage, message, progress, status)
    return _extract_audio_from_video_raw(video_path, audio_path, add_log=add_log)


# get_db_connection is now in app.db.connection (Phase 5.2)


# /, /api/models, /api/download_model, /api/system_info — in app.blueprints.system (Phase 5.5)


# /api/youtube/info, /api/youtube/download — in app.blueprints.youtube (Phase 5.6)


def _download_youtube_core(url, download_id, save_to_library=False, quality='best', start_time=None, end_time=None):
    """Adapter to app.services.youtube_pytubefix.download_youtube_audio (Phase 5.4).
    Injects dependencies (download_progress, add_process_log, get_db_connection)
    from app.py's local singletons.
    """
    return _download_youtube_audio_service(
        url=url,
        download_id=download_id,
        save_to_library=save_to_library,
        quality=quality,
        start_time=start_time,
        end_time=end_time,
        youtube_folder=app.config['YOUTUBE_FOLDER'],
        download_progress_set=download_progress.set,
        add_log=add_process_log,
        get_db_conn=get_db_connection,
    )


# The old implementation of _download_youtube_core was moved to app.services.youtube_pytubefix (Phase 5.4)


# /api/youtube/progress/<id> — in app.blueprints.youtube (Phase 5.6)


# /api/process/logs, /api/events, /api/jobs* — in app.blueprints.events (Phase 5.9)


# /api/audio/download — in app.blueprints.audio_library (Phase 5.7)


# extract_youtube_id is now in app.utils.youtube_id (Phase 5.1)


# /api/audio/* endpoints — in app.blueprints.audio_library (Phase 5.7)


# Routes moved to app.blueprints.transcription (Phase 5.8):
#   /api/transcribe, /api/history*, /api/transcription/<id>/polish,
#   /api/export/<format>
# format_timestamp is now imported from app.utils.files as format_srt_timestamp (Phase 5.1)


# /api/metrics and /api/health — in app.blueprints.system (Phase 5.5)


# FFmpeg check at startup
def check_ffmpeg_on_startup():
    """Check that FFmpeg is present at startup"""
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=NO_WINDOW,
        )
        if result.returncode == 0:
            logger.info("FFmpeg found and working")
            return True
    except FileNotFoundError:
        logger.warning("WARNING: FFmpeg not found!")
        logger.warning("YouTube download will not work without FFmpeg.")
        logger.warning("Install FFmpeg: https://ffmpeg.org/download.html")
        return False
    return False


# Handler for graceful shutdown

def cleanup():
    """Clean up resources on shutdown"""
    logger.info("Application shutting down...")
    system_monitor.stop()
    if file_cleanup is not None:
        file_cleanup.stop()
    executor.shutdown(wait=True)
    live_executor.shutdown(wait=True)
    logger.info("Application stopped")

atexit.register(cleanup)

# Database initialization at startup
init_database()
logger.info("Database initialized")
# config-registry-profiles S2: init_database() both migrates and checks the connection
# (SELECTs in app.db.migrations) — it did not fail, so the step is 'ok'. We do not
# catch the exception here: a failure of init_database() was always meant to kill startup, /api/ready
# (story 03) will simply not see boot_finished=True.
# `migrations` is deliberately NOT written into робота (робота-contract-03, finding
# 9): the key was removed from the contract in app/state.py — it is never in
# the 'error' state, because an exception from app/db/migrations.py kills the process earlier.
_state.робота['database'] = 'ok'

# T6.6: vector search scale monitoring at startup — a cheap COUNT(*)
# of embedded chunks (WITHOUT loading BLOBs), a WARNING in the log if
# RECALL_VECTOR_WARN_THRESHOLD is exceeded (default 50000). Fail-silent — monitoring must
# not get in the way of startup.
try:
    from app.services import embeddings as _embeddings_scale_check
    _embeddings_scale_check.check_vector_scale(app.config['DATABASE'])
except Exception as _e:
    logger.warning("Failed to check the vector search scale: %s", _e)

# Start the background system monitor (Phase 2)
system_monitor.start()

# Check for model updates (faster-whisper) — staleness-gated, once per ~2 weeks.
# No cron: a background daemon at startup if it has not been checked for a while. The header shows
# a line when a newer version is out (= possible new models). Fail-silent offline.
app.config['MODEL_UPDATE_STATE'] = os.path.join(app.root_path, 'model_update_state.json')
try:
    from app.services import model_updates
    model_updates.start_background_check(app.config['MODEL_UPDATE_STATE'])
except Exception as _e:
    logger.warning("Failed to start the model update check: %s", _e)

# Warm up the whisper model at startup: without this the first /api/transcribe
# hangs on "Loading model..." inside the HTTP request (30-60s+), clients with a
# sensible timeout retry — a typical source of transcription duplicates (see
# the anti-dup guard in app/blueprints/transcription.py transcribe()). A background
# daemon thread, does not block boot; RECALL_PRELOAD_WHISPER=0 — disable.
#
# config-registry-fix-r3-01: the state cycle (loading → ready|failed|disabled)
# and the criterion "the warmed-up model is still in the cache" (not current_model_name, finding
# 8) were moved into whisper_preload.start_tracked_preload() — a unit test with
# a fake manager proves the order of states, instead of polling a child
# process. Exceptions at startup are logged (T7.4) and reduce робота to
# 'failed' INSIDE the function itself.
from app.services.whisper_preload import start_tracked_preload
start_tracked_preload(whisper_manager, _state.робота)

# Auto-cleanup of old files (Phase 6.3): only if AUTO_CLEANUP_ENABLED=true in env.
# Off by default — so a local user does not accidentally lose files.
if os.environ.get('AUTO_CLEANUP_ENABLED', 'false').lower() == 'true':
    _cleanup_age_days = int(os.environ.get('AUTO_CLEANUP_DAYS', '7'))
    file_cleanup = FileCleanupService(
        upload_folder=app.config['UPLOAD_FOLDER'],
        youtube_folder=app.config['YOUTUBE_FOLDER'],
        db_path=app.config['DATABASE'],
        max_age_days=_cleanup_age_days,
    )
    file_cleanup.start()
else:
    file_cleanup = None
    logger.info("FileCleanup: disabled (AUTO_CLEANUP_ENABLED=false)")

# FFmpeg check
ffmpeg_available = check_ffmpeg_on_startup()

# Windows: no console flashing from child ffmpeg/ffprobe. Our own calls
# carry creationflags=NO_WINDOW, while pydub calls ffmpeg itself — we patch it here.
silence_pydub_console_windows()

# Phase 9: RecordingService (system audio + microphone). Created
# only if RECORDING_ENABLED (autodetect via importing pyaudiowpatch).
# Finalize PCM→WAV→MP3 is pushed into the JobQueue so the /stop endpoint
# returns instantly. Recovery of orphaned sessions after a crash-restart
# also uses this same job queue.
if getattr(cfg, 'RECORDING_ENABLED', False):
    try:
        from app.services.recording import (
            RecordingService, SessionStore, finalize_session, register_recording,
            reconcile_recordings,
        )
        _rec_store = SessionStore(cfg.RECORDING_DIR)
        _rec_lib_db_path = app.config['DATABASE']

        def _recording_finalize_callback(sid: str) -> None:
            """Push finalize into the JobQueue. Does not block the caller.

            Phase 9.10 cleanup: after a successful finalize we publish
            status='finalized' via SSE — the UI can automatically do
            teardown even if the recording was stopped from another client
            (curl, a second tab, etc.).

            Fix (orphan): right after finalize we AUTO-register the recording in the
            library (audio_downloads). Previously the insert lived only in the frontend
            /save — any break (closed tab, 504 on a long recording,
            restart during recovery) left the recording an "orphan". Now registration is
            part of the server-side finalize; recovery of orphaned sessions calls this
            callback too → auto-healing. Idempotent (youtube_id=recording_<sid>).
            """
            def _runner(_job, *_a, **_kw):
                result = finalize_session(
                    session_id=sid,
                    store=_rec_store,
                    bitrate=cfg.RECORDING_MP3_BITRATE,
                    keep_pcm=cfg.RECORDING_KEEP_PCM,
                    keep_wav=cfg.RECORDING_KEEP_WAV,
                )
                # Auto-registration in the library (idempotent). Softly: an error here
                # does not fail finalize and SSE — the recording stays on disk and can still
                # be picked up by a manual /save.
                try:
                    _manifest = _rec_store.read(sid)
                    _reg = register_recording(_rec_lib_db_path, sid, _manifest)
                    if _reg is None:
                        logger.info("Finalize %s: empty recording — not registering", sid)
                    elif _reg.get('created'):
                        logger.info(
                            "Finalize %s: auto-registered in the library (id=%s)",
                            sid, _reg.get('download_id'),
                        )
                    else:
                        logger.info(
                            "Finalize %s: already in the library (id=%s) — skipping",
                            sid, _reg.get('download_id'),
                        )
                except Exception as e:
                    logger.warning(
                        "Auto-registration of recording %s in the library failed: %s", sid, e,
                    )
                if sse_broker is not None:
                    sse_broker.publish(
                        f'recording:{sid}',
                        'status',
                        {
                            'status': 'finalized',
                            'final_mp3_path': result.get('final_mp3_path'),
                            'total_duration_sec': result.get('total_duration_sec'),
                        },
                    )
                # --- Phase 22 Story S5: video finalize (sibling step, NEVER breaks audio) ---
                if getattr(cfg, 'RECORDING_VIDEO_ENABLED', False):
                    try:
                        from app.services.recording import finalize_video
                        finalize_video(
                            sid,
                            _rec_store,
                            getattr(cfg, 'RECORDING_FFMPEG_PATH', r'C:\ffmpeg\bin\ffmpeg.exe'),
                            build_master=getattr(cfg, 'RECORDING_BUILD_MASTER_MP4', False),
                        )
                        # Re-register so has_video / primary_video_path persist to audio_downloads
                        try:
                            _vid_manifest = _rec_store.read(sid)
                            register_recording(_rec_lib_db_path, sid, _vid_manifest)
                        except Exception as _rereg_err:
                            logger.warning(
                                "video finalize %s: re-register failed: %s", sid, _rereg_err,
                            )
                        try:
                            if sse_broker is not None:
                                sse_broker.publish(
                                    f'recording:{sid}',
                                    'video_status',
                                    {'status': 'finalized', 'phase': 'finalize_done'},
                                )
                        except Exception:
                            pass
                    except Exception as e:
                        logger.warning(
                            "video finalize failed (audio already saved): %s", e,
                        )
                return result
            job_queue.submit(
                'recording_finalize',
                _runner,
                meta={'session_id': sid},
            )

        # Phase 22 (Story S4): video-capture factory.
        # Injected into RecordingService only when RECORDING_VIDEO_ENABLED=True.
        # When False (default), _video_supervisor_factory is None → audio-only,
        # no behavior change for existing users.
        _video_supervisor_factory = None
        if getattr(cfg, 'RECORDING_VIDEO_ENABLED', False):
            def _video_supervisor_factory(**kwargs):
                """Build a VideoCaptureSupervisor for a new recording session.

                Called by RecordingService.start() with exactly:
                    session_id, session_dir, tracks_spec, audio_start_wallclock,
                    store, broker
                We enrich each requested track with output_idx + monitor_label
                from the live screen enumeration, then hand the enriched list
                to VideoCaptureSupervisor.
                """
                from app.services.recording.video import VideoCaptureSupervisor
                from app.services.recording import video_probe
                from app.services.recording import screens as screens_mod
                ffmpeg = video_probe.ffmpeg_path(cfg)
                caps = video_probe.probe_capabilities(ffmpeg)
                # Build monitor index → monitor dict lookup
                raw_mons = screens_mod.enumerate_monitors()
                cal_mons = screens_mod.calibrate_output_idx(raw_mons, ffmpeg, caps)
                mon_map = {m['monitor_index']: m for m in cal_mons}
                # Enrich each requested track with output_idx + monitor_label
                enriched = []
                for t in kwargs.get('tracks_spec', []):
                    mi = t.get('monitor_index')
                    m = mon_map.get(mi, {})
                    enriched.append({
                        **t,
                        'output_idx': m.get('output_idx', mi),
                        'monitor_label': m.get('label', f'Monitor {mi}'),
                        'monitor_pos_x': m.get('pos_x', 0),
                        'monitor_pos_y': m.get('pos_y', 0),
                    })
                # Build kwargs for VideoCaptureSupervisor, replacing raw
                # tracks_spec with enriched list.
                sup_kwargs = {
                    k: kwargs[k]
                    for k in ('session_id', 'session_dir', 'audio_start_wallclock',
                              'store', 'broker')
                    if k in kwargs
                }
                return VideoCaptureSupervisor(
                    tracks_spec=enriched,
                    ffmpeg_path=ffmpeg,
                    caps=caps,
                    fps=getattr(cfg, 'RECORDING_VIDEO_FPS', 30),
                    codec=getattr(cfg, 'RECORDING_VIDEO_CODEC', 'h264_nvenc'),
                    quality=getattr(cfg, 'RECORDING_VIDEO_QUALITY', 'p5'),
                    cq=getattr(cfg, 'RECORDING_VIDEO_CQ', 23),
                    stop_timeout=getattr(cfg, 'RECORDING_VIDEO_STOP_TIMEOUT_SEC', 8.0),
                    region_overlay=getattr(cfg, 'RECORDING_REGION_OVERLAY', True),
                    overlay_color=getattr(cfg, 'RECORDING_REGION_OVERLAY_COLOR', '#e5484d'),
                    **sup_kwargs,
                )

        # ADR-026 / spike-decision item 3: we target the app by the PIDs that hold an
        # audio session on any active render endpoint (the include-tree reaches
        # only direct children). Session PIDs are collapsed to the roots of the exe tree; the one that is
        # sounding goes first (story 11). No sessions → exe roots via psutil.
        from app.services.recording.app_audio import session_roots
        from app.services.recording.audio_source import AppAudioSource, resolve_root_pids

        def _session_pid_resolver(exe: str) -> list:
            return session_roots(exe, fallback=resolve_root_pids)

        def _app_source_factory(spec, sample_rate: int, channels: int):
            src = AppAudioSource(spec, sample_rate, channels, pid_resolver=_session_pid_resolver)
            src.open()
            return src

        recording_service = RecordingService(
            store=_rec_store,
            sse_broker=sse_broker,
            chunk_seconds=cfg.RECORDING_CHUNK_SECONDS,
            sample_rate=cfg.RECORDING_SAMPLE_RATE,
            channels=cfg.RECORDING_CHANNELS,
            finalize_callback=_recording_finalize_callback,
            video_supervisor_factory=_video_supervisor_factory,
            max_active=cfg.RECORDING_MAX_ACTIVE,
            app_source_factory=_app_source_factory,
        )
        _recovered = recording_service.recover_orphaned()
        if _recovered:
            logger.info(
                "Recording recovery: found %d unfinished sessions: %s",
                len(_recovered), _recovered,
            )
        # Second pass: sessions already marked crashed that still never got
        # final.mp3 — i.e. the finalize job itself crashed (restart during finalization).
        # Without this step such a recording is not visible ANYWHERE: recover_orphaned looks
        # only at active statuses, reconcile — only at finalized ones, /save for
        # crashed returns 500. The PCM lay on disk until someone dug in by hand.
        # Limited by MAX_FINALIZE_ATTEMPTS so that a deterministically broken recording does not
        # put a heavy job on every startup.
        _requeued = recording_service.recover_unfinalized()
        if _requeued:
            logger.warning(
                "Recording recovery: %d unfinished session(s) re-"
                "queued for finalize: %s",
                len(_requeued), _requeued,
            )
        # Phase 9.10: save for the UI banner — the UI endpoint will pick it up and clear it
        recording_recovery_log = list(_recovered) + list(_requeued)

        # Boot-time reconcile: guarantee that every finalized session from disk is
        # present and correct in the Media Library (audio_downloads) — register orphans,
        # heal dead file_paths after the project was moved, sync titles.
        # NEVER transcribes (the user decides in the UI). Idempotent → in a daemon
        # thread so the disk scan does not block startup (the same pattern as the whisper
        # warm-up above). Twice under the reloader — safe (all steps are idempotent).
        def _run_recording_reconcile(_db=_rec_lib_db_path, _store=_rec_store):
            try:
                reconcile_recordings(_db, _store)
            except Exception as _e:
                logger.warning("Boot-time reconcile of recordings failed: %s", _e)
        threading.Thread(
            target=_run_recording_reconcile,
            name='recording-reconcile',
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning("Recording service failed to start: %s", e)
        recording_service = None
        recording_recovery_log = []
else:
    recording_service = None
    recording_recovery_log = []
    if cfg.HEADLESS:
        # config-registry-profiles S2: cfg.RECORDING_ENABLED is already forced to False
        # in get_config() (config.py, Assumption 3) without trying to import pyaudiowpatch —
        # this log just names the reason explicitly for the headless operator.
        logger.info("profile=headless: recorder off")
    else:
        logger.info("Recording: disabled (RECORDING_ENABLED=False)")

# T2.1 (REMEDIATION_PLAN Wave 1): JobQueue persistence — the same recovery
# pattern as above for recording (SessionStore.recover_orphaned): jobs
# in the queued/running state from the previous run could not survive
# a process restart/crash (the executor and threads start anew) — so the DB
# marks them 'crashed', and they are immediately visible via /api/jobs instead of silently
# vanishing (a task the user waited 2 hours for).
job_queue.set_db_path(app.config['DATABASE'])
_crashed_jobs = job_queue.recover_crashed()
if _crashed_jobs:
    logger.warning(
        "JobQueue recovery: %d job(s) from the previous run marked crashed: %s",
        len(_crashed_jobs), _crashed_jobs,
    )
_state.робота['job_queue'] = {'bound': True, 'recovered': len(_crashed_jobs)}

# T4.6 (REMEDIATION_PLAN Wave 2): purge of expired soft-deleted records
# (grace period RECALL_SOFTDELETE_GRACE_DAYS, default 7d) — the same
# start-time pattern as the jobs/recording recovery above. Not a periodic background
# task: one pass on restart is enough, because the undo window is counted in days.
try:
    from app.services import retention as _retention
    _retention.purge_soft_deleted(app.config['DATABASE'])
except Exception as e:
    logger.warning("Soft-delete purge not performed: %s", e)


# Phase 12.26 (Big Bet): Live transcribe worker — background transcribe
# during recording. Does not block the recording, works in a separate thread.
live_transcribe_worker = None
if recording_service is not None:
    try:
        from app.services.live_transcribe import LiveTranscribeWorker
        live_transcribe_worker = LiveTranscribeWorker(
            whisper_manager=whisper_manager,
            broker=sse_broker,
            model_name='small',  # speed/quality trade-off
            interval=8.0,
        )
        logger.info("LiveTranscribeWorker ready (model=small, interval=8s)")
    except Exception as e:
        logger.warning("LiveTranscribeWorker failed to start: %s", e)
        live_transcribe_worker = None

# Phase 19 (Co-pilot): the live call co-pilot. The service only persists into
# copilot_sessions (no Ollama dependency in Step 1); live analysis (topics/
# dispatcher/escalation) is a separate CopilotWorker in later steps. Gated by
# cfg.COPILOT_ENABLED. Availability of the local LLM is checked at runtime
# (local_llm.is_available), not here.
copilot_service = None
if getattr(cfg, 'COPILOT_ENABLED', False):
    try:
        from app.services.copilot.service import CopilotService
        # C6: one co-pilot per process; the "live" recording is determined by RecordingService.
        copilot_service = CopilotService(
            db_path=app.config['DATABASE'],
            is_recording_active=(recording_service.is_active
                                 if recording_service is not None else None),
        )
        logger.info("CopilotService ready (model_local=%s)",
                    getattr(cfg, 'LOCAL_LLM_MODEL', '?'))
    except Exception as e:
        logger.warning("CopilotService failed to start: %s", e)
        copilot_service = None
else:
    logger.info("Co-pilot: disabled (COPILOT_ENABLED=False)")

# Phase 19 (Co-pilot, Step 2): CopilotWorker — topic tracking on embeddings during
# recording. Needs the live worker (text source) + local embeddings (e5). Softly:
# if either is missing → it does not start, recording/session work as before.
copilot_worker = None
if copilot_service is not None and live_transcribe_worker is not None:
    try:
        from app.services.copilot.worker import CopilotWorker
        copilot_worker = CopilotWorker(
            broker=sse_broker,
            live_transcribe_worker=live_transcribe_worker,
            recording_service=recording_service,
            copilot_service=copilot_service,
            db_path=app.config['DATABASE'],
        )
        logger.info("CopilotWorker ready (topic tracking + dispatcher LLM)")
    except Exception as e:
        logger.warning("CopilotWorker failed to start: %s", e)
        copilot_worker = None

# Phase 5.5: bind module-level singletons for blueprints.
_state.init(
    whisper_manager=whisper_manager,
    executor=executor,
    live_executor=live_executor,
    job_queue=job_queue,
    download_progress=download_progress,
    process_logs=process_logs,
    sse_broker=sse_broker,
    active_library_transcriptions={},
    system_monitor=system_monitor,
    file_cleanup=file_cleanup,
    metrics=metrics,
    limiter=limiter,
    ffmpeg_available=ffmpeg_available,
    cfg=cfg,
    add_log=add_process_log,
    recording_service=recording_service,
    recording_recovery_log=recording_recovery_log,
    live_transcribe_worker=live_transcribe_worker,
    copilot_service=copilot_service,
    copilot_worker=copilot_worker,
    RATE_LIMIT_TRANSCRIBE=RATE_LIMIT_TRANSCRIBE,
    RATE_LIMIT_YOUTUBE=RATE_LIMIT_YOUTUBE,
    RATE_LIMIT_POLISH=RATE_LIMIT_POLISH,
)

# Phase 12.14: Security headers — applied to all responses
# (including blueprint endpoints). We allow third-party CDNs that are actually
# used in templates (Google Fonts, cdnjs FontAwesome,
# unpkg for popper/tippy, YouTube for embed/thumbnails).
_CSP_POLICY = (
    "default-src 'self'; "
    # inline styles are needed for chip colors and borders (dynamic).
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
    "https://cdnjs.cloudflare.com https://unpkg.com; "
    # inline scripts: onclick handlers in kinds modals.
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://www.youtube.com; "
    "img-src 'self' data: blob: https://i.ytimg.com https://yt3.ggpht.com "
    "https://*.youtube.com; "
    "media-src 'self' blob:; "
    "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
    # unpkg — so the browser can pull the popper/tippy sourcemaps (otherwise it is noisy in the console).
    "connect-src 'self' https://unpkg.com; "
    "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.after_request
def _add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault(
        'Permissions-Policy',
        'geolocation=(), microphone=(self), camera=(), payment=()',
    )
    # CSP — we support headers only for HTML, not for JSON/file endpoints
    # (so as not to block fetch in API-only clients).
    ctype = response.headers.get('Content-Type', '')
    if ctype.startswith('text/html'):
        response.headers.setdefault('Content-Security-Policy', _CSP_POLICY)
    return response


# Phase 5.5+: register blueprints
app.register_blueprint(_system_bp)
app.register_blueprint(_youtube_bp)
app.register_blueprint(_audio_bp)
app.register_blueprint(_transcription_bp)
app.register_blueprint(_events_bp)
app.register_blueprint(_recording_bp)
app.register_blueprint(_speakers_bp)
app.register_blueprint(_bookmarks_bp)
app.register_blueprint(_memory_bp)
app.register_blueprint(_documents_bp)
app.register_blueprint(_telegram_bp)
app.register_blueprint(_research_bp)
app.register_blueprint(_copilot_bp)
app.register_blueprint(_settings_bp)
app.register_blueprint(_comments_bp)

# T1.2 (Wave 1): a single before_request authentication gate for all /api/*
# (except /api/health). A centralized point instead of a decorator on each of
# ~90 endpoints — impossible to forget to protect a new one. Details of the decision
# (localhost+trusted / RECALL_API_KEY / fail-closed) — app/core/auth.py.
from app.core import auth as _auth
_auth.register(app)

# T7.4 (Wave 4): a single catch-all error handler for unhandled exceptions —
# traceback to the log (with request_id via _RequestIdLogFilter above), the client gets
# a generic message + request_id, WITHOUT the internal message (str(exc)).
# Registered AFTER the auth gate and register_blueprint, but the order of registration of
# errorhandlers in Flask does not depend on the before_request/blueprint order —
# @app.errorhandler(429)/(413) above stay higher priority for their own codes.
from app.core import error_handlers as _error_handlers
_error_handlers.register(app)

# Rate limits on blueprint functions. We attach AFTER register_blueprint.
limiter.limit(lambda: RATE_LIMIT_YOUTUBE)(app.view_functions['youtube.download_youtube'])
limiter.limit(lambda: RATE_LIMIT_YOUTUBE)(app.view_functions['audio_library.download_audio_only'])
limiter.limit(lambda: RATE_LIMIT_TRANSCRIBE)(app.view_functions['transcription.transcribe'])
# Phase 16A: document upload — the same limit as transcription.
if 'documents.upload_document' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_TRANSCRIBE)(app.view_functions['documents.upload_document'])
limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.polish_transcription'])
# Phase 12.15: Claude API endpoints (summarize/translate) — the same limit as polish.
if 'transcription.summarize_transcription' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.summarize_transcription'])
if 'transcription.translate_transcription' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.translate_transcription'])
if 'transcription.sentiment_transcription' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.sentiment_transcription'])
if 'transcription.extract_topics_endpoint' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.extract_topics_endpoint'])
# Phase 12.15: expensive server-side ops (merge speakers, segment edits).
# High frequency is not expected — a strict limit.
RATE_LIMIT_MUTATIONS = "60 per minute"
RATE_LIMIT_BULK = "10 per hour"
for view_name in [
    'speakers.merge_speakers',
    'transcription.merge_segments', 'transcription.split_segment',
]:
    if view_name in app.view_functions:
        limiter.limit(lambda: RATE_LIMIT_MUTATIONS)(app.view_functions[view_name])
# Phase 12.15: bookmark/saved searches — high-frequency UI actions.
RATE_LIMIT_USER_DATA = "120 per minute"
for view_name in [
    'bookmarks.create_bookmark', 'bookmarks.delete_bookmark',
    'bookmarks.update_bookmark',
    'bookmarks.create_saved_search', 'bookmarks.delete_saved_search',
    'bookmarks.touch_saved_search',
]:
    if view_name in app.view_functions:
        limiter.limit(lambda: RATE_LIMIT_USER_DATA)(app.view_functions[view_name])
# Phase 9.10 hardening: limit on recording.start_recording. Pause/resume/
# stop/state are not limited — these are continuation activity for an existing session.
if 'recording.start_recording' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_RECORDING_START)(app.view_functions['recording.start_recording'])
# T5.1: settings_api.test_anthropic_key makes a real Claude call (Haiku,
# cheap, but not unlimited) — the same limit as polish. save/toggle/status —
# only a write to .env / reading os.environ, with no external calls — MUTATIONS
# is enough to keep disk writes from being spammed.
if 'settings_api.test_anthropic_key' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['settings_api.test_anthropic_key'])
for view_name in ['settings_api.save_anthropic_key', 'settings_api.clear_anthropic_key', 'settings_api.toggle_copilot']:
    if view_name in app.view_functions:
        limiter.limit(lambda: RATE_LIMIT_MUTATIONS)(app.view_functions[view_name])


def _maybe_launch_telegram_listener():
    """Phase 17: the SINGLE launch point — app.py brings up the Telegram listener as a
    MANAGED child process (a single `python app.py` = both the web app and the listener).

    The listener stays a SEPARATE process (not a thread): Telethon has its own
    asyncio loop + an exclusive session (SQLite cannot be opened by two clients), and the
    Flask reloader would tear the in-process connection. So the "single point" = the app manages
    the lifecycle of the subprocess, rather than merging them into one process.

    Launches only if: TELEGRAM_ENABLED (telethon+keys) and a session file already exists
    (otherwise the interactive login would hang in the subprocess → run telegram_login.py first).
    stdout/stderr are inherited → listener logs go to the same console."""
    if not getattr(cfg, 'TELEGRAM_ENABLED', False):
        logger.info("Telegram: disabled (no keys or telethon) — not starting the listener")
        _state.робота['telegram'] = 'disabled'
        return
    session_file = str(getattr(cfg, 'TELEGRAM_SESSION', 'telegram')) + '.session'
    if not os.path.exists(session_file):
        logger.warning("Telegram: no session (%s). First run once: "
                       "python telegram_login.py", session_file)
        _state.робота['telegram'] = 'absent'
        return
    _state.робота['telegram'] = 'starting'
    try:
        proc = subprocess.Popen([sys.executable, 'telegram_listener.py'],
                                cwd=str(getattr(cfg, 'BASE_DIR', '.')))
    except Exception as e:
        logger.error("Telegram: failed to start the listener: %s", e)
        _state.робота['telegram'] = 'absent'
        return
    logger.info("Telegram: listener started as a child process (pid=%s)", proc.pid)
    _state.робота['telegram'] = 'running'

    def _stop_listener():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    atexit.register(_stop_listener)


def _maybe_start_call_watcher():
    """K2: the "Record?" / "Stop?" hints driven by the call itself (call_watch.py).

    Only under ``__main__`` and only in the process that serves requests — under pytest
    (importing app.py) the thread does not start and sends no notifications. A disabled flag,
    non-Windows or no pycaw → one log line and nothing more."""
    if recording_service is None or not _settings.env_bool('RECORDING_AUTOPROMPT'):
        logger.info("Call hints: disabled (no recording or RECORDING_AUTOPROMPT=0)")
        return
    if sys.platform != 'win32':
        logger.info("Call hints: Windows only — not starting")
        return
    try:
        import pycaw  # noqa: F401 — presence check, the enumeration itself is lazy
        from app.blueprints.recording import stop_session
        from app.services.recording.app_audio import list_call_sessions
        from app.services.recording.call_watch import CallWatcher, ToastNotifier, parse_apps
        from app.services.recording.service import SessionNotFoundError
    except ImportError as e:
        logger.info("Call hints: dependency missing (%s) — not starting", e)
        return

    def _live_sessions():
        out = []
        for sid in recording_service.active_session_ids():
            try:
                st = recording_service.get_state(sid)
            except SessionNotFoundError:
                continue
            streams = st.get('streams') or {}
            out.append({
                'sid': sid,
                'system_source': st.get('system_source'),
                'is_paused': bool(st.get('is_paused')),
                'mic_peak': float((streams.get('mic') or {}).get('peak') or 0.0),
                'sys_peak': float((streams.get('system') or {}).get('peak') or 0.0),
            })
        return out

    _root = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(os.path.join(_root, 'logs'), exist_ok=True)
        watcher = CallWatcher(
            apps=parse_apps(_settings.env('RECORDING_AUTOPROMPT_APPS')),
            prompt_seconds=_settings.env_int('RECORDING_AUTOPROMPT_SECONDS'),
            autostop_minutes=_settings.env_int('RECORDING_AUTOSTOP_SILENCE_MIN'),
            base_url=f'http://127.0.0.1:{cfg.PORT}',
            enumerate_calls=list_call_sessions,
            live_sessions=_live_sessions,
            stop_fn=stop_session,
            notifier=ToastNotifier(icon_path=os.path.join(_root, 'static', 'icon-192.png')),
            log_path=os.path.join(_root, 'logs', 'call_watch.jsonl'),
        )
    except Exception as e:
        logger.warning("Call hints failed to start: %s", e, exc_info=True)
        return
    app.extensions['call_watcher'] = watcher
    watcher.start()



def _maybe_start_pipes():
    """K4: scheduled pipes (app/services/pipes.py).

    Only under ``__main__`` — under pytest (importing app.py) the thread does not start.
    ``PIPES_ENABLED=0`` → one log line and nothing more."""
    if not _settings.env_bool('PIPES_ENABLED'):
        logger.info("Pipes: disabled (PIPES_ENABLED=0)")
        return
    try:
        from app.services import tg_bot
        from app.services.pipes import PipeScheduler, load_pipes

        if not tg_bot.is_configured():
            logger.warning("Pipes: the bot is not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_BOT_CHAT_ID) — "
                           "runs will be written to history with an error")
        scheduler = PipeScheduler(load_pipes(), db_path=app.config['DATABASE'],
                                   deliver=tg_bot.send, buttons=True)
    except Exception as e:
        logger.warning("Pipes failed to start: %s", e, exc_info=True)
        return
    app.extensions['pipe_scheduler'] = scheduler
    scheduler.start()
    # K5: the "done / not relevant / postpone" buttons under briefings — the presses
    # arrive via long-polling into this process (a webhook without a public address is impossible).
    if tg_bot.is_configured():
        try:
            from app.services.brief_actions import ButtonPoller
            poller = ButtonPoller(db_path=app.config['DATABASE'])
            app.extensions['brief_buttons'] = poller
            poller.start()
        except Exception as e:
            logger.warning("Brief buttons failed to start: %s", e, exc_info=True)

# config-registry-profiles S2: the module-level boot sequence is complete
# (DB/migrations/JobQueue/recorder gate/counters/blueprints/auth/error handler
# all above). The Telegram listener is launched below only under `__main__` (reloader
# gate, comment of _maybe_launch_telegram_listener) — робота['telegram']
# is updated by that call regardless of this flag. /api/ready (story 03)
# reads boot_finished as "the app.py module is fully assembled", not "all optional
# child processes have already started".
_state.робота['boot_finished'] = True

if __name__ == '__main__':
    logger.info("Whisper UI started!")
    logger.info(f"Open http://localhost:{cfg.PORT} in the browser")
    if not ffmpeg_available:
        logger.warning("FFmpeg is not installed - YouTube features will be unavailable")

    debug_mode = bool(cfg.DEBUG)  # single source of truth — config.py (env FLASK_DEBUG)

    # Bind address: a safe default of 127.0.0.1 (the local machine only). Exposing
    # to the whole LAN — only by an explicit opt-in (RECALL_BIND_ALL=1 or FLASK_HOST=0.0.0.0),
    # because the app has no authentication yet (see docs/REMEDIATION_PLAN.md T1.2).
    bind_all = _settings.env_bool('RECALL_BIND_ALL')
    host = '0.0.0.0' if (bind_all or cfg.HOST == '0.0.0.0') else cfg.HOST
    if host == '0.0.0.0':
        logger.warning(
            "RECALL_BIND_ALL/FLASK_HOST=0.0.0.0: the app is reachable over the network "
            "WITHOUT authentication (see T1.2 in docs/REMEDIATION_PLAN.md) — "
            "enable only on a trusted LAN."
        )

    # We bring up the Telegram listener ONCE. Under the reloader (debug) the server
    # child has WERKZEUG_RUN_MAIN='true' — we skip it so as not to spawn
    # duplicates on every reload; the stable watcher process starts it.
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        _maybe_launch_telegram_listener()
    # K2: the call watcher — in the process that serves requests (under the reloader
    # this is the child with WERKZEUG_RUN_MAIN='true', without the reloader — this same one).
    if not debug_mode or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        _maybe_start_call_watcher()
        _maybe_start_pipes()
    # threaded=True for SSE: each connection in its own thread, does not block the others.
    app.run(debug=debug_mode, host=host, port=cfg.PORT, threaded=True)