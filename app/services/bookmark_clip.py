"""Bookmark clip: cut the audio around a bookmarked segment and pack it with its text.

``cut()`` runs ffprobe (duration) and ffmpeg (the cut, mp3 to stdout) with
``creationflags=proc.NO_WINDOW``: the live app runs under ``pythonw.exe`` and a console
child without the flag flashes a window over the call.

``resolve_audio()`` repeats the checks of ``GET /api/transcription/<id>/audio``
(``transcription.py``): the record exists and is not deleted, the file is on disk, the
path lies under the allowed roots.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import zipfile
from typing import Optional

from app.utils import proc
from app.utils.paths import safe_path_within_any

logger = logging.getLogger(__name__)

DEFAULT_PAD = 5.0
MAX_PAD = 60.0
_TIMEOUT = 120


class ClipUnavailable(RuntimeError):
    """ffmpeg/ffprobe missing or the cut failed."""


def allowed_roots(cfg) -> list[str]:
    """Same roots as the audio route."""
    base = cfg.get('BASE_DIR', '.')
    return [
        cfg.get('UPLOAD_FOLDER', ''),
        os.path.join(base, 'youtube_downloads'),
        os.path.join(base, 'recordings'),
    ]


def resolve_audio(file_path: Optional[str], cfg) -> tuple[Optional[str], Optional[str]]:
    """Return ``(abs_path, None)`` or ``(None, reason)`` with reason ``no_media`` | ``forbidden``."""
    if not file_path or not os.path.isfile(file_path):
        return None, 'no_media'
    abs_path = safe_path_within_any(allowed_roots(cfg), file_path)
    if abs_path is None:
        logger.warning("Clip request blocked (path outside allowed roots): %s", file_path)
        return None, 'forbidden'
    return abs_path, None


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ClipUnavailable(f'{name} not found in PATH')
    return path


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT,
                             creationflags=proc.NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        raise ClipUnavailable(f'{os.path.basename(cmd[0])} failed: {e}') from e
    if res.returncode != 0:
        tail = (res.stderr or b'')[-300:].decode('utf-8', 'replace')
        raise ClipUnavailable(f'{os.path.basename(cmd[0])} exit {res.returncode}: {tail}')
    return res


def duration(src: str) -> float:
    res = _run([_tool('ffprobe'), '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', src])
    try:
        return float((res.stdout or b'').decode('ascii', 'replace').strip())
    except ValueError as e:
        raise ClipUnavailable(f'ffprobe gave no duration for {src}') from e


def cut(src: str, start: float, end: float, *, pad: float = DEFAULT_PAD) -> bytes:
    """mp3 bytes of ``src`` from ``start - pad`` to ``end + pad``, clamped to the file."""
    total = duration(src)
    lo = max(0.0, float(start) - pad)
    hi = min(total, float(end) + pad)
    if hi <= lo:
        raise ClipUnavailable(f'empty range {lo:.3f}..{hi:.3f} (duration {total:.3f})')
    res = _run([_tool('ffmpeg'), '-hide_banner', '-loglevel', 'error',
                '-ss', f'{lo:.3f}', '-to', f'{hi:.3f}', '-i', src,
                '-vn', '-acodec', 'libmp3lame', '-f', 'mp3', 'pipe:1'])
    data = res.stdout or b''
    if not data:
        raise ClipUnavailable('ffmpeg produced no audio')
    return data


def _ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def clip_text(segment: dict, note: Optional[str], note_label: str) -> str:
    """Text of the bookmarked segment: times, speaker if present, text, the note."""
    head = f"[{_ts(segment.get('start', 0))} - {_ts(segment.get('end', segment.get('start', 0)))}]"
    speaker = segment.get('speaker')
    text = (segment.get('text') or '').strip()
    lines = [head, f'{speaker}: {text}' if speaker else text]
    if note:
        lines += ['', f'{note_label}: {note}']
    return '\n'.join(lines) + '\n'


def build_zip(mp3: bytes, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('clip.mp3', mp3)
        zf.writestr('clip.txt', text.encode('utf-8'))
    return buf.getvalue()
