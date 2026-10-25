"""Allowed import roots (`RECALL_IMPORT_ROOTS`) and the one check of a path against them.

Stdlib only. `RECALL_IMPORT_ROOTS` is read from the environment at call time: a `;`-separated
list of absolute directories (`;` because `:` clashes with a Windows drive letter). Unset or
empty means no root, so every path is refused ("forbid until allowed").
"""
from __future__ import annotations

import os


class PathNotAllowed(Exception):
    """The path is not inside any allowed root (or no root is configured)."""


def roots() -> list[str]:
    """The configured roots, absolute and normalized; empty when unset."""
    raw = os.environ.get("RECALL_IMPORT_ROOTS", "")
    out: list[str] = []
    for part in raw.split(";"):
        part = part.strip().strip('"')
        if part:
            out.append(os.path.normpath(os.path.abspath(part)))
    return out


def resolve_allowed(path: str) -> str:
    """The real path of ``path`` when it equals a root or lies inside one, else ``PathNotAllowed``.

    Symlinks and `..` are resolved before the comparison; the boundary is the path separator,
    so a prefix sibling (`C:\\docs-evil` next to the root `C:\\docs`) is refused. Existence is
    not checked here."""
    if not path or not isinstance(path, str):
        raise PathNotAllowed(path)
    cand_real = os.path.realpath(path)
    cand_cmp = os.path.normcase(cand_real)
    for root in roots():
        root_cmp = os.path.normcase(os.path.realpath(root))
        if cand_cmp == root_cmp or cand_cmp.startswith(root_cmp.rstrip(os.sep) + os.sep):
            return cand_real
    raise PathNotAllowed(path)
