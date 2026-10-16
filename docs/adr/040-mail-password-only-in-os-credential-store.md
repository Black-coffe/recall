# ADR-040: Mail app passwords live only in the OS credential store (keyring)

- Status: proposed
- Date: 2026-10-16
- Spec: docs/specs/gmail-ingest

## Context
Brief, verbatim:

> A stored app password equals the key to the mailbox, for every public-mirror user too.
> The plan must state: where and how the password is stored (open question for the grill), that it never reaches
> logs, API responses, the MCP surface or backups in clear text, and that the UI tells the user how to revoke it
> (Google Account → Security → App passwords).

Owner's answer 1 chose "Хранилище системы": Windows Credential Manager (later macOS Keychain) via `keyring`; "Пароля нет
ни в базе, ни в .env, ни в бэкапе restic: в базе только адрес ящика. После восстановления на другом компьютере пароль
придётся ввести заново." (The password is in neither the database, nor .env, nor the restic backup: the database holds only the mailbox address. After a restore on another computer the password has to be entered again.)

Plan A18, verbatim:

> **A18 Threat model of the credential store** (side-agent note, 2026-10-16): Windows Credential Manager keeps the password
> out of files, the DB, `.env`, git, the public mirror and backups; it does NOT protect it from any process running under
> the same Windows account (including local agents). Accepted residual risk; mitigation is user-side: one app password per
> mailbox, revoke on suspicion. The mail screen says so (story 06).

## Options
None recorded beyond the chosen shape - the brief records the grill answer only.

## Decision
- The password is stored only via `keyring` under service `"Recall Gmail"`, username = normalised address.
  `mail_accounts.get_password()` is the only reader. No DB column, no `.env` or settings-registry entry (C9), no file.
- No dict returned by the account store, no API response, no MCP output carries the password; the
  `POST /api/mail/accounts` body is never logged (A17); IMAP errors are scrubbed of secrets (C3).
- Tests use an autouse in-memory keyring backend and never touch the real credential store (C10).

## Consequences
- Backups carry no password (A15); after a restore on another machine the password must be re-entered.
- Accepted residual risk: any process under the same OS account, including local agents, can read the password.
  Mitigation is user-side (one app password per mailbox, revoke on suspicion), and the Mail screen states it.
- Deleting an account removes its keyring entry (A5).

## Revisit when
Not recorded.
