# services — infrastructure

last-verified: 2026-10-15

Part of `app/services/*`; hub — [services.md](services.md).

- **app/core/i18n.py** (140, not a service, but shared infrastructure; `app/core/i18n_catalog/`) — `LANGS`, `ui_lang_setting()`,
  `server_lang()`, `request_lang()`, `tr`/`tr_n`, `localize_sse`. An unknown key → returns the key + a warning. `RECALL_UI_LANG`
  is read from `os.environ` on every call. Contract details — `blueprints.md` § i18n, `docs/wiki/ui-language.md`.
- **job_queue.py** — `JobQueue.submit(kind, fn, meta)` on top of an executor; `Job` (state machine, `is_cancelled()`). Cancel
  is cooperative; history is bounded.
- **sse_broker.py** — in-memory pub/sub: `publish()`, `subscribe()`, `stream_events()`. `Queue.Full` silently drops; keepalive
  15 s; single-process (multi → Redis).
- **system_monitor.py** — daemon snapshot of CPU/GPU/RAM every N sec (the API does not block on `cpu_percent`).
- **file_cleanup.py** — daemon that cleans orphan upload/youtube temp files (does not touch DB-referenced ones; read-only DB).
- **metrics.py** — `MetricsRegistry`: `inc()`, `observe_duration()`, `render()` (Prometheus text).
- **file_manager.py** — `FileManager`: info/list/validate/checksum, `FileInfo`.
- **model_updates.py** — daemon that checks PyPI for a new faster-whisper (`CHECK_INTERVAL_DAYS=14`, `start_background_check`,
  fail-silent).
- **local_llm.py** — Ollama HTTP client: `is_available()`, `generate()`, `generate_json()`, `warmup()`, `resolve_model()`;
  `LOCAL_LLM_MODEL` default `qwen2.5:14b-instruct-q5_K_M` (`:51`), family substitution if the exact model is missing. $0; **a single
  `_GEN_LOCK` (`:46`) serializes all local calls** (GPU contention); urllib only.
- **models.py** — `get_default_model()` (`DEFAULT_MODEL` default `claude-opus-5`, override env `CLAUDE_MODEL` without restart).
- **pricing.py** — `MODEL_PRICES` + `estimate_cost()`; the single source of prices (rag, research, copilot/escalate, summaries).
