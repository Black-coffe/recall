"""VideoCaptureSupervisor — screen-capture sidecar (Phase 22, Story S1).

Manages one or more ffmpeg screen-capture processes (ddagrab → h264_nvenc,
fragmented MP4) alongside an audio recording session.  The #1 invariant of
this module: **nothing it does may ever raise into its caller**.  Every public
method swallows all exceptions internally.

Dependencies
------------
`app.services.recording.video_probe` (Story S0, created in parallel) provides:
    - build_capture_command(*, output_idx, out_path, fps, codec, quality, cq, ffmpeg) -> list[str]
    - probe_capabilities(ffmpeg=None) -> dict  (keys: nvenc_h264, nvenc_hevc, ddagrab, gdigrab)
    - ffmpeg_path(cfg=None) -> str
    - VideoUnavailable (exception)

These are imported lazily inside methods so this module loads cleanly even when
video_probe does not yet exist on disk.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from app.utils.proc import NO_WINDOW
except ImportError:  # demo-CLI знизу запускають файлом, а не через `-m`
    NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NVENC error signatures that trigger the codec fallback
# ---------------------------------------------------------------------------
_NVENC_LIMIT_PATTERNS = [
    "OpenEncodeSessionEx failed",
    "out of memory",
    "No capacity",
    "Cannot load nvEncodeAPI",
]

# Regex to parse ffmpeg progress lines:
#   frame=  123 fps= 30 q=28.0 size=    1024kB time=00:00:04.10 bitrate= ...
#   Lsize=   1234kB  (end-of-stream)
_FFMPEG_PROGRESS_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+)",
    re.IGNORECASE,
)
# drop= відсутній у прогрес-рядку деяких ffmpeg-білдів (зокрема нашого gyan.dev)
# — парсимо окремо, дефолт 0, інакше весь рядок не матчиться і fps лишається '?'.
_FFMPEG_DROP_RE = re.compile(r"drop=\s*(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class VideoTrack:
    track_id: str               # 'mon0', 'mon1', ...
    monitor_index: int
    output_idx: int             # ddagrab DXGI adapter index
    monitor_label: str
    path: str                   # absolute output file path
    codec: str = "h264_nvenc"
    fps: int = 30
    mode: str = "full"          # 'full' | 'region'
    region: Optional[dict] = None  # monitor-local crop {'x','y','w','h'}; None = full
    monitor_pos_x: int = 0      # desktop X of monitor top-left (virtual-desktop coords)
    monitor_pos_y: int = 0      # desktop Y of monitor top-left (virtual-desktop coords)
    proc: object = None         # subprocess.Popen, or None
    overlay_proc: object = None  # region_overlay subprocess, or None; NOT in as_dict()
    status: str = "starting"    # starting | recording | failed | finalized
    start_wallclock: float = 0.0
    start_offset_sec: float = 0.0
    error: Optional[str] = None
    last_stats: dict = field(default_factory=dict)  # {fps, dropped, bytes}

    def as_dict(self) -> dict:
        """Return a JSON-serialisable snapshot (no Popen object)."""
        return {
            "track_id":        self.track_id,
            "monitor_index":   self.monitor_index,
            "output_idx":      self.output_idx,
            "monitor_label":   self.monitor_label,
            "path":            self.path,
            "codec":           self.codec,
            "fps":             self.fps,
            "mode":            self.mode,
            "region":          self.region,
            "monitor_pos_x":   self.monitor_pos_x,
            "monitor_pos_y":   self.monitor_pos_y,
            "status":          self.status,
            "start_wallclock": self.start_wallclock,
            "start_offset_sec": self.start_offset_sec,
            "error":           self.error,
            "last_stats":      dict(self.last_stats),
        }


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class VideoCaptureSupervisor:
    """Orchestrates screen-capture ffmpeg processes for one recording session.

    Parameters
    ----------
    session_id : str
        Recording session identifier (e.g. 'rec_abc123').
    session_dir : str
        Absolute path to the session directory where mp4 files are written.
    tracks_spec : list[dict]
        Each dict: {'monitor_index':int, 'output_idx':int, 'monitor_label':str,
                    'fps':int, 'codec':str}.  Optional keys inherit defaults.
    ffmpeg_path : str
        Path to the ffmpeg executable.
    audio_start_wallclock : float
        ``time.time()`` value at the moment audio recording started.  Used to
        compute ``start_offset_sec`` for A/V sync.
    caps : dict | None
        Result of ``probe_capabilities()``.  If None, NVENC fallback decisions
        are conservative (no hevc retry).
    broker : SSEBroker | None
        Optional SSE broker.  Events published: ``video_status``, ``video_stats``.
        The event name ``'error'`` is deliberately avoided (the broker closes
        the stream on that name).
    store : object | None
        Optional session store.  Must expose
        ``add_video_track(session_id, track_dict) -> None``.
    fps, codec, quality, cq : defaults used when a track spec omits them.
    stop_timeout : float
        Seconds to wait for graceful 'q' stop before SIGTERM/SIGKILL.
    popen_factory : callable
        Injected for testing; defaults to ``subprocess.Popen``.
    """

    def __init__(
        self,
        *,
        session_id: str,
        session_dir: str,
        tracks_spec: List[Dict[str, Any]],
        ffmpeg_path: str,
        audio_start_wallclock: float,
        caps: Optional[Dict[str, Any]] = None,
        broker=None,
        store=None,
        fps: int = 30,
        codec: str = "h264_nvenc",
        quality: str = "p5",
        cq: int = 23,
        stop_timeout: float = 8.0,
        popen_factory=subprocess.Popen,
        region_overlay: bool = True,
        overlay_color: str = '#e5484d',
        overlay_factory=None,
    ) -> None:
        self._session_id = session_id
        self._session_dir = session_dir
        self._ffmpeg_path = ffmpeg_path
        self._audio_start_wallclock = audio_start_wallclock
        self._caps = caps or {}
        self._broker = broker
        self._store = store
        self._default_fps = fps
        self._default_codec = codec
        self._default_quality = quality
        self._default_cq = cq
        self._stop_timeout = stop_timeout
        self._popen_factory = popen_factory
        self._region_overlay = region_overlay
        self._overlay_color = overlay_color
        self._overlay_factory = overlay_factory or subprocess.Popen

        # Build track objects from spec
        self._tracks: List[VideoTrack] = []
        for spec in tracks_spec:
            idx = spec.get("monitor_index", 0)
            track = VideoTrack(
                track_id=f"mon{idx}",
                monitor_index=idx,
                output_idx=spec.get("output_idx", idx),
                monitor_label=spec.get("monitor_label", f"Monitor {idx}"),
                path=os.path.join(session_dir, f"video_mon{idx}.mp4"),
                codec=spec.get("codec", codec),
                fps=spec.get("fps", fps),
                mode=spec.get("mode", "full"),
                region=spec.get("region"),
                monitor_pos_x=spec.get("monitor_pos_x", 0),
                monitor_pos_y=spec.get("monitor_pos_y", 0),
            )
            self._tracks.append(track)

        # Per-track stderr queues (created in start())
        self._stderr_queues: Dict[str, queue.Queue] = {}
        self._stderr_threads: List[threading.Thread] = []
        # Throttle for persisting last_stats to the manifest (track_id -> last ts)
        self._last_stats_persist: Dict[str, float] = {}

        self._stop_evt = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

        # Segment counter per track (for pause/resume)
        self._seg_counters: Dict[str, int] = {t.track_id: 0 for t in self._tracks}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn an ffmpeg capture process for each track.  Never raises."""
        try:
            from app.services.recording.video_probe import build_capture_command
        except Exception as _e:
            logger.error("video_probe not available, cannot start video capture: %s", _e)
            for t in self._tracks:
                t.status = "failed"
                t.error = f"video_probe import failed: {_e}"
                self._publish_status(t)
            return

        for track in self._tracks:
            try:
                self._spawn_track(track, build_capture_command)
            except Exception as e:
                # Should never reach here — _spawn_track is already guarded —
                # but be absolutely sure we never propagate.
                logger.error("Unexpected error spawning track %s: %s", track.track_id, e)
                track.status = "failed"
                track.error = str(e)
                self._publish_status(track)

        # Persist to manifest
        if self._store is not None:
            for track in self._tracks:
                try:
                    self._store.add_video_track(self._session_id, track.as_dict())
                except Exception as e:
                    logger.warning("store.add_video_track failed: %s", e)

        # Launch watchdog
        self._stop_evt.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            name=f"video-watchdog-{self._session_id}",
            daemon=True,
        )
        self._watchdog.start()

    def pause(self) -> None:
        """Stop each running capture process to avoid recording during pause.

        Track objects are preserved; status remains 'recording' (paused state
        is implicit — the process is gone but track is not marked failed).
        Full segment-concat is a later story.  Never raises.
        """
        for track in self._tracks:
            if track.status == "recording" and track.proc is not None:
                try:
                    self._graceful_stop_proc(track.proc)
                    track.proc = None
                    logger.debug("Paused capture for %s", track.track_id)
                except Exception as e:
                    logger.warning("pause: error stopping %s: %s", track.track_id, e)
                self._kill_overlay(track)

    def resume(self) -> None:
        """Re-spawn each previously-running track into a new segment file.

        Increments the per-track segment counter; new file is
        ``video_mon{idx}_seg{n}.mp4``.  Never raises.
        """
        try:
            from app.services.recording.video_probe import build_capture_command
        except Exception as e:
            logger.error("video_probe unavailable on resume: %s", e)
            return

        for track in self._tracks:
            if track.status not in ("recording", "starting"):
                continue
            try:
                self._seg_counters[track.track_id] += 1
                n = self._seg_counters[track.track_id]
                idx = track.monitor_index
                new_path = os.path.join(
                    self._session_dir, f"video_mon{idx}_seg{n}.mp4"
                )
                track.path = new_path
                self._spawn_track(track, build_capture_command)
            except Exception as e:
                logger.warning("resume: error re-spawning %s: %s", track.track_id, e)

    def stop(self, graceful: bool = True) -> None:
        """Stop all running capture processes and join the watchdog.  Never raises."""
        self._stop_evt.set()

        for track in self._tracks:
            if track.proc is None:
                track.status = "finalized"
                self._kill_overlay(track)
                continue
            try:
                if graceful:
                    self._graceful_stop_proc(track.proc)
                else:
                    try:
                        track.proc.terminate()
                    except Exception:
                        pass
                    try:
                        track.proc.kill()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("stop: error stopping %s: %s", track.track_id, e)
            finally:
                track.proc = None
                track.status = "finalized"
                self._publish_status(track)
            self._kill_overlay(track)

        # Join watchdog
        if self._watchdog is not None:
            try:
                self._watchdog.join(timeout=self._stop_timeout + 2.0)
            except Exception as e:
                logger.debug("watchdog join error: %s", e)

        # Update manifest
        if self._store is not None:
            for track in self._tracks:
                try:
                    self._store.add_video_track(self._session_id, track.as_dict())
                except Exception as e:
                    logger.warning("store.add_video_track (stop) failed: %s", e)

    def status_snapshot(self) -> List[Dict[str, Any]]:
        """Return a JSON-serialisable list of all track states."""
        return [t.as_dict() for t in self._tracks]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spawn_track(self, track: VideoTrack, build_capture_command) -> None:
        """Spawn the ffmpeg process for one track.  Sets status and proc.

        Propagates exceptions to caller, which wraps in try/except.
        """
        argv = build_capture_command(
            output_idx=track.output_idx,
            out_path=track.path,
            fps=track.fps,
            codec=track.codec,
            quality=self._default_quality,
            cq=self._default_cq,
            ffmpeg=self._ffmpeg_path,
            region=track.region,
        )
        now = time.time()
        track.start_wallclock = now
        track.start_offset_sec = now - self._audio_start_wallclock

        stderr_q: queue.Queue = queue.Queue(maxsize=500)
        self._stderr_queues[track.track_id] = stderr_q

        proc = self._popen_factory(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True,
            creationflags=NO_WINDOW,
        )
        track.proc = proc
        track.status = "recording"
        track.error = None

        # Per-process stderr reader thread
        t = threading.Thread(
            target=self._stderr_reader,
            args=(proc, stderr_q, track.track_id),
            name=f"stderr-{track.track_id}",
            daemon=True,
        )
        t.start()
        self._stderr_threads.append(t)

        self._publish_status(track)
        logger.info(
            "Started video capture %s → %s (offset %.2fs)",
            track.track_id, track.path, track.start_offset_sec,
        )
        self._spawn_overlay(track)

    # ------------------------------------------------------------------
    # Overlay helpers (best-effort cosmetic sidecar, NEVER raises)
    # ------------------------------------------------------------------

    def _overlay_argv(self, track: VideoTrack) -> list:
        """Build argv for the region_overlay subprocess."""
        x = track.monitor_pos_x + int(track.region['x'])
        y = track.monitor_pos_y + int(track.region['y'])
        w = int(track.region['w'])
        h = int(track.region['h'])
        return [
            sys.executable, '-m', 'app.services.recording.region_overlay',
            '--x', str(x),
            '--y', str(y),
            '--w', str(w),
            '--h', str(h),
            '--label', f"{w}\xd7{h}",
            '--color', self._overlay_color,
        ]

    def _spawn_overlay(self, track: VideoTrack) -> None:
        """Spawn the overlay subprocess for a region track.  NEVER raises."""
        if not (self._region_overlay and track.mode == 'region' and track.region):
            return
        try:
            track.overlay_proc = self._overlay_factory(
                self._overlay_argv(track),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        except Exception as e:
            logger.warning("_spawn_overlay failed for %s (cosmetic, ignored): %s",
                           track.track_id, e)
            track.overlay_proc = None

    def _kill_overlay(self, track: VideoTrack) -> None:
        """Kill the overlay subprocess if running.  NEVER raises."""
        if track.overlay_proc is None:
            return
        try:
            track.overlay_proc.terminate()
        except Exception:
            pass
        try:
            if track.overlay_proc.poll() is None:
                track.overlay_proc.kill()
        except Exception:
            pass
        track.overlay_proc = None

    @staticmethod
    def _stderr_reader(proc: subprocess.Popen, q: queue.Queue, track_id: str) -> None:
        """Reads lines from proc.stderr and enqueues them."""
        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                if line:
                    try:
                        q.put_nowait(line)
                    except queue.Full:
                        # Drop oldest to make room
                        try:
                            q.get_nowait()
                            q.put_nowait(line)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug("stderr_reader(%s) exiting: %s", track_id, e)

    def _drain_stderr(self, track: VideoTrack) -> List[str]:
        """Drain all pending lines from the track's stderr queue."""
        q = self._stderr_queues.get(track.track_id)
        if q is None:
            return []
        lines = []
        while True:
            try:
                lines.append(q.get_nowait())
            except queue.Empty:
                break
        return lines

    def _watchdog_loop(self) -> None:
        """Background daemon: polls track health and publishes stats every ~1s."""
        try:
            while not self._stop_evt.wait(timeout=1.0):
                for track in self._tracks:
                    if track.status not in ("recording", "starting"):
                        continue
                    self._tick_track(track)
        except Exception as e:
            logger.error("watchdog_loop terminated unexpectedly: %s", e)

    def _tick_track(self, track: VideoTrack) -> None:
        """Single watchdog tick for one track."""
        try:
            lines = self._drain_stderr(track)

            # Parse progress from the most recent matching line
            for line in reversed(lines):
                m = _FFMPEG_PROGRESS_RE.search(line)
                if m:
                    try:
                        file_bytes = os.path.getsize(track.path)
                    except OSError:
                        file_bytes = 0
                    dm = _FFMPEG_DROP_RE.search(line)
                    track.last_stats = {
                        "fps":     float(m.group(2)),
                        "dropped": int(dm.group(1)) if dm else 0,
                        "bytes":   file_bytes,
                    }
                    self._publish_stats(track)
                    self._maybe_persist_stats(track)
                    break

            # Check if process died unexpectedly
            if track.proc is not None and track.proc.poll() is not None:
                if track.status == "recording":
                    stderr_tail = "\n".join(lines[-40:]) if lines else ""
                    self._handle_unexpected_exit(track, stderr_tail)
        except Exception as e:
            logger.debug("_tick_track(%s) error: %s", track.track_id, e)

    def _handle_unexpected_exit(self, track: VideoTrack, stderr_tail: str) -> None:
        """Handle a process that died while status was 'recording'."""
        uptime = time.time() - track.start_wallclock
        is_nvenc_limit = uptime < 4.0 and any(
            pat in stderr_tail for pat in _NVENC_LIMIT_PATTERNS
        )

        if is_nvenc_limit and self._caps.get("nvenc_hevc") and track.codec != "hevc_nvenc":
            logger.warning(
                "NVENC session limit hit for %s (uptime %.1fs) — retrying with hevc_nvenc",
                track.track_id, uptime,
            )
            track.codec = "hevc_nvenc"
            try:
                from app.services.recording.video_probe import build_capture_command
                self._spawn_track(track, build_capture_command)
                return  # Successfully retried
            except Exception as e:
                logger.error("hevc_nvenc retry failed for %s: %s", track.track_id, e)
                track.status = "failed"
                track.error = f"hevc_nvenc retry failed: {e}"
        else:
            reason = "NVENC session limit (no hevc fallback)" if is_nvenc_limit else "process exited unexpectedly"
            if stderr_tail:
                # Extract last non-empty line as reason
                for ln in reversed(stderr_tail.split("\n")):
                    if ln.strip():
                        reason = ln.strip()
                        break
            track.status = "failed"
            track.error = reason
            logger.error(
                "Video capture %s died (uptime %.1fs): %s",
                track.track_id, uptime, reason,
            )

        self._publish_status(track)

    def _graceful_stop_proc(self, proc: subprocess.Popen) -> None:
        """Send 'q\\n' to ffmpeg stdin, wait, then escalate to terminate/kill."""
        try:
            proc.stdin.write("q\n")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=self._stop_timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # SSE helpers
    # ------------------------------------------------------------------

    def _publish_status(self, track: VideoTrack) -> None:
        """Publish video_status event (never 'error' — that closes SSE stream)."""
        if self._broker is None:
            return
        try:
            self._broker.publish(
                f"recording:{self._session_id}",
                "video_status",
                track.as_dict(),
            )
        except Exception as e:
            logger.debug("_publish_status error: %s", e)

    def _maybe_persist_stats(self, track: VideoTrack) -> None:
        """Persist last_stats to the manifest, throttled to ~5s per track.

        Keeps get_state()/reload accurate (live fps/bytes survive a page
        reload) without writing the manifest on every 1s watchdog tick.
        Best-effort: any store error is swallowed and never affects capture.
        Final stats are also persisted on stop via add_video_track(as_dict()).
        """
        if self._store is None:
            return
        now = time.time()
        if now - self._last_stats_persist.get(track.track_id, 0.0) < 5.0:
            return
        self._last_stats_persist[track.track_id] = now
        try:
            self._store.update_video_track(
                self._session_id, track.track_id, last_stats=dict(track.last_stats)
            )
        except Exception as e:
            logger.debug("persist last_stats failed for %s: %s", track.track_id, e)

    def _publish_stats(self, track: VideoTrack) -> None:
        """Publish video_stats event."""
        if self._broker is None:
            return
        try:
            self._broker.publish(
                f"recording:{self._session_id}",
                "video_stats",
                {
                    "track_id": track.track_id,
                    "stats": track.last_stats,
                },
            )
        except Exception as e:
            logger.debug("_publish_stats error: %s", e)


# ---------------------------------------------------------------------------
# Demo CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    parser = argparse.ArgumentParser(
        description="Demo: record one monitor for N seconds via VideoCaptureSupervisor"
    )
    parser.add_argument("--monitor", type=int, default=0, help="DXGI output index (default 0)")
    parser.add_argument("--seconds", type=int, default=8, help="Recording duration (default 8)")
    parser.add_argument("--out", default="./video_demo.mp4", help="Output file path")
    parser.add_argument("--ffmpeg", default=None, help="Path to ffmpeg (default: search PATH)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        stream=sys.stdout,
    )

    # Resolve ffmpeg path
    try:
        from app.services.recording.video_probe import ffmpeg_path as _ffmpeg_path, probe_capabilities
        _ff = args.ffmpeg or _ffmpeg_path()
        _caps = probe_capabilities(ffmpeg=_ff)
    except Exception as e:
        print(f"[demo] video_probe unavailable ({e}); using 'ffmpeg' from PATH")
        _ff = args.ffmpeg or "ffmpeg"
        _caps = {}

    tracks_spec = [
        {
            "monitor_index": args.monitor,
            "output_idx":    args.monitor,
            "monitor_label": f"Monitor {args.monitor}",
            "fps":           30,
            "codec":         "h264_nvenc",
        }
    ]

    audio_start = time.time()
    sup = VideoCaptureSupervisor(
        session_id="demo_session",
        session_dir=os.path.dirname(os.path.abspath(args.out)) or ".",
        tracks_spec=tracks_spec,
        ffmpeg_path=_ff,
        audio_start_wallclock=audio_start,
        caps=_caps,
        broker=None,
        store=None,
    )

    # Override the track path to the user-supplied --out
    if sup._tracks:
        sup._tracks[0].path = os.path.abspath(args.out)

    print(f"[demo] Starting capture → {os.path.abspath(args.out)}")
    sup.start()

    for i in range(args.seconds):
        time.sleep(1)
        snap = sup.status_snapshot()
        for t in snap:
            stats = t.get("last_stats", {})
            print(
                f"  t+{i+1:02d}s  {t['track_id']}  status={t['status']}"
                f"  fps={stats.get('fps', '?')}  dropped={stats.get('dropped', '?')}"
                f"  bytes={stats.get('bytes', '?')}"
            )

    print("[demo] Stopping...")
    sup.stop(graceful=True)

    for t in sup.status_snapshot():
        out_path = t["path"]
        try:
            size = os.path.getsize(out_path)
        except OSError:
            size = 0
        print(
            f"[demo] DONE  track={t['track_id']}  status={t['status']}"
            f"  path={out_path}  size={size} bytes"
        )
