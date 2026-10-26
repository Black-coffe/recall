# ADR-042: MCP full control supersedes read-first

- Status: Accepted (2026-10-26, spec 9 `mcp-triggers` closes the program; see "Final status")
- Date: 2026-10-18 (decisions 5 and 11 amended 2026-10-19 for spec 3)
- Narrowed by ADR-051 (2026-10-26): the owner's own UI Stop/Discard runs without the dialog, and the agent's
  `recording_stop` / `recording_cancel_own` are D actions allowed during a recording; decision 5 and the
  "refused during a recording" invariant read with those exceptions.
- Spec: docs/specs/mcp-foundations-a (draft); finalised by docs/specs/mcp-triggers; decisions 5 and 11: docs/specs/mcp-foundations-b

## Context
The MCP surface was decided read-first on 02.07.2026: write tools were removed, and later only two narrow writes
(`mailbox`, `settings`) were added. The owner now wants the MCP to reach what the UI reaches.

<!-- verbatim:start - owner quote, kept in the original language with the English translation -->
Request: "И я хочу расширить функционал MCP, убрать раз и навсегда статус read-only и добавить ему статус всего функционала, который есть в визуальном интерфейсе." (I want to extend the MCP, remove the read-only status once and for all, and give it all the functionality the visual interface has.)
<!-- verbatim:end -->

<!-- verbatim:start - owner quote, kept in the original language with the English translation -->
Choice for approving destructive actions: "Окно Windows от Recall (Рекомендую)" (a Windows window raised by Recall, recommended).
<!-- verbatim:end -->

Spec 3 forces: app.py runs under `pythonw.exe` in the interactive session 1 (scheduled task, `LogonType Interactive`),
serves with Werkzeug `threaded=True`; the only proven origin is `ui` (per-boot token), every other header is a label;
MCP `_api` times out at 60 s; the twelve destructive routes that exist today are open to `curl`.

## Options
- Keep read-first and add write tools one by one without a model: rejected, nothing tells a reader which tool can change
  what, and a wrong hint weakens client prompting.
- A gate key in `.env` plus a toast or browser approval page: rejected, the agent runs in the same user session, can read
  the key, and a browser page can be pressed by browser automation.
- One registry with classes, proofs instead of labels, a native dialog for destructive actions: chosen.
- Dialog mechanism (spec 3): in-process `MessageBoxW` via `ctypes` on its own thread (chosen: no dependency, system-modal,
  works from `pythonw.exe`); tkinter (rejected: not system-modal, Tk off the main thread of a Flask process is unsafe);
  a child process (rejected: `.venv\Scripts\pythonw.exe` is a shim, killing it on timeout leaves the real interpreter
  and its window alive).

## Decision
1. **Read-first (02.07.2026) is retired.** The MCP is a full-control surface, governed by the rules below.
2. **R/W/D registry.** Every tool has one entry in `_TOOL_CLASS` in `mcp_server.py`: class R (read), W (write) or D
   (destructive), provenance kind, open-world flag and declared limits. R means "changes no archive table"; bookkeeping
   rows (ask log, job rows, audit) are allowed. MCP annotations and `_meta` are derived from the entry, and a test fails
   on a tool without one. `video_analyze` is W. After spec 2 there are 45 tools: 42 read-only and 3 write.
3. **Profiles.** `RECALL_MCP_PROFILE=full` (default) registers every tool; `read` registers the R tools plus
   `video_analyze`; any other value is treated as `read`. `RECALL_MCP_READONLY=1` registers no W or D tool at all.
4. **Proofs, not labels.** `classify_request()` recognises two origins: `ui`, proven by the per-boot UI token, and
   `unknown`. `X-Recall-Actor` is a label for the audit and never changes a decision. The token is issued but not yet
   enforced; nothing consumes the classification before spec 3. Against a local process `ui` is a label too: the
   token sits in the shell page, so any process that can `GET /` can send it, look like the UI in the dialog's
   "who asks" line and skip the recording auto-refusal. The dialog stays the only decision, and its `ui` line also
   says "if you did not just do this, press No" (repair round 2).
5. **Destructive actions (spec 3, `mcp-foundations-b`).** Every D action, whoever sends it (MCP, hook, curl, script,
   the UI itself), is approved only through a native Windows dialog raised by app.py; a browser page is not an approval
   surface, and the SPA drops its own `UI.confirmModal` on the gated buttons so there is exactly one confirmation.
   Exception (owner, during build): bulk delete is a physical delete with no restore, so it keeps its type-to-confirm
   in the UI before the Windows dialog (owner's choice 2026-10-19: "restore the typed confirm plus the window"); the type-to-confirm is an extra step,
   never a substitute for the dialog. Rejected: one window for bulk delete. Bulk delete refuses the whole request if
   any requested id is missing when the dialog is built, so only listed records can be deleted.
   - *Mechanism.* `app/services/approvals.py` (no Flask import) raises `user32.MessageBoxW` through `ctypes` on a
     dedicated thread with `MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SYSTEMMODAL | MB_SETFOREGROUND | MB_TOPMOST`
     (default button = No, so Enter denies); the request thread waits on a `threading.Event` for 45 s. Yes → approved.
     No or timeout → 403 `APPROVAL_DENIED` with `reason: "no" | "timeout"`. Not Windows, an invisible window station
     (`GetUserObjectInformationW(UOI_FLAGS)` without `WSF_VISIBLE`) or `MessageBoxW` returning 0 → 503
     `APPROVAL_UNAVAILABLE`. On timeout the window is closed best-effort (`FindWindowW` on the unique title +
     `PostMessageW(WM_COMMAND, IDNO)`); a click that lands after the deadline is discarded. The undocumented
     `MessageBoxTimeoutW` is not used. *Spike result (2026-10-19, `pythonw.exe`, session 1, no recording
     active):* a `MessageBoxW` raised from a non-main thread is shown, the window station reports `WSF_VISIBLE`,
     `FindWindowW` finds the box by its unique title and `PostMessageW(WM_COMMAND, IDNO)` closes it at the deadline (the
     box returns IDNO, about 8 s in the run); a click returns IDYES. Nothing in the mechanism above was refuted.
   - *One dialog at a time.* A second D request while one is pending gets 409 `APPROVAL_PENDING` at once; nothing is
     queued. Rejected: a FIFO (stacks 45 s waits past the 60 s MCP timeout; the owner cannot tell which action a queued
     window belongs to).
   - *Where the gate lives.* `app/core/approval_gate.py` registers one `before_request` immediately after
     `auth.register(app)`, keyed on Flask endpoint names in a `_D_ROUTES` table (the twelve routes of the recon plus `POST /api/history/bulk_delete`, thirteen). Each
     entry names an action from the D enum (F3) and a summary builder in `app/services/approval_summaries.py` that reads
     the object name and the blast-radius count from the DB (`request.view_args` and the cached JSON body are available
     there). A builder that finds no object skips the dialog and lets the view return its own 404. A precondition the
   view would refuse (a direction with projects, the self speaker) is answered by the gate itself, no dialog, in the
   view's shape (409 `has_projects` with the project list; the self-speaker refusal), so a destructive view never runs
   without a dialog and no race opens between the gate's check and the view (owner, after escalation round 3:
   "never runs a destructive view without a dialog (no gate->view race)"). A body the builder cannot describe (bad ids, a bulk id missing at summary time, a `limit` that is not a
   positive int within the route's bound) is refused with 400 `refused:*` and gives its ceiling slot back. Rejected: a
     `@requires_approval` decorator on each view (the same risk of forgetting, twelve blueprint edits, the audit has to
     find the decision in another place). A test walks `app.url_map`: every `DELETE` route is in `_D_ROUTES` or in
     `_D_EXEMPT` with a reason.
   - *Text (A3, F10).* Built from the action enum and the DB in the owner's UI language, never from the request:
     verb; object kind, name and id; how many rows it touches; whether an undo exists; who asks (`ui` or `unknown`,
     with the claimed `X-Recall-Actor` / `X-Recall-Tool` shown as "claims to be"). Model text reaches the dialog only
     through an optional `X-Recall-Note` header, stripped of `«»`, cut to 60 characters and quoted under "Text from
     the agent:"; DB names in the dialog are stripped of `«»` too, so no text can close the quote early; a Telegram `chat_id` missing from `tg_monitored_chats` is shown quoted with a "not verified" mark; no tool
     sends it yet, the contract is fixed here so spec 4 does not invent another.
   - *Recording (F11, Ask 4).* A non-`ui` D request while a recording is active gets 409 `RECORDING_ACTIVE` with no
     dialog; a `ui` request still gets the dialog. The gate calls the probe fail-closed, `recording_active(default=True)`;
     the existing callers keep the fail-open default.
   - *Ceilings (F24, Ask 6).* D: 3 per minute and 20 per day per actor class (`ui`, `unknown`), counted on every
     request that reaches the gate (denials and timeouts included, that is the loop signal), checked before the dialog
     → 429 `RATE_CEILING` `{retry_after_sec}`. W: 10 changes per family per 10 minutes, enforced MCP-side in
     `_api(write=True)` from `_TOOL_CLASS[tool].limits`, counted on 2xx, same error shape. Rejected: W families in
     app.py (it cannot trust the tool label and would duplicate the family table). Counters are in-process and reset on
     restart: a loop guard, not a security control.
   - *Tests and headless.* The dialog function is a module attribute replaced in-process (`tests/conftest.py` autouse →
     deny); nothing outside the process, no env var and no key, turns a denial into an approval (decision 6).
   - *Residual risk (A4).* An agent with desktop control can press the dialog; a host without a desktop cannot approve
     D actions at all, from the UI included (fail closed). `docs/SECURITY.md` states both.
6. **Same-user bypass, stated plainly.** There is no key in `.env` and no gate mode that lets a request skip the dialog.
   A process running as the same OS user can still read the database and files directly; the dialog protects the app's
   own routes, not the user's account.
7. **Host and Origin rule.** A direct request from a loopback address with a non-loopback `Host` loses local trust on
   `/api/*` and gets 401 without a key; other paths get 403 `HOST_REJECTED`. A non-GET `/api/*` request with an `Origin`
   passes only for a loopback `http` origin on the same port as `Host`, otherwise 403 `ORIGIN_REJECTED`. The proxied
   branch (Tailscale serve, K5 links) and non-loopback remotes are untouched.
8. **Event ring.** `sse_broker` keeps a per-channel ring of 500 events with a sequence cursor, served by
   `/api/automation/events` and consumed by the `wait_for` tool. Beyond the ring or after a restart it reports
   `gap` or `reset` instead of replaying.
9. **Error codes.** Every MCP error is `{ok: false, error_code, error, hint?, status_code?}`, normalised on the MCP side
   only; blueprint response shapes the SPA reads are not rewritten. `app/core/error_codes.py` (stdlib only) maps every
   code to its canonical HTTP status, and `tests/test_error_codes.py` fails when an `error_code` literal in
   `mcp_server.py`, the blueprints or `app/core/auth.py` is missing from it. Dynamic codes are added by hand.
   Spec 3 adds `APPROVAL_DENIED` 403, `APPROVAL_UNAVAILABLE` 503, `APPROVAL_PENDING` 409, `RATE_CEILING` 429, and
   extends the scan to `app/core/approval_gate.py`.
10. **Jobs hygiene.** A failed job stores a code and a generic text, never the raw exception. A cancel that arrives too
    late is reported as `cancel_ignored`. Slow kinds run on their own single-worker executor.
11. **Audit `mcp_actions` (spec 3) and what stays out.** Migration v53 creates `mcp_actions(id, at, request_id, actor,
    claimed_actor, claimed_tool, method, path, endpoint, action, object_ref, summary, decision, http_status, error_code,
    duration_ms)` with an index on `at`. `app/core/audit_hooks.py` registers one `after_request` that writes a row for
    every non-GET `/api/*` request, 4xx/5xx and gate denials included, and one `teardown_request` that writes the row
    with status 500 when the request produced no response. A `POST /connect/<kind>/<token>` (key and mailbox saves) is
    written too, with the path stored as `/connect/<kind>/*`. Excluded: GET, a non-D request carrying the listener's
    valid `X-Telegram-Token` (a `_D_ROUTES` request is always written), and endpoints in `_AUDIT_SKIP`
    (high-frequency live POSTs, each with a reason). No request body or
    query string is stored: `summary` and `object_ref` come from the gate's DB-built summary; `X-Recall-Args` is not
    read (deferred to the spec that ships the first tool needing it; a column is additive). `claimed_*` are labels and
    are stored as such. Rows older than 90 days are purged at boot by `retention.purge_mcp_actions`, beside
    `purge_soft_deleted`. `list_mcp_actions` (R, `provenance_kind=system`, direct DB) filters `since`, `actor`,
    `decision`, `action`, `path_prefix`, `status_min`, `limit` ≤ 200 with an id cursor, and returns the claimed labels
    under `claimed` with `trusted: false`. The tool count becomes 46 (43 read-only + 3 write).
    Still outside this ADR after spec 3: `X-Recall-Args`, the mail 12-month cap on `mailbox(sync)`,
    `category_change_exposes`, werkzeug logging of the connect token path, `get_job_status(wait_seconds)`, the `recall`
    skill text.

## Consequences
The MCP can change the archive in classes W and D under the rules above; clients get one error shape and one code table.
After spec 3 the thirteen destructive routes answer `curl` with a Windows dialog or a denial; the SPA shows a "waiting
for the Windows window" state instead of its own confirm; a headless host has no working delete at all; every
mutating request leaves an audit row for 90 days; the W ceiling lives per MCP process (Desktop and Code each get their
own 10 per family). Accepted debt: `_D_ROUTES` is an allowlist a new destructive route must join by hand (the
`url_map` test catches the `DELETE` verb only); the daily summary with undo (owner Q4) reads `mcp_actions` in spec 8.

## Invariants created (spec 3)
- A D route runs only after `approvals.ask()` returned `approved` in this very request; no header, env var, key, profile
  or mode produces `approved` without a dialog. A refusal the view would give is answered by the gate, never by
  running the view; a body the gate cannot summarise is refused (fail closed).
- The dialog text is built from the D enum and the DB in the owner's UI language; request-supplied text appears only
  quoted, cut to 60 characters, under "Text from the agent:".
- Any failure to raise, show or finish the dialog is a denial: timeout, no desktop, not Windows, `MessageBoxW` error.
- At most one approval dialog exists at a time; a concurrent D request is refused with `APPROVAL_PENDING`, never queued.
- A non-`ui` D request during a recording is refused before any dialog; the recording probe is read fail-closed there.
- Every non-GET `/api/*` request writes one `mcp_actions` row, denied and failed ones included, with no body and no
  query string; `X-Recall-Actor`/`X-Recall-Tool` are stored as claimed labels, never as the actor.
- Family (W) ceilings are counted where the tool name is trustworthy, in the MCP process; D ceilings are counted in
  app.py per actor class. Both are loop guards, not security controls.
- The gate consumes `classify_request()`; a new origin proof is added there, never inferred in the gate.

## Revisit when
A desktop-control agent is part of the owner's normal sessions; the owner hits the `ui`-class D ceiling in a clean-up
pass (raise the numbers, not the mechanism); the headless profile needs deletions; a second interactive session or OS
user runs Recall; the `MessageBoxW` close path stops working after a Windows update (then the stale window stays and only the
deadline rules, which is still fail-closed).

## Final status (2026-10-26)
The program in `docs/specs/mcp-full-control/report.md` §9 is closed: nine specs, 95 tools (56 read + 39 write, of
which 16 are D), under the tool cap of 100 (ADR-052). The decisions above stand as written, read with the ADR-051
exceptions.

Shipped:

| # | Spec | Version | What it added |
|---|------|---------|---------------|
| 1 | `mcp-mail-connect` | 4.19.0 | `mailbox`, `settings`; secrets only on a one-time local connect page |
| 2 | `mcp-foundations-a` | 4.20.0 | `_TOOL_CLASS` registry, derived annotations, profiles, error envelope and codes, Host/Origin rule, `wait_for` |
| 3 | `mcp-foundations-b` | 4.22.0 | the Windows dialog for every D route, `mcp_actions` audit, `list_mcp_actions`, family ceilings |
| 4 | `mcp-mail-and-keys` | 4.24.0 | the first D tools: mail backfill, label enrich, disconnect, removing the Anthropic key; `system_status` |
| 5 | `mcp-archive-org` | 4.26.0 | tasks, bookmarks, comments, taxonomy, speakers, transcripts, with their D tools |
| 6 | `mcp-pipeline` | 4.27.0 | transcription, documents, audio, jobs, models, `transcript_ai`, diagnostics |
| 7 | `mcp-telegram-automation` | 4.31.0 | Telegram chats and backfill, `automation_status`, `pipes` run |
| 8 | `mcp-recording-live` | 4.32.0 | `recording`, `copilot`, `recording_stop`, `recording_cancel_own`, devices, copilot timeline |
| 9 | `mcp-triggers` | this spec | skills in `skills/`, the MCP prompt `post_digest`, `docs/recipes/hooks.md`, the `recall` / `recall-ro` / `recall-scheduled` registrations |

(The report numbers `mcp-recording-live` 7 and `mcp-telegram-automation` 8; they shipped in the order above.)
`mcp-token-budget` (4.21.0) ran beside the program and set the read tools' page budget.

Dropped:
- The `client` gate mode and `RECALL_MCP_UNATTENDED_ALLOW`: decision 5 / §11 A1-A2, no key or mode skips the dialog.
  A scheduled run that reaches a D tool is refused when nobody answers (`APPROVAL_DENIED` after 45 s).
- `_HOOK_ROUTES` and the gate key for hooks: a hook is an unproven caller like any agent; it gets the toast before a
  recording and the dialog before a D action. The recipes say so.
- The Q1 merges of existing read tools: not needed under the cap of 100 (ADR-052) and declined in the grill of spec 9.
- Auto-record on a detected call (`RECORDING_AUTOPROMPT_ACTION`): moved to a separate recording spec together with the
  K2 measurement fixes (`docs/specs/k2-measure/report.md`).
- The `transcribe_file` MCP prompt and every other path-taking prompt: Claude Code splits prompt arguments on
  whitespace (report fact 3d); `post_digest` is the only prompt.
