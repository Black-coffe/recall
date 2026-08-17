"""SystemMonitor — фоновый поток, который раз в N секунд снимает CPU/GPU/RAM.

Раньше /api/system_stats делал psutil.cpu_percent(interval=0.1) синхронно — это
блокировало воркер на 100мс на каждый запрос. Теперь endpoint просто читает
снапшот, накопленный фоновым потоком, без блокировки.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

import psutil


try:
    import torch  # для GPU stats
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False


logger = logging.getLogger(__name__)


class SystemMonitor:
    def __init__(self, interval: float = 1.0):
        self._interval = max(0.5, interval)
        self._lock = threading.Lock()
        self._snapshot: Dict = self._empty_snapshot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Прайм cpu_percent — первый вызов всегда возвращает 0.0
        psutil.cpu_percent(interval=None)

    def _empty_snapshot(self) -> Dict:
        return {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "memory_available_gb": 0.0,
            "memory_total_gb": 0.0,
            "gpu_available": False,
            "ts": 0.0,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="SystemMonitor")
        self._thread.start()
        logger.info(f"SystemMonitor started (interval={self._interval}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("SystemMonitor stopped")

    def get_snapshot(self) -> Dict:
        with self._lock:
            return dict(self._snapshot)

    def _run(self) -> None:
        while not self._stop.is_set():
            snap = self._collect()
            with self._lock:
                self._snapshot = snap
            self._stop.wait(self._interval)

    def _collect(self) -> Dict:
        snap: Dict = {
            "cpu_percent": psutil.cpu_percent(interval=None),  # неблокирующий
            "memory_percent": psutil.virtual_memory().percent,
            "memory_available_gb": psutil.virtual_memory().available / (1024 ** 3),
            "memory_total_gb": psutil.virtual_memory().total / (1024 ** 3),
            "gpu_available": False,
            "ts": time.time(),
        }
        if _HAS_TORCH:
            try:
                if torch.cuda.is_available():
                    snap["gpu_available"] = True
                    snap["gpu_name"] = torch.cuda.get_device_name(0)
                    try:
                        allocated = torch.cuda.memory_allocated(0)
                        total = torch.cuda.get_device_properties(0).total_memory
                        snap["gpu_memory_used_gb"] = allocated / (1024 ** 3)
                        snap["gpu_memory_total_gb"] = total / (1024 ** 3)
                        snap["gpu_utilization"] = (allocated / total) * 100 if total else 0.0
                    except Exception:
                        pass
            except Exception:
                snap["gpu_available"] = False
        return snap


# Глобальный монитор (один на процесс). start() вызывается в app.py.
monitor = SystemMonitor(interval=1.0)
