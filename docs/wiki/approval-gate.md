---
domain: mcp-approval-gate
tags: [mcp, security, approval, audit, adr-042]
related: [adr/042-mcp-full-control-supersedes-read-first.md, SECURITY.md, MCP_SETUP.md]
last-verified: 2026-10-21
---

# Approval gate: a destructive action runs only after a native dialog said yes

Every D action is approved through a Windows dialog raised by `app.py` (`app/services/approvals.py`, one
`before_request` in `app/core/approval_gate.py`). The audit of every changing request is the table `mcp_actions`,
read with the MCP tool `list_mcp_actions`. The invariants below are quoted from ADR-042 (spec 3).

## Invariants
- A D route runs only after `approvals.ask()` returned `approved` in this very request; no header, env var, key, profile
  or mode produces `approved` without a dialog.
- The dialog text is built from the D enum and the DB in the owner's UI language; request-supplied text appears only
  quoted, stripped of `«»`, cut to 60 characters, under "Text from the agent:". A Telegram chat id Recall does not
  monitor is shown quoted with a "not verified" mark.
- Fail closed on bodies: a body the summary cannot describe (bad ids, a bulk id missing at summary time, a `limit`
  that is not a positive int within the route's bound) is refused with 400 `refused:*`, no dialog, no view, and does
  not spend a D ceiling slot. A precondition the view refuses itself (direction with projects, self speaker) skips the
  dialog and returns the view's own error.
- Any failure to raise, show or finish the dialog is a denial: timeout, no desktop, not Windows, `MessageBoxW` error.
- At most one approval dialog exists at a time; a concurrent D request is refused with `APPROVAL_PENDING`, never queued.
- A non-`ui` D request during a recording is refused before any dialog; the recording probe is read fail-closed there.
- Every non-GET `/api/*` and `/connect/*` request writes one `mcp_actions` row, denied and failed ones included, with
  no body and no query string; a connect path is stored as `/connect/<kind>/*`. The listener's valid
  `X-Telegram-Token` skips the audit only on non-D routes; `X-Recall-Actor`/`X-Recall-Tool` are stored as claimed labels, never as the actor.
- Family (W) ceilings are counted where the tool name is trustworthy, in the MCP process; D ceilings are counted in
  app.py per actor class. Both are loop guards, not security controls.
- A conditional D route (`_CONDITIONAL_D_ROUTES`, re-transcribe that would evict a version) is gated only for a non-`ui` caller and only when its predicate holds; a predicate error gates (fail closed). 4.26.0: non-`ui` private-direction delete/merge and delete of a record with a live job are refused before any dialog (`PRIVATE_CATEGORY_CHANGE`, `JOB_ACTIVE`).
- 4.27.0 D actions added: `document_import_folder` and `document_reparse` (routes `documents.import_folder`, `reparse_document`, `reparse_job`; gated for every caller, `ui` included: a `ui` request still gets the dialog, only the recording refusal and the conditional routes (`approval_gate.py:187,213`) look at the class) and the conditional `video_describe_claude` (`transcription.analyze_video_endpoint`, non-`ui` caller whose request would describe frames through Claude; local analysis stays ungated). Their summaries are built from the DB and body (folder quoted and cut, record `display_name`); a refusal the view itself would give (bad folder, no original, no video) skips the dialog. `D_ACTIONS` now lists 18.
- The gate consumes `classify_request()`; a new origin proof is added there, never inferred in the gate.

## Boundary
The dialog is a confirmation, not authentication: an agent with desktop control can press it, and a same-user process
can read the DB directly. The `ui` class is a label against a local process (the token is readable from the shell
page), so the dialog's `ui` line also says to press "No" if the owner did not just act. See `docs/SECURITY.md`, section "Local boundary and the approval dialog".
