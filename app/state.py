"""Module-level singletons для blueprints.

Заповнюються в app.py під час ініціалізації. Blueprints імпортують
звідси, щоб уникнути циркулярних import'ів з app.py.

Це pragmatic Flask-паттерн (як `flask.current_app`, але з простішим
доступом без application context). Підходить для single-instance
deployment'у.
"""
from typing import Any, Optional

# Whisper engine
whisper_manager: Any = None

# Concurrency
executor: Any = None  # batch pool (T2.5): youtube/audio download, doc_import,
# enrichment(_backfill), tg_embed/tg_transcribe, video_analysis
live_executor: Any = None  # T2.5: live-critical pool — зараз лише recording_finalize
job_queue: Any = None

# Stores (Phase 2)
download_progress: Any = None
process_logs: Any = None
sse_broker: Any = None
# Active /api/transcribe jobs кореновані за audio_download_id (source='library').
# dict[int, {audio_download_id, started_at, stage, progress}]. Live-state для UI:
# показувати «транскрибується» на картці у Audio Library.
active_library_transcriptions: Any = None

# Background services
system_monitor: Any = None
file_cleanup: Any = None

# Cross-cutting
metrics: Any = None
limiter: Any = None
ffmpeg_available: bool = False
cfg: Any = None

# Phase 9: System audio recording
recording_service: Any = None  # RecordingService instance
# Phase 9.10: список session_id які були recovery'ні при старті.
# UI показує banner з цим списком, після подяки очищається.
recording_recovery_log: Any = None
# Phase 12.26 (Big Bet): live transcribe worker
live_transcribe_worker: Any = None
# Phase 19 (Co-pilot): живий ко-пілот дзвінка — persist + (далі) live-аналіз
copilot_service: Any = None  # CopilotService instance
copilot_worker: Any = None   # CopilotWorker (топік-трекінг на ембеддингах, Крок 2)

# Function singletons (Phase 5)
add_log: Any = None  # add_process_log: callable(pid, stage, msg, progress, status)

# Rate limit configs (Phase 6.2)
RATE_LIMIT_TRANSCRIBE: str = '60/minute'
RATE_LIMIT_YOUTUBE: str = '30/minute'
RATE_LIMIT_POLISH: str = '20/hour'


def init(**kwargs):
    """Bulk-set singletons. Викликається з app.py після initialization."""
    g = globals()
    for k, v in kwargs.items():
        if k not in g:
            raise KeyError(f"unknown state key: {k}")
        g[k] = v
