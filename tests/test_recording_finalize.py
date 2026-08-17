"""Тести для app.services.recording.finalize (Phase 9.4)."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app.services.recording.finalize import (
    FinalizeError,
    finalize_session,
    mix_streams,
    pcm_to_wav,
)
from app.services.recording.session_store import (
    STATUS_FINALIZED,
    STREAM_MIC,
    STREAM_SYSTEM,
    SessionStore,
)


SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # int16


def _generate_sine_pcm(
    duration_sec: float,
    freq_hz: float = 440.0,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    amplitude: float = 0.3,
) -> bytes:
    """Генерує синусоїду в int16 LE PCM. Працює і для моно, і для stereo."""
    n = int(duration_sec * sample_rate)
    samples = []
    for i in range(n):
        t = i / sample_rate
        val = int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * t))
        for _ in range(channels):
            samples.append(val)
    return struct.pack(f'<{len(samples)}h', *samples)


# ---------------------------------------------------------------- pcm_to_wav

def test_pcm_to_wav_creates_valid_wav(tmp_path: Path):
    pcm = tmp_path / 'test.pcm'
    wav = tmp_path / 'test.wav'
    pcm.write_bytes(_generate_sine_pcm(0.5))  # 0.5 сек

    frames = pcm_to_wav(pcm, wav, SAMPLE_RATE, CHANNELS)
    assert frames == int(0.5 * SAMPLE_RATE)
    assert wav.is_file()

    with wave.open(str(wav), 'rb') as wf:
        assert wf.getnchannels() == CHANNELS
        assert wf.getsampwidth() == SAMPLE_WIDTH
        assert wf.getframerate() == SAMPLE_RATE
        assert wf.getnframes() == frames


def test_pcm_to_wav_missing_pcm_creates_empty_wav(tmp_path: Path):
    wav = tmp_path / 'empty.wav'
    frames = pcm_to_wav(
        pcm_path=tmp_path / 'nonexistent.pcm',
        wav_path=wav,
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )
    assert frames == 0
    assert wav.is_file()
    with wave.open(str(wav), 'rb') as wf:
        assert wf.getnframes() == 0


def test_pcm_to_wav_truncates_partial_frame(tmp_path: Path):
    """Якщо PCM довжина не кратна frame_size — хвіст ігнорується."""
    pcm = tmp_path / 'partial.pcm'
    wav = tmp_path / 'partial.wav'
    # 4 frames + 1 байт зайвий
    pcm.write_bytes(_generate_sine_pcm(0.0001) + b'\xFF')
    frames = pcm_to_wav(pcm, wav, SAMPLE_RATE, CHANNELS)
    # Перевірка що результат валідний WAV з усіченими байтами
    with wave.open(str(wav), 'rb') as wf:
        assert wf.getnframes() == frames
        # Розмір файлу = 44-байтний хедер + frames * frame_size
        expected_data = frames * CHANNELS * SAMPLE_WIDTH
        assert wf.getnframes() * CHANNELS * SAMPLE_WIDTH == expected_data


def test_pcm_to_wav_streaming_large_file(tmp_path: Path):
    """Перевіряємо що великий PCM (5 MB) не падає та коректно записується."""
    pcm = tmp_path / 'big.pcm'
    wav = tmp_path / 'big.wav'
    big_data = _generate_sine_pcm(0.5)  # ~94KB
    pcm.write_bytes(big_data * 50)  # ~4.7 MB

    frames = pcm_to_wav(pcm, wav, SAMPLE_RATE, CHANNELS, chunk_size=64 * 1024)
    expected_frames = len(big_data * 50) // (CHANNELS * SAMPLE_WIDTH)
    assert frames == expected_frames


# ---------------------------------------------------------------- mix_streams

@pytest.fixture
def sine_wavs(tmp_path: Path) -> tuple[Path, Path]:
    """Створює два WAV: 440Hz mic + 880Hz system, по 1 секунді."""
    mic_pcm = tmp_path / 'mic.pcm'
    sys_pcm = tmp_path / 'system.pcm'
    mic_pcm.write_bytes(_generate_sine_pcm(1.0, freq_hz=440))
    sys_pcm.write_bytes(_generate_sine_pcm(1.0, freq_hz=880))

    mic_wav = tmp_path / 'mic.wav'
    sys_wav = tmp_path / 'system.wav'
    pcm_to_wav(mic_pcm, mic_wav, SAMPLE_RATE, CHANNELS)
    pcm_to_wav(sys_pcm, sys_wav, SAMPLE_RATE, CHANNELS)
    return mic_wav, sys_wav


def test_mix_two_wavs_to_mp3(sine_wavs: tuple[Path, Path], tmp_path: Path):
    mic_wav, sys_wav = sine_wavs
    out = tmp_path / 'mixed.mp3'
    result = mix_streams(mic_wav, sys_wav, out, bitrate='128k')
    assert result == out
    assert out.is_file()
    assert out.stat().st_size > 1000  # MP3 має бути ненульовим


def test_mix_only_mic(sine_wavs: tuple[Path, Path], tmp_path: Path):
    mic_wav, _ = sine_wavs
    out = tmp_path / 'mic_only.mp3'
    mix_streams(mic_wav, None, out, bitrate='128k')
    assert out.is_file()


def test_mix_only_system(sine_wavs: tuple[Path, Path], tmp_path: Path):
    _, sys_wav = sine_wavs
    out = tmp_path / 'sys_only.mp3'
    mix_streams(None, sys_wav, out, bitrate='128k')
    assert out.is_file()


def test_mix_both_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        mix_streams(None, None, tmp_path / 'x.mp3')


def test_mix_with_gain(sine_wavs: tuple[Path, Path], tmp_path: Path):
    mic_wav, sys_wav = sine_wavs
    out = tmp_path / 'gain.mp3'
    mix_streams(mic_wav, sys_wav, out, mic_gain_db=6.0, system_gain_db=-3.0,
                bitrate='128k')
    assert out.is_file()


# ---------------------------------------------------------------- finalize_session

@pytest.fixture
def store_with_session(tmp_path: Path):
    """Створює SessionStore + 1 сесію з реальним PCM на диску."""
    store = SessionStore(tmp_path / 'sessions')
    sid = 'rec_test_finalize'
    store.create(sid, sample_rate=SAMPLE_RATE, channels=CHANNELS,
                 mic_device_name='TestMic', system_device_name='TestSys')

    mic_pcm_path = store.pcm_path(sid, STREAM_MIC)
    sys_pcm_path = store.pcm_path(sid, STREAM_SYSTEM)
    mic_pcm_path.write_bytes(_generate_sine_pcm(0.5, freq_hz=440))
    sys_pcm_path.write_bytes(_generate_sine_pcm(0.5, freq_hz=880))
    store.update_stream_bytes(sid, STREAM_MIC, mic_pcm_path.stat().st_size)
    store.update_stream_bytes(sid, STREAM_SYSTEM, sys_pcm_path.stat().st_size)
    return store, sid


def test_finalize_session_creates_mp3_and_marks_finalized(store_with_session):
    store, sid = store_with_session
    result = finalize_session(sid, store, bitrate='128k')

    assert result['status'] == STATUS_FINALIZED
    assert result['final_mp3_path'] is not None
    assert Path(result['final_mp3_path']).is_file()
    assert result['total_duration_sec'] >= 0.4  # ~0.5

    mf = store.read(sid)
    assert mf['status'] == STATUS_FINALIZED
    assert 'final_mp3_path' in mf
    assert 'streams_finalized' in mf
    assert STREAM_MIC in mf['streams_finalized']
    assert STREAM_SYSTEM in mf['streams_finalized']


def test_finalize_session_removes_pcm_by_default(store_with_session):
    store, sid = store_with_session
    finalize_session(sid, store, bitrate='128k', keep_pcm=False)
    assert not store.pcm_path(sid, STREAM_MIC).exists()
    assert not store.pcm_path(sid, STREAM_SYSTEM).exists()


def test_finalize_session_keeps_pcm_when_flag_set(store_with_session):
    store, sid = store_with_session
    finalize_session(sid, store, bitrate='128k', keep_pcm=True)
    assert store.pcm_path(sid, STREAM_MIC).is_file()
    assert store.pcm_path(sid, STREAM_SYSTEM).is_file()


def test_finalize_session_removes_wav_by_default(store_with_session):
    """Проміжні mic.wav/system.wav видаляються після final.mp3 (інакше
    накопичуються ~1 ГБ/сесію)."""
    store, sid = store_with_session
    result = finalize_session(sid, store, bitrate='128k')
    session_dir = store.session_dir(sid)
    assert Path(result['final_mp3_path']).is_file()      # mp3 лишається
    assert not (session_dir / 'mic.wav').exists()
    assert not (session_dir / 'system.wav').exists()


def test_finalize_session_keeps_wav_when_flag_set(store_with_session):
    store, sid = store_with_session
    finalize_session(sid, store, bitrate='128k', keep_wav=True)
    session_dir = store.session_dir(sid)
    assert (session_dir / 'mic.wav').is_file()
    assert (session_dir / 'system.wav').is_file()


def test_finalize_session_mic_only(tmp_path: Path):
    store = SessionStore(tmp_path / 'sessions')
    sid = 'rec_mic_only'
    store.create(sid, SAMPLE_RATE, CHANNELS, mic_device_name='Mic')
    mic_pcm = store.pcm_path(sid, STREAM_MIC)
    mic_pcm.write_bytes(_generate_sine_pcm(0.3))
    store.update_stream_bytes(sid, STREAM_MIC, mic_pcm.stat().st_size)

    result = finalize_session(sid, store, bitrate='128k')
    assert result['status'] == STATUS_FINALIZED
    assert Path(result['final_mp3_path']).is_file()
    # system stream відсутній — тільки mic у streams_finalized
    assert STREAM_MIC in result['streams']
    assert STREAM_SYSTEM not in result['streams']


def test_finalize_session_empty_streams_marks_finalized_with_no_mp3(tmp_path: Path):
    store = SessionStore(tmp_path / 'sessions')
    sid = 'rec_empty'
    store.create(sid, SAMPLE_RATE, CHANNELS, mic_device_name='Mic',
                 system_device_name='Sys')
    # Нічого не пишемо в .pcm — обидва порожні

    result = finalize_session(sid, store, bitrate='128k')
    assert result['status'] == STATUS_FINALIZED
    assert result['final_mp3_path'] is None
    assert result['total_duration_sec'] == 0.0


def test_finalize_session_missing_manifest_raises(tmp_path: Path):
    store = SessionStore(tmp_path / 'sessions')
    with pytest.raises(FinalizeError):
        finalize_session('rec_does_not_exist', store)


def test_finalize_uses_per_stream_native_rate(tmp_path: Path):
    """Phase 9.10 fix: якщо stream має native rate ≠ top-level
    (mic FOX 88200/1 vs target 48000/2), pcm_to_wav повинен
    використовувати per-stream rate. Це гарантує що WAV-хедер
    співпадає з реальним PCM payload'ом."""
    store = SessionStore(tmp_path / 'sessions')
    sid = 'rec_ps_test'
    store.create(sid, sample_rate=48000, channels=2,
                 mic_device_name='FOX', system_device_name='Loopback')
    # Симулюємо реальний сценарій: mic native = 88200/1, system = 48000/2
    store.set_stream_params(sid, STREAM_MIC, sample_rate=88200, channels=1)
    store.set_stream_params(sid, STREAM_SYSTEM, sample_rate=48000, channels=2)

    # Mic PCM = 1 секунда mono 88200Hz int16 = 88200 * 2 = 176400 bytes
    mic_pcm = store.pcm_path(sid, STREAM_MIC)
    mic_pcm.write_bytes(_generate_sine_pcm(1.0, freq_hz=440, sample_rate=88200, channels=1))

    # System PCM = 1 секунда stereo 48000Hz int16 = 48000 * 4 = 192000 bytes
    sys_pcm = store.pcm_path(sid, STREAM_SYSTEM)
    sys_pcm.write_bytes(_generate_sine_pcm(1.0, freq_hz=880, sample_rate=48000, channels=2))

    store.update_stream_bytes(sid, STREAM_MIC, mic_pcm.stat().st_size)
    store.update_stream_bytes(sid, STREAM_SYSTEM, sys_pcm.stat().st_size)

    # keep_wav=True — тест читає mic.wav/system.wav назад для перевірки хедера
    result = finalize_session(sid, store, bitrate='128k', keep_wav=True)

    # Verify mic.wav has correct 88200/1 header (НЕ 48000/2 як top-level)
    import wave
    mic_wav_path = result['streams'][STREAM_MIC]['wav_path']
    with wave.open(mic_wav_path, 'rb') as wf:
        assert wf.getnchannels() == 1, "mic.wav має бути mono"
        assert wf.getframerate() == 88200, "mic.wav має native 88200Hz, не 48000"
        assert abs(wf.getnframes() / wf.getframerate() - 1.0) < 0.01, \
            "duration ~ 1.0s"

    # System.wav має 48000/2
    sys_wav_path = result['streams'][STREAM_SYSTEM]['wav_path']
    with wave.open(sys_wav_path, 'rb') as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 48000

    # streams info містить per-stream rate/channels
    assert result['streams'][STREAM_MIC]['sample_rate'] == 88200
    assert result['streams'][STREAM_MIC]['channels'] == 1
    assert result['streams'][STREAM_SYSTEM]['sample_rate'] == 48000
    assert result['streams'][STREAM_SYSTEM]['channels'] == 2


def test_finalize_falls_back_to_top_level_for_legacy_manifest(tmp_path: Path):
    """Backward compat: старі manifest'и без per-stream sample_rate/channels
    повинні фінілізуватись з top-level params."""
    store = SessionStore(tmp_path / 'sessions')
    sid = 'rec_legacy'
    store.create(sid, sample_rate=SAMPLE_RATE, channels=CHANNELS,
                 mic_device_name='Old')
    # Симулюємо legacy manifest: видаляємо per-stream params з диска
    def _strip(mf):
        mf['streams'][STREAM_MIC].pop('sample_rate', None)
        mf['streams'][STREAM_MIC].pop('channels', None)
    store.modify(sid, _strip)

    mic_pcm = store.pcm_path(sid, STREAM_MIC)
    mic_pcm.write_bytes(_generate_sine_pcm(0.5))

    # Не має падати
    result = finalize_session(sid, store, bitrate='128k')
    assert result['status'] == STATUS_FINALIZED
    # Fallback на top-level (48000/2)
    assert result['streams'][STREAM_MIC]['sample_rate'] == SAMPLE_RATE
    assert result['streams'][STREAM_MIC]['channels'] == CHANNELS


# ------------------------------------------------- ffmpeg-гілка mix'у (великі файли)

def _rms_db(path: Path) -> float:
    """RMS у дБ через pydub — спільна лінійка для обох гілок mix'у."""
    from pydub import AudioSegment
    seg = AudioSegment.from_file(str(path))
    samples = seg.get_array_of_samples()
    if not len(samples):
        return -99.0
    acc = sum(float(s) ** 2 for s in samples) / len(samples)
    return 20 * math.log10(math.sqrt(acc) / 32768) if acc > 0 else -99.0


def _write_sine_wav(path: Path, freq: float, rate: int, channels: int,
                    duration_sec: float = 1.0) -> None:
    pcm = _generate_sine_pcm(duration_sec, freq, rate, channels)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(rate)
        wf.writeframes(pcm)


@pytest.mark.parametrize('mic_rate,mic_ch,sys_rate,sys_ch', [
    (44100, 1, 48000, 2),   # реальна конфігурація: моно-мікрофон + stereo loopback
    (48000, 2, 48000, 2),
    (44100, 1, 44100, 1),
])
def test_ffmpeg_mix_matches_pydub_levels(tmp_path: Path, mic_rate, mic_ch,
                                         sys_rate, sys_ch):
    """ffmpeg-гілка мусить звучати ІДЕНТИЧНО pydub-гілці.

    Регресія на дві тихі пастки, які видно лише вимірюванням:
    - ``amix`` без ``normalize=0`` ділив би кожен вхід на N (−6 дБ);
    - ``aformat=channel_layouts=stereo`` на МОНО-вході тихішає на 3 дБ
      (rematrix-нормалізація swresample), тоді як pydub дублює канал 1:1.
      Б'є саме по mic-доріжці — найтихішій і найважливішій.
    """
    from app.services.recording.finalize import _mix_via_ffmpeg

    mic = tmp_path / 'mic.wav'
    sysw = tmp_path / 'system.wav'
    _write_sine_wav(mic, 440.0, mic_rate, mic_ch)
    _write_sine_wav(sysw, 880.0, sys_rate, sys_ch)

    via_ffmpeg = tmp_path / 'ff.mp3'
    _mix_via_ffmpeg(mic, sysw, via_ffmpeg, '128k', 0.0, 0.0)
    via_pydub = tmp_path / 'py.mp3'          # малі файли → pydub-гілка
    mix_streams(mic, sysw, via_pydub, bitrate='128k')

    assert abs(_rms_db(via_ffmpeg) - _rms_db(via_pydub)) < 0.2


def test_large_input_takes_ffmpeg_branch(tmp_path: Path, monkeypatch):
    """Поріг реально перемикає гілку — інакше багатогодинна сесія
    підняла б гігабайти в RAM просто на старті застосунку."""
    from app.services.recording import finalize as finalize_mod

    mic = tmp_path / 'mic.wav'
    _write_sine_wav(mic, 440.0, SAMPLE_RATE, CHANNELS)
    monkeypatch.setattr(finalize_mod, 'FFMPEG_MIX_THRESHOLD_BYTES', 1)

    called: list[str] = []
    real = finalize_mod._mix_via_ffmpeg

    def spy(*a, **kw):
        called.append('ffmpeg')
        return real(*a, **kw)

    monkeypatch.setattr(finalize_mod, '_mix_via_ffmpeg', spy)
    out = tmp_path / 'big.mp3'
    finalize_mod.mix_streams(mic, None, out, bitrate='128k')
    assert called == ['ffmpeg']
    assert out.is_file() and out.stat().st_size > 0


def test_ffmpeg_mix_failure_falls_back_to_pydub(tmp_path: Path, monkeypatch):
    """Збій ffmpeg не має валити finalize — відкіт на pydub."""
    from app.services.recording import finalize as finalize_mod

    mic = tmp_path / 'mic.wav'
    _write_sine_wav(mic, 440.0, SAMPLE_RATE, CHANNELS)
    monkeypatch.setattr(finalize_mod, 'FFMPEG_MIX_THRESHOLD_BYTES', 1)
    monkeypatch.setattr(
        finalize_mod, '_mix_via_ffmpeg',
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('ffmpeg впав')),
    )
    out = tmp_path / 'fallback.mp3'
    finalize_mod.mix_streams(mic, None, out, bitrate='128k')
    assert out.is_file() and out.stat().st_size > 0
