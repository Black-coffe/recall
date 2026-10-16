# services — `app/services/*` (hub)

last-verified: 2026-10-16

Business logic outside HTTP. The map is split into slices of ≤80 lines; this file is only an index.
Language (4.17.0): service code, comments, docstrings and logs are English (`.claude/rules/code-language.md`, guard
`tests/test_code_english.py`: Cyrillic in comments/docstrings/logger-call strings fails, quotes ≤60 chars exempt). User-facing
service texts go through `tr()` with uk/en entries in `app/core/i18n_catalog/svc_*.py`: `svc_boot`, `svc_embed`, `svc_llm`,
`svc_media`, `svc_pipes`, `svc_recording`, `svc_retrieval`, `svc_tasks`, `svc_taxonomy`, `svc_telegram` (keys `<area>.<topic>.<name>`;
modules auto-discovered by `i18n_catalog.load_modules()`, duplicate key → `ValueError`). Exempt: LLM prompts, parsing regexes, DB data.
Separate map files: [copilot.md](copilot.md), [recording.md](recording.md).

- [services-media.md](services-media.md) — transcription, YouTube, diarization, live, `vocabulary` (K1), audio dedup,
  TG link repair, `record_meta` (title/description).
- [services-video-privacy.md](services-video-privacy.md) — video visual timeline (`video_frames/analysis/timeline`),
  `screen_secrets`, `privacy` (what does not go outside via MCP/pipes).
- [services-pipes-bot.md](services-pipes-bot.md) — scheduled pipes (`pipes*`), cards, `brief_actions` buttons,
  Bot API `tg_bot` (K4/K5).
- [services-taxonomy.md](services-taxonomy.md) — `categories`: direction→project tree, tags, filters, `resolve_node`.
- [services-embeddings.md](services-embeddings.md) — `embeddings` (chunks, prefixes, epochs), `reembed`, `summaries`.
- [services-retrieval.md](services-retrieval.md) — `retrieval` (RRF, rewrite, explain, thread context), `archive_grep`,
  `reranker`, `rag` (Ask Archive), `query_rewrite`, `categorize`.
- [services-enrichment.md](services-enrichment.md) — `enrichment`, `document_parser`, `text_polishing`, `research`,
  `commitments` (deadlines, digests, `apply_action`), `scope`.
- [services-telegram.md](services-telegram.md) — `tg_threads`, `tg_chat_context`, `tg_tasks`, `tg_questions`.
- [services-mail.md](services-mail.md) — Gmail ingest (4.18.0): `mail_accounts` (keyring), `mail_imap` (read-only), `mail_parse`,
  `mail_sync` (+`MailPoller`), `document_ingest` (shared `ingest_file`).
- [services-entities.md](services-entities.md) — `entity_dedup`, `tg_entities` (+morphology), `comments.link_entities`,
  `evals/graph_links`.
- [services-infra.md](services-infra.md) — `job_queue`, `sse_broker`, monitoring, `local_llm`, `models`, `pricing`.
