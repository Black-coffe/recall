# recording — server-side recording (`app/services/recording/`)

Mic + system audio with a live meter, live segments (SSE), pause/resume, finalize in the background,
recovery of orphaned sessions. **Up to `RECORDING_MAX_ACTIVE` (default 2) parallel sessions**; system
audio — the whole endpoint or isolated per application. Windows-only (`pyaudiowpatch` for mic/`all`,
comtypes+pycaw for process loopback). ADR-026 (+ amendments A1-A4). Wiki: `docs/wiki/recording-sources-and-sessions.md`.

## Modules
- **service.py** — `RecordingService(..., max_active=2, app_source_factory=None)`: a registry
  `_sessions: dict[sid,_ActiveSession]` under `_registry_lock` (the dict only), `_start_lock` serializes
  the limit, a per-session `RLock` on pause/resume/stop/retarget/tick. `start(..., system_source=SystemSpec)`,
  `pause()`, `resume()`, `stop()`, `discard()`, `retarget_system(sid, spec, reason)`, `get_state()`,
  `active_session_ids()`, `active_session_id` (only when exactly one, otherwise None), `is_active(sid)`,
  `recover_orphaned()`, `recover_unfinalized()`. `_build_source()` — the single spec→source dispatch.
  Errors (`code`): `SessionConflictError` `SESSION_LIMIT` (carries `.active`, `.limit`),
  `SessionNotFoundError` `NOT_FOUND`, `NoSystemStreamError` `NO_SYSTEM_STREAM`, `RecordingError`
  (`NO_SYSTEM_DEVICE` for `all` without an index).
- **audio_source.py** — the capture boundary. `AudioSource` Protocol (duck-typed `WasapiRecorder` +
  `drain_events()`), `MicSpec`, `SystemSpec(mode all|include|exclude, apps, device_index)` with
  `from_dict/to_dict/validate`, `UnsupportedSourceSpecError` (`UNSUPPORTED_SOURCE_SPEC`),
  `MAX_INCLUDE_CLIENTS=4`, format `48000/2 s16le`. `ProcessLoopbackSource` (one client pid×mode,
  bounded queue drop-oldest), `MixedSource` (int16 saturated sum, the part that lags >500 ms
  is padded with zeros), `AppAudioSource` (holds the clients, watcher thread `app-audio-watcher` every 2 s
  → `refresh_pids()` → event `pid_change`; pads silence up to wall-clock time on a deficit >50 ms),
  `resolve_root_pids()` (psutil roots of the exe), `make_app_source()`.
- **process_loopback.py** — `ProcessLoopbackClient` (ActivateAudioInterfaceAsync
  `VAD\Process_Loopback`, INCLUDE/EXCLUDE tree, polling on an MTA thread), `LoopbackClient`
  Protocol, `ClientFactory`, `default_client_factory`. Native code is loaded lazily (`_native()`).
- **app_audio.py** — `list_sounding_apps(live_specs)` (Active+Inactive sessions on ALL active
  render endpoints, dedup by exe, `recorded_by`; never raises → `available:false`),
  `session_pids()`, `session_roots(exe, fallback=)` (session PIDs → root of the exe tree, the tree
  with an Active session first). Independent of `audio_source.py`.
  `list_call_sessions(enumerate_sessions=, own_pid=)` (K2) → `{exe: {mic|out: {active, peak, endpoint}}}`
  over Active/Inactive sessions of both directions (own PID skipped); `None` when the enumeration itself failed.
- **call_watch.py** (K2, 4.9.0) — `CallWatcher` (thread `call-watch`, 1 s tick, all dependencies injected →
  `tick()` is testable). A call = an exe from the list whose mic AND out are Active for `prompt_seconds` in a row and each side is
  > `SPEECH_PEAK=0.02` for ≥3 ticks → one toast «Record it?» (not if a live recording already captures the exe: `captures()`).
  The end of a call (sessions inactive for 20 s or silence for 40 s) withdraws an unread prompt and asks «Stop recording?» about
  every live unpaused recording that captures the exe; then `autostop_minutes` of silence on both tracks → `stop_fn`
  (the Stop button path, the recording stays intact). `pending()/consume(token,action)/dismiss(id)`: the prompt id = a one-time token,
  TTL 3 h. JSONL log of decisions (no PID/language, rotation 5 MB → `.1`). `enumerate → None` keeps the state. `ToastNotifier`.
- **toast.py** (K2) — Windows toasts without a pip dependency: `powershell.exe -EncodedCommand` (WinRT
  `ToastNotificationManager`, `NO_WINDOW`), AUMID `Recall.CallPrompt` in HKCU; `build_xml`, `show`, `remove`,
  `ensure_registered`. Buttons — protocol activation (a URL in the browser). Returns `bool`, does not raise.
- **recorder.py** — `WasapiRecorder` (+ `drain_events()` → `[]`), `list_input_devices()`,
  `resolve_device_params()`, `LevelMeter`, `AudioFrame`.
- **finalize.py** — `pcm_to_wav()`, `finalize_session()` (mic+system → overlay → final.mp3). Reads bytes only.
- **pcm_writer.py** — `ChunkedPcmWriter`: O_APPEND, fsync ~5s, lazy-open.
- **session_store.py** — `SessionStore`: a JSON manifest with atomic rename, RLock per sid. Statuses
  recording/paused/stopping/finalized/crashed/discarded. `create(..., system_source=)`,
  `set_stream_source(sid, stream, source, event)`, `stream_source(manifest, stream)` (no `source` → `all`).

## System audio source
- `all` at start → `recorder_factory` (`WasapiRecorder`, endpoint `device_index`) — byte-for-byte as before 4.7.0.
- `include:[≤4 exe]` / `exclude:[exactly 1 exe]` → `app_source_factory`; in `app.py:835-841` the PID
  resolver = `session_roots(exe, fallback=resolve_root_pids)`. Exclude without a running application →
  EXCLUDE of own PID (= all the sound). Exclude with several trees → only the first (the sounding one).
- The `.pcm` format of a stream is unchanged: `all` on a stream that started as include/exclude = EXCLUDE-own-PID.
- Retarget: the new source is opened first, the remainder of the old one is flushed, frames of the new one with
  `ts < swap_ts` are dropped, a gap >20 ms → silence. On pause only the spec changes (`applied_on_resume`).
- Every change → a record in `streams.system.source_events` (`reason` user|pid_change|auto_restore;
  PIDs are not written to the manifest) + SSE `source_changed`.

## Video tracks → visual timeline (4.10.0)
Screen video lives in `recording_video_tracks` (`start_offset_sec`), `audio_downloads.has_video=1` + `primary_video_path`.
`video_analysis.resolve_recording_video()` reduces this to `{primary_video_path, session_dir, start_offset_sec}`; the frame time in the
timeline = time in the video + `start_offset_sec`. Thumbnails are cached in `<session_dir>/frames_cache/thumbs.npz`; frame images
are no longer written to disk. Pipeline, endpoints, secrets — `services.md` (video_*.py), `blueprints.md`, wiki `video-timeline-and-screen-secrets`.

## FSM (per session)
```
[recording] ──pause()──> [paused] ──resume()─┐
     │                        │              │
     └──── stop() ──> [stopping] ──finalize──> [finalized]
                          └──crash──> [crashed]  (recover_orphaned / recover_unfinalized)
```

## Registration in the Audio library (`library.py`)
`register_recording()` — an idempotent upsert into `audio_downloads` (`youtube_id='recording_'+sid`),
category from `copilot_sessions.category_id`. *finalize ≠ saving: the upsert is done by the finalize callback.*

**Gotchas:** a client on a dead PID streams silence without an error — the watcher is the only death detector;
the watcher truncates include >4 processes to 4 (open() raises in the same situation); the copilot is one per
process (`CopilotBusyError`, `memory/map/copilot.md`); reembed is deferred while at least one recording is alive.
**K2:** nothing starts without a click; autostop acts only after «Stop recording?» and not on pause; the watcher lives only
under `__main__` (`app.extensions['call_watcher']`, `app.py:1189`); settings `RECORDING_AUTOPROMPT[_APPS|_SECONDS]`,
`RECORDING_AUTOSTOP_SILENCE_MIN` (`app/core/settings.py:187-204`, `.env.example:120-129`); HTTP — `memory/map/blueprints.md`.
**SSE** (`recording:{sid}`): level (10Hz), status, chunk_saved, error, source_changed.
**Tests:** `test_recording_devices.py`, `tests/test_{audio_source,app_audio,call_watch,parallel_recordings,
recording_service,recording_blueprint,session_store}.py`. Spike kit: `scripts/spikes/process_loopback/`.

last-verified: 2026-10-13 (the video tracks section); the rest 2026-10-08
