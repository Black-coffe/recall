"""
app/services/recording/video_probe.py  —  Phase 22, Story S0

Pure, read-only capability probe + command-builder for screen video capture.
No audio imports, no heavy dependencies.  Import-safe at any time.

Verified capture chain (tested live, 2560×1440, h264_nvenc):
  ffmpeg -hide_banner -loglevel info -y
    -init_hw_device d3d11va
    -filter_complex "ddagrab=output_idx=0:framerate=30,hwdownload,format=bgra"
    -c:v h264_nvenc -preset p5 -cq 23
    -movflags +frag_keyframe+empty_moov+default_base_moof
    -f mp4 OUT.mp4

IMPORTANT: NVENC accepts bgra system-memory frames directly from ddagrab+hwdownload.
Do NOT add hwupload_cuda or scale_cuda — they break the chain.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

try:
    from app.utils.proc import NO_WINDOW
except ImportError:  # запуск файлу напряму (`python app/services/recording/video_probe.py`)
    NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# ---------------------------------------------------------------------------
# Default constants (mirrors Config — kept here so module is standalone)
# ---------------------------------------------------------------------------
_DEFAULT_FFMPEG = r'C:\ffmpeg\bin\ffmpeg.exe'

# Module-level probe cache: {ffmpeg_path: caps_dict}
_PROBE_CACHE: dict[str, dict[str, bool]] = {}


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class VideoUnavailable(RuntimeError):
    """Raised when video capture cannot proceed (ffmpeg missing, etc.)."""


# ---------------------------------------------------------------------------
# ffmpeg path resolver
# ---------------------------------------------------------------------------

def ffmpeg_path(cfg=None) -> str:
    """Return the configured ffmpeg executable path.

    Args:
        cfg: A config object (or None).  Reads RECORDING_FFMPEG_PATH attribute
             if present, otherwise falls back to the hardcoded default.

    Raises:
        VideoUnavailable: if the resolved path is not an existing file.
    """
    path = _DEFAULT_FFMPEG
    if cfg is not None:
        path = getattr(cfg, 'RECORDING_FFMPEG_PATH', path)
    else:
        # Try to read from config module without creating a hard import cycle
        try:
            import config as _cfg_mod
            _cfg = getattr(_cfg_mod, 'current_config', None)
            if _cfg is not None:
                path = getattr(_cfg, 'RECORDING_FFMPEG_PATH', path)
        except Exception:
            pass

    if not os.path.isfile(path):
        raise VideoUnavailable(
            f"ffmpeg not found at {path!r}. "
            "Set RECORDING_FFMPEG_PATH env var or install ffmpeg to C:\\ffmpeg\\bin\\."
        )
    return path


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------

def probe_capabilities(ffmpeg: Optional[str] = None) -> dict[str, bool]:
    """Probe ffmpeg for the codec/filter capabilities we care about.

    Runs ``ffmpeg -hide_banner -encoders`` and ``ffmpeg -hide_banner -filters``
    once per ffmpeg path and caches the result.  Never raises — returns
    all-False on any error so callers can degrade gracefully.

    Returns:
        dict with bool values for keys:
            nvenc_h264, nvenc_hevc, av1_nvenc, ddagrab, gdigrab
    """
    _all_false: dict[str, bool] = {
        'nvenc_h264': False,
        'nvenc_hevc': False,
        'av1_nvenc': False,
        'ddagrab': False,
        'gdigrab': False,
    }

    try:
        ff = ffmpeg or ffmpeg_path()
    except VideoUnavailable:
        return dict(_all_false)

    if ff in _PROBE_CACHE:
        return dict(_PROBE_CACHE[ff])

    caps = dict(_all_false)

    try:
        enc_result = subprocess.run(
            [ff, '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=15,
            creationflags=NO_WINDOW,
        )
        enc_out = enc_result.stdout + enc_result.stderr
        caps['nvenc_h264'] = 'h264_nvenc' in enc_out
        caps['nvenc_hevc'] = 'hevc_nvenc' in enc_out
        caps['av1_nvenc'] = 'av1_nvenc' in enc_out
    except Exception:
        pass  # leave encoder caps False

    try:
        filt_result = subprocess.run(
            [ff, '-hide_banner', '-filters'],
            capture_output=True, text=True, timeout=15,
            creationflags=NO_WINDOW,
        )
        filt_out = filt_result.stdout + filt_result.stderr
        caps['ddagrab'] = 'ddagrab' in filt_out
    except Exception:
        pass  # leave ddagrab False

    try:
        # gdigrab — це INPUT-DEVICE (libavdevice), він у списку `-devices`, НЕ `-filters`.
        dev_result = subprocess.run(
            [ff, '-hide_banner', '-devices'],
            capture_output=True, text=True, timeout=15,
            creationflags=NO_WINDOW,
        )
        dev_out = dev_result.stdout + dev_result.stderr
        caps['gdigrab'] = 'gdigrab' in dev_out
    except Exception:
        pass  # leave gdigrab False

    _PROBE_CACHE[ff] = caps
    return dict(caps)


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def _even_region(region: dict) -> tuple[int, int, int, int]:
    """Coerce region dict to (w, h, x, y) with even w/h (rounded down, min 2)."""
    x = max(0, int(region.get('x', 0)))
    y = max(0, int(region.get('y', 0)))
    w = max(2, int(region.get('w', 2)) & ~1)
    h = max(2, int(region.get('h', 2)) & ~1)
    return w, h, x, y


def build_capture_command(
    *,
    output_idx: int,
    out_path: str,
    fps: int = 30,
    codec: str = 'h264_nvenc',
    quality: str = 'p5',
    cq: int = 23,
    ffmpeg: Optional[str] = None,
    region: Optional[dict] = None,
) -> list[str]:
    """Build the argv list for the VERIFIED ddagrab → NVENC capture chain.

    Output is a fragmented MP4 suitable for streaming / crash-resilient
    recording (frag_keyframe + empty_moov + default_base_moof).

    Args:
        output_idx: DXGI output index (0 = primary monitor).
        out_path:   Destination file path (absolute recommended).
        fps:        Capture frame rate (default 30).
        codec:      NVENC codec name (default 'h264_nvenc').
        quality:    NVENC preset name (default 'p5').
        cq:         Constant Quality value (default 23).
        ffmpeg:     Path to ffmpeg binary; resolved via ffmpeg_path() if None.
        region:     Optional monitor-local crop {'x', 'y', 'w', 'h'}.
                    w/h are rounded down to nearest even int (NVENC requirement).
                    None → full monitor (no crop appended).

    Returns:
        List of strings ready for subprocess.Popen / subprocess.run.
    """
    ff = ffmpeg or ffmpeg_path()
    filter_chain = (
        f"ddagrab=output_idx={output_idx}:framerate={fps},"
        "hwdownload,format=bgra"
    )
    if region:
        w, h, x, y = _even_region(region)
        filter_chain += f",crop={w}:{h}:{x}:{y}"
    return [
        ff,
        '-hide_banner', '-loglevel', 'info',
        '-y',
        '-init_hw_device', 'd3d11va',
        '-filter_complex', filter_chain,
        '-c:v', codec,
        '-preset', quality,
        '-cq', str(cq),
        '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
        '-f', 'mp4',
        out_path,
    ]


def build_probe_frame_command(
    *,
    output_idx: int,
    out_path: str,
    ffmpeg: Optional[str] = None,
) -> list[str]:
    """Build argv for a single-frame grab (monitor calibration / thumbnail).

    Grabs one frame via ddagrab, scales to width=320 (preserving aspect),
    and writes a JPEG.  Fast and non-blocking.

    Args:
        output_idx: DXGI output index (0 = primary monitor).
        out_path:   Destination JPEG path.
        ffmpeg:     Path to ffmpeg binary; resolved via ffmpeg_path() if None.

    Returns:
        List of strings ready for subprocess.run.
    """
    ff = ffmpeg or ffmpeg_path()
    filter_chain = (
        f"ddagrab=output_idx={output_idx}:framerate=1,"
        "hwdownload,format=bgra,"
        "scale=320:-1"
    )
    return [
        ff,
        '-hide_banner', '-loglevel', 'error',
        '-y',
        '-init_hw_device', 'd3d11va',
        '-filter_complex', filter_chain,
        '-frames:v', '1',
        '-c:v', 'mjpeg',
        '-f', 'image2',
        out_path,
    ]


# ---------------------------------------------------------------------------
# gdigrab fallback (documented, not used in Phase 22 primary path)
# ---------------------------------------------------------------------------
# gdigrab is the software GDI-based screen capture — works without NVIDIA GPU
# but is CPU-heavy and can't use NVENC directly (needs hwupload_cuda first).
# Keep as a reference / fallback for machines without ddagrab support.
#
# Verified fallback chain (CPU + hevc_nvenc or libx264):
#   ffmpeg -hide_banner -loglevel info -y
#     -f gdigrab -framerate 30 -i desktop
#     -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2"
#     -c:v hevc_nvenc -preset p5 -cq 28
#     -movflags +frag_keyframe+empty_moov+default_base_moof
#     -f mp4 OUT.mp4

def build_gdigrab_command(
    *,
    out_path: str,
    fps: int = 30,
    codec: str = 'hevc_nvenc',
    quality: str = 'p5',
    cq: int = 28,
    ffmpeg: Optional[str] = None,
) -> list[str]:
    """Build argv for a gdigrab (GDI, CPU, full desktop) fallback chain.

    Use when ddagrab / D3D11 is unavailable (no NVIDIA GPU or older driver).
    The scale filter ensures even dimensions required by most encoders.
    """
    ff = ffmpeg or ffmpeg_path()
    return [
        ff,
        '-hide_banner', '-loglevel', 'info',
        '-y',
        '-f', 'gdigrab',
        '-framerate', str(fps),
        '-i', 'desktop',
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
        '-c:v', codec,
        '-preset', quality,
        '-cq', str(cq),
        '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
        '-f', 'mp4',
        out_path,
    ]
