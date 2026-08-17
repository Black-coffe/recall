"""SSE broker — pub/sub для серверных событий.

Поток:
- Backend дёргает broker.publish(channel_id, event_dict).
- Подписчики (SSE endpoint) получают события через broker.subscribe(channel_id),
  который возвращает blocking iterator поверх queue.Queue.
- Подключение разрывается → подписчик удаляется.

Простая in-memory реализация, потокобезопасная. Для multi-process потребуется Redis.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Iterator, List, Optional


logger = logging.getLogger(__name__)


class SSEBroker:
    """Очередь подписчиков на канал, идентифицируемый строкой (обычно process_id)."""

    def __init__(self, queue_maxsize: int = 200):
        self._lock = threading.Lock()
        # channel_id → list[Queue]
        self._subscribers: Dict[str, List[queue.Queue]] = {}
        self._queue_maxsize = queue_maxsize

    def publish(self, channel_id: str, event: str, data: Any) -> None:
        """Опубликовать событие. event — имя SSE-события (например 'progress', 'log', 'segment')."""
        if not channel_id:
            return
        payload = {"event": event, "data": data, "ts": time.time()}
        with self._lock:
            subs = list(self._subscribers.get(channel_id, []))
        # send без _lock, чтобы медленный subscriber не блокировал publish
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                logger.warning(f"SSE queue full for channel {channel_id}, dropping event {event}")

    def subscribe(self, channel_id: str, history: Optional[List[Dict]] = None) -> queue.Queue:
        """Подписаться. Возвращает Queue — её итерируем в endpoint'е."""
        q: queue.Queue = queue.Queue(maxsize=self._queue_maxsize)
        # Положить начальную "историю" (если есть) — например, накопленные логи процесса.
        if history:
            for h in history:
                try:
                    q.put_nowait(h)
                except queue.Full:
                    break
        with self._lock:
            self._subscribers.setdefault(channel_id, []).append(q)
        logger.debug(f"SSE subscribe to {channel_id}, total subs: {len(self._subscribers[channel_id])}")
        return q

    def unsubscribe(self, channel_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(channel_id, [])
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs and channel_id in self._subscribers:
                del self._subscribers[channel_id]
        logger.debug(f"SSE unsubscribe from {channel_id}")

    def stream_events(self, channel_id: str, history: Optional[List[Dict]] = None,
                      keepalive_seconds: float = 15.0,
                      end_event: str = "complete") -> Iterator[bytes]:
        """Generator для Flask SSE response. Шлёт SSE-форматированные байты (UTF-8).

        Завершается, когда:
          - получено событие с event == end_event;
          - либо клиент дисконнектится (Flask закроет генератор).
        Keepalive comments идут раз в `keepalive_seconds` для удержания соединения.
        """
        q = self.subscribe(channel_id, history=history)
        try:
            while True:
                try:
                    payload = q.get(timeout=keepalive_seconds)
                except queue.Empty:
                    yield b": keepalive\n\n"
                    continue

                event = payload.get("event", "message")
                data = payload.get("data", {})
                serialized = json.dumps(data, ensure_ascii=False)
                yield f"event: {event}\ndata: {serialized}\n\n".encode("utf-8")

                if event == end_event or event == "error":
                    yield b"event: close\ndata: {}\n\n"
                    return
        finally:
            self.unsubscribe(channel_id, q)


# Глобальный broker (один на процесс)
broker = SSEBroker()
