"""Phase 9: System audio + microphone recording (WASAPI loopback).

Public API:
- :func:`list_input_devices` — enumeration of mic + loopback devices.
- :class:`WasapiRecorder` — a low-level wrapper around a WASAPI stream.
- :class:`LevelMeter` — rolling RMS+peak over the last 100ms for the VU meter.
- :class:`SessionStore` — a file-based session store with an atomic manifest.
- :class:`ChunkedPcmWriter` — an append-only PCM writer with fsync.

Higher-level services (RecordingService, finalize) are added in the next
phases 9.3-9.4.
"""
from app.services.recording.finalize import (
    FinalizeError,
    discard_session_files,
    finalize_session,
    finalize_video,
    mix_streams,
    pcm_to_wav,
)
from app.services.recording.library import register_recording
from app.services.recording.reconcile import reconcile_recordings
from app.services.recording.pcm_writer import ChunkedPcmWriter
from app.services.recording.recorder import (
    AudioFrame,
    DeviceInfo,
    LevelMeter,
    RecorderError,
    WasapiRecorder,
    list_input_devices,
    resolve_device_params,
)
from app.services.recording.service import (
    RecordingError,
    RecordingService,
    SessionConflictError,
    SessionNotFoundError,
)
from app.services.recording.session_store import (
    ACTIVE_STATUSES,
    MANIFEST_VERSION,
    STATUS_CRASHED,
    STATUS_DISCARDED,
    STATUS_FINALIZED,
    STATUS_PAUSED,
    STATUS_RECORDING,
    STATUS_STOPPING,
    STREAM_MIC,
    STREAM_SYSTEM,
    SessionStore,
    SessionStoreError,
)


__all__ = [
    # finalize
    'FinalizeError',
    'discard_session_files',
    'finalize_session',
    'finalize_video',
    'mix_streams',
    'pcm_to_wav',
    # library
    'register_recording',
    'reconcile_recordings',
    # recorder
    'AudioFrame',
    'DeviceInfo',
    'LevelMeter',
    'RecorderError',
    'WasapiRecorder',
    'list_input_devices',
    'resolve_device_params',
    # session_store
    'ACTIVE_STATUSES',
    'MANIFEST_VERSION',
    'STATUS_CRASHED',
    'STATUS_DISCARDED',
    'STATUS_FINALIZED',
    'STATUS_PAUSED',
    'STATUS_RECORDING',
    'STATUS_STOPPING',
    'STREAM_MIC',
    'STREAM_SYSTEM',
    'SessionStore',
    'SessionStoreError',
    # pcm_writer
    'ChunkedPcmWriter',
    # service
    'RecordingError',
    'RecordingService',
    'SessionConflictError',
    'SessionNotFoundError',
]
