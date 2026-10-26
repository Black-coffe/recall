# ADR-051: Agent-started recordings: provenance, mid-call stop through the dialog, fail-closed start notice

- Status: accepted (owner, 2026-10-26; shipped in 4.32.0)
- Date: 2026-10-25
- Spec: docs/specs/mcp-recording-live (Tier 4; built after docs/specs/mcp-telegram-automation)

## Context
ADR-042 made the MCP a full-control surface with an R/W/D registry and a native Windows dialog for every D
action. Its recording rule (decision 5, "Recording (F11, Ask 4)") refuses any non-`ui` D request while a recording
runs, with no dialog, so the owner's call is never interrupted by an agent. Spec 7 now gives the agent the recording
itself: start, pause, resume, retarget, stop and cancel, plus the copilot nouns. Two of its D actions,
`recording_stop` and `recording_cancel_own`, only make sense while a recording runs, so they collide with that rule.

The owner's binding answers (report §11-12, brief Asks):

<!-- verbatim:start - owner asks, kept in the original language with the English translation -->
Ask 3: "Пауза/продолжение/смена источника — только для записей агента; твои записи агент может только
остановить." (Pause, resume and source change only on the agent's own recordings; the owner's recordings the agent
may only stop.)
Ask 4: "Остановка — через окно Windows, работает посреди звонка, ответ сразу; отмена своей свежей (<5 мин)
записи — тоже через окно." (Stop through the Windows window, works mid-call, answers at once; cancelling its own
fresh (<5 min) recording also through the window.)
Q2: "Без уведомления — не запускать" (no notification, no start).
<!-- verbatim:end -->

Facts that shape the design (verified 2026-10-25):
- `app/core/approval_gate.py:213` is the mid-recording refusal; `:104` `_CONDITIONAL_D_ROUTES` gates a route for
  non-`ui` callers only; `:222` answers a `Precondition` from the summary builder in the view's own shape.
- `app/core/auth.py:114` `classify_request()` proves only `ui`; `X-Recall-Actor` is a label. There is no
  `_HOOK_ROUTES`: a hook is an ordinary non-`ui` caller.
- The manifest (`app/services/recording/session_store.py:130-172`) has no `started_by`; `/state`
  (`service.py:282`) and `/api/recordings/active` (`recording.py:1346`) do not expose who started a session.
- `recording.py:605` `autoprompt_action` starts a recording from the K2 toast through `_start_session` with a
  one-shot token; its HTTP caller is a browser hit from the toast, so `classify_request()` says `unknown` although
  the owner pressed the button.
- `recording.py:668` `stop_session` is synchronous: `service.stop` (`service.py:630`, flush join up to 5 s, stream
  close, finalize callback which submits the `recording_finalize` job) then `live_transcribe_worker.stop(wait=True)`
  and the copilot end. A second stop gets 404 (`:706`). The K2 autostop already calls it from a background thread.
- `approvals.py:59` `_station_visible` is private and lives inside `_show_dialog`; the toast module
  (`app/services/recording/toast.py`) posts through WinRT and returns `bool`, never raises; `ToastNotifier`
  (`call_watch.py:124`) exists only when `RECORDING_AUTOPROMPT=1` (`app.py:1250`).
- `copilot.py:533` `/stop` calls `service.end` only; the worker is stopped by `end_for_recording` (`:58`).
  `copilot/config.py:134-142` accepts `api_enabled`/`budget_usd` from the request.
- Spec 8 (`mcp-telegram-automation`) ships first and also edits `D_ACTIONS`, `_D_ROUTES`,
  `approval_summaries.BUILDERS`, `_TOOL_CLASS`, the approvals i18n catalogue and `test_endpoints_smoke.PLAN_A1`.

## Options

Fork 1, mid-call D actions:
1. Per-action allowlist in the gate: `recording_stop` and `recording_cancel_own` skip only the mid-recording
   refusal; dialog, ceiling, one-at-a-time and audit stay. One constant, one condition. Chosen.
2. Move the refusal into each summary builder. Rejected: the refusal would leave the gate and the invariant "a
   non-`ui` D request during a recording is refused before any dialog" would have as many copies as builders.
3. Make stop a W action with no dialog. Rejected: contradicts Ask 4 and A1.

Fork 1b, who gets the dialog on stop/discard:
1. `_CONDITIONAL_D_ROUTES` with an always-true predicate: a non-`ui` caller gets the dialog, the owner's own Stop
   button does not (precedent: re-transcribe eviction and the Claude video description, 4.26-4.27). Chosen.
2. `_D_ROUTES`: the owner's Stop button also raises a Windows dialog on every call end, and the SPA stop/discard
   buttons need the "waiting for the Windows window" state. Rejected for now; it is a one-line move if the owner
   wants it (see Revisit when).

Fork 2, `started_by`:
1. A `started_by` keyword on `RecordingService.start` written by `SessionStore.create` into the manifest, default
   `'ui'`. Chosen: a new manifest never lacks the field, every reader (state, active, guards, summary builders)
   reads one place, every existing caller and test is unchanged.
2. `store.modify` in the route after `start()` returns. Rejected: a window in which a live manifest has no field,
   and crash recovery of that window writes a manifest without provenance.
3. Values: owner class `ui` (UI token proof), `call_prompt` (the K2 toast token proof, passed explicitly by
   `autoprompt_action`), reserved `auto_call` (Q3, not written yet); agent class `mcp`, `hook` (the sanitised
   `X-Recall-Actor` label when it is one of these two) and `unknown` (any other non-`ui` caller). The class is the
   proof; `mcp` versus `hook` is a claim, shown as such. A manifest without the field is owner class (recordings
   from before this release).

Fork 3, toast-before-start:
1. A small service module `app/services/recording/start_notice.py` that checks the window station through a new
   public `approvals.station_visible()` and posts a Recall toast through `toast.ensure_registered` + `toast.show`;
   `_start_session` calls it for an agent-class start before `recording_service.start` and refuses with 503
   `NOTIFY_UNAVAILABLE` when it returns `False`. Chosen: no dependency on the K2 watcher being enabled, the same
   no-desktop probe as the dialog, a module attribute tests stub like `_show_dialog`.
2. Reuse `ToastNotifier` from `app.extensions['call_watcher']`. Rejected: absent when `RECORDING_AUTOPROMPT=0`.
3. A `MessageBoxW` confirmation before start. Rejected: the owner asked for a notification, not an approval, and a
   modal box at the start of a call is the wrong interruption.

Fork 4, background stop:
1. A daemon thread per stop in a new `app/services/recording/stop_control.py` with a `stopping` registry
   (`request_stop` returns `started` or `already`), the whole `stop_session` as the thread body. Chosen: the K2
   autostop already runs `stop_session` off a thread, the manifest status and SSE already carry progress, nothing
   needs a job row.
2. A `JobQueue` kind on the live executor. Rejected: a wrapper for `fn(job, ...)`, a kind to register, a job row
   nobody asked to poll; the live executor is one worker and finalize runs there.

Fork 5, duration timer: `threading.Timer` in `stop_control` (the pattern of `autosave.py:53`), wall-clock,
cancelled by every stop and discard path, expiry goes through `request_stop`. Rejected: a tick inside the recorder's
flush loop (touches the capture path) and re-arming after a restart (a restart marks live sessions crashed anyway).

Fork 6, copilot clamp: server-side at recording start for an agent-class start (keys `api_enabled`, `budget_usd`,
`escalate` present → 400 `INVALID_ARGUMENT`; otherwise forced `api_enabled=false`, `budget_usd=0`), and the clamp
is bound to the copilot session, not the caller. Rejected: clamping in the MCP tool only (the label is not proof).

## Decision
1. **Mid-call D actions.** `approval_gate.py` gains `RECORDING_ALLOWED_ACTIONS = frozenset({"recording_stop",
   "recording_cancel_own"})` next to `D_ACTIONS`; the condition at `:213` becomes `actor != "ui" and action not in
   RECORDING_ALLOWED_ACTIONS and recording_active(default=True)`. Everything else in the gate is unchanged for
   these actions: the per-class ceilings (3/min, 20/day), `APPROVAL_PENDING`, the 45 s dialog, the audit row.
   `recording.stop_recording` and `recording.discard_recording` enter `_CONDITIONAL_D_ROUTES` with a predicate that
   always holds, so the owner's own Stop and Discard in the SPA stay as they are. A hook is a non-`ui` caller and
   stops only through the dialog (A5 settled).
2. **Dialog text (A3).** Two builders in `approval_summaries.py` read the manifest through
   `state.recording_service.store` and the live snapshot, never the request: `_recording_stop` → `Summary(
   "recording_stop", kind "recording", name = manifest name or auto_name, id = session id, count = whole minutes
   recorded so far, undo = False, extra = owner|agent)`; `_recording_cancel_own` → the same with count = seconds
   recorded. `_KIND_TABLE["recording"] = "recording_sessions"`. The `name` an agent sends with a stop reaches the
   dialog only through the existing `X-Recall-Note` contract (quoted, 60 characters); the body `name` (≤120) is
   applied by the view after approval. Builder answers without a dialog: manifest missing → `None` (view 404);
   session not live or already in the stopping registry → `Precondition("already_stopping", 200, {success, already:
   true, status})`, the first 200 precondition, so a repeat never reaches a destructive view; for cancel_own:
   owner-class session → 403 `OWNER_SESSION`, not live or recorded ≥ 300 s → 409 `CANCEL_WINDOW_CLOSED`, kept
   (autosave running/done or a transcript exists) → 409 `RECORDING_KEPT`.
3. **`started_by` provenance.** `SessionStore.create(started_by='ui')` writes the field; `RecordingService.start`
   passes it through; `_start_session(data, *, started_by)` derives it as in Fork 2 option 3 from
   `classify_request()` and the sanitised `X-Recall-Actor`; `autoprompt_action` passes `'call_prompt'`. `/state`,
   `/api/recordings/active` and `get_active_recording` expose `started_by` (missing → `'ui'`). The owner-session
   guard is a pure function in a new `app/services/recording/guards.py` (`OWNER_CLASS = {'ui', 'call_prompt',
   'auto_call'}`, `is_owner_session(manifest)`, `cancel_own_check(...)`, the moved `_recording_kept` logic) and is
   enforced in the blueprints: pause, resume and system-source refuse a non-`ui` caller on an owner-class session
   with 403 `OWNER_SESSION`; stop is allowed on any session; the copilot routes settings, action, stop and
   reingest apply the same guard through the session's `recording_session_id` (no bound recording or unreadable
   manifest → owner class → 403). MCP checks nothing the server does not check.
4. **Toast-before-start, fail closed.** For an agent-class `started_by`, `_start_session` validates the body
   (streams, source spec, video flag, duration, copilot clamp), then calls `start_notice.notify_start(...)` and
   starts capture only on `True`; `False` → 503 `NOTIFY_UNAVAILABLE`, nothing started, nothing written. The text
   is built server-side in the owner's UI language from the resolved request: microphone name, system apps or
   "all system audio", language, the recording name, "with screen" when `video.enabled` is true, and "claims to
   be <label>". `approvals.station_visible()` becomes public and returns `False` on any error or off Windows.
   Owner-class starts (UI, K2 toast) post no toast.
5. **Background stop.** `POST /<sid>/stop` answers at once: live session → `stop_control.request_stop(sid, name,
   stop_fn=stop_session)` → 200 `{success, session_id, status: "stopping", already: false}`; a repeat while the
   thread runs, or a manifest that is no longer live → 200 `{..., already: true}`; no manifest → 404. The K2
   toast's `stop` action keeps its synchronous call. `stop_session` and `discard_recording` call
   `stop_control.cancel_timer(sid)` so every path disarms the timer.
6. **Duration timer.** Body `max_duration_min`: integer 1..240, else 400 `INVALID_ARGUMENT`; default 180 for an
   agent-class start, none for the owner's starts unless sent. Stored in the manifest as `max_duration_min` and
   `expires_at` (ISO, exposed by `/state`); armed with `threading.Timer` after `start()` returns; expiry calls
   `request_stop` only if the session is still live. Wall-clock: a pause does not extend it. Not re-armed after a
   restart.
7. **Copilot clamp and stop.** Agent-class start: `copilot` keys `api_enabled`, `budget_usd`, `escalate` → 400
   before anything starts; the stored config is `api_enabled=false`, `budget_usd=0` (importance and mode as sent;
   importance is allowed by Ask 5). `copilot_service.update_settings` must keep `api_enabled=false` for such a
   session whatever the importance resolves to (the worker must verify the resolve path, `service.py`, and clamp
   there if it re-derives the flag). `/action` refuses `escalate` from a non-`ui` caller with 400 and stamps
   `payload.actor` server-side (`ui`, or the sanitised label among `mcp`/`hook`, else `unknown`); feedback stats
   count only rows whose `actor` is absent or `ui`. `/stop` stops the worker (`copilot_worker.stop(rec_sid,
   wait=False)`) before `service.end`. Reingest is deduplicated by an additive column
   `copilot_sessions.reingest_transcription_id` (plus `reingest_by`): a repeat returns the same id with
   `created: false`.
8. **Error codes** added to `app/core/error_codes.py`: `OWNER_SESSION` 403, `NOTIFY_UNAVAILABLE` 503,
   `CANCEL_WINDOW_CLOSED` 409.
9. **Merge with spec 8.** Every edit this spec makes to a shared table is append-only in its own commented block
   (`# mcp-recording-live`): `D_ACTIONS`, `_CONDITIONAL_D_ROUTES` (not `_D_ROUTES`, so `PLAN_A1` in
   `test_endpoints_smoke.py` is untouched), `BUILDERS`, `_KIND_TABLE`, `_TOOL_CLASS.update({...})`, the approvals
   catalogue, `error_codes`. Stories name no line numbers from the recon as targets; the tool count in docs and
   `test_tool_count_docs` is taken from the tree after spec 8 ships.

## Consequences
The agent can run its own recordings end to end and stop the owner's; the owner always sees a toast before an
agent capture and a Windows window before an agent stop or cancel, mid-call included; a stop returns in
milliseconds and the UI keeps its current polling/SSE flow because the manifest status and events are unchanged.
Harder: the gate now has one action set that bypasses one of its checks (documented, tested); the stopping
registry is in-process (a crash during a stop leaves the manifest `stopping`, which crash recovery already
handles); a second interactive session is still out of scope. Accepted debt: the owner's own Stop is ungated
(Fork 1b), `mcp` versus `hook` in `started_by` is a label, the timer is not re-armed after a restart, `escalate`
stays UI-only.

## Invariants created
- A non-`ui` D request during a recording is refused before any dialog unless its action is in
  `RECORDING_ALLOWED_ACTIONS` (`recording_stop`, `recording_cancel_own`); those two keep the dialog, the ceilings,
  the one-at-a-time rule and the audit row.
- `started_by` is written once, at manifest creation, from a server-side proof: `ui` (UI token), `call_prompt` (K2
  toast token), reserved `auto_call`; every other caller is agent class (`mcp`, `hook` from the sanitised label,
  else `unknown`). A manifest without the field is owner class. Nothing in a request can set the owner class.
- A non-`ui` caller may pause, resume, retarget or operate the copilot only on an agent-class session (403
  `OWNER_SESSION` otherwise); stop is allowed on any session. The guard runs in app.py, never only in the MCP.
- An agent-class start writes nothing and opens no stream until a Recall toast was posted on a visible window
  station; no toast, no start (503 `NOTIFY_UNAVAILABLE`). The toast text is built by the server from the resolved
  request and says "with screen" whenever video is requested.
- The dialog text for a recording action comes from the manifest and the live snapshot; the request's `name`
  appears only as the quoted agent note. A stop or cancel of a session that is not live, or already stopping,
  never runs the view: the gate answers `already: true` (200) or the refusal code.
- Every stop and discard path disarms the duration timer; expiry stops through the same `request_stop` as the
  route. An agent-class start without `max_duration_min` expires at 180 min; 240 is the ceiling for every caller.
- A copilot session bound to an agent-class recording has `api_enabled=false` and `budget_usd=0` for its whole life;
  `escalate` is refused for non-`ui` callers; agent card feedback never enters the feedback statistics.

## Revisit when
The owner asks for the Windows window on her own Stop too (move the two routes from `_CONDITIONAL_D_ROUTES` to
`_D_ROUTES` and add the SPA waiting state); Q3 auto-record ships (`auto_call` becomes a written value and the
toast rule for it is decided); a hook needs to start recordings without a toast (then a hook proof must be added in
`classify_request`, never a gate exemption); a stop thread is observed hanging on `live_transcribe_worker.stop`
(bound the wait, keep the registry); a second OS user or interactive session runs Recall.
