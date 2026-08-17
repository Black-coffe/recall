"""Тести для boot-time reconcile записів у бібліотеку
(``app.services.recording.reconcile``, 08.07.2026).

Чекер запускається на кожному старті app.py і приводить Медіатеку
(``audio_downloads``) у відповідність до диска БЕЗ транскрибації:
реєструє сиріт, лікує мертві ``file_path`` (переїзд проєкту Whisper→Recall),
синхронізує auto-name назви з manifest.name. Реальні назви не чіпає,
не-фіналізовані сесії ігнорує, порожні (без mp3) пропускає.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.db.migrations import init_database
from app.services.recording.reconcile import reconcile_recordings
from app.services.recording.session_store import (
    MANIFEST_FILENAME,
    STATUS_FINALIZED,
    SessionStore,
)


# --------------------------------------------------------------- fixtures/helpers

@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = str(tmp_path / 'reconcile_test.db')
    init_database(p)
    return p


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / 'sessions')


def _set_manifest_field(store: SessionStore, sid: str, **fields):
    path = store.session_dir(sid) / MANIFEST_FILENAME
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw.update(fields)
    path.write_text(json.dumps(raw), encoding='utf-8')


def _make_finalized(store: SessionStore, sid: str, *, name=None,
                    duration: float = 60.0, create_file: bool = True,
                    final_path: str | None = None) -> str:
    """Фіналізована сесія з реальним final.mp3 на диску. Повертає шлях до mp3."""
    store.create(sid, 48000, 2, mic_device_name='Mic', system_device_name='Sys')
    session_dir = store.session_dir(sid)
    real = session_dir / 'final.mp3'
    if create_file:
        real.write_bytes(b'fake mp3 audio bytes')
    fields = {
        'status': STATUS_FINALIZED,
        'final_mp3_path': final_path if final_path is not None else str(real),
        'total_duration_sec': duration,
    }
    if name is not None:
        fields['name'] = name
    _set_manifest_field(store, sid, **fields)
    return str(real)


def _insert_audio_download(db_path: str, sid: str, **overrides) -> int:
    fields = {
        'youtube_url': f'recording://{sid}',
        'youtube_id': f'recording_{sid}',
        'title': f'Запис {sid[:8]}',
        'source_type': 'recording',
        'recording_session_id': sid,
        'file_path': None,
    }
    fields.update(overrides)
    cols = ', '.join(fields)
    ph = ', '.join('?' * len(fields))
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f'INSERT INTO audio_downloads ({cols}) VALUES ({ph})', list(fields.values()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _audio_row(db_path: str, sid: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            'SELECT * FROM audio_downloads WHERE recording_session_id = ?', (sid,)
        ).fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------- orphan registration

def test_orphan_registered_with_manifest_name(db_path, store):
    _make_finalized(store, 'rec_orphan1', name='Дзвінок з Vodafone')
    stats = reconcile_recordings(db_path, store)

    assert stats['registered'] == 1
    row = _audio_row(db_path, 'rec_orphan1')
    assert row is not None
    assert row['title'] == 'Дзвінок з Vodafone'
    assert row['source_type'] == 'recording'


def test_orphan_without_name_uses_auto_name_date(db_path, store):
    """Немає власної назви → назва = auto_name (дата створення) — саме те,
    що просив юзер: «якщо назви нема, беремо коли створено»."""
    _make_finalized(store, 'rec_orphan2', name=None)
    auto = store.read('rec_orphan2')['auto_name']
    assert auto  # SessionStore генерує "Запис YYYY-MM-DD HH:MM"

    reconcile_recordings(db_path, store)
    row = _audio_row(db_path, 'rec_orphan2')
    assert row['title'] == auto


def test_reconcile_never_transcribes(db_path, store):
    """Чекер лише реєструє в audio_downloads — жодного рядка транскрипту
    не з'являється."""
    _make_finalized(store, 'rec_notr', name='Без транскрипту')
    reconcile_recordings(db_path, store)

    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute('SELECT COUNT(*) FROM transcriptions').fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_orphan_without_final_file_skipped(db_path, store):
    """Фіналізована сесія, але final.mp3 нема (порожній/аборт) — не реєструємо."""
    _make_finalized(store, 'rec_empty', name='Порожній', create_file=False,
                    final_path=r'E:\nonexistent\final.mp3')
    stats = reconcile_recordings(db_path, store)

    assert stats['registered'] == 0
    assert stats['skipped_no_file'] == 1
    assert _audio_row(db_path, 'rec_empty') is None


def test_non_finalized_session_ignored(db_path, store):
    """Сесія в статусі recording (ще пише) — у бібліотеку не тягнемо."""
    store.create('rec_active', 48000, 2)  # status=recording за замовчуванням
    (store.session_dir('rec_active') / 'final.mp3').write_bytes(b'x')
    stats = reconcile_recordings(db_path, store)

    assert stats['registered'] == 0
    assert _audio_row(db_path, 'rec_active') is None


# --------------------------------------------------------------- path healing

def test_dead_file_path_healed_in_audio_downloads(db_path, store):
    """Рядок у бібліотеці існує, але file_path мертвий (старий Whisper-корінь).
    Reconcile підміняє його на живий final.mp3 з папки сесії."""
    real = _make_finalized(store, 'rec_moved', name='Переїхав')
    dead = r'E:\Projects\Whisper\recordings\sessions\rec_moved\final.mp3'
    _insert_audio_download(db_path, 'rec_moved', title='Переїхав', file_path=dead)

    stats = reconcile_recordings(db_path, store)

    assert stats['path_healed'] == 1
    assert _audio_row(db_path, 'rec_moved')['file_path'] == real


def test_dead_file_path_healed_in_transcriptions(db_path, store):
    """file_path у transcriptions цього запису теж лікується (щоб оригінал/
    re-transcribe з Архіву не бились об мертвий шлях)."""
    real = _make_finalized(store, 'rec_txmoved', name='Архівний')
    dead = r'E:\Projects\Whisper\recordings\sessions\rec_txmoved\final.mp3'
    _insert_audio_download(db_path, 'rec_txmoved', file_path=real)  # audiolib вже здоровий
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO transcriptions (source_type, source_name, file_path, transcript_text) "
            "VALUES ('recording', 'Архівний', ?, 'текст')", (dead,))
        conn.commit()
    finally:
        conn.close()

    stats = reconcile_recordings(db_path, store)
    assert stats['tx_path_healed'] == 1

    conn = sqlite3.connect(db_path)
    try:
        fp = conn.execute(
            "SELECT file_path FROM transcriptions WHERE source_name='Архівний'").fetchone()[0]
    finally:
        conn.close()
    assert fp == real


def test_healthy_path_not_touched(db_path, store):
    """Живий file_path не чіпаємо (path_healed=0)."""
    real = _make_finalized(store, 'rec_ok', name='Здоровий')
    _insert_audio_download(db_path, 'rec_ok', title='Здоровий', file_path=real)

    stats = reconcile_recordings(db_path, store)
    assert stats['path_healed'] == 0
    assert _audio_row(db_path, 'rec_ok')['file_path'] == real


# --------------------------------------------------------------- title sync

def test_placeholder_title_synced_from_manifest(db_path, store):
    """Auto-name плейсхолдер у бібліотеці + справжня назва в manifest →
    підтягуємо назву."""
    real = _make_finalized(store, 'rec_rename', name='Vodafon та фонд')
    auto = store.read('rec_rename')['auto_name']  # "Запис YYYY-..."
    _insert_audio_download(db_path, 'rec_rename', title=auto, file_path=real)

    stats = reconcile_recordings(db_path, store)
    assert stats['title_synced'] == 1
    assert _audio_row(db_path, 'rec_rename')['title'] == 'Vodafon та фонд'


def test_real_title_not_overwritten(db_path, store):
    """Не-плейсхолдерну (реальну) назву в бібліотеці НІКОЛИ не перезаписуємо,
    навіть якщо manifest.name інший."""
    real = _make_finalized(store, 'rec_keep', name='Нова назва з manifest')
    _insert_audio_download(db_path, 'rec_keep', title='Моя ручна назва', file_path=real)

    stats = reconcile_recordings(db_path, store)
    assert stats['title_synced'] == 0
    assert _audio_row(db_path, 'rec_keep')['title'] == 'Моя ручна назва'


# --------------------------------------------------------------- idempotency

def test_idempotent_second_run_no_changes(db_path, store):
    """Другий прогін поспіль не робить жодних змін."""
    _make_finalized(store, 'rec_idem', name='Vodafon та фонд')
    dead = r'E:\Projects\Whisper\recordings\sessions\rec_idem\final.mp3'
    _insert_audio_download(db_path, 'rec_idem', title=f'Запис {"rec_idem"[:8]}', file_path=dead)

    first = reconcile_recordings(db_path, store)
    assert first['path_healed'] + first['title_synced'] >= 1  # щось зробив

    second = reconcile_recordings(db_path, store)
    assert second['registered'] == 0
    assert second['path_healed'] == 0
    assert second['title_synced'] == 0
    assert second['tx_path_healed'] == 0
    assert second['errors'] == 0


def test_broken_session_does_not_abort_sweep(db_path, store):
    """Одна зламана сесія (корумпований manifest) не зриває весь прохід —
    здорова сирота поруч усе одно реєструється."""
    _make_finalized(store, 'rec_good', name='Здорова')
    bad_dir = store.base_dir / 'rec_bad'
    bad_dir.mkdir(parents=True)
    (bad_dir / MANIFEST_FILENAME).write_text('{broken json', encoding='utf-8')

    stats = reconcile_recordings(db_path, store)
    # corrupt manifest відсіюється list_all() (не потрапляє у scanned), головне —
    # здорова сесія зареєстрована і sweep не впав
    assert _audio_row(db_path, 'rec_good') is not None
    assert stats['registered'] == 1
