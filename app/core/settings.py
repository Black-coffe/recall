"""Реєстр усіх env-змінних Recall (спек `config-registry-profiles`, story 01).

Єдине джерело істини для назви/дефолту/типу/групи/профілю/призначення
кожного env-флагу, який читає код. Використовується для:

  - генерації ``.env.example`` (``python -m app.core.settings env-example``);
  - тесту повноти (``tests/test_settings_registry.py``) — кожна назва,
    знайдена статичним скануванням ``os.environ.get/getenv/[...]`` у коді
    (``app/``, ``app.py``, ``config.py``, ``mcp_server.py``,
    ``telegram_listener.py``, ``telegram_login.py``, ``whisper_manager_new.py``),
    має тут запис;
  - ``config.py::get_config()`` — бере рядкові дефолти звідси замість
    дубльованих літералів, розсіяних по класу ``Config``.

Модуль НЕ імпортує ``app.*``, ``flask``, ``torch``, ``config`` — має лишатись
завантажуваним будь-де без важких залежностей (памʼятка
``mcp-stdio-no-heavy-models``).

НЕ переписує виклики ``os.environ.get(...)`` у сервісах (Law 2 VULYK,
non-goal плану `config-registry-profiles`) — більшість записів тут лише
ДОКУМЕНТУЄ існуюче читання; саме читання лишається на місці у сервісі.
Дублі дефолтів між ``config.py`` і сервісом (напр. ``RECALL_RERANK_ENABLED``)
не усуваються — це борг наступних історій, фіксується у ``gates``.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Literal

Kind = Literal["str", "int", "float", "bool", "path", "secret"]
Scope = Literal["config", "runtime"]
ProfileTag = Literal["all", "desktop", "headless"]


@dataclass(frozen=True)
class Setting:
    """Один запис реєстру.

    ``gates`` — обовʼязкова окрема від ``doc`` фраза: що саме цей флаг
    вмикає/вимикає/обмежує. ``group`` визначає секцію згенерованого
    ``.env.example`` (порядок секцій = порядок першої появи у ``REGISTRY``).
    ``scope`` — 'config', якщо значення читає ``config.py`` у ``Config``
    (і віддає через ``get_config()``), 'runtime' — якщо сервіс читає
    ``os.environ`` напряму в момент виклику.
    """

    name: str
    default: str
    kind: Kind
    group: str
    doc: str
    gates: str
    profile: ProfileTag = "all"
    scope: Scope = "runtime"
    #: Профіле-залежний дефолт (config-registry-fix-01, Contracts §Auth).
    #: Не ``None`` → у профілі ``headless`` перекриває ``default`` (якщо
    #: env-змінна не задана явно). Останнє поле датакласу навмисно — щоб усі
    #: наявні позиційні виклики ``Setting(...)`` лишались валідними.
    default_headless: str | None = None


REGISTRY: tuple[Setting, ...] = (
    # --- Сервер / профіль ---
    Setting("FLASK_DEBUG", "false", "bool", "Сервер",
            "Прапорець debug-режиму Flask.",
            "DEBUG=True дозволяє дефолтний SECRET_KEY без hard-fail; DEBUG=False вимагає справжній ключ.",
            scope="config"),
    Setting("FLASK_HOST", "127.0.0.1", "str", "Сервер",
            "Безпечний дефолт (Волна 0/T1.1) — слухати лише локальну машину.",
            "Мережевий bind (0.0.0.0) вимагає явного SECRET_KEY, інакше config.py hard-fail.",
            scope="config"),
    Setting("FLASK_PORT", "5050", "int", "Сервер",
            "Порт Flask-сервера.", "Порт, на якому слухає app.py.", scope="config"),
    Setting("APP_ENV", "development", "str", "Сервер",
            "Вибір класу конфігурації.",
            "development|production|testing → DevelopmentConfig|ProductionConfig|TestingConfig.",
            scope="config"),
    Setting("SECRET_KEY", "dev-secret-key-change-in-production", "secret", "Сервер",
            "Ключ сесій Flask.",
            "Дефолт/порожньо + DEBUG=False → автоген і запис у .env; те саме + мережевий bind → hard-fail.",
            scope="config"),
    Setting("FORCE_CPU", "false", "bool", "Сервер",
            "Форсувати CPU для whisper/embeddings/reranker.",
            "true → GPU не використовується навіть за наявності CUDA.", scope="config"),
    Setting("LOG_LEVEL", "INFO", "str", "Сервер",
            "Рівень логування застосунку.", "Передається у basicConfig/RotatingFileHandler.",
            scope="config"),
    Setting("RECALL_PROFILE", "desktop", "str", "Сервер",
            "desktop — повний застосунок (recorder, SPA-shell); headless — лише API/інжест.",
            "headless вимикає RECORDING_ENABLED/RECORDING_VIDEO_ENABLED і SPA-shell/sw.js (404 'headless'); "
            "інше значення → ValueError з profile().",
            scope="config"),
    Setting("RECALL_LOG_FORMAT", "text", "str", "Сервер",
            "text|json — формат рядків логу.",
            "json вмикає JsonFormatter на file+stream хендлерах у app.py.",
            ),

    # --- Транскрипція (whisper) ---
    Setting("WHISPER_BACKEND", "faster", "str", "Транскрипція",
            "faster (CTranslate2, дефолт) або openai (legacy fallback).",
            "Обирає бекенд у whisper_manager_new.py.", scope="config"),
    Setting("WHISPER_MAX_PARALLEL", "2", "int", "Транскрипція",
            "Розмір семафора паралельних транскрипцій.",
            "На RTX 3090: tiny/base/small=4, medium=2, large=1.", scope="config"),
    Setting("WHISPER_BATCH_SIZE", "8", "int", "Транскрипція",
            "batch_size для BatchedInferencePipeline на довгих файлах (faster backend).",
            "Впливає лише на faster-whisper.", scope="config"),
    Setting("WHISPER_MODEL_CACHE_SIZE", "3", "int", "Транскрипція",
            "LRU-кеш кількох одночасно завантажених whisper-моделей (напр. 'small' live + 'medium' full).",
            "Запобігає evict/reload моделі на CUDA між live- і full-транскрипцією."),
    Setting("RECALL_PRELOAD_WHISPER", "1", "bool", "Транскрипція",
            "Прогрів whisper-моделі у фоні одразу після старту застосунку.",
            "0 → без прогріву, перший запит транскрипції холодний."),
    Setting("RECALL_PRELOAD_WHISPER_MODEL", "large-v3-turbo", "str", "Транскрипція",
            "Яку модель прогрівати.", "Дзеркалить TELEGRAM_WHISPER_MODEL за замовчуванням."),

    # --- Rate limits ---
    Setting("RATE_LIMIT_TRANSCRIBE", "60/minute", "str", "Rate limits",
            "Ліміт flask-limiter для /api/transcribe.", "Формат flask-limiter (N/interval)."),
    Setting("RATE_LIMIT_YOUTUBE", "30/minute", "str", "Rate limits",
            "Ліміт для YouTube-ендпоінтів.", "Формат flask-limiter (N/interval)."),
    Setting("RATE_LIMIT_POLISH", "20/hour", "str", "Rate limits",
            "Ліміт для Claude polish-запитів.", "Формат flask-limiter (N/interval)."),
    Setting("RATE_LIMIT_RECORDING_START", "10/minute", "str", "Rate limits",
            "Ліміт запуску записів (Phase 9.10 hardening).",
            "Кожна сесія ~10MB/хв на диску — низький лім запобігає script-driven abuse."),

    # --- Виконавці (job queue) ---
    Setting("LIVE_EXECUTOR_WORKERS", "1", "int", "Виконавці",
            "Розмір пулу для latency-критичних job (recording_finalize).",
            "Один recorder одночасно зазвичай — 1 воркер завжди вільний, не чекає batch."),
    Setting("BATCH_EXECUTOR_WORKERS", "2", "int", "Виконавці",
            "Розмір пулу для решти job (tg_transcribe, video_analysis тощо).",
            "Ці job можуть вантажити GPU (faster-whisper) — свідомо не збільшується."),

    # --- Автоочистка ---
    Setting("AUTO_CLEANUP_ENABLED", "false", "bool", "Автоочистка",
            "Вмикає періодичне видалення старих тимчасових файлів (Phase 6.3).",
            "За дефолтом вимкнено — щоб локальний користувач не втратив файли випадково."),
    Setting("AUTO_CLEANUP_DAYS", "7", "int", "Автоочистка",
            "Вік файлів (днів), старші за який видаляються.", "Діє лише якщо AUTO_CLEANUP_ENABLED=true."),

    # --- Запис (recorder) ---
    Setting("RECORDING_ENABLED", "", "bool", "Запис",
            "Autodetect через імпорт pyaudiowpatch; '0'/'false' форсує вимкнення.",
            "headless завжди False без спроби імпорту pyaudiowpatch (Assumption 3).", scope="config",
            profile="desktop"),
    Setting("RECORDING_CHUNK_SECONDS", "5", "int", "Запис",
            "Розмір чанка сесії запису.", "Raw PCM пишеться чанками для crash-resilience.", scope="config"),
    Setting("RECORDING_SAMPLE_RATE", "48000", "int", "Запис",
            "Частота дискретизації запису.", "WASAPI loopback + mic.", scope="config"),
    Setting("RECORDING_CHANNELS", "2", "int", "Запис", "Кількість каналів запису.",
            "Передається в RecordingService.start() як channels — кількість доріжок mic+system зведення.",
            scope="config"),
    Setting("RECORDING_MP3_BITRATE", "192k", "str", "Запис",
            "Бітрейт фінального MP3 при зведенні.",
            "Йде у finalize_session() як bitrate кодування final.mp3.", scope="config"),
    Setting("RECORDING_KEEP_PCM", "False", "bool", "Запис",
            "Не видаляти проміжні raw PCM після фіналізації.", "true — займає значно більше диска.",
            scope="config"),
    Setting("RECORDING_KEEP_WAV", "False", "bool", "Запис",
            "Лишати роздільні mic.wav/system.wav після зведення у final.mp3.",
            "За дефолтом видаляються — інакше ~1ГБ/сесію мертвого місця.", scope="config"),
    Setting("RECORDING_MIN_DISK_MB", "500", "int", "Запис",
            "Мінімум вільного місця (МБ), нижче якого запис не стартує.",
            "Нижче — recording/screens.py відмовляє стартувати запис із повідомленням про нестачу диска.",
            scope="config"),

    # --- Відео-запис (screen capture) ---
    Setting("RECORDING_VIDEO_ENABLED", "True", "bool", "Відео-запис",
            "Phase 22: захоплення екрана (ddagrab → NVENC).",
            "Вимкнути якщо немає NVIDIA GPU; headless завжди False (Assumption 3).", scope="config",
            profile="desktop"),
    Setting("RECORDING_FFMPEG_PATH", r"C:\ffmpeg\bin\ffmpeg.exe", "path", "Відео-запис",
            "Шлях до ffmpeg для відео-пайплайна.",
            "Не знайдено за шляхом — video_probe.ffmpeg_path() кидає помилку з підказкою встановити ffmpeg.",
            scope="config"),
    Setting("RECORDING_VIDEO_FPS", "30", "int", "Відео-запис", "FPS відео-запису.",
            "FPS, з яким VideoCaptureSupervisor запускає ffmpeg-захоплення екрана.", scope="config"),
    Setting("RECORDING_VIDEO_CODEC", "h264_nvenc", "str", "Відео-запис",
            "Кодек NVENC для відео-запису.",
            "Кодек, переданий у VideoCaptureSupervisor — nvenc-специфічний, вимагає NVIDIA GPU.",
            scope="config"),
    Setting("RECORDING_VIDEO_QUALITY", "p5", "str", "Відео-запис",
            "nvenc -preset.",
            "Передається як -preset у VideoCaptureSupervisor (nvenc).", scope="config"),
    Setting("RECORDING_VIDEO_CQ", "23", "int", "Відео-запис",
            "Константний quality-фактор nvenc.",
            "Передається як -cq у VideoCaptureSupervisor; менше значення — вища якість і більший файл.",
            scope="config"),
    Setting("RECORDING_VIDEO_MAX_TRACKS", "3", "int", "Відео-запис",
            "Максимум одночасних відео-доріжок (моніторів/вікон).",
            "Оголошено в config.py, але жоден сервіс його не читає — обмеження кількості доріжок фактично "
            "не застосовується.", scope="config"),
    Setting("RECORDING_VIDEO_STOP_TIMEOUT_SEC", "8", "float", "Відео-запис",
            "Таймаут очікування зупинки відео-процесу.",
            "Скільки чекати graceful-стоп ffmpeg (proc.wait) у _graceful_stop_proc() перед terminate/kill.",
            scope="config"),
    Setting("RECORDING_BUILD_MASTER_MP4", "False", "bool", "Відео-запис",
            "Збирати master MP4 з усіх доріжок після завершення.",
            "true — finalize_video() додатково збирає master.mp4; false — лишаються тільки окремі доріжки.",
            scope="config"),
    Setting("RECORDING_REGION_OVERLAY", "True", "bool", "Відео-запис",
            "Показувати оверлей вибраної області захоплення.",
            "false — VideoCaptureSupervisor не малює оверлей області захоплення на екрані під час запису.",
            scope="config"),
    Setting("RECORDING_REGION_OVERLAY_COLOR", "#e5484d", "str", "Відео-запис",
            "Колір оверлея області захоплення.",
            "Діє лише коли RECORDING_REGION_OVERLAY=true.", scope="config"),
    Setting("RECORDING_VIDEO_SCENE_THRESHOLD", "0.4", "float", "Відео-запис",
            "Поріг зміни сцени для scene-keyframe вибірки (Phase 23B-A).",
            "Передається в extract_scene_keyframes() як scene_threshold — вищий поріг, менше кадрів.",
            scope="config"),
    Setting("RECORDING_VIDEO_ANALYSIS_MAX_FRAMES", "120", "int", "Відео-запис",
            "Максимум кадрів на аналіз відео (OCR/vision).",
            "Стеля кадрів, переданих у extract_scene_keyframes() — понад неї кадри взагалі не вибираються.",
            scope="config"),

    # --- Vision (опис кадрів) ---
    Setting("VIDEO_VISION_BACKEND", "local", "str", "Vision",
            "local (Ollama VL, $0/офлайн, дефолт), claude (Anthropic vision, платно), off.",
            "Усе деградує: нема моделі/ключа → опис порожній, кадри+OCR працюють як раніше.", scope="config"),
    Setting("VIDEO_VISION_MODEL_LOCAL", "qwen2.5vl:7b", "str", "Vision",
            "Локальна VL-модель Ollama для опису кадрів.",
            "Діє лише коли VIDEO_VISION_BACKEND=local — модель, яку video_vision.py викликає через Ollama.",
            scope="config"),
    Setting("VIDEO_VISION_MODEL_CLAUDE", "claude-haiku-4-5", "str", "Vision",
            "Claude vision-модель (якщо backend=claude).",
            "Діє лише коли VIDEO_VISION_BACKEND=claude — модель, яку video_vision.py викликає через Anthropic API.",
            scope="config"),
    Setting("VIDEO_VISION_MAX_FRAMES", "120", "int", "Vision",
            "Максимум кадрів на vision-опис.",
            "Після стелі решта кадрів сесії лишаються з порожнім vision-описом (тільки OCR).", scope="config"),
    Setting("VIDEO_VISION_TIMEOUT", "90", "float", "Vision",
            "Таймаут виклику vision-моделі (секунди).",
            "Таймаут HTTP-виклику describe_frame() до Ollama/Claude — вихід за нього best-effort лишає опис "
            "кадру порожнім.", scope="config"),

    # --- Telegram: слухач акаунта ---
    Setting("TELEGRAM_API_ID", "0", "int", "Telegram", "api_id з https://my.telegram.org/apps.",
            "Разом з TELEGRAM_API_HASH + встановленим telethon вмикає TELEGRAM_ENABLED.", scope="config"),
    Setting("TELEGRAM_API_HASH", "", "secret", "Telegram", "api_hash з https://my.telegram.org/apps.",
            "Секрет — не в коді.", scope="config"),
    Setting("TELEGRAM_SESSION", "telegram", "path", "Telegram",
            "Базове імʼя файлу сесії Telethon (реальний дефолт — BASE_DIR/telegram, Telethon додає .session).",
            "SQLite-файл сесії не можна відкрити двома клієнтами одночасно.", scope="config"),
    Setting("TELEGRAM_CONTROL_HOST", "127.0.0.1", "str", "Telegram",
            "Host localhost control-API дочірнього процесу-слухача.",
            "На цьому host telegram_listener.py піднімає control-HTTP-сервер; app.py звертається на ту саму "
            "адресу.", scope="config"),
    Setting("TELEGRAM_CONTROL_PORT", "5051", "int", "Telegram",
            "Порт localhost control-API слухача.", "Flask проксирує сюди list-dialogs/status/backfill.",
            scope="config"),
    Setting("TELEGRAM_WHISPER_MODEL", "large-v3-turbo", "str", "Telegram",
            "Whisper-модель для голосових/аудіо/відео з TG.",
            "large-v3-turbo — мультимовна, ~4x швидша за large-v3.", scope="config"),
    Setting("TELEGRAM_WHISPER_LANG", "uk", "str", "Telegram",
            "Мова транскрипції TG-медіа ('auto' — автовизначення).",
            "Передається у транскрипцію голосових/аудіо з TG у app/blueprints/telegram.py як параметр language.",
            scope="config"),
    Setting("TELEGRAM_CATCHUP", "1", "bool", "Telegram",
            "Догрузка повідомлень, що прийшли поки слухач не працював.",
            "0 вимикає — на живих даних 30 втрачених буднів за 8 місяців без цього."),
    Setting("TELEGRAM_CATCHUP_DEBOUNCE_MIN", "15", "float", "Telegram",
            "Не догоняти чат, якщо його вже догоняли менше N хвилин тому.",
            "app.py під час розробки стартує десятки разів на день."),
    Setting("TELEGRAM_DIALOG_LIMIT", "500", "int", "Telegram",
            "Скільки діалогів перелічувати (/dialogs, /coverage, догонка).",
            "200 упиралось у стелю — «чату немає серед діалогів» не відрізнялось від «не вліз у вікно»."),
    Setting("TELEGRAM_BACKFILL_DELAY", "0.5", "float", "Telegram",
            "Пауза (сек) між повідомленнями при backfill/ремонті/догонці.",
            "Затримка між повідомленнями в telegram_listener.py — запобігає FloodWaitError від Telegram API."),
    Setting("TELEGRAM_SELF_NAME", "", "str", "Telegram",
            "Імʼя власника в переписці (хто «я»).",
            "Береться від слухача автоматично; задавати вручну лише якщо слухач не використовується."),
    Setting("TELEGRAM_SELF_USERNAME", "", "str", "Telegram",
            "Нікнейм власника (без @).", "Потрібен щоб ловити @згадки без ніка з сесії слухача."),
    Setting("TELEGRAM_ORIGINAL_MAX_MB", "100", "int", "Telegram",
            "Поріг (МБ), важчі за який оригінали voice/audio видаляються після витягу тексту.",
            "tg-media-policy-02: рішення власника — видаляються ЛИШЕ оригінали важчі за поріг, "
            "дрібні лишаються на диску; діє тільки якщо витяг тексту вдався."),

    # --- Нитки Telegram-переписки ---
    Setting("TG_THREAD_BURST_GAP_MIN", "180", "int", "Нитки Telegram",
            "Пауза (хв), після якої розмова вважається новим сплеском.",
            "180 — з заміру розподілу: на 30хв третина сплесків вироджується в одинаків, на 1440хв заходить кілька робочих днів."),
    Setting("TG_THREAD_BATCH_MSGS", "25", "int", "Нитки Telegram",
            "Скільки повідомлень за раз віддавати моделі для розбиття сплеску.",
            "Разом з TG_THREAD_BATCH_CHARS ділить сплеск на під-пачки — межа спрацьовує першою з двох."),
    Setting("TG_THREAD_BATCH_CHARS", "6000", "int", "Нитки Telegram",
            "Стеля символів на під-пачку розбиття сплеску.",
            "Без різання довгі сплески (до 338k символів) не влазять у контекст моделі."),
    Setting("TG_THREAD_CANDIDATES", "5", "int", "Нитки Telegram",
            "Скільки відкритих ниток чату показувати моделі як кандидатів на продовження.",
            "Дефолт limit у open_threads() (tg_threads.py) — скільки кандидатів бачить модель розбиття."),
    Setting("TG_THREAD_MODEL", "", "str", "Нитки Telegram",
            "Модель для розбиття сплесків на нитки. Порожньо = взяти LOCAL_LLM_MODEL.",
            "7B дала те саме розбиття, що й 32B, за вчетверо менший час."),
    Setting("TG_THREAD_IDLE_DAYS", "3", "int", "Нитки Telegram",
            "Скільки днів без нових повідомлень нитка лишається кандидатом на продовження.",
            "3, а не 21 — на прогоні з вікном 21 день максимальний розрив упирався рівно в межу вікна, а не в зміст."),
    Setting("TG_THREAD_MAX_MSGS", "30", "int", "Нитки Telegram",
            "Мʼяка стеля обсягу нитки (забороняє НОВУ розмову, не рве поточну).",
            "Без неї ниток зібрали значна частина архіву, найбільша — повідомлень за два місяці."),

    # --- Досьє Telegram-чату ---
    Setting("TG_CONTEXT_MIN_NEW", "30", "int", "Досьє чату",
            "Скільки нових повідомлень накопичити перед перебудовою профілю чату.",
            "Перебудова досьє в tg_chat_context.py не стартує, поки не набереться стільки нових повідомлень "
            "(або не мине TG_CONTEXT_MAX_AGE_H)."),
    Setting("TG_CONTEXT_MAX_AGE_H", "24", "int", "Досьє чату",
            "Оновити навіть мовчазний чат раз на стільки годин.",
            "Другий поріг тієї ж пари з TG_CONTEXT_MIN_NEW — спрацьовує навіть без нових повідомлень."),
    Setting("TG_CONTEXT_THREADS", "12", "int", "Досьє чату",
            "Скільки останніх ниток показувати моделі при побудові досьє.",
            "Досьє про поточний стан справ, а не літопис."),
    Setting("TG_CONTEXT_THREAD_SAMPLE", "4", "int", "Досьє чату",
            "Скільки повідомлень нитки давати моделі для розбору.",
            "LIMIT у SQL-запиті вибірки повідомлень нитки для побудови досьє (tg_chat_context.py)."),
    Setting("TG_CONTEXT_MODEL", "", "str", "Досьє чату",
            "Модель для побудови досьє чату. Порожньо = взяти LOCAL_LLM_MODEL.",
            "32B на 24GB GPU непридатна для цієї задачі — заміряно, один чат не вклався і в 300с."),
    Setting("TG_CONTEXT_TIMEOUT", "300", "float", "Досьє чату",
            "Таймаут побудови досьє (сек).", "Пакетна задача, дефолтні 60с local_llm тут малі."),

    # --- Зобовʼязання з переписки ---
    Setting("TG_TASKS_TRIAGE_MODEL", "qwen2.5:7b-instruct", "str", "Зобовʼязання з переписки",
            "Локальна модель відсіву ниток без домовленостей.",
            "7B-клас: триаж — рішення так/ні, найдешевша задача каскаду."),
    Setting("TG_TASKS_TRIAGE_TIMEOUT", "180", "float", "Зобовʼязання з переписки",
            "Таймаут виклику триажу.",
            "Переданий у local_llm-виклик триажу (max_tokens=200) в tg_tasks.py."),
    Setting("TG_TASKS_TRIAGE_PREVIEW", "400", "int", "Зобовʼязання з переписки",
            "Прев'ю символів повідомлення для триажу.",
            "Символьна стеля _preview() кожного повідомлення, що йде в промпт триажу."),
    Setting("TG_TASKS_TRIAGE_MAX_MSGS", "40", "int", "Зобовʼязання з переписки",
            "Скільки повідомлень нитки показувати триажу.",
            "Дефолт limit у thread_messages() — скільки останніх повідомлень бачить і триаж, і структурний "
            "витяг."),
    Setting("TG_TASKS_MODEL", "", "str", "Зобовʼязання з переписки",
            "Claude-модель витягу структури. Порожньо = дефолтна модель архіву (CLAUDE_MODEL/DEFAULT_MODEL).",
            "Порожньо — tg_tasks.py бере модель за замовчуванням архіву, не окрему."),
    Setting("TG_TASKS_EFFORT", "low", "str", "Зобовʼязання з переписки",
            "Effort для Claude-витягу структури зобовʼязань.", "Витяг структури — глибокий thinking не окупається."),
    Setting("TG_TASKS_PREVIEW", "1500", "int", "Зобовʼязання з переписки",
            "Прев'ю символів повідомлення для витягу (більше ніж у триажу).",
            "Символьна стеля _preview() повідомлення при структурному витягу зобовʼязань (не триажі)."),
    Setting("TG_TASKS_MAX_CHARS", "24000", "int", "Зобовʼязання з переписки",
            "Стеля символів нитки для витягу структури.",
            "Накопичення повідомлень нитки в tg_tasks.py зупиняється, щойно сумарна довжина перевищує цю "
            "стелю."),

    # --- Питання без відповіді ---
    Setting("TG_QUESTIONS_WINDOW_H", "24", "float", "Питання без відповіді",
            "Годин присутності в чаті після питання, щоб вважати його побаченим.",
            "Дефолт REPLY_WINDOW_H у tg_questions.py — вікно, у якому шукається відповідь/реакція на питання."),
    Setting("TG_QUESTIONS_DAYS", "90", "int", "Питання без відповіді",
            "Вікно (днів) пошуку питань без відповіді.",
            "Ширше за звід задач (30 днів) — питання без відповіді часом висить місяцями."),

    # --- Граф сутностей ---
    Setting("TG_ENTITIES_MORPH_ENABLED", "0", "bool", "Граф сутностей",
            "Вимикач морфо-матчера імен (відмінки/скорочення).",
            "Дефолт вимкнено — мердж не змінює поведінку системи без свідомого ввімкнення."),
    Setting("TG_ENTITIES_NAMES_TTL", "300", "float", "Граф сутностей",
            "TTL (сек) кешу словника назв сутностей.",
            "load_names — 6200 рядків на кожне повідомлення; короткий TTL, щоб нова сутність ловилась без рестарту."),

    # --- RAG retrieval ---
    Setting("RAG_TOP_K", "12", "int", "RAG retrieval", "Скільки чанків повертає /api/memory/ask за замовчуванням.",
            "Дефолт параметра k у /api/memory/ask, коли клієнт не передав свій."),
    Setting("RAG_RECENCY_WEIGHT", "0.25", "float", "RAG retrieval",
            "Вага бусту свіжості поверх RRF-скору.", "Помірна навмисно — RRF лишається головним сигналом."),
    Setting("RAG_RECENCY_HALF_LIFE_DAYS", "180", "float", "RAG retrieval",
            "Піврозпад бусту свіжості (днів).",
            "Через стільки днів буст свіжості (RAG_RECENCY_WEIGHT) спадає вдвічі."),
    Setting("RAG_MAX_PER_MEETING", "3", "int", "RAG retrieval",
            "Максимум чанків з одного мітингу у фінальному топі (диверсифікація).",
            "Стеля в retrieval.py диверсифікує топ — понад неї чанки того самого мітингу відкидаються."),
    Setting("RAG_FTS_CUTOFF_MIN_WORDS", "3", "int", "RAG retrieval",
            "Від скількох слів запиту вмикається м'який FTS relevance-cutoff (T6.8).",
            "Разом з RAG_FTS_MIN_MATCHES вирізає з FTS-кандидатів чанки з надто малою кількістю збігів "
            "термів."),
    Setting("RAG_FTS_MIN_MATCHES", "2", "int", "RAG retrieval",
            "Мінімум реальних збігів термів для запитів довших за cutoff.",
            "Діє лише коли запит довший за RAG_FTS_CUTOFF_MIN_WORDS слів."),
    Setting("RAG_RERANK_POOL_SIZE", "24", "int", "RAG retrieval",
            "Скільки топ-кандидатів (за RRF+recency) прогонити через cross-encoder rerank.",
            "Діє лише коли RECALL_RERANK_ENABLED=1 — розмір пулу перед rerank-моделлю."),
    Setting("RECALL_RERANK_ENABLED", "0", "bool", "RAG retrieval",
            "Вмикає локальний cross-encoder rerank (bge-reranker-v2-m3) для RAG-чату (T6.4).",
            "Лише для app/services/rag.py — не для copilot/categorize/MCP. Дефолт дублюється у config.py "
            "(документально, не усунуто — борг S4/S6)."),
    Setting("RECALL_RERANK_MODEL", "BAAI/bge-reranker-v2-m3", "str", "RAG retrieval",
            "HF-модель cross-encoder rerank.",
            "Діє лише коли RECALL_RERANK_ENABLED=1 — яку HF-модель завантажує reranker.py."),
    Setting("RERANK_DEVICE", "", "str", "RAG retrieval",
            "Пристрій для reranker ('cuda'/'cpu'). Порожньо = автовизначення.",
            "Порожньо — reranker.py бере cuda за наявності torch.cuda.is_available(), інакше cpu."),
    Setting("RAG_COMMENT_WEIGHT", "0.6", "float", "RAG retrieval — коментарі",
            "Множник бусту коментарів відносно kind_weight (correction 1.0 … question 0.4).",
            "Сильний тай-брейкер, не глушилка — щоб на будь-яке питання не видавались самі коментарі."),
    Setting("RAG_COMMENT_CANDIDATE_CAP", "20", "int", "RAG retrieval — коментарі",
            "Скільки кандидатів брати з окремого індексу коментарів.",
            "Стеля кількості кандидатів з comment-індексу перед злиттям з основним RRF."),
    Setting("RAG_COMMENT_MAX_SHARE", "0.34", "float", "RAG retrieval — коментарі",
            "Максимальна частка топу, яку можуть зайняти коментарі.",
            "Без стелі 30 посаджених коментарів забирали 115 слотів зі 120 при k=8 (Волна 2.5)."),
    Setting("RAG_ATTACH_COMMENT_CAP", "8", "int", "RAG retrieval — коментарі",
            "Максимум підшитих коментарів на одну відповідь.",
            "Дефолт cap у функції підшивки коментарів до знахідки — понад нього коментарі відкидаються."),
    Setting("RAG_THREAD_STITCH", "6", "int", "RAG retrieval — нитки",
            "Скільки сусідів по TG-нитці підшивати до знахідки у промпт.",
            "Відповідь — наступна репліка нитки, спільних слів із запитом у неї немає."),
    Setting("RAG_THREAD_MSG_CHARS", "1500", "int", "RAG retrieval — нитки",
            "Стеля символів на одне сусіднє повідомлення у підшивці нитки.",
            "Діє на сусідів; сама знахідка (хіт) ріжеться окремо, до залишку RAG_THREAD_CHARS."),
    Setting("RAG_THREAD_CHARS", "8000", "int", "RAG retrieval — нитки",
            "Стеля символів на всю підшивку нитки (усі повідомлення разом).",
            "Без неї одна довга нитка (156к символів) витісняла інші джерела з промпту."),

    # --- Ембеддинги ---
    Setting("EMBED_MODEL", "intfloat/multilingual-e5-large", "str", "Ембеддинги",
            "Модель ембеддингів (1024-dim, мультимовна).",
            "bge-m3 відкинуто: не має safetensors, конфліктує з torch<2.6 pin (CVE-2025-32434)."),
    Setting("EMBED_DEVICE", "", "str", "Ембеддинги",
            "Пристрій для embeddings ('cuda'/'cpu'). Порожньо = автовизначення.",
            "Порожньо — embeddings.py бере cuda за наявності torch.cuda.is_available(), інакше cpu."),
    Setting("RECALL_VECTOR_WARN_THRESHOLD", "50000", "int", "Ембеддинги",
            "Поріг к-сті embedded-чанків, після якого логується WARNING про масштаб (T6.6).",
            "Brute-force numpy vector search — сигнал планувати міграцію на ANN (sqlite-vec) заздалегідь."),

    # --- OCR / документи ---
    Setting("OCR_LANG", "ukr+rus+eng", "str", "OCR / документи",
            "Мовні пакети Tesseract для OCR документів.",
            "Бракує мовного пакету — pytesseract кидає TesseractError, document_parser.py фолбечить на "
            "дефолтну мову (eng)."),
    Setting("OCR_DPI", "200", "int", "OCR / документи",
            "DPI рендеру сторінки PDF для OCR.",
            "Передається у page.get_pixmap(dpi=...) перед OCR — вищий DPI підвищує якість ціною часу."),
    Setting("OCR_MAX_PAGES", "50", "int", "OCR / документи",
            "Кап синхронного OCR (сторінок).",
            "Понад цю кількість сторінок document_parser.py більше не запускає OCR для документа."),
    Setting("RECALL_IMPORT_ROOTS", "", "str", "OCR / документи",
            "Список абсолютних директорій (розділених ';'), дозволених для import-folder.",
            "Порожньо → import-folder заборонений звідусіль (deny-by-default, T1.3)."),
    Setting("DATABASE", "whisper_history.db", "path", "OCR / документи",
            "Шлях до SQLite БД для standalone CLI-запуску app/services/comments.py.",
            "config.py тримає власний хардкод DATABASE='whisper_history.db' (дубль, non-goal 5 — не усунуто)."),

    # --- Claude API ---
    Setting("ANTHROPIC_API_KEY", "", "secret", "Claude API",
            "Ключ Anthropic. Без нього polish/enrichment/RAG-чат/копілот-верифікація/vision(claude) вимкнені.",
            "https://console.anthropic.com/settings/keys"),
    Setting("CLAUDE_MODEL", "claude-opus-5", "str", "Claude API",
            "Дефолтна модель для тексту/RAG (єдина точка правди — app/services/models.py).",
            "default_model() в app/services/models.py повертає це значення, якщо задане — інакше DEFAULT_MODEL "
            "з коду."),
    Setting("HF_TOKEN", "", "secret", "Claude API",
            "Токен Hugging Face для pyannote (діаризація).",
            "Вимагає прийняття ліцензії моделей pyannote/speaker-diarization-3.1 і pyannote/segmentation-3.0."),

    # --- Копілот ---
    Setting("COPILOT_ENABLED", "1", "bool", "Копілот",
            "Вмикає живий ко-пілот дзвінка (Phase 19).",
            "Деградує м'яко: нема Ollama/моделі → is_available()=False, запис/STT/RAG працюють як раніше.",
            scope="config"),
    Setting("LOCAL_LLM_URL", "http://localhost:11434", "str", "Копілот",
            "HTTP-адреса локального Ollama-сервісу.",
            "Базовий URL, на який local_llm.py шле /api/generate і /api/tags до Ollama.", scope="config"),
    Setting("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct-q5_K_M", "str", "Копілот",
            "Локальна модель-диспетчер копілота.",
            "Модель за замовчуванням для всіх local_llm-викликів (копілот, нитки TG, досьє чату), якщо "
            "виклик не перекрив своєю.", scope="config"),
    Setting("LOCAL_LLM_KEEPALIVE", "30m", "str", "Копілот",
            "Ollama keep_alive для моделі копілота.",
            "Значення keep_alive у запиті до Ollama — скільки тримати модель у пам'яті GPU без вивантаження.",
            scope="config"),
    Setting("LOCAL_LLM_NUM_CTX", "8192", "int", "Копілот",
            "Розмір контексту локальної моделі копілота.",
            "Значення num_ctx у запиті до Ollama — довші промпти за цю межу модель обрізає/деградує.",
            scope="config"),
    Setting("LOCAL_LLM_TIMEOUT", "60", "float", "Копілот",
            "Таймаут виклику локальної моделі копілота (сек).",
            "Дефолтний timeout HTTP-запиту local_llm.py до Ollama, якщо виклик не передав свій.",
            scope="config"),
    Setting("COPILOT_MODEL_API", "claude-sonnet-5", "str", "Копілот",
            "«Звичайна» Claude-модель верифікації важливого (local-first cascade).",
            "Читається щоразу (не кешується) у copilot/config.py — модель для звичайної (не 'гнарлі') "
            "ескалації до Claude."),
    Setting("COPILOT_MODEL_API_GNARLY", "claude-opus-5", "str", "Копілот",
            "Модель-арбітр для 'гнарлі'/uncertain кейсів копілота.",
            "Свідомо ІНША модель за COPILOT_MODEL_API — не схлопувати."),
    Setting("COPILOT_TOPIC_TICK_SEC", "6", "float", "Копілот",
            "Період тіку топік-стейт-машини копілота (сек).",
            "Період циклу _tick_sec у copilot/worker.py — як часто перевіряється стан топіка."),
    Setting("COPILOT_TOPIC_WINDOW_CHARS", "200", "int", "Копілот",
            "Мінімальне вікно символів для оцінки топіку копілота.",
            "_min_window_chars у copilot/worker.py — доки вікно транскрипту менше, оцінка топіка не "
            "запускається."),

    # --- Auth / доступ ---
    Setting("RECALL_BIND_ALL", "", "bool", "Auth / доступ",
            "Безпечний дефолт (Волна 0 / T1.1): слухати лише локальну машину. Вихід на\n"
            "всю мережу — свідомий opt-in: RECALL_BIND_ALL=1 або FLASK_HOST=0.0.0.0.\n"
            "Мережевий bind вимагає явного SECRET_KEY (інакше hard-fail) і, як правило,\n"
            "RECALL_LOCAL_TRUSTED=0 + RECALL_API_KEY (див. нижче).",
            "1 → SECRET_KEY-дефолт стає hard-fail (config.py), а не автоген.", scope="config"),
    Setting("RECALL_LOCAL_TRUSTED", "1", "bool", "Auth / доступ",
            "Запити з localhost не потребують ключа, поки увімкнено.",
            "T1.2: вимкнути (0), якщо застосунок доступний по мережі і ключ треба навіть локально.",
            default_headless="0"),
    Setting("RECALL_API_KEY", "", "secret", "Auth / доступ",
            "Ключ доступу для запитів НЕ з localhost (або RECALL_LOCAL_TRUSTED=0).",
            "Заголовок X-Recall-Api-Key або Authorization: Bearer. Не задано → fail-closed для не-localhost."),

    # --- MCP-сервер ---
    Setting("RECALL_API_URL", "http://127.0.0.1:5050", "str", "MCP-сервер",
            "Адреса живого Flask (app.py), на яку mcp_server.py проксирує write/live/AI-дії.",
            "Базовий API_URL у mcp_server.py — куди йдуть усі проксійовані HTTP-запити до app.py."),
    Setting("RECALL_MCP_READONLY", "", "bool", "MCP-сервер",
            "Аварійний вимикач усіх записів MCP (на читання не впливає).",
            "За замовчуванням вимкнено (порожньо) — окремий тумблер від архітектурного "
            "read-first (32 read-only тулзи)."),
    Setting("RECALL_MCP_KEY", "", "secret", "MCP-сервер",
            "Bearer-ключ для http-транспорту mcp_server.py.",
            "Потрібен ЛИШЕ для --transport http (слухає 127.0.0.1:5060); дефолтний stdio ключа не потребує.\n"
            'Згенерувати: python -c "import secrets; print(secrets.token_urlsafe(32))"'),
    Setting("RECALL_MCP_DEBUG_LOG", "", "str", "MCP-сервер",
            "1/true → логувати кожен виклик MCP-тулзи у logs/mcp_calls.log; або явний шлях.",
            "За замовчуванням вимкнено (нуль-оверхед)."),
    Setting("MCP_PORT", "5060", "int", "MCP-сервер",
            "Порт http-транспорту mcp_server.py.",
            "Діє лише з --transport http; дефолтний stdio-транспорт цей порт не використовує."),

    # --- Логування ---
    Setting("RECALL_LOG_TO_FILE", "0", "bool", "Логування",
            "Вмикає RotatingFileHandler модульного logger'а 'whisper_ui'.",
            "За дефолтом вимкнено — другий хендлер на спільному файлі блокував rollover на Windows (WinError 32)."),
)


def by_name() -> dict[str, Setting]:
    """Реєстр як словник {name: Setting}."""
    return {s.name: s for s in REGISTRY}


_BY_NAME = by_name()


def _lookup(name: str) -> Setting:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"env-змінна {name!r} не зареєстрована у app.core.settings.REGISTRY"
        ) from None


def env(name: str) -> str:
    """Сирий рядок з os.environ, інакше профіле-залежний дефолт з реєстру.

    Резолюція (config-registry-fix-01, Contracts §Auth):
    ``os.environ[name]`` → ``default_headless`` (якщо ``profile() == 'headless'``
    і поле не ``None``) → ``default``. KeyError, якщо назва не зареєстрована.
    """
    setting = _lookup(name)
    if name in os.environ:
        return os.environ[name]
    if setting.default_headless is not None and profile() == "headless":
        return setting.default_headless
    return setting.default


def env_bool(name: str) -> bool:
    """Типізований bool-читач. Truthy-набір: '1'/'true'/'yes'/'on' (регістронезалежно)."""
    return env(name).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str) -> int:
    return int(env(name))


def env_float(name: str) -> float:
    return float(env(name))


def profile() -> str:
    """RECALL_PROFILE → 'desktop' (дефолт) або 'headless'. Інше значення → ValueError."""
    raw = os.environ.get("RECALL_PROFILE", "desktop").strip().lower()
    if raw not in ("desktop", "headless"):
        raise ValueError(
            f"RECALL_PROFILE={raw!r} невідомий — очікується 'desktop' або 'headless'"
        )
    return raw


#: Системні/тестові env-змінні, які тест повноти реєстру пропускає — вони не
#: належать домену Recall (PATH, PYTEST_CURRENT_TEST тощо) і не мають дефолту,
#: осмисленого для .env.example.
IGNORED_ENV_NAMES = frozenset({
    "WERKZEUG_RUN_MAIN",
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "PYTHONPATH",
    "PYTEST_CURRENT_TEST",
})

#: Змінні, що йдуть у згенерований .env.example АКТИВНИМИ (без '#'), бо
#: потрібні для звичайного локального старту — решта коментується як
#: довідкова/опціональна (той самий стиль, що мав ручний .env.example).
_ACTIVE_BY_DEFAULT = frozenset({
    "FLASK_DEBUG", "FLASK_HOST", "FLASK_PORT", "APP_ENV", "SECRET_KEY",
    "FORCE_CPU", "LOG_LEVEL",
})


def render_env_example() -> str:
    """Згенерувати вміст .env.example з REGISTRY."""
    lines: list[str] = [
        "# Recall — приклад .env",
        "# ЗГЕНЕРОВАНО з app/core/settings.py — не редагувати вручну.",
        "# Оновити: .venv/Scripts/python.exe -m app.core.settings env-example > .env.example",
    ]
    seen_groups: list[str] = []
    for s in REGISTRY:
        if s.group not in seen_groups:
            lines.append("")
            lines.append(f"# --- {s.group} ---")
            seen_groups.append(s.group)
        for doc_line in s.doc.splitlines():
            lines.append(f"# {doc_line}" if doc_line else "#")
        if s.gates:
            for gate_line in s.gates.splitlines():
                lines.append(f"# {gate_line}" if gate_line else "#")
        if s.default_headless is not None:
            # Профіле-залежний дефолт: пояснювальний коментар (обидва значення)
            # + один валідний dotenv-рядок (дефолт desktop), закоментований '# '.
            # Розкоментувавши його, оператор отримує синтаксично коректний NAME=value.
            lines.append(
                f"# профілі — desktop={s.default}, headless={s.default_headless}"
            )
            lines.append(f"# {s.name}={s.default}")
        else:
            prefix = "" if s.name in _ACTIVE_BY_DEFAULT else "# "
            lines.append(f"{prefix}{s.name}={s.default}")
    return "\n".join(lines) + "\n"


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.core.settings")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("env-example", help="Друкує згенерований .env.example у stdout")
    args = parser.parse_args(argv)
    if args.command == "env-example":
        sys.stdout.write(render_env_example())


if __name__ == "__main__":
    _main()
