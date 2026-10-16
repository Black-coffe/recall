"""Eval infrastructure for retrieval/RAG (REMEDIATION_PLAN Wave 3, T6.1).

See evals/README.md for the golden-set format, when to run it and how to keep your own
data local. ``evals.metrics`` is deterministic math (recall@k,
citation-rate), tested offline without API/GPU. ``evals.run_eval`` is a CLI that
really hits the DB and (optionally) the Claude API.
"""
