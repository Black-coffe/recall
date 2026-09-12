# Файл: WhisperUI/config.py
# Путь: WhisperUI/config.py
# Описание: Централизованная конфигурация приложения

"""
Конфигурация для Whisper UI
"""

import logging
import os
import secrets
import threading
from pathlib import Path

from app.core import settings

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

    # Настройки сервера (дефолти з реєстру app/core/settings.py)
    DEBUG = settings.env_bool('FLASK_DEBUG')
    HOST = settings.env('FLASK_HOST')
    PORT = settings.env_int('FLASK_PORT')

    # Безопасность. Дефолт лишається як fallback для DEBUG=True (dev). Єдина
    # перевірка «дефолт неприпустимий у production-подібному режимі» — hard-fail
    # ЛІНИВО при першому get_config() (config-registry-profiles S3; раніше — тут
    # же в модулі, T1.5), біля вибору current_config. НЕ дублювати цю
    # перевірку деінде (раніше було 2 незалежні механізми — ProductionConfig
    # RuntimeError-property + app.py warning — які тригерились по-різному).
    SECRET_KEY = settings.env('SECRET_KEY')

    # Профіль (config-registry-profiles S3): 'desktop' (дефолт) або 'headless'.
    # Реальне значення виставляється лінивo у get_config() (RECALL_PROFILE
    # читається лише там, разом з рештою side effects) — тут лише безпечний
    # плейсхолдер, щоб атрибут існував одразу після `import config`.
    PROFILE = 'desktop'
    HEADLESS = False

    # Лимиты
    RATE_LIMIT_YOUTUBE = 10  # Максимум YouTube загрузок в час  # TODO: not yet implemented
    RATE_LIMIT_TRANSCRIBE = 30  # Максимум транскрипций в час  # TODO: not yet implemented

    # Автоочистка
    AUTO_CLEANUP_DAYS = 7  # Удалять временные файлы старше N дней  # TODO: not yet implemented
    AUTO_CLEANUP_ENABLED = True  # TODO: not yet implemented

    # GPU настройки
    FORCE_CPU = settings.env_bool('FORCE_CPU')
    GPU_BATCH_SIZE = 16  # Размер батча для GPU  # TODO: not yet implemented

    # Whisper backend (Phase 1 v4.0 roadmap)
    # 'faster' — faster-whisper (CTranslate2), 3-5x быстрее на GPU. Default.
    # 'openai' — референсный openai-whisper, fallback.
    WHISPER_BACKEND = settings.env('WHISPER_BACKEND').lower()
    # Параллельные транскрипции через семафор. На RTX 3090 (24GB):
    # tiny/base/small=4, medium=2, large=1.
    WHISPER_MAX_PARALLEL = settings.env_int('WHISPER_MAX_PARALLEL')
    # batch_size для BatchedInferencePipeline на длинных файлах (faster backend)
    WHISPER_BATCH_SIZE = settings.env_int('WHISPER_BATCH_SIZE')

    # Логирование
    LOG_LEVEL = settings.env('LOG_LEVEL')
    LOG_FILE = 'whisper_ui.log'
    LOG_MAX_SIZE = 10 * 1024 * 1024  # 10MB
    LOG_BACKUP_COUNT = 5

    # Phase 9: System Audio Recording (WASAPI loopback + mic).
    # Сесії пишуться в RECORDING_DIR як raw PCM з atomic manifest.json
    # для crash-resilience. Фіналізація (PCM→WAV→mix→MP3) запускається
    # на стопі або при recovery після креша.
    #
    # RECORDING_ENABLED autodetect'иться через імпорт pyaudiowpatch.
    # Можна форсово вимкнути явним значенням env var RECORDING_ENABLED
    # (будь-яким falsy для settings.env_bool — '0'/'false'/'no'/'off'/...).
    #
    # Викликається ЛІНИВО з get_config() (config-registry-profiles S3), НЕ при
    # визначенні класу — імпорт pyaudiowpatch більше не побічний ефект `import
    # config`. У headless-профілі не викликається взагалі (RECORDING_ENABLED
    # форсовано False без спроби імпорту, Assumption 3).
    @staticmethod
    def _detect_recording_enabled():
        # Дефолт реєстру — порожній рядок (autodetect). Явне значення (будь-яке,
        # не лише falsy) перекриває autodetect і читається ЧЕРЕЗ
        # settings.env_bool (config-registry-fix-01) — без власного парсера.
        if settings.env('RECORDING_ENABLED').strip() and not settings.env_bool('RECORDING_ENABLED'):
            return False
        try:
            import pyaudiowpatch  # noqa: F401
            return True
        except ImportError:
            return False

    # Плейсхолдер до першого get_config() (лінива ініціалізація нижче в модулі).
    RECORDING_ENABLED = False
    RECORDING_DIR = BASE_DIR / 'recordings' / 'sessions'
    RECORDING_CHUNK_SECONDS = settings.env_int('RECORDING_CHUNK_SECONDS')
    RECORDING_SAMPLE_RATE = settings.env_int('RECORDING_SAMPLE_RATE')
    RECORDING_CHANNELS = settings.env_int('RECORDING_CHANNELS')
    RECORDING_MP3_BITRATE = settings.env('RECORDING_MP3_BITRATE')
    RECORDING_KEEP_PCM = settings.env_bool('RECORDING_KEEP_PCM')
    # Проміжні mic.wav/system.wav — лише крок зведення у final.mp3, нічого їх
    # після finalize не читає. За замовчуванням видаляємо (інакше ~1 ГБ/сесію
    # мертвого місця). RECORDING_KEEP_WAV=true — лишати роздільні доріжки.
    RECORDING_KEEP_WAV = settings.env_bool('RECORDING_KEEP_WAV')
    RECORDING_MIN_DISK_MB = settings.env_int('RECORDING_MIN_DISK_MB')

    # Phase 22: Screen video capture (ddagrab → NVENC, isolated subsystem).
    # Вимкніть через RECORDING_VIDEO_ENABLED=False якщо немає NVIDIA GPU.
    # У headless форсується False у get_config() (Assumption 3) — тут дефолт
    # для desktop-профілю.
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
    RECORDING_VIDEO_SCENE_THRESHOLD = settings.env_float('RECORDING_VIDEO_SCENE_THRESHOLD')
    RECORDING_VIDEO_ANALYSIS_MAX_FRAMES = settings.env_int('RECORDING_VIDEO_ANALYSIS_MAX_FRAMES')
    # Phase 23B: Vision-опис кадрів («що показано на екрані») → у RAG поряд з OCR.
    # Бекенд: 'local' (Ollama VL-модель, $0/офлайн, default), 'claude' (Anthropic
    # vision API, платно, точніше), 'off'. Усе деградує: нема моделі/ключа →
    # опис порожній, кадри+OCR працюють як раніше. Ці константи дублює
    # video_vision.py (читає env напряму), щоб лишатись standalone-тестованим.
    VIDEO_VISION_BACKEND = settings.env('VIDEO_VISION_BACKEND').strip().lower()
    VIDEO_VISION_MODEL_LOCAL = settings.env('VIDEO_VISION_MODEL_LOCAL')
    VIDEO_VISION_MODEL_CLAUDE = settings.env('VIDEO_VISION_MODEL_CLAUDE')
    VIDEO_VISION_MAX_FRAMES = settings.env_int('VIDEO_VISION_MAX_FRAMES')
    VIDEO_VISION_TIMEOUT = settings.env_float('VIDEO_VISION_TIMEOUT')

    # Phase 17: Telegram ingestion — слухання реального TG-АКАУНТА (MTProto/Telethon).
    # api_id/api_hash беруться з my.telegram.org → .env (СЕКРЕТИ, не в коді).
    # Слухач — ОКРЕМИЙ процес (telegram_listener.py): він єдиний тримає сесію
    # (SQLite-файл telegram.session не можна відкрити двома клієнтами), а Flask
    # спілкується з ним по localhost control-API. TELEGRAM_ENABLED autodetect:
    # потрібні і ключі, і встановлений telethon — інакше feature м'яко вимкнена.
    # int(... or '0'): TELEGRAM_API_ID= (порожньо) у .env — окремий випадок від
    # "змінної немає" (settings.env усе одно поверне дефолт лише за відсутності
    # ключа), тож зберігаємо явний or-фолбек.
    TELEGRAM_API_ID = int(settings.env('TELEGRAM_API_ID') or '0')
    TELEGRAM_API_HASH = settings.env('TELEGRAM_API_HASH')
    # Шлях до файлу сесії Telethon (без розширення Telethon додасть .session).
    # config-registry-fix-01: раніше цей рядок читав os.environ напряму з
    # ДРУГИМ, незалежно хардкодженим дефолтом str(BASE_DIR / 'telegram') —
    # реєстровий Setting.default ('telegram') ігнорувався повністю, .env.example
    # друкував значення, яке код і не думав використовувати. Тепер єдине
    # джерело рядка-імені — реєстр (settings.env); BASE_DIR-приєднання
    # лишається (абсолютний шлях, незалежний від CWD — на цьому свідомо
    # будується tests/test_headless_boot.py::config-registry-fix-02, не чіпати
    # цю властивість). Якщо TELEGRAM_SESSION заданий АБСОЛЮТНИМ шляхом,
    # приєднання BASE_DIR його не змінює (pathlib: правий абсолютний операнд
    # заміняє лівий повністю).
    TELEGRAM_SESSION = str(BASE_DIR / settings.env('TELEGRAM_SESSION'))
    # Куди слухач зберігає завантажені медіа (фото/відео/голос/документи) перед
    # маршрутизацією у пайплайн. Не комітиться (.gitignore telegram_media/).
    TELEGRAM_MEDIA_DIR = BASE_DIR / 'telegram_media'
    # localhost control-API слухача (Flask проксирує сюди list-dialogs/status/backfill).
    TELEGRAM_CONTROL_HOST = settings.env('TELEGRAM_CONTROL_HOST')
    TELEGRAM_CONTROL_PORT = settings.env_int('TELEGRAM_CONTROL_PORT')
    # Транскрипція голосових/аудіо/відео з TG. large-v3-turbo — мультимовна, ~4x
    # швидша за large-v3, точність на рівні; кращий дефолт за medium на RTX 3090.
    # Голосові зазвичай короткі. Мова: 'uk' за замовч., для UA/RU-чатів можна
    # 'auto' (автовизначення) або конкретну мову.
    TELEGRAM_WHISPER_MODEL = settings.env('TELEGRAM_WHISPER_MODEL')
    TELEGRAM_WHISPER_LANG = settings.env('TELEGRAM_WHISPER_LANG')

    # Викликається ЛІНИВО з get_config() (config-registry-profiles S3), НЕ при
    # визначенні класу — імпорт telethon більше не побічний ефект `import config`.
    # os.environ.get(...) напряму (не settings.env): перевіряємо ВІДСУТНІСТЬ
    # ключа (None), а не значення-за-дефолтом — settings.env повернув би
    # непорожній рядок-дефолт навіть коли змінної немає.
    @staticmethod
    def _detect_telegram_enabled():
        if not (os.environ.get('TELEGRAM_API_ID') and os.environ.get('TELEGRAM_API_HASH')):
            return False
        try:
            import telethon  # noqa: F401
            return True
        except ImportError:
            return False

    # Плейсхолдер до першого get_config() (лінива ініціалізація нижче в модулі).
    TELEGRAM_ENABLED = False

    # Phase 19 (Co-pilot, Крок 0): локальний LLM-диспетчер для живого ко-пілота.
    # Ollama — ОКРЕМИЙ системний сервіс (localhost HTTP), як telegram_listener
    # окремий процес. Ми лише говоримо з ним по HTTP; `ollama serve` піднімає
    # користувач/ОС. Усе опціональне й деградує: нема Ollama/моделі →
    # local_llm.is_available()=False, ко-пілот м'яко вимикається, запис/STT/RAG
    # працюють як раніше. Реальний gate — runtime-пінг у local_llm.py (не import),
    # бо доступність залежить від зовнішнього процесу. Ці константи дублює
    # local_llm.py (читає ті самі env напряму), щоб сервіс лишався standalone-
    # тестованим без Flask-контексту (як embeddings.py з EMBED_MODEL).
    # Дефолт-true виражений у реєстрі (default="1"), не інверсною falsy-
    # перевіркою на місці виклику (config-registry-fix-01) — settings.env_bool
    # єдиний булевий парсер.
    COPILOT_ENABLED = settings.env_bool('COPILOT_ENABLED')
    LOCAL_LLM_URL = settings.env('LOCAL_LLM_URL')
    LOCAL_LLM_MODEL = settings.env('LOCAL_LLM_MODEL')
    LOCAL_LLM_KEEPALIVE = settings.env('LOCAL_LLM_KEEPALIVE')
    LOCAL_LLM_NUM_CTX = settings.env_int('LOCAL_LLM_NUM_CTX')
    LOCAL_LLM_TIMEOUT = settings.env_float('LOCAL_LLM_TIMEOUT')

    # T6.4 (Волна 4): опційний локальний cross-encoder rerank (bge-reranker-v2-m3)
    # над top-кандидатами retrieval.search(). За замовчуванням OFF — вмикається
    # ЛИШЕ для RAG-чату «Запитай архів» (app/services/rag.py), не для copilot/
    # categorize/MCP. Значення тут — довідкове/для видимості в конфізі; сам
    # rag.py читає env НАПРЯМУ (той самий патерн, що COPILOT_ENABLED/
    # embeddings.py — standalone-тестований без Flask-контексту).
    RECALL_RERANK_ENABLED = settings.env_bool('RECALL_RERANK_ENABLED')


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
    """Чи слухатиме застосунок не тільки localhost (`RECALL_BIND_ALL` через
    єдиний парсер `settings.env_bool`, як і `app.py`)."""
    if settings.env_bool('RECALL_BIND_ALL'):
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


def _finalize_secret_key() -> None:
    """T1.5: hard-fail/автоген SECRET_KEY. Викликається ЛІНИВО з get_config()
    (config-registry-profiles S3) — раніше це був код на рівні модуля, тож
    спрацьовувало вже при `import config`.

    config-registry-fix-01 (знахідка 2): мережевий bind hard-fail перевіряється
    ПЕРШИМ, незалежно від DEBUG. Раніше умова `not current_config.DEBUG` стояла
    зовні й повністю пропускала hard-fail, коли DEBUG=True — а розширений
    truthy-набір settings.env_bool ('1'/'true'/'yes'/'on') робить
    FLASK_DEBUG=1 (раніше давав DEBUG=False через .lower()=='true') тепер
    DEBUG=True. Семантика рішення власника не змінюється (локально —
    автоген, лише мережевий bind — hard-fail); фіксується сам bug, де
    DEBUG=True міг обійти hard-fail і при мережевому доступі."""
    if current_config.SECRET_KEY not in _INSECURE_SECRET_KEYS:
        return
    if _network_exposed():
        raise RuntimeError(
            "Небезпечний SECRET_KEY при мережевому доступі: RECALL_BIND_ALL або "
            "FLASK_HOST=0.0.0.0 разом із дефолтним/порожнім SECRET_KEY = ризик "
            "підробки сесій ззовні. Встановіть випадковий SECRET_KEY у .env:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"\n'
            "і додайте SECRET_KEY=<результат> у .env."
        )
    if current_config.DEBUG:
        # Локальний dev-запуск, не мережевий — дефолтний ключ прийнятний.
        return
    _generated_key = secrets.token_hex(32)
    current_config.SECRET_KEY = _generated_key
    _saved = _persist_secret_key(_generated_key)
    _logger.warning(
        "SECRET_KEY не було встановлено — згенеровано новий%s. "
        "Мережевий доступ (RECALL_BIND_ALL/FLASK_HOST=0.0.0.0) вимагатиме явного ключа.",
        " і збережено в .env" if _saved else " (лише в пам'яті — збереження в .env не вдалось)",
    )


_config_initialized = False
_config_lock = threading.Lock()


def _initialize_dynamic_config() -> None:
    """Побічні ефекти, що раніше виконувались при `import config` (важкі
    імпорти pyaudiowpatch/telethon, SECRET_KEY hard-fail/автоген) —
    config-registry-profiles S3 переносить їх сюди, у ЛІНИВУ ініціалізацію
    при першому виклику get_config()."""
    profile_name = settings.profile()
    current_config.PROFILE = profile_name
    current_config.HEADLESS = profile_name == 'headless'

    if current_config.HEADLESS:
        # headless: не піднімаємо recorder — і НЕ намагаємось імпортувати
        # pyaudiowpatch взагалі (Assumption 3, config-registry-profiles).
        current_config.RECORDING_ENABLED = False
        current_config.RECORDING_VIDEO_ENABLED = False
    else:
        current_config.RECORDING_ENABLED = Config._detect_recording_enabled()

    current_config.TELEGRAM_ENABLED = Config._detect_telegram_enabled()

    _finalize_secret_key()


def get_config():
    """Получить текущую конфигурацию.

    Побічні ефекти (autodetect recorder/telegram, SECRET_KEY hard-fail/
    автоген, профіль) виконуються ЛІНИВО тут, при першому виклику — НЕ при
    `import config` (config-registry-profiles S3). Захищено `_config_lock`
    (config-registry-fix-01, знахідка 15): без замка два потоки, що
    змагаються за перший виклик, могли б обидва виконати
    `_initialize_dynamic_config()` і обидва дописати `.env`."""
    global _config_initialized
    if not _config_initialized:
        with _config_lock:
            if not _config_initialized:
                _initialize_dynamic_config()
                _config_initialized = True
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