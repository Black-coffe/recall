"""Eval-інфраструктура для retrieval/RAG (REMEDIATION_PLAN Волна 3, T6.1).

Дивись evals/README.md — формат golden-set, коли ганяти, як тримати власні
дані локально. ``evals.metrics`` — детермінована математика (recall@k,
citation-rate), тестована офлайн без API/GPU. ``evals.run_eval`` — CLI, що
дійсно ходить у БД і (опційно) Claude API.
"""
