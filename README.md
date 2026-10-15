[Українською](README.uk.md)

# 🧠 Recall — the memory of your calls

A local RAG archive of all your conversations. It transcribes (faster-whisper), diarizes
(pyannote), builds an entity graph, extracts tasks and lets you ask the archive in
natural language. It runs entirely on your machine — nothing goes to the cloud,
except optional Claude calls (polish/enrichment/co-pilot) and a weekly
encrypted backup to Google Drive.

![Phase](https://img.shields.io/badge/phase-19–20-blue)
![Python](https://img.shields.io/badge/python-3.12-green)
![GPU](https://img.shields.io/badge/GPU-CUDA_12.1-red)
![Backend](https://img.shields.io/badge/whisper-faster--whisper-orange)
![Stack](https://img.shields.io/badge/RAG-local-purple)
![Backup](https://img.shields.io/badge/backup-restic+GDrive-success)

> The transcription core is Whisper, so the lower stack, paths and venv are historically
> named `whisper`. The product's face is **Recall**, tagline «the memory of your calls».

## ✨ Features

- 🎙️ **Transcription** — faster-whisper (CTranslate2) with a VAD filter (Silero); openai-whisper as a fallback. A dynamic catalog of ~17 models (tiny → large-v3, distilled, turbo).
- 🗣️ **Diarization** — pyannote, speaker separation + naming on the recording page.
- 🔴 **Recording** — a server-side recorder (microphone + system loopback) with live diarization and live segments over SSE.
- 🎚️ **Parallel recordings with sound isolation** (4.7.0) — two calls at once, each with its own microphone and system sound; system sound per application: «everything / only these / everything except this» (Windows process loopback). Recall shows what is sounding right now and asks «Exclude Zoom from this recording? Keep only Zoom in the other one?» — switching on the fly without stopping the recording. The copilot works on one recording, with the option to move it. The sound source is a separate `AudioSource` interface, for macOS/Linux later.
- 🧩 **Entity graph and tasks** — Claude extracts summary / key points / action items / entities; a task dashboard + directions (categories).
- 🔍 **Vector search + RAG chat** — e5-large embeddings (1024-dim) + FTS5, hybrid retrieval (RRF + recency + diversity), «ask the archive» with streaming.
- 🎬 **YouTube** — audio download via **pytubefix** (public videos, anonymous), optional trimming.
- 📄 **Documents** (Phase 16) — PDF/DOCX/PPTX/XLSX/CSV/MD/images → text in RAG (OCR via Tesseract).
- ✈️ **Telegram** (Phase 17) — listening to a real TG account (Telethon/MTProto) → text/voice/media from selected chats into the same RAG.
- 🤖 **Live call co-pilot** (Phase 19) — a local model (Qwen-14B/Ollama, $0) tracks topics, searches the archive and catches contradictions; Claude verifies what matters (local-first cascade).
- 🔌 **MCP server** (Phase 20) — access to the RAG archive straight from Claude Code / Desktop.
- 💾 **Backup** — a weekly encrypted restic archive on Google Drive.

The UI is the client-side SPA «Recall»: a warm paper aesthetic, the Fraunces serif, an amber
accent, **light theme only**.

## 🚀 Quick start

### 0. What you need

| What | Requirement |
|---|---|
| OS | Windows 10/11. Sound and screen recording work on Windows only; macOS and Linux have not been tested — adapt together with Claude Code |
| GPU | NVIDIA with CUDA 12.1 (tested on an RTX 3090). `large-v3` ≈ 10 GB of video memory, `large-v3-turbo` ≈ 6 GB. Without NVIDIA — `FORCE_CPU=true` in `.env`: it works, but slowly |
| Software | Python 3.12, Git, FFmpeg in PATH |
| Disk space | ≈ 6 GB for `.venv` + ≈ 4 GB of models (Whisper, e5); with diarization ≈ 10 GB or more |
| Time | 40–90 min on a clean machine; the longest part is downloading ≈ 8 GB of packages and models |

> **Easiest — with Claude Code.** Clone the repository, open Claude Code in the folder and ask:
> «Install Recall following README.md, run it and show what it can do». The commands below are what it will run.

### 1. Dependencies

```cmd
REM Clone the repository
git clone https://github.com/Black-coffe/recall.git
cd recall

REM Create a virtual environment
python -m venv .venv

REM Build tools: openai-whisper is built from source and needs pkg_resources
.venv\Scripts\python.exe -m pip install "setuptools<81" wheel

REM PyTorch with CUDA 12.1 (for GPU acceleration) — install BEFORE requirements.txt,
REM the torchaudio version must have the same +cu121 suffix
.venv\Scripts\python.exe -m pip install torch==2.5.1+cu121 torchvision torchaudio==2.5.1+cu121 --index-url https://download.pytorch.org/whl/cu121

REM The rest of the Python dependencies (without build isolation so that setuptools<81 applies)
.venv\Scripts\python.exe -m pip install --no-build-isolation -r requirements.txt

REM Settings: copy the template, then see section 3
copy .env.example .env
```

Check that everything is as it should be (`True`, `2.5.1+cu121` twice, `av` below 19, `pip check` with no conflicts):

```cmd
.venv\Scripts\python.exe -c "import torch, torchaudio, av; print(torch.cuda.is_available(), torch.__version__, torchaudio.__version__, av.__version__)"
.venv\Scripts\python.exe -m pip check
```

> `faster-whisper` is pinned to 1.1.1 on purpose; the app says «update available» — **do not update**
> (the reason is in a comment in `requirements.txt`).

### 2. FFmpeg (required)

Needed for YouTube and for processing long audio.

**Windows:** download it from [ffmpeg.org](https://ffmpeg.org/download.html), unpack
to `C:\ffmpeg`, add `C:\ffmpeg\bin` to PATH. Check: `ffmpeg -version`.

### 3. `.env` — keys and port

Everything is optional: transcription, search and OCR work without any key. In `.env` (created by the command
`copy .env.example .env`) uncomment what you need:

| Variable | What for | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | meeting cards, answers over the archive, co-pilot verification | these features are silently off |
| `HF_TOKEN` | diarization (who is speaking). Token — huggingface.co/settings/tokens; first accept the licenses of [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) and [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) | transcription without speaker separation |
| `FLASK_PORT` | UI port, `5050` by default | 5050 |
| `FORCE_CPU` | `true` — without a GPU | GPU, if present |

### 4. Launch

```cmd
REM Via the launcher: stops ALL running copies of Recall (app.py) and opens http://localhost:5050
Recall.bat

REM Or directly (if you changed FLASK_PORT or keep a second copy)
.venv\Scripts\python.exe app.py
```

Open in the browser: **http://localhost:5050**

> `app.py` is the single entry point: it starts the Telegram listener as a child
> process itself (if there are keys + a session). The MCP server (`mcp_server.py`) is started separately.

### 5. First launch — what to expect

- Models are downloaded on first use, not at install time: Whisper `large-v3-turbo` (≈ 1.5 GB) —
  on the first transcription, e5 (≈ 2.2 GB) — on the first search, pyannote (≈ 1.5 GB) — on the first diarization.
  The first transcription and the first search take 2–3 min each; after that — seconds.
- The first search right after the first transcription may return nothing: the recording is still being indexed. Retry
  in a minute.

### 6. (Optional) Telegram ingestion

```cmd
REM api_id/api_hash keys from https://my.telegram.org → into .env:
REM   TELEGRAM_API_ID=... / TELEGRAM_API_HASH=...

REM One-time QR login (creates telegram.session)
.venv\Scripts\python.exe telegram_login.py
```

After that `app.py` starts the listener automatically; chat selection is in the UI (the «Telegram» tab).

> The README is checked by the test `tests/test_install_docs.py`: every install command must match
> `requirements.txt` and `.env.example`. If you change a dependency, change it here too, otherwise the test is red.

## 🛠️ Technologies

- **Backend**: Python 3.12, Flask 3
- **Transcription**: faster-whisper (CTranslate2), openai-whisper (fallback), PyTorch 2.5.1+cu121 / CUDA 12.1
- **Diarization**: pyannote.audio + speechbrain
- **RAG**: e5-large embeddings (sentence-transformers) + SQLite FTS5, brute-force numpy retrieval
- **YouTube**: pytubefix
- **Telegram**: Telethon (MTProto)
- **Co-pilot LLM**: Ollama / Qwen-14B (local, $0) + Anthropic Claude (verification)
- **Documents**: PyMuPDF, python-pptx, openpyxl, Tesseract (OCR)
- **MCP**: fastmcp
- **Audio**: pydub, FFmpeg, pyaudiowpatch (WASAPI recording)
- **Backup**: restic + rclone (Google Drive)
- **Frontend**: vanilla SPA (client router), Service Worker (offline cache)

## 📊 Performance

faster-whisper (CTranslate2 + VAD) gives a multiple-fold speedup over vanilla Whisper.
On an **RTX 3090**, `medium` / `large-v3` are recommended for quality, `small` / `distil-*`
for speed. Files >10 min automatically go through the batched pipeline; the actual
speed depends on the model, the language and the GPU.

## 📁 Project structure

```
recall/  (Recall)
├── app.py                     # Flask: boot, singletons, registration of 14 blueprints
├── config.py                  # Dev/Prod/Test configs + env flags
├── whisper_manager_new.py     # Transcription engine (faster-whisper / openai fallback)
├── telegram_listener.py       # Separate process: Telegram listener (Telethon/MTProto)
├── telegram_login.py          # One-time QR login to Telegram
├── telegram_common.py         # Shared Flask↔listener token
├── mcp_server.py              # MCP server: RAG archive for Claude (Phase 20)
├── requirements.txt           # Python dependencies
├── run.bat / Recall.bat       # Launch for Windows
├── whisper_history.db         # SQLite database
├── CLAUDE.md                  # Instructions for Claude Code + model routing matrix
├── app/                       # Modular architecture
│   ├── blueprints/            # 14 blueprints (HTTP API)
│   ├── services/              # Business logic (RAG, copilot, recording, …)
│   ├── db/                    # Connection + migrations
│   ├── core/                  # Exceptions, logger
│   └── state.py               # Global singletons
├── templates/
│   └── shell.html             # SPA shell
├── static/
│   ├── js/recall/             # SPA: namespace/router/shell/api/ui + views/*
│   ├── css/recall.css         # Editorial catalog, light theme
│   └── sw.js                  # Service worker (offline cache)
├── docs/
│   ├── map/                   # 🗺️ Code map
│   ├── DESIGN_SYSTEM.md
│   ├── COPILOT_ROADMAP.md
│   └── MCP_SETUP.md
├── uploads/  youtube_downloads/  transcripts/  documents/
├── telegram_media/  recordings/  models/  db_backups/
└── .venv/
```

> 🗺️ **Code map:** [`memory/map/`](memory/map/README.md) — navigation across the modules for
> developers and Claude Code (check it before editing large modules). The technical
> stack reference + the model routing matrix — [`CLAUDE.md`](CLAUDE.md).

## 🔧 API Endpoints (selection)

The routes are split across 14 blueprints in `app/blueprints/*` (the full list is in
[`memory/map/blueprints.md`](memory/map/blueprints.md)).

- **Transcription**: `POST /api/transcribe`, `GET /api/history`, `POST /api/transcription/<id>/{polish,summarize,translate,topics}`, `POST /api/export/<format>`
- **YouTube**: `POST /api/youtube/{info,download}`, `GET /api/youtube/progress/<id>`
- **Audio library**: `GET /api/audio/downloads`, `POST /api/audio/{download,play/<id>}`
- **Recording**: `POST /api/recording/{start,<sid>/stop,<sid>/save}`, `GET /api/recording/<sid>/stream` (SSE); parallel recordings — `GET /api/recordings/active` (`sessions[]`), `GET /api/recording/apps` (what is sounding), `POST /api/recording/<sid>/system-source` (switch the source on the fly), `POST /api/copilot/move`
- **Memory/RAG**: `GET /api/memory/{search,entities,action-items}`, `POST /api/memory/ask/stream` (SSE)
- **Documents/Telegram/Research/Co-pilot/Speakers**: `/api/documents/*`, `/api/telegram/*`, `/api/research/*`, `/api/copilot/*`, `/api/speakers/*`
- **System**: `GET /api/{health,models,system_info,system_stats,metrics}`

## 🐛 Troubleshooting

### The GPU is not used

```cmd
REM Check CUDA
.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"

REM Reinstall PyTorch with CUDA 12.1
.venv\Scripts\python.exe -m pip install torch==2.5.1+cu121 torchvision torchaudio==2.5.1+cu121 --index-url https://download.pytorch.org/whl/cu121
```

Forced CPU mode: the environment variable `FORCE_CPU=true`.

### YouTube does not download

1. Check FFmpeg: `ffmpeg -version`
2. Update pytubefix: `.venv\Scripts\python.exe -m pip install -U pytubefix`
3. Private / age-restricted videos are not supported: the download is anonymous,
   there is no authorization in the code (`cookies.txt` solves nothing — nobody reads it).

### Unicode errors on Windows

The backend uses a logger instead of print; if needed, set the console encoding:
`chcp 65001`.

## 🔒 Security and privacy

- ✅ Data lives on your machine (SQLite + local files)
- ✅ External calls only to the Anthropic API (polish/enrichment/co-pilot) and Google Drive (backup)
- ✅ Path Traversal protection; parameterized SQL queries
- ✅ Input validation; upload size limit (`MAX_CONTENT_LENGTH`, up to 20 GB for video)
- ✅ The Telegram listener and MCP — localhost only + shared-secret / Bearer key

## 📝 License

MIT License

## 🤝 Contributing

Pull requests are welcome! For big changes, open an issue first.

---

**Made with ❤️ for the Ukrainian community**

📧 Support: [open an issue](https://github.com/Black-coffe/recall/issues)
