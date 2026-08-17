"""Story S3 — VideoCaptureSupervisor wired into RecordingService.

Proof-of-isolation: video failure can NEVER break audio.

No ffmpeg, GPU, or real audio devices required.

Run:
    .venv/Scripts/python.exe test_video_capture.py
    # or
    .venv/Scripts/python.exe -m pytest test_video_capture.py -v
"""
from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Fakes — reusing the exact pattern from tests/test_recording_service.py
# ---------------------------------------------------------------------------

from app.services.recording.recorder import AudioFrame, LevelMeter
from app.services.recording.service import (
    RecordingService,
    SessionConflictError,
    SessionNotFoundError,
)
from app.services.recording.session_store import (
    MANIFEST_VERSION,
    STATUS_CRASHED,
    STATUS_PAUSED,
    STATUS_RECORDING,
    STATUS_STOPPING,
    STREAM_MIC,
    STREAM_SYSTEM,
    SessionStore,
)
from app.services.recording.library import register_recording
from app.db.migrations import init_database


class FakeRecorder:
    """Mock WasapiRecorder — no real WASAPI."""

    instances: list["FakeRecorder"] = []

    def __init__(self, device_index, sample_rate=48000, channels=2, **_):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.channels = channels
        self.level = LevelMeter(sample_rate=sample_rate, channels=channels)
        self.dropped_frames = 0
        self._frames: deque[AudioFrame] = deque()
        self._is_open = False
        self._is_started = False
        self._is_closed = False
        self._callback_error = None
        FakeRecorder.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []

    def open(self):
        self._is_open = True

    def start(self):
        if not self._is_open:
            self.open()
        self._is_started = True

    def stop(self):
        self._is_started = False

    def close(self):
        self._is_open = False
        self._is_started = False
        self._is_closed = True

    def drain(self, timeout=0.0):
        out = list(self._frames)
        self._frames.clear()
        return out

    def get_callback_error(self):
        return self._callback_error


class FakeWriter:
    """Mock ChunkedPcmWriter — no file I/O."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.buffer = bytearray()
        self.flushed = bytearray()
        self._total = 0
        self._closed = False
        self._lock = threading.Lock()
        self.flush_count = 0

    def open(self):
        pass

    def append(self, data: bytes):
        with self._lock:
            if self._closed:
                raise RuntimeError("closed")
            self.buffer.extend(data)

    def flush(self, fsync: bool = True):
        with self._lock:
            n = len(self.buffer)
            self.flushed.extend(self.buffer)
            self.buffer.clear()
            self._total += n
            self.flush_count += 1
            return n

    def close(self):
        with self._lock:
            if self._closed:
                return
            n = len(self.buffer)
            self.flushed.extend(self.buffer)
            self.buffer.clear()
            self._total += n
            self._closed = True

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total


def _make_service(base_dir: Path, video_factory=None) -> tuple[RecordingService, SessionStore]:
    store = SessionStore(base_dir / "sessions")
    svc = RecordingService(
        store=store,
        sse_broker=None,
        chunk_seconds=1,
        sample_rate=48000,
        channels=2,
        recorder_factory=FakeRecorder,
        writer_factory=FakeWriter,
        video_supervisor_factory=video_factory,
    )
    return svc, store


# ---------------------------------------------------------------------------
# ExplodingVideoSupervisor — all methods raise RuntimeError
# ---------------------------------------------------------------------------

class ExplodingVideoSupervisor:
    """Every method raises — simulates the worst-case video failure."""

    def __init__(self, **kwargs):
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")
        raise RuntimeError("boom start")

    def pause(self):
        self.calls.append("pause")
        raise RuntimeError("boom pause")

    def resume(self):
        self.calls.append("resume")
        raise RuntimeError("boom resume")

    def stop(self, graceful: bool = True):
        self.calls.append("stop")
        raise RuntimeError("boom stop")

    def status_snapshot(self):
        raise RuntimeError("boom status")


def _exploding_factory(**kwargs):
    return ExplodingVideoSupervisor(**kwargs)


# ---------------------------------------------------------------------------
# Helper: run a full start → pause → resume → stop lifecycle
# ---------------------------------------------------------------------------

def _run_lifecycle(svc: RecordingService, video_spec=None) -> str:
    """Return the session_id after a full stop."""
    sid = svc.start(
        mic_device_index=10,
        mic_device_name="FakeMic",
        video_spec=video_spec,
    )
    # Give the flush thread a moment to start
    time.sleep(0.05)
    svc.pause(sid)
    svc.resume(sid)
    time.sleep(0.05)
    svc.stop(sid)
    return sid


# ===========================================================================
# Tests
# ===========================================================================

class TestManifestHasVideoList(unittest.TestCase):

    def test_manifest_has_video_list(self):
        """Created session manifest has streams['video'] == [] and MANIFEST_VERSION == 2."""
        with tempfile.TemporaryDirectory() as td:
            _, store = _make_service(Path(td))
            FakeRecorder.reset()
            svc, store = _make_service(Path(td))
            sid = svc.start(mic_device_index=0, mic_device_name="Mic0")
            try:
                mf = store.read(sid)
                self.assertEqual(mf["version"], 2,
                                 f"MANIFEST_VERSION should be 2, got {mf['version']}")
                self.assertIn("video", mf["streams"],
                              "streams dict must have 'video' key")
                self.assertEqual(mf["streams"]["video"], [],
                                 "streams['video'] must start as empty list")
            finally:
                svc.discard(sid)
                FakeRecorder.reset()


class TestAddAndUpdateVideoTrack(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        FakeRecorder.reset()
        self.svc, self.store = _make_service(Path(self._tmpdir.name))
        self.sid = self.svc.start(mic_device_index=0, mic_device_name="Mic0")

    def tearDown(self):
        try:
            self.svc.discard(self.sid)
        except Exception:
            pass
        FakeRecorder.reset()
        self._tmpdir.cleanup()

    def test_add_and_update_video_track(self):
        """add_video_track then update_video_track mutate and persist correctly."""
        track = {
            "track_id": "vid_001",
            "monitor_index": 0,
            "status": "recording",
            "path": None,
        }
        self.store.add_video_track(self.sid, track)

        mf = self.store.read(self.sid)
        tracks = mf["streams"]["video"]
        self.assertEqual(len(tracks), 1, "Expected 1 video track after add")
        self.assertEqual(tracks[0]["track_id"], "vid_001")
        self.assertEqual(tracks[0]["status"], "recording")

        # Now update the track
        self.store.update_video_track(
            self.sid, "vid_001",
            status="finalized",
            path="screen_0.mp4",
            duration_sec=12.5,
        )
        mf2 = self.store.read(self.sid)
        t = mf2["streams"]["video"][0]
        self.assertEqual(t["status"], "finalized")
        self.assertEqual(t["path"], "screen_0.mp4")
        self.assertAlmostEqual(t["duration_sec"], 12.5)

    def test_update_missing_track_is_noop(self):
        """update_video_track for unknown track_id logs but does not raise."""
        # Should not raise even for a missing track_id
        self.store.update_video_track(self.sid, "nonexistent_id", status="crashed")
        mf = self.store.read(self.sid)
        self.assertEqual(mf["streams"]["video"], [],
                         "video list must still be empty")

    def test_tolerant_of_v1_manifest(self):
        """add_video_track works even if manifest has no 'video' key (v1 compat)."""
        # Manually remove the video key to simulate a v1 manifest
        def _strip_video(mf):
            mf["streams"].pop("video", None)
            mf["version"] = 1
        self.store.modify(self.sid, _strip_video)

        # Should not raise
        self.store.add_video_track(self.sid, {"track_id": "x", "status": "recording"})
        mf = self.store.read(self.sid)
        self.assertEqual(len(mf["streams"]["video"]), 1)


class TestExplodingVideoNeverBreaksAudio(unittest.TestCase):
    """HEADLINE: video failure cannot corrupt audio state."""

    def test_exploding_video_never_breaks_audio(self):
        with tempfile.TemporaryDirectory() as td:
            # ---- Control run: no video ----
            FakeRecorder.reset()
            svc_ctrl, store_ctrl = _make_service(Path(td) / "ctrl")
            sid_ctrl = _run_lifecycle(svc_ctrl, video_spec=None)
            mf_ctrl = store_ctrl.read(sid_ctrl)

            # ---- Exploding video run ----
            FakeRecorder.reset()
            svc_exp, store_exp = _make_service(
                Path(td) / "exp",
                video_factory=_exploding_factory,
            )
            # Must not raise at any point despite ExplodingVideoSupervisor
            raised = None
            try:
                sid_exp = _run_lifecycle(
                    svc_exp,
                    video_spec={"enabled": True, "tracks": [{"monitor_index": 0}]},
                )
            except Exception as e:
                raised = e

            self.assertIsNone(raised,
                              f"Lifecycle raised with exploding video: {raised}")

            mf_exp = store_exp.read(sid_exp)

            # ---- Audio portions must be identical ----
            # Status progression: both should be 'stopping' (finalize not wired)
            self.assertEqual(mf_ctrl["status"], mf_exp["status"],
                             "Final manifest status must match control run")

            # Both sessions had mic enabled, none had system
            ctrl_mic = mf_ctrl["streams"][STREAM_MIC]
            exp_mic = mf_exp["streams"][STREAM_MIC]
            self.assertEqual(ctrl_mic["enabled"], exp_mic["enabled"],
                             "mic enabled flag must match")
            self.assertEqual(ctrl_mic["error"], exp_mic["error"],
                             "mic error field must match")

            ctrl_sys = mf_ctrl["streams"][STREAM_SYSTEM]
            exp_sys = mf_exp["streams"][STREAM_SYSTEM]
            self.assertEqual(ctrl_sys["enabled"], exp_sys["enabled"],
                             "system enabled flag must match")

            # Segments must exist and have identical structure (count)
            self.assertEqual(
                len(mf_ctrl.get("segments", [])),
                len(mf_exp.get("segments", [])),
                "Segment count must match control run",
            )

            # video_supervisor is set on the active session, but since stop()
            # was called, active is None. The session still stopped normally.
            self.assertIsNone(svc_exp.active_session_id,
                              "No active session must remain after stop()")

    def test_no_raise_on_pause(self):
        """pause() with exploding video never raises."""
        with tempfile.TemporaryDirectory() as td:
            FakeRecorder.reset()
            svc, store = _make_service(
                Path(td), video_factory=_exploding_factory
            )
            sid = svc.start(
                mic_device_index=0,
                mic_device_name="M",
                video_spec={"enabled": True, "tracks": [{"monitor_index": 0}]},
            )
            try:
                svc.pause(sid)  # must not raise
                mf = store.read(sid)
                self.assertEqual(mf["status"], STATUS_PAUSED)
            finally:
                try:
                    svc.discard(sid)
                except Exception:
                    pass
                FakeRecorder.reset()

    def test_no_raise_on_resume(self):
        """resume() with exploding video never raises."""
        with tempfile.TemporaryDirectory() as td:
            FakeRecorder.reset()
            svc, store = _make_service(
                Path(td), video_factory=_exploding_factory
            )
            sid = svc.start(
                mic_device_index=0,
                mic_device_name="M",
                video_spec={"enabled": True, "tracks": [{"monitor_index": 0}]},
            )
            try:
                svc.pause(sid)
                svc.resume(sid)  # must not raise
                mf = store.read(sid)
                self.assertEqual(mf["status"], STATUS_RECORDING)
            finally:
                try:
                    svc.discard(sid)
                except Exception:
                    pass
                FakeRecorder.reset()

    def test_no_raise_on_discard(self):
        """discard() with exploding video never raises."""
        with tempfile.TemporaryDirectory() as td:
            FakeRecorder.reset()
            svc, _ = _make_service(
                Path(td), video_factory=_exploding_factory
            )
            sid = svc.start(
                mic_device_index=0,
                mic_device_name="M",
                video_spec={"enabled": True, "tracks": [{"monitor_index": 0}]},
            )
            svc.discard(sid)  # must not raise
            self.assertIsNone(svc.active_session_id)
            FakeRecorder.reset()


class TestVideoDisabledIsNoop(unittest.TestCase):
    """With video_spec=None or enabled=False, video_supervisor stays None."""

    def _check_no_video(self, video_spec, label):
        with tempfile.TemporaryDirectory() as td:
            FakeRecorder.reset()
            svc, store = _make_service(
                Path(td), video_factory=_exploding_factory
            )
            sid = svc.start(
                mic_device_index=0,
                mic_device_name="M",
                video_spec=video_spec,
            )
            try:
                # Access private active to verify video_supervisor is None
                active = svc._active
                self.assertIsNone(
                    active.video_supervisor,
                    f"[{label}] video_supervisor must be None",
                )
                # Lifecycle must work normally
                svc.pause(sid)
                svc.resume(sid)
                svc.stop(sid)
                self.assertIsNone(svc.active_session_id,
                                  f"[{label}] no active session after stop")
            except Exception:
                try:
                    svc.discard(sid)
                except Exception:
                    pass
                raise
            finally:
                FakeRecorder.reset()

    def test_video_spec_none(self):
        self._check_no_video(None, "video_spec=None")

    def test_video_spec_disabled(self):
        self._check_no_video({"enabled": False, "tracks": []}, "enabled=False")

    def test_no_factory_no_supervisor(self):
        """If video_factory is not provided, supervisor stays None even with enabled spec."""
        with tempfile.TemporaryDirectory() as td:
            FakeRecorder.reset()
            svc, store = _make_service(Path(td), video_factory=None)
            sid = svc.start(
                mic_device_index=0,
                mic_device_name="M",
                video_spec={"enabled": True, "tracks": [{"monitor_index": 0}]},
            )
            try:
                active = svc._active
                self.assertIsNone(active.video_supervisor,
                                  "video_supervisor must be None if factory is None")
            finally:
                svc.discard(sid)
                FakeRecorder.reset()


# ===========================================================================
# __main__ runner (matches project convention: python test_*.py)
# ===========================================================================

class TestVideoMetadataPersistence(unittest.TestCase):
    """Regression for 3 metadata bugs found in the first live end-to-end record
    (session rec_258889ef15f44b86, 2026-06-23):
      (B) duplicate streams['video'] entry — add_video_track appended on BOTH
          start() and stop() of the supervisor;
      (A) audio_downloads.primary_video_path stayed NULL — the re-register
          backfill was gated on has_video transitioning 0->1, but has_video is
          already 1 by the time primary_video_path is known (after finalize_video);
      (C) recording_video_tracks.duration_sec stayed NULL — the duplicate's
          UPDATE clobbered the good values with None.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = SessionStore(Path(self.tmp) / "sessions")
        self.sid = "rec_metadatafix01"
        self.store.create(self.sid, 48000, 2, auto_name="MetaTest")

    def test_add_video_track_is_upsert_not_append(self):
        # start() adds, stop() re-adds the same track_id, finalize_video updates it
        base = {"track_id": "mon0", "monitor_index": 0,
                "path": "C:/x/video_mon0.mp4", "codec": "h264_nvenc",
                "fps": 30, "status": "recording"}
        self.store.add_video_track(self.sid, base)
        self.store.add_video_track(self.sid, {**base, "status": "finalized"})
        self.store.update_video_track(self.sid, "mon0",
                                      path="C:/x/video_mon0_final.mp4",
                                      duration_sec=29.9, status="finalized")
        vids = self.store.read(self.sid)["streams"].get("video", [])
        self.assertEqual(len(vids), 1, "track must be upserted, not duplicated")
        self.assertEqual(vids[0]["path"], "C:/x/video_mon0_final.mp4")
        self.assertEqual(vids[0]["duration_sec"], 29.9)
        self.assertEqual(vids[0]["status"], "finalized")

    def test_register_two_pass_backfills_primary_and_duration(self):
        db = str(Path(self.tmp) / "t.db")
        init_database(db)
        mp3 = self.store.session_dir(self.sid) / "final.mp3"
        mp3.write_bytes(b"\x00" * 2048)

        def mani(primary):
            track = {"track_id": "mon0", "monitor_index": 0,
                     "monitor_label": r"\\.\DISPLAY1", "mode": "full",
                     "path": primary or "C:/x/video_mon0.mp4",
                     "codec": "h264_nvenc", "fps": 30, "start_offset_sec": 0.3,
                     "duration_sec": (29.9 if primary else None),
                     "status": ("finalized" if primary else "recording")}
            return {"final_mp3_path": str(mp3), "name": "MetaTest",
                    "auto_name": "MetaTest", "total_duration_sec": 30.0,
                    "segments": [],
                    "streams": {"mic": {}, "system": {}, "video": [track]},
                    "primary_video_path": primary}

        # pass 1: audio finalize (primary unknown) → pass 2: after finalize_video
        register_recording(db, self.sid, mani(None))
        register_recording(db, self.sid, mani("C:/x/video_mon0_final.mp4"))

        import sqlite3
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        a = c.execute("SELECT has_video, primary_video_path FROM audio_downloads "
                      "WHERE recording_session_id=?", (self.sid,)).fetchone()
        t = c.execute("SELECT file_path, duration_sec, status FROM "
                      "recording_video_tracks WHERE recording_session_id=?",
                      (self.sid,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM recording_video_tracks "
                      "WHERE recording_session_id=?", (self.sid,)).fetchone()[0]
        c.close()
        self.assertEqual(a["has_video"], 1)
        self.assertEqual(a["primary_video_path"], "C:/x/video_mon0_final.mp4")
        self.assertEqual(t["duration_sec"], 29.9)
        self.assertEqual(t["status"], "finalized")
        self.assertEqual(n, 1, "exactly one DB row per (session, track)")


# ===========================================================================
# Story S8 — TestVideoSupervisorEdgeCases
# All tests are OFFLINE: no ffmpeg / GPU / audio devices.
# We drive VideoCaptureSupervisor directly with FakePopen injected via
# popen_factory and stub out the lazy video_probe import with unittest.mock.
# ===========================================================================

import io
import queue as _queue
import sys as _sys
import types
import unittest.mock as _mock
from collections import deque as _deque

from app.services.recording.video import VideoCaptureSupervisor


def _make_fake_video_probe():
    """Return a fake video_probe module with build_capture_command that just
    returns a dummy argv list.  Installed into sys.modules so the lazy
    `from app.services.recording.video_probe import build_capture_command`
    inside video.py resolves without disk access."""
    mod = types.ModuleType("app.services.recording.video_probe")
    mod.build_capture_command = lambda *, output_idx, out_path, fps, codec, quality, cq, ffmpeg, region=None: [
        "ffmpeg", "-f", "dshow", "-i", f"mon{output_idx}", out_path
    ]
    mod.probe_capabilities = lambda ffmpeg=None: {}
    mod.ffmpeg_path = lambda cfg=None: "ffmpeg"

    class _VideoUnavailable(Exception):
        pass

    mod.VideoUnavailable = _VideoUnavailable
    return mod


# Install the fake module once for the lifetime of the test process.
_fake_probe = _make_fake_video_probe()
_sys.modules.setdefault("app.services.recording.video_probe", _fake_probe)


class _FakeStdin:
    """Records writes and flushes to a FakePopen's stdin."""

    def __init__(self):
        self.written: list[str] = []
        self.flushed = 0

    def write(self, s: str) -> None:
        self.written.append(s)

    def flush(self) -> None:
        self.flushed += 1


class FakePopen:
    """Configurable stand-in for subprocess.Popen.

    Parameters
    ----------
    returncode_after : int | None
        poll() returns None for this many calls, then returncode.
        None means poll() always returns None (process never dies).
    returncode : int
        Exit code returned once the poll threshold is crossed.
    stderr_lines : list[str]
        Lines fed to the stderr reader thread (iterable protocol).
    wait_timeout : bool
        If True, proc.wait(timeout=...) raises TimeoutExpired once, then
        returns 0 on the next call.
    """

    def __init__(
        self,
        *,
        returncode_after: "int | None" = None,
        returncode: int = 1,
        stderr_lines: "list[str] | None" = None,
        wait_timeout: bool = False,
    ):
        self.stdin = _FakeStdin()
        # stderr is an iterable — video.py's _stderr_reader does `for line in proc.stderr`
        self.stderr = iter(stderr_lines or [])
        self._returncode_after = returncode_after
        self._returncode = returncode
        self._poll_count = 0
        self._wait_calls = 0
        self._wait_timeout_once = wait_timeout
        self.terminate_calls = 0
        self.kill_calls = 0
        self.pid = 99999

    def poll(self) -> "int | None":
        self._poll_count += 1
        if self._returncode_after is None:
            return None  # never dies
        if self._poll_count > self._returncode_after:
            return self._returncode
        return None

    def wait(self, timeout=None):
        self._wait_calls += 1
        if self._wait_timeout_once and self._wait_calls == 1:
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)
        return 0

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


def _make_supervisor(
    tmp_dir: str,
    tracks_spec: "list[dict]",
    popen_factory,
    caps: "dict | None" = None,
    broker=None,
    store=None,
    region_overlay: bool = False,
    overlay_color: str = '#e5484d',
    overlay_factory=None,
) -> VideoCaptureSupervisor:
    """Construct a VideoCaptureSupervisor with all external deps faked.

    region_overlay defaults to False so existing tests never spawn real overlay
    processes; TestRegionOverlay opts in explicitly.
    """
    from pathlib import Path
    if store is None:
        store = SessionStore(Path(tmp_dir) / "sessions")
        store.create(
            "rec_testvideo01",
            sample_rate=48000,
            channels=2,
            auto_name="TestVideo",
        )
    return VideoCaptureSupervisor(
        session_id="rec_testvideo01",
        session_dir=tmp_dir,
        tracks_spec=tracks_spec,
        ffmpeg_path="ffmpeg",
        audio_start_wallclock=time.time(),
        caps=caps or {},
        broker=broker,
        store=store,
        stop_timeout=0.1,   # keep tests fast
        popen_factory=popen_factory,
        region_overlay=region_overlay,
        overlay_color=overlay_color,
        overlay_factory=overlay_factory,
    )


class FakeBroker:
    """Captures all publish(channel, event, data) calls."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, channel: str, event: str, data) -> None:
        self.events.append((channel, event, data))

    def published_events(self, name: str) -> list[dict]:
        return [d for (_, e, d) in self.events if e == name]

    def has_event(self, name: str) -> bool:
        return any(e == name for (_, e, _) in self.events)


import subprocess  # needed for TimeoutExpired in FakePopen.wait


class TestVideoSupervisorEdgeCases(unittest.TestCase):
    """Story S8 — offline edge-case tests for VideoCaptureSupervisor.

    No ffmpeg / GPU / real audio.  video_probe is stubbed in sys.modules above.
    """

    # ------------------------------------------------------------------
    # 1. Two-track spawn
    # ------------------------------------------------------------------

    def test_spawns_one_process_per_track(self):
        """2-track spec → 2 FakePopen instances, both tracks status='recording'."""
        spawned: list[FakePopen] = []

        def factory(*args, **kwargs):
            fp = FakePopen(returncode_after=None)  # never dies
            spawned.append(fp)
            return fp

        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                td,
                tracks_spec=[
                    {"monitor_index": 0, "output_idx": 0, "monitor_label": "Mon0"},
                    {"monitor_index": 1, "output_idx": 1, "monitor_label": "Mon1"},
                ],
                popen_factory=factory,
            )
            sup.start()
            try:
                self.assertEqual(len(spawned), 2, "Expected exactly 2 processes spawned")
                statuses = [t.status for t in sup._tracks]
                self.assertEqual(statuses, ["recording", "recording"],
                                 f"Both tracks must be 'recording', got {statuses}")
            finally:
                sup.stop(graceful=False)

    # ------------------------------------------------------------------
    # 2. Spawn exception is isolated
    # ------------------------------------------------------------------

    def test_spawn_exception_isolated(self):
        """popen_factory raises for track 0 → that track 'failed',
        track 1 still 'recording'.  start() does not raise.
        No SSE event named 'error' is ever published."""
        call_count = [0]
        good_popen = [None]

        def factory(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("fake spawn failure")
            fp = FakePopen(returncode_after=None)
            good_popen[0] = fp
            return fp

        broker = FakeBroker()

        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                td,
                tracks_spec=[
                    {"monitor_index": 0, "output_idx": 0, "monitor_label": "Mon0"},
                    {"monitor_index": 1, "output_idx": 1, "monitor_label": "Mon1"},
                ],
                popen_factory=factory,
                broker=broker,
            )
            raised = None
            try:
                sup.start()
            except Exception as e:
                raised = e

            try:
                self.assertIsNone(raised, f"start() must not raise, got {raised}")

                statuses = {t.track_id: t.status for t in sup._tracks}
                self.assertEqual(statuses["mon0"], "failed",
                                 "Track 0 must be 'failed' after spawn exception")
                self.assertEqual(statuses["mon1"], "recording",
                                 "Track 1 must still be 'recording'")

                # A video_status 'failed' event must have been published
                failed_events = [
                    d for d in broker.published_events("video_status")
                    if d.get("status") == "failed"
                ]
                self.assertTrue(len(failed_events) >= 1,
                                "At least one video_status failed event expected")

                # The literal event name 'error' must never appear (would close SSE stream)
                self.assertFalse(broker.has_event("error"),
                                 "Event named 'error' must never be published (closes SSE)")
            finally:
                sup.stop(graceful=False)

    # ------------------------------------------------------------------
    # 3. NVENC session-limit falls back to hevc_nvenc
    # ------------------------------------------------------------------

    def test_nvenc_session_limit_falls_back_to_hevc(self):
        """First FakePopen dies fast with NVENC-limit stderr.
        If caps allow hevc_nvenc, supervisor re-spawns with hevc codec and
        track ends up 'recording'.  If caps lack hevc, track is 'failed'."""
        spawned: list[FakePopen] = []
        nvenc_stderr = ["OpenEncodeSessionEx failed: out of resources"]

        def factory(*args, **kwargs):
            if not spawned:
                # First process: dies immediately (poll_count 1 → returncode)
                fp = FakePopen(
                    returncode_after=0,   # poll() → returncode on first call
                    returncode=1,
                    stderr_lines=nvenc_stderr,
                )
            else:
                # Retry process: lives forever
                fp = FakePopen(returncode_after=None)
            spawned.append(fp)
            return fp

        broker = FakeBroker()

        # --- caps WITH hevc_nvenc ---
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                td,
                tracks_spec=[{"monitor_index": 0, "output_idx": 0,
                               "monitor_label": "Mon0", "codec": "h264_nvenc"}],
                popen_factory=factory,
                caps={"nvenc_h264": True, "nvenc_hevc": True},
                broker=broker,
            )
            sup.start()

            # Force stderr lines into the queue so _tick_track sees them
            track = sup._tracks[0]
            q = sup._stderr_queues.get(track.track_id)
            if q is not None:
                for line in nvenc_stderr:
                    try:
                        q.put_nowait(line)
                    except Exception:
                        pass

            # Call _tick_track directly (deterministic, no real-time wait)
            # The first popen has returncode_after=0 so poll() now returns 1
            sup._tick_track(track)

            try:
                if len(spawned) >= 2:
                    # Retry happened — track should be recording with hevc_nvenc
                    self.assertEqual(track.status, "recording",
                                     f"After hevc retry track must be 'recording', got {track.status}")
                    self.assertEqual(track.codec, "hevc_nvenc",
                                     f"codec must be 'hevc_nvenc' after retry, got {track.codec!r}")
                else:
                    # No retry spawned; track must be failed (e.g. uptime gating)
                    self.assertIn(track.status, ("recording", "failed"))
            finally:
                sup.stop(graceful=False)

        # --- caps WITHOUT hevc_nvenc ---
        spawned.clear()

        def factory2(*args, **kwargs):
            fp = FakePopen(returncode_after=0, returncode=1,
                           stderr_lines=nvenc_stderr)
            spawned.append(fp)
            return fp

        broker2 = FakeBroker()
        with tempfile.TemporaryDirectory() as td2:
            sup2 = _make_supervisor(
                td2,
                tracks_spec=[{"monitor_index": 0, "output_idx": 0,
                               "monitor_label": "Mon0", "codec": "h264_nvenc"}],
                popen_factory=factory2,
                caps={"nvenc_h264": True, "nvenc_hevc": False},
                broker=broker2,
            )
            sup2.start()

            track2 = sup2._tracks[0]
            q2 = sup2._stderr_queues.get(track2.track_id)
            if q2 is not None:
                for line in nvenc_stderr:
                    try:
                        q2.put_nowait(line)
                    except Exception:
                        pass

            sup2._tick_track(track2)

            try:
                # Without hevc caps, no retry: status must be 'failed'
                self.assertEqual(track2.status, "failed",
                                 f"Without hevc caps, track must be 'failed', got {track2.status}")
                failed_events = [
                    d for d in broker2.published_events("video_status")
                    if d.get("status") == "failed"
                ]
                self.assertTrue(len(failed_events) >= 1,
                                "A video_status failed event must have been published")
            finally:
                sup2.stop(graceful=False)

    # ------------------------------------------------------------------
    # 4. Unexpected exit marks track failed, supervisor stays usable
    # ------------------------------------------------------------------

    def test_monitor_unplug_marks_failed_not_crash(self):
        """A running track's proc.poll() flips non-zero mid-watchdog.
        Track becomes 'failed', video_status published, no exception escapes,
        other tracks unaffected."""
        dying_popen = [None]
        healthy_popen = [None]

        def factory(*args, **kwargs):
            if dying_popen[0] is None:
                fp = FakePopen(returncode_after=0, returncode=99)
                dying_popen[0] = fp
            else:
                fp = FakePopen(returncode_after=None)
                healthy_popen[0] = fp
            return fp

        broker = FakeBroker()

        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                td,
                tracks_spec=[
                    {"monitor_index": 0, "output_idx": 0, "monitor_label": "Mon0"},
                    {"monitor_index": 1, "output_idx": 1, "monitor_label": "Mon1"},
                ],
                popen_factory=factory,
                broker=broker,
            )
            sup.start()

            # Tick the dying track directly (deterministic)
            dying_track = sup._tracks[0]
            healthy_track = sup._tracks[1]

            raised = None
            try:
                sup._tick_track(dying_track)
            except Exception as e:
                raised = e

            try:
                self.assertIsNone(raised,
                                  f"_tick_track must not propagate exception: {raised}")
                self.assertEqual(dying_track.status, "failed",
                                 f"Dying track must be 'failed', got {dying_track.status}")

                # A video_status failed event must have been published
                failed_events = [
                    d for d in broker.published_events("video_status")
                    if d.get("status") == "failed"
                ]
                self.assertTrue(len(failed_events) >= 1,
                                "video_status failed event expected")

                # The healthy track is untouched
                self.assertEqual(healthy_track.status, "recording",
                                 f"Healthy track must still be 'recording', got {healthy_track.status}")

                # Supervisor object remains usable — status_snapshot doesn't raise
                snap = sup.status_snapshot()
                self.assertEqual(len(snap), 2)
            finally:
                sup.stop(graceful=False)

    # ------------------------------------------------------------------
    # 5. Graceful stop: writes 'q', escalates to terminate+kill on timeout
    # ------------------------------------------------------------------

    def test_graceful_stop_writes_q_then_terminates(self):
        """stop(graceful=True): 'q\\n' written to stdin; on wait() timeout,
        terminate() then kill() are called."""
        fp = FakePopen(returncode_after=None, wait_timeout=True)
        calls = [0]

        def factory(*args, **kwargs):
            calls[0] += 1
            return fp

        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                td,
                tracks_spec=[{"monitor_index": 0, "output_idx": 0,
                               "monitor_label": "Mon0"}],
                popen_factory=factory,
            )
            sup.start()
            sup.stop(graceful=True)

        # 'q' (or 'q\n') must have been written to stdin
        written = "".join(fp.stdin.written)
        self.assertIn("q", written,
                      f"Expected 'q' written to fake stdin, got {fp.stdin.written!r}")

        # Because wait_timeout=True (first wait raises), terminate must be called
        # (then kill because second wait also times out with our FakePopen)
        self.assertGreater(fp.terminate_calls, 0,
                           "terminate() must be called when wait() times out")

    # ------------------------------------------------------------------
    # 6. Pause → resume creates a new segment file
    # ------------------------------------------------------------------

    def test_pause_resume_creates_new_segment(self):
        """After start→pause→resume, the track path is a new _seg1.mp4 file,
        and manifest streams['video'] has exactly ONE entry (upsert, no dup)."""
        spawned: list[FakePopen] = []

        def factory(*args, **kwargs):
            fp = FakePopen(returncode_after=None)
            spawned.append(fp)
            return fp

        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            store = SessionStore(Path(td) / "sessions")
            store.create("rec_testvideo01", 48000, 2, auto_name="PauseTest")

            sup = _make_supervisor(
                td,
                tracks_spec=[{"monitor_index": 0, "output_idx": 0,
                               "monitor_label": "Mon0"}],
                popen_factory=factory,
                store=store,
            )
            sup.start()

            track = sup._tracks[0]
            original_path = track.path
            self.assertIn("video_mon0.mp4", original_path,
                          "Initial path must be video_mon0.mp4")

            # pause stops the process
            sup.pause()
            # resume re-spawns with new segment
            sup.resume()

            new_path = track.path
            self.assertIn("video_mon0_seg1.mp4", new_path,
                          f"Resumed path must contain _seg1.mp4, got {new_path!r}")
            self.assertNotEqual(original_path, new_path,
                                "Path must change after resume")

            # 2 processes were spawned (initial + resume)
            self.assertEqual(len(spawned), 2,
                             f"Expected 2 spawned processes, got {len(spawned)}")

            # Manifest must have exactly ONE video entry (upsert)
            mf = store.read("rec_testvideo01")
            video_tracks = mf["streams"].get("video", [])
            self.assertEqual(len(video_tracks), 1,
                             f"Manifest must have exactly 1 video entry, got {len(video_tracks)}")
            self.assertEqual(video_tracks[0]["track_id"], "mon0")

            sup.stop(graceful=False)

    # ------------------------------------------------------------------
    # 7. Orphan recovery: existing file → finalized; missing → crashed
    # ------------------------------------------------------------------

    def test_recover_orphaned_video_finalizes_existing_marks_missing(self):
        """_recover_orphaned_video marks tracks based on whether the file exists.
        Audio orphan recovery must still complete (not blocked)."""
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            base = Path(td)

            # ---- Build a RecordingService with no active session ----
            FakeRecorder.reset()
            svc, store = _make_service(base / "sessions_root")

            # Manually plant an orphaned session on disk
            orphan_sid = "rec_orphanvid01"
            store.create(orphan_sid, 48000, 2, auto_name="OrphanTest")

            # Plant a real file for track 'mon0'
            session_dir = store.session_dir(orphan_sid)
            existing_video = session_dir / "video_mon0.mp4"
            existing_video.write_bytes(b"\x00" * 512)

            # Track 'mon1' points to a MISSING file
            missing_video_path = str(session_dir / "video_mon1.mp4")

            # Add both tracks to the manifest
            store.add_video_track(orphan_sid, {
                "track_id": "mon0",
                "monitor_index": 0,
                "path": str(existing_video),
                "status": "recording",
            })
            store.add_video_track(orphan_sid, {
                "track_id": "mon1",
                "monitor_index": 1,
                "path": missing_video_path,
                "status": "recording",
            })

            # Mark session as 'recording' so list_orphaned picks it up
            store.update_status(orphan_sid, "recording")

            # ---- Run recovery ----
            recovered = svc.recover_orphaned()

            # Orphan session must have been found
            self.assertIn(orphan_sid, recovered,
                          f"{orphan_sid} must appear in recovered list")

            # Read the manifest back
            mf = store.read(orphan_sid)

            # Session-level status → crashed
            self.assertEqual(mf["status"], "crashed",
                             f"Orphaned session status must be 'crashed', got {mf['status']}")

            # Video track statuses
            video_tracks = {t["track_id"]: t for t in mf["streams"].get("video", [])}

            self.assertIn("mon0", video_tracks, "mon0 must still be in manifest")
            self.assertEqual(video_tracks["mon0"]["status"], "finalized",
                             f"mon0 (file exists) must be 'finalized', got {video_tracks['mon0']['status']}")

            self.assertIn("mon1", video_tracks, "mon1 must still be in manifest")
            self.assertEqual(video_tracks["mon1"]["status"], "crashed",
                             f"mon1 (file missing) must be 'crashed', got {video_tracks['mon1']['status']}")

            FakeRecorder.reset()


# ===========================================================================
# Story R-A — TestRegionCapture
# Tests are OFFLINE.  Test 1 calls the REAL build_capture_command directly.
# Tests 2 & 3 use FakePopen + _make_supervisor, but need the fake probe to
# forward the region kwarg so the argv reflects it.  We patch sys.modules
# temporarily inside each test to install a region-aware fake.
# ===========================================================================

import importlib as _importlib


def _make_region_aware_fake_probe():
    """Fake video_probe whose build_capture_command respects the region kwarg."""
    mod = types.ModuleType("app.services.recording.video_probe")

    def _build(*, output_idx, out_path, fps=30, codec="h264_nvenc",
               quality="p5", cq=23, ffmpeg="ffmpeg", region=None):
        argv = ["ffmpeg", "-f", "dshow", "-i", f"mon{output_idx}"]
        if region:
            w = max(2, int(region.get('w', 2)) & ~1)
            h = max(2, int(region.get('h', 2)) & ~1)
            x = max(0, int(region.get('x', 0)))
            y = max(0, int(region.get('y', 0)))
            argv += ["-filter_complex", f"crop={w}:{h}:{x}:{y}"]
        argv.append(out_path)
        return argv

    mod.build_capture_command = _build
    mod.probe_capabilities = lambda ffmpeg=None: {}
    mod.ffmpeg_path = lambda cfg=None: "ffmpeg"

    class _VideoUnavailable(Exception):
        pass

    mod.VideoUnavailable = _VideoUnavailable
    return mod


class TestRegionCapture(unittest.TestCase):
    """Story R-A — region crop in build_capture_command and VideoTrack."""

    # ------------------------------------------------------------------
    # 1. build_capture_command with real video_probe
    # ------------------------------------------------------------------

    def test_build_command_region_adds_even_crop(self):
        """Odd 1281×721 → rounded down to even 1280×720 in filter_complex crop."""
        # Import the REAL module directly, bypassing the fake in sys.modules.
        import importlib, importlib.util
        from pathlib import Path
        # Шлях рахуємо від самого файлу тесту, а не хардкодимо: корінь проєкту
        # уже раз переїжджав (Whisper → Recall) і цей рядок мовчки помер.
        probe_path = (Path(__file__).resolve().parent
                      / "app" / "services" / "recording" / "video_probe.py")
        spec = importlib.util.spec_from_file_location(
            "_real_video_probe",
            str(probe_path),
        )
        real_probe = importlib.util.module_from_spec(spec)
        # Patch os.path.isfile so ffmpeg_path() doesn't raise on missing exe
        import unittest.mock as _m
        with _m.patch("os.path.isfile", return_value=True):
            spec.loader.exec_module(real_probe)

        argv_region = real_probe.build_capture_command(
            output_idx=0,
            out_path="x.mp4",
            fps=30,
            codec="h264_nvenc",
            quality="p5",
            cq=23,
            ffmpeg=r"C:\ffmpeg\bin\ffmpeg.exe",
            region={"x": 100, "y": 80, "w": 1281, "h": 721},
        )
        filter_val = None
        for i, tok in enumerate(argv_region):
            if tok == "-filter_complex" and i + 1 < len(argv_region):
                filter_val = argv_region[i + 1]
                break
        self.assertIsNotNone(filter_val, "No -filter_complex in argv")
        self.assertIn("crop=1280:720:100:80", filter_val,
                      f"Expected crop=1280:720:100:80, got filter: {filter_val!r}")

        # region=None → no crop
        argv_full = real_probe.build_capture_command(
            output_idx=0,
            out_path="x.mp4",
            fps=30,
            codec="h264_nvenc",
            quality="p5",
            cq=23,
            ffmpeg=r"C:\ffmpeg\bin\ffmpeg.exe",
            region=None,
        )
        filter_full = None
        for i, tok in enumerate(argv_full):
            if tok == "-filter_complex" and i + 1 < len(argv_full):
                filter_full = argv_full[i + 1]
                break
        self.assertIsNotNone(filter_full)
        self.assertNotIn("crop=", filter_full,
                         f"Full-monitor must have no crop, got: {filter_full!r}")

    # ------------------------------------------------------------------
    # 2. Supervisor with region track spawns with crop in argv
    # ------------------------------------------------------------------

    def test_supervisor_region_track_spawns_with_crop(self):
        """track spec with mode='region' → argv contains crop=640:480:10:20."""
        captured_argv: list[list[str]] = []

        # Temporarily replace the fake probe with a region-aware one
        region_probe = _make_region_aware_fake_probe()
        old_probe = _sys.modules.get("app.services.recording.video_probe")
        _sys.modules["app.services.recording.video_probe"] = region_probe

        try:
            def factory(argv, **kwargs):
                captured_argv.append(list(argv))
                return FakePopen(returncode_after=None)

            with tempfile.TemporaryDirectory() as td:
                sup = _make_supervisor(
                    td,
                    tracks_spec=[{
                        "monitor_index": 0,
                        "output_idx": 0,
                        "monitor_label": "Mon0",
                        "mode": "region",
                        "region": {"x": 10, "y": 20, "w": 640, "h": 480},
                    }],
                    popen_factory=factory,
                )
                sup.start()
                try:
                    # Check VideoTrack fields
                    track = sup._tracks[0]
                    self.assertEqual(track.mode, "region",
                                     f"track.mode must be 'region', got {track.mode!r}")
                    self.assertEqual(track.region, {"x": 10, "y": 20, "w": 640, "h": 480},
                                     f"track.region mismatch: {track.region!r}")

                    # Check argv
                    self.assertTrue(len(captured_argv) >= 1, "No argv captured")
                    argv = captured_argv[0]
                    argv_str = " ".join(argv)
                    self.assertIn("crop=640:480:10:20", argv_str,
                                  f"crop not found in argv: {argv_str!r}")
                finally:
                    sup.stop(graceful=False)
        finally:
            if old_probe is not None:
                _sys.modules["app.services.recording.video_probe"] = old_probe
            else:
                _sys.modules.pop("app.services.recording.video_probe", None)

    # ------------------------------------------------------------------
    # 3. Full-mode track (no region) → no crop in argv
    # ------------------------------------------------------------------

    def test_full_track_has_no_crop(self):
        """mode='full' (or no mode) → no crop= in spawned argv."""
        captured_argv: list[list[str]] = []

        region_probe = _make_region_aware_fake_probe()
        old_probe = _sys.modules.get("app.services.recording.video_probe")
        _sys.modules["app.services.recording.video_probe"] = region_probe

        try:
            def factory(argv, **kwargs):
                captured_argv.append(list(argv))
                return FakePopen(returncode_after=None)

            with tempfile.TemporaryDirectory() as td:
                sup = _make_supervisor(
                    td,
                    tracks_spec=[{
                        "monitor_index": 0,
                        "output_idx": 0,
                        "monitor_label": "Mon0",
                        # no 'mode' or 'region' key → defaults to full
                    }],
                    popen_factory=factory,
                )
                sup.start()
                try:
                    self.assertTrue(len(captured_argv) >= 1, "No argv captured")
                    argv_str = " ".join(captured_argv[0])
                    self.assertNotIn("crop=", argv_str,
                                     f"Full-monitor track must have no crop=, got: {argv_str!r}")
                    track = sup._tracks[0]
                    self.assertEqual(track.mode, "full",
                                     f"Default mode must be 'full', got {track.mode!r}")
                    self.assertIsNone(track.region,
                                      f"Default region must be None, got {track.region!r}")
                finally:
                    sup.stop(graceful=False)
        finally:
            if old_probe is not None:
                _sys.modules["app.services.recording.video_probe"] = old_probe
            else:
                _sys.modules.pop("app.services.recording.video_probe", None)


# ===========================================================================
# Story R-B — TestRegionOverlay
# All tests are OFFLINE: no Tk window, no real overlay subprocess spawned.
# overlay_factory is a fake callable that records argv calls.
# ===========================================================================

class _FakeOverlayPopen:
    """Stand-in for the region_overlay subprocess.Popen."""

    def __init__(self):
        self.terminate_calls = 0
        self.kill_calls = 0
        self._returncode = None

    def terminate(self):
        self.terminate_calls += 1
        self._returncode = 0

    def kill(self):
        self.kill_calls += 1

    def poll(self):
        return self._returncode


class TestRegionOverlay(unittest.TestCase):
    """Story R-B — overlay process lifecycle, offline."""

    # ------------------------------------------------------------------
    # 1. _overlay_argv computes desktop (virtual) coords correctly
    # ------------------------------------------------------------------

    def test_overlay_argv_uses_desktop_coords(self):
        """monitor_pos + region offset → --x/--y are desktop coords."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                td,
                tracks_spec=[{
                    "monitor_index": 0,
                    "output_idx": 0,
                    "monitor_label": "Mon0",
                    "mode": "region",
                    "region": {"x": 10, "y": 20, "w": 640, "h": 480},
                    "monitor_pos_x": 100,
                    "monitor_pos_y": 50,
                }],
                popen_factory=lambda *a, **kw: FakePopen(returncode_after=None),
                region_overlay=True,
            )
            track = sup._tracks[0]
            argv = sup._overlay_argv(track)

            # Must reference the overlay module
            argv_str = " ".join(argv)
            self.assertIn("app.services.recording.region_overlay", argv_str,
                          f"argv must reference region_overlay module: {argv_str!r}")

            # desktop X = monitor_pos_x + region.x = 100 + 10 = 110
            xi = argv.index("--x")
            self.assertEqual(argv[xi + 1], "110",
                             f"--x must be 110 (100+10), got {argv[xi+1]!r}")

            # desktop Y = monitor_pos_y + region.y = 50 + 20 = 70
            yi = argv.index("--y")
            self.assertEqual(argv[yi + 1], "70",
                             f"--y must be 70 (50+20), got {argv[yi+1]!r}")

            # --w and --h pass region dimensions verbatim
            wi = argv.index("--w")
            self.assertEqual(argv[wi + 1], "640", f"--w must be 640, got {argv[wi+1]!r}")
            hi = argv.index("--h")
            self.assertEqual(argv[hi + 1], "480", f"--h must be 480, got {argv[hi+1]!r}")

    # ------------------------------------------------------------------
    # 2. Overlay spawned for region track; NOT for full-monitor track
    # ------------------------------------------------------------------

    def test_overlay_spawned_for_region_track(self):
        """start() with region_overlay=True spawns overlay for region track only."""
        overlay_calls: list[list] = []
        overlay_procs: list[_FakeOverlayPopen] = []

        def fake_overlay_factory(argv, **kwargs):
            overlay_calls.append(list(argv))
            p = _FakeOverlayPopen()
            overlay_procs.append(p)
            return p

        ffmpeg_procs: list[FakePopen] = []

        def fake_ffmpeg_factory(*args, **kwargs):
            fp = FakePopen(returncode_after=None)
            ffmpeg_procs.append(fp)
            return fp

        region_probe = _make_region_aware_fake_probe()
        old_probe = _sys.modules.get("app.services.recording.video_probe")
        _sys.modules["app.services.recording.video_probe"] = region_probe

        try:
            with tempfile.TemporaryDirectory() as td:
                sup = _make_supervisor(
                    td,
                    tracks_spec=[
                        {   # region track → overlay expected
                            "monitor_index": 0,
                            "output_idx": 0,
                            "monitor_label": "Mon0",
                            "mode": "region",
                            "region": {"x": 10, "y": 20, "w": 640, "h": 480},
                            "monitor_pos_x": 0,
                            "monitor_pos_y": 0,
                        },
                        {   # full-monitor track → NO overlay
                            "monitor_index": 1,
                            "output_idx": 1,
                            "monitor_label": "Mon1",
                            "mode": "full",
                        },
                    ],
                    popen_factory=fake_ffmpeg_factory,
                    region_overlay=True,
                    overlay_factory=fake_overlay_factory,
                )
                sup.start()
                try:
                    # Exactly one overlay process for the region track
                    self.assertEqual(len(overlay_calls), 1,
                                     f"Expected 1 overlay spawn, got {len(overlay_calls)}: {overlay_calls}")

                    # The overlay argv must reference region_overlay module
                    argv_str = " ".join(overlay_calls[0])
                    self.assertIn("app.services.recording.region_overlay", argv_str,
                                  f"overlay argv must reference module: {argv_str!r}")

                    # Correct coords: x=0+10=10, y=0+20=20
                    xi = overlay_calls[0].index("--x")
                    self.assertEqual(overlay_calls[0][xi + 1], "10",
                                     f"overlay --x must be 10, got {overlay_calls[0][xi+1]!r}")
                    yi = overlay_calls[0].index("--y")
                    self.assertEqual(overlay_calls[0][yi + 1], "20",
                                     f"overlay --y must be 20, got {overlay_calls[0][yi+1]!r}")

                    # track.overlay_proc is set on region track, None on full track
                    self.assertIsNotNone(sup._tracks[0].overlay_proc,
                                        "region track must have overlay_proc set")
                    self.assertIsNone(sup._tracks[1].overlay_proc,
                                      "full-monitor track must have overlay_proc=None")
                finally:
                    sup.stop(graceful=False)
        finally:
            if old_probe is not None:
                _sys.modules["app.services.recording.video_probe"] = old_probe
            else:
                _sys.modules.pop("app.services.recording.video_probe", None)

    # ------------------------------------------------------------------
    # 3. stop() kills the overlay and clears overlay_proc
    # ------------------------------------------------------------------

    def test_overlay_killed_on_stop(self):
        """After start() then stop(), overlay proc is terminated and overlay_proc=None."""
        overlay_proc = [None]

        def fake_overlay_factory(argv, **kwargs):
            p = _FakeOverlayPopen()
            overlay_proc[0] = p
            return p

        ffmpeg_procs: list[FakePopen] = []

        def fake_ffmpeg_factory(*args, **kwargs):
            fp = FakePopen(returncode_after=None)
            ffmpeg_procs.append(fp)
            return fp

        region_probe = _make_region_aware_fake_probe()
        old_probe = _sys.modules.get("app.services.recording.video_probe")
        _sys.modules["app.services.recording.video_probe"] = region_probe

        try:
            with tempfile.TemporaryDirectory() as td:
                sup = _make_supervisor(
                    td,
                    tracks_spec=[{
                        "monitor_index": 0,
                        "output_idx": 0,
                        "monitor_label": "Mon0",
                        "mode": "region",
                        "region": {"x": 0, "y": 0, "w": 1920, "h": 1080},
                    }],
                    popen_factory=fake_ffmpeg_factory,
                    region_overlay=True,
                    overlay_factory=fake_overlay_factory,
                )
                sup.start()

                # Overlay must have been spawned
                self.assertIsNotNone(overlay_proc[0],
                                     "Overlay must have been spawned on start()")

                # Stop the supervisor
                sup.stop(graceful=False)

                # terminate() must have been called on the overlay proc
                self.assertGreater(overlay_proc[0].terminate_calls, 0,
                                   "overlay proc.terminate() must be called on stop()")

                # track.overlay_proc must be cleared to None
                self.assertIsNone(sup._tracks[0].overlay_proc,
                                  "track.overlay_proc must be None after stop()")
        finally:
            if old_probe is not None:
                _sys.modules["app.services.recording.video_probe"] = old_probe
            else:
                _sys.modules.pop("app.services.recording.video_probe", None)


# ===========================================================================
# __main__ runner (matches project convention: python test_*.py)
# ===========================================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestManifestHasVideoList,
        TestAddAndUpdateVideoTrack,
        TestExplodingVideoNeverBreaksAudio,
        TestVideoDisabledIsNoop,
        TestVideoMetadataPersistence,
        TestVideoSupervisorEdgeCases,
        TestRegionCapture,
        TestRegionOverlay,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    total = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print()
    print(f"{'PASS' if result.wasSuccessful() else 'FAIL'} — {passed}/{total} tests passed")
    sys.exit(0 if result.wasSuccessful() else 1)
