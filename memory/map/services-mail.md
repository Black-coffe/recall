# services — Gmail ingest (mail_*, document_ingest)

last-verified: 2026-10-16 (gmail-ingest, 4.18.0, schema v52)

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
- `add_account` (keyring first, row second; a DB failure restores the previous keyring value; existing address = password replaced),
  `delete_account` (account + label rows + keyring entry; archived emails/attachments stay), `list_accounts` (+`configured`),
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
- Inline images (`Content-ID`/`inline`) are flagged `Attachment.inline` and skipped by sync.

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
- `MailPoller(db_path)`: `start/stop/is_alive/trigger(account_id=None, months=None)/run_once`; interval read from `MAIL_POLL_MINUTES`
  each loop (no `interval_min` attribute); `MAIL_POLL_ENABLED=0` -> `run_once` still serves queued triggers but no periodic poll.

## document_ingest.py (moved out of `blueprints/documents.py`, shared)
`find_duplicate`, `persist_document`, `submit_enrichment`, `describe_tabular`, `structure_json`, `table_preview`, and
`ingest_file(db_path, path, *, original_name, category_id, enrich, describe_tables, text_filter, store) -> {status, transcription_id}`
(`created|duplicate|no_text|error`; never raises for a bad file). `text_filter` runs before hashing (dedup hash is of the masked text);
`store(path)` is called only for a new file (folder import copies into `documents/`). Callers: `documents.py` folder import, `mail_sync`.

## Gotchas
- `enrichment.enrich_transcription` returns `skipped`/`mail_enrich_off` when `mail_enrich == 0` and not `force`; backfill/list SQL use
  `mail_enrich IS NOT 0` (`enrichment.py:498, 594, 708`). Telegram's embed-only guard is separate.
- Credential store protects the password from files/DB/git/backups, not from other processes of the same Windows account (accepted risk).
- Tests: autouse in-memory keyring in `tests/conftest.py`; IMAP is always a fake via `factory=` or a patched `mail_imap.connect`.
