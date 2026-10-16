"""Tests for SSEBroker (Phase 2)."""
import json
import threading
import time

from app.services.sse_broker import SSEBroker


def test_subscribe_unsubscribe():
    broker = SSEBroker()
    q = broker.subscribe("ch1")
    broker.unsubscribe("ch1", q)
    # After unsubscribe the channel must disappear from the list
    assert "ch1" not in broker._subscribers


def test_publish_delivers_to_subscriber():
    broker = SSEBroker()
    q = broker.subscribe("ch1")
    broker.publish("ch1", "test_event", {"foo": "bar"})
    payload = q.get(timeout=1)
    assert payload["event"] == "test_event"
    assert payload["data"] == {"foo": "bar"}


def test_publish_to_other_channel_isolated():
    broker = SSEBroker()
    q1 = broker.subscribe("ch1")
    q2 = broker.subscribe("ch2")
    broker.publish("ch1", "evt", {"x": 1})
    # ch2 must receive nothing
    assert q2.qsize() == 0
    # ch1 — receives
    assert q1.qsize() == 1


def test_history_replays_on_subscribe():
    broker = SSEBroker()
    history = [
        {"event": "log", "data": {"msg": "first"}, "ts": 1},
        {"event": "log", "data": {"msg": "second"}, "ts": 2},
    ]
    q = broker.subscribe("ch1", history=history)
    # Two entries in the history must be in the queue
    assert q.qsize() == 2
    first = q.get_nowait()
    assert first["data"]["msg"] == "first"


def test_stream_events_yields_bytes():
    """stream_events must return bytes (needed for Werkzeug direct_passthrough)."""
    broker = SSEBroker()
    history = [{"event": "log", "data": {"x": 1}, "ts": 1}]

    gen = broker.stream_events("ch1", history=history, keepalive_seconds=10)
    # The first yield is the history.
    chunk = next(gen)
    assert isinstance(chunk, bytes)
    assert b"event: log" in chunk
    assert b'"x": 1' in chunk

    # Finish the generator via close
    gen.close()


def test_publish_no_subscribers_does_not_crash():
    broker = SSEBroker()
    # Publishing to an empty channel must not fail
    broker.publish("nobody", "evt", {"foo": "bar"})


def test_concurrent_publish_thread_safe():
    """We simulate simultaneous publishing from two threads."""
    broker = SSEBroker()
    q = broker.subscribe("ch1")
    N = 50

    def producer(start):
        for i in range(N):
            broker.publish("ch1", "evt", {"i": start + i})

    t1 = threading.Thread(target=producer, args=(0,))
    t2 = threading.Thread(target=producer, args=(1000,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 100 publishes from two threads — the queue must hold 100 (no losses)
    assert q.qsize() == 2 * N
