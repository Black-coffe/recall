"""Reading and validating the golden-set for evals/gate.py (eval-gate, story 01).

The format is plan contract C1 (docs/specs/eval-gate/plan.md): JSONL, one
record per line (modern format), or legacy `{"items": [...]}` / a bare
list (the old `evals/run_eval.py` schema, read without converting the file -
only in memory). This is ONLY reading and normalization; building the set (log
mining, labelling) is a separate story (`evals/build_golden.py`, story 02).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional


class GoldenSetError(ValueError):
    """Invalid golden-set: `evals.gate` catches it and returns exit 2. The single
    error type for `read_jsonl` (D13/D3): file not found, unreadable, a line
    is not a valid JSON object. Every CLI catches this ONE type instead of three."""


_VALID_SLICES = ("calls", "tg", "docs")
_VALID_STATUS = ("labeled", "unlabeled", "negative")

# The same map that C8 (log mining) will use for new
# records; here only for inferring `slice` in legacy records.
_SOURCE_TYPE_TO_SLICE = {"telegram": "tg", "document": "docs"}


def _slice_from_source_type(source_type: Optional[str]) -> str:
    if not source_type:
        return "calls"
    return _SOURCE_TYPE_TO_SLICE.get(source_type, "calls")


def _where(line_no: Optional[int]) -> str:
    return f" (line {line_no})" if line_no else ""


def _normalize(item: dict, *, source: str, default_status: str,
               line_no: Optional[int] = None) -> dict:
    item_id = item.get("id")
    if not item_id:
        raise GoldenSetError(f"golden-set: record without 'id'{_where(line_no)}")
    if not item.get("question"):
        raise GoldenSetError(f"golden-set {item_id!r}: no 'question'{_where(line_no)}")

    slice_ = item.get("slice") or "calls"
    if slice_ not in _VALID_SLICES:
        raise GoldenSetError(
            f"golden-set {item_id!r}: invalid slice {slice_!r}{_where(line_no)}")

    status = item.get("status") or default_status
    if status not in _VALID_STATUS:
        raise GoldenSetError(
            f"golden-set {item_id!r}: invalid status {status!r}{_where(line_no)}")

    # directions-projects-tags-08 (C8): `tags` is an optional filter axis
    # next to `category_id`, strip + drop empties (same approach as
    # `categories.parse_tags`, but without touching the DB: only raw
    # normalization of the golden record's strings). `expected_leak` only documents
    # the item's intent (the metric always FAILs on `filter_leak > 0` regardless of
    # the value), so the only valid value is `0`; anything else is a
    # normalization error, not a silent default.
    expected_leak = item.get("expected_leak")
    if expected_leak is not None and expected_leak != 0:
        raise GoldenSetError(
            f"golden-set {item_id!r}: invalid expected_leak {expected_leak!r} "
            f"(the only valid value is 0){_where(line_no)}")

    return {
        "id": item_id,
        "question": item["question"],
        "slice": slice_,
        "category_id": item.get("category_id"),
        "tags": [t.strip() for t in (item.get("tags") or []) if isinstance(t, str) and t.strip()],
        "expected_transcription_ids": item.get("expected_transcription_ids") or [],
        "expected_source_name_contains": item.get("expected_source_name_contains") or [],
        "expected_facts": item.get("expected_facts") or [],
        "notes": item.get("notes", ""),
        "source": item.get("source") or source,
        "status": status,
        "candidates": item.get("candidates") or [],
    }


def _lookup_source_type(conn: Optional[sqlite3.Connection],
                         transcription_ids: list) -> Optional[str]:
    if conn is None or not transcription_ids:
        return None
    try:
        row = conn.execute(
            "SELECT source_type FROM transcriptions WHERE id = ?",
            (transcription_ids[0],),
        ).fetchone()
    except sqlite3.Error as exc:
        # Do not swallow silently (CLAUDE.md: no bare except without a log; finding
        # 18): a broken snapshot / missing table becomes visible instead of a silent
        # default slice="calls".
        print(f"[golden_io] could not determine source_type for "
              f"{transcription_ids[0]}: {exc}", file=sys.stderr)
        return None
    return row[0] if row is not None else None


def read_jsonl(path: str) -> list[dict]:
    """Shared JSONL reader for ALL eval CLIs (D13, finding 21): one
    JSON object per non-empty line, WITHOUT golden-set-specific normalization
    (`id`/`slice`/`status`/... stay the layer of `read_golden`/`_normalize` for
    the golden-set and each CLI's own schema for other sets, e.g.
    tasks-golden). The contract is fixed: `read_jsonl(path) -> list[dict]`.

    Raises a SINGLE error type, `GoldenSetError` (D3): file not found,
    unreadable, or a line is not a valid JSON object (with the line number);
    every CLI catches one `except GoldenSetError` and maps it to exit 2."""
    p = Path(path)
    if not p.exists():
        raise GoldenSetError(f"golden-set {path}: file not found")
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldenSetError(f"golden-set {path}: could not read: {exc}") from exc

    items = []
    for line_no, raw in enumerate(raw_text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoldenSetError(
                f"golden-set {path}: invalid JSON at line {line_no}: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise GoldenSetError(
                f"golden-set {path}: line {line_no} must be an object")
        items.append(obj)
    return items


def _read_jsonl(path: Path) -> list[dict]:
    """Golden-set-specific layer over `read_jsonl`: normalizes each raw
    object (`id`/`slice`/`status`/...). The position in the list (1-based) is not
    the literal file line number (empty lines drop out before normalization),
    but JSON errors themselves (`read_jsonl`) already carry the exact line number."""
    raw_items = read_jsonl(str(path))
    return [_normalize(obj, source="manual", default_status="labeled", line_no=i)
            for i, obj in enumerate(raw_items, start=1)]


def _read_legacy(path: Path, conn: Optional[sqlite3.Connection]) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw_items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw_items, list):
        raise GoldenSetError(
            f"golden-set {path}: expected a list or {{'items': [...]}}")

    out = []
    for item in raw_items:
        item = dict(item)
        if not item.get("slice"):
            expected_ids = item.get("expected_transcription_ids") or []
            source_type = _lookup_source_type(conn, expected_ids)
            item["slice"] = _slice_from_source_type(source_type)
        item.setdefault("status", "labeled")
        out.append(_normalize(item, source="legacy", default_status="labeled"))
    return out


def read_golden(path: str, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Read a golden-set: JSONL (C1, `.jsonl`) or legacy JSON (anything else).

    `conn` is an optional read-only connection to a snapshot; used only
    to infer `slice` in legacy records without an explicit field (from the `source_type`
    of the first expected transcript); without it such records fall to
    `"calls"`. JSONL records without `slice` also fall to `"calls"` (C1 does not
    infer it from data for the new format: the builder must set it).
    """
    p = Path(path)
    if p.suffix == ".jsonl":
        return _read_jsonl(p)
    return _read_legacy(p, conn)
