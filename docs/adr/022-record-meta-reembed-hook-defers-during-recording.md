# ADR-022: Re-embed after a metadata edit runs through a stdlib-only hook, deferred by timer
while a recording is active — never holding a `job_queue` slot

- Status: proposed
- Date: 2026-09-21
- Spec: docs/specs/editable-title-description

## Context

From `docs/specs/editable-title-description/plan.md`, `## Tradeoffs`:

> **Хук `record_meta.after_meta_update` (01, no-op) замість виклику `reembed` прямо з PATCH**
> проти «RAG-історія теж править `transcription.py`»: три історії на одному файлі означали б три
> хвилі; хук дає 02 і 03 в одній хвилі без спільного файлу. Ціна — одна функція-заглушка на два
> дні життя.
>
> **Відкладення re-embed таймером** (слот `job_queue` вільний) проти «задача чекає в слоті»:
> `ThreadPoolExecutor(max_workers=2)` ділять транскрипція і фіналізація — сплячий re-embed під час
> запису відібрав би половину пулу. Ціна — модульний набір «очікуючих» id з локом.

From the same plan.md, `## Contracts`, C2/C4 (as amended by the round-2 remediation, story 08):

> `after_meta_update(transcription_id: int, *, changed: bool, db_path: str) -> None`: при
> `changed` → `reembed.schedule_record_reembed(transcription_id, db_path=db_path)` (lazy import);
> інакше no-op.
>
> `reembed.schedule_record_reembed(transcription_id: int, *, db_path: str) -> str` → `"queued"`
> (задача в `job_queue`, kind `reembed_record`), `"deferred"` (запис активний — повтор таймером,
> слот `job_queue` не зайнятий), `"pending"` (уже чекає — згорнуто), `"skipped"` (mute-режим/нема
> torch, лог).

## Options

1. **`record_meta.after_meta_update` hook (stdlib-only, lazy-imports `reembed`), timer-deferred
   while recording** (chosen) — one throwaway no-op function shared by stories 02/03 in the same
   wave with no common file; re-embed never competes with the recording pool for a slot.
2. **Call `reembed` directly from the PATCH handler, and from every other story that touches
   `transcription.py`** — simpler call graph, but three stories editing one file would have forced
   three separate waves instead of one.
3. **Submit the deferred re-embed to `job_queue` immediately and let it wait its turn** — simpler
   state machine (one queue, no separate "pending"/timer bookkeeping), but
   `ThreadPoolExecutor(max_workers=2)` is shared with transcription and finalization; a sleeping
   re-embed job occupying a queued slot during a live recording would take half the pool.

## Decision

`record_meta.update_meta()` never calls `reembed` itself. On a changed field it calls
`record_meta.after_meta_update(transcription_id, changed=True, db_path=...)`, which lazily
imports `app.services.reembed` and calls `schedule_record_reembed(transcription_id,
db_path=db_path)`. That function checks whether a recording is currently active: if so, it
returns `"deferred"` and re-arms itself via `threading.Timer` without occupying a `job_queue`
slot; otherwise it submits a `job_queue` job of kind `reembed_record` and returns `"queued"`. A
second edit to the same record while one is already pending collapses into `"pending"` — the id is
removed from the pending set at the start of the job body, before the row is read, so a caller
that received `"pending"` is guaranteed to be covered by a job that reads the row after that call.

## Consequences

`transcription.py` stays untouched by this spec — the hook lets stories 02 (creation paths) and
03 (RAG prefix/reembed) work in the same wave without a shared file to serialize on. The cost is
one no-op placeholder function living for the two days between story 01 and story 03, and a small
module-level "pending ids" set guarded by a lock to implement the deferral and collapse-on-repeat
behavior. A live recording never loses half its thread pool to a background re-embed; the price is
one additional state (`"deferred"`) and a timer re-arm loop instead of a queued-and-waiting job.

## Invariants created

- A metadata-mutation side effect (re-embed, and any future equivalent background work triggered
  by editing a record) is dispatched through `record_meta.after_meta_update`, never called
  directly from an HTTP handler or from another story's business logic.
- Work that would contend with the recording `ThreadPoolExecutor` (`max_workers=2`) for a slot
  must defer by timer with no `job_queue` slot held while a recording is active — it must not
  queue-and-wait.
- A call that returns `"pending"` is guaranteed to be covered by a job reading fresh state; no
  caller may treat `"pending"` as "already stale, ignore."

## Revisit when

A future consumer of `after_meta_update` needs synchronous completion (the caller must know the
side effect finished before it returns) — the current contract is fire-and-forget/best-effort and
would need a different mechanism. Also revisit if the recording pool's capacity changes from
`max_workers=2` enough that a queued-and-waiting job would no longer risk starving live
transcription/finalization.
