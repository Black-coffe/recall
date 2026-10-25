# ADR-039: Gmail connects over IMAP with an app password, not the Gmail API/OAuth

- Status: proposed
- Date: 2026-10-16
- Spec: docs/specs/gmail-ingest

## Context
The owner wants "simple ways for anyone to connect 1, 2 or more Gmail accounts to their Recall". Brief, verbatim:

> Fact-check behind the connection choice (2026-10-16, Google T1 docs): `gmail.readonly` is a restricted scope
> (verification + security assessment to publish an OAuth app); OAuth apps in Testing status get refresh tokens that
> expire 7 days after consent; app passwords remain the exception to the 14.03.2025 password-login shutdown and need
> 2-Step Verification (not available for org accounts that disallow them, Advanced Protection, security-key-only 2SV).
> Rejected: Gmail API/OAuth (per-user Cloud project, weekly re-login). Connection is read-only.

<!-- verbatim:start - owner quote, kept in the original language with the English translation -->
Pre-grill answer: "IMAP + пароль приложения (Рекомендую)" (IMAP + app password (recommended)).
<!-- verbatim:end -->

## Options
1. Gmail API with OAuth (`gmail.readonly`): restricted scope, so publishing needs Google verification and a security
   assessment; unpublished (Testing) apps get refresh tokens that expire 7 days after consent, which means a per-user
   Cloud project and a weekly re-login.
2. IMAP with a Google app password (chosen).

## Decision
Mailboxes connect over IMAP (`imap.gmail.com:993`, a module constant; the code takes host/port parameters) with an app
password entered in the Mail screen. Only Gmail is offered in the UI (A13).

## Consequences
- App passwords need 2-Step Verification and are unavailable for org accounts that disallow them, Advanced Protection,
  and security-key-only 2SV (brief); those accounts cannot connect.
- An app password is not scoped: it grants full IMAP and SMTP access, so read-only behaviour must be enforced in code
  (ADR-041) and the secret must be stored carefully (ADR-040).
- Thread keys come from RFC headers, not Gmail `X-GM-THRID` (A8).

## Revisit when
Not recorded.
