"""Background auto-cleanup of old files (Phase 6.3).

Scenario:
- Every N hours check uploads/, youtube_downloads/.
- Delete files older than CLEANUP_AGE_DAYS (default 7 days).
- Does NOT touch files from the audio_downloads DB (only orphan upload temp files).
- Does NOT touch files from the transcriptions DB (file_path if still valid).
- Logs what it deleted + the total size.

Safe: a global DB lock is not needed — we do not run ALTER TABLE / DROP.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Set


logger = logging.getLogger(__name__)


class FileCleanupService:
    def __init__(
        self,
        upload_folder: str,
        youtube_folder: str,
        db_path: str,
        max_age_days: int = 7,
        check_interval_hours: float = 6.0,
    ):
        self.upload_folder = Path(upload_folder)
        self.youtube_folder = Path(youtube_folder)
        self.db_path = db_path
        self.max_age = timedelta(days=max_age_days)
        self.check_interval = check_interval_hours * 3600
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="FileCleanup")
        self._thread.start()
        logger.info(
            f"FileCleanup started: max_age={self.max_age.days}d, "
            f"interval={self.check_interval / 3600:.1f}h"
        )

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("FileCleanup stopped")

    def _run(self):
        # First pass 60 s after start (let the app warm up).
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"FileCleanup error: {e}", exc_info=True)
            self._stop.wait(self.check_interval)

    # -----------------------------------------------------
    # Public for /api/admin/cleanup-now or a test
    # -----------------------------------------------------
    def run_once(self) -> dict:
        """One cleanup pass. Returns statistics."""
        protected = self._collect_protected_paths()
        cutoff = time.time() - self.max_age.total_seconds()

        stats = {
            "deleted_count": 0,
            "deleted_bytes": 0,
            "skipped_protected": 0,
            "checked": 0,
        }

        for folder in (self.upload_folder, self.youtube_folder):
            if not folder.exists():
                continue
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                stats["checked"] += 1
                abs_path = str(path.resolve()).lower()
                if abs_path in protected:
                    stats["skipped_protected"] += 1
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > cutoff:
                    continue
                size = path.stat().st_size
                try:
                    path.unlink()
                    stats["deleted_count"] += 1
                    stats["deleted_bytes"] += size
                    logger.info(f"FileCleanup: deleted {path.name} ({size / 1024 / 1024:.1f}MB)")
                except OSError as e:
                    logger.warning(f"FileCleanup: cannot delete {path.name}: {e}")

        if stats["deleted_count"] > 0:
            logger.info(
                f"FileCleanup pass: deleted {stats['deleted_count']} files, "
                f"{stats['deleted_bytes'] / 1024 / 1024:.1f}MB freed, "
                f"{stats['skipped_protected']} protected"
            )
        return stats

    def _collect_protected_paths(self) -> Set[str]:
        """Collect file_path values from the DB — do NOT touch them, even if old."""
        protected: Set[str] = set()
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                # Files of active audio downloads in the library
                for row in conn.execute('SELECT file_path FROM audio_downloads WHERE file_path IS NOT NULL'):
                    p = row[0]
                    if p:
                        protected.add(str(Path(p).resolve()).lower())
                # Files of YouTube transcriptions (may be needed for re-export)
                for row in conn.execute("SELECT file_path FROM transcriptions WHERE source_type='youtube' AND file_path IS NOT NULL"):
                    p = row[0]
                    if p:
                        protected.add(str(Path(p).resolve()).lower())
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"FileCleanup: cannot read DB for protected paths: {e}")
        return protected
