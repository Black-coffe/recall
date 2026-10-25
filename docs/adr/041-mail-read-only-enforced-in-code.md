# ADR-041: Mailbox read-only access is enforced in Recall's code

- Status: proposed
- Date: 2026-10-16
- Spec: docs/specs/gmail-ingest

## Context
Brief, verbatim:

> An app password is not scoped: with it IMAP can read, delete and move mail in the whole mailbox, and SMTP can send
> mail. "Read-only" is a promise of Recall's code (EXAMINE / BODY.PEEK, no STORE/EXPUNGE/APPEND/COPY/MOVE, no SMTP),
> not a Google restriction.

<!-- verbatim:start - owner quote, kept in the original language with the English translation -->
Ask 2: "Recall только читает ящик: ничего не удаляет, не перемещает, не помечает прочитанным и не отправляет." (Recall only reads the mailbox: it deletes nothing, moves nothing, marks nothing as read and sends nothing.)
<!-- verbatim:end -->

## Options
None recorded - the brief states the chosen shape only (it follows from ADR-039: no scoped credential exists).

## Decision
All IMAP access goes through the `ReadOnlyImap` wrapper in `app/services/mail_imap.py` (stdlib only, no `smtplib`).
Folders are opened with EXAMINE and messages fetched with `UID FETCH ... BODY.PEEK[]`. No method or passthrough sends
STORE, EXPUNGE, APPEND, COPY, MOVE, CREATE, DELETE, RENAME, SUBSCRIBE, or a plain SELECT. Forbidden for future code: any
mail write command, any SMTP use, and any way around the wrapper.

## Consequences
Read-only status is only as strong as this wrapper; Google enforces nothing. Recall never marks mail as read.

## Revisit when
Not recorded.
