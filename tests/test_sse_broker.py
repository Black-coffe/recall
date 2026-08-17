"""Тесты для SSEBroker (Phase 2)."""
import json
import threading
import time

from app.services.sse_broker import SSEBroker


def test_subscribe_unsubscribe():
    broker = SSEBroker()
    q = broker.subscribe("ch1")
    broker.unsubscribe("ch1", q)
    # Після unsubscribe канал має зникнути зі списку
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
    # ch2 не має нічого отримати
    assert q2.qsize() == 0
    # ch1 — отримує
    assert q1.qsize() == 1


def test_history_replays_on_subscribe():
    broker = SSEBroker()
    history = [
        {"event": "log", "data": {"msg": "first"}, "ts": 1},
        {"event": "log", "data": {"msg": "second"}, "ts": 2},
    ]
    q = broker.subscribe("ch1", history=history)
    # Дві записи в історії мають бути в черзі
    assert q.qsize() == 2
    first = q.get_nowait()
    assert first["data"]["msg"] == "first"


def test_stream_events_yields_bytes():
    """stream_events має повертати bytes (необхідно для Werkzeug direct_passthrough)."""
    broker = SSEBroker()
    history = [{"event": "log", "data": {"x": 1}, "ts": 1}]

    gen = broker.stream_events("ch1", history=history, keepalive_seconds=10)
    # Перший yield — це історія.
    chunk = next(gen)
    assert isinstance(chunk, bytes)
    assert b"event: log" in chunk
    assert b'"x": 1' in chunk

    # Завершуємо генератор шляхом close
    gen.close()


def test_publish_no_subscribers_does_not_crash():
    broker = SSEBroker()
    # Публікація в пустий канал не повинна впасти
    broker.publish("nobody", "evt", {"foo": "bar"})


def test_concurrent_publish_thread_safe():
    """Симулюємо одночасну публікацію з двох потоків."""
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

    # 100 publish'ів від двох потоків — у черзі має бути 100 (без втрат)
    assert q.qsize() == 2 * N
