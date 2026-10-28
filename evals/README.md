# evals/ — recall@k / citation-rate harness for retrieval and RAG

**Why.** The hybrid retrieval thresholds (`app/services/retrieval.py`:
`RAG_RECENCY_WEIGHT`, `RAG_MAX_PER_MEETING`, RRF) and the RAG chat prompt
(`app/services/rag.py`) were tuned by manual runs. Without a reproducible
harness, any change to the prompt, chunking, model or weights means you learn about a
regression from a user complaint, not from a number. This directory is not a replacement for full-fledged
evals platforms, just a minimal "run N questions → compare before/after" script.

## Gate (`evals.gate`) — the recommended path for CI/local

`evals/run_eval.py` (below) is a manual research script. `evals/gate.py` is what
gives a PASS/FAIL verdict and is suitable for a mandatory run: it computes only FREE
retrieval (no Claude calls), always at TWO `k` values (8 and 12 — the lesson of
`eval-verdict-flips-with-k`: the verdict flips depending on a single k) and runs
on a DB SNAPSHOT (`VACUUM INTO` from a `mode=ro` connection, ADR-003) — the live file is never
opened for writing.

**How to lift this guarantee.** The only legal bypass is the combination `--no-snapshot
--yes-live`: it opens the live DB itself in WAL mode (`retrieval.search` goes through
`get_db_connection`, `PRAGMA journal_mode=WAL`) while the recorder and `telegram_listener.py`
write to it at the same time. The gate allows this ONLY when the target really is the live DB
(`Config.DATABASE`) — the same pair of flags pointed at a snapshot or any other
file still goes through a protective temporary copy (`_scratch_copy`): the measurement runs
against the copy, and the named path itself is never opened for search.

These are TWO DIFFERENT guarantees, and they must not be confused. On the snapshot path (below,
`VACUUM INTO` from a `mode=ro` connection) writing is physically impossible — SQLite itself
will refuse regardless of which code called it, this is an OS/DB-engine guarantee. On
the scratch-copy path it is a CODE guarantee, not an OS one: `_scratch_copy` simply copies the
file before anything opens it for search, and nothing physical stops
a future edit from mistakenly pointing `retrieval.search` directly at the named path.

### Command (on the live DB — auto-snapshot)

```bash
.venv/Scripts/python.exe -m evals.gate \
    --golden evals/golden_set.local.json \
    --db whisper_history.db \
    --k 8,12 \
    --baseline evals/baseline.local.json \
    -q
```

`--db whisper_history.db` (`Config.DATABASE`) is recognized as the live DB
(`evals/graph_links.py::is_live_db`) → the gate itself takes a snapshot
(`evals/snapshot.py::auto_snapshot`) into `evals/snapshots/<basename>-<YYYYMMDD-HHMMSS>.db`
and searches against it; the live DB itself is opened only `mode=ro` for `VACUUM INTO` — writing
to it is physically impossible, not merely by code convention. The same basename reuses an
already taken snapshot instead of a new 300+ MB file if the source (size+mtime) has not
changed since — a new snapshot and pruning of old ones of the same basename
happen only when the source has really moved. To force a run WITHOUT a snapshot (e.g. the
given path already is a snapshot) use `--no-snapshot`; on a file that matches
`Config.DATABASE` this is forbidden without an explicit `--yes-live` (the gate must run on a copy
by default, not on the live file "because it is faster").

Exit codes: `0` — PASS. `1` — EXCLUSIVELY a quality verdict: the `--min-recall` threshold is not met,
the mean dropped against `--baseline` by more than `--max-drop`, or an item went
hit→miss beyond `--max-item-regressions`. `2` — everything else (data/usage/
infrastructure): no golden file or DB, live DB without `--yes-live` with
`--no-snapshot`, invalid golden-set, no `status="labeled"` item evaluated at
some `k`, `--json-out` without `.local.` in the path name, a baseline taken under a different
provenance (a different snapshot or `vector_available`/`rerank`), an infrastructure failure during
the measurement (model, snapshot, disk).

### `--rerank` — measure the configuration that actually answers

By default the gate measures ranking WITHOUT the cross-encoder reranker, while the live
RAG chat works WITH it (`RECALL_RERANK_ENABLED=1` in `.env` → `rag.py`
`_RERANK_ENABLED` → `retrieval.search(rerank=True)`). A measurement on 17.09.2026 over 14
items: recall@8 **50.0% without the reranker vs 78.6% with it**, at k=12 — 78.6% in
both. So a run without the flag understates the live quality and, worse, any A/B
(embedding model, prefixes, chunking) compares a ranking that does not exist in production.

```bash
.venv/Scripts/python.exe -m evals.gate     --golden evals/golden_set.local.json --db whisper_history.db     --k 8,12 --rerank --json-out evals/run_<що_міряємо>.local.json -q
```

The default deliberately stayed `off`: otherwise all previously taken baselines and runs would have changed
meaning retroactively. Instead the run signs itself — the configuration
is printed in the verdict line (`[gate] PASS k=8,12 [rerank=on, rewrite=off]`,
the only line under `-q`) and goes into `--json-out` under the `provenance` key. A baseline
taken with a different `rerank` is not admitted to comparison (the same protection that already
guards the DB snapshot), so a baseline for the reranker must be taken separately
(`--write-baseline`), not by overwriting the old one.

`rewrite` in the provenance is not a gate flag: `retrieval.search` reads the hot
`RAG_QUERY_REWRITE` itself, and the run only records what it was taken with.

### Thresholds (`--min-recall`) — a collapse detector, not a quality criterion

The default `8=0.15,12=0.40` is not a number out of thin air but derived by the rule
`floor_0.05(measured recall@k of the calibration baseline − 2/n)`, where `n` is the number of
`labeled` items in the baseline (a margin of two set items: at `n=15` one item weighs
6.7 pp, a threshold narrower than the set's granularity would catch labeling, not ranking).
The actual margin is wider than these 2 items: `floor_0.05` also rounds the result down
to the 0.05 grid, adding up to a whole cell on top. On the `n=15` baseline this is not
13.3 pp (`2/15`), but 17.1 for `k=8`: the `0.15` threshold lies 17.1 pp below the
measured `recall@8=0.32143`, not 13.3.

**Policy on baseline drift (owner decision, delta D22 in
`docs/specs/eval-gate/plan.md` — private repository only, `docs/specs/` is not
part of the public mirror).** The rule above is applied at EVERY
baseline capture and gives an UPPER bound for that particular measurement, not the value
of the shipped default: the shipped default is the LOWEST of all values derived
from all baselines ever taken. The reason is that the corpus provably drifts in both
directions (the "short-lived baseline" section below), and the threshold stays a collapse
detector, not a quality criterion: pulling the number up with every new baseline
would mean catching a false red on the next downward drift rather than a real
ranking regression.

Measurement of 03.09.2026 ~22:00 (`n=15`, the current baseline, snapshot
`evals/snapshots/whisper_history-20260903-182053.db`; full provenance — private
repository only, `docs/specs/eval-gate/plan.md`, delta D22):
`recall@8=0.32143`, `recall@12=0.60714` → the rule gives
`floor_0.05(0.32143−2/15)=0.15`, `floor_0.05(0.60714−2/15)=0.45`. The shipped
`k=12` stays `0.40` — below this `0.45`: the previous baseline (a snapshot that
ended in `145235`, seven hours earlier the same day, since deleted by the
pruner) measured `recall@12=0.53571` → `floor_0.05(...)=0.40`, and that, the
lowest of the two, remains the shipped default.

The threshold catches a COLLAPSE (retrieval broke and returns nothing useful) — the same
round of measurements proved that an absolute threshold does NOT catch sabotage at all: at k=8 a
deliberately broken (`--sabotage`) ranking gave a HIGHER recall than the clean run
(39.3% → 42.9%, the same golden-set, a different snapshot of the same session). Sabotage is caught by
the per-item regression against the baseline — `--max-item-regressions` (default `0`): no item
may go from hit to miss relative to the baseline.

### Red run — checking the gate itself

`--sabotage shuffle|reverse` deterministically (seed 0) breaks the candidate POOL BEFORE
truncating to `k`: the head of the pool (the old top-k) and the tail are shuffled INDEPENDENTLY, then the tail
is placed before the head — no old top-k element survives into the new first `k`
(`reverse` gives the same guarantee trivially, by reversing the whole pool). Proven
on the default `--k 8,12`, not only on small test `k` — an early version (plain
`shuffle` over the whole pool) stayed silent (`rc=0`) precisely at 8/12, though it went red
at small `k`.

**A precondition without which the guarantee silently does not hold:** it is proven
for a pool of size `4k` — exactly what the gate REQUESTS (`pool_size = k*4`), not for the pool
`retrieval.search` will ACTUALLY return. On a thin archive or a narrow
`category_id` filter fewer candidates may come back. The minimum the guarantee
really requires is `2k`: both `shuffle` (the tail needs at least `k` elements outside the
old head) and `reverse` (the old index must go past the border of the new top-k)
break below this threshold. If the returned pool is thinner than `2k`, old top-k
elements may survive into the new first `k`, and `--sabotage` can print
PASS having broken nothing — the code neither checks nor warns about this condition.

An absolute threshold does not catch sabotage (section above) — the gate is obliged to return `1`
**against the baseline** (`--baseline`), via per-item regression or a drop in the mean:

```bash
.venv/Scripts/python.exe -m evals.gate \
    --golden evals/golden_set.local.json --db whisper_history.db \
    --k 8,12 --baseline evals/baseline.local.json --sabotage shuffle -q
# [gate] FAIL k=8,12 — k=8: mean_drop 0.071 > max-drop 0.020; item_regressions 2 > 0; ...
```

Measured 03.09.2026 ~22:20 on the current baseline (snapshot `182053`, the same golden-set,
`n=15`): `shuffle` — k=8 `item_regressions=2`, `mean_drop=0.071`; k=12
`recall_at_k=0.143`, `item_regressions=7`, `mean_drop=0.464`. `reverse` — k=8
`recall_at_k=0.107`, `item_regressions=3`, `mean_drop=0.214`; k=12
`recall_at_k=0.107`, `item_regressions=8`, `mean_drop=0.500`. Both modes
fail the gate (`1`) on the default `--k 8,12` — the full provenance remains only
in the private repository (`docs/specs/eval-gate/plan.md`, delta D22):
`docs/specs/` is not part of the public mirror, the numbers above are all the information
the public reader gets.

A non-zero exit here is exactly the criterion that the gate really checks something, rather than always
printing PASS.

### Golden-set format (JSONL, plan C1)

The modern format is one JSON object per line in a `.jsonl` file (the old
`golden_set.local.json`/`{"items": [...]}` is still read without conversion —
`evals/golden_io.py` recognizes the format by the file extension):

```json
{"id": "budget-decision-01", "question": "...", "slice": "calls", "category_id": null, "expected_transcription_ids": [101], "expected_source_name_contains": ["..."], "expected_facts": ["..."], "notes": "", "source": "manual", "status": "labeled", "candidates": []}
```

| Field | Purpose |
|---|---|
| `slice` | `"calls"` \| `"tg"` \| `"docs"` — the slice by which the gate computes a separate aggregation (`by_slice`) on top of the shared one. |
| `source` | `"mcp_log"` (mined by `build_golden.py` from real queries) \| `"manual"` \| `"legacy"` (migrated from the old JSON). |
| `status` | `"labeled"` — takes part in the metrics; `"unlabeled"` — skipped ENTIRELY (the search is not even run); `"negative"` — the search is run and stays in the per-item report, but is not included in `aggregate`/`by_slice` (deliberately empty `expected_*`, as opposed to not yet labeled). |
| `candidates` | A hint from `build_golden.py` (top-N `retrieval.search` at mining time) — NOT the truth; a human either confirms them into `expected_*` or ignores them. |

Task deadlines/owners are a separate format (plan C7), built by `evals/build_tasks_golden.py`
and checked by `evals/tasks_eval.py`:

```json
{"id": "task-0001", "action_item_id": 4821, "transcription_id": 512, "task": "...", "owner_name_raw": "...", "due_raw": "до кінця тижня", "meeting_date": "2026-08-20", "source": null, "truth": {"owner": null, "due_date": null, "due_precision": null}, "status": "unlabeled"}
```

`source: null` — the task is inherited from the Claude call card; `"tg_thread"` — extracted from
the conversation (`commitments.tg_tasks`). `truth.owner`/`truth.due_date`/`truth.due_precision`
are the manual truth; `tasks_eval.py` checks `commitments.parse_due(due_raw, meeting_date)` against
`truth.due_date` and the current `owner_entity_id` from the snapshot (canonical name + all aliases) against
`truth.owner` — without any Claude call (A3 of the plan: the accuracy of Claude's extraction itself
stays outside the scope of this eval).

### The workflow for filling the set

1. `.venv/Scripts/python.exe -m evals.build_golden --db <snapshot> --stats` — mines
   `logs/mcp_calls.log` (`RECALL_MCP_DEBUG_LOG=1`) into new `status="unlabeled"` items with
   candidates and prints a `slice × status` table with the deficit to the target (**≥50 per slice**
   — a DATA-filling target, not a code acceptance criterion, A2 of the plan).
2. Label manually: for each `unlabeled` item check `candidates` against the archive,
   fill `expected_transcription_ids`/`expected_source_name_contains`/
   `expected_facts`, set `"status": "labeled"` (or `"negative"` if there are deliberately no
   relevant sources — do not confuse with not yet labeled).
3. `.venv/Scripts/python.exe -m evals.gate --golden <file> --db <snapshot>
   --write-baseline evals/baseline.local.json` — record the current run as a new
   baseline once the set is stable.
4. Subsequent runs use `--baseline evals/baseline.local.json`: the gate compares both the mean
   and each item separately (hit→miss relative to the baseline).

The same workflow for tasks: `build_tasks_golden.py --stats` (target **≥30**, C7 of the plan) →
fill `truth.owner`/`truth.due_date`/`truth.due_precision`, `status="labeled"` →
`tasks_eval.py --golden ... --db ... --gate`.

### Calibration baseline — taking it, updating it, and why it is short-lived here

The baseline is taken with the same `--write-baseline` from step 3 above. Historically the absolute
threshold was disabled here (`--min-recall 8=0,12=0`), because the old default
(`8=0.60,12=0.75`) blocked writing the baseline before it even existed —
`--write-baseline` does not write a baseline on a FAIL verdict. The currently shipped default
(`8=0.15,12=0.40`) is derived from the first set of baselines ever taken
(the "Thresholds" section above — the rule gives an UPPER bound for each taken one, the default is the
lowest of all derived) and does not block against the current baseline — but it is
a number for the specific archive it was calibrated on, not a universal constant:
on another corpus (smaller size, different labeling) the actual recall may well
lie lower. Disabling the threshold when first taking a baseline remains a safe
habit, not a bypass of your own number:

```bash
.venv/Scripts/python.exe -m evals.gate \
    --golden evals/golden_set.local.json --db whisper_history.db \
    --k 8,12 --min-recall 8=0,12=0 --write-baseline evals/baseline.local.json
```

A baseline is valid only if it survives its own re-measurement: a second run of the same
command against the same snapshot with `--baseline evals/baseline.local.json` instead of
`--write-baseline` must give `0` and no per-item divergence. A baseline that cannot
be compared with itself is not a baseline: either the snapshot mutated during the measurement
(ADR-003 violated), or retrieval is nondeterministic — both cases mean there is
nothing to measure with yet. `evals/baseline.local.json` stays out of git (the file does not exist until
the first `--write-baseline` is run); the full provenance — date, snapshot identity,
`n`, `vector_available`, reranker state, measured aggregates — is recorded as a
delta in `docs/specs/eval-gate/plan.md` (private repository only —
`docs/specs/` is not part of the public mirror), the baseline file itself does not go there either.

**The baseline is short-lived on this machine.** `app.py` with the child `telegram_listener.py`
continuously tops up the archive with background ingest. `auto_snapshot` honestly detects the change of
the source and builds a NEW snapshot — after that the previous baseline reads as "a different
provenance", and the gate returns `2`, not `1`. This is NOT a ranking regression: a red gate
on this machine is first checked for provenance (the message `[gate] базова лінія ... знята за іншим провенансом`), and only if the provenance matches is the exit
read as a quality verdict. The practical consequence — the steps "take the baseline" and "compare against it"
should be run SEQUENTIALLY, in one script, without pauses between them: even in less than two
hours of downtime background ingest manages to shift recall@k by about one item
of the set (at `n=15` — 6.7 pp), which is exactly the quantity the two-item margin
from the "Thresholds" section is designed to absorb.

The pruner (`evals/snapshot.py::_prune_other_snapshots`) protects from deletion the snapshots
named by `--baseline`/`--write-baseline` of this run, AND the snapshot that the conventional baseline
file `evals/baseline.local.json` points to — regardless of whether the run
mentioned a baseline in its flags at all (`_protected_snapshot_paths` always checks the conventional
path, in addition to the flags passed). A run without any baseline flag (a sabotage run, a manual measurement) is no longer an orphan: a baseline physically lying
on disk will survive.

This does not resurrect what was pruned BEFORE this protection: last time (03.09 ~18:20)
`evals/baseline.local.json` after the repair still pointed to
`whisper_history-20260903-145235.db`, deleted by the pruner, and the baseline had to be retaken
with the same `--write-baseline` the same day ~22:00, on the snapshot `whisper_history-20260903-182053.db`.
Now the baseline file points to exactly this snapshot, and it still lies on disk
(`evals/snapshots/`) — the unconditional protection of the conventional path keeps it alive
until the baseline is overwritten with a new one.

**The S1 criterion and why there is no permanent regression gate here.** "main is green
on a clean run, red on sabotage" was first taken by a single run of a pair of steps
on 03.09.2026 ~18:20 (clean → `0`, `--sabotage shuffle` → `1`, `--sabotage reverse`
→ `1`) and RE-TAKEN the same day ~22:20 against the current baseline (snapshot `182053`) with the
same result: clean → `0`, `shuffle` → `1`, `reverse` → `1`. The criterion is
reproducible — but precisely against this preserved snapshot (the unconditional protection of the
conventional baseline file, above, keeps it on disk), not "in general, on
any future state of the archive".

This does not make a regression gate permanent on this machine. Sabotage is caught only by
the checks against the baseline (`--max-drop`/`--max-item-regressions`, the "Thresholds" section
above), and the baseline itself BETWEEN SESSIONS (not between runs within one) stays
short-lived (the section above): background ingest shifts the snapshot provenance in less
than two hours of downtime, and the next run with a different snapshot gives `2` (incomparable),
not a quality verdict. A permanent gate that survives a machine restart and archive drift
does not exist here and will not, until the corpus is frozen (S4, Linux CI): the only thing that
survives a restart is the absolute threshold (`--min-recall`), which, per the
"Thresholds" section above, does not catch sabotage.

### Gate privacy

In addition to `golden_set.local.json` (below) — all gate artifacts with real
questions, transcript IDs or names from the archive are gitignored by the `*.local.*` convention:

- `evals/*.local.jsonl` — golden sets (`golden_candidates.local.jsonl`,
  `tasks_golden.local.jsonl`, etc.).
- `evals/baseline*.local.json` — baselines.
- `evals/*.local.json` — the gate's `--json-out` (e.g. `evals/gate_run.local.json`):
  the full per-item result carries real questions and archive `transcription_id`s;
  `gate.py` itself refuses to write `--json-out` without `.local.` in the path name
  (exit `2`) — a file without this convention in the path will not pass. `evals/compare.py`
  reads these files (and optionally writes its own `--json-out` with the same ids/questions) —
  keep its output under the same `*.local.json` convention (compare does not check this
  itself — it is not a gate, trust is on the human who sets the path).
- `evals/snapshots/` — DB snapshots (`VACUUM INTO`).
- `evals/after_*.json`, `evals/before_*.json`, `evals/*_rerank.json` — results of
  manual `run_eval.py` runs.

Only synthetic examples (`golden_set.example.json`) stay in the repository — the same
principle as for the main golden-set below.

## Wave B — golden-set ≥30 and run comparison (`evals.compare`)

**Golden-set ≥30.** `evals/build_golden.py` (story 01, already in `main`) mines candidates
not only from `logs/mcp_calls.log` but also from the `/api/memory/ask` log (`--from-ask-log`) — real
questions that were already asked of the archive, not invented anew. `--stats` prints the
slice×status table and the deficit to the target of ≥30 labeled entries; `--merge` appends new
candidates to the existing `--out`, without overwriting already labeled ones (without `--yes-overwrite-labeled`).

**Comparing two gate runs (`evals.compare`).** The decision "switch the live
embedding model (or another config that affects ranking) or not" is made NOT by
a single number but by a table at BOTH `k` (8 and 12) + a per-item diff — the same lesson
`eval-verdict-flips-with-k`. The procedure (Integration gate of the Wave B plan, items 3-7):

1. Snapshot A (`VACUUM INTO`) on the current `.env` → gate with `--json-out a.local.json`.
2. Copy A → B, apply the new config (`EMBED_MODEL`/`EMBED_VERSION`, `reembed run --db B`
   etc.) → gate on B with the same golden-set and `--k` → `--json-out b.local.json`.
3. `python -m evals.compare a.local.json b.local.json --label-a e5 --label-b qwen3`.
4. Decide by the `k | A | B | Δ` table and the per-item diff (`hit→miss`/`miss→hit`) at BOTH k —
   not only by the mean. Compare does not issue a PASS/FAIL verdict — it is a report, the verdict is made by a human.

`compare.py` computes nothing itself and does not touch the DB — it only reads two already prepared
gate `--json-out` files and puts them side by side:

```bash
.venv/Scripts/python.exe -m evals.compare \
    evals/a.local.json evals/b.local.json \
    --label-a e5 --label-b qwen3 \
    --json-out evals/compare_run.local.json
```

It prints the provenance of both runs (DB snapshot path, golden-set, `k`, item count and
`recall_at_k` from the gate's `aggregate` — the embedding model/version/`rerank`/`RAG_QUERY_REWRITE`
are NOT recorded in the gate's `--json-out`, so the difference between A and B is named by `--label-a/--label-b`,
not by the file), then a table at each shared `k` and the `hit→miss`/`miss→hit` lists (id +
question, truncated to 80 characters). If the item sets differ between runs — a separate
warning and comparison over the intersection only; items present in only one run are marked as
"not compared". Exit `0` for any valid files (compare is a report, not a gate);
`2` — a file is unreadable or there is no shared `k` (a message in stderr, not a
traceback).

## Structure

```
evals/
  __init__.py
  metrics.py              — DETERMINISTIC metric math (recall@k, citation-rate).
                             No network/GPU/DB — hence testable offline
                             (tests/test_eval_metrics.py).
  run_eval.py              — CLI: runs the golden-set against the real archive
                             (retrieval.search + rag.answer_question) and prints a table.
  gate.py                  — CLI: PASS/FAIL retrieval gate on a DB snapshot (both k, baseline).
  build_golden.py          — CLI: mining golden-set candidates from logs (mcp/ask).
  compare.py               — CLI: two gate `--json-out` files side by side — Δ table + per-item diff
                             (Wave B, story 06). Computes nothing itself and does not touch the DB.
  golden_set.example.json  — golden-set TEMPLATE with synthetic entries
                             (NOT real transcripts) — copy and adapt.
  README.md                — this file.
```

## Golden-set format

A JSON object with an `"items"` key (a list) or just a list of entries. Each
entry:

```json
{
  "id": "budget-decision-01",
  "question": "Яке рішення ухвалили щодо бюджету на маркетинг у 2 кварталі?",
  "category_id": null,
  "expected_transcription_ids": [101],
  "expected_source_name_contains": ["Синк з маркетингом"],
  "expected_facts": [
    "бюджет на маркетинг Q2 збільшили до 50 000 грн",
    "відповідальна за виконання — Олена"
  ],
  "notes": "людський коментар — чому це хороший test-case"
}
```

All fields except `id`/`question` are optional; each metric in the aggregation is computed
only over entries where the corresponding criterion is set (an empty list = the criterion
is skipped, not a failure).

| Field | Purpose |
|---|---|
| `expected_transcription_ids` | Exact IDs of source transcripts → recall@k. **Brittle**: they change on archive re-ingest. |
| `expected_source_name_contains` | A more robust alternative — a substring of `source_name`. Does not depend on IDs. |
| `expected_facts` | Facts that MUST be in the answer. NOT checked automatically (no LLM-judge) — meant for manual eyeball review of `--json-out` and for the LLM-judge prompt (`--llm-judge`). |
| `category_id` | Restrict the search to a direction (int / `"none"` / `null`) — like the `retrieval.search` parameter. |

Negative cases (deliberately no relevant sources in the archive) — leave
`expected_transcription_ids`/`expected_source_name_contains` as empty
lists; they drop out of the recall@k aggregation, but `--json-out` lets you
manually check that the model honestly says "not found" rather than hallucinating.

See `golden_set.example.json` — three synthetic examples (a regular
case with one source, a case with several expected sources, and a negative
case).

## How to build YOUR OWN golden-set (from your archive)

1. Copy `golden_set.example.json` → `golden_set.local.json` (or any
   name outside git — see "Privacy" below).
2. Take 15-30 real questions that you (or your team) would actually ask the
   archive — including a few where there is deliberately NO answer (negative cases
   catch hallucinations).
3. For each question, manually find in your archive the correct `transcription_id`(s)
   and/or the source name (the "Ask the archive" UI or SQL).
4. Optionally — write out 1-3 facts the answer MUST contain (`expected_facts`)
   for the LLM-judge/manual review.
5. Run `run_eval.py` on this file — see below.

**Privacy.** `golden_set.local.json` (or any of your copies with real
questions/IDs from your archive) contains data about your real meetings —
**do NOT commit it**. Only the synthetic
`golden_set.example.json` stays in the repository. Add your file to `.gitignore` (for example
`evals/*.local.json`) if you plan to keep several versions.

## When to run

- **Before a release** — a regression check that the latest changes did not worsen
  recall@k / citation-rate relative to the previous run.
- **When changing** `app/services/retrieval.py` (RRF weights, recency, diversification),
  `app/services/rag.py` (system prompt, chunking), or the default model
  (`app/services/models.py`) — run BEFORE and AFTER, compare `--json-out`.
- **NOW it CAN be run in CI (retrieval-only)** — `evals.gate` (the "Gate" section above)
  computes only free retrieval (no GPU/key: e5-large locally, otherwise
  an automatic FTS fallback) and returns a non-zero code on regressions, so it can be
  built into the quit-gate on every commit/PR (`CLAUDE.vulyk.md ## Commands`, the
  `eval` row). Paid modes are still a NO: a full `run_eval.py` run with answers
  (`rag.answer_question`) and especially `--llm-judge` cost real Claude tokens —
  run them manually/as needed, do not build them into a mandatory pre-commit
  or CI gate.

## Running

```bash
# 1) Retrieval only — FREE, no ANTHROPIC_API_KEY.
#    Checks only recall@k / source_name_hit_rate (no citation-rate,
#    since there is no answer).
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json \
    --db whisper_history.db \
    --retrieval-only

# 2) Full run: retrieval + answer generation via rag.answer_question
#    (a real Claude API call — PAID, requires ANTHROPIC_API_KEY in .env).
#    Adds citation_rate / citations_valid_rate.
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json \
    --db whisper_history.db \
    --json-out evals/last_run.json

# 3) + LLM-judge of relevance — an ADDITIONAL Claude call for EVERY question
#    (even more expensive). Off by default, enabled explicitly with a flag.
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json \
    --llm-judge

# 4) T6.4: before/after local cross-encoder rerank (bge-reranker-v2-m3,
#    FREE, GPU/CPU — lazy-loads the model on first call). Run the
#    SAME golden-set TWICE (without and with --rerank), compare
#    recall@k/source_name_hit_rate/citation_rate:
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json --db whisper_history.db \
    --retrieval-only --json-out evals/before_rerank.json
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json --db whisper_history.db \
    --retrieval-only --rerank --json-out evals/after_rerank.json
```

Flags: `--top-k` (default 8, as in `retrieval.search`), `--model` (override of the
default model from `app/services/models.py`), there is no separate `--category-id`
flag — it is set PER ENTRY of the golden-set (`"category_id"`), because different
questions may concern different directions. `--rerank` (T6.4) — enable the
local cross-encoder rerank for this run: with `--retrieval-only` it is
passed directly to `retrieval.search(rerank=True)`; without it, it sets
`RECALL_RERANK_ENABLED=1` before importing `app.services.rag`, so the full
run (`rag.answer_question`) also reranks — the same path as the production
RAG chat (`app/services/rag.py`).

## Interpreting the metrics

| Metric | What it means | Source |
|---|---|---|
| `recall@k` | The mean share of `expected_transcription_ids` found in the top-k retrieved (over entries where this field is set) | `retrieval.search` |
| `source_name_hit_rate` | The share of entries where the top-k contained a source with the expected name (substring) | `retrieval.search` |
| `citation_rate` | The share of answers containing at least one `[n]` citation (required by the RAG system prompt) | `rag.answer_question` |
| `citations_valid_rate` | The share of answers where ALL `[n]` citations refer to fragments actually provided (1..found) — catches a "hallucinated" source number | `rag.answer_question` |
| `llm_judge_score` (optional) | The mean 1-5 score from a Claude judge (relevance + coverage of `expected_facts`) | a separate Claude call, `--llm-judge` |

Compare BEFORE/AFTER via `--json-out` (two files, `python -m evals.compare` —
the "Wave B" section above) — this harness deliberately does not make an automatic regression gate
(a threshold of "X% drop = fail"), because on a small golden-set (15-30 questions) such a
threshold easily fires falsely. Look at the per-entry table (`per_item`
in the JSON) — wherever `recall_at_k` fell to 0 or `citations_valid` became
`false`, that is where to dig.

## The deterministic part without API/GPU

`evals/metrics.py` — pure functions (`recall_at_k`, `source_name_hit`,
`has_citation`, `citations_in_range`, `evaluate_item`, `aggregate`), with no
network call or DB access whatsoever. Covered by
`tests/test_eval_metrics.py` on synthetic data (lists of chunk dicts,
answer strings) — no real `ANTHROPIC_API_KEY` or GPU is needed.
This is the part that is EASY and NECESSARY to keep in a regular `pytest` run;
`run_eval.py` (real retrieval/RAG calls) is separate, a manual run.

## Task re-run measurement (`evals.task_rerun_measure`)

ADR-056 matches a re-extracted task to a decided one by an EXACT key
(`task_proposals.text_key`: sha1 of NFC, casefolded, punctuation-stripped,
whitespace-collapsed text) within one record. Whether that is enough when the model
rephrases a task on a re-run is measured here, not assumed.

The script samples records that already hold meeting-card tasks (`action_items.source IS
NULL`; Telegram threads and comments are not sampled), re-runs `extract_meeting_card` with
the owner's names, and writes NOTHING to the DB (it opens it `mode=ro`). For each new candidate
the report lists the nearest existing task of the same record, `exact_hit`, the token
Jaccard over `commitments._tokens` (a report column only, never a production key),
`for_whom`, `confidence`, a `missing_fields` flag and an empty `verdict` column for the hand
pass. The summary gives candidates, exact hits and the non-compliance share.

```bash
# cost first - no Claude call
.venv/Scripts/python.exe -m evals.task_rerun_measure --db evals/snapshots/<file>.db --sample 30 --dry-run
# the paid run (needs a working ANTHROPIC_API_KEY, otherwise exit 2)
.venv/Scripts/python.exe -m evals.task_rerun_measure --db evals/snapshots/<file>.db --sample 30 \
    --out docs/specs/task-approval/rerun.local.md
```

Exit codes: `0` done, `2` usage / no DB / no key, `3` every extraction failed. A miss share
above 10 % opens the next spec (Jaccard within the record), never embeddings.
