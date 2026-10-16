---
domain: gmail-ingest
tags: [mail, imap, privacy, keyring, ingest-channel]
related: [video-timeline-and-screen-secrets.md, rag-context-integrity.md, scheduled-pipes-delivery.md, ui-language.md]
last-verified: 2026-10-16
---

# Gmail ingest: read-only channel, password and masking invariants

Mail is the third live ingest channel (next to recordings and Telegram). Code map: `memory/map/services-mail.md`. One record per email (`transcriptions.source_type='email'`, `mail_*` columns, schema v52); attachments are ordinary `document` records linked through `mail_attachments`.

Invariants:
- The channel is read-only toward Gmail: `ReadOnlyImap` exposes a fixed set of methods, mailboxes are opened with EXAMINE (`select(readonly=True)`), messages fetched with `BODY.PEEK[]` (`\Seen` untouched). There is no passthrough to the connection and no `smtplib`.
- The app password exists only in the OS credential store (`keyring`, service `Recall Gmail`, username = normalised address). It is never in the DB, `.env`, settings registry, logs, git, the mirror, backups or any dict `mail_accounts` returns; `mail_accounts.get_password` is its only reader. `POST /api/mail/accounts` never echoes or logs it; `MailError` text is scrubbed, and a failed login drops the server text entirely.
- Residual risk, accepted: Windows Credential Manager does not protect the password from other processes under the same Windows account. Mitigation is user-side (one app password per mailbox, revoke on suspicion); the Mail screen says so.
- Nothing is ingested until the owner enables a label (`mail_labels.enabled`, new rows default 0). Disabling a label stops future sync only; deleting an account removes account, label rows and keyring entry, while ingested emails and attachments stay.
- Masking before any write: body, subject (and so title/`source_name`) and attachment text go through `mail_parse.mask` (`screen_secrets` + mail phrasings: labelled passwords/PINs/tokens, one-time codes, Bearer/Basic). Text attachments are masked before the file hits disk; binary attachment files are kept as received and only their parsed text is masked. The pipes mask (IBAN/phones/amounts) is NOT applied to mail.
- Claude is off by default: `mail_enrich` (set from the label's `enrich` toggle, also on attachment documents) is honoured by `enrichment.py` (`mail_enrich == 0` -> skipped `mail_enrich_off`; backfill and list queries use `mail_enrich IS NOT 0`). Emails embed only (job `mail_embed`).
- A failed message never advances the label watermark past itself: `last_uid` stays below the smallest failed UID so it is refetched.
- Dedup is `(mail_account_id, mail_message_id)` (unique partial index); one email under two enabled labels is one record owned by the label that saw it first. Attachments dedup globally by text hash; the same file in two emails is one document with two links.
- MCP stays read-only: `email_status` is a proxy of `GET /api/mail/status` returning whitelisted keys only; email records carry an `email` block in `get_transcript`/`list_recent`/`search_history`.
