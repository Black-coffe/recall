# ADR-056: Task proposals live in `action_items`; decisions are tombstones that survive re-runs

- Status: proposed
- Date: 2026-10-27
- Spec: docs/specs/task-approval (brief Asks 1-9 are the contract; grill docs/grill/2026-10-27-task-approval.md)

## Context
Every AI-found task is inserted straight into `action_items` with `status='open'` by three writers
(`app/services/enrichment.py:405`, `app/services/tg_tasks.py:351`, `app/services/comments.py:921`), and each writer
first deletes its own previous rows (`enrichment.py:345` every task of the record; `tg_tasks.py:327` its thread;
`comments.py:912` its comment). The live DB holds 5484 rows: open 3233, stale 2250, done 1; `action_item_events`
(migration v51, `app/db/migrations.py:2110`) has never received a row. The owner wants (Asks 1-9): nothing is a task until
he presses "Yes"; only his tasks and promises made to him are proposed; a daily Telegram batch of at most 10 with
Yes/No and undo, the same queue through Claude over MCP; 3 days from the first show, then a hidden reversible
`expired`; decisions survive re-processing; all 5484 old rows go to an archive reachable on request and returnable
by one command; approved rows live until closed; a shown-to-pressed counter from day one.

Readers that filter `status` today: `commitments._commitments_filter` (:928, default `open`), `commitments_coverage`
(:1074, own WHERE), `dedup` (:671), `mark_stale` (:728), `sweep_dropped` (:759), `dropped_commitments`, `stale_topics`,
`weekly_digest` (:1364), `apply_action`/`undo_action` (:1425/:1458, `STATUS_ACTIONS = done|cancelled|open`),
`memory.py` PATCH (:1326, `open|done|cancelled`), MCP `list_action_items` (`mcp_server.py:1705`, enum
`open|stale|done|cancelled|all`), `pipes.py:208` (K5 undo summary over events), `pipes_cards.py`, the SPA
`views/tasks.js` chips (`open|done|stale|cancelled|all`), and `evals/tasks_eval.py` (reads rows by id, not by status).
Constraints: SQLite additive migrations only; the full pytest migrates the live DB; one user; consumption through
Claude + MCP. Memory lesson `e5-cosine-cannot-thread-chat`: e5 cosine separates reply pairs from random pairs by
+0.02 sigma, so embeddings are not a matching key.

## Options
1. **New status values in `action_items`** (`proposed`, `rejected`, `expired`, `legacy`; approval = `open`) - one table,
   stable row ids, every existing reader keeps working because `open` keeps meaning "a live task"; cost: four more
   values every status enum must name, and a bulk status flip in the migration.
2. **Separate `task_proposals` table promoted into `action_items` on approval** - clean separation; cost: promotion
   copies a row and changes its id (breaks K5 undo, the event journal and the eval golden ids), owner/due parsing and
   dedup exist twice, "show me what was proposed and rejected" needs a UNION in every reader.
3. **Boolean `approved` flag on `action_items`** - smallest diff; cost: rejected/expired/legacy still need a value,
   and `open AND NOT approved` leaks into every default filter that forgets the flag.

Re-run survival: (a) never delete decided rows, replace only `proposed` ones, skip a candidate whose key matches a
decided row (chosen); (b) keep delete-and-reinsert and copy decisions over by key (loses the id, re-fires events);
(c) stop re-extracting tasks on re-run (loses corrections of a bad first card).

Matching key: (a) `transcription_id` + hash of normalised text (chosen as the start, measured); (b) token Jaccard
like `commitments.dedup` (fallback if measurement says so); (c) e5 cosine (rejected by the memory lesson).

## Decision
1. **Proposals are rows of `action_items`.** Migration v59 (additive) adds `for_whom TEXT`, `confidence REAL`,
   `importance INTEGER`, `text_key TEXT`, `first_shown_at TEXT`, `decided_at TEXT`, `pre_legacy_status TEXT`, an index
   on `(transcription_id, text_key)` and one on `(status, first_shown_at)`. Status values become
   `proposed | open | done | cancelled | stale | rejected | expired | legacy`. **`open` keeps its meaning "a live task";
   from v59 on a live task is one the owner approved**, so every reader whose default is `open` (digest, MCP, SPA,
   `commitments_coverage`) shows approved tasks only, with no code change to its default. Every status enum
   (`STATUS_ACTIONS`, memory.py PATCH, MCP `list_action_items`, SPA chips, pipes cards) names the new values
   explicitly; `all` stays literally all, `legacy` is a value the caller asks for by name (this is the "MCP flag").
2. **Writers insert `status='proposed'` and never delete a decided row.** The three delete statements become
   `DELETE ... AND status = 'proposed'` within the writer's own scope (record / thread / comment, as today). Before
   inserting, a candidate's `text_key` (sha1 of NFC, lower-cased, punctuation-stripped, whitespace-collapsed text) is
   looked up in the same `transcription_id`; a hit in any status (open, done, cancelled, rejected, expired, legacy,
   stale) means the candidate is not inserted. A candidate whose `for_whom='third_party'` or `confidence` below
   `TASK_PROPOSAL_MIN_CONFIDENCE` (setting, default 0.6) is not inserted either; the writer logs the dropped counts.
   A missing field (model non-compliance) is stored as `for_whom='unknown'`, `confidence=NULL`, `importance=2` and is
   proposed: non-compliance is not low confidence, and the measurement story counts it. Fuzzy matching is not built
   in this spec: a measurement story re-runs extraction on records that already carry decisions and reports how many
   re-proposed candidates a human calls "the same task"; if that share exceeds 10 %, the next spec adds Jaccard over
   `commitments._tokens` within the same record, never embeddings.
3. **The window clock is `first_shown_at`, set by the show path, expired by a daily pass.** The daily batch (a pipe
   next to the K4/K5 ones) selects `status='proposed' AND dup_of IS NULL` ordered by `due_date IS NULL, due_date,
   importance DESC, id`, takes at most 10, sends them as K5 cards (Yes / No / undo) and writes one `show` event per
   item whose `first_shown_at` is NULL, setting it; a later show of the same item writes nothing. The MCP queue
   (`list_action_items(status='proposed')`) and the SPA set `first_shown_at` the same way through one function
   (`commitments.mark_shown`). The same pass then sets `status='expired'` on `proposed` rows with
   `first_shown_at <= now - 3 days`, one `expire` event each (`source='system'`). Nothing expires lazily inside a
   reader. `apply_action` gains `approve` (proposed/expired/rejected/legacy -> `open`, `decided_at` set) and `reject`
   (proposed/expired -> `rejected`); both are journaled and undoable like `done`. Every event row carries
   `source in (bot|web|mcp|system)`; Ask 9 metrics are SQL over `action_item_events` (shown = distinct items with a
   `show` event, pressed = distinct items with `approve|reject`, review days = distinct dates of those in the last 7,
   active = `COUNT(status='open')`) returned as a `proposals` section of `weekly_digest`, not as a new MCP tool
   (tool cap 100, ADR-052; 96 today).
4. **Legacy flip, one command back.** v59 runs `UPDATE action_items SET pre_legacy_status = status, status = 'legacy'
   WHERE pre_legacy_status IS NULL` for every row present at migration time (open, stale, done, cancelled alike:
   Ask 7 says all 5484), records the count in the `schema_versions` description, writes no per-row events.
   Reverse: `python -m app.services.commitments unlegacy [--dry-run]` =
   `UPDATE action_items SET status = pre_legacy_status, pre_legacy_status = NULL WHERE status = 'legacy'`. One row
   returns by `apply_action(id, 'approve')` (journaled). `mark_stale`, `sweep_dropped` and `dedup` add
   `AND decided_at IS NULL` so an approved row is never auto-staled (Ask 8) and never auto-hidden; `dedup` runs over
   `proposed` rows too, so a repeat is not proposed twice. Approved rows older than 14 days or overdue reach the Monday
   digest with done / postpone / drop buttons (Ask 8) through the existing K5 card path.
5. **Extraction fields ride the existing calls.** `text_polishing` prompts for the meeting card (`extract_meeting_card`
   and the one-call card), comment analysis and `extract_chat_tasks` add to each action item
   `"for_whom": "owner" | "promised_to_owner" | "third_party"`, `"confidence": 0..1`, `"importance": 1 | 2 | 3`, with the
   owner's canonical name and aliases from the entity graph passed into the prompt so the model can tell "mine" from
   "theirs". No extra API call; the parsers default as in decision 2.

Deciding factor: option 1 is the only one where a decision is a status change on a row whose id never moves, so K5
undo, the v51 journal, `dup_of`, owner links and the eval golden set all keep working, and the undo of the whole
design is one `unlegacy` plus a writer flag back to `open`.

## Consequences
Easier: one table, one write path (`apply_action`) for every decision, metrics are a query over a journal that
already exists, the migration is reversible in one statement. Harder: eight status values, each enum and the SPA
chips must list them (a test over the enums is cheap and required); until the owner approves anything, every
`open`-default reader is empty, which is the intended fresh start but will look like a regression on day one; the
daily batch can hold the same 10 unpressed cards for three days while newer proposals wait (accepted: the owner's
own volume is 2-6 per day). Accepted debt: no fuzzy key until measured; filtered candidates (third party, low
confidence) are not stored, so lowering the threshold later needs a re-run; the v59 flip runs the moment the full
pytest or app.py touches the live DB with the new code, so the migration story backs up `whisper_history.db` first.

## Invariants created
- `status='open'` on `action_items` means "approved by the owner and alive"; a writer never inserts `open`, only
  `proposed`, and only `apply_action` moves a row between statuses, writing one `action_item_events` row in the same
  transaction.
- A writer deletes only its own rows with `status='proposed'`; a row in any other status is never deleted by
  extraction, and a candidate whose `(transcription_id, text_key)` matches an existing row in any status is not
  inserted.
- `first_shown_at` is set once, by the first show on any surface (bot, web, mcp), together with a `show` event; the
  3-day window counts from it, never from extraction, and expiry happens only in the daily pass, never in a reader.
- `mark_stale`, `sweep_dropped` and `dedup` never change a row with `decided_at IS NOT NULL`.
- `legacy` rows keep `pre_legacy_status`; `unlegacy` restores all of them and is the only bulk path out of `legacy`.
- Matching across re-runs is `transcription_id` + normalised text hash; a fuzzy key is added only after the
  measurement story reports a miss share, and never on embeddings.
- The owner's "mine / promised to me" verdict, confidence and importance come from the same extraction call that
  produces the task; no second model call per task.

## Revisit when
The measurement story reports more than 10 % of re-proposed candidates judged "the same task" (add Jaccard within the
record); the owner asks for filtered candidates back (add a `filtered` status instead of not inserting); a second
user or shared archive appears (`for_whom` relative to one owner stops being well-defined); the batch queue blocks
for days because unpressed cards hold the 10 slots (switch to "unshown first"); the tool cap forces the metrics into
their own tool.
