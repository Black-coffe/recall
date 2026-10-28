# File: WhisperUI/config.py
# Path: WhisperUI/config.py
# Description: Centralized application configuration

"""
Configuration for Whisper UI
"""

import logging
import os
import secrets
import threading
from pathlib import Path

from app.core import settings

_logger = logging.getLogger(__name__)

# Load .env BEFORE the class is defined: some settings (TELEGRAM_API_ID/
# HASH/ENABLED, etc.) are read from os.environ at class definition time. If
# .env were loaded later (as in app.py, where `import config` comes before load_dotenv),
# these values would stay empty → e.g. TELEGRAM_ENABLED=False despite keys in .env.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / '.env')
except ImportError:
    pass


class Config:
    """Base configuration"""

    # Main paths
    BASE_DIR = Path(__file__).parent
    UPLOAD_FOLDER = 'uploads'
    TRANSCRIPTS_FOLDER = 'transcripts'
    YOUTUBE_FOLDER = 'youtube_downloads'
    MODELS_FOLDER = 'models'
    # Phase 16A: originals of uploaded documents (PDF/DOCX/MD/…).
    DOCUMENTS_FOLDER = 'documents'

    # Database
    DATABASE = 'whisper_history.db'
    DATABASE_BACKUP_FOLDER = 'db_backups'

    # Maximum sizes
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024 * 1024  # 20GB for video files
    MAX_YOUTUBE_DURATION = 3 * 60 * 60      # 3 hours maximum for YouTube

    # Supported formats
    ALLOWED_EXTENSIONS = {
        'mp3', 'mp4', 'mpeg', 'mpga', 'm4a',
        'wav', 'webm', 'ogg', 'flac',
        'avi', 'mov', 'mkv', 'wmv'
    }

    # Whisper models — canonical source; whisper_manager_new.py has its own
    # models_info dict that should eventually be replaced with this one.
    WHISPER_MODELS = {
        'tiny': {
            'size': '39 MB',
            'params': '39M',
            'speed': '~32x',
            'vram': '1 GB'
        },
        'base': {
            'size': '74 MB',
            'params': '74M',
            'speed': '~16x',
            'vram': '1 GB'
        },
        'small': {
            'size': '244 MB',
            'params': '244M',
            'speed': '~6x',
            'vram': '2 GB'
        },
        'medium': {
            'size': '769 MB',
            'params': '769M',
            'speed': '~2x',
            'vram': '5 GB'
        },
        'large': {
            'size': '1550 MB',
            'params': '1550M',
            'speed': '~1x',
            'vram': '10 GB'
        }
    }

    # Languages
    LANGUAGES = {
        'uk': 'Українська',
        'en': 'English',
        'ru': 'Русский',
        'es': 'Español',
        'fr': 'Français',
        'de': 'Deutsch',
        'it': 'Italiano',
        'pt': 'Português',
        'pl': 'Polski',
        'tr': 'Türkçe',
        'ja': '日本語',
        'ko': '한국어',
        'zh': '中文',
        'auto': 'Автовизначення'
    }

    # YouTube settings
    YOUTUBE_DL_OPTIONS = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_color': True,
        'socket_timeout': 30,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 10,
    }

    # Server settings (defaults from the registry app/core/settings.py)
    DEBUG = settings.env_bool('FLASK_DEBUG')
    HOST = settings.env('FLASK_HOST')
    PORT = settings.env_int('FLASK_PORT')

    # Security. The default stays as a fallback for DEBUG=True (dev). The only
    # check for "default is unacceptable in a production-like mode" is a hard-fail
    # LAZILY on the first get_config() (config-registry-profiles S3; formerly — here
    # in the module, T1.5), next to the choice of current_config. DO NOT duplicate this
    # check elsewhere (there used to be 2 independent mechanisms — ProductionConfig
    # RuntimeError property + app.py warning — which triggered differently).
    SECRET_KEY = settings.env('SECRET_KEY')

    # Profile (config-registry-profiles S3): 'desktop' (default) or 'headless'.
    # The real value is set lazily in get_config() (RECALL_PROFILE
    # is read only there, along with the rest of the side effects) — here only a safe
    # placeholder so the attribute exists right after `import config`.
    PROFILE = 'desktop'
    HEADLESS = False

    # Rate limits (RATE_LIMIT_*) and file auto-cleanup (AUTO_CLEANUP_*) are read from the
    # settings registry in app.py, not from here.

    # GPU settings
    FORCE_CPU = settings.env_bool('FORCE_CPU')

    # Whisper backend (Phase 1 v4.0 roadmap)
    # 'faster' — faster-whisper (CTranslate2), 3-5x faster on GPU. Default.
    # 'openai' — reference openai-whisper, fallback.
    WHISPER_BACKEND = settings.env('WHISPER_BACKEND').lower()
    # Parallel transcriptions via a semaphore. On an RTX 3090 (24GB):
    # tiny/base/small=4, medium=2, large=1.
    WHISPER_MAX_PARALLEL = settings.env_int('WHISPER_MAX_PARALLEL')
    # batch_size for BatchedInferencePipeline on long files (faster backend)
    WHISPER_BATCH_SIZE = settings.env_int('WHISPER_BATCH_SIZE')

    # Logging
    LOG_LEVEL = settings.env('LOG_LEVEL')
    LOG_FILE = 'whisper_ui.log'
    LOG_MAX_SIZE = 10 * 1024 * 1024  # 10MB
    LOG_BACKUP_COUNT = 5

    # Phase 9: System Audio Recording (WASAPI loopback + mic).
    # Sessions are written to RECORDING_DIR as raw PCM with an atomic manifest.json
    # for crash resilience. Finalization (PCM→WAV→mix→MP3) runs
    # on stop or on recovery after a crash.
    #
    # RECORDING_ENABLED is autodetected via the pyaudiowpatch import.
    # It can be forced off with an explicit env var value RECORDING_ENABLED
    # (any falsy value for settings.env_bool — '0'/'false'/'no'/'off'/...).
    #
    # Called LAZILY from get_config() (config-registry-profiles S3), NOT at
    # class definition time — importing pyaudiowpatch is no longer a side effect of `import
    # config`. Not called at all in the headless profile (RECORDING_ENABLED
    # is forced to False without trying the import, Assumption 3).
    @staticmethod
    def _detect_recording_enabled():
        # The registry default is an empty string (autodetect). An explicit value (any,
        # not only falsy) overrides autodetect and is read VIA
        # settings.env_bool (config-registry-fix-01) — no parser of its own.
        if settings.env('RECORDING_ENABLED').strip() and not settings.env_bool('RECORDING_ENABLED'):
            return False
        try:
            import pyaudiowpatch  # noqa: F401
            return True
        except ImportError:
            return False

    # Placeholder until the first get_config() (lazy initialization further down in the module).
    RECORDING_ENABLED = False
    RECORDING_DIR = BASE_DIR / 'recordings' / 'sessions'
    RECORDING_CHUNK_SECONDS = settings.env_int('RECORDING_CHUNK_SECONDS')
    # ADR-026: how many recordings at once (minimum 1).
    RECORDING_MAX_ACTIVE = max(1, settings.env_int('RECORDING_MAX_ACTIVE'))
    RECORDING_SAMPLE_RATE = settings.env_int('RECORDING_SAMPLE_RATE')
    RECORDING_CHANNELS = settings.env_int('RECORDING_CHANNELS')
    RECORDING_MP3_BITRATE = settings.env('RECORDING_MP3_BITRATE')
    RECORDING_KEEP_PCM = settings.env_bool('RECORDING_KEEP_PCM')
    # The intermediate mic.wav/system.wav are only a step of the mixdown into final.mp3, nothing reads them
    # after finalize. By default we delete them (otherwise ~1 GB/session
    # of dead space). RECORDING_KEEP_WAV=true — keep the separate tracks.
    RECORDING_KEEP_WAV = settings.env_bool('RECORDING_KEEP_WAV')
    RECORDING_MIN_DISK_MB = settings.env_int('RECORDING_MIN_DISK_MB')

    # Phase 22: Screen video capture (ddagrab → NVENC, isolated subsystem).
    # Turn off via RECORDING_VIDEO_ENABLED=False if there is no NVIDIA GPU.
    # In headless it is forced to False in get_config() (Assumption 3) — here the default
    # for the desktop profile.
    RECORDING_VIDEO_ENABLED = settings.env_bool('RECORDING_VIDEO_ENABLED')
    RECORDING_FFMPEG_PATH = settings.env('RECORDING_FFMPEG_PATH')
    RECORDING_VIDEO_FPS = settings.env_int('RECORDING_VIDEO_FPS')
    RECORDING_VIDEO_CODEC = settings.env('RECORDING_VIDEO_CODEC')
    RECORDING_VIDEO_QUALITY = settings.env('RECORDING_VIDEO_QUALITY')   # nvenc -preset
    RECORDING_VIDEO_CQ = settings.env_int('RECORDING_VIDEO_CQ')
    RECORDING_VIDEO_MAX_TRACKS = settings.env_int('RECORDING_VIDEO_MAX_TRACKS')
    RECORDING_VIDEO_STOP_TIMEOUT_SEC = settings.env_float('RECORDING_VIDEO_STOP_TIMEOUT_SEC')
    RECORDING_BUILD_MASTER_MP4 = settings.env_bool('RECORDING_BUILD_MASTER_MP4')
    RECORDING_REGION_OVERLAY = settings.env_bool('RECORDING_REGION_OVERLAY')
    RECORDING_REGION_OVERLAY_COLOR = settings.env('RECORDING_REGION_OVERLAY_COLOR')
    # Phase 23B-A: Video understanding — scene-keyframe OCR → RAG ingestion.
    RECORDING_VIDEO_ANALYSIS_MAX_FRAMES = settings.env_int('RECORDING_VIDEO_ANALYSIS_MAX_FRAMES')
    # Phase 23B: Vision description of frames ("what is shown on the screen") → into RAG next to OCR.
    # Backend: 'local' (Ollama VL model, $0/offline, default), 'claude' (Anthropic
    # vision API, paid, more accurate), 'off'. Everything degrades: no model/key →
    # the description is empty, frames+OCR work as before. These constants are duplicated by
    # video_vision.py (reads env directly) to stay standalone-testable.
    VIDEO_VISION_BACKEND = settings.env('VIDEO_VISION_BACKEND').strip().lower()
    VIDEO_VISION_MODEL_LOCAL = settings.env('VIDEO_VISION_MODEL_LOCAL')
    VIDEO_VISION_MODEL_CLAUDE = settings.env('VIDEO_VISION_MODEL_CLAUDE')
    VIDEO_VISION_MAX_FRAMES = settings.env_int('VIDEO_VISION_MAX_FRAMES')
    VIDEO_VISION_TIMEOUT = settings.env_float('VIDEO_VISION_TIMEOUT')

    # Phase 17: Telegram ingestion — listening to a real TG ACCOUNT (MTProto/Telethon).
    # api_id/api_hash come from my.telegram.org → .env (SECRETS, not in code).
    # The listener is a SEPARATE process (telegram_listener.py): it alone holds the session
    # (the SQLite file telegram.session cannot be opened by two clients), and Flask
    # talks to it over the localhost control API. TELEGRAM_ENABLED is autodetected:
    # both the keys and an installed telethon are needed — otherwise the feature is softly disabled.
    # int(... or '0'): TELEGRAM_API_ID= (empty) in .env is a separate case from
    # "variable absent" (settings.env would still return the default only when the
    # key is absent), so we keep the explicit or-fallback.
    TELEGRAM_API_ID = int(settings.env('TELEGRAM_API_ID') or '0')
    TELEGRAM_API_HASH = settings.env('TELEGRAM_API_HASH')
    # Path to the Telethon session file (Telethon appends .session if there is no extension).
    # config-registry-fix-01: this line used to read os.environ directly with a
    # SECOND, independently hardcoded default str(BASE_DIR / 'telegram') —
    # the registry Setting.default ('telegram') was ignored entirely, .env.example
    # printed a value the code never meant to use. Now the single
    # source of the name string is the registry (settings.env); the BASE_DIR join
    # stays (an absolute path independent of CWD — tests/test_headless_boot.py::config-registry-fix-02
    # is deliberately built on this, do not touch
    # this property). If TELEGRAM_SESSION is given as an ABSOLUTE path,
    # the BASE_DIR join does not change it (pathlib: a right-hand absolute operand
    # replaces the left one entirely).
    TELEGRAM_SESSION = str(BASE_DIR / settings.env('TELEGRAM_SESSION'))
    # Where the listener stores downloaded media (photo/video/voice/documents) before
    # routing into the pipeline. Not committed (.gitignore telegram_media/).
    TELEGRAM_MEDIA_DIR = BASE_DIR / 'telegram_media'
    # localhost control API of the listener (Flask proxies list-dialogs/status/backfill here).
    TELEGRAM_CONTROL_HOST = settings.env('TELEGRAM_CONTROL_HOST')
    TELEGRAM_CONTROL_PORT = settings.env_int('TELEGRAM_CONTROL_PORT')
    # Transcription of voice/audio/video from TG. large-v3 had the best accuracy in the
    # whisper-best-default measurement (uk WER 8.2 %, turbo 8.7 % at ~2.4x the speed).
    # Voice messages are usually short. Language: 'uk' by default, for UA/RU chats one can use
    # 'auto' (autodetect) or a specific language.
    TELEGRAM_WHISPER_MODEL = settings.env('TELEGRAM_WHISPER_MODEL')
    TELEGRAM_WHISPER_LANG = settings.env('TELEGRAM_WHISPER_LANG')

    # Called LAZILY from get_config() (config-registry-profiles S3), NOT at
    # class definition time — importing telethon is no longer a side effect of `import config`.
    # os.environ.get(...) directly (not settings.env): we check for the ABSENCE of the
    # key (None), not the default value — settings.env would return a
    # non-empty default string even when the variable is absent.
    @staticmethod
    def _detect_telegram_enabled():
        if not (os.environ.get('TELEGRAM_API_ID') and os.environ.get('TELEGRAM_API_HASH')):
            return False
        try:
            import telethon  # noqa: F401
            return True
        except ImportError:
            return False

    # Placeholder until the first get_config() (lazy initialization further down in the module).
    TELEGRAM_ENABLED = False

    # Phase 19 (Co-pilot, Step 0): local LLM dispatcher for the live co-pilot.
    # Ollama is a SEPARATE system service (localhost HTTP), like telegram_listener is
    # a separate process. We only talk to it over HTTP; `ollama serve` is brought up by the
    # user/OS. Everything is optional and degrades: no Ollama/model →
    # local_llm.is_available()=False, the co-pilot softly turns off, recording/STT/RAG
    # work as before. The real gate is a runtime ping in local_llm.py (not an import),
    # because availability depends on an external process. These constants are duplicated by
    # local_llm.py (reads the same env directly) so the service stays standalone-
    # testable without a Flask context (like embeddings.py with EMBED_MODEL).
    # The default-true is expressed in the registry (default="1"), not by an inverted falsy
    # check at the call site (config-registry-fix-01) — settings.env_bool is
    # the single boolean parser.
    COPILOT_ENABLED = settings.env_bool('COPILOT_ENABLED')
    LOCAL_LLM_URL = settings.env('LOCAL_LLM_URL')
    LOCAL_LLM_MODEL = settings.env('LOCAL_LLM_MODEL')
    LOCAL_LLM_KEEPALIVE = settings.env('LOCAL_LLM_KEEPALIVE')
    LOCAL_LLM_NUM_CTX = settings.env_int('LOCAL_LLM_NUM_CTX')
    LOCAL_LLM_TIMEOUT = settings.env_float('LOCAL_LLM_TIMEOUT')

    # T6.4 (Wave 4): optional local cross-encoder rerank (bge-reranker-v2-m3)
    # over the top candidates of retrieval.search(). OFF by default — turned on
    # ONLY for the RAG chat "Ask the archive" (app/services/rag.py), not for copilot/
    # categorize/MCP. The value here is informational/for visibility in the config; rag.py
    # itself reads env DIRECTLY (the same pattern as COPILOT_ENABLED/
    # embeddings.py — standalone-testable without a Flask context).
    RECALL_RERANK_ENABLED = settings.env_bool('RECALL_RERANK_ENABLED')


class DevelopmentConfig(Config):
    """Configuration for development"""
    # DEBUG is inherited from Config (controlled by env FLASK_DEBUG, default False) —
    # a single source of truth, so as not to diverge from app.py.
    LOG_LEVEL = 'DEBUG'


class ProductionConfig(Config):
    """Configuration for production"""
    DEBUG = False
    LOG_LEVEL = 'WARNING'

    # SECRET_KEY is inherited from Config. An insecure default for DEBUG=False
    # (including ProductionConfig, always here) is caught by the SINGLE check below
    # in the module (T1.5) — do not duplicate it here with a separate property-RuntimeError.

    # Disable insecure options
    YOUTUBE_DL_OPTIONS = {
        **Config.YOUTUBE_DL_OPTIONS,
        'nocheckcertificate': False,
    }


class TestingConfig(Config):
    """Configuration for testing"""
    TESTING = True
    DATABASE = 'test_whisper_history.db'

    # Smaller limits for tests
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB
    MAX_YOUTUBE_DURATION = 5 * 60  # 5 minutes


# Choice of configuration based on an environment variable
config_name = os.environ.get('APP_ENV', 'development')
config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig
}

# Current configuration
current_config = config_map.get(config_name, DevelopmentConfig)()


# --- T1.5: a single SECRET_KEY check, hard-fail instead of a warning ---
# There used to be two independent mechanisms that triggered differently:
#   1) ProductionConfig.SECRET_KEY — a property that raised RuntimeError, but ONLY
#      if APP_ENV=production (the ProductionConfig class was chosen);
#   2) app.py — logger.warning (does not block startup!), and only if
#      FLASK_ENV=='production' or APP_ENV=='production'.
# Consequence: DevelopmentConfig with DEBUG=False (e.g. someone just forgot to set
# FLASK_DEBUG=true and left APP_ENV at the default/non-production) was caught by
# neither — the default SECRET_KEY quietly went into a production-like launch.
#
# Now the check goes by the EFFECTIVE DEBUG (consistent with how Wave 0
# made DEBUG: default False, read from cfg), not by the APP_ENV/
# FLASK_ENV string. DEBUG=False + default/empty key → always hard-fail,
# regardless of which configuration class is chosen. DEBUG=True (dev) —
# the default is allowed.
_INSECURE_SECRET_KEYS = {
    '',
    'dev-secret-key-change-in-production',  # Config default
    'change-me-in-production',              # placeholder from .env.example
}
# Owner decision (03.07.2026): do not put an "onboarding wall" of hard-fail on
# every local launch (after Wave 0 DEBUG=False became the default, so a strict
# hard-fail would trigger on "clone & run"). Instead:
#   - network bind (RECALL_BIND_ALL / FLASK_HOST=0.0.0.0) + insecure key →
#     hard-fail (a real risk of external session forgery, a conscious step is required);
#   - local mode + insecure key → auto-generate a random key and
#     (best-effort) save it to .env so it survives a restart. DEBUG=True — default is fine.
def _network_exposed() -> bool:
    """Whether the app will listen beyond localhost (`RECALL_BIND_ALL` through
    the single parser `settings.env_bool`, like `app.py`)."""
    if settings.env_bool('RECALL_BIND_ALL'):
        return True
    return current_config.HOST == '0.0.0.0'


def _persist_secret_key(key: str) -> bool:
    """Best-effort: save SECRET_KEY to the repo-root .env through the locked writer.

    Not critical if it fails (read-only FS / a line already SECRET_KEY=): the key
    still works in the memory of this process. An existing SECRET_KEY line (e.g. a
    placeholder) is never rewritten.
    """
    from app.core import env_file
    try:
        if env_file.ENV_PATH.exists():
            with open(env_file.ENV_PATH, 'r', encoding='utf-8') as f:
                if 'SECRET_KEY=' in f.read():
                    return False
        env_file.set_env_var('SECRET_KEY', key)
        return True
    except OSError:
        return False


def _finalize_secret_key() -> None:
    """T1.5: SECRET_KEY hard-fail/autogen. Called LAZILY from get_config()
    (config-registry-profiles S3) — it used to be module-level code, so it
    ran already on `import config`.

    config-registry-fix-01 (finding 2): the network-bind hard-fail is checked
    FIRST, regardless of DEBUG. Previously the condition `not current_config.DEBUG` stood
    outside and skipped the hard-fail entirely when DEBUG=True — and the widened
    truthy set of settings.env_bool ('1'/'true'/'yes'/'on') makes
    FLASK_DEBUG=1 (formerly giving DEBUG=False via .lower()=='true') now
    DEBUG=True. The semantics of the owner decision do not change (locally —
    autogen, only a network bind — hard-fail); the bug itself is fixed, where
    DEBUG=True could bypass the hard-fail even with network access."""
    if current_config.SECRET_KEY not in _INSECURE_SECRET_KEYS:
        return
    if _network_exposed():
        raise RuntimeError(
            "Insecure SECRET_KEY with network access: RECALL_BIND_ALL or "
            "FLASK_HOST=0.0.0.0 together with a default/empty SECRET_KEY = a risk "
            "of external session forgery. Set a random SECRET_KEY in .env:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"\n'
            "and add SECRET_KEY=<result> to .env."
        )
    if current_config.DEBUG:
        # Local dev launch, not networked — the default key is acceptable.
        return
    _generated_key = secrets.token_hex(32)
    current_config.SECRET_KEY = _generated_key
    _saved = _persist_secret_key(_generated_key)
    _logger.warning(
        "SECRET_KEY was not set — a new one was generated%s. "
        "Network access (RECALL_BIND_ALL/FLASK_HOST=0.0.0.0) will require an explicit key.",
        " and saved to .env" if _saved else " (in memory only — saving to .env failed)",
    )


_config_initialized = False
_config_lock = threading.Lock()


def _initialize_dynamic_config() -> None:
    """Side effects that used to run on `import config` (heavy
    imports of pyaudiowpatch/telethon, SECRET_KEY hard-fail/autogen) —
    config-registry-profiles S3 moves them here, into LAZY initialization
    on the first call of get_config()."""
    profile_name = settings.profile()
    current_config.PROFILE = profile_name
    current_config.HEADLESS = profile_name == 'headless'

    if current_config.HEADLESS:
        # headless: do not bring up the recorder — and do NOT try to import
        # pyaudiowpatch at all (Assumption 3, config-registry-profiles).
        current_config.RECORDING_ENABLED = False
        current_config.RECORDING_VIDEO_ENABLED = False
    else:
        current_config.RECORDING_ENABLED = Config._detect_recording_enabled()

    current_config.TELEGRAM_ENABLED = Config._detect_telegram_enabled()

    _finalize_secret_key()


def get_config():
    """Get the current configuration.

    Side effects (recorder/telegram autodetect, SECRET_KEY hard-fail/
    autogen, profile) run LAZILY here, on the first call — NOT on
    `import config` (config-registry-profiles S3). Guarded by `_config_lock`
    (config-registry-fix-01, finding 15): without the lock two threads
    racing for the first call could both run
    `_initialize_dynamic_config()` and both append to `.env`."""
    global _config_initialized
    if not _config_initialized:
        with _config_lock:
            if not _config_initialized:
                _initialize_dynamic_config()
                _config_initialized = True
    return current_config


def update_config(**kwargs):
    """Update configuration parameters"""
    for key, value in kwargs.items():
        if hasattr(current_config, key):
            setattr(current_config, key, value)


def init_directories(config=None):
    """Create the required directories. Call explicitly at application startup."""
    cfg = config or current_config
    for folder in [
        cfg.UPLOAD_FOLDER,
        cfg.TRANSCRIPTS_FOLDER,
        cfg.YOUTUBE_FOLDER,
        cfg.MODELS_FOLDER,
        cfg.DATABASE_BACKUP_FOLDER
    ]:
        os.makedirs(folder, exist_ok=True)
    # Phase 9: recordings dir for WASAPI recording.
    if getattr(cfg, 'RECORDING_ENABLED', False):
        os.makedirs(cfg.RECORDING_DIR, exist_ok=True)