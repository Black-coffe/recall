"""
app/services/recording/screens.py  —  Phase 22, Story S2

Monitor enumeration and screen-capture helpers.  Zero new pip dependencies —
uses only ctypes against Windows user32/kernel32 and stdlib.

Public API
----------
enumerate_monitors() -> list[dict]
    Enumerate physical monitors via EnumDisplayMonitors + GetMonitorInfoW.

calibrate_output_idx(monitors, ffmpeg, caps) -> list[dict]
    Resolve the DXGI output_idx for each monitor by probing ddagrab.

thumbnail_for(output_idx, ffmpeg, max_w) -> str | None
    Capture one frame from a DXGI output and return a data-URI JPEG.

preflight(video_spec, cfg) -> (ok, sanitized_tracks, reason)
    Validate a video_spec dict before starting a recording session.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional

from app.utils.proc import NO_WINDOW

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------

class _RECT(ctypes.Structure):
    _fields_ = [
        ('left',   ctypes.c_long),
        ('top',    ctypes.c_long),
        ('right',  ctypes.c_long),
        ('bottom', ctypes.c_long),
    ]


class _MONITORINFOEX(ctypes.Structure):
    """MONITORINFOEX — includes szDevice (device name string)."""
    _fields_ = [
        ('cbSize',    ctypes.wintypes.DWORD),
        ('rcMonitor', _RECT),
        ('rcWork',    _RECT),
        ('dwFlags',   ctypes.wintypes.DWORD),
        ('szDevice',  ctypes.c_wchar * 32),
    ]


_MONITORINFOF_PRIMARY = 0x00000001

# Callback type for EnumDisplayMonitors
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool,
    ctypes.wintypes.HMONITOR,
    ctypes.wintypes.HDC,
    ctypes.POINTER(_RECT),
    ctypes.wintypes.LPARAM,
)

# ---------------------------------------------------------------------------
# Module-level calibration cache  {ffmpeg_path: list[dict]}
# ---------------------------------------------------------------------------
_CALIBRATION_CACHE: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# enumerate_monitors
# ---------------------------------------------------------------------------

def enumerate_monitors() -> list[dict]:
    """Return a list of monitor descriptors using EnumDisplayMonitors.

    Each entry:
        {
            'monitor_index': int,   # 0-based, order from EnumDisplayMonitors
            'label':         str,   # device name e.g. '\\\\.\\DISPLAY1'
            'width':         int,
            'height':        int,
            'pos_x':         int,   # left edge in virtual desktop coords
            'pos_y':         int,   # top  edge in virtual desktop coords
            'is_primary':    bool,
            'output_idx':    int,   # default = monitor_index; calibrate() may fix
            'thumbnail':     None,  # filled later by thumbnail_for()
        }

    Never raises — returns [] on any failure.
    """
    monitors: list[dict] = []

    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        def _callback(
            hMonitor: ctypes.wintypes.HMONITOR,
            hdcMonitor: ctypes.wintypes.HDC,
            lprcMonitor: ctypes.POINTER(_RECT),
            dwData: ctypes.wintypes.LPARAM,
        ) -> bool:
            info = _MONITORINFOEX()
            info.cbSize = ctypes.sizeof(_MONITORINFOEX)
            if not user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
                logger.warning('GetMonitorInfoW failed for hMonitor=%s', hMonitor)
                return True  # continue enumeration

            rc = info.rcMonitor
            width  = rc.right  - rc.left
            height = rc.bottom - rc.top
            pos_x  = rc.left
            pos_y  = rc.top

            label = info.szDevice.strip('\x00').strip()
            if not label:
                label = f'\\\\.\\DISPLAY{len(monitors) + 1}'

            idx = len(monitors)
            monitors.append({
                'monitor_index': idx,
                'label':         label,
                'width':         width,
                'height':        height,
                'pos_x':         pos_x,
                'pos_y':         pos_y,
                'is_primary':    bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                'output_idx':    idx,   # default; calibrate_output_idx may fix
                'thumbnail':     None,
            })
            return True  # continue enumeration

        cb = _MONITORENUMPROC(_callback)
        result = user32.EnumDisplayMonitors(None, None, cb, 0)
        if not result:
            logger.warning('EnumDisplayMonitors returned 0')

    except Exception:
        logger.exception('enumerate_monitors failed')
        return []

    return monitors


# ---------------------------------------------------------------------------
# calibrate_output_idx
# ---------------------------------------------------------------------------

def calibrate_output_idx(
    monitors: list[dict],
    ffmpeg: Optional[str] = None,
    caps: Optional[dict] = None,
) -> list[dict]:
    """Resolve DXGI output_idx for each monitor via single-frame ddagrab probes.

    DXGI output order (output_idx=0,1,…) need not match the
    EnumDisplayMonitors order.  For each output_idx K, we capture one
    frame, read the resolution from ffmpeg stderr, and match it to a
    monitor entry.

    Degrades gracefully: if ffmpeg is missing, ddagrab unavailable, or any
    probe times out, the monitors list is returned unchanged.

    Results are cached module-level (keyed by ffmpeg path).

    Args:
        monitors:  List returned by enumerate_monitors().
        ffmpeg:    Path to ffmpeg binary.  Resolved from video_probe if None.
        caps:      Capability dict from video_probe.probe_capabilities().
                   If None we do a quick check ourselves.

    Returns:
        Updated monitors list (same objects, output_idx mutated in-place).
    """
    import copy
    monitors = copy.deepcopy(monitors)

    if not monitors:
        return monitors

    # Resolve ffmpeg path
    ff = ffmpeg
    if not ff:
        try:
            from app.services.recording.video_probe import ffmpeg_path
            ff = ffmpeg_path()
        except Exception:
            logger.debug('calibrate_output_idx: ffmpeg not available, skipping')
            return monitors

    # Cache hit
    if ff in _CALIBRATION_CACHE:
        cached = _CALIBRATION_CACHE[ff]
        if len(cached) == len(monitors):
            return copy.deepcopy(cached)

    # Check ddagrab capability
    ddagrab_ok = False
    if caps is not None:
        ddagrab_ok = bool(caps.get('ddagrab'))
    else:
        try:
            from app.services.recording.video_probe import probe_capabilities
            ddagrab_ok = probe_capabilities(ff).get('ddagrab', False)
        except Exception:
            pass

    if not ddagrab_ok:
        logger.debug('calibrate_output_idx: ddagrab not available, skipping')
        return monitors

    n = len(monitors)

    # Build a resolution lookup from monitors: (w,h) -> [monitor_index]
    res_to_idxs: dict[tuple[int, int], list[int]] = {}
    for m in monitors:
        key = (m['width'], m['height'])
        res_to_idxs.setdefault(key, []).append(m['monitor_index'])

    assigned: set[int] = set()  # monitor_indices already assigned

    for k in range(n):
        try:
            # Build inline probe command (null output, just need the stream info)
            filter_chain = f"ddagrab=output_idx={k}:framerate=1,hwdownload,format=bgra"
            cmd = [
                ff,
                '-hide_banner', '-loglevel', 'info',
                '-init_hw_device', 'd3d11va',
                '-filter_complex', filter_chain,
                '-frames:v', '1',
                '-f', 'null', '-',
            ]

            # Try video_probe builder first (preferred)
            try:
                from app.services.recording import video_probe as vp
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                    tmp_path = tmp.name
                probe_cmd = vp.build_probe_frame_command(output_idx=k, out_path=tmp_path, ffmpeg=ff)
                result = subprocess.run(
                    probe_cmd,
                    capture_output=True, text=True, timeout=10,
                    creationflags=NO_WINDOW,
                )
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                stderr_text = result.stderr
            except Exception:
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=10,
                    creationflags=NO_WINDOW,
                )
                stderr_text = result.stderr

            # Parse "Stream #0:0: Video: … {W}x{H}" from stderr
            m_res = re.search(r'Stream #\S+: Video:.*?(\d{3,5})x(\d{3,5})', stderr_text)
            if not m_res:
                logger.debug('calibrate_output_idx: no stream info in stderr for output_idx=%d', k)
                continue

            w, h = int(m_res.group(1)), int(m_res.group(2))
            candidates = res_to_idxs.get((w, h), [])

            if not candidates:
                logger.debug(
                    'calibrate_output_idx: output_idx=%d reports %dx%d, no monitor match',
                    k, w, h
                )
                continue

            # Pick the first unassigned candidate
            chosen = None
            for c in candidates:
                if c not in assigned:
                    chosen = c
                    break

            if chosen is None:
                # Resolution tie among already-assigned monitors — mark uncalibrated
                for m in monitors:
                    if m['width'] == w and m['height'] == h:
                        m['calibrated'] = False
                logger.debug(
                    'calibrate_output_idx: resolution tie %dx%d for output_idx=%d, leaving default',
                    w, h, k
                )
                continue

            monitors[chosen]['output_idx'] = k
            assigned.add(chosen)
            logger.debug(
                'calibrate_output_idx: monitor_index=%d → output_idx=%d (%dx%d)',
                chosen, k, w, h
            )

        except subprocess.TimeoutExpired:
            logger.warning('calibrate_output_idx: timeout probing output_idx=%d', k)
        except Exception:
            logger.debug('calibrate_output_idx: probe failed for output_idx=%d', k, exc_info=True)

    _CALIBRATION_CACHE[ff] = copy.deepcopy(monitors)
    return monitors


# ---------------------------------------------------------------------------
# thumbnail_for
# ---------------------------------------------------------------------------

def thumbnail_for(
    output_idx: int,
    ffmpeg: Optional[str] = None,
    max_w: int = 320,
) -> Optional[str]:
    """Capture one frame from DXGI output_idx and return a data-URI JPEG.

    Uses ddagrab + hwdownload + scale → mjpeg piped to stdout.
    Returns None on any failure (GPU unavailable, ffmpeg missing, etc.).

    Args:
        output_idx: DXGI output index (from calibrate_output_idx).
        ffmpeg:     Path to ffmpeg binary.
        max_w:      Scale width (height proportional).

    Returns:
        'data:image/jpeg;base64,…' or None.
    """
    ff = ffmpeg
    if not ff:
        try:
            from app.services.recording.video_probe import ffmpeg_path
            ff = ffmpeg_path()
        except Exception:
            return None

    filter_chain = (
        f"ddagrab=output_idx={output_idx}:framerate=1,"
        "hwdownload,format=bgra,"
        f"scale={max_w}:-1"
    )
    cmd = [
        ff,
        '-hide_banner', '-loglevel', 'error',
        '-y',
        '-init_hw_device', 'd3d11va',
        '-filter_complex', filter_chain,
        '-frames:v', '1',
        '-f', 'image2pipe',
        '-c:v', 'mjpeg',
        '-',
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        if result.returncode != 0 or not result.stdout:
            logger.debug(
                'thumbnail_for: output_idx=%d ffmpeg exit=%d stderr=%s',
                output_idx, result.returncode,
                result.stderr.decode('utf-8', errors='replace')[:200],
            )
            return None

        encoded = base64.b64encode(result.stdout).decode('ascii')
        return f'data:image/jpeg;base64,{encoded}'

    except subprocess.TimeoutExpired:
        logger.warning('thumbnail_for: timeout for output_idx=%d', output_idx)
        return None
    except Exception:
        logger.debug('thumbnail_for: failed for output_idx=%d', output_idx, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# monitor_preview
# ---------------------------------------------------------------------------

def monitor_preview(
    monitor_index: int,
    ffmpeg: Optional[str] = None,
    max_w: int = 1000,
) -> dict:
    """Return a full-resolution (up to max_w) preview image for a monitor.

    Resolves monitor_index → output_idx via calibrate_output_idx, reads the
    monitor's real dimensions, and calls thumbnail_for() for the JPEG.

    Args:
        monitor_index: 0-based index from enumerate_monitors().
        ffmpeg:        Path to ffmpeg binary (resolved automatically if None).
        max_w:         Maximum width of the returned image (default 1000).

    Returns:
        {
            'success':        bool,
            'image':          str | None,   # data-URI JPEG or None
            'monitor_width':  int,          # real monitor width (0 on failure)
            'monitor_height': int,          # real monitor height (0 on failure)
        }
    Never raises.
    """
    _fail = {'success': False, 'image': None, 'monitor_width': 0, 'monitor_height': 0}
    try:
        mons = enumerate_monitors()
        if not mons:
            logger.debug('monitor_preview: no monitors found')
            return _fail

        # Find the requested monitor entry
        target = None
        for m in mons:
            if m['monitor_index'] == monitor_index:
                target = m
                break

        if target is None:
            logger.debug('monitor_preview: monitor_index=%d not found', monitor_index)
            return _fail

        mon_w = int(target.get('width', 0))
        mon_h = int(target.get('height', 0))

        # Resolve output_idx via calibration (may degrade gracefully if ffmpeg absent)
        try:
            calibrated = calibrate_output_idx(mons, ffmpeg)
            for cm in calibrated:
                if cm['monitor_index'] == monitor_index:
                    output_idx = cm['output_idx']
                    break
            else:
                output_idx = target['output_idx']
        except Exception:
            output_idx = target['output_idx']

        image = thumbnail_for(output_idx, ffmpeg, max_w=max_w)

        return {
            'success': image is not None,
            'image': image,
            'monitor_width': mon_w,
            'monitor_height': mon_h,
        }
    except Exception:
        logger.debug('monitor_preview: unexpected failure', exc_info=True)
        return _fail


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def preflight(
    video_spec: dict,
    cfg=None,
) -> tuple[bool, list[dict], Optional[str]]:
    """Validate a video_spec before starting a recording session.

    Checks:
    1. video_spec['enabled'] is True — otherwise short-circuits as disabled.
    2. GPU video capabilities: nvenc_h264 AND ddagrab must both be present.
    3. Each requested monitor_index still exists in enumerate_monitors().
    4. Free disk >= RECORDING_MIN_DISK_MB (from cfg or config module fallback).

    Args:
        video_spec: dict with at least:
            {
                'enabled': bool,
                'tracks': [{'monitor_index': int, ...}, ...]
            }
        cfg:        Config object (optional).  Used for RECORDING_MIN_DISK_MB
                    and RECORDING_DIR.

    Returns:
        (ok, sanitized_tracks, reason_if_disabled)
        - ok=True → go ahead with sanitized_tracks.
        - ok=False → reason describes why; caller should proceed audio-only.
        Never raises.
    """
    try:
        # 1. Check enabled flag
        if not video_spec.get('enabled', False):
            return False, [], 'video capture disabled in spec'

        tracks = video_spec.get('tracks', [])

        # 2. Capability check
        try:
            from app.services.recording.video_probe import (
                ffmpeg_path,
                probe_capabilities,
                VideoUnavailable,
            )
            try:
                ff = ffmpeg_path(cfg)
            except VideoUnavailable as exc:
                return False, [], str(exc)

            caps = probe_capabilities(ff)
        except Exception as exc:
            return False, [], f'capability probe error: {exc}'

        if not caps.get('nvenc_h264'):
            return False, [], 'h264_nvenc encoder not available in ffmpeg'
        if not caps.get('ddagrab'):
            return False, [], 'ddagrab filter not available in ffmpeg (NVIDIA driver required)'

        # 3. Validate monitor indices and optional region specs
        live_monitors = enumerate_monitors()
        live_indices = {m['monitor_index'] for m in live_monitors}
        # Build a lookup {monitor_index: monitor_dict} for region validation
        mon_by_idx: dict[int, dict] = {m['monitor_index']: m for m in live_monitors}

        sanitized: list[dict] = []
        skipped: list[int] = []
        for track in tracks:
            idx = track.get('monitor_index')
            if idx is None:
                logger.debug('preflight: track missing monitor_index, skipping: %s', track)
                continue
            if idx not in live_indices:
                skipped.append(idx)
                logger.warning('preflight: monitor_index=%d no longer present, dropping track', idx)
                continue

            t = dict(track)  # shallow copy — we may mutate mode/region

            # Validate region if mode == 'region'
            if t.get('mode') == 'region':
                region = t.get('region')
                mon = mon_by_idx.get(idx, {})
                mon_w = int(mon.get('width', 0))
                mon_h = int(mon.get('height', 0))

                valid_region = False
                if (
                    isinstance(region, dict)
                    and mon_w > 0 and mon_h > 0
                ):
                    rx = int(region.get('x', 0))
                    ry = int(region.get('y', 0))
                    rw = int(region.get('w', 0))
                    rh = int(region.get('h', 0))

                    # Clamp to monitor bounds
                    rx = max(0, min(rx, mon_w - 1))
                    ry = max(0, min(ry, mon_h - 1))
                    rw = min(rw, mon_w - rx)
                    rh = min(rh, mon_h - ry)

                    if rw >= 64 and rh >= 64:
                        # Region is valid (possibly clamped)
                        t['region'] = {'x': rx, 'y': ry, 'w': rw, 'h': rh}
                        valid_region = True
                        logger.debug(
                            'preflight: monitor_index=%d region validated/clamped %s',
                            idx, t['region'],
                        )
                    else:
                        logger.warning(
                            'preflight: monitor_index=%d region too small after clamp '
                            '(%dx%d), falling back to full-monitor',
                            idx, rw, rh,
                        )

                if not valid_region:
                    # Fall back to full-monitor — never reject the whole request
                    t['mode'] = 'full'
                    t.pop('region', None)
                    logger.warning(
                        'preflight: monitor_index=%d region invalid/missing, '
                        'switched to mode=full',
                        idx,
                    )

            sanitized.append(t)

        if skipped:
            logger.warning('preflight: dropped %d track(s) for missing monitors %s', len(skipped), skipped)

        if not sanitized and tracks:
            return False, [], f'no valid monitor tracks remain (missing indices: {skipped})'

        # 4. Free disk check
        min_mb = 500  # fallback default
        check_path: str = os.getcwd()

        if cfg is not None:
            min_mb = int(getattr(cfg, 'RECORDING_MIN_DISK_MB', min_mb))
            rec_dir = getattr(cfg, 'RECORDING_DIR', None)
            if rec_dir is not None:
                check_path = str(rec_dir)
        else:
            try:
                import config as _cfg_mod
                _c = getattr(_cfg_mod, 'current_config', None)
                if _c is not None:
                    min_mb = int(getattr(_c, 'RECORDING_MIN_DISK_MB', min_mb))
                    rec_dir = getattr(_c, 'RECORDING_DIR', None)
                    if rec_dir is not None:
                        check_path = str(rec_dir)
            except Exception:
                pass

        try:
            usage = shutil.disk_usage(check_path)
            free_mb = usage.free / (1024 * 1024)
            if free_mb < min_mb:
                return False, [], (
                    f'insufficient disk space: {free_mb:.0f} MB free, '
                    f'need {min_mb} MB (RECORDING_MIN_DISK_MB)'
                )
        except Exception as exc:
            logger.warning('preflight: disk check failed (%s), proceeding anyway', exc)

        return True, sanitized, None

    except Exception as exc:
        logger.exception('preflight: unexpected error')
        return False, [], f'preflight error: {exc}'
