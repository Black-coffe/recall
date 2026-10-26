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
  `tick()` is testable). Call = listed exe with mic AND out Active for `prompt_seconds` and `SPEECH_PEAK=0.02` on both sides →
  toast «Record it?»; call end → «Stop recording?», then `autostop_minutes` of silence → `stop_fn` (Stop button path).
  `pending()/consume(token,action)/dismiss(id)`: one-time token, TTL 3 h. JSONL decision log (rotation 5 MB). `enumerate → None` keeps state.
  4.31.0: `publisher=` (app.py:1296 -> `sse_broker.publish("system:calls", event, data)`) via `_emit` (queued as an effect, runs outside the lock; a publisher failure only warns):
  `call_start`, `prompt{kind record|stop}`, `call_end`, `autostop`, `prompt_withdrawn{kind, exe, reason call_end|session_started|expired|consumed|dismissed}` (payload: no token, no URL).
  `_withdraw_recorded`: a pending «record» prompt is withdrawn when a live recording already captures that app. Read by MCP `wait_for(channel='system:calls')` and `GET /api/automation/status` `calls`.
- **toast.py** (K2) — Windows toasts via `powershell.exe -EncodedCommand` (WinRT), AUMID `Recall.CallPrompt`; `build_xml`, `show`,
  `remove`, `ensure_registered`. Buttons = protocol activation (URL). Returns `bool`, never raises.
- **state.py** (4.20.0) — the one definition of "a recording is in progress": `recording_active(*, default=False)` :23 (any of `state.recording_service.
  active_session_ids()`; no service -> False; a failing probe -> `default`, 4.22.0: the approval gate passes `True` = fail closed, other callers keep False) and `RECORDING_BLOCKED` (frozenset of actions that must not run during a
  recording: `transcribe`, `video_analyze:describe_local`, `transcript_suggest_meta`, `transcript_ai:enrich`, `settings:copilot_disable`).
  The set is data only; nothing enforces it. Callers: `blueprints/transcription.py:931`, `services/reembed.py:238,417`.
- **autosave.py** (4.23.0) — server safety net for a stopped recording. `AutosaveScheduler(store, delay_sec, run_fn, hold_sec=600,
  clock, timer_factory, after_save_fn=None)`: one daemon `threading.Timer` per sid, truth = `manifest['autosave']` `{status, at, reason, fields, outcome}`;
  status pending|held|running|done|failed|cancelled (`off` only in `info()` when disabled). `schedule(sid)` (finalize callback; a prior
  `cancelled` wins, a prior `held` keeps the later deadline), `hold(sid, fields)` (deadline = max(at, now+600 s), keeps name/description/
  category_id, refused once running/done/failed/cancelled), `cancel(sid, reason)` (`/save`, `/discard`; leaves running/done/failed alone),
  `reschedule_pending()` (boot; re-arms pending/held/running at `now+delay`), `info(sid)`, `after_save(sid)` (4.23.3: after the TAB saved,
  runs `after_save_fn` once on a daemon Timer(0); result -> `manifest['autosave']['direction']`; not persisted, a restart drops it; False when
  disabled/no fn). `AutosaveJob.file_saved(sid)` is that fn: direction only, never transcribes; waits <=2 h (`WAIT_TRANSCRIPT_SEC`) for
  the tab's transcript, else `no_transcript`; returns `saved_<tid>_<dir>`. Direction outcomes: `direction_given|ask_user|set|set_meanwhile|patch_<st>`. `AutosaveJob` runs over LOOPBACK HTTP
  (`http_json`): `POST /api/transcribe` source_type=library -> title via `suggest-meta` (waits for summary <=900 s and for no live
  recording <=4 h; 409 `RECORDING_ACTIVE` retried) + `PATCH /api/history/<id>` only when no name -> direction via `suggest-category`
  (waits for embeddings) + `PATCH .../category` only at confidence >= `RECORDING_AUTOSAVE_MIN_CONFIDENCE`. Returns an outcome string
  (`skipped_already_transcribed`, `transcribe_failed_<code>`, `transcribed_<tid>_<title>_<dir>`). `delay_sec<=0` = off.
- **recorder.py** — `WasapiRecorder` (+ `drain_events()` → `[]`), `list_input_devices()`,
  `resolve_device_params()`, `LevelMeter`, `AudioFrame`.
- **finalize.py** — `pcm_to_wav()`, `finalize_session()` (mic+system → overlay → final.mp3). Reads bytes only.
- **pcm_writer.py** — `ChunkedPcmWriter`: O_APPEND, fsync ~5s, lazy-open.
- **session_store.py** — `SessionStore`: a JSON manifest with atomic rename, RLock per sid. Statuses
  recording/paused/stopping/finalized/crashed/discarded. `create(..., system_source=)`,
  `set_stream_source(sid, stream, source, event)`, `stream_source(manifest, stream)` (no `source` → `all`).

- **guards.py** (4.32.0, ADR-051) — pure who-may-drive rules: `OWNER_CLASS={ui,call_prompt,auto_call}`, `AGENT_LABELS={mcp,hook}`, `started_by_for(actor_class, claimed)` (`ui` or label or `unknown`;
  the header is never a proof), `is_owner_session(manifest)` (missing field = owner), `cancel_own_check` (`owner|not_live|too_old`, `CANCEL_OWN_WINDOW_SEC=300`), `recording_kept` (moved here from the blueprint).
- **start_notice.py** (4.32.0) — `notify_start(...)`: toast before an agent-started capture (`capture.rec.notice.*`); True only if shown; no visible desktop / any failure -> False (never raises).
- **stop_control.py** (4.32.0) — `request_stop(sid, name, stop_fn=)` -> `started` (daemon thread `rec-stop-<sid>`) | `already`; `is_stopping`; `arm_timer(sid, minutes, stop_fn, is_live)` / `cancel_timer`
  (wall-clock, in memory only: a restart does NOT re-arm); `reset()` for tests.

## Agent-started sessions (4.32.0, mcp-recording-live)
Manifest gets `started_by` (`session_store.create(started_by=)` :112; `service.start(started_by=)` :379; snapshot carries `started_by`, `max_duration_min`, `expires_at`, `service.py:339-341`).
`recording.py`: `_start_session(data, started_by=)` shared by `/start` (`_caller_started_by` :291) and the call prompt (`'call_prompt'` :851). For an agent class: limit pre-check (409 `SESSION_LIMIT`, no toast),
`start_notice.notify_start` (503 `NOTIFY_UNAVAILABLE` when not shown), copilot body limited to mode/importance and forced local (`_AGENT_COPILOT_SPEND_KEYS`), `max_duration_min` 1..`_MAX_DURATION_MIN`=240
(`_parse_duration_min` :598; writes `expires_at`, arms `stop_control.arm_timer`, :612-628). `_owner_session_refusal` :295 (403 `OWNER_SESSION`) on pause/resume/system-source for a non-`ui` caller.
`/stop` (:905) answers at once `status:'stopping'` (waits `_STOP_GRACE_SEC=0.3`), repeat/finished -> `already:true`; `/discard` refuses 409 `RECORDING_KEPT` and leaves the duration timer armed. Wiki `approval-gate.md`.

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
Tracks in `recording_video_tracks` (`start_offset_sec`); `video_analysis.resolve_recording_video()` -> `{primary_video_path, session_dir,
start_offset_sec}`; thumbs cached in `<session_dir>/frames_cache/thumbs.npz`. Rest — `services.md`, wiki `video-timeline-and-screen-secrets`.
**Live video (4.30.0):** `video.VideoTrack.segments` (`video.py:87`, serialized `:108`) = one `{n, path, start_offset_sec}` per capture file
(start/resume); `_spawn_track` (`:380-385`) appends, an hevc-fallback respawn into the same path refreshes the last entry. `video_probe`
capture argv: `-g int(fps)*2` (keyframe each 2 s) and `-flush_packets 1` (`video_probe.py:217,221`), so the growing fMP4 is readable ~2 s behind.
Reader: `services/live_frames.py` (`services-video-privacy.md`).

## FSM (per session)
recording <-> paused (pause/resume); stop() -> stopping -> finalized; crash -> crashed (recover_orphaned / recover_unfinalized).

## Library (`library.py`)
`register_recording()` — idempotent upsert into `audio_downloads` (`youtube_id='recording_'+sid`), done by the finalize callback.

**Gotchas:** a client on a dead PID streams silence without an error — the watcher is the only death detector;
the watcher truncates include >4 processes to 4 (open() raises in the same situation); the copilot is one per
process (`CopilotBusyError`, `memory/map/copilot.md`); reembed and `suggest-meta` (409 `RECORDING_ACTIVE`) are deferred while at least one recording is alive
(`state.recording_active()`).
**K2:** nothing starts without a click; autostop acts only after «Stop recording?» and not on pause; the watcher lives only
under `__main__` (`app.extensions['call_watcher']`, `app.py:1189`); settings `RECORDING_AUTOPROMPT[_APPS|_SECONDS]`,
`RECORDING_AUTOSTOP_SILENCE_MIN` (`app/core/settings.py:191-226`); HTTP — `memory/map/blueprints.md`.
**Autosave (4.23.0):** settings `RECORDING_AUTOSAVE_SEC` (60; 0 = off), `_MODEL` (large-v3), `_LANGUAGE` (auto), `_MIN_CONFIDENCE` (0.6)
(`settings.py:197-211`); singleton `state.recording_autosave` (`app/state.py:65`). Built in `app.py:858-876` (`_rec_autosave_job` then
`_rec_autosave` with `after_save_fn=_rec_autosave_job.file_saved`) BEFORE recovery; the finalize
callback schedules (`app.py:727-745`, result goes into the callback payload as `autosave`); boot re-arm `app.py:925`. Gotcha: a restart
re-arms with a fresh full delay, and a job interrupted while `running` is re-run (skipped if a transcript already exists).
`discard` is refused 409 `RECORDING_KEPT` once a transcript exists or autosave is running/done (`guards.recording_kept` via `_recording_kept`, `recording.py:1143`).
`/save` (4.23.3): cancels the deadline, then sets `autosave.saved` in one locked write; only the FIRST successful save (`saved` not yet
set) calls `after_save` (`recording.py:826-845`), so a second tab does not file twice.
**SSE** (`recording:{sid}`): level (10Hz), status, chunk_saved, error, source_changed. Since 4.20.0 every event except `level` and
`chunk_saved` is also kept in the broker's per-channel ring (`services-infra.md`) and read by MCP `wait_for`.
**Tests:** `test_recording_devices.py`, `tests/test_{audio_source,app_audio,call_watch,parallel_recordings,
recording_service,recording_blueprint,session_store}.py`.

last-verified: 2026-10-26 (mcp-recording-live 4.32.0: guards, start_notice, stop_control, agent-started sessions; `_recording_kept` is now `guards.recording_kept`, `recording.py:1143`); 2026-10-25 (mcp-telegram-automation 4.31.0: call_watch publisher, prompt_withdrawn; live-video-frames 4.30.0: video segments section); 2026-10-19 (save-direction 4.23.3: after_save/file_saved; recording-autosave 4.23.0; state.py default=, approval gate reader); 2026-10-18 (SSE ring); video tracks section 2026-10-13; the rest 2026-10-08

> **4.26.0:** autosave `http_json` sends the per-boot UI token (`autosave.py:221`), so its loopback calls count as `ui`, not as an agent (ADR-045).
