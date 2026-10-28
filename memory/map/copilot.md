# copilot — live call copilot (`app/services/copilot/`)

**Idea:** during recording a local model (Qwen-14B/Ollama, $0) runs a topic state machine,
dispatches RAG search, catches contradictions/questions/facts → operator cards. Claude
(Sonnet/Opus, the «big brother») verifies only what is flagged as important — a **local-first cascade**.
Gate `COPILOT_ENABLED`; everything degrades (no Ollama/key → whisper-only stays alive). Open work: `docs/ROADMAP.md`.

## Modules
- **config.py** — pure logic (no I/O): `resolve_settings()` folds the UI settings +
  the MODE×IMPORTANCE matrix into the final snapshot. MODES=light/medium/hard, IMPORTANCE=low/medium/high.
  Parameters: cadence_sec, escalate_threshold, top_k, safety_sweep_sec, verify_votes, min_unanchored_conf,
  **max_cards / min_card_gap_sec / verified_only / scope_projects** (Track 3, 25.07.2026).
  *api_enabled=True only if IMPORTANCE≥medium or forced; the Opus arbiter only for high+uncertain.*
- **service.py** — `CopilotService`: `start()`, `get_state()`, `end()`, `link_transcription()`,
  `get_timeline()`. A thin persist layer. Per-session RLock. *config_json — a snapshot of the resolved settings.*
  *One owner per process (4.7.0, ADR-026 D5): `start()` raises `CopilotBusyError(owner_session_id)`
  if the `active` row of another recording is still alive (`is_recording_active`, from app.py = `recording_service.is_active`);
  active rows of dead recordings are closed right there. Moving between recordings — `POST /api/copilot/move`.*
- **topics.py** — `TopicTracker`: `apply(status,label,return_to,ts)`, `topic_list()`. LLM-driven
  (NOT embeddings — embed_fn is left for compatibility, unused). *Degenerate LLM output → fallback to continue (anti-hallucination).*
- **dispatcher.py** — `Dispatcher`: `triage_and_plan()` + `analyze_with_evidence()` (2 separate
  Ollama calls with structured JSON: TRIAGE_SCHEMA / ANALYSIS_SCHEMA). *The first does not see the archive, the second sees the chunks.*
- **worker.py** — `CopilotWorker` (singleton daemon): `_tick()` — window accumulation → dispatch →
  topics → escalation. `_WS` (per-session WorkState: rolling transcript/summary, shown_chunks, topic_chunks).
  *Wakes every ~COPILOT_TOPIC_TICK_SEC (~6s); respects pause; the rolling summary is not cleared; chunk dedup.*
  *`directions-projects-tags`-25/27 (4.6.0, C10): `_scope_entity_ids` calls `scope.scope_filter_ids` and
  catches `CategoryError('ambiguous')` BEFORE the general `except Exception` — the search goes WITHOUT narrowing
  (`ids=[]`, as with not-found), but visibly to the operator: once per session (`ws.scope_ambiguous_notice_sent`)
  a `log_event` event + a card `kind="scope_notice"` through the existing insight path (outside `_card_gate`/
  the card budget — a system warning, not an insight), the text lists the candidate paths.*
- **escalate.py** — `Escalator`: `verify(observation,chunks)` (verdict real/refuted/uncertain),
  `sweep(window,chunks)` (an independent pass). Tool-use enforced (report_verdict / report_findings).
  `cost_estimate()` from pricing.py. *Graceful: no key → None, the copilot stays local; prompt caching.
  Since 28.07.2026 the defaults are Sonnet 5 (normal) / Opus 5 (gnarly); for Claude 5 thinking is turned off explicitly
  (on-by-default would eat the small max_tokens of the live verdict).*
- **export.py** — `collect_notes()`, `export_markdown()`, `export_json()`, `notes_digest()`.
  Pure functions. *evidence chains via ref_event_id (insight_verified → insight_local).*

## Data flow (cascade)
1. **Capture** — RecordingService + LiveTranscribeWorker → live segments ~8s (small Whisper, no diarization).
2. **Accumulate** (`worker._tick`) — new segments into a rolling window (~200–1000 characters).
3. **Triage** (`dispatcher.triage_and_plan`) — window+topics → Ollama → `{topic_status, needs_retrieval, observations}`.
4. **Retrieval** (conditional) — if needs_retrieval → `retrieval.search()` → chunks with chunk_id+provenance.
5. **Analysis** (`dispatcher.analyze_with_evidence`) — window+chunks → Ollama → insights with `evidence_chunk_ids`.
6. **Topics** (`TopicTracker.apply`) → SSE `copilot_topic`.
7. **Filter** — observations are screened by `min_unanchored_conf` (no proof → only high-conf) → persist `insight_local`.
8. **Verify→Show** (`worker._process_insight`, Track 3) — **verdict BEFORE showing**: persist always,
   `Escalator.verify()` → we show only `real` and specifically Claude's text (it also polishes), not the local
   model's draft. The `_card_gate` gate holds the attention budget (max_cards) and the pause between cards (min_card_gap_sec);
   what is muted stays in the DB with `payload.shown=false` for review after the call.
9. **Safety sweep** (periodic) — `Escalator.sweep()` independently looks for what was missed → sweep-findings
   (subject to the same card budget).
10. **Export** — timeline → md/json with inline notes+quotes.

## Economics / degradation
- Ollama $0. Claude verify+sweep ~cents/hour, prompt caching ~10% input.
- Budget: importance→budget_usd (low=$0/med=$1/high=$3), hard stop → local-only.
- No embeddings → no RAG proof; no Ollama → the copilot is inactive; no key → local verdicts remain;
  budget exhausted → api_enabled=False.
- **verified_only degrades softly:** if the API is unavailable (no key / budget exhausted / "local only"
  mode) — we show the local one, but only if anchored by evidence. A copilot that went silent is
  worse than an imprecise one.

**Motivation for Track 3 (measurements over 80 sessions):** 5743 local insights ≈ 72 cards per call,
Claude verified 5.9% (and that AFTER showing), `operator_action` = NULL in all 9706 events with working
👍/👎 buttons — nobody read the widget. Limits: light 3 / medium 5 / hard 8 cards.

**Agent control (4.32.0, mcp-recording-live):** `blueprints/copilot.py` `_agent_session_or_403` (non-`ui` caller on a session NOT bound to an agent-class recording -> 403 `OWNER_SESSION`; `_is_agent_session` via `guards.is_owner_session`)
guards `settings`, `action`, `stop`, `reingest`; `settings` on an agent session clamps spend (no API, budget 0, no escalate). `reingest` (:450) is idempotent: `copilot_sessions.reingest_transcription_id`/`reingest_by` (migration v57),
claimed by a conditional UPDATE (a concurrent loser rolls back), repeat -> `created:false`; `CopilotService.get_reingest` :481. MCP: `copilot`, `get_copilot_timeline` (`root-processes.md`).
**«What did I miss» (4.37.0):** `app/services/live_missed.py` — `summarize(session_id, window, segments=, now=)`; `WINDOWS` since_self|2m|5m|10m|whole (`whole` tail capped `MAX_CHARS`=30000, `truncated`). One Claude call (`escalate.DEFAULT_MODEL` via `claude_retry.call_with_retry`, 3-5 lines),
never `local_llm` (`_GEN_LOCK` would silence the copilot). Cost estimated BEFORE the call (`pricing.estimate_cost`), reserved in an in-memory per-session total, over `LIVE_MISSED_BUDGET_USD` (0.10) -> `BudgetExceeded`; no key / API failure -> `Unavailable`. Nothing persisted.
Route `POST /api/copilot/live-missed` (`blueprints/copilot.py:298`): 400 `INVALID_ARGUMENT`, 409 `AMBIGUOUS_SESSION`, 403 `AGENT_SESSION` (agent-class recording, every caller), 404 `NOT_FOUND` (not live), 429 `LIVE_BUDGET_EXHAUSTED` (+`spent_usd`,`budget_usd`), 503 `UNAVAILABLE`; texts `svc_live_missed.err.*`. MCP `live_missed`.
**Review screen:** `GET /api/copilot/sessions` rows carry `shown` (payload.shown absent counts as shown), `suppressed` (`shown = 0`), `thumbs_up/down` (owner `operator_action` only) — `service.py:283-292`; SPA `/copilot` read-only (`frontend.md`).
**Offline tests:** `test_copilot_dispatcher.py` (root), `tests/test_copilot_quiet.py` (Track 3). **DB:** `copilot_sessions`/`topics`/`events` (migration v19).
**SSE:** `copilot_topic`, `copilot_insight`. The session is linked to a transcript on transcribe → timeline on /transcript.
