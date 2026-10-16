"""SQLite FTS5 helpers (Phase 4.2).

Pure functions with no dependency on Flask/DB.
"""
import re


def sanitize_fts_query(query: str) -> str:
    """Turns free text into a safe FTS5 MATCH expression.

    Each word is wrapped in quotes (FTS5 phrase syntax) — this:
    - escapes all FTS5 special characters (* " : etc.);
    - turns the query into an AND over the words (the usual search behaviour).

    Returns '' if there are no valid words — the caller must skip the FTS search.
    """
    if not query:
        return ''
    words = re.findall(r'\w+', query, re.UNICODE)
    if not words:
        return ''
    return ' '.join(f'"{w}"' for w in words)
