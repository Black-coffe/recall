"""Import of the meeting_archive corpus into Whisper (Phase 13D).

Takes the meeting markdown files from the old meeting_archive project (the structure
`MM_YYYY/YYYY-MM-DD_*.md` with YAML frontmatter from /indexer) and loads them as
Whisper transcripts. After the import they are queued for auto-enrich (Claude
card) + embed (vectors) - like native transcriptions.

The markdown body (already structured by Claude web in their workflow) goes into
transcript_text. meeting_date is parsed from the file name. The frontmatter (summary/tags)
is removed - our pipeline generates its own structured data.

Idempotency: source_url = the absolute path of the .md; a repeated import skips
files that are already loaded.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from app.db.connection import get_db_connection


logger = logging.getLogger(__name__)

_DIR_RE = re.compile(r"^\d{2}_\d{4}$")          # 01_2026, 12_2025
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")    # in the file name
SOURCE_TYPE = "meeting_archive"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split the YAML frontmatter (--- ... ---) from the body. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:]).lstrip("\n")
            return _parse_frontmatter(fm), body
    return {}, text


def _parse_frontmatter(fm: str) -> dict:
    """Light parse of summary + tags (without full YAML)."""
    meta: dict = {}
    for line in fm.split("\n"):
        if line.startswith("summary:"):
            meta["summary"] = line[len("summary:"):].strip()
        elif line.startswith("tags:"):
            raw = line[len("tags:"):].strip().strip("[]")
            meta["tags"] = [t.strip() for t in raw.split(",") if t.strip()]
    return meta


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def parse_markdown_file(path: str) -> dict:
    """Parse a single .md → {title, meeting_date, body, meta}."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    meta, body = _split_frontmatter(text)
    fname = os.path.basename(path)
    m = _DATE_RE.search(fname)
    meeting_date = m.group(1) if m else None
    title = _title_from_body(body, os.path.splitext(fname)[0])
    return {"title": title, "meeting_date": meeting_date, "body": body, "meta": meta,
            "filename": fname}


def find_meeting_files(root: str) -> list[str]:
    """All .md files in directories of the MM_YYYY format (like the /indexer scope)."""
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    for entry in sorted(os.listdir(root)):
        sub = os.path.join(root, entry)
        if os.path.isdir(sub) and _DIR_RE.match(entry):
            for fn in sorted(os.listdir(sub)):
                if fn.lower().endswith(".md"):
                    found.append(os.path.join(sub, fn))
    return found


def import_directory(db_path: str, root: str) -> dict:
    """Load all meeting files from root. Idempotent (skip by source_url=absolute path).

    Returns {"found", "imported", "skipped", "ids": [...]}.
    """
    files = find_meeting_files(root)
    imported_ids: list[int] = []
    skipped = 0

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        for path in files:
            abspath = os.path.abspath(path)
            # T4.6: a soft-deleted one does not count as a duplicate - allow a repeated
            # import of the same file if the previous record was deleted.
            exists = c.execute(
                "SELECT id FROM transcriptions WHERE source_type = ? AND source_url = ? "
                "AND deleted_at IS NULL",
                (SOURCE_TYPE, abspath),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            try:
                parsed = parse_markdown_file(path)
            except Exception as e:
                logger.warning("[import] failed to parse %s: %s", path, e)
                skipped += 1
                continue
            if not parsed["body"].strip():
                skipped += 1
                continue

            created_at = (parsed["meeting_date"] + " 00:00:00") if parsed["meeting_date"] else None
            cur = c.execute(
                "INSERT INTO transcriptions (source_type, source_name, source_url, "
                "transcript_text, language, meeting_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                (SOURCE_TYPE, parsed["title"], abspath, parsed["body"], "uk",
                 parsed["meeting_date"], created_at),
            )
            imported_ids.append(cur.lastrowid)
        conn.commit()

    logger.info("[import] root=%s: found=%d imported=%d skipped=%d",
                root, len(files), len(imported_ids), skipped)
    return {"found": len(files), "imported": len(imported_ids),
            "skipped": skipped, "ids": imported_ids}
