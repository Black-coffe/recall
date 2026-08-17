"""Phase 9: System audio + microphone recording (WASAPI loopback).

Public API:
- :func:`list_input_devices` — енумерація mic + loopback девайсів.
- :class:`WasapiRecorder` — низькорівневий wrapper навколо WASAPI стріму.
- :class:`LevelMeter` — rolling RMS+peak за останні 100ms для VU.
- :class:`SessionStore` — файлове сховище сесій з atomic manifest.
- :class:`ChunkedPcmWriter` — append-only PCM writer з fsync.

Higher-level услуги (RecordingService, finalize) додаються в наступних
фазах 9.3-9.4.
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
