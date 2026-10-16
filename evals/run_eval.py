#!/usr/bin/env python
"""Eval harness for retrieval/RAG (REMEDIATION_PLAN Wave 3, T6.1).

Runs a golden-set of questions against the real archive (``app/services/retrieval.py``
+ ``app/services/rag.py``) and computes recall@k / citation presence / (optionally)
LLM-judge relevance. The purpose is to catch regressions BEFORE a release or before/
after a change of prompt, chunking or model, not to run in CI on every commit (see
evals/README.md).

Examples:
    # Retrieval only (free, no ANTHROPIC_API_KEY, no GPU if
    # embeddings are unavailable - FTS fallback):
    python -m evals.run_eval --golden evals/golden_set.example.json --retrieval-only

    # Full run: retrieval + answer generation (Claude API, paid):
    python -m evals.run_eval --golden evals/golden_set.local.json --db whisper_history.db

    # + LLM-judge relevance (an ADDITIONAL Claude call per question,
    # OPTIONAL, off by default):
    python -m evals.run_eval --golden evals/golden_set.local.json --llm-judge
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Allows running both as `python evals/run_eval.py` and as `python -m evals.run_eval`
# without installing the package: we insert the project root into sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals import metrics as ev_metrics  # noqa: E402


def _load_golden_set(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(
            f"Golden-set {path!r} must be either a list of records or "
            f'an object with an "items" key (a list).'
        )
    return items


def _run_retrieval(db_path: str, item: dict, top_k: int, rerank: bool = False) -> list[dict]:
    from app.services import retrieval

    res = retrieval.search(
        db_path, item["question"], top_k=top_k,
        category_id=item.get("category_id"),
        rerank=rerank,
    )
    return res["chunks"]


def _run_answer(db_path: str, item: dict, top_k: int, model: Optional[str],
                 timeout: float) -> dict:
    from app.services import rag

    return rag.answer_question(
        db_path, item["question"], top_k=top_k, model=model,
        timeout=timeout, category_id=item.get("category_id"),
    )


# --- LLM-judge (optional, paid - off by default) ---

_JUDGE_PROMPT = """Ти оцінюєш якість відповіді RAG-системи по архіву мітингів.

ПИТАННЯ: {question}

ВІДПОВІДЬ СИСТЕМИ:
{answer}

ОЧІКУВАНІ ФАКТИ (мають бути присутні у відповіді, якщо список непорожній):
{expected_facts}

Оціни відповідь за шкалою 1-5, де:
1 = не відповідає на питання / вигадана інформація (галюцинація)
3 = частково відповідає, бракує деталей або точності
5 = повна, точна відповідь, що покриває очікувані факти

Відповідай СУВОРО у форматі (без пояснень поза цим форматом):
SCORE: <1-5>
REASON: <одне речення чому>"""


def _get_judge_client():
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set: the LLM-judge requires a real "
            "Claude API call. Either remove --llm-judge or set the key."
        )
    return anthropic.Anthropic(api_key=api_key)


def _llm_judge_score(client, item: dict, answer: str, model: Optional[str]) -> dict:
    """One Claude-judge call. Goes through app/services/models.py (model ID)
    and app/services/claude_retry.py (retry on 429/5xx), consistent with T6.2/T6.3."""
    from app.services import claude_retry
    from app.services.models import get_default_model, supports_adaptive_thinking

    judge_model = model or get_default_model()
    prompt = _JUDGE_PROMPT.format(
        question=item["question"],
        answer=answer or "(порожня відповідь)",
        expected_facts="\n".join(f"- {f}" for f in (item.get("expected_facts") or []))
        or "(не задано)",
    )
    kwargs = dict(
        model=judge_model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    if supports_adaptive_thinking(judge_model):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "low"}

    def _call():
        return client.messages.create(**kwargs)

    result = claude_retry.call_with_retry(_call, what="eval-llm-judge")
    text = "".join(b.text for b in result.content if b.type == "text")

    score = None
    reason = ""
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("SCORE:"):
            try:
                score = int("".join(c for c in line.split(":", 1)[1] if c.isdigit() or c == "-"))
            except ValueError:
                score = None
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return {"score": score, "reason": reason, "raw": text}


def _print_table(agg: dict, per_item: list[dict], judge_scores: Optional[list[dict]]) -> None:
    print()
    print(f"Golden-set: {agg['total_items']} records")
    print("-" * 60)

    def _fmt(row_name: str, d: dict, pct: bool = True) -> None:
        if d["n"] == 0:
            print(f"{row_name:<28} -  (no records with this criterion)")
            return
        val = d["mean"] * 100 if pct else d["mean"]
        unit = "%" if pct else ""
        print(f"{row_name:<28} {val:6.1f}{unit}  (n={d['n']})")

    _fmt("recall@k (source share)", agg["recall_at_k"])
    _fmt("source_name_hit_rate", agg["source_name_hit_rate"])
    _fmt("citation_rate", agg["citation_rate"])
    _fmt("citations_valid_rate", agg["citations_valid_rate"])

    if judge_scores:
        valid = [j["score"] for j in judge_scores if j.get("score") is not None]
        if valid:
            print(f"{'llm_judge_score (1-5)':<28} {sum(valid) / len(valid):6.2f}   (n={len(valid)})")
    print("-" * 60)

    print("\nPer record:")
    for r in per_item:
        parts = [f"  {r['id']}:"]
        if r["recall_at_k"] is not None:
            parts.append(f"recall={r['recall_at_k']:.2f}")
        if r["source_name_hit"] is not None:
            parts.append(f"name_hit={r['source_name_hit']}")
        if r["has_citation"] is not None:
            parts.append(f"citation={r['has_citation']}")
        if r["citations_valid"] is not None and not r["citations_valid"]:
            parts.append("CITATIONS_OUT_OF_RANGE!")
        print(" ".join(parts))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", required=True, help="Path to the golden-set JSON (see golden_set.example.json)")
    parser.add_argument("--db", default="whisper_history.db", help="Path to the archive SQLite DB (default: whisper_history.db)")
    parser.add_argument("--top-k", type=int, default=8, help="top_k for retrieval.search (default: 8)")
    parser.add_argument("--model", default=None, help="Override the Claude model (default: app/services/models.get_default_model())")
    parser.add_argument("--timeout", type=float, default=180.0, help="Timeout for the rag.answer_question call (sec)")
    parser.add_argument("--retrieval-only", action="store_true",
                         help="retrieval.search only, no Claude call (free, no ANTHROPIC_API_KEY)")
    parser.add_argument("--llm-judge", action="store_true",
                         help="ADDITIONALLY score each answer with a separate Claude-judge call "
                              "(paid, OPTIONAL, off by default; ignored with --retrieval-only)")
    parser.add_argument("--rerank", action="store_true",
                         help="T6.4: enable the local cross-encoder rerank for this run "
                              "(bge-reranker-v2-m3, lazy-load on first call). With --retrieval-only "
                              "it is passed directly to retrieval.search(rerank=True); without it, it sets "
                              "RECALL_RERANK_ENABLED=1 BEFORE importing app.services.rag, so that rag.answer_question "
                              "also reranks (the same path as the RAG chat). Before/after comparison: run "
                              "the same golden-set with and without --rerank and compare recall@k/citation_rate.")
    parser.add_argument("--json-out", default=None, help="Write the full result (per-item + aggregates) to a JSON file")
    args = parser.parse_args(argv)

    if args.rerank and not args.retrieval_only:
        # rag.py reads RECALL_RERANK_ENABLED into a module constant AT IMPORT
        # (like COPILOT_ENABLED), so we set it BEFORE the first `from app.services import rag`
        # (which happens lazily inside _run_answer, so before this line the module
        # has not been imported even once in this process).
        os.environ["RECALL_RERANK_ENABLED"] = "1"

    if not os.path.exists(args.db):
        print(f"[eval] DB not found: {args.db}. Pass --db with the path to your whisper_history.db.", file=sys.stderr)
        return 2

    golden_items = _load_golden_set(args.golden)
    if not golden_items:
        print(f"[eval] Golden-set {args.golden!r} is empty.", file=sys.stderr)
        return 2

    per_item: list[dict] = []
    judge_scores: list[dict] = []
    judge_client = _get_judge_client() if (args.llm_judge and not args.retrieval_only) else None

    for item in golden_items:
        t0 = time.time()
        chunks = _run_retrieval(args.db, item, args.top_k, rerank=args.rerank)
        answer_text = None
        if not args.retrieval_only:
            ans = _run_answer(args.db, item, args.top_k, args.model, args.timeout)
            answer_text = ans.get("answer", "")
            # rag.answer_question does its OWN retrieval.search inside; so as not to
            # pay for the search twice and to compute metrics on the SAME chunks that
            # actually went into the model prompt, we take "sources" from its answer.
            chunks = ans.get("sources", chunks)

        row = ev_metrics.evaluate_item(item, chunks, answer_text)
        elapsed = time.time() - t0
        row["elapsed_s"] = round(elapsed, 2)
        per_item.append(row)

        if judge_client is not None:
            judge_scores.append(_llm_judge_score(judge_client, item, answer_text or "", args.model))

        print(f"[eval] {item.get('id')}: retrieved={len(chunks)} "
              f"{'answer_len=' + str(len(answer_text or '')) if answer_text is not None else '(retrieval-only)'} "
              f"({elapsed:.1f}s)")

    agg = ev_metrics.aggregate(per_item)
    _print_table(agg, per_item, judge_scores or None)

    if args.json_out:
        out = {"aggregate": agg, "per_item": per_item}
        if judge_scores:
            out["llm_judge"] = judge_scores
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n[eval] Full result written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
