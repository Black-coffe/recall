---
domain: task-approval
tags: [tasks, action-items, approval, adr-056]
related: [adr/056-task-proposals-in-action-items.md, specs/task-approval/brief.md, approval-gate.md]
last-verified: 2026-10-27
---

# Task approval: proposals are statuses on `action_items`

Every task the AI finds starts as a proposal and becomes a live task only after the owner
presses Yes. The design and its reasons are in [ADR-056](../adr/056-task-proposals-in-action-items.md);
the contract is the [spec](../specs/task-approval/brief.md). This note is for the reader who
opens the code later.

## The eight statuses and who moves a row

`commitments.STATUSES` is the single list. A test (`tests/test_task_status_enum.py`) fails if a
surface names a different set.

| Status | Set by | Moved out by |
|---|---|---|
| `proposed` | the three writers (meeting card, Telegram thread, comment) | Yes (`approve`), No (`reject`), the daily pass (`expired`) |
| `open` | only `approve` (Yes), never a writer | done, cancelled, stale |
| `done` | owner or agent (bot, `/tasks`, MCP) | reopen (`open`) |
| `cancelled` | owner or agent | reopen (`open`) |
| `stale` | `mark_stale` / `sweep_dropped`, only on rows with `decided_at IS NULL` | owner action |
| `rejected` | `reject` (No) | `approve` |
| `expired` | `expire_proposals`, the daily pass, 3 days after the first show | `approve` / `reject` |
| `legacy` | migration v59, once, for all rows that existed then | `approve` (one row), `unlegacy` (all) |

Every decision and status change writes one `action_item_events` row in the same transaction.

## Exact-key tombstone and its limit

Before a writer inserts a candidate, it computes `text_key` (`task_proposals.text_key`): NFC,
casefolded, punctuation removed, whitespace collapsed, then sha1. If a row with the same
`(transcription_id, text_key)` exists in any status, the candidate is not inserted. A re-run of
extraction deletes only its own `proposed` rows, so a decided task is never re-proposed.

The limit: the key is exact after normalisation. A reworded task is a new candidate and is
proposed again. Fuzzy matching is deliberately not built; it is added only if a measurement
shows a miss share above 10 %, and never on embeddings (see ADR-056).

## Day one

After migration v59 every existing row is `legacy`, so every reader whose default is `open`
(the digest, `/tasks`, MCP `list_action_items`) is empty until the owner approves something. This
is the intended fresh start, not a regression. The 3-day window counts from the first show, so
nothing expires before it has been shown.

## Undo

- Bring one row back: `task(approve, item_ids=[id])` (journaled, undoable).
- Bring all `legacy` rows back: `python -m app.services.commitments unlegacy [--dry-run] [--db PATH]`.
  It sets `status = pre_legacy_status` and clears it for every row in `legacy`.
- Stop new proposals: there is no runtime switch in the tree. Reverting the writers to insert
  `open` is a code change in the three writers, followed by `unlegacy` if the old state is wanted.

## Where the status lists live

- `app/services/commitments.py`: `STATUSES`, `DECISION_ACTIONS`, `STATUS_ACTIONS`, the transitions.
- `app/blueprints/memory.py`: the `status=` filter and the PATCH body.
- `mcp_server.py`: `_LIST_TASK_STATUSES` and the `task` tool.
- `static/js/recall/views/tasks.js`: the chips and the approve / reject gates.
- `app/services/pipes_cards.py`: the proposal card and its expired line.

Add a status by adding it to `STATUSES` and then to every surface above. The guard test lists them.
