"""Тести для register_recording — авто-реєстрації запису в Audio Library.

Покривають корінь orphan-багу: реєстрація в audio_downloads винесена з
фронтендного /save у спільний серверний хелпер, який кличе finalize-callback
(у т.ч. при recovery). Ключове — ІДЕМПОТЕНТНІСТЬ: повторний finalize / save /
recovery не плодять дублів.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.db.connection import get_db_connection
from app.db.migrations import init_database
from app.services.recording.library import register_recording


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / 'test.db')
    init_database(p)
    return p


@pytest.fixture
def final_mp3(tmp_path):
    """Реальний файл — register_recording перевіряє is_file() і stat().st_size."""
    f = tmp_path / 'final.mp3'
    f.write_bytes(b'\x00' * 2048)
    return f


def _manifest(final_mp3, **over):
    m = {
        'session_id': 'rec_abc123def456',
        'final_mp3_path': str(final_mp3),
        'total_duration_sec': 123.4,
        'segments': [{'index': 0}, {'index': 1}],
        'name': None,
        'auto_name': 'Запис 2026-06-12 14:03',
    }
    m.update(over)
    return m


def _rows(db_path, sid):
    with get_db_connection(db_path) as conn:
        return conn.execute(
            'SELECT * FROM audio_downloads WHERE recording_session_id = ?', (sid,)
        ).fetchall()


# ---------------------------------------------------------------- create

def test_register_creates_row(db_path, final_mp3):
    sid = 'rec_abc123def456'
    reg = register_recording(db_path, sid, _manifest(final_mp3))
    assert reg is not None
    assert reg['created'] is True
    assert reg['name'] == 'Запис 2026-06-12 14:03'  # fallback на auto_name
    assert reg['duration_sec'] == pytest.approx(123.4)
    assert reg['file_size'] == 2048

    rows = _rows(db_path, sid)
    assert len(rows) == 1
    row = rows[0]
    assert row['source_type'] == 'recording'
    assert row['title'] == 'Запис 2026-06-12 14:03'
    assert row['author'] == 'Локальний запис'
    assert row['youtube_id'] == f'recording_{sid}'
    assert row['recording_segments'] == 2
    assert row['recording_duration_sec'] == pytest.approx(123.4)
    assert row['duration'] == 123  # int


def test_register_uses_explicit_name(db_path, final_mp3):
    sid = 'rec_abc123def456'
    reg = register_recording(db_path, sid, _manifest(final_mp3), name='Фонд — фін модель')
    assert reg['name'] == 'Фонд — фін модель'
    assert _rows(db_path, sid)[0]['title'] == 'Фонд — фін модель'


def test_name_precedence_manifest_name_over_auto(db_path, final_mp3):
    sid = 'rec_abc123def456'
    reg = register_recording(
        db_path, sid, _manifest(final_mp3, name='Ручна назва'),
    )
    # manifest.name важливіший за auto_name, коли явної name немає
    assert reg['name'] == 'Ручна назва'


# ---------------------------------------------------------------- idempotency

def test_register_idempotent_no_duplicate(db_path, final_mp3):
    """Повторний виклик (finalize-callback + потім /save) → один рядок."""
    sid = 'rec_abc123def456'
    first = register_recording(db_path, sid, _manifest(final_mp3))
    second = register_recording(db_path, sid, _manifest(final_mp3))

    assert first['created'] is True
    assert second['created'] is False
    assert second['download_id'] == first['download_id']
    assert len(_rows(db_path, sid)) == 1


def test_register_existing_updates_title_on_explicit_name(db_path, final_mp3):
    """Авто-реєстрація (auto_name) → потім ручний /save з назвою оновлює title."""
    sid = 'rec_abc123def456'
    auto = register_recording(db_path, sid, _manifest(final_mp3))  # auto_name
    manual = register_recording(db_path, sid, _manifest(final_mp3), name='Нова назва')

    assert manual['created'] is False
    assert manual['download_id'] == auto['download_id']
    assert manual['name'] == 'Нова назва'
    rows = _rows(db_path, sid)
    assert len(rows) == 1
    assert rows[0]['title'] == 'Нова назва'


def test_register_existing_keeps_title_without_explicit_name(db_path, final_mp3):
    """Повторна авто-реєстрація не перетирає вже наявну назву."""
    sid = 'rec_abc123def456'
    register_recording(db_path, sid, _manifest(final_mp3), name='Збережена')
    again = register_recording(db_path, sid, _manifest(final_mp3))  # no explicit
    assert again['created'] is False
    assert _rows(db_path, sid)[0]['title'] == 'Збережена'


def test_youtube_id_unique_constraint_holds(db_path, final_mp3):
    """youtube_id=recording_<sid> справді UNIQUE — другий прямий insert падає,
    а хелпер цього уникає через pre-check."""
    sid = 'rec_abc123def456'
    register_recording(db_path, sid, _manifest(final_mp3))
    with pytest.raises(sqlite3.IntegrityError):
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO audio_downloads (youtube_url, youtube_id, title, "
                "file_path, source_type) VALUES (?, ?, ?, ?, ?)",
                ('x', f'recording_{sid}', 't', 'p', 'recording'),
            )
            conn.commit()


# ---------------------------------------------------------------- empty / missing

def test_register_returns_none_when_no_final_mp3_path(db_path):
    sid = 'rec_abc123def456'
    m = {'final_mp3_path': None, 'total_duration_sec': 0.0, 'auto_name': 'X'}
    assert register_recording(db_path, sid, m) is None
    assert len(_rows(db_path, sid)) == 0


def test_register_returns_none_when_file_missing(db_path, tmp_path):
    sid = 'rec_abc123def456'
    m = {'final_mp3_path': str(tmp_path / 'nope.mp3'), 'auto_name': 'X'}
    assert register_recording(db_path, sid, m) is None
    assert len(_rows(db_path, sid)) == 0


# ---------------------------------------------------------------- category (Phase 21)

def _seed_category(db_path, name='AI'):
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            'INSERT INTO categories (name, name_norm) VALUES (?, ?)',
            (name, name.casefold()),
        )
        conn.commit()
        return cur.lastrowid


def test_register_stores_explicit_category(db_path, final_mp3):
    sid = 'rec_abc123def456'
    cid = _seed_category(db_path)
    reg = register_recording(db_path, sid, _manifest(final_mp3), category_id=cid)
    assert reg['category_id'] == cid
    assert _rows(db_path, sid)[0]['category_id'] == cid


def test_register_looks_up_category_from_copilot_session(db_path, final_mp3):
    """Напрямок, заданий при записі (copilot_sessions), підтягується автоматично."""
    sid = 'rec_abc123def456'
    cid = _seed_category(db_path, 'Фонд')
    with get_db_connection(db_path) as conn:
        conn.execute(
            'INSERT INTO copilot_sessions (recording_session_id, category_id) VALUES (?, ?)',
            (sid, cid),
        )
        conn.commit()
    reg = register_recording(db_path, sid, _manifest(final_mp3))  # без явної category
    assert reg['category_id'] == cid
    assert _rows(db_path, sid)[0]['category_id'] == cid


def test_register_backfills_category_on_existing_row(db_path, final_mp3):
    """Якщо рядок уже є без категорії, а потім зʼявляється — дозаповнюємо."""
    sid = 'rec_abc123def456'
    register_recording(db_path, sid, _manifest(final_mp3))  # без категорії
    assert _rows(db_path, sid)[0]['category_id'] is None
    cid = _seed_category(db_path)
    again = register_recording(db_path, sid, _manifest(final_mp3), category_id=cid)
    assert again['created'] is False
    assert again['category_id'] == cid
    assert _rows(db_path, sid)[0]['category_id'] == cid


# ---------------------------------------------------------------- race (§12: auto-finalize vs /save)

def test_register_survives_lost_insert_race(db_path, final_mp3, monkeypatch):
    """Гонка: два виклики (auto-register при finalize і ручний /save) обидва
    проходять SELECT як «рядка нема», обидва йдуть в INSERT. До фіксу другий
    падав з IntegrityError (UNIQUE youtube_id); тепер ON CONFLICT DO NOTHING +
    повторний SELECT — той, хто програв, працює з чужим рядком (created=False).

    Симулюємо програш гонки: перший _find_existing повертає None (як бачив
    потік до вставки конкурента), наступні — реальний результат.
    """
    from app.services.recording import library as lib

    sid = 'rec_race0001'
    manifest = _manifest(final_mp3, session_id=sid)

    # Конкурент уже вставив рядок (виграв гонку).
    first = register_recording(db_path, sid, manifest)
    assert first['created'] is True

    real_find = lib._find_existing
    calls = {'n': 0}

    def racy_find(c, session_id, youtube_id):
        calls['n'] += 1
        if calls['n'] == 1:
            return None  # SELECT встиг до вставки конкурента
        return real_find(c, session_id, youtube_id)

    monkeypatch.setattr(lib, '_find_existing', racy_find)

    # Той, хто програв гонку: не падає, бачить чужий рядок, оновлює назву.
    second = register_recording(db_path, sid, manifest, name='Ручна назва')
    assert second is not None
    assert second['created'] is False
    assert second['download_id'] == first['download_id']
    assert calls['n'] >= 2  # був повторний пошук після програного INSERT

    rows = _rows(db_path, sid)
    assert len(rows) == 1
    assert rows[0]['title'] == 'Ручна назва'
