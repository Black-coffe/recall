# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [4.38.0] — 2026-10-28

### Added
- Settings → Calendars: a collapsed step-by-step guide «How to connect Google Calendar» with six English screenshots (personal data blurred, archive counters cropped out) and uk/en captions, plus a note for Google Workspace accounts whose admin hides the secret iCal address.

### Fixed
- The one-time connect page banner names its own kind: a calendar page no longer says «connect this mailbox / set this key».
- `docs/adr/057` prose is English again, so `test_docs_english` is green.

## [4.37.0] — 2026-10-28

### Added
- Pre-meeting brief: a Telegram card `MEETING_BRIEF_LEAD_MINUTES` (default 15) before a calendar event with its people, their open tasks and the last calls with them; one card per event, the briefing privacy filter, a failed send is retried until the event starts and never counted as sent (`MEETING_BRIEF_ENABLED`, migration v60 `meeting_brief_sent`). `GET /api/meeting-brief?event_id=|person=` and the MCP tool `get_meeting_brief` build the same card for an event or for a person by name, without a calendar.
- "What did I miss" on the record view and as the MCP tool `live_missed`: Claude Sonnet sums up the call since your last utterance, the last 2/5/10 minutes or the whole call (the live summary) in 3-5 lines - decided, promised, asked of you. Per-call spend cap `LIVE_MISSED_BUDGET_USD` (default 0.10); refused for agent recordings; never uses the local LLM.
- Copilot review screen `/copilot`: sessions with shown and muted cards, the owner's ratings and the md/json export; `/api/copilot/sessions` rows carry shown, suppressed and thumbs counts.
- Bookmark clip: `GET /api/bookmarks/<id>/clip?pad=` returns a zip with the audio around the bookmarked segment (default +-5 s) and its text; a Clip button on each bookmark.
- Recording-consent reminder on every recording start (UI, call prompt, auto-record, agent): a dismissible line on the record view, a line on the "Recording started" prompt and the agent start notice. It never blocks the recording (`RECORDING_CONSENT_REMINDER`, on by default).

### Changed
- MCP: 98 tools (96 -> 98, both new tools are read class).
- `docs/ROADMAP.md`: wave 2 items P2-P5 and N1 closed with 4.36.0.

## [4.36.0] — 2026-10-28

### Added
- Call autoprompt: one repeat prompt on a long call that is still unrecorded (`RECORDING_AUTOPROMPT_REPROMPT_MIN`, default 10 minutes, 0 = off), and `call_watch.jsonl` now records whether a click or auto-start really started a recording and why not (`record_started`, `record_failed`, `reprompt`).
- Opt-in auto-record: `RECORDING_AUTOPROMPT_ACTION=record` (default `prompt`, unchanged) lets the watcher start the recording itself (`started_by='auto_call'`) and immediately shows "Recording started" with Stop / Keep.
- Per-stream gain (-20..+20 dB) on the start form and during a recording (`POST /api/recording/<sid>/gain`), stored in the manifest and applied to the final mixed file.
- Participants: pick people from the graph at start, prefilled from calendar events around now (`GET /api/recording/participants`); linked to the transcript as `meeting_entities.source='owner'`.
- A recording badge on every screen and a banner for recordings recovered after a restart.
- A denied microphone or loopback answers 403 `DEVICE_ACCESS_DENIED` with the stream; the record view explains it, hints at an empty device list, reports "recording without screen", declares a recording interrupted after a server restart, and resyncs state after a failed pause/resume.

### Changed
- "Record" from the call prompt now captures the Windows default microphone and all system sound, not only the calling app.
- `telegram.exe` dropped from the default `RECORDING_AUTOPROMPT_APPS` (it prompted on every voice message); an explicit `.env` list naming it still works.
- A new transcript inherits the recording's (or library item's) description when the form leaves it empty.
- `FORCE_CPU` now actually reaches the Whisper manager (it was never read): set in `.env`, transcription runs on the CPU.
- Finished jobs older than 30 days are pruned at start; missing Claude key messages point to Settings instead of `.env`.
- `comtypes` logging quieted to WARNING. Service worker cache `recall-v95`.

## [4.35.0] — 2026-10-28

### Added
- Task approval (`docs/wiki/task-approval.md`, ADR-056): every task Claude extracts (meeting card, Telegram thread, comment analysis) lands as `proposed`, with `for_whom`, `confidence` and `importance` from the same call; third-party and low-confidence ones (`TASK_PROPOSAL_MIN_CONFIDENCE`, 0.6) are not stored. Nothing is a live task until the owner says Yes.
- Daily pipe `task-proposals` (09:00): expires overdue proposals, then sends up to 10 Telegram cards (nearest deadline, then importance) with Yes / No and undo. The 3-day window starts at the first delivered card or the SPA tasks screen; an MCP listing never starts it. Expired proposals are hidden, not deleted.
- Decisions are tombstones: re-running extraction replaces only `proposed` rows and never re-proposes text already decided in that record. `evals/task_rerun_measure.py` measures how often the exact key misses across re-runs.
- Approve / reject / edit (text or date, undoable) through `PATCH /api/memory/action-items/<id>` `{action}`, the SPA tasks screen and MCP `task`; `list_action_items` takes the new statuses. Error code `INVALID_TRANSITION` (409). MCP tool count unchanged (96).
- Monday digest: tasks stuck over 14 days or overdue, with done / postpone / drop, and a counter line (shown → pressed, review days, active tasks).
- CLI `python -m app.services.commitments unlegacy [--dry-run]` and `expire-proposals [--dry-run]`.

### Changed
- Migration v59: proposal columns on `action_items`, `payload_json` on `action_item_events`, and every existing task (5484 rows here) moves to `legacy` with its old status kept. **Day one is empty:** the tasks screen, digest and `list_action_items` show nothing until a proposal is approved; old rows answer only to `status=legacy`, and `unlegacy` undoes the flip in one statement.

### Fixed
- Recording autosave drops a typed name shorter than 3 characters (a browser that died mid-typing left a one-letter title); Claude titles the recording instead.

## [4.34.1] — 2026-10-27

### Added
- `python -m app.services.calendar_ics sync [--full] [--source ID]`: one sync now; `--full` pulls the whole feed once, before the backfill (the periodic sync keeps only ±30 days), and prints the earliest timed event it stored.

### Changed
- `calendar_bind backfill` reports `calendar_earliest` (earliest timed event), `before_calendar` (recordings older than it, no event can exist for them) and `coverable`, the denominator for the bind rate. All-day events never count toward the start of the calendar's history.

## [4.34.0] — 2026-10-26

### Added
- Calendar through secret ICS links, read-only (K6, `docs/calendar.md`): several Google / Outlook calendars, connected on a one-time local page (`/connect/calendar/<token>`; https/webcal on an allowlisted host, a trial fetch must parse). The address lives only in the OS keyring (`Recall Calendar`), never in the DB, `.env`, logs or MCP. Background sync every `CALENDAR_SYNC_MINUTES` (15) for ±30 days, recurrences expanded with `recurring-ical-events` (EXDATE, moved occurrences); a cancelled or vanished occurrence is kept as `cancelled`. Settings → Calendars: sources, last sync, attendee share, Sync now, Disconnect (behind the Windows dialog).
- A saved recording is bound to the event with the largest overlap (±10 min, at least a third of the recording; cancelled and all-day events never bind). Title order: the owner's name, then the event title, then Claude's suggestion; an event-titled recording is not renamed by the autosave. Attendees are linked to people who already exist (name, alias, merged spelling), never created; emails are never aliases.
- `python -m app.services.calendar_bind backfill [--dry-run]`: binds past recordings (start from the session `manifest.json`), titles untouched, with per-calendar counts and the person count before and after.
- MCP `list_calendar_events` (direct-DB, read-only; private-direction and stop-word events hidden) and `calendar_event` in `get_transcript`. MCP 95 → 96 tools (57 read-only).
- Migration v58: `calendar_sources`, `calendar_events`, `calendar_event_id` on `audio_downloads` and `transcriptions`. New dependency `recurring-ical-events==3.8.2` (LGPL-3.0).

### Fixed
- Enrichment no longer wipes `meeting_entities` rows it did not write (`source='calendar'`), and updates a kept row instead of replacing it.

## [4.33.0] — 2026-10-26

### Added
- Skills in `skills/`, installed by copying into `~/.claude/skills/`: `recall-record` (start, pause, resume, retarget a recording), `recall-record-stop` (stop or cancel; only when the owner types it, `disable-model-invocation`), `recall-transcribe`, `recall-digest`; `recall` rewritten from the registry (95 tools, archive reads vs reads that need Recall running).
- MCP prompt `post_digest`: `/mcp__recall__post_digest [pipe]` (default `weekly-digest`) tells the model to run the pipe through `pipes`; registered only on the `full` profile, adds no tool.
- `docs/recipes/hooks.md`: Claude Code hook recipes (start a recording, send the brief on `SessionEnd`, block a pasted secret, `ask` rule for `curl` to Recall); a hook is never a safety gate and D actions still raise the Windows dialog.
- `docs/MCP_SETUP.md` "Registrations": `recall` (full), `recall-ro` (read), `recall-scheduled` (full, no dialog bypass).

### Changed
- ADR-042 accepted: the MCP full-control program (specs 1–9) is closed, with the shipped and dropped list.

### Fixed
- VULYK `/vulyk-ship` step 4 (`/vulyk-evolve` 2026-10-25): commit names both files `--record` writes (`plan.md`, `memory/stats/ship.jsonl`); 3 ships left the ledger uncommitted and the next ship-check said NOT READY. Registered as override 2 in `scripts/recall-vulyk-overrides.sh`.
- Tests (`/vulyk-evolve` 2026-10-25): the MCP tool total is pinned once, in `tests/test_tool_count_docs.py`; six feature tests no longer hardcode it (it was hand-bumped in ~10 specs and turned council rounds RED in gmail-ingest and live-video-frames).
- VULYK tooling (`/vulyk-evolve` 2026-10-16): `scripts/scope-check.sh` no longer counts its own `memory/stats/scope.jsonl` as a story's out-of-scope edit (false `scope_breach` on every rerun in the same tree); registered as override 2 in `scripts/recall-vulyk-overrides.sh`.

## [4.32.0] — 2026-10-26

### Added
- Recording and the live call through MCP: `recording` (start, pause, resume, change the system source; start always
  shows a Recall toast first with mic, apps, language, name and "with screen", and refuses with `NOTIFY_UNAVAILABLE`
  when no toast can be shown; default limit 180 min, max 240, then it stops by itself; the screen only with an
  explicit `with_screen`), `recording_stop` (behind the native Windows dialog, answers `stopping` at once, a repeat
  answers `already`), `recording_cancel_own` (an agent's own recording younger than 5 minutes, through the dialog),
  `copilot` (mode, importance, cards, stop, reingest without duplicates), `recording_devices` (audio, screens, apps,
  call prompts without their one-shot tokens), `get_copilot_timeline`; `get_active_recording` reports `started_by`.
- `started_by` provenance on every recording session (`ui` | `call_prompt` = the owner, `mcp` | `hook` | `unknown` =
  an agent); an agent may only stop the owner's recordings, never pause or retarget them.
- An agent's copilot stays local-only for the whole session: the spend clamp holds on settings, `/api/copilot/start`
  and `/api/copilot/move`; agent ratings stay out of the copilot statistics (migration v57).
- MCP 89 → 95 tools (56 read-only).

### Security
- The call-prompt action token is no longer handed to non-UI readers of `/api/recording/autoprompt`, and a caller
  that names itself as an agent is refused on the token routes, so a raw local caller cannot start an owner-class
  recording without the toast or stop a session without the dialog.

## [4.31.0] — 2026-10-25

### Added
- Telegram chats through MCP: `telegram_chats` (monitored chats or the account's dialogs; private directions and
  stop-word chats hidden from the agent), `telegram` (change a chat's direction or tags, disable it; a move out of a
  private direction is refused), `telegram_enable_chat` and `telegram_backfill` (history or repair) behind the native
  Windows dialog, `telegram_repair_preview` (dry run, no dialog). HTTP: `PATCH /api/telegram/chats/<id>/category`,
  `POST /api/telegram/chats/<id>/disable|enable`; every MCP change is journaled in `tg_chat_events` (migration v56).
- Pipes on demand: MCP `pipes(action=run)` / `POST /api/pipes/<name>/run` sends a brief to the fixed bot chat, with a
  per-pipe lock and a 10-minute cooldown; on-demand runs (`trigger=mcp`) never close a scheduled moment. Each run
  publishes `pipe_run` on the `pipes` channel of `wait_for`.
- `automation_status` (MCP) / `GET /api/automation/status`: call prompts (kind, app, age only, never the one-shot
  action links), pipes, pollers, Telegram listener and bot.
- Call watcher events on `wait_for` channel `system:calls`: `call_start`, `prompt`, `prompt_withdrawn`, `call_end`,
  `autostop`. A pending "record" prompt is withdrawn as soon as a recording captures that app.
- Daily "MCP did this" brief (`pipes/mcp-actions.md`, 09:00, $0, silent on an empty day) with undo buttons where an
  inverse exists: task changes, a disabled chat, a changed chat direction. Undo is refused if the value changed again.
- MCP 82 → 89 tools (54 read-only).

## [4.30.0] — 2026-10-25

### Added
- Live screen frames during a recording: MCP `get_live_video` (tracks per monitor, fps, the second the video is
  readable until) and `get_live_frames` (the frame at any second, or a range by step down to one frame or "screen
  changes only", at most 12 frames per call with a `next_t` cursor). Every frame comes with OCR text; screen secrets
  are masked on the image and in the text. HTTP: `GET /api/recording/<sid>/live-video|live-frame|live-frames`.
  Computed on request only; MCP 80 → 82 tools (51 read-only).

### Changed
- Screen capture writes a keyframe every 2 s (`-g 2×fps`) and flushes each fragment to disk (`-flush_packets 1`), so
  the readable live edge lags ≤ ~2–2.5 s (was ~8 s, up to ~20 s on a still screen). Applies to recordings started
  after a restart of Recall.
- The manifest keeps every capture file of a video track (`segments`), so video recorded before a pause stays
  reachable during the call.

## [4.29.0] — 2026-10-21

### Changed
- The live call co-pilot is opt-in per call: `/record` shows "Turn on the co-pilot for this call", unchecked on every
  render; without it the recording starts with no co-pilot (no Ollama load during the call). MCP live listening —
  `get_live_transcript`, `ask_live` — works either way; `get_live_copilot` answers "not started" for such a call.

## [4.28.0] — 2026-10-21

### Changed
- Default Whisper model is `large-v3` everywhere a model is not chosen explicitly: every SPA select
  (upload, YouTube, audio library, record, settings), the onboarding hint, `POST /api/transcribe` and
  `/api/transcribe/async` (and so MCP `transcribe`, previously `base`), Telegram media, recording
  autosave and the boot warm-up. The catalog's "recommended" star moved from `large-v3-turbo` to `large-v3`.
  Chosen by a measurement of all 8 installed multilingual models on FLEURS (uk WER: large-v3 8.2 %,
  large-v3-turbo 8.7 %, medium 12.8 %, small 20.8 %, base 44.4 %) — `docs/specs/whisper-best-default/measurement.md`.
- Live call transcription runs `large-v3-turbo` instead of `small` (same speed, ~2.4x fewer errors).
- An API/MCP transcribe call without `model` on a machine without `large-v3` downloaded now answers
  `MODEL_NOT_INSTALLED` instead of silently running `base`.

## [4.27.0] — 2026-10-21

### Added
- MCP runs the pipeline: `transcribe` (library, recording, YouTube link or a local path under
  `RECALL_IMPORT_ROOTS`) as a background job over the new `POST /api/transcribe/async`, refused while
  a recording runs; the UI's synchronous `POST /api/transcribe` keeps its contract (logic extracted
  into `app/services/transcription_run.py`).
- `document_ingest` (up to 5 paths), `get_youtube_info`, `list_audio_library`, `audio_download`,
  `audio_item` (edit / restore), `transcript_ai` (polish, summarize, sentiment, translate, topics,
  enrich; a cached result costs nothing, no forced regeneration), `models` (download / check updates),
  `system_diagnostics` (package written to `logs/diagnostics/`, last 5 kept), `job_cancel`.
- `get_job_status` lists jobs, waits up to 25 s and returns the job's stored result and log.
- D tools behind the native Windows dialog: `document_import_folder`, `document_reparse`.
  MCP: 80 tools (49 read-only + 31 write).

### Changed
- Whisper model download no longer holds the model load lock: other models stay usable while one
  downloads (UI and MCP).
- Import-folder returns its job id.

### Fixed
- `video_analyze` that would send frames to Claude for description (by argument or by the
  `VIDEO_VISION_BACKEND` setting) now asks the Windows dialog when it comes from MCP.

## [4.26.0] — 2026-10-20

### Added
- MCP organises the archive (migration v55, provenance columns): write tools `task` (done / reopen /
  snooze / owner / due), `bookmark`, `comment` (agent comments carry an "agent" badge and get no
  search weight until the owner presses Confirm in the UI), `taxonomy` (directions and tags;
  private directions refused), `speaker` (rename, map a label to an existing speaker id) and
  `transcript` (title / description / direction); read tool `transcript_suggest_meta`.
- D tools behind the native Windows dialog: `taxonomy_delete`, `taxonomy_merge`, `speaker_merge`,
  `speaker_delete`, `transcript_delete`, `enrich_backfill`. MCP: 68 tools (47 read-only + 21 write).
- Read tools get filter parameters (report §9 G4).

### Fixed
- An MCP retranscribe that would evict a kept transcript version now asks the Windows dialog; without
  approval the job fails with `EVICTION_NOT_APPROVED` (hole in 4.25.0).
- `list_recent` no longer returns soft-deleted records.
- Recording autosave sends the UI token, so its title is not treated as an agent edit.

## [4.25.0] — 2026-10-20

### Added
- Re-transcribe any record that still has its media file with another locally installed Whisper model, from the Library and the Media library (a Re-transcribe action with a model picker) and over MCP (`list_whisper_models`, `retranscribe`).
- Transcript versions (migration v54, `transcript_versions`, ADR-044): every run is kept with its model; at most 3 per record, the oldest is dropped automatically, never the main one. A new run becomes main; any kept version can be made main again (UI version bar, MCP `transcript_versions` / `activate_transcript_version`).
- Two-column, time-aligned compare of two versions with word-level highlight and player seek on the transcript page.
- Switching the main version re-indexes search and RAG; summary, tasks and entities are left as they are; polished text is kept per version. MCP: 55 tools (46 read-only + 9 write).

## [4.24.0] — 2026-10-19

### Added
- MCP gets its first destructive tools, each run only after the owner presses «Yes» in the native
  Windows dialog Recall raises: `mailbox_backfill` (mail 13–120 months back), `mailbox_label_enrich`
  (paid Claude enrichment on for a mail folder; off needs no dialog), `mailbox_disconnect` (soft:
  password removed, folders off, cursors and archive kept, the answer lists the disabled folders) and
  `settings_remove_anthropic_key`.
- `settings(set, key=ui_lang|copilot_enabled, value)` and `settings(test_anthropic_key)`; a new read
  tool `system_status(section)` (health, ready, hardware, stats, polish, models, settings).
  MCP: 51 tools (44 read-only + 7 write).
- Gated routes `POST /api/mail/backfill`, `POST /api/mail/labels/<id>/enrich`,
  `DELETE /api/mail/accounts/<id>/credentials`.

### Changed
- Enabling Claude enrichment for a mail folder and a mail sync deeper than 12 months now ask the
  Windows dialog for every caller, the Recall UI included: the plain routes refuse them with
  `USE_GATED_ROUTE` (before, only a request carrying the MCP header was refused, so a script without
  it went through silently). The hard mailbox delete in the UI is shown in the dialog as a deletion
  (`mailbox_delete`), not a disconnect.

## [4.23.3] — 2026-10-19

### Fixed
- Direction after a tab save (spec `save-direction`): saving a stopped recording from its save screen cancelled the
  server autosave, and with it the only step that files the direction, so a call the k-NN was sure about (live call,
  confidence 1.0) still landed in the "no direction" banner. Now the first successful save starts a direction-only
  follow-up: it waits up to 2 h for the tab's transcript, then files it when the suggestion reaches
  `RECORDING_AUTOSAVE_MIN_CONFIDENCE`; a direction chosen in the tab is kept, and the server never transcribes.
- Two concurrent saves of one recording no longer both report `already_saved: false` (the save screen sent two
  transcription requests 70 ms apart): the `saved` mark is read and set in one locked manifest write.

## [4.23.2] — 2026-10-19

### Fixed
- Save screen of a stopped recording: the Whisper model now defaults to `large-v3` instead of `large-v3-turbo`, the
  same model as the server autosave (`RECORDING_AUTOSAVE_MODEL`), so the result no longer depends on which of the two
  paths fires first.

## [4.23.1] — 2026-10-19

### Fixed
- Save screen of a stopped recording: the transcription language now defaults to auto-detect instead of Ukrainian, so
  the tab's countdown save matches the server autosave (a call previewed as uk and spoken in ru was forced to uk).
  The live-preview language picker keeps its own default.

## [4.23.0] — 2026-10-19

### Added
- **Recording autosave after any stop** (spec `recording-autosave`; stories 01-05).
  - A recording stopped anywhere but its own Stop button (the Windows notification `«Закінчити дзвінок»`, MCP, the K2
    autostop, another tab) no longer freezes the recording tab: it switches to the save screen. The notification's
    stop button opens `/record?stopped=<sid>` on that recording's save screen instead of the empty setup form.
  - The save screen counts down 60 s (`«Збережу й транскрибую через 1:00»`); editing pauses it and it resumes 15 s
    after the last edit; at zero it saves and transcribes like `«Зберегти зараз»`.
  - Server safety net (`app/services/recording/autosave.py`): finalize sets a deadline (`RECORDING_AUTOSAVE_SEC`,
    default 60; 0 = off). If nobody saved, discarded or is editing by then, the server transcribes the recording over
    the same HTTP path as the UI (`RECORDING_AUTOSAVE_MODEL` large-v3, `RECORDING_AUTOSAVE_LANGUAGE` auto), titles it
    from the local model (not during a recording) and sets the direction when the k-NN confidence is at least
    `RECORDING_AUTOSAVE_MIN_CONFIDENCE` (0.6). What the user typed on the save screen travels with the hold and wins.
    Deadlines survive a restart. New routes `POST /api/recording/<sid>/autosave/hold`, `GET /api/recordings/uncategorized`.
  - Audio library banner: recent recordings without a direction ask for one, with k-NN candidates in one click.

### Changed
- `POST /api/recording/<sid>/discard` refuses with 409 `RECORDING_KEPT` once the recording has a transcript or its
  autosave ran: deleting `final.mp3` would orphan the transcript.

## [4.22.0] — 2026-10-19

### Added
- **MCP foundations, part B** (spec `mcp-foundations-b`; stories 01-05). 46 MCP tools (43 read-only + 3 write).
  - Approval dialog: every destructive route (the D actions of ADR-042) needs a native Windows dialog raised by
    `app.py`; No or a timeout is `APPROVAL_DENIED`, no desktop is `APPROVAL_UNAVAILABLE`, a second request while one is
    open is `APPROVAL_PENDING`; one dialog at a time, default button No, 45 s deadline.
  - Audit: migration v53, table `mcp_actions`; every changing `/api/*` request is recorded (actor, decision, outcome; no
    bodies) and purged after 90 days. New read tool `list_mcp_actions` with filters and an id cursor; the `claimed`
    labels are marked `trusted: false`.
  - Ceilings: the W tools are limited to 10 changes per family per 10 minutes in each MCP process (`RATE_CEILING` with
    `retry_after_sec`); the D routes to 3 per minute and 20 per day per actor class.
  - UI: the gated delete buttons drop their own confirm modal and wait for the dialog.
  - Docs: `docs/SECURITY.md` "Local boundary and the approval dialog", `docs/wiki/approval-gate.md`; ADR-records
    the MessageBox spike.

## [4.21.0] — 2026-10-19

### Added
- **MCP token budget** (spec `mcp-token-budget`, council GREEN in round 2). Idea taken from XERJ's MCP token budgets.
  - `get_transcript` fits `max_tokens` (default 40000, estimated as chars/3; `0` = no limit). Metadata, summary, the email
    block and comments are always kept; then the text window, thread messages and segments. A cut response carries
    `truncated: true` and a `budget` block (`next_offset`, `text_remaining_chars`, `segments_next`, omitted counts);
    continue with `offset` / `segment_offset`. Pages end on a line or sentence end.
  - `get_transcript` returns one text: `polished_text` when the record was polished (`text_source`), otherwise
    `transcript_text`; `prefer_raw=True` returns the raw one. The `recall://transcript/{id}` resource still returns the
    whole raw text.
  - `search_archive` keeps chunks in rank order inside the same budget; the overflow comes back as text-free `locators`
    with a shown / as-locators / dropped count. With a normal `top_k` the chunks are unchanged.
  - `logs/mcp_calls.log`: every `OK` line carries the response size (`48.2k chars`).

## [4.20.0] — 2026-10-18

### Added
- **MCP foundations, part A** (spec `mcp-foundations-a`; stories 01-05). 45 MCP tools (42 read-only + 3 write).
  - Registry: every tool has one `_TOOL_CLASS` entry (R/W/D, provenance kind, open-world flag, declared limits); annotations
    and `_meta` are derived from it. `RECALL_MCP_PROFILE=full|read`; `RECALL_MCP_READONLY=1` registers no write tool.
    `video_analyze` is a write tool. New read tool `wait_for` over a per-channel event ring in `sse_broker`
    (`/api/automation/events`). The secret guard's bare 16-letter rule runs only on write tools.
  - Local boundary: a foreign `Host` or cross-site `Origin` is refused on direct requests; a per-boot UI token goes to the
    SPA and `classify_request()` recognises it (not enforced yet). The proxied branch is unchanged.
  - Infra hygiene: job errors store a code and a generic text, `cancel_ignored` for a late cancel, a third executor for slow
    kinds, `recording/state.py`, one locked `.env` writer.
  - One MCP error envelope `{ok, error_code, error, hint?, status_code?}` and the code table `app/core/error_codes.py`
    with a test that every `error_code` literal is in it. A non-JSON error body is no longer passed through.
  - ADR-042 draft: MCP full control supersedes read-first.

## [4.19.0] — 2026-10-18

### Added
- **Mail and the Anthropic key connected through MCP** (spec `mcp-mail-connect`, council GREEN in round 2). The first MCP
  write tools: `mailbox` (`connect`, `refresh_labels`, `label_set`, `sync`) and `settings` (`connect_anthropic_key`); 44 MCP
  tools (42 read-only + 2 write). `connect` returns a one-time `http://127.0.0.1:5050/connect/<kind>/<token>` link (10 min,
  dies on success, on the third failed attempt or at expiry); the secret is typed only on that page, never in a tool argument,
  response or log. The pages answer direct localhost only (loopback `Host`, proxied requests refused) and the service worker
  never caches them.
- MCP writes carry `X-Recall-Actor: mcp` + `X-Recall-Tool` and leave one app.py log line (no body). For MCP writes app.py
  refuses the label's Claude toggle, moving a label out of a private direction, and more than 12 months; a label's first
  sync is capped at 12 months without re-scanning synced labels. `RECALL_MCP_READONLY=1` still refuses every write.
- Secret-shape guard on every string argument of every MCP tool (`SECRET_IN_ARGUMENT`, checked before any log write):
  grouped app passwords, any standalone 16-letter token, `sk-ant-` keys.

### Changed
- `POST /api/mail/accounts` and the Anthropic key save go through `mail_accounts.connect_account()` /
  `settings_secrets.save_anthropic_key()`, shared with the connect pages.

### Fixed
- `test_docs_english` was red on `main` since gmail-ingest: owner quotes in ADR-039..041 are marked verbatim.

## [4.18.1] — 2026-10-16

### Fixed
- The mail poller writes a start line (`Mail poller started (every N min)`) to `whisper_ui.log`, so a release can prove
  the background service runs from the log, not only from `/api/mail/status` (`docs/defects/shipped-but-not-restarted.md`).

## [4.18.0] — 2026-10-16

### Added
- **Gmail ingestion** (spec `gmail-ingest`, council GREEN in round 6). Connect one or more Gmail mailboxes with an
  address + Google app password (IMAP over SSL, read-only in code: EXAMINE/BODY.PEEK, no write or SMTP commands, a test
  proves it). The password lives only in the OS credential store via `keyring` (Windows Credential Manager) — never in
  the DB, `.env`, logs, API/MCP responses or backups. Only labels enabled on the new **Mail** screen (`/mail`) are
  ingested; a label maps to a direction and has a per-label Claude card toggle (off by default). First sync of a label
  loads the last 12 months (`MAIL_BACKFILL_MONTHS`), then a background poller (`MAIL_POLL_MINUTES`, default 5) picks up
  new mail; "load deeper" fills older months. One record per email (`source_type='email'`, dated by the Date header),
  quoted replies stripped (Gmail/Apple/Outlook, uk/ru/en attributions), forwarded content kept, emails chained by
  thread; passwords, tokens and one-time codes masked in subject, body and text attachments before anything is stored.
  Attachments go through the document pipeline as linked document records (`mail_attachments`, 25 MB cap,
  `MAIL_ATTACH_MAX_MB`). A message that fails to store is retried on the next poll.
- Migration v52 (mail accounts, labels, attachment links, email columns on `transcriptions`) — additive.
- MCP: `email_status` (accounts and per-label sync state, no secrets); `get_transcript`/`list_recent` show email
  fields and the thread's messages. 42 read-only tools.

### Changed
- Document persistence moved from `app/blueprints/documents.py` into `app/services/document_ingest.py` (one path for
  uploads, folder import and mail attachments).
- `keyring>=25.7` added to `requirements.txt`.

### Known gaps
- Turning the Claude card on for a label later does not enrich that label's emails already in the archive.

## [4.17.0] — 2026-10-16

### Changed
- **Code is in English** (spec `english-code`, part 3 of 3; council ESCALATE in round 2 on ask 4 — accepted by the
  owner, see below). Comments, docstrings, log lines, CLI output and argparse help across `app/`, `mcp_server.py`,
  `app.py`, `config.py`, `telegram_listener.py`, `evals/`, `scripts/`, `tools/` and the root scripts are English;
  comments in the SPA JS/CSS, `sw.js`, `shell.html` and the PowerShell scripts too.
- Tests: names, docstrings, comments and assert messages are English; Cyrillic test data stays on purpose (it is
  what catches Cyrillic case-folding bugs).
- MCP: all 41 tool descriptions and response texts are English; the MCP process runs with `RECALL_UI_LANG=en` and
  sends `X-Recall-Lang: en` on proxied calls (the web UI setting is not affected).
- `.env.example` and the settings registry descriptions are English.
- User-facing server texts that were still Ukrainian-only (YouTube errors, local model availability, pipe run
  errors, Telegram bot replies, transcript and co-pilot export labels, live-ask reasons) go through the uk/en
  server catalog (`app/core/i18n_catalog/svc_*`).
- Untouched by design: LLM prompts, text-parsing word patterns and regexes (deadline phrases, vocabulary,
  secret masking), data written by migrations, names, uk dictionaries, chunk context prefixes (no re-embed).
- Guard test `tests/test_code_english.py` (no Cyrillic in comments, docstrings and log calls of tracked Python)
  and the path rule `.claude/rules/code-language.md`.

### Known gaps (accepted)
- Still Ukrainian in an English UI: the co-pilot "scope matches several directions" card
  (`app/services/copilot/worker.py`), the job error after an app restart (`app/services/job_queue.py`) and the
  co-pilot export title (`app/blueprints/copilot.py`, the catalog key exists but is unused).
- Three test skip reasons are Ukrainian; the guard does not flag quoted Ukrainian prose, `print()`,
  `warnings.warn` or a log message passed through a variable.

## [4.16.1] — 2026-10-16

### Changed
- **Public documentation is in English** (spec `english-docs`, part 2 of 3; council GREEN in round 3). `README.md` is
  English with a Ukrainian twin `README.uk.md` (linked both ways); `HOW_WHISPER_WORKS.md`, `docs/` (ADRs, wiki,
  MCP setup, security, design system, video timeline), `evals/README.md`, module maps `memory/map/*`, the
  `recall-video` skill and `requirements.txt` comments are translated. Older CHANGELOG entries stay as written;
  new entries are English.
- `.claude/rules/docs-language.md`: maps, ADRs, wiki and public docs are written in English from now on.
- Guard test `tests/test_docs_english.py`: no Cyrillic prose in public-bound docs (code spans, quoted interface
  strings and names from the mirror replacement map excepted) and intra-repo anchors resolve.
- Mirror sanitizer: English twins of the private-repo-name replacement and the archive-figure rules, so the
  translated README and ADR-023 do not leak what the Ukrainian text used to hide.
- GitHub descriptions and topics of both repositories are English.

## [4.16.0] — 2026-10-15

### Додано
- **Інтерфейс українською та англійською** (спека `english-i18n`, частина 1 з 3; рада ESCALATE у раунді 3 на
  ask 4 — прийнято власником, див. нижче). Усі екрани SPA ідуть через словники `static/js/recall/i18n/*.js`
  (`Recall.t`, плюралі через `Intl.PluralRules`, формат чисел/дат за мовою). Налаштування → «Мова інтерфейсу»:
  Авто / Українська / English (`RECALL_UI_LANG`, `GET/POST /api/settings/ui-lang`, пишеться в `.env`, без рестарту).
  «Авто»: мова браузера `uk*` → українська, будь-яка інша → англійська; явний вибір сильніший.
- Тексти помилок і статусів сервера — з каталогу `app/core/i18n.py` (`tr`) мовою із заголовка `X-Recall-Lang`
  (його шле `api.js` на кожен запит, зокрема upload і SSE); без заголовка — явне налаштування, `auto` → українська.
- Брифінг і кнопки в Telegram — мовою налаштування (`auto` → українська). Зміст архіву, промпти LLM і мова зведень
  не змінювались. Охоронний тест `tests/test_i18n_coverage.py` ловить кирилицю поза словниками в SPA і в `jsonify`.
  Кеш SW `recall-v80`.

### Відомі обмеження (частина 3)
- Тексти винятків сервісів, що доходять до UI як `str(e)` (YouTube, Ollama тощо), рядки прогресу завантаження
  аудіо/YouTube і заголовки експортів (TXT/MD/DOCX) поки українські в англійському режимі.

## [4.15.0] — 2026-10-15

### Додано
- **Задачі закриваються кнопками прямо в брифінгу** (K5, спека `k5-task-buttons`, рада GREEN у раунді 3).
  Задачі зводу й «упущено» пронумеровані, під повідомленням — ряд на кожну: «N ✅ · 🚫 · ⏭». ⏭ відкриває
  «завтра / +тиждень / +місяць». Після натиску ряд показує дію і «↩ скасувати». Натиски приймаються лише з
  чату власника, long-polling-ом у процесі Recall (`app/services/brief_actions.py`); поки ПК спить, Telegram
  тримає їх до 24 год і Recall застосовує їх після пробудження — про це рядок унизу брифінгу.
- Журнал змін задач `action_item_events` (міграція v51): хто, коли, звідки (bot / web), що було й що стало.
  Зміна статусу на `/tasks` іде тим самим шляхом (`commitments.apply_action`) і теж пишеться в журнал.
  Скасувати можна останню дію над задачею (`commitments.undo_action`). MCP не змінився — 41 тулза читання.

## [4.14.1] — 2026-10-15

### Виправлено
- Чек-лист першого запуску більше не вискакує поверх запису, відкритого за посиланням брифінгу через нову
  адресу (ts.net): прапорець «пройдено» живе в localStorage окремо для кожної адреси. Тепер він не показується
  за посиланням на конкретну сторінку, а на налаштованому архіві (модель завантажена) мовчки позначається пройденим.
- Вікно чек-листа на телефоні не виходить за екран: висота обмежена, прокрутка всередині, заголовок із × прилипає.
  Кеш SW `recall-v79`. Карточка дефекту `first-run-per-origin`.

## [4.14.0] — 2026-10-15

### Змінено
- **Брифінги в Telegram — картки, а не простирадло** (K5, спека `k5-brief-cards`, рада GREEN у раунді 2).
  Повідомлення складає код у HTML (`app/services/pipes_cards.py`): задачі блоками за кореневим напрямком,
  у блоці 5 найтерміновіших і «ще N →» на сторінку задач із фільтром; картка — що · хто · до коли · звідки
  з посиланням на запис. Claude пише лише 1–2 рядки «💡 головне» (розмітку прибирає код); без Claude —
  те саме повідомлення без них. Маски й стоп-слова — на кожне поле до екранування. Бот шле з `parse_mode=HTML`
  і ріже довге повідомлення лише між блоками.
- Сторінка запису: `/transcript/<id>?task=<id>` прокручує до задачі й підсвічує її (кеш SW `recall-v77`).

### Додано
- `RECALL_PUBLIC_URL` — база посилань у картках, видима з телефона (у власника `tailscale serve`).
  Порожньо → `RECALL_API_URL`, як було. Карточка дефекту `brief-link-localhost` з `scripts/check_brief_links.py`.

### Безпека
- Доступ через `tailscale serve` більше не довірений як localhost: запит із `127.0.0.1` з `X-Forwarded-For`
  або `Tailscale-User-Login` пускається до `/api/*` лише з IP у `RECALL_PROXY_ALLOWED_IPS` (порожньо — нікого)
  або з `RECALL_API_KEY`. Інші вузли tailnet (сервер vps-prod) отримують 401. Карточка дефекту
  `proxied-access-trusted-as-local` з `scripts/check_proxy_trust.py`.

## [4.13.0] — 2026-10-15

### Додано
- **Брифінги за розкладом у Telegram-бот** (K4, спека `k4-scheduled-pipes`, рада GREEN у раунді 5). Труба —
  файл `pipes/<name>.md` (cron, read-функції з закритого реєстру, бюджет, промпт). Три перші: тижневий звід
  і забуті обіцянки (пн 9:00), питання без відповіді в Telegram (будні 9:00, порожнє не шлеться). Планувальник
  живе в `app.py` (`PIPES_ENABLED`), пропущене доганяє один раз при старті. Переказ — Sonnet у межах
  `budget_usd`; нема ключа / бюджет / помилка → простий список із позначкою.
- **Окремий бот доставки** (`app/services/tg_bot.py`, Bot API, `TELEGRAM_BOT_TOKEN` + `TELEGRAM_BOT_CHAT_ID`):
  шле лише в чат власника, ріже довгі повідомлення; `python -m app.services.tg_bot whoami` дає chat id.
- **Фільтр приватного перед ботом** (`app/services/pipes_privacy.py`, спільний стоп-лист із MCP): пункти з
  «Юридичне»/«Особисте» і зі словами з `private.txt` не йдуть (рядок «🔒 N пунктів приховано»); IBAN, картки,
  телефони, email і суми з валютою (укр/рос, «тисяч»/«тыс.») → `•••`; стоп-слово в готовому тексті зупиняє відправку.
- **Повтор після збою**: збій о 9:00 не з'їдає брифінг тижня — повтор через `PIPES_RETRY_MINUTES` (15) до
  `PIPES_RETRY_MAX` (6) спроб; завислий запуск і ручний `pipes run` момент не закривають.
- **Брифінг не мовчить**: зупинений фільтром — повідомлення без даних; «не зібрано» (невідомий власник
  Telegram) — рядок причини замість тиші.
- Історія запусків: таблиця `pipe_runs` (міграція v50) і MCP-тулза `list_pipe_runs` (41 read-only).

### Щоб увімкнути
Створити бота в @BotFather, `/start` йому; `TELEGRAM_BOT_TOKEN` у `.env`; `python -m app.services.tg_bot whoami`
→ `TELEGRAM_BOT_CHAT_ID`; `PIPES_ENABLED=1`; перезапуск Recall.

## [4.12.2] — 2026-10-14

### Виправлено
- Локальна vision-модель (`qwen2.5vl:7b`, опис кадрів відео) більше не тримає ~5 ГБ VRAM 30 хв після
  пачки кадрів: `keep_alive=60s` (`VIDEO_VISION_KEEPALIVE`). Не 0 — кадри йдуть пачкою, і з 0 модель
  перевантажувалась би на кожному (замір: 5.5–7.6 с/кадр проти 2.3–2.5).

## [4.12.1] — 2026-10-14

### Виправлено
- Пропозиція назви більше не тримає MamayLM у відеопамʼяті 30 хв після відповіді: `keep_alive="0"`
  (`LOCAL_TITLE_KEEPALIVE`), модель вивантажується одразу — дзвінок, що почнеться слідом, не лишає копілот
  і транскрипцію без місця. Ціна: кожна пропозиція з холодного старту (~45 с). `local_llm.generate_json`
  тепер пробрасує `keep_alive`.

## [4.12.0] — 2026-10-14

### Додано
- **Локальна модель пропонує назву й опис запису** (спека `recording-meta-suggest`, рада GREEN у раунді 2).
  У Бібліотеці й Медіатеці записи з авто-імʼям («Запис 2026-10-14 08:54») мають кнопку «Запропонувати
  назву»; вікно «Назва й опис» має «Запропонувати» / «Ще варіанти» — 3 назви + 3 описи від
  `LOCAL_TITLE_MODEL` (MamayLM 12B) за саммарі Claude або фрагментами транскрипту. `POST
  /api/history/<id>/suggest-meta` нічого не пише. У Медіатеці після транскрибації запису без назви вікно
  відкривається само, коли готове саммарі (стеля ~3 хв).
- Під час запису модель не кличеться (`409 RECORDING_ACTIVE`) — 40 с генерації зупинили б копілот.

### Змінено
- Назва й опис запису однакові в Бібліотеці, Медіатеці й manifest сесії (PATCH з будь-якого боку).
- Медіатека не посилається на мʼяко видалений транскрипт.

## [4.11.0] — 2026-10-14

### Додано
- **Фільтр приватного на виході MCP** (спека `mcp-visual-privacy`, рада GREEN у раунді 1). Кадри й
  текст з екрана йдуть через MCP у Claude, тож фільтр стоїть там (`app/services/privacy.py`, без моделі,
  fail closed): записи з напрямків `RECALL_PRIVATE_CATEGORIES` (дефолт «Юридичне», «Особисте», з усіма
  нащадками) не віддають візуального в жодну тулзу, включно з екранними чанками в `search_archive`,
  `grep_archive` і `ask_archive` (до виклику Claude); кадри/відрізки/чанки зі словами з `private.txt`
  (у `.gitignore`, приклад — `private.example.txt`; морфології нема — пиши основу) прибираються,
  лічильник `private_hidden`. Перемикач `RECALL_MCP_VISUAL` = images (дефолт) | text | off. UI Recall
  фільтр не зачіпає. Той самий стоп-лист візьме бот K4.
- `get_frame` перевіряє текст саме показаного кадру (`frame_text`, живий OCR), а не представника відрізка.

## [4.10.1] — 2026-10-13

### Виправлено
- **Фото з Telegram дочитуються OCR-ом** (спека `tg-photo-ocr`, рада GREEN у раунді 4). Через
  непрацюючий OCR усі 862 фото з Telegram лежали в архіві без тексту з картинки (599 — лише
  заглушка «[фото без тексту]»). Прохід `python -m app.services.tg_photo_ocr run [--dry-run]`
  перечитує оригінали, дописує текст до підпису й переіндексує; ідемпотентний.
- **Паролі й токени маскуються в самому OCR** (`document_parser`): фото й скани з Telegram,
  завантаження з UI — так само, як кадри відео. Маска ідемпотентна (повторне маскування більше не
  дописує «секрет]»).
- **Збій розбору медіа з Telegram більше не мовчить**: запис позначається `parse_failed`,
  `telegram_coverage` показує `media_unparsed`; фото без тексту збоєм не вважається.

### Інструменти
- `Recall.bat` — лише кнопка: відкриває Recall, а якщо він не запущений — стартує задачу
  `RecallAutostart`. Власної копії `app.py` більше не піднімає і процесів не вбиває (друга копія
  тримала порт зі старим кодом після випуску; сліпе вбивство обірвало б живий запис).
- `scripts/restart-recall.ps1` гасить і чужу копію `app.py`, що тримає порт 5050 (по PID-дереву).

## [4.10.0] — 2026-10-13

### Додано
- **Візуальна стрічка відеозаписів** (спека `video-visual-timeline`, рада GREEN у раунді 2):
  запис екрана Recall розкладається на мінімум кадрів — по одному на кожну зміну картинки з
  точністю ~1 с (один прохід 1 fps у мініатюри; 2-годинний запис — ~2 хв), на кожен кадр —
  текст з екрана (локальний OCR), тип, позначка повтору й фрази транскрипту за той самий час.
  Зображення не зберігаються — кадр ріжеться з відео на запит. 5 MCP-тулзів: `video_analyze`,
  `get_visual_timeline`, `get_frame` (кадр картинкою), `get_frames_for_text`, `search_visual`;
  кадри видно й у `search_archive`/`ask_archive` як «кадр ГГ:ХХ:СС». Глобальний скіл
  `recall-video`. Докладно — `docs/video-timeline.md`.
- **Секрети з екрана**: паролі, токени, ключі в OCR маскуються `[🔒 секрет]` до пошуку, RAG і
  MCP, на кадрах зафарбовуються; сирі значення — лише локальним скриптом
  `python -m app.services.screen_secrets reveal <id>` у файл.

### Виправлено
- **OCR документів і кадрів знову працює.** З 14.08 `ocr_available()` завжди повертав False
  (незімпортований хелпер), а Tesseract у системі не стояв. Тепер Recall знаходить Tesseract поза
  PATH і мовні пакети в `%LOCALAPPDATA%\Recall	essdata` (`OCR_TESSERACT_CMD`, `OCR_TESSDATA_DIR`).

### Інструменти
- `scripts/restart-recall.ps1` — безпечний перезапуск бойового Recall після випуску: не чіпає живий запис, гасить лише дерево PID задачі `RecallAutostart`, чекає `/api/ready` і звіряє версію (`-Check` — лише звірка).

## [4.9.1] — 2026-10-09

### Виправлено
- **Повнотекстова гілка пошуку по архіву — з десятків секунд до сотих** (спека
  `fts-filter-pushdown`, рада GREEN у раунді 1). Фільтр «запис не видалений / не дубль /
  напрямок, теги, період, скоуп» у `_fts_search` і в кандидатах дослідження SQLite
  передавав у FTS5 як список rowid, і FTS5 ганяв MATCH окремо на кожен id: 13–57 с на
  питання на архіві з 31 чанків. Тепер фільтр лишається поза FTS5 (`+rowid IN`):
  ≤ 0.12 с, видача та сама — 110 з 110 пар (55 golden-питань × candidate_k 40/60)
  збіглись рядок у рядок. Прискорює `ask_archive`, пошук, підказки копілота й
  дослідження; eval-канарейка, що висіла 40+ хв, проходить за 57 с.

## [4.9.0] — 2026-10-08

Recall сам помічає дзвінок і питає «Записати?», а коли дзвінок затих — «Зупинити?»
(спека `k2-call-autoprompt`, 2 історії, рада GREEN у раунді 1). Без натискання запис
не стартує ніколи.

### Додано
- **Детектор дзвінка** (`app/services/recording/call_watch.py`). Дзвінок — це застосунок зі
  списку `RECORDING_AUTOPROMPT_APPS` (Zoom, Teams, Telegram, Viber, WhatsApp, браузер для
  Meet, Discord, Slack, Signal), у якого 30 с поспіль (`RECORDING_AUTOPROMPT_SECONDS`)
  активні сесії мікрофона і звуку і в цьому вікні справді звучали обидві сторони. Диктовки
  (VoiceDrop, Wispr Flow) відсікає список, YouTube і музику — відсутність мікрофона.
  Вмикається `RECORDING_AUTOPROMPT` (типово увімкнено). Кожне рішення пишеться в
  `logs/call_watch.jsonl` (без PID і без мовлення) — за ним через 1–2 тижні буде замір і
  пороги (`docs/specs/k2-call-autoprompt/followup-measure.md`).
- **Сповіщення Windows з кнопками** (`app/services/recording/toast.py`, WinRT без
  pip-залежностей). «Записати» стартує запис мікрофона і звуку лише цього застосунку тим
  самим кодом, що кнопка сторінки запису. Якщо на «Зупинити?» ніхто не відповів і 10 хв
  тиша на обох доріжках (`RECORDING_AUTOSTOP_SILENCE_MIN`), запис зупиняється сам і
  зберігається повністю.
- **Плашка у відкритій вкладці Recall** з тими самими діями і **предвибір джерел**: «Інші
  джерела» відкриває форму запису з галочкою «Системний звук — лише <застосунок>» і
  мікрофоном дзвінка.

### Відомі обмеження
- Чотири дрібні зауваження ревʼю (повторна підказка після «Не потрібно» через паузу в
  розмові, підказка не зникає після ручного старту запису того ж застосунку, «Зупинити?» при
  записі всього системного звуку, порядок `CoUninitialize`) розбираються разом із заміром —
  `docs/specs/k2-call-autoprompt/plan.md`, `## Descoped`.

## [4.8.2] — 2026-10-08

### Змінено
- **Вигадані приклади замість реальних** у підказках полів («Спитати архів», головна, скоуп
  дзвінка, дослідження), онбордингу й налаштуваннях напрямків, в описах MCP-тулзів і в
  прикладі промпту збагачення: напрямок «Агенція» → проєкти «Маяк», «Орбіта», бренд Polaris,
  люди Олена й Тарас. Жоден реальний напрямок, проєкт чи людина в інтерфейсі більше не світиться.

## [4.8.1] — 2026-10-08

### Змінено
- **Визначення мови на кожному вікні — лише для «Авто-визначення».** У 4.8.0
  `WHISPER_MULTILINGUAL=1` перекривав і явно обрану мову («Українська» → мова все одно
  визначалась кожні ~30 с), а сліпі пари міряли його тільки на змішаних дзвінках; на
  одномовних ризик хибного перемикання uk→ru не заміряно. Тепер обрана мова — закон, а
  на «Авто-визначенні» (типово в Аудіотеці) мова визначається на кожному вікні.

## [4.8.0] — 2026-10-08

Whisper пише імена людей, проєктів і організацій правильніше, а змішану uk/ru/en мову —
мовою того, хто говорить (спека `k1-name-vocabulary`, 3 історії + ремонт, рада GREEN у
раунді 2). Рішення прийнято за заміром на розмітці власника:
`docs/specs/k1-name-vocabulary/measure/report.md`.

### Додано
- **Словник «важких» імен із графа сутностей** (`app/services/vocabulary.py`,
  `RECALL_VOCAB_MODE`, типово `hotwords`). Імена ранжуються за зустрічами й тим, як часто
  Whisper їх уже калічив; переклад і транслітерація того самого імені («Гугл» при Google,
  «Олексій» при «Алексей») помилкою не вважаються — вирішує фонетичний ключ поперек
  uk/ru/en. Близько 50 імен (≤223 токени) іде у faster-whisper на файлах, записах, живій
  транскрипції і голосових TG; YouTube — ніколи. Замір: імена 3/20 → 7/20, зайвих імен на
  контролі 0, транскрипція повільніша на ~11 % (≈ +3 с на годину аудіо; власник прийняв
  понад поріг 10 %).
- **Визначення мови на кожному вікні до 30 с** (`WHISPER_MULTILINGUAL`, типово `1`) на повній
  транскрипції, файлах і голосових TG; сліпі пари власника на слух — 7 з 10 на користь.
- **Інструмент заміру** `tools/measure_vocabulary.py` (`candidates` / `run` / `ab` / `score`).
- Карта дефекту `translation-counted-as-asr-error` з гейтом
  `python -m app.services.vocabulary check-error-pairs`.

### Змінено
- На послідовному шляху (файли й записи до 10 хв, голосові TG) зі словником попередній
  текст не підставляється в промпт — як на batched-шляху. Без цього промпт переповнював
  448 токенів і транскрипція падала цілком. Ціна: крупніші сегменти на коротких записах.

### Виправлено
- **Telegram (адмінка)**: у рядку чату видно назву, тип, @нік і телефон; чіпи тегів прибрано;
  пошук за імʼям (кирилиця/латиниця), @ніком і телефоном у будь-якому записі. Слухач віддає
  `phone`, `first_name`, `last_name`, `bot`, `deleted` (потрібен рестарт Recall).
- Встановлення з нуля: venv, `setuptools<81`, torch cu121 до requirements, `av<19`.
- `test_history_filter_category_specific`: фільтр напрямку з таксономією включає його проєкти.

## [4.7.0] — 2026-09-28

Паралельні записи з ізоляцією системного звуку по застосунку. Привід: шестигодинна
конференція в Zoom пишеться (екран + системний звук), а паралельно треба записати
робочий дзвінок (мікрофон + системний звук без Zoom), і звук одного не має потрапляти
в запис іншого (спека `parallel-recordings-app-audio`, 11 історій, 7 хвиль, рада GREEN
у раунді 3, власник ACCEPTED після живої перевірки на бойовому Recall).

### Додано
- **Кілька записів одночасно** (`RECORDING_MAX_ACTIVE`, типово 2): у кожного запису свої
  галочки мікрофона й системного звуку; третій старт → `409 SESSION_LIMIT`.
- **Системний звук по застосунку**: режими «весь / тільки ці / усе, крім цього» —
  Windows process loopback (INCLUDE/EXCLUDE дерева процесів) за інтерфейсом `AudioSource`
  (`app/services/recording/audio_source.py`, `process_loopback.py`); режим «весь»
  лишається на старому шляху `pyaudiowpatch` байт-у-байт.
- **Що зараз звучить** (`GET /api/recording/apps`): звукові сесії з усіх виходів, за exe;
  при старті другого запису — «Виключити X з цього запису?» і «Лишити в A тільки X?».
- **Перемикання джерела на льоту** (`POST /api/recording/<sid>/system-source`): без зупинки
  запису, стик добивається тишею, подія в маніфесті (`streams.system.source_events`),
  SSE `source_changed`; після стопу другого запису — плашка «Повернути весь системний звук?».
- **Копілот на одному записі** з явним перенесенням (`POST /api/copilot/move`,
  `409 COPILOT_BUSY`).
- **Екран запису**: вкладки на кожен живий запис, свій перемикач звуку, пауза і стоп;
  попередження про навушники і «два дзвінки в одному браузері — один процес».

### Змінено
- `/api/recordings/active` віддає `count` і `sessions[]`; `session_id` заповнений лише
  коли запис один. MCP-тулзи живого дзвінка (`get_active_recording`,
  `get_live_transcript`, `get_live_copilot`, `ask_live`) при кількох записах не
  вгадують — `AMBIGUOUS_SESSION` зі списком.
- `WRONG_SESSION` → `404 NOT_FOUND`; `SESSION_CONFLICT` → `409 SESSION_LIMIT`.
- Цілі захоплення — PID аудіосесій, зведені до коренів дерев; «усе, крім X» бере дерево,
  що реально звучить (при кількох деревах одного exe — два Chrome, справжній Zoom).

### Обмеження
- «Усе, крім» — рівно один застосунок (Windows: один PID на клієнта); «тільки ці» — до 4.
- Два дзвінки в одному браузері не розділяються (один процес) — лише попередження в UI.
- macOS / Linux — пізніше за тим самим інтерфейсом.

## [4.6.0] — 2026-09-23

Напрямки стають деревом рівно з двох рівнів — напрямок → проєкти — плюс наскрізні
теги, що перетинають напрямки. Привід: «Фонд Робота» тримав більша частина архіву, а
проєкти всередині (Акмекорп, NOVA, Speak Easy…) не було куди рознести (спека
`directions-projects-tags`, 28 історій, 8 хвиль, рада GREEN у раунді 7). Розкладку
виконано на бойовому архіві 22–23.09: 4 напрямки, 25 проєктів.

### Added

- **Другий рівень напрямків** (міграція v45 `categories.parent_id`): проєкт — це
  вузол тієї самої таблиці, тож усі екрани, фільтри й MCP-тулзи, що ходять через
  `category_id`, працюють далі. Фільтр напрямку розкривається в піддерево (сам
  напрямок + його проєкти); сервіс `app/services/categories.py` — єдине місце
  розбору фільтрів, шляхів і письменників (без torch).
- **Канонічна назва вузла — повний шлях «Напрямок → Проєкт»** (`path` у
  `GET /api/memory/categories` і MCP `list_categories`). Назва унікальна серед
  братів (міграція v47 — перебудова `categories`, перша неаддитивна), тож тезки в
  різних напрямках дозволені; гола неоднозначна назва ніколи не резолвиться мовчки —
  409/нота зі списком шляхів; `→` у назві вузла заборонено.
- **Теги** (M:N, `tags`/`record_tags`): ручні на сторінці запису, підказка Claude
  при збагаченні (прийняти/відхилити), успадкування від Telegram-чату на інжесті;
  CRUD, перейменування, злиття в Налаштуваннях.
- **Фільтри `tags` / `since` / `until`** поруч із напрямком: `/api/history`,
  `/api/audio/downloads`, `/api/research/*`, `/api/memory/search|ask|action-items`,
  `retrieval.search` (обидві гілки) і 9 MCP-тулзів; `tags` — OR, разом із
  `category_id` — перетин. Нових тулзів немає, `about()` — ті самі 35.
- **SPA:** селект напрямків деревом, чіп-пікер тегів (порожній словник пояснює, що
  таке тег, і веде в Налаштування), фільтр періоду і `<details>`-підказки на
  Бібліотеці, Медіатеці, Задачах, Сутностях, Дослідженні, Запитати архів; чіп
  проєкту — імʼя, повний шлях у `title`, напрямок у рядку лише при тезці; крок
  чеклісту «розкладіть напрямки по проєктах». Service worker → `recall-v68`.
- **Мірило:** golden-set приймає `tags`/`expected_leak`, гейт рахує `filter_leak`
  (FAIL при > 0); +8 пунктів на напрямки/проєкти/теги.

### Changed

- **Контекстний префікс чанка** — «Напрямок → Проєкт … теги: …» для всіх типів
  записів; епоха ембеддингів 4 → 5 через `.env` (міграція v46 `embedding_epochs`
  + гард формату: новий префікс ніколи не пишеться в стару епоху; CLI
  `reembed epoch-check`). Зміна напрямку/проєкту/тегу ставить запис у точковий
  re-embed. Бойовий архів перебудовано (записів, 13 хв на RTX 3090); гейт з
  реранкером — PASS, `filter_leak=0`, паритет recall з v4.
- **Карта чатів Telegram** (`scope apply-chats`): рядок резолвиться парою
  `(parent, category)`; `--dry-run` — той самий писальний прохід у транзакції з
  відкотом (не окрема гілка плану), тож превʼю і реальний прохід не розходяться.
- **Копілот:** скоуп дзвінка на назву-тезку — картка-попередження з переліком
  шляхів у віджеті й у потоці подій, пошук без звуження; не «не знайдено».

## [4.5.0] — 2026-09-21

Власна назва та опис для всіх записів, документів і файлів — із редагуванням
після збереження. Приводом стало «Завантажити файл»: назва бралась з імені файлу
й переназвати запис не було як (спека `editable-title-description`).

### Added

- **Поля `title` і `description` у `transcriptions`** (міграція v44, адитивна).
  `source_name` лишається походженням і фолбеком: скрізь у видачі показується
  `display_name = title, якщо непорожній, інакше source_name`. Обидві колонки
  індексуються в `transcriptions_fts` — пошук Бібліотеки (`?q=`) бачить назву й опис.
- **`PATCH /api/history/<id>`** `{title?, description?}` — редагування будь-коли
  після збереження; порожня назва = повернення до імені файлу; `title` ≤ 200,
  `description` ≤ 4000. Сервіс `app/services/record_meta.py` (без torch —
  імпортується зі stdio-MCP).
- **Назва й опис на всіх входах створення:** `POST /api/transcribe` (файл, YouTube,
  запис, файл з Аудіотеки), `POST /api/documents/upload`, стоп-екран рекордера
  (`/api/recording/<sid>/save` приймає `description`). Файл з Аудіотеки при
  транскрибуванні переносить свої назву й опис у запис.
- **Аудіотека:** `PATCH /api/audio/downloads/<id>` `{title?, description?}`;
  список `GET /api/audio/downloads` віддає `description` (ремонт раунду 1 ради:
  без цього модалка відкривалась порожньою і наступне збереження тихо стирало опис).
- **SPA:** одна модалка «Назва + Опис» (`UI.editMetaModal`, новий `.rc-textarea`) —
  олівець біля заголовка запису (URL пере-слагується) і видима дія в рядках
  Бібліотеки та Аудіотеки; поля «Назва»/«Опис» у формах файлу, YouTube, документів,
  «Опис» на стоп-екрані рекордера. Дашборд, ⌘K, цитати відповіді читають `display_name`.
  Service worker → `recall-v64`.
- **MCP (read-only, як і раніше):** `list_recent`, `get_transcript`, `search_archive`,
  `ask_archive`, ресурс і експорт показують `display_name` поруч із `source_name`;
  `get_transcript`/`list_recent` віддають `title`/`description`. Нових тулзів немає.

### Changed

- **Контекстний префікс чанка** бере назву з `display_name` і додає рядок
  `опис: …` (≤300 символів), коли опис є. Після редагування чанки саме цього
  запису переембеджуються у фоні (`reembed.schedule_record_reembed`, локальна
  модель, $0); під час живого запису задача відкладається таймером, не займаючи
  слот `job_queue`; повторні правки одного запису згортаються.
- Експорти (txt/srt/md/docx/pdf, bulk, MCP) і чип цілі коментаря показують
  `display_name`; поле `source_name` у JSON-експорті лишається.

Вмикання Хвилі B на бойовому архіві — операція власника, яку сама хвиля
свідомо не робила, плюс знахідка, що з неї випливла.

### Added (вмикання Хвилі B на архіві, 18.09)

- **`--rerank` у гейті** (`evals/gate.py`): замір тим самим cross-encoder'ом,
  яким відповідає RAG-чат. Дефолт лишився `off`, щоб раніше зняті лінії не
  змінили сенс заднім числом, натомість прогін підписує конфігурацію сам — у
  рядку вердикту (`[gate] PASS k=8,12 [rerank=on, rewrite=off]`) і в
  `--json-out` під ключем `provenance`. Лінія з іншим `rerank` до порівняння
  не допускається.
- `rewrite` у провенансі прогону (борг Хвилі B): прапорцем не керується —
  `retrieval.search` читає гарячий `RAG_QUERY_REWRITE` сам, — але прогін
  зобовʼязаний сказати, з чим він знятий.

### Changed (вмикання Хвилі B на архіві, 18.09)

- **Архів переведено на епоху v4** (e5-large + контекстний префікс чанка):
  `reembed run` по 5758 записах, `failed=0`, 12.3 хвилини, $0; префікс у
  26 994 чанках із 27 002. Модель НЕ змінена — замір на трьох знімках показав,
  що весь приріст дали префікси, а Qwen3-0.6B при паритеті гірший.
- **`RAG_RERANK_POOL_SIZE=60`** (було 24, дефолт коду не змінено). `candidate_k`
  росте з `top_k` (`max(top_k*5, 40)`), а пул реранкера — ні, тому на бойовому
  `k=12` у предреранковий топ-24 набивалися шматки одних і тих самих записів і
  правильна запись до cross-encoder'а не доїжджала. Симптом: більший k робив
  видачу ГІРШОЮ.
- Позначено 8 дублів аудіо (`dedup_audio mark`) і записано 434 хеші — повтор
  тепер відсікається на інжесті, а не лише офлайн-проходом.

### Вимірювання (30 питань, реранкер увімкнено — як у бою)

| конфігурація | recall@8 | recall@12 | попадання по імені джерела |
|---|---|---|---|
| до префіксів (v2, пул 24) | 50.0% | 51.7% | 78.6% |
| префікси (v4, пул 24) | 60.3% | 56.9% | 92.9% |
| префікси + пул 60 | 55.2% | **67.2%** | **100.0%** |

Дедуп не зрушив жодного пункту з 29 порівнюваних: він економить слоти
контексту (на перевіреному питанні 12 фрагментів стали походити з записів
замість 7), а не піднімає recall.

## [4.4.0] — 2026-09-17

Хвиля B production-RAG — контекст чанка, конфігурована модель ембедингів і
переписування запиту (`docs/specs/production-rag-wave-b/`). Хвиля НЕ вмикає
нову модель і НЕ переписує вектори живого архіву: вона дає інструменти й
доказ, а перемикання — окрема операція власника після порівняння двох
прогонів гейта.

### Added

- **Сводки на одиницю сенсу** (`app/services/summaries.py`, міграція v42):
  TG-нитка отримує власну сводку з id повідомлень-джерел, моделлю і часом;
  `stats` показує покриття трьох типів (дзвінки / документи / нитки),
  `backfill --dry-run` рахує обсяг і вартість перед витратою.
- **CLI бекфілу карток** (`python -m app.services.enrichment backfill-cards`)
  для дзвінків і документів без сводки — з `--dry-run`, `--model` і `--kind`.
- **Контекстний префікс чанка** (міграція v43, `chunks.context_prefix`):
  тип, назва/чат, дата, автор/спікер, мітка нитки чи сторінка-секція плюс
  рядок сводки одиниці. Йде в ембединг і в BM25 (`chunks_fts` тепер над
  `(context_prefix, text)`), у цитати, експорт і підшивку ниток — ні.
- **Офлайн re-embed** (`python -m app.services.reembed run`): перерахунок
  застарілих за парою (модель, версія) записів на ЗНІМКУ, `optimize` в кінці,
  відмова йти по бойовій БД без явного `--yes-live`.
- **Порівняння двох прогонів гейта** (`python -m evals.compare`): провенанс
  обох поруч, середні на кожному спільному k з дельтою і per-item diff
  `hit→miss` / `miss→hit`. Гейт не чіпали — він навмисно відмовляється
  порівнювати різний провенанс.
- **Переписування запиту** (`app/services/query_rewrite.py`): Claude дає 1–3
  пошукових формулювання, вони йдуть тими ж vector+FTS гілками і зливаються
  наявним RRF. Прапорець `rewrite` у `retrieval.search`, за замовчуванням
  вимкнено (`RAG_QUERY_REWRITE`) — шов лежить у retrieval саме для того, щоб
  безкоштовний гейт міг виміряти ефект.
- **Мінінг golden-set** із `logs/mcp_calls.log` і `ask_log` у наявний набір без
  втрати розмічених пунктів; `--stats` друкує дефіцит до цілі.

### Changed

- **Модель ембедингів конфігурується, а не підмінюється**: стиль префіксів
  обирається за родиною моделі (e5 / qwen3 / plain), `EMBED_DIM` береться з
  завантаженої моделі, `EMBED_VERSION` — у реєстрі налаштувань. Дефолти
  лишились e5 / v2; пара `Qwen/Qwen3-Embedding-0.6B` + `3` вмикається двома
  рядками `.env` після PASS порівняння.
- **Векторна гілка пошуку бере лише поточну епоху** (модель, версія): вектори
  різних епох більше не змішуються мовчки при однаковій розмірності.
- `RAG_QUERY_REWRITE` читається одним парсером через реєстр в усіх місцях
  (ADR-008) — розбіжності між гейтом і живим UI/MCP на значеннях `yes`/`on`
  бути не може.

## [4.3.0] — 2026-09-16

Хвиля A production-RAG — гігієна і вимірювання (`docs/specs/production-rag-wave-a/`,
бриф гриля 16.09) — плюс спек `test-log-isolation`, що їде тим самим релізом
(гілка Хвилі A росла з нього). Рішення власника: перед тим як міняти модель
ембедингів і режим контексту (Хвиля B), архів має перестати брехати сам собі —
нитки без стелі, дублі аудіо в пошуку, контекст не в хронології, і жодного сліду,
які питання ставились і чи відповідь була корисна.

### Added

- **Лог питань `ask_log`** (міграція v41): кожне питання з UI і MCP осідає з
  каналом, скоупом, `k`, моделлю, цитованими джерелами, токенами і вартістю.
  Оцінка 👍/👎 із нотаткою на сторінці «Запитай архів» і
  `POST /api/memory/ask/<id>/rate`; відповіді UI/MCP несуть `ask_id`.
  `evals.build_golden --from-ask-log` бере звідти кандидатів у golden-set,
  👎 — першими.
- **Дублі аудіо/YouTube** (міграція v40, `transcriptions.duplicate_of`):
  інжест рахує `content_hash` нормалізованого тексту, копія отримує
  `duplicate_of` і не йде в збагачення; пошук, FTS і гідрація чанків дублі
  оминають; у Бібліотеці позначка «дубль #N». Офлайн-прохід
  `python -m app.services.dedup_audio mark --dry-run` для наявного архіву
  (dry-run на бойовій БД пройшов; сам прохід ще не запускали).
- **Стеля символів на нитку в підшивці контексту** (`RAG_THREAD_MSG_CHARS`,
  `RAG_THREAD_CHARS`): сусіди по 1500 символів, нитка до 8000, знайдене
  повідомлення отримує більший бюджет, ніж будь-який сусід, зайві сусіди
  відкидаються з маркером обрізки. До цього одна TG-нитка могла зʼїсти
  353k токенів на питання.
- **Контекст у хронології**: цитовані чанки йдуть за датою зустрічі, чанки
  однієї записи — за позицією, коментарі власника попереду; системний промпт
  каже, що при суперечності веде ПІЗНІША версія, а раніша згадується як «було».

### Changed

- **Тести більше не пишуть у бойові логи** (`test-log-isolation`): `app.py`,
  `telegram_listener.py` і `mcp_server.py` не чіпляють `RotatingFileHandler`
  під pytest (гейт `"pytest" in sys.modules`, бо `PYTEST_CURRENT_TEST` на
  фазі імпорту ще порожній). Лог бойового процесу знову свідок при розборі
  інциденту. Перевірено: за 112 тестів обидва логи не змінились ні на байт.
- `.env.example` перегенеровано з реєстру налаштувань (нові ключі `RAG_THREAD_*`).

### Fixed

- **Посилання в особистих чатах не вигадується**: формату посилання на
  повідомлення в особистому чаті не існує, тому `_chat_link` його не будує;
  для наявних рядків — ідемпотентна міграція
  `python -m app.services.telegram_link_repair --db whisper_history.db clear-bogus-links --dry-run`
  (`--db` перед підкомандою; на бойовій БД ще не запускали).
- `--dry-run` перед підкомандою CLI-проходів більше не гасне мовчки
  (спільний `dest` у subparsers накривав батьківський namespace).
- Ручне `POST /api/memory/backfill` (і `--force`) не будує чанки й платну
  картку Claude для запису з `duplicate_of`.

## [4.2.0] — 2026-09-12

Політика медіа в Telegram-інжесті (`docs/specs/tg-media-policy/`). Рішення
власника: відео не завантажується ніколи, важкі оригінали не лишаються на
диску. До цього інжест не зважував вартості — тягнув будь-який файл цілком
заради мови, якої в ньому могло не бути.

### Changed

- **Відео більше не завантажується — жодне, жодного розміру.** Гейт стоїть у
  єдиній точці завантаження (`_ingest_message`), тож діє одразу в живому
  слухачі, догрузці, backfill і точковому ремонті. Замість файла в архіві
  зʼявляється рядок із посиланням на повідомлення і `doc_type=video` — видно,
  що відео було і що його не оброблено, з підписом і без.
  Рядок обовʼязковий не лише для зручності: без нього повідомлення невидиме
  для дедупу і watermark догрузки й спливало б знову на кожному проході.
- **Відео, надіслане «файлом»**, ловиться за розширенням назви
  (`.mp4/.mkv/.mov/.avi/.webm/.wmv/.flv/.m4v/.3gp/.mpeg/.mpg`), бо Telegram
  часто лишає такі документи без `mime_type` — інакше вони качались би
  повністю як «документ».
- **Оригінал важчий за `TELEGRAM_ORIGINAL_MAX_MB` (дефолт 100 МБ) видаляється**
  одразу після видобутку тексту. Дрібні лишаються. Видалення безпечне за MCP
  `get_original_file`: відсутній файл там і так деградує в
  `original_available: false`.

### Fixed

- **Джерело не гине разом із тишею.** Whisper на глухому чи битому аудіо
  повертає `{"text": ""}` **без** ключа `error`; перша редакція вважала це
  успіхом і видаляла важкий оригінал, лишаючи в архіві саму заглушку.
  Успіхом тепер вважається непорожній текст.
- **Хибний поріг більше не валить фоновий job.** Порожнє чи нечислове
  значення в `.env` кидало `ValueError` між записом тексту і побудовою
  векторів — повідомлення назавжди лишалось поза пошуком і поза графом, бо
  ретраїв у черги немає. Відʼємне значення приймалось мовчки і видаляло б усі
  оригінали. Читання винесено в локальний безпечний хелпер; контракт
  `env_int` не змінено.
- Недописаний проміжний `_audio.mp3` після збою `ffmpeg` більше не лишається
  сиротою.

### Measured

Станом на 12.09.2026 у `telegram_media` лежало 3,4 ГБ, з них **2,04 ГБ — відео**
(24 `.mp4` + 19 `.MOV`); оригінали не видалялись ніколи. Один ролик у догрузці
качався понад дві години на 0,3 МБ/с і встиг зайняти 1,6 ГБ, тримаючи чергу з
27 чатів — завантаження в проході послідовні.

### Tests

- `tests/test_telegram.py`: 110 разом із реєстром налаштувань — відсутність
  виклику завантаження (а не лише наявність рядка), кирилічні фікстури,
  порожній і пробільний текст від whisper, три варіанти хибного порога,
  відео-розширення без mime і негативні контролі до нього.
- Повний прогін перед злиттям — у розділі нижче за фактом.

## [4.1.1] — 2026-09-12

Догрузка Telegram після простою більше не падає мовчки
(`docs/specs/tg-catchup-crash/`).

### Fixed

- **`_catchup()` падав у власному рядку логу.** `mark[:16]` різав словник
  `{"date", "msg_id"}` як рядок → `KeyError: slice(None, 16, None)`. Виняток
  стріляв **лише коли догрузка щось реально довантажила** (рядок під `if got:`),
  виходив за межі циклу по чатах і забирав із собою і решту чатів, і
  `_catchup_state_save()`. Наслідок на бойовій машині: `telegram_catchup.json`
  не оновлювався з **25.08.2026** — 18 днів кожен старт слухача розбивався об
  перший же чат із пропусками. Telegram — переважна більшість архіву, тож дірка
  була тиха й дорога.
- **Збій одного чату більше не коштує всього проходу.** Тіло циклу отримало
  власну межу: чат потрапляє в `stats["failed_chats"]`, у лог іде рядок із
  назвою чату, причиною і traceback (`exc_info`), решта чатів обробляється.
  `_catchup_state_save()` переїхав у `finally` — виконується на кожному шляху
  виходу, включно зі збоєм усередині самого обробника.

### Changed

- `_catchup()` повертає `stats` однакової форми на успішному і збійному
  проході (`failed_chats` заводиться від початку, а не `setdefault` при збої).

### Tests

- `TestCatchup` у `tests/test_telegram.py`: сторож дивиться на стан проходу
  (жодного чату у `failed_chats`) і на вміст рядка логу, а не лише на побічні
  ефекти — інакше межа чату з другої історії знеболювала б перший дефект.
  Перевірено відкотом: із поверненим `mark[:16]` тест падає.
- Повний прогін: **1930 passed, 3 skipped, 0 failed**.

## [4.1.0] — 2026-09-12

MCP-доступ до дзвінка, що йде **прямо зараз** (`docs/specs/mcp-live-call/`).
До цього агент у Claude Desktop/Code бачив архів, але не бачив живого запису:
поки дзвінок тривав, спитати про нього було нічим. Тепер — три read-only тулзи
до активної сесії; жодна нічого не пише ні в БД, ні у віджет оператора.

### Added

- **`get_live_transcript`** — знімок живого транскрипту активного запису
  (`GET /api/recording/<id>/live-transcript`). Курсор `since_seq` віддає лише
  нове з моменту минулого виклику.
- **`get_live_copilot`** — потік подій ко-пілота цілком: зміни тем, локальні
  інсайти, вердикти Claude і внутрішні `retrieval`. Кожна подія несе `shown` і
  `operator_saw` — агент бачить і ті картки, які бюджет уваги оператора
  придушив, з чесною поміткою, що людина їх не бачила.
- **`ask_live`** — питання до живого дзвінка і/або архіву через локальну модель
  (Ollama, $0, без Claude API): `POST /api/copilot/live-ask`, сервіс
  `app/services/live_ask.py`. Спікери підписані (`[Оператор]` / `[Співрозмовник]`).
- Число MCP-тулзів: **32 → 35**, усі read-only.

### Changed

- **Курсор живого транскрипту — за порядком додавання (`seq`), не за часом.**
  У доріжок мікрофона і системного звуку **різні шкали часу**: system-доріжка
  стартує з нуля, коли мікрофон уже на 230-й секунді. Курсор за часом викидав
  усі сегменти співрозмовника як «старі»; `since_seq` не губить жодного.
  Параметр `since_sec` прибрано з MCP-поверхні, щоб ним не можна було
  скористатися помилково.
- **Під час запису `ask_live` не викликає модель узагалі** (`mode: "raw"`) —
  віддає сирий транскрипт, і відповідь складає агент. Замір: локальна модель
  видає 9.6 ток/с, 512 токенів = 53 секунди замка на GPU при тіку живої
  транскрибації 6 секунд. Генерація лишилась тільки для завершених сесій.
- Число тулзів у тестах стало інваріантом (рахується з реєстру), а не літералом.

### Tests

- `tests/test_mcp_live.py`, `tests/test_live_ask.py` — гонка доріжок, курсори,
  повнота потоку копілота, відсутність мутацій.
- Повний прогін перед злиттям: **1928 passed, 3 skipped, 0 failed**.

## [4.0.1] — 2026-09-06

Раунд 3 ремедіації S3 (`docs/specs/config-registry-fix-r3/`). Знімає вердикт
BLOCK, поставлений `lead-review` на попередній раунд. Змін у поведінці для
користувача майже немає — це рівняння коду з тим, що про нього стверджувалось.

### Fixed

- **`/api/ready` більше не називає порожній файл БД придатною базою.** Перевірка
  вимагає таблиці `schema_versions` з `MAX(version) >= 1`, а не голого `SELECT 1`.
  Раніше сценарій «БД немає → монітор смикає `/api/health` → файл створено
  порожнім → `ready: true` на застосунку без даних» відтворювався повністю.
- **`/api/health` більше не створює БД як побічний ефект** — перейшов на ту саму
  перевірку, що й `/api/ready`. `app/db/connection.py` (гарячий шлях) не змінений.
- **`RECALL_BIND_ALL` читається одним парсером.** До цього `app.py` знав
  `1/true/True`, а `config.py` — `1/true/yes/on`: на значенні `yes` конфіг вважав
  застосунок мережевим і падав із `RuntimeError` при небезпечному `SECRET_KEY`,
  тоді як сервер забіндився б на `127.0.0.1`.
- `:memory:` у `DATABASE` віддає `error` з логом замість падіння на `as_uri()`.

### Changed

- **`RECALL_PRELOAD_WHISPER` і `RECALL_LOG_TO_FILE` переведені на реєстр
  налаштувань.** Наслідок на крайніх значеннях: `RECALL_PRELOAD_WHISPER` зі
  сміттєвим значенням тепер вимикає прогрів (парсер був інверсний і вмикав його);
  `RECALL_LOG_TO_FILE` додав `on` до істинних. Дефолти не змінились.
- Логіка прогріву whisper винесена з `app.py` у
  `app/services/whisper_preload.py::start_tracked_preload()`.
- Профіле-залежний запис у `.env.example` став валідним dotenv: пояснювальний
  коментар плюс один звичайний рядок замість двох, які після розкоментування
  давали синтаксично хибний `.env`.
- `telegram_listener.py` бере шлях сесії з конфігу і не обходить `BASE_DIR`.

### Tests

- Мірило прогріву доводить виконанням, що процес побував у стані «модель
  вантажиться» — через рядки продукту в лозі, а не через таймінг.
- Покриття JSON-логів червоніє від зламаного `app.py`; фейковий «наскрізний»
  тест, що копіював умову в себе, прибраний.
- Скан env-читань бачить читання через реєстр (96 → 133 назви, поріг 133) —
  раніше міграція на `settings.env` робила їх невидимими, і покриття всихало мовчки.
- Офлайн-набір: 1864 passed, 3 skipped, 0 failed (було 1847).

### Known debt

- `FORCE_CPU` не доходить до `ModernWhisperManager` — мірило headless стверджує
  CPU-boot, а виконує GPU-шлях. Передано в S4 (`docs/specs/linux-core-ci/brief.md`),
  де воно вистрелить: Linux CI без GPU.

## [4.0.0]

Стан до введення CHANGELOG. Історія — у git.
