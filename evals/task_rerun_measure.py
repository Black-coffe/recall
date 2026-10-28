#!/usr/bin/env python
"""Re-run measurement for task proposals (ADR-056, task-approval story 02).

Question: when extraction runs again over a record, does the EXACT text key
(`task_proposals.text_key`) recognise a task the owner already decided on, or does the
model rephrase it so the tombstone misses and the task is proposed a second time?

The script re-extracts a sample of records that already hold meeting-card tasks
(`action_items.source IS NULL`; Telegram threads and comments are not sampled), WRITES
NOTHING, and lists each new candidate next to its nearest existing task of the same record:
`exact_hit` (same `text_key`) and the token Jaccard over `commitments._tokens`. The verdict
column is left empty for a hand pass. The summary gives candidates, exact hits and the
non-compliance share (candidates missing `for_whom`/`confidence`/`importance`).

Jaccard is a report column only; production matching stays exact.

The cost estimate is printed first; `--dry-run` stops there. Without
`ANTHROPIC_API_KEY` a real run refuses (exit 2). Every extraction failing -> exit 3.

CLI:
    python -m evals.task_rerun_measure --db evals/snapshots/x.db --sample 30 --dry-run
    python -m evals.task_rerun_measure --db evals/snapshots/x.db --sample 30 --out docs/specs/task-approval/rerun.local.md
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path
from typing import Callable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.graph_links import open_readonly  # noqa: E402
from app.services import commitments, pricing, task_proposals  # noqa: E402

# A rough token estimate: mixed Cyrillic/Latin text runs at about 3 characters per token;
# the card prompt itself and the JSON answer are added per record.
CHARS_PER_TOKEN = 3.0
PROMPT_TOKENS = 2_000
OUTPUT_TOKENS = 2_500
FIELDS = ("for_whom", "confidence", "importance")

Extractor = Callable[[dict], list]


def sample_records(conn, n: int, seed: int) -> list[dict]:
    """Records with meeting-card tasks, a random sample of `n` (seeded, reproducible)."""
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT a.transcription_id FROM action_items a "
        "JOIN transcriptions t ON t.id = a.transcription_id "
        "WHERE a.source IS NULL AND t.deleted_at IS NULL "
        "AND COALESCE(t.polished_text, t.transcript_text, '') <> '' "
        "ORDER BY a.transcription_id").fetchall()]
    picked = sorted(random.Random(seed).sample(ids, min(n, len(ids))))
    out = []
    for tid in picked:
        t = conn.execute(
            "SELECT id, polished_text, transcript_text, segments, meeting_date, created_at "
            "FROM transcriptions WHERE id = ?", (tid,)).fetchone()
        existing = [r[0] for r in conn.execute(
            "SELECT task FROM action_items WHERE transcription_id = ? AND source IS NULL "
            "ORDER BY id", (tid,)).fetchall()]
        out.append({
            "id": tid,
            "text": t["polished_text"] or t["transcript_text"] or "",
            "segments": t["segments"],
            "meeting_date": t["meeting_date"] or (str(t["created_at"])[:10] if t["created_at"] else None),
            "existing": existing,
        })
    return out


def estimate(records: list[dict], model: str) -> dict:
    chars = sum(len(r["text"]) for r in records)
    tokens_in = int(chars / CHARS_PER_TOKEN) + PROMPT_TOKENS * len(records)
    tokens_out = OUTPUT_TOKENS * len(records)
    return {"records": len(records), "chars": chars, "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "usd": pricing.estimate_cost(model, tokens_in, tokens_out)}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def compare(record: dict, candidates: list) -> list[dict]:
    """One report row per candidate: nearest existing task, exact hit, Jaccard, fields."""
    existing = [(t, task_proposals.text_key(t), commitments._tokens(t)) for t in record["existing"]]
    rows = []
    for raw in candidates or []:
        if not isinstance(raw, dict) or not str(raw.get("task") or "").strip():
            continue
        task = str(raw["task"]).strip()
        key = task_proposals.text_key(task)
        toks = commitments._tokens(task)
        best_text, best_j, hit = "", 0.0, False
        for text, ekey, etoks in existing:
            if ekey == key:
                best_text, best_j, hit = text, 1.0, True
                break
            j = _jaccard(toks, etoks)
            if j > best_j or not best_text:
                best_text, best_j = text, j
        norm = task_proposals.normalize_candidate(raw)
        rows.append({
            "record_id": record["id"], "candidate": task, "nearest": best_text,
            "exact_hit": hit, "jaccard": round(best_j, 3),
            "for_whom": norm["for_whom"], "confidence": norm["confidence"],
            "importance": norm["importance"],
            "missing_fields": any(f not in raw for f in FIELDS),
        })
    return rows


def _cell(value) -> str:
    return str("" if value is None else value).replace("|", "\\|").replace("\n", " ").strip()


def render(rows: list[dict], *, records: int, errors: list[str], est: dict) -> str:
    n = len(rows)
    hits = sum(r["exact_hit"] for r in rows)
    missing = sum(r["missing_fields"] for r in rows)
    lines = [
        "# Task re-run measurement",
        "",
        f"- records sampled: {records}; failed extractions: {len(errors)}",
        f"- estimated cost: ${est['usd']:.2f} ({est['chars']} characters)",
        f"- candidates: {n}",
        f"- exact hits: {hits} ({(hits / n if n else 0):.1%})",
        f"- non-compliance (missing for_whom/confidence/importance): {missing} "
        f"({(missing / n if n else 0):.1%})",
        "",
        "| record id | candidate | nearest existing | exact_hit | jaccard | for_whom | confidence "
        "| missing_fields | verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(_cell(v) for v in (
            r["record_id"], r["candidate"], r["nearest"], r["exact_hit"], r["jaccard"],
            r["for_whom"], r["confidence"], r["missing_fields"], "")) + " |")
    if errors:
        lines += ["", "## Failed extractions", ""] + [f"- {_cell(e)}" for e in errors]
    return "\n".join(lines) + "\n"


def _default_extractor(db_path: str, model: Optional[str], effort: str) -> Extractor:
    """The real meeting-card call, with the owner's names, the same inputs as enrichment."""
    from app.services import enrichment, text_polishing

    names = task_proposals.owner_names(db_path)

    def run(record: dict) -> list:
        conn = open_readonly(db_path)
        try:
            diarized = enrichment._diarized_text_for(conn, record["id"], record["segments"])
        finally:
            conn.close()
        card = text_polishing.extract_meeting_card(
            record["text"], diarized_text=diarized, model=model, effort=effort,
            meeting_date=record["meeting_date"], owner_names=names)
        return card.get("action_items") or []
    return run


def main(argv: Optional[list[str]] = None, extractor: Optional[Extractor] = None) -> int:
    from app.services import models

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="DB to read (a snapshot is preferred); opened read-only")
    ap.add_argument("--sample", type=int, default=30, help="how many records to re-extract")
    ap.add_argument("--seed", type=int, default=56, help="sampling seed (reproducible sample)")
    ap.add_argument("--model", default=None, help="Claude model; default = the archive default")
    ap.add_argument("--effort", default="low", help="extraction effort (enrichment backfill uses low)")
    ap.add_argument("--out", default=None, help="markdown report path")
    ap.add_argument("--dry-run", action="store_true", help="print the estimate and stop")
    args = ap.parse_args(argv)

    if not Path(args.db).is_file():
        print(f"no such DB: {args.db}", file=sys.stderr)
        return 2
    conn = open_readonly(args.db)
    try:
        records = sample_records(conn, args.sample, args.seed)
    finally:
        conn.close()

    model = args.model or models.get_default_model()
    est = estimate(records, model)
    print(f"sample: {est['records']} records, {est['chars']} characters, "
          f"~{est['tokens_in']} input + {est['tokens_out']} output tokens, "
          f"estimated cost ${est['usd']:.2f} ({model})")
    if args.dry_run:
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set: the re-run needs a working key "
              "(replace it on the /connect/anthropic_key/ page first)", file=sys.stderr)
        return 2
    if not args.out:
        print("--out is required for a real run", file=sys.stderr)
        return 2

    run = extractor or _default_extractor(args.db, args.model, args.effort)
    rows: list[dict] = []
    errors: list[str] = []
    for rec in records:
        try:
            candidates = run(rec)
        except Exception as e:      # one failed record must not lose the rest of the sample
            errors.append(f"record {rec['id']}: {type(e).__name__}: {e}")
            continue
        rows += compare(rec, candidates)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(render(rows, records=len(records), errors=errors, est=est),
                              encoding="utf-8")
    print(f"report: {args.out} ({len(rows)} candidates, {len(errors)} failed records)")
    if records and len(errors) == len(records):
        print("every extraction failed: check the key and the network", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
