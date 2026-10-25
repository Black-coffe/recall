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
import uuid
from collections import deque
from typing import Any, Dict, Iterable, Iterator, List, Optional


logger = logging.getLogger(__name__)

# Channels whose events are kept in a bounded ring for polling clients (MCP wait_for).
RING_PREFIXES = ("recording:", "system:", "pipes", "doc_import", "backfill")
RING_SIZE = 500
RING_EXCLUDED_EVENTS = {"level", "chunk_saved"}


class SSEBroker:
    """Subscriber queues per channel, identified by a string (usually process_id)."""

    def __init__(self, queue_maxsize: int = 200):
        self._lock = threading.Lock()
        # channel_id → list[Queue]
        self._subscribers: Dict[str, List[queue.Queue]] = {}
        self._queue_maxsize = queue_maxsize
        # Event ring: one monotonic seq per broker, a boot epoch in the cursor.
        self.boot_id: str = uuid.uuid4().hex[:8]
        self._seq = 0
        self._rings: Dict[str, deque] = {}
        # channel → seq of the newest event evicted from its ring (0 = none)
        self._evicted: Dict[str, int] = {}
        self._ring_cond = threading.Condition(self._lock)

    @staticmethod
    def _ringed(channel_id: str, event: str) -> bool:
        return channel_id.startswith(RING_PREFIXES) and event not in RING_EXCLUDED_EVENTS

    def publish(self, channel_id: str, event: str, data: Any) -> None:
        """Publish an event. event is the SSE event name (for example 'progress', 'log', 'segment')."""
        if not channel_id:
            return
        payload = {"event": event, "data": data, "ts": time.time()}
        with self._lock:
            subs = list(self._subscribers.get(channel_id, []))
            if self._ringed(channel_id, event):
                self._seq += 1
                ring = self._rings.setdefault(channel_id, deque())
                if len(ring) >= RING_SIZE:
                    self._evicted[channel_id] = ring.popleft()["seq"]
                ring.append({"seq": self._seq, "event": event, "data": data, "ts": payload["ts"]})
                self._ring_cond.notify_all()
        # send without _lock so a slow subscriber does not block publish
        for q in subs:
            wanted = getattr(q, "events", None)
            if wanted is not None and event not in wanted:
                continue
            try:
                q.put_nowait(payload)
            except queue.Full:
                logger.warning(f"SSE queue full for channel {channel_id}, dropping event {event}")

    def subscribe(self, channel_id: str, history: Optional[List[Dict]] = None,
                  events: Optional[Iterable[str]] = None) -> queue.Queue:
        """Subscribe. Returns a Queue — iterated in the endpoint.

        ``events`` limits the queue to those event names (None = all).
        """
        q: queue.Queue = queue.Queue(maxsize=self._queue_maxsize)
        q.events = set(events) if events is not None else None  # type: ignore[attr-defined]
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

    def cursor(self, channel_id: str) -> str:
        """Cursor that reads only events published after this call."""
        with self._lock:
            return f"{self.boot_id}:{self._seq}"

    def read_since(self, channel_id: str, after: Optional[str],
                   events: Optional[set] = None) -> Dict[str, Any]:
        """Ring events of the channel after the cursor, in order.

        ``reset``: the cursor is from another boot or malformed (read from the ring start).
        ``gap``: events after the cursor were already evicted from the ring.
        """
        reset = False
        gap = False
        after_seq = 0
        if after is not None:
            boot, _, num = str(after).partition(":")
            try:
                after_seq = int(num)
                if boot != self.boot_id or after_seq < 0:
                    raise ValueError
            except ValueError:
                reset = True
                after_seq = 0
        with self._lock:
            ring = self._rings.get(channel_id, ())
            if not reset and after is not None and after_seq < self._evicted.get(channel_id, 0):
                gap = True
            out = [dict(e) for e in ring
                   if e["seq"] > after_seq and (events is None or e["event"] in events)]
            nxt = f"{self.boot_id}:{self._seq}"
        return {"events": out, "next": nxt, "gap": gap, "reset": reset}

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
