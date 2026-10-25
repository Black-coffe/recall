# services — enrichment, documents, commitments, scope

last-verified: 2026-10-18 (mcp-foundations-a, 4.20.0)

Part of `app/services/*`; hub — [services.md](services.md).

## Enrichment / documents
- **enrichment.py** — "Meeting Memory": `enrich_transcription(..., model=None)` (`:424`; keyword-only `model` → into
  `text_polishing.extract_meeting_card`) — 1 Claude call → summary/key_points/action_items/entities/suggested tags →
  graph upsert (people/projects/orgs + aliases + mention-salience). Idempotent by `enriched_at`; `ENRICHMENT_VERSION=1`
  forces a re-run. `list_unenriched_ids`, `backfill --force` and `enrich_transcription` skip `duplicate_of IS NOT NULL`.
  `optimize_chunk_index()` (`:657`). CLI `python -m app.services.enrichment backfill-cards --dry-run [--model M] [--limit N]
  [--kind calls|docs|all]` — card phase only; selection `summary_json IS NULL AND source_type != 'telegram' AND deleted_at IS
  NULL AND duplicate_of IS NULL`; dry-run counts characters/tokens(≈4)/cost (`pricing.estimate_cost`), top-10 longest.
  Gmail guard (4.18.0): `enrich_transcription` returns `skipped`/`reason=mail_enrich_off` when `transcriptions.mail_enrich == 0` and
  not `force` (`:498`); `backfill-cards` and the unenriched-id list add `mail_enrich IS NOT 0` (`:594`, `:708`). NULL = not mail.
- **document_ingest.py** (4.18.0, moved out of `blueprints/documents.py`) — the document persistence chain: `find_duplicate`,
  `persist_document`, `submit_enrichment`, `describe_tabular`, `structure_json`, and `ingest_file(...)` shared by folder import and
  mail (details — [services-mail.md](services-mail.md)).
- **document_parser.py** — `parse_document()` → (text, blocks); parsers by extension (lazy): md/txt/docx/pdf/pptx/xlsx/csv/
  images. OCR Tesseract (≤50 pages), tables→markdown(+NL description by Claude). PyMuPDF/python-pptx/openpyxl are optional
  (`ParserUnavailable`); `PARSER_VERSION` → re-parse. `_configure_tesseract()`: binary `OCR_TESSERACT_CMD` or
  `C:\Program Files\Tesseract-OCR`; languages `OCR_TESSDATA_DIR` or `%LOCALAPPDATA%\Recall\tessdata`.
- **text_polishing.py** — Claude post-processor: `polish_text()`, `polish_diarized_text()` (stream), `extract_meeting_card()`,
  `describe_sheets()`, `_strip_json_fence`. Lazy client; prompt caching; model `models.get_default_model()` (default
  `claude-opus-5`, override `CLAUDE_MODEL`).
- **archive_import.py** — bulk import of `meeting_archive/MM_YYYY/*.md`; idempotent by `source_url=abs-path`.
- **research.py** — `find_mentions()`, `export_originals()` (0 tokens), `export_summary()` (map-reduce Haiku). FTS5+substring;
  ±2 segments / ±400 characters; ≤12 fragments/record; batch 40k characters. The axes `category_id/tags/since/until` — via
  `app/blueprints/research.py:_parse_axes` (`categories.parse_*`). `+rowid IN` — see retrieval.

## commitments.py — deadlines, digests, task status
Only stdlib + `app.db.connection` (imported from the stdio-MCP; no torch).
- `parse_due(raw, anchor)` (`:368`): a raw phrase → an ISO date relative to the **meeting date** + a precision class (`day|week|month|
  quarter|soon|event|next_meeting|recurring`); the raw phrase in `due` is NOT rewritten. The offline passes `backfill_due`,
  `link_owners`, `relink_owners`, `dedup`, `mark_stale`, `sweep_dropped` are idempotent, with `--dry-run`.
- Read core for MCP/UI/pipes: `list_commitments`, `count_commitments`, `commitments_coverage`, `dropped_commitments`
  (`:1140`), `stale_topics` (`:1264`), `weekly_digest` (`:1364`). The filters `category_id/tags/since/until` — a single helper
  `_scope_clauses` (`:903`); the coverage denominator is computed over the SAME set as `items`.
- Truthfulness rules: `stale_topics` confirms silence with TEXT (FTS; `last_seen_source=graph|text`, the exact form of the name —
  the error goes toward "silent"); `this_week` — from TODAY, not from Monday; `dropped_commitments` takes only spoken
  sources (`_OWN_SOURCES`, `:1137`) and is NOT deterministic on the live archive; the output carries `owner` + `owner_said`; the
  `link_owners` fallback by name — only a single-word entity; the `owner` filter also looks at the graph's ALIASES (after `entity_dedup merge`);
  sorting `meeting_date DESC`, then `id`; a task from a chat carries `chat`/`said_by` + a `chat_id`/`msg_id` pair (in
  legacy groups without `-100` the link does not exist, so `link` alone is not enough).
- K5 (4.15.0): the single WRITE path for status — `apply_action(db_path, item_id, action, *, source, actor, today)` (`:1424`;
  `done|cancelled|open|postpone:1|7|30`; postponing → `due_date`=today+N, `due_precision='day'`, status `open`, also from
  `stale`) and `undo_action(db_path, event_id, *, source, actor)` (`:1445`; only the task's LAST event, not `undo`, otherwise
  `ValueError`; the cancellation is an event `action='undo'`, `undo_of`). The change + an `action_item_events` row (v51) — one
  transaction; unknown → `LookupError`. Callers: `brief_actions` (`source='bot'`), `PATCH /api/memory/action-items/<id>`
  (`source='web'`). MCP has no status-change tool yet (its 3 write tools are `video_analyze`, `mailbox`, `settings`; ADR-042).

## scope.py — two-layer archive scope
- `apply_chat_categories(db_path, *, mapping_path=data/chat_categories.local.json, dry_run=False)` (`:227`; the map is outside git):
  a row `{chat_id, title, category, parent?, tags?}` is resolved by the pair `(norm_name(parent), norm_name(category))`, NEVER by the
  bare `category` (two rows with the same name under different `parent` → two nodes); without `parent` — `categories.resolve_node`.
  A single write path: `dry_run` runs the same code (`categories.*` with `commit=False`) and ends with `conn.rollback()`;
  dry-run `stats` == the real ones (except `reembed_scheduled`).
- `label_uncategorized(db_path, *, min_confidence=0.6)` (`:491`) — auto-labelling with three signals (record title → k-NN with
  priors → Claude Haiku arbiter).
- `scope_filter_ids(db_path, project)` (`:644`) — a slice by project/person as a **union** of the `meeting_entities` graph,
  a text mention (FTS5+title) and a same-named direction (the graph exists only in the enriched ~16%); comparison by `name_norm`.
  `_expand_to_threads()` expands the slice to whole threads. `CategoryError('ambiguous')` is passed upward (the copilot sees a
  namesake, not an empty result).

> **4.26.0:** the "3 write tools" above is stale; MCP now has 68 tools (47 R + 21 W/D), see root-processes.md.
