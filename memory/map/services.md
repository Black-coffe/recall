# services — `app/services/*` (hub)

last-verified: 2026-10-15

Business logic outside HTTP. The map is split into slices of ≤80 lines; this file is only an index.
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
- [services-entities.md](services-entities.md) — `entity_dedup`, `tg_entities` (+morphology), `comments.link_entities`,
  `evals/graph_links`.
- [services-infra.md](services-infra.md) — `job_queue`, `sse_broker`, monitoring, `local_llm`, `models`, `pricing`.
