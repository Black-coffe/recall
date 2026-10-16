"""Finalize pipeline (Phase 9.4): PCM → WAV → mix → MP3.

The last stage of a recording session: turns the raw PCM streams into a single
audio file ready for transcription and storage in the Audio Library.

Pipeline (for a two-stream session):

.. code-block:: text

    mic.pcm    ──pcm_to_wav──→ mic.wav    ──┐
                                              ├──pydub overlay──→ final.mp3
    system.pcm ──pcm_to_wav──→ system.wav ──┘

For a single-stream recording (mic only or system only) the pipeline
is shorter, without overlay, just WAV → MP3.

Pause/resume: on pause the writer flushes+closes, on resume a new segment
with offset = total_bytes. Nothing is written to the file during a pause, so
the PCM on disk contains no silence. There is no need to cut segments — the bytes
are already gapless.

Why the stdlib ``wave`` for PCM→WAV:
- It needs neither pydub nor ffmpeg for the basic conversion.
- Fast: it just adds a 44-byte RIFF header to the existing PCM bytes.
- Streaming-friendly: ``writeframes`` writes in chunks, does not load
  everything into RAM.

Why pydub for mix→MP3:
- Native overlay supports volumes and fades (for the future).
- MP3 export through ffmpeg (already in the project).
- A familiar interface: ``AudioSegment.overlay()``.
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


# The threshold above which the mix goes through ffmpeg (constant memory) instead of pydub
# (which holds the whole decoded WAV in RAM). 400 MB ≈ 40 min of stereo 48kHz — anything
# shorter pydub handles calmly; anything longer hits the process memory.
FFMPEG_MIX_THRESHOLD_BYTES = 400 * 1024 * 1024

# Ceiling for one ffmpeg mix. A 70-min recording is really mixed in ~27s, but
# a multi-hour session on a cold disk may drag on — we take a margin.
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
    """Converts raw PCM to WAV with a RIFF header.

    Does a streaming write: reads ``chunk_size`` bytes at a time and writes through
    ``wave.writeframes``. Does not load the whole file into RAM, so it is suitable
    for multi-hour recordings.

    Returns the number of audio frames written.

    Details:
    - If the PCM file is empty or missing — creates an empty WAV (0 frames)
      and returns 0. This is a valid WAV, it can be opened.
    - If the PCM size is not a multiple of (channels * sample_width_bytes) —
      the trailing bytes are ignored (rounded down to a whole sample).
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
            logger.warning("PCM not found: %s — created an empty WAV", pcm_path)
            return 0

        with open(pcm_path, 'rb') as pf:
            while True:
                chunk = pf.read(chunk_size)
                if not chunk:
                    break
                # Round the chunk down to a whole sample
                usable = (len(chunk) // frame_size) * frame_size
                if usable < len(chunk):
                    logger.debug(
                        "Tail of %d bytes in %s rounded down to %d",
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
    """Mixes mic + system into an MP3.

    Logic:
    - Large tracks (together > ``FFMPEG_MIX_THRESHOLD_BYTES``) → a streaming
      mix through ffmpeg, WITHOUT loading into RAM (see :func:`_mix_via_ffmpeg`)
    - Otherwise both WAVs → overlay through pydub → MP3
    - Only mic or only system (the other = None or empty) → straight
      WAV → MP3 without overlay
    - Both missing → :class:`FileNotFoundError`

    Gain in dB: 0 = native, +6 ≈ x2 amplitude, -6 ≈ /2.

    Returns the path to the generated MP3.
    """
    from pydub import AudioSegment  # lazy import — pydub pulls in ffmpeg on import

    output_mp3 = Path(output_mp3)
    output_mp3.parent.mkdir(parents=True, exist_ok=True)

    # pydub holds the WHOLE decoded WAV in memory (and the same amount again during
    # the overlay). For a multi-hour session that is gigabytes: a 10-hour recording =
    # 3.3 GB mic + 0.9 GB system. This is not theory — exactly such a recording went
    # crashed on 21.07.2026, and boot recovery now tries to finalize
    # such sessions automatically, so the OOM risk would move to APPLICATION START.
    # ffmpeg does the same as a stream, with constant memory.
    _inputs = [p for p in (mic_wav, system_wav) if p and Path(p).is_file()]
    _total = sum(Path(p).stat().st_size for p in _inputs)
    if _total > FFMPEG_MIX_THRESHOLD_BYTES:
        logger.info(
            "mix_streams: %.2f GB of input data — mixing through ffmpeg (not pydub)",
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
            # Not fatal: fall back to pydub. Worse for memory, but this is
            # exactly the behaviour that existed before this optimization.
            logger.warning(
                "ffmpeg mix failed (%s) — falling back to pydub, "
                "a large memory spike is possible", e,
            )

    mic_seg = _maybe_load_wav(mic_wav, mic_gain_db)
    sys_seg = _maybe_load_wav(system_wav, system_gain_db)

    if mic_seg is None and sys_seg is None:
        raise FileNotFoundError(
            f"No valid WAV to mix (mic={mic_wav}, system={system_wav})"
        )

    if mic_seg is not None and sys_seg is not None:
        # Align the length: the shorter one is overlaid at position 0,
        # the longer one stays as is.
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
    """Streaming mix through ffmpeg — constant memory at any length.

    Semantics 1:1 with the pydub branch:
    - ``amix=normalize=0`` — a plain sum of samples, like ``AudioSegment.overlay``
      (with ``normalize=1``, the ffmpeg default, every input would be divided by N → quieter).
    - ``duration=longest`` — the shorter input is padded with silence, the longer one stays
      whole (pydub puts the shorter one at position 0 of the longer).
    - target rate/channels = the maximum over the inputs — the same as ``_sync`` in pydub.
    - gain in dB through ``volume``.

    Raises an exception on any problem — the caller falls back to pydub.
    """
    import subprocess

    from pydub import AudioSegment  # the same ffmpeg resolver as in pydub

    ffmpeg_bin = getattr(AudioSegment, 'converter', None) or shutil.which('ffmpeg')
    if not ffmpeg_bin:
        raise FinalizeError('ffmpeg not found')

    inputs: list[tuple[Path, float]] = []
    for path, gain in ((mic_wav, mic_gain_db), (system_wav, system_gain_db)):
        if path is None:
            continue
        p = Path(path)
        if p.is_file() and p.stat().st_size > 0:
            inputs.append((p, gain))
    if not inputs:
        raise FinalizeError('No input WAVs for the ffmpeg mix')

    # Read the parameters from the RIFF header (wave does not load the audio data).
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
            # NOT aformat: on mono→stereo the rematrix in swresample normalizes
            # the mix and it gets quieter by 3 dB, whereas pydub set_channels() simply
            # duplicates the channel. For us this is not cosmetic — the mic track is mono and already
            # the quietest (microphone vs loopback), the extra -3 dB hits precisely
            # the owner's voice and the quality of its transcription. pan copies the
            # channel one-to-one, like pydub.
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
        raise FinalizeError('ffmpeg finished, but the MP3 is empty')
    return output_mp3


def _maybe_load_wav(path: Optional[Path | str], gain_db: float):
    """Returns an AudioSegment or None if the file is missing/empty."""
    if path is None:
        return None
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return None
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_wav(str(p))
    except Exception as e:
        logger.warning("Failed to load %s: %s", p, e)
        return None
    if seg.duration_seconds < 0.05:
        logger.debug("WAV %s is too short (%.3fs) — ignoring",
                     p, seg.duration_seconds)
        return None
    if gain_db != 0.0:
        seg = seg.apply_gain(gain_db)
    return seg


# ---------------------------------------------------------------- session finalize

class FinalizeError(RuntimeError):
    """Finalization error."""


def finalize_session(
    session_id: str,
    store: SessionStore,
    bitrate: str = '192k',
    keep_pcm: bool = False,
    keep_wav: bool = False,
    mic_gain_db: float = 0.0,
    system_gain_db: float = 0.0,
) -> dict:
    """The full pipeline for a session: PCM → WAV → MP3 → manifest update.

    Steps:
    1. Read the manifest (it must exist).
    2. For every active stream (mic/system) — pcm_to_wav.
    3. mix_streams → final.mp3.
    4. Update the manifest: status=finalized, final_mp3_path, total_duration.
    5. If keep_pcm=False — delete the .pcm files.
    6. If keep_wav=False — delete the intermediate mic.wav/system.wav (nothing
       reads them after finalize; otherwise ~1 GB/session accumulates).

    Returns a dict with the summary: paths, durations, status.

    Raises :class:`FinalizeError` on a crash at any step.
    The manifest is NOT updated before finalize in that case — the status
    stays the previous one (stopping/crashed), the recovery flow can
    retry.
    """
    try:
        manifest = store.read(session_id)
    except SessionStoreError as e:
        raise FinalizeError(f"Manifest is missing: {e}") from e

    session_dir = store.session_dir(session_id)
    # Top-level stays as the target for the mix (the final MP3),
    # but for PCM→WAV we take per-stream native parameters
    # (they may differ: mono mic 88200/1 vs stereo loopback 48000/2).
    target_sample_rate = int(manifest['sample_rate'])
    target_channels = int(manifest['channels'])
    bytes_per_sample = int(manifest.get('bytes_per_sample', 2))

    streams_info: dict[str, dict] = {}
    wav_paths: dict[str, Path] = {}

    for stream_name in (STREAM_MIC, STREAM_SYSTEM):
        stream_meta = manifest['streams'][stream_name]
        if not stream_meta.get('enabled'):
            continue
        # Per-stream native params (Phase 9.10 fix). Fallback to top-level
        # for backward compat with old manifests without these fields.
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
                f"pcm_to_wav for {stream_name} failed: {e}"
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
            logger.info("Stream %s is empty — skipping it in the mix", stream_name)

    if not wav_paths:
        # There is no data at all. We mark the manifest finalized with an empty mp3.
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
        raise FinalizeError(f"mix_streams failed: {e}") from e

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

    # Cleanup of raw PCM (optional)
    if not keep_pcm:
        for stream_name in (STREAM_MIC, STREAM_SYSTEM):
            pcm = store.pcm_path(session_id, stream_name)
            if pcm.is_file():
                try:
                    pcm.unlink()
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", pcm, e)

    # Cleanup of intermediate WAVs (optional). final.mp3 is already created; mic.wav/system.wav are
    # only a mixing step, nothing reads them after finalize. By default
    # we delete them (otherwise ~1 GB/session of dead space). keep_wav=True keeps the
    # separate tracks (for a future remix/re-diarization).
    if not keep_wav:
        for stream_name in (STREAM_MIC, STREAM_SYSTEM):
            wav = session_dir / f'{stream_name}.wav'
            if wav.is_file():
                try:
                    wav.unlink()
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", wav, e)

    return {
        'session_id': session_id,
        'status': STATUS_FINALIZED,
        'final_mp3_path': str(mp3_path),
        'total_duration_sec': round(total_duration, 3),
        'streams': streams_info,
    }


def discard_session_files(session_id: str, store: SessionStore) -> None:
    """Delete all session files (for a discard by the user)."""
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
