"""Secret-shape detector for tool arguments (stdlib only).

Passwords and API keys must never travel through chat arguments. This module recognises the
shapes a pasted secret usually has, without ever returning or logging the matched value:

- a Gmail app password, shown as four groups of four letters ("abcd efgh ijkl mnop");
- the same password with the spaces removed, or any standalone token of exactly 16 ASCII letters;
- an Anthropic key (the "sk-ant-" prefix).

The bare 16-letter rule is the noisy one ("abcdefghijklmnop" is also a long word in a search
query), so it only runs with `strict=True` (the default, used for write tools and app.py); with
`strict=False` (read tools) only the grouped and the Anthropic shapes are refused.

Imported by mcp_server.py (one check point before any logging) and by app.py.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_GROUPED = re.compile(r"(?<![A-Za-z])[A-Za-z]{4}(?: [A-Za-z]{4}){3}(?![A-Za-z])")
# Not followed by "@": a 16-letter local part of an e-mail address is not a password.
_TOKEN16 = re.compile(r"(?<![A-Za-z])[A-Za-z]{16}(?![A-Za-z@])")
_ANTHROPIC = re.compile(r"sk-ant-[A-Za-z0-9_-]{6,}")


def looks_like_secret(text: str, *, strict: bool = True) -> bool:
    """True when the text has the shape of an app password or an API key. `strict=False` skips
    the bare 16-letter rule."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_GROUPED.search(text) or _ANTHROPIC.search(text)
                or (strict and _TOKEN16.search(text)))


def _walk(value: Any, strict: bool) -> bool:
    if isinstance(value, str):
        return looks_like_secret(value, strict=strict)
    if isinstance(value, Mapping):
        return any(_walk(v, strict) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_walk(v, strict) for v in value)
    return False


def find_secret_args(args: Mapping[str, Any], *, strict: bool = True) -> list[str]:
    """Names of the arguments whose string content (nested strings included) looks like a secret.
    Returns names only, never the values. `strict=False` skips the bare 16-letter rule."""
    if not isinstance(args, Mapping):
        return []
    return [str(name) for name, value in args.items() if _walk(value, strict)]
