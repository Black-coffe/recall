"""Whisper model download as a background job (kind ``model_download``, slow executor).

The faster backend fetches the files through ``faster_whisper.utils.download_model`` into the
HF hub cache ``WhisperModel`` loads from, and never takes ``whisper_manager._load_lock``: models
already loaded keep serving while a new one downloads. The openai backend has no separate
fetch, so it keeps ``whisper_manager.download_model`` (load on CPU under the lock) as before.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from app import state
from app.core.i18n import tr


logger = logging.getLogger(__name__)

KIND = "model_download"

_submit_lock = threading.Lock()


class ModelDownloadError(Exception):
    """A refusal; ``error_code`` is an ``app.core.error_codes`` key."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(code)
        self.code = code
        self.error_code = code
        self.message = message or code


def known_models() -> list:
    """Names ``GET /api/models`` lists for the active backend."""
    return [m.get("name") for m in state.whisper_manager.get_available_models()]


def submit(name: str, *, actor: Optional[str] = None) -> dict:
    """Queue (or join) the download of ``name``: ``{"job_id", "created"}``."""
    if not isinstance(name, str) or name not in known_models():
        raise ModelDownloadError("UNKNOWN_MODEL", tr("core.system.model_unknown", model=str(name)[:60]))
    with _submit_lock:
        live = state.job_queue.find_active(KIND, model=name)
        if live is not None:
            return {"job_id": live.id, "created": False}
        meta = {"model": name, "actor": actor, "stage": "queued"}
        job = state.job_queue.submit("model_download", _job, name, meta=meta)
    return {"job_id": job.id, "created": True}


def _log(job_id: str, stage: str, message: str, progress=None, status: str = "processing") -> None:
    if state.add_log is None:
        return
    try:
        state.add_log(job_id, stage, message, progress, status)
    except Exception as e:  # a log line must never fail the download
        logger.debug("model_download: process log failed: %s", e)


def _job(job, name: str) -> dict:
    wm = state.whisper_manager
    backend = wm.backend_name
    job.meta["stage"] = "downloading"
    _log(job.id, "download", tr("core.system.model_download_log_start", model=name), 0)
    if backend == "faster":
        import whisper_manager_new
        from faster_whisper import utils as fw_utils
        repo = whisper_manager_new.faster_model_repo(name)
        if not repo:
            raise ModelDownloadError("UNKNOWN_MODEL")
        fw_utils.download_model(repo, cache_dir=whisper_manager_new.faster_cache_dir())
    elif not wm.download_model(name):
        raise ModelDownloadError("JOB_FAILED")
    job.meta["stage"] = "done"
    _log(job.id, "complete", tr("core.system.model_downloaded", model=name), 100, "completed")
    logger.info("Whisper model %s downloaded via %s", name, backend)
    return {"model": name, "backend": backend}
