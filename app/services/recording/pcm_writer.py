"""Chunked PCM writer з fsync (Phase 9.2).

Append-only writer для raw PCM-байтів з періодичним fsync для
crash-resilience. Використовується RecordingService для двох
паралельних потоків (mic + system).

Чому не WAV append:
- WAV-хедер містить ``data chunk size`` — на append треба переписувати
  заголовок. Якщо процес умер у момент write'у — заголовок битий.
- Raw PCM = просто потік семплів. Append безпечний за визначенням.
- Хедер додається на finalize коли розмір остаточний.

Чому fsync:
- ``write()`` тільки кладе у kernel buffer. Без fsync крах живлення
  втрачає до 30 секунд даних (типовий dirty_writeback_centisecs).
- ``fsync`` гарантує що дані фізично на диску.
- Викликається після кожного flush (за замовчуванням раз на ~5 сек),
  тому overhead мізерний.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


class ChunkedPcmWriter:
    """Append-only writer з in-RAM буфером та періодичним fsync.

    Threading: усі публічні методи thread-safe. Зазвичай audio thread
    викликає :meth:`append`, окремий flush thread (або service)
    викликає :meth:`flush` з регулярним інтервалом.

    Lifecycle:
    - Open: файл відкривається у binary append mode на першому
      :meth:`append` (lazy) або явно через :meth:`open`.
    - Append: байти ідуть у внутрішній буфер.
    - Flush: буфер пишеться у файл, файл fsync'ається, лічильник
      total bytes оновлюється.
    - Close: фінальний flush + закриття дескриптора. Idempotent.

    Crash semantics:
    - Дані до останнього успішного flush — гарантовано на диску.
    - Дані між flush'ами — у RAM, можуть втратитись.
    - Файл append-only, тому існуючі дані ніколи не корумпуються
      навіть якщо процес помре посеред write.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._buf = bytearray()
        self._fh: Optional[int] = None  # raw file descriptor (для fsync)
        self._total_bytes = 0
        self._closed = False
        self._lock = threading.Lock()

    # ---------- lifecycle

    def open(self) -> None:
        """Відкриває файл для append'у. Створює якщо не існує.

        Якщо файл уже існує — продовжуємо з його кінця, total_bytes
        ініціалізується розміром файлу. Це корисно для recovery.
        """
        with self._lock:
            if self._fh is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND гарантує атомарний append навіть з кількох процесів
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, 'O_BINARY'):
                flags |= os.O_BINARY
            self._fh = os.open(self.path, flags, 0o644)
            try:
                self._total_bytes = self.path.stat().st_size
            except OSError:
                self._total_bytes = 0

    def close(self) -> None:
        """Фінальний flush + закриття. Idempotent."""
        with self._lock:
            if self._closed:
                return
            # ВАЖЛИВО: спочатку дописати буфер, ПОТІМ позначити closed.
            # Якщо поставити _closed=True раніше — _flush_locked відмовить.
            try:
                self._flush_locked(fsync=True)
            except OSError as e:
                logger.warning("close() flush error на %s: %s", self.path, e)
            self._closed = True
            if self._fh is not None:
                try:
                    os.close(self._fh)
                except OSError as e:
                    logger.warning("close() error на %s: %s", self.path, e)
                self._fh = None

    # ---------- main api

    def append(self, data: bytes) -> None:
        """Додає байти у in-RAM буфер. Не пише на диск.

        Виклик з audio thread дозволений — швидка операція, без I/O.
        """
        if not data:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("PCM writer закритий")
            self._buf.extend(data)

    def flush(self, fsync: bool = True) -> int:
        """Пише буфер у файл і (опц.) робить fsync.

        Повертає кількість записаних байт цієї операції.
        Безпечний у спарних викликах: якщо буфер порожній — нічого
        не робить.
        """
        with self._lock:
            return self._flush_locked(fsync=fsync)

    def _flush_locked(self, fsync: bool) -> int:
        if self._closed and not self._buf:
            return 0
        if not self._buf:
            return 0
        if self._fh is None:
            # Повторне відкриття після close не дозволяємо
            if self._closed:
                raise RuntimeError("PCM writer закритий")
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

        # os.write може не записати все за один виклик — повторюємо
        written_total = 0
        view = memoryview(chunk)
        while written_total < len(chunk):
            n = os.write(self._fh, view[written_total:])
            if n <= 0:
                raise OSError(f"os.write повернув {n} на {self.path}")
            written_total += n

        if fsync:
            os.fsync(self._fh)

        self._total_bytes += written_total
        return written_total

    # ---------- properties

    @property
    def total_bytes(self) -> int:
        """Загальна кількість успішно записаних і fsync'ених байт."""
        with self._lock:
            return self._total_bytes

    @property
    def buffered_bytes(self) -> int:
        """Кількість байт у RAM-буфері, ще не записаних."""
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
