"""Finalize pipeline (Phase 9.4): PCM → WAV → mix → MP3.

Останній етап recording-сесії: перетворення raw PCM-стрімів у єдиний
аудіо-файл, готовий до транскрипції та зберігання в Audio Library.

Pipeline (для двопотокової сесії):

.. code-block:: text

    mic.pcm    ──pcm_to_wav──→ mic.wav    ──┐
                                              ├──pydub overlay──→ final.mp3
    system.pcm ──pcm_to_wav──→ system.wav ──┘

Для однопотокового запису (тільки mic або тільки system) — pipeline
коротший, без overlay, просто WAV → MP3.

Pause/resume: при pause writer flush+close, при resume — новий segment
з offset = total_bytes. У файл нічого не пишеться під час паузи, тому
PCM на диску не містить тишини. Cut'ити segments не треба — байти
вже без розривів.

Чому stdlib ``wave`` для PCM→WAV:
- Не потребує pydub/ffmpeg для базового перетворення.
- Швидкий: просто додає 44-байтовий RIFF-хедер до існуючих PCM-байт.
- Streaming-friendly: ``writeframes`` пише chunk'ами, не вантажить
  усе в RAM.

Чому pydub для mix→MP3:
- Native overlay підтримує гучності, fade'и (на майбутнє).
- MP3 export через ffmpeg (вже є в проєкті).
- Звичайний interface: ``AudioSegment.overlay()``.
"""
from __future__ import annotations

import logging
import os
import shutil
import wave
from pathlib import Path
from typing import Optional

from app.utils.proc import NO_WINDOW

from app.services.recording.session_store import (
    STATUS_FINALIZED,
    STREAM_MIC,
    STREAM_SYSTEM,
    SessionStore,
    SessionStoreError,
)


logger = logging.getLogger(__name__)


# Поріг, з якого mix іде через ffmpeg (константна пам'ять) замість pydub
# (тримає весь декодований WAV у RAM). 400 МБ ≈ 40 хв стерео 48кГц — усе, що
# коротше, pydub переживає спокійно; усе, що довше, б'є по пам'яті процесу.
FFMPEG_MIX_THRESHOLD_BYTES = 400 * 1024 * 1024

# Стеля на один ffmpeg-mix. Реально 70-хв запис зводиться за ~27с, але
# багатогодинна сесія на холодному диску може тягнутись — беремо із запасом.
_FFMPEG_MIX_TIMEOUT_SEC = 3600


# ---------------------------------------------------------------- pcm → wav

def pcm_to_wav(
    pcm_path: Path | str,
    wav_path: Path | str,
    sample_rate: int,
    channels: int,
    sample_width_bytes: int = 2,
    chunk_size: int = 1024 * 1024,
) -> int:
    """Конвертує raw PCM у WAV з RIFF-хедером.

    Робить streaming-write: читає по ``chunk_size`` байт і пише через
    ``wave.writeframes``. Не вантажить увесь файл у RAM, тому годиться
    для багатогодинних записів.

    Повертає кількість записаних audio frames.

    Особливості:
    - Якщо PCM-файл порожній або відсутній — створює пустий WAV (0 frames)
      і повертає 0. Це валідний WAV, можна відкрити.
    - Якщо розмір PCM не кратний (channels * sample_width_bytes) —
      хвостові байти ігноруються (округлення до повного семплу).
    """
    pcm_path = Path(pcm_path)
    wav_path = Path(wav_path)
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    frame_size = channels * sample_width_bytes
    total_frames = 0

    with wave.open(str(wav_path), 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width_bytes)
        wf.setframerate(sample_rate)

        if not pcm_path.is_file():
            logger.warning("PCM не знайдено: %s — створено пустий WAV", pcm_path)
            return 0

        with open(pcm_path, 'rb') as pf:
            while True:
                chunk = pf.read(chunk_size)
                if not chunk:
                    break
                # Округлюємо chunk вниз до повного семплу
                usable = (len(chunk) // frame_size) * frame_size
                if usable < len(chunk):
                    logger.debug(
                        "Хвіст %d байт у %s округлено вниз до %d",
                        len(chunk) - usable, pcm_path, usable,
                    )
                if usable > 0:
                    wf.writeframes(chunk[:usable])
                    total_frames += usable // frame_size

    return total_frames


# ---------------------------------------------------------------- mix → mp3

def mix_streams(
    mic_wav: Optional[Path | str],
    system_wav: Optional[Path | str],
    output_mp3: Path | str,
    bitrate: str = '192k',
    mic_gain_db: float = 0.0,
    system_gain_db: float = 0.0,
) -> Path:
    """Зводить mic + system у MP3.

    Логіка:
    - Великі доріжки (сумарно > ``FFMPEG_MIX_THRESHOLD_BYTES``) → стрімовий
      mix через ffmpeg, БЕЗ завантаження в RAM (див. :func:`_mix_via_ffmpeg`)
    - Інакше обидва WAV → overlay через pydub → MP3
    - Тільки mic або тільки system (інший = None або пустий) → одразу
      WAV → MP3 без overlay'у
    - Обидва відсутні → :class:`FileNotFoundError`

    Gain в дБ: 0 = native, +6 ≈ x2 amplitude, -6 ≈ /2.

    Повертає шлях до згенерованого MP3.
    """
    from pydub import AudioSegment  # lazy import — pydub тягне ffmpeg при імпорті

    output_mp3 = Path(output_mp3)
    output_mp3.parent.mkdir(parents=True, exist_ok=True)

    # pydub тримає ВЕСЬ декодований WAV у пам'яті (і ще раз стільки ж на час
    # overlay'у). Для багатогодинної сесії це гігабайти: 10-годинний запис =
    # 3.3 ГБ mic + 0.9 ГБ system. Це не теорія — саме такий запис ліг у
    # crashed 21.07.2026, а boot-recovery тепер намагається дофіналізувати
    # такі сесії автоматично, тобто ризик OOM переїхав би на СТАРТ застосунку.
    # ffmpeg робить те саме стрімом, з константною пам'яттю.
    _inputs = [p for p in (mic_wav, system_wav) if p and Path(p).is_file()]
    _total = sum(Path(p).stat().st_size for p in _inputs)
    if _total > FFMPEG_MIX_THRESHOLD_BYTES:
        logger.info(
            "mix_streams: %.2f ГБ вхідних даних — мікшую через ffmpeg (не pydub)",
            _total / 1e9,
        )
        try:
            return _mix_via_ffmpeg(
                mic_wav=mic_wav if mic_wav in _inputs else None,
                system_wav=system_wav if system_wav in _inputs else None,
                output_mp3=output_mp3,
                bitrate=bitrate,
                mic_gain_db=mic_gain_db,
                system_gain_db=system_gain_db,
            )
        except Exception as e:
            # Не фатально: падаємо назад у pydub. Гірше по пам'яті, але це
            # рівно та поведінка, що була до цієї оптимізації.
            logger.warning(
                "ffmpeg-mix не вдався (%s) — відкочуюсь на pydub, "
                "можливий великий сплеск пам'яті", e,
            )

    mic_seg = _maybe_load_wav(mic_wav, mic_gain_db)
    sys_seg = _maybe_load_wav(system_wav, system_gain_db)

    if mic_seg is None and sys_seg is None:
        raise FileNotFoundError(
            f"Жодного валідного WAV для зведення (mic={mic_wav}, system={system_wav})"
        )

    if mic_seg is not None and sys_seg is not None:
        # Вирівнюємо довжину: коротший буде накладено в позицію 0,
        # довший залишиться як є.
        if len(mic_seg) >= len(sys_seg):
            combined = mic_seg.overlay(sys_seg)
        else:
            combined = sys_seg.overlay(mic_seg)
    else:
        combined = mic_seg if mic_seg is not None else sys_seg

    combined.export(str(output_mp3), format='mp3', bitrate=bitrate)
    return output_mp3


def _mix_via_ffmpeg(
    mic_wav: Optional[Path | str],
    system_wav: Optional[Path | str],
    output_mp3: Path,
    bitrate: str,
    mic_gain_db: float,
    system_gain_db: float,
) -> Path:
    """Стрімовий mix через ffmpeg — константна пам'ять на будь-якій довжині.

    Семантика 1:1 з pydub-гілкою:
    - ``amix=normalize=0`` — проста сума семплів, як ``AudioSegment.overlay``
      (з ``normalize=1``, дефолтом ffmpeg, кожен вхід ділився б на N → тихіше).
    - ``duration=longest`` — коротший вхід доповнюється тишею, довший лишається
      цілим (pydub кладе коротший у позицію 0 довшого).
    - target rate/channels = максимум по входах — так само, як ``_sync`` у pydub.
    - gain в дБ через ``volume``.

    Кидає виняток при будь-якій проблемі — викликач відкочується на pydub.
    """
    import subprocess

    from pydub import AudioSegment  # той самий резолвер ffmpeg, що й у pydub

    ffmpeg_bin = getattr(AudioSegment, 'converter', None) or shutil.which('ffmpeg')
    if not ffmpeg_bin:
        raise FinalizeError('ffmpeg не знайдено')

    inputs: list[tuple[Path, float]] = []
    for path, gain in ((mic_wav, mic_gain_db), (system_wav, system_gain_db)):
        if path is None:
            continue
        p = Path(path)
        if p.is_file() and p.stat().st_size > 0:
            inputs.append((p, gain))
    if not inputs:
        raise FinalizeError('Немає вхідних WAV для ffmpeg-mix')

    # Читаємо параметри з RIFF-хедера (wave не вантажить аудіо-дані).
    params: list[tuple[int, int]] = []
    for p, _ in inputs:
        with wave.open(str(p), 'rb') as wf:
            params.append((wf.getframerate(), wf.getnchannels()))
    target_rate = max(r for r, _ in params)
    target_ch = max(c for _, c in params)
    layout = 'stereo' if target_ch >= 2 else 'mono'

    cmd = [str(ffmpeg_bin), '-y', '-hide_banner', '-loglevel', 'error']
    for p, _ in inputs:
        cmd += ['-i', str(p)]

    parts, labels = [], []
    for idx, (_, gain) in enumerate(inputs):
        src_ch = params[idx][1]
        if src_ch == 1 and target_ch >= 2:
            # НЕ aformat: при mono→stereo rematrix у swresample нормалізує
            # мікс і тихішає на 3 дБ, тоді як pydub set_channels() просто
            # дублює канал. Для нас це не косметика — mic-доріжка моно і вже
            # найтихіша (мікрофон проти loopback'у), зайві -3 дБ б'ють саме
            # по голосу власника і по якості його транскрипції. pan копіює
            # канал один-в-один, як pydub.
            chain = f'aresample={target_rate},pan={layout}|c0=c0|c1=c0'
        else:
            chain = f'aformat=sample_rates={target_rate}:channel_layouts={layout}'
        if gain:
            chain = f'volume={gain}dB,' + chain
        parts.append(f'[{idx}:a]{chain}[a{idx}]')
        labels.append(f'[a{idx}]')

    if len(inputs) == 1:
        filter_complex = f'{parts[0]};{labels[0]}anull[out]'
    else:
        filter_complex = (
            ';'.join(parts)
            + ';' + ''.join(labels)
            + f'amix=inputs={len(inputs)}:duration=longest:normalize=0[out]'
        )

    cmd += [
        '-filter_complex', filter_complex,
        '-map', '[out]',
        '-b:a', bitrate,
        str(output_mp3),
    ]

    result = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_MIX_TIMEOUT_SEC,
                            creationflags=NO_WINDOW)
    if result.returncode != 0:
        raise FinalizeError(
            f"ffmpeg rc={result.returncode}: "
            f"{result.stderr.decode('utf-8', errors='replace')[-400:]}"
        )
    if not output_mp3.is_file() or output_mp3.stat().st_size == 0:
        raise FinalizeError('ffmpeg відпрацював, але MP3 порожній')
    return output_mp3


def _maybe_load_wav(path: Optional[Path | str], gain_db: float):
    """Повертає AudioSegment або None якщо файл відсутній/порожній."""
    if path is None:
        return None
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return None
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_wav(str(p))
    except Exception as e:
        logger.warning("Не вдалось завантажити %s: %s", p, e)
        return None
    if seg.duration_seconds < 0.05:
        logger.debug("WAV %s занадто короткий (%.3fs) — ігноруємо",
                     p, seg.duration_seconds)
        return None
    if gain_db != 0.0:
        seg = seg.apply_gain(gain_db)
    return seg


# ---------------------------------------------------------------- session finalize

class FinalizeError(RuntimeError):
    """Помилка фіналізації."""


def finalize_session(
    session_id: str,
    store: SessionStore,
    bitrate: str = '192k',
    keep_pcm: bool = False,
    keep_wav: bool = False,
    mic_gain_db: float = 0.0,
    system_gain_db: float = 0.0,
) -> dict:
    """Повний pipeline для сесії: PCM → WAV → MP3 → manifest update.

    Кроки:
    1. Зчитати manifest (мусить існувати).
    2. Для кожного активного потоку (mic/system) — pcm_to_wav.
    3. mix_streams → final.mp3.
    4. Оновити manifest: status=finalized, final_mp3_path, total_duration.
    5. Якщо keep_pcm=False — видалити .pcm файли.
    6. Якщо keep_wav=False — видалити проміжні mic.wav/system.wav (їх нічого не
       читає після finalize; інакше накопичуються ~1 ГБ/сесію).

    Повертає словник із підсумком: paths, durations, status.

    Кидає :class:`FinalizeError` при крах'у на будь-якому кроці.
    Manifest НЕ оновлюється до finalize в такому випадку — статус
    залишається попереднім (stopping/crashed), recovery-flow зможе
    повторити спробу.
    """
    try:
        manifest = store.read(session_id)
    except SessionStoreError as e:
        raise FinalizeError(f"Manifest відсутній: {e}") from e

    session_dir = store.session_dir(session_id)
    # Top-level залишається як target для mix (фінальний MP3),
    # але для PCM→WAV беремо per-stream native параметри
    # (можуть відрізнятись: моно-mic 88200/1 vs stereo loopback 48000/2).
    target_sample_rate = int(manifest['sample_rate'])
    target_channels = int(manifest['channels'])
    bytes_per_sample = int(manifest.get('bytes_per_sample', 2))

    streams_info: dict[str, dict] = {}
    wav_paths: dict[str, Path] = {}

    for stream_name in (STREAM_MIC, STREAM_SYSTEM):
        stream_meta = manifest['streams'][stream_name]
        if not stream_meta.get('enabled'):
            continue
        # Per-stream native params (Phase 9.10 fix). Fallback на top-level
        # для backward compat зі старими manifest'ами без цих полів.
        stream_rate = stream_meta.get('sample_rate') or target_sample_rate
        stream_channels = stream_meta.get('channels') or target_channels
        pcm_path = store.pcm_path(session_id, stream_name)
        wav_path = session_dir / f'{stream_name}.wav'
        try:
            frames = pcm_to_wav(
                pcm_path=pcm_path,
                wav_path=wav_path,
                sample_rate=stream_rate,
                channels=stream_channels,
                sample_width_bytes=bytes_per_sample,
            )
        except Exception as e:
            raise FinalizeError(
                f"pcm_to_wav для {stream_name} зламався: {e}"
            ) from e

        duration = frames / stream_rate if stream_rate > 0 else 0.0
        streams_info[stream_name] = {
            'wav_path': str(wav_path),
            'frames': frames,
            'sample_rate': stream_rate,
            'channels': stream_channels,
            'duration_sec': round(duration, 3),
        }
        if frames > 0:
            wav_paths[stream_name] = wav_path
        else:
            logger.info("Stream %s порожній — пропускаємо у mix'і", stream_name)

    if not wav_paths:
        # Немає жодних даних. Manifest позначаємо finalized з пустим mp3.
        store.modify(session_id, lambda m: m.update({
            'status': STATUS_FINALIZED,
            'final_mp3_path': None,
            'total_duration_sec': 0.0,
            'streams_finalized': streams_info,
        }))
        return {
            'session_id': session_id,
            'status': STATUS_FINALIZED,
            'final_mp3_path': None,
            'total_duration_sec': 0.0,
            'streams': streams_info,
        }

    # Mix → MP3
    mp3_path = session_dir / 'final.mp3'
    try:
        mix_streams(
            mic_wav=wav_paths.get(STREAM_MIC),
            system_wav=wav_paths.get(STREAM_SYSTEM),
            output_mp3=mp3_path,
            bitrate=bitrate,
            mic_gain_db=mic_gain_db,
            system_gain_db=system_gain_db,
        )
    except Exception as e:
        raise FinalizeError(f"mix_streams зламався: {e}") from e

    total_duration = max(
        (info.get('duration_sec', 0.0) for info in streams_info.values()),
        default=0.0,
    )

    # Update manifest
    def _apply(mf: dict) -> None:
        mf['status'] = STATUS_FINALIZED
        mf['final_mp3_path'] = str(mp3_path)
        mf['total_duration_sec'] = round(total_duration, 3)
        mf['streams_finalized'] = streams_info
    store.modify(session_id, _apply)

    # Cleanup raw PCM (опц.)
    if not keep_pcm:
        for stream_name in (STREAM_MIC, STREAM_SYSTEM):
            pcm = store.pcm_path(session_id, stream_name)
            if pcm.is_file():
                try:
                    pcm.unlink()
                except OSError as e:
                    logger.warning("Не вдалось видалити %s: %s", pcm, e)

    # Cleanup проміжних WAV (опц.). final.mp3 уже створено; mic.wav/system.wav —
    # лише крок зведення, нічого їх після finalize не читає. За замовчуванням
    # видаляємо (інакше ~1 ГБ/сесію мертвого місця). keep_wav=True — лишити
    # роздільні доріжки (для майбутнього реміксу/редіаризації).
    if not keep_wav:
        for stream_name in (STREAM_MIC, STREAM_SYSTEM):
            wav = session_dir / f'{stream_name}.wav'
            if wav.is_file():
                try:
                    wav.unlink()
                except OSError as e:
                    logger.warning("Не вдалось видалити %s: %s", wav, e)

    return {
        'session_id': session_id,
        'status': STATUS_FINALIZED,
        'final_mp3_path': str(mp3_path),
        'total_duration_sec': round(total_duration, 3),
        'streams': streams_info,
    }


def discard_session_files(session_id: str, store: SessionStore) -> None:
    """Видалити всі файли сесії (для discard'у користувачем)."""
    session_dir = store.session_dir(session_id)
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


# ---------------------------------------------------------------- video finalize

def finalize_video(
    session_id: str,
    store: SessionStore,
    ffmpeg_path: str,
    build_master: bool = False,
) -> dict:
    """Best-effort: concat segments, remux +faststart, optionally build master.mp4.

    Each track is processed in an isolated try/except; returns partial results.
    NEVER raises — audio finalize is unaffected even if this fails entirely.
    """
    import subprocess
    import tempfile

    _TIMEOUT = 300  # seconds per ffmpeg call

    empty_result = {
        'has_video': False,
        'video_tracks': [],
        'primary_video_path': None,
        'master_path': None,
    }

    try:
        manifest = store.read(session_id)
    except Exception as e:
        logger.warning("finalize_video %s: cannot read manifest: %s", session_id, e)
        return empty_result

    tracks = manifest.get('streams', {}).get('video', [])
    if not tracks:
        return empty_result

    session_dir = store.session_dir(session_id)

    # Derive ffprobe path from ffmpeg_path (swap exe name only)
    ffmpeg_p = Path(ffmpeg_path)
    ffprobe_path = str(ffmpeg_p.parent / ffmpeg_p.name.replace('ffmpeg', 'ffprobe'))

    finalized_tracks: list[dict] = []

    for track in tracks:
        track_id = track.get('track_id', '')
        idx = track.get('monitor_index', 0)
        try:
            # Collect segment files on disk for this track
            base_name = f'video_mon{idx}'
            primary_seg = session_dir / f'{base_name}.mp4'
            extra_segs = sorted(
                session_dir.glob(f'{base_name}_seg*.mp4'),
                key=lambda p: p.name,
            )

            # All existing segment files in order
            all_segs: list[Path] = []
            if primary_seg.is_file():
                all_segs.append(primary_seg)
            all_segs.extend(s for s in extra_segs if s.is_file())

            if not all_segs:
                logger.warning(
                    "finalize_video %s track %s: no segment files found",
                    session_id, track_id,
                )
                finalized_tracks.append({**track, 'status': 'error', 'error': 'no segments'})
                continue

            final_path = session_dir / f'{base_name}_final.mp4'

            if len(all_segs) == 1:
                # Single segment — remux with faststart
                cmd = [
                    ffmpeg_path, '-hide_banner', '-y',
                    '-i', str(all_segs[0]),
                    '-c', 'copy',
                    '-movflags', '+faststart',
                    str(final_path),
                ]
                subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT, check=True,
                               creationflags=NO_WINDOW)
            else:
                # Multiple segments — concat demuxer
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', dir=session_dir,
                    delete=False, encoding='utf-8',
                ) as tf:
                    concat_list_path = tf.name
                    for seg in all_segs:
                        tf.write(f"file '{seg.as_posix()}'\n")

                cmd = [
                    ffmpeg_path, '-hide_banner', '-y',
                    '-f', 'concat', '-safe', '0',
                    '-i', concat_list_path,
                    '-c', 'copy',
                    '-movflags', '+faststart',
                    str(final_path),
                ]
                subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT, check=True,
                               creationflags=NO_WINDOW)

                try:
                    Path(concat_list_path).unlink()
                except OSError:
                    pass

            # Probe duration
            duration_sec: float = 0.0
            try:
                probe_cmd = [
                    ffprobe_path, '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1',
                    str(final_path),
                ]
                probe_result = subprocess.run(
                    probe_cmd, capture_output=True, text=True, timeout=30,
                    creationflags=NO_WINDOW,
                )
                if probe_result.returncode == 0 and probe_result.stdout.strip():
                    duration_sec = float(probe_result.stdout.strip())
            except Exception as probe_err:
                logger.debug(
                    "finalize_video %s track %s: ffprobe failed: %s",
                    session_id, track_id, probe_err,
                )

            # Store ABSOLUTE path (consistent with audio final.mp3 in audio_downloads,
            # so register/playback resolve the file the same way as for audio).
            final_abs = str(final_path)
            store.update_video_track(
                session_id, track_id,
                status='finalized',
                path=final_abs,
                duration_sec=round(duration_sec, 3),
            )
            finalized_tracks.append({
                **track,
                'status': 'finalized',
                'path': final_abs,
                'duration_sec': round(duration_sec, 3),
            })

        except Exception as track_err:
            logger.warning(
                "finalize_video %s track %s: failed: %s",
                session_id, track_id, track_err,
            )
            finalized_tracks.append({**track, 'status': 'error', 'error': str(track_err)})

    # Pick primary track: prefer is_primary flag, else first finalized track
    primary_rel: Optional[str] = None
    for t in finalized_tracks:
        if t.get('status') == 'finalized' and (t.get('is_primary') or primary_rel is None):
            primary_rel = t.get('path')
            if t.get('is_primary'):
                break

    if primary_rel:
        try:
            store.modify(session_id, lambda m: m.__setitem__('primary_video_path', primary_rel))
        except Exception as e:
            logger.warning(
                "finalize_video %s: cannot persist primary_video_path: %s",
                session_id, e,
            )

    # Optionally build master.mp4 (mux primary video + final audio)
    master_rel: Optional[str] = None
    if build_master and primary_rel:
        try:
            re_manifest = store.read(session_id)
            audio_path_str = re_manifest.get('final_mp3_path')
            if audio_path_str and Path(audio_path_str).is_file():
                primary_abs = session_dir / primary_rel
                master_path = session_dir / 'master.mp4'

                # Determine audio start offset relative to video start
                # start_offset_sec stored on the primary track (video start wallclock
                # relative to audio start wallclock). Positive = video started after
                # audio; apply itsoffset on audio input so audio shifts right.
                start_offset_sec: float = 0.0
                for t in finalized_tracks:
                    if t.get('path') == primary_rel:
                        start_offset_sec = float(t.get('start_offset_sec', 0.0))
                        break

                cmd = [
                    ffmpeg_path, '-hide_banner', '-y',
                    '-i', str(primary_abs),
                    '-itsoffset', str(start_offset_sec),
                    '-i', audio_path_str,
                    '-map', '0:v',
                    '-map', '1:a',
                    '-c:v', 'copy',
                    '-c:a', 'aac',
                    '-shortest',
                    str(master_path),
                ]
                result = subprocess.run(
                    cmd, capture_output=True, timeout=_TIMEOUT,
                    creationflags=NO_WINDOW,
                )
                if result.returncode == 0 and master_path.is_file():
                    master_rel = 'master.mp4'
                else:
                    logger.warning(
                        "finalize_video %s: master.mp4 build failed (rc=%d): %s",
                        session_id, result.returncode,
                        result.stderr.decode('utf-8', errors='replace')[-500:],
                    )
        except Exception as master_err:
            logger.warning(
                "finalize_video %s: master build exception: %s",
                session_id, master_err,
            )

    return {
        'has_video': True,
        'video_tracks': finalized_tracks,
        'primary_video_path': primary_rel,
        'master_path': master_rel,
    }
