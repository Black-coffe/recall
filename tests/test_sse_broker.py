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


# ---- event ring ----

def test_ring_only_eligible_channels_and_events():
    from app.services.sse_broker import RING_SIZE  # noqa: F401
    b = SSEBroker()
    b.publish("recording:s1", "status", {"a": 1})
    b.publish("recording:s1", "level", {"rms": 0.1})
    b.publish("recording:s1", "chunk_saved", {})
    b.publish("proc-123", "progress", {})
    assert [e["event"] for e in b.read_since("recording:s1", None)["events"]] == ["status"]
    assert b.read_since("proc-123", None)["events"] == []


def test_read_since_cursor_order_and_filter():
    b = SSEBroker()
    b.publish("recording:s1", "status", 1)
    cur = b.cursor("recording:s1")
    b.publish("recording:s1", "live_segment", 2)
    b.publish("recording:s1", "error", 3)
    res = b.read_since("recording:s1", cur)
    assert [e["data"] for e in res["events"]] == [2, 3]
    assert not res["gap"] and not res["reset"]
    res2 = b.read_since("recording:s1", cur, {"error"})
    assert [e["data"] for e in res2["events"]] == [3]
    assert b.read_since("recording:s1", res["next"])["events"] == []


def test_read_since_reset_on_other_boot_or_malformed():
    b = SSEBroker()
    b.publish("recording:s1", "status", 1)
    for bad in ("deadbeef:0", "garbage", "x:y"):
        res = b.read_since("recording:s1", bad)
        assert res["reset"] is True
        assert len(res["events"]) == 1


def test_read_since_gap_when_ring_overflowed():
    b = SSEBroker()
    cur = b.cursor("recording:s1")
    for i in range(520):
        b.publish("recording:s1", "status", i)
    res = b.read_since("recording:s1", cur)
    assert res["gap"] is True
    assert len(res["events"]) == 500


def test_subscriber_payload_shape_unchanged_and_filtered():
    b = SSEBroker()
    q_all = b.subscribe("recording:s1")
    q_err = b.subscribe("recording:s1", events=["error"])
    b.publish("recording:s1", "status", {"x": 1})
    b.publish("recording:s1", "error", {"y": 2})
    first = q_all.get(timeout=1)
    assert set(first) == {"event", "data", "ts"}
    assert q_all.get(timeout=1)["event"] == "error"
    assert q_err.get(timeout=1)["event"] == "error"
    assert q_err.empty()
