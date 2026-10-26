"""Recording session storage (Phase 9.2).

Each session = a separate folder in ``RECORDING_DIR/<session_id>/`` with:

- ``manifest.json`` — the state machine, updated atomically through tmp+rename.
- ``mic.pcm``, ``system.pcm`` — raw PCM, append-only, written through
  :class:`ChunkedPcmWriter` (another module).
- ``mic.wav``, ``system.wav``, ``final.mp3`` — created at finalize
  (Phase 9.4).

Manifest schema (version 1):

.. code-block:: json

    {
      "session_id": "rec_abc123",
      "version": 1,
      "status": "recording",
      "started_at": "2026-05-04T12:34:56+00:00",
      "sample_rate": 48000,
      "channels": 2,
      "bytes_per_sample": 2,
      "streams": {
        "mic":    {"device": "Microphone (...)", "bytes": 14400000, "error": null},
        "system": {"device": "Speakers Loopback", "bytes": 14400000, "error": null}
      },
      "segments": [
        {"index": 0, "start_ts": "...", "end_ts": "...", "duration_sec": 240.5,
         "mic_offset_bytes": 0, "system_offset_bytes": 0}
      ],
      "name": null,
      "auto_name": "Запис 2026-05-04 12:34"
    }

Crash resilience: an atomic rename guarantees that the manifest on disk
is always valid JSON. If the process dies in the middle of a write — the temporary
file stays as .tmp and is ignored; the main manifest is current as of
the moment of the previous successful rename.

Thread safety: all mutations go through :meth:`modify` under a per-session
RLock. A concurrent read is allowed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


MANIFEST_VERSION = 2
MANIFEST_FILENAME = 'manifest.json'

# Single source of truth for session_id validation. Protection against path traversal:
# session_id is used as a subfolder name in RECORDING_DIR and in URL routes,
# so we block any '..', '/', '\' or other special characters. Format:
# rec_ + 1..64 characters [a-f0-9] (hex from uuid4().hex[:16] = 16 hex chars,
# we take a wider limit for future flexibility). In tests we allow
# alphabetic characters through underscores — `^rec_[a-z0-9_]+$`.
_SESSION_ID_RE = re.compile(r'^rec_[a-z0-9_]{1,64}$')

# Allowed session statuses. Recovery looks for the first three.
STATUS_RECORDING = 'recording'
STATUS_PAUSED = 'paused'
STATUS_STOPPING = 'stopping'
STATUS_FINALIZED = 'finalized'
STATUS_CRASHED = 'crashed'
STATUS_DISCARDED = 'discarded'

ACTIVE_STATUSES = frozenset({STATUS_RECORDING, STATUS_PAUSED, STATUS_STOPPING})

STREAM_MIC = 'mic'
STREAM_SYSTEM = 'system'


class SessionStoreError(RuntimeError):
    """Error of an operation on a session."""


class SessionStore:
    """A file store for recording sessions with an atomic manifest.

    Does not keep state in memory (except locks) — the manifest is read from
    disk on every ``modify``. This is deliberate: it makes the store re-entrant
    and simpler for recovery.
    """

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # --------------------------------------------------------- public api

    def create(
        self,
        session_id: str,
        sample_rate: int,
        channels: int,
        mic_device_name: Optional[str] = None,
        system_device_name: Optional[str] = None,
        auto_name: Optional[str] = None,
        system_source: Optional[dict] = None,
        started_by: str = 'ui',
    ) -> dict:
        """Creates a new session. Returns the created manifest.

        ``started_by`` (mcp-recording-live) records who started the session,
        written once here from the server-side proof the blueprint derived.

        ``system_source`` (``SystemSpec.to_dict()``) is written to
        ``streams.system.source`` together with an empty ``source_events`` when the
        system stream exists; such a stream is ``enabled`` even without a device name
        (an include/exclude source has no endpoint).

        If session_id already exists — raises :class:`SessionStoreError`.
        Validation of session_id (path traversal protection) is in
        :meth:`session_dir`.
        """
        session_dir = self.session_dir(session_id)
        if session_dir.exists():
            raise SessionStoreError(f"Session {session_id} already exists")

        session_dir.mkdir(parents=True)
        now = _now_iso()
        manifest: dict[str, Any] = {
            'session_id': session_id,
            'version': MANIFEST_VERSION,
            'status': STATUS_RECORDING,
            'started_at': now,
            'sample_rate': int(sample_rate),
            'channels': int(channels),
            'bytes_per_sample': 2,
            'streams': {
                STREAM_MIC: {
                    'device': mic_device_name,
                    'bytes': 0,
                    'error': None,
                    'enabled': mic_device_name is not None,
                    # Per-stream native parameters. Filled in by
                    # _open_stream after resolve_device_params, because the
                    # device may not support the requested rate
                    # (for example a mono mic at 88200Hz when 48000/2 was requested).
                    'sample_rate': None,
                    'channels': None,
                },
                STREAM_SYSTEM: {
                    'device': system_device_name,
                    'bytes': 0,
                    'error': None,
                    'enabled': (system_device_name is not None
                                or system_source is not None),
                    'sample_rate': None,
                    'channels': None,
                },
                'video': [],
            },
            'segments': [{
                'index': 0,
                'start_ts': now,
                'end_ts': None,
                'duration_sec': None,
                'mic_offset_bytes': 0,
                'system_offset_bytes': 0,
            }],
            'name': None,
            'auto_name': auto_name or _default_auto_name(),
            'started_by': started_by,
        }
        if system_source is not None:
            manifest['streams'][STREAM_SYSTEM]['source'] = dict(system_source)
            manifest['streams'][STREAM_SYSTEM]['source_events'] = []
        self._write_manifest(session_dir, manifest)
        return manifest

    def session_dir(self, session_id: str) -> Path:
        """The path to the session folder. Validates session_id against path traversal.

        Raises :class:`SessionStoreError` if session_id contains
        dangerous characters ('..', '/', '\\') or does not match
        the pattern ``^rec_[a-z0-9_]{1,64}$``. This guarantees that
        ``base_dir / session_id`` never goes outside base_dir.
        """
        if not _SESSION_ID_RE.fullmatch(session_id or ''):
            raise SessionStoreError(
                f"Invalid session_id: {session_id!r} "
                "(must match ^rec_[a-z0-9_]{1,64}$)"
            )
        return self.base_dir / session_id

    def pcm_path(self, session_id: str, stream: str) -> Path:
        """The path to the raw PCM file for a stream ('mic' or 'system')."""
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Unknown stream: {stream!r}")
        return self.session_dir(session_id) / f'{stream}.pcm'

    def read(self, session_id: str) -> dict:
        """Reads the manifest from disk. Raises :class:`SessionStoreError`
        if the session/manifest is missing or broken.

        Path self-healing (03.07.2026): the manifest stores ABSOLUTE
        paths (``final_mp3_path``, ``primary_video_path``,
        ``streams.video[].path``). After a move/rename of the project
        root these paths become dead — the file physically lies in the same
        session folder, just under the old absolute prefix. If the path
        does not exist and a file with the same basename is found in THIS
        session's folder — we substitute it IN THE RETURNED dict. This is read-only:
        nothing is written to disk (the healing lives only as long as this
        dict lives; if persistence is needed, a separate modify() does it).
        """
        session_dir = self.session_dir(session_id)
        manifest = self._read_manifest(session_dir)
        healed = self._heal_paths(manifest, session_dir)
        if healed:
            logger.debug(
                "Manifest %s: self-healing replaced %d path(s) with current ones "
                "(the file(s) were found in the session folder by the old basename)",
                session_id, healed,
            )
        return manifest

    # --- self-healing (repath after project move)

    _REPATH_TOP_LEVEL_FIELDS = ('final_mp3_path', 'primary_video_path')

    @classmethod
    def _heal_paths(cls, manifest: dict, session_dir: Path) -> int:
        """Mutates ``manifest`` in place, replacing dead absolute paths
        with live ones (the same basename in ``session_dir``). Returns the number
        of replaced fields."""
        healed = 0
        for field in cls._REPATH_TOP_LEVEL_FIELDS:
            fixed = cls._heal_one_path(manifest.get(field), session_dir)
            if fixed is not None:
                manifest[field] = fixed
                healed += 1

        video_tracks = (manifest.get('streams') or {}).get('video') or []
        for track in video_tracks:
            fixed = cls._heal_one_path(track.get('path'), session_dir)
            if fixed is not None:
                track['path'] = fixed
                healed += 1

        return healed

    @staticmethod
    def _heal_one_path(path: Optional[str], session_dir: Path) -> Optional[str]:
        """``None`` if the path should not be touched; otherwise the new current path."""
        if not path or os.path.isfile(path):
            return None
        candidate = session_dir / os.path.basename(path)
        if candidate.is_file():
            return str(candidate)
        return None

    def exists(self, session_id: str) -> bool:
        manifest_path = self.session_dir(session_id) / MANIFEST_FILENAME
        return manifest_path.is_file()

    def modify(
        self,
        session_id: str,
        modifier: Callable[[dict], None],
    ) -> dict:
        """Atomic read-modify-write under the session lock.

        ``modifier(manifest)`` mutates the manifest in place. Returns the new
        manifest after the write.
        """
        with self._lock_for(session_id):
            session_dir = self.session_dir(session_id)
            manifest = self._read_manifest(session_dir)
            modifier(manifest)
            self._write_manifest(session_dir, manifest)
            return manifest

    # --- shorthand mutators

    def update_status(self, session_id: str, status: str) -> dict:
        def _m(mf: dict) -> None:
            mf['status'] = status
        return self.modify(session_id, _m)

    def set_name(self, session_id: str, name: Optional[str]) -> dict:
        def _m(mf: dict) -> None:
            mf['name'] = name
        return self.modify(session_id, _m)

    def set_description(self, session_id: str, description: Optional[str]) -> dict:
        """The recording description (editable-title-description-02) — the same path as
        `set_name`; the 'description' key is absent from old manifests,
        readers take it through ``.get('description')``."""
        def _m(mf: dict) -> None:
            mf['description'] = description
        return self.modify(session_id, _m)

    def update_stream_bytes(
        self,
        session_id: str,
        stream: str,
        total_bytes: int,
    ) -> None:
        """Update the total bytes counter for a stream. Called from
        ChunkedPcmWriter after a successful flush+fsync."""
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Unknown stream: {stream!r}")
        def _m(mf: dict) -> None:
            mf['streams'][stream]['bytes'] = int(total_bytes)
        self.modify(session_id, _m)

    def set_stream_error(
        self,
        session_id: str,
        stream: str,
        error: Optional[str],
    ) -> None:
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Unknown stream: {stream!r}")
        def _m(mf: dict) -> None:
            mf['streams'][stream]['error'] = error
        self.modify(session_id, _m)

    def set_stream_params(
        self,
        session_id: str,
        stream: str,
        sample_rate: int,
        channels: int,
    ) -> None:
        """Record the native (rate, channels) for a stream.

        Called from RecordingService._open_stream() after
        resolve_device_params() — guarantees that finalize will read the
        right parameters for the WAV header, even if the device
        did not support the requested rate (a mono microphone at 88200Hz
        when 48000/2 was requested).
        """
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Unknown stream: {stream!r}")
        def _m(mf: dict) -> None:
            mf['streams'][stream]['sample_rate'] = int(sample_rate)
            mf['streams'][stream]['channels'] = int(channels)
        self.modify(session_id, _m)

    def set_stream_source(
        self,
        session_id: str,
        stream: str,
        source: dict,
        event: Optional[dict] = None,
    ) -> None:
        """The current stream source + (optionally) one mark in ``source_events``.

        Additive keys without a bump of ``MANIFEST_VERSION``: finalize and recovery
        read only bytes, so old readers do not notice them.
        """
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Unknown stream: {stream!r}")
        def _m(mf: dict) -> None:
            st = mf['streams'][stream]
            st['source'] = dict(source)
            if event is not None:
                st.setdefault('source_events', []).append(dict(event))
        self.modify(session_id, _m)

    @staticmethod
    def stream_source(manifest: dict, stream: str) -> dict:
        """The stream source from the manifest; a manifest predating SystemSpec (no ``source``) → ``all``."""
        src = ((manifest.get('streams') or {}).get(stream) or {}).get('source')
        if not isinstance(src, dict):
            return {'mode': 'all', 'apps': [], 'device_index': None}
        return {
            'mode': src.get('mode') or 'all',
            'apps': list(src.get('apps') or []),
            'device_index': src.get('device_index'),
        }

    def add_video_track(self, session_id: str, track: dict) -> None:
        """Add a video track record to manifest['streams']['video'].

        Tolerant of v1 manifests that lack the 'video' key: initialises it
        on-the-fly so old sessions don't crash.
        """
        def _m(mf: dict) -> None:
            vids = mf['streams'].setdefault('video', [])
            tid = track.get('track_id')
            # upsert by track_id: update the existing track rather than breed duplicates
            # (called on both the supervisor's start and stop).
            for i, t in enumerate(vids):
                if t.get('track_id') == tid:
                    vids[i] = {**t, **dict(track)}
                    return
            vids.append(dict(track))
        self.modify(session_id, _m)

    def update_video_track(self, session_id: str, track_id: str, **fields) -> None:
        """Update the fields of an existing video track by its track_id.

        If track_id is not found — a warning is logged, the manifest is not changed.
        Tolerant of v1 manifests that lack the 'video' key.
        """
        def _m(mf: dict) -> None:
            tracks = mf['streams'].get('video', [])
            for t in tracks:
                if t.get('track_id') == track_id:
                    t.update(fields)
                    return
            logger.warning(
                "update_video_track: track_id %r not found in session %s",
                track_id, session_id,
            )
        self.modify(session_id, _m)

    def open_segment(self, session_id: str) -> int:
        """Closes the previous segment (if open) and opens a new one.
        Returns the index of the new segment.

        Called on resume after pause. On create the first segment
        is created automatically — this function does not need to be called.
        """
        def _m(mf: dict) -> None:
            now = _now_iso()
            segments = mf['segments']
            if segments and segments[-1].get('end_ts') is None:
                # Close the open segment
                last = segments[-1]
                last['end_ts'] = now
                last['duration_sec'] = _iso_diff_seconds(last['start_ts'], now)
            mic_total = mf['streams'][STREAM_MIC]['bytes']
            sys_total = mf['streams'][STREAM_SYSTEM]['bytes']
            segments.append({
                'index': len(segments),
                'start_ts': now,
                'end_ts': None,
                'duration_sec': None,
                'mic_offset_bytes': mic_total,
                'system_offset_bytes': sys_total,
            })
        manifest = self.modify(session_id, _m)
        return manifest['segments'][-1]['index']

    def close_segment(self, session_id: str) -> None:
        """Closes the last open segment (on pause/stop)."""
        def _m(mf: dict) -> None:
            segments = mf['segments']
            if segments and segments[-1].get('end_ts') is None:
                last = segments[-1]
                now = _now_iso()
                last['end_ts'] = now
                last['duration_sec'] = _iso_diff_seconds(last['start_ts'], now)
        self.modify(session_id, _m)

    # --- queries

    def list_all(self) -> list[dict]:
        """A list of manifests of all sessions. Broken manifests are skipped
        with a warning."""
        out: list[dict] = []
        if not self.base_dir.is_dir():
            return out
        for entry in self.base_dir.iterdir():
            if not entry.is_dir():
                continue
            manifest_path = entry / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    out.append(json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Corrupted manifest in %s: %s", entry, e)
        return out

    def list_orphaned(self) -> list[dict]:
        """Sessions in an active status (recording/paused/stopping).
        Called at application start for recovery."""
        return [m for m in self.list_all() if m.get('status') in ACTIVE_STATUSES]

    def list_unfinalized(self) -> list[dict]:
        """``crashed`` sessions that never got a ``final.mp3``.

        The second (after :meth:`list_orphaned`) entry into recovery. It closes the hole
        through which a recording could vanish FOREVER: the first restart moved an active
        session to ``crashed`` and queued a finalize, and the second restart killed
        the job itself (``jobs.state='crashed'``). After that nobody saw the session —
        ``list_orphaned`` looks only at ACTIVE_STATUSES, ``reconcile`` —
        only at already finalized ones, ``/save`` returns 500 for ``crashed``, and
        the UI banner ``/api/recordings/recovered`` is one-shot. The PCM lay on disk
        as dead cargo (a real case on 21.07.2026: a 10-hour session, 8.2 GB).

        Returns sessions with ``status == 'crashed'`` where ``final_mp3_path`` is empty
        or the file it points to does not exist (project move / manual deletion). Sessions with
        a live MP3 are not touched — ``reconcile_recordings`` picks them up.
        """
        out: list[dict] = []
        for m in self.list_all():
            if m.get('status') != STATUS_CRASHED:
                continue
            final = m.get('final_mp3_path')
            if final and Path(final).is_file():
                continue
            out.append(m)
        return out

    def delete(self, session_id: str) -> None:
        """Deletes the session folder completely. Idempotent."""
        import shutil
        session_dir = self.session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
        with self._locks_guard:
            self._locks.pop(session_id, None)

    # ---------------------------------------------------------- internals

    def _lock_for(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[session_id] = lock
            return lock

    def _read_manifest(self, session_dir: Path) -> dict:
        manifest_path = session_dir / MANIFEST_FILENAME
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except FileNotFoundError as e:
            raise SessionStoreError(f"Manifest not found: {manifest_path}") from e
        except json.JSONDecodeError as e:
            raise SessionStoreError(f"Manifest is broken ({manifest_path}): {e}") from e

        # Phase 9.10 hardening: a warning if the schema version does not match.
        # For now we only warn and return as is — future versions
        # will add the migration logic for old manifests. This makes future
        # schema changes safer: we at least know the manifest is old.
        version = manifest.get('version')
        if version is not None and version != MANIFEST_VERSION:
            logger.warning(
                "Manifest %s has version=%s, expected %s — continuing "
                "without migration (behaviour may be unexpected)",
                manifest_path, version, MANIFEST_VERSION,
            )
        return manifest

    def _write_manifest(self, session_dir: Path, manifest: dict) -> None:
        """Atomic write: write to tmp + fsync + os.replace.

        Invariant: the disk always holds valid JSON. If the process dies
        in the middle of a write — the tmp file stays, the main one is untouched.
        """
        manifest_path = session_dir / MANIFEST_FILENAME
        tmp_path = session_dir / (MANIFEST_FILENAME + '.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, manifest_path)


# ------------------------------------------------------------- helpers

def _now_iso() -> str:
    """ISO-8601 in UTC with a timezone suffix."""
    return datetime.now(tz=timezone.utc).isoformat(timespec='seconds')


def _iso_diff_seconds(start_iso: str, end_iso: str) -> float:
    """The number of seconds between two ISO dates."""
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return round((end - start).total_seconds(), 3)


def _default_auto_name() -> str:
    """An auto-suggested name like 'Запис 2026-05-04 14:32' (local time)."""
    now = datetime.now()
    return f"Запис {now.strftime('%Y-%m-%d %H:%M')}"
