"""Тести для app.services.recording.pcm_writer (Phase 9.2)."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app.services.recording.pcm_writer import ChunkedPcmWriter


# ---------------------------------------------------------------- basic api

def test_append_then_flush_creates_file(tmp_path: Path):
    pcm_path = tmp_path / 'mic.pcm'
    w = ChunkedPcmWriter(pcm_path)
    try:
        w.append(b'\x01\x02\x03\x04')
        assert w.buffered_bytes == 4
        assert w.total_bytes == 0  # ще не на диску
        n = w.flush()
        assert n == 4
        assert w.total_bytes == 4
        assert w.buffered_bytes == 0
    finally:
        w.close()
    assert pcm_path.read_bytes() == b'\x01\x02\x03\x04'


def test_multiple_flushes_accumulate(tmp_path: Path):
    p = tmp_path / 'm.pcm'
    with ChunkedPcmWriter(p) as w:
        w.append(b'AAAA')
        w.flush()
        w.append(b'BBBB')
        w.flush()
        w.append(b'CCCC')
        w.flush()
        assert w.total_bytes == 12
    assert p.read_bytes() == b'AAAABBBBCCCC'


def test_close_flushes_remaining_buffer(tmp_path: Path):
    p = tmp_path / 'm.pcm'
    w = ChunkedPcmWriter(p)
    w.append(b'XYZW')
    # Без явного flush — close мусить дописати
    w.close()
    assert p.read_bytes() == b'XYZW'
    assert w.total_bytes == 4


def test_double_close_idempotent(tmp_path: Path):
    p = tmp_path / 'm.pcm'
    w = ChunkedPcmWriter(p)
    w.append(b'data')
    w.close()
    w.close()  # не повинно падати
    assert w.is_closed is True


def test_append_after_close_raises(tmp_path: Path):
    p = tmp_path / 'm.pcm'
    w = ChunkedPcmWriter(p)
    w.close()
    with pytest.raises(RuntimeError):
        w.append(b'oops')


def test_empty_append_is_noop(tmp_path: Path):
    p = tmp_path / 'm.pcm'
    with ChunkedPcmWriter(p) as w:
        w.append(b'')
        w.flush()
        assert w.total_bytes == 0
    # Файл міг бути створений lazy-open, але пустий
    if p.exists():
        assert p.read_bytes() == b''


def test_empty_flush_is_noop(tmp_path: Path):
    p = tmp_path / 'm.pcm'
    with ChunkedPcmWriter(p) as w:
        n = w.flush()
        assert n == 0
        assert w.total_bytes == 0


# ---------------------------------------------------------------- recovery

def test_open_existing_file_continues_from_end(tmp_path: Path):
    """Recovery use-case: writer відкриває вже існуючий PCM (наприклад,
    після крах рестарту). total_bytes ініціалізується розміром файлу."""
    p = tmp_path / 'm.pcm'
    p.write_bytes(b'OLDDATA')

    with ChunkedPcmWriter(p) as w:
        w.open()
        assert w.total_bytes == 7
        w.append(b'NEW')
        w.flush()
        assert w.total_bytes == 10

    assert p.read_bytes() == b'OLDDATANEW'


def test_explicit_open(tmp_path: Path):
    p = tmp_path / 'm.pcm'
    w = ChunkedPcmWriter(p)
    w.open()
    assert p.parent.exists()
    # Файл створений (даже до append)
    assert p.exists()
    w.close()


# ---------------------------------------------------------------- threading

def test_concurrent_append_preserves_all_bytes(tmp_path: Path):
    """Якщо багато потоків викликає append, після фінального flush
    мусять зберегтися УСІ байти (не обов'язково в порядку)."""
    p = tmp_path / 'm.pcm'
    chunks_per_thread = 200
    threads_count = 8
    payload_size = 32

    def writer_fn(thread_id: int):
        for _ in range(chunks_per_thread):
            w_writer.append(bytes([thread_id]) * payload_size)

    with ChunkedPcmWriter(p) as w_writer:
        threads = [
            threading.Thread(target=writer_fn, args=(i,))
            for i in range(threads_count)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w_writer.flush()
        assert w_writer.total_bytes == threads_count * chunks_per_thread * payload_size

    data = p.read_bytes()
    assert len(data) == threads_count * chunks_per_thread * payload_size
    # Кожен потік написав по chunks_per_thread * payload_size байт зі своїм id
    for i in range(threads_count):
        assert data.count(bytes([i])) == chunks_per_thread * payload_size


def test_concurrent_flush_safe(tmp_path: Path):
    """Повторні flush з різних потоків не повинні падати або
    дублювати дані."""
    p = tmp_path / 'm.pcm'
    barrier = threading.Barrier(4)

    with ChunkedPcmWriter(p) as w:
        w.append(b'X' * 1000)

        def flush_fn():
            barrier.wait()
            w.flush()

        threads = [threading.Thread(target=flush_fn) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert w.total_bytes == 1000

    assert p.read_bytes() == b'X' * 1000


# ---------------------------------------------------------------- integrity

def test_large_write_chunk(tmp_path: Path):
    """Великі writes (>звичайного buffer) корректно обробляються."""
    p = tmp_path / 'm.pcm'
    big = bytes(range(256)) * 4096  # 1 MiB
    with ChunkedPcmWriter(p) as w:
        w.append(big)
        w.flush()
        assert w.total_bytes == len(big)
    assert p.read_bytes() == big


def test_fsync_actually_called(tmp_path: Path, monkeypatch):
    """Перевіряємо що flush(fsync=True) викликає os.fsync."""
    p = tmp_path / 'm.pcm'
    fsync_calls = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, 'fsync', tracking_fsync)
    with ChunkedPcmWriter(p) as w:
        w.append(b'data')
        w.flush(fsync=True)
        assert len(fsync_calls) >= 1

        prev_count = len(fsync_calls)
        w.append(b'more')
        w.flush(fsync=False)
        # fsync=False — додаткових викликів не має
        assert len(fsync_calls) == prev_count


def test_creates_parent_directories(tmp_path: Path):
    p = tmp_path / 'sub' / 'dir' / 'm.pcm'
    with ChunkedPcmWriter(p) as w:
        w.append(b'X')
        w.flush()
    assert p.is_file()
