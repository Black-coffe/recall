# ADR-020: The eval gate never compares mismatched provenance; a separate script diffs two runs

- Status: proposed
- Date: 2026-09-17
- Spec: docs/specs/production-rag-wave-b

## Context

From `docs/specs/production-rag-wave-b/brief.md`, `## Answers`, item 2:

> 2. **Чем доказываем, что новая модель лучше** — Отдельный скрипт сравнения двух прогонов: два
> снимка БД (старые векторы и переembedded), гейт по каждому с `--json-out`, сводка таблицей
> per-item на двух k. Сам гейт не трогаем: его отказ сравнивать разный провенанс — защита,
> добавленная в Хвиле A именно против тихого сравнения яблок с апельсинами.

And `## Asks`, item 4:

> 4. Боевая база переключается на новую модель только после сравнения двух прогонов eval-гейта на
> двух k (8 и 12) с per-item diff.

## Options

1. Modify `gate.py` itself to accept two provenances (e.g. current embeddings vs. a re-embedded
   snapshot) and compare them within one run. Rejected: the gate's refusal to compare mismatched
   provenance is a protection added in Wave A specifically against silently comparing apples to
   oranges; building A/B comparison into the gate would mean deliberately overriding that
   protection.
2. Switch the production `.env` to the candidate model/version pair directly, without a formal
   two-run comparison. Rejected — Ask 4: "Боевая база переключается на новую модель только после
   сравнения двух прогонов eval-гейта на двух k (8 и 12) с per-item diff."
3. A separate script (`evals/compare.py`) that runs the existing gate once per snapshot/config and
   diffs their two `--json-out` files afterward, with no verdict of its own. Chosen.

## Decision

`evals/gate.py` is not modified to compare across configurations; its refusal to compare mismatched
provenance (the Wave A protection against silently comparing apples to oranges) stays intact.
Judging whether one configuration (embedding model/version, or a feature flag like query rewrite)
is better than another is the job of a separate tool, `evals/compare.py`, which consumes two
independent `--json-out` files from separate gate runs and reports per-item/per-k deltas without
issuing a pass/fail verdict of its own. The production `.env` is only switched after this
comparison, per Ask 4.

## Consequences

Easier: the gate's single-provenance guarantee (and whatever protects the snapshot it reads, e.g.
ADR-003's mutation rule) never has to reason about cross-config comparisons, so adding `compare.py`
carries no risk to that invariant. Harder: proving a new configuration is better always costs two
separate gate runs plus a compare step; there is no single command that answers "does B beat A".
Known debt, not resolved by this decision: `compare.py`'s inputs carry each run's own provenance
side by side, but `gate.py` itself does not record whether a run was made with `RAG_QUERY_REWRITE`
on — the plan explicitly leaves this a one-line fix owed to Wave C, so today an operator must label
runs manually via `--label-a/--label-b`.

## Invariants created

`evals/gate.py` never accepts or compares two different `(model, version)` pairs or feature-flag
configurations within a single run. Any tool that judges "is configuration B better than
configuration A" consumes two of the gate's own `--json-out` outputs from separate runs — it never
changes the gate's single-provenance contract.

## Revisit when

A future wave needs the gate to emit machine-readable config provenance for automated comparison
(rather than an operator's manual `--label-a/--label-b`) — at that point close the Wave C debt
(recording the `rewrite` flag in gate provenance) together with whatever broader gate-provenance
work motivates it, rather than patching it a second time in isolation.
