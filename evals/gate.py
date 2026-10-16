#!/usr/bin/env python
"""Retrieval gate on a DB snapshot (eval-gate, story 01): `python -m evals.gate`.

One k is not enough (lesson `eval-verdict-flips-with-k`: a regression at k=8, a
gain at k=12): the gate always computes BOTH k and fails if either does not
pass the threshold or regresses against the baseline. It runs on a SNAPSHOT
(`VACUUM INTO`, ADR-003); the live DB is opened only to take a
snapshot from it (`evals/snapshot.py`), and `retrieval.search` never sees the live
file directly unless explicitly told otherwise (`--no-snapshot --yes-live`).

Retrieval-only: there is no Claude call (`rag.answer_question`) here;
LLM-dependent metrics stay in `evals/run_eval.py` (A1 of the plan, paid,
manual run).

Exit codes (plan C3, extended D2/D3): `0` - passed; `1` - EXCLUSIVELY a quality
verdict (threshold, drop against the baseline, item regression); `2` - everything else: no
golden, live DB without `--yes-live`, invalid golden-set, no
`status="labeled"` item scored at some k, an infrastructure failure during
measurement (model, snapshot, disk), a baseline of a different provenance.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Allows running both as `python evals/gate.py` and as `python -m evals.gate`
# without installing the package - the same trick as in evals/run_eval.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.core import settings as _settings  # noqa: E402
from app.services import categories as _categories  # noqa: E402
from evals import golden_io  # noqa: E402
from evals import metrics as ev_metrics  # noqa: E402
from evals import snapshot as ev_snapshot  # noqa: E402
from evals.graph_links import is_live_db, open_readonly  # noqa: E402

#: D15.3 - thresholds derived from the calibration baseline (`evals/baseline.local.json`),
#: not made up. Rule: `floor_0.05(measured recall_at_k - 2/n)`, where
#: `n` is the number of `labeled` items of the baseline (`_derive_min_recall_threshold`
#: below is the same function, called here by hand on the measured numbers, because
#: the baseline file is outside git and does not exist until the first `--write-baseline`). The margin is
#: TWO items: at `n=15` one item weighs 6.7 pp, a threshold narrower than
#: the set's granularity would catch labelling, not ranking.
#: Measurement of 03.09.2026 (`evals/golden_set.local.json`, n=15, `--k 8,12`,
#: snapshot `whisper_history-20260903-145235.db` - the archive is live and drifts
#: while the telegram listener works, so this is the snapshot that ACTUALLY survived
#: its own re-measurement and sabotage, not the first one taken in the session): recall_at_k@8=
#: 0.32143, recall_at_k@12=0.53571 -> threshold@8 = floor_0.05(0.32143 - 2/15) =
#: floor_0.05(0.18810) = 0.15; threshold@12 = floor_0.05(0.53571 - 2/15) =
#: floor_0.05(0.40238) = 0.40.
#: THIS IS A COLLAPSE DETECTOR, not a quality criterion and not a sabotage detector (D15.4):
#: sabotage at k=8 is in principle not caught by an absolute threshold (measurement of 03.09: clean
#: 39.3% -> sabotage 42.9%, above clean, on another snapshot of the same session).
#: Sabotage is caught by the item-level regression against the baseline (`--max-item-regressions`,
#: default 0), not by this threshold.
_DEFAULT_MIN_RECALL = "8=0.15,12=0.40"
_DEFAULT_MAX_DROP = 0.02
_DEFAULT_MAX_ITEM_REGRESSIONS = 0

#: Repair round 1 (story 13, finding on closing story 15): the path at
#: which all the `evals/README.md` examples write and read the baseline, when
#: the --baseline/--write-baseline flags of THIS RUN say nothing about it. Without this
#: conventional fallback a run without flags (a sabotage
#: run, a manual measurement, any `python -m evals.gate --db ...`) did not see
#: the live baseline at all, and the pruner freely destroyed the snapshot it
#: points to (this is how S1 died). The path is deliberately relative to cwd, the same
#: as the `--db` default (`whisper_history.db`): the gate is documented as run from
#: the repository root.
_CONVENTIONAL_BASELINE_PATH = os.path.join("evals", "baseline.local.json")


def _floor_grid(x: float, grid: float = 0.05) -> float:
    """`floor(x/grid)*grid`, robust to float error: without an epsilon
    `0.30/0.05` gives `5.999999999999999` (not `6.0`), and a boundary value
    falls into the neighbouring cell below (`0.30` -> `0.25` instead of `0.30`)."""
    return math.floor(x / grid + 1e-9) * grid


def _derive_min_recall_threshold(recall_at_k: float, n: int) -> float:
    """D15.3: the rule for deriving the default `--min-recall` from the calibration
    baseline - `floor_0.05(recall_at_k - 2/n)`. A pure function over the baseline's
    numbers (does not read the file itself), so a test can check THE RULE on
    synthetic `recall_at_k`/`n`, not on re-typed constants."""
    return _floor_grid(recall_at_k - 2.0 / n)


def _protected_snapshot_paths(*baseline_paths: Optional[str]) -> list[str]:
    """Snapshots named by live baselines (D20/finding 5); `auto_snapshot`
    must not remove them until the baseline is overwritten. `--baseline` (what we
    compare against) and `--write-baseline` (the target this run MAY not overwrite
    - D12: it is written only on PASS) both count as live baselines. A broken or
    missing baseline file simply adds nothing to the protection (does not crash the gate).

    Repair 1: `_CONVENTIONAL_BASELINE_PATH` (`evals/baseline.local.json`)
    is always checked, in addition to the passed `baseline_paths`: a run that
    named no flag still must not destroy a baseline that physically lies
    on disk by the README convention."""
    protected: list[str] = []
    seen: set[str] = set()
    for path in (*baseline_paths, _CONVENTIONAL_BASELINE_PATH):
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        snap_path = (data.get("db_snapshot") or {}).get("path")
        if snap_path:
            protected.append(snap_path)
    return protected


def _parse_k_list(spec: str) -> list[int]:
    ks = [int(x) for x in spec.split(",") if x.strip()]
    if not ks:
        raise ValueError("--k: empty list")
    return ks


def _parse_min_recall(spec: str) -> dict[int, float]:
    """`"8=0.60,12=0.75"` -> `{8: 0.60, 12: 0.75}`. A k with no entry in the map:
    the recall_at_k threshold for it is simply not checked."""
    out: dict[int, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        k_str, _, v_str = part.partition("=")
        out[int(k_str)] = float(v_str)
    return out


@contextlib.contextmanager
def _scratch_copy(db_path: str) -> Iterator[str]:
    """A temporary copy of the snapshot for measurement (D6/ADR-003): `retrieval.search`
    goes through `get_db_connection` (`PRAGMA journal_mode=WAL`), which mutates the file
    merely by opening it. The copy takes this side effect upon itself: the file that the
    gate reports as `db_snapshot` stays byte-for-byte unchanged. A deliberate
    copy of the pattern `evals/graph_links.py::_scratch_copy` (we do not import
    a private name from a neighbouring module: the same approach as
    `evals/build_golden.py::_scratch_copy`)."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # copyfile stays INSIDE the try; otherwise a copy failure (disk
        # full, permissions) leaves an empty mkstemp stub on disk forever.
        shutil.copyfile(db_path, tmp_path)
        # Finding 13: if the source carries `-wal`/`-shm`, a copy without them is an
        # older state than committed (the WAL holds transactions not yet
        # flushed into the main file). The copy must represent the same
        # committed content as the file whose identity the gate reports.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path + suffix
            if os.path.exists(sidecar):
                shutil.copyfile(sidecar, tmp_path + suffix)
        yield tmp_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path + suffix
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError as exc:
                    print(f"[gate] failed to remove the temporary file {candidate}: {exc}",
                          file=sys.stderr)


def _sabotage_pool(pool: list[dict], mode: str, k: int) -> list[dict]:
    """Deterministically (seed 0) breaks the candidate POOL BEFORE cutting to k
    (D1): it is guaranteed to push EVERY one of the pool's first k positions outside the new
    first k, at any k (proved by a test on the default 8,12, not only
    on small ones). `reverse` on a `4k` pool always satisfies this (old index
    `i` -> new index `4k-1-i` >= `4k-1-(k-1)` = `3k` >= `k`). `shuffle` now
    guarantees it too, deliberately: the pool's first k positions ("head") and the rest
    ("tail") are shuffled INDEPENDENTLY, then the tail is placed BEFORE the head, so no
    old top-k element lands in the new first k as long as the pool has at least
    k elements outside the old head (satisfied: pool = 4k). The old implementation
    (`random.Random(0).shuffle` over the whole pool) did not give this guarantee:
    at the default k=8/12 the old top-0 survived into the new top-k (finding 1)."""
    pool = list(pool)
    if mode == "reverse":
        pool.reverse()
        return pool
    if mode == "shuffle":
        head, tail = pool[:k], pool[k:]
        rnd = random.Random(0)
        rnd.shuffle(head)
        rnd.shuffle(tail)
        return tail + head
    raise ValueError(f"unknown --sabotage {mode!r}")


def _search(db_path: str, item: dict, k: int, *,
            sabotage: Optional[str], rerank: bool = False) -> tuple[list[dict], Optional[bool]]:
    """Without sabotage (D4/finding 5): the same `top_k` as production: `k`, not
    a slice of k from a `k*4` request (the comments ceiling `_cap_comments` is computed from
    `top_k`, so a larger request lets into the top what production would not have
    let in). Sabotage stays free to ask for a larger pool: it is a tool, not
    a measurement (Non-goals).

    `rerank` (18.09.2026): the second stage of `retrieval.search` with the same
    cross-encoder that the RAG chat enables (`rag.py` `_RERANK_ENABLED` from
    `RECALL_RERANK_ENABLED`). The default stays False so that old baselines and
    runs do not change meaning retroactively; the operator enables it with the
    `--rerank` flag, and the run signs itself in the provenance. We do not pass `rerank_pool_size`:
    production does not either, i.e. the pool is taken from the default of
    `retrieval` and the measurement does not diverge from production here either."""
    from app.services import retrieval

    if sabotage:
        pool_size = k * 4
        res = retrieval.search(
            db_path, item["question"], top_k=pool_size,
            category_id=item.get("category_id"), tags=item.get("tags"), rerank=rerank,
        )
        chunks = _sabotage_pool(res["chunks"], sabotage, k)
    else:
        res = retrieval.search(
            db_path, item["question"], top_k=k,
            category_id=item.get("category_id"), tags=item.get("tags"), rerank=rerank,
        )
        chunks = res["chunks"]
    return chunks[:k], res.get("vector_available")


def _filter_leak(conn: sqlite3.Connection, item: dict, chunks: list[dict]) -> Optional[int]:
    """C8: how many chunks of the results belong to a record the filter should have kept out
    - proves that `category_id`/`tags` do not let foreign records through (complements
    `recall_at_k`, which catches only "hides its own"). `None` if the item
    sets no axis (nothing to check). This is why it is not a method of
    `ev_metrics.evaluate_item` (`evals/metrics.py` is not in the `## Files` of this
    story): a pure function over the search result and the same `conn` the
    gate already reads the golden-set with."""
    category_id = item.get("category_id")
    tags = item.get("tags") or []
    has_category_axis = isinstance(category_id, int)
    if not has_category_axis and not tags:
        return None

    tids = sorted({ch["transcription_id"] for ch in chunks
                   if ch.get("transcription_id") is not None})
    if not tids:
        return 0
    ph = ",".join("?" * len(tids))

    subtree = None
    if has_category_axis:
        subtree = set(_categories.subtree_ids(conn, category_id))

    tag_keys = {_categories.norm_name(t) for t in tags} if tags else None
    tags_by_tid: dict[int, set] = {}
    if tag_keys:
        for tid, name in conn.execute(
                f"SELECT rt.transcription_id, tg.name FROM record_tags rt "
                f"JOIN tags tg ON tg.id = rt.tag_id WHERE rt.transcription_id IN ({ph})",
                tids):
            tags_by_tid.setdefault(tid, set()).add(_categories.norm_name(name))

    cat_by_tid: dict[int, Optional[int]] = {}
    if subtree is not None:
        cat_by_tid = {row[0]: row[1] for row in conn.execute(
            f"SELECT id, category_id FROM transcriptions WHERE id IN ({ph})", tids)}

    leak = 0
    for ch in chunks:
        tid = ch.get("transcription_id")
        if tid is None:
            continue
        leaks_category = subtree is not None and cat_by_tid.get(tid) not in subtree
        leaks_tags = bool(tag_keys) and not (tags_by_tid.get(tid) or set()) & tag_keys
        if leaks_category or leaks_tags:
            leak += 1
    return leak


def _item_hit(row: dict) -> Optional[bool]:
    """"Passed" per item: all set criteria (recall_at_k full,
    source_name_hit True) are met. `None` if no criterion is
    set (e.g. `status="negative"`): such an item takes part neither in the
    threshold nor in the regression against the baseline."""
    recall = row.get("recall_at_k")
    name_hit = row.get("source_name_hit")
    if recall is None and name_hit is None:
        return None
    ok = True
    if recall is not None:
        ok = ok and recall >= 0.999
    if name_hit is not None:
        ok = ok and bool(name_hit)
    return ok


def _run_k(golden_items: list[dict], db_path: str, k: int, *,
           sabotage: Optional[str],
           rerank: bool = False,
           leak_conn: Optional[sqlite3.Connection] = None
           ) -> tuple[list[dict], dict, dict, Optional[bool]]:
    """Items with `status="unlabeled"` are skipped entirely (C1); `negative`
    is counted (search runs, it stays in the per-item report) but does not
    enter `aggregate`/`by_slice` (recall_at_k/source_name_hit are `None`,
    `aggregate()` already excludes `None`). The fourth element is the `vector_available`
    of this run (D5/D6, finding 6): taken from the first search call that
    returned it: a stable property of the run (the model is either there or not),
    not per item.

    `leak_conn` (C8) is a read-only connection to the same DB being measured
    (`measure_db`): `row["filter_leak"]` is `None` for items without
    `category_id`/`tags`, otherwise the number of result chunks outside the filter."""
    rows = []
    vector_available: Optional[bool] = None
    for item in golden_items:
        if item["status"] == "unlabeled":
            continue
        chunks, vec_avail = _search(db_path, item, k, sabotage=sabotage, rerank=rerank)
        if vector_available is None:
            vector_available = vec_avail
        row = ev_metrics.evaluate_item(item, chunks)
        row["slice"] = item["slice"]
        row["status"] = item["status"]
        row["hit"] = _item_hit(row)
        row["filter_leak"] = (_filter_leak(leak_conn, item, chunks)
                               if leak_conn is not None else None)
        rows.append(row)

    labeled = [r for r in rows if r["status"] == "labeled"]
    agg = ev_metrics.aggregate(labeled)
    by_slice = {}
    for slice_name in sorted({r["slice"] for r in labeled}):
        by_slice[slice_name] = ev_metrics.aggregate(
            [r for r in labeled if r["slice"] == slice_name])
    return rows, agg, by_slice, vector_available


def _simplify_agg(agg: dict) -> dict:
    """C2 baseline format: `{"recall_at_k", "source_name_hit_rate", "n"}`
    instead of the nested `{"mean","n"}` from `ev_metrics.aggregate`."""
    return {
        "recall_at_k": agg["recall_at_k"]["mean"],
        "source_name_hit_rate": agg["source_name_hit_rate"]["mean"],
        "n": agg["total_items"],
    }


def _fmt_mean(d: dict) -> str:
    if not d or d.get("mean") is None:
        return "  —  "
    return f"{d['mean'] * 100:5.1f}%"


def _git_ignores(path: str) -> Optional[bool]:
    """True - git really ignores `path`, False - really tracks it,
    None - could not determine (path outside this repository, git absent).
    Finding 3 (review-round-2): the filename convention `.local.` alone is
    incomplete: `.gitignore` covers only `evals/*.local.json` directly, not
    the repo root and not nested directories (`evals/runs/...`), so we ask git rather than
    guess by pattern."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "check-ignore", "-q", "--",
             str(Path(path).resolve())],
            capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None  # 128 etc.: outside the repo (e.g. tests' tmp_path) or another error


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", required=True, help="Path to the golden-set (JSONL or legacy JSON)")
    parser.add_argument("--db", default="whisper_history.db", help="Path to the SQLite DB (default: whisper_history.db)")
    parser.add_argument("--k", default="8,12", help="Comma-separated list of k (default: 8,12)")
    parser.add_argument("--min-recall", default=_DEFAULT_MIN_RECALL,
                         help=f"k=threshold, comma-separated (default: {_DEFAULT_MIN_RECALL})")
    parser.add_argument("--max-drop", type=float, default=_DEFAULT_MAX_DROP,
                         help=f"Max drop of mean recall_at_k against the baseline (default: {_DEFAULT_MAX_DROP})")
    parser.add_argument("--max-item-regressions", type=int, default=_DEFAULT_MAX_ITEM_REGRESSIONS,
                         help=f"Max number of hit->miss items against the baseline (default: {_DEFAULT_MAX_ITEM_REGRESSIONS})")
    parser.add_argument("--baseline", default=None, help="Baseline (C2) for comparison and the per-item diff")
    parser.add_argument("--write-baseline", default=None, help="Write the current run as a new baseline (C2)")
    parser.add_argument("--rerank", action="store_true",
                        help="Measure WITH the cross-encoder reranker (as the RAG chat does with "
                             "RECALL_RERANK_ENABLED=1). The default is without it; the flag "
                             "is written to the provenance, so a baseline without the reranker and a run with "
                             "it are not silently compared")
    parser.add_argument("--sabotage", choices=["shuffle", "reverse"], default=None,
                         help="Deterministically break the candidate pool before cutting (C6), to test the gate itself")
    parser.add_argument("--no-snapshot", action="store_true",
                         help="Do not take a snapshot (VACUUM INTO); requires --yes-live for a live DB. "
                              "On a NON-live target the protective copy (_scratch_copy) is still "
                              "applied despite this flag (D16); the gate says so at runtime")
    parser.add_argument("--yes-live", action="store_true",
                         help="Allow --no-snapshot on a live DB (Config.DATABASE)")
    parser.add_argument("--json-out", default=None,
                         help="Write the full result to JSON (the path must match "
                              "the *.local.* privacy convention, D10)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Print only the summary line")
    args = parser.parse_args(argv)

    # D10/finding 19: --json-out writes real questions and archive transcription_ids;
    # the only new surface without a privacy convention. We require *.local.* in the name
    # AND (finding 3) check with git directly: the name alone does not prove that the
    # path really falls under some .gitignore rule (the repo root and
    # nested directories like evals/runs/ under `evals/*.local.json` do not
    # fall under it). If the check could not be done (path outside the repo: tests on
    # tmp_path, or git absent), the decision stays with the name convention.
    if args.json_out:
        name_ok = ".local." in Path(args.json_out).name
        git_ignored = _git_ignores(args.json_out)
        if not name_ok or git_ignored is False:
            print(f"[gate] --json-out {args.json_out!r} does not match the privacy convention "
                  "*.local.* (e.g. evals/gate_run.local.json): it writes real questions and "
                  "archive transcription_ids to a path with no gitignore rule.", file=sys.stderr)
            return 2

    if not os.path.exists(args.db):
        print(f"[gate] DB not found: {args.db}", file=sys.stderr)
        return 2

    try:
        ks = _parse_k_list(args.k)
        min_recall = _parse_min_recall(args.min_recall)
    except ValueError as exc:
        print(f"[gate] {exc}", file=sys.stderr)
        return 2

    db = args.db
    if args.no_snapshot:
        if is_live_db(db) and not args.yes_live:
            print(f"[gate] Refused: {db!r} is the live DB (Config.DATABASE). "
                  "--no-snapshot without --yes-live is forbidden: the gate must run on a snapshot.",
                  file=sys.stderr)
            return 2
        search_db = db
    elif is_live_db(db):
        search_db = ev_snapshot.auto_snapshot(
            db, protect=_protected_snapshot_paths(args.baseline, args.write_baseline))
    else:
        search_db = db

    try:
        conn = open_readonly(search_db)
    except Exception as exc:  # noqa: BLE001 - any reason for failing to open (corrupted file etc.) -> 2, not a crash
        print(f"[gate] could not open {search_db}: {exc}", file=sys.stderr)
        return 2
    try:
        golden_items = golden_io.read_golden(args.golden, conn=conn)
    except golden_io.GoldenSetError as exc:
        print(f"[gate] {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[gate] golden-set {args.golden!r}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if not golden_items:
        print(f"[gate] golden-set {args.golden!r} is empty.", file=sys.stderr)
        return 2

    # D6/ADR-003 (finding 7): no measurement holds a WAL-rw connection against
    # the file that the gate itself reports as `db_snapshot`; retrieval.search goes
    # against a temporary copy. The only legal bypass is `--no-snapshot
    # --yes-live`, the same one already documented in the module docstring.
    # D16: the bypass is legal ONLY when the target really is the live DB; the flag
    # pair alone proves nothing (`is_live_db` above checks this only
    # to refuse `--no-snapshot` WITHOUT `--yes-live` on a live DB, line 295;
    # on any NON-live target named by the same flags, a snapshot
    # or an arbitrary file, `_scratch_copy` stays mandatory).
    skip_scratch = args.no_snapshot and args.yes_live and is_live_db(db)
    # Finding 15: `--no-snapshot` silently did not exempt from the protective copy on a
    # NON-live target (D16); the help now says so, and the runtime must too.
    if args.no_snapshot and not skip_scratch:
        print(f"[gate] --no-snapshot: the protective copy (_scratch_copy) is still "
              f"applied - {db!r} is not the live DB (Config.DATABASE), the bypass is "
              "legal only on a live target together with --yes-live.", file=sys.stderr)
    measure_cm = contextlib.nullcontext(search_db) if skip_scratch else _scratch_copy(search_db)

    rows_by_k: dict[int, dict[str, dict]] = {}
    agg_by_k: dict[int, dict] = {}
    by_slice_by_k: dict[int, dict] = {}
    vector_available: Optional[bool] = None
    try:
        with measure_cm as measure_db:
            # C8: a separate read-only connection on the same file as the search
            # (not through retrieval.search: filter_leak is computed from the ready
            # chunks, not inside the ranking), for subtree_ids/record_tags.
            leak_conn = open_readonly(measure_db)
            try:
                for k in ks:
                    rows, agg, by_slice, vec_avail = _run_k(
                        golden_items, measure_db, k, sabotage=args.sabotage,
                        rerank=args.rerank, leak_conn=leak_conn)
                    rows_by_k[k] = {r["id"]: r for r in rows}
                    agg_by_k[k] = agg
                    by_slice_by_k[k] = by_slice
                    if vector_available is None:
                        vector_available = vec_avail
            finally:
                leak_conn.close()
    except Exception as exc:  # noqa: BLE001 - infrastructure (the model did not come up, the snapshot
        # is broken, disk full), not a quality verdict (D3/finding 4): 2, not 1 and not a crash.
        print(f"[gate] failure during measurement: {exc}", file=sys.stderr)
        return 2

    # D2/finding 2: a run where at some k there is no item with
    # status="labeled" is a data error, NEVER a PASS (even under -q). This is
    # exactly what build_golden yields without labelling: all unlabeled -> n=0 at every k.
    empty_ks = [k for k in ks if agg_by_k[k]["total_items"] == 0]
    if empty_ks:
        print(f"[gate] at k={','.join(str(k) for k in empty_ks)} there is no item with "
              "status=\"labeled\" - nothing to compute a verdict on (data error, not PASS).",
              file=sys.stderr)
        return 2

    # D5 (findings 6, 13): the provenance of the current run is what the
    # baseline is checked against BEFORE it is allowed to give a verdict.
    snap_stat = os.stat(search_db)
    current_provenance = {
        "db_snapshot": {"path": os.path.abspath(search_db), "size": snap_stat.st_size,
                         "mtime": snap_stat.st_mtime},
        "vector_available": vector_available,
        "rerank": bool(args.rerank),
        # Wave B debt (`leftovers.md`, Descoped): the gate does not enable `rewrite`
        # with a flag (`retrieval.search` reads the hot env itself), but the run
        # MUST say what it was taken with, otherwise two baselines with different
        # RAG_QUERY_REWRITE are silently merged. We resolve it with the same `env_bool`
        # as retrieval.py:718 (ADR-008: one implementation per flag).
        "rewrite": _settings.env_bool("RAG_QUERY_REWRITE"),
    }

    baseline = None
    if args.baseline:
        try:
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[gate] baseline {args.baseline!r}: {exc}", file=sys.stderr)
            return 2

        # Finding 6 (review-round-2): the provenance must cover the SET being
        # measured, not just the DB; otherwise a baseline taken over another
        # golden-set (other ids/n) is accepted as comparable only because the
        # snapshot and vector_available/rerank matched, and mean_drop is computed
        # against foreign items.
        current_ids = sorted({item_id for k in ks for item_id in rows_by_k[k]})
        base_snap = baseline.get("db_snapshot")
        provenance_ok = (
            isinstance(base_snap, dict)
            and base_snap.get("path") == current_provenance["db_snapshot"]["path"]
            and base_snap.get("size") == current_provenance["db_snapshot"]["size"]
            and base_snap.get("mtime") == current_provenance["db_snapshot"]["mtime"]
            and baseline.get("vector_available") == current_provenance["vector_available"]
            and baseline.get("rerank") == current_provenance["rerank"]
            and sorted((baseline.get("items") or {}).keys()) == current_ids
        )
        if not provenance_ok:
            print(f"[gate] baseline {args.baseline!r} was taken with a different provenance "
                  "(the snapshot, vector_available/rerank or the item set itself does not match "
                  "the current run); comparison refused, no verdict is given (D5). "
                  "Rewrite the baseline with --write-baseline on this set.", file=sys.stderr)
            return 2

    # Finding 1 (review-round-2), story 11: two doors to a PASS that checked
    # nothing. `checked_this_k` records whether at least ONE substantive
    # check really took place at this k: an absolute threshold against a
    # real number (recall_at_k OR, if it is absent,
    # source_name_hit_rate, door (b)) or a comparison with the baseline (mean_drop
    # against a real number, or at least one item with a shared base_row,
    # door (a), where the baseline was taken at another k). If no k passed ANY
    # check, there is no verdict (2), not a silent PASS.
    failures: list[str] = []
    diff_lines: list[str] = []
    unchecked_ks: list[int] = []
    # C8: the sum of `filter_leak` at each k, independent of recall/baseline,
    # is this gate's most important FAIL (the filter let a foreign record through). Items without
    # `category_id`/`tags` leave `filter_leak=None` (they do not take part).
    leak_totals: dict[int, int] = {}
    for k in ks:
        checked_this_k = False
        leak_rows = [r for r in rows_by_k[k].values() if r.get("filter_leak") is not None]
        leak_totals[k] = sum(r["filter_leak"] for r in leak_rows)
        if leak_rows:
            checked_this_k = True
            if leak_totals[k] > 0:
                failures.append(f"k={k}: filter_leak {leak_totals[k]} > 0")

        recall_mean = agg_by_k[k]["recall_at_k"]["mean"]
        hit_rate_mean = agg_by_k[k]["source_name_hit_rate"]["mean"]
        min_r = min_recall.get(k)
        if min_r is not None:
            if recall_mean is not None:
                checked_this_k = True
                if recall_mean < min_r:
                    failures.append(f"k={k}: recall_at_k {recall_mean:.3f} < min-recall {min_r:.3f}")
            elif hit_rate_mean is not None:
                # Door (b): items labelled only with `expected_source_name_contains`
                # (the README recommends this as an alternative more robust to id drift)
                # leave recall_at_k.mean = None on every item; the threshold
                # is then checked against source_name_hit_rate, otherwise this k is not
                # checked at all and a total miss silently passes.
                checked_this_k = True
                if hit_rate_mean < min_r:
                    failures.append(
                        f"k={k}: source_name_hit_rate {hit_rate_mean:.3f} < min-recall {min_r:.3f}")

        if baseline is not None:
            base_agg = (baseline.get("aggregate") or {}).get(str(k)) or {}
            base_recall = base_agg.get("recall_at_k")
            if base_recall is not None and recall_mean is not None:
                checked_this_k = True
                drop = base_recall - recall_mean
                if drop > args.max_drop:
                    failures.append(f"k={k}: mean_drop {drop:.3f} > max-drop {args.max_drop:.3f}")

            base_items = baseline.get("items") or {}
            regressions = 0
            for item_id, row in rows_by_k[k].items():
                base_row = (base_items.get(item_id) or {}).get(str(k))
                if base_row is None:
                    continue
                checked_this_k = True
                was_recall, now_recall = base_row.get("recall"), row.get("recall_at_k")
                was_hit, now_hit = base_row.get("hit"), row.get("hit")
                if was_recall != now_recall or was_hit != now_hit:
                    diff_lines.append(f"{item_id} k={k}: {was_recall}→{now_recall}")
                if was_hit is True and now_hit is False:
                    regressions += 1
            if regressions > args.max_item_regressions:
                failures.append(
                    f"k={k}: item_regressions {regressions} > max-item-regressions {args.max_item_regressions}")

        if not checked_this_k:
            unchecked_ks.append(k)

    if unchecked_ks:
        print(f"[gate] at k={','.join(str(k) for k in unchecked_ks)} there is no substantive "
              "check, neither an absolute threshold (--min-recall) against a computed metric, nor a "
              "comparable record in the baseline for this k; there is no verdict (a "
              "configuration error, not PASS).", file=sys.stderr)
        return 2

    verdict = "PASS" if not failures else "FAIL"

    # D12/finding 15: --write-baseline does NOT write a baseline on a FAIL verdict;
    # otherwise a regressed run silently becomes the new truth for comparison.
    if args.write_baseline and verdict == "PASS":
        all_ids = sorted({item_id for k in ks for item_id in rows_by_k[k]})
        baseline_out = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "db_snapshot": current_provenance["db_snapshot"],
            "vector_available": current_provenance["vector_available"],
            "rerank": current_provenance["rerank"],
            "k": ks,
            "items": {
                item_id: {
                    str(k): {"recall": rows_by_k[k][item_id]["recall_at_k"],
                             "hit": rows_by_k[k][item_id]["hit"]}
                    for k in ks if item_id in rows_by_k[k]
                }
                for item_id in all_ids
            },
            "aggregate": {str(k): _simplify_agg(agg_by_k[k]) for k in ks},
            "by_slice": {
                str(k): {s: _simplify_agg(a) for s, a in by_slice_by_k[k].items()}
                for k in ks
            },
        }
        with open(args.write_baseline, "w", encoding="utf-8") as f:
            json.dump(baseline_out, f, ensure_ascii=False, indent=2, sort_keys=True)
    elif args.write_baseline:
        print(f"[gate] --write-baseline skipped: verdict FAIL "
              f"(baseline {args.write_baseline!r} not overwritten by a regressed run).",
              file=sys.stderr)

    if not args.quiet:
        print()
        print(f"[gate] DB (search): {search_db}" + (f"  (snapshot of {db})" if search_db != db else ""))
        labeled_n = sum(1 for i in golden_items if i["status"] == "labeled")
        print(f"[gate] golden-set: {args.golden} ({len(golden_items)} records, {labeled_n} labeled)")
        print("-" * 60)
        for k in ks:
            agg = agg_by_k[k]
            print(f"k={k:<3} recall_at_k={_fmt_mean(agg['recall_at_k'])}  "
                  f"source_name_hit_rate={_fmt_mean(agg['source_name_hit_rate'])}  "
                  f"n={agg['total_items']}")
            for slice_name, s_agg in by_slice_by_k[k].items():
                print(f"    {slice_name:<8} recall_at_k={_fmt_mean(s_agg['recall_at_k'])}  n={s_agg['total_items']}")
        if diff_lines:
            print("-" * 60)
            print("Diff vs baseline:")
            for line in diff_lines:
                print(f"  {line}")
        print("-" * 60)
        if args.write_baseline and verdict == "PASS":
            print(f"[gate] baseline written to {args.write_baseline}")
        for f_ in failures:
            print(f"  FAIL: {f_}")

    # Configuration is on the same line as the verdict: the finding of 17.09.2026 was
    # exactly that runs without the reranker were read as live numbers (50.0% vs 78.6%
    # at k=8). The line under `-q` is the only one, so the signature must be right here.
    print(f"[gate] {verdict} k={','.join(str(k) for k in ks)}"
          f" [rerank={'on' if current_provenance['rerank'] else 'off'},"
          f" rewrite={'on' if current_provenance['rewrite'] else 'off'},"
          f" filter_leak={sum(leak_totals.values())}]"
          + (f" — {'; '.join(failures)}" if failures else ""))

    if args.json_out:
        out = {
            "db": os.path.abspath(search_db),
            "golden": args.golden,
            "k": ks,
            "verdict": verdict,
            "failures": failures,
            "aggregate": {str(k): {**_simplify_agg(agg_by_k[k]), "filter_leak": leak_totals[k]}
                          for k in ks},
            "by_slice": {
                str(k): {s: _simplify_agg(a) for s, a in by_slice_by_k[k].items()}
                for k in ks
            },
            "per_item": {str(k): list(rows_by_k[k].values()) for k in ks},
            # Without this, two `--json-out` files merged via `evals.compare` carry
            # no mention of which configuration each was taken with (finding
            # 17.09.2026). `compare` reads only aggregate/per_item, so an extra
            # key does not touch it, but the run now carries its signature in the file.
            "provenance": current_provenance,
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
