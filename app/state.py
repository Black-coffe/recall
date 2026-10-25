"""Module-level singletons for blueprints.

Populated in app.py during initialization. Blueprints import
from here to avoid circular imports with app.py.

This is a pragmatic Flask pattern (like `flask.current_app`, but with simpler
access without an application context). Suitable for a single-instance
deployment.
"""
from typing import Any, Optional

# Whisper engine
whisper_manager: Any = None

# Concurrency
executor: Any = None  # batch pool (T2.5): youtube/audio download, doc_import,
# enrichment(_backfill), tg_embed/tg_transcribe, video_analysis
live_executor: Any = None  # T2.5: live-critical pool — currently only recording_finalize
job_queue: Any = None

# Stores (Phase 2)
download_progress: Any = None
process_logs: Any = None
sse_broker: Any = None
# Active /api/transcribe jobs keyed by audio_download_id (source='library').
# dict[int, {audio_download_id, started_at, stage, progress}]. Live state for the UI:
# show "transcribing" on the card in the Audio Library.
active_library_transcriptions: Any = None

# Background services
system_monitor: Any = None
file_cleanup: Any = None

# Cross-cutting
metrics: Any = None
limiter: Any = None
ffmpeg_available: bool = False
cfg: Any = None

# config-registry-profiles S2/S3: the app.py boot phases write here as they
# become ready; story 03 (`GET /api/ready`) only reads. Defaults are "not ready yet"/
# "disabled", so /api/ready returns 503 until the corresponding phase completes,
# even if a request arrives in the tiny window between `import app` and the end of boot.
#
# `migrations` is deliberately absent from the contract (робота-contract-03, finding
# 9 of the S3 report): the path to 'error' is unreachable — `app/db/migrations.py` rethrows
# the exception, and `app.py` does not catch it, so the process dies before робота, even before the
# first request. A field with no real state does not belong
# in the contract.
робота: dict = {
    "database": "pending",  # 'ok' | 'error' | 'pending'
    "job_queue": {"bound": False, "recovered": 0},
    "whisper": {"preload": "disabled", "model": None},  # preload: disabled|loading|ready|failed
    "embeddings": {"loaded": False},  # informational — e5 loads lazily, робота is not blocked
    "telegram": "disabled",  # 'disabled' | 'starting' | 'running' | 'absent'
    "boot_finished": False,  # True only after all boot phases finish; part of `ready`.
}

# Phase 9: System audio recording
recording_service: Any = None  # RecordingService instance
# Phase 9.10: list of session_ids that were recovered at startup.
# The UI shows a banner with this list, cleared after acknowledgement.
recording_recovery_log: Any = None
# recording-autosave: AutosaveScheduler (deadline after finalize, hold/cancel from the save screen)
recording_autosave: Any = None
# Phase 12.26 (Big Bet): live transcribe worker
live_transcribe_worker: Any = None
# Phase 19 (Co-pilot): the live call co-pilot — persist + (later) live analysis
copilot_service: Any = None  # CopilotService instance
copilot_worker: Any = None   # CopilotWorker (topic tracking on embeddings, Step 2)

# Function singletons (Phase 5)
add_log: Any = None  # add_process_log: callable(pid, stage, msg, progress, status)

# Rate limit configs (Phase 6.2)
RATE_LIMIT_TRANSCRIBE: str = '60/minute'
RATE_LIMIT_YOUTUBE: str = '30/minute'
RATE_LIMIT_POLISH: str = '20/hour'


def init(**kwargs):
    """Bulk-set singletons. Called from app.py after initialization."""
    g = globals()
    for k, v in kwargs.items():
        if k not in g:
            raise KeyError(f"unknown state key: {k}")
        g[k] = v
