# Файл: WhisperUI/app.py
# Путь: WhisperUI/app.py
# Описание: Основной Flask сервер с YouTube интеграцией

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
# Загружаем .env как можно раньше — до того, как сервисы прочитают env vars
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
    """Wrapper що підставляє шлях БД з app.config (Phase 5.2)."""
    return _get_db_connection_raw(app.config['DATABASE'])


def init_database():
    """Wrapper що використовує app.config['DATABASE'] (Phase 5.2)."""
    _init_database_raw(app.config['DATABASE'])
import torch
from pydub import AudioSegment


class ThreadSafeProgressStore:
    """Потокобезпечне сховище для прогресу операцій.

    При set/cleanup публикует события в SSE broker (если он доступен) — это
    даёт фронту возможность подписаться через /api/events/<id> вместо polling.
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
        # Publish ВНЕ lock'а
        try:
            sse_broker.publish(key, self._sse_event_name, value)
            # Если статус терминальный — пробросим событие 'complete'/'error' для закрытия SSE.
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
        """Видаляє лише завершені (completed/error) записи при переповненні.

        T2.4 (Волна 2): раніше робив сліпий ``popitem(last=False)`` —
        витісняв НАЙСТАРІШИЙ запис незалежно від статусу, тобто міг викинути
        ще виконувану операцію (юзер втрачав прогрес-бар). Тепер — як у
        ``ThreadSafeProcessLogs._cleanup()`` поруч: шукаємо найстаріші
        термінальні (``completed``/``error``) записи і чистимо лише їх. Якщо
        весь стор заповнений активними операціями понад ліміт — нічого не
        видаляємо (тимчасово перевищуємо ``max_size``), лише логуємо
        warning; це не magic-ліміт, який важливіше за втрату прогресу.
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
                "ThreadSafeProgressStore: ліміт %d перевищено (%d записів), "
                "усі активні — жодного не витіснено.",
                self._max_size, len(self._data),
            )
            return
        for key in to_remove:
            del self._data[key]

    def cleanup_batch(self, count):
        """Видаляє count найстаріших записів"""
        with self._lock:
            for _ in range(min(count, len(self._data))):
                self._data.popitem(last=False)


class ThreadSafeProcessLogs:
    """Потокобезпечне сховище для логів процесів"""

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
        """Видаляє тільки завершені процеси при переповненні"""
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


# Налаштування логування з RotatingFileHandler
cfg = get_config()

# T8.2 (Волна 4): request-id/correlation-id у кожному лог-записі. root-логер
# (basicConfig нижче) спільний для app.py + усіх blueprints/services (вони всі
# роблять logging.getLogger(__name__), яке пропагує у root) — тож досить ОДНОГО
# Filter'а на handler, а не LoggerAdapter на кожен виклик логера по всій кодовій
# базі. g.request_id проставляється у _assign_request_id (before_request нижче,
# реєструється ПЕРШИМ — раніше за auth-гейт app/core/auth.py), тож навіть 401 від
# auth-гейта потрапляє у лог з тим самим id, що піде клієнту в X-Request-Id.
class _RequestIdLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, 'request_id', '-') if has_request_context() else '-'
        return True


# config-registry-profiles S2: RECALL_LOG_FORMAT=json перемикає обидва
# хендлери (file+stream) на однорядковий JSON (app/core/logger.JsonFormatter);
# дефолт 'text' лишає попередній людинозчитний формат без змін.
if _settings.env('RECALL_LOG_FORMAT').strip().lower() == 'json':
    from app.core.logger import JsonFormatter
    _log_formatter = JsonFormatter()
else:
    _log_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [req:%(request_id)s] - %(message)s'
    )

_file_handler = RotatingFileHandler(
    getattr(cfg, 'LOG_FILE', 'whisper_app.log'),
    maxBytes=getattr(cfg, 'LOG_MAX_SIZE', 10 * 1024 * 1024),  # 10MB
    backupCount=getattr(cfg, 'LOG_BACKUP_COUNT', 5),
    encoding='utf-8'
)
_file_handler.setFormatter(_log_formatter)
_file_handler.addFilter(_RequestIdLogFilter())

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)
_stream_handler.addFilter(_RequestIdLogFilter())

logging.basicConfig(
    level=getattr(logging, getattr(cfg, 'LOG_LEVEL', 'INFO'), logging.INFO),
    handlers=[_file_handler, _stream_handler]
)
logger = logging.getLogger(__name__)

# Приглушуємо галасливі сторонні логери. На DEBUG-рівні вони роздували лог до
# ~10MB: HTTP-клієнти логують кожен запит/байт (anthropic-стрім, HF-завантаження),
# а torchaudio/torio при діаризації кидає traceback'и «FFmpeg extension is not
# available» (нефатально — є fallback). werkzeug логує КОЖЕН HTTP-запит на INFO
# (per-request access-спам + інколи traceback при emit access-рядка) — на WARNING
# лишаються лише реальні попередження/помилки. Наші app.* логери не чіпаємо.
for _noisy in ('anthropic', 'httpx', 'httpcore', 'urllib3', 'huggingface_hub',
               'filelock', 'matplotlib', 'werkzeug'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
for _quiet in ('torio', 'torchaudio'):
    logging.getLogger(_quiet).setLevel(logging.CRITICAL)

# Багатопроцесний RotatingFileHandler на спільному whisper_ui.log (app + listener +
# stdio-mcp_server) інколи не може зробити rollover на Windows (WinError 32 — файл
# зайнятий іншим процесом) → logging друкував «--- Logging error ---» + traceback на
# кожен запис. Не валимо процес і не спамимо: глушимо самі помилки логування
# (rollover тихо пропускається). Корінь контеншну ще зменшено через RECALL_LOG_TO_FILE.
logging.raiseExceptions = False

app = Flask(__name__)
# Порт беремо з cfg.PORT (env FLASK_PORT), а не хардкодом: на нестандартному
# порту жорсткий 5050 у списку origins мовчки різав би власний же фронтенд.
CORS(app, origins=[f'http://localhost:{cfg.PORT}', f'http://127.0.0.1:{cfg.PORT}'])

# Конфігурація з config.py
app.config.from_object(cfg)

# T8.2 (Волна 4): генеруємо короткий request-id ПЕРШИМ серед app-level
# before_request-хуків (Flask виконує їх у порядку реєстрації) — реєструється
# тут, ДО register_blueprint(...) і ДО auth.register(app) (Волна 1, T1.2) нижче
# по файлу. Завдяки цьому g.request_id доступний уже в момент, коли auth-гейт
# вирішує повертати 401 — його `logger.warning(...)` теж піде через root-логер
# і підхопить request_id через _RequestIdLogFilter вище, хоча app/core/auth.py
# саму не чіпаємо. Легкий (один uuid4 hex-зріз), overhead — мікросекунди.
@app.before_request
def _assign_request_id():
    g.request_id = uuid.uuid4().hex[:12]


@app.after_request
def _add_request_id_header(response):
    request_id = getattr(g, 'request_id', None)
    if request_id:
        response.headers['X-Request-Id'] = request_id
    return response

# Phase 17: авто-перечитка Jinja-шаблонів. У non-debug Flask кешує shell.html у
# памʼяті → правки HTML не видно до рестарту процесу. Для локального single-user
# додатку дешево (перевірка mtime на рендер) і прибирає потребу рестартувати на
# кожну UI-правку шаблону. Статика (CSS/JS) і так читається свіжою.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# T1.5 (Волна 1), уточнено config-registry-fix S4 (знахідка 14): перевірка
# SECRET_KEY тепер ЄДИНА і виконується як hard-fail у config.py — ЛІНИВО,
# всередині `_finalize_secret_key()`, яку викликає `get_config()` при
# ПЕРШОМУ зверненні (тут — `cfg = get_config()` вище по файлу), НЕ в момент
# `import config`. Раніше (T1.5) це справді був модульний рівень;
# config-registry-profiles S3 переніс побічні ефекти в ліниву ініціалізацію.
# Дивись config.py біля `_finalize_secret_key`/`_initialize_dynamic_config`
# для деталей. Тут (у app.py) дублювання самої перевірки прибрано.

# Phase 6: rate limiting.
# Один Limiter, завжди увімкнений. Лімити підібрані так, щоб single-user
# локально їх не помічав (60/min для transcribe, тобто 1 на секунду).
# Polish — жорстко обмежений завжди (захист гаманця Anthropic від
# випадкового infinite loop у фронтенді).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)
# Кастомні ліміти. Налаштовуються через env-переменные.
RATE_LIMIT_TRANSCRIBE = os.environ.get('RATE_LIMIT_TRANSCRIBE', '60/minute')
RATE_LIMIT_YOUTUBE = os.environ.get('RATE_LIMIT_YOUTUBE', '30/minute')
RATE_LIMIT_POLISH = os.environ.get('RATE_LIMIT_POLISH', '20/hour')
# Phase 9.10 hardening: запобігає накопиченню session-папок при
# script-driven abuse. Кожна сесія = ~10MB/min на диску, тому
# lim треба низький.
RATE_LIMIT_RECORDING_START = os.environ.get('RATE_LIMIT_RECORDING_START', '10/minute')
logger.info(
    f"Rate limits: transcribe={RATE_LIMIT_TRANSCRIBE}, "
    f"youtube={RATE_LIMIT_YOUTUBE}, polish={RATE_LIMIT_POLISH}"
)


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "success": False,
        "error": f"Rate limit exceeded: {e.description}. Спробуйте пізніше.",
    }), 429

# Створюємо необхідні папки
for folder in [cfg.UPLOAD_FOLDER, cfg.TRANSCRIPTS_FOLDER, getattr(cfg, 'MODELS_FOLDER', 'models'),
               cfg.YOUTUBE_FOLDER, getattr(cfg, 'DOCUMENTS_FOLDER', 'documents'),
               str(getattr(cfg, 'TELEGRAM_MEDIA_DIR', 'telegram_media'))]:
    os.makedirs(folder, exist_ok=True)

# ALLOWED_*_EXTENSIONS тепер імпортуються з app.utils.files (Phase 5.1)


# Обработчик ошибки слишком большого файла
@app.errorhandler(413)
def request_entity_too_large(error):
    max_size_gb = app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024 * 1024)
    return jsonify({
        'success': False,
        'error': f'Файл занадто великий. Максимальний розмір: {max_size_gb:.1f} GB'
    }), 413


# Ініціалізуємо менеджер Whisper
whisper_manager = ModernWhisperManager()

# Потокобезпечні сховища для прогресу
MAX_DOWNLOAD_HISTORY = 100
download_progress = ThreadSafeProgressStore(max_size=MAX_DOWNLOAD_HISTORY)

# ThreadPoolExecutor'и для фонових завдань.
#
# T2.5 (Волна 2): раніше ВСІ фонові job'и (youtube/audio download, doc_import,
# enrichment/enrichment_backfill, tg_embed/tg_transcribe, video_analysis,
# recording_finalize) йшли крізь ОДИН ThreadPoolExecutor(max_workers=2).
# Довгий batch (напр. enrichment_backfill по всьому архіву — секвенційні
# виклики Claude API на сотні транскрипцій, може йти хвилинами) міг зайняти
# обидва воркери надовго → latency-критичний recording_finalize (юзер щойно
# натиснув «стоп» і чекає на готовий mp3+реєстрацію в бібліотеці) чекав у
# черзі позаду batch-задачі — «зависання» без видимої причини для юзера.
#
# Рішення: два окремі пули замість одного спільного (простіше за пріоритетну
# чергу поверх одного executor — не треба чіпати семантику submit/cancel).
# JobQueue (app/services/job_queue.py) маршрутизує job за kind: 'recording_finalize'
# → live_executor, усе інше → executor (batch). Реєстр job'ів/persist/
# get_job_status лишається ЄДИНИЙ для обох пулів.
#
# Розміри (обидва — env-перевизначувані, дефолти консервативні):
#  - LIVE_EXECUTOR_WORKERS=1: recording_finalize — ffmpeg-конвертація
#    PCM→WAV→MP3, суто CPU/диск, GPU не займає. У типовому сценарії активна
#    щонайбільше одна recording-сесія одночасно (один юзер, один recorder),
#    тож 1 воркера достатньо, і він завжди вільний — не чекає за batch.
#  - BATCH_EXECUTOR_WORKERS=2: як і в попередньому єдиному пулі. Тут і далі
#    йдуть tg_transcribe / video_analysis(local) — вони МОЖУТЬ вантажити GPU
#    (faster-whisper), тож свідомо НЕ збільшуємо цей пул — сумарна GPU-
#    конкуренція лишається такою ж, як була (максимум 2 паралельні GPU-задачі
#    з цього пулу; live_executor GPU не чіпає, тож у гіршому разі + 0 до VRAM).
#  Разом: 1 + 2 = 3 потоки замість 2 — приріст лише за рахунок некритичного
#  для VRAM ffmpeg-пулу.
LIVE_EXECUTOR_WORKERS = int(os.environ.get('LIVE_EXECUTOR_WORKERS', '1'))
BATCH_EXECUTOR_WORKERS = int(os.environ.get('BATCH_EXECUTOR_WORKERS', '2'))
executor = ThreadPoolExecutor(max_workers=BATCH_EXECUTOR_WORKERS, thread_name_prefix="batch")
live_executor = ThreadPoolExecutor(max_workers=LIVE_EXECUTOR_WORKERS, thread_name_prefix="live")

# Job queue (Phase 2, розведено на пули у T2.5): обёртка над executor'ами для
# отслеживания состояний и cancel. 'recording_finalize' — єдиний kind, що
# зараз потребує live-пулу; решта kind'ів (див. коментар вище) лишаються на
# batch-пулі без змін поведінки.
job_queue = JobQueue(
    executor=executor,
    live_executor=live_executor,
    live_kinds=frozenset({'recording_finalize'}),
)

# Потокобезпечне сховище для логів процесів
MAX_PROCESS_LOGS = 50
process_logs = ThreadSafeProcessLogs(max_size=MAX_PROCESS_LOGS)


def trim_audio_file(input_path, output_path, start_time, end_time, process_id=None):
    """Wrapper до app.utils.audio.trim_audio_file (Phase 5.4).
    Зберігає попередній API: process_id як 5-й аргумент.
    """
    add_log = None
    if process_id:
        def add_log(stage, message, progress=None, status="processing"):
            add_process_log(process_id, stage, message, progress, status)
    return _trim_audio_file_raw(input_path, output_path, start_time, end_time, add_log=add_log)


def add_process_log(process_id, stage, message, progress=None, status="processing"):
    """
    Додає запис до логу процесу (потокобезпечно)
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
    """Отримати логи процесу (потокобезпечно)"""
    return process_logs.get(process_id, last_n)


# init_database тепер у app.db.migrations (Phase 5.2)


# _sanitize_fts_query тепер імпортується з app.utils.fts (Phase 6.4 — для тестування)


# allowed_file, is_video_file тепер у app.utils.files (Phase 5.1)


def extract_audio_from_video(video_path, audio_path, process_id=None):
    """Wrapper до app.utils.audio.extract_audio_from_video (Phase 5.4)."""
    add_log = None
    if process_id:
        def add_log(stage, message, progress=None, status="processing"):
            add_process_log(process_id, stage, message, progress, status)
    return _extract_audio_from_video_raw(video_path, audio_path, add_log=add_log)


# get_db_connection тепер у app.db.connection (Phase 5.2)


# /, /api/models, /api/download_model, /api/system_info — у app.blueprints.system (Phase 5.5)


# /api/youtube/info, /api/youtube/download — у app.blueprints.youtube (Phase 5.6)


def _download_youtube_core(url, download_id, save_to_library=False, quality='best', start_time=None, end_time=None):
    """Адаптер до app.services.youtube_pytubefix.download_youtube_audio (Phase 5.4).
    Інжектує залежності (download_progress, add_process_log, get_db_connection)
    із локальних singleton'ів app.py.
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


# Стара реалізація _download_youtube_core винесена в app.services.youtube_pytubefix (Phase 5.4)


# /api/youtube/progress/<id> — у app.blueprints.youtube (Phase 5.6)


# /api/process/logs, /api/events, /api/jobs* — у app.blueprints.events (Phase 5.9)


# /api/audio/download — у app.blueprints.audio_library (Phase 5.7)


# extract_youtube_id тепер у app.utils.youtube_id (Phase 5.1)


# /api/audio/* endpoints — у app.blueprints.audio_library (Phase 5.7)


# Routes винесені у app.blueprints.transcription (Phase 5.8):
#   /api/transcribe, /api/history*, /api/transcription/<id>/polish,
#   /api/export/<format>
# format_timestamp тепер імпортується з app.utils.files як format_srt_timestamp (Phase 5.1)


# /api/metrics та /api/health — у app.blueprints.system (Phase 5.5)


# Проверка FFmpeg при запуске
def check_ffmpeg_on_startup():
    """Проверка наличия FFmpeg при запуске"""
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
            logger.info("FFmpeg найден и работает")
            return True
    except FileNotFoundError:
        logger.warning("ВНИМАНИЕ: FFmpeg не найден!")
        logger.warning("YouTube скачивание не будет работать без FFmpeg.")
        logger.warning("Установите FFmpeg: https://ffmpeg.org/download.html")
        return False
    return False


# Обробник для коректного завершення роботи

def cleanup():
    """Очищення ресурсів при завершенні роботи"""
    logger.info("Завершення роботи додатку...")
    system_monitor.stop()
    if file_cleanup is not None:
        file_cleanup.stop()
    executor.shutdown(wait=True)
    live_executor.shutdown(wait=True)
    logger.info("Додаток завершено")

atexit.register(cleanup)

# Ініціалізація бази даних при запуску
init_database()
logger.info("База даних ініціалізована")
# config-registry-profiles S2: init_database() і мігрує, і перевіряє з'єднання
# (SELECT-и в app.db.migrations) — не впав, отже крок 'ok'. Виняток тут
# не ловимо: падіння init_database() мало валити старт і раніше, /api/ready
# (story 03) просто не побачить boot_finished=True.
# `migrations` навмисно НЕ пишемо в робота (робота-contract-03, знахідка
# 9): ключ вилучений з контракту в app/state.py — стану 'error' у нього
# ніколи не буває, бо виняток з app/db/migrations.py вбиває процес раніше.
_state.робота['database'] = 'ok'

# T6.6: моніторинг масштабу vector search при старті — дешевий COUNT(*)
# embedded-чанків (БЕЗ завантаження BLOB'ів), WARNING у лог, якщо перевищено
# RECALL_VECTOR_WARN_THRESHOLD (дефолт 50000). Fail-silent — моніторинг не
# має заважати запуску.
try:
    from app.services import embeddings as _embeddings_scale_check
    _embeddings_scale_check.check_vector_scale(app.config['DATABASE'])
except Exception as _e:
    logger.warning("Не вдалося перевірити масштаб vector search: %s", _e)

# Запуск фонового монітору системи (Phase 2)
system_monitor.start()

# Перевірка оновлень моделей (faster-whisper) — staleness-gated, раз на ~2 тижні.
# Без крона: фоновий daemon на старті, якщо давно не перевіряли. Header показує
# рядок, коли вийшла новіша версія (= можливі нові моделі). Fail-silent офлайн.
app.config['MODEL_UPDATE_STATE'] = os.path.join(app.root_path, 'model_update_state.json')
try:
    from app.services import model_updates
    model_updates.start_background_check(app.config['MODEL_UPDATE_STATE'])
except Exception as _e:
    logger.warning("Не вдалося запустити перевірку оновлень моделей: %s", _e)

# Прогрів whisper-моделі при старті: без цього перший /api/transcribe
# висить на "Loading model..." усередині HTTP-запиту (30-60с+), клієнти з
# розумним таймаутом ретраять — типове джерело дублів транскрипції (див.
# anti-dup guard у app/blueprints/transcription.py transcribe()). Фоновий
# daemon-потік, не блокує boot; RECALL_PRELOAD_WHISPER=0 — вимкнути.
#
# config-registry-fix-r3-01: цикл станів (loading → ready|failed|disabled)
# і критерій "прогріта модель досі в кеші" (не current_model_name, знахідка
# 8) винесені у whisper_preload.start_tracked_preload() — юніт-тест з
# фейковим менеджером доводить порядок станів, замість полінгу дочірнього
# процесу. Винятки на старті логуються (T7.4) і зводять робота до
# 'failed' УСЕРЕДИНІ самої функції.
from app.services.whisper_preload import start_tracked_preload
start_tracked_preload(whisper_manager, _state.робота)

# Авточистка старих файлів (Phase 6.3): тільки якщо AUTO_CLEANUP_ENABLED=true в env.
# По дефолту вимкнено — щоб локальний користувач випадково не втратив файли.
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
    logger.info("FileCleanup: вимкнено (AUTO_CLEANUP_ENABLED=false)")

# Проверка FFmpeg
ffmpeg_available = check_ffmpeg_on_startup()

# Windows: жодного блимання консолі від дочірніх ffmpeg/ffprobe. Свої виклики
# несуть creationflags=NO_WINDOW, а pydub кличе ffmpeg сам — його патчимо тут.
silence_pydub_console_windows()

# Phase 9: RecordingService (system audio + microphone). Створюється
# тільки якщо RECORDING_ENABLED (autodetect через імпорт pyaudiowpatch).
# Finalize PCM→WAV→MP3 пушиться у JobQueue щоб /stop endpoint
# повертався миттєво. Recovery orphaned-сесій після крах-рестарту
# теж використовує цей самий job queue.
if getattr(cfg, 'RECORDING_ENABLED', False):
    try:
        from app.services.recording import (
            RecordingService, SessionStore, finalize_session, register_recording,
            reconcile_recordings,
        )
        _rec_store = SessionStore(cfg.RECORDING_DIR)
        _rec_lib_db_path = app.config['DATABASE']

        def _recording_finalize_callback(sid: str) -> None:
            """Push finalize у JobQueue. Не блокує викликача.

            Phase 9.10 cleanup: після успішного finalize публікуємо
            status='finalized' через SSE — UI зможе автоматично зробити
            teardown навіть якщо запис було стопнуто з іншого клієнта
            (curl, другий tab, тощо).

            Fix (orphan): одразу після finalize АВТО-реєструємо запис у
            бібліотеці (audio_downloads). Раніше insert жив лише у фронтендному
            /save — будь-який розрив (закрита вкладка, 504 на довгому записі,
            рестарт під час recovery) лишав запис «сиротою». Тепер реєстрація —
            частина серверного finalize; recovery осиротілих сесій теж кличе цей
            callback → авто-зцілення. Ідемпотентно (youtube_id=recording_<sid>).
            """
            def _runner(_job, *_a, **_kw):
                result = finalize_session(
                    session_id=sid,
                    store=_rec_store,
                    bitrate=cfg.RECORDING_MP3_BITRATE,
                    keep_pcm=cfg.RECORDING_KEEP_PCM,
                    keep_wav=cfg.RECORDING_KEEP_WAV,
                )
                # Авто-реєстрація в бібліотеці (idempotent). М'яко: помилка тут
                # не валить finalize і SSE — запис лишається на диску, його ще
                # можна підняти ручним /save.
                try:
                    _manifest = _rec_store.read(sid)
                    _reg = register_recording(_rec_lib_db_path, sid, _manifest)
                    if _reg is None:
                        logger.info("Finalize %s: порожній запис — не реєструємо", sid)
                    elif _reg.get('created'):
                        logger.info(
                            "Finalize %s: авто-зареєстровано в бібліотеці (id=%s)",
                            sid, _reg.get('download_id'),
                        )
                    else:
                        logger.info(
                            "Finalize %s: вже в бібліотеці (id=%s) — пропуск",
                            sid, _reg.get('download_id'),
                        )
                except Exception as e:
                    logger.warning(
                        "Авто-реєстрація запису %s у бібліотеці не вдалась: %s", sid, e,
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

        recording_service = RecordingService(
            store=_rec_store,
            sse_broker=sse_broker,
            chunk_seconds=cfg.RECORDING_CHUNK_SECONDS,
            sample_rate=cfg.RECORDING_SAMPLE_RATE,
            channels=cfg.RECORDING_CHANNELS,
            finalize_callback=_recording_finalize_callback,
            video_supervisor_factory=_video_supervisor_factory,
        )
        _recovered = recording_service.recover_orphaned()
        if _recovered:
            logger.info(
                "Recording recovery: знайдено %d незавершених сесій: %s",
                len(_recovered), _recovered,
            )
        # Другий прохід: сесії, які вже позначені crashed, але так і не отримали
        # final.mp3 — тобто впав сам finalize-job (рестарт під час фіналізації).
        # Без цього кроку такий запис не видно НІДЕ: recover_orphaned дивиться
        # лише на активні статуси, reconcile — лише на фіналізовані, /save для
        # crashed віддає 500. PCM лежав на диску, поки хтось не поліз руками.
        # Обмежено MAX_FINALIZE_ATTEMPTS, щоб детерміновано битий запис не
        # ставив важкий job на кожному старті.
        _requeued = recording_service.recover_unfinalized()
        if _requeued:
            logger.warning(
                "Recording recovery: %d незавершен(их) сесій повторно "
                "поставлено на finalize: %s",
                len(_requeued), _requeued,
            )
        # Phase 9.10: зберігаємо для UI banner — UI'й endpoint забере і очистить
        recording_recovery_log = list(_recovered) + list(_requeued)

        # Boot-time reconcile: гарантуємо, що кожна фіналізована сесія з диска
        # присутня і коректна в Медіатеці (audio_downloads) — реєструємо сиріт,
        # лікуємо мертві file_path після переїзду проєкту, синхронізуємо назви.
        # НІКОЛИ не транскрибує (юзер вирішує сам у UI). Ідемпотентно → у daemon-
        # потоці, щоб disk-scan не блокував старт (той самий патерн, що прогрів
        # whisper вище). Двічі під reloader'ом — безпечно (усі кроки idempotent).
        def _run_recording_reconcile(_db=_rec_lib_db_path, _store=_rec_store):
            try:
                reconcile_recordings(_db, _store)
            except Exception as _e:
                logger.warning("Boot-time reconcile записів не вдався: %s", _e)
        threading.Thread(
            target=_run_recording_reconcile,
            name='recording-reconcile',
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning("Recording service не запустився: %s", e)
        recording_service = None
        recording_recovery_log = []
else:
    recording_service = None
    recording_recovery_log = []
    if cfg.HEADLESS:
        # config-registry-profiles S2: cfg.RECORDING_ENABLED уже форсовано False
        # у get_config() (config.py, Assumption 3) без спроби import pyaudiowpatch —
        # цей лог лише називає причину явно для headless-оператора.
        logger.info("profile=headless: recorder off")
    else:
        logger.info("Recording: вимкнено (RECORDING_ENABLED=False)")

# T2.1 (REMEDIATION_PLAN Волна 1): JobQueue persistence — той самий паттерн
# recovery, що й вище для recording (SessionStore.recover_orphaned): job'и
# зі станом queued/running з попереднього запуску не могли пережити
# рестарт/крах процесу (executor і потоки стартують заново) — тож БД
# позначає їх 'crashed', і вони одразу видимі через /api/jobs, а не мовчки
# зникають (задача, на завершення якої користувач чекав 2 години).
job_queue.set_db_path(app.config['DATABASE'])
_crashed_jobs = job_queue.recover_crashed()
if _crashed_jobs:
    logger.warning(
        "JobQueue recovery: %d задач(і) з попереднього запуску позначено crashed: %s",
        len(_crashed_jobs), _crashed_jobs,
    )
_state.робота['job_queue'] = {'bound': True, 'recovered': len(_crashed_jobs)}

# T4.6 (REMEDIATION_PLAN Волна 2): purge прострочених soft-deleted записів
# (grace-період RECALL_SOFTDELETE_GRACE_DAYS, дефолт 7д) — той самий
# старт-time патерн, що й jobs/recording recovery вище. Не періодичний фон-
# таск: одного проходу на рестарті достатньо, бо undo-вікно рахується у днях.
try:
    from app.services import retention as _retention
    _retention.purge_soft_deleted(app.config['DATABASE'])
except Exception as e:
    logger.warning("Soft-delete purge не виконано: %s", e)


# Phase 12.26 (Big Bet): Live transcribe worker — фоновий transcribe
# під час recording. Не блокує запис, працює окремим потоком.
live_transcribe_worker = None
if recording_service is not None:
    try:
        from app.services.live_transcribe import LiveTranscribeWorker
        live_transcribe_worker = LiveTranscribeWorker(
            whisper_manager=whisper_manager,
            broker=sse_broker,
            model_name='small',  # компроміс швидкість/якість
            interval=8.0,
        )
        logger.info("LiveTranscribeWorker готовий (model=small, interval=8s)")
    except Exception as e:
        logger.warning("LiveTranscribeWorker не запустився: %s", e)
        live_transcribe_worker = None

# Phase 19 (Co-pilot): живий ко-пілот дзвінка. Сервіс — лише persist у
# copilot_sessions (без Ollama-залежності на Кроці 1); live-аналіз (топіки/
# диспетчер/ескалація) — окремий CopilotWorker у наступних кроках. Гейтиться
# cfg.COPILOT_ENABLED. Доступність локального LLM перевіряється у рантаймі
# (local_llm.is_available), не тут.
copilot_service = None
if getattr(cfg, 'COPILOT_ENABLED', False):
    try:
        from app.services.copilot.service import CopilotService
        copilot_service = CopilotService(db_path=app.config['DATABASE'])
        logger.info("CopilotService готовий (model_local=%s)",
                    getattr(cfg, 'LOCAL_LLM_MODEL', '?'))
    except Exception as e:
        logger.warning("CopilotService не запустився: %s", e)
        copilot_service = None
else:
    logger.info("Co-pilot: вимкнено (COPILOT_ENABLED=False)")

# Phase 19 (Co-pilot, Крок 2): CopilotWorker — топік-трекінг на ембеддингах під
# час запису. Потребує live-воркера (джерело тексту) + локальні ембеддинги (e5).
# М'яко: нема одного з них → не стартує, запис/сесія працюють як раніше.
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
        logger.info("CopilotWorker готовий (топік-трекінг + диспетчер-LLM)")
    except Exception as e:
        logger.warning("CopilotWorker не запустився: %s", e)
        copilot_worker = None

# Phase 5.5: bind module-level singletons для blueprints.
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

# Phase 12.14: Security headers — applied to всі responses
# (включно з blueprint endpoints). Дозволяємо сторонні CDN'и які реально
# використовуються у templates (Google Fonts, cdnjs FontAwesome,
# unpkg для popper/tippy, YouTube для embed/thumbnails).
_CSP_POLICY = (
    "default-src 'self'; "
    # inline styles потрібні для chip кольорів та рамок (динамічні).
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
    "https://cdnjs.cloudflare.com https://unpkg.com; "
    # inline scripts: onclick handlers у kinds modals.
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://www.youtube.com; "
    "img-src 'self' data: blob: https://i.ytimg.com https://yt3.ggpht.com "
    "https://*.youtube.com; "
    "media-src 'self' blob:; "
    "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
    # unpkg — щоб браузер міг тягнути sourcemap'и popper/tippy (інакше шумить у консолі).
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
    # CSP — підтримуємо headers тільки для HTML, а не для JSON/files endpoint'ів
    # (щоб не блокувати fetch у API-only клієнтах).
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

# T1.2 (Волна 1): єдиний before_request-гейт аутентифікації на всі /api/*
# (крім /api/health). Централізована точка замість декоратора на кожен з
# ~90 ендпоінтів — неможливо забути захистити новий. Деталі рішення
# (localhost+trusted / RECALL_API_KEY / fail-closed) — app/core/auth.py.
from app.core import auth as _auth
_auth.register(app)

# T7.4 (Волна 4): єдиний catch-all error-handler на необроблені виключення —
# traceback у лог (з request_id через _RequestIdLogFilter вище), клієнту
# узагальнене повідомлення + request_id, БЕЗ internal message (str(exc)).
# Реєструється ПІСЛЯ auth-гейта і register_blueprint, але порядок реєстрації
# errorhandler'ів у Flask не залежить від порядку before_request/blueprint —
# @app.errorhandler(429)/(413) вище лишаються пріоритетнішими для своїх кодів.
from app.core import error_handlers as _error_handlers
_error_handlers.register(app)

# Rate limits на blueprint-функціях. Підв'язуємо ПІСЛЯ register_blueprint.
limiter.limit(lambda: RATE_LIMIT_YOUTUBE)(app.view_functions['youtube.download_youtube'])
limiter.limit(lambda: RATE_LIMIT_YOUTUBE)(app.view_functions['audio_library.download_audio_only'])
limiter.limit(lambda: RATE_LIMIT_TRANSCRIBE)(app.view_functions['transcription.transcribe'])
# Phase 16A: підвантаження документів — той самий ліміт, що транскрибація.
if 'documents.upload_document' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_TRANSCRIBE)(app.view_functions['documents.upload_document'])
limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.polish_transcription'])
# Phase 12.15: Claude API endpoints (summarize/translate) — той же ліміт що polish.
if 'transcription.summarize_transcription' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.summarize_transcription'])
if 'transcription.translate_transcription' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.translate_transcription'])
if 'transcription.sentiment_transcription' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.sentiment_transcription'])
if 'transcription.extract_topics_endpoint' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['transcription.extract_topics_endpoint'])
# Phase 12.15: дорогі server-side ops (merge speakers, segment edits).
# Висока частота не очікується — strict ліміт.
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
# Phase 9.10 hardening: limit на recording.start_recording. Pause/resume/
# stop/state не лімітуємо — це continuation activity для існуючої сесії.
if 'recording.start_recording' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_RECORDING_START)(app.view_functions['recording.start_recording'])
# T5.1: settings_api.test_anthropic_key виконує реальний Claude-виклик (Haiku,
# дешево, але не безлімітно) — той же ліміт що polish. save/toggle/status —
# лише запис у .env / читання os.environ, без зовнішніх викликів — MUTATIONS
# достатньо, щоб не дати спамити диск-запис.
if 'settings_api.test_anthropic_key' in app.view_functions:
    limiter.limit(lambda: RATE_LIMIT_POLISH)(app.view_functions['settings_api.test_anthropic_key'])
for view_name in ['settings_api.save_anthropic_key', 'settings_api.clear_anthropic_key', 'settings_api.toggle_copilot']:
    if view_name in app.view_functions:
        limiter.limit(lambda: RATE_LIMIT_MUTATIONS)(app.view_functions[view_name])


def _maybe_launch_telegram_listener():
    """Phase 17: ЄДИНА точка запуску — app.py піднімає слухача Telegram як
    КЕРОВАНИЙ дочірній процес (один `python app.py` = і веб, і слухач).

    Слухач лишається ОКРЕМИМ процесом (не потік): Telethon має власний
    asyncio-loop + ексклюзивну сесію (SQLite не відкрити двома клієнтами), а
    Flask-reloader рвав би in-process конект. Тож «єдина точка» = app керує
    життєвим циклом subprocess, а не зливає їх в один процес.

    Запуск лише якщо: TELEGRAM_ENABLED (telethon+ключі) і вже існує файл сесії
    (інакше інтерактивний логін завис би у subprocess → спершу telegram_login.py).
    stdout/stderr успадковуються → логи слухача в тій самій консолі."""
    if not getattr(cfg, 'TELEGRAM_ENABLED', False):
        logger.info("Telegram: вимкнено (немає ключів або telethon) — слухача не запускаю")
        _state.робота['telegram'] = 'disabled'
        return
    session_file = str(getattr(cfg, 'TELEGRAM_SESSION', 'telegram')) + '.session'
    if not os.path.exists(session_file):
        logger.warning("Telegram: немає сесії (%s). Спершу одноразово виконайте: "
                       "python telegram_login.py", session_file)
        _state.робота['telegram'] = 'absent'
        return
    _state.робота['telegram'] = 'starting'
    try:
        proc = subprocess.Popen([sys.executable, 'telegram_listener.py'],
                                cwd=str(getattr(cfg, 'BASE_DIR', '.')))
    except Exception as e:
        logger.error("Telegram: не вдалось запустити слухача: %s", e)
        _state.робота['telegram'] = 'absent'
        return
    logger.info("Telegram: слухач запущено як дочірній процес (pid=%s)", proc.pid)
    _state.робота['telegram'] = 'running'

    def _stop_listener():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    atexit.register(_stop_listener)


# config-registry-profiles S2: boot-послідовність модульного рівня завершена
# (БД/міграції/JobQueue/recorder-гейт/лічильники/blueprints/auth/error-handler
# усі вище). Telegram-слухач запускається нижче лише під `__main__` (reloader-
# гейт, коментар _maybe_launch_telegram_listener) — робота['telegram']
# оновлюється тим викликом незалежно від цього прапорця. /api/ready (story 03)
# читає boot_finished як «модуль app.py повністю зібрано», а не «усі опційні
# дочірні процеси вже стартували».
_state.робота['boot_finished'] = True

if __name__ == '__main__':
    logger.info("Whisper UI запущено!")
    logger.info(f"Відкрийте http://localhost:{cfg.PORT} у браузері")
    if not ffmpeg_available:
        logger.warning("FFmpeg не встановлено - YouTube функції будуть недоступні")

    debug_mode = bool(cfg.DEBUG)  # єдина точка правди — config.py (env FLASK_DEBUG)

    # Bind-адреса: безпечний дефолт 127.0.0.1 (тільки локальна машина). Вихід
    # на весь LAN — лише явним opt-in (RECALL_BIND_ALL=1 або FLASK_HOST=0.0.0.0),
    # бо застосунок поки без аутентифікації (див. docs/REMEDIATION_PLAN.md T1.2).
    bind_all = _settings.env_bool('RECALL_BIND_ALL')
    host = '0.0.0.0' if (bind_all or cfg.HOST == '0.0.0.0') else cfg.HOST
    if host == '0.0.0.0':
        logger.warning(
            "RECALL_BIND_ALL/FLASK_HOST=0.0.0.0: застосунок доступний по мережі "
            "БЕЗ аутентифікації (див. T1.2 у docs/REMEDIATION_PLAN.md) — "
            "вмикайте лише в довіреній LAN."
        )

    # Слухача Telegram піднімаємо ОДИН раз. Під reloader'ом (debug) серверний
    # child має WERKZEUG_RUN_MAIN='true' — пропускаємо його, щоб не плодити
    # дублі при кожному перезавантаженні; стабільний watcher-процес запускає.
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        _maybe_launch_telegram_listener()
    # threaded=True для SSE: каждое соединение в своём потоке, не блокирует другие.
    app.run(debug=debug_mode, host=host, port=cfg.PORT, threaded=True)