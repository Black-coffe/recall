"""SSE broker — pub/sub for server events.

Flow:
- The backend calls broker.publish(channel_id, event_dict).
- Subscribers (the SSE endpoint) receive events via broker.subscribe(channel_id),
  which returns a blocking iterator over queue.Queue.
- The connection drops → the subscriber is removed.

Simple in-memory, thread-safe implementation. Multi-process would need Redis.
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
    """Subscriber queues per channel, identified by a string (usually process_id)."""

    def __init__(self, queue_maxsize: int = 200):
        self._lock = threading.Lock()
        # channel_id → list[Queue]
        self._subscribers: Dict[str, List[queue.Queue]] = {}
        self._queue_maxsize = queue_maxsize

    def publish(self, channel_id: str, event: str, data: Any) -> None:
        """Publish an event. event is the SSE event name (for example 'progress', 'log', 'segment')."""
        if not channel_id:
            return
        payload = {"event": event, "data": data, "ts": time.time()}
        with self._lock:
            subs = list(self._subscribers.get(channel_id, []))
        # send without _lock so a slow subscriber does not block publish
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                logger.warning(f"SSE queue full for channel {channel_id}, dropping event {event}")

    def subscribe(self, channel_id: str, history: Optional[List[Dict]] = None) -> queue.Queue:
        """Subscribe. Returns a Queue — iterated in the endpoint."""
        q: queue.Queue = queue.Queue(maxsize=self._queue_maxsize)
        # Put in the initial "history" (if any) — for example, the process logs accumulated so far.
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
        """Generator for the Flask SSE response. Sends SSE-formatted bytes (UTF-8).

        Ends when:
          - an event with event == end_event is received;
          - or the client disconnects (Flask closes the generator).
        Keepalive comments are sent every `keepalive_seconds` to hold the connection open.
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


# Global broker (one per process)
broker = SSEBroker()
