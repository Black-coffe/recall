---
domain: scheduled-pipes
tags: [k4, telegram-bot, privacy, scheduler]
related: [video-timeline-and-screen-secrets.md, rag-context-integrity.md]
last-verified: 2026-10-15
---

# Scheduled pipes: what leaves the archive through the bot

A pipe is `pipes/<name>.md` (frontmatter + prompt). `app/services/pipes.py` runs it on a cron and sends the result through `app/services/tg_bot.py` to `TELEGRAM_BOT_CHAT_ID` only. The Bot API is not end-to-end encrypted, so the bot is a third-party channel and the privacy filter is mandatory.

Invariants:
- Data functions are a closed read-only registry (`pipes.TOOLS`). A pipe naming anything else does not load. No write tool and no arbitrary MCP tool is reachable.
- Order is fixed: data -> `pipes_privacy.filter_data` -> render (Claude or plain list) -> `scrub_text` -> send. Neither Claude nor the plain list ever sees a private item.
- Fail closed: private-category check failed (DB error) -> every item with a source is hidden; unreadable `private.txt` -> `blocked`, nothing sent. A stop-word surviving the render -> `blocked`; the word is never written to history (`pipe_runs` is readable via MCP `list_pipe_runs`).
- The stop-list and private directions are shared with the MCP filter (`app/services/privacy.py`, `RECALL_PRIVATE_CATEGORIES`, `private.txt`). Do not fork them.
- The bot address is not a parameter of `send()`; the only recipient is the configured owner chat.
- Silence is never ambiguous: a function that returns `reason` yields an explicit "not collected" line; `blocked` and failed runs send a short notice (no data, no stop-word).
- A failed run does not close its schedule moment (`error` or `running` older than 30 min). Retry every `PIPES_RETRY_MINUTES` up to `PIPES_RETRY_MAX`. A missed moment is caught up once at start. Manual `pipes run` does not close a moment.
- Claude cost is estimated before the call; over `budget_usd`, no API key or a failure -> plain list marked with `PLAIN_MARK`.

Verified in: `app/services/pipes.py` (`run_pipe`, `_next_attempt`), `app/services/pipes_privacy.py` (`filter_data`, `scrub_text`), `app/services/tg_bot.py` (`send`), `app.py:1250` (`_maybe_start_pipes`).
