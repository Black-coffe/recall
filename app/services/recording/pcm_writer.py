"""Chunked PCM writer with fsync (Phase 9.2).

An append-only writer for raw PCM bytes with a periodic fsync for
crash resilience. Used by RecordingService for two
parallel streams (mic + system).

Why not WAV append:
- A WAV header contains ``data chunk size`` — on append the header
  has to be rewritten. If the process died during the write — the header is broken.
- Raw PCM = just a stream of samples. Append is safe by definition.
- The header is added at finalize, when the size is final.

Why fsync:
- ``write()`` only puts data into the kernel buffer. Without fsync a power failure
  loses up to 30 seconds of data (the typical dirty_writeback_centisecs).
- ``fsync`` guarantees the data is physically on disk.
- It is called after every flush (by default once per ~5 sec),
  so the overhead is negligible.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


class ChunkedPcmWriter:
    """Append-only writer with an in-RAM buffer and a periodic fsync.

    Threading: all public methods are thread-safe. Usually the audio thread
    calls :meth:`append`, a separate flush thread (or the service)
    calls :meth:`flush` at a regular interval.

    Lifecycle:
    - Open: the file is opened in binary append mode on the first
      :meth:`append` (lazy) or explicitly via :meth:`open`.
    - Append: bytes go into the internal buffer.
    - Flush: the buffer is written to the file, the file is fsynced, the
      total bytes counter is updated.
    - Close: a final flush + closing the descriptor. Idempotent.

    Crash semantics:
    - Data up to the last successful flush is guaranteed to be on disk.
    - Data between flushes is in RAM and may be lost.
    - The file is append-only, so existing data is never corrupted
      even if the process dies in the middle of a write.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._buf = bytearray()
        self._fh: Optional[int] = None  # raw file descriptor (for fsync)
        self._total_bytes = 0
        self._closed = False
        self._lock = threading.Lock()

    # ---------- lifecycle

    def open(self) -> None:
        """Opens the file for append. Creates it if it does not exist.

        If the file already exists — we continue from its end, total_bytes
        is initialized with the file size. This is useful for recovery.
        """
        with self._lock:
            if self._fh is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND guarantees an atomic append even from several processes
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, 'O_BINARY'):
                flags |= os.O_BINARY
            self._fh = os.open(self.path, flags, 0o644)
            try:
                self._total_bytes = self.path.stat().st_size
            except OSError:
                self._total_bytes = 0

    def close(self) -> None:
        """Final flush + close. Idempotent."""
        with self._lock:
            if self._closed:
                return
            # IMPORTANT: first write out the buffer, THEN mark closed.
            # If _closed=True is set earlier — _flush_locked refuses.
            try:
                self._flush_locked(fsync=True)
            except OSError as e:
                logger.warning("close() flush error on %s: %s", self.path, e)
            self._closed = True
            if self._fh is not None:
                try:
                    os.close(self._fh)
                except OSError as e:
                    logger.warning("close() error on %s: %s", self.path, e)
                self._fh = None

    # ---------- main api

    def append(self, data: bytes) -> None:
        """Adds bytes to the in-RAM buffer. Does not write to disk.

        Calling from the audio thread is allowed — a fast operation, no I/O.
        """
        if not data:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("PCM writer is closed")
            self._buf.extend(data)

    def flush(self, fsync: bool = True) -> int:
        """Writes the buffer to the file and (optionally) does an fsync.

        Returns the number of bytes written by this operation.
        Safe in paired calls: if the buffer is empty — does
        nothing.
        """
        with self._lock:
            return self._flush_locked(fsync=fsync)

    def _flush_locked(self, fsync: bool) -> int:
        if self._closed and not self._buf:
            return 0
        if not self._buf:
            return 0
        if self._fh is None:
            # Reopening after close is not allowed
            if self._closed:
                raise RuntimeError("PCM writer is closed")
            # Lazy-open
            self.path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, 'O_BINARY'):
                flags |= os.O_BINARY
            self._fh = os.open(self.path, flags, 0o644)
            try:
                self._total_bytes = self.path.stat().st_size
            except OSError:
                self._total_bytes = 0

        chunk = bytes(self._buf)
        self._buf.clear()

        # os.write may not write everything in one call — we repeat
        written_total = 0
        view = memoryview(chunk)
        while written_total < len(chunk):
            n = os.write(self._fh, view[written_total:])
            if n <= 0:
                raise OSError(f"os.write returned {n} on {self.path}")
            written_total += n

        if fsync:
            os.fsync(self._fh)

        self._total_bytes += written_total
        return written_total

    # ---------- properties

    @property
    def total_bytes(self) -> int:
        """Total number of bytes successfully written and fsynced."""
        with self._lock:
            return self._total_bytes

    @property
    def buffered_bytes(self) -> int:
        """Number of bytes in the RAM buffer that are not yet written."""
        with self._lock:
            return len(self._buf)

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    # ---------- context manager

    def __enter__(self) -> "ChunkedPcmWriter":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
