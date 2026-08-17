# 🧠 Recall — пам'ять ваших дзвінків

Локальний RAG-архів усіх ваших розмов. Транскрибує (faster-whisper), діаризує
(pyannote), будує граф сутностей, витягує задачі та дозволяє питати архів
природною мовою. Працює повністю на вашій машині — нічого не йде у хмару,
окрім опційних викликів Claude (polish/enrichment/co-pilot) та щотижневого
шифрованого backup на Google Drive.

![Phase](https://img.shields.io/badge/phase-19–20-blue)
![Python](https://img.shields.io/badge/python-3.12-green)
![GPU](https://img.shields.io/badge/GPU-CUDA_12.1-red)
![Backend](https://img.shields.io/badge/whisper-faster--whisper-orange)
![Stack](https://img.shields.io/badge/RAG-local-purple)
![Backup](https://img.shields.io/badge/backup-restic+GDrive-success)

> Ядро транскрипції — Whisper, тому нижній стек, шляхи й venv історично
> звуться `whisper`. UI-обличчя — **Recall**, tagline «пам'ять ваших дзвінків».

## ✨ Можливості

- 🎙️ **Транскрипція** — faster-whisper (CTranslate2) з VAD-фільтром (Silero); openai-whisper як fallback. Динамічний каталог ~17 моделей (tiny → large-v3, distilled, turbo).
- 🗣️ **Діаризація** — pyannote, розділення спікерів + іменування на сторінці запису.
- 🔴 **Запис** — серверний recorder (мікрофон + system loopback) з live-діаризацією та live-сегментами через SSE.
- 🧩 **Граф сутностей і задачі** — Claude витягує summary / key points / action items / entities; дашборд задач + напрямки (категорії).
- 🔍 **Vector search + RAG-чат** — e5-large embeddings (1024-dim) + FTS5, гібридний retrieval (RRF + recency + diversity), «питай архів» зі стрімом.
- 🎬 **YouTube** — завантаження аудіо через **pytubefix** (публічні відео, анонімно), опційна обрізка.
- 📄 **Документи** (Phase 16) — PDF/DOCX/PPTX/XLSX/CSV/MD/зображення → текст у RAG (OCR через Tesseract).
- ✈️ **Telegram** (Phase 17) — слухання реального TG-акаунта (Telethon/MTProto) → текст/голос/медіа з обраних чатів у той самий RAG.
- 🤖 **Живий ко-пілот дзвінка** (Phase 19) — локальна модель (Qwen-14B/Ollama, $0) веде теми, шукає в архіві й ловить протиріччя; Claude верифікує важливе (local-first cascade).
- 🔌 **MCP-сервер** (Phase 20) — доступ до RAG-архіву прямо з Claude Code / Desktop.
- 💾 **Backup** — щотижневий шифрований restic-архів на Google Drive.

UI — клієнтський SPA «Recall»: тепла паперова естетика, серіф Fraunces, бурштиновий
акцент, **тільки світла тема**.

## 🚀 Швидкий старт

### 1. Залежності

```cmd
REM Клонуйте репозиторій
git clone https://github.com/Black-coffe/recall.git
cd recall

REM Встановіть Python-залежності
.venv\Scripts\python.exe -m pip install -r requirements.txt

REM PyTorch з CUDA 12.1 (для GPU-прискорення)
.venv\Scripts\python.exe -m pip install torch==2.5.1+cu121 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### 2. FFmpeg (обов'язково)

Потрібен для YouTube та обробки довгих аудіо.

**Windows:** завантажте з [ffmpeg.org](https://ffmpeg.org/download.html), розпакуйте
в `C:\ffmpeg`, додайте `C:\ffmpeg\bin` до PATH.

### 3. Запуск

```cmd
REM Через лаунчер (з очисткою старого процесу на 5050)
Recall.bat

REM Або напряму
.venv\Scripts\python.exe app.py
```

Відкрийте в браузері: **http://localhost:5050**

> `app.py` — єдина точка запуску: він сам піднімає слухача Telegram як дочірній
> процес (якщо є ключі + сесія). MCP-сервер (`mcp_server.py`) запускається окремо.

### 4. (Опційно) Telegram ingestion

```cmd
REM Ключі api_id/api_hash з https://my.telegram.org → у .env:
REM   TELEGRAM_API_ID=... / TELEGRAM_API_HASH=...

REM Одноразовий QR-логін (створює telegram.session)
.venv\Scripts\python.exe telegram_login.py
```

Далі `app.py` автоматично запускає слухача; вибір чатів — у UI (таб «Telegram»).

## 🛠️ Технології

- **Backend**: Python 3.12, Flask 3
- **Транскрипція**: faster-whisper (CTranslate2), openai-whisper (fallback), PyTorch 2.5.1+cu121 / CUDA 12.1
- **Діаризація**: pyannote.audio + speechbrain
- **RAG**: e5-large embeddings (sentence-transformers) + SQLite FTS5, брут-форс numpy retrieval
- **YouTube**: pytubefix
- **Telegram**: Telethon (MTProto)
- **Co-pilot LLM**: Ollama / Qwen-14B (локально, $0) + Anthropic Claude (верифікація)
- **Документи**: PyMuPDF, python-pptx, openpyxl, Tesseract (OCR)
- **MCP**: fastmcp
- **Аудіо**: pydub, FFmpeg, pyaudiowpatch (WASAPI-запис)
- **Backup**: restic + rclone (Google Drive)
- **Frontend**: vanilla SPA (client router), Service Worker (offline-кеш)

## 📊 Продуктивність

faster-whisper (CTranslate2 + VAD) дає кратне прискорення проти ванільного Whisper.
На **RTX 3090** рекомендовані `medium` / `large-v3` для якості, `small` / `distil-*`
для швидкості. Файли >10 хв автоматично йдуть через batched-pipeline; фактична
швидкість залежить від моделі, мови та GPU.

## 📁 Структура проекту

```
recall/  (Recall)
├── app.py                     # Flask: boot, singletons, реєстрація 14 blueprints
├── config.py                  # Конфіги Dev/Prod/Test + env-прапори
├── whisper_manager_new.py     # Двигун транскрипції (faster-whisper / openai fallback)
├── telegram_listener.py       # Окремий процес: слухач Telegram (Telethon/MTProto)
├── telegram_login.py          # Одноразовий QR-логін у Telegram
├── telegram_common.py         # Спільний токен Flask↔listener
├── mcp_server.py              # MCP-сервер: RAG-архів для Claude (Phase 20)
├── requirements.txt           # Python-залежності
├── run.bat / Recall.bat       # Запуск для Windows
├── whisper_history.db         # SQLite база
├── CLAUDE.md                  # Інструкції для Claude Code + routing-матриця
├── app/                       # Модульна архітектура
│   ├── blueprints/            # 14 blueprints (HTTP API)
│   ├── services/              # Бізнес-логіка (RAG, copilot, recording, …)
│   ├── db/                    # Підключення + міграції
│   ├── core/                  # Винятки, логер
│   └── state.py               # Глобальні singleton-и
├── templates/
│   └── shell.html             # SPA-оболонка
├── static/
│   ├── js/recall/             # SPA: namespace/router/shell/api/ui + views/*
│   ├── css/recall.css         # Editorial-каталог, світла тема
│   └── sw.js                  # Service worker (offline-кеш)
├── docs/
│   ├── map/                   # 🗺️ Карта коду
│   ├── DESIGN_SYSTEM.md
│   ├── COPILOT_ROADMAP.md
│   └── MCP_SETUP.md
├── uploads/  youtube_downloads/  transcripts/  documents/
├── telegram_media/  recordings/  models/  db_backups/
└── .venv/
```

> 🗺️ **Карта коду:** [`docs/map/`](docs/map/README.md) — навігація по модулях для
> розробників і Claude Code (звіряти перед правкою великих модулів). Технічний
> довідник стеку + routing-матриця моделей — [`CLAUDE.md`](CLAUDE.md).

## 🔧 API Endpoints (вибірка)

Маршрути розподілені по 14 blueprints у `app/blueprints/*` (повний перелік — у
[`docs/map/blueprints.md`](docs/map/blueprints.md)).

- **Транскрипція**: `POST /api/transcribe`, `GET /api/history`, `POST /api/transcription/<id>/{polish,summarize,translate,topics}`, `POST /api/export/<format>`
- **YouTube**: `POST /api/youtube/{info,download}`, `GET /api/youtube/progress/<id>`
- **Аудіотека**: `GET /api/audio/downloads`, `POST /api/audio/{download,play/<id>}`
- **Запис**: `POST /api/recording/{start,<sid>/stop,<sid>/save}`, `GET /api/recording/<sid>/stream` (SSE)
- **Памʼять/RAG**: `GET /api/memory/{search,entities,action-items}`, `POST /api/memory/ask/stream` (SSE)
- **Документи/Telegram/Дослідження/Co-pilot/Спікери**: `/api/documents/*`, `/api/telegram/*`, `/api/research/*`, `/api/copilot/*`, `/api/speakers/*`
- **Система**: `GET /api/{health,models,system_info,system_stats,metrics}`

## 🐛 Вирішення проблем

### GPU не використовується

```cmd
REM Перевірте CUDA
.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"

REM Перевстановіть PyTorch з CUDA 12.1
.venv\Scripts\python.exe -m pip install torch==2.5.1+cu121 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

Примусовий CPU-режим: змінна оточення `FORCE_CPU=true`.

### YouTube не завантажується

1. Перевірте FFmpeg: `ffmpeg -version`
2. Оновіть pytubefix: `.venv\Scripts\python.exe -m pip install -U pytubefix`
3. Приватні / age-restricted відео не підтримуються: завантаження анонімне,
   авторизації в коді нема (`cookies.txt` нічого не вирішує — його ніхто не читає).

### Помилки Unicode в Windows

Бекенд використовує logger замість print; за потреби виставте кодування консолі:
`chcp 65001`.

## 🔒 Безпека та приватність

- ✅ Дані лежать на вашій машині (SQLite + локальні файли)
- ✅ Зовнішні виклики тільки до Anthropic API (polish/enrichment/co-pilot) і Google Drive (backup)
- ✅ Захист від Path Traversal; параметризовані SQL-запити
- ✅ Валідація вхідних даних; обмеження розміру завантаження (`MAX_CONTENT_LENGTH`, до 20 GB для відео)
- ✅ Telegram-слухач і MCP — лише localhost + shared-secret / Bearer-ключ

## 📝 Ліцензія

MIT License

## 🤝 Внесок

Pull requests вітаються! Для великих змін спочатку відкрийте issue.

---

**Створено з ❤️ для української спільноти**

📧 Підтримка: [створіть issue](https://github.com/Black-coffe/recall/issues)
