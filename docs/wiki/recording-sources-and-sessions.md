---
domain: recording
tags: [recording, audio-source, process-loopback, multi-session, copilot]
related: [rag-context-integrity]
last-verified: 2026-09-28
---

# Recording: audio sources, parallel sessions, single copilot

Source: ADR-026 (+ amendments A1-A4), shipped in 4.7.0. Code: `app/services/recording/`.

- **Sessions.** `RecordingService` holds up to `RECORDING_MAX_ACTIVE` (default 2) live sessions in a
  `sid → _ActiveSession` registry. The registry lock guards the dict only; each session has its own
  RLock, so one session's slow stop never blocks another. Over the limit → `SessionConflictError`
  → HTTP 409 `SESSION_LIMIT`. `active_session_id` is non-None only when exactly one session is live;
  any "is something recording?" check must use `active_session_ids()` (reembed defer does).
- **Source boundary.** The service reaches audio only through the `AudioSource` protocol
  (`audio_source.py`). System audio is a `SystemSpec`: `all` | `include:[1..4 exe]` |
  `exclude:[exactly 1 exe]`; anything else is `UNSUPPORTED_SOURCE_SPEC` (400) before any start.
  Apps are lowercase exe basenames; PIDs are never stored in the spec, manifest or UI.
- **`all` at start is unchanged**: `WasapiRecorder` on the endpoint index, byte-for-byte the pre-4.7
  path. `include`/`exclude` use in-process WASAPI process loopback (comtypes); no backend with a
  silent fallback to whole-endpoint capture may sit behind it (proc-tap is forbidden for that reason).
- **Format immutability.** A stream's rate/channels never change while its `.pcm` exists. Process
  loopback is always 48 000 Hz / s16 / 2 ch. Switching to `all` on a stream that started as
  include/exclude is realised as EXCLUDE-own-PID, never as a format switch.
- **Timeline integrity.** Retarget opens the new source first, drops its frames older than the swap,
  and pads silence for any gap > 20 ms. The process-loopback source additionally pads continuously
  to wall clock (EXCLUDE streams under-deliver ~0.6 %). Finalize and video alignment rely on byte
  offsets matching elapsed active time.
- **Every source change is recorded**: one entry in `streams.system.source_events`
  (`reason` user|pid_change|auto_restore) + SSE `source_changed`. Additive manifest keys, no
  `MANIFEST_VERSION` bump; a manifest without `source` reads as `all`. No SQLite migration.
- **Target resolution.** exe → PIDs via audio-session ownership on all active render endpoints,
  collapsed to process-tree roots, the tree with an Active session first; `exclude` targets only that
  tree. A client on a dead PID streams silence without error, so the 2 s PID watcher is mandatory
  and is the only death detector.
- **Copilot: one owner per process.** `CopilotService.start` raises `CopilotBusyError` when another
  live recording owns an active copilot row (enforced in the service, not the blueprint). Move is
  end-then-start, not atomic (`COPILOT_MOVE_FAILED`, `ended_from: true`).
- **No guessing the session.** `/api/recordings/active` fills `session_id`/`state` only when exactly
  one session is live; MCP live tools and `/api/copilot/live-ask` without `session_id` answer
  `AMBIGUOUS_SESSION` when more than one is live.
