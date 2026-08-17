"""Тести для app.services.recording.session_store (Phase 9.2)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.services.recording.session_store import (
    ACTIVE_STATUSES,
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    STATUS_FINALIZED,
    STATUS_PAUSED,
    STATUS_RECORDING,
    STATUS_STOPPING,
    STREAM_MIC,
    STREAM_SYSTEM,
    SessionStore,
    SessionStoreError,
)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path)


# ---------------------------------------------------------------- create

def test_create_makes_dir_and_manifest(store: SessionStore, tmp_path: Path):
    mf = store.create('rec_001', sample_rate=48000, channels=2,
                      mic_device_name='Mic A', system_device_name='Speakers Loopback')
    assert mf['session_id'] == 'rec_001'
    assert mf['version'] == MANIFEST_VERSION
    assert mf['status'] == STATUS_RECORDING
    assert mf['sample_rate'] == 48000
    assert mf['channels'] == 2
    assert mf['streams'][STREAM_MIC]['device'] == 'Mic A'
    assert mf['streams'][STREAM_SYSTEM]['device'] == 'Speakers Loopback'
    assert mf['streams'][STREAM_MIC]['enabled'] is True
    assert mf['streams'][STREAM_SYSTEM]['enabled'] is True
    assert mf['streams'][STREAM_MIC]['bytes'] == 0
    assert len(mf['segments']) == 1
    assert mf['segments'][0]['index'] == 0
    assert mf['segments'][0]['end_ts'] is None
    assert mf['name'] is None
    assert mf['auto_name']  # non-empty

    # Файл реально на диску, валідний JSON
    path = tmp_path / 'rec_001' / MANIFEST_FILENAME
    assert path.is_file()
    with open(path, encoding='utf-8') as f:
        on_disk = json.load(f)
    assert on_disk == mf


def test_create_existing_session_raises(store: SessionStore):
    store.create('rec_dup', 48000, 2)
    with pytest.raises(SessionStoreError):
        store.create('rec_dup', 48000, 2)


def test_create_mic_only(store: SessionStore):
    mf = store.create('rec_mic', 48000, 2, mic_device_name='Mic A')
    assert mf['streams'][STREAM_MIC]['enabled'] is True
    assert mf['streams'][STREAM_SYSTEM]['enabled'] is False
    assert mf['streams'][STREAM_SYSTEM]['device'] is None


# ---------------------------------------------------------------- read / exists

def test_read_returns_manifest(store: SessionStore):
    store.create('rec_002', 48000, 2)
    mf = store.read('rec_002')
    assert mf['session_id'] == 'rec_002'


def test_read_missing_raises(store: SessionStore):
    with pytest.raises(SessionStoreError):
        store.read('nonexistent')


def test_exists(store: SessionStore):
    assert store.exists('rec_003') is False
    store.create('rec_003', 48000, 2)
    assert store.exists('rec_003') is True


# ---------------------------------------------------------------- mutations

def test_update_status(store: SessionStore):
    store.create('rec_s', 48000, 2)
    store.update_status('rec_s', STATUS_PAUSED)
    assert store.read('rec_s')['status'] == STATUS_PAUSED


def test_set_name(store: SessionStore):
    store.create('rec_n', 48000, 2)
    store.set_name('rec_n', 'Зустріч з командою')
    assert store.read('rec_n')['name'] == 'Зустріч з командою'


def test_update_stream_bytes(store: SessionStore):
    store.create('rec_b', 48000, 2)
    store.update_stream_bytes('rec_b', STREAM_MIC, 1024 * 100)
    store.update_stream_bytes('rec_b', STREAM_SYSTEM, 1024 * 200)
    mf = store.read('rec_b')
    assert mf['streams'][STREAM_MIC]['bytes'] == 102400
    assert mf['streams'][STREAM_SYSTEM]['bytes'] == 204800


def test_update_stream_bytes_invalid_stream(store: SessionStore):
    store.create('rec_e', 48000, 2)
    with pytest.raises(ValueError):
        store.update_stream_bytes('rec_e', 'bogus', 100)


def test_set_stream_error(store: SessionStore):
    store.create('rec_err', 48000, 2)
    store.set_stream_error('rec_err', STREAM_MIC, 'disconnected')
    assert store.read('rec_err')['streams'][STREAM_MIC]['error'] == 'disconnected'
    store.set_stream_error('rec_err', STREAM_MIC, None)
    assert store.read('rec_err')['streams'][STREAM_MIC]['error'] is None


def test_set_stream_params_persists_native_rate_channels(store: SessionStore):
    """Phase 9.10 fix: native (rate, channels) per-stream зберігаються
    в manifest. Це критично для finalize PCM→WAV з правильним header'ом
    коли девайс не підтримує запитуваний rate (моно-mic 88200Hz при
    requested 48000/2)."""
    store.create('rec_params', 48000, 2)
    # Спочатку обидва None
    mf = store.read('rec_params')
    assert mf['streams'][STREAM_MIC]['sample_rate'] is None
    assert mf['streams'][STREAM_MIC]['channels'] is None

    # Mic FOX: 88200 mono
    store.set_stream_params('rec_params', STREAM_MIC, sample_rate=88200, channels=1)
    # System loopback: 48000 stereo
    store.set_stream_params('rec_params', STREAM_SYSTEM, sample_rate=48000, channels=2)

    mf = store.read('rec_params')
    assert mf['streams'][STREAM_MIC]['sample_rate'] == 88200
    assert mf['streams'][STREAM_MIC]['channels'] == 1
    assert mf['streams'][STREAM_SYSTEM]['sample_rate'] == 48000
    assert mf['streams'][STREAM_SYSTEM]['channels'] == 2
    # Top-level залишається target — не зачіпається per-stream params
    assert mf['sample_rate'] == 48000
    assert mf['channels'] == 2


def test_set_stream_params_invalid_stream(store: SessionStore):
    store.create('rec_inv', 48000, 2)
    with pytest.raises(ValueError):
        store.set_stream_params('rec_inv', 'bogus', 48000, 2)


# ---------------------------------------------------------------- segments

def test_open_segment_closes_previous_and_starts_new(store: SessionStore):
    import time
    store.create('rec_seg', 48000, 2)
    store.update_stream_bytes('rec_seg', STREAM_MIC, 5000)
    store.update_stream_bytes('rec_seg', STREAM_SYSTEM, 5000)
    time.sleep(0.01)  # потрібна секундна різниця в end_ts
    new_idx = store.open_segment('rec_seg')

    mf = store.read('rec_seg')
    assert len(mf['segments']) == 2
    assert new_idx == 1
    # Перший segment закритий
    assert mf['segments'][0]['end_ts'] is not None
    assert mf['segments'][0]['duration_sec'] is not None
    # Другий segment відкритий, offset = total bytes на момент створення
    assert mf['segments'][1]['end_ts'] is None
    assert mf['segments'][1]['mic_offset_bytes'] == 5000
    assert mf['segments'][1]['system_offset_bytes'] == 5000


def test_close_segment(store: SessionStore):
    store.create('rec_close', 48000, 2)
    store.close_segment('rec_close')
    mf = store.read('rec_close')
    assert mf['segments'][0]['end_ts'] is not None
    assert mf['segments'][0]['duration_sec'] is not None


def test_close_segment_idempotent(store: SessionStore):
    store.create('rec_idem', 48000, 2)
    store.close_segment('rec_idem')
    first_end = store.read('rec_idem')['segments'][0]['end_ts']
    store.close_segment('rec_idem')  # повторно — нічого не міняє
    assert store.read('rec_idem')['segments'][0]['end_ts'] == first_end


# ---------------------------------------------------------------- listing

def test_list_all_returns_all_sessions(store: SessionStore):
    store.create('rec_a', 48000, 2)
    store.create('rec_b', 48000, 2)
    ids = {m['session_id'] for m in store.list_all()}
    assert ids == {'rec_a', 'rec_b'}


def test_list_all_skips_corrupt_manifest(store: SessionStore, tmp_path: Path):
    store.create('rec_ok', 48000, 2)
    # Створюємо battling manifest у сусідній папці
    bad_dir = tmp_path / 'rec_bad'
    bad_dir.mkdir()
    (bad_dir / MANIFEST_FILENAME).write_text('{not valid json', encoding='utf-8')

    listed = store.list_all()
    ids = {m['session_id'] for m in listed}
    assert ids == {'rec_ok'}  # bad пропущено без exception


def test_list_orphaned_finds_active_statuses(store: SessionStore):
    store.create('rec_rec', 48000, 2)  # default status = recording
    store.create('rec_pau', 48000, 2)
    store.update_status('rec_pau', STATUS_PAUSED)
    store.create('rec_stop', 48000, 2)
    store.update_status('rec_stop', STATUS_STOPPING)
    store.create('rec_fin', 48000, 2)
    store.update_status('rec_fin', STATUS_FINALIZED)

    orphans = {m['session_id'] for m in store.list_orphaned()}
    assert orphans == {'rec_rec', 'rec_pau', 'rec_stop'}
    assert all(m['status'] in ACTIVE_STATUSES for m in store.list_orphaned())


# ---------------------------------------------------------------- delete

def test_delete_removes_session(store: SessionStore, tmp_path: Path):
    store.create('rec_del', 48000, 2)
    assert (tmp_path / 'rec_del').is_dir()
    store.delete('rec_del')
    assert not (tmp_path / 'rec_del').exists()


def test_delete_nonexistent_is_idempotent(store: SessionStore):
    # Не повинно кидати (сесія не існує, але session_id валідний формат)
    store.delete('rec_never_existed')


# ---------------------------------------------------------------- atomic write

def test_atomic_write_keeps_manifest_valid_on_crash(
    store: SessionStore, tmp_path: Path, monkeypatch
):
    """Симулюємо крах посеред write: os.replace кидає OSError.
    Маніфест на диску мусить лишитися від попереднього успішного write.
    """
    import os
    store.create('rec_crash', 48000, 2)
    # Початковий valid manifest на диску
    pre_crash = store.read('rec_crash')
    assert pre_crash['status'] == STATUS_RECORDING

    # Mock os.replace щоб кинуло після написання tmp
    real_replace = os.replace
    calls = []

    def boom(src, dst):
        calls.append((src, dst))
        raise OSError("simulated crash")

    monkeypatch.setattr(os, 'replace', boom)
    with pytest.raises(OSError):
        store.update_status('rec_crash', STATUS_PAUSED)

    # Скасувати mock і перевірити що manifest на диску = pre_crash
    monkeypatch.setattr(os, 'replace', real_replace)
    after = store.read('rec_crash')
    assert after['status'] == STATUS_RECORDING  # не зламалось до paused
    assert after == pre_crash


def test_atomic_write_concurrent_modify(store: SessionStore):
    """Бомбардуємо update_stream_bytes з двох потоків — кожен інкремент
    має бути видимий, manifest завжди валідний."""
    store.create('rec_concurrent', 48000, 2)
    counts = {STREAM_MIC: 1000, STREAM_SYSTEM: 1000}

    def writer(stream: str):
        for i in range(counts[stream]):
            # Інкрементуємо total — звичайний use-case з flush
            store.update_stream_bytes('rec_concurrent', stream, (i + 1) * 4096)

    t1 = threading.Thread(target=writer, args=(STREAM_MIC,))
    t2 = threading.Thread(target=writer, args=(STREAM_SYSTEM,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    final = store.read('rec_concurrent')
    assert final['streams'][STREAM_MIC]['bytes'] == counts[STREAM_MIC] * 4096
    assert final['streams'][STREAM_SYSTEM]['bytes'] == counts[STREAM_SYSTEM] * 4096


# ---------------------------------------------------------------- pcm path

def test_pcm_path(store: SessionStore, tmp_path: Path):
    store.create('rec_p', 48000, 2)
    p_mic = store.pcm_path('rec_p', STREAM_MIC)
    p_sys = store.pcm_path('rec_p', STREAM_SYSTEM)
    assert p_mic == tmp_path / 'rec_p' / 'mic.pcm'
    assert p_sys == tmp_path / 'rec_p' / 'system.pcm'


def test_pcm_path_invalid_stream(store: SessionStore):
    store.create('rec_pi', 48000, 2)
    with pytest.raises(ValueError):
        store.pcm_path('rec_pi', 'unknown')


# ---------------------------------------------------------------- security

@pytest.mark.parametrize("malicious_id", [
    '../etc/passwd',
    '..',
    '../../whisper_history.db',
    'rec_../escape',
    'rec_/abs',
    'rec_\\x',
    'rec_a/b',
    'rec_..',
    '',
    'no_prefix',
    'REC_UPPERCASE',
    'rec_with-dash',
    'rec_with space',
])
def test_session_dir_rejects_path_traversal(store: SessionStore, malicious_id: str):
    """session_dir() мусить кидати SessionStoreError на будь-який
    session_id який не співпадає з ^rec_[a-z0-9_]{1,64}$."""
    with pytest.raises(SessionStoreError):
        store.session_dir(malicious_id)


def test_create_rejects_path_traversal(store: SessionStore):
    with pytest.raises(SessionStoreError):
        store.create('../escape', 48000, 2)


def test_read_rejects_path_traversal(store: SessionStore):
    with pytest.raises(SessionStoreError):
        store.read('../escape')


def test_pcm_path_rejects_path_traversal(store: SessionStore):
    with pytest.raises(SessionStoreError):
        store.pcm_path('rec_../escape', 'mic')


def test_read_manifest_warns_on_version_mismatch(store: SessionStore, caplog):
    """Якщо manifest має version != MANIFEST_VERSION — warning у лог,
    але manifest повертається (без exception)."""
    import logging
    store.create('rec_oldver', 48000, 2)
    # Симулюємо старий manifest на диску
    path = store.session_dir('rec_oldver') / 'manifest.json'
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['version'] = 999  # майбутня неіснуюча версія
    path.write_text(json.dumps(raw), encoding='utf-8')

    with caplog.at_level(logging.WARNING):
        manifest = store.read('rec_oldver')
    assert manifest['version'] == 999
    assert any('version=999' in r.message for r in caplog.records)


def test_session_id_accepts_valid_formats(store: SessionStore):
    """Всі формати які реально використовуються (uuid hex, prod-style,
    test-style з підкреслюваннями) приймаються."""
    valid_ids = [
        'rec_abc',
        'rec_001',
        'rec_with_underscores',
        'rec_a1b2c3d4e5f60718',  # prod-style uuid hex 16 chars
        'rec_a',
    ]
    for sid in valid_ids:
        # Не кидає — успіх
        path = store.session_dir(sid)
        assert path.parent == store.base_dir
        assert path.name == sid


# ---------------------------------------------------------------- self-healing paths (03.07.2026)

def _set_manifest_field(store: SessionStore, sid: str, **fields):
    path = store.session_dir(sid) / MANIFEST_FILENAME
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw.update(fields)
    path.write_text(json.dumps(raw), encoding='utf-8')


class TestSelfHealingPaths:
    def test_dead_final_mp3_path_repathed_when_file_exists_in_session_dir(
        self, store: SessionStore, tmp_path: Path,
    ):
        """Абсолютний шлях зі старого кореня проєкту мертвий, але файл з тим
        самим ім'ям реально лежить у папці цієї сесії (переїзд проєкту) —
        read() повертає вилікуваний шлях."""
        store.create('rec_moved', 48000, 2)
        session_dir = store.session_dir('rec_moved')
        real_mp3 = session_dir / 'final.mp3'
        real_mp3.write_bytes(b'audio bytes')
        _set_manifest_field(
            store, 'rec_moved',
            final_mp3_path=r'E:\Projects\Whisper\recordings\rec_moved\final.mp3',
        )

        manifest = store.read('rec_moved')
        assert manifest['final_mp3_path'] == str(real_mp3)

    def test_existing_path_left_untouched(self, store: SessionStore, tmp_path: Path):
        """Шлях, що реально існує (навіть якщо технічно поза session_dir),
        НЕ підміняється."""
        store.create('rec_intact', 48000, 2)
        real_file = tmp_path / 'elsewhere.mp3'
        real_file.write_bytes(b'x')
        _set_manifest_field(store, 'rec_intact', final_mp3_path=str(real_file))

        manifest = store.read('rec_intact')
        assert manifest['final_mp3_path'] == str(real_file)

    def test_unrecoverable_path_left_as_is(self, store: SessionStore):
        """Ні старий шлях, ні файл з тим самим basename у session_dir не
        існують — шлях лишається як був (нічим замінити)."""
        store.create('rec_orphan', 48000, 2)
        dead_path = r'E:\Projects\Whisper\recordings\rec_orphan\final.mp3'
        _set_manifest_field(store, 'rec_orphan', final_mp3_path=dead_path)

        manifest = store.read('rec_orphan')
        assert manifest['final_mp3_path'] == dead_path

    def test_primary_video_path_and_video_track_paths_repathed(
        self, store: SessionStore,
    ):
        store.create('rec_video', 48000, 2)
        session_dir = store.session_dir('rec_video')
        real_video = session_dir / 'cam_track1.mp4'
        real_video.write_bytes(b'video bytes')
        dead_video_path = r'E:\Projects\Whisper\recordings\rec_video\cam_track1.mp4'
        _set_manifest_field(
            store, 'rec_video',
            primary_video_path=dead_video_path,
            streams={
                'mic': {'device': None, 'bytes': 0, 'error': None},
                'system': {'device': None, 'bytes': 0, 'error': None},
                'video': [{'track_id': 'cam1', 'path': dead_video_path, 'status': 'finalized'}],
            },
        )

        manifest = store.read('rec_video')
        assert manifest['primary_video_path'] == str(real_video)
        assert manifest['streams']['video'][0]['path'] == str(real_video)

    def test_read_does_not_persist_healed_path_to_disk(self, store: SessionStore):
        """read() лікує лише повернутий dict; manifest.json на диску
        лишається незмінним (лікування — не мутація persist-стану)."""
        store.create('rec_readonly', 48000, 2)
        session_dir = store.session_dir('rec_readonly')
        real_mp3 = session_dir / 'final.mp3'
        real_mp3.write_bytes(b'x')
        dead_path = r'E:\Projects\Whisper\recordings\rec_readonly\final.mp3'
        _set_manifest_field(store, 'rec_readonly', final_mp3_path=dead_path)

        store.read('rec_readonly')

        on_disk = json.loads((session_dir / MANIFEST_FILENAME).read_text(encoding='utf-8'))
        assert on_disk['final_mp3_path'] == dead_path

    def test_healthy_manifest_no_debug_log(self, store: SessionStore, caplog):
        """Немає нічого лікувати → healed=0 → жодного DEBUG-запису про
        self-healing."""
        import logging
        store.create('rec_healthy', 48000, 2)
        with caplog.at_level(logging.DEBUG):
            store.read('rec_healthy')
        assert not any('self-healing' in r.message for r in caplog.records)
