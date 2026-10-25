"""Registry of all Recall env variables (spec `config-registry-profiles`, story 01).

Single source of truth for the name/default/type/group/profile/purpose
of every env flag the code reads. Used for:

  - generating ``.env.example`` (``python -m app.core.settings env-example``);
  - the completeness test (``tests/test_settings_registry.py``) — every name
    found by static scanning of ``os.environ.get/getenv/[...]`` in the code
    (``app/``, ``app.py``, ``config.py``, ``mcp_server.py``,
    ``telegram_listener.py``, ``telegram_login.py``, ``whisper_manager_new.py``),
    has an entry here;
  - ``config.py::get_config()`` — takes string defaults from here instead of
    duplicated literals scattered across the ``Config`` class.

The module does NOT import ``app.*``, ``flask``, ``torch``, ``config`` — it must stay
loadable anywhere without heavy dependencies (note
``mcp-stdio-no-heavy-models``).

It does NOT rewrite ``os.environ.get(...)`` calls in services (Law 2 VULYK,
non-goal of the plan `config-registry-profiles`) — most entries here only
DOCUMENT the existing read; the read itself stays in place in the service.
Duplicated defaults between ``config.py`` and a service (e.g. ``RECALL_RERANK_ENABLED``)
are not removed — that is debt for later stories, recorded in ``gates``.
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
    """One registry entry.

    ``gates`` — a mandatory phrase separate from ``doc``: what exactly this flag
    turns on/off/limits. ``group`` defines the section of the generated
    ``.env.example`` (section order = order of first appearance in ``REGISTRY``).
    ``scope`` — 'config' if the value is read by ``config.py`` in ``Config``
    (and exposed through ``get_config()``), 'runtime' — if a service reads
    ``os.environ`` directly at call time.
    """

    name: str
    default: str
    kind: Kind
    group: str
    doc: str
    gates: str
    profile: ProfileTag = "all"
    scope: Scope = "runtime"
    #: Profile-dependent default (config-registry-fix-01, Contracts §Auth).
    #: Not ``None`` → in the ``headless`` profile it overrides ``default`` (if the
    #: env variable is not set explicitly). The last dataclass field on purpose — so that all
    #: existing positional ``Setting(...)`` calls stay valid.
    default_headless: str | None = None


REGISTRY: tuple[Setting, ...] = (
    # --- Server / profile ---
    Setting("FLASK_DEBUG", "false", "bool", "Server",
            "Flask debug mode flag.",
            "DEBUG=True allows the default SECRET_KEY without a hard-fail; DEBUG=False requires a real key.",
            scope="config"),
    Setting("FLASK_HOST", "127.0.0.1", "str", "Server",
            "Safe default (Wave 0/T1.1) — listen only on the local machine.",
            "A network bind (0.0.0.0) requires an explicit SECRET_KEY, otherwise config.py hard-fails.",
            scope="config"),
    Setting("FLASK_PORT", "5050", "int", "Server",
            "Flask server port.", "The port app.py listens on.", scope="config"),
    Setting("RECALL_UI_LANG", "auto", "str", "Server",
            "Interface language: auto (by system language) | uk | en.",
            "auto: system uk → Ukrainian, otherwise English; uk/en — an explicit choice, stronger than auto. "
            "Changed in Settings without a restart."),
    Setting("APP_ENV", "development", "str", "Server",
            "Choice of configuration class.",
            "development|production|testing → DevelopmentConfig|ProductionConfig|TestingConfig.",
            scope="config"),
    Setting("SECRET_KEY", "dev-secret-key-change-in-production", "secret", "Server",
            "Flask session key.",
            "Default/empty + DEBUG=False → autogen and write to .env; the same + a network bind → hard-fail.",
            scope="config"),
    Setting("FORCE_CPU", "false", "bool", "Server",
            "Force CPU for whisper/embeddings/reranker.",
            "true → the GPU is not used even when CUDA is available.", scope="config"),
    Setting("LOG_LEVEL", "INFO", "str", "Server",
            "Application log level.", "Passed to basicConfig/RotatingFileHandler.",
            scope="config"),
    Setting("RECALL_PROFILE", "desktop", "str", "Server",
            "desktop — the full application (recorder, SPA shell); headless — API/ingest only.",
            "headless turns off RECORDING_ENABLED/RECORDING_VIDEO_ENABLED and the SPA shell/sw.js (404 'headless'); "
            "any other value → ValueError from profile().",
            scope="config"),
    Setting("RECALL_LOG_FORMAT", "text", "str", "Server",
            "text|json — log line format.",
            "json turns on JsonFormatter on the file+stream handlers in app.py.",
            ),

    # --- Transcription (whisper) ---
    Setting("WHISPER_BACKEND", "faster", "str", "Transcription",
            "faster (CTranslate2, default) or openai (legacy fallback).",
            "Selects the backend in whisper_manager_new.py.", scope="config"),
    Setting("WHISPER_MAX_PARALLEL", "1", "int", "Transcription",
            "Size of the parallel transcription semaphore.",
            "1 — faster-whisper is not thread-safe on a shared model handle (whisper_manager_new.py); "
            "live streams of recordings and full transcription run one after another.", scope="config"),
    Setting("WHISPER_BATCH_SIZE", "8", "int", "Transcription",
            "batch_size for BatchedInferencePipeline on long files (faster backend).",
            "Affects faster-whisper only.", scope="config"),
    Setting("WHISPER_MODEL_CACHE_SIZE", "3", "int", "Transcription",
            "LRU cache of several simultaneously loaded whisper models (e.g. 'small' live + 'medium' full).",
            "Prevents evict/reload of the model on CUDA between live and full transcription."),
    Setting("RECALL_PRELOAD_WHISPER", "1", "bool", "Transcription",
            "Warm up the whisper model in the background right after the application starts.",
            "0 → no warm-up, the first transcription request is cold."),
    Setting("RECALL_PRELOAD_WHISPER_MODEL", "large-v3", "str", "Transcription",
            "Which model to warm up.", "Mirrors the default transcription model (large-v3, whisper-best-default)."),
    Setting("RECALL_VOCAB_MODE", "hotwords", "str", "Transcription",
            "Vocabulary of \"hard\" names from the entity graph (app/services/vocabulary.py, K1): off | hotwords | initial_prompt. "
            "Default per the K1 measurement: names 15 % → 35 %, +11 % transcription time.",
            "hotwords/initial_prompt → ≈50 names (≤223 tokens) go to faster-whisper on files, recordings, live "
            "transcription and TG voice messages; YouTube never. off — the call as before K1."),
    Setting("WHISPER_MULTILINGUAL", "1", "bool", "Transcription",
            "Language detection on every window of up to 30 s of speech (not per phrase) — faster-whisper multilingual. "
            "Default per the K1 blind pairs (7 of 10).",
            "1 → the language is detected on every window in full transcription, files and TG voice messages, but only "
            "when no language is chosen (auto); an explicit language (uk/ru/en) is not overridden (4.8.1). Live transcription and "
            "YouTube do not get it."),

    # --- Rate limits ---
    Setting("RATE_LIMIT_TRANSCRIBE", "60/minute", "str", "Rate limits",
            "Flask-limiter limit for /api/transcribe.", "Flask-limiter format (N/interval)."),
    Setting("RATE_LIMIT_YOUTUBE", "30/minute", "str", "Rate limits",
            "Limit for YouTube endpoints.", "Flask-limiter format (N/interval)."),
    Setting("RATE_LIMIT_POLISH", "20/hour", "str", "Rate limits",
            "Limit for Claude polish requests.", "Flask-limiter format (N/interval)."),
    Setting("RATE_LIMIT_RECORDING_START", "10/minute", "str", "Rate limits",
            "Limit on recording starts (Phase 9.10 hardening).",
            "Each session is ~10MB/min on disk — a low limit prevents script-driven abuse."),

    # --- Executors (job queue) ---
    Setting("LIVE_EXECUTOR_WORKERS", "1", "int", "Executors",
            "Pool size for latency-critical jobs (recording_finalize).",
            "Usually one recorder at a time — 1 worker is always free, does not wait for a batch."),
    Setting("BATCH_EXECUTOR_WORKERS", "2", "int", "Executors",
            "Pool size for the rest of the jobs (tg_transcribe, video_analysis, etc.).",
            "These jobs can load the GPU (faster-whisper) — deliberately not increased."),

    # --- Auto cleanup ---
    Setting("AUTO_CLEANUP_ENABLED", "false", "bool", "Auto cleanup",
            "Turns on periodic deletion of old temporary files (Phase 6.3).",
            "Off by default — so a local user does not lose files by accident."),
    Setting("AUTO_CLEANUP_DAYS", "7", "int", "Auto cleanup",
            "Age of files (days) beyond which they are deleted.", "Applies only if AUTO_CLEANUP_ENABLED=true."),

    # --- Recording (recorder) ---
    Setting("RECORDING_ENABLED", "", "bool", "Recording",
            "Autodetect via the pyaudiowpatch import; '0'/'false' forces it off.",
            "headless is always False without trying to import pyaudiowpatch (Assumption 3).", scope="config",
            profile="desktop"),
    Setting("RECORDING_CHUNK_SECONDS", "5", "int", "Recording",
            "Recording session chunk size.", "Raw PCM is written in chunks for crash resilience.", scope="config"),
    Setting("RECORDING_SAMPLE_RATE", "48000", "int", "Recording",
            "Recording sample rate.", "WASAPI loopback + mic.", scope="config"),
    Setting("RECORDING_CHANNELS", "2", "int", "Recording", "Number of recording channels.",
            "Passed to RecordingService.start() as channels — the number of mic+system mixdown tracks.",
            scope="config"),
    Setting("RECORDING_MP3_BITRATE", "192k", "str", "Recording",
            "Bitrate of the final MP3 at mixdown.",
            "Goes to finalize_session() as the encoding bitrate of final.mp3.", scope="config"),
    Setting("RECORDING_KEEP_PCM", "False", "bool", "Recording",
            "Do not delete the intermediate raw PCM after finalization.", "true — takes much more disk.",
            scope="config"),
    Setting("RECORDING_KEEP_WAV", "False", "bool", "Recording",
            "Keep the separate mic.wav/system.wav after mixdown into final.mp3.",
            "Deleted by default — otherwise ~1GB/session of dead space.", scope="config"),
    Setting("RECORDING_MAX_ACTIVE", "2", "int", "Recording",
            "Maximum simultaneous recordings (minimum 1).",
            "Applies only if RECORDING_ENABLED; one more start over the limit → 409 SESSION_LIMIT "
            "(RecordingService(max_active=...)).", scope="config", profile="desktop"),
    Setting("RECORDING_MIN_DISK_MB", "500", "int", "Recording",
            "Minimum free space (MB) below which recording does not start.",
            "Below it — recording/screens.py refuses to start a recording with a message about low disk space.",
            scope="config"),
    Setting("RECORDING_AUTOPROMPT", "1", "bool", "Recording",
            "Call prompts (K2, app/services/recording/call_watch.py): \"Record?\" when an application "
            "from the list has both microphone and sound active for 30 s; \"Stop?\" when the call is over.",
            "1 → in the app.py process (under __main__) it looks at audio sessions every second and sends a Windows notification; "
            "a recording never starts without a click. 0 → the thread does not start, GET /api/recording/autoprompt "
            "returns enabled:false.", profile="desktop"),
    Setting("RECORDING_AUTOSAVE_SEC", "60", "int", "Recording",
            "Seconds after a recording stops before the server saves it itself (recording-autosave).",
            "If nobody saved, discarded or is editing the stopped recording by then, the server transcribes it, "
            "titles it and files it when confident; the save screen counts down to the same moment. 0 → off.",
            profile="desktop"),
    Setting("RECORDING_AUTOSAVE_MODEL", "large-v3", "str", "Recording",
            "Whisper model of the server autosave.", "Only the autosave path; the save screen keeps its own choice.",
            profile="desktop"),
    Setting("RECORDING_AUTOSAVE_LANGUAGE", "auto", "str", "Recording",
            "Language of the server autosave transcription ('auto' = detect).",
            "The live-preview language is not used: it is a preview choice, and calls are often spoken in another "
            "language.", profile="desktop"),
    Setting("RECORDING_AUTOSAVE_MIN_CONFIDENCE", "0.6", "float", "Recording",
            "Minimum k-NN confidence for the autosave to set the direction itself.",
            "Below it the direction stays empty and the Audio library asks the user.", profile="desktop"),
    Setting("RECORDING_AUTOPROMPT_APPS",
            "zoom.exe,ms-teams.exe,teams.exe,telegram.exe,viber.exe,whatsapp.root.exe,whatsapp.exe,"
            "chrome.exe,msedge.exe,firefox.exe,discord.exe,slack.exe,signal.exe", "str", "Recording",
            "Calling applications for K2 prompts: exe names separated by commas (case-insensitive, \".exe\" may be omitted).",
            "Only these applications can trigger \"Record?\"; dictation tools (VoiceDrop, Wispr Flow) must not be added here "
            "— their conversation with the microphone is not a call.", profile="desktop"),
    Setting("RECORDING_AUTOPROMPT_SECONDS", "30", "int", "Recording",
            "How many seconds in a row the calling app must have both microphone and sound active (with voice on both "
            "sides) before \"Record?\" appears.",
            "Less → the prompt comes sooner, but more chance of a false one; works only with RECORDING_AUTOPROMPT.",
            profile="desktop"),
    Setting("RECORDING_AUTOSTOP_SILENCE_MIN", "10", "int", "Recording",
            "Minutes of silence on both tracks after \"Stop?\" after which the recording stops by itself.",
            "Applies only to recordings that have already been asked \"Stop?\"; the recording is kept in full, "
            "silence is not counted while paused.", profile="desktop"),

    # --- Video recording (screen capture) ---
    Setting("RECORDING_VIDEO_ENABLED", "True", "bool", "Video recording",
            "Phase 22: screen capture (ddagrab → NVENC).",
            "Turn off if there is no NVIDIA GPU; headless is always False (Assumption 3).", scope="config",
            profile="desktop"),
    Setting("RECORDING_FFMPEG_PATH", r"C:\ffmpeg\bin\ffmpeg.exe", "path", "Video recording",
            "Path to ffmpeg for the video pipeline.",
            "If not found at the path — video_probe.ffmpeg_path() raises an error with a hint to install ffmpeg.",
            scope="config"),
    Setting("RECORDING_VIDEO_FPS", "30", "int", "Video recording", "Video recording FPS.",
            "The FPS with which VideoCaptureSupervisor launches ffmpeg screen capture.", scope="config"),
    Setting("RECORDING_VIDEO_CODEC", "h264_nvenc", "str", "Video recording",
            "NVENC codec for video recording.",
            "The codec passed to VideoCaptureSupervisor — nvenc-specific, requires an NVIDIA GPU.",
            scope="config"),
    Setting("RECORDING_VIDEO_QUALITY", "p5", "str", "Video recording",
            "nvenc -preset.",
            "Passed as -preset to VideoCaptureSupervisor (nvenc).", scope="config"),
    Setting("RECORDING_VIDEO_CQ", "23", "int", "Video recording",
            "Constant quality factor of nvenc.",
            "Passed as -cq to VideoCaptureSupervisor; a lower value means higher quality and a larger file.",
            scope="config"),
    Setting("RECORDING_VIDEO_MAX_TRACKS", "3", "int", "Video recording",
            "Maximum simultaneous video tracks (monitors/windows).",
            "Declared in config.py, but no service reads it — the limit on the number of tracks is effectively "
            "not applied.", scope="config"),
    Setting("RECORDING_VIDEO_STOP_TIMEOUT_SEC", "8", "float", "Video recording",
            "Timeout for waiting for the video process to stop.",
            "How long to wait for the graceful stop of ffmpeg (proc.wait) in _graceful_stop_proc() before terminate/kill.",
            scope="config"),
    Setting("RECORDING_BUILD_MASTER_MP4", "False", "bool", "Video recording",
            "Build a master MP4 from all tracks after completion.",
            "true — finalize_video() additionally builds master.mp4; false — only the separate tracks remain.",
            scope="config"),
    Setting("RECORDING_REGION_OVERLAY", "True", "bool", "Video recording",
            "Show an overlay of the selected capture region.",
            "false — VideoCaptureSupervisor does not draw the capture region overlay on the screen during recording.",
            scope="config"),
    Setting("RECORDING_REGION_OVERLAY_COLOR", "#e5484d", "str", "Video recording",
            "Colour of the capture region overlay.",
            "Applies only when RECORDING_REGION_OVERLAY=true.", scope="config"),
    Setting("RECORDING_VIDEO_ANALYSIS_MAX_FRAMES", "1500", "int", "Video recording",
            "Maximum key frames per video analysis (MAX_FRAMES).",
            "Above the ceiling the shortest segments are merged with their neighbours (protection against \"flickering\" video); "
            "the result is marked capped. See docs/video-timeline.md.",
            scope="config"),

    # --- Vision (frame description) ---
    Setting("VIDEO_VISION_BACKEND", "local", "str", "Vision",
            "local (Ollama VL, $0/offline, default), claude (Anthropic vision, paid), off.",
            "Everything degrades: no model/key → the description is empty, frames+OCR work as before.", scope="config"),
    Setting("VIDEO_VISION_MODEL_LOCAL", "qwen2.5vl:7b", "str", "Vision",
            "Local Ollama VL model for frame description.",
            "Applies only when VIDEO_VISION_BACKEND=local — the model that video_vision.py calls via Ollama.",
            scope="config"),
    Setting("VIDEO_VISION_MODEL_CLAUDE", "claude-haiku-4-5", "str", "Vision",
            "Claude vision model (if backend=claude).",
            "Applies only when VIDEO_VISION_BACKEND=claude — the model that video_vision.py calls via the Anthropic API.",
            scope="config"),
    Setting("VIDEO_VISION_MAX_FRAMES", "120", "int", "Vision",
            "Maximum frames per vision description.",
            "After the ceiling the rest of the session's frames keep an empty vision description (OCR only).", scope="config"),
    Setting("VIDEO_VISION_KEEPALIVE", "60s", "str", "Vision",
            "How long Ollama keeps the local vision model in VRAM after a frame.",
            "The local_llm default is 30 min; 60 s frees ~5 GB a minute after a batch of frames, while 0 would reload "
            "the model on every frame (measured 14.10.2026: 5.5–7.6 s/frame versus 2.3–2.5)."),
    Setting("VIDEO_VISION_TIMEOUT", "90", "float", "Vision",
            "Timeout of the vision model call (seconds).",
            "Timeout of the HTTP call describe_frame() to Ollama/Claude — going over it leaves the frame description "
            "empty, best-effort.", scope="config"),

    # --- Telegram: account listener ---
    Setting("TELEGRAM_API_ID", "0", "int", "Telegram", "api_id from https://my.telegram.org/apps.",
            "Together with TELEGRAM_API_HASH + an installed telethon it turns on TELEGRAM_ENABLED.", scope="config"),
    Setting("TELEGRAM_API_HASH", "", "secret", "Telegram", "api_hash from https://my.telegram.org/apps.",
            "A secret — not in code.", scope="config"),
    Setting("TELEGRAM_SESSION", "telegram", "path", "Telegram",
            "Base name of the Telethon session file (the real default is BASE_DIR/telegram, Telethon adds .session).",
            "The SQLite session file cannot be opened by two clients at the same time.", scope="config"),
    Setting("TELEGRAM_CONTROL_HOST", "127.0.0.1", "str", "Telegram",
            "Host of the localhost control API of the child listener process.",
            "On this host telegram_listener.py brings up the control HTTP server; app.py contacts the same "
            "address.", scope="config"),
    Setting("TELEGRAM_CONTROL_PORT", "5051", "int", "Telegram",
            "Port of the listener's localhost control API.", "Flask proxies list-dialogs/status/backfill here.",
            scope="config"),
    Setting("TELEGRAM_WHISPER_MODEL", "large-v3", "str", "Telegram",
            "Whisper model for voice/audio/video from TG.",
            "large-v3 — best measured accuracy; large-v3-turbo is ~2.4x faster at slightly lower accuracy.", scope="config"),
    Setting("TELEGRAM_WHISPER_LANG", "uk", "str", "Telegram",
            "Transcription language of TG media ('auto' — autodetect).",
            "Passed to the transcription of TG voice/audio in app/blueprints/telegram.py as the language parameter.",
            scope="config"),
    Setting("TELEGRAM_CATCHUP", "1", "bool", "Telegram",
            "Catch-up of messages that arrived while the listener was not running.",
            "0 turns it off — on live data, 30 lost working days in 8 months without it."),
    Setting("TELEGRAM_CATCHUP_DEBOUNCE_MIN", "15", "float", "Telegram",
            "Do not catch up a chat if it was already caught up less than N minutes ago.",
            "app.py starts dozens of times a day during development."),
    Setting("TELEGRAM_DIALOG_LIMIT", "500", "int", "Telegram",
            "How many dialogs to list (/dialogs, /coverage, catch-up).",
            "200 hit the ceiling — \"the chat is not among the dialogs\" was indistinguishable from \"did not fit the window\"."),
    Setting("TELEGRAM_BACKFILL_DELAY", "0.5", "float", "Telegram",
            "Pause (sec) between messages during backfill/repair/catch-up.",
            "Delay between messages in telegram_listener.py — prevents FloodWaitError from the Telegram API."),
    Setting("TELEGRAM_SELF_NAME", "", "str", "Telegram",
            "The owner's name in the chat (who \"I\" am).",
            "Taken from the listener automatically; set manually only if the listener is not used."),
    Setting("TELEGRAM_SELF_USERNAME", "", "str", "Telegram",
            "The owner's nickname (without @).", "Needed to catch @mentions without the nickname from the listener session."),
    Setting("TELEGRAM_ORIGINAL_MAX_MB", "100", "int", "Telegram",
            "Threshold (MB) above which voice/audio originals are deleted after text extraction.",
            "tg-media-policy-02: owner decision — ONLY originals heavier than the threshold are deleted, "
            "small ones stay on disk; applies only if text extraction succeeded."),

    # --- Mail (Gmail ingest) ---
    Setting("MAIL_POLL_ENABLED", "1", "bool", "Mail",
            "Poll the connected Gmail accounts in the background.",
            "Off stops the periodic sync of the enabled labels; accounts and archived mail stay."),
    Setting("MAIL_POLL_MINUTES", "5", "int", "Mail",
            "Interval (minutes) between mail polls.",
            "How often the poller checks the enabled labels for new messages."),
    Setting("MAIL_BACKFILL_MONTHS", "12", "int", "Mail",
            "How many months of history a newly enabled label loads.",
            "Limits the first sync of a label; deeper history is loaded on request."),
    Setting("MAIL_ATTACH_MAX_MB", "25", "int", "Mail",
            "Largest attachment (MB) that is ingested as a document.",
            "Heavier attachments are skipped."),

    # --- Telegram chat threads ---
    Setting("TG_THREAD_BURST_GAP_MIN", "180", "int", "Telegram threads",
            "Pause (min) after which a conversation counts as a new burst.",
            "180 — from a distribution measurement: at 30 min a third of bursts degenerate into singletons, at 1440 min several working days get in."),
    Setting("TG_THREAD_BATCH_MSGS", "25", "int", "Telegram threads",
            "How many messages at a time to give the model for splitting a burst.",
            "Together with TG_THREAD_BATCH_CHARS it divides a burst into sub-batches — whichever of the two limits hits first applies."),
    Setting("TG_THREAD_BATCH_CHARS", "6000", "int", "Telegram threads",
            "Character ceiling per sub-batch of burst splitting.",
            "Without slicing, long bursts (up to 338k characters) do not fit in the model context."),
    Setting("TG_THREAD_CANDIDATES", "5", "int", "Telegram threads",
            "How many open chat threads to show the model as continuation candidates.",
            "Default limit in open_threads() (tg_threads.py) — how many candidates the splitting model sees."),
    Setting("TG_THREAD_MODEL", "", "str", "Telegram threads",
            "Model for splitting bursts into threads. Empty = take LOCAL_LLM_MODEL.",
            "7B gave the same split as 32B in a quarter of the time."),
    Setting("TG_THREAD_IDLE_DAYS", "3", "int", "Telegram threads",
            "How many days without new messages a thread stays a continuation candidate.",
            "3, not 21 — in a run with a 21-day window the maximum gap hit exactly the window edge, not the content."),
    Setting("TG_THREAD_MAX_MSGS", "30", "int", "Telegram threads",
            "Soft ceiling of thread volume (forbids a NEW conversation, does not break the current one).",
            "Without it a few threads collected a large share of the archive, the largest — messages over two months."),

    # --- Telegram thread summaries ---
    # Read by app/services/summaries.py:60-63 (production-rag-wave-b, story 02).
    # The entries DOCUMENT the existing os.environ.get — defaults are copied from there
    # verbatim, the reads themselves were not rewritten (see the module docstring).
    Setting("TG_SUMMARY_PREVIEW", "800", "int", "Thread summaries",
            "Preview of one message (characters) in the thread summary prompt.",
            "Truncates every message that summaries.py puts into the prompt: the preview shows the shape of "
            "the conversation, solid text would show only the start of one attached document."),
    Setting("TG_SUMMARY_MAX_CHARS", "20000", "int", "Thread summaries",
            "Character ceiling of the whole thread summary prompt.",
            "_thread_messages() stops collecting messages as soon as the sum of previews reaches this limit."),
    Setting("TG_SUMMARY_MAX_MSGS", "200", "int", "Thread summaries",
            "How many thread messages to give the model at most.",
            "Default limit in _thread_messages() (summaries.py) — the second limit of the same pair "
            "with TG_SUMMARY_MAX_CHARS, whichever of the two hits first."),
    Setting("TG_SUMMARY_MODEL", "", "str", "Thread summaries",
            "Model for thread summaries. Empty = the project's default model.",
            "Empty — summaries.py takes models.get_default_model()."),

    # --- Telegram chat dossier ---
    Setting("TG_CONTEXT_MIN_NEW", "30", "int", "Chat dossier",
            "How many new messages to accumulate before rebuilding the chat profile.",
            "Rebuilding the dossier in tg_chat_context.py does not start until this many new messages accumulate "
            "(or TG_CONTEXT_MAX_AGE_H passes)."),
    Setting("TG_CONTEXT_MAX_AGE_H", "24", "int", "Chat dossier",
            "Refresh even a silent chat once per this many hours.",
            "The second threshold of the same pair with TG_CONTEXT_MIN_NEW — fires even without new messages."),
    Setting("TG_CONTEXT_THREADS", "12", "int", "Chat dossier",
            "How many latest threads to show the model when building the dossier.",
            "A dossier about the current state of affairs, not a chronicle."),
    Setting("TG_CONTEXT_THREAD_SAMPLE", "4", "int", "Chat dossier",
            "How many thread messages to give the model for analysis.",
            "LIMIT in the SQL query that samples thread messages for building the dossier (tg_chat_context.py)."),
    Setting("TG_CONTEXT_MODEL", "", "str", "Chat dossier",
            "Model for building the chat dossier. Empty = take LOCAL_LLM_MODEL.",
            "32B on a 24GB GPU is unsuitable for this task — measured, one chat did not finish even in 300s."),
    Setting("TG_CONTEXT_TIMEOUT", "300", "float", "Chat dossier",
            "Dossier building timeout (sec).", "A batch task, the default 60s of local_llm is too small here."),

    # --- Commitments from chat ---
    Setting("TG_TASKS_TRIAGE_MODEL", "qwen2.5:7b-instruct", "str", "Commitments from chat",
            "Local model for filtering out threads without agreements.",
            "7B class: triage is a yes/no decision, the cheapest task of the cascade."),
    Setting("TG_TASKS_TRIAGE_TIMEOUT", "180", "float", "Commitments from chat",
            "Timeout of the triage call.",
            "Passed to the local_llm triage call (max_tokens=200) in tg_tasks.py."),
    Setting("TG_TASKS_TRIAGE_PREVIEW", "400", "int", "Commitments from chat",
            "Message preview characters for triage.",
            "The character ceiling of _preview() for each message that goes into the triage prompt."),
    Setting("TG_TASKS_TRIAGE_MAX_MSGS", "40", "int", "Commitments from chat",
            "How many thread messages to show triage.",
            "Default limit in thread_messages() — how many latest messages both triage and the structured "
            "extraction see."),
    Setting("TG_TASKS_MODEL", "", "str", "Commitments from chat",
            "Claude model for structure extraction. Empty = the archive's default model (CLAUDE_MODEL/DEFAULT_MODEL).",
            "Empty — tg_tasks.py takes the archive's default model, not a separate one."),
    Setting("TG_TASKS_EFFORT", "low", "str", "Commitments from chat",
            "Effort for the Claude extraction of commitment structure.", "Structure extraction — deep thinking does not pay off."),
    Setting("TG_TASKS_PREVIEW", "1500", "int", "Commitments from chat",
            "Message preview characters for extraction (more than in triage).",
            "The character ceiling of _preview() for a message in the structured extraction of commitments (not triage)."),
    Setting("TG_TASKS_MAX_CHARS", "24000", "int", "Commitments from chat",
            "Thread character ceiling for structure extraction.",
            "Accumulating thread messages in tg_tasks.py stops as soon as the total length exceeds this "
            "ceiling."),

    # --- Unanswered questions ---
    Setting("TG_QUESTIONS_WINDOW_H", "24", "float", "Unanswered questions",
            "Hours of presence in the chat after a question for it to count as seen.",
            "Default REPLY_WINDOW_H in tg_questions.py — the window in which a reply/reaction to the question is looked for."),
    Setting("TG_QUESTIONS_DAYS", "90", "int", "Unanswered questions",
            "Window (days) for searching unanswered questions.",
            "Wider than the task digest (30 days) — an unanswered question sometimes hangs for months."),

    # --- Entity graph ---
    Setting("TG_ENTITIES_MORPH_ENABLED", "0", "bool", "Entity graph",
            "Switch of the morphological name matcher (cases/abbreviations).",
            "Off by default — the merge does not change system behaviour without a conscious switch-on."),
    Setting("TG_ENTITIES_NAMES_TTL", "300", "float", "Entity graph",
            "TTL (sec) of the entity name dictionary cache.",
            "load_names — 6200 rows per message; a short TTL so a new entity is caught without a restart."),

    # --- RAG retrieval ---
    Setting("RAG_TOP_K", "12", "int", "RAG retrieval", "How many chunks /api/memory/ask returns by default.",
            "Default of the k parameter in /api/memory/ask when the client did not pass its own."),
    Setting("RAG_RECENCY_WEIGHT", "0.25", "float", "RAG retrieval",
            "Weight of the recency boost on top of the RRF score.", "Moderate on purpose — RRF stays the main signal."),
    Setting("RAG_RECENCY_HALF_LIFE_DAYS", "180", "float", "RAG retrieval",
            "Half-life of the recency boost (days).",
            "After this many days the recency boost (RAG_RECENCY_WEIGHT) halves."),
    Setting("RAG_MAX_PER_MEETING", "3", "int", "RAG retrieval",
            "Maximum chunks from one meeting in the final top (diversification).",
            "The ceiling in retrieval.py diversifies the top — beyond it, chunks of the same meeting are dropped."),
    Setting("RAG_FTS_CUTOFF_MIN_WORDS", "3", "int", "RAG retrieval",
            "From how many query words the soft FTS relevance cutoff turns on (T6.8).",
            "Together with RAG_FTS_MIN_MATCHES it cuts from the FTS candidates the chunks with too few term "
            "matches."),
    Setting("RAG_FTS_MIN_MATCHES", "2", "int", "RAG retrieval",
            "Minimum real term matches for queries longer than the cutoff.",
            "Applies only when the query is longer than RAG_FTS_CUTOFF_MIN_WORDS words."),
    Setting("RAG_RERANK_POOL_SIZE", "24", "int", "RAG retrieval",
            "How many top candidates (by RRF+recency) to run through the cross-encoder rerank.",
            "Applies only when RECALL_RERANK_ENABLED=1 — the pool size before the rerank model."),
    Setting("RECALL_RERANK_ENABLED", "0", "bool", "RAG retrieval",
            "Turns on the local cross-encoder rerank (bge-reranker-v2-m3) for the RAG chat (T6.4).",
            "Only for app/services/rag.py — not for copilot/categorize/MCP. The default is duplicated in config.py "
            "(documented, not eliminated — debt S4/S6)."),
    Setting("RECALL_RERANK_MODEL", "BAAI/bge-reranker-v2-m3", "str", "RAG retrieval",
            "HF model for the cross-encoder rerank.",
            "Applies only when RECALL_RERANK_ENABLED=1 — which HF model reranker.py loads."),
    Setting("RERANK_DEVICE", "", "str", "RAG retrieval",
            "Device for the reranker ('cuda'/'cpu'). Empty = autodetect.",
            "Empty — reranker.py takes cuda if torch.cuda.is_available(), otherwise cpu."),
    Setting("RAG_COMMENT_WEIGHT", "0.6", "float", "RAG retrieval — comments",
            "Multiplier of the comment boost relative to kind_weight (correction 1.0 … question 0.4).",
            "A strong tie-breaker, not a muffler — so that comments alone are not returned for any question."),
    Setting("RAG_COMMENT_CANDIDATE_CAP", "20", "int", "RAG retrieval — comments",
            "How many candidates to take from the separate comment index.",
            "Ceiling of the number of candidates from the comment index before merging with the main RRF."),
    Setting("RAG_COMMENT_MAX_SHARE", "0.34", "float", "RAG retrieval — comments",
            "Maximum share of the top that comments may occupy.",
            "Without a ceiling 30 planted comments took 115 slots out of 120 at k=8 (Wave 2.5)."),
    Setting("RAG_ATTACH_COMMENT_CAP", "8", "int", "RAG retrieval — comments",
            "Maximum attached comments per answer.",
            "Default cap in the function that attaches comments to a finding — beyond it comments are dropped."),
    Setting("RAG_THREAD_STITCH", "6", "int", "RAG retrieval — threads",
            "How many neighbours of the TG thread to stitch to a finding in the prompt.",
            "The answer is the next reply of the thread, it shares no words with the query."),
    Setting("RAG_THREAD_MSG_CHARS", "1500", "int", "RAG retrieval — threads",
            "Character ceiling per neighbouring message in the thread stitch.",
            "Applies to neighbours; the finding itself (the hit) is cut separately, to the remainder of RAG_THREAD_CHARS."),
    Setting("RAG_THREAD_CHARS", "8000", "int", "RAG retrieval — threads",
            "Character ceiling for the whole thread stitch (all messages together).",
            "Without it one long thread (156k characters) crowded other sources out of the prompt."),
    Setting("RAG_QUERY_REWRITE", "0", "bool", "RAG retrieval — query rewriting",
            "Turns on 1-3 alternative query formulations (Claude) as additional "
            "vector+FTS subqueries merged by the same RRF.",
            "Off by default — turned on selectively for measurement by the gate "
            "(evals/gate.py calls retrieval.search without the flag → reads this env)."),
    Setting("RAG_QUERY_REWRITE_MODEL", "claude-haiku-4-5-20251001", "str",
            "RAG retrieval — query rewriting",
            "Which Claude model generates the alternative query formulations.",
            "Applies only when RAG_QUERY_REWRITE=1 — a cheap auxiliary call before the main RAG query."),

    # --- Embeddings ---
    Setting("EMBED_MODEL", "intfloat/multilingual-e5-large", "str", "Embeddings",
            "Embedding model (1024-dim, multilingual).",
            "bge-m3 rejected: it has no safetensors, conflicts with the torch<2.6 pin (CVE-2025-32434). "
            "The query/passage prefix style (e5/qwen3/plain) is determined by embeddings.py from the NAME of this model."),
    Setting("EMBED_VERSION", "2", "int", "Embeddings",
            "Version of the embedding chunking/logic (T6.8) — a bump forces a re-embed of the archive.",
            "Changed ONLY in `.env` (ADR-016: the code default does not move with the live epoch). When the "
            "chunking format or prefix changes, take a number strictly higher than the pair held by the live DB "
            "(`SELECT embedding_model, embedding_version, COUNT(*) FROM transcriptions GROUP BY 1, 2`), "
            "and then run `reembed.run` — a number that already exists in the DB would leave the new format in the old "
            "epoch (stale_ids = 0). retrieval.search takes only chunks of the current pair (EMBED_MODEL, "
            "EMBED_VERSION) — vectors of another epoch are not compared."),
    Setting("EMBED_MAX_SEQ", "1024", "int", "Embeddings",
            "Sequence length ceiling for the embedding model (in tokens).",
            "Works ONLY downward: e5 with its 512 is not touched. Needed for models without "
            "sentence_bert_config.json (Qwen3-Embedding): without a ceiling sentence-transformers takes "
            "32768, monster chunks (documents/TG, up to 18k tokens) eat 17 GB of VRAM and the pass "
            "slows down 30 times. 0 = ceiling off."),
    Setting("EMBED_DEVICE", "", "str", "Embeddings",
            "Device for embeddings ('cuda'/'cpu'). Empty = autodetect.",
            "Empty — embeddings.py takes cuda if torch.cuda.is_available(), otherwise cpu."),
    Setting("RECALL_VECTOR_WARN_THRESHOLD", "50000", "int", "Embeddings",
            "Threshold of the number of embedded chunks after which a WARNING about scale is logged (T6.6).",
            "Brute-force numpy vector search — a signal to plan the migration to ANN (sqlite-vec) in advance."),

    # --- OCR / documents ---
    Setting("OCR_LANG", "ukr+rus+eng", "str", "OCR / documents",
            "Tesseract language packs for document OCR.",
            "If a language pack is missing — pytesseract raises TesseractError, document_parser.py falls back to "
            "the default language (eng)."),
    Setting("OCR_TESSERACT_CMD", "", "str", "OCR / documents",
            "Path to tesseract.exe if it is not in PATH.",
            "Empty — PATH, and without it the standard installer path "
            "C:\\Program Files\\Tesseract-OCR\\tesseract.exe."),
    Setting("OCR_TESSDATA_DIR", "", "str", "OCR / documents",
            "Folder of Tesseract language packs (ukr/rus/eng).",
            "Empty — %LOCALAPPDATA%\\Recall\\tessdata, if the folder exists. Needed because in Program Files "
            "language packs cannot be added without administrator rights."),
    Setting("OCR_TESSDATA_FAST_DIR", "", "str", "OCR / documents",
            "Fast language packs (tessdata_fast) for OCR of video frames.",
            "Empty — %LOCALAPPDATA%\\Recall\\tessdata_fast, if the folder exists; otherwise video OCR takes "
            "the regular packs. Documents always use the regular ones."),
    Setting("OCR_DPI", "200", "int", "OCR / documents",
            "DPI of the PDF page render for OCR.",
            "Passed to page.get_pixmap(dpi=...) before OCR — a higher DPI improves quality at the cost of time."),
    Setting("OCR_MAX_PAGES", "50", "int", "OCR / documents",
            "Cap of synchronous OCR (pages).",
            "Beyond this number of pages document_parser.py no longer runs OCR for the document."),
    Setting("RECALL_IMPORT_ROOTS", "", "str", "OCR / documents",
            "List of absolute directories (separated by ';') allowed for import-folder.",
            "Empty → import-folder is forbidden everywhere (deny-by-default, T1.3)."),
    Setting("DATABASE", "whisper_history.db", "path", "OCR / documents",
            "Path to the SQLite DB for the standalone CLI run of app/services/comments.py.",
            "config.py keeps its own hardcode DATABASE='whisper_history.db' (a duplicate, non-goal 5 — not eliminated)."),

    # --- Claude API ---
    Setting("ANTHROPIC_API_KEY", "", "secret", "Claude API",
            "Anthropic key. Without it polish/enrichment/RAG chat/copilot verification/vision(claude) are off.",
            "https://console.anthropic.com/settings/keys"),
    Setting("CLAUDE_MODEL", "claude-opus-5", "str", "Claude API",
            "Default model for text/RAG (single source of truth — app/services/models.py).",
            "default_model() in app/services/models.py returns this value if set — otherwise DEFAULT_MODEL "
            "from the code."),
    Setting("HF_TOKEN", "", "secret", "Claude API",
            "Hugging Face token for pyannote (diarization).",
            "Requires accepting the licenses of the models pyannote/speaker-diarization-3.1 and pyannote/segmentation-3.0."),

    # --- Copilot ---
    Setting("COPILOT_ENABLED", "1", "bool", "Copilot",
            "Turns on the live call co-pilot (Phase 19).",
            "Degrades softly: no Ollama/model → is_available()=False, recording/STT/RAG work as before.",
            scope="config"),
    Setting("LOCAL_LLM_URL", "http://localhost:11434", "str", "Copilot",
            "HTTP address of the local Ollama service.",
            "Base URL to which local_llm.py sends /api/generate and /api/tags to Ollama.", scope="config"),
    Setting("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct-q5_K_M", "str", "Copilot",
            "Local dispatcher model of the copilot.",
            "Default model for all local_llm calls (copilot, TG threads, chat dossier) if "
            "the call did not override it with its own.", scope="config"),
    Setting("LOCAL_LLM_KEEPALIVE", "30m", "str", "Copilot",
            "Ollama keep_alive for the copilot model.",
            "The keep_alive value in the request to Ollama — how long to keep the model in GPU memory without unloading.",
            scope="config"),
    Setting("LOCAL_LLM_NUM_CTX", "8192", "int", "Copilot",
            "Context size of the local copilot model.",
            "The num_ctx value in the request to Ollama — prompts longer than this limit are truncated/degrade the model.",
            scope="config"),
    Setting("LOCAL_LLM_TIMEOUT", "60", "float", "Copilot",
            "Timeout of the local copilot model call (sec).",
            "Default HTTP request timeout of local_llm.py to Ollama if the call did not pass its own.",
            scope="config"),
    Setting("LOCAL_TITLE_MODEL", "hf.co/INSAIT-Institute/MamayLM-Gemma-3-12B-IT-v2.0-GGUF:Q5_K_M",
            "str", "Record titles",
            "Local model that proposes a record's title and description (meta_suggest.py). Empty = LOCAL_LLM_MODEL.",
            "Measurement 14.10.2026 on #8677: MamayLM 12B ~40 s and three different topical variants; qwen2.5:7b in 10 s "
            "gave almost identical descriptions. During recording the model is not called at all (copilot)."),
    Setting("LOCAL_TITLE_KEEPALIVE", "0", "str", "Record titles",
            "Ollama keep_alive for the title model. 0 = unload right after the reply.",
            "Otherwise 12B holds VRAM for another 30 min (LOCAL_LLM_KEEPALIVE), and a call during that time leaves the copilot "
            "and transcription without room. Cost: every suggestion from a cold start (~45 s instead of ~15)."),
    Setting("LOCAL_TITLE_TIMEOUT", "180", "float", "Record titles",
            "Timeout of one title and description suggestion (sec).",
            "The default 60 s of local_llm is too small: 12B on a cold start takes longer to load into VRAM."),
    Setting("COPILOT_MODEL_API", "claude-sonnet-5", "str", "Copilot",
            "\"Regular\" Claude model for verifying what is important (local-first cascade).",
            "Read every time (not cached) in copilot/config.py — the model for the regular (not \"gnarly\") "
            "escalation to Claude."),
    Setting("COPILOT_MODEL_API_GNARLY", "claude-opus-5", "str", "Copilot",
            "Arbiter model for the copilot's 'gnarly'/uncertain cases.",
            "Deliberately a DIFFERENT model from COPILOT_MODEL_API — do not collapse."),
    Setting("COPILOT_TOPIC_TICK_SEC", "6", "float", "Copilot",
            "Tick period of the copilot topic state machine (sec).",
            "Period of the _tick_sec loop in copilot/worker.py — how often the topic state is checked."),
    Setting("COPILOT_TOPIC_WINDOW_CHARS", "200", "int", "Copilot",
            "Minimum character window for evaluating a copilot topic.",
            "_min_window_chars in copilot/worker.py — while the transcript window is smaller, topic evaluation does not "
            "start."),

    # --- Auth / access ---
    Setting("RECALL_BIND_ALL", "", "bool", "Auth / access",
            "Safe default (Wave 0 / T1.1): listen only on the local machine. Going out to\n"
            "the whole network is a conscious opt-in: RECALL_BIND_ALL=1 or FLASK_HOST=0.0.0.0.\n"
            "A network bind requires an explicit SECRET_KEY (otherwise hard-fail) and, as a rule,\n"
            "RECALL_LOCAL_TRUSTED=0 + RECALL_API_KEY (see below).",
            "1 → the SECRET_KEY default becomes a hard-fail (config.py), not an autogen.", scope="config"),
    Setting("RECALL_LOCAL_TRUSTED", "1", "bool", "Auth / access",
            "Requests from localhost do not need a key while enabled.",
            "T1.2: turn off (0) if the application is reachable over the network and a key is needed even locally.",
            default_headless="0"),
    Setting("RECALL_API_KEY", "", "secret", "Auth / access",
            "Access key for requests NOT from localhost (or RECALL_LOCAL_TRUSTED=0).",
            "Header X-Recall-Api-Key or Authorization: Bearer. Not set → fail-closed for non-localhost."),
    Setting("RECALL_PROXY_ALLOWED_IPS", "", "str", "Auth / access",
            "Tailnet IPs of devices allowed into Recall via tailscale serve (comma-separated). A device has TWO addresses — "
            "100.x and fd7a:… (IPv6): enter both, serve passes the one the client came with.",
            "A localhost request with X-Forwarded-For/Tailscale-User-Login is proxied: without an IP here or a key → 401. "
            "Empty → nobody through the proxy (the vps-prod server in the same tailnet too)."),

    # --- MCP server ---
    Setting("RECALL_API_URL", "http://127.0.0.1:5050", "str", "MCP server",
            "Address of the live Flask (app.py) to which mcp_server.py proxies write/live/AI actions.",
            "The base API_URL in mcp_server.py — where all proxied HTTP requests to app.py go."),
    Setting("RECALL_MCP_READONLY", "", "bool", "MCP server",
            "Emergency switch for all MCP writes (does not affect reading).",
            "Off by default (empty). On: no write tool (mailbox, settings, video_analyze) is "
            "registered at all, whatever RECALL_MCP_PROFILE says."),
    Setting("RECALL_MCP_PROFILE", "full", "str", "MCP server",
            "Which MCP tools mcp_server.py registers: full | read.",
            "full — every tool; read — the read tools plus video_analyze. "
            "An unknown value = read (one warning on stderr). Read at mcp_server.py start."),
    Setting("RECALL_MCP_KEY", "", "secret", "MCP server",
            "Bearer key for the http transport of mcp_server.py.",
            "Needed ONLY for --transport http (listens on 127.0.0.1:5060); the default stdio needs no key.\n"
            'Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"'),
    Setting("RECALL_MCP_DEBUG_LOG", "", "str", "MCP server",
            "1/true → log every MCP tool call to logs/mcp_calls.log; or an explicit path.",
            "Off by default (zero overhead)."),
    Setting("MCP_PORT", "5060", "int", "MCP server",
            "Port of the http transport of mcp_server.py.",
            "Applies only with --transport http; the default stdio transport does not use this port."),
    Setting("RECALL_MCP_VISUAL", "images", "str", "MCP server",
            "What of the video visual timeline MCP gives to Claude: images | text | off.",
            "images — frames and on-screen text (through the stop list); text — text only, get_frame refuses; "
            "off — nothing visual, and screen chunks disappear from search/grep/ask_archive. "
            "An unknown value = off. The Recall UI is not affected."),
    Setting("RECALL_PRIVATE_CATEGORIES", "Юридичне,Особисте", "str", "MCP server",
            "Directions/projects whose visual content does not go outside (names separated by commas).",
            "The name is matched at any level of the tree together with descendants; namesakes are all hidden. "
            "The same stop list — for MCP and the K4 bot."),
    Setting("RECALL_PRIVATE_WORDS_FILE", "", "str", "MCP server",
            "File of stop words/names (one per line, # — comment).",
            "Empty — private.txt in the project root (in .gitignore; example — private.example.txt). "
            "No file — no stop words; unreadable — the visual content is hidden."),

    # --- Scheduled pipes (K4) ---
    Setting("PIPES_ENABLED", "0", "bool", "Pipes",
            "Scheduled briefings (K4, app/services/pipes.py): the pipes/*.md files run by themselves.",
            "1 → in the app.py process (under __main__) a daemon thread every 30 s runs the pipes whose moment has come, "
            "and once catches up on what was missed at startup. 0 → the thread does not start, one line in the log."),
    Setting("PIPES_RETRY_MINUTES", "15", "int", "Pipes",
            "After how many minutes to retry a pipe run that failed (data, bot, other failure).",
            "A failure does not close the schedule moment: the scheduler tries again until it delivers or "
            "exhausts PIPES_RETRY_MAX."),
    Setting("PIPES_RETRY_MAX", "6", "int", "Pipes",
            "How many attempts per schedule moment (including the first).",
            "After this many errors the scheduler waits for the next moment; the bot receives \"attempts "
            "exhausted\". 6 attempts of 15 min — about an hour and a half after 9:00."),
    Setting("TELEGRAM_BOT_TOKEN", "", "secret", "Pipes",
            "Delivery bot token from @BotFather (a separate bot, not the listener account).",
            "Without it (or without TELEGRAM_BOT_CHAT_ID) pipes send nothing — every run is written "
            "to the history with the error \"bot not configured\"."),
    Setting("TELEGRAM_BOT_CHAT_ID", "", "str", "Pipes",
            "The owner's chat to which the bot sends briefings: `python -m app.services.tg_bot whoami` after /start to the bot.",
            "The bot sends ONLY here; the code accepts no other recipients."),
    Setting("PIPES_MODEL", "claude-sonnet-5", "str", "Pipes",
            "Claude model that writes 1–2 lines of \"the main thing\" above the briefing card.",
            "If the cost estimate BEFORE the call is above the pipe's budget_usd, there is no key or an error → "
            "the same message without the \"main thing\" line."),
    Setting("RECALL_PUBLIC_URL", "", "str", "Pipes",
            "Recall address visible from a phone (e.g. https://home-pc.<tailnet>.ts.net via tailscale serve).",
            "Base of links in the briefing cards in Telegram. Empty → RECALL_API_URL (open only on this PC)."),

    # --- Logging ---
    Setting("RECALL_LOG_TO_FILE", "0", "bool", "Logging",
            "Turns on the RotatingFileHandler of the module logger 'whisper_ui'.",
            "Off by default — a second handler on the shared file blocked rollover on Windows (WinError 32)."),
)


def by_name() -> dict[str, Setting]:
    """The registry as a dict {name: Setting}."""
    return {s.name: s for s in REGISTRY}


_BY_NAME = by_name()


def _lookup(name: str) -> Setting:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"env variable {name!r} is not registered in app.core.settings.REGISTRY"
        ) from None


def env(name: str) -> str:
    """Raw string from os.environ, otherwise the profile-dependent default from the registry.

    Resolution (config-registry-fix-01, Contracts §Auth):
    ``os.environ[name]`` → ``default_headless`` (if ``profile() == 'headless'``
    and the field is not ``None``) → ``default``. KeyError if the name is not registered.
    """
    setting = _lookup(name)
    if name in os.environ:
        return os.environ[name]
    if setting.default_headless is not None and profile() == "headless":
        return setting.default_headless
    return setting.default


def env_bool(name: str) -> bool:
    """Typed bool reader. Truthy set: '1'/'true'/'yes'/'on' (case-insensitive)."""
    return env(name).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str) -> int:
    return int(env(name))


def env_float(name: str) -> float:
    return float(env(name))


def profile() -> str:
    """RECALL_PROFILE → 'desktop' (default) or 'headless'. Any other value → ValueError."""
    raw = os.environ.get("RECALL_PROFILE", "desktop").strip().lower()
    if raw not in ("desktop", "headless"):
        raise ValueError(
            f"RECALL_PROFILE={raw!r} is unknown — expected 'desktop' or 'headless'"
        )
    return raw


#: System/test env variables that the registry completeness test skips — they do not
#: belong to the Recall domain (PATH, PYTEST_CURRENT_TEST, etc.) and have no default
#: meaningful for .env.example.
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
    "OMP_THREAD_LIMIT",   # set by the child tesseract during video OCR
    "TESSDATA_PREFIX",    # tesseract's own variable; Recall takes the folder from OCR_TESSDATA_DIR
})

#: Variables that go into the generated .env.example ACTIVE (without '#'), because
#: they are needed for a normal local start — the rest is commented out as
#: reference/optional (the same style the hand-written .env.example had).
_ACTIVE_BY_DEFAULT = frozenset({
    "FLASK_DEBUG", "FLASK_HOST", "FLASK_PORT", "APP_ENV", "SECRET_KEY",
    "FORCE_CPU", "LOG_LEVEL",
})


def render_env_example() -> str:
    """Generate the contents of .env.example from REGISTRY."""
    lines: list[str] = [
        "# Recall — example .env",
        "# GENERATED from app/core/settings.py — do not edit by hand.",
        "# Update: .venv/Scripts/python.exe -m app.core.settings env-example > .env.example",
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
            # Profile-dependent default: an explanatory comment (both values)
            # + one valid dotenv line (the desktop default), commented out with '# '.
            # Uncommenting it gives the operator a syntactically correct NAME=value.
            lines.append(
                f"# profiles — desktop={s.default}, headless={s.default_headless}"
            )
            lines.append(f"# {s.name}={s.default}")
        else:
            prefix = "" if s.name in _ACTIVE_BY_DEFAULT else "# "
            lines.append(f"{prefix}{s.name}={s.default}")
    return "\n".join(lines) + "\n"


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.core.settings")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("env-example", help="Prints the generated .env.example to stdout")
    args = parser.parse_args(argv)
    if args.command == "env-example":
        sys.stdout.write(render_env_example())


if __name__ == "__main__":
    _main()
