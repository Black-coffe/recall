# Файл: WhisperUI/config.py
# Путь: WhisperUI/config.py
# Описание: Централизованная конфигурация приложения

"""
Конфигурация для Whisper UI
"""

import logging
import os
import secrets
from pathlib import Path

_logger = logging.getLogger(__name__)

# Завантажуємо .env ДО визначення класу: частина налаштувань (TELEGRAM_API_ID/
# HASH/ENABLED, та ін.) читається з os.environ у момент визначення класу. Якщо
# .env вантажиться пізніше (як в app.py, де import config іде до load_dotenv),
# ці значення лишилися б порожні → напр. TELEGRAM_ENABLED=False попри ключі в .env.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    """Базовая конфигурация"""

    # Основные пути
    BASE_DIR = Path(__file__).parent
    UPLOAD_FOLDER = 'uploads'
    TRANSCRIPTS_FOLDER = 'transcripts'
    YOUTUBE_FOLDER = 'youtube_downloads'
    MODELS_FOLDER = 'models'
    # Phase 16A: оригінали підвантажених документів (PDF/DOCX/MD/…).
    DOCUMENTS_FOLDER = 'documents'

    # База данных
    DATABASE = 'whisper_history.db'
    DATABASE_BACKUP_FOLDER = 'db_backups'

    # Максимальные размеры
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024 * 1024  # 20GB для видео файлов
    MAX_YOUTUBE_DURATION = 3 * 60 * 60      # 3 часа максимум для YouTube

    # Поддерживаемые форматы
    ALLOWED_EXTENSIONS = {
        'mp3', 'mp4', 'mpeg', 'mpga', 'm4a',
        'wav', 'webm', 'ogg', 'flac',
        'avi', 'mov', 'mkv', 'wmv'
    }

    # Модели Whisper — canonical source; whisper_manager_new.py has its own
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

    # Языки
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

    # YouTube настройки
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

    # Настройки сервера
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    HOST = os.environ.get('FLASK_HOST', '127.0.0.1')
    PORT = int(os.environ.get('FLASK_PORT', 5050))

    # Безопасность. Дефолт лишається як fallback для DEBUG=True (dev). Єдина
    # перевірка «дефолт неприпустимий у production-подібному режимі» — hard-fail
    # нижче в модулі (T1.5), біля вибору current_config. НЕ дублювати цю
    # перевірку деінде (раніше було 2 незалежні механізми — ProductionConfig
    # RuntimeError-property + app.py warning — які тригерились по-різному).
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

    # Лимиты
    RATE_LIMIT_YOUTUBE = 10  # Максимум YouTube загрузок в час  # TODO: not yet implemented
    RATE_LIMIT_TRANSCRIBE = 30  # Максимум транскрипций в час  # TODO: not yet implemented

    # Автоочистка
    AUTO_CLEANUP_DAYS = 7  # Удалять временные файлы старше N дней  # TODO: not yet implemented
    AUTO_CLEANUP_ENABLED = True  # TODO: not yet implemented

    # GPU настройки
    FORCE_CPU = os.environ.get('FORCE_CPU', 'False').lower() == 'true'
    GPU_BATCH_SIZE = 16  # Размер батча для GPU  # TODO: not yet implemented

    # Whisper backend (Phase 1 v4.0 roadmap)
    # 'faster' — faster-whisper (CTranslate2), 3-5x быстрее на GPU. Default.
    # 'openai' — референсный openai-whisper, fallback.
    WHISPER_BACKEND = os.environ.get('WHISPER_BACKEND', 'faster').lower()
    # Параллельные транскрипции через семафор. На RTX 3090 (24GB):
    # tiny/base/small=4, medium=2, large=1.
    WHISPER_MAX_PARALLEL = int(os.environ.get('WHISPER_MAX_PARALLEL', '2'))
    # batch_size для BatchedInferencePipeline на длинных файлах (faster backend)
    WHISPER_BATCH_SIZE = int(os.environ.get('WHISPER_BATCH_SIZE', '8'))

    # Логирование
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_FILE = 'whisper_ui.log'
    LOG_MAX_SIZE = 10 * 1024 * 1024  # 10MB
    LOG_BACKUP_COUNT = 5

    # Phase 9: System Audio Recording (WASAPI loopback + mic).
    # Сесії пишуться в RECORDING_DIR як raw PCM з atomic manifest.json
    # для crash-resilience. Фіналізація (PCM→WAV→mix→MP3) запускається
    # на стопі або при recovery після креша.
    #
    # RECORDING_ENABLED autodetect'иться через імпорт pyaudiowpatch.
    # Можна форсово вимкнути через env var RECORDING_ENABLED=0.
    @staticmethod
    def _detect_recording_enabled():
        if os.environ.get('RECORDING_ENABLED', '').strip() in ('0', 'false', 'False'):
            return False
        try:
            import pyaudiowpatch  # noqa: F401
            return True
        except ImportError:
            return False

    RECORDING_ENABLED = _detect_recording_enabled.__func__()
    RECORDING_DIR = BASE_DIR / 'recordings' / 'sessions'
    RECORDING_CHUNK_SECONDS = int(os.environ.get('RECORDING_CHUNK_SECONDS', '5'))
    RECORDING_SAMPLE_RATE = int(os.environ.get('RECORDING_SAMPLE_RATE', '48000'))
    RECORDING_CHANNELS = int(os.environ.get('RECORDING_CHANNELS', '2'))
    RECORDING_MP3_BITRATE = os.environ.get('RECORDING_MP3_BITRATE', '192k')
    RECORDING_KEEP_PCM = os.environ.get('RECORDING_KEEP_PCM', 'False').lower() == 'true'
    # Проміжні mic.wav/system.wav — лише крок зведення у final.mp3, нічого їх
    # після finalize не читає. За замовчуванням видаляємо (інакше ~1 ГБ/сесію
    # мертвого місця). RECORDING_KEEP_WAV=true — лишати роздільні доріжки.
    RECORDING_KEEP_WAV = os.environ.get('RECORDING_KEEP_WAV', 'False').lower() == 'true'
    RECORDING_MIN_DISK_MB = int(os.environ.get('RECORDING_MIN_DISK_MB', '500'))

    # Phase 22: Screen video capture (ddagrab → NVENC, isolated subsystem).
    # Вимкніть через RECORDING_VIDEO_ENABLED=False якщо немає NVIDIA GPU.
    RECORDING_VIDEO_ENABLED = os.environ.get('RECORDING_VIDEO_ENABLED', 'True').lower() == 'true'
    RECORDING_FFMPEG_PATH = os.environ.get('RECORDING_FFMPEG_PATH', r'C:\ffmpeg\bin\ffmpeg.exe')
    RECORDING_VIDEO_FPS = int(os.environ.get('RECORDING_VIDEO_FPS', '30'))
    RECORDING_VIDEO_CODEC = os.environ.get('RECORDING_VIDEO_CODEC', 'h264_nvenc')
    RECORDING_VIDEO_QUALITY = os.environ.get('RECORDING_VIDEO_QUALITY', 'p5')   # nvenc -preset
    RECORDING_VIDEO_CQ = int(os.environ.get('RECORDING_VIDEO_CQ', '23'))
    RECORDING_VIDEO_MAX_TRACKS = int(os.environ.get('RECORDING_VIDEO_MAX_TRACKS', '3'))
    RECORDING_VIDEO_STOP_TIMEOUT_SEC = float(os.environ.get('RECORDING_VIDEO_STOP_TIMEOUT_SEC', '8'))
    RECORDING_BUILD_MASTER_MP4 = os.environ.get('RECORDING_BUILD_MASTER_MP4', 'False').lower() == 'true'
    RECORDING_REGION_OVERLAY = os.environ.get('RECORDING_REGION_OVERLAY', 'True').lower() == 'true'
    RECORDING_REGION_OVERLAY_COLOR = os.environ.get('RECORDING_REGION_OVERLAY_COLOR', '#e5484d')
    # Phase 23B-A: Video understanding — scene-keyframe OCR → RAG ingestion.
    RECORDING_VIDEO_SCENE_THRESHOLD = float(os.environ.get('RECORDING_VIDEO_SCENE_THRESHOLD', '0.4'))
    RECORDING_VIDEO_ANALYSIS_MAX_FRAMES = int(os.environ.get('RECORDING_VIDEO_ANALYSIS_MAX_FRAMES', '120'))
    # Phase 23B: Vision-опис кадрів («що показано на екрані») → у RAG поряд з OCR.
    # Бекенд: 'local' (Ollama VL-модель, $0/офлайн, default), 'claude' (Anthropic
    # vision API, платно, точніше), 'off'. Усе деградує: нема моделі/ключа →
    # опис порожній, кадри+OCR працюють як раніше. Ці константи дублює
    # video_vision.py (читає env напряму), щоб лишатись standalone-тестованим.
    VIDEO_VISION_BACKEND = os.environ.get('VIDEO_VISION_BACKEND', 'local').strip().lower()
    VIDEO_VISION_MODEL_LOCAL = os.environ.get('VIDEO_VISION_MODEL_LOCAL', 'qwen2.5vl:7b')
    VIDEO_VISION_MODEL_CLAUDE = os.environ.get('VIDEO_VISION_MODEL_CLAUDE', 'claude-haiku-4-5')
    VIDEO_VISION_MAX_FRAMES = int(os.environ.get('VIDEO_VISION_MAX_FRAMES', '120'))
    VIDEO_VISION_TIMEOUT = float(os.environ.get('VIDEO_VISION_TIMEOUT', '90'))

    # Phase 17: Telegram ingestion — слухання реального TG-АКАУНТА (MTProto/Telethon).
    # api_id/api_hash беруться з my.telegram.org → .env (СЕКРЕТИ, не в коді).
    # Слухач — ОКРЕМИЙ процес (telegram_listener.py): він єдиний тримає сесію
    # (SQLite-файл telegram.session не можна відкрити двома клієнтами), а Flask
    # спілкується з ним по localhost control-API. TELEGRAM_ENABLED autodetect:
    # потрібні і ключі, і встановлений telethon — інакше feature м'яко вимкнена.
    TELEGRAM_API_ID = int(os.environ.get('TELEGRAM_API_ID', '0') or '0')
    TELEGRAM_API_HASH = os.environ.get('TELEGRAM_API_HASH', '')
    # Шлях до файлу сесії Telethon (без розширення Telethon додасть .session).
    TELEGRAM_SESSION = os.environ.get('TELEGRAM_SESSION', str(BASE_DIR / 'telegram'))
    # Куди слухач зберігає завантажені медіа (фото/відео/голос/документи) перед
    # маршрутизацією у пайплайн. Не комітиться (.gitignore telegram_media/).
    TELEGRAM_MEDIA_DIR = BASE_DIR / 'telegram_media'
    # localhost control-API слухача (Flask проксирує сюди list-dialogs/status/backfill).
    TELEGRAM_CONTROL_HOST = os.environ.get('TELEGRAM_CONTROL_HOST', '127.0.0.1')
    TELEGRAM_CONTROL_PORT = int(os.environ.get('TELEGRAM_CONTROL_PORT', '5051'))
    # Транскрипція голосових/аудіо/відео з TG. large-v3-turbo — мультимовна, ~4x
    # швидша за large-v3, точність на рівні; кращий дефолт за medium на RTX 3090.
    # Голосові зазвичай короткі. Мова: 'uk' за замовч., для UA/RU-чатів можна
    # 'auto' (автовизначення) або конкретну мову.
    TELEGRAM_WHISPER_MODEL = os.environ.get('TELEGRAM_WHISPER_MODEL', 'large-v3-turbo')
    TELEGRAM_WHISPER_LANG = os.environ.get('TELEGRAM_WHISPER_LANG', 'uk')

    @staticmethod
    def _detect_telegram_enabled():
        if not (os.environ.get('TELEGRAM_API_ID') and os.environ.get('TELEGRAM_API_HASH')):
            return False
        try:
            import telethon  # noqa: F401
            return True
        except ImportError:
            return False

    TELEGRAM_ENABLED = _detect_telegram_enabled.__func__()

    # Phase 19 (Co-pilot, Крок 0): локальний LLM-диспетчер для живого ко-пілота.
    # Ollama — ОКРЕМИЙ системний сервіс (localhost HTTP), як telegram_listener
    # окремий процес. Ми лише говоримо з ним по HTTP; `ollama serve` піднімає
    # користувач/ОС. Усе опціональне й деградує: нема Ollama/моделі →
    # local_llm.is_available()=False, ко-пілот м'яко вимикається, запис/STT/RAG
    # працюють як раніше. Реальний gate — runtime-пінг у local_llm.py (не import),
    # бо доступність залежить від зовнішнього процесу. Ці константи дублює
    # local_llm.py (читає ті самі env напряму), щоб сервіс лишався standalone-
    # тестованим без Flask-контексту (як embeddings.py з EMBED_MODEL).
    COPILOT_ENABLED = os.environ.get('COPILOT_ENABLED', '1').strip() not in ('0', 'false', 'False')
    LOCAL_LLM_URL = os.environ.get('LOCAL_LLM_URL', 'http://localhost:11434')
    LOCAL_LLM_MODEL = os.environ.get('LOCAL_LLM_MODEL', 'qwen2.5:14b-instruct-q5_K_M')
    LOCAL_LLM_KEEPALIVE = os.environ.get('LOCAL_LLM_KEEPALIVE', '30m')
    LOCAL_LLM_NUM_CTX = int(os.environ.get('LOCAL_LLM_NUM_CTX', '8192'))
    LOCAL_LLM_TIMEOUT = float(os.environ.get('LOCAL_LLM_TIMEOUT', '60'))

    # T6.4 (Волна 4): опційний локальний cross-encoder rerank (bge-reranker-v2-m3)
    # над top-кандидатами retrieval.search(). За замовчуванням OFF — вмикається
    # ЛИШЕ для RAG-чату «Запитай архів» (app/services/rag.py), не для copilot/
    # categorize/MCP. Значення тут — довідкове/для видимості в конфізі; сам
    # rag.py читає env НАПРЯМУ (той самий патерн, що COPILOT_ENABLED/
    # embeddings.py — standalone-тестований без Flask-контексту).
    RECALL_RERANK_ENABLED = os.environ.get('RECALL_RERANK_ENABLED', '0').strip() in ('1', 'true', 'True')


class DevelopmentConfig(Config):
    """Конфигурация для разработки"""
    # DEBUG успадковується з Config (керується env FLASK_DEBUG, дефолт False) —
    # одна точка правди, щоб не розходитись з app.py.
    LOG_LEVEL = 'DEBUG'


class ProductionConfig(Config):
    """Конфигурация для продакшена"""
    DEBUG = False
    LOG_LEVEL = 'WARNING'

    # SECRET_KEY успадковується з Config. Небезпечний дефолт для DEBUG=False
    # (у т.ч. ProductionConfig, тут завжди) ловиться ЄДИНОЮ перевіркою нижче
    # в модулі (T1.5) — не дублювати тут окремим property-RuntimeError.

    # Более строгие лимиты
    RATE_LIMIT_YOUTUBE = 5  # TODO: not yet implemented
    RATE_LIMIT_TRANSCRIBE = 20  # TODO: not yet implemented

    # Отключаем небезопасные опции
    YOUTUBE_DL_OPTIONS = {
        **Config.YOUTUBE_DL_OPTIONS,
        'nocheckcertificate': False,
    }


class TestingConfig(Config):
    """Конфигурация для тестирования"""
    TESTING = True
    DATABASE = 'test_whisper_history.db'

    # Меньшие лимиты для тестов
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB
    MAX_YOUTUBE_DURATION = 5 * 60  # 5 минут


# Выбор конфигурации на основе переменной окружения
config_name = os.environ.get('APP_ENV', 'development')
config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig
}

# Текущая конфигурация
current_config = config_map.get(config_name, DevelopmentConfig)()


# --- T1.5: єдина перевірка SECRET_KEY, hard-fail замість warning ---
# Раніше було два незалежні механізми, що тригерились по-різному:
#   1) ProductionConfig.SECRET_KEY — property, кидала RuntimeError, але ЛИШЕ
#      якщо APP_ENV=production (обраний саме ProductionConfig-клас);
#   2) app.py — logger.warning (не блокує старт!), і лише якщо
#      FLASK_ENV=='production' або APP_ENV=='production'.
# Наслідок: DevelopmentConfig з DEBUG=False (напр. просто забули виставити
# FLASK_DEBUG=true, а APP_ENV лишили дефолтним/не production) не ловився
# жодним з двох — дефолтний SECRET_KEY тихо йшов у прод-подібний запуск.
#
# Тепер перевірка йде від ЕФЕКТИВНОГО DEBUG (узгоджено з тим, як Волна 0
# зробила DEBUG: дефолт False, читається з cfg), а не від рядка APP_ENV/
# FLASK_ENV. DEBUG=False + дефолтний/порожній ключ → hard-fail завжди,
# незалежно від того, який клас конфігурації обрано. DEBUG=True (dev) —
# дефолт дозволено.
_INSECURE_SECRET_KEYS = {
    '',
    'dev-secret-key-change-in-production',  # Config-дефолт
    'change-me-in-production',              # плейсхолдер з .env.example
}
# Рішення власника (03.07.2026): не ставити "стіну онбордингу" hard-fail'ом на
# кожен локальний запуск (після Волни 0 DEBUG=False став дефолтом, тож строгий
# hard-fail спрацьовував би на "clone & run"). Замість цього:
#   - мережевий bind (RECALL_BIND_ALL / FLASK_HOST=0.0.0.0) + небезпечний ключ →
#     hard-fail (реальний ризик підробки сесій ззовні, свідомий крок обов'язковий);
#   - локальний режим + небезпечний ключ → авто-генерувати випадковий ключ і
#     (best-effort) зберегти в .env, щоб він пережив рестарт. DEBUG=True — дефолт ок.
def _network_exposed() -> bool:
    """Чи слухатиме застосунок не тільки localhost (дзеркалить логіку app.py)."""
    if os.environ.get('RECALL_BIND_ALL', '').strip() in ('1', 'true', 'True'):
        return True
    return current_config.HOST == '0.0.0.0'


def _persist_secret_key(key: str) -> bool:
    """Best-effort: дописати SECRET_KEY у .env поруч із config.py.

    Не критично, якщо не вдалось (read-only FS / нема .env / вже є рядок
    SECRET_KEY=): ключ усе одно діє в пам'яті цього процесу.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    try:
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if 'SECRET_KEY=' in content:
                return False  # рядок уже є (напр. плейсхолдер) — не переписуємо чужий .env
            sep = '' if content == '' or content.endswith('\n') else '\n'
            with open(env_path, 'a', encoding='utf-8') as f:
                f.write(f'{sep}SECRET_KEY={key}\n')
        else:
            with open(env_path, 'w', encoding='utf-8') as f:
                f.write(f'SECRET_KEY={key}\n')
        return True
    except OSError:
        return False


if not current_config.DEBUG and current_config.SECRET_KEY in _INSECURE_SECRET_KEYS:
    if _network_exposed():
        raise RuntimeError(
            "Небезпечний SECRET_KEY при мережевому доступі: RECALL_BIND_ALL або "
            "FLASK_HOST=0.0.0.0 разом із дефолтним/порожнім SECRET_KEY = ризик "
            "підробки сесій ззовні. Встановіть випадковий SECRET_KEY у .env:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"\n'
            "і додайте SECRET_KEY=<результат> у .env."
        )
    _generated_key = secrets.token_hex(32)
    current_config.SECRET_KEY = _generated_key
    _saved = _persist_secret_key(_generated_key)
    _logger.warning(
        "SECRET_KEY не було встановлено — згенеровано новий%s. "
        "Мережевий доступ (RECALL_BIND_ALL/FLASK_HOST=0.0.0.0) вимагатиме явного ключа.",
        " і збережено в .env" if _saved else " (лише в пам'яті — збереження в .env не вдалось)",
    )


def get_config():
    """Получить текущую конфигурацию"""
    return current_config


def update_config(**kwargs):
    """Обновить параметры конфигурации"""
    for key, value in kwargs.items():
        if hasattr(current_config, key):
            setattr(current_config, key, value)


def init_directories(config=None):
    """Создать необходимые директории. Вызывать явно при старте приложения."""
    cfg = config or current_config
    for folder in [
        cfg.UPLOAD_FOLDER,
        cfg.TRANSCRIPTS_FOLDER,
        cfg.YOUTUBE_FOLDER,
        cfg.MODELS_FOLDER,
        cfg.DATABASE_BACKUP_FOLDER
    ]:
        os.makedirs(folder, exist_ok=True)
    # Phase 9: recordings dir для WASAPI запису.
    if getattr(cfg, 'RECORDING_ENABLED', False):
        os.makedirs(cfg.RECORDING_DIR, exist_ok=True)