# services — Telegram: threads, dossiers, tasks, questions

last-verified: 2026-10-28 (task-approval 4.35.0: tg_tasks writer); 2026-10-25 (mcp-telegram-automation 4.31.0: telegram_chats.py)

Part of `app/services/*`; hub — [services.md](services.md). Waves 4.5–5.3. Bot/pipes — [services-pipes-bot.md](services-pipes-bot.md);
entity graph from TG — [services-entities.md](services-entities.md). Sibling channel Gmail (in-process poller, not a child process) —
[services-mail.md](services-mail.md).

- **tg_threads.py** — the unit of meaning = **a thread inside a chat**. `segment_bursts()` (`:160`; pause `TG_THREAD_BURST_GAP_MIN`=180
  min), `split_batch()` (the local model splits a burst into topics, may continue an open thread; batch
  `TG_THREAD_BATCH_MSGS`=25/`_CHARS`=6000, candidates `TG_THREAD_CANDIDATES`=5), `assign_incoming()` (live ingest WITHOUT a
  model, `tg_thread_src='pending'`), `resettle_pending()`, `close_idle_threads()`, `backfill`, `thread_messages`, `stats()`.
  CLI: `backfill --dry-run`, `resettle`, `close-idle`, `stats`.
  *The e5 cosine only ranks candidates, there is deliberately no absolute threshold in the code (reply pairs 0.828 vs random 0.827).
  The decision is made by the local model `TG_THREAD_MODEL` (separate from `LOCAL_LLM_MODEL`), per burst, not per message. No Ollama →
  the burst stays one thread, visible in `tg_thread_src` (`burst`/`orphan` = degradation). Sticking together is restrained by the candidate WINDOW
  `TG_THREAD_IDLE_DAYS`=3 and the soft ceiling `TG_THREAD_MAX_MSGS`=30 (forbids a NEW conversation, does not break the current one).*
- **tg_chat_context.py** (4.5.2) — a chat dossier ON TOP of threads: `participants()` (numbers from SQL), `open_threads()`,
  `refresh_chat()` (the model adds roles/digest/questions/decisions), `refresh_all`, `needs_refresh()` (`TG_CONTEXT_MIN_NEW`=30
  new messages OR `TG_CONTEXT_MAX_AGE_H`=24 h), `get_context()`. CLI `refresh --dry-run|--force`, `show --chat`.
  MCP `get_chat_context`. *The model does NOT rewrite participant numbers; references to threads that were not shown are dropped; no
  Ollama → the hard part remains (`summary=None`, `model=None`). No torch — safe from the stdio-MCP.*
- **telegram_chats.py** (4.31.0, no Flask import) — changes of a monitored chat's direction / enabled flag, each journaled in `tg_chat_events`
  (migration v56, `migrations.py:2247`: `at, chat_id, kind category|enabled, old_value, new_value` as text — `''` = no direction, `'0'|'1'`
  enabled — `source mcp|ui|bot`, `actor`, `undone_at`, `undone_by_event_id`; index `(chat_id, kind, id)`). `get_chats(db, category_id=)` (+`tags`),
  `set_category` (refuses a move out of a private direction: `PrivateCategoryChange`; `PrivacyError` propagates), `set_enabled`,
  `enable_chat` (creates the row from the listener's `dialogs`, none -> `ListenerOffline`; history NOT loaded), `undo_event` (`UndoNotLast` when a
  later event of the same chat+kind exists OR the chat no longer holds the event's `new_value`; `ValueError('undone'|'created')`; an enable that created
  the row is never undone by deleting it), `list_events`. No-op change writes no row (`event_id=None`). The listener polls `enabled=1`, so a DB write is the whole effect.
  Callers: `blueprints/telegram.py` (`tg_chat_category|tg_disable_chat|tg_enable_chat`, `_chat_change` maps refusals), `brief_actions._undo_chat_event`.
- **tg_tasks.py** (5.1) — commitments from chat as a cascade: `triage_candidates/triage_thread/triage_all` (local
  `TG_TASKS_TRIAGE_MODEL`=qwen2.5:7b-instruct, $0), `extract_candidates/extract_thread/extract_all` (Claude on the selected →
  `action_items` with `source='tg_thread'`, 4.35.0: `status='proposed'` through `task_proposals.filter_candidates` / `existing_keys`, owner names in the prompt), `stats()`. CLI `triage --dry-run|--chat|--force|--limit`, `extract …`, `stats`.
  *The "no Claude on `source_type='telegram'`" guard is lifted SELECTIVELY. The model sees a PREVIEW of each message. A task
  is attached to a SPECIFIC message (`chat`/`said_by`/`link` in `list_commitments`). `triage_msgs` — a watermark
  (a thread that has grown is re-evaluated). A rerun erases only `source='tg_thread'`. No Ollama → `skipped` with a
  reason; Claude failure → a thread without `tasks_at` waits for the next pass. 4.35.0: a rerun erases only undecided `proposed` rows
  (`tg_tasks.py:329`); a task whose `text_key` already exists in the record in any status is not re-proposed.*
- **tg_questions.py** (5.3) — unanswered questions, SQL only (nothing is persisted): `resolve_self()` (`:108`),
  `is_question()`, `addressed_to_me()`, `open_questions(db_path, *, days=90, ...)` (`:185`). MCP `list_open_questions`.
  CLI `list`, `whoami [--refresh]`.
  *The addressee is proven by STRUCTURE (DM or @handle), not by a model; group questions with no address are deliberately not shown (counter
  `skipped_group`). The owner's presence is counted per CHAT in the `TG_QUESTIONS_WINDOW_H`=24 window, not per thread. The owner's handle is
  the listener's `/status` (`TELEGRAM_CONTROL_HOST/PORT`, cache `data/tg_self.local.json`; a cache without a handle is incomplete), overridden by
  `TELEGRAM_SELF_NAME`/`TELEGRAM_SELF_USERNAME`. The "owner is in the largest number of chats" heuristic is NOT used.*
