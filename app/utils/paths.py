"""Shared path traversal protection utility (T1.5, Wave 1).

Previously the `os.path.abspath(...).startswith(...)` / `os.path.realpath(...)` check
was written by hand and differently in every blueprint (documents.py, transcription.py,
telegram.py) — wherever the pattern was forgotten to be copied, there was no protection. Now there is a single
source of truth: `safe_path_within` (one base) and `safe_path_within_any` (several
allowed roots, e.g. uploads + youtube_downloads + recordings).

Contract:
- Resolves both `base` and `candidate` via `os.path.realpath` — expands
  symlinks, normalizes `..`/`.`, works correctly with Windows drive letters
  and UNC paths (`\\\\server\\share\\...`).
- The comparison is case-insensitive on Windows (`os.path.normcase`) — NTFS is
  case-insensitive by default; on POSIX it stays case-sensitive
  (`normcase` is a no-op there).
- The boundary is checked by the path separator (`base + os.sep`), not by a bare
  `startswith`, so that `C:\\foo` does not let through `C:\\foobar\\secret.txt`.
- Does NOT check that `candidate` exists — suitable both for paths that will still be
  created (for example, a file upload target) and for already existing ones.
- Returns the resolved absolute path of `candidate` if it equals `base`
  or lies strictly inside it; otherwise `None` (traversal/symlink-escape/
  path outside the bounds).

NOT used in `mcp_server.py`: that is a separate stdio process, and it deliberately
keeps its own light copy of the same pattern, to avoid pulling the heavy import
of the `app` package (Flask/DB initialization) into a process where startup time is
critical (see memory/mcp-stdio-no-heavy-models.md — "nothing heavy in
mcp_server.py"). If you ever need to sync the logic — sync it
by hand, do not import from here.
"""
from __future__ import annotations

import os


def safe_path_within(base: str | os.PathLike, candidate: str | os.PathLike) -> str | None:
    """Returns the resolved `candidate` if it is within `base`, otherwise None.

    Example:
        docs_dir = "documents"
        target = safe_path_within(docs_dir, os.path.join(docs_dir, filename))
        if target is None:
            raise ValueError("path traversal")
    """
    if not base or not candidate:
        return None
    base_real = os.path.realpath(str(base))
    cand_real = os.path.realpath(str(candidate))
    base_cmp = os.path.normcase(base_real)
    cand_cmp = os.path.normcase(cand_real)
    if cand_cmp == base_cmp or cand_cmp.startswith(base_cmp + os.sep):
        return cand_real
    return None


def safe_path_within_any(
    bases: "list[str | os.PathLike] | tuple[str | os.PathLike, ...]",
    candidate: str | os.PathLike,
) -> str | None:
    """Same as `safe_path_within`, but with several allowed roots
    (e.g. uploads + youtube_downloads + recordings). Returns the resolved
    path on the first match, otherwise None. Empty/falsy items of `bases`
    are skipped.
    """
    for base in bases:
        if not base:
            continue
        result = safe_path_within(base, candidate)
        if result is not None:
            return result
    return None
