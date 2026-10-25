# Hive memory index

<!-- Pointer index: <= 60 lines, always loaded. Pointers are hints - verify against code before acting.
     Maintained by drone-docs (pointers) and librarian (hygiene). Humans welcome too. -->

## Codebase map
<!-- one line per mapped module, added by /vulyk-bootstrap and /vulyk-map -->
- [index](map/index.md) — map navigation: which file covers what (migrated from docs/map 21.08.2026)
- [overview](map/overview.md) — the whole system from above: data flows, processes, entry points
- [root](map/root.md) — app.py boot (+ K4 pipes/K5 buttons), config, state, whisper_manager_new; processes — [root-processes](map/root-processes.md) (telegram_listener, mcp_server: 82 tools = 51 R + 31 W/D (4.30.0 adds R `get_live_video|get_live_frames`; 4.27.0 adds 8 W: `audio_download|audio_item|transcribe|document_ingest|job_cancel|transcript_ai|models|system_diagnostics`, 2 D: `document_import_folder|document_reparse`; before that 68 = 47 R + 21 W/D; 11 W: `video_analyze`, `mailbox`, `settings`, `retranscribe`, `activate_transcript_version`, 4.26.0 `task|bookmark|comment|taxonomy|speaker|transcript`; 10 D: mail x3, `settings_remove_anthropic_key`, `taxonomy_delete|merge`, `speaker_merge|delete`, `transcript_delete`, `enrich_backfill`); `_TOOL_CLASS` registry, profiles, W ceiling, error envelope, `wait_for`, `list_mcp_actions`)
- [blueprints](map/blueprints.md) — 18 Flask blueprints (overview, incl. mail, `connect` one-time secret pages and `automation` event long-poll) + [media](map/blueprints-media.md) + [memory](map/blueprints-memory.md)
- [services](map/services.md) — hub for app/services/* → slices services-*.md (pipes-bot: K4/K5 pipes and buttons; enrichment: commitments/scope; retrieval: RAG; taxonomy; telegram; mail: Gmail ingest + document_ingest + connect_links/secret_shape/settings_secrets; entities; video-privacy; media; embeddings; infra: job_queue slow executor, SSE event ring, env_file, error_codes, approval gate + `mcp_actions` audit, job `result`; media: 4.27.0 `transcription_run`, `transcript_ai`, `model_download`, `diagnostics`, `import_roots`)
- [copilot](map/copilot.md) — live call copilot: topic machine, dispatcher, local→Claude cascade
- [recording](map/recording.md) — server-side recorder: up to N parallel sessions, mic+loopback, per-application audio isolation (process loopback), SSE, call-driven «Record it?»/«Stop recording?» prompts (call_watch + toast, K2), autosave.py (4.23.0)
- [frontend](map/frontend.md) — SPA: router/views/cmdk, i18n.js + dictionaries, recall.css, service worker

## Unmapped territory
- tests/ (structure is described by pytest.ini + CLAUDE.md; no separate map)
- scripts/mirror/ (sanitized mirror — README in the folder, does not go to the public repo)
- evals/ (RAG recall@k + `graph_links.py` — read-only graph measurement on a DB snapshot; `compare.py` — Wave B, puts two `--json-out` gate runs side by side; described in [services-entities](map/services-entities.md))

## Wiki domains
<!-- load-bearing domain notes in docs/wiki/ -->
- [entity-graph-provenance](../docs/wiki/entity-graph-provenance.md) — one mention/one owner, `thread_match` vs `thread_morph` provenance never interchangeable, morph matcher opt-in only
- [rag-context-integrity](../docs/wiki/rag-context-integrity.md) — duplicate_of excludes from search only, thread stitching char ceilings (hit ≥ neighbour), chronological order_citables, ask_log logs every successful answer from both channels; Wave B: context prefix in vector+BM25 never in citations, vector branch never mixes embedding epochs, re-embed only on a snapshot, query-rewrite flag lives in retrieval.search; 4.6.0: prefix carries direction→project path + tags for every record type, PREFIX_FORMAT/embedding_epochs refuses to write a new prefix format into an old epoch, schedule_records_reembed batches multi-record writes
- [taxonomy-tree-and-tags](../docs/wiki/taxonomy-tree-and-tags.md) — canonical node identity is its full path (siblings-only name uniqueness, v47), a bare ambiguous name never resolves silently (CategoryError('ambiguous') propagates, never swallowed into empty), --dry-run on a write pass is the same code with commit=False + rollback, rename/reparent invalidates the whole subtree's embedded prefix
- [recording-sources-and-sessions](../docs/wiki/recording-sources-and-sessions.md) — parallel sessions (registry, per-session lock, SESSION_LIMIT), AudioSource boundary, `all` byte-identical, fixed .pcm format (EXCLUDE-own-PID), silence padding + source_events, session-PID tree-root targeting, one copilot per process, AMBIGUOUS_SESSION never guesses, call autoprompt only asks (click-token starts, autostop only after «Stop recording?»), server autosave of any stopped recording + RECORDING_KEPT discard guard (4.23.0)
- [video-timeline-and-screen-secrets](../docs/wiki/video-timeline-and-screen-secrets.md) — frames never stored (cut on the fly), secrets masked before any write/describe and redacted in every outgoing image, raw values only in screen_secrets via TTY `reveal`, cache key = video_analysis_params
- [scheduled-pipes-delivery](../docs/wiki/scheduled-pipes-delivery.md) — closed read-only TOOLS registry, code-built HTML brief cards (K5), per-field mask+stop-word check, filter before render, fail closed, proxied (tailscale) requests not local-trusted, bot only to owner chat, failed run never closes a schedule moment
- [ui-language](../docs/wiki/ui-language.md) — `RECALL_UI_LANG` auto|uk|en; browser sends `X-Recall-Lang`, server falls back to setting then uk; Telegram uses `server_lang()` at render/press time; archive content stays in its source language, code/logs are English (guard `tests/test_code_english.py`), service texts live in `svc_*` catalogs, MCP runs `en`; every UI string is a dictionary key (coverage guard)
- [gmail-ingest-privacy](../docs/wiki/gmail-ingest-privacy.md) — mail channel is read-only (EXAMINE + BODY.PEEK, no passthrough), app password only in the OS keyring (never DB/.env/logs/backups), body/subject/attachment text masked before any write, Claude off unless the label's `enrich` is on (`mail_enrich`), failed message never advances the watermark; 4.19.0: password/key typed only on a one-time loopback `/connect/` page, MCP and chat never see it; 4.24.0: enrich-on, >12-month backfill and disconnect are gated routes for every caller
- [approval-gate](../docs/wiki/approval-gate.md) — a D action runs only after a native Windows dialog said yes (fail closed, one dialog, no skip), dialog text from DB not the request, every non-GET `/api/*` audited in `mcp_actions` without body, D ceilings in app.py / W ceilings in the MCP process
- [transcript-versions](../docs/wiki/transcript-versions.md) — canonical `transcriptions` columns stay the only transcript read; `transcript_versions` is a side store (max 3, lazy original, stale active row), `transcription_speaker_map` mirrors the active version, re-run is a SLOW job refused during recording

## Verification
- build: none — interpreted Python + vanilla JS
- test: `.venv/Scripts/python.exe -m pytest -q -m "not slow"` (⚠️ migrates the live DB — not during recording)
- lint: none

## Learnings
- Consolidated: memory/learnings/CONSOLIDATED.md (run /vulyk-gc to refresh)
- Owner lessons/feedback live separately in Claude Code auto-memory (outside the repo) — a feedback layer, not a code map
