---
domain: scheduled-pipes
tags: [k4, k5, telegram-bot, privacy, scheduler, brief-cards]
related: [video-timeline-and-screen-secrets.md, rag-context-integrity.md]
last-verified: 2026-10-25
---

# Scheduled pipes: what leaves the archive through the bot

A pipe is `pipes/<name>.md` (frontmatter + prompt). `app/services/pipes.py` runs it on a cron and sends the result through `app/services/tg_bot.py` to `TELEGRAM_BOT_CHAT_ID` only. The Bot API is not end-to-end encrypted, so the bot is a third-party channel and the privacy filter is mandatory.

Brief format (K5, 4.14.0): the message is composed by code in `app/services/pipes_cards.py`, not by the model. It is HTML (`parse_mode=HTML`, tags `b`, `i`, `a` only). Tasks are grouped into blocks by root direction (top of the `categories.parent_id` tree), at most 5 cards per block and a "N more →" link to `/tasks?category_id=` for the rest. A card is what, who, due, source, linked to `/transcript/<id>?task=<task id>` (the page scrolls to and highlights the task). Claude writes only 1-2 plain lines of "headline" (`clean_headline` strips markdown). Links use `RECALL_PUBLIC_URL` (phone-reachable, e.g. a `tailscale serve` address), falling back to `RECALL_API_URL`. Blocks are separated by blank lines and `tg_bot.split_text` splits only there, so no part breaks a tag.

Invariants:
- Data functions are a closed read-only registry (`pipes.TOOLS`). A pipe naming anything else does not load. No write tool and no arbitrary MCP tool is reachable.
- Order is fixed: data -> `pipes_privacy.filter_data` -> headline (Claude, optional) -> `pipes_cards.render` -> stop-word re-check on the unescaped text -> send. Neither Claude nor the cards ever see a private item.
- Every card field is masked (`pipes_privacy.mask`: IBAN, email, cards, phones, amounts with currency -> `•••`) and checked against stop-words first, then truncated, then HTML-escaped. Truncation cannot split a phone number; escaping cannot hide a stop-word.
- Fail closed: private-category check failed (DB error) -> every item with a source is hidden; unreadable `private.txt` -> `blocked`, nothing sent. A stop-word surviving the render -> `blocked`; the word is never written to history (`pipe_runs` is readable via MCP `list_pipe_runs`).
- The stop-list and private directions are shared with the MCP filter (`app/services/privacy.py`, `RECALL_PRIVATE_CATEGORIES`, `private.txt`). Do not fork them.
- The bot address is not a parameter of `send()`; the only recipient is the configured owner chat.
- Silence is never ambiguous: a function that returns `reason` yields an explicit "not collected" line; `blocked` and failed runs send a short notice (no data, no stop-word).
- A failed run does not close its schedule moment (`error` or `running` older than 30 min). Retry every `PIPES_RETRY_MINUTES` up to `PIPES_RETRY_MAX`. A missed moment is caught up once at start. Manual `pipes run` does not close a moment.
- Claude cost is estimated before the call; over `budget_usd`, no API key or a failure -> the same cards without the headline (`rendered_by='plain'`).
- Opening the links from a phone goes through `tailscale serve`, which presents the request as 127.0.0.1. `app/core/auth.py` treats a localhost request carrying `X-Forwarded-For`, `Tailscale-User-Login`, `Forwarded` or `X-Real-IP` (4.20.0 added the last two) as proxied: it passes only if the single XFF address is in `RECALL_PROXY_ALLOWED_IPS` or a valid `RECALL_API_KEY` is sent; otherwise 401.

Task buttons (K5, 4.15.0): tasks in a brief are numbered and the message carries an inline keyboard (done / cancelled / postpone 1, 7, 30 days, undo). Presses arrive by `getUpdates` long-polling (`brief_actions.ButtonPoller`, `callback_query` only), not a webhook: Recall has no public address. A press made while the PC sleeps waits up to 24 h at Telegram and applies on wake; the brief footer says so.
- A press is accepted only from `TELEGRAM_BOT_CHAT_ID`; any other chat gets "No access" and changes nothing.
- Status and due date change only through `commitments.apply_action` / `undo_action` (bot and web `PATCH /api/memory/action-items/<id>`), which write the change and its `action_item_events` row in one transaction. Undo reverts only the latest event of a task. The MCP `task` tool (4.26.0) goes through the same route and is stamped `source='mcp'`; a pipe itself never writes (the registry stays read-only).
- Daily MCP summary (4.31.0, `pipes/mcp-actions.md`, tool `mcp_actions`, no model): lists what non-UI callers did in 24 h. A line gets an undo button (`m:<n>:t|c:<ref>`) only while its event is the last for that task, or for that chat and kind; offered inverses are task actions, chat direction changes and chat disables only. A chat undo goes through `telegram_chats.undo_event(source='bot')` and is refused when a later change exists (including one made in the SPA without a journal row), when the move would leave a private direction, or when the event created the chat. Items carrying a private `category_id` are hidden like any private item.
- A pipe can also be run on demand (`POST /api/pipes/<name>/run`, MCP `pipes`): same recipient, same filters, one run per 10 min per pipe, not moving the schedule; `trigger='mcp'`.
- `tg_bot.whoami` sets `allowed_updates` explicitly because Telegram remembers the poller's `callback_query`-only filter.

Verified in: `app/services/brief_actions.py` (`handle`, `ButtonPoller`), `app/services/commitments.py:1395-1463`, `app.py:1274-1281`, `app/services/pipes.py` (`run_pipe`, `render_claude`, `_next_attempt`), `app/services/pipes_cards.py` (`render`, `_Guard`, `link_base`), `app/services/pipes_privacy.py` (`filter_data`, `mask`, `leaked_words`), `app/services/tg_bot.py` (`send`, `split_text`), `app/core/auth.py:100-147` (`_proxied_client`, `_is_authorized`), `app.py:1250` (`_maybe_start_pipes`).
