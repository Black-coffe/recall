# services — scheduled pipes and the Telegram bot

last-verified: 2026-10-25 (mcp-telegram-automation 4.31.0: `mcp_actions` tool/pipe, `m:` undo buttons, on-demand run); 2026-10-18 (4.20.0: `.env` load path)

Part of `app/services/*`; hub — [services.md](services.md). K4 4.13.0, K5 4.14.0/4.15.0.
`pipes.py` (:636) and `tg_bot.py` (:190) call `load_dotenv(ENV_PATH)` with `app.core.env_file.ENV_PATH` (repo root), not a cwd search.
**Language (english-i18n, 4.16.0):** the service strings of the brief and the buttons take their language from the setting, not from the request:
`tr(..., lang=server_lang())` (`en` only with an explicit `RECALL_UI_LANG=en`, `auto` → uk; `app/core/i18n.py:37`). Keys —
`telegram.brief.*` (`pipes.py:336-553`, `pipes_cards.py:43`, plural `tr_n("telegram.brief.hidden")` :350) and
`telegram.btn.*` (`brief_actions._t` :41). Content from Claude ("headline"), directions, entities stay uk. `brief_actions.FOOTER`
is no longer a constant: the module-level `__getattr__` (:50) returns `footer()` at the moment of reading; `_DONE_LABEL` is replaced by
`_done_label(action, lang)` (:56); `handle` fixes the language at the moment of the press (:96). `tg_bot.py` and the CLI are not translated.
Pipe files — `pipes/*.md` (`weekly-digest` Mon 9:00, `dropped-commitments` Mon 9:00, `open-questions` Mon-Fri 9:00):
frontmatter `schedule|tools|channel|budget_usd|skip_if_empty|max_tokens` + a prompt (the model writes only 1–2 "headline" lines;
the list/groups/links are assembled by code). History — `pipe_runs` (v50, `migrations.py:2085`).

- **pipes.py** — `Cron.parse` (its own 5-field one, no croniter; local time; dom+dow both restricted → OR). `TOOLS` —
  a CLOSED registry of read functions (`weekly_digest|dropped_commitments|open_questions|mcp_actions`; any other key → `PipeError`).
  4.31.0 `mcp_actions_summary(db, now=)` (`:181`): last 24 h (`MCP_WINDOW_H`) of `mcp_actions` with `actor != 'ui'` grouped by tool/action + `refused`
  (decision != approved) + `items` = undoable events only while LAST for their task / chat+kind: task actions (`action_item_events`, `source='mcp'`),
  chat direction changes and chat disables (`tg_chat_events`, `source='mcp'`; enable is not offered). Each item carries `category_id` + `undo {kind task|chat, ref}`.
  No table -> `reason`. Pipe file `pipes/mcp-actions.md` (daily 09:00, `tools: [mcp_actions]`, `budget_usd: 0`, `skip_if_empty`: code-built, no model).
  On demand: `POST /api/pipes/<name>/run` (`blueprints.md` § automation.py) runs `run_pipe(..., buttons=True, trigger='mcp')` as a `pipe_run` job.
  `parse_pipe`/`load_pipes` (an invalid one — skipped + warning). `run_pipe(pipe, db_path, deliver, …, buttons=False)` (`:414`)
  never throws: data → `pipes_privacy.filter_data` → `render_claude` (only the "headline", `HEADLINE_MAX_TOKENS=300`;
  the cost estimate BEFORE the call > `budget_usd`, no `ANTHROPIC_API_KEY` or a failure → `_PlainFallback`, cards without a "headline",
  `rendered_by='plain'`) → `pipes_cards.render` → a second check `leaked_words(html.unescape(text))` → `deliver`.
  Statuses `ok|skipped|blocked|error|running`. `not_collected` (a function with a `reason`, e.g. `open_questions` without an owner) —
  an explicit "not collected"; `skip_if_empty` does not fire if something is hidden or not collected. With `buttons`, `item_ids` are collected,
  `deliver(text, reply_markup=brief_actions.keyboard(ids))` (no buttons → `deliver(text)`); `deliver` must accept
  `reply_markup` (`tg_bot.send`). A failure BEFORE delivery → a short message to the bot (1st and last attempt); `blocked` →
  a message without data.
  `PipeScheduler(tick=30, buttons=False)` (`:547`): the first pass is `catchup` (one run for the latest moment ≤ now), then
  `schedule`; `_next_attempt` — `error`/a stuck `running` (`STALE_RUNNING`=30 min) do NOT close the moment, retry after
  `PIPES_RETRY_MINUTES` up to `PIPES_RETRY_MAX`; `manual` does not close the moment. CLI `python -m app.services.pipes list|run <name>`
  (run → stdout, `trigger='manual'`). `list_runs` is read by MCP.
- **pipes_cards.py** — the message is assembled by CODE; HTML (`parse_mode=HTML`) only `b|i|a`. `render(title, data, *, db_path,
  today, base_url, headline, missing, hidden, words, buttons)` (`:297`) → `(html, leaked_words)`; a non-empty `leaked` = do not send.
  Tasks in blocks by ROOT direction (`_Dirs`, without a direction → `_t("noDir")` = `telegram.brief.noDir`), ≤`MAX_CARDS=5` per block, the rest →
  `ще N →` to `{base}/tasks?category_id=`. A card: what · who · due · source; link `/transcript/<tid>?task=<id>`.
  Sections: 📌 tasks (`overdue|this_week|no_date`), 🕳 dropped, ❓ questions, 💤 topics, ✏️ corrections, coverage.
  `_Guard` on EVERY field: mask (`pipes_privacy.mask`) → stop words → truncation → `html.escape`. `clean_headline` (≤2 lines).
  `link_base()` = `RECALL_PUBLIC_URL` → `RECALL_API_URL` → `127.0.0.1:5050`. Check — `scripts/check_brief_links.py`.
  With `buttons`, tasks and "dropped" are numbered `<b>N.</b>` (`_numbered`, ≤`brief_actions.MAX_ITEMS=33`), the `item_id`s are appended
  to `buttons` by number; a non-empty `buttons` → at the bottom in italics `brief_actions.footer()` (in the setting's language).
- **pipes_privacy.py** — a layer over `privacy.py`. `filter_data(data, *, db_path)` BEFORE rendering: an item with a stop word, from a private
  direction or with an undetermined source → dropped (fail closed; an unreadable `private.txt` → `PrivacyError` → `blocked`).
  `mask(text)` (IBAN, email, cards, phones, amounts with currency → `•••`), `leaked_words(text, words=None)`: a stop word that
  4.31.0: an item carrying its own `category_id` (the MCP summary) is hidden when that direction is private or the check failed (`:138-142`).
  A stop word that survived assembly → `blocked` (the word is not written even to history). `hidden_line(n)`. `scrub_text` is defined but
  `run_pipe` does not call it.
- **brief_actions.py** — the buttons "done / not relevant / postpone" under the brief. `keyboard(item_ids)` →
  `{inline_keyboard}` (a row per task; `None` if empty). `callback_data` (≤64 B, stateless):
  `a:<n>:<id>:<done|cancelled|postpone:1|7|30>`, `p|b:<n>:<id>` (deadline/back), `u:<n>:<event>` (undo), `x`; 4.31.0 `m:<n>:t|c:<ref>` (undo a task event via `commitments.undo_action` / a chat event via
  `telegram_chats.undo_event`, `source='bot'`; `keyboard(item_ids, undo_items=)` appends one `undo_row` per MCP-summary item; refusals `undo.notLast|private|already|created|notFound`
  -> callback text). `pipes_cards._mcp` renders the block (`telegram.brief.mcp.*`), numbers continue after the task numbers.
  `handle(cq, *, db_path, transport)` (`:75`): chat ≠ `tg_bot._chat_id()` → `_t("noAccess", lang)` (`telegram.btn.noAccess`); the change ONLY via
  `commitments.apply_action/undo_action(source='bot', actor='tg:<id> @user')`; `LookupError|ValueError` → text in
  `answerCallbackQuery`. The row is looked up by NUMBER (`_row_number`), the base is the last keyboard in `_KEYBOARDS` (LRU 200;
  presses after the PC sleeps arrive in a batch), otherwise `msg.reply_markup`.
  `ButtonPoller(db_path, transport, backoff=10)` (`:176`) — daemon thread `brief-buttons`: `getUpdates` long-poll
  (`POLL_TIMEOUT=15` < `tg_bot.TIMEOUT=20`), `allowed_updates=["callback_query"]`, `offset` in memory; failure → pause.
  No webhook: a press made while the PC was asleep waits in Telegram ≤24 h. Started by `app.py`.
- **tg_bot.py** — Bot API (a separate bot, NOT the MTProto listener `telegram_listener.py`) on `urllib`. `send(text, *,
  parse_mode="HTML", reply_markup=None)` sends ONLY to `TELEGRAM_BOT_CHAT_ID` (the recipient is not a parameter); `reply_markup` — only on the
  LAST part; `split_text` cuts at 4096 on blank lines. `_call(method, params, transport=)` is also used by `brief_actions`.
  `whoami()` explicitly sets `allowed_updates=[message,my_chat_member,callback_query]` (Telegram remembers the button listener's filter).
  `BotError` without the token/URL; `is_configured()`; CLI `python -m app.services.tg_bot whoami`. `transport=` is replaced in tests.
