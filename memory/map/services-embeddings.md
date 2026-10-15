# services — embeddings, re-embed, summaries

last-verified: 2026-10-15

Part of `app/services/*`; hub — [services.md](services.md).

## embeddings.py
- Model/version: `EMBED_MODEL` (env, default `intfloat/multilingual-e5-large`, `:57`), `EMBED_VERSION` (int env, default `2`,
  `:87`; the live epoch is set in `.env`, ADR-016 not revisited). `embed_text()/embed_texts()/embed_query()`.
  Lazy singleton + lock; ~/.cache/huggingface cache ~2.2GB; mute-mode if torch is missing.
- Chunkers: `_chunk_from_segments` (audio), `_chunk_from_blocks` (documents, page/section), `_chunk_from_text`. The audio
  chunk boundary is a speaker change or a pause ≥1.5 s with ≥350 of the speaker's OWN characters; `_MAX_CHARS=1000` is only a safety net; overlap ≤150
  chars only on a break at the limit.
- A change of chunking/model = bump `EMBED_VERSION` → re-embed (via `reembed.py`); after a mass pass
  `enrichment.optimize_chunk_index()` merges the `chunks_fts` segments (without it search goes 9 s → 64 s).
- `_style_for(model_name)` (`:126`): prefix style by the model FAMILY from its name (there is no separate env): `e5` (`query:`/`passage:`),
  `qwen3` (query — `Instruct: {task}\nQuery: {q}`, passage without a prefix), `plain`; all use `normalize_embeddings=True`.
  `EMBED_DIM` defaults to 1024 until lazy loading, then `_apply_model_dim()` replaces it with the actual value.
- `build_context_prefix(meta, chunk)` (`:671`, contract C4): line 1 — `[тип] назва|чат · дата · спікер|автор ·
  напрямок → проєкт · нитка: label|стор. N` (empty ones are skipped; the title via `record_meta.display_name`; the direction path
  for ALL types, BEFORE the type tail); a `теги: a, b` line (≤5, the whole line ≤60 chars, cut at a whole tag); then
  `опис: {…}` (`_PREFIX_DESCRIPTION_MAX=300`, line breaks → spaces); then `summaries.unit_summary_line()`. The embedder input =
  `prefix + "\n" + text`; `chunks.text` is text only, `chunks.context_prefix` (v43) is a separate column, it does
  NOT end up in citations/export/threads. A record without a direction and tags gives a prefix byte-identical to epoch 2. `_load_prefix_meta` — one SELECT
  (`category.name`+`parent.name`+`GROUP_CONCAT(tags.name, char(31))`, separator U+001F, because a comma is allowed in a tag name).
- Epochs (contract C3): `PREFIX_FORMAT = 2` (a fact of the CODE; 1 = before the spec, `:102`) + the table `embedding_epochs(model, version,
  prefix_format)` (v46, `migrations.py:1896`; backfill — everything before v46 is format 1). `epoch_status(conn)` (`:876`) →
  `{ok, reason, pair, db_format}` (`reason`: `ok|format_mismatch|new_epoch`); `chunk_and_embed_transcription` on
  `format_mismatch` does `logger.error` and REFUSES to write chunks; `new_epoch` → `register_epoch()` in the same transaction.
  CLI `python -m app.services.reembed epoch-check [--db PATH]` — read-only, exit 0 (`ok`/`new_epoch`) or 1; it reads
  `EMBED_VERSION` from the PROCESS ENVIRONMENT, not from `.env`.

## reembed.py
- Offline on a SNAPSHOT for the current pair: `stale_ids()/plan()` (live, non-duplicates whose pair does not match), `run(db_path, limit=,
  dry_run=)` — one `chunk_and_embed_transcription` at a time; `optimize_chunk_index()` exactly once at the end if `done>0`.
  CLI `python -m app.services.reembed run --db PATH [--dry-run] [--limit N] [--yes-live]`; `is_live_db()` refuses to run on
  `Config.DATABASE` without `--yes-live`. Comments — `python -m app.services.comments reindex`.
- `schedule_record_reembed(tid, *, db_path) -> str` (`:285`) — a point re-embed of one record (`db_path` is a required
  keyword WITHOUT a default: a default would overwrite live chunks from a test). Returns `queued` (`job_queue`, kind `reembed_record`),
  `deferred` (a live recording is in progress — `threading.Timer` after `_DEFER_SECONDS=30`, no slot is taken), `pending` (the id is in
  `_pending_ids`; removed at the START of the task body, before the row is read), `skipped` (mute-mode/no queue).
- `schedule_records_reembed(ids, *, db_path, reason) -> {mode: point|batch|none, count, stale}` (`:463`): ≤20 live ids
  (`_BATCH_THRESHOLD`) → one by one; >20 → a single `reembed_batch` (`_batch_reembed_job`, `optimize_chunk_index()` once at the end);
  ids with an old pair → `stale` (picked up by the mass `run`). The batch removes ids from `_pending_ids` BEFORE reading the row; `finally`
  releases the tail on cancellation/exception and logs it with `WARNING` (otherwise `pending` forever). "Live recording" = a non-empty
  `active_session_ids()` (not `active_session_id` — with two recordings that one returns None).

## summaries.py
- A TG-thread summary by Claude with provenance: `summarize_thread()` (≤600 chars, a preview of each message, ceiling
  `TG_SUMMARY_MAX_CHARS=20000`; a thread with raw text <300 chars → `summary_model='verbatim'` without a call),
  `backfill_candidates()/backfill()` (CLI `stats`/`backfill --dry-run --model --limit --force`; writes `tg_threads.summary` +
  v42 `summary_source_ids_json/summary_at/summary_model/summary_msgs`), `stats()`.
- `unit_summary_line(conn, tid) -> str|None` (`:354`, C1) — ≤200 chars, the first sentence: telegram → the thread summary, otherwise
  `transcriptions.summary_json.summary`; the consumer is `build_context_prefix`.
- *A thread that has grown is not re-summarized automatically (`summary_msgs` is written, there is no reader). Only SQL + stdlib at module
  level (Claude — lazy import): safe from the stdio-MCP.*
