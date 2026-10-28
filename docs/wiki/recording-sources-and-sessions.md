---
domain: recording
tags: [recording, audio-source, process-loopback, multi-session, copilot]
related: [rag-context-integrity]
last-verified: 2026-10-28 (4.37.0)
---

# Recording: audio sources, parallel sessions, single copilot

Source: ADR-026 (+ amendments A1-A4), shipped in 4.7.0; autosave 4.23.0 (`docs/specs/recording-autosave/plan.md`). Code: `app/services/recording/`.

- **Sessions.** `RecordingService` holds up to `RECORDING_MAX_ACTIVE` (default 2) live sessions in a
  `sid → _ActiveSession` registry. The registry lock guards the dict only; each session has its own
  RLock, so one session's slow stop never blocks another. Over the limit → `SessionConflictError`
  → HTTP 409 `SESSION_LIMIT`. `active_session_id` is non-None only when exactly one session is live;
  any "is something recording?" check must use `active_session_ids()`: the one definition is `recording.state.recording_active()` (reembed defer and `suggest-meta` use it).
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
- **Call autoprompt never records on its own unless the owner opts in (K2, 4.9.0).** With the default
  `RECORDING_AUTOPROMPT_ACTION=prompt` `call_watch.py` only asks; `record` is the single opt-in that starts a
  confirmed call by itself and shows "Recording started" (Stop / Keep), a failed start falls back to the prompt.
  An unrecorded, undismissed call gets one repeat after `RECORDING_AUTOPROMPT_REPROMPT_MIN`. "Record?" starts
  the Windows default mic + `{mode:'all'}` on the default loopback (`start_call_recording` in
  `app/blueprints/recording.py`; the call's exe is only logged) solely on a click of a single-use token URL; "Stop?" after
  the call ends, then `RECORDING_AUTOSTOP_SILENCE_MIN` of silence on both tracks stops that recording through
  the normal stop path (finalized whole). Only apps on the `RECORDING_AUTOPROMPT_APPS` allow-list can be a
  call; a failed session enumeration holds state rather than reading as «call ended».
- **A stopped recording is always saved (4.23.0, spec `recording-autosave`).** Whoever stops it (own Stop button, notification,
  MCP, K2 autostop, another tab), finalize sets a server deadline `RECORDING_AUTOSAVE_SEC` (default 60; 0 = off) in
  `manifest['autosave']`; the save screen counts down to the same moment. Editing the save screen only ever moves the deadline out
  (hold, +10 min) and the typed name/description/direction travel with it and win over any guess. `/save` and `/discard` cancel it. At
  the deadline the server runs the UI's own write path over loopback HTTP (transcribe, then title, then direction) so duplicate guard
  and enrichment are identical. The direction is set only at k-NN confidence >= `RECORDING_AUTOSAVE_MIN_CONFIDENCE` (0.6), otherwise it
  stays empty and the Audio library asks. The title waits while any recording is live (the local model is blocked then). Deadlines
  survive a restart (re-armed with a fresh delay). **Invariant:** once a transcript exists or autosave is running/done, discard is
  refused (409 `RECORDING_KEPT`), because deleting `final.mp3` would orphan the transcript.
- **Participants are owner-picked, existing persons only (4.36.0, spec `wave2-recording-calls`).** The start form's picker
  (prefilled from calendar events overlapping now +- 10 min) stores person ids in the manifest `participants`;
  transcription links them in `meeting_entities` with `source='owner'`, which re-enrich keeps like `calendar`
  (`app/services/recording/participants.py`). No entity is created. A transcript with an empty description inherits the
  recording's. Gain (+-20 dB per stream) is stored in the manifest and applied once at finalize.
- **Denied device is a 403, not a 400.** Windows refusing the mic (consent store) or a loopback activation
  (`E_ACCESSDENIED`) raises `DeviceAccessDeniedError` -> 403 `DEVICE_ACCESS_DENIED` with `stream`; a stream with no
  distinguishable signature keeps 400 `RECORDING_START_FAILED`.
- **Consent reminder is display only (4.37.0).** Every start path (UI `/start`, call prompt, auto-record, agent `start_notice`) carries the "warn the other party" line while `RECORDING_CONSENT_REMINDER` is on; the manifest keeps `consent_reminder: true` so an attach shows it. Recording never waits on it and nothing else is stored.
- **"What did I miss" never uses the local model and never an agent recording (4.37.0).** `live_missed` calls Claude only (the local LLM shares `_GEN_LOCK` with the copilot ticks); an agent-class session is refused for every caller (403 `AGENT_SESSION`); spend is capped per call by `LIVE_MISSED_BUDGET_USD`, kept in memory, nothing persisted.
- **No guessing the session.** `/api/recordings/active` fills `session_id`/`state` only when exactly
  one session is live; MCP live tools and `/api/copilot/live-ask` without `session_id` answer
  `AMBIGUOUS_SESSION` when more than one is live.
