# services — Gmail ingest (mail_*, document_ingest)

last-verified: 2026-10-26 (connect_links kind `calendar`, 4.34.0); 2026-10-19 (gmail-ingest 4.18.0, mcp-mail-connect 4.19.0, 4.24.0)

Part of `app/services/*`; hub — [services.md](services.md). Sibling live channel of [services-telegram.md](services-telegram.md),
but an **in-process thread** (imaplib needs no own loop), not a Popen child. HTTP — `blueprints.md` § mail.py; UI — `frontend.md`.
Plan with contracts: `docs/specs/gmail-ingest/plan.md`; privacy rules — `docs/wiki/gmail-ingest-privacy.md`.

## Flow
`MailPoller` thread → `sync_all` → `sync_account` (one IMAP connection per account) → `_sync_label` → `_fetch_store` →
`_store_email` → `parse_message` → `_insert_email` (`source_type='email'`) → `_ingest_attachments` → `_submit_embed` (+ enrichment only
if `label.enrich`). Read-only toward Gmail; nothing is ever written back.

## mail_accounts.py (no flask/torch; `keyring` + sqlite3)
- `KEYRING_SERVICE="Recall Gmail"`, username = `normalize_address()` (strip+lower, exactly one `@`, else `ValueError`).
- `get_password(address)` is the ONLY reader of the password; `has_password`. No dict returned by the module carries it.
- `connect_account(db_path, address, password) -> {account, labels}` (4.19.0, shared by `POST /api/mail/accounts` and the connect page):
  normalise address (`ValueError("INVALID_ADDRESS")`), strip all whitespace from the password (`EMPTY_PASSWORD`), `mail_imap.connect` +
  `list_folders` FIRST, then `add_account` + `upsert_folders`; nothing is stored when login/LIST fails (`MailError` propagates).
- `add_account` (keyring first, row second; a DB failure restores the previous keyring value; existing address = password replaced),
  `delete_account` (hard: account + label rows + keyring entry; archived emails/attachments stay), `disconnect_account(db, id) -> list[int]|None`
  (4.24.0, soft: drops the keyring entry, sets `enabled=0` on all labels, keeps account row, cursors, directions, records; returns the ids
  that were enabled, None = no account; `add_account` reuses the row by address so a reconnect resumes, labels stay off), `list_accounts` (+`configured`),
  `upsert_folders` (new rows `enabled=0 enrich=0`; existing rows keep settings, only `display_name`/`system` refresh),
  `list_labels`, `update_label(..., enabled|category_id|enrich=UNSET)`, `enabled_labels` (+`address`),
  `record_label_sync(**fields)` (whitelist `_SYNC_FIELDS`, else `ValueError`), `record_account_poll`, `status()` (per-label `records` count).

## mail_imap.py (stdlib only)
- `connect(address, password, *, host=GMAIL_HOST, port=993, timeout=30, factory=None) -> ReadOnlyImap`; callers use
  `mail_imap.connect` by attribute (tests monkeypatch it). Login failure drops the server text: `MailError(AUTH_FAILED, "login rejected")`.
- `MailError.code` in `AUTH_FAILED|CONNECT_FAILED|FOLDER_MISSING|PROTOCOL`; message is `scrub()`-ed of the password.
- `ReadOnlyImap`: `list_folders` (skips `\Noselect`; `system` for `\All \Trash \Junk \Drafts \Important \Flagged`), `examine`
  (`select(readonly=True)` -> `{uidvalidity, exists}`), `search_since(since, before=None)`, `search_after_uid`, `fetch_raw` (`BODY.PEEK[]`,
  batch 50), `close`. No passthrough to the connection. `encode_mutf7/decode_mutf7` for folder names.

## mail_parse.py (stdlib + `screen_secrets`; no DB/network)
- `parse_message(raw) -> ParsedEmail`: `thread_key` = first `References` id, else `In-Reply-To`, else own id; no `Message-ID` ->
  `<sha1(raw)@recall.local>`; `meeting_date` = local day of `Date`; body = text/plain, HTML only as fallback (`html_to_text`).
- `strip_quotes` drops `>` lines, "On ... wrote:" / Cyrillic attribution / Outlook headers and all after them, `gmail_quote`/`blockquote`
  in HTML; a forward marker (`_FORWARD`) keeps everything from it on. `mask(text)` = mail phrasing regexes (labels, next-line values,
  one-time codes, Bearer/Basic) + `screen_secrets.scan_text/mask_text`; applied to body AND subject (`ParsedEmail.subject` is already masked).
- Inline images are flagged `Attachment.inline` and skipped by sync.

## mail_sync.py
- `sync_account(db_path, account_id, months=None, today=None)` -> `{account_id,new,labels,errors}`; `sync_all`. No password ->
  `last_error_code="NO_PASSWORD"`. Label failures store the `MailError.code` (non-mail exceptions -> `PROTOCOL`).
- `_sync_label`: first sync = `SINCE today - MAIL_BACKFILL_MONTHS`; `months=N` on a synced label fetches only the gap below
  `synced_since` (`search_since(since, current)`); UIDVALIDITY change re-reads from `synced_since`; else `UID last_uid+1:*`.
- Watermark (`_watermark`) stays BELOW the smallest failed UID, so a failed message is retried next poll.
- Dedup: `_find_email` + unique partial index `idx_tx_mail_msg`; one email under two labels = one row (first label's `category_id`/`mail_folder`).
- Attachments: `documents/mail/<account_id>/` (`_docs_dir` confined), > `MAIL_ATTACH_MAX_MB` skipped, text attachments are masked BEFORE
  the file write (`_masked_bytes`), binary ones stay as-is on disk (only parsed text masked via `text_filter=mail_parse.mask`);
  link table `mail_attachments(email_tid, document_tid)`; unused file removed unless `status=="created"`.
- `mail_enrich` on the email and on newly created attachment documents = label's `enrich`; embed job kind `mail_embed`.
- `first_months_cap` (4.19.0) threads `sync_all`/`sync_account`/`_sync_label`: on a FIRST sync the backfill is `min(months or
  MAIL_BACKFILL_MONTHS, cap)`; synced labels ignore it. Used for MCP-enabled labels (cap 12); the poller queues `(account_id, months, cap)`.
- `MailPoller(db_path)`: `start/stop/is_alive/trigger(account_id=None, months=None, first_months_cap=None)/run_once`; interval read from `MAIL_POLL_MINUTES`
  each loop (no `interval_min` attribute); `MAIL_POLL_ENABLED=0` -> `run_once` still serves queued triggers but no periodic poll.

## document_ingest.py (moved out of `blueprints/documents.py`, shared)
`find_duplicate`, `persist_document`, `submit_enrichment`, `describe_tabular`, `structure_json`, `table_preview`, and
`ingest_file(db_path, path, *, original_name, category_id, enrich, describe_tables, text_filter, store) -> {status, transcription_id}`
(`created|duplicate|no_text|error`; never raises). `text_filter` runs before hashing; `store(path)` only for a new file. Callers: `documents.py`, `mail_sync`.

## Connect links and secret guards (mcp-mail-connect, 4.19.0; stdlib/in-memory, wiki `gmail-ingest-privacy.md`)
- **connect_links.py** — in-memory one-time links, never persisted (a restart kills them). `KINDS=("mail","anthropic_key","calendar")` (4.34.0; subject = calendar label, `services-calendar.md`),
  `TTL_SECONDS=600`, `MAX_FAILURES=3`. `issue(kind, subject=, requested_by=)` (drops older links of the same kind+subject; token
  `secrets.token_urlsafe(32)`), `peek` (never consumes), `consume`, `record_failure` -> attempts left (0 = link deleted), `url_for`
  (`http://127.0.0.1:<port>/connect/<kind>/<token>`), `port_from_host`. Tests patch `_clock`.
- **secret_shape.py** — `looks_like_secret(text, *, strict=True)` (grouped `xxxx xxxx xxxx xxxx`, `sk-ant-...`, and with `strict` any
  standalone 16-letter token not followed by `@`) and `find_secret_args(args, *, strict=True)` -> argument NAMES only (nested strings
  walked). `mcp_server._SecretGuardMiddleware` passes `strict=False` for R tools (4.20.0), strict for W/D and unregistered ones;
  `POST /api/mail/connect-link` keeps the default. It is a heuristic: a 16-letter word in a write argument is refused too.
- **settings_secrets.py** — `save_anthropic_key(key)` (format `^sk-ant-[A-Za-z0-9_-]{8,300}$`, `.env` via `env_file.set_env_var` (locked, also `os.environ`), drops
  `text_polishing._client`; `ValueError(<i18n key>)` with `ERR_EMPTY|ERR_FORMAT|ERR_ENV_WRITE`), `anthropic_key_configured()`. Shared by
  `settings_api` and the connect page; the key is never logged (length only).
- **privacy.category_change_exposes(db, old_cid, new_cid)** — True when a move leaves a private direction for an open one/none; DB
  failure -> `PrivacyError`. Called by `mail.py` PATCH for MCP callers only.

## Gotchas
- `enrichment.enrich_transcription` returns `skipped`/`mail_enrich_off` when `mail_enrich == 0` and not `force`; backfill/list SQL use
  `mail_enrich IS NOT 0` (`enrichment.py:498, 594, 708`). Telegram's embed-only guard is separate.
- Credential store does not protect the password from other processes of the same Windows account (accepted risk).
