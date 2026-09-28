# ADR-026: `AudioSource` capture boundary, live retarget and multi-session RecordingService

- Status: accepted (2026-09-28, власник: «Делаешь блок решения один» — прийняти ADR-026 з поправками A1–A4; shipped in 4.7.0 at 70fd028)
- Date: 2026-09-28
- Spec: docs/specs/parallel-recordings-app-audio

## Context

The owner wants two recordings at once with per-application isolation of system audio
(brief Asks 1–13; acceptance = 6 h Zoom conference + 1.5 h work call without Zoom). Today:

- `WasapiRecorder` (pyaudiowpatch) captures a *whole endpoint* loopback; there is no
  per-process capture in the wheel (recon A). Windows offers process loopback
  (`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`): **one target PID per client**, INCLUDE or
  EXCLUDE of that process **tree**, `GetMixFormat` unsupported (report fact-check #1, #3).
  Whether several process-loopback clients run concurrently is unproven (#4) — the wave-1
  spike decides the backend: `proc-tap` in-process vs a Rust `wasapi` sidecar.
- `RecordingService` holds `self._active: Optional[_ActiveSession]` and one global
  `RLock` held up to 5 s in `stop()` (`service.py:184-185, 415-436`). Everything
  downstream is already keyed by `sid` (files, SSE channel, live-transcribe, copilot).
- `_open_stream` takes only a `device_index`; the service already talks to the recorder
  through a duck type (`RecorderFactory`, `service.py:99`) and re-creates recorders on
  `resume()` (`:385-397`). That seam is where a platform boundary fits without rewriting
  the flush loop.
- Owner's binding decisions: no audio hub; retarget-on-the-fly with a manifest mark;
  single-recording behaviour unchanged (Ask 6); Windows only now (Ask 5); no live-DB
  migration during a recording (Ask 13); copilot on one recording, movable (Ask 8);
  MCP must not guess the session (Ask 7).

The decision needed: *where the capture boundary sits, what it promises to both candidate
backends, how a running stream switches source, and how the service becomes multi-session
without touching what already works.*

## Options

1. **Thin `AudioSource` protocol at the existing `recorder_factory` seam; `all` stays
   `WasapiRecorder`; per-app specs get a `ProcessLoopbackSource` (backend behind it);
   retarget = open-new / close-old on the same `system.pcm` with silence padding.** —
   Smallest diff, Ask 6 holds by construction, both backends fit; costs an N-client
   mixer for `include:[A,B]` and a per-event silence pad.
2. **Audio hub** (one capture process taps every sounding app; sessions subscribe). —
   No gaps and free re-routing, but needs session-arrival notifications and continuous
   N-way mixing; owner rejected it (brief).
3. **Everything through the sidecar** (mic and `all` too, PCM over stdout). — One code
   path and native crashes isolated, but breaks Ask 6 (single recording no longer
   byte-for-byte pyaudiowpatch) and makes the spike's verdict a prerequisite for *all*
   recording, not just per-app.
4. **Virtual audio devices + per-app output routing in Windows settings.** — Zero code,
   but driver install and a manual step per call; not an interface Recall controls
   (report option 4).

## Decision

**Option 1.** Deciding factor: the cheapest undo — if the spike fails, the new source type
is simply never constructed and the recorder, manifest and finalize behave exactly as on
`main`. Concretely:

### D1. Capture boundary — `AudioSource` + `SourceSpec`

New module `app/services/recording/audio_source.py`:

```python
class AudioSource(Protocol):            # == today's WasapiRecorder duck type
    sample_rate: int; channels: int
    level: LevelMeter; dropped_frames: int
    def open(self) -> None
    def start(self) -> None
    def stop(self) -> None
    def close(self) -> None             # idempotent
    def drain(self, timeout: float = 0.0) -> list[AudioFrame]
    def get_callback_error(self) -> Optional[Exception]
    def drain_events(self) -> list[dict] # NEW; WasapiRecorder returns []

@dataclass(frozen=True)
class MicSpec:    device_index: int; device_name: str
@dataclass(frozen=True)
class SystemSpec: mode: Literal['all','include','exclude']
                  apps: tuple[str, ...] = ()        # lowercase exe basenames, e.g. 'zoom.exe'
                  device_index: Optional[int] = None  # 'all' only: endpoint loopback index
```

- `WasapiRecorder` **is** an `AudioSource` after adding the one-line `drain_events()`.
  Mic and `system.all` keep constructing it via the untouched `recorder_factory`
  (Ask 6: same device resolution, same rate/channels, same PCM bytes).
- `RecordingService.__init__` gains `app_source_factory: Callable[[SystemSpec, int, int], AudioSource]`
  (spec, sample_rate, channels). It is used **only** for `include`/`exclude`. Default =
  `ProcessLoopbackSource`/`MixedSource` from the backend the spike picks.
- `_StreamState` gains `spec: MicSpec | SystemSpec`; `resume()` rebuilds the source from
  `spec` through the same factory dispatch as `start()` (today it re-reads `device_index`).
- **App identity = exe basename, lowercase** (what the UI shows, what the manifest stores,
  what survives restarts). PIDs are resolved at open time from the **audio sessions** of that
  exe on every active render endpoint (`app_audio.session_roots`), collapsed to their
  same-exe process-tree roots and ordered by active-session count; psutil exe roots are
  only the fallback when the exe holds no session (see A3) — one process-loopback client per
  root, `include_tree=True` so renderer/host children ride along.
- **Mapping to Windows semantics:**
  - `include:[A,B]` → one client per resolved root PID (cap `MAX_INCLUDE_CLIENTS = 4`;
    beyond → `UnsupportedSourceSpecError` at `open()`; if the watcher later resolves more
    than 4 roots it keeps the first 4 and logs a warning), summed by `MixedSource`: int16 saturating add
    over a common frame count; a client lagging > 500 ms is zero-filled so the mix never
    stalls. That is the whole "mixing engine".
  - `exclude:[A]` → exactly one native EXCLUDE client.
  - `exclude:[A,B,…]` (>1) → **rejected**, `UnsupportedSourceSpecError` → HTTP 400
    `UNSUPPORTED_SOURCE_SPEC`. Approximating it as "include everything else" needs
    session-arrival tracking = the audio hub. The owner's scenario excludes exactly one app.
  - `include` app not running → the source opens with zero clients and writes silence
    (timeline preserved); the watcher attaches when the exe appears.
  - `exclude` app not running → EXCLUDE targets Recall's **own PID** (emits no audio, so
    the result is all system audio through the process-loopback path at the fixed format);
    the watcher swaps to the real PID when the app appears. No mid-file format change.
- **PID change / app restart:** `AppAudioSource` (the per-spec wrapper over one or more
  `ProcessLoopbackSource` clients) runs a 2 s watcher thread
  re-resolving exe → root PIDs; on change it opens the new client(s) first, closes the
  dead ones, pads silence for the measured gap, and pushes
  `{'kind':'pid_change','exe','old_pids','new_pids','gap_ms'}` into `drain_events()`.
  The service's flush loop drains these into the manifest (D3) — the source never touches
  the store.

### D2. Backend contract (both spike candidates must fit it)

> Amended by A1/A2 below (2026-09-28): neither `proc-tap` nor the sidecar was built; the
> backend is the in-process comtypes client. The format and queue contract below still hold.

- **Fixed capture format for process loopback: 48 000 Hz, s16le, 2 ch.** It equals the
  service defaults (`sample_rate=48000, channels=2`) and is written to the manifest by the
  existing `set_stream_params`, so finalize, live-transcribe and video alignment see
  nothing new.
- In-process backend (`proc-tap`): callback → bounded `Queue[AudioFrame]` (drop-oldest,
  `dropped_frames++`) exactly like `WasapiRecorder._callback`.
- Sidecar backend (Rust `wasapi`): `recall-loopback.exe --mode include|exclude --pid N
  --rate 48000 --channels 2 --format s16le`; raw PCM on **stdout**, one JSON object per
  line on **stderr** (`{"event":"started"|"error"|"eof", ...}`); spawned with
  `creationflags=NO_WINDOW` via `app/utils/proc.py`; a reader thread fills the same
  `Queue[AudioFrame]`. Process exit with non-zero → `get_callback_error()` non-None, the
  service reports it through the existing `_check_callback_errors` path.
- Either way the Python object above the backend is `ProcessLoopbackSource(pid, mode,
  fmt)`; `MixedSource(list[ProcessLoopbackSource])` sums them. Nothing above the factory
  knows which backend runs.

### D3. Live retarget of the system stream

`RecordingService.retarget_system(session_id, spec: SystemSpec, reason: str) -> dict`,
HTTP `POST /api/recording/<sid>/system-source {mode, apps, reason?}` (a retarget back to
`all` reuses the session's own loopback `device_index`; the body carries none):

1. Under the **session** lock: build and `start()` the new source; drain the old source's
   residual frames into the writer; `stop()`+`close()` the old one; discard new-source
   frames with `timestamp < swap_ts`; swap `_StreamState.recorder/spec`.
2. **Gap handling: pad silence.** After the swap, `expected = elapsed_active_seconds ×
   bytes_per_second` (per-stream rate/channels from the manifest) minus current
   `writer.total_bytes`; if the shortfall exceeds 20 ms, append that many zero bytes.
   Reason: finalize overlays mic/system by byte offsets and video is anchored to
   `audio_start_wallclock`; an unpadded gap would desync the remaining hours. Padding is
   applied only at events (retarget, pid_change), never as continuous drift correction.
3. **Manifest (additive, no `MANIFEST_VERSION` bump):**
   `streams.system.source = {mode, apps, device_index}` (current spec; `all` written at
   `create()` so old manifests without the key read as `all`) and
   `streams.system.source_events = [{ts, elapsed_sec, offset_bytes, from, to, gap_ms,
   padded_bytes, reason}]`, `reason ∈ {'user','pid_change','auto_restore'}`.
   Store method `set_stream_source(sid, stream, spec, event)`; SSE event
   `source_changed` on `recording:{sid}`.
4. **Format immutability:** a stream's rate/channels are fixed when its PCM file is
   created. Retarget to `include/exclude` opens the process source at the stream's format
   (process loopback accepts the format we name). Retarget *back* to `all` on a stream
   that started as `all` reopens the same endpoint index as at start (same native
   params); on a stream that started as `include/exclude`, `all` is realised as
   EXCLUDE-own-PID so the format cannot change mid-file.
5. Pause/resume/recovery: retarget while paused only updates `spec` and logs the event
   with `applied_on_resume: true`; `resume()` opens the current spec. Crash recovery
   (`recover_orphaned`) needs nothing new — the manifest already holds the last spec and
   finalize reads only bytes.
6. Retarget on a session whose system stream is disabled → 409 `NO_SYSTEM_STREAM`
   (enabling a stream mid-recording is out of scope; Ask 1 fixes stream choice at start,
   Ask 4 changes only the *source* of an enabled system stream).

### D4. Multi-session `RecordingService`

- `self._sessions: dict[str, _ActiveSession]` + `self._registry_lock` (held only for
  dict lookup/mutation, never across I/O); each `_ActiveSession.lock: RLock` guards its own
  start/pause/resume/stop/retarget. `stop()` joins the flush thread under the session lock
  only — the neighbour never waits.
- Global: `SessionStore`, broker, factories, finalize callback, and a limit
  `RECORDING_MAX_ACTIVE` (settings registry, default 2 — the accepted scenario; NVENC and
  live-Whisper capacity are unmeasured beyond that). Limit reached →
  `SessionConflictError` → 409 `SESSION_LIMIT`. Unknown sid → `SessionNotFoundError` →
  404 `NOT_FOUND`; `WRONG_SESSION` disappears.
- `active_session_ids() -> list[str]` is the new query. `active_session_id` stays for
  callers: the sole sid when exactly one, `None` otherwise (zero or ambiguous).
- `GET /api/recordings/active` →
  `{success, active: bool, count, sessions: [{session_id, state}], session_id, state}`
  where `session_id`/`state` are filled **only when `count == 1`** (backward-compatible
  reattach for the current SPA and MCP), `null` when 0 or >1.
- **MCP ambiguity rule (Ask 7):** the four live tools with `session_id=None`:
  `count == 1` → that session; `count == 0` → today's `available: false`; `count > 1` →
  `{success: false, error_code: 'AMBIGUOUS_SESSION', sessions: [{session_id, name,
  auto_name, elapsed_seconds, streams, copilot_active}]}` and no further call.
  `get_active_recording` always returns `sessions`. The direct-DB fallback in
  `get_live_copilot` ("newest active copilot_sessions") is legal only because of D5
  (at most one active copilot); if the query returns >1 active rows it answers
  `AMBIGUOUS_SESSION` instead of picking.
- Ask 10 needs no new mechanism: `_inference_semaphore` (`WHISPER_MAX_PARALLEL=1`)
  already serialises full transcription against live workers; `live_transcribe_worker.stop(sid)`
  and `_end_copilot_for(sid)` are per-sid and untouched.
- No SQLite schema change anywhere in this spec: all new state lives in the manifest
  JSON and in memory (Ask 13).

### D5. Copilot single owner + move

- The invariant lives in the **service**, not the blueprint: `CopilotService.start(...)`
  raises `CopilotBusyError(owner_recording_session_id)` when a `copilot_sessions` row is
  `status='active'` for a *different* recording that `is_recording_active(sid)` reports
  live. `CopilotService` receives `is_recording_active: Callable[[str], bool]` at
  construction (wired in `app.py` to `recording_service`), so MCP or any future caller
  cannot bypass it; stale active rows whose recording is gone are ended as housekeeping
  inside the same call. Blueprint maps it to 409 `COPILOT_BUSY` with `owner_session_id`.
- Move = `POST /api/copilot/move {from_session_id, to_session_id, settings?}` implemented
  as `_end_copilot_for(from)` then `copilot_service.start(settings or previous config,
  recording_session_id=to)` + `copilot_worker.start`. Not atomic by design: if the start
  fails, the response is `COPILOT_MOVE_FAILED` with `ended_from: true`; A's history stays
  (ended row), as the owner accepted.

### D6. Enumerating sounding apps

`app/services/recording/app_audio.py::list_sounding_apps()` (pycaw + comtypes, pinned):
enumerate **all** active render endpoints (`eRender`, `DEVICE_STATE_ACTIVE`), then
`IAudioSessionManager2 → GetSessionEnumerator → IAudioSessionControl2.GetProcessId/GetState`.
Include sessions in state **Active and Inactive** (skip Expired) — a muted Zoom meeting is
Inactive and is exactly the app the owner needs to see. Skip PID 0 (system sounds) and
Recall's own PID. **Dedupe by lowercase exe basename**, union PIDs, `state='active'` if any
session is active, `endpoints=[names]`, `recorded_by=[sid…]` cross-referenced from live
sessions' `SystemSpec` (`all` counts as recording every app). Each call wraps
`CoInitialize/CoUninitialize` (Flask worker threads). Endpoint `GET /api/recording/apps`
never returns 500: on COM failure `{available: false, apps: []}`. No MCP tool (not asked).

### Rejected in one line each

- Audio hub — owner's decision; premature for two recordings.
- Sidecar for every source — violates Ask 6 and couples ordinary recording to the spike.
- `exclude` of several apps via "include the rest" — hidden audio hub.
- Continuous drift correction between mic and system — not this spec; pad only at events.
- Keying apps by PID in spec/manifest — dies with the process; exe name survives restarts.
- Adding the sounding-apps list as an MCP tool — read-first would allow it, nobody asked.
- Enabling/disabling a stream mid-recording — Ask 1 fixes streams at start.

## Consequences

Easier: the single-recording path is untouched (`WasapiRecorder` + one method), so
regressions from the new source type are confined to sessions that use it; the spike's
backend choice swaps one factory; macOS/Linux later implement `app_source_factory` and
`list_sounding_apps()` only; tests inject a fake `AudioSource` exactly as they inject
`recorder_factory` today.

Harder / debt accepted: `MixedSource` is a small hand-rolled mixer (saturating add,
zero-fill lag); silence padding at retarget makes a measured gap audible as a click-free
hole rather than a shorter track; `exclude` of >1 apps is an explicit 400 until a real need
appears; `RECORDING_MAX_ACTIVE=2` is a guess bounded by the accepted scenario, not a
measurement; the copilot move is two steps, not a transaction. `WRONG_SESSION` consumers in
`record.js` must switch to `NOT_FOUND`.

## Invariants created

1. `RecordingService` reaches every audio source only through the `AudioSource` protocol
   (`open/start/stop/close/drain/level/get_callback_error/drain_events`); no `sys.platform`
   branches and no backend imports above `audio_source.py` / the backend module.
2. A system stream with `SystemSpec.mode == 'all'` at session start is captured by
   `WasapiRecorder` on the resolved endpoint index — the single-recording PCM is
   byte-for-byte what `main` produced before this spec.
3. A stream's `sample_rate`/`channels` never change while its `.pcm` exists; process
   loopback is always opened at 48 000 Hz / s16 / 2 ch; a retarget that cannot keep the
   format is realised as EXCLUDE-own-PID, never as a format switch.
4. Every source change (user retarget, pid_change, auto_restore) appends one entry to
   `streams.system.source_events` and pads silence for any gap > 20 ms, so
   `writer.total_bytes / bytes_per_second == elapsed_active_seconds` within 20 ms at every
   event boundary.
5. `SystemSpec` identifies apps by lowercase exe basename only; PIDs are transient
   resolution results and never appear in the spec or the UI toggle.
6. `exclude` carries exactly one app; `include` carries at most `MAX_INCLUDE_CLIENTS`; any
   other spec is rejected at the API boundary with `UNSUPPORTED_SOURCE_SPEC`.
7. No lock in `RecordingService` is held across I/O of more than one session; the registry
   lock guards the dict only.
8. `/api/recordings/active` fills `session_id`/`state` only when exactly one session is
   live; MCP live tools without `session_id` return `AMBIGUOUS_SESSION` when more than one
   is live and never pick.
9. At most one `copilot_sessions` row is `status='active'` for a live recording at any
   time; the check is in `CopilotService.start`, not in a blueprint.
10. This spec adds no SQLite migration; all new recording state is manifest JSON
    (additive keys, readers default missing `source` to `all`).

## Amendments (accepted 2026-09-28 with the ADR, from the spec's Plan deltas)

Harvested after ship (4.7.0, 70fd028); accepted by the owner together with the ADR. As
built, the process-loopback source pads to wall clock only when the shortfall exceeds 50 ms
(`audio_source.py`), so A2's "equal to elapsed wall-clock time" holds to within 50 ms.

### A1. Backend = in-process comtypes process-loopback client; proc-tap forbidden

Source delta (plan.md, "2026-09-28 — spike-decision (Queen)", decision 1, verbatim):

> Backend = **in-process**, built on the spike's own comtypes/pycaw process-loopback client (`scripts/spikes/process_loopback/ploop.py`), promoted into `app/services/recording/process_loopback.py`. **proc-tap is NOT used and NOT pinned**: it only does INCLUDE (no `exclude:[zoom]`, no EXCLUDE-own-PID), only float32, and silently falls back to whole-endpoint capture if activation fails. That fallback would put the whole system audio into a recording the owner asked to isolate. Pin `pycaw` and `comtypes` at the versions spike.md records. The Rust sidecar is not built.

Rejected (verbatim): "pinning proc-tap for INCLUDE only (two capture code paths, plus the silent-fallback hazard); a sidecar (in-process passed concurrency and soak with no native crash)".

Invariant: a process-loopback activation failure surfaces as an error
(`get_callback_error()`), never as a fallback to whole-endpoint capture. No backend with a
silent endpoint fallback may sit behind `ProcessLoopbackSource`.

### A2. Wall-clock placement inside the process-loopback source

Source delta (decision 2, verbatim):

> **Wall-clock placement** (spike e): an EXCLUDE stream delivered ~0.6 % fewer bytes than wall-clock time with no discontinuity flag. Over 6 h that is ~2 min of drift against mic/video. So the process-loopback source pads silence to keep its cumulative frame count equal to elapsed wall-clock time × 48 000. The service keeps writing by byte count, as it does today.

This narrows D3.2 ("padding is applied only at events … never as continuous drift
correction") and the rejected line "Continuous drift correction … pad only at events": the
*service* still pads only at events; the *process-loopback source* pads continuously
against wall clock. `WasapiRecorder` (`all`) is unchanged. Alternatives: none recorded -
the delta states the chosen shape only.

Invariant: a process-loopback source's cumulative frames equal elapsed wall-clock × 48 000.

### A3. Targets are audio-session PIDs, collapsed to tree roots, sounding tree preferred

Source deltas, verbatim — decision 3:

> **Targets by audio-session PID** (spike c, i): include-tree reaches direct children only. So `SystemSpec.apps` (exe names) resolve to the PIDs that own an audio session on any active render endpoint, not only to the exe root. Story 02 takes an injectable `resolve_pids(exe) -> list[int]` (default: psutil exe roots). Story 05 wires in story 03's session-PID resolver. The include cap of 4 counts resolved session PIDs.

and "2026-09-28 — stage-05 rejection (owner) → story 11":

> With the owner's real Zoom running, `exclude:[zoom.exe]` excluded the idle real Zoom tree (lowest PID 21900), and a sounding `zoom.exe` stayed in the recording. […] collapse session PIDs to process-tree roots and prefer the tree with an active audio session.

Replaces D1's "PIDs are resolved … the *root* processes of that exe". Rejected (verbatim):
"widening to multi-app exclude (owner: «только под исправление исключения»)" — Invariant 6
stands.

Invariant: exe → PID resolution goes through audio-session ownership, collapsed to tree
roots; when several trees of one exe exist, `exclude` targets the tree with an Active audio
session, never "lowest PID".

### A4. The PID watcher is mandatory and is the only death detector

Source delta (decision 4, verbatim):

> **PID watcher is mandatory** (spike f): a client on a dead or dead-born PID streams silence without an error. The ADR's 2 s re-resolve loop stays, and it is the only death detector.

Invariant: no process-loopback source runs without the re-resolve watcher; client silence
is never treated as proof the target is alive.

Consequences / Revisit when for A1–A4: not recorded.

## Revisit when

- The spike shows concurrent process-loopback clients do not work → stop, back to the
  owner (brief Ask 11); this ADR's D1/D2 are moot.
- The owner needs `exclude` of two or more apps, or more than two simultaneous recordings
  (`RECORDING_MAX_ACTIVE`), or a second copilot — each reopens the corresponding rule.
- A macOS/Linux backend arrives: check that `SystemSpec` (exe basename) still names an app
  there (bundle id / PipeWire node) before extending the dataclass.
- Measured retarget gaps exceed ~100 ms in practice, or padding proves audible in
  finalized files — reconsider overlap-then-swap timing or per-event crossfade.
- `proc-tap` or the `wasapi` crate changes its process-loopback API (both are small
  projects) — the factory swap is the intended escape hatch.
