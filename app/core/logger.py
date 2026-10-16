"""
Logging setup for the application
"""

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional
from config import get_config
from app.core import settings as _settings


def setup_logger(
    name: str = "whisper_ui",
    log_file: Optional[str] = None,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Sets up a logger with file rotation and console output
    """
    
    config = get_config()
    
    # Create the logger
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Log format
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler with rotation. OFF by default (RECALL_LOG_TO_FILE != 1):
    # this module-level logger ("whisper_ui") is not used anywhere, and its second
    # RotatingFileHandler on the shared whisper_ui.log only blocked the main process's
    # rollover on Windows (WinError 32 — file in use: app.py writes with its own basicConfig,
    # the listener with its own, every stdio mcp_server opened yet another copy). It can be
    # turned back on for a specific process via RECALL_LOG_TO_FILE=1.
    # config-registry-fix-r3-01: reading switched to settings.env_bool —
    # the truthy set widened from ('1','true','yes') to ('1','true','yes','on').
    _file_logging = _settings.env_bool("RECALL_LOG_TO_FILE")
    if _file_logging and (log_file or hasattr(config, 'LOG_FILE')):
        log_path = log_file or config.LOG_FILE
        
        # Create the logs folder if it does not exist
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    # Prevent duplicate logs
    logger.propagate = False
    
    return logger


class JsonFormatter(logging.Formatter):
    """config-registry-profiles S2: `RECALL_LOG_FORMAT=json` — one JSON line
    per record instead of text (`app.py:219-275` attaches this to the file+stream
    handlers when enabled). The fields are deliberately the same as in the text format
    (`_RequestIdLogFilter` in app.py sets `record.request_id`) — no
    extra fields added (plan, Contracts §Logs)."""

    def format(self, record: logging.LogRecord) -> str:
        # config-registry-fix S4 (finding 16): milliseconds — text mode in
        # app.py carries them in `%(asctime)s` by default (a Formatter without
        # datefmt appends ",%03d"), while `formatTime` with an explicit datefmt here
        # lost them. We append them in the same format as standard logging.
        payload = {
            "ts": f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')},{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            # exc_info — a field outside the original Contracts (§Logs had
            # the same fields as the text format); kept, because without it
            # __format__ would swallow tracebacks on ERROR records. To be recorded
            # as a deliberate deviation — in Contracts S3 (story 06).
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """
    Returns a configured logger
    """
    return logging.getLogger(name)


# config-registry-fix S4 (finding 5): there used to be
# `app_logger = setup_logger("whisper_ui")` here — it ran on ANY
# import of this module (even just for `JsonFormatter`), added a
# StreamHandler and, with `RECALL_LOG_TO_FILE=1`, a SECOND `RotatingFileHandler`
# on the same `whisper_ui.log` the `app.py` handler already competes for —
# the same WinError 32 file race the flag exists to eliminate.
# Nothing in the codebase reads `app_logger` (checked with grep), so
# we remove the import side effect rather than rewrite `setup_logger`.


class LoggerMixin:
    """Mixin for adding a logger to classes"""
    
    @property
    def logger(self) -> logging.Logger:
        return get_logger(self.__class__.__name__)