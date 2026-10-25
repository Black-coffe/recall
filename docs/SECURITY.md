# Recall Data Security

> Recall is a **local** call archive: transcripts, action items and the entity graph
> (names of people/organizations) are stored on the user's machine. External calls are
> only optional, to the Anthropic API (polish/enrichment/copilot) and Google Drive (backup).
> This document describes the current security model and the user's responsibility.

## 1. Network access (implemented)

- **Bind to `127.0.0.1` by default** (Wave 0, T1.1) — the server listens on the local
  interface only. Network exposure is enabled deliberately (`RECALL_BIND_ALL=1` or
  `FLASK_HOST=0.0.0.0`) and then requires a safe `SECRET_KEY` (otherwise hard-fail).
- **Auth gate on `/api/*`** (Wave 1, T1.2): local requests (`127.0.0.1`/`::1`) with
  `RECALL_LOCAL_TRUSTED=1` pass; remote ones require `RECALL_API_KEY`
  (`X-Recall-Api-Key` or `Authorization: Bearer`). Fail-closed if no key is set.
- **Import-folder allowlist** (Wave 1, T1.3): reading files from disk is restricted to
  `RECALL_IMPORT_ROOTS` (deny-by-default) — no reading of arbitrary paths from the LAN.

## 2. Encryption at rest

### Current state (a deliberate trade-off)

| Data | State on disk |
|---|---|
| `whisper_history.db` (SQLite: transcripts, entities, action items) | **Plaintext** |
| Audio/video files (`downloads/`, recordings, ingest) | **Plaintext** |
| Weekly backup (restic → Google Drive) | **Encrypted** (restic AES) |

**That is, only the backup is encrypted, while the "live" DB and media are not.** This is a conscious decision:
Recall is desktop-like local software for a single-user machine, and encrypting the "live"
DB (SQLCipher) would affect access for all three processes (`app.py`, `telegram_listener.py`,
`mcp_server.py`), the connection scheme and the backup script. Instead, **protection of data at
rest relies on the OS full-disk encryption** (see below).

### 🔴 Recommendation: enable BitLocker (mandatory for sensitive data)

Because the DB and media are stored in plain form, **theft/loss of a laptop or
access by another user of the same machine = a full leak of all conversations**. To close this,
enable full-disk encryption:

**Windows (BitLocker):**
1. `Settings` → `Privacy & security` → `Device encryption`; or
   `Control Panel` → `BitLocker Drive Encryption` → `Turn on BitLocker` for the drive
   that holds the project and `whisper_history.db`.
2. Store the recovery key in a safe place (Microsoft account / printed copy /
   USB — **not** on the same drive).
3. Wait for the encryption to finish; make sure the status is "On".

**Alternatives:** VeraCrypt (cross-platform), FileVault (macOS), LUKS (Linux) —
if the project is run on something other than Windows.

> **User responsibility.** Recall does not enforce or verify full-disk
> encryption. Protecting `whisper_history.db` and media at rest is up to the
> user/administrator of the machine. For NDA-sensitive data, treat BitLocker (or an equivalent)
> as mandatory.

### When Option A is needed (SQLCipher, encrypting the DB itself)

Full-disk encryption protects against physical theft, but **not** against other processes/
users in an already unlocked session. If an enterprise client appears with a
compliance requirement (application-level encryption, not just disk-level), it would make sense to
move to **SQLCipher** (Option A from `REMEDIATION_PLAN` T1.6). This is an L-sized task: it affects
the connections of all three processes, the backup script and dependency pinning; start it only on an explicit
owner decision and a real need. Until there is such a requirement, Option B (this
document + BitLocker) is the accepted state.

## 3. External calls and privacy

- **Anthropic API** — only when `ANTHROPIC_API_KEY` is set (polish/enrichment/copilot
  escalation). The key is stored in `.env` (gitignored), is not logged (length only),
  and is not returned to the client. Call data is sent to Anthropic only for explicitly
  requested processing operations.
- **Google Drive** — only for the weekly encrypted restic backup, as configured by
  the user.
- **Telegram (MTProto)** — listening to a real account; `telegram.session` is a sensitive
  file (access to the account), keep it local, do not commit it (gitignored).
- **`.env`** is never committed (gitignored). It contains `SECRET_KEY`,
  `ANTHROPIC_API_KEY`, Telegram keys — all sensitive.

## 4. Diagnostics without leaks

When assembling a diagnostic bundle (T8.1) / logs, transcripts, PII and keys are not
included (redaction). The processes' file logs (`whisper_ui.log`,
`telegram_listener.log`, `mcp_server.log`) may contain operation metadata but not the
full content of calls; review them before sending to the developer.

## 5. Local boundary and the approval dialog

Every destructive route (the D actions of ADR-042: deleting records, categories, tags, speakers, audio files, mail
accounts and the stored Anthropic key, plus merges and backfills) needs a native Windows approval dialog raised by
`app.py`, whoever sends the request: the UI, an MCP tool, a script or `curl`. No key, token, environment variable,
profile or mode skips the dialog; any failure to show it is a denial.

What this does and does not protect:

- The dialog is a confirmation, not authentication. An agent that controls the desktop (screenshots, mouse, keyboard)
  can press "Yes". Keep such an agent away from sessions where Recall runs, or accept that risk.
- The `ui` actor class is a label, not proof, against a local process: the per-boot UI token is readable from the
  shell page, so a process that can `GET /` can present itself as the UI and also skip the auto-refusal during a
  recording. The dialog remains the only decision, and its `ui` line also asks you to press "No" if you did not just
  do this.
- A host without an interactive desktop (service session, no visible window station) cannot delete at all, the UI
  included: the answer is `APPROVAL_UNAVAILABLE`.
- A process running as the same OS user can read `whisper_history.db` and the media files directly and bypass Recall
  entirely. Recall's boundary is the local user account, not a process sandbox.
- Ceilings (10 changes per family per 10 minutes in each MCP process; 3 deletions per minute and 20 per day per actor
  class in `app.py`) are loop guards against a runaway agent, not security controls.
- Every changing request, the `/connect/*` key and mailbox saves included (token redacted from the path), is recorded
  in the audit (`mcp_actions`, kept 90 days, no request bodies); read it with the MCP
  tool `list_mcp_actions`. The `claimed` actor and tool labels are what the caller wrote about itself and are not
  trusted.

See [ADR-042](adr/042-mcp-full-control-supersedes-read-first.md) and the approval gate note `docs/wiki/approval-gate.md`.
