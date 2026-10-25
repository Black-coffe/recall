# 🗺️ memory/map — Recall code map

> Borrowed from [VULYK](https://github.com/Black-coffe/vulyk) (layer 4 — `memory/map/`),
> adapted to our repository. This is a **navigation hint**, not the truth:
> always check against the real code before editing (for example, before a large change
> in `app.py`). If the map has diverged from the code — update the map (delegate to a cheap agent:
> `Agent(model: haiku)` or `Agent(effort: low)`).

**Why:** `app.py` (817 lines), `whisper_manager_new.py` (867), the `transcription.py`
blueprint (~1600), `recall.css` (1397) — do not fit in one view. The map gives
an entry point: where to go for a feature without rereading half the repo.

## Map files

| File | What it describes |
|---|---|
| [overview.md](overview.md) | Architecture, 3 processes (Flask + telegram_listener + mcp_server; mail poller is a thread in Flask), boot/shutdown, SSE/logs/rate-limit/security |
| [root.md](root.md) | Root entry points: `app.py` boot, `config.py`, `app/state.py`, `whisper_manager_new.py` |
| [root-processes.md](root-processes.md) | Separate processes: `telegram_listener.py`/`telegram_login.py`, `mcp_server.py` |
| [blueprints.md](blueprints.md) | HTTP layer: overview of 18 blueprints (~190 endpoints) + system/events/automation/settings/connect/comments/documents/mail/copilot/speakers/bookmarks |
| [blueprints-media.md](blueprints-media.md) | transcription (video, export), audio_library, youtube, recording (K2 autoprompt) |
| [blueprints-memory.md](blueprints-memory.md) | memory (categories, tags, RAG, action-items), research, telegram |
| [services.md](services.md) | `app/services/*` — hub: 11 slices `services-*.md` (media, video-privacy, pipes-bot, taxonomy, embeddings, retrieval, enrichment, telegram, mail, entities, infra) |
| [services-mail.md](services-mail.md) | Gmail ingest (4.18.0): `mail_accounts`/`mail_imap`/`mail_parse`/`mail_sync` (+`MailPoller`), `document_ingest`; 4.19.0 `connect_links`/`secret_shape`/`settings_secrets` |
| [services-infra.md](services-infra.md) | job_queue, sse_broker ring, env_file, error_codes; 4.22.0 approval gate (`approval_gate`/`approvals`/`approval_summaries`) + `audit_hooks`/`mcp_actions` |
| [copilot.md](copilot.md) | `app/services/copilot/*` — live copilot, local-first cascade (Qwen→Claude) |
| [recording.md](recording.md) | `app/services/recording/*` — WASAPI recording, FSM, finalize, recovery, video tracks |
| [frontend.md](frontend.md) | SPA `static/js/recall/*` — router, views, SSE-via-fetch, service worker |

## Map conventions

Each module is described as:
- **Purpose** — 1-2 sentences.
- **Entry points** — public functions/classes/routes that are called from outside.
- **Types/state** — important globals, dataclasses, singletons.
- **Gotchas** — Windows specifics, initialization order, GPU locks, budget, the non-obvious.

How to work with the map — in `CLAUDE.md`, section `«🐝 Routing-матриця моделей»`.

*Generated 2026-06-23 from the real code (4 parallel Explore agents). On
significant architecture changes — regenerate the relevant file.*
